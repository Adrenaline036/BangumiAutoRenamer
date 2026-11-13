"""执行确认的后台等待；正式接纳仍由原队列事务决定。"""
import asyncio
import os
from types import SimpleNamespace

from ..queue_runtime import disk_call


def registry(manager):
    if not hasattr(manager, '_execution_waits'):
        manager._execution_waits = {}
    return manager._execution_waits


def changed(manager, task=None):
    from .provider_selection_work import _changed
    _changed(manager, task)


def submit(manager, task_id, fingerprint):
    previous = registry(manager).get(task_id)
    if (manager._stopping or manager.is_active(task_id) or task_id in manager._pending
            or previous is not None and not previous.worker.done()):
        raise ValueError('任务正在等待或执行，请勿重复提交')
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise ValueError('缺少当前已批准计划版本')
    work = SimpleNamespace(task_id=task_id, fingerprint=fingerprint, error='',
                           cancelled=False, cancelling=False, admitted=False)
    registry(manager)[task_id] = work
    work.worker = asyncio.create_task(_run(manager, work))
    manager._admissions.add(work.worker)
    work.worker.add_done_callback(manager._admissions.discard)
    changed(manager)
    return work


async def _run(manager, work):
    try:
        await manager.enqueue_approved(work.task_id, work.fingerprint)
        work.admitted = True
    except asyncio.CancelledError:
        work.cancelled = True
    except Exception as exc:
        work.error = str(exc)
    finally:
        manager._admissions.discard(asyncio.current_task())
        try:
            task = await disk_call(manager._read, work.task_id)
        except (OSError, ValueError):
            task = None
        work.finished = True
        changed(manager, task)


async def cancel(manager, task_id):
    work = registry(manager).get(task_id)
    if work is None or manager.is_active(task_id) or (work.worker.done() and not work.admitted):
        return False
    if getattr(work, 'cancel_task', None) is not None:
        return await asyncio.shield(work.cancel_task)
    async def finish():
        work.cancelling = True
        changed(manager)
        try:
            if not work.worker.done():
                work.worker.cancel()
                await asyncio.gather(work.worker, return_exceptions=True)
            if work.admitted:
                from ..rename.selection_wait import withdraw
                if not await withdraw(manager, task_id, getattr(work, 'request_id', None)):
                    work.cancelling = False
                    changed(manager)
                    return False
                work.admitted = False
                work.cancelled = True
                work.finished = True
                task = await disk_call(manager._read, task_id)
                changed(manager, task)
            if work.worker.cancelled():
                work.cancelled = True
                work.finished = True
            work.cancelling = False
            changed(manager)
            return work.cancelled and task_id not in manager._pending and not manager.is_active(task_id)
        finally:
            work.cancelling = False
            changed(manager)
    work.cancel_task = asyncio.create_task(finish())
    manager._admissions.add(work.cancel_task)
    work.cancel_task.add_done_callback(manager._admissions.discard)
    return await asyncio.shield(work.cancel_task)


def overlay_rows(manager, rows):
    from .tv_content_actions import _inflight
    store_key = os.path.normcase(os.path.abspath(manager.plan_store.root))
    result = []
    for original in rows:
        row = {**original, '_display': dict(original['_display'])}
        task_id = row['uuid']
        work = registry(manager).get(task_id)
        if work is not None and row.get('plan_fingerprint') != work.fingerprint:
            work = None
        queued = work is not None and work.admitted and task_id in manager._pending and not manager.is_active(task_id)
        if work is not None and (queued or not work.admitted and not getattr(work, 'finished', False)):
            row.update(_execution_wait=True, _selection_wait=True,
                       _selection_stage='正在取消' if work.cancelling else '排队中',
                       _selection_cancel=not work.cancelling, workflow_status='queued')
        elif work is not None and (work.error or work.cancelled):
            if row['workflow_status'] not in {'queued', 'executing', 'running', 'completed'}:
                row.update(_execution_review=True, _selection_review=True)
                row['_display']['feedback'] = ('执行预检失败：' + work.error if work.error else '已取消等待，原批准计划保留。')
        if any(key == (store_key, task_id) and progress.running and getattr(progress, 'approval', False)
               for key, progress in _inflight.items()):
            row['_approval_wait'] = True
        result.append(row)
    return result
