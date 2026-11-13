"""Decision-owned content selection and source evidence, never target authority."""
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re

from .content_associations import ContentAssociations, associated_source_closure, encode, scoped_path, source_key

POLICY = 'content-selection-v1'
DEFAULT_POLICY = 'content-selection-v2'
DEFAULT_ORIGIN = 'persisted-plan-and-source-basis-v1'
ADMITTED_ORIGIN = 'admitted-source-basis-v1'
DEFAULT_FIELDS = {'origin_kind', 'initial_source_basis_fingerprint', 'material_origins'}
PARTITION_POLICY = 'content-selection-v3'
_HASH = re.compile(r'[0-9a-f]{64}')


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def source_material(entry):
    """Discard all old output paths and collision permissions at the boundary."""
    value = entry.to_dict()
    value.pop('target_path')
    value.pop('collision_resolution')
    return value


def frozen_identities(rows, paths):
    """按选择顺序读取持久清单的身份字段，不访问源路径。"""
    rows = tuple(rows)
    available = {row['source_path']: row for row in rows}
    if len(available) != len(rows):
        raise ValueError('冻结来源清单有重复身份')
    try:
        return [{key: available[str(path)][key] for key in ('source_path', 'physical_key', 'size', 'mtime_ns')}
                for path in paths]
    except KeyError as exc:
        raise ValueError('冻结来源清单缺少身份，请先核查来源') from exc


