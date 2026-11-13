"""Explicit default initialization from persisted history plus neutral sources.

This backend never releases the media-pending gate or silently resets v1 choices.
"""
from dataclasses import dataclass, replace
import json
from pathlib import Path

from . import content_selection as content
from .content_associations import encode, observe_content_associations
from .content_choice import _load, _scope, _evidence
from .content_policy import ContentPolicy, _scope_binding
from .decision_state import DecisionState, DecisionStateStore, DecisionCommandService
from .inventory_plan import InventoryWorkspaceObservation, project_inventory_workspace
from .media_classifier import observe_watchable_sources
from .movie_version_selection import MovieVersionSelection
from .projection_authority import inventory_fingerprint_for_plan, validate_plan_projection_receipt
from .rename_plan import PlanEntry
from .source_basis import InitialSourceBasis, validate_inheritance


@dataclass(frozen=True)
class InitialDefaultDraft:
    """Admission-backed input, not a saved Plan, revision or receipt."""
    observation: InventoryWorkspaceObservation
    basis: InitialSourceBasis
    scope_fingerprint: str
    origin_metadata_json: str = '{}'
    source_decisions: object = None

    def __post_init__(self):
        if (type(self.observation) is not InventoryWorkspaceObservation
                or type(self.basis) is not InitialSourceBasis
                or not isinstance(self.scope_fingerprint, str)
                or content._HASH.fullmatch(self.scope_fingerprint) is None):
            raise ValueError('默认首建缺少类型化库存、来源或范围')
        from .source_decision_transfer import SourceDecisionTransfer
        if self.source_decisions is not None and type(self.source_decisions) is not SourceDecisionTransfer:
            raise ValueError('拆卡首建缺少类型化决定')
        metadata = json.loads(self.origin_metadata_json)
        if (not isinstance(metadata, dict) or encode(metadata) != self.origin_metadata_json
                or set(metadata) not in (set(), {'automatic_recognition_origin', 'automatic_search_policy'})):
            raise ValueError('默认首建不能夹带其它计划元数据')


def build_initial_default(initial):
    """Pure classification/projection of one admitted inventory observation."""
    if type(initial) is not InitialDefaultDraft:
        raise ValueError('默认首建必须使用类型化来源草稿')
    observation, basis = initial.observation, initial.basis
    raw = basis.to_dict()
    from .movie_assets import inventory_from_source_basis
    inventory = inventory_from_source_basis(basis)
    files = tuple((item, observation.source_root / Path(item.relative_path)) for item in inventory.entries)
    task = json.loads(observation.task_json)
    if (observation.task_id != raw['task_id'] or str(observation.source_root) != raw['source_root']
            or observation.inventory_fingerprint != inventory.fingerprint or observation.files != files
            or task.get('provider_identity') is not None or task.get('linked_provider_identities')):
        raise ValueError('默认首建库存不等于完整已接纳来源，或夹带已确认作品')
    plan = project_inventory_workspace(observation)
    from .rename_plan import RenamePlan
    if type(plan) is not RenamePlan:
        raise ValueError('库存纯准备已断开，不能回退旧写路径')
    rows = sorted(raw['sources'], key=lambda row: row['source_path'])
    plan = replace(plan, expected_sources=tuple(Path(row['source_path']) for row in rows),
        metadata={**plan.metadata, **json.loads(initial.origin_metadata_json),
                  'initial_source_basis': raw, 'content_policy': ContentPolicy.SAFE_WATCHABLE.value})
    by_path = {str(e.source_path): e for e in plan.entries}
    entries = tuple(by_path[row['source_path']] for row in rows)
    if initial.source_decisions is not None:
        from .source_decision_transfer import apply
        entries = apply(entries, initial.source_decisions)
        plan = replace(plan, entries=entries)
    associations = observe_content_associations(entries, task_id=plan.task_id, source_root=plan.source_root)
    excluded = _known_non_watchable_sources(entries, source_root=plan.source_root, work_title='',
        associations=associations, version_paths=set(), exclude_source_images=True)
    value = dict(policy=content.DEFAULT_POLICY, task_id=plan.task_id, source_root=str(plan.source_root),
        inventory_fingerprint=inventory_fingerprint_for_plan(plan), scope_fingerprint=initial.scope_fingerprint,
        origin_plan_fingerprint=None, parent_plan_fingerprint=None,
        reason='默认安全整理可观看内容', origin_kind=content.ADMITTED_ORIGIN,
        initial_source_basis_fingerprint=basis.fingerprint,
        material_origins=[dict(source_path=row['source_path'], kind='unresolved_source_observation',
                               plan_fingerprint=None) for row in rows],
        associations=associations.to_dict(), materials=[content.source_material(e) for e in entries],
        identities=[{k: row[k] for k in ('source_path', 'physical_key', 'size', 'mtime_ns')} for row in rows],
        version_groups=[], excluded_sources=sorted(map(str, excluded)), evidence=[])
    if initial.source_decisions is not None:
        from .source_decision_transfer import ORIGIN, FIELD
        value.update(origin_kind=ORIGIN, excluded_sources=initial.source_decisions.to_dict()['excluded_sources'],
                     **{FIELD: initial.source_decisions.to_dict()})
    value['evidence'] = _evidence(value, None)
    choice = content.ContentSelection.from_mapping(value)
    candidate = DecisionState(plan.task_id, content_policy=ContentPolicy.SAFE_WATCHABLE,
                              initial_source_basis=basis, content_selection=choice)
    draft = project_decided_content(plan, candidate, choice, None,
                                    parent_fingerprint='', action='content_default')
    return candidate, draft


