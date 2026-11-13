"""Source-only TV boundary handoff, bound to the original queue request."""
from dataclasses import dataclass, replace
import hashlib
import json
import re

from .content_associations import encode
from .target_routing import TargetRootSnapshot, resolve_target_root


@dataclass(frozen=True)
class PendingTVSelection:
    canonical_json: str

    def __post_init__(self):
        from .review_operation import ProviderSelectionAwaitingUserOutcome
        value = json.loads(self.canonical_json)
        fields = {'schema_version', 'task_id', 'request_id', 'operation_fingerprint', 'original_revision',
                  'plan_fingerprint', 'decision_fingerprint', 'scope_fingerprint', 'basis_fingerprint',
                  'content_fingerprint', 'outcome', 'roots', 'roots_fingerprint'}
        if type(value) is not dict or set(value) != fields or encode(value) != self.canonical_json:
            raise ValueError('季度待确认依据字段或规范格式无效')
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise ValueError('季度待确认依据版本无效')
        if type(value['original_revision']) is not int or value['original_revision'] < 0:
            raise ValueError('季度待确认依据原版本无效')
        if any(not isinstance(value[k], str) or not value[k].strip() for k in ('task_id', 'request_id')):
            raise ValueError('季度待确认依据缺少原任务与请求')
        if any(not isinstance(value[k], str) or re.fullmatch('[0-9a-f]{64}', value[k]) is None
               for k in fields if k.endswith('_fingerprint')):
            raise ValueError('季度待确认依据指纹无效')
        outcome = ProviderSelectionAwaitingUserOutcome.from_mapping(value['outcome'])
        roots = TargetRootSnapshot.from_dict(value['roots'])
        if (outcome.to_dict() != value['outcome'] or outcome.task_id != value['task_id']
                or roots.to_dict() != value['roots'] or roots.fingerprint != value['roots_fingerprint']):
            raise ValueError('季度待确认依据的观察或目标配置不一致')

    @classmethod
    def from_mapping(cls, value):
        return cls(encode(value))

    def to_dict(self):
        return json.loads(self.canonical_json)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.canonical_json.encode('utf-8')).hexdigest()


def metadata_fingerprint(plan):
    raw = plan.metadata.get('pending_tv_selection')
    return PendingTVSelection.from_mapping(raw).fingerprint if raw is not None else None


def preview_target(proof):
    if type(proof) is not PendingTVSelection:
        raise ValueError('季度预览缺少本次冻结依据')
    value = proof.to_dict()
    return dict(schema_version=1, request_id=value['request_id'], pending_fingerprint=proof.fingerprint,
        roots=value['roots'], roots_fingerprint=value['roots_fingerprint'])


def review_target_snapshot(task):
    """One preview consumer; historical draft snapshots remain unchanged."""
    raw = task.get('provider_selection_preview_target')
    if raw is None:
        return task.get('draft_root_snapshot'), task.get('draft_root_config_fingerprint')
    value = json.loads(encode(raw))
    if (type(value) is not dict or set(value) != {'schema_version', 'request_id', 'pending_fingerprint', 'roots', 'roots_fingerprint'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['pending_fingerprint'], str)
            or re.fullmatch('[0-9a-f]{64}', value['pending_fingerprint']) is None):
        raise ValueError('本次季度预览目标依据格式无效')
    request = (task.get('review_operation') or {}).get('context') or {}
    roots = TargetRootSnapshot.from_dict(value['roots'])
    if value['request_id'] != request.get('request_id') or roots.fingerprint != value['roots_fingerprint']:
        raise ValueError('季度预览目标不属于当前请求')
    return roots.to_dict(), roots.fingerprint


def validate_preview_binding(task, proof, operation):
    from .provider_link_decision import provider_subject_cohort_request_fingerprint
    from collections.abc import Mapping
    raw = task.get('provider_selection_preview_target')
    if not isinstance(raw, Mapping) or encode(raw) != encode(preview_target(proof)):
        raise ValueError('季度预览目标与本次冻结依据不一致')
    roots_raw, fingerprint = review_target_snapshot(task)
    frozen = proof.to_dict()
    if (encode(roots_raw) != encode(frozen['roots']) or fingerprint != frozen['roots_fingerprint']
            or raw.get('pending_fingerprint') != proof.fingerprint or raw.get('request_id') != frozen['request_id']):
        raise ValueError('季度预览producer或配置依据已断开')
    if any(command.kind == 'confirm_cohort' for command in operation.commands):
        preview = operation.context.get('provider_subject_cohort_preview') or {}
        request = preview.get('request') or {}
        if (request.get('target_root_config_fingerprint') != fingerprint
                or request.get('resolved_target_root') != str(resolve_target_root('animation', 'tv_episodes', TargetRootSnapshot.from_dict(roots_raw)))
                or preview.get('request_fingerprint') != provider_subject_cohort_request_fingerprint(request)):
            raise ValueError('季度预览请求没有使用本次冻结的完整四根')


