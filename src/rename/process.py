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
from .plan_store import PlanStore, PlanStoreError
from .decision_state import (
    DecisionStateError,
    DecisionStateStore,
)
from .plan_verifier import PlanVerifier
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
    RecognitionDecisionInput,
    TaskTerminalMutation,
    commit_recognition_projection,
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
from .get_info import Search, TMDBRequestError
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
    decide_provider_candidates,
    fuse_provider_candidates,
)
from .provider_link_catalog import ProviderLinkCatalog, ProviderLinkCatalogError
from .provider_link_decision import (
    freeze_provider_subject_boundary_edges,
    freeze_source_episode_projection,
    stage_provider_link_series_decision,
)
from .provider_link_resolver import (
    ProviderLinkResolution,
    collect_subject_observations,
    resolve_provider_link,
    subject_is_explicit_no_match,
)
from .series_catalog import SeriesCatalog, SeriesCatalogError
from .asset_collision import (
    AssetCollisionError,
    build_reuse_resolution,
    fingerprint_file,
)
from .font_archive_contract import (
    FontArchiveContractError,
    canonical_font_archive_target,
    is_font_archive_candidate,
    is_supported_font_archive,
)
from ..utils.path import (
    DECISION_STATE_PATH,
    PLAN_PATH,
    PROVIDER_LINK_CATALOG_PATH,
    RECORD_PATH,
    SERIES_CATALOG_PATH,
    TASK_PATH,
)
from .ai_processor import AIProcessor
from ..config.config_manager import cm
from ..ai.models import AIAnalysisResult
from .utils import (
    JELLYFIN_EXTRAS_DIR,
    VIDEO_SUFFIX,
    IGNORE_SUFFIX,
    is_font_archive,
    is_ignored_asset_directory,
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

SUBTITLE_SUFFIXES = {'.ass', '.ssa', '.srt', '.vtt', '.sup'}


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
    primary = {
        "namespace": "tmdb",
        "entity_type": entity_type,
        "provider_id": str(info.get("id") or ""),
    }
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
                if is_ignored_asset_directory(item):
                    continue
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
    try:
        resolved = Path(path).resolve(strict=True)
        stat_result = resolved.stat()
    except OSError as exc:
        raise TaskBatchScopeError("task source在扫描前不可读取") from exc
    location_key = str(resolved).casefold()
    frozen = next(
        (
            item for item in task_source_scope.source_identities
            if item.location_key == location_key
        ),
        None,
    )
    if frozen is None:
        return False
    current = (
        f"device:{int(stat_result.st_dev)}:inode:{int(stat_result.st_ino)}",
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )
    if current != (frozen.physical_key, frozen.size, frozen.mtime_ns):
        raise TaskBatchScopeError("task source identity在扫描前发生漂移")
    return True


def _backfill_legacy_series_assets(catalog: SeriesCatalog, series_key: str):
    """Upgrade one legacy series only from its own completed Plan/ledger facts."""

    with catalog.lock(series_key):
        current = catalog.load(series_key)
        if current.schema_version != 1:
            return current
        bindings = current.season_bindings
        if not bindings or any(not isinstance(item, Mapping) for item in bindings):
            raise SeriesCatalogError("旧作品季度登记缺少可核对的已完成任务")
        binding_ids = tuple(
            str(item.get("season_binding_id") or "") for item in bindings
        )
        task_ids = tuple(str(item.get("task_id") or "") for item in bindings)
        if (
            any(not value for value in binding_ids)
            or any(not value for value in task_ids)
            or len(set(binding_ids)) != len(binding_ids)
            or len(set(task_ids)) != len(task_ids)
        ):
            raise SeriesCatalogError("旧作品季度登记无法与已完成任务一一对应")
        evidence = []
        for task_id in task_ids:
            try:
                legacy_plan = PlanStore(PLAN_PATH).load(task_id)
                ledger_path = safe_task_path(
                    RECORD_PATH, task_id, suffix=".ledger.json"
                )
                ledger = json.loads(ledger_path.read_text("utf-8"))
            except (PlanStoreError, OSError, ValueError, json.JSONDecodeError) as exc:
                raise SeriesCatalogError(
                    "旧作品季度登记缺少可信 Plan 或 completed execution evidence"
                ) from exc
            evidence.append((legacy_plan, ledger))
        return catalog.backfill_legacy_completed(
            series_key,
            tuple(evidence),
        )


_WINDOWS_RESERVED_COMPONENT = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$",
    re.IGNORECASE,
)
_TV_FONT_COMPONENT_UTF16_LIMIT = 240


def _utf16_code_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _truncate_utf16(value: str, limit: int) -> str:
    result = value
    while result and _utf16_code_units(result) > limit:
        result = result[:-1]
    return result.rstrip(" .")


def _canonical_tv_font_target(
    canonical_root: Path,
    digest: str,
    *,
    disambiguate: bool,
    archive_suffix: str = ".7z",
) -> Path:
    """Return one safe, human-readable TV series font archive location."""
    try:
        return canonical_font_archive_target(
            canonical_root,
            digest,
            disambiguate=disambiguate,
            archive_suffix=archive_suffix,
        )
    except FontArchiveContractError as exc:
        # Preserve the established TV call-site exception boundary.
        raise ValueError(str(exc)) from exc


