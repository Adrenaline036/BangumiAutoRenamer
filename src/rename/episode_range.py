"""人工连续集数决定及其唯一投影；一个来源仍只产生一个视频。"""
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from collections.abc import Mapping

POLICY = 'episode-range.v1'


def valid_episode(value):
    return type(value) is int and 0 <= value <= 99999


def numbers(season, start, end):
    if any(type(v) is not int for v in (season, start, end)) or season < 0 or not valid_episode(start) or not valid_episode(end) or end < start or (season == 0 and start == 0):
        raise ValueError('请填写有效季号及连续起止集数；季号不小于0，起始集不小于0（特别篇不使用S00E00），结束集不小于起始集')
    return season, start, end


def coverage(entry):
    if entry.season_id is None or entry.episode_id is None:
        if entry.episode_end is not None:
            raise ValueError('结束集数缺少有效起始集号')
        return frozenset()
    season, start, end = numbers(entry.season_id, entry.episode_id,
                                entry.episode_id if entry.episode_end is None else entry.episode_end)
    return frozenset((season, n) for n in range(start, end + 1))


def work_key(work):
    if not isinstance(work, Mapping):
        work = work.to_dict()
    p = work.get('primary_provider') or {}
    key = tuple(str(p.get(k) or '') for k in ('namespace', 'entity_type', 'provider_id'))
    if not all(key) or work.get('status') != 'confirmed':
        raise ValueError('请先确认作品，再填写连续集数')
    return ':'.join(key)


@dataclass(frozen=True)
class EpisodeRangeDecision:
    source_path: str
    work_key: str
    source_basis: str
    season: int
    start: int
    end: int
    naming_mode: str
    title: str
    policy: str = POLICY

    def __post_init__(self):
        numbers(self.season, self.start, self.end)
        if (self.policy != POLICY or self.naming_mode not in {'bangumi', 'tmdb'}
                or not Path(self.source_path).is_absolute() or not self.work_key
                or len(self.source_basis) != 64 or not self.title.strip()):
            raise ValueError('连续集数决定缺少有效作品、来源或命名资料')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping) or set(value) != {f.name for f in fields(cls)}:
            raise ValueError('连续集数决定格式无效')
        return cls(**dict(value))


def from_plan(plan):
    rows = plan.metadata.get('episode_ranges', ())
    if not isinstance(rows, (tuple, list)):
        raise ValueError('连续集数决定必须是冻结清单')
    return tuple(EpisodeRangeDecision.from_mapping(r) for r in rows)


def choose(plan, state, edit):
    from . import content_selection as content
    from .plan_review import entry_id
    if set(edit) != {'entry_id', 'season', 'start', 'end', 'naming_mode'}:
        raise ValueError('连续集数输入只能包含视频、季号、起止集数和命名方式')
    selected = [e for e in content.material_entries(plan, state.content_selection)
                if entry_id(e) == edit['entry_id'] and e.media_kind == 'video']
    if len(selected) != 1 or selected[0].source_path in plan.ignored_sources:
        raise ValueError('请选择本次保留整理的一个视频')
    if state.initial_source_basis is None:
        raise ValueError('当前任务缺少冻结来源依据')
    season, start, end = numbers(edit['season'], edit['start'], edit['end'])
    title = str(state.confirmed_work.get('title') or '').strip()
    decision = EpisodeRangeDecision(str(selected[0].source_path), work_key(state.confirmed_work),
        state.initial_source_basis.fingerprint, season, start, end, edit['naming_mode'],
        f'{title} 第{start}集' if start == end else f'{title} 第{start}–{end}集')
    decisions = tuple(d for d in state.episode_ranges if d.source_path != decision.source_path) + (decision,)
    return tuple(sorted(decisions, key=lambda d:d.source_path))


def choose_many(plan, state, edit):
    """一次保存明确列出的正片编号，持久结构仍为逐来源决定。"""
    if 'rows' not in edit:
        return choose(plan, state, edit), (edit['entry_id'],)
    if set(edit) != {'season', 'rows'} or not isinstance(edit['rows'], list) or not edit['rows']:
        raise ValueError('请明确选择正片及目标编号')
    identifiers = []
    candidate = state
    for row in edit['rows']:
        if (not isinstance(row, dict) or row.get('season') != edit['season']
                or row.get('entry_id') in identifiers):
            raise ValueError('正片编号清单包含重复视频或不同季号')
        decisions = choose(plan, candidate, row)
        identifiers.append(row['entry_id'])
        candidate = replace(candidate, episode_ranges=decisions)
    return candidate.episode_ranges, tuple(identifiers)


