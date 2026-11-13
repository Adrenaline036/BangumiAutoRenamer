"""Crash-safe inter-process file locks with diagnostic owner metadata.

The lock file is intentionally persistent. Ownership is held by the operating
system advisory lock, not by file existence, so a killed process cannot leave a
permanent lock behind. This also recovers legacy ``O_CREAT | O_EXCL`` lock
files without guessing whether a long-running task is stale from its age.
"""

from __future__ import annotations

import json
import asyncio
import os
import socket
import stat
import threading
import uuid
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Optional


class FileLockBusyError(RuntimeError):
    """Raised when another live process owns the advisory lock."""


def is_lock_busy(error):
    """沿包装异常识别真实锁忙，避免按提示文字误归类。"""
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, FileLockBusyError):
            return True
        seen.add(id(error))
        error = error.__cause__ if error.__cause__ is not None else error.__context__
    return False


@dataclass(frozen=True)
class LockResource:
    """A path description, never evidence that its lock is held."""
    owner: object
    path: Path
    key: str
    strict: bool = False


@contextmanager
def resource_guard(resource, *, owner=None):
    with advisory_file_lock(resource.path, owner=owner, require_single_link=resource.strict):
        yield


class _LocalResourceGate:
    def __init__(self):
        self.mutex = threading.Lock()
        self.thread = None


_local_resource_gates = weakref.WeakValueDictionary()
_local_resource_registry = threading.Lock()
_wait_control = ContextVar('bar_shared_wait', default=None)


class LockWaitCancelledError(RuntimeError):
    """只取消尚未进入业务的共享等待，不属于外部锁忙。"""


@contextmanager
def cancellable_wait(cancelled):
    # 只抛一次，让同一事务异常处理中的回滚能继续获取锁。
    token = _wait_control.set([cancelled, False])
    try:
        yield
    finally:
        _wait_control.reset(token)


def _check_wait_cancelled():
    control = _wait_control.get()
    if control is not None and not control[1] and control[0]():
        control[1] = True
        raise LockWaitCancelledError('共享登记等待已取消，原任务可稍后重试')


@contextmanager
def serialized_resource_guard(resource, *, owner=None):
    """后台线程顺序使用共享登记库；每次仍须取得真实操作系统锁。"""
    key = (os.getpid(), os.path.normcase(str(Path(resource.path).resolve())))
    thread = threading.get_ident()
    with _local_resource_registry:
        gate = _local_resource_gates.get(key)
        if gate is None:
            gate = _LocalResourceGate()
            _local_resource_gates[key] = gate
        if gate.thread == thread:
            raise FileLockBusyError('共享登记事务不可在同一线程重入')
        acquired = gate.mutex.acquire(blocking=False)
        if acquired:
            gate.thread = thread
    if not acquired:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise FileLockBusyError('共享登记正在处理，请稍后重试；界面线程不等待文件事务')
        while True:
            _check_wait_cancelled()
            if gate.mutex.acquire(timeout=.1):
                break
        with _local_resource_registry:
            gate.thread = thread
    try:
        if not acquired:
            _check_wait_cancelled()
        with resource_guard(resource, owner=owner):
            yield
    finally:
        with _local_resource_registry:
            gate.thread = None
            gate.mutex.release()



def task_operation_resource(task_root, task_id):
    from ..utils.utils import safe_task_path
    root = Path(task_root)
    safe_task_path(root, task_id)
    return LockResource(None, root / '.execution-locks' / f'{task_id}.lock', f'task_operation:{task_id}')


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _AcquisitionProof:
    __slots__ = ()


@dataclass(frozen=True, eq=False)
class _Acquisition:
    descriptor: int
    process: int
    thread: int
    identity: tuple[int, int]


_acquisition_proofs: dict[_AcquisitionProof, _Acquisition] = {}
_acquisition_states: dict[_Acquisition, str] = {}
_registry_lock = threading.RLock()


def _acquire(descriptor: int) -> _AcquisitionProof:
    opened = os.fstat(descriptor)
    value = _Acquisition(descriptor, os.getpid(), threading.get_ident(),
                         (opened.st_dev, opened.st_ino))
    with _registry_lock:
        # The pending record is not authority. Its identity survives every
        # subsequent phase; consuming the one-use token never ends liveness.
        _acquisition_states[value] = 'acquiring'
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if _acquisition_states.get(value) != 'acquiring':
                raise ValueError('真实操作系统获锁证明在获取期间已失效')
            _acquisition_states[value] = 'locked'
            proof = _AcquisitionProof()
            _acquisition_proofs[proof] = value
            return proof
        except BaseException:
            _acquisition_states.pop(value, None)
            raise


