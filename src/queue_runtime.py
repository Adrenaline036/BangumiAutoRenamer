"""后台磁盘事务与主循环纯内存接纳之间的唯一调度边界。"""
import asyncio
from concurrent.futures import Future
from contextvars import ContextVar
from dataclasses import dataclass
import threading

_owner = ContextVar('bar_queue_owner', default=None)
PUBLICATION_TASK_NAME = 'bar-disk-publication'


@dataclass(frozen=True)
class Owner:
    loop: object
    thread: int
    caller: object

    def call(self, function, *args, **kwargs):
        if threading.get_ident() == self.thread:
            return function(*args, **kwargs)
        result = Future()
        def invoke():
            try:
                result.set_result(function(*args, **kwargs))
            except BaseException as exc:
                result.set_exception(exc)
        self.loop.call_soon_threadsafe(invoke)
        return result.result()


def on_owner(function, *args, **kwargs):
    """只用于不含磁盘I/O、锁等待或await的内存操作。"""
    owner = _owner.get()
    return function(*args, **kwargs) if owner is None else owner.call(function, *args, **kwargs)


def require_not_cancelled():
    def check():
        owner = _owner.get()
        caller = owner.caller if owner is not None else asyncio.current_task()
        if caller is not None and caller.cancelling():
            from .rename.plan_execution import PlanExecutionError
            raise PlanExecutionError('请求已取消，未接纳新任务')
    return on_owner(check)


async def disk_call(function, *args, **kwargs):
    return await _disk_call(function, args, kwargs, asyncio.current_task())


async def _disk_call(function, args, kwargs, caller, cancelled=None, stopping=lambda: False):
    from .rename.file_lock import cancellable_wait
    cancelled = cancelled if cancelled is not None else threading.Event()
    owner = Owner(asyncio.get_running_loop(), threading.get_ident(), caller)
    def work():
        token = _owner.set(owner)
        try:
            with cancellable_wait(lambda: cancelled.is_set() or stopping()):
                return function(*args, **kwargs)
        finally:
            _owner.reset(token)
    worker = asyncio.create_task(asyncio.to_thread(work))
    return await _wait_finished(worker, cancelled)


async def _wait_finished(worker, cancelled):
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancelled.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise


async def transaction(manager, function, *args, **kwargs):
    """停止服务也必须等候本次真实事务及接纳回调完成。"""
    cancelled = threading.Event()
    worker = asyncio.create_task(_disk_call(function, args, kwargs, asyncio.current_task(),
        cancelled, lambda: manager._stopping), name=PUBLICATION_TASK_NAME)
    manager._admissions.add(worker)
    try:
        return await _wait_finished(worker, cancelled)
    finally:
        manager._admissions.discard(worker)


async def stoppable_disk_call(manager, stopping, function, *args, **kwargs):
    """跟踪真实磁盘线程，停止或取消时退出等待并完成收尾。"""
    cancelled = threading.Event()
    worker = asyncio.create_task(_disk_call(function, args, kwargs, asyncio.current_task(), cancelled, stopping))
    manager._disk_calls.add(worker)
    try:
        return await _wait_finished(worker, cancelled)
    finally:
        manager._disk_calls.discard(worker)


async def ui_disk_call(function, *args, **kwargs):
    """页面后台事务与服务共用等待和停止边界。"""
    from .task_queue import task_queue
    if task_queue._stopping:
        raise RuntimeError('任务队列正在停止，请等待当前操作安全收尾')
    return await task_queue._disk(function, *args, **kwargs)
