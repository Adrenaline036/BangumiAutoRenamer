import re
import os
import json
import uuid
import hashlib
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from difflib import SequenceMatcher
from typing import Dict, List, Mapping, Tuple, Union, Optional

from jikanpy import Jikan

from .trans import is_dry_run_enabled
from .provider_operation import provider_operation
from .plan_store import PlanStore, PlanStoreError
from .decision_state import (
    DecisionStateError,
    DecisionStateStore,
)
from .plan_verifier import PlanVerifier
from .recognition_observation import adapt_new_recognition_observation
from .task_batch_scope import (
    TaskBatchScopeError,
    TaskSourceScope,
)
from .rename_plan import (
    AssetRole,
    ExecutionPolicy,
    PlanStatus,
    PresentationAssetPlanContract,
    ProviderIdentity,
    RenamePlan,
    SeriesScopeIdentity,
    WorkIdentity,
)
from .projection_authority import (
    ProjectionAuthorityError,
    RecognitionDecisionInput,
    TaskEpisodeOwnership,
    TaskTerminalMutation,
    commit_recognition_projection,
)
from .tv_planning_authority import (
    FrozenTVPlanningObservationBatch,
    TVPlanningAuthority,
    TVPlanningAuthorityBuilder,
    TVPlanningAuthorityInput,
    TVPlanningUserDecision,
)
from .presentation import (
    associate_resolved_extra_subtitles,
    build_default_presentation_contracts,
    default_presentation_group_id,
    provider_identity_from_metadata,
)
from .subtitle_workflow import (
    VideoPathMapping,
    build_subtitle_decisions,
    decision_to_plan_contract,
    format_subtitle_target_fields,
)
from ..logger import logger
from .get_info import Search, TMDBRequestError, TMDBResponseError
from .tmdb_genre import classify_tmdb_animation_evidence
from .provider_search import (
    BangumiProviderClient,
    CandidateDecisionStatus,
    ProviderSearchBatch,
    ProviderSearchCoordinator,
    ProviderSearchError,
    ProviderSearchStatus,
    ProviderWorkerOutput,
    SOURCE_BANGUMI,
    SOURCE_TMDB_MOVIE,
    SOURCE_TMDB_TV,
    build_search_policy,
    fuse_provider_candidates,
)
from .provider_link_catalog import ProviderLinkCatalog, ProviderLinkCatalogError
from .provider_link_decision import (
    freeze_source_episode_projection,
    stage_provider_link_series_decision,
)
from .provider_link_resolver import (
    ProviderLinkResolution,
)
from .review_operation import ProviderSelectionAwaitingUserOutcome
from .ova_selection import OvaSubject, OvaSeasonPending
from . import provider_special_evidence as provider_special
from .series_catalog import SeriesCatalog, SeriesCatalogError
from .asset_collision import (
    AssetCollisionError,
    build_reuse_resolution,
)

from ..utils.path import (
    DECISION_STATE_PATH,
    PLAN_PATH,
    PROVIDER_LINK_CATALOG_PATH,
    RECORD_PATH,
    SERIES_CATALOG_PATH,
    TASK_PATH,
)
from ..config.config_manager import cm
from .utils import (
    JELLYFIN_EXTRAS_DIR,
    VIDEO_SUFFIX,
    canonical_extras_target,
    jellyfin_season_dir,
)
from .media_classifier import (
    LocalRole,
    PreclassificationKind,
    classify_media,
    preclassify_media,
)
from .movie_assets import (
    DISC_STRUCTURE_REQUIRES_MANUAL_WORKFLOW,
    MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION,
    MovieAssetInventory,
    MovieAssetInventoryError,
    inventory_movie_assets,
)
from .inventory_plan import build_inventory_review_draft
from .provider_selection_authority import (
    partition_provider_candidates_by_presentation,
    provider_candidate_group_kind,
    provider_selection_capability,
    task_library_domain_from_flag,
    task_media_kind_from_flag,
)
from ..utils.utils import merge_task_at, read_task_at, safe_task_path
from .target_routing import TargetRootSnapshot, TargetRoutingError
from .canonical_naming import (
    CanonicalNamingDecision,
    CanonicalNamingError,
    build_canonical_work_root,
    resolve_fresh_collision_root,
    resolve_title_authority,
    select_bangumi_work_title_owner,
)
from .episode_inference import (
    EpisodeInferenceError,
    build_episode_hints,
    build_semantic_context,
    build_secondary_sequence_hints,
    normalize_target_stem,
    validate_episode_mapping,
)
from .cleaner import (
    remove_tag,
    to_sim_max,
    extract_season,
    SearchTitleCandidate,
    parse_release_title,
    remove_similar_part,
)

jikan = Jikan()

from .subtitle_labels import SUBTITLE_SUFFIXES