@dataclass(frozen=True)
class PendingTVContext:
    queue: object
    plan_store: object
    state_store: object
    task_root: object
    roots: object

    @classmethod
    def from_queue(cls, queue):
        from .initial_movie_completion import RootSnapshotSource
        from ..task_queue import TaskQueueManager
        if not isinstance(queue, TaskQueueManager) or type(queue.root_snapshot_source) is not RootSnapshotSource:
            raise ValueError('季度待确认缺少原队列与实际配置观察来源')
        return cls(queue, queue.plan_store, queue.decision_state_store, queue.task_root, queue.root_snapshot_source)

    @classmethod
    def from_service(cls, service):
        from .plan_review import PlanReviewService
        from .initial_movie_completion import RootSnapshotSource
        if not isinstance(service, PlanReviewService) or type(service.root_snapshot_source) is not RootSnapshotSource:
            raise ValueError('取消季度确认缺少原审核服务')
        return cls(service, service.plan_store, service.decision_state_store, service.task_root, service.root_snapshot_source)

    def validate(self, transaction):
        from ..task_queue import TaskQueueManager
        actual = type(self).from_queue(self.queue) if isinstance(self.queue, TaskQueueManager) else type(self).from_service(self.queue)
        if (actual != self or transaction.plan_store is not self.plan_store
                or transaction.state_store is not self.state_store or transaction.task_root != self.task_root):
            raise ValueError('季度待确认事务不属于原队列、任务根或配置来源')


@dataclass(frozen=True)
class PendingTVIntent:
    proof: PendingTVSelection
    context: PendingTVContext
    task_fingerprint: str
    action: str = 'awaiting'
    plan_fingerprint: str = ''


@dataclass(frozen=True)
class PendingTVPublication:
    decision: object
    draft: object


def _proof(context, current, state, outcome):
    from . import content_selection as content
    from .default_content import _origin, source_only_pending, require_unexecuted_task
    from .decision_state import DecisionStateStore
    from .plan_store import PlanStore
    from .review_operation import current_provider_selection_review_operation
    from ..utils.utils import task_record_snapshot_at
    from .task_batch_scope import TaskSourceScope
    _origin(context, current, state)
    if (not source_only_pending(current, state) or state.confirmed_work or state.series_binding is not None
            or state.pending_tv_selection is not None or state.content_selection is None
            or state.content_selection.to_dict()['policy'] != content.DEFAULT_POLICY):
        raise ValueError('季度待确认只能继承未完成的新默认来源计划')
    content.validate_plan_content(current, state)
    content.validate_source_identities(state.content_selection, plan=current)
    content.validate_omission_manifest(state.content_selection, movie_selection=None)
    _raw, task, task_fp = task_record_snapshot_at(context.task_root, current.task_id)
    require_unexecuted_task(task, recognition_worker=True)
    operation, request = current_provider_selection_review_operation(task)
    if (request.get('lifecycle') != 'pending' or request.get('expected_decision_revision') != state.revision
            or encode(request.get('candidate')) != encode(dict(outcome.candidate))
            or outcome.task_id != current.task_id):
        raise ValueError('季度待确认结果与当前实际请求不一致')
    roots = context.roots.observe()
    if str(resolve_target_root('animation', 'tv_episodes', roots)) != str(current.metadata.get('allowed_target_root')):
        raise ValueError('原动画季度目标根已变化，请重新生成计划')
    from .content_choice import _scope
    TaskSourceScope.from_mapping(task.get('task_source_scope'))
    proof = PendingTVSelection.from_mapping(dict(schema_version=1, task_id=current.task_id,
        request_id=request['request_id'], operation_fingerprint=operation.fingerprint,
        original_revision=state.revision, plan_fingerprint=PlanStore.fingerprint(current),
        decision_fingerprint=DecisionStateStore.fingerprint(state), scope_fingerprint=_scope(context, current.task_id),
        basis_fingerprint=state.initial_source_basis.fingerprint, content_fingerprint=state.content_selection.fingerprint,
        outcome=outcome.to_dict(), roots=roots.to_dict(), roots_fingerprint=roots.fingerprint))
    return proof, task_fp


