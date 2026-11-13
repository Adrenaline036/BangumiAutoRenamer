"""按冻结来源预览完整分组；只有含视频的分组创建任务。"""
from dataclasses import dataclass
import json
from pathlib import Path

from . import content_selection as content
from .content_associations import encode, partition_source_closure, source_key
from .decision_state import DecisionStateStore
from .input_directory_discovery import season_folder
from .plan_store import PlanStore


@dataclass(frozen=True)
class SourceRegroupingPreview:
    payload_json: str

    def to_dict(self):
        return json.loads(self.payload_json)

    @property
    def task_groups(self):
        return tuple(g for g in self.to_dict()['groups'] if g['video_count'])

    @property
    def fingerprint(self):
        return content.digest(self.to_dict())


def _anchor(path, root):
    for folder in path.parents:
        if folder == root:
            break
        if season_folder(folder.name) is not None:
            return folder
    return path.parent


def _payload(plan, state, groups, selected_entry_ids=None, excluded_sources=None):
    from .projection_authority import validate_plan_projection_receipt
    validate_plan_projection_receipt(plan)
    selection = content.validate_plan_content(plan, state)
    from .episode_range import require_unbound
    require_unbound(state)
    if selection is None or state.initial_source_basis is None:
        raise ValueError('当前任务缺少完整冻结来源，不能重新分组')
    value = selection.to_dict()
    sources = {row['path']: row['kind'] for row in value['associations']['sources']}
    material_paths = {row['source_path'] for row in value['materials']}
    basis = state.initial_source_basis.to_dict()
    if (material_paths != sources.keys() or material_paths != {row['source_path'] for row in basis['sources']}
            or encode(plan.metadata.get('initial_source_basis')) != encode(basis)
            or plan.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(state)):
        raise ValueError('分组材料、来源和当前决定不一致')
    extra = {}
    omitted = set()
    if selected_entry_ids is not None:
        if groups is not None:
            raise ValueError('独立作品范围由所选视频决定，不能混入整包分组')
        groups, omitted = independent_groups(plan, state, selected_entry_ids)
        extra = dict(selected_entry_ids=sorted(selected_entry_ids), omitted_sources=sorted(omitted, key=source_key))
    if excluded_sources is not None:
        if selected_entry_ids is not None or not isinstance(excluded_sources, (list, tuple)):
            raise ValueError('完整拆卡范围必须使用来源列表')
        if len(set(excluded_sources)) != len(excluded_sources) or not set(excluded_sources) <= sources.keys():
            raise ValueError('拆卡排除范围不属于当前冻结来源')
        extra['final_excluded_sources'] = sorted(excluded_sources, key=source_key)
    projected = group_sources(selection, plan.source_root, groups, omitted=omitted)
    if excluded_sources is not None:
        for group in projected:
            group['excluded_sources'] = sorted(set(group['sources']) & set(excluded_sources), key=source_key)
    return encode(dict(policy='source-regrouping-preview-v1', origin_task_id=plan.task_id,
        plan_fingerprint=PlanStore.fingerprint(plan), decision_fingerprint=DecisionStateStore.fingerprint(state),
        content_fingerprint=selection.fingerprint, basis_fingerprint=state.initial_source_basis.fingerprint,
        groups=projected, **extra))


def independent_groups(plan, state, identifiers):
    """Selected videos only; exclusions come from the existing content authority."""
    from .plan_review import entry_id
    if (not isinstance(identifiers, (list, tuple)) or not identifiers
            or any(not isinstance(i, str) or not i for i in identifiers)
            or len(identifiers) != len(set(identifiers))):
        raise ValueError('请先选择要作为独立作品整理的视频')
    selection = content.validate_plan_content(plan, state)
    from .episode_range import require_unbound
    require_unbound(state)
    if selection is None:
        raise ValueError('当前任务缺少冻结来源，请重新生成计划')
    videos = {entry_id(e): str(e.source_path) for e in plan.entries if e.media_kind == 'video'}
    if not set(identifiers) <= videos.keys():
        raise ValueError('所选视频已排除或不在当前计划中，请重新选择')
    omitted = {str(p) for p in content.omitted_sources(selection, state.movie_version_selection)}
    selected = {videos[i] for i in identifiers}
    if selected & omitted:
        raise ValueError('所选视频已排除，请先在原任务中恢复并重新核对')
    chosen = {str(p) for p in partition_source_closure(selection.associations, sorted(selected))} - omitted
    sources = {row['path']: row['kind'] for row in selection.associations.to_dict()['sources']}
    remaining = sources.keys() - chosen - omitted
    if remaining and not any(sources[p] == 'video' for p in remaining):
        raise ValueError('剩余附件归属不明确，请取消后使用“调整计划范围”完整拆卡入口')
    return [sorted(g, key=source_key) for g in (chosen, remaining) if g], omitted


