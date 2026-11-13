"""Explicit new planning generation after a proven completed rollback.

The old execution, rollback journal and ledger remain evidence, never a new
approval. This boundary writes only review state and immutable audit records.
"""
from dataclasses import dataclass, replace
import base64
import hashlib
import json
import os
import uuid

from .asset_operation import AssetOperation
from .asset_replacement import approved_batch
from . import asset_rollback
from .completed_history import read_bytes
from .decision_state import DecisionCommandService, DecisionStateStore
from .path_safety import first_reparse_component
from .plan_store import PlanStore
from .projection_authority import TaskTerminalMutation, validate_plan_projection_receipt
from ..utils.utils import safe_task_path


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _optional(path):
    # Broken links/reparse paths are not equivalent to absent evidence.
    if first_reparse_component(path) is not None:
        raise ValueError('审计证据路径包含重解析点')
    return read_bytes(path) if os.path.lexists(path) else None


def current_evidence(execution, task_id):
    return {
        'task': read_bytes(safe_task_path(execution.task_root, task_id)),
        'plan': _optional(execution.plan_store.path_for(task_id)),
        'decision': _optional(execution.decision_state_store.path_for(task_id)),
        'approval': _optional(safe_task_path(execution.review_service.review_root, task_id)),
        'ledger': _optional(safe_task_path(execution.record_root, task_id, suffix='.ledger.json')),
        'mapping': _optional(safe_task_path(execution.record_root, task_id)),
    }


def current_restart_evidence(execution, task_id):
    """Read the complete restart audit through the original task identity."""
    files = current_evidence(execution, task_id)
    task = json.loads(files['task'])
    fingerprints = {str(task.get(key)) for key in
        ('execution_fingerprint', 'execution_requested_fingerprint') if task.get(key)}
    if len(fingerprints) != 1:
        raise ValueError('原回滚操作身份缺失或不一致，不能读取审计依据')
    original_fp = fingerprints.pop()
    original = execution.plan_store.load_version(task_id, original_fp)
    batch = approved_batch(original, original_fp)
    if batch is None:
        raise ValueError('原回滚缺少完整事务身份')
    _catalog, record, _slot = asset_rollback._catalog(execution, original)
    files.update(original_plan=read_bytes(execution.plan_store._history_path(task_id, original_fp)),
        rollback_journal=read_bytes(asset_rollback._journal_path(execution, batch)),
        media_journal=read_bytes(batch.transaction_directory / 'journal.json'), catalog=_encode(record))
    return files


def audit_payload(task_id, action, files):
    """One immutable audit representation for preparation and publication."""
    return _encode({'policy': 'task-operation-audit-v1', 'task_id': task_id, 'action': action,
        'files': {key: None if raw is None else {'sha256': _digest(raw),
            'base64': base64.b64encode(raw).decode('ascii')} for key, raw in files.items()}})


def archive_evidence(execution, task_id, action, files):
    """Exclusive atomic publication; a crash cannot replace existing history."""
    payload = audit_payload(task_id, action, files)
    key = _digest(payload)
    directory = execution.record_root / 'operation-audit' / task_id
    destination = safe_task_path(directory, key)
    if first_reparse_component(destination) is not None:
        raise ValueError('审计归档路径不安全')
    if destination.exists():
        if read_bytes(destination) != payload:
            raise ValueError('不可变审计归档冲突')
        return key
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f'.{key}.{uuid.uuid4().hex}.tmp'
    try:
        with temporary.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if read_bytes(destination) != payload:
                raise ValueError('不可变审计归档冲突')
    finally:
        temporary.unlink(missing_ok=True)
    return key


@dataclass(frozen=True)
class RolledBackRestartPreparation:
    task_id: str
    current_fingerprint: str
    original_fingerprint: str
    decision_revision: int
    evidence_fingerprint: str