def validate_first_publication(service, envelope):
    """The canonical lock already refreshed basis; compare exact pure output."""
    initial = envelope.initial_default
    if (type(initial) is not InitialDefaultDraft or envelope.previous_decision is not None
            or service.plan_store.path_for(envelope.decision.task_id).exists()
            or service.state_store.path_for(envelope.decision.task_id).exists()
            or initial.basis != envelope.decision.initial_source_basis
            or initial.scope_fingerprint != _scope(service, envelope.decision.task_id)
            or envelope.episode_ownership or any(getattr(envelope, name) is not None for name in (
                'cohort_authority', 'inventory_replay_guard', 'media_archive', 'movie_change',
                'initial_movie_selection', 'initial_movie_completion', 'initial_tv_completion',
                'pending_tv_selection', 'automatic_recognition', 'content_correction'))):
        raise ValueError('默认首建范围已变化、已有计划或混入其它提交意图')
    from .source_decision_transfer import load, require_task
    from ..utils.utils import read_task_at
    require_task(initial.source_decisions, read_task_at(service.task_root, envelope.decision.task_id))
    if load(service, envelope.decision.task_id) != initial.source_decisions:
        raise ValueError('拆卡首建决定与实际保存内容不一致')
    expected = build_initial_default(initial)
    if (not isinstance(expected, tuple) or len(expected) != 2
            or (envelope.decision, envelope.draft_plan) != expected):
        raise ValueError('默认首建未使用唯一库存与内容构建结果')
    content.validate_plan_content(envelope.draft_plan, envelope.decision)
    content.validate_omission_manifest(envelope.decision.content_selection)


@dataclass(frozen=True)
class DefaultInitializationPreview:
    payload_json: str

    def __post_init__(self):
        try:
            value = json.loads(self.payload_json)
            if (not isinstance(value, dict) or set(value) != {'policy', 'task_id', 'plan_fingerprint',
                    'decision_fingerprint', 'selection', 'movie_selection'}
                    or value['policy'] != 'default-content-preview-v1' or encode(value) != self.payload_json):
                raise ValueError('默认内容预览合同无效')
            choice = content.ContentSelection.from_mapping(value['selection'])
            if (choice.to_dict()['policy'] != content.DEFAULT_POLICY or choice.to_dict()['task_id'] != value['task_id']
                    or any(not isinstance(value[k], str) or content._HASH.fullmatch(value[k]) is None
                           for k in ('plan_fingerprint', 'decision_fingerprint'))):
                raise ValueError('默认内容预览绑定无效')
            if value['movie_selection'] is not None:
                MovieVersionSelection.from_mapping(value['movie_selection'])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError('默认内容预览格式无效') from exc

    def to_dict(self):
        return json.loads(self.payload_json)

    @classmethod
    def from_mapping(cls, value):
        return cls(encode(value))


