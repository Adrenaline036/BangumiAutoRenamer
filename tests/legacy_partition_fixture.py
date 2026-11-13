"""Historical bytes only: never call the retired production journal writer."""
import base64
import json


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def _row(path):
    return dict(path=str(path.resolve()), existed=path.is_file(),
        content_b64=base64.b64encode(path.read_bytes()).decode('ascii') if path.is_file() else None)


def seed_history(store, decision, paths):
    rows = [_row(path) for path in paths]
    common = dict(partition_id=decision.partition_id, decision_fingerprint=decision.fingerprint)
    _write(store.decision_path(decision.partition_id), decision.to_dict())
    _write(store.backup_root/decision.partition_id/'base.json',
        dict(policy='partition-backup-set-v1', backups=rows, **common))
    _write(store.transaction_path(decision.partition_id), dict(schema_version=1,
        parent_task_id=decision.parent_task_id, child_task_id=decision.child_task_id,
        status='pending', phase='prepared', backups=rows, created_at='2026-09-01T00:00:00Z', **common))


def append_history(store, partition_id, path):
    journal = store.transaction_path(partition_id)
    payload = json.loads(journal.read_text('utf-8'))
    rows = payload['backups']
    # Match the documented historical serialization, not a production writer.
    from src.rename.source_partition import _canonical_fingerprint
    parent = _canonical_fingerprint(rows, domain='bar.partition-backup-set')
    added = _row(path)
    _write(store.backup_root/partition_id/'extensions'/f'{parent}.json',
        dict(policy='partition-backup-extension-v1', partition_id=partition_id,
            decision_fingerprint=payload['decision_fingerprint'], parent_fingerprint=parent, backup=added))
    rows.append(added)
    _write(journal, payload)
