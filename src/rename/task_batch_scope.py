"""Durable batch source-scope reservations for planning tasks.

The store accepts source identities collected by an outer observation boundary.
It never scans media itself.  A reservation binds one task, one origin batch and
one exact source-identity set into a deterministic lineage.  More-specific
exact-file requests win over parent-directory requests inside the same batch;
cross-batch location or physical overlap fails closed until an explicit CAS
retire-and-transfer operation is recorded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Optional

from src.rename.file_lock import FileLockBusyError
from .content_policy import ContentPolicy, parse as parse_policy, require_typed


TASK_BATCH_MANIFEST_PHASES = (
    "preparing",
    "reservations_written",
    "tasks_enqueued",
    "active",
    "completed",
    "abandoned",
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_TERMINAL_STATES = {"completed", "abandoned", "failed_closed"}


class TaskBatchScopeError(RuntimeError):
    """The requested batch transition is invalid or cannot be proven safe."""


class SourceReservationConflict(TaskBatchScopeError):
    """A location or physical identity already belongs to another lineage."""


def _canonical_sha256(value: object, *, domain: str) -> str:
    encoded = json.dumps(
        {"domain": domain, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_safe_id(value: object, *, field_name: str) -> str:
    result = str(value or "").strip()
    if not result or _SAFE_ID.fullmatch(result) is None:
        raise TaskBatchScopeError(f"{field_name} 不安全")
    return result


@dataclass(frozen=True)
class SourceIdentity:
    source_path: str
    location_key: str
    physical_key: str
    size: int
    mtime_ns: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SourceIdentity":
        if not isinstance(value, Mapping):
            raise TaskBatchScopeError("source identity 必须是对象")
        source_path = str(value.get("source_path") or "").strip()
        location_key = str(value.get("location_key") or "").strip().casefold()
        physical_key = str(value.get("physical_key") or "").strip().casefold()
        if not source_path or not location_key or not physical_key:
            raise TaskBatchScopeError("source identity 缺少路径或物理身份")
        size = int(value.get("size", -1))
        mtime_ns = int(value.get("mtime_ns", -1))
        if size < 0 or mtime_ns < 0:
            raise TaskBatchScopeError("source identity 文件证据无效")
        return cls(
            source_path=source_path,
            location_key=location_key,
            physical_key=physical_key,
            size=size,
            mtime_ns=mtime_ns,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_path": self.source_path,
            "location_key": self.location_key,
            "physical_key": self.physical_key,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
        }


def source_identity_set_fingerprint(
    values: Iterable[Mapping[str, object] | SourceIdentity],
) -> str:
    identities = tuple(
        value if isinstance(value, SourceIdentity) else SourceIdentity.from_mapping(value)
        for value in values
    )
    rows = {
        (
            item.location_key,
            item.physical_key,
            item.size,
            item.mtime_ns,
        ): item.to_dict()
        for item in identities
    }
    ordered = [rows[key] for key in sorted(rows)]
    return _canonical_sha256(ordered, domain="bar.source-identity-set")


def canonical_reservation_lineage_id(
    *,
    owner_task_id: str,
    origin_batch_scope_fingerprint: str,
    source_identity_set_fingerprint: str,
) -> str:
    owner = _require_safe_id(owner_task_id, field_name="owner_task_id")
    batch_fingerprint = str(origin_batch_scope_fingerprint or "").strip().casefold()
    source_fingerprint = str(source_identity_set_fingerprint or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", batch_fingerprint):
        raise TaskBatchScopeError("batch_scope_fingerprint 无效")
    if not re.fullmatch(r"[0-9a-f]{64}", source_fingerprint):
        raise TaskBatchScopeError("source_identity_set_fingerprint 无效")
    return _canonical_sha256(
        {
            "schema_version": 1,
            "policy_version": "reservation-lineage-v1",
            "owner_task_id": owner,
            "origin_batch_scope_fingerprint": batch_fingerprint,
            "source_identity_set_fingerprint": source_fingerprint,
        },
        domain="bar.source-reservation-lineage",
    )


@dataclass(frozen=True)
class TaskSourceScope:
    schema_version: int
    task_id: str
    media_domain: str
    batch_scope_fingerprint: str
    reservation_lineage_id: str
    reservation_revision: int
    source_identity_set_fingerprint: str
    source_identities: tuple[SourceIdentity, ...]
    content_policy: ContentPolicy | None = None

    def __post_init__(self):
        require_typed(self.content_policy)
        if self.schema_version != (2 if self.content_policy is not None else 1):
            raise TaskBatchScopeError('TaskSourceScope schema/policy 不一致')

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "TaskSourceScope":
        if not isinstance(value, Mapping):
            raise TaskBatchScopeError("TaskSourceScope 必须是对象")
        identities_value = value.get("source_identities", ())
        if not isinstance(identities_value, (list, tuple)):
            raise TaskBatchScopeError("TaskSourceScope source_identities 无效")
        identities = tuple(SourceIdentity.from_mapping(item) for item in identities_value)
        task_id = _require_safe_id(value.get("task_id"), field_name="task_id")
        media_domain = str(value.get("media_domain") or "").strip().casefold()
        if media_domain not in {"tv", "movie"}:
            raise TaskBatchScopeError("TaskSourceScope media_domain 无效")
        batch_fingerprint = str(value.get("batch_scope_fingerprint") or "").casefold()
        set_fingerprint = str(
            value.get("source_identity_set_fingerprint") or ""
        ).casefold()
        if set_fingerprint != source_identity_set_fingerprint(identities):
            raise TaskBatchScopeError("TaskSourceScope source identity fingerprint 不匹配")
        lineage = canonical_reservation_lineage_id(
            owner_task_id=task_id,
            origin_batch_scope_fingerprint=batch_fingerprint,
            source_identity_set_fingerprint=set_fingerprint,
        )
        if str(value.get("reservation_lineage_id") or "").casefold() != lineage:
            raise TaskBatchScopeError("TaskSourceScope reservation lineage 不匹配")
        revision = int(value.get("reservation_revision", -1))
        if revision < 0:
            raise TaskBatchScopeError("TaskSourceScope reservation revision 无效")
        version = value.get('schema_version')
        if type(version) is not int or version not in (1, 2):
            raise TaskBatchScopeError("TaskSourceScope schema_version 不受支持")
        if version == 1 and 'content_policy' in value:
            raise TaskBatchScopeError('旧TaskSourceScope不能夹带新policy')
        policy = parse_policy(value.get('content_policy')) if version == 2 else None
        return cls(
            schema_version=version,
            task_id=task_id,
            media_domain=media_domain,
            batch_scope_fingerprint=batch_fingerprint,
            reservation_lineage_id=lineage,
            reservation_revision=revision,
            source_identity_set_fingerprint=set_fingerprint,
            source_identities=identities,
            content_policy=policy,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            **({'content_policy': self.content_policy.value} if self.content_policy is not None else {}),
            "task_id": self.task_id,
            "media_domain": self.media_domain,
            "batch_scope_fingerprint": self.batch_scope_fingerprint,
            "reservation_lineage_id": self.reservation_lineage_id,
            "reservation_revision": self.reservation_revision,
            "source_identity_set_fingerprint": self.source_identity_set_fingerprint,
            "source_identities": [item.to_dict() for item in self.source_identities],
        }


@dataclass(frozen=True)
class BatchTaskSourceRequest:
    task_id: str
    input_scope: tuple[str, ...]
    scope_kind: str
    source_identities: tuple[SourceIdentity, ...]
    media_domain: str = "tv"
    content_policy: ContentPolicy | None = None

    def __post_init__(self):
        require_typed(self.content_policy)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "BatchTaskSourceRequest":
        if not isinstance(value, Mapping):
            raise TaskBatchScopeError("batch task request 必须是对象")
        scope_value = value.get("input_scope", ())
        identity_value = value.get("source_identities", ())
        if not isinstance(scope_value, (list, tuple)) or not isinstance(
            identity_value, (list, tuple)
        ):
            raise TaskBatchScopeError("batch task request scope/source 无效")
        scope_kind = str(value.get("scope_kind") or "").strip()
        if scope_kind not in {"exact_file", "parent_directory"}:
            raise TaskBatchScopeError("batch task request scope_kind 无效")
        media_domain = str(value.get("media_domain") or "tv").strip().casefold()
        if media_domain not in {"tv", "movie"}:
            raise TaskBatchScopeError("batch task request media_domain 无效")
        version = value.get('schema_version', 1)
        if type(version) is not int or version not in (1, 2):
            raise TaskBatchScopeError('batch task request schema_version 无效')
        if version == 1 and 'content_policy' in value:
            raise TaskBatchScopeError('旧request不能夹带新policy')
        policy = parse_policy(value.get('content_policy')) if version == 2 else None
        return cls(
            task_id=_require_safe_id(value.get("task_id"), field_name="task_id"),
            input_scope=tuple(str(item) for item in scope_value),
            scope_kind=scope_kind,
            source_identities=tuple(
                SourceIdentity.from_mapping(item) for item in identity_value
            ),
            media_domain=media_domain,
            content_policy=policy,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            **({'schema_version': 2, 'content_policy': self.content_policy.value}
               if self.content_policy is not None else {}),
            "input_scope": list(self.input_scope),
            "scope_kind": self.scope_kind,
            "source_identities": [item.to_dict() for item in self.source_identities],
            "media_domain": self.media_domain,
        }


@dataclass(frozen=True)
class SourceReservation:
    owner_task_id: str
    origin_batch_scope_fingerprint: str
    source_identity_set_fingerprint: str
    reservation_lineage_id: str
    reservation_revision: int
    source_identity: SourceIdentity
    retired: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SourceReservation":
        return cls(
            owner_task_id=str(value.get("owner_task_id") or ""),
            origin_batch_scope_fingerprint=str(
                value.get("origin_batch_scope_fingerprint") or ""
            ),
            source_identity_set_fingerprint=str(
                value.get("source_identity_set_fingerprint") or ""
            ),
            reservation_lineage_id=str(value.get("reservation_lineage_id") or ""),
            reservation_revision=int(value.get("reservation_revision", 0)),
            source_identity=SourceIdentity.from_mapping(value),
            retired=bool(value.get("retired", False)),
        )

    @property
    def source_path(self) -> str:
        return self.source_identity.source_path

    @property
    def location_key(self) -> str:
        return self.source_identity.location_key

    @property
    def physical_key(self) -> str:
        return self.source_identity.physical_key

    def to_dict(self) -> dict[str, object]:
        return {
            **self.source_identity.to_dict(),
            "owner_task_id": self.owner_task_id,
            "origin_batch_scope_fingerprint": self.origin_batch_scope_fingerprint,
            "source_identity_set_fingerprint": self.source_identity_set_fingerprint,
            "reservation_lineage_id": self.reservation_lineage_id,
            "reservation_revision": self.reservation_revision,
            "retired": self.retired,
        }


@dataclass(frozen=True)
class TaskBatchScopeManifest:
    batch_id: str
    revision: int
    state: str
    batch_scope_fingerprint: str
    requests: tuple[BatchTaskSourceRequest, ...]
    task_source_scopes: tuple[TaskSourceScope, ...]
    reservations: tuple[SourceReservation, ...]

    @property
    def orphan_reservation_count(self) -> int:
        task_ids = {item.task_id for item in self.task_source_scopes}
        return sum(
            not item.retired and item.owner_task_id not in task_ids
            for item in self.reservations
        )
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "batch_id": self.batch_id,
            "revision": self.revision,
            "state": self.state,
            "batch_scope_fingerprint": self.batch_scope_fingerprint,
            "requests": [item.to_dict() for item in self.requests],
            "task_source_scopes": [item.to_dict() for item in self.task_source_scopes],
            "reservations": [item.to_dict() for item in self.reservations],
            "orphan_reservation_count": self.orphan_reservation_count,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "TaskBatchScopeManifest":
        if int(value.get("schema_version", 0)) != 1:
            raise TaskBatchScopeError("batch manifest schema_version 不受支持")
        return cls(
            batch_id=_require_safe_id(value.get("batch_id"), field_name="batch_id"),
            revision=int(value.get("revision", 0)),
            state=str(value.get("state") or ""),
            batch_scope_fingerprint=str(value.get("batch_scope_fingerprint") or ""),
            requests=tuple(
                BatchTaskSourceRequest.from_mapping(item)
                for item in value.get("requests", ())
            ),
            task_source_scopes=tuple(
                TaskSourceScope.from_mapping(item)
                for item in value.get("task_source_scopes", ())
            ),
            reservations=tuple(
                SourceReservation.from_mapping(item)
                for item in value.get("reservations", ())
            ),
        )




class TaskBatchScopeManifestStore:
    """Single-writer JSON store for batch manifests and reservation history."""

    def __init__(
        self,
        root: Path,
        _phase_hook: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.root = Path(root)
        self.manifest_root = self.root / "manifests"
        self.history_root = self.root / "operation_history"
        root_identity = hashlib.sha256(
            str(self.root.resolve(strict=False)).casefold().encode("utf-8")
        ).hexdigest()
        # Advisory lock files are operational coordination metadata, not
        # durable business state.  Keep them outside BAR_DATA_DIR so legacy
        # snapshots, backup/export and restart comparison remain unchanged.
        self.lock_path = (
            Path(tempfile.gettempdir())
            / "bangumi-auto-renamer-locks"
            / f"task-batch-scope-{root_identity}.lock"
        )
        self._phase_hook = _phase_hook

    def _lock_resource(self):
        from .file_lock import LockResource
        return LockResource(self, self.lock_path, 'task_batch_scope')

    @contextmanager
    def _mutation_lock(self, operation: str) -> Iterator[None]:
        """Serialize conflict checks and writes across processes.

        ``os.replace`` makes one file replacement atomic, but it cannot make
        the preceding global-conflict scan and the following writes one
        transaction.  A store-wide advisory lock closes that admission race.
        Busy ownership fails closed; callers may retry through a new request.
        """

        from .file_lock import serialized_resource_guard
        try:
            with serialized_resource_guard(
                self._lock_resource(),
                owner={
                    "component": "task_batch_scope",
                    "operation": str(operation),
                },
            ):
                yield
        except FileLockBusyError as exc:
            raise TaskBatchScopeError(
                "批次来源登记暂时忙，本次排队未能完成，请稍后重试"
            ) from exc

    def path_for(self, batch_id: str) -> Path:
        identifier = _require_safe_id(batch_id, field_name="batch_id")
        return self.manifest_root / f"{identifier}.json"

    def load(self, batch_id: str) -> TaskBatchScopeManifest:
        path = self.path_for(batch_id)
        try:
            return TaskBatchScopeManifest.from_mapping(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise TaskBatchScopeError(f"无法读取 batch manifest：{exc}") from exc

    def require_active_scope(
        self,
        scope: TaskSourceScope,
        *,
        batch_id: Optional[str] = None,
    ) -> TaskBatchScopeManifest:
        """Prove that a persisted task scope still belongs to one active lineage."""

        from .recognition_reset import require_ready
        require_ready(self.root.parent, scope.task_id)
        candidates = (
            (self.load(batch_id),)
            if batch_id is not None
            else self._all_manifests()
        )
        matches = []
        for manifest in candidates:
            if manifest.state != "active":
                continue
            if manifest.batch_scope_fingerprint != scope.batch_scope_fingerprint:
                continue
            for current in manifest.task_source_scopes:
                if (
                    current.task_id == scope.task_id
                    and current.reservation_lineage_id
                    == scope.reservation_lineage_id
                    and current.reservation_revision == scope.reservation_revision
                    and current.source_identity_set_fingerprint
                    == scope.source_identity_set_fingerprint
                    and current == scope
                ):
                    matches.append(manifest)
        if len(matches) != 1:
            raise TaskBatchScopeError(
                "task source scope没有唯一active reservation lineage"
            )
        return matches[0]

    @staticmethod
    def _request_priority(request: BatchTaskSourceRequest) -> tuple[int, int, str]:
        return (
            2 if request.scope_kind == "exact_file" else 1,
            max((len(item) for item in request.input_scope), default=0),
            request.task_id,
        )

    @staticmethod
    def _batch_fingerprint(requests: tuple[BatchTaskSourceRequest, ...]) -> str:
        rows = [item.to_dict() for item in sorted(requests, key=lambda row: row.task_id)]
        return _canonical_sha256(rows, domain="bar.task-batch-scope")

    def _assigned_identities(
        self,
        requests: tuple[BatchTaskSourceRequest, ...],
    ) -> dict[str, tuple[SourceIdentity, ...]]:
        physical_locations: dict[str, set[str]] = {}
        location_physicals: dict[str, set[str]] = {}
        for request in requests:
            for identity in request.source_identities:
                physical_locations.setdefault(identity.physical_key, set()).add(
                    identity.location_key
                )
                location_physicals.setdefault(identity.location_key, set()).add(
                    identity.physical_key
                )
        if any(len(values) > 1 for values in physical_locations.values()):
            raise SourceReservationConflict(
                "同一批次包含不同路径指向同一物理文件，拒绝产生多个owner"
            )
        if any(len(values) > 1 for values in location_physicals.values()):
            raise SourceReservationConflict("同一来源路径的物理身份互相矛盾")

        candidates: dict[tuple[str, str], list[tuple[BatchTaskSourceRequest, SourceIdentity]]] = {}
        for request in requests:
            for identity in request.source_identities:
                key = (identity.location_key, identity.physical_key)
                candidates.setdefault(key, []).append((request, identity))
        assigned: dict[str, list[SourceIdentity]] = {item.task_id: [] for item in requests}
        for rows in candidates.values():
            winner, identity = max(
                rows,
                key=lambda row: self._request_priority(row[0]),
            )
            assigned[winner.task_id].append(identity)
        return {
            task_id: tuple(sorted(values, key=lambda item: item.source_path.casefold()))
            for task_id, values in assigned.items()
        }

    def _all_manifests(self) -> tuple[TaskBatchScopeManifest, ...]:
        if not self.manifest_root.exists():
            return ()
        result = []
        for path in sorted(self.manifest_root.glob("*.json")):
            try:
                result.append(
                    TaskBatchScopeManifest.from_mapping(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                )
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise TaskBatchScopeError(
                    f"batch manifest 损坏，拒绝创建新reservation：{path.name}"
                ) from exc
        return tuple(result)

    def _check_global_conflicts(
        self,
        reservations: tuple[SourceReservation, ...],
        *,
        excluding_batch_id: Optional[str] = None,
    ) -> None:
        existing = tuple(
            reservation
            for manifest in self._all_manifests()
            if manifest.batch_id != excluding_batch_id
            for reservation in manifest.reservations
            if not reservation.retired
        )
        for candidate in reservations:
            for current in existing:
                overlaps = (
                    candidate.location_key == current.location_key
                    or candidate.physical_key == current.physical_key
                )
                if not overlaps:
                    continue
                same_lineage = (
                    candidate.owner_task_id == current.owner_task_id
                    and candidate.origin_batch_scope_fingerprint
                    == current.origin_batch_scope_fingerprint
                    and candidate.source_identity_set_fingerprint
                    == current.source_identity_set_fingerprint
                    and candidate.reservation_lineage_id
                    == current.reservation_lineage_id
                )
                if not same_lineage:
                    raise SourceReservationConflict(
                        "source 已由其他task/batch lineage保留："
                        f"owner={current.owner_task_id}; "
                        f"lineage={current.reservation_lineage_id}"
                    )

    def _materialize(
        self,
        *,
        batch_id: str,
        requests: tuple[BatchTaskSourceRequest, ...],
        batch_fingerprint: str,
        state: str,
        revision: int,
    ) -> TaskBatchScopeManifest:
        assigned = self._assigned_identities(requests)
        scopes: list[TaskSourceScope] = []
        reservations: list[SourceReservation] = []
        by_task = {item.task_id: item for item in requests}
        for task_id in sorted(by_task):
            identities = assigned[task_id]
            set_fingerprint = source_identity_set_fingerprint(identities)
            lineage = canonical_reservation_lineage_id(
                owner_task_id=task_id,
                origin_batch_scope_fingerprint=batch_fingerprint,
                source_identity_set_fingerprint=set_fingerprint,
            )
            scope = TaskSourceScope(
                schema_version=2 if by_task[task_id].content_policy is not None else 1,
                content_policy=by_task[task_id].content_policy,
                task_id=task_id,
                media_domain=by_task[task_id].media_domain,
                batch_scope_fingerprint=batch_fingerprint,
                reservation_lineage_id=lineage,
                reservation_revision=0,
                source_identity_set_fingerprint=set_fingerprint,
                source_identities=identities,
            )
            scopes.append(scope)
            reservations.extend(
                SourceReservation(
                    owner_task_id=task_id,
                    origin_batch_scope_fingerprint=batch_fingerprint,
                    source_identity_set_fingerprint=set_fingerprint,
                    reservation_lineage_id=lineage,
                    reservation_revision=0,
                    source_identity=identity,
                )
                for identity in identities
            )
        return TaskBatchScopeManifest(
            batch_id=batch_id,
            revision=revision,
            state=state,
            batch_scope_fingerprint=batch_fingerprint,
            requests=requests,
            task_source_scopes=tuple(scopes),
            reservations=tuple(reservations),
        )

    def reserve_batch(
        self,
        *,
        batch_id: str,
        task_requests: Iterable[BatchTaskSourceRequest],
    ) -> TaskBatchScopeManifest:
        with self._mutation_lock("reserve_batch"):
            return self._reserve_batch_unlocked(
                batch_id=batch_id,
                task_requests=task_requests,
            )

    def _reserve_batch_unlocked(
        self,
        *,
        batch_id: str,
        task_requests: Iterable[BatchTaskSourceRequest],
    ) -> TaskBatchScopeManifest:
        identifier = _require_safe_id(batch_id, field_name="batch_id")
        requests = tuple(task_requests)
        if not requests or len({item.task_id for item in requests}) != len(requests):
            raise TaskBatchScopeError("batch task requests 为空或task_id重复")
        path = self.path_for(identifier)
        batch_fingerprint = self._batch_fingerprint(requests)
        if path.exists():
            existing = self.load(identifier)
            if existing.batch_scope_fingerprint == batch_fingerprint:
                return existing
            raise TaskBatchScopeError("batch_id 已绑定不同source scope")
        materialized = self._materialize(
            batch_id=identifier,
            requests=requests,
            batch_fingerprint=batch_fingerprint,
            state="reservations_written",
            revision=1,
        )
        self._check_global_conflicts(materialized.reservations)
        preparing = replace(
            materialized,
            revision=0,
            state="preparing",
            task_source_scopes=(),
            reservations=(),
        )
        self._save(preparing)
        self._hook("preparing")
        self._save(materialized)
        self._hook("reservations_written")
        return materialized

    def mark_tasks_enqueued(
        self,
        batch_id: str,
        *,
        expected_revision: int,
        task_ids: tuple[str, ...],
    ) -> TaskBatchScopeManifest:
        with self._mutation_lock("mark_tasks_enqueued"):
            current = self.load(batch_id)
            if current.state != "reservations_written":
                raise TaskBatchScopeError("batch 不在task record待确认阶段")
            if current.revision != int(expected_revision):
                raise TaskBatchScopeError("batch revision 已变化")
            expected_ids = {item.task_id for item in current.requests}
            if set(task_ids) != expected_ids or len(task_ids) != len(expected_ids):
                raise TaskBatchScopeError("durable task record集合与batch不一致")
            updated = replace(
                current,
                revision=current.revision + 1,
                state="tasks_enqueued",
            )
            self._save(updated)
            self._hook("tasks_enqueued")
            return updated

    def activate_batch(
        self,
        batch_id: str,
        *,
        expected_revision: int,
    ) -> TaskBatchScopeManifest:
        with self._mutation_lock("activate_batch"):
            current = self.load(batch_id)
            if current.state != "tasks_enqueued":
                raise TaskBatchScopeError("batch task records尚未durable")
            if current.revision != int(expected_revision):
                raise TaskBatchScopeError("batch revision 已变化")
            updated = replace(
                current,
                revision=current.revision + 1,
                state="active",
            )
            self._save(updated)
            self._hook("active")
            return updated

    def fail_closed(self, batch_id: str) -> TaskBatchScopeManifest:
        with self._mutation_lock("fail_closed"):
            current = self.load(batch_id)
            if current.state == "failed_closed":
                return current
            if current.state in {"completed", "abandoned"}:
                raise TaskBatchScopeError("terminal batch不能改为failed_closed")
            updated = replace(
                current,
                revision=current.revision + 1,
                state="failed_closed",
                task_source_scopes=(),
                reservations=(),
            )
            self._save(updated)
            return updated

    def transition(
        self,
        batch_id: str,
        *,
        expected_revision: int,
        target_state: str,
    ) -> TaskBatchScopeManifest:
        with self._mutation_lock("transition"):
            return self._transition_unlocked(
                batch_id,
                expected_revision=expected_revision,
                target_state=target_state,
            )

    def prepare_recognition_reset(self, scope, *, batch_id, new_batch_id, media_domain):
        """准备原任务的范围替换；实际写入由重新识别事务统一负责。"""
        current = self.require_active_scope(scope, batch_id=batch_id)
        requests = [r for r in current.requests if r.task_id == scope.task_id]
        if len(requests) != 1 or media_domain not in {'tv', 'movie'}:
            raise TaskBatchScopeError('重新识别来源请求无效')
        request = replace(requests[0], source_identities=scope.source_identities, media_domain=media_domain)
        new = self._materialize(batch_id=new_batch_id, requests=(request,),
            batch_fingerprint=self._batch_fingerprint((request,)), state='active', revision=3)
        if new.task_source_scopes[0].source_identities != scope.source_identities:
            raise TaskBatchScopeError('重新识别改变了来源集合')
        reservations = tuple(replace(r, retired=True, reservation_revision=r.reservation_revision+1)
            if r.owner_task_id == scope.task_id and not r.retired else r for r in current.reservations)
        old = replace(current, revision=current.revision+1,
            state='active' if any(not r.retired for r in reservations) else 'abandoned',
            reservations=reservations,
            task_source_scopes=tuple(s for s in current.task_source_scopes if s.task_id != scope.task_id))
        return old, new

    def retire_task(
        self,
        batch_id: str,
        *,
        task_id: str,
        expected_lineage_id: str,
    ) -> TaskBatchScopeManifest:
        """Retire one task's reservations without releasing sibling tasks."""

        with self._mutation_lock("retire_task"):
            current = self.load(batch_id)
            if current.state in {"completed", "abandoned", "failed_closed"}:
                return current
            if current.state != "active":
                raise TaskBatchScopeError("batch 尚未active，不能retire task")
            matching = tuple(
                item
                for item in current.reservations
                if item.owner_task_id == task_id
                and item.reservation_lineage_id == expected_lineage_id
                and not item.retired
            )
            if not matching:
                retired = tuple(item for item in current.reservations
                    if item.owner_task_id == task_id
                    and item.reservation_lineage_id == expected_lineage_id)
                if (retired and all(item.retired for item in retired)
                        and not any(item.owner_task_id == task_id and not item.retired
                                    for item in current.reservations)
                        and not any(item.task_id == task_id for item in current.task_source_scopes)):
                    return current
                raise TaskBatchScopeError("task reservation lineage 已变化")
            reservations = tuple(
                replace(
                    item,
                    reservation_revision=item.reservation_revision + 1,
                    retired=True,
                )
                if item in matching
                else item
                for item in current.reservations
            )
            remaining_scopes = tuple(
                item
                for item in current.task_source_scopes
                if not (
                    item.task_id == task_id
                    and item.reservation_lineage_id == expected_lineage_id
                )
            )
            has_live_reservations = any(not item.retired for item in reservations)
            updated = replace(
                current,
                revision=current.revision + 1,
                state="active" if has_live_reservations else "abandoned",
                task_source_scopes=remaining_scopes,
                reservations=reservations,
            )
            self._save(updated)
            return updated

    def _transition_unlocked(
        self,
        batch_id: str,
        *,
        expected_revision: int,
        target_state: str,
    ) -> TaskBatchScopeManifest:
        if target_state not in {"completed", "abandoned"}:
            raise TaskBatchScopeError("batch terminal state 无效")
        current = self.load(batch_id)
        if current.revision != int(expected_revision):
            raise TaskBatchScopeError("batch revision 已变化")
        if current.state in _TERMINAL_STATES:
            if current.state == target_state:
                return current
            raise TaskBatchScopeError("batch terminal state 不可倒退")
        if current.state != "active":
            raise TaskBatchScopeError("batch 尚未active，不能结束")
        updated = replace(
            current,
            revision=current.revision + 1,
            state=target_state,
        )
        self._save(updated)
        self._hook(target_state)
        return updated

    def recover_pending(self, batch_id: str) -> TaskBatchScopeManifest:
        with self._mutation_lock("recover_pending"):
            return self._recover_pending_unlocked(batch_id)

    def _recover_pending_unlocked(self, batch_id: str) -> TaskBatchScopeManifest:
        current = self.load(batch_id)
        if current.state in {"active", "completed", "abandoned", "failed_closed"}:
            return current
        if current.state == "preparing":
            recovered = replace(
                current,
                revision=current.revision + 1,
                state="failed_closed",
                task_source_scopes=(),
                reservations=(),
            )
        elif current.state in {"reservations_written", "tasks_enqueued"}:
            recovered = replace(
                current,
                revision=current.revision + 1,
                state="failed_closed",
                task_source_scopes=(),
                reservations=(),
            )
        else:
            raise TaskBatchScopeError("未知 batch state，拒绝恢复")
        self._save(recovered)
        return recovered

    def recover_all_pending(self) -> tuple[TaskBatchScopeManifest, ...]:
        """Resolve every durable pre-active manifest before workers start."""

        with self._mutation_lock("recover_all_pending"):
            recovered = []
            for manifest in self._all_manifests():
                recovered.append(self._recover_pending_unlocked(manifest.batch_id))
            return tuple(recovered)

    def retire_and_transfer(
        self,
        *,
        from_lineage_id: str,
        expected_reservation_revision: int,
        to_batch_id: str,
        to_request: BatchTaskSourceRequest,
        reason: str,
    ) -> SourceReservation:
        with self._mutation_lock("retire_and_transfer"):
            return self._retire_and_transfer_unlocked(
                from_lineage_id=from_lineage_id,
                expected_reservation_revision=expected_reservation_revision,
                to_batch_id=to_batch_id,
                to_request=to_request,
                reason=reason,
            )

    def _retire_and_transfer_unlocked(
        self,
        *,
        from_lineage_id: str,
        expected_reservation_revision: int,
        to_batch_id: str,
        to_request: BatchTaskSourceRequest,
        reason: str,
    ) -> SourceReservation:
        source_manifest: Optional[TaskBatchScopeManifest] = None
        source_reservation: Optional[SourceReservation] = None
        for manifest in self._all_manifests():
            for reservation in manifest.reservations:
                if (
                    reservation.reservation_lineage_id == from_lineage_id
                    and not reservation.retired
                ):
                    source_manifest = manifest
                    source_reservation = reservation
                    break
            if source_reservation is not None:
                break
        if source_manifest is None or source_reservation is None:
            raise TaskBatchScopeError("找不到可transfer的reservation lineage")
        if source_reservation.reservation_revision != int(
            expected_reservation_revision
        ):
            raise TaskBatchScopeError("reservation revision 已变化")
        original_bytes = self.path_for(source_manifest.batch_id).read_bytes()
        retired_rows = tuple(
            replace(
                item,
                retired=True,
                reservation_revision=item.reservation_revision + 1,
            )
            if item.reservation_lineage_id == from_lineage_id else item
            for item in source_manifest.reservations
        )
        retired_manifest = replace(
            source_manifest,
            revision=source_manifest.revision + 1,
            reservations=retired_rows,
        )
        self._save(retired_manifest)
        try:
            transferred_manifest = self._reserve_batch_unlocked(
                batch_id=to_batch_id,
                task_requests=(to_request,),
            )
        except BaseException:
            self.path_for(source_manifest.batch_id).write_bytes(original_bytes)
            raise
        transferred = next(
            item
            for item in transferred_manifest.reservations
            if item.owner_task_id == to_request.task_id
        )
        event = {
            "schema_version": 1,
            "operation": "retire_and_transfer",
            "reason": str(reason or "").strip(),
            "from_lineage_id": from_lineage_id,
            "to_lineage_id": transferred.reservation_lineage_id,
            "from_owner_task_id": source_reservation.owner_task_id,
            "to_owner_task_id": transferred.owner_task_id,
            "expected_reservation_revision": int(expected_reservation_revision),
        }
        self._append_history(transferred.reservation_lineage_id, event)
        return transferred

    def partition_active_task(
        self,
        *,
        source_batch_id: str,
        parent_scope: TaskSourceScope,
        child_task_id: str,
        selected_location_keys: Iterable[str],
        partition_batch_id: str,
    ) -> None:
        """Retired: no alternate writer or automatic compatibility fallback."""
        raise TaskBatchScopeError('旧二分拆卡已停用，请使用“调整计划范围”；无法处理的旧任务请安全结束后重新添加。')

    def prepare_partition_active_task(
        self,
        *,
        source_batch_id: str,
        parent_scope: TaskSourceScope,
        child_task_id: str,
        selected_location_keys: Iterable[str],
        partition_batch_id: str,
    ) -> None:
        """Retired: no alternate writer or automatic compatibility fallback."""
        raise TaskBatchScopeError('旧二分拆卡已停用，请使用“调整计划范围”；无法处理的旧任务请安全结束后重新添加。')

    def operation_history(self, lineage_id: str) -> tuple[Mapping[str, object], ...]:
        path = self._history_path(lineage_id)
        if not path.exists():
            return ()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskBatchScopeError("reservation operation history 损坏") from exc
        if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
            raise TaskBatchScopeError("reservation operation history 格式无效")
        return tuple(dict(item) for item in value)

    def _history_path(self, lineage_id: str) -> Path:
        value = str(lineage_id or "").casefold()
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise TaskBatchScopeError("reservation lineage id 无效")
        return self.history_root / f"{value}.json"

    def _append_history(self, lineage_id: str, event: Mapping[str, object]) -> None:
        path = self._history_path(lineage_id)
        existing = list(self.operation_history(lineage_id))
        existing.append(dict(event))
        self._atomic_json(path, existing)

    def _hook(self, phase: str) -> None:
        if self._phase_hook is not None:
            self._phase_hook(phase)

    def _save(self, manifest: TaskBatchScopeManifest) -> None:
        self._atomic_json(self.path_for(manifest.batch_id), manifest.to_dict())

    @staticmethod
    def _atomic_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