def _origin(service, plan, state):
    actual = service.plan_store.load(plan.task_id)
    if service.plan_store.fingerprint(actual) != service.plan_store.fingerprint(plan):
        raise ValueError('默认初始化的实际Plan已变化')
    return validate_origin(service, plan, state)


def validate_origin(service, plan, state):
    """核对已从当前事务读取的计划与来源，不重复加载相同记录。"""
    from .single_subject import require_single_subject_plan
    require_single_subject_plan(plan, state)
    if (state is None or state.task_id != plan.task_id
            or plan.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(state)):
        raise ValueError('默认初始化的实际Plan与Decision已变化')
    validate_plan_projection_receipt(plan)
    basis = state.initial_source_basis
    if state.content_policy != ContentPolicy.SAFE_WATCHABLE or type(basis) is not InitialSourceBasis:
        raise ValueError('默认初始化缺少冻结策略或完整来源basis')
    validate_inheritance(service, basis, _scope_binding(service, plan.task_id))
    if plan.initial_source_basis != basis:
        raise ValueError('默认初始化basis与Plan不一致')
    return basis


@dataclass(frozen=True)
class InitialMovieHistory:
    task_id: str
    plan_fingerprint: str
    groups_json: str
    excluded_versions: tuple[str, ...]
    explicit_other: tuple[str, ...]
    materials: tuple


def initial_movie_history(service, plan, state, basis):
    """Read actual revisions, never infer a removed complete group from names.

    Initial layout omissions are observations, whereas later public selection
    changes are human decisions. Newest complete group proofs take precedence;
    a partial overlap cannot safely reconstruct a restorable version.
    """
    fingerprint = service.plan_store.fingerprint(plan)
    movie = state.movie_version_selection
    if movie is None:
        return InitialMovieHistory(plan.task_id, fingerprint, '[]', (), (), ())
    current = movie.to_dict()
    chain, seen, cursor = [], set(), plan
    for _ in range(256):
        fp = service.plan_store.fingerprint(cursor)
        if fp in seen:
            raise ValueError('电影版本历史出现循环')
        seen.add(fp)
        validate_plan_projection_receipt(cursor)
        if (cursor.task_id != plan.task_id or cursor.source_root != plan.source_root
                or cursor.initial_source_basis != basis):
            raise ValueError('电影版本历史与实际来源不一致')
        raw = cursor.metadata.get('movie_version_selection')
        if raw is None:
            break
        value = MovieVersionSelection.from_mapping(raw).to_dict()
        if any(value[k] != current[k] for k in ('task_id', 'work_key', 'group_id')):
            raise ValueError('电影版本历史跨越作品或呈现组，需明确修复')
        if not chain and value != current:
            raise ValueError('电影版本历史与当前Decision不一致')
        chain.append((cursor, fp, value))
        if not cursor.revision_parent_fingerprint:
            raise ValueError('电影版本缺少实际首建历史')
        cursor = service.plan_store.load_version(plan.task_id, cursor.revision_parent_fingerprint)
    else:
        raise ValueError('电影版本历史超出有界检查范围')
    groups = list(current['groups'])
    accounted = {p for g in groups for p in g['sources']}
    excluded = set(current['excluded_sources'])
    restored_materials, explicit = {}, set()
    for index, (old, fp, value) in enumerate(chain):
        # Only newly excluded sources in an actual manual revision prove an
        # explicit choice; inherited initial SPs omissions do not.
        if old.revision_action == 'revise_movie_versions':
            if index + 1 >= len(chain):
                raise ValueError('人工电影版本选择缺少前置版本证明')
            explicit.update(set(value['excluded_sources']) - set(chain[index + 1][2]['excluded_sources']))
        for group in value['groups']:
            paths = set(group['sources'])
            if paths <= accounted:
                continue
            if paths & accounted or not paths <= excluded:
                raise ValueError('历史电影完整组与当前选择部分重叠，不能自动恢复证明')
            entries = {str(e.source_path): e for e in old.entries}
            if not paths <= entries.keys():
                raise ValueError('历史电影完整组缺少实际Plan材料')
            groups.append(group)
            accounted.update(paths)
            for path in paths:
                restored_materials[path] = (entries[path], fp)
    removed_versions = excluded & accounted
    return InitialMovieHistory(plan.task_id, fingerprint, encode(groups),
        tuple(sorted(removed_versions)), tuple(sorted((explicit & excluded) - accounted)),
        tuple(restored_materials.values()))


