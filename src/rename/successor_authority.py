"""Canonical cross-task authority for confirmed provider-subject cohorts.

The store is deliberately separate from per-task Decision documents and from
the materialized ProviderLinkCatalog.  One explicit ``confirm_cohort`` command
publishes a versioned fact; later tasks may consume it without provider I/O.
Target roots and source paths are intentionally absent from this contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional

from .decision_state import DecisionState, DecisionStateStore
from .file_lock import FileLockBusyError, advisory_file_lock
from .provider_link_resolver import ProviderLinkResolution


SUCCESSOR_AUTHORITY_SCHEMA_VERSION = 3
SUCCESSOR_AUTHORITY_POLICY_VERSION = "provider-subject-cohort-v3"
_AUTHORITY_ID = re.compile(r"^cohort-[0-9a-f]{64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_ROUTING_KEYS = frozenset({
    "canonical_root", "resolved_target_root", "target_root", "source_path",
})


class SuccessorAuthorityError(RuntimeError):
    """The canonical authority is absent, invalid, conflicting or stale."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = str(reason_code)


def successor_authority_root(decision_root: Path) -> Path:
    return Path(decision_root).parent / "provider_subject_cohort_authority"


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _reject_target_routing(value: object) -> None:
    """Keep target/source routing outside the cross-task identity authority."""

    if isinstance(value, Mapping):
        if any(str(key).casefold() in _FORBIDDEN_ROUTING_KEYS for key in value):
            raise SuccessorAuthorityError("authority_contains_target_routing")
        for item in value.values():
            _reject_target_routing(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _reject_target_routing(item)


def _sha256(value: object, *, domain: str) -> str:
    payload = {"domain": domain, "value": _plain(value)}
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def cohort_projection_fingerprint_for_decision(
    decision: DecisionState,
) -> str:
    """Fingerprint only the producer facts owned by cohort resolution.

    The complete Decision fingerprint remains useful audit evidence, but later
    special/role decisions must not revoke an unchanged cross-task cohort.
    Target routing is deliberately excluded from the series binding projection;
    the authority contract never owns a target root.
    """

    series = decision.series_binding
    series_identity = (
        {
            key: _plain(series.get(key))
            for key in ("series_key", "catalog_revision", "decision_source", "registration_authority")
            if key in series
        }
        if isinstance(series, Mapping)
        else None
    )
    tv_authority = (
        decision.tv_planning_authority.to_dict()
        if decision.tv_planning_authority is not None
        else None
    )
    payload = {
        "projection_schema_version": 1,
        "task_id": decision.task_id,
        "confirmed_work": _plain(decision.confirmed_work),
        "linked_provider_identities": _plain(
            decision.linked_provider_identities
        ),
        "series_binding_identity": series_identity,
        "season_binding": _plain(decision.season_binding),
        "library_domain": decision.library_domain,
        "layout_mode": decision.layout_mode,
        "provider_link_binding": _plain(decision.provider_link_binding),
        "provider_subject_boundary_decisions": _plain(
            decision.provider_subject_boundary_decisions
        ),
        "tv_planning_authority": _plain(tv_authority),
    }
    return _sha256(payload, domain="bar.successor-authority-source-cohort")


def _provider_key(value: Mapping[str, object]) -> tuple[str, str, str]:
    return tuple(
        str(value.get(field) or "").strip().casefold()
        for field in ("namespace", "entity_type", "provider_id")
    )


def _resolution_payload(resolution: ProviderLinkResolution) -> dict[str, object]:
    # A cohort shares official relations, never the publisher's local Catalog/root.
    if resolution.registration_authority is not None:
        from .series_registration import SeriesRegistrationAuthority, build_series_registration_authority
        local = SeriesRegistrationAuthority.from_mapping(resolution.registration_authority)
        if local.affiliation or local.materialized_mapping or local.relocation:
            official = build_series_registration_authority(resolution)
            resolution = replace(resolution, registration_authority=official.to_dict(),
                                 proposed_series_key=official.series_key)
    return {
        "automation_level": str(resolution.automation_level),
        "registration_authority": _plain(resolution.registration_authority),
        "subject_observations": _plain(resolution.subject_observations),
        "status": str(resolution.status),
        "tmdb_tv_id": str(resolution.tmdb_tv_id or ""),
        "proposed_series_key": str(resolution.proposed_series_key or ""),
        "season_assignments": [
            {"bangumi_subject_id": str(subject_id), "season_number": int(season)}
            for subject_id, season in resolution.season_assignments
        ],
        "requires_plan_review": bool(resolution.requires_plan_review),
        "evidence_path": [str(item) for item in resolution.evidence_path],
        "reason_codes": [str(item) for item in resolution.reason_codes],
        "work_segment": _plain(resolution.work_segment or {}),
        "derived_episode_ranges": [
            _plain(item) for item in resolution.derived_episode_ranges
        ],
        "boundaries": [_plain(item) for item in resolution.boundaries],
        "mapping_shape": (
            str(resolution.mapping_shape) if resolution.mapping_shape else None
        ),
        "review_disposition": (
            str(resolution.review_disposition)
            if resolution.review_disposition else None
        ),
        "resolution_fingerprint": str(
            resolution.resolution_fingerprint or ""
        ),
        "season_count_policy_version": (
            str(resolution.season_count_policy_version)
            if resolution.season_count_policy_version else None
        ),
    }


def _resolution_from_payload(value: Mapping[str, object]) -> ProviderLinkResolution:
    rows = value.get("season_assignments")
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise SuccessorAuthorityError("authority_resolution_invalid")
    assignments = tuple(
        (str(row.get("bangumi_subject_id") or ""), row.get("season_number"))
        for row in rows
    )
    if any(not subject_id or not isinstance(season, int) or isinstance(season, bool)
           for subject_id, season in assignments):
        raise SuccessorAuthorityError("authority_resolution_invalid")
    return ProviderLinkResolution(
        automation_level=str(value.get("automation_level") or ""),
        registration_authority=value.get('registration_authority'),
        subject_observations=value.get('subject_observations'),
        status=str(value.get("status") or ""),
        tmdb_tv_id=str(value.get("tmdb_tv_id") or "") or None,
        proposed_series_key=str(value.get("proposed_series_key") or "") or None,
        season_assignments=tuple((subject_id, int(season)) for subject_id, season in assignments),
        requires_plan_review=value.get("requires_plan_review") is True,
        evidence_path=tuple(str(item) for item in value.get("evidence_path", ()) or ()),
        reason_codes=tuple(str(item) for item in value.get("reason_codes", ()) or ()),
        work_segment=(
            dict(value.get("work_segment") or {})
            if isinstance(value.get("work_segment"), Mapping) else None
        ),
        derived_episode_ranges=tuple(
            dict(item) for item in value.get("derived_episode_ranges", ()) or ()
            if isinstance(item, Mapping)
        ),
        boundaries=tuple(
            dict(item) for item in value.get("boundaries", ()) or ()
            if isinstance(item, Mapping)
        ),
        mapping_shape=(
            str(value.get("mapping_shape"))
            if value.get("mapping_shape") is not None else None
        ),
        review_disposition=(
            str(value.get("review_disposition"))
            if value.get("review_disposition") is not None else None
        ),
        resolution_fingerprint=str(value.get("resolution_fingerprint") or "") or None,
        season_count_policy_version=(
            str(value.get("season_count_policy_version"))
            if value.get("season_count_policy_version") is not None else None
        ),
    )


def _normalized_provider_snapshot(value: Mapping[str, object]) -> dict[str, object]:
    provider_id = str(value.get("tmdb_tv_id") or value.get("provider_id") or "")
    seasons_value = value.get("seasons")
    if not provider_id or not isinstance(seasons_value, (list, tuple)):
        raise SuccessorAuthorityError("authority_provider_snapshot_invalid")
    seasons = []
    for row in seasons_value:
        if not isinstance(row, Mapping):
            raise SuccessorAuthorityError("authority_provider_snapshot_invalid")
        season_number = row.get("season_number")
        episode_count = row.get("episode_count")
        if (
            not isinstance(season_number, int)
            or isinstance(season_number, bool)
            or not isinstance(episode_count, int)
            or isinstance(episode_count, bool)
        ):
            raise SuccessorAuthorityError("authority_provider_snapshot_invalid")
        seasons.append({
            "season_number": season_number,
            "air_date": str(row.get("air_date") or ""),
            "episode_count": episode_count,
        })
    title = str(value.get("title") or "")
    original_title = str(value.get("original_title") or "")
    if not title or not original_title or not seasons:
        raise SuccessorAuthorityError("authority_provider_snapshot_invalid")
    return {
        "namespace": "tmdb",
        "entity_type": "tv",
        "provider_id": provider_id,
        "tmdb_tv_id": provider_id,
        "title": title,
        "original_title": original_title,
        "first_air_date": str(value.get("first_air_date") or ""),
        "seasons": seasons,
    }


@dataclass(frozen=True)
class CohortAuthorityDraft:
    """Explicit user-confirmation input; never inferred from a Plan."""

    boundary_decision: Mapping[str, object]
    work_identity: Mapping[str, object]
    resolution: ProviderLinkResolution
    provider_snapshot: Mapping[str, object]
    title_selection: Mapping[str, object]

    @classmethod
    def from_confirmation(
        cls,
        *,
        boundary_decision: Mapping[str, object],
        work_identity: Mapping[str, object],
        resolution: ProviderLinkResolution,
        provider_snapshot: Mapping[str, object],
        title_selection: Mapping[str, object],
    ) -> "CohortAuthorityDraft":
        """合包写入口已退役，历史数据只供读取。"""
        raise ValueError('合包功能已退役，请按单条目重新整理；原任务保留')

    def to_dict(self) -> dict[str, object]:
        return {
            "boundary_decision": _plain(self.boundary_decision),
            "work_identity": _plain(self.work_identity),
            "resolution": _resolution_payload(self.resolution),
            "provider_snapshot": _plain(self.provider_snapshot),
            "title_selection": _plain(self.title_selection),
        }

    def bind(
        self,
        source_decision: DecisionState,
        *,
        authority_revision: int,
    ) -> "CohortAuthority":
        rows = tuple(
            dict(item)
            for item in source_decision.provider_subject_boundary_decisions.values()
        )
        if not rows or any(
            str(row.get("decision") or "") != "same_work" for row in rows
        ):
            raise SuccessorAuthorityError("authority_source_decision_unconfirmed")
        cohort_decision_id = str(self.boundary_decision.get("cohort_decision_id") or "")
        if not cohort_decision_id or any(
            str(row.get("cohort_decision_id") or "") != cohort_decision_id
            for row in rows
        ):
            raise SuccessorAuthorityError("authority_source_decision_mismatch")
        first = rows[0]
        subject_ids = tuple(str(item) for item in first.get("cohort_subject_ids", ()) or ())
        assignments = {
            str(key): value
            for key, value in dict(first.get("season_assignments") or {}).items()
        }
        if (
            not subject_ids
            or set(assignments) != set(subject_ids)
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                   for value in assignments.values())
        ):
            raise SuccessorAuthorityError("authority_season_assignments_invalid")
        work_identity = dict(_plain(self.work_identity))
        if work_identity != dict(_plain(source_decision.confirmed_work)):
            raise SuccessorAuthorityError("authority_work_identity_mismatch")
        primary = work_identity.get("primary_provider")
        if not isinstance(primary, Mapping):
            raise SuccessorAuthorityError("authority_work_identity_invalid")
        tmdb_tv_id = str(first.get("selected_tmdb_tv_id") or "")
        primary_key = _provider_key(primary)
        bangumi_primary = (
            primary_key[:2] == ("bangumi", "subject")
            and primary_key[2] in {
                item.casefold() for item in subject_ids
            }
        )
        legacy_tmdb_primary = (
            bool(tmdb_tv_id)
            and primary_key == ("tmdb", "tv", tmdb_tv_id.casefold())
        )
        if (
            not (bangumi_primary or legacy_tmdb_primary)
            or str(self.resolution.tmdb_tv_id or "") != tmdb_tv_id
            or dict(self.resolution.season_assignments) != assignments
            or (
                bool(tmdb_tv_id)
                and str(self.provider_snapshot.get("provider_id") or "")
                != tmdb_tv_id
            )
            or (not tmdb_tv_id and bool(self.provider_snapshot))
        ):
            raise SuccessorAuthorityError("authority_provider_identity_mismatch")
        title_selection = dict(_plain(self.title_selection))
        if (
            str(title_selection.get("namespace") or "").casefold() != "bangumi"
            or str(title_selection.get("entity_type") or "").casefold() != "subject"
            or str(title_selection.get("provider_id") or "") not in subject_ids
            or not str(title_selection.get("title") or "")
        ):
            raise SuccessorAuthorityError("authority_title_selection_invalid")
        identity_payload = {
            "work_identity": _provider_key(primary),
            "cohort_subject_ids": list(subject_ids),
        }
        authority_id = "cohort-" + _sha256(
            identity_payload, domain="bar.successor-authority-identity"
        )
        return CohortAuthority.create(
            authority_id=authority_id,
            authority_revision=authority_revision,
            source_task_id=source_decision.task_id,
            source_decision_revision=source_decision.revision,
            source_decision_fingerprint=DecisionStateStore.fingerprint(source_decision),
            source_cohort_projection_fingerprint=(
                cohort_projection_fingerprint_for_decision(source_decision)
            ),
            cohort_decision_id=cohort_decision_id,
            cohort_subject_ids=subject_ids,
            cohort_edges=tuple(
                dict(item) for item in first.get("cohort_edges", ()) or ()
                if isinstance(item, Mapping)
            ),
            subject_evidence_fingerprints=dict(
                first.get("subject_evidence_fingerprints") or {}
            ),
            evidence_fingerprint=str(first.get("evidence_fingerprint") or ""),
            resolution_fingerprint=str(first.get("resolution_fingerprint") or ""),
            season_assignments=assignments,
            work_identity=work_identity,
            provider_snapshot=dict(self.provider_snapshot),
            title_selection=title_selection,
            resolution=_resolution_payload(self.resolution),
        )


