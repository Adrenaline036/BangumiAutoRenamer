"""Complete source observations; never episode, target or execution authority.

The Decision journal is the only writer. Ordinary inheritance checks actual
reservation history without reopening media. Initial observation is bounded by
the admitted scope, not a recursive directory scan or an incomplete draft.
"""
from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

from .content_associations import (
    ContentAssociations, encode, observe_content_associations, scoped_path, source_key,
)
from .content_policy import ContentPolicy
from .movie_assets import _entry_type
from .path_safety import first_reparse_component, _is_link
from .physical_identity import canonical_physical_key

POLICY = 'initial-source-basis-v1'


def _exact_layout_root(paths):
    """Layout of the complete explicit file set; never discover more sources."""
    paths = tuple(Path(path) for path in paths)
    if not paths or any(not path.is_absolute() or '..' in path.parts for path in paths):
        raise ValueError('明确文件布局必须包含非空绝对来源路径')
    try:
        return Path(os.path.commonpath([str(path.parent) for path in paths]))
    except ValueError as exc:
        raise ValueError('明确文件来源不存在共同布局根') from exc


def root_for_task(task, scope):
    path = Path(str(task.get('path') or ''))
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('来源 basis 缺少真实任务路径')
    # Admission already froze whether this was a file; inheritance must not
    # ask live is_file/is_dir to reinterpret a now missing or changed input.
    files = {source_key(i.source_path) for i in scope.source_identities}
    return str(_exact_layout_root(i.source_path for i in scope.source_identities)
               if source_key(path) in files else path)


@dataclass(frozen=True)
class InitialSourceBasis:
    payload_json: str

    def __post_init__(self):
        from .task_batch_scope import TaskSourceScope
        try:
            raw = json.loads(self.payload_json)
            if (not isinstance(raw, dict) or set(raw) != {
                    'schema_version', 'policy_version', 'task_id', 'content_policy',
                    'source_root', 'batch_id', 'task_source_scope', 'sources', 'associations'}
                    or type(raw['schema_version']) is not int or raw['schema_version'] != 1
                    or raw['policy_version'] != POLICY or raw['content_policy'] != ContentPolicy.SAFE_WATCHABLE.value
                    or encode(raw) != self.payload_json or not isinstance(raw['sources'], list)):
                raise ValueError('来源 basis 合同字段无效')
            scope = TaskSourceScope.from_mapping(raw['task_source_scope'])
            if (scope.to_dict() != raw['task_source_scope'] or scope.schema_version != 2
                    or scope.task_id != raw['task_id'] or scope.content_policy != ContentPolicy.SAFE_WATCHABLE
                    or not isinstance(raw['batch_id'], str) or not raw['batch_id']):
                raise ValueError('来源 basis scope 归属无效')
            root = Path(raw['source_root'])
            if not root.is_absolute() or '..' in root.parts:
                raise ValueError('来源 basis 根无效')
            identities = {i.source_path: i.to_dict() for i in scope.source_identities}
            seen = []
            neutral = []
            for row in raw['sources']:
                if not isinstance(row, dict) or set(row) != {
                        'source_path', 'location_key', 'physical_key', 'size', 'mtime_ns', 'kind', 'hash_evidence'}:
                    raise ValueError('来源 basis 不能包含集号、角色、目标等决定')
                path = scoped_path(row['source_path'], root)
                fields = {k: row[k] for k in ('source_path', 'location_key', 'physical_key', 'size', 'mtime_ns')}
                if fields != identities.get(str(path)) or row['hash_evidence'] != {'state': 'not_observed'}:
                    raise ValueError('来源 basis 身份或hash观察不符')
                entry = _neutral(path)
                from .content_associations import source_kind
                if source_kind(entry) != row['kind']:
                    raise ValueError('来源 basis 类别不符')
                seen.append(str(path))
                neutral.append(entry)
            if (seen != sorted(identities, key=source_key) or len(set(map(source_key, seen))) != len(seen)):
                raise ValueError('来源 basis 必须完整且唯一覆盖scope')
            associations = ContentAssociations.from_mapping(raw['associations'])
            expected = observe_content_associations(neutral, task_id=scope.task_id, source_root=root)
            if associations != expected:
                raise ValueError('来源 basis 不能夹带未观察的owner')
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError('来源 basis 合同无效') from exc

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping):
            raise ValueError('来源 basis 必须使用类型化合同')
        return cls(encode(value))

    def to_dict(self):
        return json.loads(self.payload_json)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.payload_json.encode('utf-8')).hexdigest()


