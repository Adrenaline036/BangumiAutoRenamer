"""Finalize only the approved replacement's temporary media links."""
from dataclasses import asdict
import hashlib
import os

DISCARD_REPLACED_LINKS = 'discard-replaced-links-v1'


def uses_completion_policy(operation):
    return bool(operation and operation.get('completion_policy') == DISCARD_REPLACED_LINKS)


def require_inverse_allowed(operation):
    if uses_completion_policy(operation):
        raise ValueError('此换源不提供主动回滚；中断后请核查并继续原操作，或重新生成待批准计划')


def project_completion_fields(plan, task, fields):
    """换源完成需等待临时媒体链接收尾。"""
    from .plan_store import PlanStore
    if (uses_completion_policy(plan.metadata.get('asset_operation'))
            and fields.get('workflow_status') == 'completed'
            and task.get('replacement_completed_fingerprint') != PlanStore.fingerprint(plan)):
        return {**fields, 'workflow_status': 'executing', 'execution_phase': 'replacement_finalizing', 'completed_at': None}
    return fields


def _digest(value):
    from .asset_operation import AssetOperation
    return hashlib.sha256(AssetOperation.encode(value)).hexdigest()


def _files(batch, operations, journal):
    """Names derive from the frozen transaction, never directory prefix matching."""
    rows = {}
    for index, operation in enumerate(operations):
        if operation['action'] == 'reuse_existing':
            continue
        rows[f'new-{index}'] = journal['entries'][index]['source_evidence']
        if operation['action'] == 'replace':
            evidence = next(e for e in batch.evidence if e.target == operation['target'])
            rows[f'old-{index}'] = asdict(evidence.old_target)
            rows[f'restore-{index}'] = asdict(evidence.old_target)
    for index, evidence in enumerate(batch.retirements):
        rows[f'retired-{index}'] = asdict(evidence.old_target)
    return rows


def validate_cleanup(batch, operations, journal, *, ledger=None):
    """Read-only exception for backups durably authorized for removal."""
    from .asset_replacement import DirectoryWitness, _fingerprint, require_file, safe_absolute
    value = journal.get('cleanup') if journal else None
    if value is None:
        return False
    if (batch.completion_policy != DISCARD_REPLACED_LINKS or journal.get('status') != 'completed'
            or set(value) != {'policy', 'task_id', 'plan_fingerprint', 'ledger_sha256', 'files', 'status'}
            or value['policy'] != DISCARD_REPLACED_LINKS
            or value['task_id'] != batch.task_id or value['plan_fingerprint'] != batch.plan_fingerprint
            or value['status'] not in {'cleanup_started', 'cleanup_completed'}
            or value['files'] != _files(batch, operations, journal)
            or not isinstance(value['ledger_sha256'], str) or len(value['ledger_sha256']) != 64
            or ledger is not None and value['ledger_sha256'] != _digest(ledger)):
        raise ValueError('换源收尾记录与原批准或完成记录不一致')
    directory = safe_absolute(batch.transaction_directory)
    DirectoryWitness(**journal['directory']).validate()
    if {p.name for p in directory.iterdir()} - {'journal.json', *value['files']}:
        raise ValueError('换源临时目录出现未知文件，已停止清理')
    for name, expected in value['files'].items():
        path = safe_absolute(directory/name)
        if os.path.lexists(path):
            if value['status'] == 'cleanup_completed':
                raise ValueError('已完成收尾的临时目录又出现文件')
            require_file(path, _fingerprint(expected))
    # Completion cannot turn an absent/foreign current target into a valid one.
    for index, operation in enumerate(operations):
        if operation['action'] != 'reuse_existing':
            require_file(operation['target'], _fingerprint(journal['entries'][index]['source_evidence']))
    for retired in batch.retirements:
        retired.validate_current(retired=True)
        if os.path.lexists(retired.target):
            raise ValueError('已退场目标重新出现，不能继续换源收尾')
    return True




