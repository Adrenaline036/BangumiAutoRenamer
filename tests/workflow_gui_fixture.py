"""隔离客户端中的当前任务创建与重新识别确认事件。"""
import asyncio
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import patch
from nicegui import core
from nicegui.client import Client
from nicegui.page import page
from tests._gui_helpers import GuiDriver


@asynccontextmanager
async def hold_worker(manager):
    """在断言待消费状态后放行真实 worker，避免把正常推进误判为状态漂移。"""
    await manager.stop()
    release = asyncio.Event()
    original = manager._worker
    async def waiting():
        await release.wait()
        await original()
    with patch.object(manager, '_worker', new=waiting):
        await manager.start()
        try:
            yield
        finally:
            release.set()
            await asyncio.wait_for(manager._queue.join(), 15)


async def add_source(source, *, media_type='anime'):
    from src.pages.add_task_page import AddTaskDialog
    client = Client(page('/isolated-task-admission'), request=None)
    try:
        with client:
            dialog = AddTaskDialog()
            try:
                dialog.path, dialog.media_type = str(source), media_type
                with patch.object(dialog, 'close', wraps=dialog.close) as close:
                    await dialog._next()
                    if not dialog._closed:
                        await dialog._submit()
                    return SimpleNamespace(close=close, closed=dialog._closed)
            finally:
                dialog.delete()
    finally:
        client.delete()


async def confirm_restart(operation):
    client = Client(page('/isolated-task-restart'), request=None)
    driver = GuiDriver('runTest')
    driver.client = client
    pending = None
    try:
        with client, patch.multiple(core, loop=asyncio.get_running_loop()):
            async def invoke():
                with client:
                    return await operation()
            pending = asyncio.create_task(invoke())
            await asyncio.sleep(0)
            if not pending.done():
                await driver.click('确认重新识别')
            return await asyncio.wait_for(pending, 30)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        client.delete()