@dataclass(frozen=True)
class CohortAuthority:
    authority_id: str
    authority_revision: int
    source_task_id: str
    source_decision_revision: int
    source_decision_fingerprint: str
    source_cohort_projection_fingerprint: str
    cohort_decision_id: str
    cohort_subject_ids: tuple[str, ...]
    cohort_edges: tuple[Mapping[str, object], ...]
    subject_evidence_fingerprints: Mapping[str, object]
    evidence_fingerprint: str
    resolution_fingerprint: str
    season_assignments: Mapping[str, int]
    work_identity: Mapping[str, object]
    provider_snapshot: Mapping[str, object]
    title_selection: Mapping[str, object]
    resolution: Mapping[str, object]
    authority_fingerprint: str
    status: str = "current"
    schema_version: int = SUCCESSOR_AUTHORITY_SCHEMA_VERSION
    policy_version: str = SUCCESSOR_AUTHORITY_POLICY_VERSION

    @classmethod
    def create(cls, **values) -> "CohortAuthority":
        candidate = cls(authority_fingerprint="", **values)
        _reject_target_routing(candidate._payload())
        fingerprint = _sha256(
            candidate._payload(), domain="bar.successor-authority"
        )
        record = cls(**{
            **candidate.__dict__, "authority_fingerprint": fingerprint
        })
        return cls.from_dict(record.to_dict())

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "status": self.status,
            "authority_id": self.authority_id,
            "authority_revision": self.authority_revision,
            "source_task_id": self.source_task_id,
            "source_decision_revision": self.source_decision_revision,
            "source_decision_fingerprint": self.source_decision_fingerprint,
            "source_cohort_projection_fingerprint": (
                self.source_cohort_projection_fingerprint
            ),
            "cohort_decision_id": self.cohort_decision_id,
            "cohort_subject_ids": list(self.cohort_subject_ids),
            "cohort_edges": [_plain(item) for item in self.cohort_edges],
            "subject_evidence_fingerprints": _plain(
                self.subject_evidence_fingerprints
            ),
            "evidence_fingerprint": self.evidence_fingerprint,
            "resolution_fingerprint": self.resolution_fingerprint,
            "season_assignments": dict(self.season_assignments),
            "work_identity": _plain(self.work_identity),
            "provider_snapshot": _plain(self.provider_snapshot),
            "title_selection": _plain(self.title_selection),
            "resolution": _plain(self.resolution),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._payload(), "authority_fingerprint": self.authority_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CohortAuthority":
        allowed = {
            "schema_version", "policy_version", "status", "authority_id",
            "authority_revision", "source_task_id", "source_decision_revision",
            "source_decision_fingerprint", "source_cohort_projection_fingerprint",
            "cohort_decision_id",
            "cohort_subject_ids", "cohort_edges", "subject_evidence_fingerprints",
            "evidence_fingerprint", "resolution_fingerprint", "season_assignments",
            "work_identity", "provider_snapshot", "title_selection", "resolution",
            "authority_fingerprint",
        }
        if set(value) != allowed:
            raise SuccessorAuthorityError("authority_schema_fields_invalid")
        if (
            (value.get("schema_version"), value.get("policy_version")) not in {
                (SUCCESSOR_AUTHORITY_SCHEMA_VERSION, SUCCESSOR_AUTHORITY_POLICY_VERSION),
                (2, "provider-subject-cohort-v2"),
            }
            or value.get("status") != "current"
            or _AUTHORITY_ID.fullmatch(str(value.get("authority_id") or "")) is None
            or not isinstance(value.get("authority_revision"), int)
            or isinstance(value.get("authority_revision"), bool)
            or int(value["authority_revision"]) < 1
            or not isinstance(value.get("source_decision_revision"), int)
            or isinstance(value.get("source_decision_revision"), bool)
            or int(value["source_decision_revision"]) < 1
            or _HEX64.fullmatch(str(value.get("source_decision_fingerprint") or "")) is None
            or _HEX64.fullmatch(str(
                value.get("source_cohort_projection_fingerprint") or ""
            )) is None
            or _HEX64.fullmatch(str(value.get("authority_fingerprint") or "")) is None
            or not str(value.get("source_task_id") or "")
            or _AUTHORITY_ID.fullmatch(
                str(value.get("cohort_decision_id") or "")
            ) is None
            or _HEX64.fullmatch(str(value.get("evidence_fingerprint") or "")) is None
            or _HEX64.fullmatch(str(value.get("resolution_fingerprint") or "")) is None
        ):
            raise SuccessorAuthorityError("authority_header_invalid")
        subject_ids = value.get("cohort_subject_ids")
        edges = value.get("cohort_edges")
        assignments = value.get("season_assignments")
        mappings = {
            name: value.get(name)
            for name in (
                "subject_evidence_fingerprints", "work_identity",
                "provider_snapshot", "title_selection", "resolution",
            )
        }
        if (
            not isinstance(subject_ids, list)
            or not subject_ids
            or not all(isinstance(item, str) and item for item in subject_ids)
            or len(subject_ids) != len(set(subject_ids))
            or not isinstance(edges, list)
            or not edges
            or not all(isinstance(item, Mapping) for item in edges)
            or not isinstance(assignments, Mapping)
            or any(not isinstance(item, Mapping) for item in mappings.values())
        ):
            raise SuccessorAuthorityError("authority_payload_invalid")
        record = cls(
            authority_id=str(value["authority_id"]),
            authority_revision=int(value["authority_revision"]),
            source_task_id=str(value.get("source_task_id") or ""),
            source_decision_revision=int(value["source_decision_revision"]),
            source_decision_fingerprint=str(value["source_decision_fingerprint"]),
            source_cohort_projection_fingerprint=str(
                value["source_cohort_projection_fingerprint"]
            ),
            cohort_decision_id=str(value.get("cohort_decision_id") or ""),
            cohort_subject_ids=tuple(subject_ids),
            cohort_edges=tuple(dict(item) for item in edges),
            subject_evidence_fingerprints=dict(mappings["subject_evidence_fingerprints"]),
            evidence_fingerprint=str(value.get("evidence_fingerprint") or ""),
            resolution_fingerprint=str(value.get("resolution_fingerprint") or ""),
            season_assignments={str(key): item for key, item in assignments.items()},
            work_identity=dict(mappings["work_identity"]),
            provider_snapshot=dict(mappings["provider_snapshot"]),
            title_selection=dict(mappings["title_selection"]),
            resolution=dict(mappings["resolution"]),
            authority_fingerprint=str(value["authority_fingerprint"]),
            status=str(value["status"]),
            schema_version=int(value["schema_version"]),
            policy_version=str(value["policy_version"]),
        )
        if record.authority_fingerprint != _sha256(
            record._payload(), domain="bar.successor-authority"
        ):
            raise SuccessorAuthorityError("authority_fingerprint_mismatch")
        if set(record.season_assignments) != set(record.cohort_subject_ids) or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in record.season_assignments.values()
        ):
            raise SuccessorAuthorityError("authority_season_assignments_invalid")
        if (
            set(record.subject_evidence_fingerprints)
            != set(record.cohort_subject_ids)
            or any(
                _HEX64.fullmatch(str(item or "")) is None
                for item in record.subject_evidence_fingerprints.values()
            )
        ):
            raise SuccessorAuthorityError("authority_subject_evidence_invalid")
        resolution = _resolution_from_payload(record.resolution)
        snapshot = (
            _normalized_provider_snapshot(record.provider_snapshot)
            if resolution.tmdb_tv_id else {}
        )
        if not resolution.tmdb_tv_id and record.provider_snapshot:
            raise SuccessorAuthorityError("authority_provider_snapshot_invalid")
        primary = record.work_identity.get("primary_provider")
        expected_authority_id = "cohort-" + _sha256(
            {
                "work_identity": _provider_key(primary),
                "cohort_subject_ids": list(record.cohort_subject_ids),
            },
            domain="bar.successor-authority-identity",
        ) if isinstance(primary, Mapping) else ""
        if (
            record.authority_id != expected_authority_id
            or not isinstance(primary, Mapping)
            or not (
                (
                    _provider_key(primary)[:2] == ("bangumi", "subject")
                    and _provider_key(primary)[2]
                    in {item.casefold() for item in record.cohort_subject_ids}
                )
                or (
                    bool(snapshot)
                    and _provider_key(primary)
                    == (
                        "tmdb",
                        "tv",
                        str(snapshot["provider_id"]).casefold(),
                    )
                )
            )
            or str(resolution.tmdb_tv_id or "")
            != str(snapshot.get("provider_id") or "")
            or dict(resolution.season_assignments)
            != dict(record.season_assignments)
        ):
            raise SuccessorAuthorityError("authority_identity_invalid")
        title_selection = record.title_selection
        if (
            str(title_selection.get("namespace") or "").casefold() != "bangumi"
            or str(title_selection.get("entity_type") or "").casefold()
            != "subject"
            or str(title_selection.get("provider_id") or "")
            not in record.cohort_subject_ids
            or not str(title_selection.get("title") or "")
        ):
            raise SuccessorAuthorityError("authority_title_selection_invalid")
        _reject_target_routing(record.to_dict())
        return record

    def project(
        self, selected_subject_id: str
    ) -> tuple[str, dict[str, object], ProviderLinkResolution, dict[str, object]]:
        subject_id = str(selected_subject_id)
        if subject_id not in self.cohort_subject_ids:
            raise SuccessorAuthorityError("authority_subject_mismatch")
        season_number = self.season_assignments.get(subject_id)
        if not isinstance(season_number, int) or isinstance(season_number, bool):
            raise SuccessorAuthorityError("authority_subject_season_missing")
        resolution = _resolution_from_payload(self.resolution)
        if (
            dict(resolution.season_assignments) != dict(self.season_assignments)
            or str(resolution.tmdb_tv_id or "")
            != str(self.provider_snapshot.get("provider_id") or "")
        ):
            raise SuccessorAuthorityError("authority_resolution_drift")
        primary = {'namespace': 'bangumi', 'entity_type': 'subject', 'provider_id': subject_id}
        if resolution.tmdb_tv_id:
            snapshot = _normalized_provider_snapshot(self.provider_snapshot)
            info = {
                "id": int(snapshot["provider_id"])
                if str(snapshot["provider_id"]).isdigit()
                else snapshot["provider_id"],
                "name": snapshot["title"],
                "original_name": snapshot["original_title"],
                "first_air_date": snapshot["first_air_date"],
                "genres": [{"name": "Animation"}],
                "seasons": [
                    {**dict(item), "name": f"Season {item['season_number']}"}
                    for item in snapshot["seasons"]
                ],
                "_bar_primary_provider": primary,
                "_bar_linked_provider_identities": ({
                    "namespace": "tmdb",
                    "entity_type": "tv",
                    "provider_id": str(snapshot["provider_id"]),
                },),
            }
        else:
            counts: dict[int, int] = {}
            for row in resolution.derived_episode_ranges:
                if not isinstance(row, Mapping):
                    continue
                season = row.get("season_number")
                start = row.get("episode_start")
                end = row.get("episode_end")
                if (
                    isinstance(season, int)
                    and not isinstance(season, bool)
                    and isinstance(start, int)
                    and not isinstance(start, bool)
                    and isinstance(end, int)
                    and not isinstance(end, bool)
                    and end >= start
                ):
                    counts[season] = max(counts.get(season, 0), end - start + 1)
            seasons = [
                {
                    "season_number": season,
                    "name": f"Season {season}",
                    "air_date": "",
                    "episode_count": counts.get(season, 0),
                }
                for season in sorted(set(self.season_assignments.values()))
            ]
            info = {
                "id": str(primary.get("provider_id") or ""),
                "name": str(self.work_identity.get("title") or ""),
                "original_name": str(
                    self.title_selection.get("original_title")
                    or self.title_selection.get("title")
                    or self.work_identity.get("title")
                    or ""
                ),
                "first_air_date": str(self.title_selection.get('air_date') or self.title_selection.get('date') or ''),
                "genres": [{"name": "Animation"}],
                "seasons": seasons,
                "_bar_primary_provider": primary,
                "_bar_linked_provider_identities": (),
            }
        return (
            str(self.work_identity.get("title") or info["name"]),
            info,
            resolution,
            dict(self.title_selection),
        )


class CohortAuthorityStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, authority_id: str) -> Path:
        if _AUTHORITY_ID.fullmatch(str(authority_id)) is None:
            raise SuccessorAuthorityError("authority_id_invalid")
        return self.root / f"{authority_id}.json"

    def load_optional(self, authority_id: str) -> Optional[CohortAuthority]:
        path = self.path_for(authority_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise SuccessorAuthorityError("authority_record_unreadable") from exc
        if not isinstance(value, Mapping):
            raise SuccessorAuthorityError("authority_record_invalid")
        record = CohortAuthority.from_dict(value)
        if record.authority_id != authority_id:
            raise SuccessorAuthorityError("authority_id_mismatch")
        return record

    def list_current(self) -> tuple[CohortAuthority, ...]:
        if not self.root.exists():
            return ()
        records = []
        for path in sorted(self.root.glob("*.json"), key=lambda item: item.name):
            if _AUTHORITY_ID.fullmatch(path.stem) is None:
                raise SuccessorAuthorityError("authority_store_unknown_record")
            record = self.load_optional(path.stem)
            if record is not None:
                records.append(record)
        return tuple(records)

    def preview(
        self,
        draft: CohortAuthorityDraft,
        source_decision: DecisionState,
    ) -> tuple[CohortAuthority, Optional[CohortAuthority]]:
        initial = draft.bind(source_decision, authority_revision=1)
        previous = self.load_optional(initial.authority_id)
        revision = previous.authority_revision + 1 if previous is not None else 1
        return draft.bind(source_decision, authority_revision=revision), previous

    def list_for_subject(self, subject_id: str) -> tuple[CohortAuthority, ...]:
        """Validate scope before body; unrelated legacy records are never accepted."""
        records = []
        for path in sorted(self.root.glob("*.json"), key=lambda item: item.name):
            if _AUTHORITY_ID.fullmatch(path.stem) is None:
                raise SuccessorAuthorityError("authority_store_unknown_record")
            try:
                payload = json.loads(path.read_text("utf-8"))
            except (OSError, TypeError, ValueError) as exc:
                raise SuccessorAuthorityError("authority_record_unreadable") from exc
            if not isinstance(payload, Mapping):
                raise SuccessorAuthorityError("authority_record_invalid")
            ids = payload.get("cohort_subject_ids")
            work = payload.get("work_identity")
            primary = work.get("primary_provider") if isinstance(work, Mapping) else None
            if (not isinstance(ids, (list, tuple)) or not ids
                    or any(not isinstance(i, str) or not i.isascii() or not i.isdigit()
                           or str(int(i)) != i or int(i) < 1 for i in ids)
                    or len(set(ids)) != len(ids) or not isinstance(primary, Mapping)):
                raise SuccessorAuthorityError("authority_scope_invalid")
            key = _provider_key(primary)
            expected = "cohort-" + _sha256(
                {"work_identity": key, "cohort_subject_ids": list(ids)},
                domain="bar.successor-authority-identity",
            )
            if (payload.get("authority_id") != path.stem or expected != path.stem
                    or key[:2] not in {("bangumi", "subject"), ("tmdb", "tv")}
                    or not key[2] or (key[:2] == ("bangumi", "subject") and key[2] not in ids)):
                raise SuccessorAuthorityError("authority_scope_invalid")
            if subject_id in ids:
                records.append(CohortAuthority.from_dict(payload))
        return tuple(records)

    def publish_prepared(
        self,
        record: CohortAuthority,
        *,
        expected_previous_fingerprint: str,
    ) -> CohortAuthority:
        """合包写入口已退役，历史数据只供读取。"""
        raise ValueError('合包功能已退役，请按单条目重新整理；原任务保留')

    def restore_exact(
        self,
        current: CohortAuthority,
        previous: Optional[CohortAuthority],
    ) -> None:
        with self._lock():
            stored = self.load_optional(current.authority_id)
            if (
                stored is None
                or stored.authority_fingerprint != current.authority_fingerprint
            ):
                raise SuccessorAuthorityError("authority_changed_before_rollback")
            if previous is None:
                self.path_for(current.authority_id).unlink(missing_ok=True)
            else:
                self._write(previous)

    def _write(self, record: CohortAuthority) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.path_for(record.authority_id)
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "x", encoding="utf-8") as handle:
                json.dump(record.to_dict(), handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with advisory_file_lock(
                self.root / ".authority.lock",
                owner={"kind": "provider_subject_cohort_authority"},
            ):
                yield
        except FileLockBusyError as exc:
            raise SuccessorAuthorityError("authority_store_busy") from exc


@dataclass(frozen=True)
class CohortAuthorityLookup:
    status: str
    reason_code: str
    authority: Optional[CohortAuthority] = None


def _relationship_semantics(record: CohortAuthority) -> dict[str, object]:
    """Compare owned facts, never producer/request identity or display coincidence."""
    resolution = record.resolution
    title = record.title_selection
    return {
        'members': record.cohort_subject_ids,
        'seasons': _plain(record.season_assignments),
        'edges': [{k: _plain(v) for k, v in edge.items() if k != 'evidence_fingerprint'}
                  for edge in record.cohort_edges],
        'work': {k: _plain(record.work_identity.get(k)) for k in ('domain','title','year')},
        'provider_snapshot': _plain(record.provider_snapshot),
        'title': {k: str(title.get(k) or '') for k in
                  ('namespace','entity_type','provider_id','title','original_title','localized_title')},
        # With TMDB, the complete normalized provider snapshot owns this date.
        # A newly added optional Bangumi air_date must not invalidate old copies.
        'date': '' if resolution.get('tmdb_tv_id') else str(title.get('air_date') or title.get('date') or ''),
        'resolution': {k: _plain(resolution.get(k)) for k in
                       ('tmdb_tv_id','work_segment','derived_episode_ranges','boundaries',
                        'mapping_shape','season_count_policy_version')},
    }


class CohortAuthorityReader:
    """Read versioned relationship facts; producer task identity is audit only.

    V2 depended on mutable task state. It is read-only discovery input, never
    upgraded implicitly. Explicit confirmation publishes V3 through the same
    Decision transaction and store CAS. Catalog/approval still own execution.
    """

    def __init__(self, store: CohortAuthorityStore, decision_store: DecisionStateStore):
        self.store = store
        self.decision_store = decision_store  # compatibility, never a fact source

    @classmethod
    def from_decision_root(cls, decision_root: Path) -> "CohortAuthorityReader":
        return cls(CohortAuthorityStore(successor_authority_root(decision_root)),
                   DecisionStateStore(decision_root))

    def lookup(self, selected_subject_id: str, *, consumer_task_id: str) -> CohortAuthorityLookup:
        from .series_registration import SeriesRegistrationAuthority
        subject_id = str(selected_subject_id or "").strip()
        if not subject_id or not str(consumer_task_id or "").strip():
            return CohortAuthorityLookup("invalid", "consumer_identity_invalid")
        try:
            paths = tuple(sorted(self.store.root.glob("*.json")))
            before = {str(p): p.read_bytes() for p in paths}
            matches = self.store.list_for_subject(subject_id)
            current = [row for row in matches
                       if row.schema_version == SUCCESSOR_AUTHORITY_SCHEMA_VERSION]
            if not current:
                return CohortAuthorityLookup("no_current_authority",
                    "legacy_requires_confirmation" if matches else "no_current_authority")
            semantics = _relationship_semantics(current[0])
            identities = set()
            for record in current:
                if _relationship_semantics(record) != semantics:
                    return CohortAuthorityLookup("conflict", "competing_current_authority")
                proof = SeriesRegistrationAuthority.from_mapping(
                    record.resolution.get("registration_authority"))
                if (proof.namespace != "bangumi"
                        or proof.series_key != record.resolution.get("proposed_series_key")
                        or proof.season_membership != tuple(
                            (s, record.season_assignments[s]) for s in record.cohort_subject_ids)):
                    return CohortAuthorityLookup("invalid", "authority_registration_mismatch")
                identities.add((proof.namespace, proof.anchor_provider_id,
                                proof.season_membership, proof.series_key))
                record.project(subject_id)
            if len(identities) != 1:
                return CohortAuthorityLookup("conflict", "competing_current_authority")
            after = {str(p): p.read_bytes() for p in sorted(self.store.root.glob("*.json"))}
            if before != after:
                return CohortAuthorityLookup("invalid", "authority_read_drift")
            anchor = next(iter(identities))[1]
            chosen = min(current, key=lambda row: (
                str(row.work_identity["primary_provider"].get("provider_id")) != anchor,
                row.authority_id))
            return CohortAuthorityLookup("matched", "current_authority_matched", chosen)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            return CohortAuthorityLookup("invalid",
                getattr(exc, "reason_code", "authority_registration_invalid"))
