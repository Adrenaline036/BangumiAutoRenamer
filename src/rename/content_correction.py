"""Small frozen source-correction commands; never approval or media execution."""
from dataclasses import dataclass, replace
from pathlib import Path

from . import content_selection as content
from .content_choice import _load, validate_scope, _evidence
from .content_associations import ContentAssociations
from .decision_state import DecisionStateStore
from .plan_store import PlanStore
from .projection_authority import TaskTerminalMutation, commit_projected_plan
from .collision_projection import observe_path


@dataclass(frozen=True)
class ContentCorrectionIntent:
    service: object
    task_id: str
    plan_fingerprint: str
    decision_fingerprint: str
    task_fingerprint: str
    kind: str
    source_path: Path
    video_source_path: Path | None
    reason: str
    target_observation: object


@dataclass(frozen=True)
class ContentCorrectionResult:
    decision: object
    draft: object


def _current(service, task_id, fingerprint):
    from .default_content import require_unexecuted_task
    from ..utils.utils import task_record_snapshot_at
    current, state = _load(service, task_id, fingerprint)
    _raw, task, task_fingerprint = task_record_snapshot_at(service.task_root, task_id)
    require_unexecuted_task(task)
    choice = content.validate_plan_content(current, state)
    if (choice is None or choice.to_dict()['policy'] != content.DEFAULT_POLICY or not state.confirmed_work
            or state.execution_failure is not None or state.recovery_state not in (None, 'healthy')
            or state.completed_rebuild_observation is not None):
        raise ValueError('配套归属确认只用于未执行的当前默认内容计划')
    validate_scope(service, current, choice)
    content.validate_source_identities(choice, plan=current)
    content.validate_omission_manifest(choice, movie_selection=state.movie_version_selection)
    return current, state, task_fingerprint


def _audio_result(current, state, source, owner, reason):
    """Pure rebuild from the current frozen material and final video target."""
    from .audio_projection import project_audio
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('请说明本次外置音轨归属确认的理由')
    choice = state.content_selection
    value = choice.to_dict()
    relations = value['associations']
    kinds = {row['path']: row['kind'] for row in relations['sources']}
    entries = {entry.source_path: entry for entry in current.entries}
    if (kinds.get(str(source)) != 'audio' or kinds.get(str(owner)) != 'video'
            or source not in entries or owner not in entries
            or str(source) in value['excluded_sources'] or owner in current.ignored_sources):
        raise ValueError('请选择当前保留的外置音轨及所属视频，不得恢复已排除来源')
    video = entries[owner]
    if video.presentation_asset is None or video.presentation_asset.asset_role.kind == 'unknown':
        raise ValueError('所属视频尚未确认用途，请先完成视频内容确认')
    for row in relations['relations']:
        if row['source_path'] == str(source):
            row.update(owners=[str(owner)], state='linked', source='manual')
    value.update(associations=ContentAssociations.from_mapping(relations).to_dict(),
                 parent_plan_fingerprint=PlanStore.fingerprint(current), reason=reason.strip())
    # The explicit association is fed into the same projector used by movie,
    # TV and ordinary revisions; no target path can be supplied by this command.
    preliminary = content.ContentSelection.from_mapping(value)
    view = project_audio(replace(current, projection_receipt=None,
        metadata={**current.metadata, 'content_selection': preliminary.to_dict(),
                  'content_selection_fingerprint': preliminary.fingerprint}))
    revised = {entry.source_path: entry for entry in view.entries}
    if (set(entries) != set(revised) or any(revised[path] != entry for path, entry in entries.items() if path != source)):
        raise ValueError('音轨归属确认不能顺带重建其它内容')
    selected = revised[source]
    if selected.role != 'external_audio':
        raise ValueError('音轨统一投影未能建立明确关联')
    target_key = str(selected.target_path).casefold()
    if any(str(entry.target_path).casefold() == target_key for path, entry in revised.items() if path != source):
        raise ValueError('音轨新目标与其它已保留内容冲突，请先核对命名')
    value['materials'] = [content.source_material(selected) if row['source_path'] == str(source) else row
                          for row in value['materials']]
    value['evidence'] = _evidence(value, state.movie_version_selection, old=choice)
    after = content.ContentSelection.from_mapping(value)
    candidate = replace(state, content_selection=after)
    draft = DecisionStateStore.project_plan(candidate, replace(view,
        metadata={**view.metadata, 'content_selection': after.to_dict(), 'content_selection_fingerprint': after.fingerprint},
        revision_parent_fingerprint=PlanStore.fingerprint(current), revision_action='content_audio_owner',
        revision_reason=reason.strip()))
    content.validate_plan_content(draft, candidate)
    return ContentCorrectionResult(candidate, draft)


