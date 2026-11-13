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
    trusted_relation_tmdb_anchor,
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

def provider_subject_identity_candidates(
    failure: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    """Enumerate, but never choose, the selected subject's exact TV IDs."""

    selected_id = str(failure.get("selected_subject_id") or "").strip()
    batch = failure.get("subject_observations")
    evidence_fingerprint = str(failure.get("evidence_fingerprint") or "")
    if (
        not selected_id
        or not isinstance(batch, Mapping)
        or str(batch.get("evidence_fingerprint") or "") != evidence_fingerprint
    ):
        return ()
    selected_rows = [
        item for item in batch.get("subject_observations", ()) or ()
        if isinstance(item, Mapping)
        and str(item.get("bangumi_subject_id") or "") == selected_id
    ]
    if len(selected_rows) != 1:
        return ()
    exact_ids: set[str] = set()
    for query in selected_rows[0].get("queries", ()) or ():
        if (
            not isinstance(query, Mapping)
            or query.get("outcome") != "ok"
            or query.get("truncated") is not False
        ):
            continue
        for candidate in query.get("candidates", ()) or ():
            if not isinstance(candidate, Mapping):
                continue
            tmdb_id = str(candidate.get("tmdb_tv_id") or "")
            if (
                candidate.get("namespace") == "tmdb"
                and candidate.get("entity_type") == "tv"
                and candidate.get("match_tier") == "exact"
                and tmdb_id
                and str(candidate.get("provider_id") or "") == tmdb_id
            ):
                exact_ids.add(tmdb_id)
    rows: list[dict[str, object]] = []
    for tmdb_id in sorted(exact_ids):
        try:
            snapshot = dict(trusted_relation_tmdb_anchor(batch, tmdb_id))
        except (TypeError, ValueError):
            return ()
        snapshot.update({
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": tmdb_id,
            "tmdb_tv_id": tmdb_id,
        })
        snapshot["candidate_fingerprint"] = _canonical_sha256(snapshot)
        rows.append(snapshot)
    return tuple(rows)

def _filter_selected_identity_observation(
    row: Mapping[str, object],
    tmdb_tv_id: str,
) -> dict[str, object]:
    filtered = json.loads(json.dumps(row, ensure_ascii=False))
    queries: list[dict[str, object]] = []
    for raw_query in filtered.get("queries", ()) or ():
        if not isinstance(raw_query, Mapping):
            raise ValueError("保存的所选作品查询资料不完整")
        query = dict(raw_query)
        kept = [
            dict(candidate)
            for candidate in query.get("candidates", ()) or ()
            if isinstance(candidate, Mapping)
            and candidate.get("namespace") == "tmdb"
            and candidate.get("entity_type") == "tv"
            and candidate.get("match_tier") == "exact"
            and str(candidate.get("provider_id") or "") == tmdb_tv_id
            and str(candidate.get("tmdb_tv_id") or "") == tmdb_tv_id
        ]
        query["candidates"] = kept
        query["outcome"] = "ok" if kept else "no_match"
        queries.append(query)
    filtered["queries"] = queries
    filtered.pop("response_fingerprint", None)
    filtered["response_fingerprint"] = _canonical_sha256(filtered)
    return filtered

def is_provider_identity_boundary(value: Mapping[str, object]) -> bool:
    return str(value.get("boundary_kind") or "") == "selected_provider_identity"

def selected_provider_special_observation(
    evidence: Mapping[str, object],
    tmdb_tv_id: str,
    *,
    allow_legacy_single: bool = False,
) -> object:
    observations = evidence.get("provider_special_observations")
    selected = (
        observations.get(str(tmdb_tv_id or ""))
        if isinstance(observations, Mapping) else None
    )
    if selected is None and allow_legacy_single:
        selected = evidence.get("provider_special_observation")
    return selected

def build_provider_identity_selection_receipt(
    request_fingerprint: str,
    selected_subject_id: str,
    tmdb_tv_id: str,
    boundary_edge: Mapping[str, object],
    evidence_fingerprint: str,
    resolution: ProviderLinkResolution,
    target_root_config_fingerprint: str,
    resolved_target_root: str,
    expected_revision: int,
    naming_decision_fingerprint: str,
) -> dict[str, object]:
    return {
        "schema_version": 3,
        "request_fingerprint": request_fingerprint,
        "selected_subject_id": selected_subject_id,
        "selected_tmdb_tv_id": tmdb_tv_id,
        "candidate_fingerprint": str(
            boundary_edge.get("candidate_fingerprint") or ""
        ),
        "evidence_fingerprint": evidence_fingerprint,
        "season_assignments": dict(resolution.season_assignments),
        "resolution_fingerprint": str(resolution.resolution_fingerprint or ""),
        "target_root_config_fingerprint": target_root_config_fingerprint,
        "resolved_target_root": resolved_target_root,
        "expected_decision_revision": expected_revision,
        "result_decision_revision": expected_revision + 1,
        "canonical_naming_decision_fingerprint": naming_decision_fingerprint,
    }


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

def _bangumi_cohort_segment(selected_id, ordered_ids, subject_by_id, row_by_subject, plain_batch):
    """One explicit native-layout proposal; never rewrite TMDB observations."""
    assignments = tuple((subject, index) for index, subject in enumerate(ordered_ids, 1))
    ranges = []
    for subject, season in assignments:
        count = subject_by_id[subject].get('episode_count', subject_by_id[subject].get('eps'))
        if type(count) is not int or count < 1:
            raise ValueError('续作链缺少可冻结的普通集范围')
        ranges.append(dict(bangumi_subject_id=subject, work_identity_key='bangumi:subject:' + ordered_ids[0],
            season_number=season, episode_start=1, episode_end=count))
    resolution = ProviderLinkResolution(automation_level='L2', status='inferred',
        season_assignments=assignments, work_segment={'subject_ids': ordered_ids},
        derived_episode_ranges=tuple(ranges), mapping_shape='bangumi_subjects_one_work',
        review_disposition='reviewable', reason_codes=('explicit_bangumi_relation_cohort',),
        evidence_path=(str(plain_batch.get('evidence_fingerprint') or ''),),
        resolution_fingerprint=_canonical_sha256({'assignments': assignments, 'ranges': ranges}),
        subject_observations=plain_batch)
    return ProviderSubjectCohortSegment(selected_subject_id=selected_id,
        segment_subject_ids=ordered_ids, anchor_subject_ids=(), anchor_tmdb_tv_id='',
        anchor_evidence_fingerprints={}, subject_evidence_fingerprints={
            s: str(row_by_subject[s].get('response_fingerprint') or '') for s in ordered_ids},
        working_batch=plain_batch, resolution=resolution)

def resolve_provider_subject_cohort_segment(
    *,
    selected_subject_id: str,
    subject_observations: Mapping[str, object],
    bangumi_subjects: object,
    bangumi_relations: object,
) -> ProviderSubjectCohortSegment:
    """以完整 Bangumi 关系提出本地系列，保留可靠的整季对应建议。"""

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
    if set(row_by_subject) != set(subject_by_id):
        raise ValueError('作品资料与观察范围不一致')
    if any(
        subject_id not in row_by_subject or subject_id not in subject_by_id
        for subject_id in ordered_ids
    ):
        raise ValueError("续作链的作品或 Provider 资料不完整")

    # 未公布集数的尾部续作保留在原始资料中，不参与当前季度范围。
    while ordered_ids:
        last = subject_by_id[ordered_ids[-1]]
        count = last.get('episode_count', last.get('eps'))
        if count is not None and not (type(count) is int and count == 0):
            break
        ordered_ids = ordered_ids[:-1]
    if selected_id not in ordered_ids:
        raise ValueError('所选作品尚缺少可冻结的普通集范围')

    # 数据库整季对应仅提供建议；本地系列按完整关系及用户确认确定。
    resolution = resolve_provider_link(
        selected_provider=dict(namespace='bangumi', entity_type='subject', provider_id=selected_id),
        bangumi_subjects=plain_subjects, bangumi_relations=plain_relations,
        subject_observations=plain_batch)
    if (resolution.review_disposition != 'reviewable'
            or tuple(resolution.work_segment.get('subject_ids', ())) != ordered_ids):
        return _bangumi_cohort_segment(selected_id, ordered_ids, subject_by_id, row_by_subject, plain_batch)
    anchors = tuple(s for s in ordered_ids if _exact_tmdb_candidate(row_by_subject[s]) is not None)
    return ProviderSubjectCohortSegment(selected_subject_id=selected_id,
        segment_subject_ids=ordered_ids, anchor_subject_ids=anchors,
        anchor_tmdb_tv_id=str(resolution.tmdb_tv_id or ''),
        anchor_evidence_fingerprints={s: str(row_by_subject[s]['response_fingerprint']) for s in anchors},
        subject_evidence_fingerprints={s: str(row_by_subject[s]['response_fingerprint']) for s in ordered_ids},
        working_batch=plain_batch, resolution=resolution)

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
            selected_subject_id=selected_subject_id, subject_observations=subject_observations,
            bangumi_subjects=bangumi_subjects, bangumi_relations=bangumi_relations)
    except (ValueError, RuntimeError):
        return ()
    from .provider_link_resolver import SAME_ID_AGGREGATE_SUPPORT
    if (segment.resolution.mapping_shape != 'bangumi_subjects_one_work'
            and SAME_ID_AGGREGATE_SUPPORT not in segment.resolution.reason_codes):
        return ()
    titles = {str(s.get('provider_id') or s.get('id')): str(s.get('title') or s.get('name_cn') or '')
              for s in bangumi_subjects}
    selected = segment.selected_subject_id
    return tuple(dict(from_subject_id=a, to_subject_id=b, relation='sequel',
        neighbour_subject_id=b if a == selected else a,
        neighbour_title=titles[b if a == selected else a],
        evidence_fingerprint=str(subject_observations['evidence_fingerprint']),
        anchor_subject_ids=segment.anchor_subject_ids, anchor_tmdb_tv_id=segment.anchor_tmdb_tv_id,
        anchor_evidence_fingerprints=dict(segment.anchor_evidence_fingerprints))
        for a, b in zip(segment.segment_subject_ids, segment.segment_subject_ids[1:]) if selected in (a, b))

