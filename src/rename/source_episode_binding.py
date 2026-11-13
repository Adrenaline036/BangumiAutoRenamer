"""Frozen physical observations and pure per-source episode binding.

These are planning observations, not approval or execution authority.
"""
from dataclasses import dataclass
import json
from pathlib import Path

from .content_associations import encode


@dataclass(frozen=True)
class SourceEpisodeIdentity:
    source: Path
    size: int
    mtime_ns: int
    device: int
    inode: int

    def __post_init__(self):
        if (not isinstance(self.source, Path) or not self.source.is_absolute()
                or any(type(v) is not int or v < 0 for v in (self.size, self.mtime_ns, self.device, self.inode))):
            raise ValueError('逐源集号观察缺少严格路径或物理身份')


@dataclass(frozen=True)
class SourceEpisodeObservation:
    rows: tuple[SourceEpisodeIdentity, ...]

    def __post_init__(self):
        if (type(self.rows) is not tuple or any(type(r) is not SourceEpisodeIdentity for r in self.rows)
                or len({r.source for r in self.rows}) != len(self.rows)):
            raise ValueError('逐源集号观察必须是冻结且不重复的来源集合')

    def subset(self, sources):
        sources = tuple(sources)
        wanted = set(sources)
        if len(wanted) != len(sources) or not wanted <= {row.source for row in self.rows}:
            raise ValueError('集号候选缺少完整来源观察')
        return SourceEpisodeObservation(tuple(row for row in self.rows if row.source in wanted))


@dataclass(frozen=True)
class SourceEpisodeBindingInput:
    videos: tuple[tuple[Path, int], ...]
    projection_json: str
    observation: SourceEpisodeObservation

    def __post_init__(self):
        if (type(self.videos) is not tuple or type(self.observation) is not SourceEpisodeObservation
                or any(type(row) is not tuple or len(row) != 2 or not isinstance(row[0], Path)
                       or not row[0].is_absolute() or type(row[1]) is not int or row[1] < 1 for row in self.videos)
                or len({source for source, _ in self.videos}) != len(self.videos)):
            raise ValueError('逐源集号绑定输入必须冻结且唯一')
        if not isinstance(self.projection_json, str):
            raise ValueError('数值集号投影必须冻结')
        raw = json.loads(self.projection_json)
        if not isinstance(raw, dict) or encode(raw) != self.projection_json:
            raise ValueError('数值集号投影不是规范冻结对象')


@dataclass(frozen=True)
class SourceEpisodeBindingResult:
    payload_json: str

    def to_dict(self):
        return json.loads(self.payload_json)


def observe_source_episode_identities(sources: tuple[Path, ...]) -> SourceEpisodeObservation:
    """The only source reader in this adapter; candidates share its result."""
    if (type(sources) is not tuple or len(set(sources)) != len(sources)
            or any(not isinstance(source, Path) or not source.is_absolute() for source in sources)):
        raise ValueError('集号观察需要明确且不重复的视频来源')
    rows = []
    for source in sources:
        actual = source.stat()
        rows.append(SourceEpisodeIdentity(source, actual.st_size, actual.st_mtime_ns, actual.st_dev, actual.st_ino))
    return SourceEpisodeObservation(tuple(rows))


def bind_source_episodes(value: SourceEpisodeBindingInput) -> SourceEpisodeBindingResult:
    """Pure old-schema projection. Missing observations never trigger I/O."""
    return _binding_result(value)


