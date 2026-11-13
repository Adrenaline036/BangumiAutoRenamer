"""后台作品请求与窗口观察；持久决定仍由原队列写入。"""
import asyncio
import time
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

from ..queue_runtime import disk_call


def alive(view):
    return not any(getattr(view, key, False) for key in
                   ('is_deleted', 'exit_requested', '_closed', '_provider_link_closed'))


def current_work(manager, task_id):
    return manager._provider_reads.get(task_id)


def _check_epoch(manager, work):
    if manager._stopping or manager._admission_epoch != work.epoch:
        raise ValueError('服务已停止，本次选择未继续提交，请重试。')


def _check(manager, work):
    data = manager._read(work.task_id)
    # 队列完成会更新审核时间；它不改变作品、来源或人工决定。
    comparable = lambda row: {k: v for k, v in row.items() if k != 'review_requested_at'}
    if not data or data.get('uuid') != work.task_id or comparable(data) != comparable(work.snapshot.task_data):
        raise ValueError('读取期间任务或来源已变化，请重新选择。')
    for path, raw in work.files:
        if (path.read_bytes() if path.exists() else None) != raw:
            raise ValueError('读取期间整理计划或人工决定已变化，请重新选择。')


async def _run(manager, work, reader, initial, selected, media_type, source, season_number):
    from . import plan_review_page as page
    from .initial_workspace import create_initial_workspace
    from .add_task_page import media_flags
    from ..rename.input_directory_discovery import require_source_group_confirmed
    from ..rename.provider_selection_authority import provider_candidate_group_kind, provider_selection_capability, task_media_kind_from_flag, task_library_domain_from_flag
    from ..rename.review_operation import current_provider_selection_review_operation
    try:
        _check_epoch(manager, work)
        await disk_call(_check, manager, work)
        _check_epoch(manager, work)
        require_source_group_confirmed(work.snapshot.task_data)
        if source != str(work.snapshot.task_data.get('path') or ''):
            raise ValueError('来源目录已改变，请重新添加来源。')
        if selected is not None and initial:
            comparable = lambda row: {k: v for k, v in row.items() if k != 'group_kind'}
            if not any(comparable(row) == comparable(selected)
                       for row in page.normalize_provider_candidates(work.snapshot.task_data)):
                raise ValueError('候选已变化，请重新选择。')
        candidate = await reader(work.url) if selected is None or initial else selected
        if not isinstance(candidate, dict):
            raise ValueError('作品链接未返回有效候选，请重新读取。')
        _check_epoch(manager, work)
        await disk_call(_check, manager, work)
        _check_epoch(manager, work)
        if initial and selected is not None and any(str(candidate.get(k) or '') != str(selected.get(k) or '')
                                                    for k in ('namespace', 'entity_type', 'provider_id')):
            raise ValueError('作品详情与所选条目不一致，请重新选择。')
        work.candidate = candidate
        state = work.snapshot.task_decision
        if initial:
            if any(raw is not None for _, raw in work.files):
                raise ValueError('已有整理记录，请重新打开原任务。')
            kind = provider_candidate_group_kind(candidate)
            if media_type == 'auto':
                domain = candidate.get('library_domain')
                if domain not in {'animation', 'general'} or kind is None:
                    raise ValueError('资料无法确定类型，请手动选择后重试。')
                media_type = page._media_type_from_flags(domain == 'animation', kind == 'movie_continuous')
            anime, movie = media_flags(media_type)
            capability = provider_selection_capability(candidate, group_kind=kind or '',
                task_media_kind=task_media_kind_from_flag(movie), task_library_domain=task_library_domain_from_flag(anime))
        elif work.snapshot.plan is None:
            capability = provider_selection_capability(candidate, group_kind=provider_candidate_group_kind(candidate) or '',
                task_media_kind=task_media_kind_from_flag(work.snapshot.task_data.get('is_movie')),
                task_library_domain=task_library_domain_from_flag(work.snapshot.task_data.get('is_anime')))
        else:
            capability = page._candidate_capability(work.snapshot, candidate)
        if not capability.actionable:
            raise ValueError(capability.message)
        manager._require_recognition_retry_safe(work.snapshot.task_data)
        if initial:
            work.status = '正在准备任务；关闭窗口后继续。'
            state, _ = await create_initial_workspace(work.task_id, work.snapshot.task_data, media_type=media_type)
        if manager._stopping or manager._admission_epoch != work.epoch:
            raise ValueError('服务已停止，本次选择未入队，请重试。')
        if state is None:
            raise ValueError('原任务尚无可读取的作品决定，请使用重新识别。')
        await manager.enqueue_reidentify(work.task_id, candidate, state.revision, season_number=season_number)
        # 应用停止只等待接纳收尾，不能等待尚未被单队列消费的作业。
        manager._admissions.discard(asyncio.current_task())
        work.admitted = True
        work.status = '已加入后台队列，计划按顺序生成；关闭窗口后继续。'
        await _observe_result(manager, work)
    except Exception as exc:
        work.error = str(exc)
        work.status = f'无法应用作品选择：{exc}'
    finally:
        manager._admissions.discard(asyncio.current_task())
        if work.result is not None and work.lifecycle == 'succeeded' and current_work(manager, work.task_id) is work:
            manager._provider_reads.pop(work.task_id)