def validate_bindings(plan, decisions):
    if not decisions:
        return
    if any(type(d) is not EpisodeRangeDecision for d in decisions):
        raise ValueError('连续集数必须使用类型化人工决定')
    if len({d.source_path for d in decisions}) != len(decisions):
        raise ValueError('同一视频不能具有两个连续集数决定')
    basis = plan.initial_source_basis.fingerprint
    if any(d.source_basis != basis or d.work_key != work_key(plan.work_identity) for d in decisions):
        raise ValueError('作品或来源已改变，请先核对原连续集数决定')


def project_entry(entry, decision, root, group):
    from .rename_plan import AssetRole, PresentationAssetPlanContract, build_default_version_descriptor
    from .content_selection import canonical_episode_target
    from .rename_plan import build_version_ref, EditionValue
    descriptor = entry.presentation_asset.version_descriptor if entry.presentation_asset else None
    if (descriptor is not None and descriptor.identity_policy != 'episode-version.v1'
            and isinstance(descriptor.edition.value, EditionValue) and descriptor.technical_variant_key):
        descriptor = replace(descriptor, version_ref=build_version_ref(group, decision.season, decision.start,
            descriptor.edition.value.key, descriptor.technical_variant_key))
    else:
        descriptor = build_default_version_descriptor(group, decision.season, decision.start)
    target = canonical_episode_target(root, decision.season, decision.start, entry.source_path.suffix,
                                      episode_end=decision.end, technical_label=descriptor.display_label)
    asset = PresentationAssetPlanContract(group, AssetRole('content_version'), None, descriptor)
    return replace(entry, season_id=decision.season, episode_id=decision.start,
        episode_end=decision.end if decision.end != decision.start else None, target_path=target,
        role='main_episode', presentation_asset=asset, requires_review=True,
        collision_resolution=entry.collision_resolution if entry.target_path == target else None)


def _entries(plan, decisions):
    """消费同一权威；重放不读取媒体或访问Provider。"""
    from .content_associations import subtitle_entries
    from .audio_projection import audio_entries
    from . import content_selection as content
    validate_bindings(plan, decisions)
    known = {d.source_path:d for d in decisions}
    entries = []
    root = Path(str(plan.metadata.get('canonical_work_root') or ''))
    if decisions and (not root.is_absolute() or {g.kind for g in plan.presentation_groups} != {'tv_episodes'}):
        raise ValueError('连续集数需要已确认的剧集计划和作品目录')
    present = set()
    for e in plan.entries:
        if e.media_kind == 'video' and str(e.source_path) in known:
            present.add(str(e.source_path))
            e = project_entry(e, known[str(e.source_path)], root, plan.presentation_groups[0].group_id)
        elif e.media_kind == 'video' and e.episode_end is not None:
            raise ValueError('视频结束集数缺少人工决定')
        entries.append(e)
    omitted = {str(p) for p in plan.ignored_sources}
    if known.keys() - present - omitted:
        raise ValueError('连续集数对应来源不在当前计划')
    choice = content.from_plan(plan)
    if decisions and choice is not None:
        entries = audio_entries(plan, subtitle_entries(entries, choice.associations, tv_identity=True))
    return tuple(entries)


def validate_projection(plan):
    from .plan_review import entry_id
    decisions = from_plan(plan)
    known = {d.source_path: d for d in decisions}
    for entry in plan.entries:
        if entry.media_kind == 'video' and str(entry.source_path) in known:
            d = known[str(entry.source_path)]
            if (entry.season_id, entry.episode_id, entry.episode_end) != (d.season, d.start, d.end if d.end != d.start else None):
                raise ValueError('连续集数与人工决定不一致')
    if _entries(plan, decisions) != plan.entries:
        raise ValueError('连续集数与视频、字幕或音轨投影不一致')
    return {entry_id(e) for e in plan.entries if e.media_kind == 'video'
            and str(e.source_path) in known}


def require_unbound(state):
    if state is not None and state.episode_ranges:
        raise ValueError('当前视频已确认连续集数，请先在视频用途设置中取消该决定，再更换作品或调整来源范围；原任务及人工选择保留')


def project(plan, decisions):
    metadata = dict(plan.metadata)
    if decisions:
        metadata['episode_ranges'] = [d.to_dict() for d in decisions]
    else:
        metadata.pop('episode_ranges', None)
    return replace(plan, entries=_entries(plan, decisions), metadata=metadata)
