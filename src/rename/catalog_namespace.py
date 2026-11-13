"""One held namespace for all Work/Series Catalog writers and media preflights.

Snapshots contain bytes observed under the actual OS lease, not caller-supplied
JSON digests. They are observations, never permission to execute a Plan.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time

from .file_lock import FileLockBusyError, LockResource, serialized_resource_guard

_held = threading.local()
_MAX_RECORDS = 10000
_MAX_FILE = 4 * 1024 * 1024
_MAX_BYTES = 64 * 1024 * 1024


def _error(catalog, message):
    from .work_catalog import WorkCatalogError
    from .series_catalog import SeriesCatalogError
    return (SeriesCatalogError if _backend(catalog) == "series" else WorkCatalogError)(message)


def _owner():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return os.getpid(), threading.get_ident(), id(task) if task else None


def _root_key(path):
    from .work_catalog import _path_key
    return _path_key(Path(path))


def _safe_parents(catalog, path):
    for parent in (Path(path), *Path(path).parents):
        if os.path.lexists(parent):
            value = parent.lstat()
            if not stat.S_ISDIR(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400:
                raise _error(catalog, '作品登记目录必须是普通目录，不能经过链接或重解析点')


def _identity(path):
    value = path.lstat()
    return value.st_dev, value.st_ino


@dataclass(frozen=True)
class _Lease:
    backend: str
    root: str
    key: str
    owner: tuple
    identities: tuple
    read_only: bool = False
    issued: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _LeaseLife:
    lease: _Lease
    binding: tuple
    issued: dict


_live_namespace_leases = {}


def _lease_binding(lease):
    return (lease.backend, lease.root, lease.key, lease.owner, lease.identities, lease.read_only)


@contextmanager
def _lease_lifetime(lease):
    """One issuance/revocation boundary for ordinary leases.

    Optional fields on a copy cannot prove that an actual namespace was
    acquired. Retain the exact issued instance and its original binding until
    this boundary exits; revocation precedes namespace and OS teardown.
    """
    if (type(lease) is not _Lease or lease.owner != _owner()
            or getattr(_held, 'lease', None) is not None):
        raise ValueError('作品登记租约不能嵌套或跨执行者发行')
    life = _LeaseLife(lease, _lease_binding(lease), lease.issued)
    _live_namespace_leases[id(lease)] = life
    _held.lease = lease
    try:
        yield lease
    finally:
        _live_namespace_leases.pop(id(lease), None)
        life.issued.clear()
        _held.lease = None


def lock_resources(catalog, key):
    from .file_lock import LockResource
    catalog._path(key)
    return (LockResource(catalog, catalog.lock_root / '.namespace.lock', _backend(catalog) + '_catalog_namespace', True),
            LockResource(catalog, catalog.lock_root / f'{key}.lock', _backend(catalog) + '_catalog_key:' + key, True))


def require_lease(catalog, key):
    catalog._path(key)
    lease = getattr(_held, 'lease', None)
    if (type(lease) is not _Lease or lease.root != _root_key(catalog.root)
            or lease.backend != _backend(catalog) or lease.key != key or lease.owner != _owner()):
        raise _error(catalog, '作品登记缺少当前目录和作品键的实际持锁租约')
    life = _live_namespace_leases.get(id(lease))
    if (life is None or life.lease is not lease or life.binding != _lease_binding(lease)
            or life.issued is not lease.issued):
        raise _error(catalog, '作品登记租约不是当前实际发行的原对象或已经退出')
    _safe_parents(catalog, catalog.lock_root)
    resources = lock_resources(catalog, key)
    paths = (catalog.root, *(row.path for row in resources))
    if tuple(_identity(path) for path in paths) != lease.identities:
        raise _error(catalog, '作品登记持锁目录或锁文件身份已变化')
    return lease




@contextmanager
def locked(catalog, key, *, wait_seconds=0.0, read_only=False):
    if type(read_only) is not bool:
        raise _error(catalog, '登记读写模式无效')
    with nullcontext() if read_only else mutation_scope(catalog, wait_seconds=wait_seconds):
        with _locked_namespace(catalog, key, wait_seconds=wait_seconds, read_only=read_only) as lease:
            yield lease


@contextmanager
def _locked_namespace(catalog, key, *, wait_seconds=0.0, read_only=False):
    catalog._path(key)
    if getattr(_held, 'lease', None) is not None:
        raise _error(catalog, '已持有作品登记锁；不支持嵌套取得另一租约')
    _safe_parents(catalog, catalog.lock_root)
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    # Only acquisition is retried: exceptions in the caller must not re-yield.
    stack = ExitStack()
    while True:
        try:
            for resource in lock_resources(catalog, key):
                stack.enter_context(serialized_resource_guard(resource,
                    owner={'kind': _backend(catalog) + '_catalog', 'key': key}))
            break
        except FileLockBusyError as exc:
            stack.close()
            if time.monotonic() >= deadline:
                raise _error(catalog, '作品登记已有任务占用共同目录锁，请稍后重试') from exc
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        except BaseException:
            stack.close()
            raise
    with stack:
        _safe_parents(catalog, catalog.lock_root)
        paths = (catalog.root, *(row.path for row in lock_resources(catalog, key)))
        lease = _Lease(_backend(catalog), _root_key(catalog.root), key, _owner(), tuple(_identity(p) for p in paths), read_only=read_only)
        with _lease_lifetime(lease):
            if require_lease(catalog, key) is not lease:
                raise _error(catalog, '作品登记实际租约发行校验已断开')
            yield lease


@dataclass(frozen=True)
class NamespaceSnapshot:
    root: str
    records: tuple[tuple[str, bytes], ...]
    backend: str = 'work'

    @property
    def fingerprint(self):
        rows = [(name, hashlib.sha256(raw).hexdigest()) for name, raw in self.records]
        return hashlib.sha256(json.dumps([self.backend, self.root, rows], ensure_ascii=False).encode('utf-8')).hexdigest()


def _names(catalog):
    names = []
    for path in catalog.root.iterdir():
        if path.name.casefold().startswith(_backend(catalog) + '-') and path.name.casefold().endswith('.json'):
            names.append(path.name)
            if len(names) > _MAX_RECORDS:
                raise _error(catalog, '作品登记完整集合超过安全读取上限')
    return tuple(sorted(names))


def observe(catalog, key):
    require_lease(catalog, key)
    names, rows, total = _names(catalog), [], 0
    try:
        for name in names:
            path = catalog.root / name
            before = path.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or getattr(before, 'st_file_attributes', 0) & 0x400
                    or before.st_size > _MAX_FILE):
                raise _error(catalog, '作品登记格式不安全或超过完整读取上限')
            with path.open('rb') as stream:
                opened = os.fstat(stream.fileno())
                raw = stream.read(_MAX_FILE + 1)
            after = path.lstat()
            signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
            if (signature(before) != signature(opened) or signature(before) != signature(after)
                    or len(raw) != before.st_size):
                raise _error(catalog, '作品登记快照读取期间发生变化')
            total += len(raw)
            if total > _MAX_BYTES:
                raise _error(catalog, '作品登记完整快照超过安全容量')
            payload = json.loads(raw.decode('utf-8'))
            _validate_record(catalog, payload, path.stem)
            _namespace_fields(catalog, payload)
            rows.append((name, raw))
        if names != _names(catalog):
            raise _error(catalog, '作品登记完整集合在快照期间变化')
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error(catalog, '作品登记损坏或无法完整读取') from exc
    require_lease(catalog, key)
    return NamespaceSnapshot(_root_key(catalog.root), tuple(rows), _backend(catalog))


def verify(catalog, key, snapshot):
    if type(snapshot) is not NamespaceSnapshot:
        raise _error(catalog, '作品登记快照类型无效')
    if observe(catalog, key) != snapshot:
        raise _error(catalog, '作品登记快照已变化，请重新核查')


def _namespace_fields(catalog, record):
    from .work_catalog import _provider_mapping_key
    root = record.get('canonical_root')
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise _error(catalog, '作品登记规范根格式损坏')
    # Do not turn Win32 trailing-dot/space aliases into distinct identities.
    if any(part.endswith(('.', ' ')) for part in Path(root).parts):
        raise _error(catalog, '作品登记目录包含不明确的路径别名')
    providers = (catalog._namespace_providers(record) if _backend(catalog) == 'series'
                 else record.get('providers'))
    if not isinstance(providers, list) or not providers:
        raise _error(catalog, '作品登记资料条目列表损坏')
    keys = tuple(_provider_mapping_key(row) for row in providers)
    if any(item is None for item in keys):
        raise _error(catalog, '作品登记资料条目身份损坏')
    return _root_key(root), set(keys)


@dataclass(frozen=True)
class NamespaceValidation:
    snapshot: NamespaceSnapshot
    key: str
    candidate: bytes | None


def _encoded(candidate):
    return None if candidate is None else json.dumps(candidate, ensure_ascii=False,
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def validate(catalog, key, candidate):
    lease = require_lease(catalog, key)
    raw = _encoded(candidate)
    if raw is not None:
        candidate = json.loads(raw)
        _validate_record(catalog, candidate, key)
        _namespace_fields(catalog, candidate)
    snapshot = observe(catalog, key)
    before = {Path(name).stem: json.loads(raw) for name, raw in snapshot.records}
    if (candidate is not None and key in before and _backend(catalog) == 'series'
            and before[key].get('domain') != candidate.get('domain')):
        raise _error(catalog, '已登记作品的媒体域不能随候选静默改变')
    if candidate is None:
        if key not in before:
            raise _error(catalog, '待移除的作品登记候选不存在')
    else:
        root, providers = _namespace_fields(catalog, candidate)
        for other, record in before.items():
            if other == key:
                continue
            other_root, other_providers = _namespace_fields(catalog, record)
            if root == other_root or providers.intersection(other_providers):
                raise _error(catalog, '作品冲突：规范目录或资料条目已被其他作品登记占用')
    # Only this key changes: unrelated existing conflicts are unchanged, and
    # deletion cannot introduce a conflict. This permits one-at-a-time repair.
    proof = NamespaceValidation(snapshot, key, raw)
    lease.issued[id(proof)] = proof
    return proof


def consume(catalog, key, candidate, proof):
    lease = require_lease(catalog, key)
    if (type(proof) is not NamespaceValidation or lease.issued.pop(id(proof), None) is not proof
            or proof.key != key or proof.candidate != _encoded(candidate)):
        raise _error(catalog, '作品登记命名空间候选验证已断开或失效')
    verify(catalog, key, proof.snapshot)


def _backend(catalog):
    """Only concrete schema owners can use this shared mechanism."""
    from .work_catalog import WorkCatalog
    from .series_catalog import SeriesCatalog
    if type(catalog) is WorkCatalog:
        return 'work'
    if type(catalog) is SeriesCatalog:
        return 'series'
    raise TypeError('作品登记后端必须是当前明确的Catalog类型')


def _validate_record(catalog, payload, key):
    if _backend(catalog) == 'series':
        catalog._validate_namespace_record(payload, key)
    else:
        catalog._validate(payload, key)


@contextmanager
def writer_scope(catalog, key):
    """Acquire or reuse only the same verified, physically held lease."""
    if getattr(_held, 'lease', None) is None:
        with locked(catalog, key) as lease:
            yield lease
    else:
        require_write(catalog, key)
        yield require_lease(catalog, key)


_mutations = threading.local()
_live_mutations = {}


def require_write(catalog, key):
    lease = require_lease(catalog, key)
    if lease.read_only:
        raise _error(catalog, '只读登记租约不能写入作品登记')
    return require_mutation(catalog)


def require_mutation(catalog):
    """登记写入与媒体执行必须持同一实际保护，读取租约不能代替。"""
    token = getattr(_mutations, 'token', None)
    current = _live_mutations.get(id(token))
    path = catalog.lock_root / '.mutation.lock'
    binding = (_backend(catalog), _root_key(catalog.root), _owner())
    if (current is None or current[0] is not token or current[1] != binding
            or current[2] != (_identity(catalog.root), _identity(path))):
        raise _error(catalog, '作品登记缺少实际执行写入保护')
    _safe_parents(catalog, path.parent)
    return token


@contextmanager
def mutation_scope(catalog, *, wait_seconds=0.0):
    """媒体执行期间只保持写入互斥，让其他任务读取完整登记。"""
    token = getattr(_mutations, 'token', None)
    if token is not None:
        yield require_mutation(catalog)
        return
    if getattr(_held, 'lease', None) is not None:
        raise _error(catalog, '只读登记租约不能升级为执行写入保护')
    _safe_parents(catalog, catalog.lock_root)
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while True:
        stack = ExitStack()
        try:
            resource = LockResource(catalog, catalog.lock_root / '.mutation.lock',
                                    _backend(catalog) + '_mutation', True)
            stack.enter_context(serialized_resource_guard(
                resource, owner={'kind': _backend(catalog) + '_mutation'}))
            break
        except FileLockBusyError as exc:
            stack.close()
            if time.monotonic() >= deadline:
                raise _error(catalog, '已有整理或登记修改正在进行，请等待该操作完成') from exc
            time.sleep(min(0.05, max(0.0, deadline-time.monotonic())))
        except BaseException:
            stack.close()
            raise
    with stack:
        token = object()
        _live_mutations[id(token)] = (token, (_backend(catalog), _root_key(catalog.root), _owner()),
            (_identity(catalog.root), _identity(catalog.lock_root / '.mutation.lock')))
        _mutations.token = token
        try:
            require_mutation(catalog)
            yield token
        finally:
            _live_mutations.pop(id(token), None)
            _mutations.token = None