def _require_acquisition(value, descriptor):
    with _registry_lock:
        if (type(value) is not _Acquisition or value.descriptor != descriptor
                or value.process != os.getpid() or value.thread != threading.get_ident()
                or _acquisition_states.get(value) != 'locked'):
            raise ValueError('真实操作系统获锁证明已断开、过期或不属于当前句柄')
        opened = os.fstat(descriptor)
        if (value.identity != (opened.st_dev, opened.st_ino)
                or _acquisition_states.get(value) != 'locked'):
            raise ValueError('真实操作系统获锁证明已失效或句柄身份已变化')
        return value


def _consume_acquisition(proof, descriptor):
    with _registry_lock:
        value = _acquisition_proofs.get(proof) if type(proof) is _AcquisitionProof else None
        _require_acquisition(value, descriptor)
        del _acquisition_proofs[proof]  # Retire only the one-use token.
        return value


def _release(descriptor: int) -> None:
    # Releasing through the sole OS boundary retires both unused acquisition
    # proofs and consumed capabilities, even when the OS unlock itself fails.
    with _registry_lock:
        _invalidate_descriptor(descriptor)
        if os.name == "nt":
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_UN)


def _write_metadata(handle, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    handle.seek(1)
    handle.truncate()
    handle.write(encoded)
    handle.flush()
    os.fsync(handle.fileno())


def read_lock_metadata(path: Path) -> Optional[dict]:
    """Return diagnostics; never use lock-file contents as ownership proof."""

    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw[1:].decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


@contextmanager
def task_operation_lock(task_root, task_id, *, operation, fingerprint=''):
    """One lock identity for approval, execution and safe retirement."""
    with resource_guard(task_operation_resource(task_root, task_id), owner={
            'kind': operation, 'task_id': task_id, 'plan_fingerprint': fingerprint}):
        yield


@contextmanager
def advisory_file_lock(
    path: Path,
    *,
    owner: Optional[Mapping[str, object]] = None,
    require_single_link: bool = False,
) -> Iterator[dict]:
    """Acquire a non-blocking cross-process lock with owner diagnostics.

    Existing files, including stale legacy exclusive-create locks, do not block
    acquisition. Only a live kernel lock does.
    """

    with _locked_file(path, owner=owner, require_single_link=require_single_link) as locked:
        yield locked[0]


@contextmanager
def _locked_file(path, *, owner=None, require_single_link=False):
    """One acquisition/diagnostics/release lifecycle for both interfaces."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    descriptor = handle.fileno()
    acquisition = None
    def validate_handle():
        if not require_single_link:
            return
        opened, current = os.fstat(handle.fileno()), lock_path.lstat()
        if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode)
                or getattr(current, 'st_file_attributes', 0) & 0x400
                or opened.st_nlink != 1 or current.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError('锁文件必须为身份一致的独立普通文件')
    try:
        validate_handle()
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            proof = _acquire(descriptor)
        except OSError as exc:
            raise FileLockBusyError("lock is owned by another live process") from exc
        acquisition = _consume_acquisition(proof, descriptor)
        payload = {
            "schema_version": 1,
            "status": "acquired",
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "lock_token": uuid.uuid4().hex,
            "acquired_at": _utc_now(),
            **dict(owner or {}),
        }
        validate_handle()
        _write_metadata(handle, payload)
        _require_acquisition(acquisition, descriptor)
        yield payload, handle, acquisition
        with _registry_lock:
            if _acquisition_states.get(acquisition) == 'locked':
                released = dict(payload)
                released.update({"status": "released", "released_at": _utc_now()})
                validate_handle()
                _write_metadata(handle, released)
    finally:
        # A faulty adapter may have called the real acquire but discarded its
        # proof. Never leave that unused proof replayable after closing the fd.
        with _registry_lock:
            held = _acquisition_states.get(acquisition) == 'locked'
            try:
                _invalidate_descriptor(descriptor, acquisition=acquisition)
            finally:
                try:
                    if held and acquisition.process == os.getpid() and not handle.closed:
                        _release(descriptor)
                finally:
                    handle.close()








def _invalidate_descriptor(descriptor, *, acquisition=None):
    def matches(value):
        return (value.descriptor == descriptor and value.process == os.getpid()
                and (acquisition is None or value is acquisition))
    with _registry_lock:
        for value in tuple(_acquisition_states):
            if matches(value):
                _acquisition_states.pop(value, None)
        for proof, value in tuple(_acquisition_proofs.items()):
            if matches(value):
                _acquisition_proofs.pop(proof, None)






