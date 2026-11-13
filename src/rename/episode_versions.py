"""同一未执行任务内的人工剧集版本决定与确定性投影。"""
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
import re
import uuid

from .episode_range import work_key, valid_episode
from .movie_version_selection import safe_label, label_key

POLICY = 'episode-version.v1'


def version_ref(task_id, source):
    return 'tv-' + uuid.uuid5(uuid.NAMESPACE_URL, task_id + '\n' + source).hex


@dataclass(frozen=True)
class EpisodeVersionDecision:
    source_path: str
    task_id: str
    work_key: str
    source_basis: str
    season: int
    episode: int
    label: str
    version_ref: str
    policy: str = POLICY

    def __post_init__(self):
        if (self.policy != POLICY or not isinstance(self.source_path, str)
                or not Path(self.source_path).is_absolute() or not self.task_id or not self.work_key
                or not re.fullmatch('[0-9a-f]{64}', self.source_basis)
                or type(self.season) is not int or self.season < 1
                or not valid_episode(self.episode)
                or not isinstance(self.label, str) or safe_label(self.label) != self.label
                or self.version_ref != version_ref(self.task_id, self.source_path)):
            raise ValueError('剧集版本决定缺少有效来源、作品、正片集号或标签')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping) or set(value) != {f.name for f in fields(cls)}:
            raise ValueError('剧集版本决定格式无效')
        return cls(**dict(value))


def from_plan(plan):
    rows = plan.metadata.get('episode_versions', ())
    if not isinstance(rows, (tuple, list)):
        raise ValueError('剧集版本决定必须是冻结清单')
    return tuple(EpisodeVersionDecision.from_mapping(r) for r in rows)


def choose(plan, state, edit):
    from . import content_selection as content
    from .plan_review import entry_id, PlanReviewService
    from .media_classifier import PreclassificationKind
    from .episode_inference import build_semantic_context
    if set(edit) != {'rows'} or not isinstance(edit['rows'], list):
        raise ValueError('剧集版本输入必须是完整选择清单')
    entries = {entry_id(e): e for e in content.material_entries(plan, state.content_selection)
               if e.media_kind == 'video' and e.source_path not in plan.ignored_sources}
    result = []
    for row in edit['rows']:
        if not isinstance(row, dict) or set(row) != {'entry_id', 'episode', 'label'} or row['entry_id'] not in entries:
            raise ValueError('请选择本任务保留的视频')
        e = entries[row['entry_id']]
        season = (state.season_binding or {}).get('season_number')
        pre = PlanReviewService._preclassify_inventory_video(item_path=e.source_path,
            candidate_name=e.source_path.name, season_id=season,
            extra_context_name=build_semantic_context(e.source_path.name, str(state.confirmed_work.get('title') or '')),
            forced_role=state.manual_role_decisions.get(row['entry_id']))
        if (pre.kind != PreclassificationKind.MAIN_CANDIDATE
                or row['entry_id'] in state.provider_special_decisions
                or any(d.source_path == str(e.source_path) for d in state.episode_ranges)):
            raise ValueError('剧集版本目前只支持普通单集正片；特别篇或连续集数请分别处理')
        result.append(EpisodeVersionDecision(str(e.source_path), plan.task_id, work_key(state.confirmed_work),
            state.initial_source_basis.fingerprint, season, row['episode'], safe_label(row['label']),
            version_ref(plan.task_id, str(e.source_path))))
    _unique(result)
    return tuple(sorted(result, key=lambda d: d.source_path))


def _unique(decisions):
    if len({d.source_path for d in decisions}) != len(decisions):
        raise ValueError('同一来源只能选择一次剧集版本')
    labels = [(d.season, d.episode, label_key(d.label)) for d in decisions]
    if len(set(labels)) != len(labels):
        raise ValueError('同一集的版本标签不能重复')


def require_compatible_edit(plan, state, mode, edit):
    if not state.episode_versions:
        return
    if mode in {'episode', 'work', 'boundary'}:
        raise ValueError('请先清除剧集版本选择，再更改作品、来源范围或集号映射')
    from .plan_review import entry_id
    selected = {entry_id(e) for e in plan.entries
                if str(e.source_path) in {d.source_path for d in state.episode_versions}}
    if mode in {'role', 'special', 'range'} and (edit.get('entry_id') in selected
            or selected.intersection(edit.get('entry_ids', ()))):
        raise ValueError('请先取消该视频的剧集版本选择，再调整用途或连续集数')