def resolve_provider_subject_boundary_decision(
    failure: Mapping[str, object],
    *,
    same_work: bool,
    boundary_edge: Mapping[str, object],
) -> tuple[ProviderLinkResolution, Optional[dict[str, object]]]:
    """合包写入口已退役，历史数据只供读取。"""
    raise ValueError('合包功能已退役，请按单条目重新整理；原任务保留')

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
        "season_number": (failure['requested_season_number']
            if subject_id == failure['selected_subject_id'] and failure.get('requested_season_number') is not None
            else assignments[subject_id]),
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

def _projection_video_facts(plan, projection):
    numbers = {row["source_episode"] for row in projection.get("episode_mapping", ()) or ()
               if isinstance(row, Mapping)}
    return tuple((entry.source_path, entry.episode_id) for entry in plan.entries
                 if entry.media_kind == "video" and isinstance(entry.episode_id, int)
                 and entry.episode_id in numbers)

def _bind_projection_to_plan_sources(
    plan: RenamePlan,
    projection: Mapping[str, object],
    *,
    observation=None,
) -> dict[str, object]:
    from . import source_episode_binding as binding
    from .content_associations import encode
    videos = _projection_video_facts(plan, projection)
    if observation is None:
        observation = binding.observe_source_episode_identities(tuple(source for source, _ in videos))
    if type(observation) is not binding.SourceEpisodeObservation:
        raise ValueError("逐源集号观察器未返回有效结果")
    result = binding.bind_source_episodes(binding.SourceEpisodeBindingInput(videos, encode(projection), observation))
    if type(result) is not binding.SourceEpisodeBindingResult:
        raise ValueError("逐源集号绑定器未返回有效结果")
    payload = result.to_dict()
    # Keep the legacy Python container as well as its serialized byte schema.
    payload["episode_mapping"] = tuple(payload["episode_mapping"])
    return payload

