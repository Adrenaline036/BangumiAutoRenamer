"""One accepted queue request refreshes the frozen current TV work, not search.

The ephemeral lease belongs to the exact accepted job and its real worker
thread. Persisted requests are evidence, never reconstructed execution permits.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .decision_state import DecisionStateStore
from .plan_store import PlanStore
from .plan_execution import PlanExecutionError
from .projection_authority import TaskTerminalMutation, validate_plan_projection_receipt
from ..utils.utils import (read_task_at, safe_task_path, task_record_snapshot_at,
                          replace_task_record_at_if_sha256)


FIELD = 'current_tv_refresh'
OPERATION = 'refresh_current_tv'
_HASH = re.compile(r'[0-9a-f]{64}')
_QUEUE_FIELDS = {FIELD, 'workflow_status', 'queue_position', 'started_at',
                 'completed_at', 'error', 'updated_at', 'review_requested_at'}
_FIELDS = {'policy', 'task_id', 'request_id', 'parent_plan_fingerprint',
           'decision_fingerprint', 'task_preimage_sha256', 'queued_business_sha256',
           'scope_fingerprint', 'roots_fingerprint', 'plan_task_roots_fingerprint'}
_LEASES = {}


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def business_hash(task):
    return _hash(_encode({key: value for key, value in task.items() if key not in _QUEUE_FIELDS}).encode('utf-8'))


def _roots(manager):
    from .recognition_queue_admission import _roots as admission_roots
    execution = manager.execution_service
    paths = (*admission_roots(manager), manager.review_root, execution.record_root,
             execution.work_catalog.root, execution.series_catalog.root, manager.provider_link_catalog_root)
    return _hash(_encode([str(Path(path).resolve()) for path in paths]).encode('utf-8'))


@dataclass(frozen=True)
class CurrentTVRefreshRequest:
    payload_json: str

    def __post_init__(self):
        value = json.loads(self.payload_json)
        if (not isinstance(value, dict) or set(value) != _FIELDS
                or value.get('policy') != 'current-tv-refresh-v1'
                or any(not isinstance(value[key], str) or not value[key] for key in _FIELDS)
                or _encode(value) != self.payload_json):
            raise ValueError('原作品刷新请求格式无效')
        safe_task_path(Path('.'), value['task_id'])
        if re.fullmatch(r'[0-9a-f]{32}', value['request_id']) is None:
            raise ValueError('原作品刷新缺少唯一请求身份')
        if any(_HASH.fullmatch(value[key]) is None for key in _FIELDS - {'policy', 'task_id', 'request_id'}):
            raise ValueError('原作品刷新请求缺少完整指纹')

    def to_dict(self):
        return json.loads(self.payload_json)

    @property
    def fingerprint(self):
        return _hash(self.payload_json.encode('utf-8'))

    @classmethod
    def from_mapping(cls, value):
        return cls(_encode(value))


def request_from_task(task):
    value = task.get(FIELD)
    if (not isinstance(value, dict) or set(value) != {'request', 'status', 'result_plan_fingerprint'}
            or value['status'] not in {'queued', 'running', 'failed', 'interrupted', 'completed'}):
        raise ValueError('原作品刷新记录缺失或损坏，请保留原记录核查')
    request = CurrentTVRefreshRequest.from_mapping(value['request'])
    result = value['result_plan_fingerprint']
    if ((value['status'] == 'completed' and (not isinstance(result, str) or _HASH.fullmatch(result) is None))
            or (value['status'] != 'completed' and result is not None)
            or request.to_dict()['task_id'] != task.get('uuid')):
        raise ValueError('原作品刷新结果与任务不一致')
    return request


def candidate(manager, task_id, data, scope, *, source):
    """Called only inside the existing admission boundary; no extra writes."""
    from ..task_queue import TaskJob
    from .current_plan_retirement import is_current_tv, _roots_fingerprint
    from .default_content import require_unexecuted_task
    plan, state = manager.plan_store.load(task_id), manager.decision_state_store.load(task_id)
    if not is_current_tv(plan, state):
        return None
    require_unexecuted_task(data)
    if data.get('preview_only'):
        raise ValueError('原作品刷新需要正式任务身份，不能执行预览任务')
    if FIELD in data:
        request_from_task(data)  # Do not hide a damaged earlier request with a new nonce.
    before, _, _ = task_record_snapshot_at(manager.task_root, task_id)
    nonce = uuid.uuid4().hex
    data = dict(data)
    data.update(workflow_status='queued', queue_operation=OPERATION,
        recognition_job_id=nonce, queue_position=len(manager._pending) + 1,
        task_source=source, started_at=None, completed_at=None, error=None)
    request = CurrentTVRefreshRequest(_encode(dict(policy='current-tv-refresh-v1',
        task_id=task_id, request_id=nonce, parent_plan_fingerprint=PlanStore.fingerprint(plan),
        decision_fingerprint=DecisionStateStore.fingerprint(state), task_preimage_sha256=_hash(before),
        queued_business_sha256=business_hash(data), scope_fingerprint=_hash(_encode(scope.to_dict()).encode('utf-8')),
        roots_fingerprint=_roots(manager),
        plan_task_roots_fingerprint=_roots_fingerprint(manager.plan_store.root, manager.task_root))))
    data[FIELD] = dict(request=request.to_dict(), status='queued', result_plan_fingerprint=None)
    job = TaskJob(task_id=task_id, path=Path(data['path']), operation=OPERATION,
        source=source, request_id=nonce, task_source_scope=scope, current_tv_refresh=request)
    return job, data


def accepted(manager, job):
    """Only the standard synchronous sink calls this after successful insertion."""
    if job.operation == OPERATION:
        request = job.current_tv_refresh
        if (type(request) is not CurrentTVRefreshRequest or request.to_dict()['request_id'] != job.request_id
                or not any(item is job for item in manager._queue._queue)):
            raise ValueError('原作品刷新未得到实际队列接纳')
        manager._tv_refresh_accepted[job.request_id] = job


def _check_queued_authority(manager, job, task):
    from .recognition_queue_admission import _qualify, _authority
    request = job.current_tv_refresh
    if (type(request) is not CurrentTVRefreshRequest or manager._tv_refresh_accepted.get(job.request_id) is not job
            or request_from_task(task) != request):
        raise ValueError('原作品刷新不是本队列实际接纳的原作业')
    value = request.to_dict()
    if (value['task_id'] != job.task_id or value['request_id'] != job.request_id
            or task.get('queue_operation') != OPERATION or task.get('recognition_job_id') != job.request_id
            or _roots(manager) != value['roots_fingerprint']
            or business_hash(task) != value['queued_business_sha256']):
        raise ValueError('原作品刷新请求、目录或任务业务内容已变化')
    scope, _ = _qualify(manager, job.task_id, task)
    if (scope != job.task_source_scope or _hash(_encode(scope.to_dict()).encode('utf-8')) != value['scope_fingerprint']):
        raise ValueError('原作品刷新来源范围已变化')
    _authority(manager, job.task_id, task, manager.plan_store.path_for(job.task_id).read_bytes(),
               manager.decision_state_store.path_for(job.task_id).read_bytes())
    if (task.get('plan_fingerprint') != value['parent_plan_fingerprint']
            or DecisionStateStore.fingerprint(manager.decision_state_store.load(job.task_id)) != value['decision_fingerprint']):
        raise ValueError('原作品刷新不能重新绑定后来的计划或决定')
    journal = manager.projection_commit_service._read_journal(job.task_id)
    if journal and journal.get('status') not in {'completed', 'failed_closed'}:
        raise ValueError('原作品刷新之前尚有未结束事务')


class _Lease:
    __slots__ = ()


def validate_active(context, service, current, state, request):
    if type(context) is not _Lease:
        raise ValueError('原作品刷新缺少实际worker签发的上下文')
    active = _LEASES.get(context)
    if active is None or active['service'] is not service or active['thread'] != threading.get_ident():
        raise ValueError('原作品刷新缺少实际worker的有效线程所有权')
    manager, job = active['manager'], active['job']
    if (manager.active_task_id != job.task_id or job.current_tv_refresh != request
            or service.plan_store is not manager.plan_store
            or service.decision_state_store is not manager.decision_state_store
            or service.task_root != manager.task_root or service.root_snapshot_source is not manager.root_snapshot_source):
        raise ValueError('原作品刷新上下文已失效或服务被替换')
    _, task, fingerprint = task_record_snapshot_at(manager.task_root, job.task_id)
    _check_queued_authority(manager, job, task)
    if (fingerprint != active['running_sha256'] or task[FIELD]['status'] != 'running'
            or task.get('workflow_status') != 'running'
            or PlanStore.fingerprint(current) != request.to_dict()['parent_plan_fingerprint']
            or DecisionStateStore.fingerprint(state) != request.to_dict()['decision_fingerprint']):
        raise ValueError('原作品刷新已经终止或原任务发生变化')
    return request


def marker(plan):
    return plan.to_dict()['metadata'].get(FIELD) if plan is not None else None


def marker_fingerprint(plan):
    value = marker(plan)
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'request_fingerprint', 'parent_plan_fingerprint'}
            or any(not isinstance(item, str) or _HASH.fullmatch(item) is None for item in value.values())):
        raise ValueError('原作品刷新计划标记格式无效')
    return _hash(_encode(value).encode('utf-8'))


def require_marker_inheritance(current, draft):
    if marker_fingerprint(current) != marker_fingerprint(draft):
        raise ValueError('普通事务不能新增、删除或更换原作品刷新完成标记')
    return True


def validate_envelope(transaction, envelope, previous_plan):
    intent = envelope.initial_tv_completion
    old, new = marker(previous_plan), marker(envelope.draft_plan)
    if intent is not None and intent.inputs.mode == 'refresh':
        request = validate_active(intent.refresh_context, intent.context.service, previous_plan,
                                  envelope.previous_decision, intent.inputs.refresh_request)
        if new != dict(request_fingerprint=request.fingerprint,
                       parent_plan_fingerprint=request.to_dict()['parent_plan_fingerprint']):
            raise ValueError('新计划未绑定原作品刷新请求')
        return request
    if require_marker_inheritance(previous_plan, envelope.draft_plan) is not True:
        raise ValueError('原作品刷新继承校验已断开')
    return None


def validate_terminal(transaction, previous, payload, decision, plan):
    intent = transaction.initial_tv_completion_terminal
    active_intent = getattr(transaction, '_current_tv_refresh_request', None)
    if active_intent is None:
        if previous.get(FIELD) != payload.get(FIELD):
            raise ValueError('只有原作品刷新事务能改变刷新请求结果')
        return
    request = request_from_task(previous)
    if (request != active_intent or request_from_task(payload) != request
            or previous[FIELD]['status'] != 'running' or payload[FIELD]['status'] != 'completed'
            or payload[FIELD]['result_plan_fingerprint'] != PlanStore.fingerprint(plan)
            or payload.get('queue_operation') != 'rename'
            or intent is None):
        raise ValueError('原作品刷新Task终态未使用原请求与实际新计划')


def _service(manager):
    from .plan_review import PlanReviewService
    execution = manager.execution_service
    service = PlanReviewService(manager.plan_store, manager.review_root, execution.work_catalog.root,
        decision_state_root=manager.decision_state_store.root, task_root=manager.task_root,
        provider_link_catalog_root=manager.provider_link_catalog_root, record_root=execution.record_root,
        root_snapshot_source=manager.root_snapshot_source)
    service.decision_state_store = manager.decision_state_store
    if service.series_catalog.root.resolve() != execution.series_catalog.root.resolve():
        raise ValueError('原作品刷新与实际系列登记目录不同')
    return service


def _run(context):
    from .file_lock import task_operation_lock
    from .initial_tv_completion import prepare, commit
    from .tv_completion_observations import FrozenTVResolution, TVSeasonObservation
    from .rename_plan import WorkIdentity, ProviderIdentity
    from .projection_authority import RecognitionDecisionInput
    from .series_registration import _plain
    active = _LEASES[context]
    manager, job, service = active['manager'], active['job'], active['service']
    active['thread'] = threading.get_ident()
    with task_operation_lock(manager.task_root, job.task_id, operation=OPERATION):
        current, state = service.plan_store.load(job.task_id), service.decision_state_store.load(job.task_id)
        request = validate_active(context, service, current, state, job.current_tv_refresh)
        raw = current.to_dict()['metadata']['tv_completion_resolution']
        recognition = RecognitionDecisionInput(job.task_id, WorkIdentity.from_dict(state.confirmed_work),
            tuple(ProviderIdentity.from_dict(row) for row in state.linked_provider_identities),
            state.season_binding['season_number'], state.season_binding['decision_source'],
            'tv_episodes', DecisionStateStore.fingerprint(state))
        intent = prepare(service, recognition,
            resolution=FrozenTVResolution(_encode(raw)).resolution() if raw is not None else None,
            seasons=TVSeasonObservation.from_mapping(_plain(current.metadata.get('tv_completion_seasons'))),
            special=_plain(current.metadata.get('tv_completion_special')), mode='refresh',
            refresh_context=context, refresh_request=request)
        ordinary = service._revision_task_terminal(job.task_id)
        def terminal(decision, projected, task):
            result = ordinary.builder(decision, projected, task)
            result.update(queue_operation='rename', queue_position=None)
            result[FIELD] = dict(request=request.to_dict(), status='completed',
                                 result_plan_fingerprint=PlanStore.fingerprint(projected))
            return result
        return commit(service, intent, task_terminal=TaskTerminalMutation.prepare(
            manager.task_root, job.task_id, terminal))


def _thread_run(context):
    try:
        return _run(context)
    except (KeyboardInterrupt, SystemExit) as exc:
        # The canonical transaction has intentionally retained its journal.
        # Do not let a background-thread process signal kill the queue loop.
        raise RuntimeError('原作品刷新线程中断，原事务已保留等待恢复') from exc


def require_completed_request(*, task_id, plan_root, task_root, plan_fingerprint):
    """Static approval/execute consumer; no media or Provider observation."""
    task = read_task_at(task_root, task_id)
    current = PlanStore(plan_root).load(task_id)
    current_marker = marker_fingerprint(current)
    if FIELD not in task:
        if task.get('queue_operation') == OPERATION or current_marker is not None:
            raise ValueError('原作品刷新请求缺失，不能恢复旧计划批准')
        return
    request = request_from_task(task)
    from .current_plan_retirement import _roots_fingerprint
    if request.to_dict()['plan_task_roots_fingerprint'] != _roots_fingerprint(plan_root, task_root):
        raise ValueError('原作品刷新记录不属于当前计划与任务目录')
    if plan_fingerprint == request.to_dict()['parent_plan_fingerprint']:
        raise ValueError('原作品刷新已申请，旧计划仅供查看，请完成刷新后批准新计划')
    if task[FIELD]['status'] != 'completed':
        raise ValueError('原作品刷新尚未完整完成，不能批准或执行')
    store = PlanStore(plan_root)
    result = store.load_version(task_id, task[FIELD]['result_plan_fingerprint'])
    for plan in (result, current):
        validate_plan_projection_receipt(plan)
        if marker(plan) != dict(request_fingerprint=request.fingerprint,
                               parent_plan_fingerprint=request.to_dict()['parent_plan_fingerprint']):
            raise ValueError('原作品刷新结果与实际计划历史不一致')
    journal_path = safe_task_path(Path(plan_root).parent / 'projection_journal', task_id)
    journal = json.loads(journal_path.read_bytes())
    if (PlanStore.fingerprint(current) != plan_fingerprint or task.get('plan_fingerprint') != plan_fingerprint
            or journal.get('status') != 'completed' or journal.get('current_plan_fingerprint') != plan_fingerprint
            or journal.get('authority_fingerprint') != current.projection_receipt.fingerprint):
        raise ValueError('原作品刷新缺少实际完成事务，需先核查恢复记录')
    return request


def _completed(manager, job):
    request = require_completed_request(task_id=job.task_id, plan_root=manager.plan_store.root,
        task_root=manager.task_root, plan_fingerprint=PlanStore.fingerprint(manager.plan_store.load(job.task_id)))
    journal = manager.projection_commit_service._read_journal(job.task_id)
    if (request != job.current_tv_refresh or journal.get('current_tv_refresh_request') != request.fingerprint
            or journal.get('current_tv_refresh_parent') != request.to_dict()['parent_plan_fingerprint']
            or journal.get('current_task_sha256') != task_record_snapshot_at(manager.task_root, job.task_id)[2]
            or journal.get('current_decision_fingerprint') != DecisionStateStore.fingerprint(manager.decision_state_store.load(job.task_id))):
        raise ValueError('worker未证明原作品刷新实际完整提交')


async def drive(manager, job):
    from .recognition_queue_admission import _authority_locks
    from ..task_queue import utc_now
    from ..queue_runtime import on_owner
    context = None
    running_hash = None
    claimed = False
    owns_slot = False
    def claim():
        nonlocal claimed, running_hash
        with _authority_locks(manager, job.task_id):
            _, task, fingerprint = task_record_snapshot_at(manager.task_root, job.task_id)
            _check_queued_authority(manager, job, task)
            if (task[FIELD]['status'] != 'queued' or task.get('workflow_status') != 'queued'
                    or manager.active_task_id != job.task_id or job.task_id not in manager._pending):
                raise ValueError('原作品刷新作业已过期或不是待处理原请求')
            task[FIELD] = {**task[FIELD], 'status': 'running'}
            task.update(workflow_status='running', queue_position=0, started_at=utc_now())
            running_hash = replace_task_record_at_if_sha256(manager.task_root, job.task_id,
                expected_sha256=fingerprint, data=task)
            on_owner(manager._pending.remove, job.task_id)
            claimed = True
    def finish_failure(exc):
        try:
            _completed(manager, job)
        except (ValueError, RuntimeError, OSError):
            _, task, current_hash = task_record_snapshot_at(manager.task_root, job.task_id)
            journal = manager.projection_commit_service._read_journal(job.task_id)
            if current_hash == running_hash and journal.get('status') in {'completed', 'failed_closed'}:
                task[FIELD] = {**task[FIELD], 'status': 'failed'}
                task.update(workflow_status='failed', error=f'原作品刷新未完成：{exc}', queue_position=None)
                replace_task_record_at_if_sha256(manager.task_root, job.task_id,
                    expected_sha256=current_hash, data=task)
    try:
        if manager.active_task_id is not None:
            raise ValueError('另一个作业正在运行')
        # 首次磁盘等待前占住worker，stop会等待其实际收尾而不是取消领取过程。
        manager.active_task_id = job.task_id
        owns_slot = True
        await manager._disk(claim)
        context = _Lease()
        _LEASES[context] = dict(manager=manager, job=job, service=_service(manager),
                                thread=None, running_sha256=running_hash)
        worker = asyncio.create_task(manager._disk(_thread_run, context))
        # A cancelled await is not thread completion. Keep the real owner until
        # the shielded job reaches an actual result, including repeated cancel.
        while True:
            try:
                await asyncio.shield(worker)
                break
            except asyncio.CancelledError:
                if worker.done():
                    worker.result()
                    break
        await manager._disk(_completed, manager, job)
    except Exception as exc:
        if claimed:
            await manager._disk(finish_failure, exc)
        from ..logger import logger
        logger.warning(f'原作品刷新请求未正常返回：{exc}')
    finally:
        if context is not None:
            _LEASES.pop(context, None)
        if owns_slot and manager.active_task_id == job.task_id:
            manager.active_task_id = None
        if manager._tv_refresh_accepted.get(job.request_id) is job:
            manager._tv_refresh_accepted.pop(job.request_id)


def recover_interrupted(manager, task_id):
    try:
        _, task, fingerprint = task_record_snapshot_at(manager.task_root, task_id)
    except (ValueError, OSError):
        from ..logger import logger
        logger.warning('启动时发现无法核对的任务记录，已原样保留；其余任务继续恢复')
        return True
    if task.get('queue_operation') != OPERATION:
        return False
    try:
        request_from_task(task)
    except ValueError:
        return True  # Preserve damaged evidence; never reconstruct an accepted job.
    if task[FIELD]['status'] in {'queued', 'running'}:
        task[FIELD] = {**task[FIELD], 'status': 'interrupted'}
        task.update(workflow_status='interrupted', queue_position=None,
                    error='应用重启前原作品刷新尚未完成，请核查后点击重试；不会自动执行')
        replace_task_record_at_if_sha256(manager.task_root, task_id, expected_sha256=fingerprint, data=task)
    return True
