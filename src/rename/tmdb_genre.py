"""Pure TMDB genre evidence classification.

TMDB's numeric genre identity is stable across locales while the display name
is not.  This module classifies provider payloads once, without network or
configuration access, so every consumer can preserve the same tri-state and
provenance instead of reinterpreting localized names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


TMDB_ANIMATION_POLICY_VERSION = "tmdb-animation-evidence-v1"
CONFIRMED_ANIMATION = "confirmed_animation"
CONFIRMED_NON_ANIMATION = "confirmed_non_animation"
UNKNOWN_OR_CONFLICT = "unknown_or_conflict"
_ANIMATION_GENRE_ID = 16
_LEGACY_ANIMATION_NAMES = frozenset({
    "animation",
    "anime",
    "动画",
    "动漫",
})


@dataclass(frozen=True)
class TMDBAnimationEvidence:
    """One auditable tri-state classification result."""

    status: str
    policy_version: str
    evidence_source: str
    reason_code: str

    @property
    def is_anime(self) -> Optional[bool]:
        if self.status == CONFIRMED_ANIMATION:
            return True
        if self.status == CONFIRMED_NON_ANIMATION:
            return False
        return None


def _iter_payload(value: object) -> tuple[object, ...]:
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return ()
    try:
        return tuple(value)  # type: ignore[arg-type]
    except TypeError:
        return ()


def _strict_ids(values: object) -> tuple[int, ...]:
    return tuple(
        value
        for value in _iter_payload(values)
        if type(value) is int
    )


def classify_tmdb_animation_evidence(
    *,
    genres: object = (),
    genre_ids: object = (),
) -> TMDBAnimationEvidence:
    """Classify TMDB detail/search genre evidence without guessing.

    ``genres`` is the detail shape (mappings with ``id``/``name``), while
    ``genre_ids`` is the search shape.  Numeric sources outrank display names;
    display names are only a compatibility fallback when neither numeric
    source contains a usable strict integer ID.
    """

    detail_ids = []
    genre_names = []
    for item in _iter_payload(genres):
        if not isinstance(item, Mapping):
            continue
        genre_id = item.get("id")
        if type(genre_id) is int:
            detail_ids.append(genre_id)
        name = str(item.get("name") or "").strip().casefold()
        if name:
            genre_names.append(name)

    search_ids = _strict_ids(genre_ids)
    detail_numeric = bool(detail_ids)
    search_numeric = bool(search_ids)
    detail_animation = _ANIMATION_GENRE_ID in detail_ids
    search_animation = _ANIMATION_GENRE_ID in search_ids
    legacy_animation = bool(
        _LEGACY_ANIMATION_NAMES.intersection(genre_names)
    )

    if (
        detail_numeric
        and search_numeric
        and detail_animation != search_animation
    ):
        return TMDBAnimationEvidence(
            UNKNOWN_OR_CONFLICT,
            TMDB_ANIMATION_POLICY_VERSION,
            "numeric_conflict",
            "detail_search_id16_conflict",
        )

    if detail_numeric or search_numeric:
        source = (
            "detail_and_search_genre_id"
            if detail_numeric and search_numeric
            else (
                "detail_genre_id" if detail_numeric else "search_genre_id"
            )
        )
        if detail_animation or search_animation:
            return TMDBAnimationEvidence(
                CONFIRMED_ANIMATION,
                TMDB_ANIMATION_POLICY_VERSION,
                source,
                "genre_id_16_present",
            )
        if legacy_animation:
            return TMDBAnimationEvidence(
                UNKNOWN_OR_CONFLICT,
                TMDB_ANIMATION_POLICY_VERSION,
                "numeric_name_conflict",
                "numeric_non_animation_name_conflict",
            )
        return TMDBAnimationEvidence(
            CONFIRMED_NON_ANIMATION,
            TMDB_ANIMATION_POLICY_VERSION,
            source,
            "genre_id_16_absent",
        )

    if genre_names:
        return TMDBAnimationEvidence(
            (
                CONFIRMED_ANIMATION
                if legacy_animation else CONFIRMED_NON_ANIMATION
            ),
            TMDB_ANIMATION_POLICY_VERSION,
            "legacy_name_fallback",
            (
                "legacy_animation_name_present"
                if legacy_animation else "legacy_animation_name_absent"
            ),
        )

    return TMDBAnimationEvidence(
        UNKNOWN_OR_CONFLICT,
        TMDB_ANIMATION_POLICY_VERSION,
        "no_usable_genre_evidence",
        "no_usable_genre_evidence",
    )