@dataclass(frozen=True)
class ContentSelection:
    payload_json: str
    _associations: ContentAssociations = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        try:
            value = json.loads(self.payload_json)
            fields = {'policy', 'task_id', 'source_root', 'inventory_fingerprint', 'scope_fingerprint',
                'origin_plan_fingerprint', 'parent_plan_fingerprint', 'reason', 'associations',
                'materials', 'identities', 'version_groups', 'excluded_sources', 'evidence'}
            if isinstance(value, dict) and value.get('policy') == DEFAULT_POLICY:
                fields |= DEFAULT_FIELDS
            if isinstance(value, dict) and value.get('policy') == PARTITION_POLICY:
                from .inherited_subtitle_edits import FIELDS
                fields |= FIELDS
                if 'inherited_movie_groups' in value:
                    fields.add('inherited_movie_groups')
            admitted = (isinstance(value, dict) and value.get('policy') == DEFAULT_POLICY
                        and value.get('origin_kind') == ADMITTED_ORIGIN)
            from .source_decision_transfer import ORIGIN, FIELD
            regrouped = value.get('policy') == DEFAULT_POLICY and value.get('origin_kind') == ORIGIN
            if regrouped:
                fields.add(FIELD)
                admitted = True
            hashes = ['inventory_fingerprint', 'scope_fingerprint']
            if not admitted:
                hashes += ['origin_plan_fingerprint', 'parent_plan_fingerprint']
            elif (value.get('origin_plan_fingerprint') is not None
                    or value.get('parent_plan_fingerprint') is not None and
                    (not isinstance(value['parent_plan_fingerprint'], str)
                     or _HASH.fullmatch(value['parent_plan_fingerprint']) is None)):
                raise ValueError('已接纳来源不能伪造原始Plan历史')
            if (not isinstance(value, dict) or set(value) != fields or value['policy'] not in {POLICY, DEFAULT_POLICY, PARTITION_POLICY}
                    or encode(value) != self.payload_json or not isinstance(value['reason'], str) or not value['reason'].strip()
                    or any(not isinstance(value[k], str) or _HASH.fullmatch(value[k]) is None for k in hashes)):
                raise ValueError('内容选择合同不完整')
            associations = ContentAssociations.from_mapping(value['associations'])
            object.__setattr__(self, '_associations', associations)
            relation_value = associations.to_dict()
            if (relation_value['task_id'], relation_value['source_root']) != (value['task_id'], value['source_root']):
                raise ValueError('内容选择与关联范围不一致')
            sources = {row['path']: row['kind'] for row in relation_value['sources']}
            if not sources or len(sources) > 10000:
                raise ValueError('内容来源数量超出安全范围')
            root = Path(value['source_root'])
            from .rename_plan import PlanEntry
            material_fields = {'source_path', 'season_id', 'episode_id', 'media_kind', 'role',
                'requires_review', 'evidence', 'subtitle', 'presentation_asset'}
            material_paths = set()
            for material in value['materials']:
                if not isinstance(material, dict) or set(material) not in (material_fields, material_fields | {'episode_end'}):
                    raise ValueError('恢复材料不得携带旧目标或碰撞许可')
                path = scoped_path(material['source_path'], root)
                if str(path) not in sources or str(path) in material_paths:
                    raise ValueError('内容来源材料缺失或重复')
                parsed = PlanEntry.from_dict({**material, 'target_path': str(root / '.unprojected' / path.name)})
                if (value['policy'] != PARTITION_POLICY and not regrouped and parsed.subtitle is not None
                        and (parsed.subtitle.inherited_edit_fingerprint or parsed.subtitle.decision_source == 'manual_transfer')):
                    raise ValueError('旧内容策略不得携带有限字幕继承引用')
                from .content_associations import source_kind
                if source_kind(parsed) != sources[str(path)]:
                    raise ValueError('内容来源种类与冻结材料不一致')
                material_paths.add(str(path))
            if material_paths != sources.keys():
                raise ValueError('内容来源材料未完整覆盖关联范围')
            if value['policy'] == DEFAULT_POLICY:
                if (value['origin_kind'] not in {DEFAULT_ORIGIN, ADMITTED_ORIGIN, ORIGIN}
                        or not isinstance(value['initial_source_basis_fingerprint'], str)
                        or _HASH.fullmatch(value['initial_source_basis_fingerprint']) is None
                        or not isinstance(value['material_origins'], list)):
                    raise ValueError('默认内容材料来源合同无效')
                origins = {}
                for row in value['material_origins']:
                    if (not isinstance(row, dict) or set(row) != {'source_path', 'kind', 'plan_fingerprint'}
                            or row['source_path'] not in sources or row['source_path'] in origins
                            or row['kind'] not in {'actual_plan_history', 'unresolved_source_observation'}):
                        raise ValueError('默认内容材料来源不完整或重复')
                    fp = row['plan_fingerprint']
                    if ((admitted and row['kind'] != 'unresolved_source_observation')
                            or (row['kind'] == 'unresolved_source_observation' and fp is not None)
                            or (row['kind'] == 'actual_plan_history'
                                and (not isinstance(fp, str) or _HASH.fullmatch(fp) is None))):
                        raise ValueError('默认内容材料历史指纹无效')
                    origins[row['source_path']] = row
                if origins.keys() != sources.keys():
                    raise ValueError('默认内容材料来源没有覆盖全部来源')
            identities = self._identity_rows(value['identities'], sources)
            if identities.keys() != sources.keys():
                raise ValueError('内容关联缺少两端物理身份')
            evidence = self._omission_rows(value['evidence'], sources)
            from .movie_version_selection import MovieGroupFacts, validate_group_facts
            group_facts = validate_group_facts(value['version_groups'],
                video_sources={path for path, kind in sources.items() if kind == 'video'})
            if (type(group_facts) is not MovieGroupFacts or group_facts.payload_json != encode(value['version_groups'])
                    or group_facts.video_sources != frozenset(path for path, kind in sources.items() if kind == 'video')):
                raise ValueError('内容来源组共享验证已断开')
            video_members = group_facts.sources
            excluded = value['excluded_sources']
            if (not isinstance(excluded, list) or sorted(set(excluded)) != excluded
                    or any(p not in sources for p in excluded)
                    or video_members.intersection(excluded)):
                raise ValueError('非版本内容选择越过视频完整组或已登记材料')
            if regrouped:
                from .source_decision_transfer import validate_selection
                validate_selection(value)
            if value['policy'] == PARTITION_POLICY:
                from .inherited_subtitle_edits import validate_selection, InheritedSelectionProof
                checked = validate_selection(value)
                if type(checked) is not InheritedSelectionProof or checked.payload_json != self.payload_json:
                    raise ValueError('有限字幕静态验证已断开')
        except (TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError('内容选择合同无效') from exc

    @staticmethod
    def _omission_rows(rows, sources):
        if not isinstance(rows, list):
            raise ValueError('排除清单格式无效')
        result = {}
        for row in rows:
            if not isinstance(row, dict) or row.get('source_path') not in sources or row['source_path'] in result:
                raise ValueError('排除清单存在未知或重复来源')
            if row.get('kind') != 'omission-v1' or set(row) != {'source_path', 'kind'}:
                raise ValueError('排除清单格式不受支持')
            result[row['source_path']] = row
        return result

    @staticmethod
    def _identity_rows(rows, sources):
        if not isinstance(rows, list):
            raise ValueError('内容来源证据集合无效')
        result = {}
        for row in rows:
            fields = {'source_path', 'physical_key', 'size', 'mtime_ns'}
            if (not isinstance(row, dict) or set(row) != fields or row['source_path'] not in sources
                    or row['source_path'] in result or not isinstance(row['physical_key'], str)
                    or not re.fullmatch(r'device:\d+:inode:[1-9]\d*', row['physical_key'])
                    or any(type(row[k]) is not int or row[k] < 0 for k in ('size', 'mtime_ns'))
            ):
                raise ValueError('内容来源物理身份无效')
            result[row['source_path']] = row
        return result

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping):
            raise ValueError('内容选择必须使用类型化合同')
        return cls(encode(value))

    def to_dict(self):
        return json.loads(self.payload_json)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.payload_json.encode('utf-8')).hexdigest()

    @property
    def associations(self):
        return self._associations


