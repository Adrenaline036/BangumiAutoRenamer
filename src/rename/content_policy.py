"""Frozen task policy and media execution requirements.

S4A deliberately cannot certify initial safe-content selection. A legacy v1
content migration is not that certificate. Media remains pending until S4B.
"""
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path


class ContentPolicy(str, Enum):
    SAFE_WATCHABLE = 'safe-watchable-content-v1'


def parse(value):
    if type(value) is not str:
        raise ValueError('默认整理策略 policy 格式无效')
    try:
        return ContentPolicy(value)
    except ValueError:
        raise ValueError('默认整理策略 policy 版本不受支持') from None


def require_typed(value):
    if value is not None and type(value) is not ContentPolicy:
        raise ValueError('默认整理策略必须是类型化 policy')


def _history(store, task_id):
    """Use immutable requests also after a reservation has been retired."""
    found = [(manifest, request) for manifest in store._all_manifests()
             for request in manifest.requests if request.task_id == task_id]
    policies = {request.content_policy for _, request in found}
    if len(policies) > 1:
        raise ValueError('任务历史整理策略 policy 不一致')
    return found


def policy_for_admission(manager, request):
    from .task_batch_scope import TaskSourceScope
    require_typed(request.content_policy)
    task = manager._read(request.task_id)
    state = manager.decision_state_store.load_optional(request.task_id)
    history = _history(manager.batch_scope_store, request.task_id)
    if not task:
        if state is not None or history or manager.plan_store.path_for(request.task_id).exists():
            raise ValueError('历史任务记录缺失，不能重新猜测默认整理策略')
        policy = ContentPolicy.SAFE_WATCHABLE
    else:
        raw_scope = task.get('task_source_scope')
        scope = TaskSourceScope.from_mapping(raw_scope) if raw_scope is not None else None
        policy = scope.content_policy if scope is not None else None
        if history:
            if policy != history[0][1].content_policy or scope is None:
                raise ValueError('任务整理策略与历史 scope 不一致')
            matching = [m for m, _ in history if m.batch_scope_fingerprint == scope.batch_scope_fingerprint]
            if (scope.task_id != request.task_id or len(matching) != 1
                    or task.get('batch_id') != matching[0].batch_id):
                raise ValueError('旧任务 scope 历史无法确认，不能继承策略')
        elif scope is not None:
            raise ValueError('旧任务 scope 缺少真实历史，不能继承策略')
        else:
            has_plan = manager.plan_store.path_for(request.task_id).exists()
            if state is None and not has_plan:
                # Public preview creates a task JSON before real admission.
                # A record, flag or caller label alone is not legacy authority.
                policy = ContentPolicy.SAFE_WATCHABLE
            else:
                from .projection_authority import validate_plan_projection_receipt
                if state is None or not has_plan or state.content_policy is not None:
                    raise ValueError('无scope旧任务缺少完整历史策略证明')
                plan = manager.plan_store.load(request.task_id)
                validate_plan_projection_receipt(plan)
                if (plan.metadata.get('content_policy') is not None
                        or plan.metadata.get('decision_state_fingerprint') != manager.decision_state_store.fingerprint(state)):
                    raise ValueError('无scope旧任务的Plan与Decision策略证明不一致')
        if state is not None and state.content_policy != policy:
            raise ValueError('任务与Decision整理策略 policy 不一致')
    if request.content_policy is not None and request.content_policy != policy:
        raise ValueError('请求不能更换已有任务整理策略 policy')
    return replace(request, content_policy=policy)


@dataclass(frozen=True)
class PolicyBinding:
    task_id: str
    policy: ContentPolicy | None
    scope: object = None
    batch_id: str | None = None
    source_root: str | None = None


def _scope_policy(service, task_id):
    return _scope_binding(service, task_id).policy


def _scope_binding(service, task_id):
    from ..utils.utils import read_task_at
    from .task_batch_scope import TaskBatchScopeManifestStore, TaskSourceScope
    store = TaskBatchScopeManifestStore(service.plan_store.root.parent / 'task_batch_scope')
    history = _history(store, task_id)
    task = read_task_at(service.task_root, task_id) or {}
    raw = task.get('task_source_scope')
    scope = TaskSourceScope.from_mapping(raw) if raw is not None else None
    if scope is None:
        if history and history[0][1].content_policy is not None:
            raise ValueError('新策略任务缺少真实 task scope，不能降级')
        return PolicyBinding(task_id, None)
    if scope.task_id != task_id:
        raise ValueError('任务整理策略 scope 归属不符')
    if scope.content_policy is not None or (history and history[0][1].content_policy is not None):
        store.require_active_scope(scope, batch_id=task.get('batch_id'))
    # Existing legacy operations that consume retired scopes keep their original
    # physical/recovery gates; this policy layer does not reactivate them.
    if history and scope.content_policy != history[0][1].content_policy:
        raise ValueError('任务整理策略与manifest历史不一致')
    if scope.content_policy is None:
        return PolicyBinding(task_id, None)
    from .source_basis import root_for_task
    return PolicyBinding(task_id, scope.content_policy, scope, task.get('batch_id'), root_for_task(task, scope))




