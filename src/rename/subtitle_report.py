"""Versioned, filename-only subtitle sample reports for an explicit series.

The core reporter is filesystem-free.  The adapter enumerates names without
opening media or subtitle contents, refuses links/reparse points, and returns
relative paths only.  Reports never change the parser dictionary or media.

``rule_candidates`` intentionally aggregates unknown-token frequency across
records only to prioritize human discussion.  It is not an executable matcher:
S4 file/series rules independently require their complete label signature and
ordered unknown-token signature, so one frequent token never broadens scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Mapping, Tuple

from .subtitle_labels import (
    SUBTITLE_ASSOCIATION_RULE_VERSION,
    SUBTITLE_ASSOCIATION_SCHEMA_VERSION,
    SUBTITLE_LABEL_RULE_VERSION,
    SUBTITLE_LABEL_SCHEMA_VERSION,
    SUBTITLE_SUFFIXES,
    associate_subtitle_to_video,
    parse_subtitle_label,
)
from .utils import VIDEO_SUFFIX


SUBTITLE_REPORT_SCHEMA_VERSION = 1
SUBTITLE_REPORT_RULE_VERSION = "0.3.0-dev.s3.1"
INVENTORY_POLICY_VERSION = "filename-suffix-only.v1"


class InventoryKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"


class SubtitleReportError(RuntimeError):
    pass


@dataclass(frozen=True)
class InventoryEntry:
    relative_path: str
    kind: InventoryKind = InventoryKind.FILE

    def __post_init__(self) -> None:
        value = _validate_relative_path(self.relative_path)
        object.__setattr__(self, "relative_path", value)
        object.__setattr__(self, "kind", InventoryKind(self.kind))


def inventory_series_names(root: Path, *, max_entries: int = 20_000) -> Tuple[InventoryEntry, ...]:
    """Enumerate names below one exact root without following links or reading files."""

    root = Path(root)
    if not root.is_absolute() or not root.is_dir():
        raise SubtitleReportError("Sample root must be an existing absolute directory")
    before = _walk_names(root, max_entries=max_entries)
    after = _walk_names(root, max_entries=max_entries)
    if before != after:
        raise SubtitleReportError("Filename inventory changed during enumeration")
    return before


def build_subtitle_sample_report(
    entries: Iterable[InventoryEntry],
    *,
    scope_id: str,
    privacy_mode: str = "private",
) -> Dict[str, object]:
    """Build a deterministic report from explicit relative inventory entries."""

    if not scope_id or any(value in scope_id for value in ("/", "\\", ":")):
        raise SubtitleReportError("scope_id must be opaque and path-free")
    if privacy_mode not in {"private", "summary"}:
        raise SubtitleReportError("privacy_mode must be private or summary")
    inventory = tuple(sorted(
        (item if isinstance(item, InventoryEntry) else InventoryEntry(*item) for item in entries),
        key=lambda item: (_canonical(item.relative_path), item.relative_path),
    ))
    keys: Dict[str, str] = {}
    for item in inventory:
        key = _canonical(item.relative_path)
        if key in keys:
            raise SubtitleReportError("Duplicate normalized relative path")
        keys[key] = item.relative_path

    videos_by_parent: Dict[str, List[Path]] = defaultdict(list)
    subtitle_items: List[InventoryEntry] = []
    ignored_count = 0
    suffix_counts: Counter[str] = Counter()
    for item in inventory:
        if item.kind != InventoryKind.FILE:
            continue
        path = PurePosixPath(item.relative_path)
        suffix = _normalized_suffix(path.name)
        suffix_counts[suffix or "<none>"] += 1
        if suffix in VIDEO_SUFFIX:
            videos_by_parent[str(path.parent)].append(Path(path.name))
        elif suffix in SUBTITLE_SUFFIXES:
            subtitle_items.append(item)
        else:
            ignored_count += 1

    records: List[Dict[str, object]] = []
    association_counts: Counter[str] = Counter()
    parse_counts: Counter[str] = Counter()
    confidence_counts: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    script_counts: Counter[str] = Counter()
    flag_counts: Counter[str] = Counter()
    role_counts: Counter[str] = Counter()
    normalized_token_counts: Counter[str] = Counter()
    unknown_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    candidate_records: Dict[str, List[str]] = defaultdict(list)

    for item in subtitle_items:
        relative = PurePosixPath(item.relative_path)
        association = associate_subtitle_to_video(
            Path(relative.name), videos_by_parent.get(str(relative.parent), ()),
        )
        label = parse_subtitle_label(association.raw_label)
        record_id = hashlib.sha256(item.relative_path.encode("utf-8")).hexdigest()
        association_counts[association.status.value] += 1
        parse_counts[label.status.value] += 1
        confidence_counts[label.confidence.value] += 1
        language_counts.update(label.detected_languages)
        if label.script:
            script_counts[label.script] += 1
        flag_counts.update(label.flags)
        for token in label.tokens:
            role_counts[token.role.value] += 1
            normalized_token_counts[token.normalized] += 1
        unknown_counts.update(label.unknown_tokens)
        reasons = tuple(dict.fromkeys(association.reason_codes + label.reason_codes))
        reason_counts.update(reasons)
        eligible = association.status.value == "confirmed" and bool(label.tokens)
        if eligible:
            for token in label.unknown_tokens:
                candidate_records[_canonical(token)].append(record_id)
        record: Dict[str, object] = {
            "record_id": record_id,
            "raw_suffix": relative.suffix,
            "normalized_suffix": _normalized_suffix(relative.name),
            "association": association.to_dict(),
            "label": label.to_dict(),
            "review_reasons": list(reasons),
            "eligible_for_rule_review": eligible,
        }
        if privacy_mode == "private":
            record["relative_path"] = item.relative_path
        else:
            record["association"] = _summary_association(association.to_dict())
            record["label"] = _summary_label(label.to_dict())
        records.append(record)

    candidates = [
        {
            "normalized_token": token,
            "count": len(ids),
            "sample_record_ids": sorted(ids)[:5],
            "current_roles": ["unknown"],
            "decision": "pending_human",
        }
        for token, ids in sorted(candidate_records.items(), key=lambda item: (-len(item[1]), item[0]))
    ]
    aggregates: Dict[str, object] = {
        "entry_count": len(inventory),
        "video_count": sum(len(items) for items in videos_by_parent.values()),
        "subtitle_count": len(subtitle_items),
        "ignored_file_count": ignored_count,
        "suffix_counts": _counter_rows(suffix_counts),
        "association_status_counts": _counter_rows(association_counts),
        "parse_status_counts": _counter_rows(parse_counts),
        "confidence_counts": _counter_rows(confidence_counts),
        "language_counts": _counter_rows(language_counts),
        "script_counts": _counter_rows(script_counts),
        "flag_counts": _counter_rows(flag_counts),
        "token_role_counts": _counter_rows(role_counts),
        "reason_code_counts": _counter_rows(reason_counts),
    }
    if privacy_mode == "private":
        aggregates.update({
            "normalized_token_counts": _counter_rows(normalized_token_counts),
            "unknown_token_counts": _counter_rows(unknown_counts),
        })
    payload: Dict[str, object] = {
        "schema_version": SUBTITLE_REPORT_SCHEMA_VERSION,
        "rule_version": SUBTITLE_REPORT_RULE_VERSION,
        "scope_id": scope_id,
        "privacy_mode": privacy_mode,
        "policy": INVENTORY_POLICY_VERSION,
        "recursive": True,
        "follow_reparse_points": False,
        "complete": True,
        "association_schema_version": SUBTITLE_ASSOCIATION_SCHEMA_VERSION,
        "association_rule_version": SUBTITLE_ASSOCIATION_RULE_VERSION,
        "label_schema_version": SUBTITLE_LABEL_SCHEMA_VERSION,
        "label_rule_version": SUBTITLE_LABEL_RULE_VERSION,
        "records": records,
        "aggregates": aggregates,
        "rule_candidates": candidates if privacy_mode == "private" else [],
        "has_review_items": any(record["review_reasons"] for record in records),
        "rule_review_allowed": True,
    }
    report_id = hashlib.sha256(canonical_report_bytes(payload)).hexdigest()
    return {**payload, "report_id": report_id}


def canonical_report_bytes(report: Mapping[str, object], *, include_report_id: bool = False) -> bytes:
    payload = dict(report)
    if not include_report_id:
        payload.pop("report_id", None)
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _walk_names(root: Path, *, max_entries: int) -> Tuple[InventoryEntry, ...]:
    found: List[InventoryEntry] = []

    def visit(directory: Path, relative_parent: PurePosixPath) -> None:
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: (_canonical(entry.name), entry.name))
        except OSError as exc:
            raise SubtitleReportError(f"Cannot enumerate sample directory: {exc}") from exc
        for child in children:
            try:
                mode = child.stat(follow_symlinks=False).st_mode
                attributes = getattr(child.stat(follow_symlinks=False), "st_file_attributes", 0)
            except OSError as exc:
                raise SubtitleReportError(f"Cannot inspect inventory entry type: {exc}") from exc
            if child.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise SubtitleReportError("Links and reparse points are forbidden in sample inventory")
            relative = relative_parent / child.name
            kind = InventoryKind.DIRECTORY if stat.S_ISDIR(mode) else InventoryKind.FILE
            found.append(InventoryEntry(relative.as_posix(), kind))
            if len(found) > max_entries:
                raise SubtitleReportError("Sample inventory exceeds max_entries")
            if kind == InventoryKind.DIRECTORY:
                visit(Path(child.path), relative)

    visit(root, PurePosixPath("."))
    return tuple(sorted(found, key=lambda item: (_canonical(item.relative_path), item.relative_path)))


def _validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SubtitleReportError("Inventory path must be a non-empty relative string")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ":" in path.parts[0] or any(part in {"", ".", ".."} for part in path.parts):
        raise SubtitleReportError("Inventory path must stay within the selected scope")
    return path.as_posix()


def _normalized_suffix(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name)
    dot = normalized.rfind(".")
    return normalized[dot:].casefold() if dot >= 0 else ""


def _canonical(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _counter_rows(counter: Counter[str]) -> List[Dict[str, object]]:
    return [{"value": value, "count": count} for value, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))]


def _summary_association(value: Dict[str, object]) -> Dict[str, object]:
    return {key: item for key, item in value.items() if key not in {"subtitle_path", "associated_video", "candidate_videos", "raw_label"}}


def _summary_label(value: Dict[str, object]) -> Dict[str, object]:
    return {key: item for key, item in value.items() if key not in {"raw_label", "raw_tokens", "tokens", "unknown_tokens", "track_title_tokens"}}