def _known_non_watchable_sources(entries, *, source_root, work_title, associations, version_paths,
                                 exclude_source_images=False):
    """One observation rule for default selection and its restoration boundary."""
    kinds = {r['path']: r['kind'] for r in associations.to_dict()['sources']}
    relations = {r['source_path']: r for r in associations.to_dict()['relations']}
    protected, linked = {}, {}
    for entry in entries:
        path = entry.source_path
        relation = relations.get(str(path))
        if relation and len(relation['owners']) == 1:
            linked[path] = relation['owners'][0]
        asset_role = entry.presentation_asset.asset_role.kind if entry.presentation_asset else None
        protected[path] = ('content_version' if str(path) in version_paths or asset_role == 'content_version' else
            'provider_special' if entry.role == 'provider_special' or asset_role == 'provider_special' else None)
    return frozenset(row.source_path for row in observe_watchable_sources(
        (entry.source_path for entry in entries), source_root=source_root, work_title=work_title,
        protected_roles=protected, linked_videos=linked,
        exclude_source_images=exclude_source_images) if row.disposition == 'known_non_watchable')


def non_watchable_selection_sources(choice, *, work_title=''):
    """Read frozen v2 materials only; do not reinterpret legacy selections."""
    value = choice.to_dict()
    if value['policy'] != content.DEFAULT_POLICY:
        return frozenset()
    entries = tuple(PlanEntry.from_dict({**row, 'target_path': row['source_path']}) for row in value['materials'])
    return _known_non_watchable_sources(entries, source_root=value['source_root'], work_title=work_title,
        associations=choice.associations, version_paths={p for group in value['version_groups'] for p in group['sources']})


def build_default_initialization(service, plan, state):
    basis = _origin(service, plan, state)
    if state.content_selection is not None:
        raise ValueError('已有人工或v2内容选择不能重新套用默认策略')
    fingerprint = service.plan_store.fingerprint(plan)
    movie_history = initial_movie_history(service, plan, state, basis)
    if (type(movie_history) is not InitialMovieHistory or movie_history.task_id != plan.task_id
            or movie_history.plan_fingerprint != fingerprint):
        raise ValueError('默认初始化缺少实际电影版本历史证明')
    sources = {Path(r['source_path']): r for r in basis.to_dict()['sources']}
    history = {}
    origin_fp = (plan.metadata.get('inventory_completion_context') or {}).get('origin_plan_fingerprint')
    if origin_fp:
        origin = service.plan_store.load_version(plan.task_id, origin_fp)
        validate_plan_projection_receipt(origin)
        if (origin.task_id != plan.task_id or origin.source_root != plan.source_root
                or origin.initial_source_basis != basis):
            raise ValueError('默认初始化库存历史来源不一致')
        history.update({e.source_path: (e, origin_fp) for e in origin.entries})
    history.update({e.source_path: (e, fp) for e, fp in movie_history.materials})
    history.update({e.source_path: (e, fingerprint) for e in plan.entries})
    if set(history) - sources.keys():
        raise ValueError('历史材料超出完整来源basis')
    entries, origins = [], []
    for path, source in sorted(sources.items(), key=lambda item: str(item[0])):
        old = history.get(path)
        entry = old[0] if old is not None else PlanEntry(path, path,
            media_kind=source['kind'] if source['kind'] in {'video', 'subtitle'} else 'other', requires_review=True)
        entries.append(entry)
        origins.append(dict(source_path=str(path), kind='actual_plan_history' if old else 'unresolved_source_observation',
                            plan_fingerprint=old[1] if old else None))
    associations = observe_content_associations(entries, task_id=plan.task_id, source_root=plan.source_root)
    movie = state.movie_version_selection
    groups = json.loads(movie_history.groups_json)
    if movie is not None:
        movie = MovieVersionSelection.from_mapping({**movie.to_dict(),
            'excluded_sources': list(movie_history.excluded_versions)})
    version_paths = {p for group in groups for p in group['sources']}
    excluded = set(movie_history.explicit_other)
    excluded.update(map(str, _known_non_watchable_sources(entries, source_root=plan.source_root,
        work_title=str(state.confirmed_work.get('title') or ''), associations=associations, version_paths=version_paths,
        exclude_source_images=True)))
    complete = replace(plan, expected_sources=tuple(sorted(sources, key=str)))
    value = dict(policy=content.DEFAULT_POLICY, task_id=plan.task_id, source_root=str(plan.source_root),
        inventory_fingerprint=inventory_fingerprint_for_plan(complete), scope_fingerprint=_scope(service, plan.task_id),
        origin_plan_fingerprint=fingerprint, parent_plan_fingerprint=fingerprint,
        reason='默认安全整理可观看内容', origin_kind=content.DEFAULT_ORIGIN,
        initial_source_basis_fingerprint=basis.fingerprint, material_origins=origins,
        associations=associations.to_dict(), materials=[content.source_material(e) for e in entries],
        identities=content.frozen_identities(sources.values(), (e.source_path for e in entries)), version_groups=groups,
        excluded_sources=sorted(excluded), evidence=[])
    value['evidence'] = _evidence(value, movie)
    return content.ContentSelection.from_mapping(value), movie


