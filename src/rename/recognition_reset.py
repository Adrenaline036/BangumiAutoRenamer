"""原任务重新识别：保留来源，原子退出旧决定，并可在中断后向前恢复。"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

_writer = ContextVar('recognition_reset_writer', default=None)


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _safe(path):
    from .path_safety import first_reparse_component
    path = Path(path).absolute()
    if '..' in path.parts or first_reparse_component(path) is not None:
        raise ValueError('重新识别记录不能经过链接或重解析路径')
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError('重新识别对象必须是独立普通文件')
    return path


def _directory(base, task_id):
    if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9._-]*', str(task_id)):
        raise ValueError('重新识别任务身份无效')
    return Path(base).absolute() / 'recognition_resets' / task_id


def _raw(path):
    path = _safe(path)
    return path.read_bytes() if path.exists() else None


def _publish(path, raw):
    path = _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if path.read_bytes() != raw:
        raise ValueError('重新识别记录写后校验失败')


def _record(base, task_id):
    directory = _directory(base, task_id)
    pointer = _raw(directory / 'current.json')
    if pointer is None:
        return None, None
    pointer = json.loads(pointer)
    if set(pointer) != {'request_id', 'sha256'} or not re.fullmatch('[0-9a-f]{32}', pointer['request_id']):
        raise ValueError('重新识别恢复指针无效')
    operation = directory / pointer['request_id']
    raw = _raw(operation / 'journal.json')
    if raw is None or hashlib.sha256(raw).hexdigest() != pointer['sha256']:
        raise ValueError('重新识别恢复记录指纹不符')
    record = json.loads(raw)
    if record.get('task_id') != task_id or record.get('request_id') != pointer['request_id'] or record.get('base') != str(Path(base).absolute()):
        raise ValueError('重新识别恢复记录归属不符')
    return operation, record


def require_ready(base, task_id):
    if _writer.get() == (str(Path(base).absolute()), task_id):
        return
    operation, record = _record(base, task_id)
    if operation is not None and _raw(operation / 'completed.json') != _encode({'request_id':record['request_id']}):
        raise ValueError('重新识别重置尚未完成，请先恢复；旧计划不可继续操作')


def first_revision(base, task_id):
    require_ready(base, task_id)
    _, record = _record(base, task_id)
    return record['first_revision'] if record is not None else 0


def require_plan_current(base, plan):
    floor = first_revision(base, plan.task_id)
    if floor and (type(plan.metadata.get('decision_state_revision')) is not int or plan.metadata['decision_state_revision'] < floor):
        raise ValueError('此计划已因重新识别退出，仅供历史查看')


@contextmanager
def _publishing(base, task_id):
    token = _writer.set((str(Path(base).absolute()), task_id))
    try:
        yield
    finally:
        _writer.reset(token)
        from ..utils.task_changes import mark_changed
        mark_changed()


@dataclass(frozen=True)
class RecognitionReset:
    task_id: str
    epoch: int
    payload: bytes
    job: object
    request_json: str
    opened_evidence: object


def _paths(manager, task_id, old_batch, new_batch):
    projector = manager.projection_commit_service
    return dict(task=manager.task_root / (task_id + '.json'),
        plan=manager.plan_store.path_for(task_id), decision=manager.decision_state_store.path_for(task_id),
        review=manager.review_root / (task_id + '.json'),
        projection=projector._journal_path(task_id), ownership=projector.ownership_store._path(task_id),
        old_batch=manager.batch_scope_store.path_for(old_batch), new_batch=manager.batch_scope_store.path_for(new_batch))


def _prepare(manager, task_id, request, opened_evidence, *, request_id=None):
    from .recognition_queue_admission import (_roots, _authority_locks, _check_available, _qualify,
        _authority, _workspace_data, WorkspaceOpenEvidence, _json)
    from ..utils.utils import task_record_snapshot_at
    from .automatic_recognition import AutomaticRecognitionOrigin, _digest_scope
    from ..task_queue import TaskJob
    _roots(manager)
    with _authority_locks(manager, task_id):
        _check_available(manager, task_id)
        before, current, _ = task_record_snapshot_at(manager.task_root, task_id)
        scope, batch_id = _qualify(manager, task_id, current)
        plan = _raw(manager.plan_store.path_for(task_id))
        decision = _raw(manager.decision_state_store.path_for(task_id))
        from .episode_range import require_unbound
        require_unbound(manager.decision_state_store.load_optional(task_id))
        if (type(opened_evidence) is not WorkspaceOpenEvidence or opened_evidence.task_id != task_id
                or opened_evidence.task_json != _json(current) or opened_evidence.plan != plan or opened_evidence.decision != decision):
            raise ValueError('窗口打开后任务或计划已变化，请刷新')
        _authority(manager, task_id, current, plan, decision)
        from .input_directory_discovery import require_source_group_confirmed
        require_source_group_confirmed(current)
        settings = _workspace_data(current, request, scope, plan is not None or decision is not None)
        request_id = request_id or uuid.uuid4().hex
        old, new = manager.batch_scope_store.prepare_recognition_reset(scope, batch_id=batch_id,
            new_batch_id='recognition-' + request_id, media_domain='movie' if settings['is_movie'] is True else 'tv')
        new_scope = new.task_source_scopes[0]
        data = {k:current[k] for k in ('uuid','path','created_at','preview_only','use_ai') if k in current}
        data.update({k:settings[k] for k in ('is_anime','is_movie','name','season_id','season_number','season_source')})
        data.update(batch_id=new.batch_id, batch_scope_fingerprint=new_scope.batch_scope_fingerprint,
            reservation_lineage_id=new_scope.reservation_lineage_id, reservation_revision=new_scope.reservation_revision,
            task_source_scope=new_scope.to_dict(), source_group_pending=False, provider_selection=None,
            workflow_status='queued', queue_operation='rename', queue_position=len(manager._pending)+1,
            task_source='task_workspace', started_at=None, completed_at=None, error=None, recognition_job_id=request_id)
        job = TaskJob(task_id=task_id, path=Path(data['path']), is_anime=data['is_anime'], is_movie=data['is_movie'],
            custom_name=settings['search_name'], custom_season_id=settings['season_id'], use_ai=data.get('use_ai'),
            source='task_workspace', preview_only=bool(data.get('preview_only')), task_source_scope=new_scope)
        if not job.preview_only and new_scope.content_policy is not None:
            origin = AutomaticRecognitionOrigin(task_id, request_id, new.batch_id, _digest_scope(new_scope),
                '', '', -1, str(job.path), job.is_anime, job.is_movie, job.custom_name, job.use_ai, job.custom_season_id, job.source)
            job = replace(job, recognition_origin=origin)
            data['automatic_recognition_origin'] = origin.to_dict()
        ownership = manager.projection_commit_service.ownership_store.load_optional(task_id)
        retired = replace(ownership, state='retired', revision=ownership.revision+1, reason='重新识别') if ownership else None
        base = manager.plan_store.root.parent.absolute()
        if any(Path(p).absolute().parent != base for p in (manager.task_root, manager.decision_state_store.root, manager.review_root)):
            raise ValueError('重新识别的数据目录不在同一可恢复根下')
        paths = _paths(manager, task_id, batch_id, new.batch_id)
        after = dict(task=_encode(data), old_batch=_encode(old.to_dict()), new_batch=_encode(new.to_dict()),
                     ownership=_encode(retired.to_dict()) if retired else None)
        rows = []
        for kind, path in paths.items():
            raw = _raw(path)
            if kind == 'new_batch' and raw is not None:
                raise ValueError('新识别批次已存在')
            rows.append(dict(kind=kind, path=str(path.absolute()), before=raw.hex() if raw is not None else None,
                             after=after[kind].hex() if after.get(kind) is not None else None))
        prior_floor = first_revision(base, task_id)
        used = json.loads(decision)['revision'] if decision is not None else prior_floor - 1
        record = dict(schema_version=1, task_id=task_id, request_id=request_id, base=str(base),
            old_batch=batch_id, new_batch=new.batch_id, first_revision=max(prior_floor, used+1), rows=rows)
        return RecognitionReset(task_id, manager._admission_epoch, _encode(record), job, _json(request), opened_evidence)


def _check_record(manager, record):
    if (set(record) != {'schema_version','task_id','request_id','base','old_batch','new_batch','first_revision','rows'}
            or record['schema_version'] != 1 or record['base'] != str(manager.plan_store.root.parent.absolute())
            or type(record['first_revision']) is not int or record['first_revision'] < 0
            or not re.fullmatch('[0-9a-f]{32}', record['request_id'])):
        raise ValueError('重新识别事务格式无效')
    paths = _paths(manager, record['task_id'], record['old_batch'], record['new_batch'])
    if len(record['rows']) != len(paths) or {r['kind'] for r in record['rows']} != set(paths):
        raise ValueError('重新识别恢复清单不完整')
    for row in record['rows']:
        path = _safe(row['path'])
        if path != paths[row['kind']].absolute() or not path.is_relative_to(Path(record['base'])):
            raise ValueError('重新识别恢复目标越界')
        for field in ('before','after'):
            if row[field] is not None:
                bytes.fromhex(row[field])


def _finish(manager, operation, record):
    _check_record(manager, record)
    # 先验证全部对象，出现第三种内容不继续写其他对象。
    for row in record['rows']:
        actual = _raw(row['path'])
        before = bytes.fromhex(row['before']) if row['before'] is not None else None
        after = bytes.fromhex(row['after']) if row['after'] is not None else None
        if actual not in (before, after):
            raise ValueError('重新识别恢复发现后来修改，已停止')
        if after is None and before is not None and actual is None and _raw(operation / 'retired' / (row['kind']+'.json')) != before:
            raise ValueError('重新识别历史文件缺失，已停止')
    by_kind = {r['kind']:r for r in record['rows']}
    for kind in ('new_batch','old_batch','plan','decision','review','projection','ownership','task'):
        row = by_kind[kind]
        before = bytes.fromhex(row['before']) if row['before'] is not None else None
        after = bytes.fromhex(row['after']) if row['after'] is not None else None
        path = Path(row['path'])
        if before is not None:
            backup = operation / 'before' / (kind+'.json')
            if not backup.exists():
                _publish(backup, before)
            if _raw(backup) != before:
                raise ValueError('重新识别前像核验失败')
        if _raw(path) == after:
            continue
        if after is None:
            archived = operation / 'retired' / (kind+'.json')
            if archived.exists():
                raise ValueError('重新识别历史目标已存在')
            archived.parent.mkdir(parents=True, exist_ok=True)
            path.rename(archived)
            if _raw(archived) != before:
                raise ValueError('重新识别历史文件核验失败')
        else:
            if kind == 'task' and record.get('new_batch'):
                # 新scope及空工作区已落盘，才接纳新作业身份。
                data = json.loads(after)
                from .task_batch_scope import TaskSourceScope
                scope = TaskSourceScope.from_mapping(data['task_source_scope'])
                manager.batch_scope_store.require_active_scope(scope, batch_id=data['batch_id'])
                if manager.plan_store.path_for(record['task_id']).exists() or manager.decision_state_store.path_for(record['task_id']).exists():
                    raise ValueError('旧计划或决定尚未退出')
                origin = data.get('automatic_recognition_origin')
                if origin is not None:
                    from .automatic_recognition import AutomaticRecognitionOrigin, _digest_scope
                    origin = AutomaticRecognitionOrigin.from_mapping(origin)
                    if (origin.scope_fingerprint != _digest_scope(scope) or origin.batch_id != data['batch_id']
                            or origin.job_id != data['recognition_job_id'] or origin.path != data['path']
                            or origin.original_revision != -1 or origin.plan_fingerprint or origin.decision_fingerprint):
                        raise ValueError('新作业身份与已落盘来源不一致')
            _publish(path, after)
        hook = manager.projection_commit_service._phase_hook
        if hook:
            hook('recognition_reset_' + kind)
    _publish(operation / 'completed.json', _encode({'request_id':record['request_id']}))


def _publish_transaction(manager, record, payload):
    task_id = record['task_id']
    directory = _directory(record['base'], task_id)
    operation = directory / record['request_id']
    if operation.exists():
        raise ValueError('重新识别请求已提交，不能重复执行')
    _publish(operation / 'journal.json', payload)
    previous = _raw(directory / 'current.json')
    if previous is not None:
        _publish(operation / 'before-current.json', previous)
    _publish(directory / 'current.json', _encode(dict(request_id=record['request_id'], sha256=hashlib.sha256(payload).hexdigest())))
    with _publishing(record['base'], task_id):
        _finish(manager, operation, record)


def commit(projector, manager, prepared):
    from .recognition_queue_admission import _authority_locks, _available, accepted_publication, _require_not_cancelled
    if type(prepared) is not RecognitionReset or projector is not manager.projection_commit_service:
        raise ValueError('重新识别缺少原任务的完整准备')
    task_id = prepared.task_id
    record = json.loads(prepared.payload)
    checked = _prepare(manager, task_id, json.loads(prepared.request_json), prepared.opened_evidence,
                       request_id=record['request_id'])
    if checked != prepared:
        raise ValueError('重新识别准备与原输入不一致')
    with accepted_publication(manager, epoch=prepared.epoch) as accept, \
            _authority_locks(manager, task_id), projector.ownership_store._mutation_lock():
        _available(manager, task_id)
        if manager._admission_epoch != prepared.epoch or not manager._started:
            raise ValueError('队列状态已变化，重置未提交')
        _check_record(manager, record)
        for row in record['rows']:
            if _raw(row['path']) != (bytes.fromhex(row['before']) if row['before'] is not None else None):
                raise ValueError('准备后任务资料发生变化，重置未提交')
        _require_not_cancelled()
        _publish_transaction(manager, record, prepared.payload)
        # 旧状态完整写入后由主循环接纳；接纳回调不包含磁盘操作。
        try:
            accept(prepared.job)
        except BaseException:
            # 新请求和完整记录保留，重启或人工重试不会复活旧批准。
            raise
    return task_id


async def admit(manager, task_id, request, opened_evidence):
    from .recognition_queue_admission import _require_not_cancelled
    from .plan_execution import PlanExecutionError
    try:
        _require_not_cancelled()
        if type(manager._queue) is not asyncio.Queue:
            raise ValueError('队列类型不支持安全重置')
        prepared = await manager._disk(_prepare, manager, task_id, request, opened_evidence)
        _require_not_cancelled()
        if not manager._started:
            await manager.start()
        from ..queue_runtime import transaction
        result = await transaction(manager, manager.projection_commit_service.reset_recognition, manager, prepared)
        # 原作业可能已被主循环消费者领取，持锁提交返回值是接纳凭据。
        if result != task_id:
            raise ValueError('重新识别提交没有确认原作业')
        return result
    except (ValueError, RuntimeError, OSError) as exc:
        raise PlanExecutionError(str(exc)) from exc


def recover(manager):
    from .recognition_queue_admission import _authority_locks
    base = manager.plan_store.root.parent.absolute()
    root = base / 'recognition_resets'
    if not root.exists():
        return
    for directory in root.iterdir():
        operation, record = _record(base, directory.name)
        if record is None:
            continue
        if _raw(operation / 'completed.json') == _encode({'request_id':record['request_id']}):
            continue
        with _authority_locks(manager, directory.name), manager.projection_commit_service.ownership_store._mutation_lock():
            with _publishing(base, directory.name):
                _finish(manager, operation, record)


@dataclass(frozen=True)
class _ResetStores:
    task_root: Path
    review_root: Path
    plan_store: object
    decision_state_store: object
    batch_scope_store: object
    projection_commit_service: object


def bind_discovered_type(service, proof):
    """自动判定类型后，在首个工作区前接续同一来源替换事务。"""
    from .automatic_recognition import validate_original_job, _digest_scope, parse_batch, parse_search_policy
    from .task_batch_scope import TaskBatchScopeManifestStore
    from .projection_authority import ProjectionCommitService
    from .recognition_queue_admission import _authority_locks
    domain = 'movie' if proof.resolved_task_flags()[1] else 'tv'
    stores = _ResetStores(service.task_root, service.review_root, service.plan_store,
        service.decision_state_store, TaskBatchScopeManifestStore(service.plan_store.root.parent/'task_batch_scope'),
        ProjectionCommitService(service.decision_state_store, service.plan_store, task_root=service.task_root))
    task_id = proof.origin.task_id
    with _authority_locks(stores, task_id), stores.projection_commit_service.ownership_store._mutation_lock():
        task, scope = validate_original_job(service, proof.origin)
        if parse_batch(proof.batch_json).policy != parse_search_policy(task.get('automatic_search_policy'), proof.origin):
            raise ValueError('自动类型不是原查询范围的结果')
        if scope.media_domain == domain:
            return proof
        if service.plan_store.path_for(task_id).exists() or service.decision_state_store.path_for(task_id).exists():
            raise ValueError('已有工作区不能重新绑定自动类型')
        request_id = uuid.uuid4().hex
        old, new = stores.batch_scope_store.prepare_recognition_reset(scope, batch_id=proof.origin.batch_id,
            new_batch_id='recognition-'+request_id, media_domain=domain)
        new_scope = new.task_source_scopes[0]
        origin = replace(proof.origin, batch_id=new.batch_id, scope_fingerprint=_digest_scope(new_scope))
        task.update(batch_id=new.batch_id, batch_scope_fingerprint=new_scope.batch_scope_fingerprint,
            reservation_lineage_id=new_scope.reservation_lineage_id, reservation_revision=new_scope.reservation_revision,
            task_source_scope=new_scope.to_dict(), automatic_recognition_origin=origin.to_dict())
        paths = _paths(stores, task_id, old.batch_id, new.batch_id)
        after = dict(task=_encode(task), old_batch=_encode(old.to_dict()), new_batch=_encode(new.to_dict()))
        rows = []
        for kind, path in paths.items():
            raw = _raw(path)
            if kind == 'ownership' and raw is not None and json.loads(raw).get('state') == 'retired':
                after[kind] = raw
            elif kind not in {'task','old_batch'} and raw is not None:
                raise ValueError('自动类型绑定前存在旧工作状态')
            rows.append(dict(kind=kind, path=str(path.absolute()), before=raw.hex() if raw is not None else None,
                after=after[kind].hex() if kind in after else None))
        base = service.plan_store.root.parent.absolute()
        record = dict(schema_version=1, task_id=task_id, request_id=request_id, base=str(base),
            old_batch=old.batch_id, new_batch=new.batch_id, first_revision=first_revision(base,task_id), rows=rows)
        _publish_transaction(stores, record, _encode(record))
        return replace(proof, origin=origin)
