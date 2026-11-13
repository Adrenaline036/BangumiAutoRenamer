"""Provider-link resolution from frozen local and provider evidence."""

from __future__ import annotations

import unicodedata
import uuid
import re
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from .provider_link_catalog import (
    ProviderLinkCatalog,
    ProviderLinkMapping,
    ProviderLinkMatch,
    match_mapping_provider,
)


@dataclass(frozen=True)
class ProviderLinkResolution:
    automation_level: str
    status: str
    tmdb_tv_id: Optional[str] = None
    proposed_series_key: Optional[str] = None
    season_assignments: tuple[tuple[str, int], ...] = ()
    requires_plan_review: bool = True
    evidence_path: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    series_key: Optional[str] = None
    work_key: Optional[str] = None
    season_number: Optional[int] = None
    anchor_mapping_id: Optional[str] = None
    anchor_revision: Optional[int] = None
    anchor_fingerprint: Optional[str] = None
    work_segment: Optional[Mapping[str, object]] = None
    derived_episode_ranges: tuple[Mapping[str, object], ...] = ()
    boundaries: tuple[Mapping[str, object], ...] = ()
    mapping_shape: Optional[str] = None
    review_disposition: Optional[str] = None
    resolution_fingerprint: Optional[str] = None
    season_count_policy_version: Optional[str] = None
    subject_observations: Optional[Mapping[str, object]] = None
    registration_authority: Optional[Mapping[str, object]] = None