def prepare_audio(service, task_id, fingerprint, *, source_path, video_source_path, reason):
    from .plan_review import PlanReviewService
    if type(service) is not PlanReviewService:
        raise ValueError('配套归属确认需要实际内容审核服务')
    current, state, task_fingerprint = _current(service, task_id, fingerprint)
    source, video = Path(source_path), Path(video_source_path)
    result = _audio_result(current, state, source, video, reason)
    target = next(entry.target_path for entry in result.draft.entries if entry.source_path == source)
    probe = observe_path(target)
    if probe.exists:
        raise ValueError('音轨目标已经存在，当前归属确认不能覆盖已有文件')
    return ContentCorrectionIntent(service, task_id, fingerprint, DecisionStateStore.fingerprint(state),
        task_fingerprint, 'audio_owner', source, video, reason.strip(), probe)






def _build(service, intent):
    if (type(intent) is not ContentCorrectionIntent or intent.service is not service
            or intent.kind != 'audio_owner'):
        raise ValueError('配套确认缺少本服务的冻结操作')
    current, state, task_fingerprint = _current(service, intent.task_id, intent.plan_fingerprint)
    if DecisionStateStore.fingerprint(state) != intent.decision_fingerprint or task_fingerprint != intent.task_fingerprint:
        raise ValueError('配套确认的原任务或决定已变化，请重新预览')
    result = _audio_result(current, state, intent.source_path, intent.video_source_path, intent.reason)
    target = next(entry.target_path for entry in result.draft.entries if entry.source_path == intent.source_path)
    observed = observe_path(target)
    if observed != intent.target_observation or observed.exists:
        raise ValueError('音轨目标在预览后变化，请核对冲突')
    return current, state, result


def commit(service, intent):
    _current_plan, state, result = _build(service, intent)
    terminal = service._revision_task_terminal(intent.task_id)
    if terminal.expected_sha256 != intent.task_fingerprint:
        raise ValueError('任务在确认前已变化，音轨操作未生效')
    _, plan = commit_projected_plan(service.decision_state_store, service.plan_store,
        previous=state, candidate=result.decision, draft_plan=result.draft,
        content_correction=intent, task_terminal=terminal)
    return service._record_revision(plan)


def validate_commit(transaction, envelope, terminal):
    intent = envelope.content_correction
    if intent is None:
        return None
    from .plan_review import PlanReviewService
    if (type(intent) is not ContentCorrectionIntent or type(intent.service) is not PlanReviewService
            or any(value is not None for value in (envelope.automatic_recognition, envelope.initial_tv_completion,
                envelope.initial_movie_selection, envelope.initial_movie_completion, envelope.movie_change,
                envelope.media_archive)) or type(terminal) is not TaskTerminalMutation):
        raise ValueError('配套确认缺少唯一类型化命令或混入其它业务变更')
    service = intent.service
    if (transaction.plan_store.root != service.plan_store.root
            or transaction.state_store.root != service.decision_state_store.root
            or transaction.task_root != service.task_root or terminal.task_root != service.task_root
            or terminal.task_id != intent.task_id or terminal.expected_sha256 != intent.task_fingerprint):
        raise ValueError('配套确认与实际存储、任务CAS不一致')
    _current_plan, state, result = _build(service, intent)
    if (state != envelope.previous_decision or result.decision != envelope.decision
            or result.draft != envelope.draft_plan):
        raise ValueError('配套确认未使用唯一来源重建结果')
    return result
