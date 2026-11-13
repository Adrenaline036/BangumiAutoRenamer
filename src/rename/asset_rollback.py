"""Operator-selected inverse of one approved whole replacement.

Only the selected Catalog slot is restored. Every step is journaled and may be
replayed after interruption; unrelated later seasons/versions are never rewound.
"""
from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .asset_operation import AssetOperation, plain
from .asset_replacement import approved_batch, safe_absolute
from .completed_history import read_bytes
from .plan_store import PlanStore
from .trans import Trans


def _hash(value):
    return hashlib.sha256(AssetOperation.encode(value)).hexdigest()


@dataclass(frozen=True)
class AssetRollbackPreparation:
    payload: bytes

    def __post_init__(self):
        value = json.loads(self.payload)
        if (type(self.payload) is not bytes or self.payload != AssetOperation.encode(value)
                or set(value) != {'policy', 'task_id', 'plan_fingerprint', 'operation_fingerprint',
                                  'catalog_expected', 'catalog_before', 'shared_dependencies'}
                or value['policy'] != 'asset-rollback-v1'):
            raise ValueError('回滚预检不是当前冻结结构')

    def to_dict(self): return json.loads(self.payload)

    @property
    def fingerprint(self): return hashlib.sha256(self.payload).hexdigest()


def _catalog(execution, plan):
    from .catalog_binding import binding_from_plan
    operation = AssetOperation.from_mapping(plan.metadata['asset_operation']).to_dict()
    binding = binding_from_plan(plan)
    if binding is None:
        raise ValueError('资产操作缺少当前作品登记绑定')
    current_binding = {'backend': binding.backend, 'key': binding.key,
                       'root': str(plan.metadata['canonical_work_root'])}
    before = operation.get('catalog_before', current_binding)
    if any(before.get(key) != value for key, value in current_binding.items()):
        raise ValueError('原资产范围与当前作品登记绑定不一致')
    catalog = execution.series_catalog if before['backend'] == 'series' else execution.work_catalog
    path = catalog._path(before['key'])
    record = json.loads(read_bytes(path))
    if record.get('canonical_root') != before['root']:
        raise ValueError('回滚作品根已变化，不能跨根恢复')
    if before['backend'] == 'series':
        catalog._match(record)
        season = plan.metadata['season_number']
        selected = {'bindings': [r for r in record['season_bindings'] if r['season_number'] == season]}
    else:
        catalog._validate(record, before['key'])
        ref = operation['slot_key'].split(':version:', 1)[1]
        selected = {'versions': [r for r in record['versions'] if r.get('version_ref') == ref], 'assets': []}
        for row in record['assets']:
            associations = []
            for association in row.get('associations', ()):
                if row.get('scope') == 'shared':
                    if association.get('task_id') == plan.task_id:
                        associations.append(association)
                    continue
                owner = association.get('owner_version_ref')
                if owner is None:
                    from .asset_actions import _entry_version
                    from .projection_authority import validate_plan_projection_receipt
                    owner_plan = execution.plan_store.load(association['task_id'])
                    validate_plan_projection_receipt(owner_plan)
                    entries = [e for e in owner_plan.entries if str(e.source_path) == association['source']]
                    if len(entries) != 1:
                        raise ValueError('电影伴随资产缺少唯一冻结归属')
                    owner = _entry_version(owner_plan, entries[0])
                if owner == ref:
                    associations.append(association)
            if associations:
                selected['assets'].append({**row, 'associations': associations})
    return catalog, record, selected




def _journal_path(execution, batch):
    return safe_absolute(execution.record_root.absolute() / 'asset-rollbacks' / f'{batch.transaction_key}.json')


def _read_journal(path):
    if not path.exists(): return None
    value = json.loads(read_bytes(path))
    if (set(value) != {'payload', 'sha256'} or _hash(value['payload']) != value['sha256']
            or set(value['payload']) != {'preparation', 'phase'}
            or value['payload']['phase'] not in {'rollback_prepared', 'media_restored',
                'catalog_restored', 'rolled_back'}):
        raise ValueError('回滚进度凭据损坏，不能猜测恢复')
    return value['payload']


def _write_journal(path, preparation, phase):
    payload = {'preparation': preparation.to_dict(), 'phase': phase}
    Trans._atomic_json_write(path, {'payload': payload, 'sha256': _hash(payload)})


