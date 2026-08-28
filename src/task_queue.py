"""Observable, single-worker task queue for UI and webhook submissions."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, Mapping, Optional

from nicegui import run

from .logger import logger
from .rename.plan_execution import PlanExecutionError, PlanExecutionService
from .rename.physical_identity import (
    PhysicalIdentityUnavailable,
    canonical_location_key,
    canonical_physical_key,
)
from .rename.path_safety import first_reparse_component, is_reparse_or_symlink
from .rename.decision_state import (
    DecisionStateError,
    DecisionStateStore,
)
from .rename.plan_review import PlanReviewService
from .rename.plan_store import PlanStore, PlanStoreError
from .rename.projection_authority import ProjectionCommitService
from .rename.task_batch_scope import (
    BatchTaskSourceRequest,
    SourceIdentity as BatchSourceIdentity,
    TaskBatchScopeManifestStore,
    TaskSourceScope,
)
from .rename.process import Rename
from .rename.utils import is_ignored_asset_directory
from .rename.provider_link_decision import (
    build_provider_subject_boundary_failure,
    build_provider_subject_cohort_preview,
    provider_subject_cohort_request_fingerprint,
)
from .utils.path import (
    DECISION_STATE_PATH,
    PLAN_PATH,
    PLAN_REVIEW_PATH,
    PROVIDER_LINK_CATALOG_PATH,
    RECORD_PATH,
    RECOVERY_ABANDON_AUDIT_PATH,
    RECOVERY_TRANSACTION_PATH,
    SERIES_CATALOG_PATH,
    TASK_PATH,
    WORK_CATALOG_PATH,
)
from .utils.utils import (
    get_task,
    mutate_task_at,
    read_task_at,
    restore_task_record_at,
    safe_task_path,
    task_record_lock,
    write_task,
    write_task_at,
)


ACTIVE_STATUSES = {"queued", "running", "analyzing", "executing"}
COMPLETED_STATUSES = {
    "completed",
    "failed",
    "invalid",
    "cancelled",
    "interrupted",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_ui_test_mode() -> bool:
    return os.getenv("BAR_UI_TEST_MODE", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class TaskJob:
    task_id: str
    path: Optional[Path]
    is_anime: Optional[bool] = None
    is_movie: Optional[bool] = None
    custom_name: Optional[str] = None
    custom_season_id: Optional[int] = None
    use_ai: Optional[bool] = None
    provider_selection: Optional[Dict[str, object]] = None
    source: str = "ui"
    preview_only: bool = False
    operation: str = "rename"
    expected_fingerprint: Optional[str] = None
    expected_revision: Optional[int] = None
    request_id: Optional[str] = None
    task_source_scope: Optional[TaskSourceScope] = None


Executor = Callable[[TaskJob], Awaitable[object]]


class TaskQueueManager:
    """Persist queue state before work starts and execute one job at a time."""

    def __init__(
        self,
        *,
        task_root: Path = TASK_PATH,
        executor: Optional[Executor] = None,
        approved_executor: Optional[Executor] = None,
        execution_service: Optional[PlanExecutionService] = None,
        decision_state_root: Path = DECISION_STATE_PATH,
        recovery_abandon_audit_root: Path = RECOVERY_ABANDON_AUDIT_PATH,
        plan_root: Path = PLAN_PATH,
        batch_scope_root: Optional[Path] = None,
        test_mode: Optional[bool] = None,
    ) -> None:
        self.task_root = Path(task_root)
        self.executor = executor or self._execute_rename
        effective_plan_root = Path(plan_root)
        isolated = (
            self.task_root.resolve(strict=False)
            != Path(TASK_PATH).resolve(strict=False)
            or effective_plan_root.resolve(strict=False)
            != Path(PLAN_PATH).resolve(strict=False)
        )
        execution_plan_store = PlanStore(effective_plan_root)
        execution_review_root = (
            effective_plan_root.parent / "plan_review"
            if isolated else Path(PLAN_REVIEW_PATH)
        )
        execution_record_root = (
            self.task_root.parent / "record" if isolated else Path(RECORD_PATH)
        )
        self.execution_service = execution_service or PlanExecutionService(
            execution_plan_store,
            PlanReviewService(execution_plan_store, execution_review_root),
            task_root=self.task_root,
            record_root=execution_record_root,
            catalog_root=(
                effective_plan_root.parent / "work_catalog"
                if isolated else WORK_CATALOG_PATH
            ),
            series_catalog_root=(
                effective_plan_root.parent / "series_catalog"
                if isolated else SERIES_CATALOG_PATH
            ),
            decision_state_root=Path(decision_state_root),
            recovery_transaction_root=(
                effective_plan_root.parent / "recovery_transaction"
                if isolated else RECOVERY_TRANSACTION_PATH
            ),
            provider_link_catalog_root=(
                effective_plan_root.parent / "provider_link_catalog"
                if isolated else PROVIDER_LINK_CATALOG_PATH
            ),
        )
        self.approved_executor = approved_executor or self._execute_approved
        self.decision_state_store = DecisionStateStore(decision_state_root)
        self.plan_store = PlanStore(plan_root)
        self.projection_commit_service = ProjectionCommitService(
            self.decision_state_store,
            self.plan_store,
            task_root=self.task_root,
        )
        self.batch_scope_store = TaskBatchScopeManifestStore(
            Path(batch_scope_root or (self.task_root.parent / "task_batch_scope"))
        )
        self.recovery_abandon_audit_root = Path(recovery_abandon_audit_root)
        self.test_mode = is_ui_test_mode() if test_mode is None else test_mode
        self._queue: asyncio.Queue[TaskJob] = asyncio.Queue()
        self._pending: list[str] = []
        self._worker_task: Optional[asyncio.Task] = None
        self._started = False
        self.active_task_id: Optional[str] = None

    @staticmethod
    def _input_source_files(path: Path) -> tuple[Path, ...]:
        candidate = Path(path)
        reparse = first_reparse_component(candidate)
        if reparse is not None:
            raise PlanExecutionError(
                f"任务来源包含junction/reparse重解析点：{reparse}"
            )
        resolved = candidate.resolve(strict=True)
        if resolved.is_file():
            return (resolved,)
        if not resolved.is_dir():
            raise PlanExecutionError("任务来源不是文件或目录")
        files: list[Path] = []
        pending = [resolved]
        while pending:
            directory = pending.pop()
            for child in sorted(
                directory.iterdir(), key=lambda item: str(item).casefold()
            ):
                if is_reparse_or_symlink(child):
                    continue
                if child.is_dir():
                    if not is_ignored_asset_directory(child):
                        pending.append(child)
                elif child.is_file():
                    files.append(child.resolve(strict=True))
        return tuple(sorted(files, key=lambda item: str(item).casefold()))

    @staticmethod
    def _batch_source_identity(path: Path) -> BatchSourceIdentity:
        resolved = Path(path).resolve(strict=True)
        stat_result = resolved.stat()
        try:
            physical_key = canonical_physical_key(stat_result)
        except PhysicalIdentityUnavailable as exc:
            raise PlanExecutionError("任务来源缺少可冻结的物理身份") from exc
        return BatchSourceIdentity(
            source_path=str(resolved),
            location_key=canonical_location_key(resolved),
            physical_key=physical_key,
            size=int(stat_result.st_size),
            mtime_ns=int(stat_result.st_mtime_ns),
        )

    def prepare_input_batch(
        self,
        paths: tuple[Path, ...],
        *,
        is_anime: Optional[bool],
        is_movie: Optional[bool],
        use_ai: Optional[bool],
        source: str,
        preview_only: bool,
    ) -> dict[str, object]:
        """Collect immutable input observations before the admission barrier."""

        if not paths:
            raise PlanExecutionError("批次至少需要一个来源")
        normalized: list[Path] = []
        seen = set()
        for path in paths:
            candidate = Path(path)
            key = str(candidate.resolve(strict=False)).casefold()
            if key in seen:
                raise PlanExecutionError("批次来源路径重复")
            seen.add(key)
            normalized.append(candidate)
        batch_id = f"ui-batch-{uuid.uuid4().hex}"
        media_domain = "movie" if is_movie is True else "tv"
        requests = []
        jobs = []
        preview = bool(preview_only or self.test_mode)
        for path in normalized:
            task_id = str(uuid.uuid4())
            try:
                resolved = path.resolve(strict=True)
                identities = tuple(
                    self._batch_source_identity(item)
                    for item in self._input_source_files(resolved)
                )
                scope_kind = "exact_file" if resolved.is_file() else "parent_directory"
                input_scope = (str(resolved),)
                job_path = resolved
            except OSError:
                if not preview:
                    raise PlanExecutionError(f"任务来源不存在：{path}")
                identities = ()
                scope_kind = "exact_file"
                input_scope = (str(path.resolve(strict=False)),)
                job_path = path
            requests.append(BatchTaskSourceRequest(
                task_id=task_id,
                input_scope=input_scope,
                scope_kind=scope_kind,
                source_identities=identities,
                media_domain=media_domain,
            ))
            jobs.append(TaskJob(
                task_id=task_id,
                path=job_path,
                is_anime=is_anime,
                is_movie=is_movie,
                use_ai=use_ai,
                source=source,
                preview_only=preview,
            ))
        return {
            "batch_id": batch_id,
            "task_requests": tuple(requests),
            "jobs": tuple(jobs),
        }

    async def start(self) -> None:
        if self._started:
            return
        self.task_root.mkdir(parents=True, exist_ok=True)
        self.projection_commit_service.recover_pending()
        self.batch_scope_store.recover_all_pending()
        self._recover_active_records()
        self._started = True
        if not self.test_mode:
            self._worker_task = asyncio.create_task(
                self._worker(), name="bar-task-queue"
            )

    async def stop(self) -> None:
        self._started = False
        if self._worker_task is not None:
            self._worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None

    async def enqueue(
        self,
        path: Path,
        *,
        is_anime: Optional[bool] = None,
        is_movie: Optional[bool] = None,
        task_id: Optional[str] = None,
        custom_name: Optional[str] = None,
        custom_season_id: Optional[int] = None,
        use_ai: Optional[bool] = None,
        provider_selection: Optional[Dict[str, object]] = None,
        source: str = "ui",
        preview_only: bool = False,
    ) -> str:
        if not self._started:
            await self.start()
        identifier = task_id or str(uuid.uuid4())
        task_path = safe_task_path(self.task_root, identifier)
        if task_id is not None and self._has_abandon_audit(identifier):
            raise PlanExecutionError("该任务已经移除，请新建任务后再试")
        preview = bool(preview_only or self.test_mode)
        job = TaskJob(
            task_id=identifier,
            path=Path(path),
            is_anime=is_anime,
            is_movie=is_movie,
            custom_name=custom_name,
            custom_season_id=custom_season_id,
            use_ai=use_ai,
            provider_selection=(dict(provider_selection) if provider_selection else None),
            source=source,
            preview_only=preview,
        )
        previous = task_path.read_bytes() if task_path.is_file() else None
        payload = {
                "path": str(job.path),
                "is_anime": is_anime,
                "is_movie": is_movie,
                "name": custom_name or "等待识别",
                "season_id": custom_season_id,
                "season_number": custom_season_id,
                "season_source": (
                    "explicit_user" if custom_season_id is not None else "unset"
                ),
                "uuid": identifier,
                "error": None,
                "use_ai": use_ai,
                "provider_selection": job.provider_selection,
                "workflow_status": "queued",
                "queue_position": len(self._pending) + 1,
                "created_at": utc_now(),
                "started_at": None,
                "completed_at": None,
                "task_source": source,
                "preview_only": preview,
            }
        self._write(identifier, payload)
        self._pending.append(identifier)
        try:
            self._refresh_queue_positions()
            if not preview:
                await self._queue.put(job)
        except BaseException:
            if identifier in self._pending:
                self._pending.remove(identifier)
            restore_task_record_at(self.task_root, identifier, previous)
            raise
        logger.info(
            f"[任务队列] 已入队 {identifier}；position={len(self._pending)}；"
            f"preview_only={preview}"
        )
        return identifier

    async def enqueue_batch(
        self,
        *,
        batch_id: str,
        task_requests: tuple[BatchTaskSourceRequest, ...],
        jobs: tuple[TaskJob, ...],
    ) -> tuple[str, ...]:
        """Freeze one batch and all source reservations before any worker sees it."""

        if not self._started:
            await self.start()
        requested_ids = {item.task_id for item in task_requests}
        job_ids = {item.task_id for item in jobs}
        if not jobs or requested_ids != job_ids or len(job_ids) != len(jobs):
            raise PlanExecutionError("批次任务与source scope不一致")

        # This is the admission barrier: reservations are frozen first, task
        # records become durable second, then the manifest becomes ``active``
        # before the first worker-visible queue item.
        manifest = self.batch_scope_store.reserve_batch(
            batch_id=batch_id,
            task_requests=task_requests,
        )
        if manifest.state != "reservations_written":
            raise PlanExecutionError("批次source reservation未进入待入队状态")
        scopes = {item.task_id: item for item in manifest.task_source_scopes}
        scoped_jobs = tuple(
            replace(job, task_source_scope=scopes[job.task_id]) for job in jobs
        )
        previous_records: dict[str, Optional[bytes]] = {}
        staged_ids: list[str] = []
        try:
            for job in scoped_jobs:
                scope = scopes[job.task_id]
                path = safe_task_path(self.task_root, job.task_id)
                previous_records[job.task_id] = (
                    path.read_bytes() if path.is_file() else None
                )
                payload = self._read(job.task_id) or {
                    "uuid": job.task_id,
                    "path": str(job.path or ""),
                    "created_at": utc_now(),
                }
                payload.update({
                    "path": str(job.path or ""),
                    "is_anime": job.is_anime,
                    "is_movie": job.is_movie,
                    "name": job.custom_name or "等待识别",
                    "season_id": job.custom_season_id,
                    "season_number": job.custom_season_id,
                    "season_source": (
                        "explicit_user"
                        if job.custom_season_id is not None else "unset"
                    ),
                    "use_ai": job.use_ai,
                    "provider_selection": job.provider_selection,
                    "task_source": job.source,
                    "preview_only": job.preview_only,
                    "workflow_status": "queued",
                    "queue_position": len(self._pending) + len(staged_ids) + 1,
                    "batch_id": manifest.batch_id,
                    "batch_scope_fingerprint": scope.batch_scope_fingerprint,
                    "reservation_lineage_id": scope.reservation_lineage_id,
                    "reservation_revision": scope.reservation_revision,
                    "task_source_scope": scope.to_dict(),
                    "error": None,
                })
                self._write(job.task_id, payload)
                self._pending.append(job.task_id)
                staged_ids.append(job.task_id)
            manifest = self.batch_scope_store.mark_tasks_enqueued(
                manifest.batch_id,
                expected_revision=manifest.revision,
                task_ids=tuple(job.task_id for job in scoped_jobs),
            )
            manifest = self.batch_scope_store.activate_batch(
                manifest.batch_id,
                expected_revision=manifest.revision,
            )
            if manifest.state != "active":
                raise PlanExecutionError("批次source reservation尚未active")
            self._refresh_queue_positions()
            for job in scoped_jobs:
                if not job.preview_only:
                    # asyncio.Queue is unbounded here.  A synchronous admission
                    # prevents cancellation between jobs from exposing a
                    # partially queued batch after records were restored.
                    self._queue.put_nowait(job)
        except BaseException:
            for task_id in staged_ids:
                if task_id in self._pending:
                    self._pending.remove(task_id)
                restore_task_record_at(
                    self.task_root, task_id, previous_records[task_id]
                )
            self.batch_scope_store.fail_closed(batch_id)
            raise
        return tuple(job.task_id for job in scoped_jobs)

    async def enqueue_legacy_replan(
        self,
        job: TaskJob,
        *,
        source_identities: tuple[BatchSourceIdentity, ...],
        input_scope: tuple[str, ...],
        scope_kind: str = "parent_directory",
        media_domain: str = "tv",
    ) -> str:
        """Move one legacy replan into a fresh current-schema minimal batch."""

        batch_id = f"single_task_minimal_batch-{uuid.uuid4().hex}"
        request = BatchTaskSourceRequest(
            task_id=job.task_id,
            input_scope=input_scope,
            scope_kind=scope_kind,
            source_identities=source_identities,
            media_domain=media_domain,
        )
        result = await self.enqueue_batch(
            batch_id=batch_id,
            task_requests=(request,),
            jobs=(job,),
        )
        return result[0]

    async def retry_current(self, task_id: str, *, source: str = "retry") -> str:
        """Requeue one task without changing its current admission lineage."""

        if not self._started:
            await self.start()
        data = self._read(task_id)
        scope_value = data.get("task_source_scope")
        if not isinstance(scope_value, Mapping):
            raise PlanExecutionError("任务缺少current source scope，请重新创建任务")
        scope = TaskSourceScope.from_mapping(scope_value)
        batch_id = str(data.get("batch_id") or "").strip()
        if not batch_id:
            raise PlanExecutionError("任务缺少current batch identity，请重新创建任务")
        try:
            self.batch_scope_store.require_active_scope(scope, batch_id=batch_id)
        except Exception as exc:
            raise PlanExecutionError("任务source reservation已失效，请重新创建任务") from exc
        if task_id == self.active_task_id:
            raise PlanExecutionError("任务已经在执行中")
        if task_id in self._pending:
            data.update({
                "workflow_status": "queued",
                "task_source": source,
                "error": None,
            })
            self._write(task_id, data)
            return task_id
        job = TaskJob(
            task_id=task_id,
            path=Path(str(data.get("path") or "")),
            is_anime=data.get("is_anime"),
            is_movie=data.get("is_movie"),
            custom_name=str(data.get("name") or "").strip() or None,
            custom_season_id=data.get("season_id"),
            use_ai=data.get("use_ai"),
            provider_selection=(
                dict(data["provider_selection"])
                if isinstance(data.get("provider_selection"), Mapping)
                else None
            ),
            source=source,
            preview_only=bool(data.get("preview_only")),
            task_source_scope=scope,
        )
        data.update({
            "workflow_status": "queued",
            "queue_position": len(self._pending) + 1,
            "task_source": source,
            "error": None,
        })
        self._write(task_id, data)
        self._pending.append(task_id)
        self._refresh_queue_positions()
        if not job.preview_only:
            self._queue.put_nowait(job)
        return task_id

    async def reenqueue_workspace(
        self,
        task_id: str,
        path: Path,
        *,
        is_anime: Optional[bool],
        is_movie: Optional[bool],
        custom_name: Optional[str],
        custom_season_id: Optional[int],
        use_ai: Optional[bool],
        preview_only: bool,
    ) -> str:
        """Fail closed until a stale workspace task is explicitly readmitted."""

        current = self._read(task_id)
        current_path = Path(str(current.get("path") or "")).resolve(strict=False)
        requested_path = Path(path).resolve(strict=False)
        if current_path != requested_path:
            raise PlanExecutionError(
                "来源路径已改变；请安全放弃旧任务后创建新的整理任务"
            )
        # An unchanged workspace correction is a retry of the same admission,
        # not a second reservation writer.
        current.update({
            "is_anime": is_anime,
            "is_movie": is_movie,
            "name": custom_name or "等待识别",
            "season_id": custom_season_id,
            "season_number": custom_season_id,
            "season_source": (
                "explicit_user" if custom_season_id is not None else "unset"
            ),
            "use_ai": use_ai,
            "preview_only": bool(preview_only),
        })
        self._write(task_id, current)
        return await self.retry_current(task_id, source="task_workspace")

    async def enqueue_approved(
        self, task_id: str, expected_fingerprint: str
    ) -> str:
        """Queue one exact approved Plan version for the single worker."""

        if self.test_mode:
            raise PlanExecutionError("GUI 测试模式禁止执行已批准整理计划")
        if not self._started:
            await self.start()
        if task_id == self.active_task_id or task_id in self._pending:
            raise PlanExecutionError("该任务已在队列或正在执行")
        current = self._read(task_id)
        if not current:
            raise PlanExecutionError("任务记录不存在，拒绝入队")
        if current.get("preview_only"):
            raise PlanExecutionError("测试预览任务禁止执行")
        status = str(current.get("workflow_status") or "")
        safe_pre_media_retry = bool(
            status == "failed"
            and current.get("queue_operation") == "execute_approved"
            and current.get("execution_phase") == "failed_before_media"
            and not current.get("execution_requires_manual_check")
            and current.get("execution_requested_fingerprint")
            == expected_fingerprint
        )
        if status != "approved" and not safe_pre_media_retry:
            raise PlanExecutionError("任务状态不是已批准，拒绝入队")
        self.execution_service.validate_approved(task_id, expected_fingerprint)

        job = TaskJob(
            task_id=task_id,
            path=None,
            source="approved_plan",
            operation="execute_approved",
            expected_fingerprint=expected_fingerprint,
        )
        task_path = safe_task_path(self.task_root, task_id)
        previous = task_path.read_bytes()
        current.update(
            {
                "workflow_status": "queued",
                "queue_position": len(self._pending) + 1,
                "queue_operation": "execute_approved",
                "execution_requested_fingerprint": expected_fingerprint,
                "execution_requested_at": utc_now(),
                "execution_phase": "admitted",
                "execution_requires_manual_check": False,
                "execution_may_have_written_targets": False,
                "completed_at": None,
                "error": None,
            }
        )
        self._write(task_id, current)
        self._pending.append(task_id)
        try:
            self._refresh_queue_positions()
            await self._queue.put(job)
        except BaseException:
            if task_id in self._pending:
                self._pending.remove(task_id)
            restore_task_record_at(self.task_root, task_id, previous)
            raise
        logger.info(
            f"[任务队列] 已批准整理计划入队 {task_id}；"
            f"fingerprint={expected_fingerprint[:12]}"
        )
        return task_id

    async def enqueue_reidentify(
        self,
        task_id: str,
        provider_selection: Dict[str, object],
        expected_revision: int,
    ) -> str:
        """Queue one version-bound provider selection through the real worker."""

        if self.test_mode:
            raise PlanExecutionError("界面测试模式不会运行重新识别")
        if not self._started:
            await self.start()
        if task_id == self.active_task_id or task_id in self._pending:
            raise PlanExecutionError("该任务正在处理中，请稍后再试")
        current = self._read(task_id)
        if not current:
            raise PlanExecutionError("任务记录不存在")
        if current.get("execution_requires_manual_check"):
            raise PlanExecutionError("请先处理当前恢复事项")
        if current.get("preview_only"):
            raise PlanExecutionError("预览任务不能重新识别")
        try:
            decision = self.decision_state_store.load(task_id)
        except DecisionStateError as exc:
            raise PlanExecutionError("任务内容不可用，请刷新后重试") from exc
        if decision.revision != int(expected_revision):
            raise PlanExecutionError("内容已更新，请刷新后继续")
        if not isinstance(provider_selection, dict):
            raise PlanExecutionError("所选作品信息无效")
        selection = {
            key: provider_selection.get(key)
            for key in (
                "namespace", "entity_type", "provider_id", "title",
                "original_title", "year", "poster_url", "source_url",
                "snapshot_at", "library_domain",
            )
        }
        if not all(selection.get(key) for key in (
            "namespace", "entity_type", "provider_id",
        )):
            raise PlanExecutionError("所选作品信息不完整")
        raw_path = str(current.get("path") or "").strip()
        try:
            if not raw_path:
                raise ValueError
            path = Path(raw_path)
        except (TypeError, ValueError) as exc:
            raise PlanExecutionError("任务来源路径无效") from exc
        request_id = uuid.uuid4().hex
        stored_scope = current.get("task_source_scope")
        task_source_scope = (
            TaskSourceScope.from_mapping(stored_scope)
            if isinstance(stored_scope, Mapping) else None
        )
        job = TaskJob(
            task_id=task_id,
            path=path,
            is_anime=current.get("is_anime"),
            is_movie=current.get("is_movie"),
            custom_name=None,
            custom_season_id=current.get("season_id"),
            use_ai=current.get("use_ai"),
            provider_selection=selection,
            source="provider_selection",
            operation="reidentify",
            expected_revision=int(expected_revision),
            request_id=request_id,
            task_source_scope=task_source_scope,
        )
        requested_at = utc_now()

        def stage(data: dict) -> dict:
            previous_status = str(data.get("workflow_status") or "need_confirm")
            if data.get("execution_requires_manual_check"):
                raise PlanExecutionError("请先处理当前恢复事项")
            if data.get("preview_only"):
                raise PlanExecutionError("预览任务不能重新识别")
            operation = data.get("selection_operation")
            if isinstance(operation, dict) and operation.get("state") == "pending":
                raise PlanExecutionError("该任务正在处理中，请稍后再试")
            data.update({
                "workflow_status": "queued",
                "queue_operation": "reidentify",
                "queue_position": len(self._pending) + 1,
                "reidentify_expected_revision": int(expected_revision),
                "reidentify_requested_at": requested_at,
                "started_at": None,
                "completed_at": None,
                "error": None,
                "selection_operation": {
                    "schema_version": 1,
                    "request_id": request_id,
                    "state": "pending",
                    "candidate": selection,
                    "expected_decision_revision": int(expected_revision),
                    "result_decision_revision": None,
                    "failure_code": None,
                    "reason_codes": [],
                    "retryable": True,
                    "requested_at": requested_at,
                    "finished_at": None,
                    "previous_workflow_status": previous_status,
                },
            })
            return data

        if mutate_task_at(self.task_root, task_id, stage) is None:
            raise PlanExecutionError("任务记录不存在")
        self._pending.append(task_id)
        await self._queue.put(job)
        self._refresh_queue_positions()
        return task_id

    def cancel_queued(self, task_id: str) -> bool:
        """Cancel a job that has not started; the worker will skip its stale item."""
        if task_id == self.active_task_id or task_id not in self._pending:
            return False
        try:
            self._retire_task_authorities(task_id, reason="queue-cancelled")
        except (PlanExecutionError, DecisionStateError):
            return False
        self._pending.remove(task_id)
        current = self._read(task_id)
        if current:
            current.update(
                {
                    "workflow_status": "cancelled",
                    "queue_position": None,
                    "completed_at": utc_now(),
                }
            )
            self._write(task_id, current)
        self._refresh_queue_positions()
        return True

    def _retire_task_authorities(self, task_id: str, *, reason: str) -> None:
        data = self._read(task_id)
        if not data:
            raise PlanExecutionError("任务记录不存在")
        scope_value = data.get("task_source_scope")
        batch_id = str(data.get("batch_id") or "").strip()
        if isinstance(scope_value, Mapping) and batch_id:
            scope = TaskSourceScope.from_mapping(scope_value)
            try:
                self.batch_scope_store.retire_task(
                    batch_id,
                    task_id=task_id,
                    expected_lineage_id=scope.reservation_lineage_id,
                )
            except Exception as exc:
                raise PlanExecutionError("任务source reservation无法安全释放") from exc
        ownership = self.projection_commit_service.ownership_store.load_optional(task_id)
        if ownership is not None and ownership.state == "active":
            try:
                self.projection_commit_service.ownership_store.retire(
                    task_id=task_id,
                    expected_revision=ownership.revision,
                    reason=reason,
                )
            except Exception as exc:
                raise PlanExecutionError("任务source ownership无法安全释放") from exc

    def abandon_and_remove(
        self,
        task_id: str,
        *,
        record_root: Path,
        plan_root: Path,
        review_root: Path,
    ) -> None:
        """Close current admission and remove mutable UI state, never the ledger."""

        if task_id == self.active_task_id:
            raise PlanExecutionError("任务正在执行，不能安全移除")
        if task_id in self._pending:
            if not self.cancel_queued(task_id):
                raise PlanExecutionError("任务排队状态已变化，不能安全移除")
        else:
            self._retire_task_authorities(task_id, reason="user-safe-abandon")
        with task_record_lock(self.task_root, task_id):
            safe_task_path(self.task_root, task_id).unlink(missing_ok=True)
        safe_task_path(Path(record_root), task_id).unlink(missing_ok=True)
        plan_store = PlanStore(Path(plan_root))
        try:
            plan_store.delete(task_id)
            PlanReviewService(plan_store, Path(review_root)).delete(task_id)
        except PlanStoreError:
            logger.warning(f"[任务] 拒绝删除非法 UUID 对应的整理计划：{task_id}")

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            executor_started = False
            try:
                current = self._read(job.task_id)
                if not current or current.get("workflow_status") == "cancelled":
                    continue
                if job.task_id in self._pending:
                    self._pending.remove(job.task_id)
                self.active_task_id = job.task_id
                self._refresh_queue_positions()
                if not self._update(
                    job.task_id,
                    workflow_status="running",
                    queue_position=0,
                    started_at=utc_now(),
                    error=None,
                ):
                    continue
                if job.operation == "reidentify":
                    self._validate_reidentify_revision(job)
                if (
                    job.task_source_scope is not None
                    and job.task_source_scope.task_id != job.task_id
                ):
                    raise PlanExecutionError("worker task_source_scope归属不一致")
                executor = (
                    self.approved_executor
                    if job.operation == "execute_approved"
                    else self.executor
                )
                executor_started = True
                await executor(job)
                if job.operation == "reidentify":
                    self._complete_reidentify(job)
                current = self._read(job.task_id)
                status = current.get("workflow_status")
                if status in {"queued", "running", "analyzing", "executing"}:
                    self._update(
                        job.task_id,
                        workflow_status="completed",
                        completed_at=utc_now(),
                    )
                elif status in COMPLETED_STATUSES:
                    self._update(job.task_id, completed_at=utc_now())
                elif status == "need_confirm":
                    self._update(
                        job.task_id,
                        review_requested_at=utc_now(),
                        completed_at=None,
                    )
                elif status == "approved":
                    self._update(
                        job.task_id,
                        approved_at=utc_now(),
                        completed_at=None,
                    )
            except Exception as exc:  # worker boundary must remain alive
                logger.exception(f"[任务队列] 任务 {job.task_id} 执行失败")
                try:
                    if job.operation == "reidentify":
                        self._fail_reidentify(job, exc)
                    else:
                        current = self._read(job.task_id)
                        if current.get("execution_status") == "completed":
                            continue
                        changes: Dict[str, object] = {
                            "workflow_status": "failed",
                            "error": f"[任务队列] {exc}",
                            "completed_at": utc_now(),
                            "queue_position": None,
                        }
                        if job.operation == "execute_approved":
                            phase = current.get("execution_phase")
                            if phase in {
                                "admitted",
                                "preparing",
                                "failed_before_media",
                            } or not executor_started:
                                changes.update(
                                    {
                                        "execution_phase": "failed_before_media",
                                        "execution_requires_manual_check": False,
                                        "execution_may_have_written_targets": False,
                                    }
                                )
                            else:
                                changes.update(
                                    {
                                        "execution_requires_manual_check": True,
                                        "execution_may_have_written_targets": True,
                                    }
                                )
                        self._update(job.task_id, **changes)
                except Exception:
                    logger.exception(
                        f"[任务队列] 无法持久化任务失败状态，worker继续运行: "
                        f"{job.task_id}"
                    )
            finally:
                self.active_task_id = None
                self._queue.task_done()
                try:
                    self._refresh_queue_positions()
                except Exception:
                    logger.exception("[任务队列] 刷新队列位置失败，worker继续运行")

    async def _execute_rename(self, job: TaskJob) -> object:
        if job.path is None:
            raise RuntimeError("普通重命名任务缺少来源路径")
        return await run.io_bound(
            Rename().process,
            job.path,
            job.is_anime,
            job.is_movie,
            job.task_id,
            job.custom_name,
            job.custom_season_id,
            job.use_ai,
            job.provider_selection,
            job.task_source_scope,
        )

    async def _execute_approved(self, job: TaskJob) -> object:
        if not job.expected_fingerprint:
            raise PlanExecutionError("批准执行任务缺少整理计划版本标识")
        return await run.io_bound(
            self.execution_service.execute,
            job.task_id,
            job.expected_fingerprint,
        )

    def _validate_reidentify_revision(self, job: TaskJob) -> None:
        if job.expected_revision is None:
            raise PlanExecutionError("重新识别缺少内容版本")
        try:
            decision = self.decision_state_store.load(job.task_id)
        except DecisionStateError as exc:
            raise PlanExecutionError("任务内容不可用，请刷新后重试") from exc
        if decision.revision != job.expected_revision:
            raise PlanExecutionError("内容已更新，请刷新后继续")
        operation = self._read(job.task_id).get("selection_operation")
        if (
            not isinstance(operation, dict)
            or operation.get("request_id") != job.request_id
            or operation.get("state") != "pending"
        ):
            raise PlanExecutionError("这次选择已被更新，请刷新后继续")

    def _complete_reidentify(self, job: TaskJob) -> None:
        """Promote a selection only after disk authority is self-consistent."""

        if job.expected_revision is None or not job.request_id:
            raise PlanExecutionError("重新识别缺少请求关联")
        try:
            state = self.decision_state_store.load(job.task_id)
            plan = self.plan_store.load(job.task_id)
        except (DecisionStateError, PlanStoreError) as exc:
            raise PlanExecutionError("这次选择未能保存，请重试") from exc
        fingerprint = self.decision_state_store.fingerprint(state)
        if (
            state.revision <= job.expected_revision
            or plan.metadata.get("decision_state_revision") != state.revision
            or plan.metadata.get("decision_state_fingerprint") != fingerprint
        ):
            detail = str(self._read(job.task_id).get("error") or "").strip()
            raise PlanExecutionError(
                detail or "这次选择未生效，请重试或换一个作品"
            )
        if not self._selected_provider_was_persisted(
            job.provider_selection, state.provider_evidence, plan
        ):
            raise PlanExecutionError("保存结果与本次选择不一致，请重试")

        current_operation = self._read(job.task_id).get("selection_operation")
        if (
            isinstance(current_operation, dict)
            and current_operation.get("request_id") == job.request_id
            and current_operation.get("state") == "succeeded"
            and current_operation.get("result_decision_revision") == state.revision
        ):
            return

        def promote(data: dict) -> Optional[dict]:
            operation = data.get("selection_operation")
            if (
                not isinstance(operation, dict)
                or operation.get("request_id") != job.request_id
                or operation.get("state") != "pending"
            ):
                return None
            revised_operation = dict(operation)
            revised_operation.update({
                "state": "succeeded",
                "result_decision_revision": state.revision,
                "failure_code": None,
                "reason_codes": [],
                "retryable": False,
                "finished_at": utc_now(),
            })
            data["selection_operation"] = revised_operation
            data["provider_selection"] = dict(job.provider_selection or {})
            data.pop("provider_selection_failure", None)
            return data

        if mutate_task_at(self.task_root, job.task_id, promote) is None:
            raise PlanExecutionError("这次选择已被较新的操作取代")

    @staticmethod
    def _selected_provider_was_persisted(
        selection: Optional[Mapping[str, object]],
        state_evidence: tuple[Mapping[str, object], ...],
        plan: object,
    ) -> bool:
        """Prove the committed authority contains this exact provider choice."""

        if not isinstance(selection, Mapping):
            return False
        key = tuple(
            str(selection.get(field) or "").strip().casefold()
            for field in ("namespace", "entity_type", "provider_id")
        )
        if not all(key):
            return False

        plan_identities = []
        work = getattr(plan, "work_identity", None)
        primary = getattr(work, "primary_provider", None)
        if primary is not None:
            plan_identities.append(primary.to_dict())
        plan_identities.extend(
            item.to_dict()
            for item in getattr(plan, "linked_provider_identities", ())
        )

        def matching(items):
            return next((
                item for item in items
                if tuple(
                    str(item.get(field) or "").strip().casefold()
                    for field in ("namespace", "entity_type", "provider_id")
                ) == key
            ), None)

        state_match = matching(state_evidence)
        plan_match = matching(plan_identities)
        if state_match is None or plan_match is None:
            return False
        for field in ("poster_url", "source_url", "snapshot_at"):
            expected = str(selection.get(field) or "").strip()
            if expected and (
                str(state_match.get(field) or "").strip() != expected
                or str(plan_match.get(field) or "").strip() != expected
            ):
                return False
        return True

    def _repair_selection_projection(self, task_id: str) -> None:
        data = self._read(task_id)
        operation = data.get("selection_operation")
        if not isinstance(operation, dict) or operation.get("state") != "pending":
            return
        candidate = operation.get("candidate")
        try:
            expected = int(operation.get("expected_decision_revision"))
        except (TypeError, ValueError):
            return
        if not isinstance(candidate, dict):
            return
        job = TaskJob(
            task_id=task_id,
            path=None,
            provider_selection=dict(candidate),
            operation="reidentify",
            expected_revision=expected,
            request_id=str(operation.get("request_id") or "") or None,
        )
        try:
            self._complete_reidentify(job)
        except (PlanExecutionError, OSError, RuntimeError):
            return

    async def wait_for_selection_result(
        self,
        task_id: str,
        *,
        timeout: float = 60.0,
    ) -> dict:
        """Read the persisted current attempt until it reaches a terminal state."""

        deadline = asyncio.get_running_loop().time() + max(float(timeout), 0.1)
        while True:
            operation = self._read(task_id).get("selection_operation")
            if isinstance(operation, dict) and operation.get("state") in {
                "failed", "succeeded", "interrupted",
            }:
                return dict(operation)
            if asyncio.get_running_loop().time() >= deadline:
                return dict(operation) if isinstance(operation, dict) else {}
            await asyncio.sleep(0.1)

    def _fail_reidentify(self, job: TaskJob, exc: BaseException) -> None:
        message = str(exc).strip()
        code = "selection_failed"
        if "网络" in message or "读取" in message:
            code = "provider_unavailable"
        elif "季度" in message or "匹配" in message:
            code = "relation_unresolved"
        elif "保存" in message or "持久" in message:
            code = "save_failed"
        elif "版本" in message or "更新" in message:
            code = "stale_request"

        def fail(data: dict) -> Optional[dict]:
            operation = data.get("selection_operation")
            if (
                not isinstance(operation, dict)
                or operation.get("request_id") != job.request_id
                or operation.get("state") != "pending"
            ):
                return None
            structured = data.get("provider_selection_failure")
            structured = dict(structured) if isinstance(structured, Mapping) else {}
            reason_codes = tuple(
                str(item) for item in structured.get("reason_codes", ()) or ()
                if str(item)
            )
            revised_operation = dict(operation)
            revised_operation.update({
                "state": "failed",
                "failure_code": code,
                "reason_codes": list(reason_codes or (code,)),
                "retryable": code != "stale_request",
                "selected_subject_id": str(
                    structured.get("selected_subject_id") or ""
                ) or None,
                "evidence_fingerprint": str(
                    structured.get("evidence_fingerprint") or ""
                ) or None,
                "retry_requires_refresh": bool(
                    structured.get("retry_requires_refresh")
                ),
                "provider_subject_evidence": structured or None,
                "finished_at": utc_now(),
            })
            boundary_edges = structured.get(
                "provider_subject_boundary_edges"
            )
            expected_revision = operation.get("expected_decision_revision")
            if (
                isinstance(boundary_edges, (list, tuple))
                and len(boundary_edges) == 1
                and isinstance(boundary_edges[0], Mapping)
                and isinstance(expected_revision, int)
            ):
                try:
                    revised_operation[
                        "provider_subject_cohort_preview"
                    ] = build_provider_subject_cohort_preview(
                        structured,
                        expected_decision_revision=expected_revision,
                        boundary_edge=boundary_edges[0],
                        target_root_snapshot=data.get("draft_root_snapshot"),
                        target_root_config_fingerprint=data.get(
                            "draft_root_config_fingerprint"
                        ),
                        work_domain="animation",
                        group_kind="tv_episodes",
                    )
                    selected_subject_id = str(
                        structured.get("selected_subject_id") or ""
                    )
                    observations = structured.get("subject_observations")
                    selected_row = next((
                        item
                        for item in (
                            observations.get("subject_observations", ()) or ()
                            if isinstance(observations, Mapping) else ()
                        )
                        if isinstance(item, Mapping)
                        and str(item.get("bangumi_subject_id") or "")
                        == selected_subject_id
                    ), None)
                    queries = tuple(
                        selected_row.get("queries", ()) or ()
                    ) if isinstance(selected_row, Mapping) else ()
                    selected_is_no_match = bool(queries) and all(
                        isinstance(query, Mapping)
                        and query.get("truncated") is False
                        and query.get("outcome") == "no_match"
                        and not tuple(query.get("candidates", ()) or ())
                        for query in queries
                    )
                    if selected_is_no_match:
                        revised_operation["state"] = "awaiting_user"
                        revised_operation["retryable"] = False
                    revised_operation.pop(
                        "boundary_resolution_failure", None
                    )
                except (TypeError, ValueError) as preview_error:
                    revised_operation[
                        "boundary_resolution_failure"
                    ] = build_provider_subject_boundary_failure(
                        structured, preview_error
                    )
            data["selection_operation"] = revised_operation
            data["workflow_status"] = str(
                operation.get("previous_workflow_status") or "need_confirm"
            )
            data["error"] = message or "这次选择未生效"
            data["queue_position"] = None
            data["completed_at"] = utc_now()
            return data

        mutate_task_at(self.task_root, job.task_id, fail)

    def _has_abandon_audit(self, task_id: str) -> bool:
        safe_task_path(self.recovery_abandon_audit_root, task_id)
        return any(
            path.is_file()
            for path in self.recovery_abandon_audit_root.glob(
                f"{task_id}.[0-9a-f]*.json"
            )
        )

    def _recover_active_records(self) -> None:
        for path in self.task_root.glob("*.json"):
            self._repair_selection_projection(path.stem)
            data = self._read(path.stem)
            status = data.get("workflow_status")
            if data.get("preview_only") and status in ACTIVE_STATUSES:
                if status == "queued":
                    self._pending.append(path.stem)
                continue
            if status in ACTIVE_STATUSES:
                operation = data.get("selection_operation")
                if (
                    isinstance(operation, dict)
                    and operation.get("state") == "pending"
                ):
                    operation = dict(operation)
                    operation.update({
                        "state": "interrupted",
                        "failure_code": "app_restarted",
                        "reason_codes": ["app_restarted"],
                        "retryable": True,
                        "finished_at": utc_now(),
                    })
                    data["selection_operation"] = operation
                if data.get("queue_operation") == "execute_approved":
                    phase = data.get("execution_phase")
                    if phase in {"admitted", "preparing"}:
                        data.update(
                            {
                                "workflow_status": "failed",
                                "execution_phase": "failed_before_media",
                                "execution_requires_manual_check": False,
                                "execution_may_have_written_targets": False,
                                "error": "执行准备在应用重启前未完成，文件尚未处理。",
                                "queue_position": None,
                                "completed_at": utc_now(),
                            }
                        )
                    else:
                        data.update(
                            {
                                "workflow_status": "failed",
                                "execution_requires_manual_check": True,
                                "execution_may_have_written_targets": True,
                                "error": "应用重启时执行状态不明确，请先核查目标目录。",
                                "queue_position": None,
                                "completed_at": utc_now(),
                            }
                        )
                else:
                    data.update(
                        {
                            "workflow_status": "interrupted",
                            "error": "应用重启时任务仍处于活动状态；未自动重放，请人工确认后重试",
                            "queue_position": None,
                            "completed_at": utc_now(),
                        }
                    )
                self._write(path.stem, data)
        self._refresh_queue_positions()

    def _refresh_queue_positions(self) -> None:
        for position, task_id in enumerate(self._pending, start=1):
            data = self._read(task_id)
            if data:
                data["queue_position"] = position
                self._write(task_id, data)

    def _read(self, task_id: str) -> dict:
        try:
            if self.task_root == TASK_PATH:
                data = get_task(task_id)
            else:
                data = read_task_at(self.task_root, task_id)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            logger.error(f"[任务队列] 无法读取任务记录 {task_id}: {exc}")
            return {}
        if not isinstance(data, dict):
            logger.error(f"[任务队列] 任务记录不是对象，已忽略: {task_id}")
            return {}
        if data and data.get("uuid") != task_id:
            logger.error(f"[任务队列] 任务记录 UUID 与文件名不一致，已忽略: {task_id}")
            return {}
        return data

    def _write(self, task_id: str, data: dict) -> None:
        if data.get("uuid") != task_id:
            raise ValueError("任务记录 UUID 与目标文件名不一致")
        if self.task_root == TASK_PATH:
            write_task(task_id, data)
            return
        write_task_at(self.task_root, task_id, data)

    def _update(self, task_id: str, **changes: object) -> bool:
        revised = mutate_task_at(
            self.task_root,
            task_id,
            lambda data: {**data, **changes},
        )
        if revised is None:
            logger.warning(f"[任务队列] 任务记录已不存在，跳过状态更新: {task_id}")
            return False
        return True

task_queue = TaskQueueManager()