def build(current, state, proof):
    from .decision_state import DecisionStateStore
    from .plan_store import PlanStore
    if type(proof) is not PendingTVSelection:
        raise ValueError('季度待确认重建缺少类型化依据')
    candidate = replace(state, pending_tv_selection=proof)
    draft = DecisionStateStore.project_plan(candidate, replace(current, projection_receipt=None,
        revision_parent_fingerprint=PlanStore.fingerprint(current), revision_action='tv_boundary_pending',
        revision_reason='保留原内容取舍，等待季度关系确认'))
    return PendingTVPublication(candidate, draft)


def prepare(queue, job, outcome):
    context = PendingTVContext.from_queue(queue)
    current = context.plan_store.load(job.task_id)
    state = context.state_store.load(job.task_id)
    proof, task_fp = _proof(context, current, state, outcome)
    value = proof.to_dict()
    if value['request_id'] != job.request_id or value['original_revision'] != job.expected_revision:
        raise ValueError('季度待确认结果已经被后来请求取代')
    return PendingTVIntent(proof, context, task_fp)


def validate_commit(transaction, envelope, terminal):
    from .review_operation import ProviderSelectionAwaitingUserOutcome
    intent, context = envelope.pending_tv_selection, transaction.pending_tv_selection_context
    previous = envelope.previous_decision
    if intent is None and context is None:
        if previous is not None and previous.pending_tv_selection is not None:
            from .initial_tv_completion import InitialTVCompletionIntent, InitialTVCompletion, validate_commit as validate_tv
            if (type(envelope.initial_tv_completion) is InitialTVCompletionIntent
                    and envelope.initial_tv_completion.inputs.mode == 'boundary'):
                if type(validate_tv(transaction, envelope)) is not InitialTVCompletion:
                    raise ValueError('完整季度确认验证已断开')
                return None
            raise ValueError('请先完成或取消当前季度关系确认；普通修订不能更改待确认任务')
        if envelope.decision.pending_tv_selection is not None or metadata_fingerprint(envelope.draft_plan) is not None:
            raise ValueError('季度待确认必须来自原队列的完整事务')
        return None
    from .projection_authority import TaskTerminalMutation
    if (type(intent) is not PendingTVIntent or type(context) is not PendingTVContext or intent.context != context
            or type(terminal) is not TaskTerminalMutation or terminal.expected_sha256 != intent.task_fingerprint):
        raise ValueError('季度待确认缺少原意图、实际服务或任务CAS双传输')
    context.validate(transaction)
    value = intent.proof.to_dict()
    current = context.plan_store.load(value['task_id'])
    state = context.state_store.load(value['task_id'])
    if intent.action == 'cancel':
        validate_cancellation(context, current, state, intent)
        expected = build_cancellation(current, state)
        if (previous != state or expected.decision != envelope.decision or expected.draft != envelope.draft_plan
                or terminal.task_id != current.task_id
                or any(item is not None for item in (envelope.initial_tv_completion, envelope.initial_movie_completion,
                    envelope.initial_movie_selection, envelope.movie_change, envelope.media_archive,
                    envelope.cohort_authority, envelope.automatic_recognition, envelope.content_correction))):
            raise ValueError('取消季度确认不能更改原内容或混入其它事务')
        return expected
    if intent.action != 'awaiting':
        raise ValueError('未知季度确认动作')
    proof, task_fp = _proof(context, current, state, ProviderSelectionAwaitingUserOutcome.from_mapping(value['outcome']))
    if proof != intent.proof or task_fp != intent.task_fingerprint or terminal.task_id != current.task_id:
        raise ValueError('季度待确认原请求、来源或目标配置已变化')
    expected = build(current, state, proof)
    if (type(expected) is not PendingTVPublication or previous != state or envelope.decision != expected.decision
            or envelope.draft_plan != expected.draft
            or any(item is not None for item in (envelope.initial_tv_completion, envelope.initial_movie_completion,
                envelope.initial_movie_selection, envelope.movie_change, envelope.media_archive,
                envelope.cohort_authority, envelope.automatic_recognition, envelope.content_correction))):
        raise ValueError('季度待确认没有使用唯一保全重建结果')
    return expected


def commit(intent, terminal):
    from .projection_authority import commit_projected_plan
    context = intent.context
    task_id = intent.proof.to_dict()['task_id']
    state = context.state_store.load(task_id)
    built = (build_cancellation(context.plan_store.load(task_id), state) if intent.action == 'cancel'
             else build(context.plan_store.load(task_id), state, intent.proof))
    return commit_projected_plan(context.state_store, context.plan_store, previous=state,
        candidate=built.decision, draft_plan=built.draft, task_terminal=terminal,
        pending_tv_selection=intent, pending_tv_selection_context=context)


