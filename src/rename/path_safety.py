"""Shared lexical Windows reparse/symlink safety checks."""

from __future__ import annotations

import stat
from pathlib import Path
from types import MappingProxyType
from typing import Optional


_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_reparse_or_symlink(path: Path) -> bool:
    candidate = Path(path)
    try:
        result = candidate.lstat()
        return _is_link(result)
    except OSError:
        return False


def _is_link(result) -> bool:
    return stat.S_ISLNK(result.st_mode) or bool(
        int(getattr(result, 'st_file_attributes', 0)) & _REPARSE_ATTRIBUTE)


def first_reparse_component(path: Path) -> Optional[Path]:
    """Inspect existing lexical components without resolving through a junction."""

    candidate = Path(path).expanduser()
    return _inspect_components(candidate)[0]


def _inspect_components(candidate: Path):
    """Return the first link and the leaf stat from this fresh lexical walk."""
    if not candidate.is_absolute():
        return None, None
    current = Path(candidate.anchor)
    for part in ('', *candidate.parts[1:]):
        if part:
            current = current / part
        try:
            result = current.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return None, None
        if _is_link(result):
            return current, None
    return None, result


def observe_regular_file(path: Path):
    """Check every component afresh and reuse its already-read regular leaf."""
    candidate = Path(path)
    if not candidate.is_absolute() or '..' in candidate.parts:
        raise ValueError('来源必须为无上级跳转的绝对路径')
    reparse, result = _inspect_components(candidate)
    if reparse is not None:
        raise ValueError('来源不接受链接或重解析点')
    if result is None or not stat.S_ISREG(result.st_mode):
        raise ValueError('来源不存在或不是普通文件')
    return result


def _directory_identity(result):
    if _is_link(result) or not stat.S_ISDIR(result.st_mode):
        raise ValueError('来源父目录不接受链接、重解析点或非目录')
    return result.st_dev, result.st_ino, stat.S_IFMT(result.st_mode)


def observe_regular_files(paths):
    """Share parent checks within this call only; return fresh regular leaves.

    Recheck each parent before returning, so replacement during the batch is
    rejected. Directory mtime is irrelevant (unrelated siblings may change).
    This is metadata observation, not a persistent cache or commit authority.
    """
    candidates = tuple(Path(path) for path in paths)
    if any(not path.is_absolute() or '..' in path.parts for path in candidates):
        raise ValueError('来源必须为无上级跳转的绝对路径')
    parents, leaves = {}, {}
    for path in candidates:
        if path in leaves:
            continue
        for parent in reversed(path.parents):
            if parent not in parents:
                parents[parent] = _directory_identity(parent.lstat())
        result = path.lstat()
        if _is_link(result) or not stat.S_ISREG(result.st_mode):
            raise ValueError('来源不存在、不是普通文件或为链接/重解析点')
        leaves[path] = result
    for parent, identity in parents.items():
        if _directory_identity(parent.lstat()) != identity:
            raise ValueError('来源观察期间父目录已变化')
    return MappingProxyType(leaves)


def is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False
