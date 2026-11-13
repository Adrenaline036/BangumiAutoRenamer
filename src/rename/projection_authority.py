"""Canonical Decision -> Plan projection and durable commit authority.

The module deliberately owns the projection receipt, per-task episode/source
ownership and the only production call to :meth:`PlanStore.save`.  Callers may
submit a typed envelope; they may not persist an arbitrary Plan directly.
"""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Optional

from .decision_state import DecisionState, DecisionStateStore
from .file_lock import FileLockBusyError

from .physical_identity import (
    PhysicalIdentityUnavailable,
    location_key_from_resolved,
    canonical_physical_key,
)
from .plan_store import PlanStore
from .rename_plan import (
    PLAN_SCHEMA_VERSION,
    PROJECTION_RECEIPT_SCHEMA_VERSION,
    PlanEntry,
    ProjectionReceipt,
    ProviderIdentity,
    RenamePlan,
    WorkIdentity,
)
from .successor_authority import (
    CohortAuthority,
    CohortAuthorityDraft,
    CohortAuthorityStore,
    SuccessorAuthorityError,
    successor_authority_root,
)
from .tv_planning_authority import TVPlanningAuthority
from ..utils.utils import (
    read_task_at,
    replace_task_record_at_if_sha256,
    restore_task_record_at_if_sha256,
    task_record_serialized_sha256,
    task_record_snapshot_at,
)


AUTHORITY_FINGERPRINT_FIELDS = frozenset({
    "receipt_schema_version",
    "plan_schema_version",
    "task_id",
    "decision_schema_version",
    "decision_revision",
    "decision_fingerprint",
    "inventory_schema_version",
    "inventory_revision",
    "inventory_fingerprint",
    "source_ownership_schema_version",
    "source_ownership_revision",
    "source_ownership_fingerprint",
    "episode_ownership_schema_version",
    "episode_ownership_fingerprint",
    "tv_planning_authority_schema_version",
    "tv_planning_authority_policy_version",
    "tv_planning_authority_fingerprint",
    "batch_scope_fingerprint",
    "reservation_lineage_id",
    "reservation_revision",
    "work_identity_fingerprint",
    "series_binding_fingerprint",
    "season_binding_fingerprint",
    "target_routing_fingerprint",
    "naming_policy_fingerprint",
    "subtitle_policy_fingerprint",
    "execution_policy_fingerprint",
    "policy_bundle_version",
    "projector_version",
    "entry_set_fingerprint",
})

CORRELATION_ONLY_FIELDS = frozenset({
    "created_at",
    "updated_at",
    "journal_nonce",
    "journal_attempt_id",
    "temporary_path",
    "request_token",
    "season_binding_id",
    "cohort_decision_id",
})

OPTIONAL_AUTHORITY_FIELDS = frozenset({'generated_outputs_fingerprint', 'tv_boundary_completion_receipt_fingerprint', 'pending_tv_selection_fingerprint', 'initial_source_basis_fingerprint', 'content_policy', 'completed_rebuild_fingerprint', 'asset_operation_fingerprint',
                                      'current_tv_refresh_fingerprint', 'work_affiliation_fingerprint',
                                      'initial_movie_selection_result_fingerprint',
                                      'automatic_recognition_origin_fingerprint', 'automatic_recognition_fingerprint',
                                      'automatic_search_policy_fingerprint'})

PROJECTION_COMMIT_PHASES = (
    "prepared",
    "decision_written",
    "ownership_claimed",
    "plan_written",
    "task_terminal_written",
    "completed",
)

PROJECTION_POLICY_VERSION = "projection-policy-v1"
PROJECTOR_VERSION = "final-plan-projector-v1"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_NOT_APPLICABLE_FINGERPRINT = "0" * 64


TV_AUTHORITY_FIELD_RESPONSIBILITY = MappingProxyType({
    "schema_version": "receipt_fingerprint",
    "policy_version": "receipt_fingerprint",
    "fingerprint": "receipt_fingerprint",
    "task_source_scope": "receipt_fingerprint",
    "decision_revision": "receipt_fingerprint",
    "observation_refs": "decision_ui_only",
    "work_identity": "decision_ui_only",
    "metadata_provider_identities": "decision_ui_only",
    "naming_authority": "decision_ui_only",
    "season_assignments": "decision_ui_only",
    "selected_season_number": "typed_plan_transform",
    "episode_range_ownership": "decision_ui_only",
    "file_decisions": "decision_ui_only",
    "non_regular_file_decisions": "decision_ui_only",
    "derived_provenance": "forbidden_from_plan",
})
_TV_AUTHORITY_RESPONSIBILITIES = frozenset({
    "receipt_fingerprint",
    "typed_plan_transform",
    "decision_ui_only",
    "forbidden_from_plan",
})


class ProjectionAuthorityError(RuntimeError):
    """The typed authority is incomplete, stale or cannot be committed."""


class SourceOwnershipConflict(ProjectionAuthorityError):
    """Another active task already owns the same location or physical file."""


def _tv_authority_receipt_fields(state: DecisionState) -> dict[str, object]:
    """Project Decision-owned TV authority without reinterpreting live facts."""

    authority = state.tv_planning_authority
    if authority is None:
        return {
            "tv_planning_authority_schema_version": 0,
            "tv_planning_authority_policy_version": "not-applicable",
            "tv_planning_authority_fingerprint": _NOT_APPLICABLE_FINGERPRINT,
            "batch_scope_fingerprint": _NOT_APPLICABLE_FINGERPRINT,
            "reservation_lineage_id": _NOT_APPLICABLE_FINGERPRINT,
            "reservation_revision": 0,
        }
    scope = authority.task_source_scope
    return {
        "tv_planning_authority_schema_version": authority.schema_version,
        "tv_planning_authority_policy_version": authority.policy_version,
        "tv_planning_authority_fingerprint": authority.fingerprint,
        "batch_scope_fingerprint": scope.batch_scope_fingerprint,
        "reservation_lineage_id": scope.reservation_lineage_id,
        "reservation_revision": scope.reservation_revision,
    }


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    def freeze(item: object) -> object:
        if isinstance(item, Mapping):
            return MappingProxyType({str(k): freeze(v) for k, v in item.items()})
        if isinstance(item, (tuple, list)):
            return tuple(freeze(v) for v in item)
        return item

    frozen = freeze(dict(value))
    assert isinstance(frozen, Mapping)
    return frozen


