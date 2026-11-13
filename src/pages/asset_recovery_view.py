"""One recovery confirmation for a fixed approved replacement transaction."""
from ..queue_runtime import ui_disk_call

from .progress_view import bar_progress

from src.component.dialog_frame import dialog_card
from nicegui import ui
from ..element.red import notify
from ..rename.asset_execution_recovery import AssetExecutionRecovery
from ..rename.plan_store import PlanStore, PlanStoreError
from .queue_confirmation import QueueConfirmation


async def open_asset_recovery(queue, task_id, refresh):
    try:
        plan = queue.plan_store.load(task_id)
    except PlanStoreError:
        # Missing/legacy Plan is diagnosed by the existing read-only recovery
        # view, never accepted as a replacement transaction.
        return False
    from ..rename.series_repair import is_repair
    repair=is_repair(plan.metadata.get('asset_operation'))
    if (plan.metadata.get('asset_operation') or {}).get('mode') != 'replace' and not repair:
        return False
    with ui.dialog().props('persistent') as dialog, dialog_card().classes('bar-dialog max-w-2xl'):
        ui.label('核查补齐' if repair else '核查换源').classes('bar-dialog-title')
        message = ui.label('正在核查原批准、登记及文件……').classes('bar-help')
        progress = bar_progress(ui)
        actions = ui.row().classes('w-full justify-end gap-2').classes('bar-modal-footer')
    dialog.open()
    try:
        fingerprint = PlanStore.fingerprint(plan)
        prepared = await ui_disk_call(AssetExecutionRecovery(queue.execution_service).inspect, task_id, fingerprint)
        if prepared is None:
            raise ValueError('核查已取消，请重新打开')
        message.set_text(prepared.detail)
        admission = QueueConfirmation(dialog, message, progress)
        replanning = False
        async def resume():
            def completed():
                refresh()
                notify('已按原批准加入恢复队列', type='positive')
            await admission.run(lambda: queue.enqueue_asset_recovery(task_id, fingerprint), completed)
        async def replan():
            nonlocal replanning
            if replanning:
                return
            replanning = True
            button.disable()
            close_button.disable()
            progress.set_visibility(True)
            message.set_text('正在重新核查并生成未批准计划，请等待完成……')
            try:
                current = await ui_disk_call(AssetExecutionRecovery(queue.execution_service).inspect, task_id, fingerprint)
                if current is None or current.state != 'replan_required':
                    raise ValueError('恢复状态已变化，请重新核查')
                review = queue.execution_service.review_service
                operation, _draft = await ui_disk_call(review.asset_actions().prepare_missing if repair else review.asset_actions().prepare_replacement, plan)
                await queue.confirm_asset_plan(task_id, fingerprint, operation, recovery_preparation=current)
                if dialog.is_deleted or not dialog.value:
                    return
                dialog.close(); refresh()
                notify('已生成新计划，请重新批准；旧事务证据保持不变', type='positive')
            except Exception as exc:
                if not dialog.is_deleted and dialog.value:
                    notify(str(exc), type='negative'); message.set_text(str(exc)); button.enable()
            finally:
                replanning = False
                if not dialog.is_deleted and dialog.value:
                    close_button.enable()
                    progress.set_visibility(False)
        with actions:
            close_button = ui.button('关闭', on_click=admission.cancel, icon='o_close').props('flat')
            if prepared.state != 'replan_required':
                button = ui.button('继续', icon='o_refresh', on_click=resume).classes('bar-review-action').props('unelevated color=blue-7')
                admission.button = button
            else:
                button = ui.button('重新生成', icon='o_refresh', on_click=replan).classes('bar-review-action').props('unelevated color=blue-7')
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        message.set_text(str(exc))
        with actions:
            ui.button('关闭', on_click=dialog.close, icon='o_close').props('flat')
    finally:
        progress.set_visibility(False)
    return True


async def open_asset_rollback(queue, task_id, refresh):
    """Explicit read-only preview followed by a version-bound queue command."""
    with ui.dialog().props('persistent') as dialog, dialog_card().classes('bar-dialog max-w-2xl'):
        ui.label('回滚本次换源').classes('bar-dialog-title')
        message = ui.label('正在核查备份、所选季度／版本……').classes('bar-help')
        progress = bar_progress(ui)
        actions = ui.row().classes('w-full justify-end gap-2').classes('bar-modal-footer')
    dialog.open()
    try:
        plan = queue.plan_store.load(task_id)
        fingerprint = PlanStore.fingerprint(plan)
        prepared = await ui_disk_call(queue.execution_service.prepare_asset_rollback, task_id, fingerprint)
        if prepared is None:
            raise ValueError('核查已取消，请重新打开')
        message.set_text('恢复本次换源前的季度／版本，撤回本次新输出。其他季度和电影版本不变，源下载文件不变。此步骤不会立即执行。')
        from ..rename.asset_replacement import approved_batch
        batch = approved_batch(plan, fingerprint)
        with actions:
            ui.label(f"所选范围：{plan.metadata['asset_operation']['slot_key']}").classes('bar-help break-all w-full')
            ui.label(f'恢复备份：{batch.transaction_directory}').classes('bar-help break-all w-full')
        admission = QueueConfirmation(dialog, message, progress)
        async def confirm():
            def completed():
                refresh()
                notify('已将确认的回滚加入队列', type='positive')
            await admission.run(lambda: queue.enqueue_asset_rollback(task_id, fingerprint,
                expected_recovery_fingerprint=prepared.fingerprint), completed)
        with actions:
            ui.button('取消', on_click=admission.cancel, icon='o_close').props('flat')
            button = ui.button('确认回滚', icon='o_undo', on_click=confirm).classes('bar-review-action').props('unelevated color=blue-7')
            admission.button = button
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        message.set_text(str(exc))
        with actions:
            ui.button('关闭', on_click=dialog.close, icon='o_close').props('flat')
    finally:
        progress.set_visibility(False)
    return True


async def open_rolled_back_restart(queue, task_id, refresh):
    """Preview only; the explicit command creates a fresh unapproved revision."""
    with ui.dialog() as dialog, dialog_card().classes('bar-dialog max-w-2xl'):
        ui.label('回滚后重新整理').classes('bar-dialog-title')
        message = ui.label('正在核查原回滚记录、当前计划和来源……').classes('bar-help')
        progress = bar_progress(ui)
        actions = ui.row().classes('w-full justify-end gap-2').classes('bar-modal-footer')
    dialog.open()
    try:
        prepared = await queue.prepare_rolled_back_restart(task_id)
        message.set_text('将保留原回滚审计、作品和版本选择，生成新的未批准计划；不会写入媒体。若仍要换源，需要重新选择目标版本、确认差异并批准。')
        busy = False

        async def confirm():
            nonlocal busy
            if busy:
                return
            busy = True
            button.disable()
            try:
                await queue.restart_rolled_back(task_id, prepared)
                dialog.close()
                refresh()
                notify('新整理计划已生成，请打开任务审核；尚未批准或执行', type='positive')
            except (OSError, RuntimeError, ValueError) as exc:
                message.set_text(str(exc))
                notify(str(exc), type='negative')
                button.enable()
                busy = False

        with actions:
            ui.button('取消', on_click=dialog.close, icon='o_close').props('flat')
            button = ui.button('生成新整理计划', icon='o_refresh', on_click=confirm).props('unelevated color=blue-7')
    except (OSError, RuntimeError, ValueError) as exc:
        message.set_text(str(exc))
        with actions:
            ui.button('关闭', on_click=dialog.close, icon='o_close').props('flat')
    finally:
        progress.set_visibility(False)
    return True