async def _observe_result(manager, work):
    from . import plan_review_page as page
    from ..rename.review_operation import current_provider_selection_review_operation
    _, submitted = current_provider_selection_review_operation(await disk_call(manager._read, work.task_id))
    request_id = getattr(work, 'request_id', None) or submitted.get('request_id')
    work.request_id = request_id
    operation = await manager.wait_for_selection_result(work.task_id, timeout=None)
    if manager._admission_epoch != work.epoch:
        raise ValueError('服务已停止，请重新打开任务核对已保存的状态。')
    latest_task = await disk_call(manager._read, work.task_id)
    _check_epoch(manager, work)
    _, latest = current_provider_selection_review_operation(latest_task)
    if not request_id or operation.get('request_id') != request_id or latest != operation:
        raise ValueError('任务选择已更新，请查看当前结果。')
    if operation.get('lifecycle') not in {'succeeded', 'awaiting_user', 'awaiting_season'}:
        raise ValueError(str(latest_task.get('error') or '作品选择未完成，请核对任务。'))
    plan, state, result_task = await disk_call(_read_result, manager, work.task_id)
    _check_epoch(manager, work)
    _, result_operation = current_provider_selection_review_operation(result_task)
    if result_operation != operation:
        raise ValueError('任务选择已更新，请查看当前结果。')
    if (operation.get('result_plan_fingerprint') != page.PlanStore.fingerprint(plan)
            or operation.get('result_decision_revision') != state.revision
            or plan.metadata.get('decision_state_fingerprint') != page.DecisionStateStore.fingerprint(state)):
        raise ValueError('作品决定已变化，请查看当前结果。')
    work.lifecycle = operation['lifecycle']
    work.operation = operation
    work.result = plan, state
    work.status = '整理计划已更新。' if operation['lifecycle'] == 'succeeded' else '作品已选择，请继续确认季度或作品归属。'

def _read_result(manager, task_id):
    return (manager.plan_store.load(task_id), manager.decision_state_store.load(task_id),
            manager._read(task_id))


def _prepare(manager, task_id, snapshot):
    from . import plan_review_page as page
    from ..rename.input_directory_discovery import require_source_group_confirmed
    require_source_group_confirmed(manager._read(task_id))
    plan, state = snapshot.plan, snapshot.task_decision
    if plan is None and manager.plan_store.path_for(task_id).exists():
        plan = manager.plan_store.load(task_id)
    if state is None and manager.decision_state_store.path_for(task_id).exists():
        state = manager.decision_state_store.load(task_id)
    if plan is not None:
        if page.PlanStore.fingerprint(manager.plan_store.load(task_id)) != page.PlanStore.fingerprint(plan):
            raise ValueError('计划已变化，请刷新后继续。')
        if state is None or manager.decision_state_store.load(task_id).revision != state.revision:
            raise ValueError('人工决定已变化，请刷新后继续。')
    files = [(path, path.read_bytes() if path.exists() else None) for path in
             (manager.plan_store.path_for(task_id), manager.decision_state_store.path_for(task_id))]
    return SimpleNamespace(plan=plan, task_decision=state, task_data=snapshot.task_data), files