def _freeze_tv_font_asset_contract(plan: RenamePlan) -> RenamePlan:
    """Freeze TV font source evidence and resolve any existing target now."""

    if not any(group.kind == "tv_episodes" for group in plan.presentation_groups):
        return plan
    metadata = dict(plan.metadata)
    season_number = metadata.get("season_number")
    series_key = str(metadata.get("series_key") or "")
    canonical_root = Path(str(metadata.get("canonical_work_root") or ""))
    series_catalog = SeriesCatalog(SERIES_CATALOG_PATH) if series_key else None
    unresolved = list(metadata.get("asset_collision_unresolved", ()))
    manifest_assets = []
    revised_entries = []
    source_evidence_by_path = {}
    source_errors = {}
    def is_confirmed_tv_font(entry) -> bool:
        return is_font_archive(entry.source_path) or (
            entry.role == "font"
            and is_supported_font_archive(entry.source_path)
        )

    for entry in plan.entries:
        if not is_confirmed_tv_font(entry):
            continue
        try:
            source_evidence_by_path[entry.source_path] = fingerprint_file(
                entry.source_path
            )
        except AssetCollisionError as exc:
            source_errors[entry.source_path] = str(exc)
    content_identities = {
        (evidence.sha256, evidence.size)
        for evidence in source_evidence_by_path.values()
    }
    primary_identity = min(content_identities) if content_identities else None
    planned_by_digest = {}
    planned_targets = set()
    for entry in plan.entries:
        if not is_confirmed_tv_font(entry):
            revised_entries.append(entry)
            continue
        source_evidence = source_evidence_by_path.get(entry.source_path)
        if source_evidence is None:
            unresolved.append({
                "code": "font_source_evidence_unavailable",
                "source_path": str(entry.source_path),
                "target_path": str(entry.target_path),
                "detail": source_errors.get(
                    entry.source_path,
                    "无法读取字体来源证据",
                ),
            })
            revised_entries.append(entry)
            continue
        target = entry.target_path
        collision = entry.collision_resolution
        entry_requires_review = False
        trusted_location = None
        if series_catalog is not None:
            try:
                catalog_path = SERIES_CATALOG_PATH / f"{series_key}.json"
                if catalog_path.is_file():
                    registered_locations = {
                        os.path.normcase(os.path.abspath(path))
                        for path in series_catalog.validated_associated_locations(
                            series_key,
                            scope="series",
                            role="fonts",
                        )
                    }
                    trusted_location = series_catalog.resolve_associated_location(
                        series_key,
                        scope="series",
                        role="fonts",
                        size=source_evidence.size,
                        digest=source_evidence.sha256,
                    )
                else:
                    registered_locations = set()
                    if int(metadata.get("series_catalog_revision") or 0) != 0:
                        raise SeriesCatalogError(
                            "作品季度登记不存在但 Plan 期望非零 revision"
                        )
                if trusted_location is not None:
                    target = trusted_location
                elif source_evidence.sha256 in planned_by_digest:
                    target = planned_by_digest[source_evidence.sha256]
                else:
                    primary = _canonical_tv_font_target(
                        canonical_root,
                        source_evidence.sha256,
                        disambiguate=False,
                        archive_suffix=entry.source_path.suffix,
                    )
                    primary_key = os.path.normcase(os.path.abspath(primary))
                    target = _canonical_tv_font_target(
                        canonical_root,
                        source_evidence.sha256,
                        disambiguate=(
                            (source_evidence.sha256, source_evidence.size)
                            != primary_identity
                            or
                            primary_key in registered_locations
                            or primary_key in planned_targets
                        ),
                        archive_suffix=entry.source_path.suffix,
                    )
            except (SeriesCatalogError, ValueError) as exc:
                unresolved.append({
                    "code": "font_catalog_evidence_conflict",
                    "source_path": str(entry.source_path),
                    "target_path": str(entry.target_path),
                    "detail": str(exc),
                })
                revised_entries.append(entry)
                continue
        target_key = os.path.normcase(os.path.abspath(target))
        planned_by_digest.setdefault(source_evidence.sha256, target)
        planned_targets.add(target_key)
        if trusted_location is not None:
            try:
                collision = build_reuse_resolution(
                    entry.source_path, target
                )
            except AssetCollisionError as exc:
                collision = None
                detail = str(exc)
            else:
                detail = "既有字体位置与当前来源内容不同"
            if collision is None:
                entry_requires_review = True
                unresolved.append({
                    "code": "font_target_content_conflict",
                    "source_path": str(entry.source_path),
                    "target_path": str(target),
                    "detail": detail,
                })
        elif series_catalog is not None and os.path.lexists(target):
            collision = None
            entry_requires_review = True
            unresolved.append({
                "code": "font_unregistered_target_conflict",
                "source_path": str(entry.source_path),
                "target_path": str(target),
                "detail": "确定性字体位置已存在但没有可信作品登记",
            })
        elif series_catalog is None and os.path.lexists(target):
            try:
                collision = build_reuse_resolution(entry.source_path, target)
            except AssetCollisionError as exc:
                collision = None
                detail = str(exc)
            else:
                detail = "既有目标与当前字体包内容不同"
            if collision is None:
                entry_requires_review = True
                unresolved.append({
                    "code": "font_target_content_conflict",
                    "source_path": str(entry.source_path),
                    "target_path": str(target),
                    "detail": detail,
                })
        revised_entries.append(
            replace(
                entry,
                target_path=target,
                collision_resolution=collision,
                requires_review=entry_requires_review,
            )
        )
        manifest_assets.append({
            "role": "fonts",
            "scope": "series",
            "season_number": season_number,
            "source_path": str(entry.source_path),
            "target_path": str(target),
            "source_size": source_evidence.size,
            "source_sha256": source_evidence.sha256,
        })
    metadata["associated_asset_manifest"] = {
        "schema_version": 1,
        "assets": sorted(
            manifest_assets,
            key=lambda item: str(item["source_path"]).casefold(),
        ),
    }
    if unresolved:
        metadata["asset_collision_unresolved"] = unresolved
    return replace(
        plan,
        entries=tuple(revised_entries),
        metadata=metadata,
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
    def __init__(self):
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
        self.ai_processor = AIProcessor()

        self.R = {}
        self.subtitle_contracts = {}
        self.last_plan: Optional[RenamePlan] = None
        self.active_task_source_scope: Optional[TaskSourceScope] = None

    def _bangumi_candidate_options(
        self,
        candidates: Tuple[SearchTitleCandidate, ...],
        *,
        is_anime: bool,
    ) -> List[Dict[str, object]]:
        """Return bounded BGM snapshots without affecting type/layout decisions."""

        client = getattr(self, 'bangumi_search', None)
        if not is_anime or client is None or not candidates:
            return []
        collected: List[Dict[str, object]] = []
        for candidate in candidates[:2]:
            try:
                collected.extend(client.search_animation(
                    candidate.title,
                    year=candidate.year or None,
                ))
            except ProviderSearchError as exc:
                logger.warning(f'[Bangumi 检索] {exc}；保留 TMDB 与本地已有身份')
                continue
            if collected:
                break
        primary = candidates[0]
        return fuse_provider_candidates(
            primary.title,
            collected,
            year=primary.year or None,
            animation=True,
        )

    def _provider_search_batch(
        self,
        candidates: Tuple[SearchTitleCandidate, ...],
        *,
        is_anime: Optional[bool],
        is_movie: Optional[bool],
    ) -> ProviderSearchBatch:
        """Run identity discovery concurrently without consulting disk layout."""

        domain = (
            'animation' if is_anime is True
            else ('general' if is_anime is False else 'unknown')
        )
        media_hint = (
            'movie' if is_movie is True
            else ('tv' if is_movie is False else 'unknown')
        )
        bangumi_client = getattr(self, 'bangumi_search', None)
        policy = build_search_policy(
            domain=domain,
            media_hint=media_hint,
            tmdb_configured=bool(getattr(self.search, 'TMDB_KEY', '')),
            bangumi_configured=bangumi_client is not None,
        )
        workers = {}

        if SOURCE_BANGUMI in policy.scheduled_sources:
            def search_bangumi() -> ProviderWorkerOutput:
                collected: List[Dict[str, object]] = []
                for candidate in candidates[:2]:
                    collected.extend(bangumi_client.search_animation(
                        candidate.title,
                        year=candidate.year or None,
                    ))
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
    ) -> Union[Tuple[str, Dict, Optional[bool], bool], str]:
        decision = decide_provider_candidates(batch)
        self.last_type_candidates = {}
        for result in batch.results:
            runtime = result.runtime if isinstance(result.runtime, dict) else {}
            observations = runtime.get('observations') or {}
            for kind, observation in observations.items():
                self.last_type_candidates[kind] = dict(observation)

        if decision.status != CandidateDecisionStatus.AUTO_ELIGIBLE:
            evidence = '；'.join(decision.evidence)
            if decision.status == CandidateDecisionStatus.NO_MATCH:
                return f'[作品搜索] 没有找到可信候选；{evidence}'
            return f'[作品搜索] 需要人工选择作品；{evidence}'

        selected = decision.selected_candidate or {}
        entity_type = str(selected.get('entity_type') or '')
        source = (
            SOURCE_TMDB_MOVIE if entity_type == 'movie'
            else (SOURCE_TMDB_TV if entity_type == 'tv' else '')
        )
        result = next((item for item in batch.results if item.source == source), None)
        runtime = result.runtime if result and isinstance(result.runtime, dict) else {}
        name = str(runtime.get('name') or '')
        info = runtime.get('info') if isinstance(runtime.get('info'), dict) else None
        if (
            not name
            or not info
            or str(info.get('id') or '') != str(selected.get('provider_id') or '')
        ):
            return '[作品搜索] 唯一候选缺少可执行的 TMDB 详情，请人工选择作品'

        resolved_anime = is_anime
        if resolved_anime is None:
            resolved_anime = classify_tmdb_animation_evidence(
                genres=info.get('genres', ()),
                genre_ids=info.get('genre_ids', ()),
            ).is_anime
        return name, info, resolved_anime, entity_type == 'movie'

    def _resolve_bangumi_provider_selection(
        self,
        selection: Mapping[str, object],
    ) -> Tuple[str, Dict[str, object], ProviderLinkResolution]:
        """Resolve one selected Bangumi subject into executable TMDB TV data."""

        provider_id = str(selection.get('provider_id') or '').strip()
        selected_provider = {
            'namespace': 'bangumi',
            'entity_type': 'subject',
            'provider_id': provider_id,
        }
        catalog = ProviderLinkCatalog(PROVIDER_LINK_CATALOG_PATH)
        subjects: Tuple[Mapping[str, object], ...] = ()
        resolution = resolve_provider_link(
            selected_provider=selected_provider,
            provider_link_catalog=catalog,
        )
        if resolution.automation_level != 'L0':
            client = getattr(self, 'bangumi_search', None)
            if client is None:
                client = BangumiProviderClient(
                    token=str(getattr(cm, 'config', {}).get('bangumi_token', '')),
                    user_agent=str(getattr(cm, 'config', {}).get(
                        'bangumi_user_agent',
                        'BangumiAutoRenamer/0.3 (local media organizer)',
                    )),
                )
            subjects, relations = client.load_crosswalk_evidence(provider_id)
            observation_batch = collect_subject_observations(
                selected_subject_id=provider_id,
                bangumi_subjects=subjects,
                bangumi_relations=relations,
                search_subject=self._observe_tmdb_tv_subject,
                max_subjects=max(len(subjects), 1),
            )
            resolution = resolve_provider_link(
                selected_provider=selected_provider,
                provider_link_catalog=catalog,
                bangumi_subjects=subjects,
                bangumi_relations=relations,
                subject_observations=observation_batch,
            )
            stable_series_key = (
                f"series-{uuid.uuid5(uuid.NAMESPACE_URL, 'bar:tmdb:tv:' + str(resolution.tmdb_tv_id))}"
                if resolution.tmdb_tv_id else None
            )
            resolution = replace(
                resolution,
                proposed_series_key=stable_series_key,
                subject_observations=dict(observation_batch),
                evidence_path=tuple(
                    value for value in (
                        str(observation_batch.get('evidence_fingerprint') or ''),
                        str(resolution.resolution_fingerprint or ''),
                    ) if value
                ),
            )
            requires_cohort_confirmation = (
                resolution.review_disposition == 'reviewable'
                and subject_is_explicit_no_match(
                    observation_batch, provider_id
                )
            )
            if (
                resolution.review_disposition != 'reviewable'
                or requires_cohort_confirmation
            ):
                boundary_edges = freeze_provider_subject_boundary_edges(
                    selected_subject_id=provider_id,
                    subject_observations=observation_batch,
                    bangumi_subjects=subjects,
                    bangumi_relations=relations,
                )
                self.active_provider_link_failure = {
                    'schema_version': 1,
                    'selected_subject_id': provider_id,
                    'reason_codes': list(
                        resolution.reason_codes
                        or (
                            ('subject_candidate_evidence_missing',)
                            if requires_cohort_confirmation else ()
                        )
                    ),
                    'evidence_fingerprint': str(
                        observation_batch.get('evidence_fingerprint') or ''
                    ),
                    'retry_requires_refresh': any(
                        reason in {
                            'subject_candidate_transport_error',
                            'subject_candidate_budget_exhausted',
                            'subject_evidence_truncated',
                            'subject_query_truncated',
                        }
                        for reason in resolution.reason_codes
                    ),
                    'subject_observations': dict(observation_batch),
                    'bangumi_subjects': [dict(item) for item in subjects],
                    'bangumi_relations': [dict(item) for item in relations],
                    'provider_subject_boundary_edges': [
                        dict(item) for item in boundary_edges
                    ],
                }
            if requires_cohort_confirmation:
                raise ProviderSearchError(
                    '[作品选择] 已找到同系列季度关系，请确认是否与相邻作品一起整理'
                )
        if resolution.automation_level not in {'L0', 'L1', 'L2', 'L3'}:
            reasons = '、'.join(resolution.reason_codes) or '证据不足'
            raise ProviderSearchError(
                f'所选 Bangumi 作品暂时无法安全匹配季度：{reasons}'
            )
        if not resolution.tmdb_tv_id:
            raise ProviderSearchError('季度匹配缺少 TMDB 电视剧身份')
        season_by_subject = dict(resolution.season_assignments)
        if provider_id not in season_by_subject:
            raise ProviderSearchError('季度匹配没有包含所选 Bangumi 作品')
        if subjects:
            try:
                self.active_provider_naming_selection = (
                    select_bangumi_work_title_owner(
                        subjects=subjects,
                        season_assignments=season_by_subject,
                    )
                )
            except CanonicalNamingError as exc:
                raise ProviderSearchError(str(exc)) from exc
        try:
            name, info = self.search.get_tv_info_by_id(
                int(resolution.tmdb_tv_id)
            )
        except (TMDBRequestError, TypeError, ValueError) as exc:
            raise ProviderSearchError(
                '无法读取匹配到的 TMDB 电视剧详情，请稍后重试'
            ) from exc
        if not name or not isinstance(info, dict):
            query = str(
                selection.get('original_title') or selection.get('title') or ''
            ).strip()
            try:
                name, info = self.search.get_tv_info(query) if query else ('', None)
            except TMDBRequestError:
                name, info = '', None
        if not name or not isinstance(info, dict):
            raise ProviderSearchError(
                '匹配到的 TMDB 电视剧详情不完整，请稍后重试'
            )
        if str(info.get('id') or '') != str(resolution.tmdb_tv_id):
            raise ProviderSearchError('TMDB 电视剧详情与季度匹配身份不一致')
        return str(name), info, resolution

    def _observe_tmdb_tv_subject(
        self,
        subject: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Return one bounded, transport-truthful TMDB TV observation."""

        subject_id = str(subject.get('provider_id') or '').strip()
        observed_at = str(
            (self.active_provider_selection or {}).get('snapshot_at') or ''
        ) or datetime.now(timezone.utc).isoformat()
        air_date = str(subject.get('air_date') or '')
        year = int(air_date[:4]) if air_date[:4].isdigit() else None
        queries = []
        seen = set()
        for locale, raw_query in (
            ('original', subject.get('original_title')),
            ('localized', subject.get('title')),
        ):
            query = str(raw_query or '').strip()
            if not query or query.casefold() in seen:
                continue
            seen.add(query.casefold())
            outcome = 'no_match'
            candidates = ()
            info = None
            try:
                evidence_search = getattr(
                    type(self.search),
                    'get_tv_subject_observation_info',
                    None,
                )
                if callable(evidence_search):
                    name, info = evidence_search(self.search, query, year)
                else:
                    name, info = self.search.get_tv_info(query, year)
            except TMDBRequestError:
                outcome = 'transport_error'
            else:
                if name and isinstance(info, Mapping) and info.get('id') is not None:
                    outcome = 'ok'
                    candidates = _tmdb_subject_observation_candidates(
                        query, str(name), info, expected_year=year
                    )
                    if not candidates:
                        outcome = 'no_match'
            queries.append({
                'locale': locale,
                'query': query,
                'year': year,
                'outcome': outcome,
                'truncated': bool(
                    isinstance(info, Mapping)
                    and info.get('_bar_search_truncated') is True
                ),
                'candidates': candidates,
            })
        return {
            'bangumi_subject_id': subject_id,
            'observed_at': observed_at,
            'policy_version': 'subject-candidate-v1',
            'queries': tuple(queries),
        }

    def _crosswalk_tmdb_candidates(
        self,
        subjects: Tuple[Mapping[str, object], ...],
    ) -> Tuple[Dict[str, object], ...]:
        """Freeze bounded TMDB TV candidates from the earliest series titles."""

        ordered = sorted(
            subjects,
            key=lambda item: (
                str(item.get('air_date') or '9999-99-99'),
                str(item.get('provider_id') or ''),
            ),
        )
        queries = []
        for subject in ordered:
            for title in (
                subject.get('original_title'),
                subject.get('title'),
            ):
                value = str(title or '').strip()
                if value and value not in queries:
                    queries.append(value)
        candidates: Dict[str, Dict[str, object]] = {}
        for query in queries[:4]:
            subject = next(
                (
                    item for item in ordered
                    if query in {
                        str(item.get('original_title') or '').strip(),
                        str(item.get('title') or '').strip(),
                    }
                ),
                ordered[0],
            )
            air_date = str(subject.get('air_date') or '')
            year = int(air_date[:4]) if air_date[:4].isdigit() else 0
            try:
                name, info = self.search.get_tv_info(query, year)
            except TMDBRequestError:
                continue
            if not name or not isinstance(info, dict) or info.get('id') is None:
                continue
            provider_id = str(info['id'])
            candidates[provider_id] = {
                'namespace': 'tmdb',
                'entity_type': 'tv',
                'provider_id': provider_id,
                'title': str(name),
                'original_title': str(info.get('original_name') or ''),
                'first_air_date': str(info.get('first_air_date') or ''),
                'seasons': tuple(
                    {
                        'season_number': season.get('season_number'),
                        'air_date': str(season.get('air_date') or ''),
                        'episode_count': season.get('episode_count'),
                    }
                    for season in info.get('seasons', ())
                    if isinstance(season, dict)
                ),
            }
        return tuple(candidates.values())

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

    def _freeze_provider_special_candidates(
        self,
        plan: RenamePlan,
        provider_info: Mapping[str, object],
        *,
        is_movie: bool,
        provider_link_resolution: Optional[ProviderLinkResolution] = None,
    ) -> RenamePlan:
        """Freeze bounded TMDB Season 0 evidence without choosing for the user."""

        resolver = getattr(self.search, 'get_season_info', None)
        provider_id = provider_info.get('id')
        if (
            is_movie
            or not self.active_provider_selection
            or not callable(resolver)
            or provider_id is None
        ):
            return plan
        try:
            season = resolver(int(provider_id), 0)
            rows = tuple(
                season.get('episodes') or ()
                if isinstance(season, Mapping) else ()
            )
        except (TMDBRequestError, TypeError, ValueError):
            return plan
        evidence = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            episode_number = row.get('episode_number')
            episode_id = row.get('id')
            if (
                isinstance(episode_number, bool)
                or not isinstance(episode_number, int)
                or episode_number < 1
                or episode_id in (None, '')
            ):
                continue
            evidence.append({
                'provider_namespace': 'tmdb',
                'provider_entity_type': 'tv',
                'provider_id': str(provider_id),
                'season_number': 0,
                'episode_number': episode_number,
                'provider_episode_id': str(episode_id),
                'title': str(row.get('name') or ''),
                'air_date': str(row.get('air_date') or ''),
                'runtime': row.get('runtime'),
            })
        if not evidence:
            return plan
        selected_id = str(
            (self.active_provider_selection or {}).get('provider_id') or ''
        )
        selected_ranges = tuple(
            row for row in (
                provider_link_resolution.derived_episode_ranges
                if provider_link_resolution is not None else ()
            )
            if isinstance(row, Mapping)
            and str(row.get('bangumi_subject_id') or '') == selected_id
            and str(row.get('tmdb_tv_id') or '') == str(provider_id)
            and isinstance(row.get('season_number'), int)
            and not isinstance(row.get('season_number'), bool)
            and isinstance(row.get('episode_start'), int)
            and not isinstance(row.get('episode_start'), bool)
            and isinstance(row.get('episode_end'), int)
            and not isinstance(row.get('episode_end'), bool)
            and int(row['episode_start']) >= 1
            and int(row['episode_end']) >= int(row['episode_start'])
        )
        overflow_videos = {}
        if len(selected_ranges) == 1:
            selected_range = selected_ranges[0]
            selected_season = int(selected_range['season_number'])
            start = int(selected_range['episode_start'])
            end = int(selected_range['episode_end'])
            overflow_videos = {
                str(entry.source_path): (entry, start, end)
                for entry in plan.entries
                if entry.media_kind == 'video'
                and entry.season_id == selected_season
                and isinstance(entry.episode_id, int)
                and not isinstance(entry.episode_id, bool)
                and not start <= int(entry.episode_id) <= end
                and entry.presentation_asset is not None
                and entry.presentation_asset.asset_role.kind == 'content_version'
            }

        candidates = {}
        for entry in plan.entries:
            asset = entry.presentation_asset
            if (
                entry.media_kind != 'video'
                or asset is None
                or asset.asset_role.kind == 'content_version'
            ):
                continue
            identity = os.path.normpath(str(entry.source_path)).casefold()
            identifier = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]
            candidates[identifier] = tuple(evidence)
        for entry, _start, _end in overflow_videos.values():
            identity = os.path.normpath(str(entry.source_path)).casefold()
            identifier = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]
            candidates[identifier] = tuple(evidence)
        if not candidates:
            return plan
        metadata = dict(plan.metadata)
        metadata['provider_special_candidates'] = candidates
        if overflow_videos:
            work_root = Path(str(
                plan.metadata.get('canonical_work_root')
                or plan.metadata.get('target_root')
                or ''
            ))
            entries = list(plan.entries)
            overflow_targets = {}
            overflow_rows = []
            for index, entry in enumerate(entries):
                overflow = overflow_videos.get(str(entry.source_path))
                if overflow is None:
                    continue
                _original, start, end = overflow
                stem = normalize_target_stem(entry.source_path.stem, work_root.name)
                target = (
                    work_root / jellyfin_season_dir(int(entry.season_id or 1))
                    / JELLYFIN_EXTRAS_DIR
                    / f"{stem or entry.source_path.stem}{entry.source_path.suffix.lower()}"
                )
                entries[index] = replace(
                    entry, target_path=target, requires_review=True
                )
                overflow_targets[str(entry.source_path)] = target
                identity = os.path.normpath(str(entry.source_path)).casefold()
                overflow_rows.append({
                    'entry_id': hashlib.sha256(
                        identity.encode('utf-8')
                    ).hexdigest()[:20],
                    'source_path': str(entry.source_path),
                    'projected_episode': int(entry.episode_id),
                    'episode_start': start,
                    'episode_end': end,
                })
            for index, entry in enumerate(entries):
                subtitle = entry.subtitle
                owner = (
                    str(subtitle.associated_video_source_path)
                    if subtitle is not None
                    and subtitle.associated_video_source_path is not None
                    else ''
                )
                target_video = overflow_targets.get(owner)
                if subtitle is None or target_video is None:
                    continue
                target = format_subtitle_target_fields(
                    target_video=target_video,
                    source_subtitle=entry.source_path,
                    primary_language=subtitle.primary_language or '',
                    script=subtitle.script,
                    track_title_tokens=subtitle.track_title_tokens,
                    flags=subtitle.flags,
                )
                entries[index] = replace(
                    entry, target_path=target, requires_review=True
                )
            metadata['provider_regular_episode_overflows'] = tuple(
                sorted(overflow_rows, key=lambda row: str(row['source_path']).casefold())
            )
            plan = replace(plan, entries=tuple(entries), metadata=metadata)
        else:
            plan = replace(plan, metadata=metadata)
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
            if len(selected) == 1 and prefer_selected_subject_primary:
                primary = selected[0]
                linked = tuple(
                    dict.fromkeys(
                        (tmdb_provider,)
                        + tuple(identity for identity in linked if identity != primary)
                    )
                )
                is_anime = True
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
    ):
        item_name = item_path.name
        if item_repeat:
            item_name_remove = remove_similar_part(item_repeat, item_path.stem)
        else:
            item_name_remove = item_path.stem

        item_suffix = item_path.suffix.lower()

        reduced_name = item_name.replace(itme_path_main_name, '')
        logger.info(f'[处理任务] 移去主要内容后的文件名：{reduced_name}')

        if is_ignored_asset_directory(item_path.parent):
            logger.info(f'[处理任务] 忽略配套目录文件：{item_path}')
        elif is_font_archive(item_path):
            target = work_path / 'Associated' / item_path.name
            self.R[item_path] = target
            logger.info(f'[处理任务] 字体包：{item_path.name} -> {target}')
        elif is_font_archive_candidate(item_path):
            target = work_path / 'Associated' / item_path.name
            self.R[item_path] = target
            logger.info(
                f'[处理任务] 待确认压缩包：{item_path.name} -> {target}'
            )
        else:
            for ignore_tag in IGNORE_SUFFIX:
                if ignore_tag in item_suffix:
                    logger.info(f'[处理任务] 忽略文件：{item_path.name}')
                    break
            else:
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
                    target = (
                        work_path
                        / jellyfin_season_dir(scoped_season)
                        / JELLYFIN_EXTRAS_DIR
                    )
                    target_name = normalized_name
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
        if isinstance(task_source_scope, Mapping):
            task_source_scope = TaskSourceScope.from_mapping(task_source_scope)
        if (
            task_source_scope is not None
            and _tuuid is not None
            and task_source_scope.task_id != _tuuid
        ):
            raise TaskBatchScopeError("task source scope与任务ID不一致")
        if path.is_dir():
            is_video = False
            for sub_path in path.iterdir():
                if (
                    not sub_path.is_dir()
                    and sub_path.suffix in VIDEO_SUFFIX
                    and _source_matches_task_scope(sub_path, task_source_scope)
                ):
                    is_video = True

            if is_video:
                self._process(
                    path,
                    _is_anime,
                    _is_movie,
                    _tuuid,
                    cus_name,
                    cus_season_id,
                    use_ai=use_ai,
                    provider_selection=provider_selection,
                    task_source_scope=task_source_scope,
                )
            else:
                for sub_path in path.iterdir():
                    if (
                        sub_path.is_file()
                        and not _source_matches_task_scope(
                            sub_path, task_source_scope
                        )
                    ):
                        continue
                    self._process(
                        sub_path,
                        _is_anime,
                        _is_movie,
                        _tuuid,
                        cus_name,
                        cus_season_id,
                        use_ai=use_ai,
                        provider_selection=provider_selection,
                        task_source_scope=task_source_scope,
                    )
        else:
            self._process(
                path,
                _is_anime,
                _is_movie,
                _tuuid,
                cus_name,
                cus_season_id,
                use_ai=use_ai,
                provider_selection=provider_selection,
                task_source_scope=task_source_scope,
            )

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

    def check_task_type(
        self,
        _uuid: str,
        rtpath_name: str,
        year: int,
        path: Path,
        is_anime: Optional[bool] = None,
        is_movie: Optional[bool] = None,
        search_candidates: Optional[Tuple[SearchTitleCandidate, ...]] = None,
    ) -> Union[Tuple[str, Dict, Optional[bool], bool], str]:
        season_id = 1
        self.last_type_candidates = {}
        pos = 0
        logger.info('[处理任务] 未传入任务类型，开始判断该文件是否为电影！')

        layout_requires_classification = False
        if is_movie is None and path.is_dir():
            try:
                layout_inventory = inventory_movie_assets(path)
            except MovieAssetInventoryError as exc:
                return f'[电影资产清单] {exc.code}，请人工检查目录后重试'
            layout_requires_classification = any(
                reason in layout_inventory.reason_codes
                for reason in (
                    DISC_STRUCTURE_REQUIRES_MANUAL_WORKFLOW,
                    MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION,
                )
            )

        # Work identity and on-disk layout are separate axes.  A movie folder
        # may contain extras, alternate cuts, or an episodic presentation, so
        # video count alone must never lock the provider type to TV.
        series_locked = is_movie is False
        movie_locked = is_movie is True
        if series_locked:
            logger.info(
                '[处理任务] 调用方已明确指定电视剧类型；'
                '电视剧未确认时不会回退到电影结果'
            )
        elif movie_locked:
            logger.info('[处理任务] 调用方已明确指定电影类型')

        candidates = search_candidates
        if candidates is None:
            default_candidates = [
                SearchTitleCandidate(rtpath_name, year, '调用方提供的标题')
            ]
            if year:
                default_candidates.append(
                    SearchTitleCandidate(
                        rtpath_name,
                        0,
                        '调用方标题移除年份过滤条件',
                    )
                )
            candidates = tuple(default_candidates)

        s1_name, s1_info, s1_issue = '', None, None
        if not movie_locked:
            try:
                s1_name, s1_info, s1_issue = self._search_title_candidates(
                    self.search.get_tv_info,
                    candidates,
                    '电视剧',
                )
                logger.info(f'[处理任务] 搜索到的电视剧名称: {s1_name}')
            except TMDBRequestError:
                logger.error(
                    f'[处理任务] 电视剧 TMDB 网络请求失败，停止类型探测: '
                    f'{rtpath_name}'
                )
                return (
                    f'[TMDB] 电视剧网络请求失败，请检查代理或网络后重试: '
                    f'{rtpath_name}'
                )

        if not s1_name and (
            series_locked or (s1_issue and not layout_requires_classification)
        ):
            issue = s1_issue or '显式电视剧类型未搜索到可信电视剧信息'
            if series_locked:
                try:
                    opposite_name, opposite_info, _ = self._search_title_candidates(
                        self.search.get_movie_info,
                        candidates,
                        '电影',
                    )
                except TMDBRequestError:
                    opposite_name, opposite_info = '', None
                if opposite_name and opposite_info:
                    self.last_type_candidates['movie'] = {
                        'name': opposite_name, 'info': opposite_info,
                    }
                    return (
                        '[TMDB] 按当前信息没有找到正确的电视剧匹配；'
                        f'搜索到可能的电影结果《{opposite_name}》，请人工选择'
                    )
            logger.warning(
                f'[处理任务] {issue}；拒绝回退电影搜索'
            )
            return f'[TMDB] {issue}，请人工确认作品后重试'

        s2_name, s2_info, s2_issue = '', None, None
        if not series_locked and (
            not s1_name or layout_requires_classification
        ):
            try:
                s2_name, s2_info, s2_issue = self._search_title_candidates(
                    self.search.get_movie_info,
                    candidates,
                    '电影',
                )
                logger.info(f'[处理任务] 搜索到的电影名称: {s2_name}')
            except TMDBRequestError:
                logger.error(
                    f'[处理任务] 电影 TMDB 网络请求失败，停止类型探测: '
                    f'{rtpath_name}'
                )
                return (
                    f'[TMDB] 电影网络请求失败，请检查代理或网络后重试: '
                    f'{rtpath_name}'
                )

        if movie_locked and not s2_name:
            try:
                opposite_name, opposite_info, _ = self._search_title_candidates(
                    self.search.get_tv_info,
                    candidates,
                    '电视剧',
                )
            except TMDBRequestError:
                opposite_name, opposite_info = '', None
            if opposite_name and opposite_info:
                self.last_type_candidates['tv'] = {
                    'name': opposite_name, 'info': opposite_info,
                }
                return (
                    '[TMDB] 按当前信息没有找到正确的电影匹配；'
                    f'搜索到可能的电视剧结果《{opposite_name}》，请人工选择'
                )

        if layout_requires_classification and s1_name and s2_name:
            self.last_type_candidates = {
                'tv': {'name': s1_name, 'info': s1_info},
                'movie': {'name': s2_name, 'info': s2_info},
            }
            logger.warning(
                '[处理任务] 多视频布局同时存在可信电视剧与电影身份，'
                '拒绝自动选择任一类型'
            )
            return (
                '[TMDB] 多视频目录同时存在可信电视剧和电影身份，'
                '请人工确认作品与呈现形式后重试'
            )

        if (
            layout_requires_classification
            and s1_issue
            and '置信度过低' not in s1_issue
            and s2_name
        ):
            logger.warning(
                '[处理任务] 电视剧身份存在歧义且电影身份可信，'
                '拒绝把歧义静默解释为电影'
            )
            return (
                f'[TMDB] {s1_issue}；同时存在可信电影身份，'
                '请人工确认作品与呈现形式后重试'
            )

        if not s1_name and not s2_name and (s1_issue or s2_issue):
            issue = s1_issue or s2_issue
            logger.warning(f'[处理任务] {issue}，停止自动匹配')
            return f'[TMDB] {issue}，请人工确认作品后重试'

        season_id = extract_season(rtpath_name)

        if s1_name:
            pos += 1
        elif s2_name:
            pos -= 1

        if season_id == -1:
            pos -= 0.6
            if path.is_file():
                pos -= 0.5
        else:
            pos += 0.6
            if path.is_file():
                pos += 0.5

        if path.is_dir():
            path_file_num = len([i for i in path.iterdir() if i.is_file()])
            if path_file_num > 6:
                pos += 0.4
            else:
                pos -= 0.4

        if (
            series_locked
            or bool(s1_name)
            or pos > 0
            or (is_movie is not None and not is_movie)
        ):
            logger.info('[处理任务] 该文件可能为电视剧！')
            is_movie = False
            info = s1_info
            name = s1_name

            if not info:
                logger.warning(f'[处理任务] 未搜索到电视剧信息, 跳过{rtpath_name}')
                return f'[TMDB] 未搜索到电视剧信息, 跳过{rtpath_name}'

            if is_anime is None:
                is_anime = classify_tmdb_animation_evidence(
                    genres=info.get('genres', ()),
                    genre_ids=info.get('genre_ids', ()),
                ).is_anime
        else:
            logger.info('[处理任务] 该文件可能为电影！')
            is_movie = True
            info = s2_info
            name = s2_name

            if not info:
                logger.warning(f'[处理任务] 未搜索到电影信息, 跳过{rtpath_name}')
                return self.error_reply(
                    _uuid,
                    f'[TMDB] 未搜索到电影信息, 跳过{rtpath_name}',
                    path,
                    is_anime,
                )

            if is_anime is None:
                is_anime = classify_tmdb_animation_evidence(
                    genres=info.get('genres', ()),
                    genre_ids=info.get('genre_ids', ()),
                ).is_anime
        return name, info, is_anime, is_movie

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
        self.active_task_source_scope = task_source_scope
        self.active_provider_selection = (
            dict(provider_selection) if provider_selection else None
        )
        self.active_provider_link_failure: Optional[Dict[str, object]] = None
        self.active_provider_link_resolution: Optional[ProviderLinkResolution] = None
        self.active_provider_naming_selection: Optional[Dict[str, object]] = None
        self.active_linked_provider_identities: Tuple[ProviderIdentity, ...] = ()
        provider_decision_source = 'provider_unique'
        provider_decision_evidence = (
            '全部计划来源已结束，且只有一个合格的 TMDB 候选',
        )
        provider_link_resolution: Optional[ProviderLinkResolution] = None
        provider_series_catalog_revision: Optional[int] = None
        provider_batch: Optional[ProviderSearchBatch] = None
        selected_provider_candidates: List[Dict[str, object]] = []
        if _tuuid:
            _uuid = _tuuid
        else:
            _uuid = str(uuid.uuid4())

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
        if path.is_file() and path.suffix.lower() not in VIDEO_SUFFIX:
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
        search_candidates = parsed_title.candidates
        if not cus_name and path.is_dir():
            search_candidates = _with_parent_title_candidates(
                search_candidates,
                path,
            )
        uses_builtin_type_detector = (
            getattr(self.check_task_type, '__func__', None)
            is Rename.check_task_type
        )
        if provider_selection:
            selection = {
                key: provider_selection.get(key)
                for key in (
                    'namespace', 'entity_type', 'provider_id', 'title',
                    'original_title', 'year', 'poster_url', 'source_url',
                    'snapshot_at', 'library_domain',
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
            if namespace == 'tmdb' and entity_type in {'movie', 'tv'} and provider_id:
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
                        if chosen_domain != 'general':
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
            elif namespace == 'bangumi' and provider_id:
                try:
                    selected_name, selected_info, provider_link_resolution = (
                        self._resolve_bangumi_provider_selection(selection)
                    )
                    selected_season = dict(
                        provider_link_resolution.season_assignments
                    ).get(provider_id)
                    if not isinstance(selected_season, int):
                        raise ProviderSearchError(
                            '季度匹配没有包含所选 Bangumi 作品'
                        )
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
                    self.active_linked_provider_identities = (selected_subject,)
                except (
                    ProviderLinkCatalogError,
                    ProviderSearchError,
                    TypeError,
                    ValueError,
                ) as exc:
                    task_type = f'[作品选择] {exc}'
            else:
                task_type = '[作品选择] 所选候选缺少有效的资料来源、类型或作品 ID'
        elif uses_builtin_type_detector:
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
            )
        else:
            # Compatibility seam for existing callers that explicitly replace
            # the detector.  Normal runtime always uses the concurrent batch.
            bangumi_candidates = self._bangumi_candidate_options(
                search_candidates,
                is_anime=bool(_is_anime),
            )
            task_type = self.check_task_type(
                _uuid,
                rtpath_name,
                year,
                path,
                _is_anime,
                _is_movie,
                search_candidates=search_candidates,
            )
        if isinstance(task_type, str):
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
                    discovery_inventory = inventory_movie_assets(path)
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
                if blocking_reason:
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
                        blocking_reason,
                        {},
                        mixed_candidates={
                            'movie_continuous': discovery_candidates,
                        },
                        season_number=cus_season_id,
                        season_source=("explicit_user" if cus_season_id is not None else "unset"),
                    )
            if provider_batch is not None and path.is_file():
                try:
                    discovery_inventory = inventory_movie_assets(path)
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
                    mixed_inventory = inventory_movie_assets(path)
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
                    mixed_candidates={'movie_continuous': work_candidates},
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
        if is_movie and path.is_dir():
            try:
                movie_inventory = inventory_movie_assets(path)
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
                None,
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
                        mixed_candidates = {
                            'movie_continuous': work_candidates,
                        }
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
        ai_available = self.ai_processor.ai_client.is_available()
        ai_allowed = ai_available and use_ai is not False

        # 【Step.2】
        # 如果是电影
        if is_movie:
            if not name:
                logger.warning(f'[处理任务] 未搜索到电影信息, 跳过{rtpath_name}')
                return self.error_reply(
                    _uuid,
                    f'[TMDB] 未搜索到电影信息, 跳过{rtpath_name}',
                    path,
                    is_anime,
                    is_movie,
                )

            if is_anime:
                _WORK_PATH = self.ANIME_MOVIE_PATH
            else:
                _WORK_PATH = self.MOVIE_PATH

            first_data = info['release_date']
            first_year = first_data.split('-')[0]
            try:
                work_path = build_canonical_work_root(
                    resolved_target_root=_WORK_PATH,
                    decision=naming_decision,
                    year=int(first_year),
                )
                work_path = resolve_fresh_collision_root(
                    generated_root=work_path,
                    decision=naming_decision,
                )
            except (CanonicalNamingError, TypeError, ValueError) as exc:
                return self.error_reply(
                    _uuid, f"[作品目录] {exc}", path, is_anime, is_movie, name
                )
            if path.is_file():
                self.R[path] = work_path / f'{work_path.name}{path.suffix}'
            else:
                for item_path in path.iterdir():
                    item_name = item_path.name
                    self.R[item_path] = (
                        work_path / f'{work_path.name}{item_path.suffix}'
                        if item_path.is_file()
                        and item_path.suffix.lower() in VIDEO_SUFFIX
                        else work_path / f'{work_path.name} - {item_name}'
                    )
            season_id = 0
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
                    year=int(first_year),
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
                level = provider_link_resolution.automation_level
                if level in {'L0', 'L1'}:
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

            season_id = self.get_season_id(
                info,
                work_path,
                path,
                titles,
            )

            season_authority = '目录识别'
            if cus_season_id is not None:
                season_id = int(cus_season_id)
                season_authority = (
                    '跨站映射'
                    if provider_link_resolution is not None
                    else '人工指定'
                )
            logger.info(
                f'[处理任务] 最终季号：{season_id}；来源：{season_authority}'
            )

            mapping_error = None
            # 【AI增强处理】
            # 如果是动漫且启用了AI，使用AI分析文件映射
            if is_anime and ai_allowed:
                logger.info("[处理任务] 启用AI分析动漫文件映射")
                logger.info("[处理任务] 填充详细季信息")
                tv_info = self.search.fill_season_info(info)
                ai_result: AIAnalysisResult | None = (
                    self.ai_processor.analyze_anime_files(path, tv_info)
                )

                # 检查AI置信度阈值
                confidence_threshold = cm.get_config("ai_confidence_threshold")
                should_use_ai = False

                if ai_result:
                    if (
                        confidence_threshold == "High"
                        and ai_result.confidence == "High"
                    ):
                        should_use_ai = True
                    elif confidence_threshold == "Medium" and ai_result.confidence in [
                        "High",
                        "Medium",
                    ]:
                        should_use_ai = True
                    elif confidence_threshold == "Low":
                        should_use_ai = True

                if should_use_ai and ai_result:
                    logger.info("[处理任务] 使用AI分析结果进行文件映射")
                    # AI流程独立生成映射，不再需要传统方法预处理
                    self.R = self.ai_processor.apply_ai_mapping(
                        ai_result=ai_result, base_path=path, work_path=work_path
                    )
                    # 如果AI没有返回任何有效映射，则回退到传统方法
                    if not self.R:
                        logger.warning(
                            "[处理任务] AI未返回有效映射，回退到传统方法处理"
                        )
                        mapping_error = self._process_traditional(
                            path, rtpath_name, work_path, season_id, name
                        )
                else:
                    logger.info("[处理任务] AI置信度不足或AI结果无效，使用传统方法处理")
                    mapping_error = self._process_traditional(
                        path, rtpath_name, work_path, season_id, name
                    )
            else:
                # 传统处理方式
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
        provider_date = str(
            info.get("release_date") if is_movie else info.get("first_air_date")
            or ""
        )
        provider_year = (
            int(provider_date[:4])
            if len(provider_date) >= 4 and provider_date[:4].isdigit()
            else None
        )
        try:
            provider_identity = provider_identity_from_metadata(
                namespace="tmdb",
                entity_type="movie" if is_movie else "tv",
                provider_id=info.get("id"),
                title=name,
                year=provider_year,
                decision_source=provider_decision_source,
                evidence=provider_decision_evidence,
                original_title=(
                    self.active_provider_selection.get('original_title')
                    if self.active_provider_selection else info.get('original_title')
                    or info.get('original_name')
                ),
                poster_url=(
                    self.active_provider_selection.get('poster_url')
                    if self.active_provider_selection else None
                ),
                source_url=(
                    self.active_provider_selection.get('source_url')
                    if self.active_provider_selection else None
                ),
                snapshot_at=(
                    self.active_provider_selection.get('snapshot_at')
                    if self.active_provider_selection else None
                ),
            )
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
                )
            )
            if not is_movie:
                presentation_assets = dict(presentation_assets)
                group_id = presentation_groups[0].group_id
                for source in self.R:
                    if (
                        is_font_archive_candidate(source)
                        and not is_font_archive(source)
                    ):
                        presentation_assets[source] = (
                            PresentationAssetPlanContract(
                                group_id,
                                AssetRole("unknown"),
                                None,
                                None,
                                "shared",
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
        plan = RenamePlan.from_mapping(
            task_id=_uuid,
            source_root=path,
            mapping=self.R,
            expected_sources=_plan_expected_sources(
                path, self.R, self.active_task_source_scope
            ),
            metadata={
                "name": name,
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
                "mapping_source": (
                    "ai"
                    if is_anime and ai_allowed
                    else "rules"
                ),
                "task_ai_policy": use_ai,
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
                SeriesScopeIdentity.from_provider("tmdb", "tv", info["id"])
                if not is_movie and info.get("id") is not None else None
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
        plan = _freeze_tv_font_asset_contract(plan)
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
        plan = self._freeze_provider_special_candidates(
            plan,
            info,
            is_movie=bool(is_movie),
            provider_link_resolution=provider_link_resolution,
        )
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
            decision_store = DecisionStateStore(DECISION_STATE_PATH)
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
                return staged

            transform = (
                transform_provider_state
                if provider_link_resolution is not None
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
                    "use_ai": is_anime and ai_allowed,
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
            )
            self.last_plan = plan
            plan_path = PlanStore(PLAN_PATH).path_for(plan.task_id)
        except (DecisionStateError, PlanStoreError, OSError, ValueError) as exc:
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
                "use_ai": (
                    is_anime and ai_allowed
                ),
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
            "use_ai": is_anime and ai_allowed,
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
                )
                if classification is not None:
                    classifications[item] = classification

            videos = [
                item for item in batch
                if item.suffix.lower() in VIDEO_SUFFIX and item in self.R
            ]
            # Subtitle association and label parsing are intentionally deferred
            # to the common S0/S1 layer after AI/rule video mapping is complete.
            # This prevents the legacy case-sensitive suffix copier from
            # bypassing structured review.
            return None

        if path.is_file():
            logger.info(f"[处理任务] 开始对 [单文件] {path.name}处理")
            batch_error = process_batch([path])
            if batch_error:
                return batch_error
        else:
            logger.info(f"[处理任务] 开始对 [文件夹] {path.name}处理")
            root_items = [item for item in path.iterdir() if item.is_file()]
            batch_error = process_batch(root_items)
            if batch_error:
                return batch_error
            for item_path in path.iterdir():
                logger.info(f"[处理任务] 处理嵌套文件夹 {item_path.name}")
                if item_path.is_dir():
                    if is_ignored_asset_directory(item_path):
                        logger.info(f"[处理任务] 跳过配套目录 {item_path.name}")
                        continue
                    batch_error = process_batch(list(item_path.iterdir()))
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
            'is_anime': is_anime,
            'is_movie': is_movie,
            'name': name,
            'season_id': season_id,
            'uuid': str(_uuid),
            'error': error,
        }
        if provider_candidates:
            task_data['provider_candidates'] = provider_candidates
            task_data['type_match_state'] = 'mismatch'
        if self.active_provider_link_failure is not None:
            task_data['provider_selection_failure'] = dict(
                self.active_provider_link_failure
            )
        merge_task_at(TASK_PATH, str(_uuid), task_data)
        return error

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

        error = (
            f'[电影资产清单] {reason_code}；复杂呈现形式需要后续人工建模，'
            '当前未生成可执行整理计划'
        )
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
            reason_code == MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION
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
        task_data = {
            'path': str(path),
            'is_anime': is_anime,
            'is_movie': None if layout_unresolved else True,
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
                'namespace': 'tmdb',
                'entity_type': provider_entity_type,
                'provider_id': str((provider_info or {}).get('id') or ''),
                'title': name,
                'year': release_year,
                'original_title': str(
                    active_selection.get('original_title')
                    if (
                        str(active_selection.get('namespace') or '').casefold()
                        == 'tmdb'
                        and selected_entity_type == provider_entity_type
                    )
                    else (provider_info or {}).get(
                        'original_title'
                        if provider_entity_type == 'movie'
                        else 'original_name'
                    )
                    or ''
                ) or None,
                'poster_url': str(
                    active_selection.get('poster_url')
                    if str(active_selection.get('namespace') or '').casefold()
                    == 'tmdb'
                    else ''
                ) or None,
                'source_url': str(
                    active_selection.get('source_url')
                    if str(active_selection.get('namespace') or '').casefold()
                    == 'tmdb'
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
        if (
            inventory is not None
            and isinstance(task_data.get('provider_identity'), dict)
        ):
            try:
                draft = build_inventory_review_draft(
                    merged,
                    dry_run=is_dry_run_enabled(),
                )
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
                )

                def finalize_inventory_task(_decision, projected, current):
                    verification = PlanVerifier().verify(projected)
                    current.update({
                        "workflow_status": "invalid",
                        "plan_status": verification.status.value,
                        "plan_entry_count": len(projected.entries),
                        "plan_file": PlanStore(PLAN_PATH).path_for(
                            projected.task_id
                        ).name,
                        "inventory_draft_created": True,
                    })
                    return current

                _decision, draft = commit_recognition_projection(
                    DecisionStateStore(DECISION_STATE_PATH),
                    PlanStore(PLAN_PATH),
                    recognition,
                    draft,
                    task_terminal=TaskTerminalMutation.prepare(
                        TASK_PATH, str(_uuid), finalize_inventory_task
                    ),
                )
                self.last_plan = draft
            except (DecisionStateError, PlanStoreError, ValueError, OSError) as exc:
                logger.error(f'[库存草案持久化] {exc}')
        logger.warning(error)
        return error
