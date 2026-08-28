"""Crash-safe inter-process file locks with diagnostic owner metadata.

The lock file is intentionally persistent. Ownership is held by the operating
system advisory lock, not by file existence, so a killed process cannot leave a
permanent lock behind. This also recovers legacy ``O_CREAT | O_EXCL`` lock
files without guessing whether a long-running task is stale from its age.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Optional


class FileLockBusyError(RuntimeError):
    """Raised when another live process owns the advisory lock."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _acquire(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _write_metadata(handle, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    handle.seek(1)
    handle.truncate()
    handle.write(encoded)
    handle.flush()
    os.fsync(handle.fileno())


def read_lock_metadata(path: Path) -> Optional[dict]:
    """Return diagnostics; never use lock-file contents as ownership proof."""

    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw[1:].decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


@contextmanager
def advisory_file_lock(
    path: Path,
    *,
    owner: Optional[Mapping[str, object]] = None,
) -> Iterator[dict]:
    """Acquire a non-blocking cross-process lock with owner diagnostics.

    Existing files, including stale legacy exclusive-create locks, do not block
    acquisition. Only a live kernel lock does.
    """

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _acquire(handle.fileno())
        except OSError as exc:
            raise FileLockBusyError("lock is owned by another live process") from exc
        acquired = True
        payload = {
            "schema_version": 1,
            "status": "acquired",
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "lock_token": uuid.uuid4().hex,
            "acquired_at": _utc_now(),
            **dict(owner or {}),
        }
        _write_metadata(handle, payload)
        yield payload
        released = dict(payload)
        released.update({"status": "released", "released_at": _utc_now()})
        _write_metadata(handle, released)
    finally:
        if acquired:
            _release(handle.fileno())
        handle.close()
