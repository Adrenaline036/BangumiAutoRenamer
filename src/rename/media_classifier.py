import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from .cleaner import extract_number
from .utils import EXTRA_TAG


class LocalRole(str, Enum):
    """The role a file has in the user's organized collection."""

    MAIN_EPISODE = 'main_episode'
    EPISODE_LIKE_BONUS = 'episode_like_bonus'
    DISC_EXTRA = 'disc_extra'
    PROVIDER_SPECIAL = 'provider_special'
    UNKNOWN = 'unknown'


class ExtraTagEvidence(str, Enum):
    NONE = 'none'
    CONFIRMED = 'confirmed'
    AMBIGUOUS = 'ambiguous'


class PreclassificationKind(str, Enum):
    """The only pre-numbering decision used by a batch pipeline."""

    CONFIRMED_EXTRA = 'confirmed_extra'
    MAIN_CANDIDATE = 'main_candidate'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class MediaPreclassification:
    kind: PreclassificationKind
    classification: 'MediaClassification'


@dataclass(frozen=True)
class MediaClassification:
    local_role: LocalRole
    season_scope: int
    provider_type: str
    episode_number: Optional[int]
    confidence: str
    reason: str


EXTRA_DIRECTORY_NAMES = {
    'bonus',
    'bonus disc',
    'extra',
    'extras',
    'featurette',
    'featurettes',
    'sp',
    'sps',
    'special',
    'specials',
    '映像特典',
    '特典',
}

_TECHNICAL_PATTERNS = (
    r'\bma\d{1,2}p\b',
    r'\b\d{3,4}p\b',
    r'\b(?:x|h)26[45]\b',
    r'\bav1\b',
    r'\b\d{1,2}bit\b',
    r'\b(?:aac|flac|opus|ac-?3|e-?ac-?3)[._ -]?\d(?:\.\d)?\b',
)

_BRACKET_PATTERN = re.compile(
    r'\[(?P<square>[^\]]*)\]'
    r'|【(?P<cjk>[^】]*)】'
    r'|\((?P<round>[^)]*)\)'
    r'|（(?P<cjk_round>[^）]*)）'
    r'|<(?P<angle>[^>]*)>'
)

# VCB-S places these feature types under ``SPs``.  Human-language labels and
# very short abbreviations are only trusted when they are structurally marked
# (for example ``[IV02]``) or are the entire residual filename.  This avoids
# treating titles such as ``Event Series`` or season marker ``IV`` as extras.
_VCB_EXTRA_TAGS = {
    'TV-SPOT',
    'CM Collection',
    'Character PV',
    'PV Collection',
    'Web Preview',
    'MV',
    'Commentary',
    'Introduction',
    'Making',
    'Program',
    'Interview',
    'Promotion Clip',
    'Promotional Clip',
    'Talk',
    'Stage',
    'Special',
    'SP',
}
_CONTEXT_REQUIRED_EXTRA_TAGS = {
    tag.casefold()
    for tag in {
        'IV',
        'NC',
        'OP',
        'PV',
        'ED',
        'CM',
        'Event',
        'Fans',
        'Advice',
        'Preview',
        'Trailer',
        'Teaser',
        'Menu',
        *_VCB_EXTRA_TAGS,
    }
}


def _normalize_directory_name(value: str) -> str:
    normalized = unicodedata.normalize('NFKC', value).casefold()
    return ' '.join(normalized.replace('_', ' ').replace('-', ' ').split())


def _is_inside_extra_directory(item_path: Path) -> bool:
    return any(
        _normalize_directory_name(parent.name) in EXTRA_DIRECTORY_NAMES
        for parent in item_path.parents
    )


def _contains_tag(value: str, tag: str) -> bool:
    # A role tag must be a complete token.  In particular, strings such as
    # CRC32 ``B63ED209`` must not turn an ordinary episode into an ``ED``.
    left_boundary = r'a-zA-Z0-9\u4e00-\u9fff'
    right_boundary = r'a-zA-Z\u4e00-\u9fff'
    return bool(
        re.search(
            rf'(?<![{left_boundary}]){re.escape(tag)}(?![{right_boundary}])',
            value,
        )
    )