def validate_commit(service, envelope):
    previous, candidate, draft = envelope.previous_decision, envelope.decision, envelope.draft_plan
    binding = _scope_binding(service, candidate.task_id)
    policy = binding.policy
    require_typed(candidate.content_policy)
    if previous is not None:
        if previous.content_policy != policy or candidate.content_policy != policy:
            raise ValueError('既有Decision整理策略 policy 不得清除或重置')
        if draft.metadata.get('content_policy') != (policy.value if policy else None):
            raise ValueError('Plan整理策略 policy 与Decision不一致')
    elif candidate.content_policy not in (None, policy):
        raise ValueError('首次Decision不能指定不同整理策略 policy')
    elif draft.metadata.get('content_policy') not in (None, policy.value if policy else None):
        raise ValueError('首次Plan整理策略 policy 与scope不一致')
    return binding


def bind_commit(service, envelope):
    binding = validate_commit(service, envelope)
    if type(binding) is not PolicyBinding or binding.task_id != envelope.decision.task_id:
        raise ValueError('默认整理策略 policy 验证器已断开')
    envelope = replace(envelope, decision=replace(envelope.decision, content_policy=binding.policy))
    from .source_basis import bind_initial_source_basis
    return bind_initial_source_basis(service, envelope, binding=binding)


def validate_state_write(store, current, candidate):
    """Shared direct/no-Plan writer guard; never silently assigns policy."""
    from .task_batch_scope import TaskBatchScopeManifestStore
    from .inherited_subtitle_edits import require_unchanged, InheritanceWriteCheck
    old = current.content_selection if current is not None else None
    checked = require_unchanged(old, candidate.content_selection)
    if (type(checked) is not InheritanceWriteCheck or checked.previous is not old
            or checked.candidate is not candidate.content_selection):
        raise ValueError('有限字幕直接写入保护已断开')
    require_typed(candidate.content_policy)
    history = _history(TaskBatchScopeManifestStore(store.root.parent / 'task_batch_scope'), candidate.task_id)
    expected = history[0][1].content_policy if history else (current.content_policy if current else None)
    if candidate.content_policy != expected or (current is not None and current.content_policy != expected):
        raise ValueError('Decision直接写入不能丢失或更换整理策略 policy')
    if candidate.content_selection is not None:
        from .inherited_subtitle_edits import POLICY
        from .source_decision_transfer import ORIGIN
        value = candidate.content_selection.to_dict()
        if value['policy'] == POLICY or value.get('origin_kind') == ORIGIN:
            from .source_basis import require_prepared_state_publication
            if require_prepared_state_publication(store, current, candidate) is not candidate:
                raise ValueError('有限字幕实际prepared发布证明已断开')
    return candidate


def require_media_ready(plan):
    """Media execution gate."""
    from .single_subject import require_single_subject_plan
    require_single_subject_plan(plan)
    if 'content_policy' not in plan.metadata:
        if (plan.metadata.get('initial_source_basis') is not None or plan.metadata.get('content_selection') is not None
                or plan.projection_receipt is not None and plan.projection_receipt.authority.get('content_policy') is not None):
            raise ValueError('新内容计划不能删除整理策略以绕过媒体核查')
        return  # Historical no-policy contract retains all original gates.
    from .projection_authority import validate_plan_projection_receipt
    validate_plan_projection_receipt(plan)
    require_content_complete(plan)


def unresolved_video_reason(entry, *, movie, plan=None):
    """Shared current-entry qualification; never match diagnostic display names."""
    from .episode_range import valid_episode
    asset = entry.presentation_asset
    if asset is not None and asset.asset_role.kind == 'unknown':
        return '仍有未知用途内容，请选择用途或排除后再审核'
    if asset is None or entry.role in {None, '', 'unknown'}:
        return '视频用途尚未明确，请在内容面板修正'
    if not movie and asset.asset_role.kind == 'content_version' and (
            type(entry.season_id) is not int or entry.season_id < 0
            or not valid_episode(entry.episode_id)):
        from .special_selection import validate_descriptive_special
        if validate_descriptive_special(plan, entry):
            return None
        return '剧集内容仍缺少明确季号或集号'
    return None