def omitted_sources(selection, movie_selection):
    if not isinstance(selection, ContentSelection):
        raise ValueError('内容选择缺少Decision权威')
    value = selection.to_dict()
    explicit = set(value['excluded_sources'])
    known_groups = {g['version_ref']: g for g in value['version_groups']}
    members = {p for g in known_groups.values() for p in g['sources']}
    if movie_selection is None:
        if members:
            raise ValueError('内容选择缺少完整电影版本权威')
    else:
        movie = movie_selection.to_dict()
        selected = set()
        for group in movie['groups']:
            proof = known_groups.get(group['version_ref'])
            # Labels/descriptors can change through an explicit current command.
            if proof is None or proof['sources'] != group['sources']:
                raise ValueError('电影版本未使用冻结完整组来源')
            selected.update(group['sources'])
        if set(movie['excluded_sources']) != members - selected:
            raise ValueError('电影排除必须只由完整视频组决定')
        explicit.update(movie['excluded_sources'])
    return set(associated_source_closure(selection.associations, explicit))


def validate_omission_manifest(selection, *, movie_selection=None):
    """Validate the excluded path set, never reopen excluded files."""
    if not isinstance(selection, ContentSelection):
        raise ValueError('缺少内容选择来源证据')
    value = selection.to_dict()
    evidence = {Path(row['source_path']): row for row in value['evidence']}
    if set(evidence) != omitted_sources(selection, movie_selection):
        raise ValueError('内容排除与排除清单不一致')
    return evidence


def validate_source_identities(selection, *, plan):
    from .projection_authority import validate_plan_projection_receipt
    validate_plan_projection_receipt(plan)
    original = from_plan(plan)
    if original is None or original.associations.to_dict()['sources'] != selection.associations.to_dict()['sources']:
        raise ValueError('内容来源种类与原计划不一致')
    rows = plan.initial_source_basis.to_dict()['sources']
    current = selection.to_dict()['identities']
    if current != frozen_identities(rows, (row['source_path'] for row in current)):
        raise ValueError('内容来源身份与原计划清单不一致')


def source_scope(task_root, task_id):
    from ..utils.utils import read_task_at
    from .task_batch_scope import TaskSourceScope
    task = read_task_at(Path(task_root), task_id)
    scope = TaskSourceScope.from_mapping(task['task_source_scope'])
    if scope.task_id != task_id:
        raise ValueError('内容来源不属于当前任务')
    return scope


def scope_fingerprint(task_root, task_id):
    return digest(source_scope(task_root, task_id).to_dict())