def _shared_dependencies(execution, plan, record):
    if plan.metadata['asset_operation']['catalog_before']['backend'] != 'work':
        return []
    from dataclasses import asdict
    from .asset_replacement import observe_file
    created = {str(row['target']) for row in execution._build_execution_operations(plan) if row['action']=='create'}
    result=[]
    for row in record['assets']:
        if row.get('scope')!='shared' or row['target'] not in created:
            continue
        others=[a for a in row['associations'] if a['task_id']!=plan.task_id]
        if others:
            result.append(dict(target=row['target'], associations=others, evidence=asdict(observe_file(row['target']))))
    return sorted(result,key=lambda row:row['target'])


def _transfer(execution, plan, batch, prepared):
    from .asset_replacement import ApprovedRollbackPreservation, _fingerprint
    protection=ApprovedRollbackPreservation(plan.task_id,PlanStore.fingerprint(plan),prepared.fingerprint,
        tuple((row['target'],_fingerprint(row['evidence'])) for row in prepared.to_dict()['shared_dependencies']))
    return Trans(plan.as_mapping(), plan.task_id, PlanStore.fingerprint(plan), mode='hardlink',
        dry_run=False, record_root=execution.record_root,
        operations=execution._build_execution_operations(plan), replacement_authority=batch,
        rollback_preservation=protection)


def prepare(execution, task_id, fingerprint):
    plan = execution.validate_approved_authority(task_id, fingerprint)
    if plan.metadata.get('generated_outputs') is not None:
        raise ValueError('本计划含NFO；自动回滚不能确认回收站安全，请先核对生成记录并人工处理')
    from .replacement_completion import require_inverse_allowed
    require_inverse_allowed(plan.metadata.get('asset_operation'))
    operation = AssetOperation.from_mapping(plan.metadata['asset_operation'])
    batch = approved_batch(plan, fingerprint)
    journal = _read_journal(_journal_path(execution, batch))
    catalog, record, current = _catalog(execution, plan)
    before = operation.to_dict()['catalog_before']['rows']
    if journal is not None:
        prepared = AssetRollbackPreparation(AssetOperation.encode(journal['preparation']))
        value = prepared.to_dict()
        if (value['task_id'], value['plan_fingerprint'], value['operation_fingerprint']) != (
                task_id, fingerprint, operation.fingerprint):
            raise ValueError('回滚进度不属于当前批准计划')
        if value['catalog_before'] != before:
            raise ValueError('回滚登记前态与原批准操作不一致')
        if value['shared_dependencies'] != _shared_dependencies(execution, plan, record):
            raise ValueError('回滚共享附件的活跃归属或内容已变化，请重新核查')
        expected_rows = value['catalog_expected'].get('bindings', value['catalog_expected'].get('versions', ()))
        if value['catalog_expected'] != before and (not expected_rows or any(
                row.get('task_id') != task_id for row in expected_rows)):
            raise ValueError('回滚登记后态不属于原批准任务')
        if current not in (value['catalog_expected'], before):
            raise ValueError('所选季度／版本已进入后续代际，不能回滚旧操作')
    else:
        rows = current.get('bindings', current.get('versions', ()))
        if current != before and (not rows or any(row.get('task_id') != task_id for row in rows)):
            raise ValueError('所选季度／版本已由后续任务修改，不能回滚旧操作')
        prepared = AssetRollbackPreparation(AssetOperation.encode(dict(policy='asset-rollback-v1',
            task_id=task_id, plan_fingerprint=fingerprint, operation_fingerprint=operation.fingerprint,
            catalog_expected=current, catalog_before=before,
            shared_dependencies=_shared_dependencies(execution,plan,record))))
    transfer = _transfer(execution, plan, batch, prepared)
    operations = transfer._replacement_operations(batch)
    media_journal = transfer._read_replacement_journal(batch, operations)
    transfer._validate_replacement_state(batch, operations, media_journal, rollback=True)
    return prepared