async def submit(view, selected=None, season_number=None):
    from . import plan_review_page as page
    from ..rename.input_directory_discovery import require_source_group_confirmed
    manager = page.task_queue
    task_id = getattr(view, 'task_id', None) or view.plan.task_id
    require_source_group_confirmed(view.task_data)
    # 点击时冻结输入；磁盘线程只读取快照，不访问控件。
    selected = deepcopy(selected)
    snapshot = SimpleNamespace(plan=getattr(view, 'plan', None),
        task_decision=getattr(view, 'task_decision', None), task_data=deepcopy(view.task_data))
    epoch = manager._admission_epoch
    field = getattr(view, 'provider_url_input', None) or getattr(view, 'provider_url', None)
    url = page.provider_candidate_url(selected) if selected is not None else str(getattr(field, 'value', '') or '').strip()
    type_field = getattr(view, 'rerecognition_media_type', None) or getattr(view, 'media_type', None)
    source_field = getattr(view, 'rerecognition_path', None) or getattr(view, 'path_input', None)
    media_type = type_field.value if type_field is not None else 'auto'
    source = str(source_field.value or '') if source_field is not None else str(snapshot.task_data.get('path') or '')
    inputs = (url, selected, season_number, media_type, source, capture_inputs(view))
    existing = current_work(manager, task_id)
    if existing is not None and (not existing.worker.done() or getattr(existing, 'cancelling', False)):
        if getattr(existing, 'inputs', None) != inputs:
            raise ValueError('已有作品选择正在处理；如需更换输入，请先取消等待。')
        return existing
    if manager.is_active(task_id) or task_id in manager._pending or task_id in manager._plan_updates:
        raise ValueError('任务已排队或正在处理，请稍后查看。')
    if not url and selected is None:
        raise ValueError('请先粘贴作品详情页链接。')
    work = SimpleNamespace(task_id=task_id, epoch=epoch, snapshot=snapshot, files=[],
        url=url, inputs=inputs, draft=capture_inputs(view), started=time.monotonic(),
        status='等待处理；关闭窗口后继续。', admitted=False, cancelled=False,
        cancelling=False, error='', candidate=None, result=None)
    # 注册不等待磁盘或公共接纳锁，第一次点击即可观察、去重和取消。
    work.worker = asyncio.create_task(_start(manager, work, selected, media_type, source, season_number))
    manager._provider_reads[task_id] = work
    manager._admissions.add(work.worker)
    work.worker.add_done_callback(lambda _: _publish_completed(manager, work))
    _changed(manager)
    return work


async def _start(manager, work, selected, media_type, source, season_number):
    from . import plan_review_page as page
    try:
        async with manager._admission_lock:
            _check_epoch(manager, work)
            work.snapshot, work.files = await disk_call(_prepare, manager, work.task_id, work.snapshot)
            _check_epoch(manager, work)
            if manager.is_active(work.task_id) or work.task_id in manager._pending or work.task_id in manager._plan_updates:
                raise ValueError('任务已排队或正在处理，请稍后查看。')
        work.status = '正在后台读取作品链接；关闭窗口后继续。'
        initial = work.snapshot.plan is None and work.snapshot.task_decision is None
        await _run(manager, work, page._read_manual_provider_url, initial, selected,
                   media_type, source, season_number)
    except asyncio.CancelledError:
        # cancel() 在真实线程结束后核对接纳结果，避免误报取消成功。
        pass
    except Exception as exc:
        work.error = str(exc)
        work.status = f'无法应用作品选择：{exc}'
    finally:
        manager._admissions.discard(asyncio.current_task())


async def _resume(manager, work):
    try:
        await _observe_result(manager, work)
    except Exception as exc:
        work.error = str(exc)
        work.status = f'无法应用作品选择：{exc}'
    finally:
        if work.result is not None and work.lifecycle == 'succeeded' and current_work(manager, work.task_id) is work:
            manager._provider_reads.pop(work.task_id)


async def cancel(manager, task_id):
    work = current_work(manager, task_id)
    if work is None or manager.is_active(task_id):
        return False
    pending = getattr(work, 'cancel_task', None)
    if pending is not None and not pending.done():
        return await asyncio.shield(pending)
    if work.worker.done():
        return bool(getattr(work, 'cancelled', False))
    work.cancelling = True
    _changed(manager)
    work.status = '正在取消等待，请等待当前操作安全收尾。'
    work.cancel_task = asyncio.create_task(_cancel(manager, work))
    manager._admissions.add(work.cancel_task)
    work.cancel_task.add_done_callback(manager._admissions.discard)
    return await asyncio.shield(work.cancel_task)


async def _cancel(manager, work):
    worker = work.worker
    try:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        if work.admitted:
            from ..rename.selection_wait import withdraw
            if not await withdraw(manager, work.task_id, work.request_id):
                work.status = '任务已开始处理，不能取消等待。'
                work.worker = asyncio.create_task(_resume(manager, work))
                work.worker.add_done_callback(lambda _: _publish_completed(manager, work))
                return False
        work.cancelled = True
        work.error = ''
        work.status = '已取消等待；原任务和输入已保留，可重新提交。'
        return True
    except Exception as exc:
        work.status = f'取消等待失败：{exc}'
        if work.admitted:
            work.worker = asyncio.create_task(_resume(manager, work))
            work.worker.add_done_callback(lambda _: _publish_completed(manager, work))
        raise
    finally:
        work.cancelling = False
        _changed(manager)


