import json
import hashlib
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from .path import TASK_PATH, RECORD_PATH
from ..rename.file_lock import FileLockBusyError
from ..rename.review_operation import normalize_task_review_authority


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_TASK_REPLACE_MAX_ATTEMPTS = 5
_TASK_REPLACE_DEADLINE_SECONDS = 1.0
_TASK_REPLACE_INITIAL_BACKOFF_SECONDS = 0.05
_TASK_REPLACE_MAX_BACKOFF_SECONDS = 0.25
_WINDOWS_REPLACE_RETRY_ERRORS = {5, 32, 33}


def safe_task_path(root: Path, task_id: str, suffix: str = ".json") -> Path:
    """Build one exact task-scoped path without permitting traversal."""

    value = str(task_id)
    if (
        not value
        or value in {".", ".."}
        or _SAFE_TASK_ID.fullmatch(value) is None
    ):
        raise ValueError(f"Unsafe task_id: {value!r}")
    if not suffix.startswith(".") or any(
        separator in suffix for separator in ("/", "\\")
    ):
        raise ValueError(f"Unsafe task suffix: {suffix!r}")
    return Path(root) / f"{value}{suffix}"


def unpack_style(style_dict: dict):
    return '; '.join([f'{k}: {v}' for k, v in style_dict.items()])


def get_record(uuid: str) -> Optional[dict]:
    path = safe_task_path(RECORD_PATH, uuid)
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    else:
        return None


def read_task_file(path: Path):
    """只把真实缺失当作空记录；原子替换造成的共享冲突短暂重读。"""
    for attempt in range(3):
        try:
            with path.open('r', encoding='utf-8') as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return None
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(.005)
        else:
            if not isinstance(data, dict):
                raise ValueError('任务记录不是有效对象')
            return data


def get_task(uuid: str) -> dict:
    data = read_task_file(safe_task_path(TASK_PATH, uuid))
    if data is None:
        return {}
    data.setdefault('is_movie', None)
    return dict(sorted(data.items(), key=lambda row: ({'is_anime': 0, 'is_movie': 1}.get(row[0], 2), row[0])))


def write_task(task_id: str, data: dict) -> None:
    write_task_at(TASK_PATH, task_id, data)


def mutate_task(
    task_id: str,
    mutation: Callable[[dict], Optional[dict]],
) -> Optional[dict]:
    """Conditionally update a task below the current authoritative root."""

    return mutate_task_at(TASK_PATH, task_id, mutation)


def read_task_at(root: Path, task_id: str) -> dict:
    from ..rename.recognition_reset import require_ready
    require_ready(Path(root).parent, task_id)
    return read_task_file(safe_task_path(Path(root), task_id)) or {}


def task_record_snapshot_at(
    root: Path, task_id: str
) -> tuple[Optional[bytes], dict, str]:
    """Return exact bytes, parsed payload and a CAS fingerprint under the lock."""
    from ..rename.recognition_reset import require_ready
    require_ready(Path(root).parent, task_id)

    root = Path(root)
    with task_record_lock(root, task_id):
        path = safe_task_path(root, task_id)
        if not path.exists():
            return None, {}, "missing"
        raw = path.read_bytes()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("任务记录无法作为事务快照读取") from exc
        if not isinstance(value, dict) or value.get("uuid") != task_id:
            raise ValueError("任务记录事务快照与 task_id 不一致")
        return raw, value, hashlib.sha256(raw).hexdigest()


def replace_task_record_at_if_sha256(
    root: Path,
    task_id: str,
    *,
    expected_sha256: str,
    data: dict,
) -> str:
    """CAS-write one task record and return the exact persisted byte hash."""

    root = Path(root)
    with task_record_lock(root, task_id):
        path = safe_task_path(root, task_id)
        current = path.read_bytes() if path.exists() else None
        current_sha256 = (
            hashlib.sha256(current).hexdigest() if current is not None else "missing"
        )
        if current_sha256 != expected_sha256:
            raise RuntimeError("任务记录已变化，拒绝覆盖式事务提交")
        _atomic_write_task_at(root, task_id, dict(data))
        return hashlib.sha256(path.read_bytes()).hexdigest()


