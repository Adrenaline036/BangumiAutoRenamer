"""Async GUI transport for the single canonical TV content command."""
from ..queue_runtime import ui_disk_call
import asyncio
import inspect
from contextlib import nullcontext
from dataclasses import dataclass
import os

from ..rename.plan_store import PlanStore


async def await_result(value):
    return await value if inspect.isawaitable(value) else value


@dataclass
class RevisionProgress:
    running: bool = False
    status: str = ''
    worker: object = None
    completion: object = None
    error: str = ''
    fingerprint: str = ''
    revision: int = -1


# UI transport only, scoped to a store and task. Backend revision/CAS remains
# authoritative. All registry accesses happen on the application event loop.
_inflight = {}


class SavedRevisionNeedsReload(ValueError):
    """The command returned successfully, but its saved view could not be loaded."""


def _operation_key(owner):
    return (os.path.normcase(os.path.abspath(owner.review_service.plan_store.root)), owner.plan.task_id)


def revision_progress(owner):
    progress = _inflight.get(_operation_key(owner))
    if progress is None:
        progress = getattr(owner, '_tv_content_progress', None) or RevisionProgress()
    owner._tv_content_progress = progress
    return progress


async def revise(owner, *, mode, edit, expected_revision, expected_fingerprint, notify, message, quiet=False):
    edit = dict(edit)
    service, task_id = owner.review_service, owner.plan.task_id
    return await run_revision(owner,
        submit=lambda: service.revise_tv_content(task_id, expected_fingerprint,
            expected_revision=expected_revision, mode=mode, edit=edit),
        expected_revision=expected_revision, expected_fingerprint=expected_fingerprint,
        notify=notify, message=message, quiet=quiet)


def revision_matches(owner, fingerprint, revision):
    if owner.task_decision is None:
        return False
    actual = (PlanStore.fingerprint(owner.plan), owner.task_decision.revision)
    return actual == (fingerprint, revision)


def revision_owner_alive(owner, *, worker_field='_tv_content_worker', closed_field='_tv_content_closed'):
    return not any(getattr(owner, key, False) for key in
                   ('is_deleted', 'exit_requested', closed_field, worker_field + '_disconnected'))


def register_revision_lifecycle(owner, notify, *, worker_field='_tv_content_worker',
                                closed_field='_tv_content_closed', quiet=False):
    """Register every waiter, including another window joining a shared worker."""
    lifecycle_field = worker_field + '_lifecycle_registered'
    if getattr(owner, lifecycle_field, False):
        return
    client = getattr(owner, 'client', None)
    if client is not None and callable(getattr(client, 'on_disconnect', None)):
        client.on_disconnect(lambda: setattr(owner, worker_field + '_disconnected', True))
    if client is not None and callable(getattr(client, 'on_connect', None)):
        def reconnected():
            setattr(owner, worker_field + '_disconnected', False)
            if not quiet and revision_owner_alive(owner, worker_field=worker_field, closed_field=closed_field):
                progress = revision_progress(owner)
                notify(progress.status if progress.running else '后台操作已结束，请关闭后重新打开任务核对最新结果。', type='info')
        client.on_connect(reconnected)
    if callable(getattr(owner, 'on_value_change', None)):
        owner.on_value_change(lambda event: setattr(owner, closed_field, True) if not event.value else None)
    setattr(owner, lifecycle_field, True)


