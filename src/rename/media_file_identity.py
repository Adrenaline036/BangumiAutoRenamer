"""Short-lived filesystem identity for media actions; no content fingerprint."""
from dataclasses import dataclass
from pathlib import Path


MEDIA_IDENTITY = 'hardlink-identity-v1'


@dataclass(frozen=True)
class MediaFileIdentity:
    size: int
    device: int
    inode: int
    kind: str = MEDIA_IDENTITY

    def __post_init__(self):
        if (self.kind != MEDIA_IDENTITY or
                any(type(v) is not int or v < 0 for v in (self.size, self.device, self.inode)) or
                self.inode == 0):
            raise ValueError('媒体文件缺少可靠的文件系统身份')



def media_identity(path):
    from .path_safety import observe_regular_file
    observed = observe_regular_file(path)
    return MediaFileIdentity(observed.st_size, observed.st_dev, observed.st_ino)


def frozen_sources(plan):
    """从已批准清单读取身份；不重新观察源路径。"""
    import re
    from collections.abc import Mapping
    for entry in plan.entries:
        source = entry.source_path
        if (not source.is_absolute() or '..' in source.parts or
                not source.is_relative_to(plan.source_root) or source == plan.source_root):
            raise ValueError('来源越出批准源根目录')
    rows = plan.metadata.get('source_identity_manifest')
    if rows is None and not plan.metadata.get('projection_authority_required'):
        return {}
    if not isinstance(rows, (list, tuple)):
        raise ValueError('Plan 缺少冻结来源物理身份')
    result = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError('来源身份条目无效')
        source = Path(str(row.get('source_path') or ''))
        physical = re.fullmatch(r'device:(\d+):inode:(\d+)', str(row.get('physical_key') or ''))
        from .physical_identity import location_key_from_resolved
        if (not source.is_absolute() or '..' in source.parts or physical is None
                or row.get('location_key') != location_key_from_resolved(source) or source in result):
            raise ValueError('来源身份路径或物理标识无效')
        result[source] = MediaFileIdentity(row.get('size'), int(physical[1]), int(physical[2]))
    for entry in plan.entries:
        source = entry.source_path
        if (source not in result or not source.is_relative_to(plan.source_root)
                or source == plan.source_root):
            raise ValueError('来源不属于批准清单或源根目录')
    return {entry.source_path: result[entry.source_path] for entry in plan.entries}


def verify_link_target(path, expected, *, hardlink=True):
    """核对本次目标，期望值只能来自采集身份或执行凭据。"""
    from .path_safety import observe_regular_file
    observed = observe_regular_file(path)
    if ((observed.st_size, observed.st_dev, observed.st_ino) !=
            (expected.size, expected.device, expected.inode)
            or (hardlink and observed.st_nlink < 2)):
        raise ValueError('目标不再满足已记录的硬链接身份和大小')
    return observed


def verify_recorded_target(plan, entry, row, *, sources=None):
    """完成凭据与冻结来源绑定；旧摘要字段只读保留。"""
    from .path_safety import observe_regular_file
    observed = observe_regular_file(entry.target_path)
    expected = (frozen_sources(plan) if sources is None else sources).get(entry.source_path)
    size = row.get('source_size')
    if size is None and expected is not None:
        size = expected.size
    source = MediaFileIdentity(observed.st_size if size is None else size, row.get('source_device'), row.get('source_inode'))
    if expected is not None and source != expected:
        raise ValueError('执行来源身份与批准清单不一致')
    target_size = row.get('target_size')
    target = MediaFileIdentity(source.size if target_size is None else target_size,
                               row.get('target_device'), row.get('target_inode'))
    reused = entry.collision_resolution is not None
    if reused:
        collision = entry.collision_resolution
        if (row.get('evidence_kind') != collision.evidence_kind
                or target != MediaFileIdentity(collision.target_size, collision.target_device, collision.target_inode)
                or source.size != collision.source_size
                or any(row.get(k) != getattr(collision, k) for k in
                       ('source_xxh3_128','target_xxh3_128'))):
            raise ValueError('复用执行凭据与批准决定不一致')
    elif source != target:
        raise ValueError('完成凭据未证明同一硬链接实体')
    linked = not reused or source == target
    if linked and (source != target or type(row.get('target_link_count')) is not int or row['target_link_count'] < 2):
        raise ValueError('完成凭据缺少硬链接证明')
    if ((observed.st_size, observed.st_dev, observed.st_ino) != (target.size, target.device, target.inode)
            or (linked and observed.st_nlink < 2)):
        raise ValueError('目标与执行凭据不一致')
    if reused and not linked:
        from .asset_collision import AssetCollisionError, validate_reuse_resolution
        try:
            validate_reuse_resolution(entry.source_path, entry.target_path, collision, source_identity=source)
        except AssetCollisionError as exc:
            raise ValueError(str(exc)) from exc
    return observed


