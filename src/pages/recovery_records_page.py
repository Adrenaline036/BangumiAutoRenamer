"""Read-only orphan discovery and explicitly confirmed audit-only closure."""

from src.component.dialog_frame import dialog_card
import hashlib
import os

from nicegui import ui

from ..rename.decision_state import DecisionStateStore
from ..rename.plan_store import PlanStore
from ..rename.recovery_transaction import CatalogDetachService, RecoveryTransactionStore
from ..rename.series_catalog import SeriesCatalog
from ..rename.work_catalog import WorkCatalog
from ..utils.path import (DECISION_STATE_PATH, PLAN_PATH, RECOVERY_TRANSACTION_PATH,
                          SERIES_CATALOG_PATH, TASK_PATH, WORK_CATALOG_PATH)
from ..utils.utils import safe_task_path


def orphan_records(service):
    rows = []
    task_ids = {p.stem for p in service.transaction_store.root.glob('*.json')}
    for task_id in sorted(task_ids):
        if os.path.lexists(safe_task_path(service.task_root, task_id)):
            continue
        path = service.transaction_store.path_for(task_id)
        transaction = service.transaction_store.load_optional(task_id)
        if transaction is not None and transaction.phase != 'completed':
            rows.append((transaction.task_id, hashlib.sha256(path.read_bytes()).hexdigest()))
    return rows


def show_recovery_records():
    service = CatalogDetachService(transaction_store=RecoveryTransactionStore(RECOVERY_TRANSACTION_PATH),
        decision_store=DecisionStateStore(DECISION_STATE_PATH), plan_store=PlanStore(PLAN_PATH),
        work_catalog=WorkCatalog(WORK_CATALOG_PATH), series_catalog=SeriesCatalog(SERIES_CATALOG_PATH),
        task_root=TASK_PATH)
    with ui.context.client.content, ui.dialog() as dialog, dialog_card().classes('bar-dialog w-full').style('max-width:720px'):
        ui.label('恢复记录').classes('bar-dialog-title')
        ui.label('仅结束已移除任务的残留恢复记录；不恢复任务、不修改媒体或现有作品登记。')
        try:
            rows = orphan_records(service)
        except (OSError, ValueError, RuntimeError) as exc:
            rows = []
            ui.label(f'恢复记录仍需核查：{exc}').classes('text-negative')
        if not rows:
            ui.label('未发现待处理的孤立恢复记录。')
        for task_id, fingerprint in rows:
            with ui.card().classes('bar-confirm-card w-full'):
                ui.label('任务已移除，恢复未完成')
                ui.label(task_id).classes('text-caption')
                confirm = ui.checkbox('我确认只结束这条恢复记录，不恢复媒体')

                def close_record(identifier=task_id, expected=fingerprint, checkbox=confirm):
                    if not checkbox.value:
                        ui.notify('请先确认安全结束范围', type='warning')
                        return
                    try:
                        service.archive_orphaned_transaction(identifier, expected_fingerprint=expected)
                    except (OSError, ValueError, RuntimeError) as exc:
                        ui.notify(str(exc), type='negative')
                        return
                    ui.notify('恢复记录已安全结束；媒体未修改', type='positive')
                    dialog.close()

                ui.button('安全结束', on_click=close_record, icon='o_archive').props('color=blue-7').classes('rounded-lg')
        ui.button('关闭', on_click=dialog.close, icon='o_close').props('flat').classes('bar-modal-footer')
    dialog.open()