def _binding_result(value: SourceEpisodeBindingInput) -> SourceEpisodeBindingResult:
    """The one numeric/physical fingerprint kernel, also used at consumption."""
    from .provider_link_decision import _canonical_sha256
    if type(value) is not SourceEpisodeBindingInput:
        raise ValueError('逐源集号绑定缺少类型化输入')
    projection = json.loads(value.projection_json)
    numeric_rows = projection.get('episode_mapping')
    if (not isinstance(numeric_rows, list)
            or any(not isinstance(row, dict) or set(row) != {'source_episode', 'target_episode'}
                   or any(type(row[k]) is not int or row[k] < 1 for k in row) for row in numeric_rows)):
        raise ValueError('数值集号映射格式无效')
    by_episode = {row['source_episode']: row['target_episode'] for row in numeric_rows}
    if len(by_episode) != len(numeric_rows):
        raise ValueError('数值集号映射包含重复原集号')
    identities = {row.source: row for row in value.observation.rows}
    if (set(identities) != {source for source, _ in value.videos}
            or any(episode not in by_episode for _, episode in value.videos)):
        raise ValueError('逐源集号观察未精确覆盖本次映射视频')
    source_rows = tuple(sorted(({
        'source_path': str(source), 'source_episode': episode, 'target_episode': by_episode[episode],
        'source_size': identities[source].size, 'source_mtime_ns': identities[source].mtime_ns,
        'source_device': identities[source].device, 'source_inode': identities[source].inode,
    } for source, episode in value.videos), key=lambda row: row['source_path'].casefold()))
    inventory_payload = {'inventory_scope': projection.get('inventory_scope'),
        'source_episodes': tuple({'source_path': row['source_path'], 'source_episode': row['source_episode']}
                                for row in source_rows)}
    payload = {key: item for key, item in projection.items() if key != 'projection_fingerprint'}
    payload['episode_mapping'] = source_rows
    payload['inventory_fingerprint'] = _canonical_sha256(inventory_payload)
    return SourceEpisodeBindingResult(encode({**payload, 'projection_fingerprint': _canonical_sha256(payload)}))


def restrict_source_episode_projection(current, state, destination_basis, observation):
    """Pure restriction of a receipted original choice, never a new choice.

    The caller must independently bind/reobserve the actual origin and both
    destination scopes. This result supplies numeric observations only; it does
    not publish a Decision or confer initial-completion/refresh authority.
    """
    return _restriction_result(current, state, destination_basis, observation)


