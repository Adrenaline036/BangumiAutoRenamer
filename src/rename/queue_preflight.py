"""Read-only, request-local execution admission checks; never execution authority.

The worker still validates the actual approved operation immediately before
side effects. No cache or persisted approval is created by this module.
"""
from dataclasses import dataclass
import json
from time import perf_counter

from .plan_execution import PlanExecutionError


@dataclass(frozen=True)
class QueueAdmissionPreparation:
    task_snapshot: str
    checked_plan: object
    rollback_fingerprint: str | None
    elapsed_ms: float


def prepare_queue_admission(read_task, execution, task_id, fingerprint, *, mode,
                            expected_recovery_fingerprint=None):
    started = perf_counter()
    task = read_task(task_id)
    if not task or task.get('uuid') != task_id:
        raise PlanExecutionError('任务记录不存在，拒绝入队')
    if task.get('preview_only'):
        raise PlanExecutionError('测试预览任务禁止执行')
    snapshot = json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    status = str(task.get('workflow_status') or '')
    checked_plan = None
    rollback_fingerprint = None
    if mode == 'asset_rollback':
        from .asset_rollback import AssetRollbackPreparation
        if status not in {'completed', 'partial_failed', 'failed', 'catalog_update_pending', 'interrupted', 'rolled_back'}:
            raise PlanExecutionError('当前任务状态不允许回滚')
        preparation = execution.prepare_asset_rollback(task_id, fingerprint)
        if type(preparation) is not AssetRollbackPreparation:
            raise PlanExecutionError('回滚预检没有返回有效结果，请重新核查')
        if preparation.fingerprint != expected_recovery_fingerprint:
            raise PlanExecutionError('回滚范围已变化，请重新核查后确认')
        rollback_fingerprint = preparation.fingerprint
    elif mode == 'asset_recovery':
        from .asset_execution_recovery import AssetExecutionRecovery, AssetRecoveryPreparation
        if status not in {'failed', 'catalog_update_pending', 'completed', 'partial_failed'}:
            raise PlanExecutionError('当前任务状态不允许换源恢复')
        preparation = AssetExecutionRecovery(execution).inspect(task_id, fingerprint)
        if not isinstance(preparation, AssetRecoveryPreparation):
            raise PlanExecutionError('换源恢复预检没有返回有效结果，请重新核查')
        if preparation.state == 'replan_required':
            raise PlanExecutionError(preparation.detail)
        checked_plan = preparation.plan
    elif mode == 'approved_plan':
        from .execution_recovery import safe_approved_execution_retry
        safe_retry = (safe_approved_execution_retry(task)
                      and task.get('execution_requested_fingerprint') == fingerprint)
        if status != 'approved' and not safe_retry:
            raise PlanExecutionError('任务状态不是已批准，拒绝入队')
        checked_plan = execution.validate_approved(task_id, fingerprint)
    else:
        raise PlanExecutionError('未知执行入口，拒绝入队')
    if mode != 'asset_rollback' and checked_plan is None:
        raise PlanExecutionError('执行预检没有返回有效计划，拒绝入队')
    return QueueAdmissionPreparation(snapshot, checked_plan, rollback_fingerprint,
        round((perf_counter() - started) * 1000, 3))