def _extra_tag_evidence(value: str) -> ExtraTagEvidence:
    normalized = unicodedata.normalize('NFKC', value).casefold().replace('_', ' ')
    normalized = re.sub(
        r'(?<![a-f0-9])[a-f0-9]{8}(?:[a-f0-9]{24})?(?![a-f0-9])',
        ' ',
        normalized,
    )
    bracket_contents = [
        unicodedata.normalize('NFKC', content).casefold().replace('_', ' ')
        for match in _BRACKET_PATTERN.finditer(value)
        for content in match.groups()
        if content is not None
    ]
    residual_stem = re.sub(
        r'\.(?:mkv|mka|mp4|flac|png|ass|7z|zip)$',
        '',
        normalized,
    ).strip(' ._-')

    # These multi-word phrases describe an asset rather than a work type.
    # They remain strong evidence after the confirmed series title has been
    # removed, even when release metadata follows the phrase.
    if re.search(
        r'(?<![a-z0-9])(?:promotion|promotional)[ ._-]+clip(?![a-z])',
        normalized,
    ):
        return ExtraTagEvidence.CONFIRMED

    ambiguous = False
    all_tags = {tag.casefold() for tag in EXTRA_TAG} | {
        tag.casefold() for tag in _VCB_EXTRA_TAGS
    }
    for tag in sorted(all_tags, key=len, reverse=True):
        if not _contains_tag(normalized, tag):
            continue
        if tag not in _CONTEXT_REQUIRED_EXTRA_TAGS:
            return ExtraTagEvidence.CONFIRMED
        if any(_contains_tag(content, tag) for content in bracket_contents):
            return ExtraTagEvidence.CONFIRMED
        if re.fullmatch(rf'{re.escape(tag)}\d{{0,3}}(?:_\d+)?', residual_stem):
            return ExtraTagEvidence.CONFIRMED
        ambiguous = True

    return (
        ExtraTagEvidence.AMBIGUOUS
        if ambiguous
        else ExtraTagEvidence.NONE
    )


def _episode_like_bonus_number(value: str) -> tuple[bool, Optional[int]]:
    normalized = unicodedata.normalize('NFKC', value)
    integrated_episode = re.search(
        r'(?<!\d)(\d{1,3})\s*\((?:OVA|OAD)(?:\d{1,3})?\)',
        normalized,
        re.IGNORECASE,
    )
    if integrated_episode:
        return True, int(integrated_episode.group(1))

    numbered_bonus = re.search(
        r'(?<![A-Za-z0-9])(?:OVA|OAD|SPECIAL)[\s._-]*(\d{1,3})(?!\d)',
        normalized,
        re.IGNORECASE,
    )
    if numbered_bonus:
        return True, int(numbered_bonus.group(1))

    bracket_contents = [
        content.strip()
        for match in _BRACKET_PATTERN.finditer(normalized)
        for content in match.groups()
        if content is not None
    ]
    if any(
        re.fullmatch(r'(?:OVA|OAD)', content, re.IGNORECASE)
        for content in bracket_contents
    ):
        return True, None

    if re.search(
        r'(?<![A-Za-z0-9])(?:OVA|OAD)(?![A-Za-z0-9])',
        normalized,
        re.IGNORECASE,
    ):
        return True, None

    residual_stem = re.sub(
        r'\.(?:mkv|mka|mp4|ass)$',
        '',
        normalized,
        flags=re.IGNORECASE,
    ).strip(' ._-')
    if re.fullmatch(r'(?:OVA|OAD)', residual_stem, re.IGNORECASE):
        return True, None

    if re.search(r'(?<!\d)\d{1,3}\.5(?!\d)', normalized):
        return True, None
    return False, None