def group_sources(selection, source_root, groups=None, *, omitted=()):
    """One pure complete grouping rule, also used to validate a preview."""
    if type(selection) is not content.ContentSelection:
        raise ValueError('完整分组缺少冻结内容选择')
    value = selection.to_dict()
    sources = {row['path']: row['kind'] for row in value['associations']['sources']}
    omitted = set(omitted)
    if not omitted <= sources.keys():
        raise ValueError('省略来源不属于当前冻结内容')
    if str(source_root) != value['source_root']:
        raise ValueError('分组根与冻结内容不一致')
    physical = [row['physical_key'] for row in value['identities']]
    if len(physical) != len(set(physical)):
        raise ValueError('不同位置指向同一物理来源，不能重复分组')
    videos = {p for p, kind in sources.items() if kind == 'video'}
    if groups is None:
        by_folder = {}
        for path in sorted(videos, key=source_key):
            by_folder.setdefault(_anchor(Path(path), source_root), []).append(path)
        groups = [tuple(str(p) for p in partition_source_closure(selection.associations, selected))
                  for selected in by_folder.values()]
        assigned = {p for group in groups for p in group}
        remainder = tuple(sorted(sources.keys() - assigned, key=source_key))
        if remainder:
            groups.append(remainder)
    if not isinstance(groups, (list, tuple)) or not groups or len(groups) > len(sources):
        raise ValueError('请提供完整、非空的分组列表')
    normalized, assigned = [], set()
    for group in groups:
        if (not isinstance(group, (list, tuple)) or not group or len(group) > len(sources)
                or any(not isinstance(p, str) or p not in sources for p in group)):
            raise ValueError('分组包含空组或不属于当前任务的来源')
        paths = set(group)
        if len(paths) != len(group) or assigned.intersection(paths):
            raise ValueError('同一来源不能重复分配到多个组')
        assigned.update(paths)
        normalized.append(paths)
    if assigned & omitted or assigned | omitted != sources.keys():
        raise ValueError('分组遗漏来源；排除内容和附件也必须有明确去向')
    movie_groups = list(value['version_groups'])
    movie_groups.extend(value.get('inherited_movie_groups', {}).get('groups', ()))
    result = []
    for paths in normalized:
        selected = sorted(paths & videos, key=source_key)
        if selected:
            closure = partition_source_closure(selection.associations, selected)
            if not {str(p) for p in closure} <= paths | omitted:
                raise ValueError('字幕或音轨必须与其绑定的视频放在同一组')
        for movie_group in movie_groups:
            members = set(movie_group['sources'])
            if paths & members and not members <= paths:
                raise ValueError('电影版本的全部分段必须在同一组')
        anchors = {_anchor(Path(p), source_root) for p in selected}
        label = ('不整理的杂项' if not selected else '自定义视频组' if len(anchors) != 1
                 else str(next(iter(anchors)).relative_to(source_root)))
        if label == '.':
            label = '根目录视频'
        result.append(dict(label=label, sources=sorted(paths, key=source_key), video_count=len(selected),
            excluded_sources=sorted(paths.intersection(value['excluded_sources']), key=source_key),
            reason='不创建任务，源文件保留' if not selected else '目录分组仅供核对，尚未确认各新卡的作品和季度'))
    return result


def project_preview(plan, state, groups=None, selected_entry_ids=None, excluded_sources=None):
    """Pure projection including excluded material and unresolved files."""
    return SourceRegroupingPreview(_payload(plan, state, groups, selected_entry_ids, excluded_sources))


