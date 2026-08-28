"""Stage inferred cross-provider decisions for ordinary Plan review.

G3 deliberately freezes the decision in DecisionState and the projected Plan.
Catalog merge/materialization is a G4 transaction so an inferred edge can never
silently mutate an already materialized mapping.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional

from .decision_state import DecisionState
from .provider_link_resolver import (
    ProviderLinkResolution,
    derive_source_episode_projection,
    resolve_provider_link,
)
from .presentation import rebind_content_version_identity
from .rename_plan import (
    EvidenceCandidate,
    EvidenceValue,
    RenamePlan,
    build_version_ref,
)
from .target_routing import TargetRootSnapshot, resolve_target_root


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _rehash_subject_observation_batch(
    batch: Mapping[str, object],
    observations: list[dict[str, object]],
) -> dict[str, object]:
    normalized = dict(batch)
    normalized["subject_observations"] = observations
    normalized["truncated"] = False
    normalized["boundary_complete"] = True
    normalized.pop("evidence_fingerprint", None)
    normalized["evidence_fingerprint"] = _canonical_sha256(normalized)
    return normalized


def _normalized_relation(value: object) -> str:
    relation = str(value or "").strip().casefold()
    if relation in {"续集", "sequel"}:
        return "sequel"
    if relation in {"前传", "prequel"}:
        return "prequel"
    return relation


def _canonical_relation_edge(
    relation: Mapping[str, object],
) -> tuple[str, str, str]:
    source_id = str(relation.get("from") or "").strip()
    target_id = str(relation.get("to") or "").strip()
    relation_kind = _normalized_relation(relation.get("relation"))
    if relation_kind == "prequel":
        source_id, target_id = target_id, source_id
        relation_kind = "sequel"
    return source_id, target_id, relation_kind


_ORDER_FREE_COHORT_FIELDS = {
    "cohort_subject_ids",
    "cohort_edges",
    "subject_evidence_fingerprints",
    "season_assignments",
    "derived_episode_ranges",
}


def _canonical_cohort_value(value: object, *, field: str = "") -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_cohort_value(item, field=str(key))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        rows = [
            _canonical_cohort_value(item, field=field)
            for item in value
        ]
        if field in _ORDER_FREE_COHORT_FIELDS:
            rows.sort(key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ))
        return rows
    return value


def provider_subject_cohort_request_fingerprint(
    request: Mapping[str, object],
) -> str:
    """Return one order-stable fingerprint shared by preview and receipt."""

    if not isinstance(request, Mapping):
        raise ValueError("cohort request must be a mapping")
    return _canonical_sha256(_canonical_cohort_value(request))


def _subject_rows(
    batch: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    return {
        str(item.get("bangumi_subject_id") or ""): dict(item)
        for item in batch.get("subject_observations", ()) or ()
        if isinstance(item, Mapping)
        and str(item.get("bangumi_subject_id") or "")
    }


def _exact_tmdb_candidate(
    row: object,
) -> Optional[dict[str, object]]:
    if not isinstance(row, Mapping):
        return None
    candidates: dict[str, dict[str, object]] = {}
    for query in row.get("queries", ()) or ():
        if not isinstance(query, Mapping) or query.get("truncated") is not False:
            return None
        if query.get("outcome") != "ok":
            continue
        for candidate in query.get("candidates", ()) or ():
            if not isinstance(candidate, Mapping):
                continue
            provider_id = str(candidate.get("provider_id") or "")
            tmdb_id = str(candidate.get("tmdb_tv_id") or "")
            if (
                candidate.get("namespace") == "tmdb"
                and candidate.get("entity_type") == "tv"
                and candidate.get("match_tier") == "exact"
                and provider_id
                and provider_id == tmdb_id
            ):
                candidates[tmdb_id] = dict(candidate)
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def _complete_no_match_observation(row: object) -> bool:
    if not isinstance(row, Mapping):
        return False
    queries = tuple(row.get("queries", ()) or ())
    return bool(queries) and all(
        isinstance(query, Mapping)
        and query.get("truncated") is False
        and query.get("outcome") == "no_match"
        and not tuple(query.get("candidates", ()) or ())
        for query in queries
    )


def _canonical_subject_chain(
    *,
    selected_subject_id: str,
    bangumi_subjects: list[dict[str, object]],
    bangumi_relations: list[dict[str, object]],
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    subject_ids = {
        str(item.get("provider_id") or item.get("id") or "")
        for item in bangumi_subjects
        if str(item.get("provider_id") or item.get("id") or "")
    }
    if selected_subject_id not in subject_ids:
        raise ValueError("保存的所选作品资料不完整")
    edges = {
        (source_id, target_id)
        for relation in bangumi_relations
        for source_id, target_id, relation_kind in (
            _canonical_relation_edge(relation),
        )
        if relation_kind == "sequel"
        and source_id in subject_ids
        and target_id in subject_ids
    }
    adjacency = {selected_subject_id: set()}
    for source_id, target_id in edges:
        adjacency.setdefault(source_id, set()).add(target_id)
        adjacency.setdefault(target_id, set()).add(source_id)
    pending = [selected_subject_id]
    component: set[str] = set()
    while pending:
        subject_id = pending.pop()
        if subject_id in component:
            continue
        component.add(subject_id)
        pending.extend(adjacency.get(subject_id, ()))
    component_edges = tuple(sorted(
        edge for edge in edges
        if edge[0] in component and edge[1] in component
    ))
    indegree = {subject_id: 0 for subject_id in component}
    outgoing = {subject_id: [] for subject_id in component}
    for source_id, target_id in component_edges:
        indegree[target_id] += 1
        outgoing[source_id].append(target_id)
    if any(value > 1 for value in indegree.values()) or any(
        len(value) > 1 for value in outgoing.values()
    ):
        raise ValueError("续作关系存在分支，不能自动确认季度边界")
    roots = sorted(
        subject_id for subject_id, value in indegree.items() if value == 0
    )
    if len(roots) != 1:
        raise ValueError("续作关系存在循环或多解")
    ordered = []
    cursor = roots[0]
    while cursor:
        if cursor in ordered:
            raise ValueError("续作关系存在循环")
        ordered.append(cursor)
        next_rows = sorted(outgoing[cursor])
        cursor = next_rows[0] if next_rows else ""
    if set(ordered) != component:
        raise ValueError("续作关系不完整")
    return tuple(ordered), component_edges


def _overlay_batch(
    batch: Mapping[str, object],
    rows: dict[str, dict[str, object]],
    *,
    overlay_subject_ids: set[str],
    selected_subject_id: str,
    selected_candidate: Mapping[str, object],
) -> dict[str, object]:
    revised_rows = []
    for subject_id, original in rows.items():
        row = json.loads(json.dumps(original, ensure_ascii=False))
        if subject_id in overlay_subject_ids:
            queries = list(row.get("queries", ()) or ())
            queries.append({
                "locale": "decision_overlay",
                "query": f"same-work:{selected_subject_id}",
                "year": None,
                "outcome": "ok",
                "truncated": False,
                "candidates": [dict(selected_candidate)],
                "decision_source": "explicit_user_same_work",
            })
            row["queries"] = queries
            row.pop("response_fingerprint", None)
            row["response_fingerprint"] = _canonical_sha256(row)
        revised_rows.append(row)
    return _rehash_subject_observation_batch(batch, revised_rows)


@dataclass(frozen=True)
class ProviderSubjectCohortSegment:
    """One fail-closed cohort segment derived from persisted observations."""

    selected_subject_id: str
    segment_subject_ids: tuple[str, ...]
    anchor_subject_ids: tuple[str, ...]
    anchor_tmdb_tv_id: str
    anchor_evidence_fingerprints: Mapping[str, str]
    subject_evidence_fingerprints: Mapping[str, str]
    working_batch: Mapping[str, object]
    resolution: ProviderLinkResolution

    def to_dict(self) -> dict[str, object]:
        return {
            "selected_subject_id": self.selected_subject_id,
            "segment_subject_ids": self.segment_subject_ids,
            "anchor_subject_ids": self.anchor_subject_ids,
            "anchor_tmdb_tv_id": self.anchor_tmdb_tv_id,
            "anchor_evidence_fingerprints": dict(
                self.anchor_evidence_fingerprints
            ),
            "subject_evidence_fingerprints": dict(
                self.subject_evidence_fingerprints
            ),
            "resolution_fingerprint": str(
                self.resolution.resolution_fingerprint or ""
            ),
        }


def _provider_anchor_authority(
    candidate: Mapping[str, object],
) -> dict[str, object]:
    """Return only season-mapping authority used for anchor concordance."""

    seasons = []
    for item in candidate.get("seasons", ()) or ():
        if not isinstance(item, Mapping):
            raise ValueError("TMDB 锚点的季度资料不完整")
        season_number = item.get("season_number")
        episode_count = item.get("episode_count")
        if not isinstance(season_number, int) or not isinstance(episode_count, int):
            raise ValueError("TMDB 锚点的季度资料不完整")
        seasons.append({
            "season_number": season_number,
            "air_date": str(item.get("air_date") or ""),
            "episode_count": episode_count,
        })
    if not seasons:
        raise ValueError("TMDB 锚点缺少季度资料")
    return {
        "tmdb_tv_id": str(candidate.get("tmdb_tv_id") or ""),
        "first_air_date": str(candidate.get("first_air_date") or ""),
        "seasons": tuple(sorted(
            seasons, key=lambda item: int(item["season_number"])
        )),
    }


def resolve_provider_subject_cohort_segment(
    *,
    selected_subject_id: str,
    subject_observations: Mapping[str, object],
    bangumi_subjects: object,
    bangumi_relations: object,
) -> ProviderSubjectCohortSegment:
    """Resolve one unique linear work segment without mutating observations.

    A selected no-match row may borrow authority only from exact anchors inside
    the same complete sequel chain.  Multiple possible TMDB identities or
    contradictory snapshots for one identity fail closed.
    """

    selected_id = str(selected_subject_id or "").strip()
    if (
        not selected_id
        or not isinstance(subject_observations, Mapping)
        or not isinstance(bangumi_subjects, (list, tuple))
        or not isinstance(bangumi_relations, (list, tuple))
        or subject_observations.get("truncated") is not False
        or subject_observations.get("boundary_complete") is not True
    ):
        raise ValueError("保存的作品资料范围不完整")

    plain_batch = json.loads(json.dumps(subject_observations, ensure_ascii=False))
    plain_subjects = [
        dict(item) for item in bangumi_subjects if isinstance(item, Mapping)
    ]
    plain_relations = [
        dict(item) for item in bangumi_relations if isinstance(item, Mapping)
    ]
    row_by_subject = _subject_rows(plain_batch)
    if selected_id not in row_by_subject:
        raise ValueError("保存的所选作品资料不完整")
    ordered_ids, _component_edges = _canonical_subject_chain(
        selected_subject_id=selected_id,
        bangumi_subjects=plain_subjects,
        bangumi_relations=plain_relations,
    )
    subject_by_id = {
        str(item.get("provider_id") or item.get("id") or ""): item
        for item in plain_subjects
    }
    if any(
        subject_id not in row_by_subject or subject_id not in subject_by_id
        for subject_id in ordered_ids
    ):
        raise ValueError("续作链的作品或 Provider 资料不完整")

    exact_by_subject: dict[str, dict[str, object]] = {}
    for subject_id in ordered_ids:
        exact = _exact_tmdb_candidate(row_by_subject[subject_id])
        if exact is not None:
            exact_by_subject[subject_id] = exact
        elif not _complete_no_match_observation(row_by_subject[subject_id]):
            raise ValueError(
                "相邻作品包含截断、传输失败或多候选资料，不能自动补全"
            )
    if not exact_by_subject:
        raise ValueError("续作链内没有可用的 TMDB 精确锚点")

    selected_index = ordered_ids.index(selected_id)
    selected_exact = exact_by_subject.get(selected_id)
    possible_tmdb_ids = (
        (str(selected_exact.get("tmdb_tv_id") or ""),)
        if selected_exact is not None
        else tuple(sorted({
            str(candidate.get("tmdb_tv_id") or "")
            for candidate in exact_by_subject.values()
            if str(candidate.get("tmdb_tv_id") or "")
        }))
    )
    possible_segments = []
    for tmdb_id in possible_tmdb_ids:
        left = selected_index
        while left > 0:
            previous_id = ordered_ids[left - 1]
            previous = exact_by_subject.get(previous_id)
            if previous is not None and str(
                previous.get("tmdb_tv_id") or ""
            ) != tmdb_id:
                break
            left -= 1
        right = selected_index
        while right + 1 < len(ordered_ids):
            following_id = ordered_ids[right + 1]
            following = exact_by_subject.get(following_id)
            if following is not None and str(
                following.get("tmdb_tv_id") or ""
            ) != tmdb_id:
                break
            right += 1
        segment_ids = ordered_ids[left : right + 1]
        anchor_ids = tuple(sorted(
            subject_id for subject_id in segment_ids
            if subject_id in exact_by_subject
            and str(exact_by_subject[subject_id].get("tmdb_tv_id") or "")
            == tmdb_id
        ))
        if anchor_ids:
            possible_segments.append((tmdb_id, left, right, anchor_ids))
    if len(possible_segments) != 1:
        raise ValueError("当前资料存在多个相容的 TMDB 身份或季度分段")

    tmdb_id, left_limit, right_limit, window_anchor_ids = possible_segments[0]
    anchor_authorities = {
        _canonical_sha256(_provider_anchor_authority(
            exact_by_subject[subject_id]
        ))
        for subject_id in window_anchor_ids
    }
    if len(anchor_authorities) != 1:
        raise ValueError("同一 TMDB 身份的锚点季度资料互相矛盾")
    canonical_anchor_id = window_anchor_ids[0]
    canonical_candidate = exact_by_subject[canonical_anchor_id]
    matches = []
    for left in range(left_limit, selected_index + 1):
        for right in range(selected_index, right_limit + 1):
            candidate_ids = ordered_ids[left : right + 1]
            candidate_anchor_ids = tuple(
                subject_id for subject_id in candidate_ids
                if subject_id in exact_by_subject
                and str(
                    exact_by_subject[subject_id].get("tmdb_tv_id") or ""
                ) == tmdb_id
            )
            if not candidate_anchor_ids:
                continue
            overlay_ids = {
                subject_id for subject_id in candidate_ids
                if subject_id not in exact_by_subject
            }
            candidate_batch = _overlay_batch(
                plain_batch,
                row_by_subject,
                overlay_subject_ids=overlay_ids,
                selected_subject_id=selected_id,
                selected_candidate=canonical_candidate,
            )
            candidate_relations = [{
                "from": candidate_ids[index],
                "to": candidate_ids[index + 1],
                "relation": "sequel",
            } for index in range(len(candidate_ids) - 1)]
            candidate_resolution = resolve_provider_link(
                selected_provider={
                    "namespace": "bangumi",
                    "entity_type": "subject",
                    "provider_id": selected_id,
                },
                bangumi_subjects=[subject_by_id[item] for item in candidate_ids],
                bangumi_relations=candidate_relations,
                subject_observations=candidate_batch,
            )
            if (
                candidate_resolution.review_disposition == "reviewable"
                and str(candidate_resolution.tmdb_tv_id or "") == tmdb_id
            ):
                matches.append((
                    len(candidate_ids),
                    left,
                    right,
                    candidate_batch,
                    candidate_resolution,
                ))
    if not matches:
        raise ValueError("当前资料没有唯一相容的季度分段")
    maximum = max(item[0] for item in matches)
    maximal = [item for item in matches if item[0] == maximum]
    if len(maximal) != 1:
        raise ValueError("当前资料存在多个同样相容的季度分段")
    _length, left, right, working_batch, resolution = maximal[0]
    segment_ids = ordered_ids[left : right + 1]
    anchor_ids = tuple(sorted(
        subject_id for subject_id in segment_ids
        if subject_id in exact_by_subject
        and str(exact_by_subject[subject_id].get("tmdb_tv_id") or "") == tmdb_id
    ))

    boundaries = []
    for outside_index, inside_index in ((left - 1, left), (right + 1, right)):
        if outside_index < 0 or outside_index >= len(ordered_ids):
            continue
        outside_id = ordered_ids[outside_index]
        inside_id = ordered_ids[inside_index]
        from_id, to_id = (
            (outside_id, inside_id)
            if outside_index < inside_index else (inside_id, outside_id)
        )
        exact = exact_by_subject.get(outside_id)
        boundaries.append({
            "from_subject_id": from_id,
            "to_subject_id": to_id,
            "relation": "sequel",
            "boundary_kind": (
                "different_unique_tmdb_id"
                if exact is not None else "season_incompatible_manual"
            ),
            "outside_tmdb_tv_id": (
                str(exact.get("tmdb_tv_id") or "") if exact else ""
            ),
        })
    normalized_boundaries = tuple(sorted(
        boundaries,
        key=lambda item: (
            str(item["from_subject_id"]), str(item["to_subject_id"])
        ),
    ))
    resolution_payload = {
        "tmdb_tv_id": resolution.tmdb_tv_id,
        "work_segment": {"subject_ids": tuple(segment_ids)},
        "season_assignments": tuple(resolution.season_assignments),
        "derived_episode_ranges": tuple(resolution.derived_episode_ranges),
        "boundaries": normalized_boundaries,
        "mapping_shape": resolution.mapping_shape,
        "season_count_policy_version": resolution.season_count_policy_version,
    }
    reason_codes = tuple(dict.fromkeys(
        tuple(resolution.reason_codes)
        + (
            ("linear_one_to_one_air_date_locked",)
            if resolution.season_count_policy_version == "date-count-delta-v1"
            else ()
        )
    ))
    resolution = replace(
        resolution,
        reason_codes=reason_codes,
        work_segment={"subject_ids": tuple(segment_ids)},
        boundaries=normalized_boundaries,
        resolution_fingerprint=_canonical_sha256(resolution_payload),
    )
    return ProviderSubjectCohortSegment(
        selected_subject_id=selected_id,
        segment_subject_ids=tuple(segment_ids),
        anchor_subject_ids=anchor_ids,
        anchor_tmdb_tv_id=tmdb_id,
        anchor_evidence_fingerprints={
            subject_id: str(
                row_by_subject[subject_id].get("response_fingerprint") or ""
            )
            for subject_id in anchor_ids
        },
        subject_evidence_fingerprints={
            subject_id: str(
                row_by_subject[subject_id].get("response_fingerprint") or ""
            )
            for subject_id in segment_ids
        },
        working_batch=working_batch,
        resolution=resolution,
    )


def _freeze_selected_exact_direct_edges(
    *,
    selected_subject_id: str,
    subject_observations: Mapping[str, object],
    bangumi_subjects: object,
    bangumi_relations: object,
) -> tuple[dict[str, object], ...]:
    """Preserve the legacy ambiguous-edge projection for exact selections."""

    if (
        not isinstance(bangumi_subjects, (list, tuple))
        or not isinstance(bangumi_relations, (list, tuple))
        or subject_observations.get("truncated") is not False
        or subject_observations.get("boundary_complete") is not True
    ):
        return ()
    rows = _subject_rows(subject_observations)
    if _exact_tmdb_candidate(rows.get(selected_subject_id)) is None:
        return ()
    titles = {
        str(item.get("provider_id") or item.get("id") or ""): str(
            item.get("title") or item.get("name_cn") or item.get("original_title")
            or item.get("name") or ""
        ).strip()
        for item in bangumi_subjects
        if isinstance(item, Mapping)
    }
    evidence_fingerprint = str(
        subject_observations.get("evidence_fingerprint") or ""
    )
    edges = []
    for relation in bangumi_relations:
        if not isinstance(relation, Mapping):
            continue
        source_id, target_id, relation_kind = _canonical_relation_edge(relation)
        if selected_subject_id not in {source_id, target_id}:
            continue
        neighbour_id = (
            target_id if source_id == selected_subject_id else source_id
        )
        if (
            relation_kind != "sequel"
            or not _complete_no_match_observation(rows.get(neighbour_id))
        ):
            continue
        edges.append({
            "from_subject_id": source_id,
            "to_subject_id": target_id,
            "relation": relation_kind,
            "neighbour_subject_id": neighbour_id,
            "neighbour_title": titles.get(neighbour_id, ""),
            "evidence_fingerprint": evidence_fingerprint,
        })
    unique = {
        (
            str(item["from_subject_id"]),
            str(item["to_subject_id"]),
            str(item["relation"]),
        ): item
        for item in edges
    }
    return tuple(unique[key] for key in sorted(unique))


def freeze_provider_subject_boundary_edges(
    *,
    selected_subject_id: str,
    subject_observations: Mapping[str, object],
    bangumi_subjects: object,
    bangumi_relations: object,
) -> tuple[dict[str, object], ...]:
    """Freeze selected-neighbour actions plus their cohort anchor authority."""

    try:
        segment = resolve_provider_subject_cohort_segment(
            selected_subject_id=selected_subject_id,
            subject_observations=subject_observations,
            bangumi_subjects=bangumi_subjects,
            bangumi_relations=bangumi_relations,
        )
    except (ValueError, RuntimeError):
        return _freeze_selected_exact_direct_edges(
            selected_subject_id=str(selected_subject_id or "").strip(),
            subject_observations=subject_observations,
            bangumi_subjects=bangumi_subjects,
            bangumi_relations=bangumi_relations,
        )
    selected_id = segment.selected_subject_id
    rows = _subject_rows(subject_observations)
    selected_unresolved = _complete_no_match_observation(rows.get(selected_id))
    subject_titles = {
        str(item.get("provider_id") or item.get("id") or ""): str(
            item.get("title") or item.get("name_cn") or item.get("original_title")
            or item.get("name") or ""
        ).strip()
        for item in bangumi_subjects or ()
        if isinstance(item, Mapping)
    }
    evidence_fingerprint = str(
        subject_observations.get("evidence_fingerprint") or ""
    )
    segment_ids = set(segment.segment_subject_ids)
    edges: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for relation in bangumi_relations:
        if not isinstance(relation, Mapping):
            continue
        source_id, target_id, relation_kind = _canonical_relation_edge(relation)
        if selected_id not in {source_id, target_id} or relation_kind != "sequel":
            continue
        if source_id not in segment_ids or target_id not in segment_ids:
            continue
        neighbour_id = target_id if source_id == selected_id else source_id
        if not (
            selected_unresolved
            or _complete_no_match_observation(rows.get(neighbour_id))
        ):
            continue
        key = (source_id, target_id, relation_kind)
        if key in seen:
            continue
        seen.add(key)
        edges.append({
            "from_subject_id": source_id,
            "to_subject_id": target_id,
            "relation": relation_kind,
            "neighbour_subject_id": neighbour_id,
            "neighbour_title": subject_titles.get(neighbour_id, ""),
            "evidence_fingerprint": evidence_fingerprint,
            "anchor_subject_ids": segment.anchor_subject_ids,
            "anchor_tmdb_tv_id": segment.anchor_tmdb_tv_id,
            "anchor_evidence_fingerprints": dict(
                segment.anchor_evidence_fingerprints
            ),
        })
    if selected_unresolved and len(edges) > 1:
        ordered_ids = segment.segment_subject_ids
        selected_index = ordered_ids.index(selected_id)
        anchor_indexes = tuple(
            ordered_ids.index(subject_id)
            for subject_id in segment.anchor_subject_ids
        )
        nearest_anchor = min(
            anchor_indexes,
            key=lambda index: (abs(index - selected_index), index),
        )
        neighbour_index = selected_index + (
            -1 if nearest_anchor < selected_index else 1
        )
        neighbour_id = ordered_ids[neighbour_index]
        edges = [
            item for item in edges
            if str(item.get("neighbour_subject_id") or "") == neighbour_id
        ]
    if not edges and not selected_unresolved:
        legacy_edges = _freeze_selected_exact_direct_edges(
            selected_subject_id=selected_id,
            subject_observations=subject_observations,
            bangumi_subjects=bangumi_subjects,
            bangumi_relations=bangumi_relations,
        )
        edges = [{
            **dict(item),
            "anchor_subject_ids": segment.anchor_subject_ids,
            "anchor_tmdb_tv_id": segment.anchor_tmdb_tv_id,
            "anchor_evidence_fingerprints": dict(
                segment.anchor_evidence_fingerprints
            ),
        } for item in legacy_edges]
    return tuple(sorted(
        edges,
        key=lambda item: (
            str(item["from_subject_id"]),
            str(item["to_subject_id"]),
            str(item["relation"]),
        ),
    ))


def resolve_provider_subject_boundary_decision(
    failure: Mapping[str, object],
    *,
    same_work: bool,
    boundary_edge: Mapping[str, object],
) -> tuple[ProviderLinkResolution, Optional[dict[str, object]]]:
    """Resolve one explicit neighbour decision from persisted evidence only."""

    selected_id = str(failure.get("selected_subject_id") or "")
    batch = failure.get("subject_observations")
    subjects = failure.get("bangumi_subjects")
    relations = failure.get("bangumi_relations")
    evidence_fingerprint = str(failure.get("evidence_fingerprint") or "")
    if (
        not selected_id
        or not isinstance(batch, Mapping)
        or not isinstance(subjects, (list, tuple))
        or not isinstance(relations, (list, tuple))
        or len(evidence_fingerprint) != 64
    ):
        raise ValueError("保存的相邻作品资料不完整")
    plain_batch = json.loads(json.dumps(batch, ensure_ascii=False))
    plain_subjects = [dict(item) for item in subjects if isinstance(item, Mapping)]
    plain_relations = [dict(item) for item in relations if isinstance(item, Mapping)]
    row_by_subject = _subject_rows(plain_batch)
    selected_row = row_by_subject.get(selected_id)
    if selected_row is None:
        raise ValueError("保存的所选作品资料不完整")

    frozen_edges = failure.get("provider_subject_boundary_edges")
    if not isinstance(frozen_edges, (list, tuple)):
        raise ValueError("保存的相邻作品关系不完整")
    requested_edge = {
        "from_subject_id": str(boundary_edge.get("from_subject_id") or ""),
        "to_subject_id": str(boundary_edge.get("to_subject_id") or ""),
        "relation": _normalized_relation(boundary_edge.get("relation")),
        "evidence_fingerprint": str(
            boundary_edge.get("evidence_fingerprint") or ""
        ),
    }
    adjacent = next((
        relation for relation in plain_relations
        if _canonical_relation_edge(relation) == (
            requested_edge["from_subject_id"],
            requested_edge["to_subject_id"],
            requested_edge["relation"],
        )
    ), None)
    frozen_match = next((
        item for item in frozen_edges
        if isinstance(item, Mapping)
        and str(item.get("from_subject_id") or "") == requested_edge["from_subject_id"]
        and str(item.get("to_subject_id") or "") == requested_edge["to_subject_id"]
        and _normalized_relation(item.get("relation")) == requested_edge["relation"]
        and str(item.get("evidence_fingerprint") or "") == evidence_fingerprint
        and requested_edge["evidence_fingerprint"] == evidence_fingerprint
    ), None)
    if adjacent is None or frozen_match is None:
        raise ValueError("相邻作品关系已经变化")
    source_id = requested_edge["from_subject_id"]
    target_id = requested_edge["to_subject_id"]
    if selected_id not in {source_id, target_id}:
        raise ValueError("相邻作品关系与所选作品不一致")

    boundary_decision = None
    if same_work:
        segment = resolve_provider_subject_cohort_segment(
            selected_subject_id=selected_id,
            subject_observations=plain_batch,
            bangumi_subjects=plain_subjects,
            bangumi_relations=plain_relations,
        )
        edge_inside_segment = (
            source_id in segment.segment_subject_ids
            and target_id in segment.segment_subject_ids
        )
        edge_is_segment_boundary = any(
            str(item.get("from_subject_id") or "") == source_id
            and str(item.get("to_subject_id") or "") == target_id
            and _normalized_relation(item.get("relation"))
            == requested_edge["relation"]
            for item in segment.resolution.boundaries
            if isinstance(item, Mapping)
        )
        if not edge_inside_segment and not edge_is_segment_boundary:
            raise ValueError("相邻作品关系不属于当前唯一季度分段")
        frozen_anchor_ids = tuple(sorted(
            str(item) for item in frozen_match.get("anchor_subject_ids", ()) or ()
            if str(item)
        ))
        if (
            frozen_anchor_ids != tuple(sorted(segment.anchor_subject_ids))
            or str(frozen_match.get("anchor_tmdb_tv_id") or "")
            != segment.anchor_tmdb_tv_id
            or dict(frozen_match.get("anchor_evidence_fingerprints") or {})
            != dict(segment.anchor_evidence_fingerprints)
        ):
            raise ValueError("TMDB 锚点资料已经变化")
        resolution = segment.resolution
        cohort_edges = tuple({
            "from_subject_id": segment.segment_subject_ids[index],
            "to_subject_id": segment.segment_subject_ids[index + 1],
            "relation": "sequel",
            "evidence_fingerprint": evidence_fingerprint,
        } for index in range(len(segment.segment_subject_ids) - 1))
        decision_payload = {
            "schema_version": 2,
            "selected_subject_id": selected_id,
            "cohort_subject_ids": segment.segment_subject_ids,
            "cohort_edges": cohort_edges,
            "subject_evidence_fingerprints": dict(
                segment.subject_evidence_fingerprints
            ),
            "selected_tmdb_tv_id": segment.anchor_tmdb_tv_id,
            "anchor_subject_ids": segment.anchor_subject_ids,
            "anchor_tmdb_tv_id": segment.anchor_tmdb_tv_id,
            "anchor_evidence_fingerprints": dict(
                segment.anchor_evidence_fingerprints
            ),
            "season_assignments": dict(resolution.season_assignments),
            "derived_episode_ranges": tuple(resolution.derived_episode_ranges),
            "resolution_fingerprint": str(
                resolution.resolution_fingerprint or ""
            ),
            "evidence_fingerprint": evidence_fingerprint,
        }
        boundary_decision = {
            **decision_payload,
            "cohort_decision_id": (
                "cohort-" + provider_subject_cohort_request_fingerprint(
                    decision_payload
                )
            ),
            "from_subject_id": source_id,
            "to_subject_id": target_id,
            "relation": requested_edge["relation"],
            "decision": "same_work",
        }
    else:
        working_batch = _rehash_subject_observation_batch(
            plain_batch, [selected_row]
        )
        working_subjects = [
            item for item in plain_subjects
            if str(item.get("provider_id") or item.get("id") or "") == selected_id
        ]
        resolution = resolve_provider_link(
            selected_provider={
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": selected_id,
            },
            bangumi_subjects=working_subjects,
            bangumi_relations=[],
            subject_observations=working_batch,
        )
        if (
            resolution.review_disposition != "reviewable"
            or not resolution.tmdb_tv_id
        ):
            raise ValueError("当前资料仍不足以生成可靠的季度映射")
        resolution = replace(resolution, boundaries=({
            "from_subject_id": source_id,
            "to_subject_id": target_id,
            "relation": requested_edge["relation"],
            "boundary_kind": "explicit_user_separate",
            "outside_tmdb_tv_id": "",
            "evidence_fingerprint": evidence_fingerprint,
        },))

    resolution = replace(
        resolution,
        proposed_series_key=f"series-{uuid.uuid5(uuid.NAMESPACE_URL, 'bar:tmdb:tv:' + str(resolution.tmdb_tv_id))}",
        subject_observations=dict(plain_batch),
        evidence_path=(
            evidence_fingerprint,
            str(resolution.resolution_fingerprint or ""),
        ),
    )
    return resolution, boundary_decision


def build_provider_subject_cohort_preview(
    failure: Mapping[str, object],
    *,
    expected_decision_revision: int,
    boundary_edge: Mapping[str, object],
    target_root_snapshot: Mapping[str, object],
    target_root_config_fingerprint: object,
    work_domain: str,
    group_kind: str,
) -> dict[str, object]:
    """Build the persisted, user-visible authority for one same-work click."""

    if not isinstance(target_root_snapshot, Mapping):
        raise ValueError("草案缺少冻结目标配置，请重新生成季度预览")
    snapshot = TargetRootSnapshot.from_dict(target_root_snapshot)
    if str(target_root_config_fingerprint or "").strip() != snapshot.fingerprint:
        raise ValueError("草案目标配置已经变化，请重新生成季度预览")
    resolved_target_root = resolve_target_root(
        work_domain, group_kind, snapshot
    )

    resolution, decision = resolve_provider_subject_boundary_decision(
        failure,
        same_work=True,
        boundary_edge=boundary_edge,
    )
    if not isinstance(decision, Mapping):
        raise ValueError("未生成完整的季度合并预览")
    subject_ids = tuple(
        str(item) for item in decision.get("cohort_subject_ids", ()) or ()
        if str(item)
    )
    subjects = {
        str(item.get("provider_id") or item.get("id") or ""): item
        for item in failure.get("bangumi_subjects", ()) or ()
        if isinstance(item, Mapping)
    }
    assignments = dict(resolution.season_assignments)
    assignment_rows = tuple({
        "bangumi_subject_id": subject_id,
        "title": str(
            subjects.get(subject_id, {}).get("title")
            or subjects.get(subject_id, {}).get("name_cn")
            or subjects.get(subject_id, {}).get("original_title")
            or subjects.get(subject_id, {}).get("name")
            or subject_id
        ),
        "season_number": assignments[subject_id],
    } for subject_id in subject_ids if subject_id in assignments)
    outside_ids = []
    for boundary in resolution.boundaries:
        for key in ("from_subject_id", "to_subject_id"):
            subject_id = str(boundary.get(key) or "")
            if subject_id and subject_id not in subject_ids:
                outside_ids.append(subject_id)
    outside_titles = tuple(
        str(
            subjects.get(subject_id, {}).get("title")
            or subjects.get(subject_id, {}).get("name_cn")
            or subjects.get(subject_id, {}).get("original_title")
            or subjects.get(subject_id, {}).get("name")
            or subject_id
        )
        for subject_id in dict.fromkeys(outside_ids)
    )
    request = {
        "schema_version": 3,
        "same_work": True,
        "selected_subject_id": str(
            decision.get("selected_subject_id") or ""
        ),
        "cohort_subject_ids": subject_ids,
        "cohort_edges": tuple(
            dict(item) for item in decision.get("cohort_edges", ()) or ()
            if isinstance(item, Mapping)
        ),
        "subject_evidence_fingerprints": dict(
            decision.get("subject_evidence_fingerprints") or {}
        ),
        "selected_tmdb_tv_id": str(
            decision.get("selected_tmdb_tv_id") or ""
        ),
        "season_assignments": assignments,
        "derived_episode_ranges": tuple(
            dict(item) for item in resolution.derived_episode_ranges
        ),
        "resolution_fingerprint": str(
            resolution.resolution_fingerprint or ""
        ),
        "evidence_fingerprint": str(
            failure.get("evidence_fingerprint") or ""
        ),
        "expected_decision_revision": expected_decision_revision,
        "cohort_decision_id": str(
            decision.get("cohort_decision_id") or ""
        ),
        "target_root_config_fingerprint": snapshot.fingerprint,
        "resolved_target_root": str(resolved_target_root),
    }
    return {
        "schema_version": 3,
        "request_fingerprint": provider_subject_cohort_request_fingerprint(
            request
        ),
        "request": request,
        "selected_subject_id": request["selected_subject_id"],
        "cohort_subject_ids": subject_ids,
        "cohort_edges": request["cohort_edges"],
        "subject_evidence_fingerprints": request[
            "subject_evidence_fingerprints"
        ],
        "selected_tmdb_tv_id": request["selected_tmdb_tv_id"],
        "season_assignments": assignment_rows,
        "derived_episode_ranges": request["derived_episode_ranges"],
        "resolution_fingerprint": request["resolution_fingerprint"],
        "cohort_decision_id": request["cohort_decision_id"],
        "target_root_config_fingerprint": snapshot.fingerprint,
        "resolved_target_root": str(resolved_target_root),
        "outside_subject_titles": outside_titles,
    }


def build_provider_subject_boundary_failure(
    failure: Mapping[str, object],
    error: BaseException,
) -> dict[str, object]:
    """Return a persistent public failure without exposing private authority."""

    rows = _subject_rows(
        failure.get("subject_observations")
        if isinstance(failure.get("subject_observations"), Mapping)
        else {}
    )
    unresolved_ids = tuple(
        subject_id for subject_id, row in rows.items()
        if _exact_tmdb_candidate(row) is None
    )
    subjects = {
        str(item.get("provider_id") or item.get("id") or ""): item
        for item in failure.get("bangumi_subjects", ()) or ()
        if isinstance(item, Mapping)
    }
    titles = tuple(
        str(
            subjects.get(subject_id, {}).get("title")
            or subjects.get(subject_id, {}).get("name_cn")
            or subjects.get(subject_id, {}).get("original_title")
            or subjects.get(subject_id, {}).get("name")
            or "未命名相邻作品"
        )
        for subject_id in unresolved_ids
    )
    detail = str(error).strip()
    category = (
        "ambiguous_segment"
        if "多" in detail or "分支" in detail or "循环" in detail
        else "incompatible_or_incomplete_evidence"
    )
    return {
        "schema_version": 1,
        "public_code": "cohort_resolution_failed",
        "public_title": "无法安全确定季度边界",
        "category": category,
        "affected_subject_titles": titles or ("相邻作品",),
        "safe_actions": ("change_work", "separate", "remove"),
        "technical_summary": (
            "候选季度关系不是唯一结果"
            if category == "ambiguous_segment"
            else "相邻作品资料不完整或与所选电视剧季度不相容"
        ),
    }


def _bind_projection_to_plan_sources(
    plan: RenamePlan,
    projection: Mapping[str, object],
) -> dict[str, object]:
    by_episode = {
        int(row["source_episode"]): int(row["target_episode"])
        for row in projection.get("episode_mapping", ()) or ()
        if isinstance(row, Mapping)
    }
    def source_row(entry) -> dict[str, object]:
        stat = entry.source_path.stat()
        return {
            "source_path": str(entry.source_path),
            "source_episode": int(entry.episode_id),
            "target_episode": by_episode[int(entry.episode_id)],
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "source_device": stat.st_dev,
            "source_inode": stat.st_ino,
        }

    source_rows = tuple(
        sorted(
            (
                source_row(entry)
                for entry in plan.entries
                if entry.media_kind == "video"
                and isinstance(entry.episode_id, int)
                and int(entry.episode_id) in by_episode
            ),
            key=lambda row: str(row["source_path"]).casefold(),
        )
    )
    inventory_payload = {
        "inventory_scope": projection.get("inventory_scope"),
        "source_episodes": tuple(
            {
                "source_path": row["source_path"],
                "source_episode": row["source_episode"],
            }
            for row in source_rows
        ),
    }
    payload = {
        key: value
        for key, value in projection.items()
        if key != "projection_fingerprint"
    }
    payload["episode_mapping"] = source_rows
    payload["inventory_fingerprint"] = _canonical_sha256(inventory_payload)
    return {**payload, "projection_fingerprint": _canonical_sha256(payload)}


def apply_source_episode_projection(
    plan: RenamePlan,
    projection: Mapping[str, object],
) -> RenamePlan:
    """Project one frozen source mapping into all video/subtitle Plan rows."""

    source_targets = {
        str(row.get("source_path") or ""): int(row["target_episode"])
        for row in projection.get("episode_mapping", ()) or ()
        if isinstance(row, Mapping)
        and str(row.get("source_path") or "")
        and isinstance(row.get("target_episode"), int)
    }
    by_episode = {}
    for row in projection.get("episode_mapping", ()) or ():
        if not isinstance(row, Mapping):
            continue
        source_episode = row.get("source_episode")
        target_episode = row.get("target_episode")
        if isinstance(source_episode, int) and isinstance(target_episode, int):
            by_episode.setdefault(source_episode, set()).add(target_episode)
    unique_episode_targets = {
        source: next(iter(targets))
        for source, targets in by_episode.items()
        if len(targets) == 1
    }
    season_number = int(plan.metadata.get("season_number") or 1)
    entries = []
    owner_ref_map: dict[str, str] = {}
    for entry in plan.entries:
        target_episode = source_targets.get(str(entry.source_path))
        if target_episode is None and isinstance(entry.episode_id, int):
            target_episode = unique_episode_targets.get(int(entry.episode_id))
        if target_episode is None:
            entries.append(entry)
            continue
        marker = f"S{season_number:02d}E{target_episode:02d}"
        target_name = re.sub(
            r"S\d{2}E\d+", marker, entry.target_path.name, count=1
        )
        projected = replace(
            entry,
            target_path=entry.target_path.with_name(target_name),
            season_id=season_number,
            episode_id=target_episode,
        )
        old_asset = projected.presentation_asset
        old_descriptor = old_asset.version_descriptor if old_asset else None
        old_ref = old_descriptor.version_ref if old_descriptor else None
        projected = replace(
            projected,
            presentation_asset=rebind_content_version_identity(
                projected.presentation_asset,
                group_kind="tv_episodes",
                season_id=season_number,
                episode_id=target_episode,
            ),
        )
        new_asset = projected.presentation_asset
        new_descriptor = new_asset.version_descriptor if new_asset else None
        new_ref = new_descriptor.version_ref if new_descriptor else None
        if old_ref and new_ref:
            owner_ref_map[old_ref] = new_ref
        entries.append(projected)

    if owner_ref_map:
        entries = [
            replace(
                entry,
                presentation_asset=replace(
                    entry.presentation_asset,
                    owner_version_ref=owner_ref_map.get(
                        entry.presentation_asset.owner_version_ref,
                        entry.presentation_asset.owner_version_ref,
                    ),
                ),
            )
            if entry.presentation_asset is not None
            and entry.presentation_asset.owner_version_ref in owner_ref_map
            else entry
            for entry in entries
        ]

    # Two source files may intentionally represent different technical
    # versions of one logical episode.  The inventory draft initially gives
    # both a structural ``default`` identity; once a projection makes the
    # collision explicit, bind an exact filename resolution token so the two
    # versions remain distinct without inventing HDR/audio facts.
    content_by_episode: dict[tuple[int, int], list[int]] = {}
    for index, entry in enumerate(entries):
        asset = entry.presentation_asset
        if (
            entry.media_kind == "video"
            and isinstance(entry.season_id, int)
            and isinstance(entry.episode_id, int)
            and asset is not None
            and asset.asset_role.kind == "content_version"
        ):
            content_by_episode.setdefault(
                (entry.season_id, entry.episode_id), []
            ).append(index)
    for (bound_season, bound_episode), indexes in content_by_episode.items():
        if len(indexes) < 2:
            continue
        resolution_rows = []
        for index in indexes:
            match = re.search(
                r"(?<!\d)(2160p|1080p|720p|480p)(?!\d)",
                entries[index].source_path.name,
                flags=re.IGNORECASE,
            )
            resolution_rows.append(
                (index, match.group(1).lower() if match else None)
            )
        labels = [label for _, label in resolution_rows if label]
        if len(labels) != len(indexes) or len(set(labels)) != len(labels):
            continue
        for index, label in resolution_rows:
            entry = entries[index]
            asset = entry.presentation_asset
            descriptor = asset.version_descriptor if asset is not None else None
            edition = descriptor.edition.value if descriptor is not None else None
            edition_key = getattr(edition, "key", None)
            if descriptor is None or not edition_key or label is None:
                continue
            resolution = EvidenceValue(
                status="resolved",
                value=label,
                selected_source="filename_hint",
                candidates=(EvidenceCandidate(
                    value=label,
                    source="filename_hint",
                    evidence=("源文件名包含明确分辨率 token",),
                ),),
            )
            technical_key = f"resolution-{label}"
            rebound = replace(
                descriptor,
                version_ref=build_version_ref(
                    asset.presentation_group,
                    bound_season,
                    bound_episode,
                    edition_key,
                    technical_key,
                ),
                technical_variant_key=technical_key,
                resolution=resolution,
                display_label=label,
            )
            entries[index] = replace(
                entry,
                presentation_asset=replace(asset, version_descriptor=rebound),
            )

    groups = tuple(
        replace(
            group,
            preferred_version_ref=(
                None if group.kind == "tv_episodes"
                else group.preferred_version_ref
            ),
        )
        for group in plan.presentation_groups
    )
    metadata = dict(plan.metadata)
    metadata["source_episode_projection"] = dict(projection)
    metadata.pop("source_episode_projection_candidates", None)
    return replace(
        plan,
        entries=tuple(entries),
        presentation_groups=groups,
        metadata=metadata,
    )


def freeze_source_episode_projection(
    plan: RenamePlan,
    resolution: ProviderLinkResolution,
    *,
    selected_subject_id: str,
) -> tuple[RenamePlan, Optional[Mapping[str, object]]]:
    """Freeze a unique projection or bounded candidates without file I/O."""

    if resolution.mapping_shape != "many_subjects_one_season":
        return plan, None
    selected_range = next(
        (
            row for row in resolution.derived_episode_ranges
            if str(row.get("bangumi_subject_id") or "") == selected_subject_id
        ),
        None,
    )
    if not isinstance(selected_range, Mapping):
        return plan, None
    source_numbers = tuple(
        sorted({
            int(entry.episode_id)
            for entry in plan.entries
            if entry.media_kind == "video" and isinstance(entry.episode_id, int)
        })
    )
    derived = derive_source_episode_projection(
        source_episode_numbers=source_numbers,
        target_episode_start=int(selected_range["episode_start"]),
        target_episode_end=int(selected_range["episode_end"]),
        inventory_scope="complete",
    )
    if derived.get("status") == "reviewable":
        projection = _bind_projection_to_plan_sources(plan, derived)
        return apply_source_episode_projection(plan, projection), projection
    candidates = tuple(
        _bind_projection_to_plan_sources(plan, candidate)
        for candidate in derived.get("candidates", ()) or ()
        if isinstance(candidate, Mapping)
    )
    if len(candidates) > 1:
        metadata = dict(plan.metadata)
        metadata["source_episode_projection_candidates"] = candidates
        return replace(plan, metadata=metadata), None
    return plan, None


def stage_provider_link_decision(
    state: DecisionState,
    resolution: ProviderLinkResolution,
) -> DecisionState:
    """Return a DecisionState containing one complete cross-provider decision."""

    if resolution.automation_level not in {"L0", "L1", "L2", "L3"}:
        raise ValueError("当前跨站映射不能进入整理计划审核")
    if resolution.automation_level == "L0":
        if resolution.status != "materialized_confirmed":
            raise ValueError("现有作品信息尚未确认，不能离线复用")
        if not resolution.anchor_mapping_id:
            raise ValueError("现有作品信息缺少版本锚点")
    elif resolution.status != "inferred" or not resolution.requires_plan_review:
        raise ValueError("跨站映射不是待审核推断")
    if not resolution.proposed_series_key or not resolution.tmdb_tv_id:
        raise ValueError("跨站映射缺少 Series/TMDB 身份")
    if not resolution.season_assignments or not resolution.evidence_path:
        raise ValueError("跨站映射缺少季度或证据路径")

    snapshot = {
        "automation_level": resolution.automation_level,
        "series_key": resolution.proposed_series_key,
        "tmdb_tv_id": resolution.tmdb_tv_id,
        "season_assignments": [
            {"bangumi_subject_id": subject_id, "season_number": season_number}
            for subject_id, season_number in resolution.season_assignments
        ],
        "evidence_path": list(resolution.evidence_path),
        "reason_codes": list(resolution.reason_codes),
        "anchor": (
            {
                "mapping_id": resolution.anchor_mapping_id,
                "revision": resolution.anchor_revision,
                "evidence_fingerprint": resolution.anchor_fingerprint,
            }
            if resolution.anchor_mapping_id else None
        ),
    }
    if resolution.subject_observations is not None:
        snapshot.update({
            "schema_version": 2,
            "selected_subject_id": str(
                resolution.subject_observations.get("selected_subject_id") or ""
            ),
            "subject_observations": dict(resolution.subject_observations),
            "work_segment": dict(resolution.work_segment or {}),
            "derived_episode_ranges": [
                dict(item) for item in resolution.derived_episode_ranges
            ],
            "boundaries": [dict(item) for item in resolution.boundaries],
            "mapping_shape": resolution.mapping_shape,
            "season_count_policy_version": resolution.season_count_policy_version,
        })
        resolution_payload = {
            key: snapshot.get(key)
            for key in (
                "tmdb_tv_id", "work_segment", "season_assignments",
                "derived_episode_ranges", "boundaries", "mapping_shape",
                "season_count_policy_version",
            )
        }
        snapshot["resolution_fingerprint"] = hashlib.sha256(
            json.dumps(
                resolution_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    fingerprint = hashlib.sha256(
        json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    binding = {
        "mapping_id": resolution.anchor_mapping_id or f"provider-link-{uuid.uuid4()}",
        "revision": resolution.anchor_revision or 0,
        "evidence_fingerprint": fingerprint,
        "status": resolution.status,
        **snapshot,
    }
    return replace(state, provider_link_binding=binding)


def stage_provider_link_series_decision(
    state: DecisionState,
    resolution: ProviderLinkResolution,
    *,
    canonical_root: Path,
    catalog_revision: int,
) -> DecisionState:
    """Freeze one cross-provider TV decision as executable series intent.

    ``synchronized_candidate`` already created the season identity.  Reusing it
    here is important: the cross-provider link, DecisionState projection and
    eventual SeriesCatalog record must not invent independent series/season
    identifiers for the same recognition result.
    """

    staged = stage_provider_link_decision(state, resolution)
    root = Path(canonical_root)
    if not str(root) or not isinstance(catalog_revision, int) or catalog_revision < 0:
        raise ValueError("作品季度登记信息不完整")
    season = staged.season_binding
    if not isinstance(season, Mapping):
        raise ValueError("作品季度登记缺少季度决定")
    binding_id = str(season.get("season_binding_id") or "")
    season_number = season.get("season_number")
    if not binding_id or not isinstance(season_number, int):
        raise ValueError("作品季度登记缺少季度决定")

    linked_subject_ids = {
        str(item.get("provider_id") or "")
        for item in staged.provider_evidence
        if item.get("namespace") == "bangumi"
        and item.get("entity_type") == "subject"
        and str(item.get("provider_id") or "")
    }
    assignments = dict(resolution.season_assignments)
    if not linked_subject_ids or not any(
        assignments.get(subject_id) == season_number
        for subject_id in linked_subject_ids
    ):
        raise ValueError("所选作品与季度决定不一致")

    series_key = str(resolution.proposed_series_key or "")
    if not series_key:
        raise ValueError("作品季度登记缺少系列身份")
    return replace(
        staged,
        series_binding={
            "series_key": series_key,
            "canonical_root": str(root),
            "catalog_revision": catalog_revision,
            "decision_source": "provider_crosswalk",
        },
        layout_mode="tv_episodes",
        catalog_link_intent=None,
    )


def provider_link_review_summary(
    value: Optional[Mapping[str, object]],
) -> Optional[dict[str, object]]:
    """Build a plain-language, renderer-independent Plan review projection."""

    if not isinstance(value, Mapping):
        return None
    level = str(value.get("automation_level") or "")
    series_key = str(value.get("series_key") or "")
    tmdb_id = str(value.get("tmdb_tv_id") or "")
    assignments = value.get("season_assignments")
    evidence = value.get("evidence_path")
    if level not in {"L0", "L1", "L2", "L3"} or not series_key or not tmdb_id:
        return None
    if not isinstance(assignments, (list, tuple)) or not isinstance(
        evidence, (list, tuple)
    ):
        return None
    rows = []
    for item in assignments:
        if not isinstance(item, Mapping):
            return None
        subject_id = str(item.get("bangumi_subject_id") or "")
        season_number = item.get("season_number")
        if not subject_id or not isinstance(season_number, int):
            return None
        rows.append(f"Bangumi #{subject_id} → 第 {season_number} 季")
    return {
        "level": level,
        "series_key": series_key,
        "tmdb_tv_id": tmdb_id,
        "assignments": tuple(rows),
        "evidence": tuple(str(item) for item in evidence if str(item)),
        "pending_notice": (
            "已使用现有作品信息；批准后才会执行文件整理。"
            if level == "L0"
            else "这是待审核匹配；批准只确认决定，尚未执行文件整理。"
        ),
    }
