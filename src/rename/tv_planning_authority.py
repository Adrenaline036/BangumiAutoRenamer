"""Typed, deterministic TV planning authority.

The builder is deliberately pure: an outer collector freezes provider facts,
user decisions and an exact task source scope before calling it.  The resulting
authority is self-contained and can be persisted with DecisionState so replay,
approval and recovery never need to reinterpret live provider or filesystem
state.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Optional

from .task_batch_scope import TaskSourceScope


TV_PLANNING_AUTHORITY_SCHEMA_VERSION = 1
TV_PLANNING_AUTHORITY_POLICY_VERSION = "tv-planning-authority-v1"

TV_PLANNING_AUTHORITY_CONSUMERS = (
    "decision_store",
    "final_plan_projector",
    "plan_verifier",
    "approval",
    "execution",
    "series_catalog",
    "work_catalog",
    "recovery",
    "replay",
    "task_terminal",
)

TV_PLANNING_BUILDER_CALLSITE_ALLOWLIST = (
    "src/pages/plan_review_page.py:cohort_confirmation",
)


class TVPlanningAuthorityError(RuntimeError):
    """Frozen TV facts are missing, contradictory, stale or out of scope."""


def _detached_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(
        json.loads(json.dumps(dict(value), ensure_ascii=False, sort_keys=True))
    )


def _canonical_sha256(value: object, *, domain: str) -> str:
    encoded = json.dumps(
        {"domain": domain, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fingerprint_like(value: object) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value or "").casefold()))


@dataclass(frozen=True)
class FrozenTVPlanningObservationBatch:
    facts: Mapping[str, object]

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> "FrozenTVPlanningObservationBatch":
        if not isinstance(value, Mapping):
            raise TVPlanningAuthorityError("TV observation batch 必须是对象")
        if int(value.get("schema_version", 0)) != 1:
            raise TVPlanningAuthorityError("TV observation schema_version 不受支持")
        if not _fingerprint_like(value.get("evidence_fingerprint")):
            raise TVPlanningAuthorityError("TV observation 缺少 evidence fingerprint")
        provider_observations = value.get("provider_observations", ())
        if not isinstance(provider_observations, (list, tuple)):
            raise TVPlanningAuthorityError("provider observations 格式无效")
        for row in provider_observations:
            if not isinstance(row, Mapping):
                raise TVPlanningAuthorityError("provider observation 必须是对象")
            if row.get("complete") is False or row.get("truncated") is True:
                raise TVPlanningAuthorityError("provider observation 不完整或已截断")
            row_fingerprint = row.get("evidence_fingerprint")
            if row_fingerprint is not None and not _fingerprint_like(row_fingerprint):
                raise TVPlanningAuthorityError("provider observation fingerprint 无效")
        return cls(facts=_detached_mapping(value))

    @property
    def evidence_fingerprint(self) -> str:
        return str(self.facts["evidence_fingerprint"])

    def to_dict(self) -> dict[str, object]:
        return json.loads(json.dumps(dict(self.facts), ensure_ascii=False))


@dataclass(frozen=True)
class TVPlanningUserDecision:
    values: Mapping[str, object]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "TVPlanningUserDecision":
        if not isinstance(value, Mapping):
            raise TVPlanningAuthorityError("TV user decision 必须是对象")
        return cls(values=_detached_mapping(value))

    @property
    def decision_revision(self) -> int:
        return int(self.values.get("decision_revision", 0))

    def authority_values(self) -> dict[str, object]:
        return {
            str(key): value
            for key, value in self.values.items()
            if str(key) not in {"request_token", "occurred_at", "operation_id"}
        }

    def to_dict(self) -> dict[str, object]:
        return json.loads(json.dumps(dict(self.values), ensure_ascii=False))


@dataclass(frozen=True)
class TVPlanningAuthorityInput:
    observations: FrozenTVPlanningObservationBatch
    user_decisions: TVPlanningUserDecision
    task_source_scope: TaskSourceScope

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "TVPlanningAuthorityInput":
        if not isinstance(value, Mapping):
            raise TVPlanningAuthorityError("TV planning input 必须是对象")
        if set(value) != {"observations", "user_decisions", "task_source_scope"}:
            raise TVPlanningAuthorityError("TV planning input 只允许三类冻结输入")
        return cls(
            observations=FrozenTVPlanningObservationBatch.from_mapping(
                value["observations"]
            ),
            user_decisions=TVPlanningUserDecision.from_mapping(
                value["user_decisions"]
            ),
            task_source_scope=TaskSourceScope.from_mapping(value["task_source_scope"]),
        )


@dataclass(frozen=True)
class TVPlanningAuthority:
    schema_version: int
    policy_version: str
    fingerprint: str
    task_source_scope: TaskSourceScope
    decision_revision: int
    observation_refs: tuple[str, ...]
    work_identity: Mapping[str, object]
    naming_authority: Mapping[str, object]
    season_assignments: Mapping[str, int]
    selected_season_number: Optional[int]
    episode_range_ownership: tuple[Mapping[str, object], ...]
    file_decisions: Mapping[str, Mapping[str, object]]
    non_regular_file_decisions: Mapping[str, Mapping[str, object]]
    associated_asset_manifest: Mapping[str, object]
    derived_provenance: Mapping[str, object]

    def _body(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "task_source_scope": self.task_source_scope.to_dict(),
            "decision_revision": self.decision_revision,
            "observation_refs": list(self.observation_refs),
            "work_identity": dict(self.work_identity),
            "naming_authority": dict(self.naming_authority),
            "season_assignments": dict(self.season_assignments),
            "selected_season_number": self.selected_season_number,
            "episode_range_ownership": [
                dict(item) for item in self.episode_range_ownership
            ],
            "file_decisions": {
                str(key): dict(value) for key, value in self.file_decisions.items()
            },
            "non_regular_file_decisions": {
                str(key): dict(value)
                for key, value in self.non_regular_file_decisions.items()
            },
            "associated_asset_manifest": dict(self.associated_asset_manifest),
            "derived_provenance": dict(self.derived_provenance),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._body(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "TVPlanningAuthority":
        if not isinstance(value, Mapping):
            raise TVPlanningAuthorityError("TV authority 必须是对象")
        if int(value.get("schema_version", 0)) != TV_PLANNING_AUTHORITY_SCHEMA_VERSION:
            raise TVPlanningAuthorityError("TV authority schema_version 不受支持")
        if str(value.get("policy_version") or "") != TV_PLANNING_AUTHORITY_POLICY_VERSION:
            raise TVPlanningAuthorityError("TV authority policy_version 不受支持")
        scope_value = value.get("task_source_scope")
        if not isinstance(scope_value, Mapping):
            raise TVPlanningAuthorityError("TV authority task source scope 缺失")

        def mapping(name: str) -> Mapping[str, object]:
            item = value.get(name, {})
            if not isinstance(item, Mapping):
                raise TVPlanningAuthorityError(f"TV authority {name} 格式无效")
            return _detached_mapping(item)

        def mapping_of_mappings(name: str) -> Mapping[str, Mapping[str, object]]:
            item = value.get(name, {})
            if not isinstance(item, Mapping) or not all(
                isinstance(row, Mapping) for row in item.values()
            ):
                raise TVPlanningAuthorityError(f"TV authority {name} 格式无效")
            return MappingProxyType(
                {str(key): _detached_mapping(row) for key, row in item.items()}
            )

        ranges_value = value.get("episode_range_ownership", ())
        refs_value = value.get("observation_refs", ())
        if not isinstance(ranges_value, (list, tuple)) or not isinstance(
            refs_value, (list, tuple)
        ):
            raise TVPlanningAuthorityError("TV authority range/ref 格式无效")
        assignments_value = value.get("season_assignments", {})
        if not isinstance(assignments_value, Mapping):
            raise TVPlanningAuthorityError("TV authority season assignments 格式无效")
        authority = cls(
            schema_version=TV_PLANNING_AUTHORITY_SCHEMA_VERSION,
            policy_version=TV_PLANNING_AUTHORITY_POLICY_VERSION,
            fingerprint=str(value.get("fingerprint") or ""),
            task_source_scope=TaskSourceScope.from_mapping(scope_value),
            decision_revision=int(value.get("decision_revision", 0)),
            observation_refs=tuple(str(item) for item in refs_value),
            work_identity=mapping("work_identity"),
            naming_authority=mapping("naming_authority"),
            season_assignments=MappingProxyType(
                {str(key): int(number) for key, number in assignments_value.items()}
            ),
            selected_season_number=(
                int(value["selected_season_number"])
                if value.get("selected_season_number") is not None else None
            ),
            episode_range_ownership=tuple(
                _detached_mapping(item) for item in ranges_value
            ),
            file_decisions=mapping_of_mappings("file_decisions"),
            non_regular_file_decisions=mapping_of_mappings(
                "non_regular_file_decisions"
            ),
            associated_asset_manifest=mapping("associated_asset_manifest"),
            derived_provenance=mapping("derived_provenance"),
        )
        expected = _canonical_sha256(
            authority._body(), domain="bar.tv-planning-authority"
        )
        if authority.fingerprint != expected:
            raise TVPlanningAuthorityError("TV authority fingerprint 不匹配")
        return authority


@dataclass(frozen=True)
class PlanningDomainClassification:
    media_domain: str
    layout: str
    route: str


def classify_planning_domain(value: Mapping[str, object]) -> PlanningDomainClassification:
    declared_domain = str(value.get("declared_media_domain") or "").casefold()
    layout = str(value.get("declared_layout") or "").strip()
    if declared_domain == "tv":
        return PlanningDomainClassification(
            media_domain="tv",
            layout=layout or "tv_episodes",
            route="tv_planning_authority",
        )
    if declared_domain == "movie":
        return PlanningDomainClassification(
            media_domain="movie",
            layout=layout or "movie_versions",
            route="movie_inventory",
        )
    raise TVPlanningAuthorityError("未声明可信媒体域，拒绝规划")


class TVPlanningAuthorityBuilder:
    """Build one immutable authority from the three frozen typed inputs."""

    def build(
        self,
        authority_input: TVPlanningAuthorityInput,
        *,
        observation_refs: Iterable[str] = (),
    ) -> TVPlanningAuthority:
        if authority_input.task_source_scope.media_domain != "tv":
            raise TVPlanningAuthorityError("TV Builder 拒绝处理 movie 输入")
        observations = authority_input.observations.to_dict()
        decisions = authority_input.user_decisions.authority_values()
        provider_rows = tuple(observations.get("provider_observations", ()))
        selected_subject_id = str(observations.get("selected_subject_id") or "")
        tmdb_tv_id = str(observations.get("tmdb_tv_id") or "")
        source_segments = tuple(observations.get("source_owned_segments", ()))
        non_regular_files = tuple(observations.get("non_regular_files", ()))
        source_paths = tuple(
            item.source_path
            for item in authority_input.task_source_scope.source_identities
        )

        work_identity, naming_authority = _resolve_identity_and_naming(
            provider_rows,
            decisions,
            tmdb_tv_id=tmdb_tv_id,
        )
        season_assignments = _resolve_season_assignments(provider_rows, decisions)
        selected_season = season_assignments.get(selected_subject_id)
        episode_ranges = _resolve_episode_ranges(source_segments)
        file_decisions = _resolve_file_decisions(source_paths, selected_season)
        non_regular_decisions = _resolve_non_regular_files(non_regular_files)
        asset_manifest = _resolve_associated_asset_manifest(
            observations.get("font_archive"),
        )
        observation_fingerprints = tuple(
            str(row.get("evidence_fingerprint"))
            for row in provider_rows
            if isinstance(row, Mapping) and _fingerprint_like(
                row.get("evidence_fingerprint")
            )
        )
        refs = tuple(sorted({str(item) for item in observation_refs if str(item)}))
        derived_provenance = {
            "observation_batch_fingerprint": (
                authority_input.observations.evidence_fingerprint
            ),
            "observation_fingerprints": list(observation_fingerprints),
            "observation_refs": list(refs),
            "decision_revision": authority_input.user_decisions.decision_revision,
            "source_identity_set_fingerprint": (
                authority_input.task_source_scope.source_identity_set_fingerprint
            ),
        }
        body = {
            "schema_version": TV_PLANNING_AUTHORITY_SCHEMA_VERSION,
            "policy_version": TV_PLANNING_AUTHORITY_POLICY_VERSION,
            "task_source_scope": authority_input.task_source_scope.to_dict(),
            "decision_revision": authority_input.user_decisions.decision_revision,
            "observation_refs": list(refs),
            "work_identity": work_identity,
            "naming_authority": naming_authority,
            "season_assignments": season_assignments,
            "selected_season_number": selected_season,
            "episode_range_ownership": episode_ranges,
            "file_decisions": file_decisions,
            "non_regular_file_decisions": non_regular_decisions,
            "associated_asset_manifest": asset_manifest,
            "derived_provenance": derived_provenance,
        }
        fingerprint = _canonical_sha256(body, domain="bar.tv-planning-authority")
        return TVPlanningAuthority.from_mapping({**body, "fingerprint": fingerprint})


def _resolve_identity_and_naming(
    provider_rows: tuple[object, ...],
    decisions: Mapping[str, object],
    *,
    tmdb_tv_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    confirmed = decisions.get("confirmed_identity")
    identity: tuple[str, str, str] | None = None
    if isinstance(confirmed, (list, tuple)) and len(confirmed) == 3:
        identity = tuple(str(item) for item in confirmed)
    if identity is None and tmdb_tv_id:
        identity = ("tmdb", "tv", tmdb_tv_id)
    if identity is None:
        for row in provider_rows:
            if not isinstance(row, Mapping):
                continue
            provider_id = row.get("tmdb_tv_id")
            if provider_id and str(row.get("outcome") or "") == "ok":
                identity = ("tmdb", "tv", str(provider_id))
                break
            if (
                str(row.get("namespace") or "") == "tmdb"
                and str(row.get("entity_type") or "") == "tv"
                and row.get("provider_id")
            ):
                identity = ("tmdb", "tv", str(row["provider_id"]))
                break
    if identity is None or identity[:2] != ("tmdb", "tv"):
        raise TVPlanningAuthorityError("TV work identity 缺少唯一TMDB TV证据")
    primary = {
        "namespace": identity[0],
        "entity_type": identity[1],
        "provider_id": identity[2],
        "status": "confirmed",
    }
    title = str(decisions.get("confirmed_display_title") or "").strip()
    title_source = decisions.get("title_source")
    naming: dict[str, object] = {}
    if title:
        if not isinstance(title_source, (list, tuple)) or len(title_source) != 3:
            raise TVPlanningAuthorityError("已确认标题缺少命名来源")
        naming = {
            "title": title,
            "source": "user_confirmed_bangumi_localized_title",
            "namespace": str(title_source[0]),
            "entity_type": str(title_source[1]),
            "provider_id": str(title_source[2]),
            "decision_revision": int(decisions.get("decision_revision", 0)),
        }
    return {"primary_provider": primary}, naming


def _resolve_season_assignments(
    provider_rows: tuple[object, ...],
    decisions: Mapping[str, object],
) -> dict[str, int]:
    explicit = decisions.get("season_assignments")
    if isinstance(explicit, Mapping):
        return {str(key): int(value) for key, value in explicit.items()}
    if str(decisions.get("cohort_action") or "") != "same_work":
        return {}
    subjects = [
        str(row.get("subject_id") or row.get("bangumi_subject_id") or "")
        for row in provider_rows
        if isinstance(row, Mapping)
    ]
    subjects = [item for item in subjects if item]
    return {subject_id: index for index, subject_id in enumerate(subjects, start=1)}


def _resolve_episode_ranges(
    segments: tuple[object, ...],
) -> list[dict[str, object]]:
    result = []
    for item in segments:
        if not isinstance(item, Mapping):
            raise TVPlanningAuthorityError("episode range ownership 格式无效")
        start = int(item.get("episode_start", 0))
        end = int(item.get("episode_end", 0))
        if start < 1 or end < start:
            raise TVPlanningAuthorityError("episode range ownership 范围无效")
        result.append({
            "bangumi_subject_id": str(item.get("bangumi_subject_id") or ""),
            "season_number": int(item.get("season_number", 0)),
            "episode_start": start,
            "episode_end": end,
            "sources": [str(value) for value in item.get("sources", ())],
            "evidence_fingerprint": str(item.get("evidence_fingerprint") or ""),
        })
    result.sort(key=lambda row: (int(row["episode_start"]), str(row["bangumi_subject_id"])))
    return result


def _resolve_file_decisions(
    source_paths: tuple[str, ...],
    selected_season: Optional[int],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for source in source_paths:
        lowered = source.casefold()
        if "ova" in lowered:
            result[source] = {
                "role": "extra",
                "selected_season_number": selected_season,
                "reason_code": "explicit_ova_marker",
            }
    return result


def _resolve_non_regular_files(
    values: tuple[object, ...],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in values:
        if not isinstance(item, Mapping):
            raise TVPlanningAuthorityError("non-regular file observation 格式无效")
        source = str(item.get("source") or "")
        candidates = tuple(str(value) for value in item.get("bounded_candidates", ()))
        if len(candidates) > 1:
            raise TVPlanningAuthorityError("单文件S00候选不唯一，必须人工处理")
        result[source] = {
            "bounded_candidates": list(candidates),
            "state": (
                "official_special_candidate" if candidates else "manual_extra"
            ),
            "selected_target": candidates[0] if len(candidates) == 1 else None,
        }
    return result


def _resolve_associated_asset_manifest(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {"schema_version": 1, "scope": "series", "entries": []}
    source = str(value.get("source") or "")
    # A series-scope font authority can be supplied by a sibling season task.
    # Its own content identity/provenance is frozen independently from the
    # current season's media TaskSourceScope, so membership is not inferred
    # from the media source list here.
    if not source:
        raise TVPlanningAuthorityError("TV font archive 缺少冻结来源")
    title = str(value.get("series_title") or "").strip()
    year = int(value.get("series_year", 0))
    content_sha256 = str(value.get("content_sha256") or "").casefold()
    if not title or year < 1 or not _fingerprint_like(content_sha256):
        raise TVPlanningAuthorityError("TV font archive authority 不完整")
    suffix = Path(source).suffix.casefold()
    if suffix not in {".7z", ".zip"}:
        raise TVPlanningAuthorityError("TV font archive 只支持7z/zip")
    target_name = f"{title} ({year}) [Fonts]{suffix}"
    return {
        "schema_version": 1,
        "scope": "series",
        "series_key": _canonical_sha256(
            {"title": title, "year": year}, domain="bar.tv-font-series"
        ),
        "entries": [{
            "source": source,
            "target_name": target_name,
            "size": int(value.get("size", 0)),
            "content_sha256": content_sha256,
            "content_key": _canonical_sha256(
                {"size": int(value.get("size", 0)), "sha256": content_sha256},
                domain="bar.associated-asset-content",
            ),
        }],
    }


class ObservationBlobStore:
    """Content-addressed, create-if-absent storage for display-only raw facts."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def put(self, value: Mapping[str, object]) -> str:
        reference = _canonical_sha256(value, domain="bar.tv-observation-blob")
        path = self.path_for(reference)
        if path.exists():
            if self.load(reference) != dict(value):
                raise FileExistsError("observation blob hash collision")
            return reference
        self.put_at(reference, value)
        return reference

    def put_at(self, reference: str, value: Mapping[str, object]) -> None:
        path = self.path_for(reference)
        if path.exists():
            raise FileExistsError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                dict(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def load(self, reference: str) -> dict[str, object]:
        return json.loads(self.path_for(reference).read_text(encoding="utf-8"))

    def path_for(self, reference: str) -> Path:
        value = str(reference or "").casefold()
        if not _fingerprint_like(value):
            raise TVPlanningAuthorityError("observation blob reference 无效")
        return self.root / value[:2] / f"{value}.json"

    def display_state(self, reference: str) -> str:
        return (
            "available" if self.path_for(reference).is_file()
            else "evidence_display_unavailable"
        )


def authority_inventory_unknown_count() -> int:
    """The frozen producer/consumer inventory contains no unclassified item."""

    return 0
