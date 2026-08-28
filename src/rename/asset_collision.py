"""Read-only evidence collection for approved existing-target reuse."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .rename_plan import CollisionResolution


class AssetCollisionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    sha256: str
    device: int
    inode: int


def fingerprint_file(path: Path) -> FileFingerprint:
    """Hash one stable regular file and reject changes during the read."""

    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise AssetCollisionError(f"碰撞证据不是普通文件：{path}")
    before = path.stat()
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise AssetCollisionError(f"无法读取碰撞文件：{path}: {exc}") from exc
    after = path.stat()
    identity_before = (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
    )
    identity_after = (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    )
    if identity_before != identity_after:
        raise AssetCollisionError(f"文件在计算散列期间发生变化：{path}")
    return FileFingerprint(
        size=after.st_size,
        sha256=digest.hexdigest(),
        device=after.st_dev,
        inode=after.st_ino,
    )


def build_reuse_resolution(
    source: Path, target: Path
) -> Optional[CollisionResolution]:
    """Return approved evidence only when both paths have identical bytes."""

    source = Path(source)
    target = Path(target)
    if target.is_symlink() or not target.is_file():
        return None
    try:
        source_stat = source.stat()
        target_stat = target.stat()
    except OSError as exc:
        raise AssetCollisionError(f"无法读取同名资产状态：{exc}") from exc
    if source_stat.st_size != target_stat.st_size:
        return None
    source_evidence = fingerprint_file(source)
    target_evidence = fingerprint_file(target)
    if source_evidence.sha256 != target_evidence.sha256:
        return None
    return CollisionResolution(
        action="reuse_existing",
        source_size=source_evidence.size,
        source_sha256=source_evidence.sha256,
        target_size=target_evidence.size,
        target_sha256=target_evidence.sha256,
        target_device=target_evidence.device,
        target_inode=target_evidence.inode,
    )


def validate_reuse_resolution(
    source: Path, target: Path, resolution: CollisionResolution
) -> tuple[FileFingerprint, FileFingerprint]:
    """Revalidate the exact approved bytes immediately before execution."""

    if resolution.action != "reuse_existing":
        raise AssetCollisionError("不支持的目标碰撞处置")
    source_evidence = fingerprint_file(source)
    target_evidence = fingerprint_file(target)
    if (
        source_evidence.size != resolution.source_size
        or source_evidence.sha256 != resolution.source_sha256
        or target_evidence.size != resolution.target_size
        or target_evidence.sha256 != resolution.target_sha256
        or source_evidence.sha256 != target_evidence.sha256
    ):
        raise AssetCollisionError("源文件或既有目标已变化，请重新生成整理计划")
    if (
        resolution.target_device is not None
        and target_evidence.device != resolution.target_device
    ) or (
        resolution.target_inode is not None
        and target_evidence.inode != resolution.target_inode
    ):
        raise AssetCollisionError("既有目标实体已被替换，请重新生成整理计划")
    return source_evidence, target_evidence
