"""Pure contracts for associating external subtitles with known videos.

S0 deliberately does not parse language labels or touch the filesystem.  It
only answers whether a subtitle filename belongs to exactly one video that was
already accepted by the video mapping pipeline, and preserves the remaining
label text for the later S1 parser.
"""

from __future__ import annotations

import unicodedata
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

from .utils import VIDEO_SUFFIX


PathLike = Union[str, Path]
SUBTITLE_ASSOCIATION_SCHEMA_VERSION = 1
SUBTITLE_ASSOCIATION_RULE_VERSION = "0.3.0-dev.s0.1"
SUBTITLE_LABEL_SCHEMA_VERSION = 1
SUBTITLE_LABEL_RULE_VERSION = "0.3.0-dev.s1.2"
SUBTITLE_SUFFIXES = frozenset({'.ass', '.ssa', '.srt', '.vtt', '.sup', '.mks', '.idx', '.sub'})


class AssociationStatus(str, Enum):
    """Status of the independent subtitle-to-video association dimension."""

    CONFIRMED = "confirmed"
    NO_VIDEO = "no_video"
    MULTIPLE_VIDEOS = "multiple_videos"
    INVALID_SUBTITLE = "invalid_subtitle"


class DimensionResolution(str, Enum):
    """Resolution state for one safety dimension, not overall execution."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    CONFLICT = "conflict"


class LabelParseStatus(str, Enum):
    """S1 parse outcome; it is independent from association and target safety."""

    RESOLVED = "resolved"
    NEEDS_REVIEW = "needs_review"
    CONFLICT = "conflict"
    EMPTY = "empty"


class LabelConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TokenRole(str, Enum):
    LANGUAGE = "language"
    COMPOSITE_LANGUAGE = "composite_language"
    AMBIGUOUS_LANGUAGE = "ambiguous_language"
    FLAG = "flag"
    TITLE = "title"
    VERSION = "version"
    UNKNOWN = "unknown"


class NormalizationKind(str, Enum):
    EXACT = "exact"
    CASEFOLD = "casefold"
    WIDTH_FOLD = "width_fold"
    COMPATIBILITY = "compatibility"


@dataclass(frozen=True)
class TokenDecision:
    """Traceable classification of one complete, non-overlapping label atom."""

    raw: str
    normalized: str
    span_start: int
    span_end: int
    role: TokenRole
    semantic_ids: Tuple[str, ...] = ()
    confidence: LabelConfidence = LabelConfidence.HIGH
    reason_codes: Tuple[str, ...] = ()
    normalization_kind: NormalizationKind = NormalizationKind.EXACT

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantic_ids", tuple(self.semantic_ids))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))

    def to_dict(self) -> Dict[str, object]:
        return {
            "raw": self.raw,
            "normalized": self.normalized,
            "span_start": self.span_start,
            "span_end": self.span_end,
            "role": self.role.value,
            "semantic_ids": list(self.semantic_ids),
            "confidence": self.confidence.value,
            "reason_codes": list(self.reason_codes),
            "normalization_kind": self.normalization_kind.value,
        }


@dataclass(frozen=True)
class SubtitleLabelResult:
    """Immutable S1 result. Parsing never reads subtitle contents or files.

    ``detected_languages`` is the ordered, deduplicated set of every language
    evidenced by tokens, including the primary and additional languages.
    ``additional_languages`` is an ordered subset explicitly marked as
    non-primary; consumers must not concatenate the two collections.
    """

    raw_label: str
    tokens: Tuple[TokenDecision, ...]
    status: LabelParseStatus
    resolution_state: DimensionResolution
    primary_language: Optional[str] = None
    script: Optional[str] = None
    region_hint: Optional[str] = None
    detected_languages: Tuple[str, ...] = ()
    additional_languages: Tuple[str, ...] = ()
    track_title_tokens: Tuple[str, ...] = ()
    flags: Tuple[str, ...] = ()
    unknown_tokens: Tuple[str, ...] = ()
    confidence: LabelConfidence = LabelConfidence.HIGH
    reason_codes: Tuple[str, ...] = ()
    rule_version: str = SUBTITLE_LABEL_RULE_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "tokens", "detected_languages", "additional_languages",
            "track_title_tokens", "flags", "unknown_tokens", "reason_codes",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))

    @property
    def blocks_execution(self) -> bool:
        return self.resolution_state != DimensionResolution.RESOLVED

    @property
    def raw_tokens(self) -> Tuple[str, ...]:
        return tuple(token.raw for token in self.tokens)

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": SUBTITLE_LABEL_SCHEMA_VERSION,
            "rule_version": self.rule_version,
            "raw_label": self.raw_label,
            "tokens": [token.to_dict() for token in self.tokens],
            "raw_tokens": list(self.raw_tokens),
            "status": self.status.value,
            "resolution_state": self.resolution_state.value,
            "primary_language": self.primary_language,
            "script": self.script,
            "region_hint": self.region_hint,
            "detected_languages": list(self.detected_languages),
            "additional_languages": list(self.additional_languages),
            "track_title_tokens": list(self.track_title_tokens),
            "flags": list(self.flags),
            "unknown_tokens": list(self.unknown_tokens),
            "confidence": self.confidence.value,
            "reason_codes": list(self.reason_codes),
            "blocks_execution": self.blocks_execution,
        }


@dataclass(frozen=True)
class SubtitleAssociationResult:
    """Immutable, JSON-friendly result of S0 filename association.

    ``candidate_videos`` contains only the longest tied candidates.  Multiple
    subtitles may independently resolve to the same video; ambiguity means one
    subtitle has multiple equally valid video candidates, not that a video has
    multiple subtitle tracks.
    """

    subtitle_path: Path
    status: AssociationStatus
    resolution_state: DimensionResolution
    associated_video: Optional[Path] = None
    candidate_videos: Tuple[Path, ...] = ()
    raw_label: str = ""
    reason_codes: Tuple[str, ...] = ()
    rule_version: str = SUBTITLE_ASSOCIATION_RULE_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "subtitle_path", Path(self.subtitle_path))
        object.__setattr__(
            self,
            "associated_video",
            Path(self.associated_video) if self.associated_video else None,
        )
        object.__setattr__(
            self,
            "candidate_videos",
            tuple(Path(path) for path in self.candidate_videos),
        )
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))

    @property
    def blocks_execution(self) -> bool:
        """Whether association alone prevents later execution."""

        return self.resolution_state != DimensionResolution.RESOLVED

    def to_dict(self) -> Dict[str, object]:
        """Return the versioned S0 report contract without writing it."""

        return {
            "schema_version": SUBTITLE_ASSOCIATION_SCHEMA_VERSION,
            "rule_version": self.rule_version,
            "subtitle_path": str(self.subtitle_path),
            "status": self.status.value,
            "resolution_state": self.resolution_state.value,
            "associated_video": (
                str(self.associated_video) if self.associated_video else None
            ),
            "candidate_videos": [str(path) for path in self.candidate_videos],
            "raw_label": self.raw_label,
            "reason_codes": list(self.reason_codes),
            "blocks_execution": self.blocks_execution,
        }


@dataclass(frozen=True)
class _CandidateMatch:
    path: Path
    raw_prefix_end: int
    normalized_stem_length: int
    normalized_only: bool


def associate_subtitle_to_video(
    subtitle_path: PathLike,
    video_paths: Iterable[PathLike],
) -> SubtitleAssociationResult:
    """Associate one subtitle with the longest unique confirmed video stem.

    The function is intentionally filename-only and filesystem-free.  Matching
    uses Unicode NFKC plus ``casefold`` for cross-platform consistency, while
    ``raw_label`` preserves the source spelling for later review.  Fuzzy title,
    episode and media-role inference are explicitly out of scope.
    """

    subtitle = Path(subtitle_path)
    subtitle_stem, subtitle_suffix = _split_normalized_extension(subtitle)
    if subtitle_suffix not in SUBTITLE_SUFFIXES:
        return SubtitleAssociationResult(
            subtitle_path=subtitle,
            status=AssociationStatus.INVALID_SUBTITLE,
            resolution_state=DimensionResolution.UNRESOLVED,
            reason_codes=("ASSOC_UNSUPPORTED_SUBTITLE_EXTENSION",),
        )

    matches = []
    seen_paths = set()
    for raw_video in video_paths:
        video = Path(raw_video)
        if video.parent != subtitle.parent or video in seen_paths:
            continue
        seen_paths.add(video)
        video_stem, video_suffix = _split_normalized_extension(video)
        if video_suffix not in VIDEO_SUFFIX:
            continue
        prefix_end = _matching_prefix_end(subtitle_stem, video_stem)
        if prefix_end is None:
            continue
        matches.append(
            _CandidateMatch(
                path=video,
                raw_prefix_end=prefix_end,
                normalized_stem_length=len(_canonical(video_stem)),
                normalized_only=(
                    subtitle_stem[:prefix_end] != video_stem
                ),
            )
        )

    if not matches:
        return SubtitleAssociationResult(
            subtitle_path=subtitle,
            status=AssociationStatus.NO_VIDEO,
            resolution_state=DimensionResolution.UNRESOLVED,
            reason_codes=("ASSOC_NO_VIDEO",),
        )

    longest = max(match.normalized_stem_length for match in matches)
    finalists = tuple(
        sorted(
            (
                match
                for match in matches
                if match.normalized_stem_length == longest
            ),
            key=lambda match: (
                _canonical(str(match.path)),
                str(match.path),
            ),
        )
    )
    if len(finalists) != 1:
        return SubtitleAssociationResult(
            subtitle_path=subtitle,
            status=AssociationStatus.MULTIPLE_VIDEOS,
            resolution_state=DimensionResolution.CONFLICT,
            candidate_videos=tuple(match.path for match in finalists),
            reason_codes=("ASSOC_MULTIPLE_LONGEST_VIDEOS",),
        )

    selected = finalists[0]
    raw_label = _strip_leading_label_separators(
        subtitle_stem[selected.raw_prefix_end:]
    )
    reasons = ["ASSOC_EXACT_VIDEO_STEM_BOUNDARY"]
    if selected.normalized_only:
        reasons.append("ASSOC_NFKC_CASEFOLD_MATCH")
    if len(matches) > 1:
        reasons.append("ASSOC_LONGEST_VIDEO_STEM")
    if not raw_label:
        reasons.append("TAG_EMPTY")

    return SubtitleAssociationResult(
        subtitle_path=subtitle,
        status=AssociationStatus.CONFIRMED,
        resolution_state=DimensionResolution.RESOLVED,
        associated_video=selected.path,
        candidate_videos=(selected.path,),
        raw_label=raw_label,
        reason_codes=tuple(reasons),
    )


# Exact token aliases only.  Compound aliases are protected before generic
# separators can split them; substring matching is deliberately forbidden.
_LANGUAGE_ALIASES = {
    "sc": ("language:zh", "script:Hans"),
    "chs": ("language:zh", "script:Hans"),
    "gb": ("language:zh", "script:Hans"),
    "gbk": ("language:zh", "script:Hans"),
    "gb2312": ("language:zh", "script:Hans"),
    "简": ("language:zh", "script:Hans"),
    "简体": ("language:zh", "script:Hans"),
    "简中": ("language:zh", "script:Hans"),
    "zh-hans": ("language:zh", "script:Hans"),
    "zh_hans": ("language:zh", "script:Hans"),
    "zh-cn": ("language:zh", "script:Hans", "region:CN"),
    "zh_cn": ("language:zh", "script:Hans", "region:CN"),
    "tc": ("language:zh", "script:Hant"),
    "cht": ("language:zh", "script:Hant"),
    "big5": ("language:zh", "script:Hant"),
    "繁": ("language:zh", "script:Hant"),
    "繁体": ("language:zh", "script:Hant"),
    "繁中": ("language:zh", "script:Hant"),
    "正體": ("language:zh", "script:Hant"),
    "zh-hant": ("language:zh", "script:Hant"),
    "zh_hant": ("language:zh", "script:Hant"),
    "zh-tw": ("language:zh", "script:Hant", "region:TW"),
    "zh_tw": ("language:zh", "script:Hant", "region:TW"),
    "zh-hk": ("language:zh", "script:Hant", "region:HK"),
    "zh_hk": ("language:zh", "script:Hant", "region:HK"),
    "zh": ("language:zh",),
    "chi": ("language:zh",),
    "zho": ("language:zh",),
    "chinese": ("language:zh",),
    "中文": ("language:zh",),
    "中字": ("language:zh",),
    "ja": ("language:ja",),
    "jpn": ("language:ja",),
    "日语": ("language:ja",),
    "日文": ("language:ja",),
    "en": ("language:en",),
    "eng": ("language:en",),
    "english": ("language:en",),
    "英语": ("language:en",),
    "英文": ("language:en",),
    "ko": ("language:ko",),
    "kor": ("language:ko",),
    "korean": ("language:ko",),
    "韩语": ("language:ko",),
    "韩文": ("language:ko",),
}

_COMPOSITE_ALIASES = {
    "jpsc": ("language:zh", "script:Hans", "additional:ja", "title:日简双语"),
    "jpchs": ("language:zh", "script:Hans", "additional:ja", "title:日简双语"),
    "jpnchs": ("language:zh", "script:Hans", "additional:ja", "title:日简双语"),
    "jp_sc": ("language:zh", "script:Hans", "additional:ja", "title:日简双语"),
    "jp+sc": ("language:zh", "script:Hans", "additional:ja", "title:日简双语"),
    "jptc": ("language:zh", "script:Hant", "additional:ja", "title:日繁双语"),
    "jpcht": ("language:zh", "script:Hant", "additional:ja", "title:日繁双语"),
    "jpncht": ("language:zh", "script:Hant", "additional:ja", "title:日繁双语"),
    "jp_tc": ("language:zh", "script:Hant", "additional:ja", "title:日繁双语"),
    "jp+tc": ("language:zh", "script:Hant", "additional:ja", "title:日繁双语"),
}

_AMBIGUOUS_LANGUAGE_ALIASES = {
    "cn": ("language:zh", "region:CN"),
    "tw": ("language:zh", "region:TW"),
    "jp": ("language:ja", "region:JP"),
}

_FLAG_ALIASES = {
    "default": "default",
    "forced": "forced",
    "foreign": "forced",
    "sdh": "sdh",
    "cc": "sdh",
}
_FLAG_ORDER = ("default", "forced", "sdh")
_BRACKETS = {"[": "]", "【": "】", "(": ")", "（": "）"}


def parse_subtitle_label(raw_label: str) -> SubtitleLabelResult:
    """Parse a filename label into a conservative, traceable S1 decision.

    NFKC/casefold is comparison-only.  Full-width ASCII is accepted as a safe
    width variant, while other compatibility decompositions are always sent to
    review even when their normalized spelling resembles a known alias.
    """

    if not isinstance(raw_label, str):
        raise TypeError("raw_label must be str")
    tokens = list(_lex_label(raw_label))
    if not tokens:
        return SubtitleLabelResult(
            raw_label=raw_label,
            tokens=(),
            status=LabelParseStatus.EMPTY,
            resolution_state=DimensionResolution.UNRESOLVED,
            confidence=LabelConfidence.LOW,
            reason_codes=("TAG_EMPTY",),
        )

    # ``hi`` is Hindi alone, but a hearing-impaired alias when another explicit
    # language exists.  This contextual rewrite preserves its raw token.
    has_other_language = any(
        token.role in {TokenRole.LANGUAGE, TokenRole.COMPOSITE_LANGUAGE}
        for token in tokens
        if token.normalized != "hi"
    )
    contextual_tokens = []
    for token in tokens:
        if token.normalized != "hi":
            contextual_tokens.append(token)
            continue
        if has_other_language:
            contextual_tokens.append(_replace_token(
                token,
                role=TokenRole.FLAG,
                semantic_ids=("flag:sdh",),
                reason_codes=token.reason_codes + ("FLAG_HI_CONTEXT",),
            ))
        else:
            contextual_tokens.append(_replace_token(
                token,
                role=TokenRole.LANGUAGE,
                semantic_ids=("language:hi",),
                reason_codes=token.reason_codes + ("LANG_HINDI_ALIAS",),
            ))
    tokens = contextual_tokens

    reasons: List[str] = []
    languages: List[str] = []
    scripts: List[str] = []
    regions: List[str] = []
    additional: List[str] = []
    titles: List[str] = []
    flags: List[str] = []
    unknown: List[str] = []
    has_composite = False
    needs_review = False

    for token in tokens:
        if token.normalization_kind == NormalizationKind.COMPATIBILITY:
            _append_unique(reasons, "TAG_UNSAFE_COMPATIBILITY_NORMALIZATION")
            needs_review = True
        if token.confidence != LabelConfidence.HIGH:
            needs_review = True
            _append_unique(reasons, "TAG_TOKEN_NEEDS_REVIEW")
        if token.role == TokenRole.COMPOSITE_LANGUAGE:
            has_composite = True
        if token.role == TokenRole.AMBIGUOUS_LANGUAGE:
            needs_review = True
            _append_unique(reasons, "TAG_AMBIGUOUS_LANGUAGE_ALIAS")
        if token.role == TokenRole.UNKNOWN:
            unknown.append(token.raw)
            needs_review = True
            _append_unique(reasons, "TAG_UNKNOWN_TOKEN")
        if token.role in {TokenRole.TITLE, TokenRole.VERSION}:
            titles.append(token.raw)
        for semantic in token.semantic_ids:
            kind, _, value = semantic.partition(":")
            if kind == "language":
                _append_unique(languages, value)
            elif kind == "script":
                _append_unique(scripts, value)
            elif kind == "region":
                _append_unique(regions, value)
            elif kind == "additional":
                _append_unique(additional, value)
                _append_unique(languages, value)
            elif kind == "title" and token.role == TokenRole.COMPOSITE_LANGUAGE:
                _append_unique(titles, value)
            elif kind == "flag":
                _append_unique(flags, value)

    conflict = False
    if len(scripts) > 1:
        conflict = True
        _append_unique(reasons, "TAG_SCRIPT_CONFLICT")
    if len(regions) > 1:
        conflict = True
        _append_unique(reasons, "TAG_REGION_CONFLICT")
    if scripts and regions:
        incompatible = (
            ("Hans" in scripts and any(r in {"TW", "HK"} for r in regions))
            or ("Hant" in scripts and "CN" in regions)
        )
        if incompatible:
            conflict = True
            _append_unique(reasons, "TAG_SCRIPT_REGION_CONFLICT")

    primary: Optional[str] = None
    if has_composite:
        primary = "zh"
        if any(language not in {"zh", "ja"} for language in languages):
            needs_review = True
            _append_unique(reasons, "TAG_EXTRA_LANGUAGE_NEEDS_REVIEW")
    elif len(languages) == 1:
        primary = languages[0]
    elif len(languages) > 1:
        needs_review = True
        _append_unique(reasons, "TAG_MULTIPLE_LANGUAGES_NEED_REVIEW")

    if not languages:
        needs_review = True
        _append_unique(reasons, "TAG_LANGUAGE_MISSING")
    if primary == "zh" and not scripts:
        needs_review = True
        _append_unique(reasons, "TAG_CHINESE_SCRIPT_UNKNOWN")

    if conflict:
        status = LabelParseStatus.CONFLICT
        resolution = DimensionResolution.CONFLICT
        confidence = LabelConfidence.LOW
    elif needs_review:
        status = LabelParseStatus.NEEDS_REVIEW
        resolution = DimensionResolution.UNRESOLVED
        confidence = LabelConfidence.MEDIUM if languages else LabelConfidence.LOW
    else:
        status = LabelParseStatus.RESOLVED
        resolution = DimensionResolution.RESOLVED
        confidence = LabelConfidence.HIGH
        _append_unique(reasons, "TAG_RESOLVED_EXACT_ALIAS")

    ordered_flags = tuple(flag for flag in _FLAG_ORDER if flag in flags)
    return SubtitleLabelResult(
        raw_label=raw_label,
        tokens=tuple(tokens),
        status=status,
        resolution_state=resolution,
        primary_language=primary,
        script=scripts[0] if len(scripts) == 1 else None,
        region_hint=regions[0] if len(regions) == 1 else None,
        detected_languages=tuple(languages),
        additional_languages=tuple(additional),
        track_title_tokens=tuple(titles),
        flags=ordered_flags,
        unknown_tokens=tuple(unknown),
        confidence=confidence,
        reason_codes=tuple(reasons),
    )


def _lex_label(raw_label: str) -> Tuple[TokenDecision, ...]:
    tokens: List[TokenDecision] = []
    index = 0
    while index < len(raw_label):
        if _is_token_separator(raw_label[index]):
            index += 1
            continue
        start = index
        opener = raw_label[index]
        malformed = False
        if opener in _BRACKETS:
            closer = _BRACKETS[opener]
            close_index = raw_label.find(closer, index + 1)
            if close_index < 0:
                boundary = next(
                    (
                        position for position in range(index + 1, len(raw_label))
                        if _is_token_separator(raw_label[position])
                    ),
                    len(raw_label),
                )
                end = boundary
                content = raw_label[index + 1:end]
                malformed = True
            else:
                end = close_index + 1
                content = raw_label[index + 1:close_index]
            raw = raw_label[start:end]
            index = end
            token = _classify_token(raw, content, start, end, bracketed=True)
            if malformed:
                token = _replace_token(
                    token,
                    role=TokenRole.UNKNOWN,
                    semantic_ids=(),
                    confidence=LabelConfidence.LOW,
                    reason_codes=token.reason_codes + ("TAG_UNCLOSED_BRACKET",),
                )
            tokens.append(token)
            continue

        while index < len(raw_label):
            if raw_label[index] in _BRACKETS or _is_token_separator(raw_label[index]):
                break
            index += 1
        raw = raw_label[start:index]
        tokens.append(_classify_token(raw, raw, start, index, bracketed=False))
    return tuple(tokens)


def _classify_token(
    raw: str,
    content: str,
    start: int,
    end: int,
    *,
    bracketed: bool,
) -> TokenDecision:
    normalized = _canonical(content)
    normalization_kind = _normalization_kind(content)
    common = {
        "raw": raw,
        "normalized": normalized,
        "span_start": start,
        "span_end": end,
        "normalization_kind": normalization_kind,
    }
    if normalized in _COMPOSITE_ALIASES:
        return TokenDecision(
            **common,
            role=TokenRole.COMPOSITE_LANGUAGE,
            semantic_ids=_COMPOSITE_ALIASES[normalized],
            reason_codes=("TAG_COMPOSITE_LANGUAGE_ALIAS",),
        )
    if normalized in _LANGUAGE_ALIASES:
        return TokenDecision(
            **common,
            role=TokenRole.LANGUAGE,
            semantic_ids=_LANGUAGE_ALIASES[normalized],
            reason_codes=("TAG_LANGUAGE_ALIAS",),
        )
    if normalized in _AMBIGUOUS_LANGUAGE_ALIASES:
        return TokenDecision(
            **common,
            role=TokenRole.AMBIGUOUS_LANGUAGE,
            semantic_ids=_AMBIGUOUS_LANGUAGE_ALIASES[normalized],
            confidence=LabelConfidence.MEDIUM,
            reason_codes=("TAG_AMBIGUOUS_LANGUAGE_ALIAS",),
        )
    if normalized in _FLAG_ALIASES:
        return TokenDecision(
            **common,
            role=TokenRole.FLAG,
            semantic_ids=(f"flag:{_FLAG_ALIASES[normalized]}",),
            reason_codes=("TAG_FLAG_ALIAS",),
        )
    if normalized == "hi":
        return TokenDecision(
            **common,
            role=TokenRole.AMBIGUOUS_LANGUAGE,
            confidence=LabelConfidence.MEDIUM,
            reason_codes=("TAG_HI_CONTEXT_PENDING",),
        )
    if re.fullmatch(r"v\d+(?:\.\d+)*", normalized):
        return TokenDecision(
            **common,
            role=TokenRole.VERSION,
            semantic_ids=(f"version:{normalized}",),
            reason_codes=("TAG_VERSION_TOKEN",),
        )
    if re.fullmatch(r"rev\d*", normalized) or normalized in {
        "fix", "fixed", "dialogue", "dialog", "signs", "commentary", "特效",
    }:
        return TokenDecision(
            **common,
            role=TokenRole.TITLE,
            semantic_ids=(f"title:{content}",),
            reason_codes=("TAG_TRACK_TITLE_TOKEN",),
        )
    if bracketed and content:
        return TokenDecision(
            **common,
            role=TokenRole.TITLE,
            semantic_ids=(f"title:{content}",),
            confidence=LabelConfidence.MEDIUM,
            reason_codes=("TAG_BRACKETED_TITLE_CANDIDATE",),
        )
    return TokenDecision(
        **common,
        role=TokenRole.UNKNOWN,
        confidence=LabelConfidence.LOW,
        reason_codes=("TAG_UNKNOWN_TOKEN",),
    )


def _replace_token(token: TokenDecision, **changes: object) -> TokenDecision:
    values = {
        "raw": token.raw,
        "normalized": token.normalized,
        "span_start": token.span_start,
        "span_end": token.span_end,
        "role": token.role,
        "semantic_ids": token.semantic_ids,
        "confidence": token.confidence,
        "reason_codes": token.reason_codes,
        "normalization_kind": token.normalization_kind,
    }
    values.update(changes)
    return TokenDecision(**values)  # type: ignore[arg-type]


def _normalization_kind(value: str) -> NormalizationKind:
    normalized = unicodedata.normalize("NFKC", value)
    if normalized == value:
        if value.casefold() == value:
            return NormalizationKind.EXACT
        return NormalizationKind.CASEFOLD
    width_folded = "".join(_fold_width_character(character) for character in value)
    if width_folded == normalized:
        return NormalizationKind.WIDTH_FOLD
    return NormalizationKind.COMPATIBILITY


def _fold_width_character(character: str) -> str:
    codepoint = ord(character)
    if 0xFF01 <= codepoint <= 0xFF5E:
        return chr(codepoint - 0xFEE0)
    if codepoint == 0x3000:
        return " "
    return character


def _is_token_separator(character: str) -> bool:
    normalized = unicodedata.normalize("NFKC", character)
    return normalized.isspace() or normalized in {".", "。", "·"}


def _append_unique(values: List[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _canonical(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _split_normalized_extension(path: Path) -> Tuple[str, str]:
    """Return the raw stem and normalized suffix, accepting full-width dots."""

    raw_name = path.name
    normalized_name = unicodedata.normalize("NFKC", raw_name)
    dot = normalized_name.rfind(".")
    if dot < 0:
        return raw_name, ""
    normalized_stem = normalized_name[:dot].casefold()
    suffix = normalized_name[dot:].casefold()
    matching_ends = [
        end
        for end in range(len(raw_name) + 1)
        if _canonical(raw_name[:end]) == normalized_stem
    ]
    if not matching_ends:
        return raw_name, ""
    return raw_name[:max(matching_ends)], suffix


def _matching_prefix_end(subtitle_stem: str, video_stem: str) -> Optional[int]:
    """Return the raw subtitle index after a normalized video-stem match."""

    normalized_video = _canonical(video_stem)
    if not normalized_video:
        return None
    matching_ends = []
    for end in range(1, len(subtitle_stem) + 1):
        if _canonical(subtitle_stem[:end]) != normalized_video:
            continue
        if end == len(subtitle_stem) or _is_label_boundary(subtitle_stem[end]):
            matching_ends.append(end)
    return max(matching_ends) if matching_ends else None


def _is_label_boundary(character: str) -> bool:
    normalized = unicodedata.normalize("NFKC", character)
    return normalized.isspace() or normalized in {".", "_", "-", "。", "[", "(", "【"}


def _strip_leading_label_separators(value: str) -> str:
    index = 0
    while index < len(value):
        normalized = unicodedata.normalize("NFKC", value[index])
        if not (normalized.isspace() or normalized in {".", "_", "-", "。"}):
            break
        index += 1
    return value[index:]
