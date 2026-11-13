"""撤回尚未开始的作品选择；不撤销作品决定或来源登记。"""
import hashlib
from ..queue_runtime import on_owner, transaction
from .recognition_queue_admission import _authority_locks, _raw
from ..utils.utils import task_record_snapshot_at, restore_task_record_at_if_sha256


def remember(manager, job, prepared, data):
    work = manager._provider_reads.get(job.task_id)
    if work is None or work.worker.done() or job.operation != 'reidentify':
        return
    manager._selection_waits[job.request_id] = (prepared, dict(data), job)
    work.admitted = True
    work.request_id = job.request_id


def _withdraw_record(manager, receipt):
    prepared, accepted, job = receipt
    with _authority_locks(manager, job.task_id):
        if (manager._admission_epoch != prepared.epoch
                or _raw(manager.plan_store.path_for(job.task_id)) != prepared.plan
                or _raw(manager.decision_state_store.path_for(job.task_id)) != prepared.decision):
            raise ValueError('计划或人工决定已变化，未取消本次等待，请核对任务。')
        raw, current, _ = task_record_snapshot_at(manager.task_root, job.task_id)
        comparable = lambda data: {k: v for k, v in data.items() if k != 'queue_position'}
        if comparable(current) != comparable(accepted):
            raise ValueError('任务记录已变化，未取消本次等待，请核对任务。')
        def check():
            if (manager._stopping or manager.is_active(job.task_id)
                    or manager._selection_waits.get(job.request_id) is not receipt
                    or job.task_id not in manager._pending):
                raise ValueError('任务已经开始或队列状态已变化，不能取消等待。')
        def complete():
            manager._pending.remove(job.task_id)
            manager._selection_waits.pop(job.request_id)
            manager._withdrawn_selection_requests.add(job.request_id)
        on_owner(check)
        # 恢复精确字节，兼容旧记录的格式；CAS拒绝覆盖后来写入的内容。
        restore_task_record_at_if_sha256(manager.task_root, job.task_id,
            expected_current_sha256=hashlib.sha256(raw).hexdigest(), previous=prepared.task)
        on_owner(complete)


async def withdraw(manager, task_id, request_id):
    async with manager._admission_lock:
        receipt = manager._selection_waits.get(request_id)
        if manager.is_active(task_id) or receipt is None or receipt[2].task_id != task_id:
            return False
        # 在第一次让出主循环前登记 publication，消费者须等到恢复落盘。
        await transaction(manager, _withdraw_record, manager, receipt)
        return True


def execution_receipt(manager, job, previous, accepted):
    """执行预检通过后记录同一接纳事务的原始字节，只供排队撤回。"""
    from types import SimpleNamespace
    prepared = SimpleNamespace(epoch=manager._admission_epoch, task=previous,
        plan=_raw(manager.plan_store.path_for(job.task_id)),
        decision=_raw(manager.decision_state_store.path_for(job.task_id)))
    return prepared, dict(accepted), job


def remember_execution(manager, job, receipt):
    if receipt is None:
        return
    work = getattr(manager, '_execution_waits', {}).get(job.task_id)
    if work is not None and work.fingerprint == job.expected_fingerprint:
        manager._selection_waits[job.request_id] = receipt
        work.admitted = True
        work.request_id = job.request_id
