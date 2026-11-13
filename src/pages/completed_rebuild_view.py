"""Read-only inspection first; rebuilding always requires a separate click."""
from ..queue_runtime import ui_disk_call

from .tv_content_actions import await_result
from .progress_view import bar_progress

from src.component.dialog_frame import dialog_card
from nicegui import ui
from ..element.red import notify
from ..rename.completed_rebuild import CompletedRebuildService
from ..rename.plan_store import PlanStore
from ..rename.decision_state import DecisionStateStore
from ..utils.path import (PLAN_PATH, DECISION_STATE_PATH, TASK_PATH, RECORD_PATH,
    WORK_CATALOG_PATH, SERIES_CATALOG_PATH, PROVIDER_LINK_CATALOG_PATH)


def configured_service():
    return CompletedRebuildService(plan_store=PlanStore(PLAN_PATH),
        decision_store=DecisionStateStore(DECISION_STATE_PATH), task_root=TASK_PATH,
        record_root=RECORD_PATH, catalog_root=WORK_CATALOG_PATH,
        series_catalog_root=SERIES_CATALOG_PATH, provider_link_catalog_root=PROVIDER_LINK_CATALOG_PATH)


async def open_rebuild_dialog(task_id, *, manager=None, after_change=None, on_generated=None):
    if manager is None:
        from ..task_queue import task_queue
        manager = task_queue
    service = manager.execution_service.rebuild_service
    with ui.context.client.content, ui.dialog() as dialog, dialog_card().classes('bar-confirm-card w-[640px] max-w-full'):
        ui.label('核查并补齐').classes('text-lg font-bold')
        message = ui.label('正在核对原任务、历史记录和现有文件……')
        progress = bar_progress(ui)
        actions = ui.row().classes('bar-modal-footer')
    dialog.open()
    try:
        view = await ui_disk_call(service.inspect, task_id)
    except Exception as exc:
        if dialog.value and not dialog.is_deleted:
            progress.set_visibility(False)
            message.set_text(str(exc))
            with actions: ui.button('关闭', on_click=dialog.close, icon='o_close').props('flat')
        return dialog
    if not dialog.value or dialog.is_deleted:
        return dialog
    progress.set_visibility(False)
    message.set_text(view.detail)
    with actions:
        ui.button('关闭', on_click=dialog.close, icon='o_close').props('flat')
        if view.state != 'missing':
            return dialog
        if view.state == 'missing':
            ui.label(f'缺失 {view.missing_count} 项，保留 {view.reuse_count} 项。只生成新计划，须重新批准后才可执行。')
        async def confirm():
            if not dialog.value or dialog.is_deleted: return
            button.disable(); progress.set_visibility(True)
            try:
                projected = await manager.generate_completed_rebuild(task_id, expected_fingerprint=view.fingerprint)
                if projected is None:
                    raise ValueError('重建结果尚未确认，请重新核查原任务状态')
                if dialog.is_deleted or not dialog.value:
                    if after_change: await await_result(after_change())
                    return
                if on_generated: await await_result(on_generated(projected))
                notify('已生成新的待批准计划，尚未执行文件操作', type='positive')
                dialog.close()
                if after_change: await await_result(after_change())
            except Exception as exc:
                if not dialog.is_deleted and dialog.value:
                    message.set_text(str(exc)); notify(str(exc), type='negative')
            finally:
                if not dialog.is_deleted:
                    button.enable(); progress.set_visibility(False)
        button = ui.button('生成待批准计划', on_click=confirm, icon='o_playlist_add_check').classes('bar-review-action').props('color=blue-7')
    return dialog
