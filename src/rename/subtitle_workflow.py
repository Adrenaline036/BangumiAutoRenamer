"""Read-only subtitle decisions built from already confirmed video mappings.

This module is intentionally filesystem-free.  Callers provide subtitle paths,
the frozen source-video -> target-video mapping, and (optionally) an explicit
snapshot of existing target paths.  It never discovers files or mutates media.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple, Union

from .subtitle_labels import (
    DimensionResolution,
    LabelParseStatus,
    SubtitleAssociationResult,
    SubtitleLabelResult,
    associate_subtitle_to_video,
    parse_subtitle_label,
)


PathLike = Union[str, Path]
SUBTITLE_WORKFLOW_SCHEMA_VERSION = 1
SUBTITLE_WORKFLOW_RULE_VERSION = "0.3.0-dev.s2.1"


@dataclass(frozen=True)
class VideoPathMapping:
    """The S0 integration contract: associate by source, format by target."""

    source_path: Path
    target_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_path", Path(self.source_path))
        object.__setattr__(self, "target_path", Path(self.target_path))


@dataclass(frozen=True)
class SubtitleWorkflowDecision:
    source_subtitle: Path
    association: SubtitleAssociationResult
    label: SubtitleLabelResult
    associated_video_target: Optional[Path] = None
    target_preview: Optional[Path] = None
    target_resolution: DimensionResolution = DimensionResolution.UNRESOLVED
    reason_codes: Tuple[str, ...] = ()
    rule_version: str = SUBTITLE_WORKFLOW_RULE_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_subtitle", Path(self.source_subtitle))
        if self.associated_video_target is not None:
            object.__setattr__(
                self, "associated_video_target", Path(self.associated_video_target)
            )
        if self.target_preview is not None:
            object.__setattr__(self, "target_preview", Path(self.target_preview))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))

    @property
    def blocks_execution(self) -> bool:
        return any((
            self.association.resolution_state != DimensionResolution.RESOLVED,
            self.label.resolution_state != DimensionResolution.RESOLVED,
            self.target_resolution != DimensionResolution.RESOLVED,
        ))

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": SUBTITLE_WORKFLOW_SCHEMA_VERSION,
            "rule_version": self.rule_version,
            "source_subtitle": str(self.source_subtitle),
            "association": self.association.to_dict(),
            "label": self.label.to_dict(),
            "associated_video_target": (
                str(self.associated_video_target)
                if self.associated_video_target is not None else None
            ),
            "target_preview": (
                str(self.target_preview) if self.target_preview is not None else None
            ),
            "target_resolution": self.target_resolution.value,
            "reason_codes": list(self.reason_codes),
            "blocks_execution": self.blocks_execution,
        }


def build_subtitle_decisions(
    subtitle_paths: Iterable[PathLike],
    video_mappings: Iterable[VideoPathMapping],
    *,
    existing_target_paths: Iterable[PathLike] = (),
) -> Tuple[SubtitleWorkflowDecision, ...]:
    """Return deterministic, read-only decisions for an explicit input batch."""

    mappings = tuple(
        item if isinstance(item, VideoPathMapping) else VideoPathMapping(*item)
        for item in video_mappings
    )
    source_videos = tuple(mapping.source_path for mapping in mappings)
    targets_by_source: Dict[str, List[Path]] = {}
    for mapping in mappings:
        targets_by_source.setdefault(_path_key(mapping.source_path), []).append(
            mapping.target_path
        )
    existing_keys = {_path_key(Path(path)) for path in existing_target_paths}

    decisions: List[SubtitleWorkflowDecision] = []
    for raw_subtitle in subtitle_paths:
        subtitle = Path(raw_subtitle)
        association = associate_subtitle_to_video(subtitle, source_videos)
        label = parse_subtitle_label(association.raw_label)
        reasons = list(association.reason_codes) + list(label.reason_codes)
        target_video: Optional[Path] = None
        target_preview: Optional[Path] = None
        target_state = DimensionResolution.UNRESOLVED

        if association.associated_video is None:
            _append_unique(reasons, "TARGET_ASSOCIATION_UNRESOLVED")
        else:
            candidates = _unique_paths(
                targets_by_source.get(_path_key(association.associated_video), [])
            )
            if len(candidates) != 1:
                target_state = (
                    DimensionResolution.CONFLICT if candidates
                    else DimensionResolution.UNRESOLVED
                )
                _append_unique(
                    reasons,
                    "TARGET_VIDEO_MAPPING_CONFLICT" if candidates
                    else "TARGET_VIDEO_MAPPING_MISSING",
                )
            else:
                target_video = candidates[0]
                if label.status == LabelParseStatus.RESOLVED:
                    target_preview = format_subtitle_target(
                        target_video, subtitle, label
                    )
                    if _path_key(target_preview) in existing_keys:
                        target_state = DimensionResolution.CONFLICT
                        _append_unique(reasons, "TARGET_ALREADY_EXISTS")
                    else:
                        target_state = DimensionResolution.RESOLVED
                        _append_unique(reasons, "TARGET_PREVIEW_SAFE")
                else:
                    _append_unique(reasons, "TARGET_LABEL_UNRESOLVED")

        decisions.append(SubtitleWorkflowDecision(
            source_subtitle=subtitle,
            association=association,
            label=label,
            associated_video_target=target_video,
            target_preview=target_preview,
            target_resolution=target_state,
            reason_codes=tuple(reasons),
        ))

    decisions = _apply_batch_conflicts(decisions)
    return tuple(sorted(decisions, key=lambda item: (_path_key(item.source_subtitle), str(item.source_subtitle))))


def format_subtitle_target(
    target_video: PathLike,
    source_subtitle: PathLike,
    label: SubtitleLabelResult,
) -> Path:
    """Format the sole canonical Jellyfin-oriented target for a resolved label."""

    if label.status != LabelParseStatus.RESOLVED or not label.primary_language:
        raise ValueError("subtitle label must be resolved before formatting")
    return format_subtitle_target_fields(
        target_video=target_video,
        source_subtitle=source_subtitle,
        primary_language=label.primary_language,
        script=label.script,
        track_title_tokens=label.track_title_tokens,
        flags=label.flags,
    )


def format_subtitle_target_fields(
    *,
    target_video: PathLike,
    source_subtitle: PathLike,
    primary_language: str,
    script: Optional[str],
    track_title_tokens: Iterable[str] = (),
    flags: Iterable[str] = (),
) -> Path:
    """Shared canonical formatter for automatic and reviewed decisions."""

    video = Path(target_video)
    subtitle = Path(source_subtitle)
    language = _format_language(primary_language, script)
    parts = [video.stem, language]
    for title in track_title_tokens:
        cleaned = _clean_title_token(title)
        if cleaned:
            parts.append(cleaned)
    parts.extend(flags)
    normalized_name = unicodedata.normalize("NFKC", subtitle.name)
    dot = normalized_name.rfind(".")
    suffix = normalized_name[dot:].casefold() if dot >= 0 else ""
    return video.parent / (".".join(parts) + suffix)


def decision_to_plan_contract(decision: SubtitleWorkflowDecision):
    """Convert a read-only decision to the strict Plan v2 subtitle contract."""

    from .rename_plan import SubtitlePlanContract

    return SubtitlePlanContract(
        associated_video_source_path=decision.association.associated_video,
        association_candidates=decision.association.candidate_videos,
        raw_label=decision.label.raw_label,
        primary_language=decision.label.primary_language,
        script=decision.label.script,
        region_hint=decision.label.region_hint,
        additional_languages=decision.label.additional_languages,
        track_title_tokens=decision.label.track_title_tokens,
        flags=decision.label.flags,
        unknown_tokens=decision.label.unknown_tokens,
        association_state=decision.association.resolution_state,
        parse_state=decision.label.resolution_state,
        association_rule_version=decision.association.rule_version,
        label_rule_version=decision.label.rule_version,
        reason_codes=decision.reason_codes,
        decision_source="automatic",
    )


def _apply_batch_conflicts(
    decisions: List[SubtitleWorkflowDecision],
) -> List[SubtitleWorkflowDecision]:
    target_indexes: Dict[str, List[int]] = {}
    default_indexes: Dict[str, List[int]] = {}
    for index, decision in enumerate(decisions):
        if decision.target_preview is not None:
            target_indexes.setdefault(_path_key(decision.target_preview), []).append(index)
        if (
            decision.associated_video_target is not None
            and "default" in decision.label.flags
        ):
            default_indexes.setdefault(
                _path_key(decision.associated_video_target), []
            ).append(index)

    conflicts: Dict[int, List[str]] = {}
    for indexes in target_indexes.values():
        if len(indexes) > 1:
            for index in indexes:
                conflicts.setdefault(index, []).append("TARGET_BATCH_COLLISION")
    for indexes in default_indexes.values():
        if len(indexes) > 1:
            for index in indexes:
                conflicts.setdefault(index, []).append("TARGET_MULTIPLE_DEFAULT_TRACKS")

    for index, codes in conflicts.items():
        reasons = list(decisions[index].reason_codes)
        for code in codes:
            _append_unique(reasons, code)
        decisions[index] = replace(
            decisions[index],
            target_resolution=DimensionResolution.CONFLICT,
            reason_codes=tuple(reasons),
        )
    return decisions


def _format_language(language: str, script: Optional[str]) -> str:
    if language == "zh":
        if script not in {"Hans", "Hant"}:
            raise ValueError("Chinese subtitle script must be Hans or Hant")
        return f"zh-{script}"
    if script is not None:
        raise ValueError("Only Chinese subtitles may carry a script")
    return language


def _clean_title_token(value: str) -> str:
    if len(value) >= 2 and (value[0], value[-1]) in {
        ("[", "]"), ("【", "】"), ("(", ")"), ("（", "）"),
    }:
        value = value[1:-1]
    value = value.strip()
    if any(character in value for character in '<>:"/\\|?*'):
        raise ValueError("track title contains a filesystem-unsafe character")
    return value


def _unique_paths(paths: Iterable[Path]) -> Tuple[Path, ...]:
    found: Dict[str, Path] = {}
    for path in paths:
        found.setdefault(_path_key(path), path)
    return tuple(found[key] for key in sorted(found))


def _path_key(path: Path) -> str:
    return unicodedata.normalize("NFKC", str(path)).casefold()


def _append_unique(values: List[str], value: str) -> None:
    if value not in values:
        values.append(value)
