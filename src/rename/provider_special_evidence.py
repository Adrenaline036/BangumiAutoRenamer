"""Canonical frozen Provider Season 0 evidence and Plan projection.

The Provider response is an observation, never a user decision.  Every Plan
producer consumes the same fingerprinted observation through the projector in
this module so an absent/error response cannot be confused with an
authoritative empty Season 0.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Optional, Sequence

from .episode_inference import normalize_target_stem
from .get_info import TMDBRequestError
from .media_classifier import LocalRole
from .rename_plan import PlanEntry, RenamePlan
from .subtitle_workflow import format_subtitle_target_fields
from .utils import JELLYFIN_EXTRAS_DIR, jellyfin_season_dir


PROVIDER_SPECIAL_OBSERVATION_SCHEMA_VERSION = 1
_OBSERVATION_DOMAIN = "bar.provider-special-observation.v1"
_OBSERVATION_STATES = frozenset({
    "completed_with_rows",
    "completed_empty",
    "not_run",
    "unavailable_or_error",
})
_PROVIDER_SPECIAL_ELIGIBLE_LOCAL_ROLES = frozenset({
    LocalRole.EPISODE_LIKE_BONUS.value,
    LocalRole.PROVIDER_SPECIAL.value,
})


class ProviderSpecialEvidenceError(ValueError):
    """Raised when frozen Provider-special evidence is invalid or disconnected."""


def _fingerprint(payload: Mapping[str, object]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(
        f"{_OBSERVATION_DOMAIN}\n{serialized}".encode("utf-8")
    ).hexdigest()


def _normalize_episode_rows(
    rows: Sequence[object],
    *,
    provider_namespace: str,
    provider_entity_type: str,
    provider_id: str,
) -> tuple[Mapping[str, object], ...]:
    evidence: list[Mapping[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ProviderSpecialEvidenceError(
                "Provider Season 0 returned an invalid episode row"
            )
        episode_number = row.get("episode_number")
        episode_id = row.get("id", row.get("provider_episode_id"))
        runtime = row.get("runtime")
        if (
            isinstance(episode_number, bool)
            or not isinstance(episode_number, int)
            or episode_number < 1
            or episode_id in (None, "")
            or isinstance(runtime, bool)
            or (runtime is not None and not isinstance(runtime, int))
        ):
            raise ProviderSpecialEvidenceError(
                "Provider Season 0 returned incomplete episode identity"
            )
        evidence.append(MappingProxyType({
            "provider_namespace": provider_namespace,
            "provider_entity_type": provider_entity_type,
            "provider_id": provider_id,
            "season_number": 0,
            "episode_number": episode_number,
            "provider_episode_id": str(episode_id),
            "title": str(row.get("name", row.get("title")) or ""),
            "air_date": str(row.get("air_date") or ""),
            "runtime": runtime,
        }))
    return tuple(evidence)


@dataclass(frozen=True)
class ProviderSpecialObservation:
    """Immutable, fingerprinted result of one bounded Provider Season 0 read."""

    state: str
    provider_namespace: str
    provider_entity_type: str
    provider_id: str
    episodes: tuple[Mapping[str, object], ...]
    observation_fingerprint: str
    schema_version: int = PROVIDER_SPECIAL_OBSERVATION_SCHEMA_VERSION
    season_number: int = 0
    catalog_json: str | None = None

    @property
    def displayable(self):
        if self.schema_version == 3:
            from .specials_v3 import availability
            return availability(json.loads(self.catalog_json)['sources'])[1]
        if self.state in {'completed_with_rows', 'completed_empty'}:
            return True
        if self.schema_version == 2:
            return self.state == 'incomplete' or any(
                source['complete'] for source in json.loads(self.catalog_json)['catalog'])
        return False

    @property
    def output_candidates_available(self):
        return bool(self.episodes) and (self.schema_version in (2, 3) or self.state == 'completed_with_rows')

    def to_dict(self) -> dict[str, object]:
        if self.schema_version in (2, 3):
            return {**json.loads(self.catalog_json), 'observation_fingerprint': self.observation_fingerprint}
        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "provider_identity": {
                "namespace": self.provider_namespace,
                "entity_type": self.provider_entity_type,
                "provider_id": self.provider_id,
            },
            "season_number": self.season_number,
            "episodes": [dict(row) for row in self.episodes],
            "observation_fingerprint": self.observation_fingerprint,
        }

    @classmethod
    def build(
        cls,
        *,
        state: str,
        provider_namespace: str,
        provider_entity_type: str,
        provider_id: str,
        episodes: Sequence[Mapping[str, object]] = (),
    ) -> "ProviderSpecialObservation":
        namespace = str(provider_namespace or "").strip().casefold()
        entity_type = str(provider_entity_type or "").strip().casefold()
        identity = str(provider_id or "").strip()
        if (
            state not in _OBSERVATION_STATES
            or not namespace
            or not entity_type
            or not identity
        ):
            raise ProviderSpecialEvidenceError(
                "Provider-special observation identity/state is invalid"
            )
        frozen_rows = tuple(MappingProxyType(dict(row)) for row in episodes)
        if (state == "completed_with_rows") != bool(frozen_rows):
            raise ProviderSpecialEvidenceError(
                "Provider-special observation state does not match its rows"
            )
        payload = {
            "schema_version": PROVIDER_SPECIAL_OBSERVATION_SCHEMA_VERSION,
            "state": state,
            "provider_identity": {
                "namespace": namespace,
                "entity_type": entity_type,
                "provider_id": identity,
            },
            "season_number": 0,
            "episodes": [dict(row) for row in frozen_rows],
        }
        return cls(
            state=state,
            provider_namespace=namespace,
            provider_entity_type=entity_type,
            provider_id=identity,
            episodes=frozen_rows,
            observation_fingerprint=_fingerprint(payload),
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> "ProviderSpecialObservation":
        if value.get('schema_version') == 3:
            from . import specials_v3
            try:
                return specials_v3.from_mapping(value)
            except (ValueError, TypeError, KeyError) as exc:
                raise ProviderSpecialEvidenceError(str(exc)) from exc
        if value.get('schema_version') == 2:
            from . import special_catalog
            try:
                return special_catalog.from_mapping(value)
            except (TypeError, ValueError, KeyError) as exc:
                raise ProviderSpecialEvidenceError(str(exc)) from exc
        if set(value) != {
            "schema_version",
            "state",
            "provider_identity",
            "season_number",
            "episodes",
            "observation_fingerprint",
        }:
            raise ProviderSpecialEvidenceError(
                "Provider-special observation fields are invalid"
            )
        identity = value.get("provider_identity")
        rows = value.get("episodes")
        if (
            value.get("schema_version")
            != PROVIDER_SPECIAL_OBSERVATION_SCHEMA_VERSION
            or value.get("season_number") != 0
            or not isinstance(identity, Mapping)
            or not isinstance(rows, (list, tuple))
        ):
            raise ProviderSpecialEvidenceError(
                "Provider-special observation schema is invalid"
            )
        normalized = _normalize_episode_rows(
            tuple(rows),
            provider_namespace=str(identity.get("namespace") or ""),
            provider_entity_type=str(identity.get("entity_type") or ""),
            provider_id=str(identity.get("provider_id") or ""),
        )
        observation = cls.build(
            state=str(value.get("state") or ""),
            provider_namespace=str(identity.get("namespace") or ""),
            provider_entity_type=str(identity.get("entity_type") or ""),
            provider_id=str(identity.get("provider_id") or ""),
            episodes=normalized,
        )
        if observation.observation_fingerprint != str(
            value.get("observation_fingerprint") or ""
        ):
            raise ProviderSpecialEvidenceError(
                "Provider-special observation fingerprint is invalid"
            )
        return observation


def freeze_provider_special_observation(
    *,
    provider_namespace: str,
    provider_entity_type: str,
    provider_id: str,
    resolver: Optional[Callable[[object, int], object]],
    enabled: bool,
) -> ProviderSpecialObservation:
    """Perform at most one bounded Season 0 read and freeze its exact state."""

    identity = str(provider_id or "").strip()
    if not enabled:
        return ProviderSpecialObservation.build(
            state="not_run",
            provider_namespace=provider_namespace,
            provider_entity_type=provider_entity_type,
            provider_id=identity,
        )
    if not callable(resolver):
        return ProviderSpecialObservation.build(
            state="unavailable_or_error",
            provider_namespace=provider_namespace,
            provider_entity_type=provider_entity_type,
            provider_id=identity,
        )
    try:
        resolver_id: object = int(identity) if identity.isdigit() else identity
        season = resolver(resolver_id, 0)
        if not isinstance(season, Mapping):
            raise ProviderSpecialEvidenceError(
                "Provider Season 0 response is unavailable"
            )
        rows = season.get("episodes") or ()
        if not isinstance(rows, (list, tuple)):
            raise ProviderSpecialEvidenceError(
                "Provider Season 0 episode rows are unavailable"
            )
        normalized = _normalize_episode_rows(
            tuple(rows),
            provider_namespace=str(provider_namespace),
            provider_entity_type=str(provider_entity_type),
            provider_id=identity,
        )
    except (TMDBRequestError, ProviderSpecialEvidenceError, TypeError, ValueError):
        return ProviderSpecialObservation.build(
            state="unavailable_or_error",
            provider_namespace=provider_namespace,
            provider_entity_type=provider_entity_type,
            provider_id=identity,
        )
    return ProviderSpecialObservation.build(
        state="completed_with_rows" if normalized else "completed_empty",
        provider_namespace=provider_namespace,
        provider_entity_type=provider_entity_type,
        provider_id=identity,
        episodes=normalized,
    )


def _provider_special_eligibility(entry: PlanEntry) -> str:
    """Return the one canonical typed per-file Season 0 eligibility."""

    if entry.role in _PROVIDER_SPECIAL_ELIGIBLE_LOCAL_ROLES:
        return "eligible"
    if entry.role == LocalRole.DISC_EXTRA.value:
        return "clear_extra"
    asset = entry.presentation_asset
    if asset is None:
        return "unavailable"
    role = asset.asset_role
    if role.kind == "content_version":
        return "not_applicable"
    if role.kind != "extra":
        return "unavailable"
    if role.subtype == LocalRole.DISC_EXTRA.value:
        return "clear_extra"
    if role.subtype in _PROVIDER_SPECIAL_ELIGIBLE_LOCAL_ROLES:
        return "eligible"
    return "unavailable"


def _entry_id(entry: PlanEntry) -> str:
    identity = os.path.normpath(str(entry.source_path)).casefold()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def bangumi_client():
    from ..config.config_manager import cm
    from .provider_search import BangumiProviderClient
    config = getattr(cm, 'config', {})
    return BangumiProviderClient(token=str(config.get('bangumi_token', '')),
        user_agent=str(config.get('bangumi_user_agent') or 'BangumiAutoRenamer/0.3 (local media organizer)'))


def observe_specials_for_provider(provider, *, client=None):
    """One source-specific read, projected into the existing official-row contract."""
    from .provider_search import ProviderSearchError
    namespace, kind, identity = provider['namespace'], provider['entity_type'], str(provider['provider_id'])
    if (namespace, kind) == ('tmdb', 'tv'):
        from .get_info import Search
        return freeze_provider_special_observation(provider_namespace=namespace, provider_entity_type=kind,
            provider_id=identity, resolver=(client or Search()).get_season_info, enabled=True)
    if (namespace, kind) != ('bangumi', 'subject') or not identity.isdigit():
        raise ProviderSpecialEvidenceError('当前作品没有可读取的官方特别篇身份')
    from .special_catalog import read_source, build_observation
    source = read_source(provider, client=client or bangumi_client())
    # Preserve the exact v1 contract only for clean, unique positive integers.
    numbers = [row['source_number'] for row in source['rows']]
    if (source['complete'] and all(n is not None and n.isdigit() and int(n) >= 1 for n in numbers)
            and len(set(numbers)) == len(numbers)):
        normalized = _normalize_episode_rows([
            dict(id=row['provider_episode_id'], episode_number=int(row['source_number']),
                 name=row['title'], air_date=row['air_date'], runtime=row['runtime']) for row in source['rows']],
            provider_namespace=namespace, provider_entity_type=kind, provider_id=identity)
        return ProviderSpecialObservation.build(state=source['state'], provider_namespace=namespace,
            provider_entity_type=kind, provider_id=identity, episodes=normalized)
    return build_observation(provider, [source])


def special_provider_ids(plan: RenamePlan, resolution=None) -> frozenset[str]:
    """The frozen work/link owns provider identity, never candidate text."""
    linked_id = str(getattr(resolution, 'tmdb_tv_id', '') or '')
    if linked_id:
        return frozenset({linked_id})
    primary = plan.work_identity.primary_provider if plan.work_identity is not None else None
    if primary is not None and primary.namespace == 'tmdb' and primary.entity_type == 'tv' and primary.status == 'confirmed':
        return frozenset({str(primary.provider_id)}) if primary.provider_id else frozenset()
    return frozenset(str(p.provider_id) for p in plan.linked_provider_identities
        if p.namespace == 'tmdb' and p.entity_type == 'tv' and p.status == 'confirmed' and p.provider_id)


def validate_special_identity(plan: RenamePlan, observation: ProviderSpecialObservation, resolution=None) -> None:
    if observation.schema_version == 2:
        from .special_catalog import validate_relation
        payload = observation.to_dict()
        validate_relation(payload['provider_identity'], payload['catalog'], payload['relation'])
    primary = plan.work_identity.primary_provider if plan.work_identity else None
    if (primary is not None and primary.status == 'confirmed' and primary.namespace == 'bangumi'
            and primary.entity_type == 'subject' and observation.provider_namespace == 'bangumi'
            and observation.provider_entity_type == 'subject' and observation.provider_id == str(primary.provider_id)):
        return
    if (observation.provider_namespace != 'tmdb' or observation.provider_entity_type != 'tv'
            or observation.provider_id not in special_provider_ids(plan, resolution)):
        raise ProviderSpecialEvidenceError('Provider-special observation does not match the Plan authority')


def official_special_rows(plan: RenamePlan, identifier: str, *, include_omitted=False):
    """An explicit user may match a video even when the classifier is unsure.

    This reads actual frozen provider rows, not the automatic recommendation
    dictionary. The sole special projector may replay omitted stored choices;
    the UI and new edits are restricted to the retained videos.
    """
    videos = [e for e in plan.entries if _entry_id(e) == identifier and e.media_kind == 'video']
    if len(videos) != 1 or (not include_omitted and videos[0].source_path in plan.ignored_sources):
        return ()
    value = plan.metadata.get('provider_special_observation')
    if value is None:
        return ()
    frozen = ProviderSpecialObservation.from_mapping(value)
    validate_special_identity(plan, frozen)
    return frozen.episodes if frozen.output_candidates_available else ()


def project_provider_special_evidence(
    plan: RenamePlan,
    observation: Mapping[str, object] | ProviderSpecialObservation,
    *,
    selected_subject_id: str = "",
    provider_link_resolution: object = None,
) -> RenamePlan:
    """Project one frozen observation into a Plan without choosing for users."""

    frozen = (
        observation
        if isinstance(observation, ProviderSpecialObservation)
        else ProviderSpecialObservation.from_mapping(observation)
    )
    validate_special_identity(plan, frozen, provider_link_resolution)

    assignments = dict(
        getattr(provider_link_resolution, "season_assignments", {}) or {}
    )
    selected_season = assignments.get(str(selected_subject_id or ""))
    owned_subject_ids = {
        str(subject_id)
        for subject_id, season_number in assignments.items()
        if isinstance(selected_season, int)
        and not isinstance(selected_season, bool)
        and season_number == selected_season
    }
    derived_ranges = tuple(
        getattr(provider_link_resolution, "derived_episode_ranges", ()) or ()
    )
    tmdb_ids = special_provider_ids(plan, provider_link_resolution)
    selected_ranges = tuple(
        row for row in derived_ranges
        if isinstance(row, Mapping)
        and str(row.get("bangumi_subject_id") or "") in owned_subject_ids
        and str(row.get("tmdb_tv_id") or "") in tmdb_ids
        and row.get("season_number") == selected_season
        and isinstance(row.get("episode_start"), int)
        and not isinstance(row.get("episode_start"), bool)
        and isinstance(row.get("episode_end"), int)
        and not isinstance(row.get("episode_end"), bool)
        and int(row["episode_start"]) >= 1
        and int(row["episode_end"]) >= int(row["episode_start"])
    )
    if selected_ranges and {
        str(row.get("bangumi_subject_id") or "") for row in selected_ranges
    } != owned_subject_ids:
        raise ProviderSpecialEvidenceError(
            "Decision-owned episode ranges are incomplete"
        )
    owned_ranges = tuple(
        (int(row["episode_start"]), int(row["episode_end"]))
        for row in selected_ranges
    )
    overflow_videos: dict[
        str, tuple[PlanEntry, tuple[tuple[int, int], ...]]
    ] = {}
    if owned_ranges and isinstance(selected_season, int):
        overflow_videos = {
            str(entry.source_path): (entry, owned_ranges)
            for entry in plan.entries
            if entry.media_kind == "video"
            and entry.season_id == selected_season
            and isinstance(entry.episode_id, int)
            and not isinstance(entry.episode_id, bool)
            and not any(
                start <= int(entry.episode_id) <= end
                for start, end in owned_ranges
            )
            and entry.presentation_asset is not None
            and entry.presentation_asset.asset_role.kind == "content_version"
        }

    candidates: dict[str, tuple[Mapping[str, object], ...]] = {}
    eligibility_unavailable: set[str] = set()
    for entry in plan.entries:
        if entry.media_kind != "video":
            continue
        eligibility = _provider_special_eligibility(entry)
        if eligibility in {"not_applicable", "clear_extra"}:
            continue
        identifier = _entry_id(entry)
        if eligibility == "eligible":
            if frozen.output_candidates_available:
                candidates[identifier] = frozen.episodes
        else:
            eligibility_unavailable.add(identifier)
    if frozen.output_candidates_available:
        for entry, _owned_ranges in overflow_videos.values():
            candidates[_entry_id(entry)] = frozen.episodes

    metadata = dict(plan.metadata)
    metadata.pop("provider_special_candidates", None)
    metadata.pop("provider_special_eligibility_unavailable", None)
    metadata["provider_special_observation"] = frozen.to_dict()
    if candidates:
        metadata["provider_special_candidates"] = candidates
    if eligibility_unavailable:
        metadata["provider_special_eligibility_unavailable"] = tuple(
            sorted(eligibility_unavailable)
        )

    if overflow_videos and frozen.output_candidates_available:
        work_root = Path(str(
            plan.metadata.get("canonical_work_root")
            or plan.metadata.get("target_root")
            or ""
        ))
        entries = list(plan.entries)
        overflow_targets: dict[str, Path] = {}
        overflow_rows: list[dict[str, object]] = []
        for index, entry in enumerate(entries):
            overflow = overflow_videos.get(str(entry.source_path))
            if overflow is None:
                continue
            _original, entry_owned_ranges = overflow
            stem = normalize_target_stem(entry.source_path.stem, work_root.name)
            target = (
                work_root / jellyfin_season_dir(int(entry.season_id or 1))
                / JELLYFIN_EXTRAS_DIR
                / f"{stem or entry.source_path.stem}{entry.source_path.suffix.lower()}"
            )
            entries[index] = replace(entry, target_path=target, requires_review=True)
            overflow_targets[str(entry.source_path)] = target
            overflow_rows.append({
                "entry_id": _entry_id(entry),
                "source_path": str(entry.source_path),
                "projected_episode": int(entry.episode_id),
                "episode_start": min(start for start, _end in entry_owned_ranges),
                "episode_end": max(end for _start, end in entry_owned_ranges),
                "owned_ranges": tuple({
                    "episode_start": start,
                    "episode_end": end,
                } for start, end in entry_owned_ranges),
            })
        for index, entry in enumerate(entries):
            subtitle = entry.subtitle
            owner = (
                str(subtitle.associated_video_source_path)
                if subtitle is not None
                and subtitle.associated_video_source_path is not None
                else ""
            )
            target_video = overflow_targets.get(owner)
            if subtitle is None or target_video is None:
                continue
            entries[index] = replace(
                entry,
                target_path=format_subtitle_target_fields(
                    target_video=target_video,
                    source_subtitle=entry.source_path,
                    primary_language=subtitle.primary_language or "",
                    script=subtitle.script,
                    track_title_tokens=subtitle.track_title_tokens,
                    flags=subtitle.flags,
                ),
                requires_review=True,
            )
        metadata["provider_regular_episode_overflows"] = tuple(
            sorted(overflow_rows, key=lambda row: str(row["source_path"]).casefold())
        )
        return replace(plan, entries=tuple(entries), metadata=metadata)

    return replace(plan, metadata=metadata)
