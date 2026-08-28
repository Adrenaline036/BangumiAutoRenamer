"""Crash-safe contracts for detaching an invalid local catalog registration.

The transaction record is coordination evidence, not a new business authority.
This module keeps diagnosis and phase planning pure so page reads cannot mutate
Catalog, DecisionState, or Plan state.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional

from .decision_state import DecisionStateStore
from .file_lock import FileLockBusyError, advisory_file_lock
from .plan_store import PlanStore
from .projection_authority import (
    ProjectionCommitService,
    TaskTerminalMutation,
    commit_projected_plan,
)
from .plan_verifier import PlanVerifier
from .series_catalog import SeriesCatalog, SeriesCatalogError
from .work_catalog import WorkCatalog, WorkCatalogError


_PHASES = (
    "prepared",
    "catalog_archived",
    "decision_detached",
    "plan_projected",
    "completed",
)


class RecoveryTransactionError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecoveryTransaction:
    task_id: str
    operation: str
    phase: str
    catalog_backend: str = ""
    catalog_key: str = ""
    expected_decision_revision: int = 0
    decision_fingerprint: str = ""
    old_plan_fingerprint: str = ""
    prepared_plan_fingerprint: str = ""
    catalog_record_fingerprint: str = ""
    recovery_nonce: str = ""

    @classmethod
    def from_dict(cls, value: object) -> "RecoveryTransaction":
        if not isinstance(value, Mapping):
            raise RecoveryTransactionError("恢复事务根节点必须是对象")
        operation = str(value.get("operation") or "")
        phase = str(value.get("phase") or "")
        task_id = str(value.get("task_id") or "")
        if operation != "catalog_detach_pending":
            raise RecoveryTransactionError("恢复事务 operation 无效")
        if phase not in _PHASES:
            raise RecoveryTransactionError("恢复事务 phase 无效")
        if not task_id:
            raise RecoveryTransactionError("恢复事务缺少 task_id")
        return cls(
            task_id=task_id,
            operation=operation,
            phase=phase,
            catalog_backend=str(value.get("catalog_backend") or ""),
            catalog_key=str(value.get("catalog_key") or ""),
            expected_decision_revision=int(
                value.get("expected_decision_revision") or 0
            ),
            decision_fingerprint=str(value.get("decision_fingerprint") or ""),
            old_plan_fingerprint=str(value.get("old_plan_fingerprint") or ""),
            prepared_plan_fingerprint=str(
                value.get("prepared_plan_fingerprint") or ""
            ),
            catalog_record_fingerprint=str(
                value.get("catalog_record_fingerprint") or ""
            ),
            recovery_nonce=str(value.get("recovery_nonce") or ""),
        )

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "operation": self.operation,
            "phase": self.phase,
            "catalog_backend": self.catalog_backend,
            "catalog_key": self.catalog_key,
            "expected_decision_revision": self.expected_decision_revision,
            "decision_fingerprint": self.decision_fingerprint,
            "old_plan_fingerprint": self.old_plan_fingerprint,
            "prepared_plan_fingerprint": self.prepared_plan_fingerprint,
            "catalog_record_fingerprint": self.catalog_record_fingerprint,
            "recovery_nonce": self.recovery_nonce,
        }


@dataclass(frozen=True)
class RecoveryStep:
    next_phase: str
    archive_catalog: bool = False
    write_decision: bool = False
    write_plan: bool = False
    generate_recovery_nonce: bool = False
    advance_transaction: bool = False


@dataclass(frozen=True)
class TransactionDiagnosis:
    state: str
    allowed_actions: tuple[str, ...] = ()
    mutate_business_state: bool = False
    advance_phase: bool = False


@dataclass(frozen=True)
class TransactionReconciliation:
    action: str
    resume_phase: Optional[str] = None
    archive_catalog: bool = False
    mutate_business_state: bool = False


class RecoveryTransactionStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, task_id: str) -> Path:
        if not task_id or Path(task_id).name != task_id:
            raise RecoveryTransactionError("恢复事务 task_id 不安全")
        return self.root / f"{task_id}.json"

    def load_optional(self, task_id: str) -> Optional[RecoveryTransaction]:
        path = self.path_for(task_id)
        if not path.exists():
            return None
        try:
            return RecoveryTransaction.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RecoveryTransactionError(f"无法读取恢复事务：{exc}") from exc

    def save(self, transaction: RecoveryTransaction) -> None:
        destination = self.path_for(transaction.task_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "x", encoding="utf-8") as handle:
                json.dump(
                    transaction.to_dict(), handle, ensure_ascii=False, indent=2
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self, task_id: str) -> None:
        self.path_for(task_id).unlink(missing_ok=True)

    @contextmanager
    def lock(self, task_id: str):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / f".{self.path_for(task_id).name}.lock"
        try:
            with advisory_file_lock(
                lock_path,
                owner={"kind": "catalog_detach", "task_id": task_id},
            ):
                yield
        except FileLockBusyError as exc:
            raise RecoveryTransactionError(
                "该作品登记恢复事务正在执行，请稍后重试"
            ) from exc


class CatalogDetachService:
    """Execute one Catalog detach transaction through durable phase markers.

    A page click may run all phases, but every business write is followed by a
    transaction checkpoint.  A crash therefore leaves either a resumable phase
    or a fail-closed evidence conflict; it never makes a completed transaction
    coexist with ``catalog_detach_pending``.
    """

    _DETACHED_METADATA_KEYS = {
        "catalog_work_key",
        "proposed_work_key",
        "catalog_revision",
        "catalog_allow_outside_current_root",
        "series_key",
        "series_catalog_revision",
    }

    def __init__(
        self,
        *,
        transaction_store: RecoveryTransactionStore,
        decision_store: DecisionStateStore,
        plan_store: PlanStore,
        work_catalog: WorkCatalog,
        series_catalog: SeriesCatalog,
        task_root: Path,
    ):
        self.transaction_store = transaction_store
        self.decision_store = decision_store
        self.plan_store = plan_store
        self.work_catalog = work_catalog
        self.series_catalog = series_catalog
        self.task_root = Path(task_root)

    def _task_terminal(self, task_id: str) -> TaskTerminalMutation:
        def update(_decision, projected, current):
            verification = PlanVerifier().verify(projected)
            current.update({
                "workflow_status": (
                    "need_confirm"
                    if verification.status.value == "accepted"
                    else verification.status.value
                ),
                "plan_status": verification.status.value,
                "plan_entry_count": len(projected.entries),
                "completed_at": None,
            })
            return current

        return TaskTerminalMutation.prepare(self.task_root, task_id, update)

    def execute(
        self, task_id: str, *, catalog_backend: str, catalog_key: str
    ) -> tuple[object, object, RecoveryTransaction]:
        if catalog_backend not in {"work", "series"}:
            raise RecoveryTransactionError("作品登记后端无效")
        if not str(catalog_key).strip():
            raise RecoveryTransactionError("作品登记键为空")
        with self.transaction_store.lock(task_id):
            transaction = self.transaction_store.load_optional(task_id)
            if transaction is None:
                transaction = self._prepare(
                    task_id,
                    catalog_backend=catalog_backend,
                    catalog_key=str(catalog_key),
                )
            elif transaction.phase == "completed":
                decision = self.decision_store.load(task_id)
                plan = self.plan_store.load(task_id)
                self._assert_completed_consistency(transaction, decision, plan)
                return decision, plan, transaction
            elif (
                transaction.catalog_backend != catalog_backend
                or transaction.catalog_key != str(catalog_key)
            ):
                raise RecoveryTransactionError(
                    "未完成恢复事务指向另一条作品登记，必须先对账"
                )
            return self._advance(transaction)

    def reconcile_and_execute(
        self, task_id: str, *, catalog_backend: str, catalog_key: str
    ) -> tuple[object, object, Optional[RecoveryTransaction]]:
        """Repair a missing/corrupt phase record from business evidence only."""

        with self.transaction_store.lock(task_id):
            decision = self.decision_store.load(task_id)
            plan = self.plan_store.load(task_id)
            if decision.recovery_state != "catalog_detach_pending":
                raise RecoveryTransactionError("当前任务没有待对账的作品登记事务")
            self._assert_catalog_binding(
                decision,
                plan,
                catalog_backend=catalog_backend,
                catalog_key=catalog_key,
            )
            root = (
                self.series_catalog.root
                if catalog_backend == "series"
                else self.work_catalog.root
            )
            current = root / f"{catalog_key}.json"
            if current.is_file():
                self._catalog_path(catalog_backend, catalog_key)
                candidate = replace(
                    decision,
                    recovery_state=None,
                    execution_failure=None,
                )
                decision, projected = commit_projected_plan(
                    self.decision_store,
                    self.plan_store,
                    previous=decision,
                    candidate=candidate,
                    draft_plan=plan,
                    task_terminal=self._task_terminal(task_id),
                )
                self.transaction_store.delete(task_id)
                return decision, projected, None

            tombstones = self._valid_tombstones(catalog_backend, catalog_key)
            if len(tombstones) != 1:
                raise RecoveryTransactionError(
                    "作品登记已缺失，但匹配归档不是唯一一份；必须人工对账"
                )
            record_bytes = tombstones[0].read_bytes()
            transaction = RecoveryTransaction(
                task_id=task_id,
                operation="catalog_detach_pending",
                phase="catalog_archived",
                catalog_backend=catalog_backend,
                catalog_key=catalog_key,
                expected_decision_revision=decision.revision,
                decision_fingerprint=self.decision_store.fingerprint(decision),
                old_plan_fingerprint=self.plan_store.fingerprint(plan),
                catalog_record_fingerprint=hashlib.sha256(record_bytes).hexdigest(),
                recovery_nonce=uuid.uuid4().hex,
            )
            self.transaction_store.save(transaction)
            return self._advance(transaction)

    def _prepare(
        self, task_id: str, *, catalog_backend: str, catalog_key: str
    ) -> RecoveryTransaction:
        decision = self.decision_store.load(task_id)
        plan = self.plan_store.load(task_id)
        self._assert_catalog_binding(
            decision, plan, catalog_backend=catalog_backend, catalog_key=catalog_key
        )
        catalog_path = self._catalog_path(catalog_backend, catalog_key)
        try:
            record_bytes = catalog_path.read_bytes()
        except OSError as exc:
            raise RecoveryTransactionError(
                f"无法读取待归档作品登记：{exc}"
            ) from exc
        if decision.recovery_state != "catalog_detach_pending":
            candidate = replace(decision, recovery_state="catalog_detach_pending")
            decision, plan = commit_projected_plan(
                self.decision_store,
                self.plan_store,
                previous=decision,
                candidate=candidate,
                draft_plan=plan,
                task_terminal=self._task_terminal(task_id),
            )
        transaction = RecoveryTransaction(
            task_id=task_id,
            operation="catalog_detach_pending",
            phase="prepared",
            catalog_backend=catalog_backend,
            catalog_key=catalog_key,
            expected_decision_revision=decision.revision,
            decision_fingerprint=self.decision_store.fingerprint(decision),
            old_plan_fingerprint=self.plan_store.fingerprint(plan),
            catalog_record_fingerprint=hashlib.sha256(record_bytes).hexdigest(),
            recovery_nonce=uuid.uuid4().hex,
        )
        self.transaction_store.save(transaction)
        return transaction

    def _advance(
        self, transaction: RecoveryTransaction
    ) -> tuple[object, object, RecoveryTransaction]:
        for _ in range(8):
            decision = self.decision_store.load(transaction.task_id)
            plan = self.plan_store.load(transaction.task_id)
            if transaction.phase == "prepared":
                status = self._catalog_status(transaction)
                if status == "intact":
                    self._archive_catalog(transaction)
                elif status != "archived_with_matching_tombstone":
                    raise RecoveryTransactionError(
                        "作品登记已缺失且找不到匹配归档，必须人工对账"
                    )
                transaction = replace(transaction, phase="catalog_archived")
                self.transaction_store.save(transaction)
                continue

            if transaction.phase in {"catalog_archived", "decision_detached"}:
                detached = (
                    decision.catalog_link_intent is None
                    and decision.series_binding is None
                    and decision.season_binding is None
                    and decision.recovery_state is None
                    and decision.execution_failure is None
                )
                candidate = (
                    decision if detached else replace(
                        decision,
                        catalog_link_intent=None,
                        series_binding=None,
                        season_binding=None,
                        recovery_state=None,
                        execution_failure=None,
                    )
                )
                projected = self._project_detached_plan(
                    transaction, candidate, plan
                )
                decision_changed = (
                    self.decision_store.fingerprint(candidate)
                    != self.decision_store.fingerprint(decision)
                )
                plan_changed = (
                    self.plan_store.fingerprint(plan)
                    != self.plan_store.fingerprint(projected)
                )
                if decision_changed:
                    decision, plan = commit_projected_plan(
                        self.decision_store,
                        self.plan_store,
                        previous=decision,
                        candidate=candidate,
                        draft_plan=projected,
                        task_terminal=self._task_terminal(transaction.task_id),
                    )
                elif plan_changed:
                    plan = ProjectionCommitService(
                        self.decision_store,
                        self.plan_store,
                        task_root=self.task_root,
                    ).commit_revision(
                        decision,
                        projected,
                        task_terminal=self._task_terminal(transaction.task_id),
                    )
                # The canonical projector adds the typed receipt and its
                # metadata fingerprint. Bind the transaction to the actual
                # persisted projection, not to the unsigned draft above.
                projected_fingerprint = self.plan_store.fingerprint(plan)
                if transaction.prepared_plan_fingerprint != projected_fingerprint:
                    transaction = replace(
                        transaction,
                        prepared_plan_fingerprint=projected_fingerprint,
                    )
                    self.transaction_store.save(transaction)
                transaction = replace(
                    transaction,
                    phase="plan_projected",
                    expected_decision_revision=decision.revision,
                    decision_fingerprint=self.decision_store.fingerprint(decision),
                )
                self.transaction_store.save(transaction)
                continue

            if transaction.phase == "plan_projected":
                self._assert_projected_consistency(transaction, decision, plan)
                transaction = replace(transaction, phase="completed")
                self.transaction_store.save(transaction)
                continue

            self._assert_completed_consistency(transaction, decision, plan)
            return decision, plan, transaction
        raise RecoveryTransactionError("作品登记恢复事务未能在有限阶段内完成")

    def _project_detached_plan(self, transaction, decision, plan):
        metadata = {
            key: value
            for key, value in dict(plan.metadata).items()
            if key not in self._DETACHED_METADATA_KEYS
        }
        metadata.update(
            {
                "recovery_reason": "catalog_detached",
                "recovery_nonce": transaction.recovery_nonce,
            }
        )
        return self.decision_store.project_plan(
            decision, replace(plan, metadata=metadata)
        )

    def _assert_projected_consistency(self, transaction, decision, plan) -> None:
        if decision.recovery_state is not None:
            raise RecoveryTransactionError(
                "作品登记已解除，但恢复状态尚未清除，拒绝完成事务"
            )
        if self.plan_store.fingerprint(plan) != transaction.prepared_plan_fingerprint:
            raise RecoveryTransactionError("恢复事务的 Plan 投影不一致")
        expected_decision = str(
            plan.metadata.get("decision_state_fingerprint") or ""
        )
        if expected_decision != self.decision_store.fingerprint(decision):
            raise RecoveryTransactionError("恢复事务的 DecisionState 投影不一致")

    def _assert_completed_consistency(self, transaction, decision, plan) -> None:
        if transaction.phase != "completed":
            raise RecoveryTransactionError("恢复事务尚未完成")
        self._assert_projected_consistency(transaction, decision, plan)

    def _assert_catalog_binding(
        self, decision, plan, *, catalog_backend: str, catalog_key: str
    ) -> None:
        if catalog_backend == "series":
            binding = decision.series_binding or {}
            frozen = str(
                binding.get("series_key") or plan.metadata.get("series_key") or ""
            )
        else:
            intent = decision.catalog_link_intent or {}
            frozen = str(
                intent.get("work_key")
                or plan.metadata.get("catalog_work_key")
                or plan.metadata.get("proposed_work_key")
                or ""
            )
        if frozen != catalog_key:
            raise RecoveryTransactionError(
                "当前决策与整理计划没有共同指向这条作品登记"
            )

    def _catalog_path(self, backend: str, key: str) -> Path:
        try:
            if backend == "series":
                self.series_catalog.load(key)
                return self.series_catalog.root / f"{key}.json"
            self.work_catalog.load(key)
            return self.work_catalog.root / f"{key}.json"
        except (SeriesCatalogError, WorkCatalogError) as exc:
            raise RecoveryTransactionError(str(exc)) from exc

    def _catalog_status(self, transaction: RecoveryTransaction) -> str:
        root = (
            self.series_catalog.root
            if transaction.catalog_backend == "series"
            else self.work_catalog.root
        )
        current = root / f"{transaction.catalog_key}.json"
        if current.is_file():
            return "intact"
        for candidate in self._valid_tombstones(
            transaction.catalog_backend, transaction.catalog_key
        ):
            try:
                digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            except OSError:
                continue
            if digest == transaction.catalog_record_fingerprint:
                return "archived_with_matching_tombstone"
        return "missing"

    def _valid_tombstones(self, backend: str, key: str) -> list[Path]:
        root = (
            self.series_catalog.root if backend == "series" else self.work_catalog.root
        )
        archive_root = root / ".deleted"
        if not archive_root.is_dir():
            return []
        valid = []
        for candidate in archive_root.glob(f"{key}.*.json"):
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                if backend == "series":
                    self.series_catalog.validate_archived_payload(payload, key)
                else:
                    self.work_catalog.validate_archived_payload(payload, key)
            except (
                OSError,
                json.JSONDecodeError,
                TypeError,
                SeriesCatalogError,
                WorkCatalogError,
            ):
                continue
            valid.append(candidate)
        return valid

    def _archive_catalog(self, transaction: RecoveryTransaction) -> None:
        try:
            if transaction.catalog_backend == "series":
                self.series_catalog.archive_missing(
                    transaction.catalog_key, task_root=self.task_root
                )
            else:
                self.work_catalog.archive_missing(
                    transaction.catalog_key, task_root=self.task_root
                )
        except (SeriesCatalogError, WorkCatalogError) as exc:
            raise RecoveryTransactionError(str(exc)) from exc


def reconcile_catalog_detach_progress(
    transaction: RecoveryTransaction,
    *,
    decision,
    persisted_plan_fingerprint: str,
    catalog_status: str = "",
) -> RecoveryStep:
    """Return the next idempotent write, never perform it."""

    if transaction.phase == "prepared":
        if catalog_status == "archived_with_matching_tombstone":
            return RecoveryStep(
                "catalog_archived", advance_transaction=True
            )
        return RecoveryStep("prepared", archive_catalog=True)

    if transaction.phase == "catalog_archived":
        detached = all(
            getattr(decision, field, None) is None
            for field in (
                "catalog_link_intent",
                "series_binding",
                "season_binding",
            )
        ) and getattr(decision, "recovery_state", None) is None
        if detached:
            return RecoveryStep(
                "decision_detached", advance_transaction=True
            )
        return RecoveryStep("catalog_archived", write_decision=True)

    if transaction.phase == "decision_detached":
        if (
            transaction.prepared_plan_fingerprint
            and persisted_plan_fingerprint
            == transaction.prepared_plan_fingerprint
        ):
            return RecoveryStep("plan_projected", advance_transaction=True)
        return RecoveryStep("decision_detached", write_plan=True)

    if transaction.phase == "plan_projected":
        if (
            transaction.prepared_plan_fingerprint
            and persisted_plan_fingerprint
            == transaction.prepared_plan_fingerprint
        ):
            return RecoveryStep("completed", advance_transaction=True)
        return RecoveryStep("plan_projected", write_plan=True)

    return RecoveryStep("completed")


def transaction_blocks_execution(
    transaction: Optional[RecoveryTransaction],
    *,
    current_decision_fingerprint: str = "",
    plan_decision_fingerprint: str = "",
) -> bool:
    if transaction is not None and transaction.phase != "completed":
        return True
    return bool(
        current_decision_fingerprint
        and plan_decision_fingerprint
        and current_decision_fingerprint != plan_decision_fingerprint
    )


def diagnose_detach_transaction(
    decision, *, transaction_payload: object
) -> TransactionDiagnosis:
    if getattr(decision, "recovery_state", None) != "catalog_detach_pending":
        return TransactionDiagnosis("not_pending")
    if transaction_payload is None:
        return TransactionDiagnosis(
            "transaction_missing", ("reconcile_detach_transaction",)
        )
    try:
        if isinstance(transaction_payload, str):
            transaction_payload = json.loads(transaction_payload)
        RecoveryTransaction.from_dict(transaction_payload)
    except (json.JSONDecodeError, TypeError, ValueError, RecoveryTransactionError):
        return TransactionDiagnosis(
            "transaction_corrupt", ("reconcile_detach_transaction",)
        )
    return TransactionDiagnosis("transaction_present")


def plan_detach_transaction_reconciliation(
    *,
    catalog_status: str,
    tombstone_matches: bool,
    decision_is_detached: bool,
) -> TransactionReconciliation:
    if catalog_status == "intact" and not decision_is_detached:
        return TransactionReconciliation("cancel_detach")
    if catalog_status == "missing" and tombstone_matches:
        return TransactionReconciliation(
            "reconstruct_transaction",
            resume_phase=(
                "decision_detached" if decision_is_detached else "catalog_archived"
            ),
        )
    return TransactionReconciliation("manual_repair_required")


def diagnose_detach_transaction_conflict(
    transaction: RecoveryTransaction,
    *,
    current_catalog_backend: str,
    current_catalog_key: str,
    current_decision_fingerprint: str,
    current_plan_fingerprint: str,
) -> TransactionDiagnosis:
    frozen_values = (
        (transaction.catalog_backend, current_catalog_backend),
        (transaction.catalog_key, current_catalog_key),
        (transaction.decision_fingerprint, current_decision_fingerprint),
        (transaction.old_plan_fingerprint, current_plan_fingerprint),
    )
    conflict = any(frozen and frozen != current for frozen, current in frozen_values)
    if not conflict:
        return TransactionDiagnosis("transaction_consistent")
    actions = (
        ("abandon_stale_transaction",)
        if transaction.phase == "prepared"
        else ("reconcile_detach_transaction",)
    )
    return TransactionDiagnosis("transaction_conflict", actions)