def require_content_complete(plan):
    """Pure semantic qualification, also usable before signing a draft.

    This is not execution admission: public media approval/execution additionally
    requires the signed Plan, current Decision, source evidence and all existing
    verifier/Catalog/recovery gates.
    """
    from .work_affiliation import pending_from_plan
    if pending_from_plan(plan) is not None:
        raise ValueError('请先确认归入已有作品或作为另一部作品整理，再批准计划')
    if 'content_policy' in plan.metadata:
        parse(plan.metadata['content_policy'])
    else:
        return
    from . import content_selection as content
    choice = content.validate_plan_content(plan)
    if choice is None or choice.to_dict()['policy'] != content.DEFAULT_POLICY:
        raise ValueError('默认内容方案尚待生成或需要保留人工选择升级（content_policy_pending）')
    work = plan.work_identity
    if (work is None or work.status != 'confirmed' or work.domain not in {'animation', 'general'}
            or not work.title or not work.primary_provider.provider_id):
        raise ValueError('请先确认作品；默认草案不能批准或执行媒体')
    groups = plan.presentation_groups
    if len(groups) != 1 or groups[0].kind not in {'tv_episodes', 'movie_continuous'}:
        raise ValueError('请先确认影视观看形式并分别处理不同作品')
    if plan.metadata.get('mapping_source') != 'inventory_smart_completion':
        raise ValueError('来源尚未经过完整内容规划，请先完成内容确认')
    if (plan.metadata.get('smart_completion_unresolved_videos')
            or plan.metadata.get('episode_assignment_conflict_sources')
            or plan.metadata.get('asset_collision_unresolved')):
        raise ValueError('仍有未确认的视频、集号或目标冲突，请在内容面板修正')
    entries = {str(entry.source_path): entry for entry in plan.entries}
    videos = {path: entry for path, entry in entries.items() if entry.media_kind == 'video'}
    if not videos:
        raise ValueError('尚无可观看视频，不能批准空媒体任务')
    relations = choice.associations.to_dict()
    kinds = {row['path']: row['kind'] for row in relations['sources']}
    owners = {row['source_path']: row for row in relations['relations']}
    movie = groups[0].kind == 'movie_continuous'
    if movie:
        from .movie_version_selection import validate_selection
        selection = validate_selection(plan)
        if not selection.to_dict()['groups']:
            raise ValueError('电影尚未确认任何完整版本')
    else:
        season = plan.metadata.get('season_number')
        if (type(season) is not int or season < 0 or not plan.metadata.get('season_binding_id')
                or plan.metadata.get('season_source') in {None, '', 'unset', 'legacy_placeholder'}):
            raise ValueError('请确认作品季度，不能使用未裁决的默认季号')
    for path, entry in entries.items():
        asset = entry.presentation_asset
        if asset is not None and asset.asset_role.kind == 'unknown':
            raise ValueError('仍有未知用途内容，请选择用途或排除后再审核')
        kind = kinds.get(path)
        if entry.media_kind == 'video':
            reason = unresolved_video_reason(entry, movie=movie, plan=plan)
            if reason:
                raise ValueError(reason)
        elif kind in {'subtitle', 'audio'}:
            relation = owners.get(path)
            if (relation is None or relation['state'] != 'linked' or len(relation['owners']) != 1
                    or relation['owners'][0] not in videos):
                raise ValueError('字幕或外置音轨尚未关联到唯一保留视频')
            if kind == 'subtitle' and (entry.subtitle is None
                    or str(entry.subtitle.associated_video_source_path) != relation['owners'][0]):
                raise ValueError('字幕材料与已确认的视频关联不一致')
            if kind == 'audio' and entry.role != 'external_audio':
                raise ValueError('外置音轨尚未完成统一关联投影')
        elif kind == 'other' and entry.role != 'subtitle_archive':
            # validate_selection above has checked the canonical movie
            # projection, including the shared auxiliary target and asset.
            if not (movie and entry.media_kind == 'associated' and asset is not None
                    and asset.asset_role.kind == 'associated'
                    and asset.presentation_group == groups[0].group_id
                    and asset.owner_version_ref == 'shared'):
                raise ValueError('仍有未分类配套资源，请明确用途或排除')
