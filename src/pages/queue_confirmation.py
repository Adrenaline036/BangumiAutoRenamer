"""Ephemeral UI feedback; queue admission remains the sole commit authority."""

from .progress_view import bar_progress

from src.component.dialog_frame import dialog_card
import asyncio
from nicegui import ui

from ..element.red import notify


class QueueConfirmation:
    def __init__(self, dialog, message=None, progress=None):
        self.dialog = dialog
        self.message = message
        self.progress = progress
        self.button = None
        self.pending = None
        self.closed = False

    def cancel(self):
        self.closed = True
        if self.pending is not None:
            self.pending.cancel()
        self.dialog.close()

    async def run(self, command, on_success):
        if self.closed or self.pending is not None:
            return
        self.pending = asyncio.current_task()
        if self.button is not None:
            self.button.disable()
        if self.message is not None:
            self.message.set_text('正在后台校验文件与批准版本，尚未入队；可取消本次请求……')
        if self.progress is not None:
            self.progress.set_visibility(True)
        try:
            await command()
        except asyncio.CancelledError:
            self.closed = True
            if self.message is not None:
                self.message.set_text('已取消本次入队请求；没有取消或回滚已执行的任务。')
        except Exception as exc:
            if self.message is not None:
                self.message.set_text(str(exc))
            notify(str(exc), type='negative')
        else:
            self.closed = True
            self.dialog.close()
            on_success()
        finally:
            self.pending = None
            if self.button is not None:
                self.button.enable()
            if self.progress is not None:
                self.progress.set_visibility(False)


def open_queue_confirmation(command, on_success, *, title, background=None):
    with ui.dialog().props('persistent') as dialog, dialog_card().classes('bar-dialog max-w-lg'):
        ui.label(title).classes('bar-dialog-title')
        message = ui.label('将核查原批准版本；通过后进入实际执行队列。源文件不会移动。').classes('bar-help')
        progress = bar_progress(ui)
        progress.set_visibility(False)
        state = QueueConfirmation(dialog, message, progress)
        with ui.row().classes('w-full justify-end gap-2').classes('bar-modal-footer'):
            ui.button('取消', on_click=state.cancel, icon='o_close').props('flat')
            async def confirm():
                if background is None:
                    await state.run(command, on_success)
                elif not state.closed:
                    from .execution_wait import submit
                    try:
                        submit(*background)
                    except (ValueError, RuntimeError) as exc:
                        notify(str(exc), type='warning')
                        return
                    state.closed = True
                    dialog.close()
            state.button = ui.button('确认入队', on_click=confirm, icon='o_playlist_add_check').props('color=red-7')
    dialog.open()
