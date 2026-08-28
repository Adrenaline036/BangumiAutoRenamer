"""Canonical filesystem identity primitives shared by admission and execution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class PhysicalIdentityUnavailable(ValueError):
    """The filesystem did not provide a trustworthy physical file identity."""


def canonical_location_key(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=True))).casefold()


def canonical_physical_key(stat_result: Any) -> str:
    device = int(stat_result.st_dev)
    inode = int(stat_result.st_ino)
    if device < 0 or inode <= 0:
        raise PhysicalIdentityUnavailable("文件物理身份/inode不可用")
    return f"device:{device}:inode:{inode}"