def _fingerprint(value: object, *, domain: str) -> str:
    payload = {
        "domain": domain,
        "version": 1,
        "value": _thaw(value),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _assert_tv_authority_responsibility_table() -> None:
    """Fail closed when the typed authority grows without a projection decision."""

    actual = {item.name for item in fields(TVPlanningAuthority)}
    declared = set(TV_AUTHORITY_FIELD_RESPONSIBILITY)
    if actual != declared:
        missing = sorted(actual - declared)
        stale = sorted(declared - actual)
        raise ProjectionAuthorityError(
            "TV authority responsibility table is incomplete: "
            f"missing={missing}, stale={stale}"
        )
    invalid = sorted(
        set(TV_AUTHORITY_FIELD_RESPONSIBILITY.values())
        - _TV_AUTHORITY_RESPONSIBILITIES
    )
    if invalid:
        raise ProjectionAuthorityError(
            f"TV authority responsibility table has invalid classes: {invalid}"
        )


def _lexical_path_key(path: Path | str) -> str:
    """Normalize an already-frozen path without consulting the filesystem."""

    return os.path.normcase(os.path.abspath(os.path.normpath(str(path)))).casefold()










def _without_correlation(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _without_correlation(item)
            for key, item in value.items()
            if str(key) not in CORRELATION_ONLY_FIELDS
        }
    if isinstance(value, (tuple, list)):
        return [_without_correlation(item) for item in value]
    return value


def canonical_authority_fingerprint(authority: Mapping[str, object]) -> str:
    """Hash only the frozen positive allowlist with domain separation."""

    if not isinstance(authority, Mapping):
        raise ProjectionAuthorityError("projection authority must be an object")
    unknown = set(authority) - AUTHORITY_FINGERPRINT_FIELDS - OPTIONAL_AUTHORITY_FIELDS - CORRELATION_ONLY_FIELDS
    missing = AUTHORITY_FINGERPRINT_FIELDS - set(authority)
    if unknown or missing:
        raise ProjectionAuthorityError(
            f"projection authority fields mismatch: missing={sorted(missing)!r}, "
            f"unknown={sorted(unknown)!r}"
        )
    frozen = {
        field: _without_correlation(authority[field])
        for field in sorted(AUTHORITY_FINGERPRINT_FIELDS | (OPTIONAL_AUTHORITY_FIELDS & set(authority)))
    }
    return _fingerprint(frozen, domain="bar.projection-authority")


def build_projection_receipt(
    authority: Mapping[str, object],
) -> ProjectionReceipt:
    frozen = {
        field: _without_correlation(authority[field])
        for field in sorted(AUTHORITY_FINGERPRINT_FIELDS | OPTIONAL_AUTHORITY_FIELDS)
        if field in authority
    }
    fingerprint = canonical_authority_fingerprint(authority)
    return ProjectionReceipt(authority=frozen, fingerprint=fingerprint)


def validate_projection_receipt(receipt: ProjectionReceipt) -> None:
    if not isinstance(receipt, ProjectionReceipt):
        raise ProjectionAuthorityError("Plan has no typed ProjectionReceipt")
    if receipt.schema_version != PROJECTION_RECEIPT_SCHEMA_VERSION:
        raise ProjectionAuthorityError("ProjectionReceipt schema is stale")
    expected = canonical_authority_fingerprint(receipt.authority)
    if receipt.fingerprint != expected:
        raise ProjectionAuthorityError("ProjectionReceipt fingerprint mismatch")


@dataclass(frozen=True)
class TaskEpisodeOwnership:
    source_path: Path
    bangumi_subject_id: str
    work_identity_key: str
    work_identity_fingerprint: str
    season_number: int
    source_episode: int
    target_episode: int
    range_authority_id: str
    evidence_fingerprint: str
    decision_source: str
    schema_version: int = 2

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_path", Path(self.source_path))
        if (
            self.schema_version != 2
            or not self.bangumi_subject_id
            or not self.work_identity_key
            or self.work_identity_fingerprint
            != hashlib.sha256(self.work_identity_key.encode("utf-8")).hexdigest()
            or self.season_number < 0
            or self.source_episode < 1
            or self.target_episode < 1
            or not self.range_authority_id
            or _HEX64.fullmatch(self.evidence_fingerprint) is None
            or not self.decision_source
        ):
            raise ProjectionAuthorityError("TaskEpisodeOwnership is incomplete")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_path": str(self.source_path),
            "bangumi_subject_id": self.bangumi_subject_id,
            "work_identity_key": self.work_identity_key,
            "work_identity_fingerprint": self.work_identity_fingerprint,
            "season_number": self.season_number,
            "source_episode": self.source_episode,
            "target_episode": self.target_episode,
            "range_authority_id": self.range_authority_id,
            "evidence_fingerprint": self.evidence_fingerprint,
            "decision_source": self.decision_source,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "TaskEpisodeOwnership":
        version = int(value.get("schema_version", 0))
        if version == 1:
            tmdb_tv_id = str(value.get("tmdb_tv_id") or "")
            work_identity_key = f"tmdb:tv:{tmdb_tv_id}" if tmdb_tv_id else ""
            work_identity_fingerprint = (
                hashlib.sha256(work_identity_key.encode("utf-8")).hexdigest()
                if work_identity_key else ""
            )
        else:
            work_identity_key = str(value.get("work_identity_key") or "")
            work_identity_fingerprint = str(
                value.get("work_identity_fingerprint") or ""
            )
        return cls(
            source_path=Path(str(value.get("source_path") or "")),
            bangumi_subject_id=str(value.get("bangumi_subject_id") or ""),
            work_identity_key=work_identity_key,
            work_identity_fingerprint=work_identity_fingerprint,
            season_number=int(value.get("season_number", -1)),
            source_episode=int(value.get("source_episode", 0)),
            target_episode=int(value.get("target_episode", 0)),
            range_authority_id=str(value.get("range_authority_id") or ""),
            evidence_fingerprint=str(value.get("evidence_fingerprint") or ""),
            decision_source=str(value.get("decision_source") or ""),
            schema_version=2,
        )


@dataclass(frozen=True)
class SourceIdentity:
    source_path: Path
    location_key: str
    physical_key: str
    size: int
    mtime_ns: int
    inventory_fingerprint: str
    schema_version: int = 1

    @classmethod
    def from_path(
        cls, path: Path, *, inventory_fingerprint: str
    ) -> "SourceIdentity":
        source = Path(path).resolve(strict=True)
        stat_result = source.stat()
        try:
            physical_key = canonical_physical_key(stat_result)
        except PhysicalIdentityUnavailable as exc:
            raise ProjectionAuthorityError("source physical identity/inode 不可用") from exc
        return cls(
            source_path=source,
            location_key=location_key_from_resolved(source),
            physical_key=physical_key,
            size=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
            inventory_fingerprint=inventory_fingerprint,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_path": str(self.source_path),
            "location_key": self.location_key,
            "physical_key": self.physical_key,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "inventory_fingerprint": self.inventory_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "SourceIdentity":
        return cls(
            source_path=Path(str(value.get("source_path") or "")),
            location_key=str(value.get("location_key") or ""),
            physical_key=str(value.get("physical_key") or ""),
            size=int(value.get("size", 0)),
            mtime_ns=int(value.get("mtime_ns", 0)),
            inventory_fingerprint=str(value.get("inventory_fingerprint") or ""),
            schema_version=int(value.get("schema_version", 0)),
        )


@dataclass(frozen=True)
class SourceOwnershipRecord:
    task_id: str
    revision: int
    decision_revision: int
    inventory_fingerprint: str
    identities: tuple[SourceIdentity, ...]
    state: str = "active"
    reason: str = ""
    schema_version: int = 1

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_dict(), domain="bar.source-ownership")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "revision": self.revision,
            "decision_revision": self.decision_revision,
            "inventory_fingerprint": self.inventory_fingerprint,
            "identities": [item.to_dict() for item in self.identities],
            "state": self.state,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "SourceOwnershipRecord":
        identities = value.get("identities", ())
        if not isinstance(identities, (list, tuple)):
            raise ProjectionAuthorityError("SourceOwnership identities are invalid")
        return cls(
            task_id=str(value.get("task_id") or ""),
            revision=int(value.get("revision", 0)),
            decision_revision=int(value.get("decision_revision", 0)),
            inventory_fingerprint=str(value.get("inventory_fingerprint") or ""),
            identities=tuple(
                SourceIdentity.from_dict(item)
                for item in identities if isinstance(item, Mapping)
            ),
            state=str(value.get("state") or ""),
            reason=str(value.get("reason") or ""),
            schema_version=int(value.get("schema_version", 0)),
        )


class SourceOwnershipStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _lock_resource(self):
        from .file_lock import LockResource
        return LockResource(self, self.root / '.ownership.lock', 'source_ownership')

    def _mutation_lock(self, *, owner=None):
        from .file_lock import serialized_resource_guard
        return serialized_resource_guard(self._lock_resource(), owner=owner)

    def _path(self, task_id: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9._-]+", task_id) is None:
            raise ProjectionAuthorityError("unsafe source ownership task id")
        return self.root / f"{task_id}.json"

    def load_optional(self, task_id: str) -> Optional[SourceOwnershipRecord]:
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ProjectionAuthorityError("source ownership record is unreadable") from exc
        if not isinstance(value, Mapping):
            raise ProjectionAuthorityError("source ownership record is invalid")
        return SourceOwnershipRecord.from_dict(value)

    def claim(
        self,
        *,
        task_id: str,
        decision_revision: int,
        inventory_fingerprint: str,
        identities: Iterable[SourceIdentity],
    ) -> SourceOwnershipRecord:
        rows = tuple(identities)
        self.root.mkdir(parents=True, exist_ok=True)
        with self._mutation_lock(
            owner={"kind": "source_ownership", "task_id": task_id},
        ):
            existing = self.load_optional(task_id)
            if existing is not None and existing.state == "active":
                if decision_revision < existing.decision_revision:
                    raise SourceOwnershipConflict(
                        "source ownership decision revision is stale"
                    )
                if (
                    decision_revision == existing.decision_revision
                    and existing.identities == rows
                ):
                    return existing
                if decision_revision == existing.decision_revision:
                    raise SourceOwnershipConflict(
                        "task revision already owns a different source set"
                    )
            if type(self.observe_conflicts((task_id,), rows)) is not tuple:
                raise SourceOwnershipConflict('source ownership conflict check disconnected')
            record = SourceOwnershipRecord(
                task_id=task_id,
                revision=(existing.revision + 1 if existing is not None else 0),
                decision_revision=decision_revision,
                inventory_fingerprint=inventory_fingerprint,
                identities=rows,
            )
            self._write(record)
            return record

    def observe_conflicts(self, excluded_task_ids, identities):
        """One read-only location/physical conflict check for all claim paths."""
        excluded = frozenset(excluded_task_ids)
        for identifier in excluded:
            self._path(identifier)
        rows = tuple(identities)
        location_keys = {item.location_key for item in rows}
        physical_keys = {item.physical_key for item in rows}
        observed = []
        for path in self.root.glob('*.json'):
            if path.stem in excluded:
                continue
            other = self.load_optional(path.stem)
            if other is None:
                continue
            observed.append(other)
            if other.state == 'active' and (location_keys.intersection(item.location_key for item in other.identities)
                    or physical_keys.intersection(item.physical_key for item in other.identities)):
                raise SourceOwnershipConflict(f'source is already owned by task {other.task_id}')
        return tuple(observed)

    def preview_claim(
        self,
        *,
        task_id: str,
        decision_revision: int,
        inventory_fingerprint: str,
        identities: Iterable[SourceIdentity],
    ) -> SourceOwnershipRecord:
        """Freeze the exact intended ownership record before its side effect."""

        rows = tuple(identities)
        existing = self.load_optional(task_id)
        if (
            existing is not None
            and existing.state == "active"
            and existing.decision_revision == decision_revision
            and existing.identities == rows
        ):
            return existing
        return SourceOwnershipRecord(
            task_id=task_id,
            revision=(existing.revision + 1 if existing is not None else 0),
            decision_revision=decision_revision,
            inventory_fingerprint=inventory_fingerprint,
            identities=rows,
        )

    def partition(
        self,
        *,
        parent_task_id: str,
        expected_parent_fingerprint: str,
        child_task_id: str,
        selected_location_keys: Iterable[str],
        partition_fingerprint: str,
        parent_inventory_fingerprint: str,
    ) -> tuple[SourceOwnershipRecord, SourceOwnershipRecord]:
        """Retired: no alternate writer or automatic compatibility fallback."""
        raise ProjectionAuthorityError('旧二分拆卡已停用，请使用“调整计划范围”；无法处理的旧任务请安全结束后重新添加。')

    def restore_exact(
        self,
        current: SourceOwnershipRecord,
        previous: Optional[SourceOwnershipRecord],
    ) -> None:
        """Undo one exact ownership revision after the projection fails."""

        with self._mutation_lock(
            owner={"kind": "source_ownership_rollback", "task_id": current.task_id},
        ):
            stored = self.load_optional(current.task_id)
            if stored is None or stored.fingerprint != current.fingerprint:
                raise ProjectionAuthorityError(
                    "source ownership changed before rollback"
                )
            if previous is None:
                self._path(current.task_id).unlink(missing_ok=True)
            else:
                self._write(previous)

    def retire(
        self, *, task_id: str, expected_revision: int, reason: str
    ) -> SourceOwnershipRecord:
        with self._mutation_lock(
            owner={"kind": "source_ownership_retire", "task_id": task_id},
        ):
            current = self.load_optional(task_id)
            if (
                current is None
                or current.state != "active"
                or current.revision != expected_revision
                or not reason
            ):
                raise ProjectionAuthorityError("source ownership retire is stale")
            retired = replace(
                current,
                revision=current.revision + 1,
                state="retired",
                reason=reason,
            )
            self._write(retired)
            return retired

    def _write(self, record: SourceOwnershipRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self._path(record.task_id)
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "x", encoding="utf-8") as handle:
                json.dump(record.to_dict(), handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class InventoryReplayGuard:
    """The observations and prior authority used to prepare a TV retry."""

    decision_fingerprint: str
    plan_fingerprint: str
    source_root: Path
    inventory_fingerprint: str
    source_identities: tuple[SourceIdentity, ...]

    def validate(self, previous, current, plan, identities):
        if (previous is None or current is None or plan is None
                or DecisionStateStore.fingerprint(previous) != self.decision_fingerprint
                or DecisionStateStore.fingerprint(current) != self.decision_fingerprint
                or PlanStore.fingerprint(plan) != self.plan_fingerprint):
            raise ProjectionAuthorityError('重试期间整理选择已变化，请刷新后重试')
        expected = {replace(i, inventory_fingerprint='') for i in self.source_identities}
        actual = {replace(i, inventory_fingerprint='') for i in identities}
        from .movie_assets import inventory_movie_assets
        if actual != expected or inventory_movie_assets(self.source_root).fingerprint != self.inventory_fingerprint:
            raise ProjectionAuthorityError('重试期间源文件已变化，请重新核查来源')


@dataclass(frozen=True)
class ProjectionAuthorityEnvelope:
    previous_decision: Optional[DecisionState]
    decision: DecisionState
    draft_plan: RenamePlan
    inventory_fingerprint: str
    episode_ownership: tuple[TaskEpisodeOwnership, ...] = ()
    cohort_authority: Optional[CohortAuthorityDraft] = None
    inventory_replay_guard: Optional[InventoryReplayGuard] = None
    media_archive: Optional[object] = None
    movie_change: Optional[object] = None
    initial_movie_selection: Optional[object] = None
    initial_movie_completion: Optional[object] = None
    initial_tv_completion: Optional[object] = None
    pending_tv_selection: Optional[object] = None
    automatic_recognition: Optional[object] = None
    content_correction: Optional[object] = None
    initial_default: Optional[object] = None


TaskTerminalBuilder = Callable[
    [DecisionState, RenamePlan, dict[str, object]], Mapping[str, object]
]


@dataclass(frozen=True)
class TaskTerminalMutation:
    """CAS-bound task mutation committed after the receipted Plan write."""

    task_root: Path
    task_id: str
    previous_bytes: Optional[bytes]
    previous_payload: Mapping[str, object]
    expected_sha256: str
    builder: TaskTerminalBuilder

    @classmethod
    def prepare(
        cls,
        task_root: Path,
        task_id: str,
        builder: TaskTerminalBuilder,
    ) -> "TaskTerminalMutation":
        previous, payload, fingerprint = task_record_snapshot_at(
            Path(task_root), task_id
        )
        return cls(
            task_root=Path(task_root),
            task_id=str(task_id),
            previous_bytes=previous,
            previous_payload=(dict(payload) if payload else {"uuid": str(task_id)}),
            expected_sha256=fingerprint,
            builder=builder,
        )

    def render(self, decision: DecisionState, plan: RenamePlan) -> dict:
        if decision.task_id != self.task_id or plan.task_id != self.task_id:
            raise ProjectionAuthorityError("task terminal mutation task id differs")
        payload = dict(self.builder(decision, plan, dict(self.previous_payload)))
        if payload.get("uuid") != self.task_id:
            raise ProjectionAuthorityError("task terminal mutation changed task id")
        payload["projection_receipt_fingerprint"] = (
            plan.projection_receipt.fingerprint
            if plan.projection_receipt is not None else None
        )
        payload["plan_fingerprint"] = PlanStore.fingerprint(plan)
        payload["decision_state_revision"] = decision.revision
        payload["decision_state_fingerprint"] = (
            DecisionStateStore.fingerprint(decision)
        )
        history_value = payload.get("operation_history", ())
        if not isinstance(history_value, (list, tuple)) or not all(
            isinstance(item, Mapping) for item in history_value
        ):
            raise ProjectionAuthorityError("task operation history is invalid")
        history = [dict(item) for item in history_value]
        authority_fingerprint = (
            plan.projection_receipt.fingerprint
            if plan.projection_receipt is not None else ""
        )
        history.append({
            "operation_id": uuid.uuid4().hex,
            "code": "projection_committed",
            "phase": "task_terminal",
            "decision_revision": decision.revision,
            "authority_fingerprint": authority_fingerprint,
            "outcome": "succeeded",
            "occurred_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
        })
        payload["operation_history"] = history
        payload["operation_history_state"] = "current"
        # Clear only stale values carried through unchanged from the previous
        # task snapshot.  A builder may create a new PlanVerifier/provider
        # failure in this same terminal mutation; that current error remains
        # user-visible and must not be mistaken for the older failure.
        if payload.get("error") == self.previous_payload.get("error"):
            payload["error"] = None
        if payload.get("current_operation_failure") == (
            self.previous_payload.get("current_operation_failure")
        ):
            payload["current_operation_failure"] = None
        return payload


@dataclass(frozen=True)
class RecognitionDecisionInput:
    """Explicit resolver output used to create Decision authority."""

    task_id: str
    work_identity: WorkIdentity
    linked_provider_identities: tuple[ProviderIdentity, ...] = ()
    season_number: Optional[int] = None
    season_source: str = "unset"
    layout_mode: Optional[str] = None
    # Set only by the fresh observation adapter. Empty string pins no record;
    # None preserves callers that already use their own current-state guard.
    expected_previous_fingerprint: Optional[str] = None
    initial_movie_request: Optional[object] = None
    automatic_recognition: Optional[object] = None

    def candidate(self, previous: Optional[DecisionState]) -> DecisionState:
        if previous is not None and previous.task_id != self.task_id:
            raise ProjectionAuthorityError('recognition and Decision task ids differ')
        if self.expected_previous_fingerprint is not None:
            actual = DecisionStateStore.fingerprint(previous) if previous is not None else ''
            if self.expected_previous_fingerprint != actual:
                raise ProjectionAuthorityError('recognition observation is stale: 当前记录已变化')
        if self.work_identity.status != "confirmed":
            raise ProjectionAuthorityError("recognition work identity is not confirmed")
        primary = self.work_identity.primary_provider.to_dict()
        linked = tuple(item.to_dict() for item in self.linked_provider_identities)
        providers = (primary,) + linked
        season_binding = previous.season_binding if previous is not None else None
        old_primary = None
        if previous is not None:
            stored = previous.confirmed_work.get("primary_provider")
            if isinstance(stored, Mapping):
                old_primary = tuple(
                    str(stored.get(field) or "").casefold()
                    for field in ("namespace", "entity_type", "provider_id")
                )
        new_primary = tuple(
            str(primary.get(field) or "").casefold()
            for field in ("namespace", "entity_type", "provider_id")
        )
        identity_changed = previous is not None and old_primary != new_primary
        if identity_changed:
            season_binding = None
        if self.season_number is not None and self.season_source not in {
            "unset", "legacy_placeholder"
        }:
            from .season_identity import confirmed_season_binding
            season_binding = confirmed_season_binding(primary, int(self.season_number),
                self.season_source, previous=previous)
        base = previous or DecisionState(task_id=self.task_id)
        changes: dict[str, object] = {
            "confirmed_work": self.work_identity.to_dict(),
            "provider_evidence": providers,
            "linked_provider_identities": linked,
            "season_binding": season_binding,
            "library_domain": (
                self.work_identity.domain
                if self.work_identity.domain in {"animation", "general"}
                else None
            ),
            "layout_mode": self.layout_mode,
        }
        if identity_changed:
            changes.update({
                "movie_version_selection": None,
                "tv_planning_authority": None,
                "completed_rebuild_observation": None,
                "asset_operation": None,
                "series_binding": None,
                "manual_role_decisions": {},
                "provider_special_decisions": {},
                "catalog_link_intent": None,
                "recovery_state": None,
                "execution_failure": None,
                "provider_link_binding": None,
                "source_episode_projection": None,
                "source_episode_projection_decision": None,
                "provider_subject_boundary_decisions": {},
            })
        return replace(base, **changes)


@dataclass(frozen=True)
class UnresolvedRecognitionInput:
    """Typed unresolved observation boundary; it never guesses a work identity."""

    task_id: str

    def candidate(self, previous: Optional[DecisionState]) -> DecisionState:
        if previous is not None:
            if previous.task_id != self.task_id:
                raise ProjectionAuthorityError(
                    "unresolved recognition task id differs"
                )
            return previous
        return DecisionState(task_id=self.task_id)


@dataclass(frozen=True)
class ProviderReselectionPendingInput:
    """Typed authority reset for a selection that still needs cohort review."""

    task_id: str
    expected_revision: int

    def candidate(self, previous: Optional[DecisionState]) -> DecisionState:
        if (
            previous is None
            or previous.task_id != self.task_id
            or previous.revision != self.expected_revision
        ):
            raise ProjectionAuthorityError("provider reselection authority is stale")
        return replace(
            previous,
            movie_version_selection=None,
            confirmed_work={},
            provider_evidence=(),
            linked_provider_identities=(),
            series_binding=None,
            season_binding=None,
            library_domain=None,
            layout_mode=None,
            manual_role_decisions={},
            provider_special_decisions={},
            catalog_link_intent=None,
            recovery_state=None,
            execution_failure=None,
            provider_link_binding=None,
            source_episode_projection=None,
            source_episode_projection_decision=None,
            provider_subject_boundary_decisions={},
            tv_planning_authority=None,
            completed_rebuild_observation=None,
            asset_operation=None,
        )


class FinalPlanProjector:
    """Pure projection from one typed envelope to an immutable receipted Plan."""

    @staticmethod
    def project(
        envelope: ProjectionAuthorityEnvelope,
        ownership: SourceOwnershipRecord,
    ) -> RenamePlan:
        state = envelope.decision
        draft = DecisionStateStore.project_plan(state, envelope.draft_plan)
        episode_rows = tuple(envelope.episode_ownership)
        metadata = dict(draft.metadata)
        metadata.pop('generated_outputs', None)
        if metadata.get('asset_operation') is not None and state.asset_operation is None:
            raise ProjectionAuthorityError('资产操作权威已断开')
        metadata.pop('asset_operation', None)
        if state.asset_operation is not None:
            metadata['asset_operation'] = state.asset_operation.to_dict()
        if metadata.get('completed_rebuild_observation') is not None and state.completed_rebuild_observation is None:
            raise ProjectionAuthorityError('重建权威已断开，不能按普通计划重新签发')
        # Derived authority never survives clearing/replacing its Decision producer.
        metadata.pop('completed_rebuild_observation', None)
        if state.completed_rebuild_observation is not None:
            metadata['completed_rebuild_observation'] = state.completed_rebuild_observation.to_dict()
        if episode_rows:
            metadata["task_episode_ownership"] = [
                item.to_dict() for item in episode_rows
            ]
        episode_fingerprint = _fingerprint(
            [item.to_dict() for item in episode_rows],
            domain="bar.task-episode-ownership",
        )
        metadata["task_episode_ownership_fingerprint"] = episode_fingerprint
        metadata["source_ownership_fingerprint"] = ownership.fingerprint
        metadata["source_identity_manifest"] = [
            item.to_dict() for item in ownership.identities
        ]
        metadata["projection_authority_required"] = True
        tv_fields = _tv_authority_receipt_fields(state)
        metadata.update(tv_fields)
        metadata["tv_planning_authority_required"] = (
            state.tv_planning_authority is not None
        )
        projected = replace(draft, metadata=metadata, projection_receipt=None)
        entries = [entry.to_dict() for entry in projected.entries]
        authority = {
            "receipt_schema_version": PROJECTION_RECEIPT_SCHEMA_VERSION,
            "plan_schema_version": PLAN_SCHEMA_VERSION,
            "task_id": projected.task_id,
            "decision_schema_version": state.schema_version,
            "decision_revision": state.revision,
            "decision_fingerprint": DecisionStateStore.fingerprint(state),
            "inventory_schema_version": 1,
            "inventory_revision": 0,
            "inventory_fingerprint": envelope.inventory_fingerprint,
            "source_ownership_schema_version": ownership.schema_version,
            "source_ownership_revision": ownership.revision,
            "source_ownership_fingerprint": ownership.fingerprint,
            "episode_ownership_schema_version": 2,
            "episode_ownership_fingerprint": episode_fingerprint,
            **tv_fields,
            "work_identity_fingerprint": _fingerprint(
                projected.work_identity.to_dict()
                if projected.work_identity is not None else {},
                domain="bar.work-identity",
            ),
            "series_binding_fingerprint": _fingerprint(
                _without_correlation(state.series_binding or {}),
                domain="bar.series-binding",
            ),
            "season_binding_fingerprint": _fingerprint(
                _without_correlation(state.season_binding or {}),
                domain="bar.season-binding",
            ),
            "target_routing_fingerprint": _fingerprint(
                [str(entry.target_path) for entry in projected.entries],
                domain="bar.target-routing",
            ),
            "naming_policy_fingerprint": _fingerprint(
                {
                    key: metadata.get(key)
                    for key in (
                        "canonical_title", "canonical_work_root", "season_number",
                        "naming_policy_version", "title_policy_version",
                    )
                },
                domain="bar.naming-policy",
            ),
            "subtitle_policy_fingerprint": _fingerprint(
                [rule.to_dict() for rule in projected.subtitle_override_rules],
                domain="bar.subtitle-policy",
            ),
            "execution_policy_fingerprint": _fingerprint(
                projected.execution_policy.to_dict()
                if projected.execution_policy is not None else {},
                domain="bar.execution-policy",
            ),
            "policy_bundle_version": PROJECTION_POLICY_VERSION,
            "projector_version": PROJECTOR_VERSION,
            "entry_set_fingerprint": _fingerprint(
                entries, domain="bar.plan-entry-set"
            ),
        }
        if state.completed_rebuild_observation is not None:
            authority['completed_rebuild_fingerprint'] = state.completed_rebuild_observation.fingerprint
        if state.content_policy is not None:
            authority['content_policy'] = state.content_policy.value
        from .work_affiliation import authority_fingerprint as affiliation_fingerprint
        affiliated = affiliation_fingerprint(projected)
        if affiliated is not None:
            authority['work_affiliation_fingerprint'] = affiliated
        if state.initial_source_basis is not None:
            authority['initial_source_basis_fingerprint'] = state.initial_source_basis.fingerprint
        if state.pending_tv_selection is not None:
            authority['pending_tv_selection_fingerprint'] = state.pending_tv_selection.fingerprint
        from .tv_boundary_completion import receipt_fingerprint
        boundary_receipt = receipt_fingerprint(projected)
        if boundary_receipt is not None:
            authority['tv_boundary_completion_receipt_fingerprint'] = boundary_receipt
        if state.asset_operation is not None:
            authority['asset_operation_fingerprint'] = state.asset_operation.fingerprint
        from .initial_movie_selection import selection_result_fingerprint
        selection_result = selection_result_fingerprint(projected)
        if selection_result is not None:
            authority['initial_movie_selection_result_fingerprint'] = selection_result
        from .automatic_recognition import metadata_fingerprints
        authority.update({key: value for key, value in metadata_fingerprints(projected).items() if value is not None})
        from .current_tv_refresh import marker_fingerprint
        refresh_fingerprint = marker_fingerprint(projected)
        if refresh_fingerprint is not None:
            authority['current_tv_refresh_fingerprint'] = refresh_fingerprint
        receipt = build_projection_receipt(authority)
        metadata["projection_receipt_fingerprint"] = receipt.fingerprint
        return replace(projected, metadata=metadata, projection_receipt=receipt)

    @staticmethod
    def revise(current: RenamePlan, draft: RenamePlan) -> RenamePlan:
        """Re-sign a Plan-only revision against its frozen Decision receipt."""

        if current.task_id != draft.task_id or current.projection_receipt is None:
            raise ProjectionAuthorityError("revision has no current receipt authority")
        validate_projection_receipt(current.projection_receipt)
        from .current_tv_refresh import require_marker_inheritance
        if require_marker_inheritance(current, draft) is not True:
            raise ProjectionAuthorityError('Plan-only刷新标记继承校验已断开')
        from .pending_tv_selection import metadata_fingerprint as pending_fingerprint
        if pending_fingerprint(current) is not None or pending_fingerprint(draft) is not None:
            raise ProjectionAuthorityError('季度关系待确认期间不能进行Plan-only修订')
        from .tv_boundary_completion import receipt_fingerprint
        if receipt_fingerprint(current) != receipt_fingerprint(draft):
            raise ProjectionAuthorityError('Plan-only修订不能替换季度完成凭据')
        from .initial_movie_selection import selection_result_fingerprint
        if selection_result_fingerprint(current) != selection_result_fingerprint(draft):
            raise ProjectionAuthorityError('Plan-only revision不能替换首次作品选择结果')
        from .automatic_recognition import metadata_fingerprints
        if metadata_fingerprints(current) != metadata_fingerprints(draft):
            raise ProjectionAuthorityError('Plan-only revision不能替换原自动作业或资料观察')
        if current.metadata.get('content_policy') != draft.metadata.get('content_policy'):
            raise ProjectionAuthorityError('Plan-only revision不能更换整理策略 policy')
        if _thaw(current.metadata.get('initial_source_basis')) != _thaw(draft.metadata.get('initial_source_basis')):
            raise ProjectionAuthorityError('Plan-only revision不能更换来源 basis')
        from .content_selection import validate_plan_content
        if _thaw(current.metadata.get('content_selection')) != _thaw(draft.metadata.get('content_selection')):
            raise ProjectionAuthorityError('Plan-only revision不能更换内容选择')
        validate_plan_content(draft)
        if _thaw(current.metadata.get('asset_operation')) != _thaw(draft.metadata.get('asset_operation')):
            raise ProjectionAuthorityError('Plan-only revision不能更换资产操作')
        if _thaw(current.metadata.get('completed_rebuild_observation')) != _thaw(draft.metadata.get('completed_rebuild_observation')):
            raise ProjectionAuthorityError('Plan-only revision cannot replace rebuild authority')
        authority = dict(current.projection_receipt.authority)
        metadata = dict(draft.metadata)
        authority.update({
            "target_routing_fingerprint": _fingerprint(
                [str(entry.target_path) for entry in draft.entries],
                domain="bar.target-routing",
            ),
            "naming_policy_fingerprint": _fingerprint(
                {
                    key: metadata.get(key)
                    for key in (
                        "canonical_title", "canonical_work_root", "season_number",
                        "naming_policy_version", "title_policy_version",
                    )
                },
                domain="bar.naming-policy",
            ),
            "subtitle_policy_fingerprint": _fingerprint(
                [rule.to_dict() for rule in draft.subtitle_override_rules],
                domain="bar.subtitle-policy",
            ),
            "execution_policy_fingerprint": _fingerprint(
                draft.execution_policy.to_dict()
                if draft.execution_policy is not None else {},
                domain="bar.execution-policy",
            ),
            "entry_set_fingerprint": _fingerprint(
                [entry.to_dict() for entry in draft.entries],
                domain="bar.plan-entry-set",
            ),
        })
        for field in (
            "tv_planning_authority_schema_version",
            "tv_planning_authority_policy_version",
            "tv_planning_authority_fingerprint",
            "batch_scope_fingerprint",
            "reservation_lineage_id",
            "reservation_revision",
        ):
            metadata[field] = authority[field]
        metadata["tv_planning_authority_required"] = (
            current_tv_authority
        )
        receipt = build_projection_receipt(authority)
        metadata["projection_authority_required"] = True
        metadata["projection_receipt_fingerprint"] = receipt.fingerprint
        from .generated_outputs import bind_outputs
        return bind_outputs(replace(draft, metadata=metadata, projection_receipt=receipt),
                            current.metadata.get('generated_outputs'))


def validate_plan_projection_receipt(plan: RenamePlan) -> None:
    """Recompute every Plan-owned receipt field and reject post-signing drift."""
    from .content_selection import validate_plan_content
    try:
        validate_plan_content(plan)
    except (ValueError, TypeError, KeyError) as exc:
        raise ProjectionAuthorityError(str(exc)) from exc

    receipt = plan.projection_receipt
    if receipt is None:
        raise ProjectionAuthorityError("Plan has no ProjectionReceipt")
    validate_projection_receipt(receipt)
    authority = dict(receipt.authority)
    from .generated_outputs import validate_manifest, FIELD
    if authority.get(FIELD) != validate_manifest(plan):
        raise ProjectionAuthorityError('生成文件与投影凭据不一致')
    from .current_tv_refresh import marker_fingerprint
    if authority.get('current_tv_refresh_fingerprint') != marker_fingerprint(plan):
        raise ProjectionAuthorityError('原作品刷新标记与实际投影凭据不一致')
    from .initial_movie_selection import selection_result_fingerprint
    if authority.get('initial_movie_selection_result_fingerprint') != selection_result_fingerprint(plan):
        raise ProjectionAuthorityError('首次作品选择结果与投影凭据不一致')
    from .automatic_recognition import metadata_fingerprints
    if any(authority.get(key) != value for key, value in metadata_fingerprints(plan).items()):
        raise ProjectionAuthorityError('自动作业或资料观察与投影凭据不一致')
    if authority.get('content_policy') != plan.metadata.get('content_policy'):
        raise ProjectionAuthorityError('整理策略 policy 与投影凭据不一致')
    from .work_affiliation import authority_fingerprint as affiliation_fingerprint
    if authority.get('work_affiliation_fingerprint') != affiliation_fingerprint(plan):
        raise ProjectionAuthorityError('作品归属决定与投影凭据不一致')
    raw_basis = plan.metadata.get('initial_source_basis')
    from .pending_tv_selection import metadata_fingerprint as pending_fingerprint
    if authority.get('pending_tv_selection_fingerprint') != pending_fingerprint(plan):
        raise ProjectionAuthorityError('季度待确认依据与投影凭据不一致')
    from .tv_boundary_completion import receipt_fingerprint
    if authority.get('tv_boundary_completion_receipt_fingerprint') != receipt_fingerprint(plan):
        raise ProjectionAuthorityError('季度完成凭据与投影凭据不一致')
    basis = plan.initial_source_basis if raw_basis is not None else None
    if authority.get('initial_source_basis_fingerprint') != (basis.fingerprint if basis else None):
        raise ProjectionAuthorityError('来源 basis 与投影凭据不一致')
    metadata = dict(plan.metadata)
    from .asset_operation import AssetOperation
    raw_operation = metadata.get('asset_operation')
    operation_fp = AssetOperation.from_mapping(raw_operation).fingerprint if raw_operation is not None else None
    if authority.get('asset_operation_fingerprint') != operation_fp:
        raise ProjectionAuthorityError('资产操作与投影凭据不一致')
    from .rebuild_observation import CompletedRebuildObservation
    raw_rebuild = metadata.get('completed_rebuild_observation')
    rebuild_fp = (CompletedRebuildObservation.from_mapping(_thaw(raw_rebuild)).fingerprint
                  if raw_rebuild is not None else None)
    if authority.get('completed_rebuild_fingerprint') != rebuild_fp:
        raise ProjectionAuthorityError('重建观察与投影凭据不一致')
    expected = {
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "task_id": plan.task_id,
        "decision_revision": metadata.get("decision_state_revision"),
        "decision_fingerprint": metadata.get("decision_state_fingerprint"),
        "source_ownership_fingerprint": metadata.get(
            "source_ownership_fingerprint"
        ),
        "episode_ownership_fingerprint": metadata.get(
            "task_episode_ownership_fingerprint"
        ),
        "tv_planning_authority_schema_version": metadata.get(
            "tv_planning_authority_schema_version"
        ),
        "tv_planning_authority_policy_version": metadata.get(
            "tv_planning_authority_policy_version"
        ),
        "tv_planning_authority_fingerprint": metadata.get(
            "tv_planning_authority_fingerprint"
        ),
        "batch_scope_fingerprint": metadata.get("batch_scope_fingerprint"),
        "reservation_lineage_id": metadata.get("reservation_lineage_id"),
        "reservation_revision": metadata.get("reservation_revision"),
        "work_identity_fingerprint": _fingerprint(
            plan.work_identity.to_dict() if plan.work_identity is not None else {},
            domain="bar.work-identity",
        ),
        "target_routing_fingerprint": _fingerprint(
            [str(entry.target_path) for entry in plan.entries],
            domain="bar.target-routing",
        ),
        "naming_policy_fingerprint": _fingerprint(
            {
                key: metadata.get(key)
                for key in (
                    "canonical_title", "canonical_work_root", "season_number",
                    "naming_policy_version", "title_policy_version",
                )
            },
            domain="bar.naming-policy",
        ),
        "subtitle_policy_fingerprint": _fingerprint(
            [rule.to_dict() for rule in plan.subtitle_override_rules],
            domain="bar.subtitle-policy",
        ),
        "execution_policy_fingerprint": _fingerprint(
            plan.execution_policy.to_dict()
            if plan.execution_policy is not None else {},
            domain="bar.execution-policy",
        ),
        "policy_bundle_version": PROJECTION_POLICY_VERSION,
        "projector_version": PROJECTOR_VERSION,
        "entry_set_fingerprint": _fingerprint(
            [entry.to_dict() for entry in plan.entries],
            domain="bar.plan-entry-set",
        ),
    }
    if 'series_binding_snapshot' in metadata:
        expected['series_binding_fingerprint'] = _fingerprint(
            _without_correlation(metadata['series_binding_snapshot']), domain='bar.series-binding')
    mismatched = sorted(
        key for key, value in expected.items()
        if authority.get(key) != value
    )
    if metadata.get("projection_receipt_fingerprint") != receipt.fingerprint:
        mismatched.append("projection_receipt_fingerprint")
    if mismatched:
        raise ProjectionAuthorityError(
            "ProjectionReceipt does not match Plan fields: "
            + ", ".join(sorted(set(mismatched)))
        )


PhaseHook = Callable[[str], None]


class ProjectionCommitService:
    """Commit Decision, ownership and receipted Plan behind one journal."""

    def reset_recognition(self, manager, prepared):
        from .recognition_reset import commit
        return commit(self, manager, prepared)

    def __init__(
        self,
        state_store: DecisionStateStore,
        plan_store: PlanStore,
        *,
        ownership_store: Optional[SourceOwnershipStore] = None,
        cohort_authority_store: Optional[CohortAuthorityStore] = None,
        journal_root: Optional[Path] = None,
        task_root: Optional[Path] = None,
        _phase_hook: Optional[PhaseHook] = None,
        media_archive_context=None,
        movie_change_context=None,
        initial_movie_completion_context=None,
        initial_tv_completion_context=None,
        pending_tv_selection_context=None,
    ) -> None:
        self.state_store = state_store
        self.plan_store = plan_store
        self.ownership_store = ownership_store or SourceOwnershipStore(
            plan_store.root.parent / "source_ownership"
        )
        self.cohort_authority_store = (
            cohort_authority_store
            or CohortAuthorityStore(successor_authority_root(state_store.root))
        )
        self.journal_root = Path(
            journal_root or (plan_store.root.parent / "projection_journal")
        )
        self.task_root = Path(task_root or (plan_store.root.parent / "task"))
        self._phase_hook = _phase_hook
        self.media_archive_context = media_archive_context
        self.movie_change_context = movie_change_context
        self.initial_movie_completion_context = initial_movie_completion_context
        self.initial_movie_completion_terminal = None
        self.initial_tv_completion_context = initial_tv_completion_context
        self.pending_tv_selection_context = pending_tv_selection_context
        self.initial_tv_completion_terminal = None

    def commit(
        self,
        envelope: ProjectionAuthorityEnvelope,
        *,
        task_terminal: Optional[TaskTerminalMutation] = None,
    ) -> tuple[DecisionState, RenamePlan]:
        from contextlib import nullcontext
        outer_lock = nullcontext()
        if any(value is not None for value in (envelope.initial_tv_completion,
                self.initial_tv_completion_context, envelope.movie_change, self.movie_change_context, envelope.initial_movie_selection)):
            raise ProjectionAuthorityError('TV、电影修订与首次作品选择必须通过公共命令入口')
        if envelope.initial_movie_completion is not None or self.initial_movie_completion_context is not None:
            from .initial_movie_completion import InitialMovieCompletionIntent, InitialMovieCompletionContext
            if (type(envelope.initial_movie_completion) is not InitialMovieCompletionIntent
                    or type(self.initial_movie_completion_context) is not InitialMovieCompletionContext
                    or envelope.initial_movie_completion.task_id != envelope.decision.task_id
                    or envelope.movie_change is not None or self.movie_change_context is not None
                    or envelope.initial_movie_selection is not None
                    or envelope.media_archive is not None or self.media_archive_context is not None):
                raise ProjectionAuthorityError('首次电影完成缺少双传输或混入其它事务')
            self.initial_movie_completion_terminal = task_terminal
            outer_lock = self.initial_movie_completion_context.publication_lock(self, envelope.initial_movie_completion)
        with outer_lock:
            with self._transaction_lock(envelope.decision.task_id):
                return self._commit_locked(envelope, task_terminal=task_terminal)

    def _commit_locked(
        self,
        envelope: ProjectionAuthorityEnvelope,
        *,
        task_terminal: Optional[TaskTerminalMutation],
        partition_decision=None,
    ) -> tuple[DecisionState, RenamePlan]:
        from .single_subject import require_single_subject_plan
        require_single_subject_plan(envelope.draft_plan, envelope.decision)
        if envelope.cohort_authority is not None:
            raise ProjectionAuthorityError('合包功能已退役，不能发布旧合包决定')
        if partition_decision is not None:
            raise ProjectionAuthorityError('旧二分拆卡已停用，请使用“调整计划范围”。')
        from .content_policy import bind_commit
        envelope = bind_commit(self, envelope)
        previous = envelope.previous_decision
        expected_revision = previous.revision if previous is not None else -1
        publication = getattr(self, '_public_revision', None)
        previous_plan = publication.current if publication is not None else self._load_optional_plan(envelope.decision.task_id)
        from .current_tv_refresh import validate_envelope as validate_tv_refresh
        self._current_tv_refresh_request = validate_tv_refresh(self, envelope, previous_plan)
        from .pending_tv_selection import validate_commit as validate_pending_tv
        pending_tv_publication = validate_pending_tv(self, envelope, task_terminal)
        from .tv_boundary_completion import receipt_fingerprint
        if (previous_plan is not None and (envelope.initial_tv_completion is None
                or envelope.initial_tv_completion.inputs.mode != 'boundary')
                and receipt_fingerprint(previous_plan) != receipt_fingerprint(envelope.draft_plan)):
            raise ProjectionAuthorityError('只有完整季度确认可产生完成凭据，其余事务必须继承')
        from .automatic_recognition import validate_commit as validate_automatic_commit
        automatic_publication = validate_automatic_commit(self, envelope, task_terminal)
        from .content_correction import validate_commit as validate_content_correction
        correction_result = validate_content_correction(self, envelope, task_terminal)
        from .initial_movie_selection import selection_result_fingerprint
        if (envelope.initial_movie_selection is None
                and selection_result_fingerprint(previous_plan) != selection_result_fingerprint(envelope.draft_plan)):
            raise ProjectionAuthorityError('仅实际首次作品交接可产生选择结果，普通修订必须原样继承')
        from .content_choice import validate_commit as validate_content_commit
        validate_content_commit(self, envelope, automatic_publication=automatic_publication,
                                correction_result=correction_result, pending_tv_publication=pending_tv_publication, current_plan=previous_plan)
        written: Optional[DecisionState] = None
        ownership: Optional[SourceOwnershipRecord] = None
        prepared_authority: Optional[CohortAuthority] = None
        previous_cohort_authority: Optional[CohortAuthority] = None
        previous_ownership = self.ownership_store.load_optional(
            envelope.decision.task_id
        )
        identities = self._freeze_source_identities(
            envelope.draft_plan.expected_sources,
            inventory_fingerprint=envelope.inventory_fingerprint,
            scope=self._stored_source_scope(envelope.decision.task_id),
        )
        replay_guard = envelope.inventory_replay_guard
        if envelope.draft_plan.metadata.get('inventory_retry_guard_required') and replay_guard is None:
            raise ProjectionAuthorityError('库存重试缺少冻结提交守卫')
        if replay_guard is not None:
            replay_guard.validate(previous, self.state_store.load_optional(envelope.decision.task_id),
                                  previous_plan, identities)
            metadata = dict(envelope.draft_plan.metadata)
            metadata.pop('inventory_retry_guard_required', None)
            envelope = replace(envelope, draft_plan=replace(envelope.draft_plan, metadata=metadata))
        from .asset_operation import freeze_movie_operation
        material_plan = (envelope.draft_plan if publication is not None else
                         DecisionStateStore.project_plan(envelope.decision, envelope.draft_plan))
        envelope = replace(envelope, decision=freeze_movie_operation(envelope.decision,
            material_plan, self.plan_store.root.parent))
        prepared_decision = self.state_store.prepare_save(
            envelope.decision,
            expected_revision=expected_revision,
        )
        if envelope.cohort_authority is not None:
            try:
                prepared_authority, previous_cohort_authority = (
                    self.cohort_authority_store.preview(
                        envelope.cohort_authority,
                        prepared_decision,
                    )
                )
            except SuccessorAuthorityError as exc:
                raise ProjectionAuthorityError(
                    f"successor authority preparation failed: {exc.reason_code}"
                ) from exc
        from .media_archive import archive_required, validate_archive_commit
        archive = validate_archive_commit(self, envelope, task_terminal)
        if archive is None and (envelope.media_archive is not None or archive_required(envelope, task_terminal)):
            raise ProjectionAuthorityError('媒体历史提交校验已断开')
        if archive is not None:
            archive.publish(self.media_archive_context)
            self._checkpoint('media_history_archived')
            # Archival is observable I/O. Recheck the same shared evidence
            # before journalling a new epoch, including completed source reentry.
            if validate_archive_commit(self, envelope, task_terminal) != archive:
                raise ProjectionAuthorityError('归档后的媒体历史核查已断开或变化')
        self._prepare_journal(
            envelope.decision.task_id,
            authority_input_fingerprint=_fingerprint(
                {
                    "decision": envelope.decision.to_dict(),
                    "inventory_fingerprint": envelope.inventory_fingerprint,
                    "episode_ownership": [
                        item.to_dict() for item in envelope.episode_ownership
                    ],
                    "cohort_authority": (
                        envelope.cohort_authority.to_dict()
                        if envelope.cohort_authority is not None else None
                    ),
                },
                domain="bar.projection-commit-input",
            ),
            source_lock_keys=tuple(
                sorted(
                    {
                        key
                        for identity in identities
                        for key in (
                            f"location:{identity.location_key}",
                            f"physical:{identity.physical_key}",
                        )
                    }
                )
            ),
            previous_decision=previous,
            previous_plan=previous_plan,
            previous_ownership=previous_ownership,
            previous_cohort_authority=previous_cohort_authority,
            intended_cohort_authority=prepared_authority,
            task_terminal=task_terminal,
        )
        self._write_journal(
            envelope.decision.task_id,
            "prepared",
            "pending",
            extra={
                "current_decision_fingerprint": self.state_store.fingerprint(
                    prepared_decision
                ),
                "current_tv_refresh_request": (self._current_tv_refresh_request.fingerprint
                    if self._current_tv_refresh_request is not None else None),
                "current_tv_refresh_parent": (self._current_tv_refresh_request.to_dict()['parent_plan_fingerprint']
                    if self._current_tv_refresh_request is not None else None),
            },
        )
        try:
            self._checkpoint("prepared")
            written = self.state_store.save_prepared(
                prepared_decision,
                expected_revision=expected_revision,
            )
            envelope = replace(envelope, decision=written)
            self._write_journal(
                written.task_id,
                "decision_written",
                "pending",
                extra={
                    "current_decision_fingerprint": self.state_store.fingerprint(
                        written
                    ),
                    **(
                        {
                            "current_cohort_authority_id": (
                                prepared_authority.authority_id
                            ),
                            "current_cohort_authority_fingerprint": (
                                prepared_authority.authority_fingerprint
                            ),
                        }
                        if prepared_authority is not None else {}
                    ),
                },
            )
            if prepared_authority is not None:
                expected_previous_fingerprint = (
                    previous_cohort_authority.authority_fingerprint
                    if previous_cohort_authority is not None else ""
                )
                try:
                    published = self.cohort_authority_store.publish_prepared(
                        prepared_authority,
                        expected_previous_fingerprint=(
                            expected_previous_fingerprint
                        ),
                    )
                except SuccessorAuthorityError as exc:
                    raise ProjectionAuthorityError(
                        f"successor authority publish failed: {exc.reason_code}"
                    ) from exc
                if (
                    published.authority_fingerprint
                    != prepared_authority.authority_fingerprint
                ):
                    raise ProjectionAuthorityError(
                        "successor authority publish fingerprint drifted"
                    )
            self._checkpoint("decision_written")
            intended_ownership = self.ownership_store.preview_claim(
                task_id=written.task_id,
                decision_revision=written.revision,
                inventory_fingerprint=envelope.inventory_fingerprint,
                identities=identities,
            )
            self._write_journal(
                written.task_id,
                "decision_written",
                "pending",
                extra={
                    "current_ownership_fingerprint": intended_ownership.fingerprint,
                },
            )
            ownership = self.ownership_store.claim(
                task_id=written.task_id,
                decision_revision=written.revision,
                inventory_fingerprint=envelope.inventory_fingerprint,
                identities=identities,
            )
            if ownership.fingerprint != intended_ownership.fingerprint:
                raise ProjectionAuthorityError(
                    "source ownership changed before journaled claim"
                )
            self._write_journal(
                written.task_id,
                "ownership_claimed",
                "pending",
                extra={
                    "current_ownership_fingerprint": ownership.fingerprint,
                },
            )
            self._checkpoint("ownership_claimed")
            plan = FinalPlanProjector.project(envelope, ownership)
            from .generated_outputs import seal_outputs
            plan = seal_outputs(plan)
            self._write_journal(
                written.task_id,
                "ownership_claimed",
                "pending",
                extra={
                    "current_plan_fingerprint": self.plan_store.fingerprint(plan),
                    "authority_fingerprint": plan.projection_receipt.fingerprint,
                },
            )
            self._save_plan(plan)
            self._write_journal(
                written.task_id,
                "plan_written",
                "pending",
                extra={
                    "current_plan_fingerprint": self.plan_store.fingerprint(plan),
                    "authority_fingerprint": plan.projection_receipt.fingerprint,
                },
            )
            self._checkpoint("plan_written")
            current_task_sha256 = self._commit_task_terminal(
                written, plan, task_terminal
            )
            self._write_journal(written.task_id, "task_terminal_written", "pending")
            if current_task_sha256:
                self._write_journal(
                    written.task_id,
                    "task_terminal_written",
                    "pending",
                    extra={"current_task_sha256": current_task_sha256},
                )
            self._checkpoint("task_terminal_written")
            self._write_journal(written.task_id, "completed", "completed")
            self._checkpoint("completed")
            return written, plan
        except (KeyboardInterrupt, SystemExit):
            # A process-level interruption deliberately leaves a pending,
            # verifiable journal for the next instance to recover.
            raise
        except BaseException:
            self._rollback_pending(envelope.decision.task_id)
            self._write_journal(
                envelope.decision.task_id,
                self._journal_phase(envelope.decision.task_id),
                "failed_closed",
            )
            raise

    def recover(self, task_id: str) -> str:
        with self._transaction_lock(task_id):
            journal = self._read_journal(task_id)
            status = str(journal.get("status") or "")
            if status in {"completed", "failed_closed"}:
                return status
            if not journal:
                raise ProjectionAuthorityError("projection recovery journal is missing")
            self._rollback_pending(task_id)
            self._write_journal(
                task_id,
                str(journal.get("phase") or "prepared"),
                "failed_closed",
            )
            return "failed_closed"

    def recover_pending(self) -> Mapping[str, str]:
        """Fail closed every pending journal before the queue accepts work."""

        if not self.journal_root.exists():
            return {}
        outcomes: dict[str, str] = {}
        failures: list[str] = []
        for path in sorted(self.journal_root.glob("*.json")):
            task_id = path.stem
            try:
                journal = self._read_journal(task_id)
                if str(journal.get("status") or "") != "pending":
                    continue
                outcomes[task_id] = self.recover(task_id)
            except BaseException as exc:
                failures.append(f"{task_id}: {exc}")
        if failures:
            raise ProjectionAuthorityError(
                "projection startup recovery failed: " + "; ".join(failures)
            )
        return outcomes

    def commit_source_partition_revision(
        self,
        decision: DecisionState,
        draft_plan: RenamePlan,
        *,
        before_plan_save: Callable[[RenamePlan], None],
        partition_decision=None,
    ) -> tuple[DecisionState, RenamePlan]:
        """Retired: no alternate writer or automatic compatibility fallback."""
        raise ProjectionAuthorityError('旧二分拆卡已停用，请使用“调整计划范围”；无法处理的旧任务请安全结束后重新添加。')

    def commit_revision(
        self,
        decision: DecisionState,
        draft_plan: RenamePlan,
        *,
        task_terminal: Optional[TaskTerminalMutation] = None,
    ) -> RenamePlan:
        """Persist a revision without inferring or advancing Decision authority."""

        with self._transaction_lock(decision.task_id):
            return self._commit_revision_locked(
                decision, draft_plan, task_terminal=task_terminal
            )

    def _commit_revision_locked(
        self,
        decision: DecisionState,
        draft_plan: RenamePlan,
        *,
        task_terminal: Optional[TaskTerminalMutation],
    ) -> RenamePlan:

        if decision.task_id != draft_plan.task_id:
            raise ProjectionAuthorityError("Decision and Plan task ids differ")
        current = self.state_store.load(decision.task_id)
        if current.pending_tv_selection is not None:
            raise ProjectionAuthorityError('季度关系待确认期间不能进行Plan-only修订')
        if self.state_store.fingerprint(current) != self.state_store.fingerprint(decision):
            raise ProjectionAuthorityError("Decision changed before Plan revision")
        if current.content_policy is not None and current.initial_source_basis is None:
            raise ProjectionAuthorityError('来源 basis 首次安装需要完整Decision与Plan重新生成')
        previous_plan = None
        try:
            previous_plan = self.plan_store.load(draft_plan.task_id)
        except Exception:
            previous_plan = None
        inventory_fingerprint = inventory_fingerprint_for_plan(draft_plan)
        envelope = ProjectionAuthorityEnvelope(
            previous_decision=current,
            decision=current,
            draft_plan=draft_plan,
            inventory_fingerprint=inventory_fingerprint,
            episode_ownership=_episode_rows_from_plan(draft_plan),
        )
        from .current_tv_refresh import validate_envelope as validate_tv_refresh
        self._current_tv_refresh_request = validate_tv_refresh(self, envelope, previous_plan)
        from .content_choice import validate_commit as validate_content_commit
        from .content_selection import validate_plan_content
        from .content_policy import bind_commit
        envelope = bind_commit(self, envelope)
        validate_content_commit(self, envelope)
        validate_plan_content(draft_plan, current)
        ownership = None
        previous_ownership = self.ownership_store.load_optional(decision.task_id)
        identities = self._freeze_source_identities(
            draft_plan.expected_sources,
            inventory_fingerprint=inventory_fingerprint,
            scope=self._stored_source_scope(decision.task_id),
        )
        self._prepare_journal(
            decision.task_id,
            authority_input_fingerprint=_fingerprint(
                {
                    "decision": decision.to_dict(),
                    "inventory_fingerprint": inventory_fingerprint,
                    "episode_ownership": [
                        item.to_dict() for item in envelope.episode_ownership
                    ],
                },
                domain="bar.projection-revision-input",
            ),
            source_lock_keys=tuple(
                sorted(
                    {
                        key
                        for identity in identities
                        for key in (
                            f"location:{identity.location_key}",
                            f"physical:{identity.physical_key}",
                        )
                    }
                )
            ),
            previous_decision=current,
            previous_plan=previous_plan,
            previous_ownership=previous_ownership,
            previous_cohort_authority=None,
            intended_cohort_authority=None,
            task_terminal=task_terminal,
        )
        try:
            self._checkpoint("prepared")
            self._write_journal(decision.task_id, "decision_written", "pending")
            self._checkpoint("decision_written")
            intended_ownership = self.ownership_store.preview_claim(
                task_id=decision.task_id,
                decision_revision=decision.revision,
                inventory_fingerprint=inventory_fingerprint,
                identities=identities,
            )
            self._write_journal(
                decision.task_id,
                "decision_written",
                "pending",
                extra={
                    "current_ownership_fingerprint": intended_ownership.fingerprint,
                },
            )
            ownership = self.ownership_store.claim(
                task_id=decision.task_id,
                decision_revision=decision.revision,
                inventory_fingerprint=inventory_fingerprint,
                identities=identities,
            )
            if ownership.fingerprint != intended_ownership.fingerprint:
                raise ProjectionAuthorityError(
                    "source ownership changed before journaled claim"
                )
            self._write_journal(
                decision.task_id,
                "ownership_claimed",
                "pending",
                extra={"current_ownership_fingerprint": ownership.fingerprint},
            )
            self._checkpoint("ownership_claimed")
            projected = FinalPlanProjector.project(envelope, ownership)
            from .generated_outputs import seal_outputs
            projected = seal_outputs(projected)
            self._write_journal(
                decision.task_id,
                "ownership_claimed",
                "pending",
                extra={
                    "current_plan_fingerprint": self.plan_store.fingerprint(
                        projected
                    ),
                    "authority_fingerprint": (
                        projected.projection_receipt.fingerprint
                    ),
                },
            )
            self._save_plan(projected)
            self._write_journal(
                decision.task_id,
                "plan_written",
                "pending",
                extra={
                    "current_plan_fingerprint": self.plan_store.fingerprint(
                        projected
                    ),
                    "authority_fingerprint": (
                        projected.projection_receipt.fingerprint
                    ),
                },
            )
            self._checkpoint("plan_written")
            current_task_sha256 = self._commit_task_terminal(
                decision, projected, task_terminal
            )
            self._write_journal(decision.task_id, "task_terminal_written", "pending")
            if current_task_sha256:
                self._write_journal(
                    decision.task_id,
                    "task_terminal_written",
                    "pending",
                    extra={"current_task_sha256": current_task_sha256},
                )
            self._checkpoint("task_terminal_written")
            self._write_journal(decision.task_id, "completed", "completed")
            self._checkpoint("completed")
            return projected
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            self._rollback_pending(decision.task_id)
            self._write_journal(
                decision.task_id,
                self._journal_phase(decision.task_id),
                "failed_closed",
            )
            raise

    def _checkpoint(self, phase: str) -> None:
        if self._phase_hook is not None:
            self._phase_hook(phase)

    def _stored_source_scope(self, task_id):
        from .task_batch_scope import TaskSourceScope
        task = read_task_at(self.task_root, task_id)
        raw = task.get('task_source_scope')
        scope = TaskSourceScope.from_mapping(raw) if raw is not None else None
        if scope is not None and scope.task_id != task_id:
            raise ProjectionAuthorityError('持久来源清单不属于当前任务')
        return scope

    @staticmethod
    def _freeze_source_identities(
        paths: Iterable[Path],
        *,
        inventory_fingerprint: str,
        scope=None,
    ) -> tuple[SourceIdentity, ...]:
        if scope is not None:
            available = {Path(row.source_path): row for row in scope.source_identities}
            try:
                return tuple(SourceIdentity(Path(row.source_path), row.location_key, row.physical_key,
                    row.size, row.mtime_ns, inventory_fingerprint)
                    for path in paths for row in (available[Path(path)],))
            except KeyError as exc:
                raise ProjectionAuthorityError('计划来源越出持久清单') from exc
        identities = []
        for path in paths:
            try:
                identities.append(
                    SourceIdentity.from_path(
                        Path(path),
                        inventory_fingerprint=inventory_fingerprint,
                    )
                )
            except (OSError, ProjectionAuthorityError) as exc:
                raise ProjectionAuthorityError(
                    f"projection source 缺失或物理身份不可用：{path}"
                ) from exc
        return tuple(identities)

    def _lock_resource(self, task_id):
        from .file_lock import LockResource
        from ..utils.utils import safe_task_path
        safe_task_path(self.task_root, task_id)
        return LockResource(self, self.journal_root / f'.{task_id}.projection.lock', f'projection_commit:{task_id}')

    @contextmanager
    def _transaction_lock(self, task_id: str):
        from contextlib import ExitStack
        from .file_lock import resource_guard
        with ExitStack() as stack:
            try:
                stack.enter_context(resource_guard(
                    self._lock_resource(task_id),
                    owner={"kind": "projection_commit", "task_id": task_id},
                ))
            except FileLockBusyError as exc:
                raise ProjectionAuthorityError(
                    "projection transaction is already active"
                ) from exc
            yield

    def _load_optional_plan(self, task_id: str) -> Optional[RenamePlan]:
        path = self.plan_store.path_for(task_id)
        return self.plan_store.load(task_id) if path.exists() else None

    def _prepare_journal(
        self,
        task_id: str,
        *,
        authority_input_fingerprint: str,
        source_lock_keys: tuple[str, ...],
        previous_decision: Optional[DecisionState],
        previous_plan: Optional[RenamePlan],
        previous_ownership: Optional[SourceOwnershipRecord],
        previous_cohort_authority: Optional[CohortAuthority],
        intended_cohort_authority: Optional[CohortAuthority],
        task_terminal: Optional[TaskTerminalMutation],
    ) -> None:
        existing = self._read_journal(task_id)
        if existing and existing.get('status') not in {'completed', 'failed_closed'}:
            raise ProjectionAuthorityError('projection transaction is pending; 请先解决原事务恢复冲突')
        if task_terminal is not None:
            if task_terminal.task_id != task_id:
                raise ProjectionAuthorityError("task terminal mutation task id differs")
            if task_terminal.task_root.resolve(strict=False) != self.task_root.resolve(
                strict=False
            ):
                raise ProjectionAuthorityError("task terminal mutation root differs")
            actual_bytes, _task, actual_fingerprint = task_record_snapshot_at(self.task_root, task_id)
            if (actual_bytes != task_terminal.previous_bytes
                    or actual_fingerprint != task_terminal.expected_sha256):
                raise ProjectionAuthorityError('任务在事务首写前已变化，保留后来记录并重新生成计划')
        attempt_id = uuid.uuid4().hex
        backup_root = self.journal_root / "backups"
        backup_path = backup_root / f"{task_id}.{attempt_id}.rollback.json"
        rollback_payload = {
            "schema_version": 1,
            "task_id": task_id,
            "previous_decision": (
                previous_decision.to_dict() if previous_decision is not None else None
            ),
            "previous_plan": (
                previous_plan.to_dict() if previous_plan is not None else None
            ),
            "previous_ownership": (
                previous_ownership.to_dict()
                if previous_ownership is not None else None
            ),
            "previous_cohort_authority": (
                previous_cohort_authority.to_dict()
                if previous_cohort_authority is not None else None
            ),
            "intended_cohort_authority_id": (
                intended_cohort_authority.authority_id
                if intended_cohort_authority is not None else None
            ),
            "previous_task_bytes_b64": (
                base64.b64encode(task_terminal.previous_bytes).decode("ascii")
                if task_terminal is not None
                and task_terminal.previous_bytes is not None
                else None
            ),
            "previous_task_sha256": (
                task_terminal.expected_sha256
                if task_terminal is not None else None
            ),
        }
        self._atomic_json_write(backup_path, rollback_payload)
        backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        self._write_journal(
            task_id,
            "prepared",
            "pending",
            reset=True,
            extra={
                "authority_input_fingerprint": authority_input_fingerprint,
                "task_lock_key": f"task:{task_id}",
                "source_lock_keys": list(source_lock_keys),
                "lock_order": ["task", "source"],
                "attempt_id": attempt_id,
                "rollback_ref": f"backups/{backup_path.name}",
                "rollback_sha256": backup_sha256,
                "task_terminal_required": task_terminal is not None,
            },
        )

    def _rollback_pending(self, task_id: str) -> None:
        journal = self._read_journal(task_id)
        reference = str(journal.get("rollback_ref") or "")
        expected_sha256 = str(journal.get("rollback_sha256") or "")
        if not reference or _HEX64.fullmatch(expected_sha256) is None:
            raise ProjectionAuthorityError("projection rollback reference is missing")
        backup_root = (self.journal_root / "backups").resolve(strict=False)
        backup_path = (self.journal_root / reference).resolve(strict=False)
        if backup_path.parent != backup_root or not backup_path.is_file():
            raise ProjectionAuthorityError("projection rollback reference is unsafe")
        if hashlib.sha256(backup_path.read_bytes()).hexdigest() != expected_sha256:
            raise ProjectionAuthorityError("projection rollback reference is corrupt")
        try:
            payload = json.loads(backup_path.read_text("utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ProjectionAuthorityError(
                "projection rollback payload is unreadable"
            ) from exc
        if not isinstance(payload, Mapping) or payload.get("task_id") != task_id:
            raise ProjectionAuthorityError("projection rollback payload is invalid")

        # A later task edit must not strand this transaction's published
        # cross-task authority. Restore only our exact cohort publication first;
        # a newer legitimate authority is still preserved and stops recovery.
        self._rollback_cohort_authority(journal, payload)

        expected_task = str(journal.get("current_task_sha256") or "")
        if expected_task:
            previous_task_sha256 = str(
                payload.get("previous_task_sha256") or ""
            )
            encoded = payload.get("previous_task_bytes_b64")
            try:
                previous_task = (
                    base64.b64decode(str(encoded), validate=True)
                    if encoded is not None else None
                )
            except (TypeError, ValueError) as exc:
                raise ProjectionAuthorityError(
                    "projection task rollback payload is invalid"
                ) from exc
            try:
                _raw, _current, current_task_sha256 = task_record_snapshot_at(
                    self.task_root, task_id
                )
                if current_task_sha256 == expected_task:
                    restore_task_record_at_if_sha256(
                        self.task_root,
                        task_id,
                        expected_current_sha256=expected_task,
                        previous=previous_task,
                    )
                elif current_task_sha256 != previous_task_sha256:
                    raise RuntimeError(
                        "task record differs from both transaction states"
                    )
            except (OSError, RuntimeError, ValueError) as exc:
                raise ProjectionAuthorityError(
                    "task record changed before projection recovery"
                ) from exc

        previous_plan_payload = payload.get("previous_plan")
        previous_plan = (
            RenamePlan.from_dict(previous_plan_payload)
            if isinstance(previous_plan_payload, Mapping) else None
        )
        expected_plan = str(journal.get("current_plan_fingerprint") or "")
        if expected_plan:
            current_path = self.plan_store.path_for(task_id)
            if current_path.exists():
                current_plan = self.plan_store.load(task_id)
                current_fingerprint = self.plan_store.fingerprint(current_plan)
                previous_fingerprint = (
                    self.plan_store.fingerprint(previous_plan)
                    if previous_plan is not None else ""
                )
                if current_fingerprint == expected_plan:
                    if previous_plan is None:
                        self.plan_store.delete_current(task_id)
                    else:
                        self.plan_store.restore_current(previous_plan)
                elif current_fingerprint != previous_fingerprint:
                    raise ProjectionAuthorityError(
                        "current Plan changed before projection recovery"
                    )
            elif previous_plan is not None:
                self.plan_store.restore_current(previous_plan)

        previous_ownership_payload = payload.get("previous_ownership")
        previous_ownership = (
            SourceOwnershipRecord.from_dict(previous_ownership_payload)
            if isinstance(previous_ownership_payload, Mapping) else None
        )
        expected_ownership = str(
            journal.get("current_ownership_fingerprint") or ""
        )
        if expected_ownership:
            current_ownership = self.ownership_store.load_optional(task_id)
            previous_fingerprint = (
                previous_ownership.fingerprint
                if previous_ownership is not None else ""
            )
            current_fingerprint = (
                current_ownership.fingerprint
                if current_ownership is not None else ""
            )
            if current_fingerprint == expected_ownership:
                assert current_ownership is not None
                self.ownership_store.restore_exact(
                    current_ownership, previous_ownership
                )
            elif current_fingerprint != previous_fingerprint:
                raise ProjectionAuthorityError(
                    "source ownership changed before projection recovery"
                )

        previous_decision_payload = payload.get("previous_decision")
        previous_decision = (
            DecisionState.from_dict(previous_decision_payload)
            if isinstance(previous_decision_payload, Mapping) else None
        )
        expected_decision = str(
            journal.get("current_decision_fingerprint") or ""
        )
        if expected_decision:
            current_decision = self.state_store.load_optional(task_id)
            previous_fingerprint = (
                self.state_store.fingerprint(previous_decision)
                if previous_decision is not None else ""
            )
            current_fingerprint = (
                self.state_store.fingerprint(current_decision)
                if current_decision is not None else ""
            )
            if current_fingerprint == expected_decision:
                self.state_store.restore_recovery(
                    task_id,
                    expected_current_fingerprint=expected_decision,
                    previous=previous_decision,
                )
            elif current_fingerprint != previous_fingerprint:
                raise ProjectionAuthorityError(
                    "DecisionState changed before projection recovery"
                )

    def _rollback_cohort_authority(self, journal, payload):
        previous_raw = payload.get('previous_cohort_authority')
        previous = CohortAuthority.from_dict(previous_raw) if isinstance(previous_raw, Mapping) else None
        expected = str(journal.get('current_cohort_authority_fingerprint') or '')
        authority_id = str(journal.get('current_cohort_authority_id') or payload.get('intended_cohort_authority_id') or '')
        if not expected:
            return
        try:
            current = self.cohort_authority_store.load_optional(authority_id)
            actual = current.authority_fingerprint if current is not None else ''
            before = previous.authority_fingerprint if previous is not None else ''
            if actual == expected:
                self.cohort_authority_store.restore_exact(current, previous)
            elif actual != before:
                raise ProjectionAuthorityError('successor authority changed before projection recovery')
        except SuccessorAuthorityError as exc:
            raise ProjectionAuthorityError('successor authority rollback failed') from exc

    def _save_plan(self, plan: RenamePlan) -> None:
        """The sole production call site for the storage primitive."""

        self.plan_store.save(plan)

    def _commit_task_terminal(
        self,
        decision: DecisionState,
        plan: RenamePlan,
        mutation: Optional[TaskTerminalMutation],
    ) -> str:
        if mutation is None:
            return ""
        payload = mutation.render(decision, plan)
        from .current_tv_refresh import validate_terminal as validate_tv_refresh_terminal
        validate_tv_refresh_terminal(self, mutation.previous_payload, payload, decision, plan)
        from .pending_tv_selection import validate_terminal as validate_pending_terminal
        validate_pending_terminal(mutation.previous_payload, payload, decision, plan)
        from .tv_boundary_completion import validate_terminal as validate_boundary_terminal
        validate_boundary_terminal(mutation.previous_payload, payload, decision, plan)
        if self.initial_tv_completion_context is not None:
            binding = decision.season_binding or {}
            if (any(type(payload.get(key)) is not int or payload[key] != binding.get('season_number')
                    for key in ('season_id', 'season_number'))
                    or payload.get('season_source') != binding.get('decision_source')):
                raise ProjectionAuthorityError('TV任务终态季度与本次已裁决季度不一致')
        predicted_sha256 = task_record_serialized_sha256(payload)
        # Persist the intended post-write hash before the CAS.  Recovery can
        # therefore distinguish "write never happened" from "write happened
        # but the process died before advancing the journal phase".
        self._write_journal(
            decision.task_id,
            "plan_written",
            "pending",
            extra={"current_task_sha256": predicted_sha256},
        )
        try:
            written_sha256 = replace_task_record_at_if_sha256(
                self.task_root,
                decision.task_id,
                expected_sha256=mutation.expected_sha256,
                data=payload,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProjectionAuthorityError(
                "task terminal mutation could not be committed"
            ) from exc
        if written_sha256 != predicted_sha256:
            raise ProjectionAuthorityError(
                "task terminal mutation serializer fingerprint drifted"
            )
        return written_sha256

    def _journal_path(self, task_id: str) -> Path:
        return self.journal_root / f"{task_id}.json"

    def _journal_phase(self, task_id: str) -> str:
        return str(self._read_journal(task_id).get("phase") or "prepared")

    def _read_journal(self, task_id: str) -> Mapping[str, object]:
        path = self._journal_path(task_id)
        if not path.exists():
            return {}
        value = json.loads(path.read_text("utf-8"))
        return value if isinstance(value, Mapping) else {}

    def _write_journal(
        self,
        task_id: str,
        phase: str,
        status: str,
        *,
        extra: Optional[Mapping[str, object]] = None,
        reset: bool = False,
    ) -> None:
        if phase not in PROJECTION_COMMIT_PHASES:
            raise ProjectionAuthorityError("projection journal phase is invalid")
        self.journal_root.mkdir(parents=True, exist_ok=True)
        destination = self._journal_path(task_id)
        payload = {} if reset else dict(self._read_journal(task_id))
        payload.update({
            "schema_version": 2,
            "task_id": task_id,
            "phase": phase,
            "status": status,
        })
        if extra is not None:
            payload.update(dict(extra))
        self._atomic_json_write(destination, payload)

    @staticmethod
    def _atomic_json_write(destination: Path, payload: Mapping[str, object]) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "x", encoding="utf-8") as handle:
                json.dump(dict(payload), handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def inventory_fingerprint_for_plan(plan: RenamePlan) -> str:
    return _fingerprint(
        {
            "source_root": str(plan.source_root),
            "expected_sources": [str(path) for path in plan.expected_sources],
        },
        domain="bar.plan-inventory",
    )


def _episode_rows_from_plan(plan: RenamePlan) -> tuple[TaskEpisodeOwnership, ...]:
    raw_rows = plan.metadata.get("task_episode_ownership", ())
    if not isinstance(raw_rows, (list, tuple)):
        return ()
    return tuple(
        TaskEpisodeOwnership.from_mapping(item)
        for item in raw_rows
        if isinstance(item, Mapping)
    )


def project_source_partition_revision(
    decision: DecisionState,
    draft_plan: RenamePlan,
    ownership: SourceOwnershipRecord,
) -> RenamePlan:
    """Retired pure builder; cannot issue a legacy partition projection."""
    raise ProjectionAuthorityError('旧二分拆卡已停用，请使用“调整计划范围”。')


def commit_projected_plan(
    state_store: DecisionStateStore,
    plan_store: PlanStore,
    *,
    previous: Optional[DecisionState],
    candidate: DecisionState,
    draft_plan: RenamePlan,
    task_terminal: Optional[TaskTerminalMutation] = None,
    cohort_authority: Optional[CohortAuthorityDraft] = None,
    inventory_replay_guard: Optional[InventoryReplayGuard] = None,
    media_archive=None,
    media_archive_context=None,
    initial_movie_selection=None,
    initial_movie_completion=None,
    initial_movie_completion_context=None,
    pending_tv_selection=None,
    pending_tv_selection_context=None,
    automatic_recognition=None,
    content_correction=None,
    initial_default=None,
) -> tuple[DecisionState, RenamePlan]:
    envelope = ProjectionAuthorityEnvelope(
        previous_decision=previous,
        decision=candidate,
        draft_plan=draft_plan,
        inventory_fingerprint=inventory_fingerprint_for_plan(draft_plan),
        episode_ownership=_episode_rows_from_plan(draft_plan),
        cohort_authority=cohort_authority,
        inventory_replay_guard=inventory_replay_guard,
        media_archive=media_archive,
        initial_movie_selection=initial_movie_selection,
        initial_movie_completion=initial_movie_completion,
        pending_tv_selection=pending_tv_selection,
        automatic_recognition=automatic_recognition,
        content_correction=content_correction,
        initial_default=initial_default,
    )
    return ProjectionCommitService(
        state_store,
        plan_store,
        task_root=(task_terminal.task_root if task_terminal is not None else None),
        media_archive_context=media_archive_context,
        initial_movie_completion_context=initial_movie_completion_context,
        pending_tv_selection_context=pending_tv_selection_context,
    ).commit(envelope, task_terminal=task_terminal)


def commit_recognition_projection(
    state_store: DecisionStateStore,
    plan_store: PlanStore,
    recognition: (
        RecognitionDecisionInput
        | UnresolvedRecognitionInput
        | ProviderReselectionPendingInput
    ),
    draft_plan: RenamePlan,
    *,
    state_transform: Optional[Callable[[DecisionState], DecisionState]] = None,
    task_terminal: Optional[TaskTerminalMutation] = None,
    inventory_replay_guard: Optional[InventoryReplayGuard] = None,
) -> tuple[DecisionState, RenamePlan]:
    """Commit explicit recognition authority without deriving it from a Plan."""

    if recognition.task_id != draft_plan.task_id:
        raise ProjectionAuthorityError("recognition and Plan task ids differ")
    previous = state_store.load_optional(draft_plan.task_id)
    candidate = recognition.candidate(previous)
    if state_transform is not None:
        candidate = state_transform(candidate)
        if candidate.task_id != draft_plan.task_id:
            raise ProjectionAuthorityError("decision transform changed task id")
    return commit_projected_plan(
        state_store,
        plan_store,
        previous=previous,
        candidate=candidate,
        draft_plan=draft_plan,
        task_terminal=task_terminal,
        inventory_replay_guard=inventory_replay_guard,
    )


def commit_provider_reselection_pending(
    state_store: DecisionStateStore,
    plan_store: PlanStore,
    reselection: ProviderReselectionPendingInput,
    draft_plan: RenamePlan,
    *,
    task_terminal: TaskTerminalMutation,
) -> tuple[DecisionState, RenamePlan]:
    """Atomically supersede old work authority with an unresolved fresh Plan."""

    return commit_recognition_projection(
        state_store,
        plan_store,
        reselection,
        draft_plan,
        task_terminal=task_terminal,
    )


def persist_plan_revision(
    state_store: DecisionStateStore,
    plan_store: PlanStore,
    draft_plan: RenamePlan,
    *,
    task_terminal: Optional[TaskTerminalMutation] = None,
) -> RenamePlan:
    decision = state_store.load(draft_plan.task_id)
    return ProjectionCommitService(
        state_store,
        plan_store,
        task_root=(task_terminal.task_root if task_terminal is not None else None),
    ).commit_revision(
        decision,
        draft_plan,
        task_terminal=task_terminal,
    )
