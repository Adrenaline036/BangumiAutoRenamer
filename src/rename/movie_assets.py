"""Read-only, metadata-only inventory for movie directory layouts.

M0 deliberately stops before identity, role, version, target-path, or mapping
decisions.  The inventory contains relative paths only and never opens media or
subtitle content.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from dataclasses import asdict, dataclass
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Optional, Tuple

from .utils import VIDEO_SUFFIX, is_ignored_asset_directory


MOVIE_ASSET_INVENTORY_SCHEMA_VERSION = 1
DISC_STRUCTURE_REQUIRES_MANUAL_WORKFLOW = (
    "DISC_STRUCTURE_REQUIRES_MANUAL_WORKFLOW"
)
MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION = (
    "MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION"
)
SINGLE_VIDEO_LAYOUT = "SINGLE_VIDEO_LAYOUT"
NO_VIDEO_FILE_FOUND = "NO_VIDEO_FILE_FOUND"

_DISC_DIRECTORY_NAMES = frozenset({"bdmv", "video_ts"})
_SUBTITLE_SUFFIXES = (".ass", ".ssa", ".srt", ".vtt", ".sup")
_REPARSE_POINT_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class MovieAssetInventoryError(RuntimeError):
    """Fail-closed inventory error with no absolute path in its public data."""

    def __init__(self, code: str, relative_path: Optional[str] = None):
        self.code = code
        self.relative_path = relative_path
        location = relative_path if relative_path is not None else "."
        super().__init__(f"{code}: {location}")


@dataclass(frozen=True)
class MovieAssetEntry:
    relative_path: str
    entry_type: str
    size_bytes: Optional[int]
    mtime_ns: int


@dataclass(frozen=True)
class MovieAssetInventory:
    schema_version: int
    entries: Tuple[MovieAssetEntry, ...]
    video_count: int
    reason_codes: Tuple[str, ...]
    max_entries: int
    fingerprint: str

    def to_dict(self) -> dict:
        """Return a JSON-safe snapshot which contains no selected root path."""

        return {
            "schema_version": self.schema_version,
            "entries": [asdict(entry) for entry in self.entries],
            "video_count": self.video_count,
            "reason_codes": list(self.reason_codes),
            "max_entries": self.max_entries,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: object) -> "MovieAssetInventory":
        if not isinstance(data, dict):
            raise ValueError("Movie asset inventory must be an object")
        if data.get("schema_version") != MOVIE_ASSET_INVENTORY_SCHEMA_VERSION:
            raise ValueError("Unsupported movie asset inventory schema")
        raw_entries = data.get("entries")
        if not isinstance(raw_entries, list):
            raise ValueError("Movie asset inventory entries must be a list")
        entries = []
        for item in raw_entries:
            if not isinstance(item, dict):
                raise ValueError("Movie asset inventory entry must be an object")
            relative_path = item.get("relative_path")
            entry_type = item.get("entry_type")
            size_bytes = item.get("size_bytes")
            mtime_ns = item.get("mtime_ns")
            if (
                not isinstance(relative_path, str)
                or not relative_path
                or not isinstance(entry_type, str)
                or isinstance(mtime_ns, bool)
                or not isinstance(mtime_ns, int)
                or (
                    size_bytes is not None
                    and (isinstance(size_bytes, bool) or not isinstance(size_bytes, int))
                )
            ):
                raise ValueError("Movie asset inventory entry fields are invalid")
            entries.append(MovieAssetEntry(
                relative_path, entry_type, size_bytes, mtime_ns
            ))
        video_count = data.get("video_count")
        max_entries = data.get("max_entries")
        fingerprint = data.get("fingerprint")
        reason_codes = data.get("reason_codes")
        if (
            isinstance(video_count, bool)
            or not isinstance(video_count, int)
            or isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or not isinstance(fingerprint, str)
            or not isinstance(reason_codes, list)
            or not all(isinstance(item, str) for item in reason_codes)
        ):
            raise ValueError("Movie asset inventory summary fields are invalid")
        inventory = cls(
            schema_version=MOVIE_ASSET_INVENTORY_SCHEMA_VERSION,
            entries=tuple(entries),
            video_count=video_count,
            reason_codes=tuple(reason_codes),
            max_entries=max_entries,
            fingerprint=fingerprint,
        )
        expected = _fingerprint_payload(
            inventory.entries, inventory.reason_codes, inventory.max_entries
        )
        if fingerprint != expected:
            raise ValueError("Movie asset inventory fingerprint mismatch")
        return inventory


@dataclass(frozen=True)
class MovieAssetDrift:
    relative_path: str
    fields: Tuple[str, ...]


@dataclass(frozen=True)
class MovieAssetInventoryComparison:
    unchanged: bool
    expected_fingerprint: str
    current_fingerprint: str
    added_paths: Tuple[str, ...]
    removed_paths: Tuple[str, ...]
    changed: Tuple[MovieAssetDrift, ...]


def _normalized_suffix(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name).casefold()
    for suffix in _SUBTITLE_SUFFIXES:
        if normalized.endswith(suffix):
            return suffix
    return Path(normalized).suffix


def _relative_path(parts: Tuple[str, ...]) -> str:
    return PurePosixPath(*parts).as_posix()


def _is_untrusted_link(path: Path, stat_result: os.stat_result) -> bool:
    return path.is_symlink() or bool(
        getattr(stat_result, "st_file_attributes", 0) & _REPARSE_POINT_FLAG
    )


def _safe_lstat(path: Path, relative_path: Optional[str]) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as exc:
        raise MovieAssetInventoryError(
            "INVENTORY_FILESYSTEM_ERROR", relative_path
        ) from exc
    if _is_untrusted_link(path, result):
        raise MovieAssetInventoryError(
            "UNTRUSTED_FILESYSTEM_LINK", relative_path
        )
    return result


def _entry_type(name: str, mode: int) -> str:
    if stat.S_ISDIR(mode):
        if unicodedata.normalize("NFKC", name).casefold() in _DISC_DIRECTORY_NAMES:
            return "disc_structure"
        return "directory"
    if not stat.S_ISREG(mode):
        return "unsupported"
    suffix = _normalized_suffix(name)
    if suffix in VIDEO_SUFFIX:
        return "video"
    if suffix in _SUBTITLE_SUFFIXES:
        return "subtitle"
    return "associated"


def _fingerprint_payload(
    entries: Tuple[MovieAssetEntry, ...],
    reason_codes: Tuple[str, ...],
    max_entries: int,
) -> str:
    payload = {
        "schema_version": MOVIE_ASSET_INVENTORY_SCHEMA_VERSION,
        "entries": [asdict(entry) for entry in entries],
        "reason_codes": list(reason_codes),
        "max_entries": max_entries,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def inventory_movie_assets(
    root: Path,
    *,
    max_entries: int = 10_000,
) -> MovieAssetInventory:
    """Return a deterministic metadata snapshot without following links.

    ``root`` is an operator-selected local directory.  It is never persisted in
    or returned from the snapshot; every reported path is relative to it.
    """

    if (
        isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or max_entries < 1
    ):
        raise MovieAssetInventoryError("INVALID_INVENTORY_ENTRY_LIMIT")

    root_path = Path(os.path.abspath(os.fspath(root)))
    root_stat = _safe_lstat(root_path, None)
    if not stat.S_ISDIR(root_stat.st_mode) and not stat.S_ISREG(root_stat.st_mode):
        raise MovieAssetInventoryError("INVENTORY_ROOT_NOT_DIRECTORY")

    entries = []
    reason_codes = []
    root_is_file = stat.S_ISREG(root_stat.st_mode)
    root_is_disc = (
        not root_is_file and
        unicodedata.normalize("NFKC", root_path.name).casefold()
        in _DISC_DIRECTORY_NAMES
    )
    if root_is_file:
        entry_type = _entry_type(root_path.name, root_stat.st_mode)
        if entry_type == "unsupported":
            raise MovieAssetInventoryError(
                "UNSUPPORTED_FILESYSTEM_ENTRY", root_path.name
            )
        entries.append(MovieAssetEntry(
            root_path.name, entry_type, root_stat.st_size, root_stat.st_mtime_ns
        ))
    elif root_is_disc:
        reason_codes.append(DISC_STRUCTURE_REQUIRES_MANUAL_WORKFLOW)
        entries.append(
            MovieAssetEntry(".", "disc_structure", None, root_stat.st_mtime_ns)
        )
    else:
        pending = [(root_path, tuple())]
        while pending:
            directory, parent_parts = pending.pop()
            _safe_lstat(
                directory,
                _relative_path(parent_parts) if parent_parts else None,
            )
            try:
                remaining = max_entries - len(entries)
                if remaining < 1:
                    raise MovieAssetInventoryError(
                        "INVENTORY_MAX_ENTRIES_EXCEEDED",
                        _relative_path(parent_parts) if parent_parts else None,
                    )
                with os.scandir(directory) as iterator:
                    children = list(islice(iterator, remaining + 1))
                    if len(children) > remaining:
                        overflow_parts = parent_parts + (children[-1].name,)
                        raise MovieAssetInventoryError(
                            "INVENTORY_MAX_ENTRIES_EXCEEDED",
                            _relative_path(overflow_parts),
                        )
                    children.sort(
                        key=lambda item: (
                            unicodedata.normalize("NFKC", item.name).casefold(),
                            item.name,
                        ),
                    )
            except OSError as exc:
                relative = _relative_path(parent_parts) if parent_parts else None
                raise MovieAssetInventoryError(
                    "INVENTORY_FILESYSTEM_ERROR", relative
                ) from exc

            child_directories = []
            for child in children:
                parts = parent_parts + (child.name,)
                relative = _relative_path(parts)
                child_path = directory / child.name
                child_stat = _safe_lstat(child_path, relative)
                if (
                    stat.S_ISDIR(child_stat.st_mode)
                    and is_ignored_asset_directory(child.name)
                ):
                    continue
                entry_type = _entry_type(child.name, child_stat.st_mode)
                if entry_type == "unsupported":
                    raise MovieAssetInventoryError(
                        "UNSUPPORTED_FILESYSTEM_ENTRY", relative
                    )
                entries.append(
                    MovieAssetEntry(
                        relative_path=relative,
                        entry_type=entry_type,
                        size_bytes=(
                            child_stat.st_size
                            if stat.S_ISREG(child_stat.st_mode)
                            else None
                        ),
                        mtime_ns=child_stat.st_mtime_ns,
                    )
                )
                if entry_type == "directory":
                    child_directories.append((child_path, parts))
                elif entry_type == "disc_structure":
                    reason_codes.append(
                        DISC_STRUCTURE_REQUIRES_MANUAL_WORKFLOW
                    )

            # Stack is reversed so the resulting depth-first walk is sorted.
            pending.extend(reversed(child_directories))

    entry_tuple = tuple(entries)
    video_count = sum(entry.entry_type == "video" for entry in entry_tuple)
    if video_count > 1:
        reason_codes.append(MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION)
    elif video_count == 1:
        reason_codes.append(SINGLE_VIDEO_LAYOUT)
    else:
        reason_codes.append(NO_VIDEO_FILE_FOUND)
    reason_tuple = tuple(dict.fromkeys(reason_codes))
    fingerprint = _fingerprint_payload(entry_tuple, reason_tuple, max_entries)
    return MovieAssetInventory(
        schema_version=MOVIE_ASSET_INVENTORY_SCHEMA_VERSION,
        entries=entry_tuple,
        video_count=video_count,
        reason_codes=reason_tuple,
        max_entries=max_entries,
        fingerprint=fingerprint,
    )


def compare_movie_asset_inventory(
    root: Path,
    expected: MovieAssetInventory,
) -> MovieAssetInventoryComparison:
    """Rescan and expose relative-path type/size/mtime drift evidence."""

    current = inventory_movie_assets(root, max_entries=expected.max_entries)
    expected_by_path = {entry.relative_path: entry for entry in expected.entries}
    current_by_path = {entry.relative_path: entry for entry in current.entries}
    expected_paths = set(expected_by_path)
    current_paths = set(current_by_path)
    added = tuple(sorted(current_paths - expected_paths))
    removed = tuple(sorted(expected_paths - current_paths))
    changed = []
    for relative_path in sorted(expected_paths & current_paths):
        before = expected_by_path[relative_path]
        after = current_by_path[relative_path]
        fields = tuple(
            field
            for field in ("entry_type", "size_bytes", "mtime_ns")
            if getattr(before, field) != getattr(after, field)
        )
        if fields:
            changed.append(MovieAssetDrift(relative_path, fields))
    return MovieAssetInventoryComparison(
        unchanged=expected.fingerprint == current.fingerprint,
        expected_fingerprint=expected.fingerprint,
        current_fingerprint=current.fingerprint,
        added_paths=added,
        removed_paths=removed,
        changed=tuple(changed),
    )