def _inspect(execution, task_id):
    files = current_restart_evidence(execution, task_id)
    task = json.loads(files['task'])
    if (task.get('asset_rollback_phase') != 'rolled_back'
            or task.get('execution_status') != 'rolled_back'
            or task.get('workflow_status') not in {'rolled_back', 'need_confirm', 'invalid', 'approved'}
            or task.get('execution_requires_manual_check')
            or task.get('execution_may_have_written_targets') or task.get('preview_only')):
        raise ValueError('只有完整回滚后的任务可以重新生成计划；请先完成原回滚')
    fingerprints = {str(task.get(k)) for k in ('execution_fingerprint', 'execution_requested_fingerprint')
                    if task.get(k)}
    if len(fingerprints) != 1:
        raise ValueError('原回滚操作身份缺失或不一致，不能猜测重新生成')
    original_fp = fingerprints.pop()
    original = execution.plan_store.load_version(task_id, original_fp)
    current = execution.plan_store.load(task_id)
    state = execution.decision_state_store.load(task_id)
    for plan in (original, current):
        validate_plan_projection_receipt(plan)
    if (current.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(state)
            or current.metadata.get('decision_state_revision') != state.revision):
        raise ValueError('当前计划与决定不一致，请先核查')
    operation = AssetOperation.from_mapping(original.metadata.get('asset_operation'))
    batch = approved_batch(original, original_fp)
    if batch is None:
        raise ValueError('原回滚缺少完整事务身份')
    journal_path = asset_rollback._journal_path(execution, batch)
    journal = asset_rollback._read_journal(journal_path)
    if journal is None or journal['phase'] != 'rolled_back':
        raise ValueError('原回滚日志尚未完成；任务标记不能代替回滚凭据')
    prepared = asset_rollback.AssetRollbackPreparation(AssetOperation.encode(journal['preparation']))
    value = prepared.to_dict()
    if (value['task_id'], value['plan_fingerprint'], value['operation_fingerprint']) != (
            task_id, original_fp, operation.fingerprint):
        raise ValueError('回滚日志与原计划或操作不一致')
    if value['catalog_before'] != operation.to_dict()['catalog_before']['rows']:
        raise ValueError('回滚前登记证据不一致')
    catalog, record, slot = asset_rollback._catalog(execution, original)
    if slot != value['catalog_before']:
        raise ValueError('所选季度或版本已进入后续代际，请重新核查')
    transfer = asset_rollback._transfer(execution, original, batch, prepared)
    operations = transfer._replacement_operations(batch)
    media_journal = transfer._read_replacement_journal(batch, operations)
    if media_journal is None or media_journal.get('status') != 'rolled_back':
        raise ValueError('媒体回滚日志未完成')
    transfer._validate_replacement_state(batch, operations, media_journal, rollback=True)
    ledger = json.loads(files['ledger'] or b'{}')
    if (ledger.get('task_id'), ledger.get('plan_fingerprint'), ledger.get('status')) != (
            task_id, original_fp, 'rolled_back'):
        raise ValueError('原回滚执行记录缺失或身份不一致')
    # A new plan needs current sources; safe abandon intentionally does not.
    execution._validate_frozen_source_identities(current)
    from .content_selection import validate_media_intent
    validate_media_intent(current, state, task_root=execution.task_root)
    signature = _digest(_encode({key: _digest(raw) if raw is not None else None for key, raw in files.items()}))
    return (RolledBackRestartPreparation(task_id, PlanStore.fingerprint(current), original_fp,
                state.revision, signature), current, state, original, files, catalog)


def prepare(execution, task_id):
    return _inspect(execution, task_id)[0]


def restart(execution, task_id, preparation):
    if type(preparation) is not RolledBackRestartPreparation or preparation.task_id != task_id:
        raise ValueError('重新生成必须使用本任务的类型化核查结果')
    with execution._execution_lock(task_id, preparation.current_fingerprint):
        checked, _plan, _state, original, _files, catalog = _inspect(execution, task_id)
        key = original.metadata['asset_operation']['catalog_before']['key']
        with catalog.lock(key):
            checked, current, state, _original, files, _catalog = _inspect(execution, task_id)
            if checked != preparation:
                raise ValueError('任务、计划或回滚证据已变化，请重新核查')
            selection = state.movie_version_selection
            metadata = dict(current.metadata)
            for field in ('asset_operation', 'completed_rebuild_observation',
                          'asset_replacement_retained', 'asset_replacement_retired', 'completed_rebuild_fingerprint'):
                metadata.pop(field, None)
            if selection is not None:
                from .movie_version_selection import rebind_selection
                selection = rebind_selection(selection, selection.to_dict()['work_key'])
                metadata.update(movie_version_selection=selection.to_dict(),
                                movie_version_selection_fingerprint=selection.fingerprint)
            # Collision permissions were granted to the old operation only.
            draft = replace(current, entries=tuple(replace(e, collision_resolution=None) for e in current.entries),
                metadata=metadata, projection_receipt=None,
                revision_parent_fingerprint=checked.current_fingerprint,
                revision_action='restart_after_rollback', revision_reason='完整回滚后创建新未批准计划')
            from .plan_review import _resolve_asset_collisions
            from pathlib import Path
            entries, collisions = _resolve_asset_collisions(draft.entries,
                Path(draft.metadata['canonical_work_root']), package_label=draft.source_root.name)
            draft = replace(draft, entries=tuple(entries), metadata={**draft.metadata,
                'asset_collision_unresolved': tuple(collisions)})

            def terminal(_decision, projected, task):
                from .plan_verifier import PlanVerifier
                result = PlanVerifier().verify(projected)
                clean = {k: v for k, v in task.items() if not k.startswith(('execution_', 'asset_rollback_'))}
                clean.update(workflow_status='need_confirm' if result.status.value == 'accepted' else result.status.value,
                    plan_status=result.status.value, queue_operation=None, queue_position=None, approved_at=None,
                    completed_at=None, started_at=None, error=None, execution_status=None,
                    execution_requires_manual_check=False, execution_may_have_written_targets=False,
                    last_operation_audit=audit_key, plan_entry_count=len(projected.entries))
                return clean

            changes = {'asset_operation': None, 'completed_rebuild_observation': None,
                       'execution_failure': None, 'movie_version_selection': selection}
            from .content_selection import validate_media_intent
            validate_media_intent(current, state, task_root=execution.task_root)
            from .media_archive import MediaArchiveContext, MediaArchiveIntent
            archive_context = MediaArchiveContext.for_execution(execution)
            archive = MediaArchiveIntent.prepare(archive_context, current, state,
                action='restart_after_rollback', audit_files=files)
            audit_key = archive.audit_key
            _state, projected = DecisionCommandService(execution.decision_state_store, execution.plan_store,
                media_archive_context=archive_context).update(
                task_id, expected_revision=checked.decision_revision,
                changes=changes,
                media_archive=archive,
                planner=lambda _: draft, task_terminal=TaskTerminalMutation.prepare(execution.task_root, task_id, terminal))
            return execution.review_service._record_revision(projected)