def _neutral(path):
    return SimpleNamespace(source_path=Path(path), media_kind=_entry_type(Path(path).name, stat.S_IFREG),
                           role='', subtitle=None)


def _identity_from_stat(path, actual, *, location_key):
    """Project the leaf metadata just read by a checked traversal."""
    if _is_link(actual) or not stat.S_ISREG(actual.st_mode):
        raise ValueError('来源 basis 必须是普通文件')
    return dict(source_path=str(path), location_key=location_key,
                physical_key=canonical_physical_key(actual), size=actual.st_size, mtime_ns=actual.st_mtime_ns)


def _basis_payload(*, task_id, source_root, batch_id, scope):
    """Pure projection of a proven admitted scope, not a fresh file check."""
    from .content_associations import source_kind
    entries, rows = [], []
    for item in sorted(scope.source_identities, key=lambda i: source_key(i.source_path)):
        path = scoped_path(item.source_path, Path(source_root))
        entry = _neutral(path)
        entries.append(entry)
        rows.append({**item.to_dict(), 'kind': source_kind(entry), 'hash_evidence': {'state': 'not_observed'}})
    associations = observe_content_associations(entries, task_id=task_id, source_root=source_root)
    return dict(schema_version=1, policy_version=POLICY,
        task_id=task_id, content_policy=ContentPolicy.SAFE_WATCHABLE.value, source_root=str(source_root),
        batch_id=batch_id, task_source_scope=scope.to_dict(), sources=rows, associations=associations.to_dict())


def project_source_basis(*, task_id, source_root, batch_id, scope):
    """从已接纳的完整范围投影，不重新观察源文件。"""
    return InitialSourceBasis.from_mapping(_basis_payload(
        task_id=task_id, source_root=source_root, batch_id=batch_id, scope=scope))


def _manifest_request_layout(store, manifest, scope):
    """Pure typed request projection, never proof of a persisted reservation."""
    from .task_batch_scope import TaskBatchScopeManifestStore, TaskBatchScopeManifest, TaskSourceScope
    if (type(store) is not TaskBatchScopeManifestStore or type(manifest) is not TaskBatchScopeManifest
            or type(scope) is not TaskSourceScope):
        raise ValueError('原请求布局必须使用实际typed manifest与scope')
    if manifest.batch_scope_fingerprint != store._batch_fingerprint(manifest.requests):
        raise ValueError('来源 basis 历史requests指纹不符')
    rebuilt = store._materialize(batch_id=manifest.batch_id, requests=manifest.requests,
        batch_fingerprint=manifest.batch_scope_fingerprint, state=manifest.state, revision=manifest.revision)
    if scope not in rebuilt.task_source_scopes:
        raise ValueError('来源 basis 原scope缺少真实历史证明')
    request = next(r for r in manifest.requests if r.task_id == scope.task_id)
    if request.scope_kind == 'parent_directory':
        # Public directory admission binds one input root, not any ancestor.
        if len(request.input_scope) != 1:
            raise ValueError('来源 basis 目录请求根不唯一')
        expected = request.input_scope[0]
    else:
        # Layout covers every explicitly admitted file, including companions
        # outside the video directory. A sole video remains the observation
        # path; this layout does not authorize recursively scanning its root.
        paths = [Path(p) for p in request.input_scope]
        videos = [p for p in paths if _entry_type(p.name, stat.S_IFREG) == 'video']
        expected = _exact_layout_root(paths)
    observation = (Path(expected) if request.scope_kind == 'parent_directory'
                   else videos[0] if len(videos) == 1 else Path(expected))
    return request, Path(expected), observation



def _proven_request(service, *, batch_id, scope, source_root):
    """One actual disk request proof for first observation and historical reuse."""
    from .task_batch_scope import TaskBatchScopeManifestStore
    store = TaskBatchScopeManifestStore(service.plan_store.root.parent / 'task_batch_scope')
    manifest = store.load(batch_id)
    request, layout, observation = _manifest_request_layout(store, manifest, scope)
    if source_root != str(layout):
        raise ValueError('来源 basis 与实际admission请求根不一致')
    return manifest, request, layout, observation

def _request_proof(service, *, batch_id, scope, source_root):
    _proven_request(service, batch_id=batch_id, scope=scope, source_root=source_root)
    return scope