def build_default_draft(service, plan, state, choice, movie):
    return project_decided_content(plan, state, choice, movie,
        parent_fingerprint=service.plan_store.fingerprint(plan), action='content_default')


def project_decided_content(plan, state, choice, movie, *, parent_fingerprint, action):
    """Pure projection of already decided materials, never default classification."""
    complete = replace(plan, expected_sources=tuple(Path(r['source_path']) for r in choice.to_dict()['materials']))
    omitted = content.omitted_sources(choice, movie)
    revised = replace(complete, entries=content.material_entries(complete, choice),
        ignored_sources=tuple(sorted(omitted, key=str)), projection_receipt=None,
        metadata={**complete.metadata, 'content_selection': choice.to_dict(), 'content_selection_fingerprint': choice.fingerprint},
        revision_parent_fingerprint=parent_fingerprint, revision_action=action,
        revision_reason=choice.to_dict()['reason'])
    return DecisionStateStore.project_plan(replace(state, content_selection=choice, movie_version_selection=movie), revised)


def validate_initial_draft(service, plan, state, choice, movie, draft):
    expected = build_default_draft(service, plan, state, choice, movie)
    if (draft.expected_sources != expected.expected_sources or draft.ignored_sources != expected.ignored_sources
            or draft.entries != expected.entries or draft.presentation_groups != expected.presentation_groups
            or draft.subtitle_override_rules != expected.subtitle_override_rules):
        raise ValueError('默认初始化的实际Plan未使用完整冻结材料投影')


def source_only_pending(plan, state):
    # Inventory placeholders can contain a provisional tv_episodes display
    # group. A display group is not a confirmed work.
    return (plan.metadata.get('mapping_source') == 'inventory_review_draft'
            and not state.confirmed_work and state.layout_mode is None
            and state.movie_version_selection is None and state.tv_planning_authority is None)


def require_unexecuted_task(task, *, recognition_worker=False):
    """Shared actual task-risk predicate; planned asset intents are not execution."""
    forbidden = {'executing', 'completed', 'partial_failed', 'catalog_update_pending'}
    if not recognition_worker:
        from .review_operation import build_review_operation
        from .selection_gate import validate_selection_for_plan
        operation, _ = build_review_operation(task or {})
        selection = operation.context
        if selection.get('kind') == 'provider_selection' and selection.get('lifecycle') in {'pending', 'failed', 'awaiting_season'}:
            validate_selection_for_plan(task)
        forbidden.update({'queued', 'running', 'processing'})
    _require_task_risk(task, forbidden)


def _require_task_risk(task, forbidden):
    if (not task or task.get('workflow_status') in forbidden or task.get('queue_operation') == 'execute_approved'
            or any(task.get(key) not in (None, False, '') for key in (
                'execution_status', 'execution_phase', 'execution_requested_fingerprint',
                'execution_requires_manual_check', 'execution_may_have_written_targets',
                'execution_recovery_disposition'))):
        raise ValueError('实际任务已有执行或恢复风险，不能作为未完成来源编辑')


