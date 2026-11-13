"""Bounded, lossless source grouping observations, not work/season authority."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

from .media_classifier import EXTRA_DIRECTORY_NAMES, is_audio_disc_directory, _normalize_directory_name
from .path_safety import first_reparse_component, _is_link, _directory_identity
from .physical_identity import location_key_from_resolved
from .source_basis import _identity_from_stat, _exact_layout_root
from .task_batch_scope import SourceIdentity
from .utils import VIDEO_SUFFIX


class SourceTitleUncertain(ValueError):
    """A season folder has no consistent actual file-title observation."""


def season_folder(name):
    match = re.fullmatch(r'(?:season\s*|s)(\d{1,2})|第\s*(\d{1,2})\s*季', name.strip(), re.I)
    return int(next(value for value in match.groups() if value is not None)) if match else None


@dataclass(frozen=True)
class SourceGroup:
    path: Path
    input_scope: tuple[str, ...]
    scope_kind: str
    identities: tuple[SourceIdentity, ...]
    reason: str = ''

    def to_dict(self):
        return dict(path=str(self.path), input_scope=list(self.input_scope), scope_kind=self.scope_kind,
                    identities=[row.to_dict() for row in self.identities], reason=self.reason)


@dataclass(frozen=True)
class SourceDiscovery:
    roots: tuple[Path, ...]
    entries: tuple[str, ...]
    groups: tuple[SourceGroup, ...]
    identities: tuple[SourceIdentity, ...]


def discover_sources(paths, *, max_entries=10000, max_depth=32):
    """Fail the entire observation on unreadable, linked or truncated inputs."""
    roots, entries, groups, identities = [], [], [], []
    seen_locations, seen_physical = set(), set()
    for supplied in paths:
        candidate = Path(supplied).expanduser()
        if first_reparse_component(candidate) is not None:
            raise ValueError('来源包含链接或重解析点，请重新选择普通目录')
        root = candidate.resolve(strict=True)
        if root in roots or any(root in old.parents or old in root.parents for old in roots):
            raise ValueError('选择的来源重复或父子重叠，请保留一个完整来源')
        roots.append(root)
        files, directories = {}, []
        parents = {parent: _directory_identity(parent.lstat()) for parent in reversed(root.parents)}
        root_info = root.lstat()

        def visit(path, depth, info):
            if depth > max_depth or len(entries) >= max_entries:
                raise ValueError('来源扫描超过数量或深度上限，未生成部分任务；请缩小来源')
            # Windows 3.11 DirEntry metadata may omit dev/ino. Keep the real
            # identity contract; Linux can use the complete DirEntry result.
            if int(info.st_ino) <= 0:
                info = path.lstat()
            if _is_link(info):
                raise ValueError('来源包含链接或重解析点，未完成扫描')
            entries.append(str(path))
            if stat.S_ISDIR(info.st_mode):
                directories.append(path)
                parents[path] = _directory_identity(info)
                with os.scandir(path) as stream:
                    children = sorted(stream, key=lambda item: item.name.casefold())
                for child in children:
                    visit(Path(child.path), depth + 1, child.stat(follow_symlinks=False))
            elif stat.S_ISREG(info.st_mode):
                # Actual directory-entry names below a resolved, checked root.
                identity = SourceIdentity.from_mapping(_identity_from_stat(
                    path, info, location_key=location_key_from_resolved(path)))
                if identity.location_key in seen_locations or identity.physical_key in seen_physical:
                    raise ValueError('来源包含重复位置或同一物理文件，不能分配到不同任务')
                seen_locations.add(identity.location_key)
                seen_physical.add(identity.physical_key)
                files[path] = identity
                identities.append(identity)
            else:
                raise ValueError('来源包含非普通文件，未完成扫描')

        visit(root, 0, root_info)
        for parent, identity in parents.items():
            if _directory_identity(parent.lstat()) != identity:
                raise ValueError('来源扫描期间父目录已变化，请重新选择')
        if not files:
            raise ValueError('来源内没有普通文件，请重新选择')
        assigned = set()

        def rows(selected):
            return tuple(files[path] for path in sorted(selected, key=lambda item: str(item).casefold()))

        def walk(folder):
            descendants = {path for path in files if folder in path.parents}
            if not descendants:
                return
            videos = {path for path in descendants if path.suffix.lower() in VIDEO_SUFFIX}
            if not videos:
                return
            supplemental = any(_normalize_directory_name(part) in EXTRA_DIRECTORY_NAMES or is_audio_disc_directory(part)
                               for part in folder.parts)
            direct = any(path.parent == folder for path in videos)
            independent = any(directory != folder and folder in directory.parents
                              and season_folder(directory.name) is not None
                              and any(directory in path.parents for path in videos) for directory in directories)
            if supplemental or direct:
                reason = ('花絮、特典或音频附加内容；请核对完整来源组' if supplemental else
                          '正片与独立季度混合在同一层级；请核对完整来源组' if independent else '')
                groups.append(SourceGroup(folder, (str(folder),), 'parent_directory', rows(descendants), reason))
                assigned.update(descendants)
                return
            for child in directories:
                if child.parent == folder:
                    walk(child)

        if stat.S_ISREG(root_info.st_mode):
            reason = '' if root.suffix.lower() in VIDEO_SUFFIX else '独立附件尚未明确所属作品；请核对完整来源组'
            groups.append(SourceGroup(root, (str(root),), 'exact_file', rows(files), reason))
            assigned.update(files)
        else:
            walk(root)
        remainder = set(files) - assigned
        if remainder:
            selected = rows(remainder)
            inputs = tuple(row.source_path for row in selected)
            groups.append(SourceGroup(_exact_layout_root(inputs), inputs, 'exact_file', selected,
                                      '季度外共享或未分配附件；未自动复制到任何季度，请核对完整来源组'))
    if not groups:
        raise ValueError('请先选择来源')
    return SourceDiscovery(tuple(roots), tuple(entries), tuple(groups), tuple(identities))


def collect_source(path):
    """单任务与批量预览共用首次采集；单任务保留完整输入范围。"""
    observed = discover_sources((path,))
    root = observed.roots[0]
    kind = 'exact_file' if any(row.source_path == str(root) for row in observed.identities) else 'parent_directory'
    return SourceGroup(root, (str(root),), kind, observed.identities)


def validate_discovery(discovery):
    """确认冻结清单被完整分组；来源变化由明确重新扫描处理。"""
    if type(discovery) is not SourceDiscovery or not discovery.roots or not discovery.identities:
        raise ValueError('缺少完整来源发现结果')
    entries = set(discovery.entries)
    rows = tuple(row for group in discovery.groups for row in group.identities)
    if (len(entries) != len(discovery.entries) or set(rows) != set(discovery.identities)
            or len(rows) != len(discovery.identities) or len(set(rows)) != len(rows)
            or len({row.location_key for row in rows}) != len(rows)
            or len({row.physical_key for row in rows}) != len(rows)):
        raise ValueError('来源分组与完整采集清单不一致')
    for root in discovery.roots:
        if (not root.is_absolute() or str(root) not in entries or '..' in root.parts
                or sum(root == other or root in other.parents or other in root.parents
                       for other in discovery.roots) != 1):
            raise ValueError('来源根重复或范围不明确')
    for group in discovery.groups:
        validate_group_sources(group.to_dict(), group.identities)
        if any(row.source_path not in entries or not any(Path(row.source_path) == root
                or root in Path(row.source_path).parents for root in discovery.roots) for row in group.identities):
            raise ValueError('来源组越出首次采集清单')


def discovery_task_groups(discovery):
    """Derive cards, retaining complete observations for drift/ownership checks.

    Ordinary work/video groups are not disposable based on a title word.
    Only whole supplemental/remainder groups with positive evidence are omitted.
    """
    from .media_classifier import observe_watchable_sources
    if type(discovery) is not SourceDiscovery:
        raise ValueError('缺少完整来源发现结果')
    kept = []
    for group in discovery.groups:
        root = next((root for root in discovery.roots if group.path == root or root in group.path.parents), None)
        if root is None:
            raise ValueError('来源组越出首次采集清单')
        if not group.reason or any(row.source_path == str(root) for row in group.identities):
            kept.append(group)  # Directly selected files have no containing subtree evidence.
            continue
        observations = observe_watchable_sources((row.source_path for row in group.identities), source_root=root)
        if not observations or any(row.disposition != 'known_non_watchable' for row in observations):
            kept.append(group)
    return tuple(kept)


def source_group_pending(task):
    return bool(task.get('source_group_pending') or task.get('queue_operation') == 'await_source_confirmation')


def require_source_group_confirmed(task):
    if source_group_pending(task):
        raise ValueError('请先核对完整来源组并明确继续；普通重试或作品链接不能跳过来源确认')


def validate_pending_group(task, scope):
    """Check the persisted preview against the exact admitted and current set."""
    raw = task.get('source_group_observation')
    if not source_group_pending(task) or not isinstance(raw, dict) or not raw.get('reason'):
        raise ValueError('缺少待确认来源组，请刷新任务')
    identities = tuple(SourceIdentity.from_mapping(row) for row in raw['identities'])
    if set(identities) != set(scope.source_identities) or str(task.get('path')) != raw['path']:
        raise ValueError('待确认来源组与已登记范围不一致')
    validate_group_sources(raw, identities)


def validate_group_sources(raw, identities):
    """所有来源组入口只核对已采集身份和布局。"""
    paths = {row.source_path for row in identities}
    root = Path(raw['path'])
    if (not paths or len(paths) != len(identities) or not root.is_absolute()
            or '..' in root.parts or len({row.physical_key for row in identities}) != len(paths)):
        raise ValueError('待确认来源范围无效')
    for row in identities:
        path = Path(row.source_path)
        if (not path.is_absolute() or '..' in path.parts
                or row != SourceIdentity.from_mapping(row.to_dict())
                or row.location_key != location_key_from_resolved(path)):
            raise ValueError('待确认来源身份与清单不一致')
    if raw['scope_kind'] == 'parent_directory':
        valid = raw['input_scope'] == [str(root)] and all(root in Path(p).parents for p in paths)
    else:
        valid = (raw['scope_kind'] == 'exact_file' and set(raw['input_scope']) == paths
                 and len(raw['input_scope']) == len(paths)
                 and (str(root) in paths or root == _exact_layout_root(paths)))
    if not valid:
        raise ValueError('待确认来源布局无效')
