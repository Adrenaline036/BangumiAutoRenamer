"""Deterministic schema-v5 presentation grouping helpers.

The helpers in this module do not inspect or mutate media.  They only turn an
already-confirmed provider identity and an existing source/target mapping into
the presentation contracts required by an executable rename plan.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Tuple

from .rename_plan import (
    AssetRole,
    EditionValue,
    EvidenceCandidate,
    EvidenceValue,
    PlanEntry,
    PresentationAssetPlanContract,
    PresentationGroup,
    ProviderIdentity,
    VersionDescriptor,
    build_default_version_descriptor,
    build_version_ref,
    format_presentation_version_label,
)
from .subtitle_labels import DimensionResolution, SUBTITLE_SUFFIXES


PROVIDER_REGISTRY = MappingProxyType({
    "tmdb": frozenset({"movie", "tv"}),
    "bangumi": frozenset({"subject"}),
})

DEFAULT_PRESENTATION_GROUP_IDS = MappingProxyType({
    "movie_continuous": "movie-continuous",
    "tv_episodes": "tv-episodes",
})


def default_presentation_group_id(group_kind: str) -> str:
    """Return the stable internal group id for an automatically created group."""

    try:
        return DEFAULT_PRESENTATION_GROUP_IDS[str(group_kind)]
    except KeyError as exc:
        raise ValueError(f"Unsupported automatic presentation group: {group_kind}") from exc


def provider_identity_from_metadata(
    *,
    namespace: str,
    entity_type: str,
    provider_id: object,
    title: str,
    year: Optional[int],
    decision_source: str,
    evidence: Iterable[str] = (),
    original_title: Optional[str] = None,
    poster_url: Optional[str] = None,
    source_url: Optional[str] = None,
    snapshot_at: Optional[str] = None,
) -> ProviderIdentity:
    """Build a confirmed provider identity after registry validation."""

    normalized_namespace = str(namespace).strip().casefold()
    normalized_type = str(entity_type).strip().casefold()
    if normalized_type not in PROVIDER_REGISTRY.get(normalized_namespace, frozenset()):
        raise ValueError(
            f"Unsupported provider identity: {normalized_namespace}/{normalized_type}"
        )
    normalized_id = str(provider_id).strip()
    normalized_title = str(title).strip()
    if not normalized_id or not normalized_title:
        raise ValueError("Confirmed provider identity requires id and title")
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
        raise ValueError("Provider year must be an integer or null")
    return ProviderIdentity(
        status="confirmed",
        namespace=normalized_namespace,
        entity_type=normalized_type,
        provider_id=normalized_id,
        title=normalized_title,
        year=year,
        decision_source=str(decision_source),
        evidence=tuple(str(item) for item in evidence),
        original_title=(str(original_title).strip() or None)
        if original_title is not None else None,
        poster_url=(str(poster_url).strip() or None)
        if poster_url is not None else None,
        source_url=(str(source_url).strip() or None) if source_url else (
            f"https://www.themoviedb.org/{normalized_type}/{normalized_id}"
            if normalized_namespace == "tmdb"
            else f"https://bgm.tv/subject/{normalized_id}"
        ),
        snapshot_at=(str(snapshot_at).strip() or None) if snapshot_at else (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        ),
    )


def build_default_presentation_contracts(
    mapping: Mapping[Path, Path],
    *,
    group_id: str,
    group_kind: str,
    display_name: str,
    provider_identity: ProviderIdentity,
) -> Tuple[Tuple[PresentationGroup, ...], Mapping[Path, PresentationAssetPlanContract]]:
    """Add the R2 structural-default descriptor to a single-version mapping.

    TV content identity includes season/episode.  Movie content identity fixes
    both fields to ``null`` irrespective of filename tokens, as required by R1.
    Subtitles deliberately carry no presentation contract and inherit the
    grouping from their associated video during verification.
    """

    if group_kind not in {"movie_continuous", "tv_episodes"}:
        raise ValueError("Default executable grouping supports movie or TV only")
    group = PresentationGroup(
        group_id=str(group_id),
        kind=group_kind,
        display_name=str(display_name),
        preferred_version_ref=None,
        provider_identity=provider_identity,
    )
    assets = {}
    content_count = 0
    for source, target in mapping.items():
        source_path = Path(source)
        if source_path.suffix.casefold() in SUBTITLE_SUFFIXES:
            continue
        entry = PlanEntry.from_paths(source_path, Path(target))
        if entry.media_kind == "video":
            is_content = group_kind == "movie_continuous" or (
                entry.season_id is not None and entry.episode_id is not None
            )
            if is_content:
                season_id = entry.season_id if group_kind == "tv_episodes" else None
                episode_id = entry.episode_id if group_kind == "tv_episodes" else None
                contract = PresentationAssetPlanContract(
                    presentation_group=group.group_id,
                    asset_role=AssetRole("content_version"),
                    segment=None,
                    version_descriptor=build_default_version_descriptor(
                        group.group_id, season_id, episode_id
                    ),
                )
                content_count += 1
            else:
                contract = PresentationAssetPlanContract(
                    presentation_group=group.group_id,
                    asset_role=AssetRole("extra", "other"),
                    segment=None,
                    version_descriptor=None,
                )
        else:
            contract = PresentationAssetPlanContract(
                presentation_group=group.group_id,
                asset_role=AssetRole("associated"),
                segment=None,
                version_descriptor=None,
            )
        assets[source_path] = contract
    # A provider-backed single special can reach review before the operator
    # confirms whether it is official Season 00 content or an extra.  Keep a
    # truthful, blocked Plan instead of dropping the review entry entirely;
    # PlanVerifier supplies the required operator exit.
    return (group,), MappingProxyType(assets)


def associate_resolved_extra_subtitles(plan):
    """Project resolved subtitles of extra videos as associated resources.

    A subtitle attached to a TV OVA/bonus video must stay beside that video,
    but it is not a subtitle track of a primary content version.  The strict
    subtitle verifier intentionally accepts only primary content-version
    tracks, so production plans must use the same associated-resource model as
    the inventory completion path instead of presenting a false "choose movie
    primary" repair to TV users.
    """

    assets_by_source = {
        entry.source_path: entry.presentation_asset
        for entry in plan.entries
        if entry.presentation_asset is not None
    }
    changed = False
    entries = []
    for entry in plan.entries:
        subtitle = entry.subtitle
        associated_source = (
            subtitle.associated_video_source_path
            if subtitle is not None else None
        )
        video_asset = assets_by_source.get(associated_source)
        if (
            entry.media_kind == "subtitle"
            and subtitle is not None
            and subtitle.association_state == DimensionResolution.RESOLVED
            and subtitle.parse_state == DimensionResolution.RESOLVED
            and not subtitle.unknown_tokens
            and video_asset is not None
            and video_asset.asset_role.kind == "extra"
        ):
            entries.append(replace(
                entry,
                media_kind="associated",
                role="extra_subtitle",
                requires_review=False,
                evidence=entry.evidence + (
                    "字幕与特别篇视频同名，作为特别篇配套资源",
                ),
                subtitle=None,
                presentation_asset=PresentationAssetPlanContract(
                    video_asset.presentation_group,
                    AssetRole("associated"),
                    None,
                    None,
                    video_asset.owner_version_ref,
                ),
            ))
            changed = True
            continue
        entries.append(entry)
    return replace(plan, entries=tuple(entries)) if changed else plan


def rebind_content_version_identity(
    contract: Optional[PresentationAssetPlanContract],
    *,
    group_kind: str,
    season_id: Optional[int],
    episode_id: Optional[int],
) -> Optional[PresentationAssetPlanContract]:
    """Recompute version_ref after a reviewed target changes TV episode identity."""

    if contract is None or contract.asset_role.kind != "content_version":
        return contract
    descriptor = contract.version_descriptor
    if descriptor is None or not descriptor.technical_variant_key:
        return contract
    edition = descriptor.edition.value
    edition_key = getattr(edition, "key", None)
    if not edition_key:
        return contract
    bound_season = season_id if group_kind == "tv_episodes" else None
    bound_episode = episode_id if group_kind == "tv_episodes" else None
    rebound: VersionDescriptor = replace(
        descriptor,
        version_ref=build_version_ref(
            contract.presentation_group,
            bound_season,
            bound_episode,
            edition_key,
            descriptor.technical_variant_key,
        ),
    )
    return replace(contract, version_descriptor=rebound)


def build_manual_version_descriptor(
    *,
    presentation_group: str,
    group_kind: str,
    season_id: Optional[int],
    episode_id: Optional[int],
    edition_key: str,
    edition_label: Optional[str],
    resolution: str,
    dynamic_range: str,
    key_audio: str,
    reason: str,
    previous: Optional[VersionDescriptor] = None,
) -> VersionDescriptor:
    """Create a deterministic descriptor while preserving prior candidates."""

    reason = str(reason).strip()
    values = {
        "edition": EditionValue(str(edition_key).strip(), _clean(edition_label)),
        "resolution": _required(resolution, "resolution"),
        "dynamic_range": _required(dynamic_range, "dynamic_range"),
        "key_audio": _required(key_audio, "key_audio"),
    }
    if not values["edition"].key or not reason:
        raise ValueError("Manual version confirmation requires edition key and reason")
    resolved = {}
    for field_name, value in values.items():
        old = getattr(previous, field_name) if previous is not None else None
        candidates = tuple(old.candidates) if old is not None else ()
        manual = EvidenceCandidate(
            value=value,
            source="manual_confirmation",
            evidence=(reason,),
        )
        if manual not in candidates:
            candidates += (manual,)
        resolved[field_name] = EvidenceValue(
            status="resolved",
            value=value,
            selected_source="manual_confirmation",
            candidates=candidates,
            manual_reason=reason,
        )
    variant_payload = json.dumps(
        {
            "resolution": values["resolution"],
            "dynamic_range": values["dynamic_range"],
            "key_audio": values["key_audio"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    technical_key = "manual-" + hashlib.sha256(variant_payload).hexdigest()[:12]
    bound_season = season_id if group_kind == "tv_episodes" else None
    bound_episode = episode_id if group_kind == "tv_episodes" else None
    display_label = format_presentation_version_label(
        values["edition"].label,
        values["resolution"],
        values["dynamic_range"],
        values["key_audio"],
    )
    return VersionDescriptor(
        version_ref=build_version_ref(
            presentation_group,
            bound_season,
            bound_episode,
            values["edition"].key,
            technical_key,
        ),
        edition=resolved["edition"],
        technical_variant_key=technical_key,
        resolution=resolved["resolution"],
        dynamic_range=resolved["dynamic_range"],
        key_audio=resolved["key_audio"],
        display_label=display_label,
    )


def _clean(value: Optional[str]) -> Optional[str]:
    cleaned = " ".join(str(value or "").strip().split())
    return cleaned or None


def _required(value: str, field_name: str) -> str:
    cleaned = _clean(value)
    if cleaned is None:
        raise ValueError(f"Manual version confirmation requires {field_name}")
    return cleaned