def require_content_edit(task, plan, state):
    from .review_operation import build_review_operation
    operation, _ = build_review_operation(task or {})
    context = operation.context
    permitted = (source_only_pending(plan, state)
        and context.get('kind') == 'provider_selection'
        and context.get('lifecycle') in {'failed', 'awaiting_season'}
        and 'review_operation_invalid' not in operation.blocking_reasons)
    if permitted:
        _require_task_risk(task, {'executing', 'completed', 'partial_failed',
            'catalog_update_pending', 'queued', 'running', 'processing'})
    else:
        require_unexecuted_task(task)


def unfinished_source_edit(service, plan, state, *, content_only=False):
    """Actual receipted source editing, not a completion or execution permit."""
    if (state is None or state.content_selection is None
            or state.content_selection.to_dict()['policy'] != content.DEFAULT_POLICY
            or plan.metadata.get('mapping_source') != 'inventory_review_draft'):
        return False
    _origin(service, plan, state)
    if (state.layout_mode is not None or state.movie_version_selection is not None
            or state.tv_planning_authority is not None
            or state.recovery_state not in (None, 'healthy')
            or any(getattr(state, name) is not None for name in (
                'completed_rebuild_observation', 'asset_operation',
                'execution_failure', 'series_binding', 'catalog_link_intent'))):
        raise ValueError('未完成来源编辑不能包含布局、执行或恢复决定')
    content.validate_plan_content(plan, state)
    from ..utils.utils import read_task_at
    task = read_task_at(service.task_root, plan.task_id)
    if content_only:
        require_content_edit(task, plan, state)
    else:
        require_unexecuted_task(task)
    return True


def prepare(service, task_id, fingerprint):
    plan, state = _load(service, task_id, fingerprint)
    if state.content_selection is not None:
        _origin(service, plan, state)
        if state.content_selection.to_dict()['policy'] != content.DEFAULT_POLICY:
            raise ValueError('已有v1人工内容选择，不能重套默认策略')
        content.validate_plan_content(plan, state)
        choice, movie = state.content_selection, state.movie_version_selection
    else:
        choice, movie = build_default_initialization(service, plan, state)
    return DefaultInitializationPreview.from_mapping(dict(policy='default-content-preview-v1', task_id=task_id,
        plan_fingerprint=fingerprint, decision_fingerprint=DecisionStateStore.fingerprint(state),
        selection=choice.to_dict(), movie_selection=movie.to_dict() if movie else None))


def commit(service, preview):
    if type(preview) is not DefaultInitializationPreview:
        raise ValueError('默认初始化必须确认冻结预览')
    value = preview.to_dict()
    plan, state = _load(service, value['task_id'], value['plan_fingerprint'])
    _origin(service, plan, state)
    if DecisionStateStore.fingerprint(state) != value['decision_fingerprint']:
        raise ValueError('默认内容预览或来源证据已变化')
    if state.content_selection is not None:
        if (state.content_selection.to_dict()['policy'] != content.DEFAULT_POLICY
                or state.content_selection.to_dict() != value['selection']
                or (state.movie_version_selection.to_dict() if state.movie_version_selection else None)
                   != value['movie_selection']):
            raise ValueError('默认内容预览或既有人工选择已变化')
        content.validate_plan_content(plan, state)
        return plan
    # The canonical transaction independently rebuilds and compares selection,
    # sources and draft under its lock. Do not rebuild once more before it.
    choice = content.ContentSelection.from_mapping(value['selection'])
    movie = MovieVersionSelection.from_mapping(value['movie_selection']) if value['movie_selection'] else None
    draft = build_default_draft(service, plan, state, choice, movie)
    _, projected = DecisionCommandService(service.decision_state_store, service.plan_store).update(
        plan.task_id, expected_revision=state.revision,
        changes=dict(content_selection=choice, movie_version_selection=movie),
        planner=lambda _: draft, task_terminal=service._revision_task_terminal(plan.task_id))
    return service._record_revision(projected)