def apply_source_episode_projection(
    plan: RenamePlan,
    projection: Mapping[str, object],
    *,
    source_only: bool = False,
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
        if source_only and entry.media_kind != 'video':
            entries.append(entry)
            continue
        target_episode = source_targets.get(str(entry.source_path))
        if not source_only and target_episode is None and isinstance(entry.episode_id, int):
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
    entries = list(bind_tv_episode_technical_versions(entries))

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

def tv_filename_resolution(path):
    matches = re.findall(r'(?<!\d)(2160p|1080p|720p|480p)(?!\d)', path.name, flags=re.IGNORECASE)
    return matches[0].lower() if len(matches) == 1 else None

def tv_technical_reference_paths(paths):
    """Collapse only filenames differing in one explicit resolution token."""
    families = {}
    for path in paths:
        label = tv_filename_resolution(path)
        family = (str(path.parent), re.sub(r'(?<!\d)(2160p|1080p|720p|480p)(?!\d)',
            '<resolution>', path.name, flags=re.IGNORECASE).casefold()) if label else ('unique', str(path))
        families.setdefault(family, []).append((path, label))
    representatives, owners = [], {}
    for rows in families.values():
        labels = [label for _, label in rows]
        if len(rows) > 1 and all(labels) and len(labels) == len(set(labels)):
            representative = rows[0][0]
            representatives.append(representative)
            owners.update((path, representative) for path, _ in rows)
        else:
            representatives.extend(path for path, _ in rows)
            owners.update((path, path) for path, _ in rows)
    return tuple(representatives), owners

def bind_tv_episode_technical_versions(entries):
    """Shared exact filename-resolution version binding, never guessed HDR/audio."""
    entries = list(entries)
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
            resolution_rows.append(
                (index, tv_filename_resolution(entries[index].source_path))
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
    return tuple(entries)

def freeze_source_episode_projection(
    plan: RenamePlan,
    resolution: ProviderLinkResolution,
    *,
    selected_subject_id: str,
    source_observation=None,
    source_only: bool = False,
) -> tuple[RenamePlan, Optional[Mapping[str, object]]]:
    """Observe sources once, then freeze unique or bounded pure projections."""
    from .source_episode_binding import SourceEpisodeObservation
    if ((source_observation is not None and type(source_observation) is not SourceEpisodeObservation)
            or source_only and source_observation is None):
        raise ValueError('新TV逐源投影必须消费冻结物理观察，不能回退stat')

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
        observation = (source_observation.subset(tuple(source for source, _ in _projection_video_facts(plan, derived)))
                       if source_observation is not None else None)
        projection = _bind_projection_to_plan_sources(plan, derived, observation=observation)
        return apply_source_episode_projection(plan, projection, source_only=source_only), projection
    from . import source_episode_binding as binding
    numeric_candidates = tuple(candidate for candidate in derived.get("candidates", ()) or ()
                               if isinstance(candidate, Mapping))
    candidates = ()
    if numeric_candidates:
        all_sources = tuple(dict.fromkeys(source for candidate in numeric_candidates
                            for source, _ in _projection_video_facts(plan, candidate)))
        observed = (binding.observe_source_episode_identities(all_sources) if source_observation is None
                    else source_observation.subset(all_sources))
        if type(observed) is not binding.SourceEpisodeObservation:
            raise ValueError('逐源集号观察器未返回有效结果')
        if {row.source for row in observed.rows} != set(all_sources):
            raise ValueError('多候选观察未精确覆盖相关来源')
        candidates = tuple(_bind_projection_to_plan_sources(plan, candidate,
            observation=observed.subset(tuple(source for source, _ in _projection_video_facts(plan, candidate))))
            for candidate in numeric_candidates)
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
    if resolution.subject_observations is not None or resolution.derived_episode_ranges:
        snapshot.update({
            "schema_version": 2,
            "selected_subject_id": str(
                (state.confirmed_work or {}).get('primary_provider', {}).get('provider_id')
                or (resolution.subject_observations or {}).get("selected_subject_id") or ""
            ),
            "subject_observations": dict(resolution.subject_observations or {}),
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
        "mapping_id": resolution.anchor_mapping_id or f"provider-link-{uuid.uuid5(uuid.NAMESPACE_URL, 'bar:provider-link:' + str(snapshot['series_key']) + ':' + str(snapshot['tmdb_tv_id']))}",
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

    from . import series_registration
    if resolution.registration_authority is None:
        resolution, _ = series_registration.bind_resolution(resolution)
    proof = series_registration.SeriesRegistrationAuthority.from_mapping(resolution.registration_authority)
    if proof.series_key != resolution.proposed_series_key:
        raise ValueError('季度关系与系列登记权威不一致')
    staged = (
        stage_provider_link_decision(state, resolution)
        if resolution.tmdb_tv_id
        else replace(state, provider_link_binding=None)
    )
    if (
        not resolution.tmdb_tv_id
        and (
            resolution.review_disposition != "reviewable"
            or resolution.status != "inferred"
            or not resolution.requires_plan_review
            or not resolution.proposed_series_key
            or not resolution.season_assignments
            or not resolution.evidence_path
        )
    ):
        raise ValueError("Bangumi 主作品季度决定不完整")
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
        subject_id in assignments and (season.get('decision_source') == 'explicit_user'
            or assignments[subject_id] == season_number)
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
            "registration_authority": resolution.registration_authority,
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
