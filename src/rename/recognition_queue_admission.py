"""Single no-await publication boundary for unexecuted recognition requests.

Observations are not a new decision authority. Existing Plan/Decision/batch
writers are excluded while one exact Task is published and accepted.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from ..queue_runtime import on_owner, require_not_cancelled, transaction
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .decision_state import DecisionStateStore
from .file_lock import task_operation_lock
from .plan_execution import PlanExecutionError
from .plan_store import PlanStore
from .projection_authority import validate_plan_projection_receipt
from .review_operation import provider_selection_awaits_user
from .task_batch_scope import TaskSourceScope
from ..utils.utils import (TaskAcceptanceUncertain, publish_task_and_accept_at,
                           safe_task_path, task_record_snapshot_at)
from ..logger import logger


def _raw(path):
    return path.read_bytes() if path.exists() else None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class WorkspaceOpenEvidence:
    task_id: str
    task_json: str
    plan: bytes | None
    decision: bytes | None


def capture_workspace_open(manager, task_id, task_data):
    """Capture once when the dialog opens, never on the submit callback."""
    return WorkspaceOpenEvidence(task_id, _json(task_data),
        _raw(manager.plan_store.path_for(task_id)),
        _raw(manager.decision_state_store.path_for(task_id)))


@dataclass(frozen=True)
class RecognitionAdmissionPreparation:
    task_id: str
    mode: str
    request_json: str
    roots: tuple[str, ...]
    task: bytes
    plan: bytes | None
    decision: bytes | None
    batch_id: str
    batch: bytes
    epoch: int


def _roots(manager):
    projector = manager.projection_commit_service
    pairs = ((projector.task_root, manager.task_root),
             (projector.plan_store.root, manager.plan_store.root),
             (projector.state_store.root, manager.decision_state_store.root),
             (manager.batch_scope_store.root, manager.plan_store.root.parent / 'task_batch_scope'))
    if any(Path(a).resolve() != Path(b).resolve() for a, b in pairs):
        raise PlanExecutionError('任务服务的计划或决定目录不一致，请核查配置')
    return tuple(str(Path(path).resolve()) for path in (
        manager.task_root, manager.plan_store.root, manager.decision_state_store.root,
        manager.batch_scope_store.root, projector.journal_root))


@contextmanager
def _authority_locks(manager, task_id):
    with task_operation_lock(manager.task_root, task_id, operation='recognition_admission'):
        with manager.projection_commit_service._transaction_lock(task_id):
            with manager.decision_state_store._lock(task_id):
                with manager.batch_scope_store._mutation_lock('recognition_admission'):
                    yield


def _available(manager, task_id):
    _require_not_cancelled()
    _check_available(manager, task_id)


def _check_available(manager, task_id):
    """Read-only availability checks, also usable by the preparation worker."""
    from .recognition_reset import require_ready
    require_ready(manager.plan_store.root.parent, task_id)
    manager._require_no_pending_partition(task_id)
    projector = manager.projection_commit_service
    journal_path = projector._journal_path(task_id)
    if journal_path.exists():
        journal = projector._read_journal(task_id)
        if not journal or journal.get('status') not in {'completed', 'failed_closed'}:
            raise PlanExecutionError('原计划事务尚未完成，请先核查恢复记录')
    if manager._stopping or manager.is_active(task_id) or task_id in manager._pending or task_id in manager._plan_updates:
        raise PlanExecutionError('任务已排队、正在处理或队列正在停止，请稍后刷新')


def _require_not_cancelled():
    require_not_cancelled()


def _qualify(manager, task_id, data):
    if not data or data.get('uuid') != task_id:
        raise PlanExecutionError('任务记录不存在或身份不一致')
    manager._require_recognition_retry_safe(data)
    if data.get('queue_admission_uncertain'):
        raise PlanExecutionError('上次入队结果不确定，已暂停重试，请核查任务记录')
    try:
        scope = TaskSourceScope.from_mapping(data.get('task_source_scope'))
        batch_id = str(data.get('batch_id') or '')
        if not batch_id or scope.task_id != task_id:
            raise ValueError('scope identity')
        manifest = manager.batch_scope_store.require_active_scope(scope, batch_id=batch_id)
        requests = [item for item in manifest.requests if item.task_id == task_id]
        if len(requests) != 1 or requests[0].media_domain != scope.media_domain:
            raise ValueError('scope request')
        if (data.get('reservation_lineage_id') != scope.reservation_lineage_id
                or requests[0].content_policy != scope.content_policy):
            raise ValueError('scope lineage/policy')
        from .source_basis import _proven_request, root_for_task
        _manifest, _request, _layout, observation = _proven_request(
            manager.projection_commit_service, batch_id=batch_id, scope=scope,
            source_root=root_for_task(data, scope))
        if Path(str(data.get('path') or '')) != observation:
            raise ValueError('scope original input')
    except (ValueError, TypeError, KeyError) as exc:
        raise PlanExecutionError('任务来源范围已失效，请核查原任务') from exc
    return scope, batch_id


def _authority(manager, task_id, data, plan_raw, decision_raw):
    if decision_raw is None and plan_raw is None:
        return
    state = manager.decision_state_store.load(task_id)
    from .content_policy import _scope_binding
    from .source_basis import validate_inheritance
    binding = _scope_binding(manager.projection_commit_service, task_id)
    if state.content_policy != binding.policy:
        raise PlanExecutionError('作品决定与已接纳的整理策略不同，请核查')
    if state.initial_source_basis is not None:
        validate_inheritance(manager.projection_commit_service, state.initial_source_basis, binding)
    if plan_raw is None:
        return
    plan = manager.plan_store.load(task_id)
    from .single_subject import require_single_subject_plan
    require_single_subject_plan(plan, state)
    validate_plan_projection_receipt(plan)
    if (data.get('plan_fingerprint') != PlanStore.fingerprint(plan)
            or plan.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(state)):
        raise PlanExecutionError('计划或作品决定已更新，请刷新后继续')


def search_name(value):
    """规范化明确的检索输入；None 表示使用来源标题。"""
    return str(value or '').strip() or None


def _name(value):
    return search_name(value) or '等待识别'


def _original_search_name(manager, current):
    from .automatic_recognition import AutomaticRecognitionOrigin, validate_original_job
    raw = current.get('automatic_recognition_origin')
    if raw is None:
        raise PlanExecutionError('原任务缺少冻结的检索输入，请在任务工作区确认名称后重新识别；原任务和计划已保留')
    origin = AutomaticRecognitionOrigin.from_mapping(raw)
    validate_original_job(manager, origin)
    return search_name(origin.custom_name)


def restore_title_confirmation(manager, task_id, *, expected_task):
    """恢复旧标题异常的待确认状态；原Plan、Decision和来源登记只读。"""
    from .automatic_recognition import AutomaticRecognition, parse_batch, validate_original_job, validate_search_policy
    from .content_selection import validate_plan_content
    from .default_content import require_unexecuted_task
    from .provider_selection_authority import unconfirmed_automatic_type
    from ..utils.utils import mutate_task_at
    if (expected_task.get('workflow_status') != 'failed'
            or expected_task.get('error') != '[任务队列] TMDB 条目缺少可确认的中文标题'):
        return False
    _roots(manager)
    with _authority_locks(manager, task_id):
        _check_available(manager, task_id)
        _raw_task, task, _ = task_record_snapshot_at(manager.task_root, task_id)
        if task != expected_task:
            raise PlanExecutionError('任务已变化，请刷新后继续')
        require_unexecuted_task(task)
        _qualify(manager, task_id, task)
        _authority(manager, task_id, task, _raw(manager.plan_store.path_for(task_id)),
                   _raw(manager.decision_state_store.path_for(task_id)))
        plan, state = manager.plan_store.load(task_id), manager.decision_state_store.load(task_id)
        if not unconfirmed_automatic_type(task, plan, state):
            raise PlanExecutionError('当前任务不是未确认的自动库存，保留原记录')
        validate_plan_content(plan, state)
        proof = AutomaticRecognition.from_mapping(plan.metadata.get('automatic_recognition'))
        validate_original_job(manager.projection_commit_service, proof.origin)
        validate_search_policy(manager.projection_commit_service, plan, proof.origin, proof)
        candidates = [dict(row) for row in parse_batch(proof.batch_json).candidates]
        def restore(current):
            if current != task:
                raise PlanExecutionError('任务已变化，原反馈未覆盖后来操作')
            current.update(workflow_status='need_confirm', completed_at=None, queue_position=None,
                execution_blocked=True, error='作品标题资料不足，请选择正确作品后继续',
                provider_candidates={'work': candidates}, type_match_state='mismatch')
            return current
        if mutate_task_at(manager.task_root, task_id, restore) is None:
            raise PlanExecutionError('原任务已不存在')
    return True


def _workspace_data(current, request, scope, has_authority):
    if Path(str(current.get('path') or '')).resolve() != Path(request['path']).resolve():
        raise PlanExecutionError('来源路径已改变；请安全放弃旧任务后创建新任务')
    if bool(request['preview_only']) != bool(current.get('preview_only')):
        raise PlanExecutionError('不能通过重试改变预览任务身份')
    season = request['custom_season_id']
    if season is not None and (type(season) is not int or season < 0):
        raise PlanExecutionError('季度必须是非负整数')
    for key in ('is_anime', 'is_movie'):
        if request[key] is not None and type(request[key]) is not bool:
            raise PlanExecutionError('类型或检索设置无效')
    movie = request['is_movie']
    result = dict(current)
    result.update(is_anime=request['is_anime'], is_movie=movie,
        name=_name(request['custom_name']), season_id=season, season_number=season,
        search_name=search_name(request['custom_name']),
        season_source='explicit_user' if season is not None else 'unset',
        use_ai=current.get('use_ai'))
    return result


def _candidate(manager, task_id, mode, request, current, has_authority):
    from ..task_queue import TaskJob
    scope, _ = _qualify(manager, task_id, current)
    if mode not in {'retry', 'selected', 'source_group'}:
        raise PlanExecutionError('无法识别的重试操作')
    from .input_directory_discovery import require_source_group_confirmed, validate_pending_group
    if mode == 'source_group':
        if has_authority or current.get('preview_only'):
            raise PlanExecutionError('已有计划或作品决定，不能重新确认来源组')
        validate_pending_group(current, scope)
        current = dict(current)
        current.update(source_group_pending=False, queue_operation='rename', name=None,
                       is_anime=None, is_movie=None, season_id=None, season_number=None,
                       season_source='unset', provider_selection=None)
    else:
        require_source_group_confirmed(current)
    if mode != 'selected' and provider_selection_awaits_user(current):
        raise PlanExecutionError('这次作品选择正在等待确认，请继续确认或安全移除')
    if has_authority:
        state = manager.decision_state_store.load_readonly(task_id)
        from .queued_work_selection import complete_kind
        selected_movie = (mode == 'selected' and complete_kind(manager.plan_store.load(task_id), state, request.get('selection') or {}) == 'movie')
        if (state.content_selection is not None and state.movie_version_selection is not None
                and not selected_movie):
            raise PlanExecutionError('此电影任务已完成选片规划，暂不支持原地重新识别；请先安全结束任务，再重新添加原目录选择作品')
    data = dict(current)
    source = request.get('source', 'retry')
    if has_authority and mode != 'selected' and manager.plan_store.path_for(task_id).exists():
        from .current_tv_refresh import candidate
        refresh = candidate(manager, task_id, data, scope, source=source)
        if refresh is not None:
            return refresh
    selection = request.get('selection') if mode == 'selected' else data.get('provider_selection')
    if mode == 'selected' or (isinstance(selection, dict) and selection and not data.get('preview_only')):
        revision = (request['expected_revision'] if mode == 'selected'
                    else manager.decision_state_store.load_readonly(task_id).revision)
        return manager._prepare_reidentify_job(task_id, selection, revision,
            season_number=request.get('season_number'), current=data, selection_mode=mode)
    source = request.get('source', 'retry')
    job = TaskJob(task_id=task_id, path=Path(str(data.get('path') or '')),
        is_anime=data.get('is_anime'), is_movie=data.get('is_movie'),
        custom_name=None if mode == 'source_group' else _original_search_name(manager, data),
        custom_season_id=data.get('season_id'), use_ai=data.get('use_ai'),
        provider_selection=data.get('provider_selection'), source=source,
        preview_only=bool(data.get('preview_only')), task_source_scope=scope)
    data.update(workflow_status='queued', queue_operation='rename',
        recognition_job_id=uuid.uuid4().hex, queue_position=len(manager._pending) + 1,
        task_source=source, started_at=None, completed_at=None, error=None)
    if (not has_authority and not job.preview_only and job.provider_selection is None
            and scope.content_policy is not None):
        from .automatic_recognition import AutomaticRecognitionOrigin
        origin = AutomaticRecognitionOrigin.at_admission(manager, job, batch_id=data['batch_id'])
        job = replace(job, recognition_origin=origin)
        data['automatic_recognition_origin'] = origin.to_dict()
        data['recognition_job_id'] = origin.job_id
        # This is a newly admitted search, not a replay of the old proof.
        # Its current configuration scope is frozen by the worker before HTTP.
        data.pop('automatic_search_policy', None)
    return job, data


def prepare(manager, task_id, *, mode, request, opened_evidence=None):
    _require_not_cancelled()
    return _prepare_readonly(manager, task_id, mode=mode, request=request,
                             opened_evidence=opened_evidence)


def _prepare_readonly(manager, task_id, *, mode, request, opened_evidence=None):
    roots = _roots(manager)
    with _authority_locks(manager, task_id):
        _check_available(manager, task_id)
        raw, current, _ = task_record_snapshot_at(manager.task_root, task_id)
        _, batch_id = _qualify(manager, task_id, current)
        plan = _raw(manager.plan_store.path_for(task_id))
        decision = _raw(manager.decision_state_store.path_for(task_id))
        if opened_evidence is not None:
            if (type(opened_evidence) is not WorkspaceOpenEvidence
                    or opened_evidence.task_id != task_id
                    or opened_evidence.task_json != _json(current)
                    or opened_evidence.plan != plan or opened_evidence.decision != decision):
                raise PlanExecutionError('窗口打开后任务、计划或作品决定已变化，请刷新')
        _authority(manager, task_id, current, plan, decision)
        request_json = _json(request)
        _candidate(manager, task_id, mode, json.loads(request_json), current,
                   plan is not None or decision is not None)
        return RecognitionAdmissionPreparation(task_id, mode, request_json, roots,
            raw, plan, decision, batch_id,
            manager.batch_scope_store.path_for(batch_id).read_bytes(), manager._admission_epoch)


def _accept(manager, job, *, expected_epoch=None):
    return on_owner(_accept_on_owner, manager, job, expected_epoch=expected_epoch)


def _accept_on_owner(manager, job, *, expected_epoch=None):
    _require_not_cancelled()
    if (manager._stopping or not manager._started
            or expected_epoch is not None and manager._admission_epoch != expected_epoch
            or manager.is_active(job.task_id) or job.task_id in manager._pending or job.task_id in manager._plan_updates):
        raise PlanExecutionError('队列状态已变化或任务已被接纳，请刷新后核对')
    manager._pending.append(job.task_id)
    try:
        if not job.preview_only:
            manager._queue.put_nowait(job)
        from .current_tv_refresh import accepted
        accepted(manager, job)
    except BaseException as exc:
        # Actual asyncio.Queue, same event loop, no await: nobody can consume
        # this exact object during put. Identity (not qsize) proves insertion.
        if any(item is job for item in manager._queue._queue):
            manager._queue._queue.remove(job)
            manager._queue.task_done()
            raise TaskAcceptanceUncertain('入队返回异常且曾插入作业，已隔离并暂停该任务') from exc
        manager._pending.remove(job.task_id)
        raise


@contextmanager
def accepted_publication(manager, *, epoch):
    """接纳成功后只容纳锁清理；凭据不依赖已被 worker 消费的队列状态。"""
    accepted_job = None
    def accept(job):
        nonlocal accepted_job
        _accept(manager, job, expected_epoch=epoch)
        accepted_job = job
    try:
        yield accept
    except OSError:
        if accepted_job is None:
            raise
        logger.warning('重新识别已接纳，但锁清理发生IO错误；请勿重复提交')


def commit(manager, prepared):
    if type(prepared) is not RecognitionAdmissionPreparation:
        raise PlanExecutionError('缺少有效的重试准备，请刷新')
    task_id = prepared.task_id
    if _roots(manager) != prepared.roots or manager._admission_epoch != prepared.epoch:
        raise PlanExecutionError('队列或服务已变化，请刷新后重试')

    def before_publish():
        _require_not_cancelled()
        if not manager._started or manager._stopping or manager._admission_epoch != prepared.epoch:
            raise PlanExecutionError('队列正在停止或已变化，未接纳新任务')

    with accepted_publication(manager, epoch=prepared.epoch) as accept:
        with _authority_locks(manager, task_id):
            _available(manager, task_id)
            if (not manager._started or _raw(manager.plan_store.path_for(task_id)) != prepared.plan
                    or _raw(manager.decision_state_store.path_for(task_id)) != prepared.decision
                    or _raw(manager.batch_scope_store.path_for(prepared.batch_id)) != prepared.batch):
                raise PlanExecutionError('计划、作品决定或来源范围已变化，请刷新')
            current = json.loads(prepared.task)
            _authority(manager, task_id, current, prepared.plan, prepared.decision)
            job, data = _candidate(manager, task_id, prepared.mode,
                json.loads(prepared.request_json), current,
                prepared.plan is not None or prepared.decision is not None)
            def publish_accept():
                accept(job)
                if prepared.mode == 'selected':
                    from .selection_wait import remember
                    on_owner(remember, manager, job, prepared, data)
            publish_task_and_accept_at(manager.task_root, task_id, previous=prepared.task,
                data=data, accept=publish_accept, before_publish=before_publish)
            # No business writes after acceptance: only lock cleanup remains.
    return task_id


async def admit(manager, task_id, *, mode, request, opened_evidence=None):
    pending = manager._provider_reads.get(task_id)
    if pending is not None and not pending.worker.done() and pending.worker is not asyncio.current_task():
        raise PlanExecutionError('本任务正在读取作品链接，请等待本次选择完成。')
    epoch = manager._admission_epoch
    async with manager._admission_lock:
        if manager._stopping or manager._admission_epoch != epoch:
            raise PlanExecutionError('队列已停止或重新启动，请刷新后重试')
        if mode == 'workspace':
            from .recognition_reset import admit as reset
            return await reset(manager, task_id, request, opened_evidence)
        if type(manager._queue) is not asyncio.Queue:
            raise PlanExecutionError('队列类型不支持安全接纳')
        try:
            _require_not_cancelled()
            # Reject duplicate clicks before yielding to the running consumer.
            if manager._stopping or manager.is_active(task_id) or task_id in manager._pending or task_id in manager._plan_updates:
                raise PlanExecutionError('任务已排队、正在处理或队列正在停止，请稍后刷新')
            prepared = await manager._disk(_prepare_readonly, manager, task_id,
                mode=mode, request=request, opened_evidence=opened_evidence)
            _require_not_cancelled()
            if type(prepared) is not RecognitionAdmissionPreparation:
                raise PlanExecutionError('缺少有效的重试准备')
            if manager._stopping or manager._admission_epoch != epoch or prepared.epoch != epoch:
                raise PlanExecutionError('队列已停止或重新启动，请刷新后重试')
            if not manager._started:
                await manager.start()
            return await transaction(manager, commit, manager, prepared)
        except PlanExecutionError:
            raise
        except (ValueError, RuntimeError, OSError) as exc:
            raise PlanExecutionError(f'无法重新识别，请刷新或核查原任务：{exc}') from exc