def require_new_season(service, plan, state):
    if state.asset_operation is not None:
        raise ValueError('剧集多版本暂不支持换源或已有资产操作')
    binding = state.series_binding or {}
    key = binding.get('series_key')
    if not key:
        raise ValueError('剧集版本缺少系列身份')
    from .series_catalog import SeriesCatalogError
    try:
        record = service.series_catalog.load(key)
    except SeriesCatalogError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return
        raise
    season = (state.season_binding or {}).get('season_number')
    if any(b.get('season_number') == season for b in record.season_bindings):
        raise ValueError('该季度已登记；本次只支持同一新任务内整理剧集多版本')


def project_entry(entry, decision, root, group):
    from .rename_plan import AssetRole, PresentationAssetPlanContract, build_default_version_descriptor
    from .content_selection import canonical_episode_target
    descriptor = replace(build_default_version_descriptor(group, decision.season, decision.episode),
        version_ref=decision.version_ref, technical_variant_key='manual-' + decision.version_ref,
        display_label=decision.label, identity_policy=POLICY)
    target = canonical_episode_target(root, decision.season, decision.episode,
        entry.source_path.suffix, version_label=decision.label)
    return replace(entry, season_id=decision.season, episode_id=decision.episode, episode_end=None,
        target_path=target, role='main_episode', requires_review=True,
        presentation_asset=PresentationAssetPlanContract(group, AssetRole('content_version'), None, descriptor),
        collision_resolution=entry.collision_resolution if entry.target_path == target else None)


def _entries(plan, decisions):
    from .content_associations import subtitle_entries
    from .audio_projection import audio_entries
    from . import content_selection as content
    if type(decisions) is not tuple or any(type(d) is not EpisodeVersionDecision for d in decisions):
        raise ValueError('剧集版本必须使用类型化决定')
    _unique(decisions)
    if decisions and any(d.task_id != plan.task_id or d.work_key != work_key(plan.work_identity)
            or d.source_basis != plan.initial_source_basis.fingerprint for d in decisions):
        raise ValueError('剧集版本的作品或来源已改变，请重新确认')
    root = Path(str(plan.metadata.get('canonical_work_root') or ''))
    if decisions and (not root.is_absolute() or {g.kind for g in plan.presentation_groups} != {'tv_episodes'}):
        raise ValueError('剧集版本需要已确认的剧集计划和作品目录')
    known = {d.source_path: d for d in decisions}
    present, entries, groups = set(), [], defaultdict(list)
    for e in plan.entries:
        d = known.get(str(e.source_path))
        if e.media_kind == 'video':
            descriptor = e.presentation_asset.version_descriptor if e.presentation_asset else None
            if d:
                present.add(d.source_path)
                e = project_entry(e, d, root, plan.presentation_groups[0].group_id)
            elif descriptor and descriptor.identity_policy == POLICY:
                raise ValueError('视频版本缺少人工选择依据')
            if e.episode_id is not None and e.source_path not in plan.ignored_sources:
                groups[(e.season_id, e.episode_id)].append(e)
        entries.append(e)
    if known.keys() - present - {str(p) for p in plan.ignored_sources}:
        raise ValueError('剧集版本来源不在当前计划')
    for members in groups.values():
        chosen = [e for e in members if str(e.source_path) in known]
        if chosen and len(chosen) != len(members):
            raise ValueError('同一集的所有保留版本都需填写版本标签')
    choice = content.from_plan(plan)
    if decisions and choice is not None:
        entries = audio_entries(plan, subtitle_entries(entries, choice.associations, tv_identity=True))
    return tuple(entries)


def project(plan, decisions):
    metadata = dict(plan.metadata)
    if decisions:
        metadata['episode_versions'] = [d.to_dict() for d in decisions]
    else:
        metadata.pop('episode_versions', None)
    return replace(plan, entries=_entries(plan, decisions), metadata=metadata)


def validate_projection(plan):
    from .plan_review import entry_id
    decisions = from_plan(plan)
    if _entries(plan, decisions) != plan.entries:
        raise ValueError('剧集版本与冻结视频、字幕或音轨投影不一致')
    sources = {d.source_path for d in decisions}
    return {entry_id(e) for e in plan.entries if e.media_kind == 'video' and str(e.source_path) in sources}
