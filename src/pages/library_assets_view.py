"""Search assets by work rather than requiring users to find historical task cards."""
from ..queue_runtime import ui_disk_call

from .progress_view import bar_progress

from src.component.dialog_frame import dialog_card
from nicegui import ui
from .completed_rebuild_view import open_rebuild_dialog
from ..rename.library_assets import LibraryAssets


async def show_library_assets(*, manager=None):
    if manager is None:
        from ..task_queue import task_queue
        manager = task_queue
    execution = manager.execution_service
    with ui.context.client.content, ui.dialog() as dialog, dialog_card().classes('bar-confirm-card w-[860px] max-w-full'):
        ui.label('媒体库').classes('text-h6')
        ui.label('按作品查找现有资产。外部删除不等于退役；重建只生成待批准计划。换源请添加新来源。').classes('bar-help')
        search = ui.input('搜索作品或版本').classes('w-full').props('outlined dense')
        progress = bar_progress(ui)
        content = ui.column().classes('w-full gap-3')
        ui.button('关闭', on_click=dialog.close, icon='o_close').props('flat color=blue-7').classes('bar-modal-footer')
    dialog.open()
    rows = await ui_disk_call(LibraryAssets(execution.rebuild_service).list_assets)
    if not dialog.value or dialog.is_deleted: return
    progress.set_visibility(False)
    def render():
        content.clear()
        selected = [row for row in rows if str(search.value or '').casefold() in f'{row.title} {row.version}'.casefold()]
        with content:
            if not selected:
                ui.label('没有找到已整理资产').classes('bar-help')
            for row in selected:
                with ui.card().classes('bar-confirm-card w-full'):
                    ui.label(row.title + (f' · 第 {row.season} 季' if row.season is not None else '')).classes('font-bold')
                    if row.version:
                        ui.label(row.version).classes('bar-meta-chip')
                    ui.label(row.target).classes('bar-path-text')
                    ui.label(row.detail).classes('bar-help')
                    ui.button('重建', icon='o_restore', on_click=lambda _, task_id=row.task_id: open_rebuild_dialog(task_id, manager=manager)).classes(
                        'bar-review-action').props('unelevated color=blue-7')
    search.on_value_change(lambda _: render())
    render()
