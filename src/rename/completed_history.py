"""Read-only completion identity proof, distinct from materialization readiness."""
import hashlib
import json
from pathlib import Path

from .plan_store import PlanStore
from .path_safety import first_reparse_component


def read_bytes(path):
    path = Path(path)
    if first_reparse_component(path) is not None or not path.is_file():
        raise ValueError('历史证据缺失或路径不是可信普通文件')
    return path.read_bytes()


def ledger_for(plan_store, record_root, task_id, fingerprint, *, require_archive=False):
    plan = plan_store.load_version(task_id, fingerprint)
    root = Path(record_root)
    current = root / f'{task_id}.ledger.json'
    archived = root / 'history' / task_id / f'{fingerprint}.ledger.json'
    if require_archive and not archived.is_file():
        raise ValueError('冻结的原执行归档缺失，禁止重建执行或恢复')
    path = archived if archived.exists() else current
    raw = read_bytes(path)
    ledger = json.loads(raw)
    if ledger.get('plan_fingerprint') != fingerprint:
        raise ValueError('历史执行记录版本与计划不一致')
    return plan, ledger, raw




def validate_recorded_media_source(source, row):
    """Shared source proof; superseded targets need not still belong to it."""
    from .asset_collision import requires_manual_collision
    if first_reparse_component(source) or not source.is_file():
        raise ValueError('历史媒体来源不是可信普通文件')
    st = source.stat()
    if (row.get('source_device'), row.get('source_inode')) != (st.st_dev, st.st_ino):
        raise ValueError('原完成记录的源文件物理身份已变化')
    if row.get('evidence_kind') == 'content-xxh3-128-v1':
        from .small_file_identity import small_fingerprint
        actual=small_fingerprint(source)
        if actual.xxh3_128 != row.get('source_xxh3_128') or actual.size != row.get('source_size'):
            raise ValueError('历史来源内容已变化')
    return st


def inspect_completed_media(plan, ledger):
    """Missing targets are observations only; existing targets remain strict."""
    from .projection_authority import validate_plan_projection_receipt
    from .plan_execution import PlanExecutionService
    from .asset_collision import requires_manual_collision
    validate_plan_projection_receipt(plan)
    PlanExecutionService._validate_frozen_source_identities(plan)
    if (plan.execution_policy is None or plan.execution_policy.mode != 'hardlink'
        or not plan.execution_policy.preserve_source or plan.execution_policy.dry_run
        or ledger.get('task_id') != plan.task_id or ledger.get('status') != 'completed'
        or ledger.get('plan_fingerprint') != PlanStore.fingerprint(plan)
        or ledger.get('mode') != 'hardlink' or ledger.get('dry_run') is not False):
        raise ValueError('缺少与原计划匹配的真实硬链接完成记录')
    rows = ledger.get('entries')
    if not isinstance(rows, list) or len(rows) != len(plan.entries):
        raise ValueError('完成记录条目数量不一致')
    indexed = {(r.get('source'), r.get('target')): r for r in rows if isinstance(r, dict)}
    if len(indexed) != len(rows):
        raise ValueError('完成记录包含重复或无效条目')
    missing, existing = [], []
    for entry in plan.entries:
        source, target = entry.source_path, entry.target_path
        if first_reparse_component(source) or first_reparse_component(target):
            raise ValueError('重建来源或目标包含重解析路径')
        row = indexed.get((str(source), str(target)))
        if row is None:
            raise ValueError('完成记录的来源或目标与计划不一致')
        st = validate_recorded_media_source(source, row)
        reused = row.get('status') == 'reused_identical_asset' and row.get('transfer_type') == 'reuse_existing' and row.get('action') == 'reuse_existing'
        replacing = False
        if row.get('action') == 'replace':
            from .asset_operation import AssetOperation
            operation = AssetOperation.from_mapping(plan.metadata.get('asset_operation')).to_dict()
            replacing = (ledger.get('schema_version') == 3 and operation['mode'] == 'replace'
                and any(item['source'] == str(source) and item['target'] == str(target)
                        for item in operation['replaces']))
        created = row.get('status') == 'created' and row.get('transfer_type') == 'hardlink' and (row.get('action') == 'create' or replacing)
        if not (created or reused):
            raise ValueError('原执行条目不是可信硬链接或批准复用')
        if created and (row.get('target_device'), row.get('target_inode')) != (st.st_dev, st.st_ino):
            raise ValueError('原硬链接物理证据矛盾')
        if not target.exists():
            missing.append(str(target)); continue
        if not target.is_file():
            raise ValueError('目标被非文件占用')
        ts = target.stat()
        if reused:
            if requires_manual_collision(source):
                if ((ts.st_dev, ts.st_ino) != (st.st_dev, st.st_ino) or ts.st_nlink < 2
                        or (ts.st_dev, ts.st_ino) != (row.get('target_device'), row.get('target_inode'))):
                    raise ValueError('登记媒体目标不再是当前来源的原硬链接')
            elif row.get('evidence_kind') == 'content-xxh3-128-v1':
                from .asset_collision import validate_reuse_resolution
                if entry.collision_resolution is None:
                    raise ValueError('小文件复用缺少原批准决定')
                from .media_file_identity import MediaFileIdentity
                if row.get('source_xxh3_128') != entry.collision_resolution.source_xxh3_128:
                    raise ValueError('来源完成记录与批准内容不同')
                validate_reuse_resolution(source, target, entry.collision_resolution,
                    source_identity=MediaFileIdentity(st.st_size, st.st_dev, st.st_ino))
                if ((ts.st_dev,ts.st_ino) != (row.get('target_device'),row.get('target_inode'))
                        or row.get('target_xxh3_128') != entry.collision_resolution.target_xxh3_128):
                    raise ValueError('登记小文件复用目标已变化')
            else:
                raise ValueError('登记复用目标缺少当前内容或物理身份凭据')
        elif (ts.st_dev, ts.st_ino) != (st.st_dev, st.st_ino) or ts.st_nlink < 2:
            raise ValueError('目标不再是原来源的硬链接')
        existing.append(str(target))
    return tuple(missing), tuple(existing)


def archive_exact(record_root, task_id, fingerprint, ledger_bytes, mapping_sha256):
    root = Path(record_root)
    for source, target, expected in (
        (root / f'{task_id}.ledger.json', root/'history'/task_id/f'{fingerprint}.ledger.json', ledger_bytes),
        (root / f'{task_id}.json', root/'history'/task_id/f'{fingerprint}.mapping.json', None),
    ):
        data = read_bytes(source)
        if source.suffix == '.json' and source.name == f'{task_id}.json' and hashlib.sha256(data).hexdigest() != mapping_sha256:
            raise ValueError('归档前原映射记录已变化')
        if expected is not None and data != expected:
            raise ValueError('归档前完成记录已变化')
        if target.exists():
            if read_bytes(target) != data:
                raise ValueError('不可变执行历史与当前记录冲突')
            continue
        if first_reparse_component(target) is not None:
            raise ValueError('执行历史归档路径不安全')
        target.parent.mkdir(parents=True, exist_ok=True)
        # Publish only complete fsynced bytes. An interrupted temporary file
        # cannot masquerade as an immutable execution-history record.
        import os
        import uuid
        temporary = target.with_name(f'.{target.name}.{uuid.uuid4().hex}.tmp')
        try:
            with temporary.open('xb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            try:
                os.link(temporary, target)  # exclusive, never replaces history
            except FileExistsError:
                if read_bytes(target) != data:
                    raise ValueError('不可变执行历史与当前记录冲突')
        finally:
            temporary.unlink(missing_ok=True)
