"""Immutable path observations consumed by the one asset collision algorithm."""
from dataclasses import dataclass, replace
from pathlib import Path
import stat

from .asset_collision import AssetCollisionError, reuse_resolution_for_observations, requires_manual_collision
from .media_file_identity import MediaFileIdentity
from .small_file_identity import SmallFileFingerprint, small_fingerprint
from .rename_plan import PlanEntry


class MissingCollisionProbe(ValueError):
    def __init__(self, path, *, full_hash=False):
        super().__init__('碰撞观察缺失，必须重新收集预览证据')
        self.path, self.full_hash = Path(path), full_hash


@dataclass(frozen=True)
class PathProbe:
    path: Path
    exists: bool
    signature: tuple
    parents: tuple
    content: SmallFileFingerprint | None = None


@dataclass(frozen=True)
class CollisionProjection:
    entries: tuple[PlanEntry, ...]
    unresolved: tuple[str, ...]


@dataclass(frozen=True)
class CollisionProbes:
    rows: tuple[PathProbe, ...]

    def __post_init__(self):
        if (type(self.rows) is not tuple or any(type(row) is not PathProbe for row in self.rows)
                or len({row.path for row in self.rows}) != len(self.rows)):
            raise ValueError('碰撞观察必须是唯一的冻结路径表')

    def lookup(self, path, *, full_hash=False):
        path = Path(path)
        row = next((row for row in self.rows if row.path == path), None)
        if row is None or (full_hash and row.content is None):
            raise MissingCollisionProbe(path, full_hash=full_hash)
        return row

    def exists(self, path):
        return self.lookup(path).exists

    def reuse(self, source, target):
        target_row = self.lookup(target)
        if not target_row.exists or not stat.S_ISREG(target_row.signature[0]):
            return None
        source_row = self.lookup(source)
        if not source_row.exists or not stat.S_ISREG(source_row.signature[0]):
            raise AssetCollisionError('碰撞来源不是普通文件')
        if source_row.signature[3] != target_row.signature[3]:
            return None
        if Path(source).absolute() == Path(target).absolute():
            return None
        entity = lambda row: MediaFileIdentity(row.signature[3], row.signature[1], row.signature[2])
        if requires_manual_collision(source):
            return reuse_resolution_for_observations(entity(source_row), entity(target_row))
        return reuse_resolution_for_observations(self.lookup(source, full_hash=True).content,
                                             self.lookup(target, full_hash=True).content)


def _parents(path):
    rows = []
    for parent in reversed(Path(path).absolute().parents):
        try:
            value = parent.lstat()
        except FileNotFoundError:
            rows.append((str(parent), None))
            break
        if stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400:
            raise AssetCollisionError('碰撞路径祖先存在链接或重解析点')
        rows.append((str(parent), value.st_dev, value.st_ino, value.st_mode))
    return tuple(rows)