def _restored_catalog_candidate(execution, plan, prepared):
    catalog, record, current = _catalog(execution, plan)
    value = prepared.to_dict()
    before = value['catalog_before']
    if current == before:
        return catalog, record, False
    if current != value['catalog_expected']:
        raise ValueError('回滚登记槽位已变化，拒绝覆盖后续任务')
    operation = plan.metadata['asset_operation']
    authority = operation['catalog_before']
    if authority['backend'] == 'series':
        from .season_identity import normalize_bindings
        season = plan.metadata['season_number']
        record['season_bindings'] = normalize_bindings(
            [r for r in record['season_bindings'] if r['season_number'] != season] + before['bindings'])
    else:
        ref = operation['slot_key'].split(':version:', 1)[1]
        record['versions'] = [r for r in record['versions'] if r.get('version_ref') != ref] + before['versions']
        assets = []
        selected_associations = {(row['target'], a['task_id'], a['source'])
            for row in current['assets'] for a in row['associations']}
        for row in record['assets']:
            remaining = [a for a in row.get('associations', ())
                         if (row['target'], a['task_id'], a['source']) not in selected_associations]
            if remaining:
                assets.append({**row, 'associations': remaining})
        for old in before['assets']:
            match = next((a for a in assets if a['target'] == old['target']), None)
            if match is not None:
                if {k: v for k, v in match.items() if k != 'associations'} != {k: v for k, v in old.items() if k != 'associations'}:
                    raise ValueError('回滚附件与其他版本共用目标冲突')
                match['associations'] = match['associations'] + old['associations']
            else:
                assets.append(old)
        record['assets'] = assets
    record['revision'] = int(record['revision']) + 1
    record.setdefault('asset_rollback_receipts', {})[prepared.fingerprint] = {
        'task_id': plan.task_id, 'plan_fingerprint': PlanStore.fingerprint(plan)}
    return catalog, record, True


def _restore_catalog(execution, plan, prepared):
    catalog, record, changed = _restored_catalog_candidate(execution, plan, prepared)
    authority = plan.metadata['asset_operation']['catalog_before']
    if not changed:
        catalog._require_namespace_candidate(authority['key'], record)
        return
    if authority['backend'] == 'series':
        catalog._match(record)
        catalog._atomic_write(authority['key'], record)
    else:
        catalog.write_locked(authority['key'], record)


def rollback(execution, task_id, fingerprint, *, expected_recovery_fingerprint):
    from .plan_execution import PlanExecutionError, PlanExecutionResult, _utc_now
    with execution._execution_lock(task_id, fingerprint), ExitStack() as locks:
        plan = execution.validate_approved_authority(task_id, fingerprint)
        if plan.metadata.get('generated_outputs') is not None:
            raise ValueError('本计划含NFO；自动回滚不能确认回收站安全，请先核对生成记录并人工处理')
        operation = plan.metadata['asset_operation']
        authority = operation['catalog_before']
        catalog = execution.series_catalog if authority['backend'] == 'series' else execution.work_catalog
        locks.enter_context(catalog.lock(authority['key']))
        execution._validate_runtime_safety_gate(plan)
        if execution._read_task(task_id).get('preview_only'):
            raise PlanExecutionError('测试预览任务禁止真实回滚')
        prepared = prepare(execution, task_id, fingerprint)
        if prepared.fingerprint != expected_recovery_fingerprint:
            raise PlanExecutionError('回滚范围已变化，请重新核查后确认')
        _, candidate, _ = _restored_catalog_candidate(execution, plan, prepared)
        catalog._require_namespace_candidate(authority['key'], candidate)
        batch = approved_batch(plan, fingerprint)
        journal_path = _journal_path(execution, batch)
        saved = _read_journal(journal_path)
        if saved is not None and saved['phase'] == 'rolled_back':
            return PlanExecutionResult(task_id, fingerprint, execution.record_root / f'{task_id}.ledger.json', 'rolled_back')
        if saved is None:
            _write_journal(journal_path, prepared, 'rollback_prepared')
        execution._update_task(task_id, workflow_status='executing', execution_phase='rollback_prepared',
            asset_rollback_phase='rollback_prepared', error=None, completed_at=None)
        try:
            _transfer(execution, plan, batch, prepared).recover_replacements(rollback=True)
            _write_journal(journal_path, prepared, 'media_restored')
            _restore_catalog(execution, plan, prepared)
            _write_journal(journal_path, prepared, 'catalog_restored')
            execution._update_task(task_id, workflow_status='rolled_back', execution_status='rolled_back',
                execution_phase='rolled_back', asset_rollback_phase='rolled_back',
                error=None, completed_at=_utc_now(),
                execution_requires_manual_check=False, execution_may_have_written_targets=False)
            _write_journal(journal_path, prepared, 'rolled_back')
            return PlanExecutionResult(task_id, fingerprint, execution.record_root / f'{task_id}.ledger.json', 'rolled_back')
        except Exception as exc:
            execution._update_task(task_id, workflow_status='failed', execution_status='failed',
                asset_rollback_phase='incomplete', error=f'回滚尚未完成：{exc}',
                execution_requires_manual_check=True, execution_may_have_written_targets=True)
            raise PlanExecutionError(f'回滚尚未完成：{exc}') from exc
