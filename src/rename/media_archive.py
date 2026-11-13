"""Media-history preparation carried into the canonical projection transaction.

An intent owns evidence, never output roots or callbacks. Its runtime context
comes from the existing configured service; publication reuses its old writer.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path

from .completed_history import read_bytes
from .decision_state import DecisionStateStore
from .plan_store import PlanStore
from .projection_authority import ProjectionAuthorityError
from ..utils.utils import safe_task_path


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class MediaArchiveContext:
    service: object
    plan_store: object
    state_store: object
    task_root: Path
    record_root: Path
    family: str
    reentry_review: object = None

    @classmethod
    def for_rebuild(cls, service, *, review_service=None):
        from .completed_rebuild import CompletedRebuildService
        from .plan_review import PlanReviewService
        if not isinstance(service, CompletedRebuildService):
            raise ValueError('媒体历史缺少已配置重建服务')
        if review_service is not None and type(review_service) is not PlanReviewService:
            raise ValueError('重新加入来源需要实际审核服务上下文')
        return cls(service, service.plans, service.decisions, Path(service.tasks), Path(service.records),
                   'rebuild', review_service)

    @classmethod
    def for_execution(cls, service):
        from .plan_execution import PlanExecutionService
        if not isinstance(service, PlanExecutionService):
            raise ValueError('媒体历史缺少已配置执行服务')
        return cls(service, service.plan_store, service.decision_state_store,
            Path(service.task_root), Path(service.record_root), 'execution')

    def validate(self, transaction):
        current = (type(self).for_rebuild(self.service, review_service=self.reentry_review) if self.family == 'rebuild'
                   else type(self).for_execution(self.service) if self.family == 'execution' else None)
        if (current != self or self.plan_store is not transaction.plan_store
                or self.state_store is not transaction.state_store
                or self.task_root != transaction.task_root):
            raise ValueError('媒体历史服务配置或任务存储已变化')


@dataclass(frozen=True)
class MediaArchiveIntent:
    task_id: str
    plan_fingerprint: str
    decision_fingerprint: str
    task_sha256: str
    action: str
    archive_fingerprint: str
    ledger_bytes: bytes | None = None
    mapping_sha256: str | None = None
    audit_files: tuple[tuple[str, bytes | None], ...] = ()
    reentry_fingerprint: str | None = None

    @classmethod
    def prepare(cls, context, plan, state, *, action, archive_fingerprint='',
                ledger_bytes=None, mapping_sha256=None, audit_files=None):
        if type(context) is not MediaArchiveContext:
            raise ValueError('媒体历史缺少类型化服务上下文')
        return cls(plan.task_id, PlanStore.fingerprint(plan), DecisionStateStore.fingerprint(state),
            _sha(read_bytes(safe_task_path(context.task_root, plan.task_id))), action,
            archive_fingerprint, ledger_bytes, mapping_sha256,
            tuple(sorted((audit_files or {}).items())),
            _reentry_evidence(context, plan.task_id) if action == 'completed_source_reentry' else None)

    @property
    def audit_key(self):
        from .rolled_back_restart import audit_payload
        return _sha(audit_payload(self.task_id, 'restart-after-rollback', dict(self.audit_files)))

    def publish(self, context):
        # These aliases are the producers' original publication functions;
        # no second file writer or user-supplied callable is introduced.
        if self.action == 'restart_after_rollback':
            from .rolled_back_restart import archive_evidence
            key = archive_evidence(context, self.task_id, 'restart-after-rollback', dict(self.audit_files))
            if key != self.audit_key:
                raise ValueError('媒体历史审计身份不一致')
        else:
            if self.action == 'completed_source_reentry':
                from .completed_source_reentry import archive_exact
            else:
                from .completed_rebuild import archive_exact
            archive_exact(context.record_root, self.task_id, self.archive_fingerprint,
                self.ledger_bytes, self.mapping_sha256)


def _reentry_evidence(context, task_id):
    if context.family != 'rebuild' or context.reentry_review is None:
        raise ValueError('重新加入来源缺少绑定的审核上下文')
    from .completed_source_reentry import reentry_evidence
    return reentry_evidence(context.service, context.reentry_review, task_id)


def archive_required(envelope, task_terminal):
    previous, candidate = envelope.previous_decision, envelope.decision
    if previous is None:
        return False
    if (candidate.completed_rebuild_observation is not None
            and candidate.completed_rebuild_observation != previous.completed_rebuild_observation):
        return True
    if task_terminal is not None:
        before = task_terminal.previous_payload
        if before.get('execution_status') in {'completed', 'rolled_back'}:
            # The existing builder is a pure task projection. A new execution
            # epoch is a semantic transition, even if its action label is hidden.
            after = task_terminal.builder(candidate, envelope.draft_plan, dict(before))
            if after.get('execution_status') != before.get('execution_status'):
                return True
    return False


def validate_archive_commit(transaction, envelope, task_terminal):
    """Called inside projection locks, immediately before publication."""
    intent = envelope.media_archive
    needed = archive_required(envelope, task_terminal)
    if intent is None:
        if needed:
            raise ProjectionAuthorityError('新媒体状态过渡缺少历史归档意图')
        return None
    context = transaction.media_archive_context
    if type(intent) is not MediaArchiveIntent or type(context) is not MediaArchiveContext:
        raise ProjectionAuthorityError('媒体历史缺少类型化意图或已绑定服务配置')
    context.validate(transaction)
    action = envelope.draft_plan.revision_action
    expected_family = 'execution' if intent.action == 'restart_after_rollback' else 'rebuild'
    if (intent.action not in {'generate_completed_rebuild', 'restart_after_rollback', 'completed_source_reentry'}
            or action != intent.action or context.family != expected_family
            or intent.task_id != envelope.decision.task_id or not needed):
        raise ProjectionAuthorityError('媒体历史动作与实际状态过渡不一致')
    plan = transaction.plan_store.load(intent.task_id)
    state = transaction.state_store.load(intent.task_id)
    if (envelope.previous_decision is None
            or PlanStore.fingerprint(plan) != intent.plan_fingerprint
            or DecisionStateStore.fingerprint(state) != intent.decision_fingerprint
            or DecisionStateStore.fingerprint(envelope.previous_decision) != intent.decision_fingerprint
            or _sha(read_bytes(safe_task_path(context.task_root, intent.task_id))) != intent.task_sha256):
        raise ProjectionAuthorityError('媒体历史提交前当前计划或任务决定已变化')
    from .content_selection import validate_media_intent
    validate_media_intent(plan, state, task_root=context.task_root)
    if intent.action == 'completed_source_reentry':
        if intent.reentry_fingerprint != _reentry_evidence(context, intent.task_id):
            raise ProjectionAuthorityError('重新加入来源的完成记录、登记或媒体证据已变化')
    elif intent.reentry_fingerprint is not None or context.reentry_review is not None:
        raise ProjectionAuthorityError('其它媒体动作不能夹带重新加入来源的证据')
    if intent.action == 'restart_after_rollback':
        from .rolled_back_restart import current_restart_evidence
        if (intent.ledger_bytes is not None or intent.mapping_sha256 is not None
                or dict(intent.audit_files) != current_restart_evidence(context.service, intent.task_id)):
            raise ProjectionAuthorityError('媒体历史审计依据已变化')
    else:
        if (intent.audit_files or intent.archive_fingerprint != intent.plan_fingerprint
                or not isinstance(intent.ledger_bytes, bytes)
                or read_bytes(safe_task_path(context.record_root, intent.task_id, suffix='.ledger.json')) != intent.ledger_bytes
                or _sha(read_bytes(safe_task_path(context.record_root, intent.task_id))) != intent.mapping_sha256):
            raise ProjectionAuthorityError('媒体历史完成记录或映射依据已变化')
    return intent
