"""Typed, recoverable authority for moving sources between planning tasks."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping

from .file_lock import FileLockBusyError
from .task_batch_scope import SourceIdentity


_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SourcePartitionError(RuntimeError):
    """A cross-work source partition is stale, incomplete or unsafe."""


def _canonical_fingerprint(value: object, *, domain: str) -> str:
    payload = json.dumps(
        {"domain": domain, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _safe_id(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text or _SAFE_ID.fullmatch(text) is None:
        raise SourcePartitionError(f"{field} 不安全")
    return text


def _sha256(value: object, field: str) -> str:
    text = str(value or "").strip().casefold()
    if _SHA256.fullmatch(text) is None:
        raise SourcePartitionError(f"{field} 无效")
    return text




@dataclass(frozen=True)
class SourcePartitionDecision:
    """Immutable business decision; paths remain frozen observations."""

    partition_id: str
    parent_task_id: str
    child_task_id: str
    expected_parent_plan_fingerprint: str
    expected_parent_batch_id: str
    expected_parent_scope_fingerprint: str
    expected_parent_reservation_revision: int
    selected_source_identities: tuple[SourceIdentity, ...]
    reason: str
    action: str = "cross_work"
    policy_version: str = "source-partition-v1"
    created_at: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        _safe_id(self.partition_id, "partition_id")
        _safe_id(self.parent_task_id, "parent_task_id")
        _safe_id(self.child_task_id, "child_task_id")
        _safe_id(self.expected_parent_batch_id, "expected_parent_batch_id")
        _sha256(
            self.expected_parent_plan_fingerprint,
            "expected_parent_plan_fingerprint",
        )
        _sha256(
            self.expected_parent_scope_fingerprint,
            "expected_parent_scope_fingerprint",
        )
        if self.expected_parent_reservation_revision < 0:
            raise SourcePartitionError("expected reservation revision 无效")
        if self.action != "cross_work" or self.policy_version != "source-partition-v1":
            raise SourcePartitionError("source partition action/policy 无效")
        if self.schema_version != 1:
            raise SourcePartitionError("source partition schema 不受支持")
        if not self.selected_source_identities:
            raise SourcePartitionError("source partition 至少需要一个来源")
        if not str(self.reason or "").strip():
            raise SourcePartitionError("跨作品整理必须填写业务理由")

    @property
    def fingerprint(self) -> str:
        return _canonical_fingerprint(
            self.to_dict(), domain="bar.source-partition-decision"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "partition_id": self.partition_id,
            "parent_task_id": self.parent_task_id,
            "child_task_id": self.child_task_id,
            "expected_parent_plan_fingerprint": (
                self.expected_parent_plan_fingerprint
            ),
            "expected_parent_batch_id": self.expected_parent_batch_id,
            "expected_parent_scope_fingerprint": (
                self.expected_parent_scope_fingerprint
            ),
            "expected_parent_reservation_revision": (
                self.expected_parent_reservation_revision
            ),
            "selected_source_identities": [
                item.to_dict() for item in self.selected_source_identities
            ],
            "action": self.action,
            "reason": self.reason,
            "policy_version": self.policy_version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SourcePartitionDecision":
        rows = value.get("selected_source_identities", ())
        if not isinstance(rows, (list, tuple)):
            raise SourcePartitionError("selected source identities 无效")
        return cls(
            schema_version=int(value.get("schema_version", 0)),
            partition_id=str(value.get("partition_id") or ""),
            parent_task_id=str(value.get("parent_task_id") or ""),
            child_task_id=str(value.get("child_task_id") or ""),
            expected_parent_plan_fingerprint=str(
                value.get("expected_parent_plan_fingerprint") or ""
            ),
            expected_parent_batch_id=str(
                value.get("expected_parent_batch_id") or ""
            ),
            expected_parent_scope_fingerprint=str(
                value.get("expected_parent_scope_fingerprint") or ""
            ),
            expected_parent_reservation_revision=int(
                value.get("expected_parent_reservation_revision", -1)
            ),
            selected_source_identities=tuple(
                SourceIdentity.from_mapping(item)
                for item in rows
                if isinstance(item, Mapping)
            ),
            action=str(value.get("action") or ""),
            reason=str(value.get("reason") or ""),
            policy_version=str(value.get("policy_version") or ""),
            created_at=str(value.get("created_at") or ""),
        )




class SourcePartitionStore:
    """Immutable decisions plus rollback journals for multi-store transitions."""

    def __init__(self, root: Path, *, allowed_roots: Iterable[Path]) -> None:
        self.root = Path(root)
        self.decision_root = self.root / "decisions"
        self.transaction_root = self.root / "transactions"
        self.backup_root = self.root / 'backup_sets'
        self.allowed_roots = tuple(
            Path(item).resolve(strict=False) for item in allowed_roots
        )

    @contextmanager
    def task_lock(self, task_id: str) -> Iterator[None]:
        identifier = _safe_id(task_id, "task_id")
        from .file_lock import resource_guard
        try:
            with resource_guard(
                self._task_lock_resource(identifier),
                owner={"kind": "source_partition", "task_id": identifier},
            ):
                yield
        except FileLockBusyError as exc:
            raise SourcePartitionError("该任务正在变更source partition") from exc

    def _task_lock_resource(self, task_id):
        from .file_lock import LockResource
        identifier = _safe_id(task_id, 'task_id')
        return LockResource(self, self.root / 'locks' / f'{identifier}.lock', f'source_partition_task:{identifier}')



    def decision_path(self, partition_id: str) -> Path:
        return self.decision_root / f"{_safe_id(partition_id, 'partition_id')}.json"




    def transaction_path(self, partition_id: str) -> Path:
        return self.transaction_root / f"{_safe_id(partition_id, 'partition_id')}.json"

    def save_decision(self, decision: SourcePartitionDecision) -> None:
        """Retired: no alternate writer or automatic compatibility fallback."""
        raise SourcePartitionError('旧二分拆卡已停用，请使用“调整计划范围”；无法处理的旧任务请安全结束后重新添加。')

    def prepare(
        self,
        decision: SourcePartitionDecision,
        *,
        backup_paths: Iterable[Path],
        history_proof=None,
        source_observations=None,
        partition_batch_id=None,
        workspace_transfer=None,
    ) -> None:
        """Retired: no alternate writer or automatic compatibility fallback."""
        raise SourcePartitionError('旧二分拆卡已停用，请使用“调整计划范围”；无法处理的旧任务请安全结束后重新添加。')

    def checkpoint(self, partition_id: str, phase: str) -> None:
        """Retired: no alternate writer or automatic compatibility fallback."""
        raise SourcePartitionError('旧二分拆卡已停用，请使用“调整计划范围”；无法处理的旧任务请安全结束后重新添加。')

    def add_backup(self, partition_id: str, path: Path) -> None:
        """Retired: no alternate writer or automatic compatibility fallback."""
        raise SourcePartitionError('旧二分拆卡已停用，请使用“调整计划范围”；无法处理的旧任务请安全结束后重新添加。')

    def complete(self, partition_id: str) -> None:
        """Retired: no alternate writer or automatic compatibility fallback."""
        raise SourcePartitionError('旧二分拆卡已停用，请使用“调整计划范围”；无法处理的旧任务请安全结束后重新添加。')

    def rollback(self, partition_id: str) -> None:
        payload = self._load_transaction(partition_id)
        if payload.get("status") != "pending":
            return
        rows = self._proven_backup_set(payload)
        # V1 recorded only preimages. It cannot prove ownership of any changed
        # bytes. Validate the entire set, then close an unchanged transaction
        # without rewriting even identical files. V2 write-ahead publication is
        # a separate contract; do not infer postimages from current disk state.
        seen = set()
        for row in rows:
            if (not isinstance(row, Mapping) or set(row) != {'path', 'existed', 'content_b64'}
                    or type(row.get('existed')) is not bool):
                raise SourcePartitionError("source partition backup 无效")
            raw_path = row.get('path')
            if not isinstance(raw_path, str) or not raw_path:
                raise SourcePartitionError('source partition backup path 无效')
            target = Path(raw_path)
            from .path_safety import first_reparse_component
            if (not target.is_absolute() or '..' in target.parts
                    or first_reparse_component(target) is not None):
                raise SourcePartitionError('source partition backup path 不安全')
            self._require_allowed(target)
            key = os.path.normcase(str(target.resolve(strict=False))).casefold()
            if key in seen or target.suffix.casefold() != '.json' or target in self.allowed_roots:
                raise SourcePartitionError('source partition backup target 重复或无效')
            seen.add(key)
            if row['existed']:
                encoded = row.get("content_b64")
                if not isinstance(encoded, str):
                    raise SourcePartitionError("source partition backup content 无效")
                try:
                    original = base64.b64decode(encoded.encode('ascii'), validate=True)
                    unchanged = target.is_file() and target.read_bytes() == original
                except (OSError, ValueError, UnicodeError) as exc:
                    raise SourcePartitionError('source partition backup content 无法核对') from exc
            else:
                if row.get('content_b64') is not None:
                    raise SourcePartitionError('不存在的原目标不能携带恢复内容')
                unchanged = not target.exists()
            if not unchanged:
                raise SourcePartitionError('旧拆卡事务缺少写后证明，已保留全部现有文件，请先核对或恢复原事务')
        payload.update({"status": "failed_closed", "phase": "unchanged_legacy_closed"})
        self._atomic_json(self.transaction_path(partition_id), payload)


    @staticmethod
    def _read_backup_record(path):
        from .path_safety import first_reparse_component
        try:
            if first_reparse_component(path) is not None:
                raise SourcePartitionError('拆卡完整恢复清单路径不安全')
            value = json.loads(path.read_text('utf-8'))
            if not isinstance(value, dict):
                raise SourcePartitionError('拆卡完整恢复清单格式无效')
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise SourcePartitionError('缺少可验证的完整恢复清单，已保留拆卡保护，请先核对原事务') from exc

    def _proven_backup_set(self, payload):
        """Rebuild the immutable base and every append, independently of rows.

        An old journal without this authority is intentionally not repaired by
        copying its current list. Appends precede the mutable journal and any
        corresponding business write; an incomplete append stays fail-closed.
        """
        partition_id = _safe_id(payload.get('partition_id'), 'partition_id')
        directory = self.backup_root / partition_id
        base = self._read_backup_record(directory / 'base.json')
        common = {'partition_id': partition_id, 'decision_fingerprint': payload['decision_fingerprint']}
        if (set(base) != {'policy', *common, 'backups'} or base.get('policy') != 'partition-backup-set-v1'
                or any(base.get(k) != v for k, v in common.items()) or not isinstance(base.get('backups'), list)):
            raise SourcePartitionError('拆卡完整恢复清单与不可变决定不一致')
        rows = list(base['backups'])
        extension_root = directory / 'extensions'
        from .path_safety import first_reparse_component
        if first_reparse_component(extension_root) is not None:
            raise SourcePartitionError('拆卡追加恢复清单路径不安全')
        extensions = {}
        for path in extension_root.glob('*.json'):
            value = self._read_backup_record(path)
            parent = value.get('parent_fingerprint')
            if (len(extensions) >= 1024 or set(value) != {'policy', *common, 'parent_fingerprint', 'backup'}
                    or value.get('policy') != 'partition-backup-extension-v1'
                    or any(value.get(k) != v for k, v in common.items())
                    or not isinstance(parent, str) or _SHA256.fullmatch(parent) is None
                    or path.stem != parent or parent in extensions):
                raise SourcePartitionError('拆卡追加恢复清单断链或无效')
            extensions[parent] = value['backup']
        while extensions:
            parent = _canonical_fingerprint(rows, domain='bar.partition-backup-set')
            if parent not in extensions:
                raise SourcePartitionError('拆卡追加恢复清单缺失或分叉')
            rows.append(extensions.pop(parent))
        if rows != payload.get('backups'):
            raise SourcePartitionError('拆卡恢复清单被删减或改写，不能释放保护')
        return rows

    def recover_pending(self) -> Mapping[str, str]:
        outcomes: dict[str, str] = {}
        if not self.transaction_root.exists():
            return outcomes
        for path in sorted(self.transaction_root.glob("*.json")):
            payload = self._load_transaction(path.stem)
            if payload.get("status") != "pending":
                continue
            parent_task_id = _safe_id(payload.get("parent_task_id"), "parent_task_id")
            with self.task_lock(parent_task_id):
                self.rollback(path.stem)
            outcomes[path.stem] = "failed_closed"
        return outcomes

    def pending_for_task(self, task_id: str) -> bool:
        identifier = _safe_id(task_id, "task_id")
        if not self.transaction_root.exists():
            return False
        for path in self.transaction_root.glob("*.json"):
            payload = self._load_transaction(path.stem)
            if (
                payload.get("status") == "pending"
                and identifier in {
                    payload.get("parent_task_id"),
                    payload.get("child_task_id"),
                }
            ):
                return True
        return False

    def _load_transaction(self, partition_id: str) -> dict[str, object]:
        try:
            value = json.loads(self.transaction_path(partition_id).read_text("utf-8"))
            if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                    or value.get('schema_version') != 1 or value.get('partition_id') != partition_id
                    or value.get('status') not in {'pending', 'completed', 'failed_closed'}):
                raise SourcePartitionError('source partition transaction 格式无效')
            decision = SourcePartitionDecision.from_mapping(json.loads(
                self.decision_path(partition_id).read_text('utf-8')))
            if (decision.partition_id != partition_id or value.get('decision_fingerprint') != decision.fingerprint
                    or value.get('parent_task_id') != decision.parent_task_id
                    or value.get('child_task_id') != decision.child_task_id):
                raise SourcePartitionError('source partition transaction 与不可变决定不一致')
        except (OSError, ValueError, TypeError) as exc:
            raise SourcePartitionError("source partition transaction 无法读取") from exc
        return value

    def _require_allowed(self, path: Path) -> None:
        candidate = Path(path).resolve(strict=False)
        for root in self.allowed_roots:
            try:
                common = os.path.commonpath((str(candidate), str(root)))
                if os.path.normcase(common) == os.path.normcase(str(root)):
                    return
            except ValueError:
                continue
        raise SourcePartitionError("source partition backup path 越界")

    @staticmethod
    def _atomic_json(path: Path, value: object) -> None:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        SourcePartitionStore._atomic_bytes(path, payload)

    @staticmethod
    def _atomic_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


