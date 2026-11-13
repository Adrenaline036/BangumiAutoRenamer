"""Read-only evidence collection for approved existing-target reuse."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .rename_plan import CollisionResolution
from .media_file_identity import MEDIA_IDENTITY, MediaFileIdentity, media_identity
from .small_file_identity import SMALL_CONTENT, SmallFileFingerprint, small_fingerprint


class AssetCollisionError(RuntimeError):
    pass


def requires_manual_collision(path):
    from .utils import VIDEO_SUFFIX
    from .content_associations import AUDIO_SUFFIXES
    return Path(path).suffix.casefold() in set(VIDEO_SUFFIX) | AUDIO_SUFFIXES | {'.tak', '.ape'}






def build_reuse_resolution(
    source: Path, target: Path
) -> Optional[CollisionResolution]:
    """Return approved evidence only when both paths have identical bytes."""

    source = Path(source)
    target = Path(target)
    if target.is_symlink() or not target.is_file():
        return None
    try:
        source_id, target_id = media_identity(source), media_identity(target)
        if source.absolute() == target.absolute() or source_id.size != target_id.size:
            return None
        if requires_manual_collision(source):
            identity = reuse_resolution_for_observations(source_id, target_id)
            return identity if identity is not None and target.stat().st_nlink >= 2 else None
        return reuse_resolution_for_observations(small_fingerprint(source), small_fingerprint(target))
    except (OSError, ValueError) as exc:
        raise AssetCollisionError(str(exc)) from exc


def identity_reuse_resolution(source, target):
    if type(source) is not MediaFileIdentity or type(target) is not MediaFileIdentity:
        raise AssetCollisionError('媒体复用缺少文件系统证据')
    if source != target:
        return None
    return CollisionResolution('reuse_existing', source.size, target.size,
                               target.device, target.inode, MEDIA_IDENTITY)


def reuse_resolution_for_observations(source, target):
    """冻结证据的类型决定复用合同，小文件始终保留内容摘要。"""
    if type(source) is SmallFileFingerprint and type(target) is SmallFileFingerprint:
        return reuse_resolution_from_evidence(source, target)
    if type(source) is MediaFileIdentity and type(target) is MediaFileIdentity:
        return identity_reuse_resolution(source, target)
    raise AssetCollisionError('复用两端缺少一致的类型化文件证据')


def reuse_resolution_from_evidence(source_evidence: SmallFileFingerprint, target_evidence: SmallFileFingerprint):
    """The shared pure reuse decision for two fully observed regular files."""
    if type(source_evidence) is not SmallFileFingerprint or type(target_evidence) is not SmallFileFingerprint:
        raise AssetCollisionError('复用决定缺少完整文件证据')
    if (source_evidence.size != target_evidence.size
            or source_evidence.xxh3_128 != target_evidence.xxh3_128):
        return None
    return CollisionResolution(
        action="reuse_existing",
        source_size=source_evidence.size,
        target_size=target_evidence.size,
        target_device=target_evidence.device,
        target_inode=target_evidence.inode,
        evidence_kind=SMALL_CONTENT,
        source_xxh3_128=source_evidence.xxh3_128,
        target_xxh3_128=target_evidence.xxh3_128,
    )



def validate_reuse_resolution(source, target, resolution, *, source_identity=None):
    # 执行消费批准来源身份；显式观察入口可以现场采集。
    try:
        if (resolution.action != 'reuse_existing' or
                CollisionResolution.from_dict(resolution.to_dict()) != resolution):
            raise ValueError('目标复用证据不完整')
        source_id = media_identity(source) if source_identity is None else source_identity
        expected = MediaFileIdentity(resolution.target_size, resolution.target_device, resolution.target_inode)
        from .media_file_identity import verify_link_target
        verify_link_target(target, expected, hardlink=resolution.evidence_kind == MEDIA_IDENTITY)
        if source_id.size != resolution.source_size or Path(source).absolute() == Path(target).absolute():
            raise ValueError('来源与批准复用决定不一致')
        if resolution.evidence_kind == MEDIA_IDENTITY:
            if source_id != expected:
                raise ValueError('既有目标已被替换或不再属于批准来源')
            return source_id, expected
        if resolution.evidence_kind != SMALL_CONTENT or requires_manual_collision(source):
            raise ValueError('目标复用证据与文件类型不符')
        original = (small_fingerprint(source) if source_identity is None else
                    SmallFileFingerprint(source_id.size, resolution.source_xxh3_128, source_id.device, source_id.inode))
        current = small_fingerprint(target)
        if (original.xxh3_128 != resolution.source_xxh3_128 or
                current.xxh3_128 != resolution.target_xxh3_128 or original.xxh3_128 != current.xxh3_128):
            raise ValueError('来源或既有目标内容与批准决定不一致')
        return original, current
    except (ValueError, OSError) as exc:
        raise AssetCollisionError(str(exc)) from exc