def observe_path(path, *, full_hash=False):
    """Read one path including absence and physical ancestor identity."""
    path = Path(path)
    parents = _parents(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        result = PathProbe(path, False, (), parents)
    else:
        if stat.S_ISLNK(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400:
            raise AssetCollisionError('碰撞路径存在链接或重解析点')
        signature = lambda value: (value.st_mode, value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        content = small_fingerprint(path) if full_hash else None
        if signature(before) != signature(path.lstat()):
            raise AssetCollisionError('碰撞路径读取期间发生变化')
        result = PathProbe(path, True, signature(before), parents, content)
    if parents != _parents(path):
        raise AssetCollisionError('碰撞路径祖先在观察期间发生变化')
    return result


def verify_probes(probes):
    if type(probes) is not CollisionProbes:
        raise ValueError('缺少冻结碰撞观察')
    if any(observe_path(row.path, full_hash=row.content is not None) != row for row in probes.rows):
        raise ValueError('目标、备用目录或碰撞来源已变化，请重新预览')


def observe_collisions(entries, work_root, *, package_label=None):
    """The observer supplies exactly the probes requested by the pure algorithm."""
    entries = tuple(entries)
    rows = {}
    for _ in range(6 * len(entries) + 8):
        probes = CollisionProbes(tuple(rows.values()))
        try:
            projected = project_collisions(entries, work_root, probes, package_label=package_label)
        except MissingCollisionProbe as request:
            observed = observe_path(request.path, full_hash=request.full_hash)
            if rows.get(request.path) == observed:
                raise ValueError('碰撞观察未取得所需证据')
            rows[request.path] = observed
            continue
        if type(projected) is not CollisionProjection:
            raise ValueError('碰撞唯一投影器已断开')
        verify_probes(probes)
        return probes, projected
    raise ValueError('碰撞观察超过有限路径请求上限')


def project_collisions(entries: tuple[PlanEntry, ...], work_root: Path, probes: CollisionProbes,
                       *, package_label=None):
    """Filesystem-free replay; no callbacks or fallback observations are accepted."""
    if type(probes) is not CollisionProbes or type(entries) is not tuple:
        raise ValueError('碰撞投影缺少冻结输入')
    from .plan_review import _safe_media_name, _normalized_key

    descriptors = {
        asset.version_descriptor.version_ref: asset.version_descriptor
        for entry in entries
        if (asset := entry.presentation_asset) is not None
        and asset.version_descriptor is not None
        and asset.version_descriptor.version_ref is not None
    }
    revised = []
    unresolved = []
    for entry in entries:
        if not probes.exists(entry.target_path):
            revised.append(entry)
            continue
        asset = entry.presentation_asset
        if (
            asset is None
            or asset.asset_role.kind not in {"extra", "associated"}
            or entry.media_kind == "subtitle"
        ):
            unresolved.append(entry.source_path.name)
            revised.append(entry)
            continue
        resolution = probes.reuse(entry.source_path, entry.target_path)
        if resolution is not None:
            revised.append(replace(
                entry,
                collision_resolution=resolution,
                evidence=entry.evidence + (
                    "既有目标与本次源文件内容摘要相同，将复用已有文件",
                ),
            ))
            continue
        # A linked external audio track must remain beside its owning video.
        # An unrelated fallback directory would break automatic playback and
        # would be undone by the canonical audio projector on the next pass.
        if entry.role == 'external_audio':
            unresolved.append(entry.source_path.name)
            revised.append(entry)
            continue
        owner = asset.owner_version_ref
        descriptor = descriptors.get(owner) if owner not in {None, "shared"} else None
        label = _safe_media_name(descriptor.display_label or "") if descriptor else ""
        package = _safe_media_name(package_label or "")
        candidate = None
        if label and asset.asset_role.kind == "extra":
            if _normalized_key(label) not in _normalized_key(entry.target_path.stem):
                candidate = entry.target_path.with_name(
                    f"{entry.target_path.stem} - {label}{entry.target_path.suffix}"
                )
        if candidate is not None:
            if not probes.exists(candidate):
                revised.append(replace(
                    entry,
                    target_path=candidate,
                    collision_resolution=None,
                    evidence=entry.evidence + (
                        f"同名资产不能直接复用，按版本 {label} 保存",
                    ),
                ))
                continue
            candidate_reuse = probes.reuse(entry.source_path, candidate)
            if candidate_reuse is not None:
                revised.append(replace(
                    entry,
                    target_path=candidate,
                    collision_resolution=candidate_reuse,
                    evidence=entry.evidence + (
                        f"版本目录中的既有资产与来源包内容相同，将复用已有文件",
                    ),
                ))
                continue

        package_candidate = None
        if package and asset.asset_role.kind == "extra":
            suffix = f" - {label}" if label else ""
            package_candidate = entry.target_path.with_name(
                f"{entry.target_path.stem}{suffix} - {package}{entry.target_path.suffix}"
            )
        if package_candidate is not None:
            if not probes.exists(package_candidate):
                revised.append(replace(
                    entry,
                    target_path=package_candidate,
                    collision_resolution=None,
                    evidence=entry.evidence + (
                        "同名资产不能直接复用，按来源包建立独立目标",
                    ),
                ))
                continue
            package_reuse = probes.reuse(entry.source_path, package_candidate)
            if package_reuse is not None:
                revised.append(replace(
                    entry,
                    target_path=package_candidate,
                    collision_resolution=package_reuse,
                    evidence=entry.evidence + (
                        "同一来源包目标内容相同，将复用已有文件",
                    ),
                ))
                continue
        unresolved.append(entry.source_path.name)
        revised.append(entry)
    return CollisionProjection(tuple(revised), tuple(unresolved))
