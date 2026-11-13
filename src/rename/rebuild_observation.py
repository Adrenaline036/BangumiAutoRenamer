"""Immutable, versioned observations for an explicit completed-task rebuild."""
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Mapping


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class CompletedRebuildObservation:
    payload_json: str

    def __post_init__(self):
        value = json.loads(self.payload_json)
        required = {'schema_version', 'policy_version', 'task_id', 'origin_plan_fingerprint',
                    'parent_plan_fingerprint', 'ledger_sha256', 'mapping_sha256', 'missing_targets', 'reuse_targets'}
        if (not required <= set(value) or set(value)-required not in ({'work_proof'}, {'series_proof'})
            or any(re.fullmatch('[0-9a-f]{64}', str(value.get(k) or '')) is None
                   for k in ('origin_plan_fingerprint', 'parent_plan_fingerprint', 'ledger_sha256', 'mapping_sha256'))):
            raise ValueError('重建观察字段或历史指纹无效')
        if (value.get('schema_version') != 1 or value.get('policy_version') != 'completed-rebuild-v2'
            or not value.get('task_id') or not value.get('origin_plan_fingerprint')
            or not value.get('parent_plan_fingerprint') or not value.get('ledger_sha256')):
            raise ValueError('重建观察缺少可信历史身份')
        if self.payload_json != self._encode(value):
            raise ValueError('重建观察不是规范冻结格式')
        from pathlib import Path
        missing, reused = value['missing_targets'], value['reuse_targets']
        if (not isinstance(missing, list) or not isinstance(reused, list)
            or any(not isinstance(p, str) or not Path(p).is_absolute() for p in missing + reused)
            or len(set(missing + reused)) != len(missing + reused)):
            raise ValueError('重建目标集合重复或路径无效')

    @staticmethod
    def _encode(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping):
            raise ValueError('重建观察格式无效')
        # Convert through JSON to reject non-serializable mutable authority.
        return cls(cls._encode(dict(value)))

    def to_dict(self):
        return json.loads(self.payload_json)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.payload_json.encode('utf-8')).hexdigest()