def _provider_special_episode(value: str) -> Optional[int]:
    match = re.search(
        r'(?<![A-Za-z0-9])S0+E(\d{1,3})(?!\d)',
        value,
        re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def _preserve_episode_bracket(match: re.Match) -> str:
    content = next(group for group in match.groups() if group is not None).strip()
    if re.fullmatch(
        r'(?:\d{1,3}|E\d{1,3}|S\d{1,2}E\d{1,3})',
        content,
        re.IGNORECASE,
    ):
        return f' {content} '
    return ' '


def _strip_technical_metadata(value: str) -> str:
    cleaned = _BRACKET_PATTERN.sub(_preserve_episode_bracket, value)
    for pattern in _TECHNICAL_PATTERNS:
        cleaned = re.sub(pattern, ' ', cleaned, flags=re.IGNORECASE)
    return cleaned


def _extract_regular_episode(value: str) -> tuple[Optional[int], Optional[int]]:
    explicit = re.search(
        r'(?<![A-Za-z0-9])S(\d{1,2})E(\d{1,3})(?!\d)',
        value,
        re.IGNORECASE,
    )
    if explicit:
        return int(explicit.group(1)), int(explicit.group(2))

    cleaned = _strip_technical_metadata(value)
    numeric_candidates = re.findall(r'(?<!\d)(\d{1,3})(?!\d)', cleaned)
    if len(numeric_candidates) == 1:
        episode = int(numeric_candidates[0])
    elif numeric_candidates:
        return None, None
    else:
        episode = extract_number(cleaned)
    if episode is None:
        return None, None

    episode = int(episode)
    if 1900 <= episode <= 2099:
        return None, None
    return None, episode


def preclassify_media(
    item_path: Path,
    candidate_name: str,
    season_id: int,
    extra_context_name: Optional[str] = None,
) -> MediaPreclassification:
    """Classify role evidence before any batch episode-number inference.

    ``episode_inference`` deliberately has no role vocabulary.  A caller must
    pass only ``MAIN_CANDIDATE`` paths to it; confirmed extras and ambiguous
    files can therefore never steal a normal episode number.
    """

    semantic_context = (
        extra_context_name.strip()
        if extra_context_name is not None and extra_context_name.strip()
        else candidate_name
    )

    if semantic_context == 'AMBIGUOUS_ROLE_MARKER':
        return MediaPreclassification(PreclassificationKind.UNKNOWN, MediaClassification(
            local_role=LocalRole.UNKNOWN,
            season_scope=season_id,
            provider_type='unknown',
            episode_number=None,
            confidence='Low',
            reason='检测到 OVA/OAD，但作品标题未能可靠剥离',
        ))

    provider_episode = _provider_special_episode(semantic_context)
    if provider_episode is not None:
        return MediaPreclassification(PreclassificationKind.CONFIRMED_EXTRA, MediaClassification(
            local_role=LocalRole.PROVIDER_SPECIAL,
            season_scope=season_id,
            provider_type='sp',
            episode_number=provider_episode,
            confidence='High',
            reason='文件名包含明确的 S00E 特别篇编号',
        ))

    is_episode_like_bonus, bonus_episode = _episode_like_bonus_number(
        semantic_context
    )
    if is_episode_like_bonus:
        return MediaPreclassification(PreclassificationKind.CONFIRMED_EXTRA, MediaClassification(
            local_role=LocalRole.EPISODE_LIKE_BONUS,
            season_scope=season_id,
            provider_type='sp',
            episode_number=bonus_episode,
            confidence='High',
            reason='文件名包含 OVA/OAD/编号 Special 或半集标记',
        ))

    extra_evidence = _extra_tag_evidence(semantic_context)
    # ``semantic_context`` safely removes the matched series title, but its
    # canonical representation intentionally loses bracket structure.  Use
    # the original candidate only to strengthen a tag already seen outside
    # the title; this preserves title protection (for example ``OVA Project``)
    # while accepting structural markers such as ``[SP]`` and ``[OP]``.
    if (
        extra_evidence == ExtraTagEvidence.AMBIGUOUS
        and _extra_tag_evidence(candidate_name) == ExtraTagEvidence.CONFIRMED
    ):
        extra_evidence = ExtraTagEvidence.CONFIRMED
    if (
        extra_evidence == ExtraTagEvidence.CONFIRMED
        or _is_inside_extra_directory(item_path)
    ):
        return MediaPreclassification(PreclassificationKind.CONFIRMED_EXTRA, MediaClassification(
            local_role=LocalRole.DISC_EXTRA,
            season_scope=season_id,
            provider_type='extra',
            episode_number=None,
            confidence='High',
            reason='文件名或父目录明确表示季度附加内容',
        ))

    if extra_evidence == ExtraTagEvidence.AMBIGUOUS:
        return MediaPreclassification(PreclassificationKind.UNKNOWN, MediaClassification(
            local_role=LocalRole.UNKNOWN,
            season_scope=season_id,
            provider_type='unknown',
            episode_number=None,
            confidence='Low',
            reason='检测到含糊的特典短词，但缺少方括号、孤立标签或 SPs 目录证据',
        ))

    return MediaPreclassification(PreclassificationKind.MAIN_CANDIDATE, MediaClassification(
        local_role=LocalRole.UNKNOWN,
        season_scope=season_id,
        provider_type='normal_candidate',
        episode_number=None,
        confidence='Pending',
        reason='未发现特典证据，等待目录级剧集编号推断',
    ))


def classify_media(
    item_path: Path,
    candidate_name: str,
    season_id: int,
    extra_context_name: Optional[str] = None,
    episode_hint: Optional[int] = None,
    secondary_bonus_episode: Optional[int] = None,
) -> MediaClassification:
    """Complete classification after the shared pre-numbering stage."""

    preclassified = preclassify_media(
        item_path, candidate_name, season_id, extra_context_name
    ).classification
    if preclassified.local_role != LocalRole.UNKNOWN or preclassified.confidence == 'Low':
        return preclassified

    if secondary_bonus_episode is not None:
        return MediaClassification(
            local_role=LocalRole.EPISODE_LIKE_BONUS,
            season_scope=season_id,
            provider_type='sp',
            episode_number=secondary_bonus_episode,
            confidence='Medium',
            reason='同目录中检测到相对主序列独立重置的小型命名序列',
        )

    explicit_season, extracted_episode = _extract_regular_episode(candidate_name)
    episode = episode_hint if episode_hint is not None else extracted_episode
    if episode is not None:
        return MediaClassification(
            local_role=LocalRole.MAIN_EPISODE,
            season_scope=explicit_season if explicit_season is not None else season_id,
            provider_type='normal',
            episode_number=episode,
            confidence='High',
            reason=(
                '目录级字段推断得到唯一剧集编号'
                if episode_hint is not None
                else '提取到可信的剧集编号'
            ),
        )

    return MediaClassification(
        local_role=LocalRole.UNKNOWN,
        season_scope=season_id,
        provider_type='unknown',
        episode_number=None,
        confidence='Low',
        reason='未找到可信剧集编号或明确附加内容语义',
    )