def _tv_observation_fingerprint(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        {"domain": "bar.tv-planning-observation", "value": dict(value)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _project_provider_episode_ownership(
    plan: RenamePlan,
    resolution: ProviderLinkResolution,
    work_identity: WorkIdentity,
    *,
    selected_subject_id: str,
) -> RenamePlan:
    ranges = tuple(
        row for row in resolution.derived_episode_ranges
        if isinstance(row, Mapping)
    )
    selected_ranges = tuple(row for row in ranges
        if str(row.get("bangumi_subject_id") or "") == selected_subject_id)
    if not selected_ranges:
        return plan
    if len(selected_ranges) != 1:
        raise ValueError("所选作品没有唯一冻结的普通分集范围")
    season_number = int(selected_ranges[0].get("season_number", 0))
    same_season = selected_ranges
    assignments = dict(resolution.season_assignments)
    for row in same_season:
        if (assignments.get(str(row.get("bangumi_subject_id") or "")) != season_number
                or type(row.get("episode_start")) is not int
                or type(row.get("episode_end")) is not int
                or row["episode_start"] < 1 or row["episode_end"] < row["episode_start"]):
            raise ValueError("普通分集范围与冻结季度关系不一致")
    primary = work_identity.primary_provider
    work_identity_key = ":".join((
        str(primary.namespace or ""),
        str(primary.entity_type or ""),
        str(primary.provider_id or ""),
    ))
    work_identity_fingerprint = hashlib.sha256(
        work_identity_key.encode("utf-8")
    ).hexdigest()
    rows = []
    for entry in plan.entries:
        if (
            entry.media_kind != "video"
            or not isinstance(entry.episode_id, int)
            or isinstance(entry.episode_id, bool)
            or entry.season_id != season_number
        ):
            continue
        matches = tuple(row for row in same_season
            if row["episode_start"] <= entry.episode_id <= row["episode_end"])
        if not matches:
            # An overflow has no authority. Keep the existing verifier's
            # explicit unresolved-special path, never assign the selected owner.
            continue
        if len(matches) != 1:
            raise ValueError("普通分集没有唯一冻结的来源归属")
        owned_range = matches[0]
        subject_id = str(owned_range["bangumi_subject_id"])
        start, end = owned_range["episode_start"], owned_range["episode_end"]
        rows.append(TaskEpisodeOwnership(
            source_path=entry.source_path,
            bangumi_subject_id=subject_id,
            work_identity_key=work_identity_key,
            work_identity_fingerprint=work_identity_fingerprint,
            season_number=season_number,
            source_episode=int(entry.episode_id),
            target_episode=int(entry.episode_id),
            range_authority_id=(
                f"subject:{subject_id}:season:{season_number}:"
                f"{start}-{end}"
            ),
            evidence_fingerprint=str(
                owned_range.get("evidence_fingerprint")
                or resolution.resolution_fingerprint
                or ""
            ),
            decision_source="provider_subject_resolution",
        ))
    metadata = dict(plan.metadata)
    metadata["task_episode_ownership"] = [row.to_dict() for row in rows]
    return replace(plan, metadata=metadata)


def _planning_provider_identity(*, info, name, is_movie, selection, decision_source, evidence):
    """One shared Provider identity adapter for early TV and legacy observations."""
    provider_date = str((info.get('release_date') if is_movie else info.get('first_air_date')) or '')
    year = int(provider_date[:4]) if len(provider_date) >= 4 and provider_date[:4].isdigit() else None
    marker = info.get('_bar_primary_provider')
    identity = provider_identity_from_metadata(
        namespace=str(marker.get('namespace') or '') if isinstance(marker, Mapping) else 'tmdb',
        entity_type=str(marker.get('entity_type') or '') if isinstance(marker, Mapping) else ('movie' if is_movie else 'tv'),
        provider_id=marker.get('provider_id') if isinstance(marker, Mapping) else info.get('id'),
        title=name, year=year, decision_source=decision_source, evidence=evidence,
        original_title=selection.get('original_title') if selection else info.get('original_title') or info.get('original_name'),
        poster_url=selection.get('poster_url') if selection else None,
        source_url=selection.get('source_url') if selection else None,
        snapshot_at=selection.get('snapshot_at') if selection else None)
    return identity, year


def _build_process_tv_planning_authority(
    *,
    state,
    plan: RenamePlan,
    resolution: ProviderLinkResolution,
    task_source_scope: TaskSourceScope,
) -> TVPlanningAuthority:
    owned = _project_provider_episode_ownership(plan, resolution,
        WorkIdentity.from_dict(state.confirmed_work),
        selected_subject_id=str(state.confirmed_work.get('primary_provider', {}).get('provider_id') or ''))
    observed_paths = tuple((row['source_path'], str(Path(row['source_path']).resolve(strict=False)))
                          for row in owned.metadata.get('task_episode_ownership', ()))
    return TVPlanningAuthorityBuilder().build(build_process_tv_planning_input(
        state=state, plan=plan, resolution=resolution, task_source_scope=task_source_scope,
        observed_paths=observed_paths))


def build_process_tv_planning_input(*, state, plan, resolution, task_source_scope,
                                    observed_paths):
    """Pure shared TV input builder; path observations are explicit inputs."""
    if type(observed_paths) is not tuple:
        raise ValueError('TV规划缺少冻结来源路径观察')
    paths = dict(observed_paths)
    subject_batch = resolution.subject_observations
    materialized = None
    if subject_batch is None:
        # L0 intentionally has no fresh search batch. The complete TV command
        # rebinds this authority from its frozen, leased catalog namespace.
        from .series_registration import SeriesRegistrationAuthority
        authority = SeriesRegistrationAuthority.from_mapping(resolution.registration_authority)
        registered = json.loads(authority.materialized_mapping) if authority.materialized_mapping else {}
        link = registered.get('provider_link', {})
        if (resolution.automation_level != 'L0' or resolution.status != 'materialized_confirmed'
                or not registered or not link
                or link.get('mapping_id') != resolution.anchor_mapping_id
                or link.get('revision') != resolution.anchor_revision
                or authority.namespace != 'bangumi'
                or dict(authority.season_membership) != dict(resolution.season_assignments)
                or authority.series_key != resolution.proposed_series_key):
            raise ValueError('TV登记复用缺少当前完整系列与跨站锚点证据')
        materialized = authority.to_dict()
    elif not isinstance(subject_batch, Mapping):
        raise ValueError("TV 规划缺少冻结的 Bangumi/TMDB 观察")
    provider_rows = []
    for item in (subject_batch or {}).get("subject_observations", ()) or ():
        if not isinstance(item, Mapping):
            raise ValueError("TV 规划 Provider 观察格式无效")
        queries = tuple(
            row for row in item.get("queries", ()) or ()
            if isinstance(row, Mapping)
        )
        truncated = any(bool(row.get("truncated")) for row in queries)
        provider_rows.append({
            **dict(item),
            "complete": not truncated,
            "truncated": truncated,
            "evidence_fingerprint": str(
                item.get("response_fingerprint") or ""
            ),
        })
    selected_subject_id = str(
        state.confirmed_work.get('primary_provider', {}).get('provider_id') or ""
    )
    # Use the same exact per-source ownership projection as the Plan. A full
    # same-season package may contain several Bangumi subjects (e.g. 86 cours).
    owned_plan = _project_provider_episode_ownership(plan, resolution,
        WorkIdentity.from_dict(state.confirmed_work), selected_subject_id=selected_subject_id)
    sources_by_range = {}
    for owner in owned_plan.metadata.get("task_episode_ownership", ()):
        if owner['source_path'] not in paths:
            raise ValueError('TV规划来源路径观察缺项')
        sources_by_range.setdefault(owner["range_authority_id"], []).append(
            paths[owner['source_path']])
    source_segments = []
    for item in resolution.derived_episode_ranges:
        row = dict(item)
        range_id = (f"subject:{row.get('bangumi_subject_id')}:season:{row.get('season_number')}:"
                    f"{row.get('episode_start')}-{row.get('episode_end')}")
        row["sources"] = sources_by_range.get(range_id, [])
        row["evidence_fingerprint"] = str(
            resolution.resolution_fingerprint or ""
        )
        source_segments.append(row)
    observation_body: dict[str, object] = {
        "schema_version": 1,
        "provider_observations": provider_rows,
        "selected_subject_id": selected_subject_id,
        "tmdb_tv_id": resolution.tmdb_tv_id,
        "source_owned_segments": source_segments,
        "non_regular_files": [],
    }
    if materialized is not None:
        observation_body['materialized_registration'] = materialized
    observation_body["evidence_fingerprint"] = _tv_observation_fingerprint(
        observation_body
    )
    primary = state.confirmed_work.get("primary_provider", {})
    decisions = TVPlanningUserDecision.from_mapping({
        "decision_revision": int(state.revision) + 1,
        "confirmed_identity": tuple(
            str(primary.get(field) or "")
            for field in ("namespace", "entity_type", "provider_id")
        ),
        "confirmed_display_title": str(
            state.confirmed_work.get("title") or ""
        ),
        "title_source": tuple(
            str(primary.get(field) or "")
            for field in ("namespace", "entity_type", "provider_id")
        ),
        "metadata_provider_identities": [
            dict(item) for item in state.linked_provider_identities
        ],
        "season_assignments": dict(resolution.season_assignments),
        "cohort_action": (
            "standalone_work"
            if len(resolution.season_assignments) == 1 else "same_work"
        ),
    })
    return TVPlanningAuthorityInput(
        observations=FrozenTVPlanningObservationBatch.from_mapping(
            observation_body
        ),
        user_decisions=decisions,
        task_source_scope=task_source_scope,
    )


def _normalized_suffix(path: Path) -> str:
    name = unicodedata.normalize('NFKC', path.name)
    dot = name.rfind('.')
    return name[dot:].casefold() if dot >= 0 else ''


@dataclass(frozen=True)
class _ProviderCandidateMatch:
    candidate: SearchTitleCandidate
    name: str
    info: Dict
    score: float
    reason: str


def _normalize_title_for_score(value: str) -> str:
    normalized = unicodedata.normalize('NFKC', value).casefold()
    return ' '.join(
        ''.join(char if char.isalnum() else ' ' for char in normalized).split()
    )


def _provider_year(info: Dict, media_kind: str) -> int:
    field = 'first_air_date' if media_kind == '电视剧' else 'release_date'
    date_value = str(info.get(field) or '')
    match = re.match(r'(\d{4})', date_value)
    return int(match.group(1)) if match else 0


def _same_title_alternative_years(
    info: Dict,
    media_kind: str = '电视剧',
) -> Tuple[int, ...]:
    """Return distinct years for provider results sharing the selected title."""

    alternatives = info.get('_bar_search_alternatives') or []
    selected_id = info.get('id')
    selected = next(
        (item for item in alternatives if item.get('id') == selected_id),
        None,
    )
    selected_names = {
        _normalize_title_for_score(str(value or ''))
        for value in (
            (selected or {}).get('name'),
            (selected or {}).get('title'),
            (selected or {}).get('original_name'),
            (selected or {}).get('original_title'),
            info.get('_bar_search_name'),
            info.get('_bar_search_original_name'),
            info.get('name'),
            info.get('title'),
            info.get('original_name'),
            info.get('original_title'),
        )
        if value
    }
    selected_names.discard('')
    if not selected_names:
        return ()

    years = set()
    matching_ids = set()
    for item in alternatives:
        names = {
            _normalize_title_for_score(str(item.get(field) or ''))
            for field in ('name', 'title', 'original_name', 'original_title')
            if item.get(field)
        }
        if not selected_names.intersection(names):
            continue
        date_field = 'first_air_date' if media_kind == '电视剧' else 'release_date'
        date_value = str(item.get(date_field) or '')
        match = re.match(r'(\d{4})', date_value)
        if match:
            years.add(int(match.group(1)))
        if item.get('id') is not None:
            matching_ids.add(item.get('id'))
    if len(matching_ids) < 2 or len(years) < 2:
        return ()
    return tuple(sorted(years))


def _provider_candidate_options(
    info: Dict,
    *,
    entity_type: str,
    selected_title: str,
    limit: int = 6,
) -> List[Dict[str, object]]:
    """Keep a small, explainable set of same-name provider choices for GUI review."""

    namespace = 'tmdb'
    title_field = 'title' if entity_type == 'movie' else 'name'
    original_field = 'original_title' if entity_type == 'movie' else 'original_name'
    date_field = 'release_date' if entity_type == 'movie' else 'first_air_date'
    selected_normalized = _normalize_title_for_score(selected_title)
    raw_items = list(info.get('_bar_search_alternatives') or [])
    if not any(str(item.get('id')) == str(info.get('id')) for item in raw_items):
        raw_items.insert(0, {
            'id': info.get('id'),
            title_field: selected_title or info.get(title_field),
            original_field: info.get(original_field),
            date_field: info.get(date_field),
            'poster_path': info.get('poster_path'),
        })
    choices: List[Dict[str, object]] = []
    seen_ids = set()
    for item in raw_items:
        provider_id = str(item.get('id') or '').strip()
        if not provider_id or provider_id in seen_ids:
            continue
        item_title = str(item.get(title_field) or item.get(original_field) or '')
        normalized = _normalize_title_for_score(item_title)
        if selected_normalized and normalized:
            similarity = SequenceMatcher(None, selected_normalized, normalized).ratio()
            if similarity < 0.72:
                continue
        date_value = str(item.get(date_field) or '')
        poster_path = str(item.get('poster_path') or '')
        choices.append({
            'namespace': namespace,
            'entity_type': entity_type,
            'provider_id': provider_id,
            'title': item_title or selected_title,
            'original_title': str(item.get(original_field) or ''),
            'year': int(date_value[:4]) if len(date_value) >= 4 and date_value[:4].isdigit() else None,
            'poster_path': poster_path,
            'poster_url': (
                f'https://image.tmdb.org/t/p/w342{poster_path}'
                if poster_path.startswith('/') else poster_path
            ),
            'source_url': f'https://www.themoviedb.org/{entity_type}/{provider_id}',
            'snapshot_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        })
        seen_ids.add(provider_id)
        if len(choices) >= limit:
            break
    return choices


def _tmdb_subject_observation_candidates(
    query: str,
    name: str,
    info: Mapping[str, object],
    *,
    expected_year: Optional[int],
) -> tuple[Mapping[str, object], ...]:
    """Project Search evidence without upgrading its winner to exact.

    ``Search.get_tv_info`` deliberately returns the strongest lexical winner,
    not a proof that the winner is unique.  Its bounded alternatives therefore
    remain part of the authority used by subject-level crosswalk inference.
    """

    selected_id = str(info.get("id") or "").strip()
    raw_items = [
        dict(item)
        for item in info.get("_bar_search_alternatives", ()) or ()
        if isinstance(item, Mapping)
    ]
    if selected_id and not any(
        str(item.get("id") or "").strip() == selected_id
        for item in raw_items
    ):
        raw_items.insert(0, dict(info))
    normalized_query = _normalize_title_for_score(query)
    candidates = []
    seen_ids = set()
    for item in raw_items[:30]:
        candidate_id = str(item.get("id") or "").strip()
        if not candidate_id or candidate_id in seen_ids:
            continue
        seen_ids.add(candidate_id)
        raw_aliases = {
            str(item.get("name") or ""),
            str(item.get("original_name") or ""),
        }
        merged = dict(item)
        if candidate_id == selected_id:
            merged.update(dict(info))
        aliases = {
            *raw_aliases,
            str(merged.get("name") or ""),
            str(merged.get("original_name") or ""),
            str(name if candidate_id == selected_id else ""),
            *(
                str(value)
                for value in merged.get("alternative_titles", ()) or ()
                if value
            ),
            *(
                str(value)
                for value in merged.get("_bar_search_alternative_titles", ()) or ()
                if value
            ),
        }
        exact_title = bool(normalized_query) and any(
            _normalize_title_for_score(value) == normalized_query
            for value in aliases
            if value
        )
        date_value = str(merged.get("first_air_date") or "")
        candidate_year = (
            int(date_value[:4]) if date_value[:4].isdigit() else None
        )
        season_years = {
            int(str(season.get("air_date") or "")[:4])
            for season in merged.get("seasons", ()) or ()
            if isinstance(season, Mapping)
            and str(season.get("air_date") or "")[:4].isdigit()
            and isinstance(season.get("season_number"), int)
            and not isinstance(season.get("season_number"), bool)
            and int(season["season_number"]) > 0
        }
        year_matches = (
            expected_year is None
            or candidate_year == expected_year
            or expected_year in season_years
        )
        animation_evidence = classify_tmdb_animation_evidence(
            genres=merged.get("genres", ()),
            genre_ids=merged.get("genre_ids", ()),
        )
        animation_matches = animation_evidence.is_anime is True
        candidates.append({
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": candidate_id,
            "tmdb_tv_id": candidate_id,
            "match_tier": (
                "exact"
                if exact_title and year_matches and animation_matches
                else "fuzzy"
            ),
            "title": str(merged.get("name") or name or ""),
            "original_title": str(merged.get("original_name") or ""),
            "first_air_date": date_value,
            "score_components": {
                "title_exact": exact_title,
                "year_compatible": year_matches,
                "animation_confirmed": animation_evidence.is_anime,
                "animation_policy_version": animation_evidence.policy_version,
                "animation_evidence_source": animation_evidence.evidence_source,
                "animation_reason_code": animation_evidence.reason_code,
            },
            "seasons": tuple(
                {
                    "season_number": season.get("season_number"),
                    "air_date": str(season.get("air_date") or ""),
                    "episode_count": season.get("episode_count"),
                }
                for season in merged.get("seasons", ()) or ()
                if isinstance(season, Mapping)
            ),
        })
    return tuple(candidates)


def _dedupe_provider_candidates(
    candidates: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    """Keep provider records separate from local playback-layout choices."""

    unique: List[Dict[str, object]] = []
    seen = set()
    for candidate in candidates:
        key = (
            str(candidate.get('namespace') or '').casefold(),
            str(candidate.get('entity_type') or '').casefold(),
            str(candidate.get('provider_id') or ''),
        )
        if not key[2] or key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _resolve_plan_naming_decision(
    *,
    name: str,
    info: Mapping[str, object],
    entity_type: str,
    provider_selection: Optional[Mapping[str, object]],
    bangumi_title_selection: Optional[Mapping[str, object]] = None,
) -> CanonicalNamingDecision:
    primary_marker = info.get("_bar_primary_provider")
    primary = (
        {
            "namespace": str(primary_marker.get("namespace") or ""),
            "entity_type": str(primary_marker.get("entity_type") or ""),
            "provider_id": str(primary_marker.get("provider_id") or ""),
        }
        if isinstance(primary_marker, Mapping)
        else {
            "namespace": "tmdb",
            "entity_type": entity_type,
            "provider_id": str(info.get("id") or ""),
        }
    )
    selection = dict(provider_selection or {})
    namespace = str(selection.get("namespace") or "").strip().casefold()
    if namespace == "bangumi":
        title_selection = dict(bangumi_title_selection or selection)
        selected = {
            "namespace": "bangumi",
            "entity_type": "subject",
            "provider_id": str(title_selection.get("provider_id") or ""),
            "localized_title": str(title_selection.get("title") or ""),
            "original_title": str(
                title_selection.get("original_title") or ""
            ),
        }
        mapped = ({
            **primary,
            "localized_title": str(
                info.get("name") or info.get("title") or name or ""
            ),
            "original_title": str(
                info.get("original_name") or info.get("original_title") or ""
            ),
        },)
    else:
        selected = {
            **primary,
            "localized_title": str(
                selection.get("title")
                or info.get("name")
                or info.get("title")
                or name
                or ""
            ),
            "original_title": str(
                selection.get("original_title")
                or info.get("original_name")
                or info.get("original_title")
                or ""
            ),
        }
        mapped = ()
    return resolve_title_authority(
        selection=selected,
        mapped_candidates=mapped,
        primary_provider_identity=primary,
        primary_identity_evidence=(primary,),
    )


def _with_parent_title_candidates(
    candidates: Tuple[SearchTitleCandidate, ...],
    path: Path,
) -> Tuple[SearchTitleCandidate, ...]:
    """Add a bounded series-folder title as provider discovery evidence.

    Release folders often use a romanized child title below a Chinese/Japanese
    series folder.  TMDB may only index one of those translations for a TV
    presentation, so the parent is useful for discovery but never bypasses the
    normal confidence and manual-review gates.
    """

    parent_name = path.parent.name.strip()
    if not (parent_name.startswith("[") and "]" in parent_name[:48]):
        return candidates
    combined = list(candidates)
    seen = {item.key for item in combined}
    parent = parse_release_title(parent_name)
    for item in sorted(parent.candidates, key=lambda value: len(value.title)):
        if item.key in seen:
            continue
        combined.append(SearchTitleCandidate(
            item.title,
            item.year,
            "来自上一级作品目录，仅用于补充 TMDB 候选",
        ))
        seen.add(item.key)
    return tuple(combined[:4])


def _score_provider_match(
    candidate: SearchTitleCandidate,
    name: str,
    info: Dict,
    media_kind: str,
) -> Tuple[float, str]:
    provider_names = {
        name,
        str(info.get('name') or ''),
        str(info.get('title') or ''),
        str(info.get('original_name') or ''),
        str(info.get('original_title') or ''),
        str(info.get('_bar_search_name') or ''),
        str(info.get('_bar_search_original_name') or ''),
        *(
            str(value)
            for value in info.get('_bar_search_alternative_titles', ())
            if value
        ),
    }
    normalized_candidate = _normalize_title_for_score(candidate.title)
    similarities = [
        SequenceMatcher(
            None,
            normalized_candidate,
            _normalize_title_for_score(provider_name),
        ).ratio()
        for provider_name in provider_names
        if provider_name and _normalize_title_for_score(provider_name)
    ]
    title_similarity = max(similarities, default=0.0)
    # TMDB may localize the returned name to Chinese while ``original_name``
    # remains Japanese, so a romanized query can have zero direct similarity
    # even when the provider's top-ranked ID is correct.
    score = 0.60 + (0.35 * title_similarity)

    result_year = _provider_year(info, media_kind)
    if candidate.year:
        if result_year == candidate.year:
            score += 0.10
            year_reason = '年份一致'
        elif result_year:
            score -= 0.40
            year_reason = f'年份冲突({candidate.year}!={result_year})'
        else:
            score -= 0.05
            year_reason = '结果缺少年份'
    else:
        year_reason = '候选未限定年份'

    if not candidate.year and title_similarity < 0.25:
        score = min(score, 0.49)
        year_reason += '；标题/别名缺少最低词法证据'

    score = max(0.0, min(1.0, score))
    reason = f'标题相似度={title_similarity:.3f}；{year_reason}'
    return score, reason


def _normalize_season_match_text(value: str) -> str:
    """Normalize release metadata into comparable Unicode word tokens."""
    normalized = unicodedata.normalize('NFKC', value).casefold()
    tokenized = ''.join(char if char.isalnum() else ' ' for char in normalized)
    return ' '.join(tokenized.split())


def _plan_expected_sources(
    source_path: Path,
    mapping: Dict[Path, Path],
    task_source_scope: Optional[TaskSourceScope] = None,
) -> Tuple[Path, ...]:
    """Return mapped files plus every video in the legacy traversal scope."""

    expected = {
        Path(path) for path in mapping
        if _source_matches_task_scope(Path(path), task_source_scope)
    }

    def add_video(item: Path) -> None:
        if (
            not item.is_file()
            or item.suffix.lower() not in VIDEO_SUFFIX
            or not _source_matches_task_scope(item, task_source_scope)
        ):
            return
        expected.add(item)

    if source_path.is_file():
        add_video(source_path)
    elif source_path.is_dir():
        for item in source_path.iterdir():
            if item.is_dir():
                for nested_item in item.iterdir():
                    add_video(nested_item)
            else:
                add_video(item)

    return tuple(sorted(expected, key=lambda item: str(item).casefold()))


def _source_matches_task_scope(
    path: Path,
    task_source_scope: Optional[TaskSourceScope],
) -> bool:
    if task_source_scope is None:
        return True
    return any(Path(item.source_path) == path for item in task_source_scope.source_identities)


def _backfill_legacy_series_assets(catalog: SeriesCatalog, series_key: str):
    """Reject legacy durable backfill without a typed migration authority."""

    with catalog.lock(series_key):
        current = catalog.load(series_key)
        if current.schema_version != 1:
            return current
        raise SeriesCatalogError(
            "旧SeriesCatalog只允许只读；缺少显式current-schema migration authority"
        )


def _contains_custom_season_name(path_name: str, season_name: str) -> bool:
    """Match a complete custom season name without accepting partial words."""
    normalized_path = _normalize_season_match_text(path_name)
    normalized_season = _normalize_season_match_text(season_name)
    if not normalized_path or not normalized_season:
        return False
    return f' {normalized_season} ' in f' {normalized_path} '


def _is_generic_season_name(season_name: str) -> bool:
    """Return whether TMDB supplied a generic ``Season <number>`` name."""
    normalized = unicodedata.normalize('NFKC', season_name).strip()
    return re.fullmatch(r'Season\s*\d+', normalized, re.IGNORECASE) is not None



class Rename:
    def __init__(
        self,
        *,
        decision_state_root: Path = DECISION_STATE_PATH,
        provider_link_catalog_root: Path = PROVIDER_LINK_CATALOG_PATH,
    ):
        self.decision_state_root = Path(decision_state_root)
        self.provider_link_catalog_root = Path(provider_link_catalog_root)
        self.BANGUMI_PATH = Path(cm.get_config('bangumi_path'))
        self.MOVIE_PATH = Path(cm.get_config('movie_path'))
        self.ANIME_PATH = Path(cm.get_config('anime_path'))
        self.ANIME_MOVIE_PATH = Path(cm.get_config('anime_movie_path'))

        self.search = Search()
        self.bangumi_search = None
        if bool(getattr(cm, 'config', {}).get('bangumi_enabled', False)):
            self.bangumi_search = BangumiProviderClient(
                token=str(getattr(cm, 'config', {}).get('bangumi_token', '')),
                user_agent=str(getattr(cm, 'config', {}).get(
                    'bangumi_user_agent',
                    'BangumiAutoRenamer/0.3 (local media organizer)',
                )),
            )

        self.R = {}
        self.subtitle_contracts = {}
        self.active_media_roles: Dict[Path, str] = {}
        self.last_plan: Optional[RenamePlan] = None
        self.active_task_source_scope: Optional[TaskSourceScope] = None


    def _provider_search_policy(self, *, is_anime: Optional[bool], is_movie: Optional[bool]):
        """The queue freezes this same scope before any Provider transport."""
        domain = (
            'animation' if is_anime is True
            else ('general' if is_anime is False else 'unknown')
        )
        media_hint = (
            'movie' if is_movie is True
            else ('tv' if is_movie is False else 'unknown')
        )
        return build_search_policy(
            domain=domain,
            media_hint=media_hint,
            tmdb_configured=bool(getattr(self.search, 'TMDB_KEY', '')),
            bangumi_configured=getattr(self, 'bangumi_search', None) is not None,
        )

    def _provider_search_batch(
        self,
        candidates: Tuple[SearchTitleCandidate, ...],
        *,
        is_anime: Optional[bool],
        is_movie: Optional[bool],
    ) -> ProviderSearchBatch:
        """Run identity discovery concurrently without consulting disk layout."""
        policy = self._provider_search_policy(is_anime=is_anime, is_movie=is_movie)
        bangumi_client = getattr(self, 'bangumi_search', None)
        workers = {}

        if SOURCE_BANGUMI in policy.scheduled_sources:
            def search_bangumi() -> ProviderWorkerOutput:
                collected: List[Dict[str, object]] = []
                for candidate in candidates[:2]:
                    search = bangumi_client.search_animation if policy.domain == 'animation' else bangumi_client.search_subjects
                    kwargs = {} if policy.domain == 'animation' else {'domain': policy.domain}
                    collected.extend(search(candidate.title, year=candidate.year or None, **kwargs))
                    if collected:
                        break
                return ProviderWorkerOutput(tuple(collected))

            workers[SOURCE_BANGUMI] = search_bangumi

        def tmdb_worker(
            source: str,
            method,
            media_kind: str,
            entity_type: str,
        ) -> None:
            if source not in policy.scheduled_sources:
                return

            def run() -> ProviderWorkerOutput:
                observations: Dict[str, Dict[str, object]] = {}
                name, info, issue = self._search_title_candidates(
                    method,
                    candidates,
                    media_kind,
                    observations=observations,
                )
                observation = observations.get(entity_type, {})
                observed_info = observation.get('info') or info or {}
                observed_name = str(observation.get('name') or name or '')
                options = _provider_candidate_options(
                    observed_info,
                    entity_type=entity_type,
                    selected_title=observed_name,
                ) if observed_info else []
                return ProviderWorkerOutput(
                    tuple(options),
                    runtime={
                        'name': name,
                        'info': info,
                        'issue': issue,
                        'observations': observations,
                    },
                )

            workers[source] = run

        tmdb_worker(
            SOURCE_TMDB_TV,
            self.search.get_tv_info,
            '电视剧',
            'tv',
        )
        tmdb_worker(
            SOURCE_TMDB_MOVIE,
            self.search.get_movie_info,
            '电影',
            'movie',
        )
        primary = candidates[0]
        return ProviderSearchCoordinator(workers).search(
            policy,
            query=primary.title,
            year=primary.year or None,
        )

    def _task_type_from_provider_batch(
        self,
        batch: ProviderSearchBatch,
        *,
        is_anime: Optional[bool],
        is_movie: Optional[bool],
    ) -> Union[Tuple[str, Dict, Optional[bool], bool], str]:
        self.last_type_candidates = {}
        for result in batch.results:
            runtime = result.runtime if isinstance(result.runtime, dict) else {}
            observations = runtime.get('observations') or {}
            for kind, observation in observations.items():
                self.last_type_candidates[kind] = dict(observation)

        from .automatic_recognition import resolve_batch_candidate
        try:
            _selected, name, info, anime, movie = resolve_batch_candidate(batch,
                is_anime=is_anime, is_movie=is_movie)
            return name, info, anime, movie
        except ValueError as exc:
            return '[作品搜索] ' + str(exc)

    def _resolve_bangumi_provider_selection(
        self,
        selection: Mapping[str, object],
    ) -> Union[
        Tuple[str, Dict[str, object], Optional[ProviderLinkResolution]],
        ProviderSelectionAwaitingUserOutcome, OvaSeasonPending,
    ]:
        """只读取所选Bangumi条目，人工季号不依赖跨站或续作关系。"""
        provider_id = str(selection.get('provider_id') or '').strip()
        client = self.bangumi_search or BangumiProviderClient(
            token=str(getattr(cm, 'config', {}).get('bangumi_token', '')),
            user_agent=str(getattr(cm, 'config', {}).get('bangumi_user_agent', 'BangumiAutoRenamer/0.3')))
        detail = client._get_json(f'/v0/subjects/{provider_id}')
        from .provider_selection_authority import validate_bangumi_subject
        validate_bangumi_subject(detail, provider_id, 'animation', 'tv_episodes')
        if str(detail.get('platform', '')).casefold() == 'ova':
            subject = OvaSubject.capture(detail)
            if getattr(getattr(self, 'initial_movie_request', None), 'requested_season', None) is None:
                from .content_associations import encode
                task_id = str(getattr(self, 'active_consumer_task_id', '') or getattr(getattr(self, 'active_task_source_scope', None), 'task_id', ''))
                return OvaSeasonPending(task_id, encode(dict(selection)), subject)
            info = subject.info()
        else:
            name = str(detail.get('name_cn') or detail.get('name') or '').strip()
            info = dict(id=detail['id'], name=name, title=name, original_name=str(detail.get('name') or ''),
                first_air_date=str(detail.get('date') or ''), genres=({'id': 16, 'name': 'Animation'},),
                _bar_primary_provider=dict(namespace='bangumi', entity_type='subject', provider_id=provider_id),
                _bar_linked_provider_identities=())
        if (str(detail.get('platform', '')).casefold() != 'ova'
                and getattr(getattr(self, 'initial_movie_request', None), 'requested_season', None) is None):
            from .bangumi_season_advice import season_advice
            info['seasons'] = season_advice(client, detail)
        self.active_provider_naming_selection = {**dict(selection), 'title': info['name'], 'original_title': info['original_name']}
        self.active_provider_link_failure = None
        return info['name'], info, None



    def _animation_movie_link(
        self,
        selection: Mapping[str, object],
        movie_info: Mapping[str, object],
    ) -> Optional[ProviderIdentity]:
        """Return one unique, title/year-locked Bangumi OVA link."""

        return self._animation_bangumi_link(
            selection,
            movie_info,
            allowed_platforms={'ova', 'movie', '剧场版'},
        )

    def _animation_bangumi_link(
        self,
        selection: Mapping[str, object],
        provider_info: Mapping[str, object],
        *,
        allowed_platforms: set[str],
    ) -> Optional[ProviderIdentity]:
        """Return one unique title/year/platform-locked Bangumi identity."""

        client = getattr(self, 'bangumi_search', None)
        if client is None:
            return None
        title_values = {
            _normalize_title_for_score(str(value or ''))
            for value in (
                selection.get('title'), selection.get('original_title'),
                provider_info.get('title'), provider_info.get('name'),
                provider_info.get('original_title'), provider_info.get('original_name'),
            )
            if str(value or '').strip()
        }
        release_date = str(
            provider_info.get('release_date')
            or provider_info.get('first_air_date')
            or ''
        )
        year = int(release_date[:4]) if release_date[:4].isdigit() else None
        query = str(selection.get('original_title') or selection.get('title') or '').strip()
        if not query:
            return None
        try:
            candidates = client.search_animation(query, year=year, limit=4)
            if not candidates and year is not None:
                # TMDB and Bangumi may use theatrical/streaming dates on
                # opposite sides of New Year for the same OVA.  The fallback
                # remains title-exact, unique, and bounded below.
                candidates = client.search_animation(query, limit=4)
        except ProviderSearchError:
            return None
        matches: List[Dict[str, object]] = []
        for candidate in candidates:
            candidate_titles = {
                _normalize_title_for_score(str(candidate.get(key) or ''))
                for key in ('title', 'original_title')
                if str(candidate.get(key) or '').strip()
            }
            if not title_values.intersection(candidate_titles):
                continue
            candidate_year = candidate.get('year')
            if year is not None and candidate_year is not None:
                try:
                    if abs(int(candidate_year) - year) > 1:
                        continue
                except (TypeError, ValueError):
                    continue
            try:
                detail = client.load_subject_evidence(
                    str(candidate.get('provider_id') or '')
                )
            except ProviderSearchError:
                continue
            if str(detail.get('platform') or '').casefold() not in allowed_platforms:
                continue
            matches.append(detail)
        unique = {
            str(item.get('provider_id')): item for item in matches
        }
        if len(unique) != 1:
            return None
        item = next(iter(unique.values()))
        return ProviderIdentity(
            status='confirmed',
            namespace='bangumi',
            entity_type='subject',
            provider_id=str(item['provider_id']),
            title=str(item.get('title') or ''),
            year=item.get('year') if isinstance(item.get('year'), int) else None,
            decision_source='unique_title_year_animation_link',
            evidence=('标题、年份与动画形式唯一一致',),
            original_title=str(item.get('original_title') or '') or None,
            poster_url=str(item.get('poster_url') or '') or None,
            source_url=str(item.get('source_url') or '') or None,
            snapshot_at=str(item.get('snapshot_at') or '') or None,
        )

    def _apply_selected_bangumi_title_authority(
        self,
        plan: RenamePlan,
    ) -> RenamePlan:
        """Keep the selected Bangumi subject as the presentation title authority."""

        selection = self.active_provider_selection or {}
        if (
            str(selection.get('namespace') or '').casefold() == 'bangumi'
            and plan.work_identity is not None
            and not any(
                entry.presentation_asset is not None
                and entry.presentation_asset.asset_role.kind == 'content_version'
                for entry in plan.entries
            )
        ):
            selected_id = str(selection.get('provider_id') or '')
            selected_subjects = tuple(
                identity for identity in self.active_linked_provider_identities
                if identity.namespace == 'bangumi'
                and identity.entity_type == 'subject'
                and identity.provider_id == selected_id
            )
            if len(selected_subjects) == 1:
                selected_subject = selected_subjects[0]
                old_primary = plan.work_identity.primary_provider
                linked = tuple(
                    dict.fromkeys(
                        (old_primary,) + tuple(
                            identity for identity in plan.linked_provider_identities
                            if identity != selected_subject
                        )
                    )
                )
                plan = replace(
                    plan,
                    work_identity=replace(
                        plan.work_identity,
                        title=selected_subject.title,
                        year=selected_subject.year,
                        primary_provider=selected_subject,
                        domain='animation',
                        domain_source='provider_selection',
                    ),
                    presentation_groups=tuple(
                        replace(group, provider_identity=selected_subject)
                        for group in plan.presentation_groups
                    ),
                    linked_provider_identities=linked,
                )
        return plan

    def _recognition_identity(
        self,
        tmdb_provider: ProviderIdentity,
        *,
        title: str,
        year: Optional[int],
        is_anime: Optional[bool],
        prefer_selected_subject_primary: bool = False,
    ) -> Tuple[WorkIdentity, Tuple[ProviderIdentity, ...]]:
        """Build explicit resolver output without reading authority from Plan."""

        primary = tmdb_provider
        linked = tuple(self.active_linked_provider_identities)
        selection = self.active_provider_selection or {}
        if str(selection.get("namespace") or "").casefold() == "bangumi":
            selected_id = str(selection.get("provider_id") or "").strip()
            selected = tuple(
                identity for identity in linked
                if identity.namespace == "bangumi"
                and identity.entity_type == "subject"
                and identity.provider_id == selected_id
            )
            if len(selected) == 1:
                primary = selected[0]
                primary_key = (
                    primary.namespace,
                    primary.entity_type,
                    primary.provider_id,
                )
                tmdb_key = (
                    tmdb_provider.namespace,
                    tmdb_provider.entity_type,
                    tmdb_provider.provider_id,
                )
                linked = tuple(
                    dict.fromkeys(
                        ((tmdb_provider,) if tmdb_key != primary_key else ())
                        + tuple(
                            identity for identity in linked
                            if (
                                identity.namespace,
                                identity.entity_type,
                                identity.provider_id,
                            ) != primary_key
                        )
                    )
                )
                if is_anime is None:
                    is_anime = selection.get('library_domain') != 'general'
        domain = (
            "animation" if is_anime is True
            else "general" if is_anime is False
            else "unknown"
        )
        chosen_domain = str(selection.get("library_domain") or "").casefold()
        domain_source = (
            "provider_selection"
            if primary.namespace == "bangumi"
            or chosen_domain in {"animation", "general"}
            else "unknown" if domain == "unknown"
            else "task_metadata"
        )
        return (
            WorkIdentity(
                status="confirmed",
                domain=domain,
                title=title,
                year=year,
                primary_provider=primary,
                domain_source=domain_source,
            ),
            linked,
        )

    def get_season_id(
        self,
        tv_info: Dict,
        work_path: Path,
        path: Path,
        titles: Optional[List[Dict]],
    ):
        season_id = 1
        path_name = path.name
        all_similaritys: List[Dict[float, int]] = []
        int_rtpath_name = extract_season(path_name)
        title_season_candidate = (
            '未匹配' if int_rtpath_name in (-1, None) else str(int_rtpath_name)
        )
        logger.info(f'[处理任务] 目录标题季号候选：{title_season_candidate}')

        for season in tv_info['seasons']:
            info_season_id = season['season_number']

            sname: str = season['name']
            logger.info(f'[处理任务] Season{info_season_id} 季度名: {sname}')

            '''
            int_season = extract_season(sname)
            logger.info(f'[处理任务] 提取信息季号:{int_season}')
            '''

            if (
                int_rtpath_name not in (-1, None)
                and info_season_id == int_rtpath_name
            ):
                season_id = int_rtpath_name
                break

            # Generic TMDB names are handled only by explicit season numbers.
            if not _is_generic_season_name(sname):
                if _contains_custom_season_name(path.name, sname):
                    logger.info(f'[处理任务] 季度名称处于标题中：{sname}')
                    season_id = info_season_id
                    break

                if titles:
                    # 或者计算相似度
                    for title in titles:
                        similaritys = {}
                        if title['type'] in [
                            'Default',
                            'Synonym',
                            'English',
                            'French',
                        ]:
                            ename = title['title']
                            candidate_path_name = path_name.replace(ename, '')
                            normalized_season_name = (
                                _normalize_season_match_text(sname)
                            )
                            normalized_candidate = _normalize_season_match_text(
                                remove_tag(candidate_path_name)
                            )
                            if (
                                not normalized_season_name
                                or not normalized_candidate
                            ):
                                continue
                            similarity = SequenceMatcher(
                                None,
                                normalized_season_name,
                                normalized_candidate,
                            ).ratio()

                            # logger.debug(f'相似度{tindex}：{similarity}')
                            similaritys[similarity] = info_season_id
                            all_similaritys.append(similaritys)
        else:
            if all_similaritys:
                logger.info(f'[处理任务] 相似度：{all_similaritys}')
                season_id = to_sim_max(all_similaritys)

        logger.info(f'[处理任务] 目录启发式季号候选：{season_id}（非最终值）')
        return season_id

    def process_sub(
        self,
        itme_path_main_name: str,
        item_repeat: Optional[List[str]],
        item_path: Path,
        work_path: Path,
        season_id: int,
        episode_hint: Optional[int] = None,
        semantic_context: Optional[str] = None,
        secondary_bonus_episode: Optional[int] = None,
        target_series_title: Optional[str] = None,
        source_root: Optional[Path] = None,
    ):
        item_name = item_path.name
        if item_repeat:
            item_name_remove = remove_similar_part(item_repeat, item_path.stem)
        else:
            item_name_remove = item_path.stem

        item_suffix = item_path.suffix.lower()

        reduced_name = item_name.replace(itme_path_main_name, '')
        logger.info(f'[处理任务] 移去主要内容后的文件名：{reduced_name}')

        classification = classify_media(
            item_path=item_path,
            candidate_name=item_name_remove,
            season_id=season_id,
            extra_context_name=(
                semantic_context
                if semantic_context is not None
                else reduced_name
            ),
            episode_hint=episode_hint,
            secondary_bonus_episode=secondary_bonus_episode,
        )
        chosen_season = getattr(self, 'active_explicit_general_season', None)
        if chosen_season is not None and classification.local_role == LocalRole.MAIN_EPISODE:
            classification = replace(classification, season_scope=chosen_season)
        scoped_season = classification.season_scope
        normalized_stem = normalize_target_stem(
            item_path.stem,
            target_series_title or itme_path_main_name,
        )
        normalized_name = f'{normalized_stem}{item_path.suffix}'

        if classification.local_role in {
            LocalRole.DISC_EXTRA,
            LocalRole.EPISODE_LIKE_BONUS,
        }:
            relative = item_path.relative_to(source_root) if source_root is not None else Path(item_path.name)
            if item_suffix in VIDEO_SUFFIX:
                relative = relative.with_name(normalized_name)
            extra_target = canonical_extras_target(work_path / jellyfin_season_dir(scoped_season), relative)
            target, target_name = extra_target.parent, extra_target.name
        elif classification.local_role == LocalRole.PROVIDER_SPECIAL:
            target = work_path / jellyfin_season_dir(scoped_season)
            target_name = normalized_name
        elif classification.local_role == LocalRole.MAIN_EPISODE:
            target = work_path / jellyfin_season_dir(scoped_season)
            episode = classification.episode_number
            if episode is None:
                raise ValueError('main_episode 缺少 episode_number')
            target_name = (
                f'S{scoped_season:02d}E{episode:02d} - {normalized_name}'
            )
        else:
            target = (
                work_path
                / 'ManualReview'
                / jellyfin_season_dir(scoped_season)
            )
            target_name = normalized_name
            if item_suffix not in VIDEO_SUFFIX and source_root is not None:
                relative = item_path.relative_to(source_root)
                target = target / relative.parent
                target_name = item_path.name

        self.R[item_path] = target / target_name
        logger.info(
            f'[处理任务] 分类 {classification.local_role.value}：'
            f'{item_path.name} -> {target / target_name}；'
            f'provider_type={classification.provider_type}；'
            f'confidence={classification.confidence}；'
            f'reason={classification.reason}'
        )
        return classification
        logger.info(f'[处理任务] 处理完成{item_name}')
        return None

    def process(
        self,
        path: Path,
        _is_anime: Optional[bool] = None,
        _is_movie: Optional[bool] = None,
        _tuuid: Optional[str] = None,
        cus_name: Optional[str] = None,
        cus_season_id: Optional[int] = None,
        use_ai: Optional[bool] = None,
        provider_selection: Optional[Dict[str, object]] = None,
        task_source_scope: Optional[TaskSourceScope | Mapping[str, object]] = None,
    ):
        if provider_selection:
            grouped_selection = partition_provider_candidates_by_presentation(
                (provider_selection,)
            )
            group_kind = next(iter(grouped_selection), "")
            capability = provider_selection_capability(
                provider_selection,
                group_kind=group_kind,
                task_media_kind=task_media_kind_from_flag(_is_movie),
                task_library_domain=task_library_domain_from_flag(_is_anime),
            )
            if not capability.actionable:
                raise ProviderSearchError(capability.message)
        if isinstance(task_source_scope, Mapping):
            task_source_scope = TaskSourceScope.from_mapping(task_source_scope)
        if (
            task_source_scope is not None
            and _tuuid is not None
            and task_source_scope.task_id != _tuuid
        ):
            raise TaskBatchScopeError("task source scope与任务ID不一致")
        automatic_origin = getattr(self, 'automatic_recognition_origin', None)
        if automatic_origin is not None:
            from .automatic_recognition import validate_original_job
            from .default_activation import service_for_worker
            from .source_basis import SourceInputProof, observe_initial_input
            service = service_for_worker(self)
            task, active_scope = validate_original_job(service, automatic_origin)
            source_input = observe_initial_input(service, service.plan_store.load(automatic_origin.task_id),
                service.decision_state_store.load(automatic_origin.task_id))
            if (type(source_input) is not SourceInputProof or task_source_scope != active_scope
                    or _tuuid != automatic_origin.task_id or path != source_input.observation_path
                    or _is_anime != task['is_anime'] or _is_movie != task['is_movie']):
                raise ValueError('自动worker输入与实际原作业范围不一致')
        # 输入范围在接纳时确定；目录形状不再隐式拆成多次作品处理。
        return self._process(path, _is_anime, _is_movie, _tuuid, cus_name, cus_season_id,
            use_ai=use_ai, provider_selection=provider_selection, task_source_scope=task_source_scope)

    @staticmethod
    def discovery_title_candidates(path, custom_name=None, *, task_source_scope=None):
        candidates = parse_release_title(custom_name or path.name).candidates
        directory = (not any(Path(row.source_path) == path for row in task_source_scope.source_identities)
                     if task_source_scope is not None else path.is_dir())
        if not custom_name and directory:
            from .input_directory_discovery import season_folder, SourceTitleUncertain
            season = season_folder(path.name)
            if season is not None:
                videos = (sorted(Path(row.source_path) for row in task_source_scope.source_identities
                                 if Path(row.source_path).suffix.lower() in VIDEO_SUFFIX)
                          if task_source_scope is not None else
                          sorted(item for item in path.iterdir() if item.is_file() and item.suffix.lower() in VIDEO_SUFFIX))
                hints = []
                for video in videos:
                    episode = re.search(r'(?<![A-Za-z0-9])S(\d{1,2})E\d{1,3}(?!\d)', video.stem, re.I)
                    if episode and int(episode[1]) != season:
                        raise SourceTitleUncertain('季度目录缺少非空一致的同季作品标题，请在任务卡手动确认作品')
                    prefix = video.stem[:episode.start()].strip(' -_') if episode else ''
                    parsed = parse_release_title(prefix).candidates if prefix else ()
                    if not episode or int(episode[1]) != season or not parsed or not parsed[0].title.strip() or parsed[0].title.isdigit():
                        continue
                    hints.append(parsed[0])
                if hints and len({item.key for item in hints}) == 1:
                    # Season N is layout, not a fallback work title. Searching
                    # it in another provider domain introduces unrelated works.
                    return tuple(hints[:1])
                if not hints and videos:
                    # Parent name is only a search clue. Do not enumerate
                    # parents/siblings or use structural/library directory names.
                    parent = path.parent.name.strip()
                    structural = re.fullmatch(r'(?:bd|bdrip|bluray|blu-ray|dvd|\d{3,4}p|\d+|anime|animation|movies?|tv|media|videos?|downloads?|sources?|incoming|test[ _-]*source|动画|动漫|电影|电视剧|媒体|下载)', parent, re.I)
                    if parent and season_folder(parent) is None and not structural and path.parent != path.parent.parent:
                        values = parse_release_title(parent).candidates
                        if values and values[0].title.strip() and not values[0].title.isdigit():
                            return values[:4]
                raise SourceTitleUncertain('季度目录缺少非空一致的同季作品标题，请在任务卡手动确认作品')
        return (_with_parent_title_candidates(candidates, path)
                if not custom_name and directory else candidates)

    def _search_title_candidates(
        self,
        search_method,
        candidates: Tuple[SearchTitleCandidate, ...],
        media_kind: str,
        *,
        observations: Optional[Dict[str, Dict[str, object]]] = None,
    ) -> Tuple[str, Optional[Dict], Optional[str]]:
        if observations is None:
            if not hasattr(self, 'last_type_candidates'):
                self.last_type_candidates = {}
            observations = self.last_type_candidates
        matches: List[_ProviderCandidateMatch] = []
        ambiguity_issues: List[str] = []
        for index, candidate in enumerate(candidates[:4], start=1):
            logger.info(
                f'[处理任务] {media_kind}搜索候选{index}: '
                f'title={candidate.title}；year={candidate.year or "未提供"}；'
                f'reason={candidate.reason}'
            )
            name, info = search_method(candidate.title, candidate.year)
            if not name or not info:
                continue
            ambiguous_years = (
                _same_title_alternative_years(info, media_kind)
                if not candidate.year
                else ()
            )
            if ambiguous_years:
                issue = (
                    f'{media_kind}同名作品存在多个首播年份 '
                    f'{"/".join(str(value) for value in ambiguous_years)}；'
                    '源名称未提供年份'
                )
                logger.warning(f'[处理任务] {issue}，拒绝自动选择首条结果')
                ambiguity_issues.append(issue)
                candidate_key = 'tv' if media_kind == '电视剧' else 'movie'
                observations.setdefault(
                    candidate_key,
                    {'name': name, 'info': info},
                )
                continue
            score, reason = _score_provider_match(
                candidate,
                name,
                info,
                media_kind,
            )
            logger.info(
                f'[处理任务] {media_kind}候选结果: {name}；'
                f'score={score:.3f}；{reason}'
            )
            matches.append(
                _ProviderCandidateMatch(candidate, name, info, score, reason)
            )
            if score >= 0.95:
                candidate_key = 'tv' if media_kind == '电视剧' else 'movie'
                observations[candidate_key] = {
                    'name': name, 'info': info, 'trusted': True,
                }
                return name, info, None

        if not matches:
            return '', None, ambiguity_issues[0] if ambiguity_issues else None

        matches.sort(key=lambda match: match.score, reverse=True)
        best = matches[0]
        candidate_key = 'tv' if media_kind == '电视剧' else 'movie'
        observations[candidate_key] = {
            'name': best.name,
            'info': best.info,
            'trusted': best.score >= 0.60,
        }
        if best.score < 0.60:
            return '', None, f'{media_kind}候选结果置信度过低'

        if len(matches) > 1:
            second = matches[1]
            best_id = best.info.get('id')
            second_id = second.info.get('id')
            if (
                best_id is not None
                and second_id is not None
                and best_id != second_id
                and best.score - second.score < 0.08
            ):
                return (
                    '',
                    None,
                    f'{media_kind}候选结果存在歧义：'
                    f'{best.name}({best.score:.3f}) / '
                    f'{second.name}({second.score:.3f})',
                )
        return best.name, best.info, None


    @provider_operation
    def _process(
        self,
        path: Path,
        _is_anime: Optional[bool] = None,
        _is_movie: Optional[bool] = None,
        _tuuid: Optional[str] = None,
        cus_name: Optional[str] = None,
        cus_season_id: Optional[int] = None,
        use_ai: Optional[bool] = None,
        provider_selection: Optional[Dict[str, object]] = None,
        task_source_scope: Optional[TaskSourceScope] = None,
    ):
        self.subtitle_contracts = {}
        self.active_media_roles = {}
        self.active_task_source_scope = task_source_scope
        self.active_task_media_kind = _is_movie
        self.active_provider_selection = (
            dict(provider_selection) if provider_selection else None
        )
        self.active_provider_link_failure: Optional[Dict[str, object]] = None
        self.active_provider_link_resolution: Optional[ProviderLinkResolution] = None
        self.active_season_reference = None
        self.active_provider_naming_selection: Optional[Dict[str, object]] = None
        self.active_linked_provider_identities: Tuple[ProviderIdentity, ...] = ()
        provider_decision_source = 'provider_unique'
        provider_decision_evidence = (
            '全部计划来源已结束，且只有一个合格的 TMDB 候选',
        )
        provider_link_resolution: Optional[ProviderLinkResolution] = None
        provider_series_catalog_revision: Optional[int] = None
        manual_series_binding = None
        provider_batch: Optional[ProviderSearchBatch] = None
        selected_provider_candidates: List[Dict[str, object]] = []
        if _tuuid:
            _uuid = _tuuid
        else:
            _uuid = str(uuid.uuid4())
        # Consumer identity belongs to the task Decision, not to the optional
        # source-scope compatibility envelope.  Older current tasks may lack a
        # TaskSourceScope but still have an exact persisted task UUID.
        self.active_consumer_task_id = str(_uuid)

        # 【Step.0】 开始处理
        logger.info(f'[处理任务] 开始处理{path.name}')

        # 【Step.1】
        # 先移除无用的标签, 方便之后搜索
        parsed_title = parse_release_title(path.name)
        rtpath_name, year = parsed_title.primary.key
        logger.info(
            f'[处理任务] 发布标题解析后: {rtpath_name}；年份: '
            f'{year if year else "未提供"}'
        )
        logger.info(
            f'[处理任务] 发布标题字段: 技术标签={list(parsed_title.technical_tags)}；'
            f'发布组={parsed_title.release_group or "未确认"}'
        )

        # 如果该路径不是一个视频文件或者不是一个文件夹, 则跳过
        selected_file = (any(Path(row.source_path) == path for row in task_source_scope.source_identities)
                         if task_source_scope is not None else path.is_file())
        if selected_file and path.suffix.lower() not in VIDEO_SUFFIX:
            logger.info(f'[处理任务] {path.name} 不是一个视频文件, 跳过')
            return

        # 【特殊改】
        if cus_name:
            parsed_title = parse_release_title(cus_name)
            rtpath_name, year = parsed_title.primary.key
            logger.info(
                f'[处理任务] 使用自定义标题: {rtpath_name}；年份: '
                f'{year if year else "未提供"}'
            )

        # 【Step.1.5】
        # 判断类型是否为电影
        search_candidates = self.discovery_title_candidates(path, cus_name, task_source_scope=task_source_scope)
        if provider_selection:
            selection = {
                key: provider_selection.get(key)
                for key in (
                    'namespace', 'entity_type', 'provider_id', 'title',
                    'original_title', 'year', 'poster_url', 'source_url',
                    'snapshot_at', 'library_domain',
                    'platform',
                )
            }
            selected_provider_candidates = [selection]
            bangumi_candidates = (
                selected_provider_candidates
                if selection.get('namespace') == 'bangumi' else []
            )
            provider_decision_source = 'explicit_user'
            provider_decision_evidence = (
                '用户在候选卡片中锁定了资料来源与稳定作品 ID',
            )
            namespace = str(selection.get('namespace') or '').casefold()
            entity_type = str(selection.get('entity_type') or '').casefold()
            provider_id = str(selection.get('provider_id') or '').strip()
            group_kind = provider_candidate_group_kind(selection) or ''
            capability = provider_selection_capability(
                selection,
                group_kind=group_kind,
                task_media_kind=task_media_kind_from_flag(_is_movie),
                task_library_domain=task_library_domain_from_flag(_is_anime),
            )
            if not capability.actionable:
                task_type = f'[作品选择] {capability.message}'
            elif namespace == 'tmdb' and entity_type == 'tv' and _is_anime is True and not isinstance(task_source_scope, TaskSourceScope):
                task_type = '[作品选择] 缺少冻结的任务来源范围，请从当前任务重新生成计划。'
            elif namespace == 'tmdb' and entity_type in {'movie', 'tv'} and provider_id:
                try:
                    resolver = (
                        self.search.get_movie_info_by_id
                        if entity_type == 'movie'
                        else self.search.get_tv_info_by_id
                    )
                    selected_name, selected_info = resolver(int(provider_id))
                except (TMDBRequestError, TypeError, ValueError):
                    selected_name, selected_info = '', None
                if selected_name and selected_info:
                    resolved_anime = _is_anime
                    chosen_domain = str(
                        selection.get('library_domain') or ''
                    ).casefold()
                    if resolved_anime is None:
                        genre_evidence = classify_tmdb_animation_evidence(
                            genres=selected_info.get('genres', ()),
                            genre_ids=selected_info.get('genre_ids', ()),
                        )
                        animation_link = None
                        if entity_type == 'movie' and chosen_domain != 'general':
                            animation_link = (
                                self._animation_movie_link(selection, selected_info)
                                if entity_type == 'movie'
                                else self._animation_bangumi_link(
                                    selection,
                                    selected_info,
                                    allowed_platforms={'tv', 'web', 'ova'},
                                )
                            )
                        if animation_link is not None:
                            self.active_linked_provider_identities = (animation_link,)
                            resolved_anime = True
                        elif chosen_domain == 'animation':
                            resolved_anime = True
                        elif chosen_domain == 'general':
                            resolved_anime = False
                        elif genre_evidence.is_anime is not None:
                            resolved_anime = genre_evidence.is_anime
                        else:
                            # Evidence is insufficient. Keep the library domain
                            # unresolved so review must offer an explicit exit.
                            resolved_anime = None
                    if (
                        resolved_anime is True
                        and entity_type == 'movie'
                        and chosen_domain != 'general'
                        and not self.active_linked_provider_identities
                    ):
                        animation_link = (
                            self._animation_movie_link(selection, selected_info)
                            if entity_type == 'movie'
                            else self._animation_bangumi_link(
                                selection,
                                selected_info,
                                allowed_platforms={'tv', 'web', 'ova'},
                            )
                        )
                        if animation_link is not None:
                            self.active_linked_provider_identities = (animation_link,)
                    task_type = (
                        selected_name,
                        selected_info,
                        resolved_anime,
                        entity_type == 'movie',
                    )
                else:
                    task_type = '[作品选择] 无法按所选 TMDB ID 获取完整详情，请稍后重试'
            elif (capability.actionable and namespace == 'bangumi' and provider_id
                    and (capability.group_kind == 'movie_continuous' or _is_anime is False)):
                try:
                    from .provider_selection_authority import validate_bangumi_subject
                    detail = self.bangumi_search._get_json(f'/v0/subjects/{provider_id}')
                    domain = 'animation' if _is_anime is True else 'general' if _is_anime is False else str(selection.get('library_domain') or '')
                    validate_bangumi_subject(detail, provider_id, domain, capability.group_kind)
                    selected_name = str(detail.get('name_cn') or detail['name']).strip()
                    movie = capability.group_kind == 'movie_continuous'
                    selected_info = dict(id=detail['id'], name=selected_name, title=selected_name,
                        original_name=detail.get('name', ''), original_title=detail.get('name', ''),
                        _bar_primary_provider=dict(namespace='bangumi', entity_type='subject', provider_id=provider_id))
                    selected_info['release_date' if movie else 'first_air_date'] = detail.get('date', '')
                    task_type = (selected_name, selected_info, domain == 'animation', movie)
                except (ProviderSearchError, TypeError, ValueError) as exc:
                    from .provider_transport import ProviderRequestError
                    if isinstance(exc, ProviderRequestError):
                        self.active_provider_link_failure = {
                            'reason_codes': ['provider_transport_' + exc.diagnostic['code']],
                            'transport_diagnostic': dict(exc.diagnostic),
                            'retryable': exc.retryable,
                            'retry_requires_refresh': exc.retryable,
                        }
                    task_type = f'[作品选择] {exc}'
            elif capability.actionable and namespace == 'bangumi' and provider_id:
                try:
                    resolution_outcome = (
                        self._resolve_bangumi_provider_selection(selection)
                    )
                    if isinstance(
                        resolution_outcome,
                        (ProviderSelectionAwaitingUserOutcome, OvaSeasonPending),
                    ):
                        return resolution_outcome
                    (
                        selected_name,
                        selected_info,
                        provider_link_resolution,
                    ) = resolution_outcome
                    if provider_link_resolution is not None:
                        selected_season = dict(provider_link_resolution.season_assignments).get(provider_id)
                        if not isinstance(selected_season, int):
                            raise ProviderSearchError('季度匹配没有包含所选 Bangumi 作品')
                        if cus_season_id is None:
                            cus_season_id = selected_season
                    self.active_provider_link_resolution = provider_link_resolution
                    task_type = (
                        selected_name,
                        selected_info,
                        True,
                        False,
                    )
                    selected_subject = ProviderIdentity(
                        status='confirmed',
                        namespace='bangumi',
                        entity_type='subject',
                        provider_id=provider_id,
                        title=str(selection.get('title') or ''),
                        year=(selection.get('year') if isinstance(selection.get('year'), int) else None),
                        decision_source='explicit_user',
                        evidence=('用户明确选择的 Bangumi 季度条目',),
                        original_title=str(selection.get('original_title') or '') or None,
                        poster_url=str(selection.get('poster_url') or '') or None,
                        source_url=str(selection.get('source_url') or '') or None,
                        snapshot_at=str(selection.get('snapshot_at') or '') or None,
                    )
                    metadata_links = []
                    first_air_date = str(
                        selected_info.get('first_air_date') or ''
                    )
                    metadata_year = (
                        int(first_air_date[:4])
                        if first_air_date[:4].isdigit() else None
                    )
                    for raw_link in selected_info.get(
                        '_bar_linked_provider_identities', ()
                    ) or ():
                        if not isinstance(raw_link, Mapping):
                            continue
                        try:
                            metadata_links.append(provider_identity_from_metadata(
                                namespace=str(raw_link.get('namespace') or ''),
                                entity_type=str(raw_link.get('entity_type') or ''),
                                provider_id=raw_link.get('provider_id'),
                                title=str(
                                    selected_info.get('name')
                                    or selected_name
                                    or ''
                                ),
                                year=metadata_year,
                                decision_source='provider_unique',
                                original_title=str(
                                    selected_info.get('original_name')
                                    or selected_info.get('original_title')
                                    or ''
                                ) or None,
                            ))
                        except ValueError:
                            continue
                    self.active_linked_provider_identities = tuple(
                        dict.fromkeys((selected_subject, *metadata_links))
                    )
                except (
                    ProviderLinkCatalogError,
                    ProviderSearchError,
                    TypeError,
                    ValueError,
                ) as exc:
                    from .provider_transport import ProviderRequestError
                    if isinstance(exc, ProviderRequestError):
                        self.active_provider_link_failure = {
                            'reason_codes': ['provider_transport_' + exc.diagnostic['code']],
                            'transport_diagnostic': dict(exc.diagnostic),
                            'retryable': exc.retryable,
                            'retry_requires_refresh': exc.retryable,
                        }
                    task_type = f'[作品选择] {exc}'
            else:
                task_type = '[作品选择] 所选候选缺少有效的资料来源、类型或作品 ID'
        else:
            provider_batch = self._provider_search_batch(
                search_candidates,
                is_anime=_is_anime,
                is_movie=_is_movie,
            )
            bangumi_candidates = [
                dict(candidate) for candidate in provider_batch.candidates
                if candidate.get('namespace') == 'bangumi'
            ]
            task_type = self._task_type_from_provider_batch(
                provider_batch,
                is_anime=_is_anime,
                is_movie=_is_movie,
            )
        if isinstance(task_type, str):
            automatic_origin = getattr(self, 'automatic_recognition_origin', None)
            if automatic_origin is not None:
                from .default_activation import preserve_unresolved_observation
                # The actual queue already published a complete, scope-bound
                # source workspace. A failed search must not rescan its parent
                # or replace the frozen inventory via the legacy movie reply.
                if provider_selection is not None or provider_batch is None:
                    raise ValueError('未确认自动结果缺少原Provider查询批次')
                return preserve_unresolved_observation(self, automatic_origin, provider_batch, task_type)
            if provider_selection:
                # A failed explicit choice must remain a truthful selection
                # failure.  Falling back to the old multi-video movie draft
                # both hides the actual crosswalk error and fabricates a
                # confirmed movie identity from a Bangumi TV subject.
                return self.error_reply(
                    _uuid,
                    task_type,
                    path,
                    _is_anime,
                    _is_movie,
                    provider_candidates={
                        'work': selected_provider_candidates
                    } if selected_provider_candidates else None,
                )
            if (
                (provider_batch is not None or selected_provider_candidates)
                and path.is_dir()
            ):
                try:
                    discovery_inventory = self._inventory_for_input(_uuid, path)
                except MovieAssetInventoryError:
                    discovery_inventory = None
                blocking_reason = next((
                    reason for reason in (
                        DISC_STRUCTURE_REQUIRES_MANUAL_WORKFLOW,
                        MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION,
                    )
                    if (
                        discovery_inventory is not None
                        and reason in discovery_inventory.reason_codes
                    )
                ), None)
                if blocking_reason or (
                    discovery_inventory is not None and discovery_inventory.video_count > 0
                ):
                    discovery_candidates = (
                        [dict(candidate) for candidate in provider_batch.candidates]
                        if provider_batch is not None
                        else selected_provider_candidates
                    )
                    movie_candidate = next((
                        candidate for candidate in discovery_candidates
                        if candidate.get('entity_type') == 'movie'
                    ), {})
                    return self._movie_inventory_reply(
                        _uuid,
                        path,
                        _is_anime,
                        str(movie_candidate.get('title') or '') or None,
                        discovery_inventory,
                        blocking_reason or 'WORK_IDENTITY_REQUIRES_SELECTION',
                        {},
                        mixed_candidates=(
                            partition_provider_candidates_by_presentation(
                                discovery_candidates
                            )
                        ),
                        season_number=cus_season_id,
                        season_source=("explicit_user" if cus_season_id is not None else "unset"),
                    )
            if provider_batch is not None and path.is_file():
                try:
                    discovery_inventory = self._inventory_for_input(_uuid, path)
                except MovieAssetInventoryError:
                    discovery_inventory = None
                if discovery_inventory is not None:
                    candidates = [
                        dict(candidate) for candidate in provider_batch.candidates
                    ]
                    grouped = {
                        'movie_continuous': [
                            item for item in candidates
                            if item.get('entity_type') == 'movie'
                        ],
                        'tv_episodes': [
                            item for item in candidates
                            if item.get('entity_type') in {'tv', 'subject'}
                        ],
                    }
                    return self._movie_inventory_reply(
                        _uuid,
                        path,
                        _is_anime,
                        None,
                        discovery_inventory,
                        MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION,
                        {},
                        mixed_candidates={
                            key: value for key, value in grouped.items() if value
                        },
                        season_number=cus_season_id,
                        season_source=(
                            'explicit_user' if cus_season_id is not None else 'unset'
                        ),
                    )
            if (
                path.is_dir()
                and '多视频目录同时存在可信电视剧和电影身份' in task_type
            ):
                try:
                    mixed_inventory = self._inventory_for_input(_uuid, path)
                except MovieAssetInventoryError:
                    mixed_inventory = None
                candidates = getattr(self, 'last_type_candidates', {})
                movie_candidate = candidates.get('movie', {})
                tv_candidate = candidates.get('tv', {})
                movie_info = movie_candidate.get('info') or {}
                work_candidates = _dedupe_provider_candidates(
                    _provider_candidate_options(
                        movie_info,
                        entity_type='movie',
                        selected_title=str(movie_candidate.get('name') or ''),
                    )
                    + _provider_candidate_options(
                        tv_candidate.get('info') or {},
                        entity_type='tv',
                        selected_title=str(tv_candidate.get('name') or ''),
                    ) + bangumi_candidates
                )
                return self._movie_inventory_reply(
                    _uuid,
                    path,
                    _is_anime,
                    movie_candidate.get('name'),
                    mixed_inventory,
                    MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION,
                    movie_info,
                    mixed_candidates=partition_provider_candidates_by_presentation(
                        work_candidates
                    ),
                    season_number=cus_season_id,
                    season_source=("explicit_user" if cus_season_id is not None else "unset"),
                )
            candidate_payload: Dict[str, List[Dict[str, object]]] = {}
            if selected_provider_candidates:
                candidate_payload['work'] = selected_provider_candidates
            if provider_batch is not None and provider_batch.candidates:
                candidate_payload['work'] = [
                    dict(candidate) for candidate in provider_batch.candidates
                ]
            for candidate_type, candidate in getattr(
                self, 'last_type_candidates', {}
            ).items():
                info = candidate.get('info') or {}
                group_kind = (
                    'movie_continuous' if candidate_type == 'movie'
                    else 'tv_episodes'
                )
                candidate_payload.setdefault(group_kind, []).extend(
                    _provider_candidate_options(
                    info,
                    entity_type=candidate_type,
                    selected_title=str(candidate.get('name') or ''),
                    )
                )
            if bangumi_candidates and provider_batch is None:
                candidate_payload['work'] = bangumi_candidates
            return self.error_reply(
                _uuid,
                task_type,
                path,
                _is_anime,
                _is_movie,
                provider_candidates=candidate_payload or None,
            )

        name, info, is_anime, is_movie = (
            task_type[0],
            task_type[1],
            task_type[2],
            task_type[3],
        )
        self.active_explicit_general_season = (
            int(cus_season_id) if cus_season_id is not None and not is_anime and not is_movie else None
        )
        automatic_origin = getattr(self, 'automatic_recognition_origin', None)
        if automatic_origin is not None:
            try:
                from .automatic_recognition import AutomaticRecognition, freeze_batch
                from .default_activation import complete_tv_observation, complete_movie_observation
                if provider_selection is not None or provider_batch is None:
                    raise ValueError('自动完成必须消费原队列作业的完整Provider批次')
                proof = AutomaticRecognition(automatic_origin, freeze_batch(provider_batch),
                    search_candidates[0].title, search_candidates[0].year or None)
                return complete_movie_observation(self, proof) if is_movie else complete_tv_observation(self, proof)
            except (DecisionStateError, PlanStoreError, ProjectionAuthorityError, OSError, ValueError) as exc:
                self.R = {}
                return self.error_reply(_uuid, f'[自动完整规划] {exc}', path, is_anime, is_movie, name, cus_season_id)
        if is_movie:
            try:
                movie_inventory = self._inventory_for_input(_uuid, path)
            except MovieAssetInventoryError as exc:
                self.R = {}
                return self._movie_inventory_reply(
                    _uuid,
                    path,
                    is_anime,
                    name,
                    None,
                    exc.code,
                    info,
                    season_number=cus_season_id,
                    season_source=("explicit_user" if cus_season_id is not None else "unset"),
                )
            blocking_reasons = (
                DISC_STRUCTURE_REQUIRES_MANUAL_WORKFLOW,
                MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION,
            )
            blocking_reason = next(
                (
                    reason
                    for reason in blocking_reasons
                    if reason in movie_inventory.reason_codes
                ),
                "MOVIE_PRIMARY_SELECTION_REQUIRED",
            )
            if blocking_reason is not None:
                self.R = {}
                mixed_candidates = None
                if blocking_reason == MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION:
                    observed = getattr(self, 'last_type_candidates', {})
                    tv_observation = observed.get('tv', {})
                    tv_info = tv_observation.get('info') or {}
                    movie_options = _provider_candidate_options(
                        info,
                        entity_type='movie',
                        selected_title=str(name or ''),
                    )
                    tv_options = _provider_candidate_options(
                        tv_info,
                        entity_type='tv',
                        selected_title=str(tv_observation.get('name') or ''),
                    )
                    work_candidates = _dedupe_provider_candidates(
                        movie_options + tv_options + bangumi_candidates
                    )
                    if work_candidates:
                        mixed_candidates = (
                            partition_provider_candidates_by_presentation(
                                work_candidates
                            )
                        )
                return self._movie_inventory_reply(
                    _uuid,
                    path,
                    is_anime,
                    name,
                    movie_inventory,
                    blocking_reason,
                    info,
                    mixed_candidates=mixed_candidates,
                    season_number=cus_season_id,
                    season_source=("explicit_user" if cus_season_id is not None else "unset"),
                )
        # Inventory-only observations stop before canonical naming authority:
        # their raw candidate label is evidence for manual classification, not
        # a persisted claim about Provider locale provenance.  Every path that
        # can continue into an executable Plan still resolves here and fails
        # closed on incomplete or conflicting title evidence.
        try:
            naming_decision = _resolve_plan_naming_decision(
                name=str(name or ""),
                info=info,
                entity_type=("movie" if is_movie else "tv"),
                provider_selection=self.active_provider_selection,
                bangumi_title_selection=self.active_provider_naming_selection,
            )
        except CanonicalNamingError as exc:
            return self.error_reply(
                _uuid,
                f"[作品标题] {exc}",
                path,
                is_anime,
                is_movie,
                str(name or ""),
                cus_season_id,
            )
        name = naming_decision.display_title
        previous_tv_state = DecisionStateStore(self.decision_state_root).load_optional(str(_uuid))
        if not is_movie and previous_tv_state is not None and previous_tv_state.content_selection is not None:
            try:
                from .initial_tv_completion import prepare as prepare_tv, commit as commit_tv
                from .initial_movie_completion import RootSnapshotSource
                from .target_routing import TargetRootSnapshot
                from .plan_review import PlanReviewService
                from .default_activation import require_supported_tv_selection
                require_supported_tv_selection(previous_tv_state)
                provider_identity, provider_year = _planning_provider_identity(info=info, name=name, is_movie=False,
                    selection=self.active_provider_selection, decision_source=provider_decision_source,
                    evidence=provider_decision_evidence)
                work, linked = self._recognition_identity(provider_identity, title=name, year=provider_year, is_anime=is_anime)
                from .tv_completion_observations import TVSeasonObservation
                from .season_identity import resolve_initial_tv_season
                seasons = TVSeasonObservation.capture(work, info)
                root_source = RootSnapshotSource(lambda: TargetRootSnapshot.capture({
                    'bangumi_path': self.BANGUMI_PATH, 'movie_path': self.MOVIE_PATH,
                    'anime_path': self.ANIME_PATH, 'anime_movie_path': self.ANIME_MOVIE_PATH}))
                service = PlanReviewService(PlanStore(PLAN_PATH), PLAN_PATH.parent / 'plan_review',
                    catalog_root=PLAN_PATH.parent / 'work_catalog', decision_state_root=self.decision_state_root,
                    task_root=TASK_PATH, provider_link_catalog_root=self.provider_link_catalog_root,
                    record_root=RECORD_PATH, root_snapshot_source=root_source)
                service.series_catalog = SeriesCatalog(SERIES_CATALOG_PATH)
                from .source_basis import SourceInputProof, observe_initial_input
                source_input = observe_initial_input(service, service.plan_store.load(str(_uuid)), previous_tv_state)
                if type(source_input) is not SourceInputProof or source_input.observation_path != path:
                    raise ValueError('TV worker路径不属于原admission请求')
                from .season_identity import SeasonConfirmationRequired
                try:
                    season, season_source = resolve_initial_tv_season(seasons, work, source_input.observation_path,
                        getattr(self, 'initial_movie_request', None).requested_season, provider_link_resolution)
                except SeasonConfirmationRequired:
                    from .ova_selection import TVSeasonPending
                    from .content_associations import encode
                    return TVSeasonPending(str(_uuid), encode(dict(self.active_provider_selection)), encode(seasons.to_dict()))
                recognition = RecognitionDecisionInput(str(_uuid), work, linked, season, season_source, 'tv_episodes',
                    DecisionStateStore.fingerprint(previous_tv_state), getattr(self, 'initial_movie_request', None))
                from .special_crosswalk import observe as observe_specials
                from .season_reference_record import read as read_reference, record_path
                primary = work.primary_provider.to_dict()
                reference = self.active_season_reference or read_reference(
                    record_path(TASK_PATH, str(_uuid), primary), primary)
                special = observe_specials(primary, season, reference=reference,
                    bgm=self.bangumi_search, tmdb=self.search)
                intent = prepare_tv(service, recognition, resolution=provider_link_resolution, special=special, seasons=seasons)
                def finish_tv(_decision, projected, current):
                    verification = PlanVerifier().verify(projected)
                    current.update(name=name, is_anime=is_anime, is_movie=False, season_id=season,
                        season_number=season, season_source=season_source, workflow_status='need_confirm',
                        plan_status=verification.status.value, plan_entry_count=len(projected.entries),
                        plan_file=service.plan_store.path_for(projected.task_id).name, error=None, completed_at=None)
                    current.pop('provider_selection', None)
                    return current
                terminal = TaskTerminalMutation.prepare(TASK_PATH, str(_uuid), finish_tv)
                _, self.last_plan = commit_tv(service, intent, task_terminal=terminal)
                self.R = {}
                return PlanStatus.NEED_CONFIRM.value
            except (DecisionStateError, PlanStoreError, ProjectionAuthorityError, OSError, ValueError) as exc:
                self.R = {}
                return self.error_reply(_uuid, f'[TV完整规划] {exc}', path, is_anime, is_movie, name, cus_season_id)

        # 【Step.2】
        # 如果是电影
        if is_movie:
            raise RuntimeError('电影必须通过统一库存确认版本，不能使用旧规则映射')
        # 如果是剧集类型
        else:
            if is_anime:
                if not name:
                    logger.info('[处理任务] TMDB未搜索到!转为MyAnimeList搜索！')
                    search_result = jikan.search(
                        'anime',
                        rtpath_name,
                        page=1,
                    )
                    for i in search_result['data']:
                        if i['type'] == 'Anime':
                            data = i
                            break
                    else:
                        for i in search_result['data']:
                            if i['type'] == 'TV':
                                data = i
                                break
                        else:
                            data = search_result['data'][0]
                    titles = data['titles']
                    logger.info((f'[处理任务] MyAnimeList识别结果: {titles}'))
                else:
                    titles = None
                _WORK_PATH = self.ANIME_PATH
            else:
                titles = [{'type': 'Default', 'title': name}]
                _WORK_PATH = self.BANGUMI_PATH

            if not name:
                logger.warning(f'[处理任务] 未搜索到剧集信息, 跳过{rtpath_name}')
                return self.error_reply(
                    _uuid,
                    f'[TMDB] 未搜索到剧集信息, 跳过{rtpath_name}',
                    path,
                    is_anime,
                    is_movie,
                )

            first_data: str = info['first_air_date']
            first_year = first_data.split('-')[0]
            try:
                work_path = build_canonical_work_root(
                    resolved_target_root=_WORK_PATH,
                    decision=naming_decision,
                    year=int(first_year) if first_year.isdecimal() else None,
                )
                work_path = resolve_fresh_collision_root(
                    generated_root=work_path,
                    decision=naming_decision,
                )
            except (CanonicalNamingError, TypeError, ValueError) as exc:
                return self.error_reply(
                    _uuid, f"[作品目录] {exc}", path, is_anime, is_movie, name
                )

            if provider_link_resolution is not None:
                from . import series_registration
                provider_link_resolution, registered = series_registration.bind_resolution(
                    provider_link_resolution, SeriesCatalog(SERIES_CATALOG_PATH),
                    provider_catalog_root=self.provider_link_catalog_root)
                self.active_provider_link_resolution = provider_link_resolution
                level = provider_link_resolution.automation_level
                if registered is not None or level in {'L0', 'L1'}:
                    series_key = str(
                        provider_link_resolution.proposed_series_key or ''
                    )
                    try:
                        series_catalog = SeriesCatalog(SERIES_CATALOG_PATH)
                        existing_series = series_catalog.load(series_key)
                        if existing_series.schema_version == 1:
                            existing_series = _backfill_legacy_series_assets(
                                series_catalog, series_key
                            )
                    except SeriesCatalogError as exc:
                        self.R = {}
                        logger.error(f'[作品季度登记] {exc}')
                        return self.error_reply(
                            _uuid,
                            '[作品季度登记] 已保存的作品关系不可用，请重新匹配作品',
                            path,
                            is_anime,
                            is_movie,
                            name,
                        )
                    if existing_series.series_key != series_key:
                        self.R = {}
                        return self.error_reply(
                            _uuid,
                            '[作品季度登记] 已保存的作品关系不一致，请重新匹配作品',
                            path,
                            is_anime,
                            is_movie,
                            name,
                        )
                    work_path = existing_series.canonical_root
                    provider_series_catalog_revision = existing_series.revision
                else:
                    provider_series_catalog_revision = 0
            elif is_anime is False:
                from . import series_registration
                manual_work = WorkIdentity(status='confirmed', domain='general', title=name,
                    year=int(first_year) if first_year.isdecimal() else None, domain_source='provider_selection',
                    primary_provider=provider_identity_from_metadata(namespace='tmdb', entity_type='tv',
                        provider_id=info.get('id'), title=name,
                        year=int(first_year) if first_year.isdecimal() else None, decision_source='provider_unique'))
                manual_series_binding = series_registration.manual_tv_binding(manual_work,
                    SeriesCatalog(SERIES_CATALOG_PATH), work_path)
                work_path = Path(manual_series_binding['canonical_root'])

            season_id = self.get_season_id(
                info,
                work_path,
                path,
                titles,
            )

            season_authority = '目录识别'
            if cus_season_id is not None:
                season_id = int(cus_season_id)
                if not is_anime and not any(
                    isinstance(row, Mapping) and type(row.get('season_number')) is int
                    and row['season_number'] == season_id for row in info.get('seasons', ())
                ):
                    return self.error_reply(_uuid, '所选季度不在当前作品资料中，请重新选择季度',
                                            path, is_anime, is_movie, name)
                season_authority = (
                    '跨站映射'
                    if provider_link_resolution is not None
                    else '人工指定'
                )
            logger.info(
                f'[处理任务] 最终季号：{season_id}；来源：{season_authority}'
            )

            mapping_error = self._process_traditional(
                path, rtpath_name, work_path, season_id, name
            )

            if mapping_error:
                self.R = {}
                logger.error(f'[处理任务] 映射预检失败，未执行文件操作：{mapping_error}')
                return self.error_reply(
                    _uuid,
                    f'[映射预检] {mapping_error}',
                    path,
                    is_anime,
                    is_movie,
                    name,
                    season_id,
                )

        self._prepare_subtitle_plan(work_path)
        try:
            provider_identity, provider_year = _planning_provider_identity(info=info, name=name, is_movie=is_movie,
                selection=self.active_provider_selection, decision_source=provider_decision_source,
                evidence=provider_decision_evidence)
            presentation_groups, presentation_assets = (
                build_default_presentation_contracts(
                    self.R,
                    group_id=default_presentation_group_id(
                        "movie_continuous" if is_movie else "tv_episodes"
                    ),
                    group_kind=(
                        "movie_continuous" if is_movie else "tv_episodes"
                    ),
                    display_name=name,
                    provider_identity=provider_identity,
                    local_roles=self.active_media_roles,
                )
            )
        except ValueError as exc:
            self.R = {}
            logger.error(f"[呈现建模] {exc}")
            return self.error_reply(
                _uuid,
                f"[呈现建模] {exc}",
                path,
                is_anime,
                is_movie,
                name,
                season_id,
            )
        from .target_routing import TargetRootSnapshot
        lifecycle_roots = TargetRootSnapshot.capture({
            'bangumi_path': self.BANGUMI_PATH, 'movie_path': self.MOVIE_PATH,
            'anime_path': self.ANIME_PATH, 'anime_movie_path': self.ANIME_MOVIE_PATH})
        plan = RenamePlan.from_mapping(
            task_id=_uuid,
            source_root=path,
            mapping=self.R,
            expected_sources=_plan_expected_sources(
                path, self.R, self.active_task_source_scope
            ),
            metadata={
                "name": name,
                'target_root_snapshot': lifecycle_roots.to_dict(),
                'target_root_config_fingerprint': lifecycle_roots.fingerprint,
                "season_id": season_id,
                "season_number": (season_id if not is_movie else None),
                "season_source": (
                    "provider_crosswalk" if provider_link_resolution is not None
                    else "explicit_user" if cus_season_id is not None
                    else ("inferred_path" if not is_movie else "unset")
                ),
                "target_root": str(work_path),
                "is_anime": is_anime,
                "is_movie": is_movie,
                "tv_authority_required": bool(is_anime and not is_movie),
                "mapping_source": "rules",
                "canonical_naming_required": True,
                "canonical_naming_decision": naming_decision.to_dict(),
                **({
                    "series_key": str(
                        provider_link_resolution.proposed_series_key or ""
                    ),
                    "series_catalog_revision": int(
                        provider_series_catalog_revision or 0
                    ),
                    "canonical_work_root": str(work_path),
                } if provider_link_resolution is not None else {}),
                **({'series_key': manual_series_binding['series_key'],
                    'series_catalog_revision': manual_series_binding['catalog_revision'],
                    'canonical_work_root': str(work_path)} if manual_series_binding is not None else {}),
                "provider_candidates": ({
                    "work": selected_provider_candidates
                } if selected_provider_candidates else ({
                    "work": [
                        dict(candidate) for candidate in provider_batch.candidates
                    ]
                } if provider_batch is not None else ({
                    "work": fuse_provider_candidates(
                        rtpath_name,
                        _provider_candidate_options(
                            info,
                            entity_type=("movie" if is_movie else "tv"),
                            selected_title=str(name or ""),
                        ) + bangumi_candidates,
                        year=year or None,
                        animation=bool(is_anime),
                    )
                } if bangumi_candidates else {}))),
            },
            subtitle_contracts=self.subtitle_contracts,
            presentation_groups=presentation_groups,
            presentation_assets=presentation_assets,
            series_scope=(
                (
                    SeriesScopeIdentity.from_series_key(
                        provider_link_resolution.proposed_series_key
                    )
                    if provider_link_resolution is not None
                    and provider_link_resolution.proposed_series_key
                    else SeriesScopeIdentity.from_provider(
                        provider_identity.namespace or "",
                        provider_identity.entity_type or "",
                        provider_identity.provider_id or "",
                    )
                )
                if not is_movie and provider_identity.provider_id is not None
                else None
            ),
            execution_policy=(
                ExecutionPolicy(
                    target_root=work_path,
                    dry_run=is_dry_run_enabled(),
                )
                if cm.get_config("mode") == "链接" else None
            ),
        )
        plan = associate_resolved_extra_subtitles(plan)
        if self.active_linked_provider_identities:
            plan = replace(
                plan,
                linked_provider_identities=self.active_linked_provider_identities,
            )
        if not is_movie and provider_link_resolution is None:
            plan = replace(
                plan,
                entries=tuple(
                    replace(entry, requires_review=True)
                    if (
                        entry.presentation_asset is not None
                        and entry.presentation_asset.asset_role.kind
                        == "content_version"
                    )
                    else entry
                    for entry in plan.entries
                ),
            )
        provider_id = info.get("id")
        if (
            not is_movie
            and self.active_provider_selection
            and provider_link_resolution is not None
            and provider_link_resolution.tmdb_tv_id
        ):
            provider_special_observation = (
                provider_special.freeze_provider_special_observation(
                    provider_namespace="tmdb",
                    provider_entity_type="tv",
                    provider_id=str(provider_link_resolution.tmdb_tv_id),
                    resolver=getattr(self.search, "get_season_info", None),
                    enabled=True,
                )
            )
            plan = provider_special.project_provider_special_evidence(
                plan,
                provider_special_observation,
                selected_subject_id=str(
                    self.active_provider_selection.get("provider_id") or ""
                ),
                provider_link_resolution=provider_link_resolution,
            )
            plan = self._apply_selected_bangumi_title_authority(plan)
        source_episode_projection = None
        if provider_link_resolution is not None:
            plan, source_episode_projection = freeze_source_episode_projection(
                plan,
                provider_link_resolution,
                selected_subject_id=str(
                    (self.active_provider_selection or {}).get("provider_id") or ""
                ),
            )
        # RenamePlan freezes nested metadata into read-only mappings.  Task
        # records are JSON, so take the candidates from the Plan's canonical
        # serialized projection instead of shallow-copying the frozen view.
        serialized_provider_candidates = dict(
            plan.to_dict().get("metadata", {}).get("provider_candidates") or {}
        )
        self.last_plan = plan
        try:
            decision_store = DecisionStateStore(self.decision_state_root)
            def transform_provider_state(state):
                staged = stage_provider_link_series_decision(
                    state,
                    provider_link_resolution,
                    canonical_root=work_path,
                    catalog_revision=provider_series_catalog_revision,
                )
                if source_episode_projection is not None:
                    staged = replace(
                        staged,
                        source_episode_projection=dict(source_episode_projection),
                        source_episode_projection_decision={
                            "decision": "automatic_unique",
                            "projection_fingerprint": str(
                                source_episode_projection.get(
                                    "projection_fingerprint"
                                ) or ""
                            ),
                        },
                    )
                if isinstance(
                    provider_link_resolution.subject_observations, Mapping
                ):
                    active_scope = getattr(
                        self, 'active_task_source_scope', None
                    )
                    if not isinstance(active_scope, TaskSourceScope):
                        raise ValueError('TV 规划缺少冻结的任务来源范围')
                    staged = replace(
                        staged,
                        tv_planning_authority=(
                            _build_process_tv_planning_authority(
                                state=staged,
                                plan=plan,
                                resolution=provider_link_resolution,
                                task_source_scope=active_scope,
                            )
                        ),
                    )
                return staged

            transform = (
                transform_provider_state
                if provider_link_resolution is not None
                else (lambda state: replace(state, series_binding=manual_series_binding,
                    catalog_link_intent=None, layout_mode='tv_episodes')) if manual_series_binding is not None
                else None
            )


            season_source = (
                "provider_crosswalk" if provider_link_resolution is not None
                else "explicit_user" if cus_season_id is not None
                else ("inferred_path" if not is_movie else "unset")
            )
            recognized_work, recognized_linked = self._recognition_identity(
                provider_identity,
                title=name,
                year=provider_year,
                is_anime=is_anime,
                prefer_selected_subject_primary=(
                    path.is_file()
                    and bool(plan.metadata.get("provider_special_candidates"))
                ),
            )
            recognition = RecognitionDecisionInput(
                task_id=str(_uuid),
                work_identity=recognized_work,
                linked_provider_identities=recognized_linked,
                season_number=(season_id if not is_movie else None),
                season_source=season_source,
                layout_mode=("movie_versions" if is_movie else "tv_episodes"),
            )

            plan, recognition = adapt_new_recognition_observation(decision_store, recognition, plan)
            inventory_replay_guard = None
            if not is_movie:
                from .plan_review import PlanReviewError, PlanReviewService
                retry_service = PlanReviewService(PlanStore(PLAN_PATH), PLAN_PATH.parent / 'plan_review',
                    catalog_root=PLAN_PATH.parent / 'work_catalog',
                    decision_state_root=self.decision_state_root, task_root=TASK_PATH)
                try:
                    replayed = retry_service.retry_tv_inventory(plan, recognition)
                except PlanReviewError as exc:
                    raise ValueError(str(exc)) from exc
                if replayed is not None:
                    plan, inventory_replay_guard = replayed
                    if provider_link_resolution is not None:
                        plan = provider_special.project_provider_special_evidence(
                            plan, provider_special_observation,
                            selected_subject_id=str((self.active_provider_selection or {}).get('provider_id') or ''),
                            provider_link_resolution=provider_link_resolution)
                        plan, source_episode_projection = freeze_source_episode_projection(
                            plan, provider_link_resolution,
                            selected_subject_id=str((self.active_provider_selection or {}).get('provider_id') or ''))
            if provider_link_resolution is not None:
                plan = _project_provider_episode_ownership(
                    plan, provider_link_resolution, recognized_work,
                    selected_subject_id=str((self.active_provider_selection or {}).get('provider_id') or ''))

            def finalize_task(_decision, projected, current):
                result = PlanVerifier().verify(projected)
                issue_text = "；".join(
                    issue.message for issue in result.issues
                )
                projection_review = any(
                    issue.code == "source_episode_projection_decision_required"
                    for issue in result.issues
                )
                workflow = (
                    PlanStatus.NEED_CONFIRM.value
                    if result.status == PlanStatus.ACCEPTED or projection_review
                    else result.status.value
                )
                current.update({
                    "path": str(path),
                    "is_anime": is_anime,
                    "is_movie": is_movie,
                    "name": name,
                    "season_id": season_id,
                    "season_number": (season_id if not is_movie else None),
                    "season_source": season_source,
                    "uuid": str(_uuid),
                    "error": (
                        f"[计划校验] {issue_text}"
                        if result.status == PlanStatus.INVALID
                        and not projection_review else None
                    ),
                    "use_ai": use_ai,
                    "dry_run": is_dry_run_enabled(),
                    "workflow_status": workflow,
                    "plan_status": result.status.value,
                    "plan_entry_count": len(projected.entries),
                    "plan_file": PlanStore(PLAN_PATH).path_for(
                        projected.task_id
                    ).name,
                    "provider_candidates": serialized_provider_candidates,
                    "completed_at": None,
                })
                current.pop("provider_selection", None)
                return current

            task_terminal = TaskTerminalMutation.prepare(
                TASK_PATH, str(_uuid), finalize_task
            )
            _decision, plan = commit_recognition_projection(
                decision_store,
                PlanStore(PLAN_PATH),
                recognition,
                plan,
                state_transform=transform,
                task_terminal=task_terminal,
                inventory_replay_guard=inventory_replay_guard,
            )
            self.last_plan = plan
            plan_path = PlanStore(PLAN_PATH).path_for(plan.task_id)
        except (DecisionStateError, PlanStoreError, ProjectionAuthorityError, OSError, ValueError) as exc:
            self.R = {}
            logger.error(f"[计划持久化] {exc}")
            return self.error_reply(
                _uuid,
                f"[计划持久化] {exc}",
                path,
                is_anime,
                is_movie,
                name,
                season_id,
            )

        verification = PlanVerifier().verify(plan)
        if verification.status != PlanStatus.ACCEPTED:
            issue_text = "；".join(issue.message for issue in verification.issues)
            projection_review = any(
                issue.code == "source_episode_projection_decision_required"
                for issue in verification.issues
            )
            persisted_workflow = (
                PlanStatus.NEED_CONFIRM.value
                if projection_review else verification.status.value
            )
            self.R = {}
            task_data = {
                "path": str(path),
                "is_anime": is_anime,
                "is_movie": is_movie,
                "name": name,
                "season_id": season_id,
                "season_number": (season_id if not is_movie else None),
                "season_source": (
                    "provider_crosswalk" if provider_link_resolution is not None
                    else "explicit_user" if cus_season_id is not None
                    else ("inferred_path" if not is_movie else "unset")
                ),
                "uuid": str(_uuid),
                "error": (
                    f"[计划校验] {issue_text}"
                    if verification.status == PlanStatus.INVALID
                    and not projection_review
                    else None
                ),
                "use_ai": use_ai,
                "provider_selection": self.active_provider_selection,
                "dry_run": is_dry_run_enabled(),
                "workflow_status": persisted_workflow,
                "plan_status": verification.status.value,
                "plan_entry_count": len(plan.entries),
                "plan_file": plan_path.name,
                "provider_candidates": serialized_provider_candidates,
            }
            task_data.pop("provider_selection", None)
            task_data = read_task_at(TASK_PATH, str(_uuid))
            if verification.status == PlanStatus.NEED_CONFIRM or projection_review:
                logger.warning(
                    "[计划校验] 计划已暂停并持久化，等待人工确认："
                    f"{_uuid}"
                )
                return PlanStatus.NEED_CONFIRM.value
            logger.error(
                f'[计划校验] status={verification.status.value}；{issue_text}'
            )
            return task_data["error"]

        # A structurally valid plan is not the same as an operator-approved
        # plan.  Persist every accepted plan and stop at the review boundary;
        # only PlanExecutionService may invoke Trans after a version-bound,
        # all-entry approval and a second explicit execution confirmation.
        task_path = TASK_PATH / f"{_uuid}.json"
        task_data = {
            "path": str(path),
            "is_anime": is_anime,
            "is_movie": is_movie,
            "name": name,
            "season_id": season_id,
            "season_number": (season_id if not is_movie else None),
            "season_source": (
                "provider_crosswalk" if provider_link_resolution is not None
                else "explicit_user" if cus_season_id is not None
                else ("inferred_path" if not is_movie else "unset")
            ),
            "uuid": str(_uuid),
            "error": None,
            "use_ai": use_ai,
            "provider_selection": self.active_provider_selection,
            "dry_run": is_dry_run_enabled(),
            "plan_status": verification.status.value,
            "plan_entry_count": len(plan.entries),
            "provider_candidates": serialized_provider_candidates,
            "plan_file": plan_path.name,
            "workflow_status": PlanStatus.NEED_CONFIRM.value,
        }
        self.R = {}
        task_data.pop("provider_selection", None)
        task_data = read_task_at(TASK_PATH, str(_uuid))
        logger.info(
            "[计划审核] 结构校验已通过，整理计划已暂停并等待人工批准："
            f"{_uuid}"
        )
        return PlanStatus.NEED_CONFIRM.value

    def _process_traditional(
        self,
        path: Path,
        rtpath_name: str,
        work_path: Path,
        season_id: int,
        target_series_title: Optional[str] = None,
    ):
        """传统处理方式"""
        classifications = {}
        self.active_media_roles = {}

        def process_batch(items):
            batch = [
                item for item in items
                if item.is_file()
                and _source_matches_task_scope(
                    item, getattr(self, "active_task_source_scope", None)
                )
            ]
            preclassifications = {
                item: preclassify_media(
                    item_path=item,
                    candidate_name=item.name,
                    season_id=season_id,
                    extra_context_name=build_semantic_context(item.name, rtpath_name),
                )
                for item in batch
                if item.suffix.lower() in VIDEO_SUFFIX
            }
            main_candidates = [
                item
                for item, preclassification in preclassifications.items()
                if preclassification.kind == PreclassificationKind.MAIN_CANDIDATE
            ]
            secondary_sequence_hints = build_secondary_sequence_hints(
                main_candidates,
                rtpath_name,
            )
            try:
                episode_hints = build_episode_hints(main_candidates, rtpath_name)
            except EpisodeInferenceError as exc:
                return str(exc)
            resolution = getattr(self, "active_provider_link_resolution", None)
            if resolution is not None and resolution.mapping_shape == "many_subjects_one_season":
                selected_subject_id = str(
                    (self.active_provider_selection or {}).get("provider_id") or ""
                )
                selected_range = next(
                    (
                        row for row in resolution.derived_episode_ranges
                        if str(row.get("bangumi_subject_id") or "")
                        == selected_subject_id
                    ),
                    None,
                )
                if isinstance(selected_range, Mapping):
                    start = int(selected_range["episode_start"])
                    end = int(selected_range["episode_end"])
                    for item in main_candidates:
                        if item in episode_hints:
                            continue
                        numeric_tokens = tuple(
                            int(value)
                            for value in re.findall(r"\[(\d{1,4})\]", item.stem)
                        )
                        candidates = tuple(
                            value for value in numeric_tokens if start <= value <= end
                        )
                        if len(candidates) == 1:
                            episode_hints[item] = candidates[0]

            for item in batch:
                classification = self.process_sub(
                    rtpath_name,
                    None,
                    item,
                    work_path,
                    season_id,
                    episode_hint=episode_hints.get(item),
                    semantic_context=build_semantic_context(
                        item.name,
                        rtpath_name,
                    ),
                    secondary_bonus_episode=secondary_sequence_hints.get(item),
                    target_series_title=target_series_title,
                    source_root=path if path.is_dir() else path.parent,
                )
                if classification is not None:
                    classifications[item] = classification
                    self.active_media_roles[item] = (
                        classification.local_role.value
                    )

            videos = [
                item for item in batch
                if item.suffix.lower() in VIDEO_SUFFIX and item in self.R
            ]
            # 规则视频映射完成后，字幕关联和标签解析统一交由 S0/S1 层处理，
            # 避免按区分大小写的后缀直接复制字幕而绕过结构化审核。
            return None

        if path.is_file():
            logger.info(f"[处理任务] 开始对 [单文件] {path.name}处理")
            batch_error = process_batch([path])
            if batch_error:
                return batch_error
        else:
            logger.info(f"[处理任务] 开始对 [文件夹] {path.name}处理")
            # Reuse the bounded, link-rejecting inventory for deep auxiliary
            # files only. Video discovery retains the existing two-level scope.
            safe_inventory = inventory_movie_assets(path)
            root_items = [item for item in path.iterdir() if item.is_file()]
            batch_error = process_batch(root_items)
            if batch_error:
                return batch_error
            for item_path in path.iterdir():
                logger.info(f"[处理任务] 处理嵌套文件夹 {item_path.name}")
                if item_path.is_dir():
                    batch_error = process_batch(list(item_path.iterdir()))
                    if batch_error:
                        return batch_error

            nested_auxiliaries = [
                path / Path(entry.relative_path)
                for entry in safe_inventory.entries
                if entry.size_bytes is not None
                and entry.entry_type != "video"
                and len(Path(entry.relative_path).parts) > 2
            ]
            batch_error = process_batch(nested_auxiliaries)
            if batch_error:
                return batch_error

        main_video_mapping = {
            source: classification.episode_number
            for source, classification in classifications.items()
            if classification.local_role == LocalRole.MAIN_EPISODE
            and source.suffix.lower() in VIDEO_SUFFIX
            and classification.episode_number is not None
        }
        active_resolution = getattr(
            self, "active_provider_link_resolution", None
        )
        if not (
            active_resolution is not None
            and active_resolution.mapping_shape == "many_subjects_one_season"
        ):
            try:
                validate_episode_mapping(main_video_mapping)
            except EpisodeInferenceError as exc:
                return str(exc)

        duplicate_targets = {}
        for source, target in self.R.items():
            if source.suffix.lower() not in VIDEO_SUFFIX:
                continue
            duplicate_targets.setdefault(target, []).append(source)
        for target, sources in duplicate_targets.items():
            if len(sources) < 2:
                continue
            for source in sorted(sources, key=lambda item: str(item).casefold()):
                token = hashlib.sha256(
                    source.name.casefold().encode("utf-8")
                ).hexdigest()[:10]
                self.R[source] = target.with_name(
                    f"{target.stem} [version-{token}]{target.suffix}"
                )

        self._prepare_subtitle_plan(work_path)
        target_sources = {}
        for source, target in self.R.items():
            previous = target_sources.get(target)
            if previous is not None and previous != source:
                return f'目标路径冲突：{previous.name} 与 {source.name} -> {target}'
            target_sources[target] = source
        return None

    def _prepare_subtitle_plan(self, work_path: Path) -> None:
        """Build S0/S1 decisions from the confirmed source->target video map."""

        video_mappings = tuple(
            VideoPathMapping(source, target)
            for source, target in self.R.items()
            if source.suffix.lower() in VIDEO_SUFFIX
        )
        subtitle_sources = tuple(
            source for source in self.R
            if _normalized_suffix(source) in SUBTITLE_SUFFIXES
        )
        decisions = build_subtitle_decisions(subtitle_sources, video_mappings)
        contracts = {}
        for decision in decisions:
            contracts[decision.source_subtitle] = decision_to_plan_contract(decision)
            if decision.target_preview is not None:
                self.R[decision.source_subtitle] = decision.target_preview
            else:
                token = hashlib.sha256(
                    str(decision.source_subtitle).encode("utf-8")
                ).hexdigest()[:10]
                suffix = _normalized_suffix(decision.source_subtitle)
                if decision.associated_video_target is not None:
                    video_target = decision.associated_video_target
                    self.R[decision.source_subtitle] = video_target.with_name(
                        f"{video_target.stem}.subtitle-review-{token}{suffix}"
                    )
                else:
                    normalized_name = unicodedata.normalize(
                        "NFKC", decision.source_subtitle.name
                    )
                    self.R[decision.source_subtitle] = (
                        work_path / "ManualReview" / "subtitles"
                        / f"{token}-{normalized_name}"
                    )
            logger.info(
                "[字幕计划] "
                f"{decision.source_subtitle.name}；association="
                f"{decision.association.resolution_state.value}；parse="
                f"{decision.label.resolution_state.value}；target="
                f"{decision.target_resolution.value}；只生成整理计划，未执行"
            )
        self.subtitle_contracts = contracts

    def error_reply(
        self,
        _uuid: str,
        error: str,
        path: Path,
        is_anime: Optional[bool] = None,
        is_movie: Optional[bool] = None,
        name: Optional[str] = None,
        season_id: Optional[int] = None,
        provider_candidates: Optional[Dict[str, List[Dict[str, object]]]] = None,
    ):
        task_path = TASK_PATH / f'{_uuid}.json'
        task_data = {
            'path': str(path),
            'uuid': str(_uuid),
            'error': error,
            'workflow_status': 'need_confirm',
            'execution_blocked': True,
            'queue_position': None,
            'completed_at': None,
        }
        # Failed observations cannot erase previously confirmed identity.
        task_data.update({
            key: value for key, value in {
                'is_anime': is_anime, 'is_movie': is_movie,
                'name': name, 'season_id': season_id,
            }.items() if value is not None
        })
        if provider_candidates:
            task_data['provider_candidates'] = provider_candidates
            task_data['type_match_state'] = 'mismatch'
        if getattr(self, 'active_provider_link_failure', None) is not None:
            task_data['provider_selection_failure'] = dict(
                self.active_provider_link_failure
            )
        merge_task_at(TASK_PATH, str(_uuid), task_data)
        return error

    def _inventory_for_input(self, task_id, path):
        """现代任务消费原清单；真正的无策略旧任务保留首次观察。"""
        from .content_policy import _scope_binding
        from .default_activation import service_for_worker
        from .source_basis import SourceInputProof, observe_initial_input
        from .movie_assets import inventory_from_source_basis
        service = service_for_worker(self)
        state = service.decision_state_store.load_optional(task_id)
        binding = _scope_binding(service, task_id)
        scope = self.active_task_source_scope
        if (binding.policy is None and (state is None or state.content_policy is None)
                and (scope is None or scope.content_policy is None)):
            return inventory_movie_assets(path)
        if state is None:
            raise ValueError('现代来源缺少原决定清单，不能退回重新扫描')
        plan = service.plan_store.load(task_id)
        proof = observe_initial_input(service, plan, state)
        if (type(proof) is not SourceInputProof or path != proof.observation_path
                or self.active_task_source_scope != binding.scope):
            raise ValueError('初始清单与实际输入或当前范围不一致')
        return inventory_from_source_basis(state.initial_source_basis)

    def _movie_inventory_reply(
        self,
        _uuid: str,
        path: Path,
        is_anime: Optional[bool],
        name: Optional[str],
        inventory: Optional[MovieAssetInventory],
        reason_code: str,
        provider_info: Optional[Dict] = None,
        mixed_candidates: Optional[Dict[str, List[Dict[str, object]]]] = None,
        season_number: Optional[int] = None,
        season_source: str = "unset",
    ) -> str:
        """Persist a non-executable M0 observation and stop before Plan v3."""

        error = ('[作品选择] 请确认作品后继续生成未批准整理计划' if
            reason_code == 'WORK_IDENTITY_REQUIRES_SELECTION' else (
            f'[电影资产清单] {reason_code}；复杂呈现形式需要后续人工建模，'
            '当前未生成可执行整理计划'
        ))
        active_selection = dict(
            getattr(self, 'active_provider_selection', None) or {}
        )
        selected_entity_type = str(
            active_selection.get('entity_type') or ''
        ).casefold()
        provider_entity_type = (
            selected_entity_type
            if selected_entity_type in {'movie', 'tv'}
            else 'tv'
            if (provider_info or {}).get('first_air_date') is not None
            else 'movie'
        )
        release_date = str(
            (provider_info or {}).get(
                'release_date'
                if provider_entity_type == 'movie'
                else 'first_air_date'
            )
            or ''
        )
        release_year = (
            int(release_date[:4])
            if len(release_date) >= 4 and release_date[:4].isdigit()
            else None
        )
        movie_key = 'anime_movie_path' if is_anime else 'movie_path'
        tv_key = 'anime_path' if is_anime else 'bangumi_path'
        movie_root = (
            getattr(self, 'ANIME_MOVIE_PATH', None)
            if is_anime else getattr(self, 'MOVIE_PATH', None)
        )
        tv_root = (
            getattr(self, 'ANIME_PATH', None)
            if is_anime else getattr(self, 'BANGUMI_PATH', None)
        )
        movie_root = Path(
            movie_root if movie_root is not None else cm.get_config(movie_key)
        )
        tv_root = Path(
            tv_root if tv_root is not None else cm.get_config(tv_key)
        )
        # The initial inventory draft needs one placeholder root, but it must
        # never become a routing authority.  The final review action captures
        # all four live roots and resolves the chosen work domain/layout.
        allowed_target_root = movie_root
        target_name = (
            f'{name} ({release_year})' if name and release_year else str(name or '待确认电影')
        )
        layout_unresolved = (
            reason_code in {MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION,
                            'WORK_IDENTITY_REQUIRES_SELECTION'}
        )
        selected_task_media_kind = (
            True if selected_entity_type == "movie"
            else False if selected_entity_type == "tv"
            else getattr(self, "active_task_media_kind", None)
        )
        task_path = TASK_PATH / f'{_uuid}.json'
        proposed_work_key = None
        if task_path.exists():
            try:
                previous = json.loads(task_path.read_text(encoding='UTF-8'))
                proposed_work_key = previous.get('proposed_work_key')
            except (OSError, json.JSONDecodeError, AttributeError):
                proposed_work_key = None
        if not isinstance(proposed_work_key, str) or not proposed_work_key.startswith('work-'):
            proposed_work_key = f'work-{uuid.uuid4()}'
        try:
            root_snapshot = TargetRootSnapshot.capture({
                key: cm.get_config(key)
                for key in (
                    'bangumi_path', 'movie_path', 'anime_path', 'anime_movie_path'
                )
            })
            root_snapshot_data = root_snapshot.to_dict()
            root_snapshot_fingerprint = root_snapshot.fingerprint
        except TargetRoutingError:
            root_snapshot_data = {}
            root_snapshot_fingerprint = ""
        inventory_provider = (provider_info or {}).get('_bar_primary_provider') or {}
        same_selection = bool(active_selection.get('provider_id')) and (
            str(active_selection.get('namespace') or '').casefold(),
            selected_entity_type, str(active_selection.get('provider_id')),
        ) == (
            str(inventory_provider.get('namespace') or 'tmdb').casefold(),
            str(inventory_provider.get('entity_type') or provider_entity_type).casefold(),
            str((provider_info or {}).get('id') or ''),
        )
        task_data = {
            'path': str(path),
            'is_anime': is_anime,
            'is_movie': (
                selected_task_media_kind
                if selected_task_media_kind is not None
                else None if layout_unresolved else True
            ),
            'name': name,
            # A multi-video inventory has not established a TV season unless
            # the operator supplied one with this task.  Season 00 is a real
            # decision, never the generic M0 sentinel.
            'season_id': season_number,
            'season_number': season_number,
            'season_source': season_source,
            'uuid': str(_uuid),
            'error': error,
            'workflow_status': 'invalid',
            'plan_status': 'inventory_only',
            'plan_entry_count': 0,
            'execution_blocked': True,
            'inventory_status': 'requires_manual_workflow',
            'movie_inventory': inventory.to_dict() if inventory else None,
            'movie_inventory_reason': reason_code,
            'draft_target_root': str(allowed_target_root),
            'draft_initial_target_root': str(movie_root / target_name),
            'draft_suggested_roots': {
                'movie_continuous': str(movie_root),
                'tv_episodes': str(tv_root),
            },
            'proposed_work_key': proposed_work_key,
            'draft_root_snapshot': root_snapshot_data,
            'draft_root_config_fingerprint': root_snapshot_fingerprint,
            'provider_identity': None if (
                layout_unresolved
                and not getattr(self, 'active_provider_selection', None)
            ) else {
                'namespace': str(((provider_info or {}).get('_bar_primary_provider') or {}).get('namespace') or 'tmdb'),
                'entity_type': str(((provider_info or {}).get('_bar_primary_provider') or {}).get('entity_type') or provider_entity_type),
                'provider_id': str((provider_info or {}).get('id') or ''),
                'title': name,
                'year': release_year,
                'original_title': str(
                    active_selection.get('original_title')
                    if same_selection
                    else (provider_info or {}).get(
                        'original_title'
                        if provider_entity_type == 'movie'
                        else 'original_name'
                    )
                    or ''
                ) or None,
                'poster_url': str(
                    active_selection.get('poster_url')
                    if same_selection
                    else ''
                ) or None,
                'source_url': str(
                    active_selection.get('source_url')
                    if same_selection
                    else ''
                ) or None,
                'snapshot_at': str(
                    active_selection.get('snapshot_at') or ''
                ) or None,
            },
            'provider_candidates': mixed_candidates or {},
            'linked_provider_identities': [
                item.to_dict()
                for item in getattr(self, 'active_linked_provider_identities', ())
            ],
        }
        merged = merge_task_at(TASK_PATH, str(_uuid), task_data)
        inventory_persisted = False
        if (
            inventory is not None
            and isinstance(task_data.get('provider_identity'), dict)
        ):
            try:
                raw_identity = dict(task_data["provider_identity"])
                explicit_provider = bool(
                    getattr(self, "active_provider_selection", None)
                )
                mapped_from_bangumi = (
                    str(active_selection.get("namespace") or "").casefold()
                    == "bangumi"
                    and str(raw_identity.get("entity_type") or "") == "tv"
                )
                inventory_provider = provider_identity_from_metadata(
                    namespace=str(raw_identity.get("namespace") or "tmdb"),
                    entity_type=str(raw_identity.get("entity_type") or "movie"),
                    provider_id=raw_identity.get("provider_id"),
                    title=str(raw_identity.get("title") or name),
                    year=raw_identity.get("year"),
                    decision_source=(
                        "provider_crosswalk"
                        if mapped_from_bangumi
                        else "explicit_user"
                        if explicit_provider
                        else "provider_unique"
                    ),
                    evidence=((
                        "用户选择 Bangumi 条目后由冻结跨站证据锁定 TMDB 电视剧 ID"
                        if mapped_from_bangumi
                        else "用户在候选卡片中锁定了资料来源与稳定作品 ID"
                        if explicit_provider
                        else "全部计划来源已结束，且只有一个合格的资料候选"
                    ),),
                    original_title=raw_identity.get("original_title"),
                    poster_url=raw_identity.get("poster_url"),
                    source_url=raw_identity.get("source_url"),
                    snapshot_at=raw_identity.get("snapshot_at"),
                )
                recognized_work, recognized_linked = self._recognition_identity(
                    inventory_provider,
                    title=str(raw_identity.get("title") or name),
                    year=raw_identity.get("year"),
                    is_anime=is_anime,
                )
                recognition = RecognitionDecisionInput(
                    task_id=str(_uuid),
                    work_identity=recognized_work,
                    linked_provider_identities=recognized_linked,
                    season_number=season_number,
                    season_source=season_source,
                    layout_mode=None,
                    initial_movie_request=getattr(self, 'initial_movie_request', None),
                )

                from .plan_review import PlanReviewService
                review_service = PlanReviewService(PlanStore(PLAN_PATH), PLAN_PATH.parent / 'plan_review',
                    catalog_root=PLAN_PATH.parent / 'work_catalog',
                    decision_state_root=self.decision_state_root, task_root=TASK_PATH)
                previous_inventory_state = review_service.decision_state_store.load_optional(str(_uuid))
                from .initial_movie_selection import pending_movie_selection
                current_inventory = (review_service.plan_store.load(str(_uuid))
                    if previous_inventory_state is not None and previous_inventory_state.content_selection is not None
                    else None)
                initial_selection = pending_movie_selection(current_inventory, previous_inventory_state)
                # Both first selection and re-selection before content completion
                # consume the current workspace through the same canonical command.
                draft = (current_inventory if initial_selection
                    else build_inventory_review_draft(merged, dry_run=is_dry_run_enabled()))
                draft, recognition = adapt_new_recognition_observation(
                    review_service.decision_state_store, recognition, draft)
                had_movie_choice = previous_inventory_state is not None and previous_inventory_state.movie_version_selection is not None
                previous_plan_fingerprint = (review_service.plan_store.fingerprint(review_service.plan_store.load(str(_uuid)))
                    if had_movie_choice else None)
                replayed = None if initial_selection else review_service.retry_movie_inventory(draft, recognition)
                if replayed is not None:
                    draft = replayed
                    recognition = replace(recognition, layout_mode='movie_versions')

                def transform_inventory(state):
                    if not had_movie_choice:
                        return state
                    if (state.revision != previous_inventory_state.revision
                            or review_service.plan_store.fingerprint(review_service.plan_store.load(str(_uuid))) != previous_plan_fingerprint):
                        raise ValueError('电影任务已变化，请重新核查后重试')
                    # A fresh manual-selection draft cannot inherit movie choices
                    # from a different inventory or work. Completion freezes anew.
                    changes = dict(movie_version_selection=None, asset_operation=None,
                        completed_rebuild_observation=None,
                        catalog_link_intent=None, recovery_state=None, execution_failure=None)
                    if replayed is not None:
                        key = str(draft.metadata.get('catalog_work_key') or draft.metadata.get('proposed_work_key') or '')
                        changes.update(movie_version_selection=review_service.movie_selection_for_completion(draft),
                            catalog_link_intent={'work_key': key, 'catalog_revision': int(draft.metadata.get('catalog_revision') or 0)},
                            recovery_state='healthy')
                    else:
                        changes.update(manual_role_decisions={}, provider_special_decisions={})
                    return replace(state, **changes)

                def finalize_inventory_task(_decision, projected, current):
                    verification = PlanVerifier().verify(projected)
                    current.update({
                        "workflow_status": "need_confirm" if replayed is not None or reason_code == 'MOVIE_PRIMARY_SELECTION_REQUIRED' else "invalid",
                        "plan_status": verification.status.value,
                        "plan_entry_count": len(projected.entries),
                        "plan_file": PlanStore(PLAN_PATH).path_for(
                            projected.task_id
                        ).name,
                        "inventory_draft_created": True,
                    })
                    if reason_code == 'MOVIE_PRIMARY_SELECTION_REQUIRED':
                        current['error'] = None
                    return current

                terminal = TaskTerminalMutation.prepare(TASK_PATH, str(_uuid), finalize_inventory_task)
                if initial_selection:
                    from .initial_movie_selection import prepare as prepare_initial_selection, commit as commit_initial_selection
                    intent = prepare_initial_selection(review_service, recognition)
                    _decision, draft = commit_initial_selection(review_service, intent, task_terminal=terminal)
                else:
                    _decision, draft = commit_recognition_projection(
                        DecisionStateStore(self.decision_state_root),
                        PlanStore(PLAN_PATH),
                        recognition,
                        draft,
                        state_transform=transform_inventory,
                        task_terminal=terminal,
                    )
                self.last_plan = draft
                inventory_persisted = True
                if replayed is not None:
                    return PlanStatus.NEED_CONFIRM.value
            except (DecisionStateError, PlanStoreError, ValueError, OSError) as exc:
                logger.error(f'[库存草案持久化] {exc}')
        if reason_code == 'MOVIE_PRIMARY_SELECTION_REQUIRED' and inventory_persisted:
            logger.info('[电影正片选择] 库存草案已保存，请选择正片并确认版本；尚不可执行')
            return PlanStatus.NEED_CONFIRM.value
        logger.warning(error)
        return error