def observe_admitted_basis(service, task_id):
    """Project proven admission metadata for the first draft, not authority.

    Canonical publication independently checks the persisted scope binding.
    A directory input does not authorize discovering files added after admission.
    """
    from .content_policy import _scope_binding
    from ..utils.utils import task_record_snapshot_at
    before, task, _fingerprint = task_record_snapshot_at(service.task_root, task_id)
    binding = _scope_binding(service, task_id)
    if binding.policy != ContentPolicy.SAFE_WATCHABLE or binding.scope is None:
        raise ValueError('新库存观察缺少实际已接纳来源范围')
    _manifest, _request, layout, observation = _proven_request(service,
        batch_id=binding.batch_id, scope=binding.scope, source_root=binding.source_root)
    if Path(str(task.get('path') or '')) != observation:
        raise ValueError('新库存观察路径不等于实际原输入')
    basis = InitialSourceBasis.from_mapping(_basis_payload(task_id=task_id, source_root=layout,
        batch_id=binding.batch_id, scope=binding.scope))
    after, _task, _fingerprint = task_record_snapshot_at(service.task_root, task_id)
    if before != after or _scope_binding(service, task_id) != binding:
        raise ValueError('新库存观察期间任务或来源范围变化')
    if type(basis) is not InitialSourceBasis:
        raise ValueError('新库存来源观察已断开')
    return basis


@dataclass(frozen=True)
class SourceInputProof:
    """Frozen request observation path, never a target or source-scope override."""
    task_id: str
    batch_id: str
    basis_fingerprint: str
    scope_fingerprint: str
    request_fingerprint: str
    input_kind: str
    observation_path: Path
    layout_root: Path

    def __post_init__(self):
        if (type(self.task_id) is not str or not self.task_id or type(self.batch_id) is not str or not self.batch_id
                or self.input_kind not in {'parent_directory', 'exact_file'}
                or any(type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value)
                       for value in (self.basis_fingerprint, self.scope_fingerprint, self.request_fingerprint))
                or any(not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts
                       for path in (self.observation_path, self.layout_root))
                or not self.observation_path.is_relative_to(self.layout_root)):
            raise ValueError('原输入路径证明格式无效')


def observe_initial_input(service, plan, state):
    """Reconcile immutable admission with actual active ownership, without media I/O."""
    from .default_content import _origin
    from .content_policy import _scope_binding
    from .task_batch_scope import TaskSourceScope
    from ..utils.utils import task_record_snapshot_at
    before, task, _fingerprint = task_record_snapshot_at(service.task_root, plan.task_id)
    basis = _origin(service, plan, state)
    binding = _scope_binding(service, plan.task_id)
    raw = basis.to_dict()
    historical, request, layout, observation = _proven_request(service,
        batch_id=raw['batch_id'], scope=TaskSourceScope.from_mapping(raw['task_source_scope']),
        source_root=raw['source_root'])
    current, active_request, active_layout, active_observation = _proven_request(service,
        batch_id=binding.batch_id, scope=binding.scope, source_root=binding.source_root)
    if (request.to_dict() != active_request.to_dict() or layout != active_layout or observation != active_observation
            or plan.source_root != layout or Path(str(task.get('path') or '')) != observation):
        raise ValueError('原输入路径与历史请求、当前scope或布局根不一致')
    after, _task, _fingerprint = task_record_snapshot_at(service.task_root, plan.task_id)
    if before != after:
        raise ValueError('原输入路径观察期间任务已变化')
    digest = lambda value: hashlib.sha256(encode(value).encode('utf-8')).hexdigest()
    return SourceInputProof(plan.task_id, binding.batch_id, basis.fingerprint, digest(binding.scope.to_dict()),
        digest({'historical_batch': historical.batch_scope_fingerprint, 'active_batch': current.batch_scope_fingerprint,
                'request': request.to_dict()}), request.scope_kind, observation, layout)


def _historical_scope(service, basis):
    from .task_batch_scope import TaskSourceScope
    raw = basis.to_dict()
    return _request_proof(service, batch_id=raw['batch_id'],
        scope=TaskSourceScope.from_mapping(raw['task_source_scope']), source_root=raw['source_root'])


def validate_inheritance(service, basis, binding):
    old = _historical_scope(service, basis)
    _validate_scope_equivalence(old, binding.scope, basis.to_dict()['source_root'], binding.source_root)


