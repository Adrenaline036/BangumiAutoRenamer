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
from .file_lock import FileLockBusyError, advisory_file_lock
from .font_archive_contract import is_supported_font_archive
from .physical_identity import (
    PhysicalIdentityUnavailable,
    canonical_location_key,
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
from .tv_planning_authority import TVPlanningAuthority
from .utils import is_font_archive
from ..utils.utils import (
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
    "font_policy_fingerprint",
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
    "naming_authority": "decision_ui_only",
    "season_assignments": "decision_ui_only",
    "selected_season_number": "typed_plan_transform",
    "episode_range_ownership": "decision_ui_only",
    "file_decisions": "decision_ui_only",
    "non_regular_file_decisions": "decision_ui_only",
    "associated_asset_manifest": "typed_plan_transform",
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


def _is_tv_font_plan_entry(entry) -> bool:
    return is_font_archive(entry.source_path) or (
        entry.role == "font"
        and entry.media_kind == "associated"
        and is_supported_font_archive(entry.source_path)
    )


def _validate_projected_tv_font_manifest(
    manifest: Mapping[str, object],
    plan: RenamePlan,
) -> None:
    """Validate the typed Plan shape without reading current media state."""

    if manifest.get("schema_version") != 1:
        raise ProjectionAuthorityError("TV font Plan manifest schema is invalid")
    rows = manifest.get("assets")
    if not isinstance(rows, (list, tuple)):
        raise ProjectionAuthorityError("TV font Plan manifest assets are invalid")
    metadata = plan.metadata
    canonical_root = str(metadata.get("canonical_work_root") or "")
    season_number = metadata.get("season_number")
    if not canonical_root or not isinstance(season_number, int) or isinstance(
        season_number, bool
    ):
        raise ProjectionAuthorityError(
            "TV font Plan manifest has no canonical work/season authority"
        )
    associated_root = Path(canonical_root) / "Associated"
    font_entries = {
        (_lexical_path_key(entry.source_path), _lexical_path_key(entry.target_path)):
            entry
        for entry in plan.entries
        if _is_tv_font_plan_entry(entry)
    }
    if len(font_entries) != sum(
        1 for entry in plan.entries if _is_tv_font_plan_entry(entry)
    ):
        raise ProjectionAuthorityError("TV font Plan entries are duplicated")
    seen = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ProjectionAuthorityError("TV font Plan manifest row is invalid")
        source = Path(str(row.get("source_path") or ""))
        target = Path(str(row.get("target_path") or ""))
        pair = (_lexical_path_key(source), _lexical_path_key(target))
        size = row.get("source_size")
        digest = str(row.get("source_sha256") or "").casefold()
        if (
            row.get("role") != "fonts"
            or row.get("scope") != "series"
            or row.get("season_number") != season_number
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or _HEX64.fullmatch(digest) is None
            or pair not in font_entries
            or pair in seen
            or _lexical_path_key(target.parent)
            != _lexical_path_key(associated_root)
        ):
            raise ProjectionAuthorityError(
                "TV font Plan manifest does not match canonical Plan entries"
            )
        seen.add(pair)
    if seen != set(font_entries):
        raise ProjectionAuthorityError(
            "TV font Plan manifest does not cover every canonical font entry"
        )


def _project_tv_associated_asset_manifest(
    authority: TVPlanningAuthority,
    plan: RenamePlan,
) -> tuple[tuple[PlanEntry, ...], dict[str, object]]:
    """Transform Decision-owned font identity into the one Plan consumer shape."""

    _assert_tv_authority_responsibility_table()
    source_manifest = authority.associated_asset_manifest
    if (
        not isinstance(source_manifest, Mapping)
        or source_manifest.get("schema_version") != 1
        or source_manifest.get("scope") != "series"
    ):
        raise ProjectionAuthorityError("TV font authority manifest is invalid")
    rows = source_manifest.get("entries")
    if not isinstance(rows, (list, tuple)):
        raise ProjectionAuthorityError("TV font authority entries are invalid")
    series_key = str(source_manifest.get("series_key") or "")
    if rows and not series_key:
        raise ProjectionAuthorityError("TV font authority series identity is absent")
    season_number = plan.metadata.get("season_number")
    if (
        not isinstance(season_number, int)
        or isinstance(season_number, bool)
        or authority.selected_season_number != season_number
    ):
        raise ProjectionAuthorityError("TV font authority season identity is stale")

    canonical_root = str(plan.metadata.get("canonical_work_root") or "")
    if not canonical_root:
        raise ProjectionAuthorityError("TV font authority has no canonical work root")
    associated_root = Path(canonical_root) / "Associated"
    plan_by_source = {}
    for index, entry in enumerate(plan.entries):
        source_key = _lexical_path_key(entry.source_path)
        if source_key in plan_by_source:
            raise ProjectionAuthorityError("TV Plan source identity is duplicated")
        plan_by_source[source_key] = (index, entry)

    projected_rows = []
    seen_sources = set()
    canonical_entries = list(plan.entries)
    for row in rows:
        if not isinstance(row, Mapping):
            raise ProjectionAuthorityError("TV font authority row is invalid")
        source = Path(str(row.get("source") or ""))
        target_name = str(row.get("target_name") or "")
        size = row.get("size")
        digest = str(row.get("content_sha256") or "").casefold()
        content_key = str(row.get("content_key") or "").casefold()
        source_key = _lexical_path_key(source)
        indexed_entry = plan_by_source.get(source_key)
        index, entry = indexed_entry if indexed_entry is not None else (-1, None)
        if (
            not str(source)
            or not target_name
            or Path(target_name).name != target_name
            or "/" in target_name
            or "\\" in target_name
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or _HEX64.fullmatch(digest) is None
            or _HEX64.fullmatch(content_key) is None
            or entry is None
            or source_key in seen_sources
            or entry.media_kind != "associated"
            or not is_supported_font_archive(entry.source_path)
            or _lexical_path_key(entry.target_path.parent)
            != _lexical_path_key(associated_root)
        ):
            raise ProjectionAuthorityError(
                "TV font authority does not match canonical Plan entries"
            )
        seen_sources.add(source_key)
        canonical_entry = replace(
            entry,
            target_path=associated_root / target_name,
            media_kind="associated",
            role="font",
        )
        canonical_entries[index] = canonical_entry
        projected_rows.append({
            "role": "fonts",
            "scope": "series",
            "season_number": season_number,
            "source_path": str(canonical_entry.source_path),
            "target_path": str(canonical_entry.target_path),
            "source_size": size,
            "source_sha256": digest,
        })
    unowned_fonts = {
        _lexical_path_key(entry.source_path)
        for entry in plan.entries
        if _is_tv_font_plan_entry(entry)
    } - seen_sources
    if unowned_fonts:
        raise ProjectionAuthorityError(
            "TV font authority does not cover every canonical font entry"
        )
    manifest = {
        "schema_version": 1,
        "assets": sorted(
            projected_rows,
            key=lambda row: (
                _lexical_path_key(row["source_path"]),
                _lexical_path_key(row["target_path"]),
            ),
        ),
    }
    canonical_plan = replace(plan, entries=tuple(canonical_entries))
    _validate_projected_tv_font_manifest(manifest, canonical_plan)
    return tuple(canonical_entries), manifest


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
    unknown = set(authority) - AUTHORITY_FINGERPRINT_FIELDS - CORRELATION_ONLY_FIELDS
    missing = AUTHORITY_FINGERPRINT_FIELDS - set(authority)
    if unknown or missing:
        raise ProjectionAuthorityError(
            f"projection authority fields mismatch: missing={sorted(missing)!r}, "
            f"unknown={sorted(unknown)!r}"
        )
    frozen = {
        field: _without_correlation(authority[field])
        for field in sorted(AUTHORITY_FINGERPRINT_FIELDS)
    }
    return _fingerprint(frozen, domain="bar.projection-authority")


def build_projection_receipt(
    authority: Mapping[str, object],
) -> ProjectionReceipt:
    frozen = {
        field: _without_correlation(authority[field])
        for field in sorted(AUTHORITY_FINGERPRINT_FIELDS)
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
    tmdb_tv_id: str
    season_number: int
    source_episode: int
    target_episode: int
    range_authority_id: str
    evidence_fingerprint: str
    decision_source: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_path", Path(self.source_path))
        if (
            self.schema_version != 1
            or not self.bangumi_subject_id
            or not self.tmdb_tv_id
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
            "tmdb_tv_id": self.tmdb_tv_id,
            "season_number": self.season_number,
            "source_episode": self.source_episode,
            "target_episode": self.target_episode,
            "range_authority_id": self.range_authority_id,
            "evidence_fingerprint": self.evidence_fingerprint,
            "decision_source": self.decision_source,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "TaskEpisodeOwnership":
        return cls(
            source_path=Path(str(value.get("source_path") or "")),
            bangumi_subject_id=str(value.get("bangumi_subject_id") or ""),
            tmdb_tv_id=str(value.get("tmdb_tv_id") or ""),
            season_number=int(value.get("season_number", -1)),
            source_episode=int(value.get("source_episode", 0)),
            target_episode=int(value.get("target_episode", 0)),
            range_authority_id=str(value.get("range_authority_id") or ""),
            evidence_fingerprint=str(value.get("evidence_fingerprint") or ""),
            decision_source=str(value.get("decision_source") or ""),
            schema_version=int(value.get("schema_version", 0)),
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
            location_key=canonical_location_key(source),
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
        with advisory_file_lock(
            self.root / ".ownership.lock",
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
            location_keys = {item.location_key for item in rows}
            physical_keys = {item.physical_key for item in rows}
            for path in self.root.glob("*.json"):
                if path.stem == task_id:
                    continue
                other = self.load_optional(path.stem)
                if other is None or other.state != "active":
                    continue
                if location_keys.intersection(
                    item.location_key for item in other.identities
                ) or physical_keys.intersection(
                    item.physical_key for item in other.identities
                ):
                    raise SourceOwnershipConflict(
                        f"source is already owned by task {other.task_id}"
                    )
            record = SourceOwnershipRecord(
                task_id=task_id,
                revision=(existing.revision + 1 if existing is not None else 0),
                decision_revision=decision_revision,
                inventory_fingerprint=inventory_fingerprint,
                identities=rows,
            )
            self._write(record)
            return record

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

    def restore_exact(
        self,
        current: SourceOwnershipRecord,
        previous: Optional[SourceOwnershipRecord],
    ) -> None:
        """Undo one exact ownership revision after the projection fails."""

        with advisory_file_lock(
            self.root / ".ownership.lock",
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
        with advisory_file_lock(
            self.root / ".ownership.lock",
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
class ProjectionAuthorityEnvelope:
    previous_decision: Optional[DecisionState]
    decision: DecisionState
    draft_plan: RenamePlan
    inventory_fingerprint: str
    episode_ownership: tuple[TaskEpisodeOwnership, ...] = ()


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

    def candidate(self, previous: Optional[DecisionState]) -> DecisionState:
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
            season_binding = {
                "season_binding_id": f"season-binding-{uuid.uuid4()}",
                "season_number": int(self.season_number),
                "decision_source": self.season_source,
            }
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
        if state.tv_planning_authority is not None:
            manifest_plan = replace(draft, metadata=metadata)
            canonical_entries, associated_manifest = (
                _project_tv_associated_asset_manifest(
                    state.tv_planning_authority, manifest_plan
                )
            )
            draft = replace(draft, entries=canonical_entries)
            metadata["associated_asset_manifest"] = associated_manifest
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
            "episode_ownership_schema_version": 1,
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
            "font_policy_fingerprint": _fingerprint(
                metadata.get("associated_asset_manifest", ()),
                domain="bar.font-policy",
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
        receipt = build_projection_receipt(authority)
        metadata["projection_receipt_fingerprint"] = receipt.fingerprint
        return replace(projected, metadata=metadata, projection_receipt=receipt)

    @staticmethod
    def revise(current: RenamePlan, draft: RenamePlan) -> RenamePlan:
        """Re-sign a Plan-only revision against its frozen Decision receipt."""

        if current.task_id != draft.task_id or current.projection_receipt is None:
            raise ProjectionAuthorityError("revision has no current receipt authority")
        validate_projection_receipt(current.projection_receipt)
        authority = dict(current.projection_receipt.authority)
        metadata = dict(draft.metadata)
        current_tv_authority = (
            int(authority["tv_planning_authority_schema_version"]) > 0
        )
        if current_tv_authority:
            current_manifest = current.metadata.get("associated_asset_manifest")
            if not isinstance(current_manifest, Mapping):
                raise ProjectionAuthorityError(
                    "current TV Plan has no typed associated asset manifest"
                )
            draft_manifest = metadata.get("associated_asset_manifest")
            if (
                draft_manifest is not None
                and _thaw(draft_manifest) != _thaw(current_manifest)
            ):
                raise ProjectionAuthorityError(
                    "Plan-only revision cannot replace TV font authority"
                )
            metadata["associated_asset_manifest"] = _thaw(current_manifest)
            _validate_projected_tv_font_manifest(
                metadata["associated_asset_manifest"],
                replace(draft, metadata=metadata),
            )
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
            "font_policy_fingerprint": _fingerprint(
                metadata.get("associated_asset_manifest", ()),
                domain="bar.font-policy",
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
        return replace(draft, metadata=metadata, projection_receipt=receipt)


def validate_plan_projection_receipt(plan: RenamePlan) -> None:
    """Recompute every Plan-owned receipt field and reject post-signing drift."""

    receipt = plan.projection_receipt
    if receipt is None:
        raise ProjectionAuthorityError("Plan has no ProjectionReceipt")
    validate_projection_receipt(receipt)
    authority = dict(receipt.authority)
    metadata = dict(plan.metadata)
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
        "font_policy_fingerprint": _fingerprint(
            metadata.get("associated_asset_manifest", ()),
            domain="bar.font-policy",
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

    def __init__(
        self,
        state_store: DecisionStateStore,
        plan_store: PlanStore,
        *,
        ownership_store: Optional[SourceOwnershipStore] = None,
        journal_root: Optional[Path] = None,
        task_root: Optional[Path] = None,
        _phase_hook: Optional[PhaseHook] = None,
    ) -> None:
        self.state_store = state_store
        self.plan_store = plan_store
        self.ownership_store = ownership_store or SourceOwnershipStore(
            plan_store.root.parent / "source_ownership"
        )
        self.journal_root = Path(
            journal_root or (plan_store.root.parent / "projection_journal")
        )
        self.task_root = Path(task_root or (plan_store.root.parent / "task"))
        self._phase_hook = _phase_hook

    def commit(
        self,
        envelope: ProjectionAuthorityEnvelope,
        *,
        task_terminal: Optional[TaskTerminalMutation] = None,
    ) -> tuple[DecisionState, RenamePlan]:
        with self._transaction_lock(envelope.decision.task_id):
            return self._commit_locked(envelope, task_terminal=task_terminal)

    def _commit_locked(
        self,
        envelope: ProjectionAuthorityEnvelope,
        *,
        task_terminal: Optional[TaskTerminalMutation],
    ) -> tuple[DecisionState, RenamePlan]:
        previous = envelope.previous_decision
        expected_revision = previous.revision if previous is not None else -1
        previous_plan = self._load_optional_plan(envelope.decision.task_id)
        written: Optional[DecisionState] = None
        ownership: Optional[SourceOwnershipRecord] = None
        previous_ownership = self.ownership_store.load_optional(
            envelope.decision.task_id
        )
        identities = self._freeze_source_identities(
            envelope.draft_plan.expected_sources,
            inventory_fingerprint=envelope.inventory_fingerprint,
        )
        prepared_decision = self.state_store.prepare_save(
            envelope.decision,
            expected_revision=expected_revision,
        )
        self._prepare_journal(
            envelope.decision.task_id,
            authority_input_fingerprint=_fingerprint(
                {
                    "decision": envelope.decision.to_dict(),
                    "inventory_fingerprint": envelope.inventory_fingerprint,
                    "episode_ownership": [
                        item.to_dict() for item in envelope.episode_ownership
                    ],
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
            task_terminal=task_terminal,
        )
        self._write_journal(
            envelope.decision.task_id,
            "prepared",
            "pending",
            extra={
                "current_decision_fingerprint": self.state_store.fingerprint(
                    prepared_decision
                )
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
                    )
                },
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
        if self.state_store.fingerprint(current) != self.state_store.fingerprint(decision):
            raise ProjectionAuthorityError("Decision changed before Plan revision")
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
        ownership = None
        previous_ownership = self.ownership_store.load_optional(decision.task_id)
        identities = self._freeze_source_identities(
            draft_plan.expected_sources,
            inventory_fingerprint=inventory_fingerprint,
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

    @staticmethod
    def _freeze_source_identities(
        paths: Iterable[Path],
        *,
        inventory_fingerprint: str,
    ) -> tuple[SourceIdentity, ...]:
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

    @contextmanager
    def _transaction_lock(self, task_id: str):
        self.journal_root.mkdir(parents=True, exist_ok=True)
        try:
            with advisory_file_lock(
                self.journal_root / f".{task_id}.projection.lock",
                owner={"kind": "projection_commit", "task_id": task_id},
            ):
                yield
        except FileLockBusyError as exc:
            raise ProjectionAuthorityError(
                "projection transaction is already active"
            ) from exc

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
        task_terminal: Optional[TaskTerminalMutation],
    ) -> None:
        if task_terminal is not None:
            if task_terminal.task_id != task_id:
                raise ProjectionAuthorityError("task terminal mutation task id differs")
            if task_terminal.task_root.resolve(strict=False) != self.task_root.resolve(
                strict=False
            ):
                raise ProjectionAuthorityError("task terminal mutation root differs")
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


def commit_projected_plan(
    state_store: DecisionStateStore,
    plan_store: PlanStore,
    *,
    previous: Optional[DecisionState],
    candidate: DecisionState,
    draft_plan: RenamePlan,
    task_terminal: Optional[TaskTerminalMutation] = None,
) -> tuple[DecisionState, RenamePlan]:
    envelope = ProjectionAuthorityEnvelope(
        previous_decision=previous,
        decision=candidate,
        draft_plan=draft_plan,
        inventory_fingerprint=inventory_fingerprint_for_plan(draft_plan),
        episode_ownership=_episode_rows_from_plan(draft_plan),
    )
    return ProjectionCommitService(
        state_store,
        plan_store,
        task_root=(task_terminal.task_root if task_terminal is not None else None),
    ).commit(envelope, task_terminal=task_terminal)


def commit_recognition_projection(
    state_store: DecisionStateStore,
    plan_store: PlanStore,
    recognition: RecognitionDecisionInput | UnresolvedRecognitionInput,
    draft_plan: RenamePlan,
    *,
    state_transform: Optional[Callable[[DecisionState], DecisionState]] = None,
    task_terminal: Optional[TaskTerminalMutation] = None,
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