def validate_preview(preview, plan, state, groups=None, selected_entry_ids=None, excluded_sources=None):
    if type(preview) is not SourceRegroupingPreview or preview.payload_json != _payload(plan, state, groups, selected_entry_ids, excluded_sources):
        raise ValueError('完整分组预览已断开或与当前选择不一致')
    return preview


def observe_current(manager, task_id, expected_plan_fingerprint):
    """Read actual persisted bindings only; never stat or reopen source media."""
    from .default_content import require_unexecuted_task
    from .projection_authority import inventory_fingerprint_for_plan, validate_plan_projection_receipt
    from .task_batch_scope import TaskSourceScope, TaskBatchScopeManifest
    from .plan_review import PlanReviewDecision
    if manager.is_active(task_id) or task_id in manager._pending or task_id in manager._plan_updates:
        raise ValueError('任务仍在处理，请稍后重新分组')
    manager._require_no_pending_partition(task_id)
    task = manager._read(task_id)
    require_unexecuted_task(task)
    plan = manager.plan_store.load(task_id)
    state = manager.decision_state_store.load_readonly(task_id)
    validate_plan_projection_receipt(plan)
    fingerprint = PlanStore.fingerprint(plan)
    state_fingerprint = DecisionStateStore.fingerprint(state)
    if (fingerprint != expected_plan_fingerprint or task.get('plan_fingerprint') != fingerprint
            or task.get('decision_state_fingerprint') != state_fingerprint
            or task.get('decision_state_revision') != state.revision
            or task.get('projection_receipt_fingerprint') != plan.projection_receipt.fingerprint
            or plan.metadata.get('decision_state_fingerprint') != state_fingerprint):
        raise ValueError('任务、计划或决定已变化，请刷新分组预览')
    selection = content.validate_plan_content(plan, state)
    from .episode_range import require_unbound
    require_unbound(state)
    if selection is None or state.initial_source_basis is None:
        raise ValueError('任务缺少完整冻结来源，不能直接重新分组')
    scope = TaskSourceScope.from_mapping(task.get('task_source_scope'))
    batch = manager.batch_scope_store.require_active_scope(scope, batch_id=task.get('batch_id'))
    if type(batch) is not TaskBatchScopeManifest:
        raise ValueError('来源批次校验已断开')
    basis = state.initial_source_basis.to_dict()
    known = {row.source_path: row.to_dict() for row in scope.source_identities}
    fields = ('source_path', 'location_key', 'physical_key', 'size', 'mtime_ns')
    if (scope.task_id != task_id or scope.to_dict() != basis['task_source_scope']
            or task.get('batch_id') != basis['batch_id']
            or selection.to_dict()['scope_fingerprint'] != content.digest(scope.to_dict())
            or {row['source_path']: row for row in selection.to_dict()['identities']}
               != {p: {k: row[k] for k in ('source_path', 'physical_key', 'size', 'mtime_ns')} for p, row in known.items()}):
        raise ValueError('当前任务来源范围已变化')
    ownership_store = manager.projection_commit_service.ownership_store
    owner = ownership_store.load_optional(task_id)
    if (owner is None or owner.task_id != task_id or owner.state != 'active'
            or owner.decision_revision != state.revision
            or owner.inventory_fingerprint != inventory_fingerprint_for_plan(plan)
            or len(owner.identities) != len(known)
            or {str(i.source_path): {k: i.to_dict()[k] for k in fields} for i in owner.identities} != known):
        raise ValueError('当前任务的完整来源占用已变化')
    others = ownership_store.observe_conflicts((task_id,), owner.identities)
    if type(others) is not tuple:
        raise ValueError('其它任务来源冲突检查已断开')
    review_path = PlanStore(manager.review_root).path_for(task_id)
    review_bytes = review_path.read_bytes() if review_path.exists() else None
    if review_bytes is not None:
        review = PlanReviewDecision.from_dict(json.loads(review_bytes))
        if review.task_id != task_id or review.plan_fingerprint != fingerprint or review.status.value != 'draft' or review.approved_entry_ids:
            raise ValueError('原任务已有批准或审核变化，请先回到原任务处理')
    return (encode(task), plan, state, batch, owner, tuple(sorted(others, key=lambda row: row.task_id)), review_bytes)
