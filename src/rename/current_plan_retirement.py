"""Immutable retirement of a current TV Plan after proven pre-media failure.

Retiring approval is distinct from retiring source ownership. The witness is
published before deleting Review or updating Task, and is never rolled back
because a later queue request failed. Readers do not inspect media.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from .content_policy import ContentPolicy
from .decision_state import DecisionStateStore
from .plan_execution import PlanExecutionError
from .plan_store import PlanStore
from .projection_authority import validate_plan_projection_receipt
from .task_batch_scope import TaskSourceScope
from ..utils.utils import mutate_task_at, safe_task_path


_HASH = re.compile(r'[0-9a-f]{64}')
_FIELDS = {'policy', 'task_id', 'plan_fingerprint', 'decision_fingerprint',
           'task_preimage_sha256', 'roots_fingerprint', 'batch_id', 'scope_fingerprint',
           'reservation_lineage_id', 'failure_code', 'execution_requested_fingerprint'}


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _roots_fingerprint(plan_root, task_root):
    return _hash(_encode([str(Path(plan_root).resolve()), str(Path(task_root).resolve())]).encode('utf-8'))


@dataclass(frozen=True)
class TVPlanRetirementWitness:
    payload_json: str

    def __post_init__(self):
        value = json.loads(self.payload_json)
        if (not isinstance(value, dict) or set(value) != _FIELDS
                or value['policy'] != 'current-tv-plan-retirement-v1'
                or _encode(value) != self.payload_json
                or any(not isinstance(value[key], str) or not value[key] for key in _FIELDS)):
            raise ValueError('旧计划退役证据格式无效')
        safe_task_path(Path('.'), value['task_id'])
        for key in ('plan_fingerprint', 'decision_fingerprint', 'task_preimage_sha256',
                    'roots_fingerprint', 'scope_fingerprint', 'execution_requested_fingerprint'):
            if _HASH.fullmatch(value[key]) is None:
                raise ValueError('旧计划退役证据指纹无效')
        if value['execution_requested_fingerprint'] != value['plan_fingerprint']:
            raise ValueError('旧批准与退役计划不一致')
        if value['failure_code'] not in {'target_conflict_requires_replan',
                'catalog_changed_requires_replan', 'legacy_target_conflict_requires_replan'}:
            raise ValueError('旧计划退役不是明确的执行前失败')

    def to_dict(self):
        return json.loads(self.payload_json)


def _path(plan_root, task_id, fingerprint):
    if not isinstance(fingerprint, str) or _HASH.fullmatch(fingerprint) is None:
        raise ValueError('旧计划退役缺少准确指纹')
    task_name = safe_task_path(Path(plan_root).absolute().parent / 'plan_retirements', task_id).stem
    return Path(plan_root).absolute().parent / 'plan_retirements' / task_name / (fingerprint + '.json')


def _safe_components(path):
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('旧计划退役证据不能经过链接或重解析路径')


def _read(path):
    _safe_components(path)
    if not os.path.lexists(path):
        return None
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 16384:
        raise ValueError('旧计划退役证据不是独立普通文件')
    with path.open('rb') as handle:
        opened = os.fstat(handle.fileno())
        raw = handle.read(16385)
        after = path.lstat()
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                    info.st_mode, info.st_nlink, getattr(info, 'st_file_attributes', 0))
        if (identity(before) != identity(opened) or identity(before) != identity(after)
                or len(raw) > 16384):
            raise ValueError('读取旧计划退役证据期间文件已变化')
    return TVPlanRetirementWitness(raw.decode('utf-8'))


def _publish(plan_root, witness):
    if type(witness) is not TVPlanRetirementWitness:
        raise ValueError('缺少类型化的旧计划退役证据')
    value = witness.to_dict()
    path = _path(plan_root, value['task_id'], value['plan_fingerprint'])
    existing = _read(path)
    if existing is not None:
        if existing != witness:
            raise ValueError('旧计划退役证据已变化，不能重新签发或覆盖')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_components(path)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('xb') as handle:
            handle.write(witness.payload_json.encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        # Publish the complete file without replacing any existing target.
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _read(path) != witness:
                raise ValueError('旧计划退役发布遇到不同证据，已保留')
    finally:
        temporary.unlink(missing_ok=True)


def require_not_retired_plan(*, task_id, plan_root, task_root, plan_fingerprint):
    """Shared approval/execution static gate, with no media or Provider I/O."""
    from .recognition_reset import first_revision, require_plan_current
    if first_revision(Path(plan_root).parent, task_id):
        require_plan_current(Path(plan_root).parent, PlanStore(plan_root).load_version(task_id, plan_fingerprint))
    try:
        witness = _read(_path(plan_root, task_id, plan_fingerprint))
        if witness is None:
            return
        value = witness.to_dict()
        if (value['task_id'] != task_id or value['plan_fingerprint'] != plan_fingerprint
                or value['roots_fingerprint'] != _roots_fingerprint(plan_root, task_root)):
            raise ValueError('旧计划退役证据与当前任务目录不一致')
        raise ValueError('此旧计划已退役，仅供查看；请重试生成新计划后再批准')
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('旧计划退役证据无法核对，请保留原记录并核查') from exc


def is_current_tv(plan, state):
    current = (state is not None and state.content_policy == ContentPolicy.SAFE_WATCHABLE
            and state.content_selection is not None and bool(state.confirmed_work)
            and state.layout_mode == 'tv_episodes')
    if current and 'tv_completion_resolution' not in plan.metadata:
        raise ValueError('当前TV计划缺少原完成资料，不能退回旧重规划路径')
    return current


def retire_failed_current_tv(manager, task_id, expected_fingerprint):
    """Own all real locks; repeated calls consume the same unchanged witness."""
    from .recognition_queue_admission import _authority_locks, _available, _roots
    from .execution_recovery import requires_execution_replan
    from .plan_review import PlanReviewService
    from .content_selection import validate_plan_content
    from ..task_queue import utc_now
    try:
        _roots(manager)
        execution = manager.execution_service
        if (execution.plan_store.root.resolve() != manager.plan_store.root.resolve()
                or execution.task_root.resolve() != manager.task_root.resolve()
                or execution.decision_state_store.root.resolve() != manager.decision_state_store.root.resolve()
                or execution.review_service.review_root.resolve() != manager.review_root.resolve()):
            raise ValueError('执行与重规划服务目录不一致')
        with _authority_locks(manager, task_id):
            _available(manager, task_id)
            plan = manager.plan_store.load(task_id)
            state = manager.decision_state_store.load(task_id)
            if not is_current_tv(plan, state):
                return False  # Movie and legacy producers are outside this command.
            validate_plan_projection_receipt(plan)
            if (PlanStore.fingerprint(plan) != expected_fingerprint
                    or plan.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(state)):
                raise ValueError('旧计划或作品决定已变化，不能退役后来批准')
            validate_plan_content(plan, state)
            def retire(task):
                if (not requires_execution_replan(task)
                        or task.get('plan_fingerprint') != expected_fingerprint
                        or task.get('execution_requested_fingerprint') != expected_fingerprint):
                    raise ValueError('旧执行失败状态已变化，请刷新后核查')
                ledger = safe_task_path(execution.record_root, task_id, suffix='.ledger.json')
                if os.path.lexists(ledger):
                    raise ValueError('存在执行记录，不能作为零写入计划退役')
                scope = TaskSourceScope.from_mapping(task.get('task_source_scope') or {})
                batch_id = str(task.get('batch_id') or '')
                manager.batch_scope_store.require_active_scope(scope, batch_id=batch_id)
                if scope.task_id != task_id or scope.media_domain != 'tv' or scope.content_policy != state.content_policy:
                    raise ValueError('退役来源范围与当前TV决定不一致')
                witness = TVPlanRetirementWitness(_encode(dict(policy='current-tv-plan-retirement-v1',
                    task_id=task_id, plan_fingerprint=expected_fingerprint,
                    decision_fingerprint=DecisionStateStore.fingerprint(state),
                    task_preimage_sha256=_hash(safe_task_path(manager.task_root, task_id).read_bytes()),
                    roots_fingerprint=_roots_fingerprint(manager.plan_store.root, manager.task_root),
                    batch_id=batch_id, scope_fingerprint=_hash(_encode(scope.to_dict()).encode('utf-8')),
                    reservation_lineage_id=scope.reservation_lineage_id,
                    failure_code=task.get('execution_failure_code') or 'legacy_target_conflict_requires_replan',
                    execution_requested_fingerprint=expected_fingerprint)))
                _publish(manager.plan_store.root, witness)
                # Once the witness exists, failure here cannot revive approval.
                PlanReviewService(manager.plan_store, manager.review_root).delete(task_id)
                history = list(task.get('execution_replan_history') or [])
                history.append(dict(fingerprint=expected_fingerprint, error=task.get('error'),
                                    code=task.get('execution_failure_code'), retired_at=utc_now()))
                task.update(workflow_status='need_confirm', queue_operation='rename',
                    execution_status=None, execution_phase=None, execution_requested_fingerprint=None,
                    execution_fingerprint=None, execution_failure_code=None, execution_ledger=None,
                    execution_replan_history=history, error=None)
                return task
            if mutate_task_at(manager.task_root, task_id, retire) is None:
                raise ValueError('原任务已不存在')
        return True
    except PlanExecutionError:
        raise
    except (ValueError, RuntimeError, OSError) as exc:
        raise PlanExecutionError(f'无法安全退役旧计划：{exc}') from exc
