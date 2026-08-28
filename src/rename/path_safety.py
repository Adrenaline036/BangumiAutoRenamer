"""Shared lexical Windows reparse/symlink safety checks."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Optional


_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_reparse_or_symlink(path: Path) -> bool:
    candidate = Path(path)
    try:
        if candidate.is_symlink():
            return True
        attributes = int(
            getattr(candidate.stat(follow_symlinks=False), "st_file_attributes", 0)
        )
        return bool(attributes & _REPARSE_ATTRIBUTE)
    except OSError:
        return False


def first_reparse_component(path: Path) -> Optional[Path]:
    """Inspect existing lexical components without resolving through a junction."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        return None
    current = Path(candidate.anchor)
    if current.exists() and is_reparse_or_symlink(current):
        return current
    for part in candidate.parts[1:]:
        current = current / part
        if not current.exists():
            break
        if is_reparse_or_symlink(current):
            return current
    return None


def is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False
