"""In-process task-change hint; durable task files remain authoritative."""
from threading import Lock
from inspect import isawaitable
import asyncio
from contextlib import suppress

_lock = Lock()
_revision = 0
_waiters = set()


def mark_changed():
    global _revision
    with _lock:
        _revision += 1
        for future in tuple(_waiters):
            with suppress(RuntimeError):
                future.get_loop().call_soon_threadsafe(_wake, future)


def _wake(future):
    if not future.done():
        future.set_result(None)


async def wait_changed(observed, timeout=None):
    """先核对修订再登记等待，写线程只通知，不传递业务权威。"""
    future = asyncio.get_running_loop().create_future()
    with _lock:
        if _revision != observed:
            return True
        _waiters.add(future)
    try:
        await asyncio.wait_for(future, timeout)
        return True
    except TimeoutError:
        return False
    finally:
        with _lock:
            _waiters.discard(future)


def revision():
    with _lock:
        return _revision


class DashboardRefresh:
    """One instance per connected page. Quiet polls do not read task files."""
    def __init__(self, refresh):
        self.refresh = refresh
        self.seen = revision()
        self.pending = None

    def poll(self):
        current = revision()
        if current == self.seen or (self.pending is not None and not self.pending.done()):
            return False
        result = self.refresh()
        if isawaitable(result):
            async def finish():
                completed = await result
                if completed is not None:
                    self.seen = current
                return completed
            self.pending = asyncio.create_task(finish())
            return self.pending
        if result is not None:
            self.seen = current
        return result