def finish_replacement(execution, task_id, fingerprint):
    """持有任务锁核对并完成换源收尾。"""
    from .plan_execution import PlanExecutionService
    if not isinstance(execution, PlanExecutionService):
        raise ValueError('换源收尾需要完整执行服务上下文')
    plan = execution.plan_store.load(task_id)
    if not uses_completion_policy(plan.metadata.get('asset_operation')):
        return True
    with execution._execution_lock(task_id, fingerprint):
        return _finish_locked(execution, task_id, fingerprint)


def _finish_locked(execution, task_id, fingerprint):
    from .plan_store import PlanStore
    from .asset_execution_recovery import AssetExecutionRecovery
    from .catalog_binding import binding_from_plan
    from .asset_replacement import DirectoryWitness, _fingerprint, require_file, safe_absolute
    from .trans import Trans
    from datetime import datetime, timezone
    plan = execution.plan_store.load(task_id)
    if not uses_completion_policy(plan.metadata.get('asset_operation')):
        return True
    if PlanStore.fingerprint(plan) != fingerprint:
        raise ValueError('换源收尾计划版本已改变')
    try:
        binding = binding_from_plan(plan)
        if binding is None:
            raise ValueError('换源收尾缺少登记绑定')
        catalog = execution.series_catalog if binding.backend == 'series' else execution.work_catalog
        with catalog.lock(binding.key):
            prepared = AssetExecutionRecovery(execution).inspect(task_id, fingerprint)
            if prepared.state != 'completed':
                raise ValueError('媒体或登记尚未完成，不能清理旧媒体链接')
            batch = prepared.capability
            transfer = Trans(plan.as_mapping(), task_id, fingerprint, mode='hardlink', dry_run=False,
                record_root=execution.record_root, operations=execution._build_execution_operations(plan),
                replacement_authority=batch)
            operations = transfer._replacement_operations(batch)
            journal = transfer._read_replacement_journal(batch, operations)
            ledger = execution._load_ledger(execution.record_root/f'{task_id}.ledger.json', task_id, fingerprint, plan)
            directory = safe_absolute(batch.transaction_directory)
            files = _files(batch, operations, journal)
            if {p.name for p in directory.iterdir()} - {'journal.json', *files}:
                raise ValueError('换源临时目录出现未知文件，已停止清理')
            if 'cleanup' not in journal:
                # inspect validated every required old/retired link before this durable boundary.
                journal['cleanup'] = dict(policy=DISCARD_REPLACED_LINKS, task_id=task_id,
                    plan_fingerprint=fingerprint, ledger_sha256=_digest(ledger), files=files, status='cleanup_started')
                transfer._write_replacement_journal(directory, journal)
            validate_cleanup(batch, operations, journal, ledger=ledger)
            if journal['cleanup']['status'] != 'cleanup_completed':
                for name, expected in files.items():
                    DirectoryWitness(**journal['directory']).validate()
                    path = safe_absolute(directory/name)
                    if os.path.lexists(path):
                        require_file(path, _fingerprint(expected))
                        path.unlink()
                validate_cleanup(batch, operations, journal, ledger=ledger)
                journal['cleanup']['status'] = 'cleanup_completed'
                transfer._write_replacement_journal(directory, journal)
            execution._update_task(task_id, workflow_status='completed', execution_phase='completed',
                replacement_completed_fingerprint=fingerprint, completed_at=datetime.now(timezone.utc).isoformat(),
                replacement_cleanup_error=None, error=None, execution_recovery_message=None)
        return True
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        execution._update_task(task_id, workflow_status='partial_failed', execution_phase='replacement_cleanup_failed',
            completed_at=None, replacement_cleanup_error=str(exc), execution_recovery_message=str(exc), error=str(exc))
        raise


def media_completion_fields(plan, timestamp):
    return project_completion_fields(plan, {}, dict(workflow_status='completed',
        execution_phase='completed', completed_at=timestamp))


def has_completed_media(task):
    return (task.get('execution_status') == 'completed'
            and (task.get('execution_phase') in {'replacement_finalizing', 'replacement_cleanup_failed'}
                 or task.get('workflow_status') == 'completed'))