async def run_revision(owner, *, submit, expected_revision, expected_fingerprint, notify, message,
                       worker_field='_tv_content_worker', closed_field='_tv_content_closed', on_success=None,
                       quiet=False, adopt=None, on_finished=None, started_message=None, on_error=None, approval=False):
    """Shared GUI waiter lifecycle; submit still uses a typed backend command."""
    def alive():
        return revision_owner_alive(owner, worker_field=worker_field, closed_field=closed_field)

    if not alive():
        if not getattr(owner, 'is_deleted', False):
            notify('此窗口已关闭或断开，请重新打开任务核对当前状态。', type='warning')
        return
    key = _operation_key(owner)
    if key in _inflight or getattr(owner, worker_field, None) is not None:
        revision_progress(owner)
        notify('本任务仍在处理中，请勿重复提交；可以关闭后重新打开查看进度。', type='info')
        return
    client = getattr(owner, 'client', None)
    register_revision_lifecycle(owner, notify, worker_field=worker_field, closed_field=closed_field, quiet=quiet)
    service, task_id = owner.review_service, owner.plan.task_id
    fingerprint = expected_fingerprint
    if (owner.task_decision is None or owner.task_decision.revision != expected_revision
            or PlanStore.fingerprint(owner.plan) != fingerprint):
        notify('页面已经更新，请刷新后重新选择。', type='warning')
        return
    if not quiet:
        notify(started_message or '正在后台核对内容；请勿重复点击。关闭窗口不等于取消后台操作。', type='info')
    progress = RevisionProgress(fingerprint=fingerprint, revision=expected_revision)
    owner._tv_content_progress = progress
    progress.running, progress.status = True, '正在准备整理计划…'
    progress.error = ''
    _inflight[key] = progress
    progress.approval = approval
    if approval:
        from .execution_wait import changed
        from .plan_review_page import task_queue
        changed(task_queue)
    def operation():
        plan = submit()
        try:
            current = service.plan_store.load(task_id)
            state = service.decision_state_store.load(task_id)
            if (PlanStore.fingerprint(current) != PlanStore.fingerprint(plan)
                    or current.metadata.get('decision_state_fingerprint') != service.decision_state_store.fingerprint(state)):
                raise ValueError('保存后任务已有更新，不会覆盖后来选择。')
        except Exception as exc:
            raise SavedRevisionNeedsReload(str(exc)) from exc
        return plan, state

    worker = asyncio.create_task(ui_disk_call(operation))
    progress.worker = worker
    setattr(owner, worker_field, worker)

    async def finish_in_client(done):
        if done is not getattr(owner, worker_field, None):
            return
        if done.cancelled():
            progress.status = '后台终态尚未确认，请重新打开核对；当前操作仍保持锁定。'
            if alive():
                notify('后台终态尚未确认，请刷新核对；当前操作仍保持锁定。', type='warning')
            return
        saved = False
        try:
            plan, state = done.result()
            saved = True
            progress.status = '处理已完成，请重新打开任务查看最新计划。'
            if alive():
                if (PlanStore.fingerprint(owner.plan) != fingerprint
                        or owner.task_decision.revision != expected_revision):
                    raise ValueError('界面已有后来修订，请刷新当前结果；不会覆盖后来选择。')
                owner.task_decision = state
                if on_success is not None:
                    await await_result(on_success())
                if adopt is None:
                    await await_result(owner._adopt_revision(plan))
                else:
                    await await_result(adopt(plan))
                progress.status = '处理已完成'
                if not quiet:
                    notify(message, type='positive')
        except Exception as exc:
            saved = saved or isinstance(exc, SavedRevisionNeedsReload)
            progress.status = ('计划已保存，页面未能同步，请重新打开查看最新计划。' if saved else
                               f'处理失败：{exc}；请重新打开任务核对。')
            progress.error = progress.status
            if alive():
                if on_error is not None and not saved:
                    await await_result(on_error(exc))
                else:
                    notify(progress.status if saved else str(exc), type='warning' if saved else 'negative')
        finally:
            setattr(owner, worker_field, None)
            progress.running = False
            if _inflight.get(key) is progress:
                _inflight.pop(key)
            if approval:
                from ..queue_runtime import disk_call
                from ..utils.utils import read_task_at
                try:
                    task = await disk_call(read_task_at, service.task_root, task_id)
                    changed(task_queue, task)
                except (OSError, ValueError):
                    changed(task_queue)
            if alive() and on_finished is not None:
                await await_result(on_finished())

    async def finish():
        try:
            await asyncio.shield(worker)
        except (Exception, asyncio.CancelledError):
            pass
        with client if client is not None else nullcontext():
            await finish_in_client(worker)

    completion = asyncio.create_task(finish())
    progress.completion = completion
    try:
        await asyncio.shield(completion)
    except asyncio.CancelledError:
        if alive():
            notify('等待已中断，后台仍在处理；不能据此重复提交。', type='warning')
    except Exception:
        pass  # The sole completion callback reports the error and releases busy state.

    if (worker.done() and not worker.cancelled() and not progress.error and alive()):
        try:
            plan, state = worker.result()
            if (PlanStore.fingerprint(owner.plan) == PlanStore.fingerprint(plan)
                    and owner.task_decision.revision == state.revision):
                return plan, state
        except Exception:
            return None