class ProviderCohortAnchorError(ValueError):
    """Frozen cohort evidence cannot supply one trustworthy TV anchor."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def resolve_provider_link(**kwargs) -> ProviderLinkResolution:
    resolution = _resolve_provider_link(**kwargs)
    if resolution.status == 'inferred' and not resolution.anchor_mapping_id and resolution.season_assignments and resolution.evidence_path:
        from . import series_registration
        resolution, _ = series_registration.bind_resolution(resolution)
    return resolution


def _resolve_provider_link(
    *,
    selected_provider: Mapping[str, object],
    existing_links: Iterable[ProviderLinkMapping] = (),
    provider_link_catalog: Optional[ProviderLinkCatalog] = None,
    bangumi_subjects: Iterable[Mapping[str, object]] = (),
    bangumi_relations: Iterable[Mapping[str, object]] = (),
    tmdb_candidates: Iterable[Mapping[str, object]] = (),
    subject_observations: Optional[Mapping[str, object]] = None,
    fetch_bangumi: Optional[Callable[..., object]] = None,
    fetch_tmdb: Optional[Callable[..., object]] = None,
) -> ProviderLinkResolution:
    """Prefer L0, then infer from already-frozen G2 evidence.

    Fetch callbacks are explicit so misspelled/future arguments fail loudly.
    G2 deliberately does not schedule network work; callers must provide bounded
    snapshots.  Most importantly, L0 never invokes either callback.
    """

    match = None
    known_links = tuple(existing_links)
    if provider_link_catalog is not None:
        match = provider_link_catalog.find_exact(
            selected_provider, reusable_only=True
        )
        known_links = known_links + provider_link_catalog.list_mappings()
    else:
        matches = []
        for mapping in known_links:
            if not isinstance(mapping, ProviderLinkMapping):
                continue
            if not mapping.reusable_offline:
                continue
            candidate = match_mapping_provider(mapping, selected_provider)
            if candidate is not None:
                matches.append(candidate)
        if len(matches) == 1:
            match = matches[0]

    if match is not None:
        return _l0_resolution(match)
    if subject_observations is not None:
        return _resolve_subject_observations(
            selected_provider=selected_provider,
            bangumi_subjects=tuple(bangumi_subjects),
            bangumi_relations=tuple(bangumi_relations),
            batch=subject_observations,
        )
    resolution = _resolve_frozen_evidence(
        selected_provider=selected_provider,
        bangumi_subjects=tuple(bangumi_subjects),
        bangumi_relations=tuple(bangumi_relations),
        tmdb_candidates=tuple(tmdb_candidates),
        fetch_callbacks_present=fetch_bangumi is not None or fetch_tmdb is not None,
    )
    return _apply_confirmed_anchor(resolution, known_links)


_SUBJECT_OBSERVATION_POLICY = "subject-candidate-v1"
_SEASON_COUNT_POLICY = "date-count-delta-v1"
SAME_ID_AGGREGATE_SUPPORT = "same_identity_aggregate_support"


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def trusted_relation_tmdb_anchor(
    subject_batch: Mapping[str, object],
    tmdb_tv_id: str,
    *,
    require_complete: bool = True,
) -> Mapping[str, object]:
    """Return one complete and concordant exact snapshot from frozen evidence.

    Resolver generation and UI confirmation deliberately share this pure gate.
    It performs no provider or filesystem I/O and rejects incomplete evidence.
    """

    if not isinstance(subject_batch, Mapping):
        raise ProviderCohortAnchorError("subject_observation_batch_invalid")
    if (
        subject_batch.get("schema_version") != 1
        or subject_batch.get("policy_version") != _SUBJECT_OBSERVATION_POLICY
        or type(subject_batch.get("truncated")) is not bool
        or type(subject_batch.get("boundary_complete")) is not bool
    ):
        raise ProviderCohortAnchorError("subject_observation_batch_invalid")
    if require_complete and (
        bool(subject_batch.get("truncated"))
        or not bool(subject_batch.get("boundary_complete"))
    ):
        raise ProviderCohortAnchorError("subject_evidence_truncated")
    batch_payload = {
        key: value
        for key, value in subject_batch.items()
        if key != "evidence_fingerprint"
    }
    if subject_batch.get("evidence_fingerprint") != _canonical_sha256(
        batch_payload
    ):
        raise ProviderCohortAnchorError("subject_evidence_fingerprint_mismatch")
    requested_id = str(tmdb_tv_id or "")
    if not requested_id:
        raise ProviderCohortAnchorError("cohort_anchor_identity_invalid")

    candidates = []
    seen_subjects = set()
    observations = subject_batch.get("subject_observations", ()) or ()
    if not isinstance(observations, (list, tuple)) or not observations:
        raise ProviderCohortAnchorError("subject_candidate_evidence_missing")
    for observation in observations:
        if not isinstance(observation, Mapping):
            raise ProviderCohortAnchorError("subject_observation_invalid")
        subject_id = str(observation.get("bangumi_subject_id") or "")
        if not subject_id or subject_id in seen_subjects:
            raise ProviderCohortAnchorError("subject_observation_duplicate")
        seen_subjects.add(subject_id)
        if observation.get("policy_version") != _SUBJECT_OBSERVATION_POLICY:
            raise ProviderCohortAnchorError("subject_observation_policy_unsupported")
        observation_payload = {
            key: value
            for key, value in observation.items()
            if key != "response_fingerprint"
        }
        if observation.get("response_fingerprint") != _canonical_sha256(
            observation_payload
        ):
            raise ProviderCohortAnchorError(
                "subject_observation_fingerprint_mismatch"
            )
        queries = observation.get("queries", ()) or ()
        if not isinstance(queries, (list, tuple)) or not queries:
            raise ProviderCohortAnchorError("subject_candidate_evidence_missing")
        for query in queries:
            if not isinstance(query, Mapping) or type(
                query.get("truncated")
            ) is not bool:
                raise ProviderCohortAnchorError("subject_observation_invalid")
            if bool(query.get("truncated")):
                raise ProviderCohortAnchorError("subject_query_truncated")
            outcome = str(query.get("outcome") or "")
            query_candidates = query.get("candidates", ()) or ()
            if not isinstance(query_candidates, (list, tuple)):
                raise ProviderCohortAnchorError("subject_observation_invalid")
            if outcome != "ok" and query_candidates:
                raise ProviderCohortAnchorError(
                    "subject_query_outcome_inconsistent"
                )
            if outcome == "transport_error":
                raise ProviderCohortAnchorError(
                    "subject_candidate_transport_error"
                )
            if outcome == "invalid_response":
                raise ProviderCohortAnchorError(
                    "subject_candidate_invalid_response"
                )
            if outcome == "budget_exhausted":
                raise ProviderCohortAnchorError(
                    "subject_candidate_budget_exhausted"
                )
            if outcome not in {"ok", "no_match"}:
                raise ProviderCohortAnchorError(
                    "subject_query_outcome_unsupported"
                )
            for candidate in query_candidates:
                if not isinstance(candidate, Mapping):
                    raise ProviderCohortAnchorError("subject_observation_invalid")
                candidate_id = str(candidate.get("tmdb_tv_id") or "")
                provider_id = str(candidate.get("provider_id") or candidate_id)
                if candidate.get("match_tier") == "exact" and (
                    candidate.get("namespace") != "tmdb"
                    or candidate.get("entity_type") != "tv"
                    or not candidate_id
                    or provider_id != candidate_id
                ):
                    raise ProviderCohortAnchorError(
                        "subject_candidate_identity_invalid"
                    )
                if (
                    candidate.get("match_tier") == "exact"
                    and candidate_id == requested_id
                ):
                    candidates.append(dict(candidate))
    if not candidates:
        raise ProviderCohortAnchorError("subject_candidate_evidence_missing")
    snapshots = {
        json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for candidate in candidates
    }
    if len(snapshots) != 1:
        raise ProviderCohortAnchorError("cohort_anchor_snapshot_conflict")
    return json.loads(next(iter(snapshots)))


def collect_subject_observations(
    *,
    selected_subject_id: str,
    bangumi_subjects: Iterable[Mapping[str, object]],
    bangumi_relations: Iterable[Mapping[str, object]],
    search_subject: Callable[[Mapping[str, object]], Mapping[str, object]],
    max_subjects: int,
) -> Mapping[str, object]:
    """Collect a bounded, selected-first, content-addressed observation batch.

    This is a pure adapter boundary: callers perform transport work and return
    raw observations; this function normalizes TMDB candidate identity and
    freezes the evidence fingerprints consumed by the resolver.
    """

    if not isinstance(max_subjects, int) or isinstance(max_subjects, bool) or max_subjects <= 0:
        raise ValueError("max_subjects must be a positive integer")
    subjects = {
        _provider_id(item): item
        for item in bangumi_subjects
        if _provider_id(item)
    }
    if selected_subject_id not in subjects:
        raise ValueError("selected subject is missing from the bounded evidence set")
    adjacency = {subject_id: set() for subject_id in subjects}
    for relation in bangumi_relations:
        source = str(relation.get("from") or "")
        target = str(relation.get("to") or "")
        if source in adjacency and target in adjacency:
            adjacency[source].add(target)
            adjacency[target].add(source)
    traversal = []
    pending = [selected_subject_id]
    seen = set()
    while pending:
        subject_id = pending.pop(0)
        if subject_id in seen:
            continue
        seen.add(subject_id)
        traversal.append(subject_id)
        pending.extend(
            subject_id
            for subject_id in sorted(adjacency.get(subject_id, ()))
            if subject_id not in seen
        )
    selected_ids = traversal[:max_subjects]
    observations = []
    for subject_id in selected_ids:
        raw = search_subject(subjects[subject_id])
        if not isinstance(raw, Mapping):
            raise ValueError("search_subject must return an observation mapping")
        payload = {
            key: value
            for key, value in raw.items()
            if key != "response_fingerprint"
        }
        queries = []
        for query in payload.get("queries", ()) or ():
            if not isinstance(query, Mapping):
                raise ValueError("observation query must be a mapping")
            query_payload = dict(query)
            candidates = []
            for candidate in query_payload.get("candidates", ()) or ():
                if not isinstance(candidate, Mapping):
                    raise ValueError("observation candidate must be a mapping")
                candidate_payload = dict(candidate)
                if (
                    candidate_payload.get("namespace") == "tmdb"
                    and candidate_payload.get("entity_type") == "tv"
                ):
                    candidate_payload["tmdb_tv_id"] = str(
                        candidate_payload.get("tmdb_tv_id")
                        or candidate_payload.get("provider_id")
                        or ""
                    )
                candidates.append(candidate_payload)
            query_payload["candidates"] = tuple(candidates)
            queries.append(query_payload)
        payload["queries"] = tuple(
            sorted(
                queries,
                key=lambda item: (
                    str(item.get("locale") or ""),
                    str(item.get("query") or ""),
                    str(item.get("year") or ""),
                ),
            )
        )
        observations.append(
            {**payload, "response_fingerprint": _canonical_sha256(payload)}
        )
    observations.sort(key=lambda item: str(item.get("bangumi_subject_id") or ""))
    observed_at = next(
        (
            str(item.get("observed_at"))
            for item in observations
            if item.get("bangumi_subject_id") == selected_subject_id
            and item.get("observed_at")
        ),
        datetime.now(timezone.utc).isoformat(),
    )
    truncated = len(traversal) > len(selected_ids)
    batch = {
        "schema_version": 1,
        "selected_subject_id": selected_subject_id,
        "observed_at": observed_at,
        "policy_version": _SUBJECT_OBSERVATION_POLICY,
        "truncated": truncated,
        "boundary_complete": not truncated,
        "subject_observations": tuple(observations),
    }
    return {**batch, "evidence_fingerprint": _canonical_sha256(batch)}


def derive_source_episode_projection(
    *,
    source_episode_numbers: Iterable[object],
    target_episode_start: int,
    target_episode_end: int,
    inventory_scope: str,
) -> Mapping[str, object]:
    """Derive the only safe absolute/relative integer episode projection."""

    raw_numbers = tuple(source_episode_numbers)
    if any(
        not isinstance(value, int) or isinstance(value, bool)
        for value in raw_numbers
    ):
        return _projection_problem("blocked", "non_integer_main_episode")
    numbers = tuple(int(value) for value in raw_numbers)
    if len(numbers) != len(set(numbers)):
        return _projection_problem("blocked", "duplicate_source_episode")
    if inventory_scope not in {"complete", "partial", "unknown"}:
        return _projection_problem("blocked", "inventory_scope_invalid")
    if inventory_scope == "unknown":
        return _projection_problem(
            "manual_required", "unknown_inventory_confirmation_required"
        )
    ordered = tuple(sorted(numbers))
    if inventory_scope == "complete" and ordered:
        if ordered != tuple(range(ordered[0], ordered[-1] + 1)):
            return _projection_problem("blocked", "complete_inventory_episode_gap")
    if (
        not isinstance(target_episode_start, int)
        or isinstance(target_episode_start, bool)
        or not isinstance(target_episode_end, int)
        or isinstance(target_episode_end, bool)
        or target_episode_start <= 0
        or target_episode_end < target_episode_start
    ):
        return _projection_problem("blocked", "target_episode_range_invalid")

    candidates = []
    for mode, offset in (
        ("absolute", 0),
        ("relative", target_episode_start - 1),
    ):
        pairs = tuple((source, source + offset) for source in ordered)
        if not all(target_episode_start <= target <= target_episode_end for _, target in pairs):
            continue
        candidate = _projection_payload(
            mode=mode,
            offset=offset,
            inventory_scope=inventory_scope,
            target_episode_start=target_episode_start,
            target_episode_end=target_episode_end,
            pairs=pairs,
        )
        if not any(
            item["episode_mapping"] == candidate["episode_mapping"]
            for item in candidates
        ):
            candidates.append(candidate)
    if not candidates:
        return _projection_problem("blocked", "source_episode_projection_out_of_range")
    if len(candidates) > 1:
        return {
            "status": "manual_required",
            "reason_codes": ("source_episode_projection_ambiguous",),
            "candidates": tuple(candidates),
        }
    result = dict(candidates[0])
    result["status"] = (
        "manual_required" if inventory_scope == "partial" else "reviewable"
    )
    result["reason_codes"] = (
        ("partial_inventory_confirmation_required",)
        if inventory_scope == "partial"
        else ()
    )
    return result


def validate_derived_episode_ranges(
    ranges: Iterable[Mapping[str, object]],
) -> Mapping[str, object]:
    """Validate canonical TMDB-season range reservations without I/O."""

    normalized = []
    reasons = set()
    for item in ranges:
        if not isinstance(item, Mapping):
            reasons.add("episode_range_invalid")
            continue
        try:
            row = {
                "bangumi_subject_id": str(item.get("bangumi_subject_id") or ""),
                "tmdb_tv_id": str(item.get("tmdb_tv_id") or ""),
                "season_number": int(item["season_number"]),
                "episode_start": int(item["episode_start"]),
                "episode_end": int(item["episode_end"]),
            }
        except (KeyError, TypeError, ValueError):
            reasons.add("episode_range_invalid")
            continue
        if (
            not row["bangumi_subject_id"]
            or not row["tmdb_tv_id"]
            or row["season_number"] <= 0
            or row["episode_start"] <= 0
            or row["episode_end"] < row["episode_start"]
        ):
            reasons.add("episode_range_invalid")
        normalized.append(row)
    normalized.sort(
        key=lambda row: (
            row["tmdb_tv_id"],
            row["season_number"],
            row["episode_start"],
            row["episode_end"],
            row["bangumi_subject_id"],
        )
    )
    for index, left in enumerate(normalized):
        for right in normalized[index + 1 :]:
            if (
                left["tmdb_tv_id"] != right["tmdb_tv_id"]
                or left["season_number"] != right["season_number"]
            ):
                continue
            if max(left["episode_start"], right["episode_start"]) <= min(
                left["episode_end"], right["episode_end"]
            ):
                reasons.add("episode_range_overlap")
    fingerprint_payload = {"ranges": tuple(normalized)}
    return {
        "valid": not reasons,
        "reason_codes": tuple(sorted(reasons)),
        "validation_fingerprint": _canonical_sha256(fingerprint_payload),
    }


def _projection_payload(
    *,
    mode: str,
    offset: int,
    inventory_scope: str,
    target_episode_start: int,
    target_episode_end: int,
    pairs: tuple[tuple[int, int], ...],
) -> dict[str, object]:
    payload = {
        "mode": mode,
        "offset": offset,
        "inventory_scope": inventory_scope,
        "target_episode_start": target_episode_start,
        "target_episode_end": target_episode_end,
        "episode_mapping": tuple(
            {"source_episode": source, "target_episode": target}
            for source, target in pairs
        ),
    }
    return {**payload, "projection_fingerprint": _canonical_sha256(payload)}


def _projection_problem(status: str, reason: str) -> Mapping[str, object]:
    return {"status": status, "reason_codes": (reason,)}




def _l0_resolution(match: ProviderLinkMatch) -> ProviderLinkResolution:
    mapping = match.mapping
    evidence = mapping.to_dict()['evidence_snapshot']
    if isinstance(evidence.get('latest_evidence'), Mapping):
        evidence = evidence['latest_evidence']
    tmdb_tv_id = None
    for edge in mapping.provider_edges:
        if edge.get("namespace") == "tmdb" and edge.get("entity_type") == "tv":
            tmdb_tv_id = str(edge.get("provider_id"))
            break
    if tmdb_tv_id is None and match.edge_kind == "season":
        for edge in mapping.season_edges:
            if int(edge["season_number"]) == match.season_number:
                tmdb_tv_id = str(edge["tmdb_tv_id"])
                break
    assignments = tuple(
        (
            str(edge.get("provider_id") or ""),
            int(edge["season_number"]),
        )
        for edge in mapping.season_edges
        if edge.get("namespace") == "bangumi"
        and edge.get("entity_type") == "subject"
        and str(edge.get("provider_id") or "")
        and isinstance(edge.get("season_number"), int)
    )
    return ProviderLinkResolution(
        automation_level="L0",
        status=mapping.status,
        tmdb_tv_id=tmdb_tv_id,
        proposed_series_key=mapping.local_series_key,
        season_assignments=assignments,
        requires_plan_review=False,
        evidence_path=(mapping.mapping_id, mapping.evidence_fingerprint),
        reason_codes=("materialized_exact_offline",),
        series_key=mapping.local_series_key,
        work_key=mapping.local_work_key,
        season_number=match.season_number,
        anchor_mapping_id=mapping.mapping_id,
        anchor_revision=mapping.revision,
        anchor_fingerprint=mapping.evidence_fingerprint,
        derived_episode_ranges=tuple(dict(bangumi_subject_id=str(e['provider_id']),
            tmdb_tv_id=str(e['tmdb_tv_id']), season_number=e['season_number'],
            episode_start=e['episode_start'], episode_end=e['episode_end'])
            for e in mapping.season_edges if e.get('episode_start') is not None and e.get('episode_end') is not None),
        work_segment=evidence.get('work_segment'),
        # Materialized ranges are current authority. Original discovery
        # observations are not a new provider-selection observation batch.
        subject_observations=None,
        mapping_shape=evidence.get('mapping_shape'),
        season_count_policy_version=evidence.get('season_count_policy_version'),
        resolution_fingerprint=evidence.get('resolution_fingerprint'),
    )


def _bangumi_standalone_resolution(
    *,
    selected_id: str,
    subject: Mapping[str, object],
    batch: Mapping[str, object],
    reason_code: str,
) -> ProviderLinkResolution:
    """Project one selected Bangumi subject without promoting TMDB metadata."""

    episode_count = subject.get("episode_count")
    if not isinstance(episode_count, int) or isinstance(
        episode_count, bool
    ) or episode_count < 1:
        episode_count = subject.get("eps")
    derived_ranges: tuple[Mapping[str, object], ...] = ()
    if isinstance(episode_count, int) and not isinstance(
        episode_count, bool
    ) and episode_count > 0:
        derived_ranges = ({
            "bangumi_subject_id": selected_id,
            "work_identity_key": f"bangumi:subject:{selected_id}",
            "season_number": 1,
            "episode_start": 1,
            "episode_end": int(episode_count),
        },)
    payload = {
        "primary_provider": ("bangumi", "subject", selected_id),
        "season_assignments": ((selected_id, 1),),
        "derived_episode_ranges": derived_ranges,
        "evidence_fingerprint": str(batch.get("evidence_fingerprint") or ""),
        "reason_code": reason_code,
    }
    return ProviderLinkResolution(
        automation_level="L2",
        status="inferred",
        tmdb_tv_id=None,
        season_assignments=((selected_id, 1),),
        requires_plan_review=True,
        evidence_path=(str(batch["evidence_fingerprint"]),),
        reason_codes=(reason_code,),
        work_segment={"subject_ids": (selected_id,)},
        derived_episode_ranges=derived_ranges,
        boundaries=(),
        mapping_shape="bangumi_standalone",
        review_disposition="reviewable",
        resolution_fingerprint=_canonical_sha256(payload),
        subject_observations=dict(batch),
    )


def _resolve_subject_observations(
    *,
    selected_provider: Mapping[str, object],
    bangumi_subjects: tuple[Mapping[str, object], ...],
    bangumi_relations: tuple[Mapping[str, object], ...],
    batch: Mapping[str, object],
) -> ProviderLinkResolution:
    selected_id = _provider_id(selected_provider)
    if (
        selected_provider.get("namespace") != "bangumi"
        or selected_provider.get("entity_type") != "subject"
        or not selected_id
    ):
        return _subject_problem("blocked", "crosswalk_inference_requires_bangumi_subject")
    if not isinstance(batch, Mapping):
        return _subject_problem("blocked", "subject_observation_batch_invalid")
    if (
        not isinstance(batch.get("schema_version"), int)
        or isinstance(batch.get("schema_version"), bool)
        or int(batch["schema_version"]) != 1
    ):
        return _subject_problem(
            "blocked", "subject_observation_batch_schema_unsupported"
        )
    if type(batch.get("truncated")) is not bool or type(
        batch.get("boundary_complete")
    ) is not bool:
        return _subject_problem("blocked", "subject_observation_batch_invalid")
    if batch.get("policy_version") != _SUBJECT_OBSERVATION_POLICY:
        return _subject_problem(
            "blocked", "subject_observation_batch_policy_unsupported"
        )
    batch_payload = {
        key: value for key, value in batch.items() if key != "evidence_fingerprint"
    }
    if batch.get("evidence_fingerprint") != _canonical_sha256(batch_payload):
        return _subject_problem("blocked", "subject_evidence_fingerprint_mismatch")
    if str(batch.get("selected_subject_id") or "") != selected_id:
        return _subject_problem(
            "blocked", "subject_observation_selected_mismatch"
        )
    observations = {}
    for item in batch.get("subject_observations", ()) or ():
        if not isinstance(item, Mapping):
            return _subject_problem("blocked", "subject_observation_invalid")
        subject_id = str(item.get("bangumi_subject_id") or "")
        if not subject_id:
            return _subject_problem("blocked", "subject_observation_invalid")
        if subject_id in observations:
            return _subject_problem("blocked", "subject_observation_duplicate")
        if item.get("policy_version") != _SUBJECT_OBSERVATION_POLICY:
            return _subject_problem(
                "blocked", "subject_observation_policy_unsupported"
            )
        observation_payload = {
            key: value
            for key, value in item.items()
            if key != "response_fingerprint"
        }
        if item.get("response_fingerprint") != _canonical_sha256(
            observation_payload
        ):
            return _subject_problem(
                "blocked", "subject_observation_fingerprint_mismatch"
            )
        observations[subject_id] = item

    subjects = {
        _provider_id(item): item
        for item in bangumi_subjects
        if _provider_id(item)
    }
    if selected_id not in subjects:
        return _subject_problem("blocked", "bangumi_subject_evidence_missing")
    sequel_edges = set()
    for relation in bangumi_relations:
        source = str(relation.get("from") or "")
        target = str(relation.get("to") or "")
        relation_type = _normalize_relation(relation.get("relation"))
        if relation_type == "prequel":
            source, target = target, source
            relation_type = "sequel"
        if (
            relation_type == "sequel"
            and source in subjects
            and target in subjects
            and _is_tv_subject(subjects[source])
            and _is_tv_subject(subjects[target])
        ):
            sequel_edges.add((source, target))
    component = _connected_component(selected_id, sequel_edges)
    component_edges = tuple(
        edge
        for edge in sequel_edges
        if edge[0] in component and edge[1] in component
    )
    indegree = {node: 0 for node in component}
    outdegree = {node: 0 for node in component}
    for source, target in component_edges:
        outdegree[source] += 1
        indegree[target] += 1
    if any(value > 1 for value in indegree.values()) or any(
        value > 1 for value in outdegree.values()
    ):
        return _subject_problem("blocked", "branching_sequel_graph")
    ordered_ids = _topological_chain(component, component_edges)
    if ordered_ids is None:
        return _subject_problem("blocked", "cyclic_sequel_graph")

    candidate_by_subject = {}
    weak_problem = None
    missing_observations = set()
    for subject_id in ordered_ids:
        observation = observations.get(subject_id)
        if observation is None:
            missing_observations.add(subject_id)
            continue
        candidate, problem = _candidate_from_observation(observation)
        if problem is not None:
            if problem.reason_codes == ("subject_candidate_confidence_insufficient",):
                weak_problem = problem
                continue
            if (
                _is_explicit_no_match_observation(observation)
                and problem.reason_codes
                == ("subject_candidate_evidence_missing",)
            ):
                continue
            if (
                subject_id == selected_id
                and problem.reason_codes == ("locale_candidate_conflict",)
            ):
                return _bangumi_standalone_resolution(
                    selected_id=selected_id,
                    subject=subjects[selected_id],
                    batch=batch,
                    reason_code="bangumi_primary_tmdb_metadata_ambiguous",
                )
            return problem
        candidate_by_subject[subject_id] = candidate

    if weak_problem is not None:
        aggregate = _same_identity_aggregate(
            batch, ordered_ids, subjects, observations, candidate_by_subject
        )
        return aggregate if aggregate is not None else weak_problem

    if selected_id in missing_observations:
        return _subject_problem(
            "manual_required",
            (
                "subject_evidence_truncated"
                if bool(batch.get("truncated"))
                else "subject_candidate_evidence_missing"
            ),
        )
    selected_candidate = candidate_by_subject.get(selected_id)
    if selected_candidate is not None:
        selected_tmdb_id = _tmdb_candidate_id(selected_candidate)
        try:
            anchor = trusted_relation_tmdb_anchor(
                batch, selected_tmdb_id, require_complete=False
            )
        except ProviderCohortAnchorError as exc:
            return _subject_problem("blocked", exc.reason_code)
        selected_index = ordered_ids.index(selected_id)
        left = selected_index
        while left > 0:
            neighbour_id = ordered_ids[left - 1]
            neighbour = candidate_by_subject.get(neighbour_id)
            if neighbour is None:
                return _subject_problem(
                    "manual_required",
                    (
                        "subject_evidence_truncated"
                        if bool(batch.get("truncated"))
                        else "subject_candidate_evidence_missing"
                    ),
                )
            if _tmdb_candidate_id(neighbour) != selected_tmdb_id:
                break
            left -= 1
        right = selected_index
        while right + 1 < len(ordered_ids):
            neighbour_id = ordered_ids[right + 1]
            neighbour = candidate_by_subject.get(neighbour_id)
            if neighbour is None:
                return _subject_problem(
                    "manual_required",
                    (
                        "subject_evidence_truncated"
                        if bool(batch.get("truncated"))
                        else "subject_candidate_evidence_missing"
                    ),
                )
            if _tmdb_candidate_id(neighbour) != selected_tmdb_id:
                break
            right += 1
        segment_ids = ordered_ids[left : right + 1]
        if any(subject_id in missing_observations for subject_id in segment_ids):
            return _subject_problem(
                "manual_required",
                (
                    "subject_evidence_truncated"
                    if bool(batch.get("truncated"))
                    else "subject_candidate_evidence_missing"
                ),
            )
        resolution = _resolve_segment_seasons(
            selected_tmdb_id=selected_tmdb_id,
            candidate=anchor,
            segment_ids=segment_ids,
            segment_subjects=[subjects[subject_id] for subject_id in segment_ids],
            boundaries=_cohort_candidate_boundaries(
                ordered_ids=ordered_ids,
                candidate_by_subject=candidate_by_subject,
                selected_tmdb_id=selected_tmdb_id,
                segment_left=left,
                segment_right=right,
            ),
        )
        return replace(resolution, subject_observations=dict(batch))

    candidate_ids = tuple(sorted({
        _tmdb_candidate_id(candidate)
        for candidate in candidate_by_subject.values()
        if _tmdb_candidate_id(candidate)
    }))
    if not candidate_ids:
        if (
            len(ordered_ids) == 1
            and subject_is_explicit_no_match(batch, selected_id)
        ):
            return _bangumi_standalone_resolution(
                selected_id=selected_id,
                subject=subjects[selected_id],
                batch=batch,
                reason_code="explicit_bangumi_standalone",
            )
        return _subject_problem(
            "manual_required", "subject_candidate_evidence_missing"
        )

    selected_index = ordered_ids.index(selected_id)
    successful = []
    failed = []
    for candidate_id in candidate_ids:
        try:
            anchor = trusted_relation_tmdb_anchor(batch, candidate_id)
        except ProviderCohortAnchorError as exc:
            return _subject_problem("blocked", exc.reason_code)

        left = selected_index
        while left > 0:
            neighbour = candidate_by_subject.get(ordered_ids[left - 1])
            if (
                neighbour is not None
                and _tmdb_candidate_id(neighbour) != candidate_id
            ):
                break
            left -= 1
        right = selected_index
        while right + 1 < len(ordered_ids):
            neighbour = candidate_by_subject.get(ordered_ids[right + 1])
            if (
                neighbour is not None
                and _tmdb_candidate_id(neighbour) != candidate_id
            ):
                break
            right += 1
        segment_ids = ordered_ids[left : right + 1]
        if any(subject_id in missing_observations for subject_id in segment_ids):
            failed.append(
                _subject_problem(
                    "manual_required",
                    (
                        "subject_evidence_truncated"
                        if bool(batch.get("truncated"))
                        else "subject_candidate_evidence_missing"
                    ),
                )
            )
            continue
        if not any(
            subject_id in candidate_by_subject
            and _tmdb_candidate_id(candidate_by_subject[subject_id])
            == candidate_id
            for subject_id in segment_ids
        ):
            continue
        boundaries = _cohort_candidate_boundaries(
            ordered_ids=ordered_ids,
            candidate_by_subject=candidate_by_subject,
            selected_tmdb_id=candidate_id,
            segment_left=left,
            segment_right=right,
        )
        resolution = _resolve_segment_seasons(
            selected_tmdb_id=candidate_id,
            candidate=anchor,
            segment_ids=segment_ids,
            segment_subjects=[subjects[subject_id] for subject_id in segment_ids],
            boundaries=boundaries,
        )
        if resolution.status == "inferred" and selected_id in dict(
            resolution.season_assignments
        ):
            successful.append(resolution)
        else:
            failed.append(resolution)
    if len(successful) > 1:
        return _subject_problem("blocked", "cohort_anchor_ambiguous")
    if successful:
        return replace(successful[0], subject_observations=dict(batch))
    if selected_candidate is not None and failed:
        return replace(failed[0], subject_observations=dict(batch))
    return _subject_problem(
        "manual_required", "cohort_anchor_season_coverage_missing"
    )


def _is_explicit_no_match_observation(
    observation: Mapping[str, object],
) -> bool:
    queries = observation.get("queries", ()) or ()
    return bool(queries) and all(
        isinstance(query, Mapping)
        and query.get("outcome") == "no_match"
        and query.get("truncated") is False
        and not (query.get("candidates", ()) or ())
        for query in queries
    )


def subject_is_explicit_no_match(
    subject_batch: Mapping[str, object],
    subject_id: str,
) -> bool:
    """Return whether one frozen subject has only complete no-match queries."""

    if not isinstance(subject_batch, Mapping):
        return False
    matches = [
        observation
        for observation in subject_batch.get("subject_observations", ()) or ()
        if isinstance(observation, Mapping)
        and str(observation.get("bangumi_subject_id") or "") == str(subject_id)
    ]
    return len(matches) == 1 and _is_explicit_no_match_observation(matches[0])


def _cohort_candidate_boundaries(
    *,
    ordered_ids: tuple[str, ...],
    candidate_by_subject: Mapping[str, Mapping[str, object]],
    selected_tmdb_id: str,
    segment_left: int,
    segment_right: int,
) -> tuple[Mapping[str, object], ...]:
    """Keep every distinct outside work transition in stable chain order."""

    rows = []
    for index in range(len(ordered_ids) - 1):
        next_index = index + 1
        left_inside = segment_left <= index <= segment_right
        right_inside = segment_left <= next_index <= segment_right
        if left_inside and right_inside:
            continue
        left_id = _tmdb_candidate_id(
            candidate_by_subject.get(ordered_ids[index]) or {}
        )
        right_id = _tmdb_candidate_id(
            candidate_by_subject.get(ordered_ids[next_index]) or {}
        )
        if left_inside != right_inside:
            outside_id = right_id if left_inside else left_id
        elif left_id and right_id and left_id != right_id:
            outside_id = right_id
        else:
            continue
        if not outside_id or outside_id == selected_tmdb_id:
            continue
        rows.append({
            "from_subject_id": ordered_ids[index],
            "to_subject_id": ordered_ids[next_index],
            "relation": "sequel",
            "boundary_kind": "different_unique_tmdb_id",
            "outside_tmdb_tv_id": outside_id,
        })
    return tuple(rows)


def _same_identity_aggregate(batch, ordered_ids, subjects, observations, exact):
    """One complete, count-proven aggregate; fuzzy observations stay observations.

    Only this resolver proves the exception. Cohort and UI consumers recompute
    it from the original batch instead of manufacturing exact-tier candidates.
    """
    from .tmdb_genre import TMDB_ANIMATION_POLICY_VERSION

    identities = {_tmdb_candidate_id(item) for item in exact.values()}
    if len(ordered_ids) < 2 or len(identities) != 1:
        return None
    identity = next(iter(identities))
    try:
        anchor = trusted_relation_tmdb_anchor(batch, identity)
    except ProviderCohortAnchorError:
        return None
    fields = ('namespace', 'entity_type', 'provider_id', 'tmdb_tv_id',
              'title', 'original_title', 'first_air_date', 'seasons')
    anchor_metadata = _canonical_sha256({k: anchor.get(k) for k in fields})
    for subject_id in ordered_ids:
        count = subjects[subject_id].get('episode_count')
        if type(count) is not int or count <= 0:
            return None
        row = observations.get(subject_id)
        if not row or not row.get('queries'):
            return None
        for query in row['queries']:
            candidates = query.get('candidates')
            if (query.get('outcome') != 'ok' or query.get('truncated') is not False
                    or not isinstance(candidates, (tuple, list)) or len(candidates) != 1):
                return None
            candidate = candidates[0]
            if not isinstance(candidate, Mapping):
                return None
            score = candidate.get('score_components')
            if (not isinstance(score, Mapping)
                    or score.get('animation_confirmed') is not True
                    or score.get('year_compatible') is not True
                    or score.get('animation_policy_version') != TMDB_ANIMATION_POLICY_VERSION
                    or not score.get('animation_evidence_source')
                    or _canonical_sha256({k: candidate.get(k) for k in fields}) != anchor_metadata):
                return None
    resolution = _resolve_segment_seasons(
        selected_tmdb_id=identity, candidate=anchor, segment_ids=ordered_ids,
        segment_subjects=[subjects[s] for s in ordered_ids], boundaries=(),
    )
    if (resolution.review_disposition != 'reviewable'
            or resolution.mapping_shape != 'many_subjects_one_season'):
        return None
    return replace(resolution, subject_observations=dict(batch),
                   reason_codes=(*resolution.reason_codes, SAME_ID_AGGREGATE_SUPPORT))


def _candidate_from_observation(
    observation: Mapping[str, object],
) -> tuple[Optional[Mapping[str, object]], Optional[ProviderLinkResolution]]:
    exact_by_query = []
    saw_candidate = False
    allowed_outcomes = {
        "ok",
        "no_match",
        "transport_error",
        "invalid_response",
        "budget_exhausted",
    }
    for query in observation.get("queries", ()) or ():
        if not isinstance(query, Mapping):
            return None, _subject_problem("blocked", "subject_observation_invalid")
        if type(query.get("truncated")) is not bool:
            return None, _subject_problem("blocked", "subject_observation_invalid")
        if bool(query.get("truncated")):
            return None, _subject_problem("manual_required", "subject_query_truncated")
        outcome = str(query.get("outcome") or "")
        if outcome not in allowed_outcomes:
            return None, _subject_problem(
                "blocked", "subject_query_outcome_unsupported"
            )
        query_candidates = tuple(query.get("candidates", ()) or ())
        if outcome != "ok" and query_candidates:
            return None, _subject_problem(
                "blocked", "subject_query_outcome_inconsistent"
            )
        if outcome == "transport_error":
            return None, _subject_problem(
                "manual_required", "subject_candidate_transport_error"
            )
        if outcome == "invalid_response":
            return None, _subject_problem(
                "blocked", "subject_candidate_invalid_response"
            )
        if outcome == "budget_exhausted":
            return None, _subject_problem(
                "manual_required", "subject_candidate_budget_exhausted"
            )
        query_exact = {}
        for candidate in query_candidates:
            if not isinstance(candidate, Mapping):
                return None, _subject_problem(
                    "blocked", "subject_observation_invalid"
                )
            saw_candidate = True
            candidate_id = str(candidate.get("tmdb_tv_id") or "")
            provider_id = str(candidate.get("provider_id") or candidate_id)
            if candidate.get("match_tier") == "exact" and (
                candidate.get("namespace") != "tmdb"
                or candidate.get("entity_type") != "tv"
                or not candidate_id
                or provider_id != candidate_id
            ):
                return None, _subject_problem(
                    "blocked", "subject_candidate_identity_invalid"
                )
            if candidate.get("match_tier") == "exact" and candidate_id:
                query_exact[candidate_id] = candidate
        if len(query_exact) > 1:
            return None, _subject_problem("blocked", "subject_candidate_tie")
        exact_by_query.extend(query_exact.items())
    exact = {}
    for candidate_id, candidate in exact_by_query:
        exact[candidate_id] = candidate
    if len(exact) > 1:
        return None, _subject_problem("blocked", "locale_candidate_conflict")
    if not exact:
        return None, _subject_problem(
            "manual_required",
            (
                "subject_candidate_confidence_insufficient"
                if saw_candidate
                else "subject_candidate_evidence_missing"
            ),
        )
    return next(iter(exact.values())), None


def _resolve_segment_seasons(
    *,
    selected_tmdb_id: str,
    candidate: Mapping[str, object],
    segment_ids: tuple[str, ...],
    segment_subjects: list[Mapping[str, object]],
    boundaries: tuple[Mapping[str, object], ...],
) -> ProviderLinkResolution:
    seasons = sorted(
        (
            item
            for item in candidate.get("seasons", ()) or ()
            if isinstance(item, Mapping)
            and isinstance(item.get("season_number"), int)
            and not isinstance(item.get("season_number"), bool)
            and int(item["season_number"]) > 0
        ),
        key=lambda item: int(item["season_number"]),
    )
    if not seasons:
        return _subject_problem("blocked", "tmdb_positive_seasons_missing")
    season_numbers = tuple(int(item["season_number"]) for item in seasons)
    if season_numbers != tuple(
        range(season_numbers[0], season_numbers[0] + len(season_numbers))
    ):
        return _subject_problem("blocked", "tmdb_season_gap")
    if len(segment_subjects) == 1:
        subject_count = segment_subjects[0].get("episode_count")
        if isinstance(subject_count, int) and not isinstance(subject_count, bool):
            for start in range(len(seasons)):
                total = 0
                for end in range(start, len(seasons)):
                    count = seasons[end].get("episode_count")
                    if not isinstance(count, int) or isinstance(count, bool):
                        break
                    total += count
                    if end > start and total == subject_count:
                        return _subject_problem(
                            "blocked", "one_subject_multiple_seasons_unsupported"
                        )
                    if total >= subject_count:
                        break

    exact_matches = []
    for start in range(len(seasons)):
        for end in range(start + 1, len(seasons) + 1):
            window = seasons[start:end]
            for partition in _episode_partitions(segment_subjects, window):
                exact_matches.append((window, partition))
    exact_matches = _deduplicate_season_matches(exact_matches)
    if len(exact_matches) > 1:
        return _subject_problem("blocked", "season_window_ambiguous")
    if exact_matches:
        window, partition = exact_matches[0]
        return _successful_subject_resolution(
            selected_tmdb_id=selected_tmdb_id,
            segment_ids=segment_ids,
            seasons=window,
            partition=partition,
            boundaries=boundaries,
            date_locked=False,
        )

    date_matches = []
    count_mismatch_seen = False
    window_size = len(segment_subjects)
    for start in range(0, len(seasons) - window_size + 1):
        window = seasons[start : start + window_size]
        if not all(
            _dates_uniquely_align(subject, season)
            for subject, season in zip(segment_subjects, window)
        ):
            continue
        counts_match = all(
            isinstance(subject.get("episode_count"), int)
            and not isinstance(subject.get("episode_count"), bool)
            and isinstance(season.get("episode_count"), int)
            and not isinstance(season.get("episode_count"), bool)
            and abs(int(subject["episode_count"]) - int(season["episode_count"]))
            <= 1
            for subject, season in zip(segment_subjects, window)
        )
        if not counts_match:
            count_mismatch_seen = True
            continue
        partition = tuple((subject,) for subject in segment_subjects)
        date_matches.append((window, partition))
    date_matches = _deduplicate_season_matches(date_matches)
    if len(date_matches) > 1:
        return _subject_problem("blocked", "season_window_ambiguous")
    if date_matches:
        window, partition = date_matches[0]
        return _successful_subject_resolution(
            selected_tmdb_id=selected_tmdb_id,
            segment_ids=segment_ids,
            seasons=window,
            partition=partition,
            boundaries=boundaries,
            date_locked=True,
        )
    return _subject_problem(
        "blocked",
        (
            "season_episode_count_mismatch"
            if count_mismatch_seen
            else "season_count_mismatch"
        ),
    )


def _successful_subject_resolution(
    *,
    selected_tmdb_id: str,
    segment_ids: tuple[str, ...],
    seasons: list[Mapping[str, object]],
    partition: tuple[tuple[Mapping[str, object], ...], ...],
    boundaries: tuple[Mapping[str, object], ...],
    date_locked: bool,
) -> ProviderLinkResolution:
    assignments = []
    ranges = []
    many_to_one = False
    for season, group in zip(seasons, partition):
        season_number = int(season["season_number"])
        season_count = int(season["episode_count"])
        many_to_one = many_to_one or len(group) > 1
        cursor = 1
        for subject in group:
            subject_id = _provider_id(subject)
            assignments.append((subject_id, season_number))
            if len(group) == 1:
                episode_start, episode_end = 1, season_count
            else:
                episode_start = cursor
                episode_end = cursor + int(subject["episode_count"]) - 1
                cursor = episode_end + 1
            ranges.append(
                {
                    "bangumi_subject_id": subject_id,
                    "tmdb_tv_id": selected_tmdb_id,
                    "season_number": season_number,
                    "episode_start": episode_start,
                    "episode_end": episode_end,
                }
            )
    validation = validate_derived_episode_ranges(ranges)
    if not validation["valid"]:
        return _subject_problem("blocked", *validation["reason_codes"])
    normalized_boundaries = tuple(
        sorted(
            (dict(item) for item in boundaries),
            key=lambda item: (
                str(item.get("from_subject_id") or ""),
                str(item.get("to_subject_id") or ""),
            ),
        )
    )
    normalized_ranges = tuple(
        sorted(
            ranges,
            key=lambda item: (
                int(item["season_number"]),
                int(item["episode_start"]),
                str(item["bangumi_subject_id"]),
            ),
        )
    )
    mapping_shape = (
        "many_subjects_one_season" if many_to_one else "one_subject_one_season"
    )
    resolution_payload = {
        "tmdb_tv_id": selected_tmdb_id,
        "work_segment": {"subject_ids": tuple(segment_ids)},
        "season_assignments": tuple(assignments),
        "derived_episode_ranges": normalized_ranges,
        "boundaries": normalized_boundaries,
        "mapping_shape": mapping_shape,
        "season_count_policy_version": (
            _SEASON_COUNT_POLICY if date_locked else None
        ),
    }
    return ProviderLinkResolution(
        automation_level="L3" if many_to_one else "L2",
        status="inferred",
        tmdb_tv_id=selected_tmdb_id,
        season_assignments=tuple(assignments),
        requires_plan_review=True,
        reason_codes=(
            ("season_window_date_locked",)
            if date_locked
            else (("split_cour_many_to_one",) if many_to_one else ())
        ),
        work_segment={"subject_ids": tuple(segment_ids)},
        derived_episode_ranges=normalized_ranges,
        boundaries=normalized_boundaries,
        mapping_shape=mapping_shape,
        review_disposition="reviewable",
        resolution_fingerprint=_canonical_sha256(resolution_payload),
        season_count_policy_version=(
            _SEASON_COUNT_POLICY if date_locked else None
        ),
    )


def _deduplicate_season_matches(matches):
    unique = {}
    for seasons, partition in matches:
        key = (
            tuple(int(item["season_number"]) for item in seasons),
            tuple(
                tuple(_provider_id(subject) for subject in group)
                for group in partition
            ),
        )
        unique[key] = (seasons, partition)
    return list(unique.values())


def _dates_uniquely_align(
    subject: Mapping[str, object], season: Mapping[str, object]
) -> bool:
    subject_date = _parse_date(subject.get("air_date"))
    season_date = _parse_date(season.get("air_date"))
    return (
        subject_date is not None
        and season_date is not None
        and abs((subject_date - season_date).days) <= 370
    )


def _tmdb_candidate_id(value: Mapping[str, object]) -> str:
    return str(value.get("tmdb_tv_id") or value.get("provider_id") or "")


def _subject_problem(
    review_disposition: str, *reason_codes: str
) -> ProviderLinkResolution:
    return ProviderLinkResolution(
        automation_level="L4",
        status="conflicted",
        requires_plan_review=True,
        reason_codes=tuple(reason_codes),
        review_disposition=review_disposition,
        derived_episode_ranges=(),
        boundaries=(),
    )


def _resolve_frozen_evidence(
    *,
    selected_provider: Mapping[str, object],
    bangumi_subjects: tuple[Mapping[str, object], ...],
    bangumi_relations: tuple[Mapping[str, object], ...],
    tmdb_candidates: tuple[Mapping[str, object], ...],
    fetch_callbacks_present: bool,
) -> ProviderLinkResolution:
    selected_id = _provider_id(selected_provider)
    if (
        selected_provider.get("namespace") != "bangumi"
        or selected_provider.get("entity_type") != "subject"
        or not selected_id
    ):
        return _conflict(
            "crosswalk_inference_requires_bangumi_subject",
            "provider_fetch_deferred" if fetch_callbacks_present else None,
        )

    subjects = {
        _provider_id(item): item
        for item in bangumi_subjects
        if _provider_id(item)
    }
    selected_subject = subjects.get(selected_id)
    if selected_subject is None:
        return _conflict(
            "bangumi_subject_evidence_missing",
            "provider_fetch_deferred" if fetch_callbacks_present else None,
        )
    if not _is_tv_subject(selected_subject):
        return _conflict("non_mainline_relation", level="L3", status="inferred")

    sequel_edges = set()
    non_mainline_selected = False
    for relation in bangumi_relations:
        source = str(relation.get("from") or "")
        target = str(relation.get("to") or "")
        relation_type = _normalize_relation(relation.get("relation"))
        if not source or not target:
            continue
        if relation_type == "sequel":
            edge = (source, target)
        elif relation_type == "prequel":
            edge = (target, source)
        else:
            if selected_id in {source, target}:
                non_mainline_selected = True
            continue
        if _is_tv_subject(subjects.get(edge[0])) and _is_tv_subject(
            subjects.get(edge[1])
        ):
            sequel_edges.add(edge)

    if non_mainline_selected and not any(
        selected_id in edge for edge in sequel_edges
    ):
        return _conflict("non_mainline_relation", level="L3", status="inferred")

    component = _connected_component(selected_id, sequel_edges)
    component_edges = [
        edge
        for edge in sequel_edges
        if edge[0] in component and edge[1] in component
    ]
    indegree = {node: 0 for node in component}
    outdegree = {node: 0 for node in component}
    for source, target in component_edges:
        outdegree[source] += 1
        indegree[target] += 1
    if any(value > 1 for value in indegree.values()) or any(
        value > 1 for value in outdegree.values()
    ):
        return _conflict("branching_sequel_graph")

    ordered_subject_ids = _topological_chain(component, component_edges)
    if ordered_subject_ids is None:
        return _conflict("cyclic_sequel_graph")

    candidates = [item for item in tmdb_candidates if _provider_id(item)]
    if len(candidates) > 1:
        normalized_titles = [
            _normalize_title(item.get("original_title") or item.get("title"))
            for item in candidates
        ]
        reason = (
            "same_title_reboot_collision"
            if len(set(normalized_titles)) < len(normalized_titles)
            else "multiple_tmdb_candidates"
        )
        return _conflict(reason)
    if not candidates:
        return _conflict(
            "tmdb_candidate_evidence_missing",
            "provider_fetch_deferred" if fetch_callbacks_present else None,
        )
    tmdb = candidates[0]
    tmdb_id = _provider_id(tmdb)
    if not _candidate_title_matches_subjects(tmdb, ordered_subject_ids, subjects):
        return _conflict("tmdb_title_evidence_mismatch")
    seasons = sorted(
        (
            item
            for item in tmdb.get("seasons", ())
            if isinstance(item, Mapping)
            and isinstance(item.get("season_number"), int)
            and int(item["season_number"]) > 0
        ),
        key=lambda item: int(item["season_number"]),
    )
    season_numbers = [int(item["season_number"]) for item in seasons]
    if not seasons:
        return _conflict("tmdb_positive_seasons_missing")
    if season_numbers != list(
        range(season_numbers[0], season_numbers[0] + len(season_numbers))
    ):
        return _conflict("tmdb_season_gap")

    ordered_subjects = [subjects[subject_id] for subject_id in ordered_subject_ids]
    partitions = _episode_partitions(ordered_subjects, seasons)
    date_locked_one_to_one = False
    if not partitions:
        # Bangumi and TMDB do not always count bundled OVAs/specials in the
        # same season total.  Episode totals therefore cannot be the sole
        # bridge.  A strict one-to-one fallback is still deterministic when
        # the sequel graph is linear, both sides expose the same number of
        # consecutive seasons, and every paired premiere date is present and
        # close.  Missing dates or split-cour shapes continue to fail closed.
        date_partition = _date_locked_one_to_one_partition(
            ordered_subjects, seasons
        )
        if date_partition is not None:
            partitions = (date_partition,)
            date_locked_one_to_one = True
    if len(partitions) != 1:
        reason = "season_partition_ambiguous" if partitions else "season_count_mismatch"
        return _conflict(reason, level="L3")

    partition = partitions[0]
    assignments = []
    evidence = []
    many_to_one = False
    for season, group in zip(seasons, partition):
        season_number = int(season["season_number"])
        many_to_one = many_to_one or len(group) > 1
        for subject in group:
            subject_id = _provider_id(subject)
            assignments.append((subject_id, season_number))
            evidence.append(f"bangumi:{subject_id}->tmdb:{tmdb_id}:S{season_number:02d}")

    return ProviderLinkResolution(
        automation_level="L3" if many_to_one else "L2",
        status="inferred",
        tmdb_tv_id=tmdb_id,
        season_assignments=tuple(assignments),
        requires_plan_review=True,
        evidence_path=tuple(evidence),
        reason_codes=(
            "linear_one_to_one_air_date_locked"
            if date_locked_one_to_one
            else (
                "split_cour_many_to_one"
                if many_to_one else "linear_one_to_one"
            ),
        ),
    )


def _episode_partitions(
    subjects: list[Mapping[str, object]],
    seasons: list[Mapping[str, object]],
) -> list[tuple[tuple[Mapping[str, object], ...], ...]]:
    """Return at most two exact consecutive partitions (two means ambiguous)."""

    subject_counts = [item.get("episode_count") for item in subjects]
    season_counts = [item.get("episode_count") for item in seasons]
    if any(not isinstance(value, int) or value <= 0 for value in subject_counts):
        return []
    if any(not isinstance(value, int) or value <= 0 for value in season_counts):
        return []
    results = []

    def visit(subject_index: int, season_index: int, groups: list[tuple]):
        if len(results) >= 2:
            return
        if season_index == len(seasons):
            if subject_index == len(subjects):
                results.append(tuple(groups))
            return
        remaining_subjects = len(subjects) - subject_index
        remaining_seasons = len(seasons) - season_index
        if remaining_subjects < remaining_seasons:
            return
        target = int(season_counts[season_index])
        total = 0
        final_end = len(subjects) - (remaining_seasons - 1)
        for end in range(subject_index + 1, final_end + 1):
            total += int(subject_counts[end - 1])
            if total == target:
                group = tuple(subjects[subject_index:end])
                if _date_alignment_plausible(group[0], seasons[season_index]):
                    visit(end, season_index + 1, groups + [group])
            if total >= target:
                break

    visit(0, 0, [])
    return results


def _date_locked_one_to_one_partition(
    subjects: list[Mapping[str, object]],
    seasons: list[Mapping[str, object]],
) -> Optional[tuple[tuple[Mapping[str, object], ...], ...]]:
    if len(subjects) != len(seasons) or not subjects:
        return None
    groups = []
    for subject, season in zip(subjects, seasons):
        subject_date = _parse_date(subject.get("air_date"))
        season_date = _parse_date(season.get("air_date"))
        if subject_date is None or season_date is None:
            return None
        if abs((subject_date - season_date).days) > 370:
            return None
        groups.append((subject,))
    return tuple(groups)


def _date_alignment_plausible(
    subject: Mapping[str, object], season: Mapping[str, object]
) -> bool:
    subject_date = _parse_date(subject.get("air_date"))
    season_date = _parse_date(season.get("air_date"))
    if subject_date is None or season_date is None:
        return True
    return abs((subject_date - season_date).days) <= 370


def _connected_component(
    selected_id: str, edges: Iterable[tuple[str, str]]
) -> set[str]:
    adjacency = {selected_id: set()}
    for source, target in edges:
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)
    pending = [selected_id]
    visited = set()
    while pending:
        node = pending.pop()
        if node in visited:
            continue
        visited.add(node)
        pending.extend(adjacency.get(node, ()))
    return visited


def _topological_chain(
    nodes: set[str], edges: Iterable[tuple[str, str]]
) -> Optional[tuple[str, ...]]:
    incoming = {node: 0 for node in nodes}
    outgoing = {node: [] for node in nodes}
    for source, target in edges:
        outgoing[source].append(target)
        incoming[target] += 1
    roots = sorted(node for node, degree in incoming.items() if degree == 0)
    if len(roots) != 1:
        return None
    ordered = []
    pending = roots[:]
    while pending:
        node = pending.pop(0)
        ordered.append(node)
        for target in outgoing[node]:
            incoming[target] -= 1
            if incoming[target] == 0:
                pending.append(target)
    return tuple(ordered) if len(ordered) == len(nodes) else None


def _conflict(
    *reason_codes: Optional[str],
    level: str = "L4",
    status: str = "conflicted",
) -> ProviderLinkResolution:
    return ProviderLinkResolution(
        automation_level=level,
        status=status,
        reason_codes=tuple(reason for reason in reason_codes if reason),
    )


def _provider_id(value: Mapping[str, object]) -> str:
    return str(value.get("provider_id") or "")


def _is_tv_subject(value: Optional[Mapping[str, object]]) -> bool:
    if not isinstance(value, Mapping):
        return False
    from .provider_selection_authority import is_bangumi_serial_platform
    return is_bangumi_serial_platform(value.get("platform"))


def _normalize_relation(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if normalized in {"续集", "sequel"}:
        return "sequel"
    if normalized in {"前传", "prequel"}:
        return "prequel"
    return "other"


def _normalize_title(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


_SEASON_SUFFIX = re.compile(
    r"(?:season|series|cour|part)[\s._-]*(?:\d+|[ivxlcdm]+)$",
    re.IGNORECASE,
)
_ORDINAL_SEASON_SUFFIX = re.compile(
    r"(?:second|third|fourth|fifth|sixth)\s+season$",
    re.IGNORECASE,
)
_CJK_SEASON_SUFFIX = re.compile(
    r"(?:第[0-9一二三四五六七八九十]+[季期]|"
    r"(?:セカンド|サード|フォース|フィフス)シーズン)$"
)


def _title_series_root(value: object) -> str:
    """Normalize only explicit season markers, never arbitrary subtitles."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    previous = None
    while text and text != previous:
        previous = text
        text = _SEASON_SUFFIX.sub("", text).strip(" -_:·△")
        text = _ORDINAL_SEASON_SUFFIX.sub("", text).strip(" -_:·△")
        text = _CJK_SEASON_SUFFIX.sub("", text).strip(" -_:·△")
    return "".join(character for character in text if character.isalnum())