def _validate_scope_equivalence(old, current, original_root, current_root):
    """Historical replan may change lineage, but never its source identity set."""
    if (current is None or old.task_id != current.task_id or old.content_policy != current.content_policy
            or old.media_domain != current.media_domain
            or old.source_identity_set_fingerprint != current.source_identity_set_fingerprint
            or original_root != current_root):
        raise ValueError('来源 basis 与当前scope不等价，须明确重新观察')


def bind_initial_source_basis(service, envelope, *, binding):
    previous, candidate, draft = envelope.previous_decision, envelope.decision, envelope.draft_plan
    if draft.task_id != candidate.task_id or binding.task_id != candidate.task_id:
        raise ValueError('来源计划、决定与实际范围不属于同一任务')
    old = previous.initial_source_basis if previous else None
    supplied = candidate.initial_source_basis
    raw = draft.metadata.get('initial_source_basis')
    projected_basis = draft.initial_source_basis if raw is not None else None
    if binding.policy is None:
        if supplied is not None or old is not None or raw is not None:
            raise ValueError('旧任务不能追认来源 basis')
        return envelope
    if binding.scope is None or str(draft.source_root) != binding.source_root:
        raise ValueError('来源 basis 与真实任务根不一致')
    _request_proof(service, batch_id=binding.batch_id, scope=binding.scope, source_root=binding.source_root)
    if old is not None:
        if supplied != old or projected_basis != old:
            raise ValueError('冻结来源 basis 不得清除、变更或伪造Plan')
        validate_inheritance(service, old, binding)
        return envelope
    elif raw is not None and (supplied is None or projected_basis != supplied):
        raise ValueError('首次来源 basis 与Plan输入不一致')
    observed = project_source_basis(task_id=candidate.task_id, source_root=binding.source_root,
        batch_id=binding.batch_id, scope=binding.scope)
    if type(observed) is not InitialSourceBasis:
        raise ValueError('来源 basis producer 已断开')
    if old is None and supplied is not None and supplied != observed:
        raise ValueError('首次来源 basis 不等于实际重新观察')
    if old is None and not candidate.confirmed_work:
        actual = {source_key(i.source_path) for i in binding.scope.source_identities}
        entries = [source_key(e.source_path) for e in draft.entries]
        ignored = [source_key(p) for p in draft.ignored_sources]
        expected = [source_key(p) for p in draft.expected_sources]
        from .default_content import InitialDefaultDraft
        first_default = type(envelope.initial_default) is InitialDefaultDraft
        covered = set(entries) | set(ignored) if first_default else set(entries)
        if (covered != actual or (not first_default and ignored) or set(entries) & set(ignored)
                or set(expected) != actual or len(set(ignored)) != len(ignored)
                or len(set(entries)) != len(entries) or len(set(expected)) != len(expected)):
            raise ValueError('首次库存计划必须完整且唯一覆盖实际已接纳来源')
    return replace(envelope, decision=replace(candidate, initial_source_basis=observed))


def validate_state_write(store, current, candidate):
    old = current.initial_source_basis if current else None
    new = candidate.initial_source_basis
    if new == old:
        return
    if new is None:
        raise ValueError('直接Decision写入不能清除来源 basis')
    require_prepared_state_publication(store, current, candidate)


def require_prepared_state_publication(store, current, candidate):
    """Exact journal proof shared by protected Decision authority fields."""
    # No public boolean exemption: the exact existing transaction and rollback
    # bytes must authorize this already prepared candidate before the writer.
    root = store.root.parent / 'projection_journal'
    try:
        journal = json.loads((root / f'{candidate.task_id}.json').read_text('utf-8'))
        if (journal.get('task_id') != candidate.task_id or journal.get('phase') != 'prepared'
                or journal.get('status') != 'pending'
                or journal.get('current_decision_fingerprint') != store.fingerprint(candidate)):
            raise ValueError('来源 basis 缺少当前prepared事务证明')
        ref = str(journal.get('rollback_ref') or '')
        backup = (root / ref).resolve()
        if backup.parent != (root / 'backups').resolve() or not ref or first_reparse_component(backup):
            raise ValueError('来源 basis rollback路径不安全')
        data = backup.read_bytes()
        if hashlib.sha256(data).hexdigest() != journal.get('rollback_sha256'):
            raise ValueError('来源 basis rollback指纹不符')
        payload = json.loads(data)
        if (payload.get('task_id') != candidate.task_id
                or payload.get('previous_decision') != (current.to_dict() if current else None)):
            raise ValueError('来源 basis rollback旧记录不符')
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('直接Decision写入缺少来源 basis 原事务证明') from exc
    return candidate