_INPUT_FIELDS = {
    'url': ('provider_url_input', 'provider_url'),
    'media_type': ('rerecognition_media_type', 'media_type'),
    'source': ('rerecognition_path', 'path_input'),
    'name': ('rerecognition_name', 'name_input'),
    'season': ('rerecognition_season', 'season_input'),
}


def capture_inputs(view):
    result = {}
    for key, names in _INPUT_FIELDS.items():
        field = next((getattr(view, name, None) for name in names if getattr(view, name, None) is not None), None)
        if field is not None:
            result[key] = deepcopy(field.value)
    return result


def controls(view, work):
    busy = not work.worker.done() or getattr(work, 'cancelling', False)
    button = getattr(view, 'provider_submit_button', None)
    if button is not None and not button.is_deleted:
        button.set_enabled(not busy)
    button = getattr(view, 'cancel_wait_button', None)
    if button is not None and not button.is_deleted:
        from . import plan_review_page as page
        button.set_visibility(busy)
        button.set_enabled(busy and not getattr(work, 'cancelling', False)
                           and not page.task_queue.is_active(work.task_id))


def waiting(manager, task_id):
    work = current_work(manager, task_id)
    return bool(work is not None and (getattr(work, 'cancelling', False) or
        (not work.worker.done() and not work.error and work.result is None)))


def _changed(manager, task=None):
    from ..utils.task_changes import mark_changed
    from .data_table_page import project_selection_change
    mark_changed()
    project_selection_change(manager, task)


def _publish_completed(manager, work):
    _changed(manager)
    asyncio.create_task(_publish_result(manager, work))


async def _publish_result(manager, work):
    # 单任务记录读取不等待列表扫描、Provider或共享登记事务。
    if current_work(manager, work.task_id) not in (None, work):
        return
    try:
        task = await disk_call(manager._read, work.task_id)
    except (OSError, ValueError):
        task = None
    if current_work(manager, work.task_id) not in (None, work):
        return
    work.completed_task = task
    _changed(manager, task)


def _matches_result(work, task):
    saved = getattr(work, 'completed_task', None)
    if not saved:
        return True
    return all(task.get(key) == saved.get(key) for key in ('workflow_status', 'plan_fingerprint'))


def overlay_rows(manager, rows):
    positions = {task_id: index for index, task_id in enumerate(manager._pending, 1)}
    projected = []
    for original in rows:
        row = {**original, '_display': dict(original['_display'])}
        task_id = str(row['uuid'])
        if row.get('workflow_status') == 'queued' and task_id in positions:
            row['queue_position'] = positions[task_id]
        work = current_work(manager, task_id)
        if work is not None and _matches_result(work, row):
            current_terminal = (row.get('_selection_finished') and work.admitted
                and row.get('_selection_request_id') == getattr(work, 'request_id', None))
            if current_terminal:
                row['_selection_review'] = True
            elif waiting(manager, task_id):
                row['_selection_wait'] = True
                active = manager.is_active(task_id)
                row['workflow_status'] = 'running' if active else 'queued'
                row['_selection_stage'] = ('正在取消' if getattr(work, 'cancelling', False) else
                    '处理中' if active else '排队中')
                row['_selection_cancel'] = not active and not getattr(work, 'cancelling', False)
            elif work.error or work.result is not None or getattr(work, 'cancelled', False):
                row['_selection_review'] = True
                row['workflow_status'] = 'need_confirm'
                row['queue_position'] = None
                row['_display']['feedback'] = work.status if work.error else ''
        projected.append(row)
    return projected


async def select(view, selected_candidate=None, *, season_number=None):
    from . import plan_review_page as page
    if not alive(view):
        return
    page._provider_selection_alive(view)
    try:
        work = await submit(view, selected_candidate, season_number)
        close = getattr(view, '_close_review', None) or view.close
        close()
    except (ValueError, OSError, RuntimeError, TypeError) as exc:
        if alive(view):
            with getattr(view, 'client', None) or nullcontext():
                page.notify(str(exc), type='negative')


def attach(view):
    from . import plan_review_page as page
    task_id = getattr(view, 'task_id', None) or getattr(getattr(view, 'plan', None), 'task_id', None)
    work = current_work(page.task_queue, task_id)
    if work is None or not _matches_result(work, view.task_data):
        return
    for key, names in _INPUT_FIELDS.items():
        if key not in getattr(work, 'draft', {}): continue
        field = next((getattr(view, name, None) for name in names if getattr(view, name, None) is not None), None)
        if field is not None: field.set_value(work.draft[key])
    controls(view, work)
    if work.error:
        with view.selection_region:
            page.ui.label(work.status).classes('bar-task-feedback bar-error-text')