def task_record_serialized_sha256(data: dict) -> str:
    """Return the hash produced by the canonical task-record serializer."""

    return hashlib.sha256(_serialize_task_record(data)).hexdigest()


class TaskAcceptanceUncertain(RuntimeError):
    """The sink may own the job: retain its published Task, never blind retry."""


def publish_task_and_accept_at(root: Path, task_id: str, *, previous: bytes,
                               data: dict, accept: Callable[[], None],
                               before_publish: Callable[[], None]) -> None:
    """Hold the real record lock across exact CAS, publication and acceptance.

    All other authorities must already be locked by the caller. A failed
    atomic replacement or sink can restore only this call's exact postimage;
    an external replacement/removal is never overwritten or resurrected.
    """
    root = Path(root)
    with task_record_lock(root, task_id):
        path = safe_task_path(root, task_id)
        current = path.read_bytes() if path.exists() else None
        if current != previous:
            raise RuntimeError('任务记录已变化，请刷新后重试')
        postimage = _serialize_task_record(data)
        before_publish()
        try:
            _atomic_replace_task_bytes(path, postimage)
            accept()
        except TaskAcceptanceUncertain:
            if path.exists() and path.read_bytes() == postimage:
                _atomic_replace_task_bytes(path, _serialize_task_record({**data,
                    'queue_admission_uncertain': True, 'workflow_status': 'need_confirm',
                    'error': '入队结果不确定，已隔离作业并暂停自动重试'}))
            raise
        except BaseException:
            current = path.read_bytes() if path.exists() else None
            if current == postimage:
                _atomic_replace_task_bytes(path, previous)
            raise


def restore_task_record_at_if_sha256(
    root: Path,
    task_id: str,
    *,
    expected_current_sha256: str,
    previous: Optional[bytes],
) -> None:
    """Restore exact pre-transaction bytes only when the task is unchanged."""

    root = Path(root)
    with task_record_lock(root, task_id):
        path = safe_task_path(root, task_id)
        current = path.read_bytes() if path.exists() else None
        current_sha256 = (
            hashlib.sha256(current).hexdigest() if current is not None else "missing"
        )
        if current_sha256 != expected_current_sha256:
            raise RuntimeError("任务记录已变化，拒绝覆盖式事务恢复")
        if previous is None:
            path.unlink(missing_ok=True)
            from .task_changes import mark_changed
            mark_changed()
        else:
            _atomic_replace_task_bytes(path, previous)


def task_record_resource(root: Path, task_id: str):
    from ..rename.file_lock import LockResource
    root = Path(root)
    lock_path = root / f".{safe_task_path(root, task_id).name}.lock"
    return LockResource(None, lock_path, f'task_record:{task_id}')


@contextmanager
def task_record_lock(root: Path, task_id: str) -> Iterator[None]:
    from ..rename.file_lock import resource_guard
    try:
        with resource_guard(
            task_record_resource(root, task_id),
            owner={"kind": "task_record", "task_id": task_id},
        ):
            yield
    except FileLockBusyError as exc:
        raise RuntimeError("任务记录正在更新，请稍后重试") from exc


def write_task_at(root: Path, task_id: str, data: dict) -> None:
    """Replace a task record while preserving queue-owned operation state."""

    root = Path(root)
    with task_record_lock(root, task_id):
        previous = read_task_at(root, task_id)
        payload = normalize_task_review_authority(data, previous)
        _atomic_write_task_at(root, task_id, payload)