def validate_media_intent(plan, state, *, task_root):
    """Read-only gate for an explicit media intent, using the current content decision."""
    selection = validate_plan_content(plan, state)
    if selection is None:
        raise ValueError('媒体意图缺少当前内容决定')
    from .projection_authority import validate_plan_projection_receipt
    from .decision_state import DecisionStateStore
    validate_plan_projection_receipt(plan)
    if state is None or plan.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(state):
        raise ValueError('内容媒体意图的Decision与当前Plan不一致')
    if scope_fingerprint(task_root, plan.task_id) != selection.to_dict()['scope_fingerprint']:
        raise ValueError('内容媒体意图的任务来源范围已变化')
    validate_source_identities(selection, plan=plan)
    validate_omission_manifest(selection, movie_selection=state.movie_version_selection)
    return selection


def require_plan_binding(plan, selection):
    from .projection_authority import inventory_fingerprint_for_plan
    value = selection.to_dict()
    if (value['task_id'] != plan.task_id or Path(value['source_root']) != plan.source_root
            or value['inventory_fingerprint'] != inventory_fingerprint_for_plan(plan)):
        raise ValueError('内容选择与当前任务来源范围不一致')
    if value['policy'] in {DEFAULT_POLICY, PARTITION_POLICY}:
        basis = plan.initial_source_basis
        if (basis.fingerprint != value['initial_source_basis_fingerprint']
                or {r['source_path'] for r in basis.to_dict()['sources']} != {str(p) for p in plan.expected_sources}):
            raise ValueError('默认内容来源与完整basis不一致')
        if value['policy'] == PARTITION_POLICY:
            inherited = value['inherited_subtitle_edits']
            if inherited['contexts'].get(value['initial_source_basis_fingerprint']) != basis.to_dict():
                raise ValueError('有限字幕目的basis与实际Plan不一致')


def canonical_episode_target(work_root, season, episode, suffix, *, technical_label=None, episode_end=None, version_label=None):
    """Project frozen episode facts, never infer an episode from a filename."""
    from .utils import jellyfin_season_dir
    from .episode_range import valid_episode, numbers
    root = Path(work_root)
    if not root.is_absolute() or type(season) is not int or season < 0 or not valid_episode(episode) or (season == 0 and episode == 0):
        raise ValueError('分集内容缺少明确季度或集号')
    if technical_label is not None and technical_label not in {'2160p', '1080p', '720p', '480p'}:
        raise ValueError('分集技术版本标签必须是明确分辨率')
    if episode_end is not None:
        numbers(season, episode, episode_end)
    if version_label is not None:
        from .movie_version_selection import safe_label
        if technical_label is not None or episode_end is not None or safe_label(version_label) != version_label:
            raise ValueError('剧集版本标签无效或混用了连续集数')
    span = f'-E{episode_end:02d}' if episode_end is not None and episode_end != episode else ''
    label = version_label or technical_label
    tail = span + (f' - {label}' if label else '')
    return root / jellyfin_season_dir(season) / f'{root.name} S{season:02d}E{episode:02d}{tail}{suffix.lower()}'


def material_entries(plan, selection):
    """Hydrate source facts for projection; no old target or approval is stored."""
    from .rename_plan import PlanEntry
    require_plan_binding(plan, selection)
    current = {entry.source_path: entry for entry in plan.entries}
    root = Path(str(plan.metadata.get('canonical_work_root') or plan.metadata.get('target_root') or ''))
    if not root.is_absolute():
        raise ValueError('内容来源投影缺少当前作品根')
    result = []
    for row in selection.to_dict()['materials']:
        source = Path(row['source_path'])
        if source in current:
            result.append(current[source]); continue
        # Conservative material placeholders are not executable output.
        target = root / 'ManualReview' / source.relative_to(plan.source_root)
        if (row['media_kind'] == 'video' and type(row['episode_id']) is int and row['episode_id'] >= 0
                and {g.kind for g in plan.presentation_groups} == {'tv_episodes'}):
            # Episode facts were frozen from the original receipted inventory;
            # current official-special decisions project again afterwards.
            target = canonical_episode_target(root, plan.metadata.get('season_number'), row['episode_id'], source.suffix)
        result.append(PlanEntry.from_dict({**row, 'target_path': str(target),
            'requires_review': True, 'collision_resolution': None}))
    if set(current) - {e.source_path for e in result}:
        raise ValueError('当前计划包含未冻结来源')
    return tuple(result)