def _candidate_title_matches_subjects(
    candidate: Mapping[str, object],
    subject_ids: Iterable[str],
    subjects: Mapping[str, Mapping[str, object]],
) -> bool:
    candidate_titles = {
        _title_series_root(candidate.get("title")),
        _title_series_root(candidate.get("original_title")),
    }
    candidate_titles.discard("")
    for subject_id in subject_ids:
        subject = subjects[subject_id]
        subject_titles = {
            _title_series_root(subject.get("title")),
            _title_series_root(subject.get("original_title")),
        }
        subject_titles.discard("")
        for left in candidate_titles:
            for right in subject_titles:
                if min(len(left), len(right)) >= 3 and left == right:
                    return True
    return False


def _apply_confirmed_anchor(
    resolution: ProviderLinkResolution,
    existing_links: tuple[ProviderLinkMapping, ...],
) -> ProviderLinkResolution:
    """Promote a proven L2 inference to L1 when exactly one frozen anchor exists."""

    if resolution.automation_level != "L2" or not resolution.tmdb_tv_id:
        return resolution
    assignment_subjects = {subject for subject, _season in resolution.season_assignments}
    anchors = []
    for mapping in existing_links:
        if not isinstance(mapping, ProviderLinkMapping):
            continue
        if mapping.status != "materialized_confirmed" or not mapping.local_series_key:
            continue
        bangumi_ids = {
            str(edge.get("provider_id") or "")
            for edge in mapping.provider_edges
            if edge.get("namespace") == "bangumi"
            and edge.get("entity_type") == "subject"
        }
        tmdb_ids = {
            str(edge.get("provider_id") or "")
            for edge in mapping.provider_edges
            if edge.get("namespace") == "tmdb" and edge.get("entity_type") == "tv"
        } | {str(edge.get("tmdb_tv_id") or "") for edge in mapping.season_edges}
        if bangumi_ids & assignment_subjects and resolution.tmdb_tv_id in tmdb_ids:
            anchors.append(mapping)
    unique = {mapping.mapping_id: mapping for mapping in anchors}
    if len(unique) > 1:
        return _conflict("multiple_confirmed_series_anchors")
    if not unique:
        return resolution
    anchor = next(iter(unique.values()))
    occupied = {
        int(edge["season_number"]): str(edge.get("provider_id") or "")
        for edge in anchor.season_edges
        if isinstance(edge.get("season_number"), int)
    }
    for subject_id, season_number in resolution.season_assignments:
        owner = occupied.get(season_number)
        if owner and owner != subject_id:
            return _conflict("anchored_tmdb_season_already_occupied")
    return ProviderLinkResolution(
        **{
            **resolution.__dict__,
            "automation_level": "L1",
            "proposed_series_key": anchor.local_series_key,
            "series_key": anchor.local_series_key,
            "reason_codes": ("anchored_confirmed_series",),
            "anchor_mapping_id": anchor.mapping_id,
            "anchor_revision": anchor.revision,
            "anchor_fingerprint": anchor.evidence_fingerprint,
        }
    )


def _parse_date(value: object) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None