def _restriction_result(current, state, destination_basis, observation):
    """Single restriction kernel, rechecked when its public result is consumed."""
    from types import SimpleNamespace
    from .decision_state import DecisionState, DecisionStateStore
    from .rename_plan import RenamePlan
    from .source_basis import InitialSourceBasis
    from .physical_identity import canonical_physical_key
    from .projection_authority import validate_plan_projection_receipt
    if (type(current) is not RenamePlan or type(state) is not DecisionState
            or type(destination_basis) is not InitialSourceBasis
            or type(observation) is not SourceEpisodeObservation
            or type(state.initial_source_basis) is not InitialSourceBasis):
        raise ValueError('集号子集必须消费原计划、决定及类型化目的来源观察')
    validate_plan_projection_receipt(current)
    if (current.task_id != state.task_id
            or current.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(state)
            or current.initial_source_basis != state.initial_source_basis):
        raise ValueError('集号子集的原计划、决定或完整来源依据不一致')
    original = json.loads(encode(state.source_episode_projection))
    decision = json.loads(encode(state.source_episode_projection_decision))
    core = {'mode', 'offset', 'inventory_scope', 'target_episode_start', 'target_episode_end',
            'episode_mapping', 'inventory_fingerprint', 'projection_fingerprint'}
    if (not isinstance(original, dict) or original != current.to_dict()['metadata'].get('source_episode_projection')
            or set(original) not in (core, core | {'status', 'reason_codes'})
            or not isinstance(decision, dict) or set(decision) != {'decision', 'projection_fingerprint'}
            or decision['decision'] not in {'absolute', 'relative', 'automatic_unique'}
            or original['mode'] not in {'absolute', 'relative'}
            or decision['decision'] not in {original['mode'], 'automatic_unique'}
            or decision['projection_fingerprint'] != original['projection_fingerprint']
            or original['inventory_scope'] != 'complete'):
        raise ValueError('原集号选择、模式或字段不允许严格子集继承')
    # A first complete scan's automatic conclusion is not a conclusion about
    # the partial destination. Manual candidates never had status fields.
    automatic = decision['decision'] == 'automatic_unique'
    if (automatic != ('status' in original)
            or automatic and (original['status'] != 'reviewable' or original['reason_codes'] != [])):
        raise ValueError('原自动或人工集号选择的状态组合无效')
    start, end, offset = (original[k] for k in ('target_episode_start', 'target_episode_end', 'offset'))
    if (any(type(v) is not int for v in (start, end, offset)) or start < 1 or end < start
            or offset != (0 if original['mode'] == 'absolute' else start - 1)):
        raise ValueError('原集号投影区间或偏移无效')
    before, after = state.initial_source_basis.to_dict(), destination_basis.to_dict()
    old_sources = {row['source_path']: row for row in before['sources']}
    new_sources = {row['source_path']: row for row in after['sources']}
    if (after['task_id'] != state.task_id or not new_sources.keys() < old_sources.keys()
            or any(old_sources[path] != row for path, row in new_sources.items())):
        raise ValueError('目的来源必须是原父卡的严格且物理身份不变子集')
    mappings = original['episode_mapping']
    fields = {'source_path', 'source_episode', 'target_episode', 'source_size', 'source_mtime_ns',
              'source_device', 'source_inode'}
    if not isinstance(mappings, list) or not mappings:
        raise ValueError('原逐源集号映射为空或无效')
    numbers, paths, identities, videos = {}, set(), [], []
    for row in mappings:
        if (not isinstance(row, dict) or set(row) != fields or not isinstance(row['source_path'], str)
                or any(type(row[k]) is not int for k in fields - {'source_path'})
                or row['source_episode'] < 1 or not start <= row['target_episode'] <= end
                or row['target_episode'] != row['source_episode'] + offset
                or row['source_path'] in paths):
            raise ValueError('原逐源集号映射包含重复来源或非法数值')
        path = row['source_path']
        physical = SourceEpisodeIdentity(Path(path), row['source_size'], row['source_mtime_ns'],
                                         row['source_device'], row['source_inode'])
        known = old_sources.get(path)
        if (known is None or known['kind'] != 'video' or known['size'] != physical.size
                or known['mtime_ns'] != physical.mtime_ns
                or known['physical_key'] != canonical_physical_key(
                    SimpleNamespace(st_dev=physical.device, st_ino=physical.inode))):
            raise ValueError('原逐源集号映射与完整来源物理身份不同')
        prior = numbers.setdefault(row['source_episode'], row['target_episode'])
        if prior != row['target_episode']:
            raise ValueError('同一原集号不能包含互相冲突的目标集号')
        paths.add(path)
        identities.append(physical)
        videos.append((physical.source, row['source_episode']))
    numeric = {k: v for k, v in original.items()
               if k not in {'episode_mapping', 'inventory_fingerprint', 'projection_fingerprint'}}
    numeric['episode_mapping'] = [{'source_episode': n, 'target_episode': target} for n, target in numbers.items()]
    # Reuse the original physical/fingerprint kernel to validate every original
    # row and both old digests before restricting; a matching digest alone is
    # not proof of valid numeric semantics.
    full = bind_source_episodes(SourceEpisodeBindingInput(tuple(videos), encode(numeric),
                               SourceEpisodeObservation(tuple(identities))))
    if type(full) is not SourceEpisodeBindingResult or full.to_dict() != original:
        raise ValueError('原逐源集号映射或摘要不能由同一绑定核验证')
    retained = tuple(row for row in videos if str(row[0]) in new_sources)
    expected = SourceEpisodeObservation(tuple(row for row in identities if str(row.source) in new_sources))
    if not retained or observation != expected:
        raise ValueError('目的逐源集号观察缺失、越界或发生物理漂移')
    used = {number for _, number in retained}
    partial = {k: v for k, v in numeric.items() if k not in {'status', 'reason_codes'}}
    partial['inventory_scope'] = 'partial'
    partial['episode_mapping'] = [row for row in numeric['episode_mapping'] if row['source_episode'] in used]
    request = SourceEpisodeBindingInput(retained, encode(partial), observation)
    result = bind_source_episodes(request)
    if type(result) is not SourceEpisodeBindingResult or result != _binding_result(request):
        raise ValueError('目的逐源集号绑定核已断开')
    return result