def prepare_projection(plan, selection, movie_selection):
    """Fill excluded source facts before current special/version projection."""
    require_plan_binding(plan, selection)
    omitted = omitted_sources(selection, movie_selection)
    if set(plan.ignored_sources) != omitted:
        raise ValueError('Plan-only不能更改内容排除；请使用统一内容选择')
    expected = set(plan.expected_sources) - omitted
    if not expected <= {e.source_path for e in plan.entries}:
        raise ValueError('保留内容来源缺失，不能从旧材料偷偷补回')
    return replace(plan, entries=material_entries(plan, selection), metadata={**plan.metadata,
        'content_selection': selection.to_dict(), 'content_selection_fingerprint': selection.fingerprint})


def filter_projection(plan, selection, movie_selection):
    omitted = omitted_sources(selection, movie_selection)
    from .content_associations import source_kind
    entries = tuple(e for e in plan.entries if e.source_path not in omitted)
    if any(source_kind(e) == 'other' for e in entries):
        raise ValueError('仅支持视频及配套字幕音轨，其他文件应保持不整理')
    metadata = {**plan.metadata, 'content_selection': selection.to_dict(),
                'content_selection_fingerprint': selection.fingerprint}
    if metadata.get('mapping_source') == 'inventory_smart_completion':
        from .content_policy import unresolved_video_reason
        movie = {group.kind for group in plan.presentation_groups} == {'movie_continuous'}
        # Rebuild from retained identities, not the old basename-only summary.
        unresolved = tuple(e.source_path.name for e in entries
                           if e.media_kind == 'video' and unresolved_video_reason(e, movie=movie, plan=plan))
        metadata['smart_completion_unresolved_videos'] = unresolved
        metadata['presentation_assignment_state'] = 'partial' if unresolved else 'confirmed'
    return replace(plan, entries=entries, ignored_sources=tuple(sorted(omitted, key=str)), metadata=metadata)


def from_plan(plan):
    from .rename_plan import RenamePlan
    if type(plan) is RenamePlan:
        return plan.parsed_content_selection
    raw = plan.metadata.get('content_selection')
    return ContentSelection.from_mapping(raw) if raw is not None else None


def require_legacy_writer(plan):
    if plan.metadata.get('content_selection') is not None:
        raise ValueError('此计划已使用统一内容选择；旧整体重建入口未完成迁移，不能覆盖现有选择')


def validate_plan_content(plan, state=None):
    # Decision has already constructed and validated this immutable value.
    # Compare the Plan projection exactly, without reparsing every source row.
    if state is not None and isinstance(state.content_selection, ContentSelection):
        selection = state.content_selection
        raw = plan.metadata.get('content_selection')
        if not isinstance(raw, Mapping) or encode(raw) != selection.payload_json:
            raise ValueError('内容选择与当前Decision不一致')
    else:
        selection = from_plan(plan)
    if state is not None and selection != state.content_selection:
        raise ValueError('内容选择与当前Decision不一致')
    if selection is None:
        return None
    from .movie_version_selection import from_plan as movie_from_plan
    movie = movie_from_plan(plan) if plan.metadata.get('movie_version_selection') is not None else None
    require_plan_binding(plan, selection)
    if plan.metadata.get('content_selection_fingerprint') != selection.fingerprint:
        raise ValueError('内容选择指纹不一致')
    omitted = omitted_sources(selection, movie)
    if (set(plan.ignored_sources) != omitted or {e.source_path for e in plan.entries} != set(plan.expected_sources) - omitted
            or {Path(row['source_path']) for row in selection.to_dict()['evidence']} != omitted):
        raise ValueError('当前计划没有使用唯一内容选择投影')
    return selection
