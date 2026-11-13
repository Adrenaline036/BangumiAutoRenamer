import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .canonical_naming import canonicalize_media_leaf
from .utils import VIDEO_SUFFIX


class EpisodeInferenceError(ValueError):
    """Raised when a directory cannot be mapped without collisions."""


@dataclass(frozen=True)
class NumberCandidate:
    value: int
    start: int
    end: int
    explicit: bool


_VIDEO_OR_SUBTITLE_SUFFIX = re.compile(
    r'\.(?:mkv|mka|mp4|avi|wmv|flv|mov|mpg|mpeg|m4v|rm|rmvb|ass|ssa|srt)$',
    re.IGNORECASE,
)
_BRACKET_BLOCK = re.compile(r'\[[^\]]*\]|【[^】]*】|\([^)]*\)|（[^）]*）|<[^>]*>')
_TECHNICAL_WORD = re.compile(
    r'(?:\b(?:bd(?:rip|remux)?|blu[- ]?ray|web[- ]?dl|webrip|hdtv|'
    r'dvd(?:rip)?|uhd|remux|hevc|avc|av1|x26[45]|h\.?26[45]|hi10p|'
    r'flac|aac|opus|dts|ac-?3|e-?ac-?3|truehd|chap(?:ter|ters)?|'
    r'dual[ ._-]*audio|multi[ ._-]*audio)\b|'
    r'\b(?:480|576|720|1080|1440|2160)[pi]\b|'
    r'\b\d{3,4}\s*[x×]\s*\d{3,4}\b|\b\d{1,2}\s*bit\b)',
    re.IGNORECASE,
)
_TECHNICAL_UNDERSCORE_TOKEN = re.compile(
    r'(?<![A-Za-z0-9])(?:ma\d{1,2}p|(?:480|576|720|1080|1440|2160)p|'
    r'x26[45]|h26[45]|aac|flac)(?![A-Za-z0-9])',
    re.IGNORECASE,
)
# This is deliberately presentation-only.  Role classification lives solely
# in ``media_classifier``; retaining a human descriptive bracket here prevents
# two separately classified extras (for example NCOP and Menu) from receiving
# the same display filename.
_DESCRIPTIVE_TARGET_LABEL = re.compile(
    r'(?<![A-Za-z0-9])(?:OVA|OAD|NCOP|NCED|SP|SPECIAL|PV|CM|MENU|SCENE[ _-]*CUT)'
    r'(?![A-Za-z0-9])', re.IGNORECASE,
)
_COMPOUND_SUBTITLE_LANGUAGE_SUFFIX = re.compile(
    r'\.(?:sc|tc|chs|cht|jpsc|jptc|zh(?:[._-](?:hans|hant|cn|tw))?)$',
    re.IGNORECASE,
)


def _canonical_text(value: str) -> str:
    normalized = unicodedata.normalize('NFKC', value)
    normalized = normalized.replace('⁄', '/').replace('／', '/')
    normalized = re.sub(r'[^0-9A-Za-z\u4e00-\u9fff/]+', ' ', normalized)
    return ' '.join(normalized.casefold().split())


def build_semantic_context(filename: str, series_title: str) -> str:
    """Return title-excluded text for content-role markers.

    The comparison is punctuation-insensitive and normalizes ``½``, ``1/2``
    and ``1⁄2`` to the same token sequence.  Role markers are therefore never
    taken from a confidently matched series title.
    """

    stem = _VIDEO_OR_SUBTITLE_SUFFIX.sub('', filename)
    file_text = _canonical_text(stem)
    title_text = _canonical_text(series_title)
    title_matched = bool(title_text and re.search(
        rf'(?<![0-9a-z\u4e00-\u9fff]){re.escape(title_text)}'
        rf'(?![0-9a-z\u4e00-\u9fff])',
        file_text,
    ))
    if title_matched:
        file_text = re.sub(
            rf'(?<![0-9a-z\u4e00-\u9fff]){re.escape(title_text)}'
            rf'(?![0-9a-z\u4e00-\u9fff])',
            ' ',
            file_text,
            count=1,
        )
    elif re.search(
        r'(?<!\d)\d{1,3}\s*\((?:OVA|OAD)(?:\d{1,3})?\)',
        stem,
        re.IGNORECASE,
    ):
        # ``13(OVA)`` is a structural bonus-episode marker rather than a
        # free-standing title word.  Preserve it for media_classifier even
        # when the confirmed series title and release filename use different
        # languages; the classifier still routes it to the season Extras
        # outlet and never fabricates a regular episode number.
        return file_text.upper()
    elif re.search(
        r'(?<![A-Za-z0-9])(?:OVA|OAD)(?![A-Za-z0-9])',
        file_text,
        re.IGNORECASE,
    ):
        # A role word outside a confidently matched title is not sufficient
        # evidence: it may belong to an untranslated or differently punctuated
        # series title.  Keep an explicit sentinel so callers route it safely.
        return 'AMBIGUOUS_ROLE_MARKER'
    return ' '.join(file_text.split()).upper()