def validate_terminal(previous_task, payload, decision, plan):
    """The queue's callback cannot replace the authoritative pending result."""
    from .review_operation import current_provider_selection_review_operation, TaskWorkflowState
    from .plan_store import PlanStore
    proof = decision.pending_tv_selection
    if proof is None:
        return
    frozen = proof.to_dict()
    original, request = current_provider_selection_review_operation(previous_task)
    active, result = current_provider_selection_review_operation(payload)
    validate_preview_binding(payload, proof, active)
    if (original.fingerprint != frozen['operation_fingerprint'] or request.get('request_id') != frozen['request_id']
            or active.state != TaskWorkflowState.ACTIONABLE or not active.commands
            or result.get('lifecycle') != 'awaiting_user' or result.get('request_id') != frozen['request_id']
            or result.get('result_decision_revision') != decision.revision
            or result.get('expected_decision_revision') != decision.revision
            or result.get('result_plan_fingerprint') != PlanStore.fingerprint(plan)
            or encode(result.get('provider_subject_evidence')) != encode(frozen['outcome']['provider_subject_evidence'])
            or payload.get('workflow_status') != 'need_confirm'):
        raise ValueError('季度待确认任务终态没有绑定原请求和本次完整计划')


def validate_cancellation(context, current, state, intent):
    from .plan_store import PlanStore
    from .projection_authority import validate_plan_projection_receipt
    from .review_operation import current_provider_selection_review_operation
    from ..utils.utils import task_record_snapshot_at
    validate_plan_projection_receipt(current)
    _raw, task, task_fp = task_record_snapshot_at(context.task_root, current.task_id)
    _active, request = current_provider_selection_review_operation(task)
    if (state.pending_tv_selection != intent.proof or metadata_fingerprint(current) != intent.proof.fingerprint
            or PlanStore.fingerprint(current) != intent.plan_fingerprint or task_fp != intent.task_fingerprint
            or request.get('request_id') != intent.proof.to_dict()['request_id']
            or request.get('lifecycle') != 'awaiting_user'
            or request.get('result_plan_fingerprint') != intent.plan_fingerprint):
        raise ValueError('取消动作不属于当前季度关系请求')


def build_cancellation(current, state):
    from .decision_state import DecisionStateStore
    from .plan_store import PlanStore
    candidate = replace(state, pending_tv_selection=None)
    metadata = dict(current.metadata)
    metadata.pop('pending_tv_selection', None)
    draft = DecisionStateStore.project_plan(candidate, replace(current, metadata=metadata, projection_receipt=None,
        revision_parent_fingerprint=PlanStore.fingerprint(current), revision_action='tv_boundary_cancel',
        revision_reason='取消本次季度关系确认，保留原内容取舍'))
    return PendingTVPublication(candidate, draft)


def cancel(service, task_id, fingerprint, *, request_id):
    from .content_choice import _load
    from .projection_authority import TaskTerminalMutation
    from .review_operation import current_provider_selection_review_operation, set_review_operation, transition_provider_selection_review_operation
    from ..utils.utils import task_record_snapshot_at
    current, state = _load(service, task_id, fingerprint)
    if state.pending_tv_selection is None or state.pending_tv_selection.to_dict()['request_id'] != request_id:
        raise ValueError('没有匹配的当前季度关系请求可取消')
    context = PendingTVContext.from_service(service)
    _raw, _task, task_fp = task_record_snapshot_at(service.task_root, task_id)
    intent = PendingTVIntent(state.pending_tv_selection, context, task_fp, 'cancel', fingerprint)
    validate_cancellation(context, current, state, intent)
    def close(decision, plan, task):
        active, operation = current_provider_selection_review_operation(task)
        if operation.get('request_id') != request_id or operation.get('lifecycle') != 'awaiting_user':
            raise ValueError('本次关系请求已被替换')
        set_review_operation(task, transition_provider_selection_review_operation(task,
            dict(state='cancelled', result_decision_revision=decision.revision, failure_code=None, retryable=False),
            expected_fingerprint=active.fingerprint))
        task.update(workflow_status='need_confirm', error=None, completed_at=None)
        return task
    _decision, plan = commit(intent, TaskTerminalMutation.prepare(service.task_root, task_id, close))
    return service._record_revision(plan)
