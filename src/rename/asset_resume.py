"""Resume an original approved replacement; never infer a fresh approval."""
from contextlib import ExitStack

from .asset_execution_recovery import AssetExecutionRecovery
from .catalog_binding import binding_from_plan


def resume_asset(execution, task_id, fingerprint, *, expected_execution_status=None):
    from .plan_execution import PlanExecutionError, PlanExecutionResult, _utc_now
    recovery = AssetExecutionRecovery(execution)
    with execution._execution_lock(task_id, fingerprint), ExitStack() as locks:
        if expected_execution_status is not None:
            current_task = execution._read_task(task_id)
            if (current_task.get('execution_status') != expected_execution_status
                    or current_task.get('workflow_status') not in {'failed', 'catalog_update_pending'}):
                raise PlanExecutionError('任务不处于作品登记待恢复状态')
        plan = execution.plan_store.load(task_id)
        binding = binding_from_plan(plan)
        if binding is None:
            raise PlanExecutionError('换源恢复缺少登记绑定')
        catalog = execution.series_catalog if binding.backend == 'series' else execution.work_catalog
        locks.enter_context(catalog.lock(binding.key))
        # A rejected namespace guard must not rewrite the recovery state.
        catalog.require_namespace_plan(plan, binding.key)
        try:
            prepared = recovery.inspect(task_id, fingerprint)
            if prepared.state == 'replan_required':
                raise PlanExecutionError(prepared.detail)
            execution._validate_runtime_safety_gate(plan)
            task = execution._read_task(task_id)
            if task.get('preview_only'):
                raise PlanExecutionError('测试预览任务禁止执行')
            ledger_path = execution.record_root / f'{task_id}.ledger.json'
            if prepared.state == 'resume_media':
                prepared = recovery.validate_preparation(prepared)
                execution._update_task(task_id, workflow_status='executing', execution_status='executing',
                    execution_fingerprint=fingerprint, execution_phase='media_boundary_entered',
                    execution_requires_manual_check=True, execution_may_have_written_targets=True)
                from .generated_outputs import resume_outputs
                outputs = resume_outputs(plan, execution.record_root)
                transfer = execution.transfer_factory(plan.as_mapping(), task_id, fingerprint,
                    mode=plan.execution_policy.mode, dry_run=plan.execution_policy.dry_run,
                    record_root=execution.record_root, operations=execution._build_execution_operations(plan),
                    replacement_authority=prepared.capability,
                    **({'generated_outputs':outputs} if outputs is not None else {}))
                error = transfer.trans_file()
                if error:
                    raise PlanExecutionError(str(error))
            ledger = execution._load_ledger(ledger_path, task_id, fingerprint, plan)
            if ledger.get('status') != 'completed':
                raise PlanExecutionError('换源恢复尚未完成全部文件')
            # Reinspect journal and exact completed physical proofs before Catalog.
            prepared = recovery.inspect(task_id, fingerprint)
            if prepared.state == 'catalog_pending':
                if binding.backend == 'series':
                    catalog.register_completed(plan, expected_revision=binding.revision, completed_ledger=ledger)
                else:
                    catalog.register_completed(plan, ledger, expected_revision=binding.revision, lock_held=True)
            elif prepared.state != 'completed':
                raise PlanExecutionError('换源恢复未达到登记提交条件')
            execution._materialize_provider_link(plan)
            from .replacement_completion import media_completion_fields
            fields = media_completion_fields(plan, _utc_now())
            execution._update_task(task_id, **fields, execution_status='completed',
                execution_fingerprint=fingerprint, execution_ledger=str(ledger_path),
                execution_completed_at=_utc_now(), error=None,
                execution_requires_manual_check=False, execution_may_have_written_targets=False,
                execution_recovery_message=None)
            locks.close()  # Catalog is finished; retain the outer task execution lock.
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            execution._update_task(task_id, workflow_status='failed', execution_status='failed',
                execution_requires_manual_check=True, execution_may_have_written_targets=True,
                execution_recovery_message=str(exc), error=str(exc))
            raise PlanExecutionError(str(exc)) from exc

    from .replacement_completion import finish_replacement
    finish_replacement(execution, task_id, fingerprint)
    return PlanExecutionResult(task_id, fingerprint, ledger_path, 'completed')