def _excluded_spans(value: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    for pattern in (
        r'\d+\s*[/⁄／]\s*\d+',
        r'\d+(?:\.\d+)?\s*[x×]\s*\d+',
        r'\d{3,4}\s*[x×]\s*\d{3,4}',
        r'(?<!\d)(?:19|20)\d{2}(?!\d)',
        r'\b[a-fA-F0-9]{8}(?:[a-fA-F0-9]{24})?\b',
    ):
        spans.extend((match.start(), match.end()) for match in re.finditer(pattern, value))

    for block in _BRACKET_BLOCK.finditer(value):
        if (
            _TECHNICAL_WORD.search(block.group(0))
            or _TECHNICAL_UNDERSCORE_TOKEN.search(block.group(0))
        ):
            spans.append((block.start(), block.end()))
    return spans


def _inside_any(start: int, end: int, spans: Sequence[Tuple[int, int]]) -> bool:
    return any(start >= span_start and end <= span_end for span_start, span_end in spans)


def _number_candidates(filename: str) -> List[NumberCandidate]:
    stem = _VIDEO_OR_SUBTITLE_SUFFIX.sub('', unicodedata.normalize('NFKC', filename))
    excluded = _excluded_spans(stem)
    candidates: Dict[Tuple[int, int], NumberCandidate] = {}

    explicit_patterns = (
        re.compile(r'(?<![A-Za-z0-9])S\d{1,2}E(?P<episode>\d{1,3})(?!\d)', re.IGNORECASE),
        re.compile(r'(?<![A-Za-z0-9])E(?:P)?[ ._-]*(?P<episode>\d{1,3})(?!\d)', re.IGNORECASE),
        re.compile(r'[\[【(（]\s*(?P<episode>\d{1,3})(?:v\d+)?\s*[\]】)）]'),
    )
    for pattern in explicit_patterns:
        for match in pattern.finditer(stem):
            start, end = match.span('episode')
            if not _inside_any(start, end, excluded):
                candidates[(start, end)] = NumberCandidate(
                    int(match.group('episode')), start, end, True
                )

    general = re.compile(
        r'(?<![A-Za-z0-9])(?P<episode>\d{1,3})(?:v\d+)?(?![A-Za-z0-9])',
        re.IGNORECASE,
    )
    for match in general.finditer(stem):
        start, end = match.span('episode')
        if _inside_any(start, end, excluded):
            continue
        value = int(match.group('episode'))
        if value == 0:
            continue
        candidates.setdefault(
            (start, end),
            NumberCandidate(value, start, end, False),
        )

    return sorted(candidates.values(), key=lambda item: (item.start, item.end))


def _sequence_family_key(path: Path, series_title: str) -> str:
    """Return the stable descriptive part surrounding a sequence number."""

    stem = _VIDEO_OR_SUBTITLE_SUFFIX.sub('', unicodedata.normalize('NFKC', path.name))

    def preserve_descriptive_block(match: re.Match) -> str:
        block = match.group(0)
        content = block[1:-1].strip()
        if (
            not content
            or _TECHNICAL_WORD.search(block)
            or _TECHNICAL_UNDERSCORE_TOKEN.search(block)
            or re.fullmatch(
                r'(?:\d{1,3}(?:v\d+)?|E\d{1,3}|S\d{1,2}E\d{1,3})',
                content,
                re.IGNORECASE,
            )
            or re.fullmatch(r'[A-Fa-f0-9]{8}(?:[A-Fa-f0-9]{24})?', content)
        ):
            return ' '
        return f' {content} '

    stem = _BRACKET_BLOCK.sub(preserve_descriptive_block, stem)
    file_text = _canonical_text(stem)
    title_text = _canonical_text(series_title)
    if title_text:
        file_text = re.sub(
            rf'(?<![0-9a-z\u4e00-\u9fff]){re.escape(title_text)}'
            rf'(?![0-9a-z\u4e00-\u9fff])',
            ' ',
            file_text,
            count=1,
        )
    file_text = re.sub(r'(?<![a-z0-9])\d{1,3}(?:v\d+)?(?![a-z0-9])', ' ', file_text)
    return ' '.join(file_text.split())


def build_secondary_sequence_hints(
    paths: Iterable[Path],
    series_title: str,
) -> Dict[Path, int]:
    """Detect a small named sequence that resets numbering beside a main run.

    This does not assign meaning to a word such as ``Super`` globally.  It only
    treats a stable named family as episode-like bonus material when a much
    larger dominant sequence exists in the same directory and their episode
    numbers overlap.  Explicit season/part labels are deliberately excluded.
    """

    all_paths = list(paths)
    video_paths = [path for path in all_paths if path.suffix.lower() in VIDEO_SUFFIX]
    families: Dict[str, Dict[Path, int]] = {}
    for path in video_paths:
        candidates = _number_candidates(path.name)
        if len(candidates) != 1:
            continue
        key = _sequence_family_key(path, series_title)
        families.setdefault(key, {})[path] = candidates[0].value

    valid_families = {
        key: mapping
        for key, mapping in families.items()
        if len(mapping) >= 2 and len(set(mapping.values())) == len(mapping)
    }
    if len(valid_families) < 2:
        return {}

    dominant_key, dominant = max(valid_families.items(), key=lambda item: len(item[1]))
    if len(dominant) < 6:
        return {}
    dominant_values = set(dominant.values())
    secondary_by_key: Dict[str, Dict[Path, int]] = {}
    for key, mapping in valid_families.items():
        if key == dominant_key or not key:
            continue
        if re.search(r'(?<![a-z0-9])(?:season|series|part|cour)\s*\d+', key):
            continue
        values = set(mapping.values())
        if len(mapping) * 3 > len(dominant):
            continue
        if not values.intersection(dominant_values):
            continue
        secondary_by_key[key] = mapping

    hints: Dict[Path, int] = {}
    for path in all_paths:
        key = _sequence_family_key(path, series_title)
        if key not in secondary_by_key:
            continue
        candidates = _number_candidates(path.name)
        if len(candidates) == 1:
            hints[path] = candidates[0].value
    return hints


def build_episode_hints(
    paths: Iterable[Path],
    series_title: str,
) -> Dict[Path, int]:
    """Infer one stable numeric field across a directory without deleting text."""

    all_paths = list(paths)
    secondary_sequence_hints = build_secondary_sequence_hints(all_paths, series_title)
    reference_paths = [
        path
        for path in all_paths
        if path.suffix.lower() in VIDEO_SUFFIX and path not in secondary_sequence_hints
    ]
    by_path = {path: _number_candidates(path.name) for path in all_paths}
    if not reference_paths:
        return {}

    max_columns = max((len(by_path[path]) for path in reference_paths), default=0)
    scored_columns: List[Tuple[float, int]] = []
    for column in range(max_columns):
        column_items = [
            by_path[path][column]
            for path in reference_paths
            if len(by_path[path]) > column
        ]
        if not column_items:
            continue
        coverage = len(column_items) / len(reference_paths)
        unique_values = {item.value for item in column_items}
        uniqueness = len(unique_values) / len(column_items)
        explicit_ratio = sum(item.explicit for item in column_items) / len(column_items)
        if len(reference_paths) == 1:
            if explicit_ratio == 1:
                scored_columns.append((2.0, column))
            continue
        if coverage < 0.60 or uniqueness < 0.80:
            continue
        ordered = sorted(unique_values)
        span = ordered[-1] - ordered[0] + 1
        density = len(ordered) / span if span else 0.0
        if density < 0.50:
            continue
        score = coverage + uniqueness + density + explicit_ratio
        scored_columns.append((score, column))

    if not scored_columns:
        return {}
    scored_columns.sort(reverse=True)
    if len(scored_columns) > 1 and scored_columns[0][0] - scored_columns[1][0] < 0.15:
        return {}
    selected_column = scored_columns[0][1]

    hints: Dict[Path, int] = {}
    for path, candidates in by_path.items():
        if path in secondary_sequence_hints:
            continue
        if len(candidates) > selected_column:
            hints[path] = candidates[selected_column].value
    validate_episode_mapping(
        {
            path: episode
            for path, episode in hints.items()
            if path.suffix.lower() in VIDEO_SUFFIX
        }
    )
    return hints


def validate_episode_mapping(mapping: Dict[Path, int]) -> None:
    seen: Dict[int, Path] = {}
    for path, episode in mapping.items():
        previous = seen.get(episode)
        if previous is not None and previous != path:
            raise EpisodeInferenceError(
                f'剧集编号冲突：{previous.name} 与 {path.name} 均映射为 E{episode:03d}'
            )
        seen[episode] = path


def normalize_target_stem(value: str, series_title: Optional[str] = None) -> str:
    """Remove bounded release metadata while preserving descriptive media labels."""

    source = unicodedata.normalize('NFKC', value).strip()
    protected_title = _canonical_text(series_title or '')
    # ``Path.stem`` removes only the final subtitle extension, so a compound
    # name such as ``video [group].sc.ass`` reaches this function with ``.sc``
    # still attached.  Detach that language suffix before detecting a trailing
    # release group; otherwise the video loses ``[group]`` while its subtitle
    # keeps it and Jellyfin can no longer associate their base stems.
    subtitle_language_suffix = ''
    subtitle_suffix_match = _COMPOUND_SUBTITLE_LANGUAGE_SUFFIX.search(source)
    if subtitle_suffix_match:
        subtitle_language_suffix = subtitle_suffix_match.group(0)
        source = source[:subtitle_suffix_match.start()].rstrip()

    def is_protected_bracket(block: str) -> bool:
        content = block[1:-1].strip()
        canonical_content = _canonical_text(content)
        return bool(
            not canonical_content
            or _DESCRIPTIVE_TARGET_LABEL.search(content)
            or _TECHNICAL_WORD.search(content)
            or _TECHNICAL_UNDERSCORE_TOKEN.search(content)
            or re.fullmatch(
                r'(?:\d{1,3}(?:v\d+)?|E\d{1,3}|S\d{1,2}E\d{1,3})',
                content,
                re.IGNORECASE,
            )
            or (protected_title and canonical_content in protected_title)
        )

    leading_group = re.match(
        r'^\s*(\[[A-Za-z0-9][A-Za-z0-9._!&+\'\-]{1,31}\])\s*',
        source,
    )
    if leading_group and not is_protected_bracket(leading_group.group(1)):
        source = source[leading_group.end():].lstrip()
    had_technical = False
    technical_blocks = [
        match
        for match in _BRACKET_BLOCK.finditer(source)
        if (
            (
                _TECHNICAL_WORD.search(match.group(0))
                or _TECHNICAL_UNDERSCORE_TOKEN.search(match.group(0))
            )
            and not _DESCRIPTIVE_TARGET_LABEL.search(match.group(0))
        )
    ]
    last_technical_end = technical_blocks[-1].end() if technical_blocks else -1
    suffix_group = re.search(
        r'\s+[-–—]\s+[A-Za-z][A-Za-z0-9._!&+()\'\-]{1,63}\s*$',
        source,
    )
    suffix_is_release_group = bool(
        suffix_group
        and last_technical_end >= 0
        and suffix_group.start() >= last_technical_end
    )
    bracket_group = re.search(
        r'\[[A-Za-z][A-Za-z0-9._!&+\'\-]{1,31}]\s*$',
        source,
    )
    bracket_is_release_group = bool(
        bracket_group
        and not is_protected_bracket(bracket_group.group(0))
    )

    def replace_block(match: re.Match) -> str:
        nonlocal had_technical
        block = match.group(0)
        if (
            (
                _TECHNICAL_WORD.search(block)
                or _TECHNICAL_UNDERSCORE_TOKEN.search(block)
            )
            and not _DESCRIPTIVE_TARGET_LABEL.search(block)
        ):
            had_technical = True
            return ' '
        return block

    cleaned = _BRACKET_BLOCK.sub(replace_block, source)
    if had_technical and suffix_is_release_group:
        cleaned = re.sub(
            r'\s+[-–—]\s+[A-Za-z][A-Za-z0-9._!&+()\'\-]{1,63}\s*$',
            ' ',
            cleaned,
        )
    if bracket_is_release_group:
        cleaned = re.sub(
            r'\[[A-Za-z][A-Za-z0-9._!&+\'\-]{1,31}]\s*$',
            ' ',
            cleaned,
        )
    year_matches = list(re.finditer(
        r'(?<!\d)(?:19|20)\d{2}(?!\d)(?=\s*[-–—]\s*\S)',
        cleaned,
        flags=re.IGNORECASE,
    ))
    for year_match in reversed(year_matches):
        year = year_match.group(0)
        if year in protected_title.split():
            continue
        cleaned = cleaned[:year_match.start()] + ' ' + cleaned[year_match.end():]
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' ._-')
    cleaned = re.sub(r'\s+[-–—]\s+', ' - ', cleaned)
    return canonicalize_media_leaf(
        f"{cleaned.strip(' ._-')}{subtitle_language_suffix}"
    )
