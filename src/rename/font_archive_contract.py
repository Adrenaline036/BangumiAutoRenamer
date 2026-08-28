"""Shared naming and evidence contracts for strict ``[Fonts].7z`` assets."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from typing import Mapping
import unicodedata

from .asset_collision import (
    AssetCollisionError,
    fingerprint_file,
    validate_reuse_resolution,
)
from .plan_store import PlanStore
from .rename_plan import PlanEntry, RenamePlan
from .utils import is_font_archive
from .work_intent import (
    WORK_FONT_APPEND_POLICY,
    WORK_FONT_CONDITIONAL_ACTION,
)


class FontArchiveContractError(RuntimeError):
    pass


_WINDOWS_RESERVED_COMPONENT = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$",
    re.IGNORECASE,
)
_FONT_COMPONENT_UTF16_LIMIT = 240
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_FONT_ARCHIVE_SUFFIXES = frozenset({".7z", ".zip"})


def is_supported_font_archive(path: Path | str) -> bool:
    """Return whether an explicitly confirmed font archive can stay packed."""

    return Path(path).suffix.casefold() in _SUPPORTED_FONT_ARCHIVE_SUFFIXES


def is_font_archive_candidate(path: Path | str) -> bool:
    """Recognize a standalone ``[Fonts]`` marker in a supported archive name."""

    candidate = Path(path)
    if not is_supported_font_archive(candidate):
        return False
    stem = unicodedata.normalize("NFKC", candidate.stem).casefold()
    token = "[fonts]"
    start = stem.find(token)
    while start >= 0:
        before = stem[start - 1] if start else ""
        end = start + len(token)
        after = stem[end] if end < len(stem) else ""
        if (not before or not before.isalnum()) and (
            not after or not after.isalnum()
        ):
            return True
        start = stem.find(token, start + 1)
    return False


@dataclass(frozen=True)
class FrozenFontAsset:
    entry: PlanEntry
    size: int
    sha256: str

    @property
    def content_key(self) -> str:
        return f"sha256:{self.size}:{self.sha256}"


def _utf16_code_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _truncate_utf16(value: str, limit: int) -> str:
    result = value
    while result and _utf16_code_units(result) > limit:
        result = result[:-1]
    return result.rstrip(" .")


def canonical_font_archive_target(
    canonical_root: Path,
    digest: str,
    *,
    disambiguate: bool,
    archive_suffix: str = ".7z",
) -> Path:
    """Return a stable, Windows-safe human-readable font archive target."""

    normalized_digest = str(digest or "").casefold()
    if _SHA256.fullmatch(normalized_digest) is None:
        raise FontArchiveContractError("字体内容指纹无效")
    suffix = str(archive_suffix or "").casefold()
    if suffix not in _SUPPORTED_FONT_ARCHIVE_SUFFIXES:
        raise FontArchiveContractError("字体归档扩展名无效")
    # Re-wrapping an existing WindowsPath whose final component contains ':'
    # makes pathlib reinterpret that component as a drive-relative path.
    root = canonical_root if isinstance(canonical_root, Path) else Path(canonical_root)
    base = unicodedata.normalize("NFC", root.name).strip()
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).rstrip(" .")
    if not base or _WINDOWS_RESERVED_COMPONENT.fullmatch(base):
        raise FontArchiveContractError("作品名称无法生成安全的字体文件名")
    qualifier = f" [{normalized_digest}]" if disambiguate else ""
    tail = f"{qualifier} [Fonts]{suffix}"
    candidate = f"{base}{tail}"
    if _utf16_code_units(candidate) > _FONT_COMPONENT_UTF16_LIMIT:
        stable = hashlib.sha256(base.encode("utf-8")).hexdigest()[:12]
        marker = f" [{stable}]"
        budget = _FONT_COMPONENT_UTF16_LIMIT - _utf16_code_units(marker + tail)
        base = _truncate_utf16(base, budget)
        if not base:
            raise FontArchiveContractError("作品名称过长，无法生成安全的字体文件名")
        candidate = f"{base}{marker}{tail}"
    return root / "Associated" / candidate


def is_movie_plan(plan: RenamePlan) -> bool:
    return {group.kind for group in plan.presentation_groups} == {"movie_continuous"}


def _work_key(plan: RenamePlan) -> str:
    return str(
        plan.metadata.get("catalog_work_key")
        or plan.metadata.get("proposed_work_key")
        or ""
    )


def _strict_movie_font_entry(entry: PlanEntry) -> bool:
    contract = entry.presentation_asset
    return (
        (is_font_archive(entry.source_path) or (
            entry.role == "font" and is_supported_font_archive(entry.source_path)
        ))
        and entry.media_kind == "associated"
        and entry.role == "font"
        and contract is not None
        and contract.asset_role.kind == "associated"
        and contract.owner_version_ref == "shared"
    )


def validate_movie_font_manifest(
    plan: RenamePlan,
    *,
    verify_current: bool = True,
) -> tuple[FrozenFontAsset, ...]:
    """Validate the immutable work-scoped font manifest before media writes."""

    if not is_movie_plan(plan):
        return ()
    fonts = [entry for entry in plan.entries if _strict_movie_font_entry(entry)]
    manifest = plan.metadata.get("associated_asset_manifest")
    if not fonts:
        if manifest is not None:
            raise FontArchiveContractError("无字体电影计划不应包含字体来源清单")
        return ()
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
        raise FontArchiveContractError("电影字体来源清单格式无效")
    if manifest.get("scope") != "work":
        raise FontArchiveContractError("电影字体来源清单作用域无效")
    work_key = _work_key(plan)
    if manifest.get("catalog_binding") != {"backend": "work", "key": work_key}:
        raise FontArchiveContractError("电影字体来源清单未绑定当前作品登记")
    for key in (
        "season_number", "season_id", "series_key", "season_binding_id",
        "provider_link_snapshot",
    ):
        if plan.metadata.get(key) is not None:
            raise FontArchiveContractError("电影字体计划混入了剧集季度身份")
    rows = manifest.get("assets")
    if not isinstance(rows, (list, tuple)):
        raise FontArchiveContractError("电影字体来源清单资产无效")
    entries = {
        (_path_key(entry.source_path), _path_key(entry.target_path)): entry
        for entry in fonts
    }
    if len(entries) != len(fonts):
        raise FontArchiveContractError("电影字体条目重复")
    seen = set()
    frozen = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise FontArchiveContractError("电影字体来源清单条目无效")
        source = Path(str(row.get("source_path") or ""))
        target = Path(str(row.get("target_path") or ""))
        pair = (_path_key(source), _path_key(target))
        entry = entries.get(pair)
        size = row.get("source_size")
        digest = str(row.get("source_sha256") or "").casefold()
        if (
            row.get("role") != "fonts"
            or row.get("scope") != "work"
            or row.get("season_number") is not None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or _SHA256.fullmatch(digest) is None
            or entry is None
            or not _strict_movie_font_entry(entry)
            or pair in seen
        ):
            raise FontArchiveContractError("电影字体来源清单与批准条目不一致")
        seen.add(pair)
        if verify_current:
            try:
                source_evidence = fingerprint_file(source)
                if source_evidence.size != size or source_evidence.sha256 != digest:
                    raise FontArchiveContractError(
                        "电影字体源文件已变化，请重新生成并批准整理计划"
                    )
                if entry.collision_resolution is not None:
                    _source, target_evidence = validate_reuse_resolution(
                        source, target, entry.collision_resolution
                    )
                    if target_evidence.size != size or target_evidence.sha256 != digest:
                        raise FontArchiveContractError(
                            "批准复用的电影字体位置已变化，请重新生成整理计划"
                        )
            except AssetCollisionError as exc:
                raise FontArchiveContractError(str(exc)) from exc
        frozen.append(FrozenFontAsset(entry=entry, size=size, sha256=digest))
    if seen != set(entries):
        raise FontArchiveContractError("电影字体来源清单未覆盖全部批准字体条目")
    return tuple(frozen)


def validate_completed_movie_font_assets(
    plan: RenamePlan,
    ledger: Mapping[str, object],
) -> tuple[FrozenFontAsset, ...]:
    """Verify manifest, completed ledger and current hardlink/reuse evidence."""

    frozen = validate_movie_font_manifest(plan, verify_current=True)
    if not frozen:
        return ()
    if (
        not isinstance(ledger, Mapping)
        or ledger.get("status") != "completed"
        or ledger.get("task_id") != plan.task_id
        or ledger.get("plan_fingerprint") != PlanStore.fingerprint(plan)
    ):
        raise FontArchiveContractError("电影字体缺少可信的已完成执行记录")
    rows = ledger.get("entries")
    if not isinstance(rows, list) or len(rows) != len(plan.entries):
        raise FontArchiveContractError("电影字体执行记录条目数量不匹配")
    by_pair = {
        (_path_key(Path(str(row.get("source") or ""))),
         _path_key(Path(str(row.get("target") or "")))): row
        for row in rows if isinstance(row, Mapping)
    }
    for item in frozen:
        entry = item.entry
        pair = (_path_key(entry.source_path), _path_key(entry.target_path))
        row = by_pair.get(pair)
        if not isinstance(row, Mapping):
            raise FontArchiveContractError("电影字体执行记录与批准条目不一致")
        try:
            source_evidence = fingerprint_file(entry.source_path)
            target_evidence = fingerprint_file(entry.target_path)
        except AssetCollisionError as exc:
            raise FontArchiveContractError(str(exc)) from exc
        if (
            source_evidence.size != item.size
            or source_evidence.sha256 != item.sha256
            or target_evidence.size != item.size
            or target_evidence.sha256 != item.sha256
        ):
            raise FontArchiveContractError("电影字体当前内容与批准证据不一致")
        if entry.collision_resolution is None:
            if row.get("status") == "reused_identical_asset":
                intent = plan.metadata.get("work_font_intent")
                assets = intent.get("assets") if isinstance(intent, Mapping) else None
                authority = row.get("conditional_authority")
                matching = [
                    value for value in (assets or ())
                    if isinstance(value, Mapping)
                    and _path_key(Path(str(value.get("source_path") or "")))
                    == _path_key(entry.source_path)
                    and _path_key(Path(str(value.get("target_path") or "")))
                    == _path_key(entry.target_path)
                    and value.get("content_key") == item.content_key
                    and value.get("action") == WORK_FONT_CONDITIONAL_ACTION
                ]
                work_key = str(
                    plan.metadata.get("catalog_work_key")
                    or plan.metadata.get("proposed_work_key")
                    or ""
                )
                if not (
                    len(matching) == 1
                    and isinstance(authority, Mapping)
                    and authority.get("policy") == WORK_FONT_APPEND_POLICY
                    and authority.get("work_key") == work_key
                    and authority.get("content_key") == item.content_key
                    and _path_key(Path(str(authority.get("target_path") or "")))
                    == _path_key(entry.target_path)
                    and row.get("action") == "reuse_existing"
                    and row.get("transfer_type") == "reuse_existing"
                    and row.get("source_size") == item.size
                    and row.get("source_sha256") == item.sha256
                    and row.get("target_size") == item.size
                    and row.get("target_sha256") == item.sha256
                ):
                    raise FontArchiveContractError(
                        "电影字体条件复用记录与批准的作品意图不一致"
                    )
                continue
            try:
                target_nlink = entry.target_path.stat().st_nlink
            except OSError as exc:
                raise FontArchiveContractError("无法读取电影字体hardlink证据") from exc
            recorded_identity = tuple(
                row.get(key) for key in (
                    "source_device", "source_inode", "target_device", "target_inode"
                )
            )
            identity_matches = recorded_identity == (
                source_evidence.device,
                source_evidence.inode,
                target_evidence.device,
                target_evidence.inode,
            )
            legacy_identity_absent = all(value is None for value in recorded_identity)
            if (
                row.get("status") != "created"
                or row.get("action") != "create"
                or row.get("transfer_type") != "hardlink"
                or source_evidence.device != target_evidence.device
                or source_evidence.inode != target_evidence.inode
                or target_nlink < 2
                or not (identity_matches or legacy_identity_absent)
            ):
                raise FontArchiveContractError("电影字体不再满足已完成hardlink证明")
        else:
            try:
                validate_reuse_resolution(
                    entry.source_path, entry.target_path, entry.collision_resolution
                )
            except AssetCollisionError as exc:
                raise FontArchiveContractError(str(exc)) from exc
            if (
                row.get("status") != "reused_identical_asset"
                or row.get("action") != "reuse_existing"
                or row.get("transfer_type") != "reuse_existing"
            ):
                raise FontArchiveContractError("电影字体复用记录与批准证据不一致")
    return frozen


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))