def merge_task_at(root: Path, task_id: str, changes: dict) -> dict:
    """Atomically merge one worker result without dropping operational fields."""

    root = Path(root)
    with task_record_lock(root, task_id):
        payload = read_task_at(root, task_id)
        if not payload:
            payload = {"uuid": task_id}
        payload.update(dict(changes))
        _atomic_write_task_at(root, task_id, payload)
        return payload


def mutate_task_at(
    root: Path,
    task_id: str,
    mutation: Callable[[dict], Optional[dict]],
) -> Optional[dict]:
    """Apply a conditional read-modify-write under the task record lock."""

    root = Path(root)
    with task_record_lock(root, task_id):
        current = read_task_at(root, task_id)
        if not current:
            return None
        revised = mutation(dict(current))
        if revised is None:
            return None
        _atomic_write_task_at(root, task_id, revised)
        return revised


def restore_task_record_at(
    root: Path,
    task_id: str,
    previous: Optional[bytes],
) -> None:
    """Restore the exact pre-admission bytes, or remove a newly created task."""

    root = Path(root)
    with task_record_lock(root, task_id):
        path = safe_task_path(root, task_id)
        if previous is None:
            path.unlink(missing_ok=True)
            from .task_changes import mark_changed
            mark_changed()
            return
        _atomic_replace_task_bytes(path, previous)


def restore_task_record_if_unchanged_at(root: Path, task_id: str,
                                      expected: dict, previous: bytes) -> bool:
    """Undo only this admission, retaining exact bytes and any later writer."""
    root = Path(root)
    with task_record_lock(root, task_id):
        if read_task_at(root, task_id) != expected:
            return False
        _atomic_replace_task_bytes(safe_task_path(root, task_id), previous)
        return True


def _atomic_write_task_at(root: Path, task_id: str, data: dict) -> None:
    from ..rename.recognition_reset import require_ready
    require_ready(Path(root).parent, task_id)
    if data.get("uuid") != task_id:
        raise ValueError("任务记录 UUID 与目标文件名不一致")
    path = safe_task_path(Path(root), task_id)
    serialized = _serialize_task_record(data)
    _atomic_replace_task_bytes(path, serialized)


def _serialize_task_record(data: dict) -> bytes:
    return json.dumps(
        data,
        ensure_ascii=False,
        indent=4,
    ).encode("utf-8")


def _atomic_replace_task_bytes(path: Path, serialized: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f'.{path.name}.{uuid.uuid4().hex}.tmp'
    try:
        with open(temporary, 'xb') as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        _replace_task_with_retry(temporary, path)
        from .task_changes import mark_changed
        mark_changed()
    finally:
        temporary.unlink(missing_ok=True)


def _replace_task_with_retry(temporary: Path, target: Path) -> None:
    started = time.monotonic()
    for attempt in range(1, _TASK_REPLACE_MAX_ATTEMPTS + 1):
        try:
            os.replace(temporary, target)
            return
        except PermissionError as exc:
            winerror = getattr(exc, "winerror", None)
            fallback_errno = getattr(exc, "errno", None)
            retryable = os.name == "nt" and (
                winerror in _WINDOWS_REPLACE_RETRY_ERRORS
                or (
                    winerror is None
                    and fallback_errno in {13, 16}
                )
            )
            elapsed = time.monotonic() - started
            if (
                not retryable
                or attempt >= _TASK_REPLACE_MAX_ATTEMPTS
                or elapsed >= _TASK_REPLACE_DEADLINE_SECONDS
            ):
                raise
            delay = min(
                _TASK_REPLACE_INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                _TASK_REPLACE_MAX_BACKOFF_SECONDS,
                _TASK_REPLACE_DEADLINE_SECONDS - elapsed,
            )
            if delay <= 0:
                raise
            time.sleep(delay)


no_scroll_bar = '''
<style>
body {
    scrollbar-width: thin;
    scrollbar-color: #c8ced9 transparent;
}
html, body {
    max-width: 100%;
    overflow-x: hidden;
    overflow-y: auto;
}
</style>
'''
