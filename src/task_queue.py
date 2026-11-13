"""识别与媒体执行分别串行的任务队列。"""

from __future__ import annotations

import asyncio
import threading
import json
import os
import uuid
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, Iterable, Mapping, Optional

from nicegui import run

from .logger import logger
from .rename.plan_execution import PlanExecutionError, PlanExecutionService
from .rename import plan_execution as plan_execution_contract
from .rename.execution_recovery import requires_execution_replan, safe_approved_execution_retry
from .rename.decision_state import (
    DecisionStateError,
    DecisionStateStore,
)
from .rename.plan_review import (
    PlanReviewService,
    entry_id,
)
from .rename.inventory_plan import InventoryDraftError, build_inventory_review_draft
from .rename.plan_store import PlanStore, PlanStoreError
from .rename.plan_verifier import PlanVerifier
from .rename.provider_selection_authority import (
    partition_provider_candidates_by_presentation,
    provider_candidate_group_kind,
    provider_selection_capability,
    task_library_domain_from_task,
    task_media_kind_from_task,
)
from .rename.projection_authority import (
    ProjectionAuthorityError,
    ProjectionCommitService,
    ProviderReselectionPendingInput,
    TaskTerminalMutation,
    commit_provider_reselection_pending,
    inventory_fingerprint_for_plan,
)
from .rename.source_partition import (
    SourcePartitionStore,
)
from .rename.task_batch_scope import (
    BatchTaskSourceRequest,
    TaskBatchScopeManifestStore,
    TaskSourceScope,
)
from .rename.process import Rename
from .rename.trans import is_dry_run_enabled
from .rename.review_operation import (
    ProviderSelectionAwaitingUserOutcome,
    TaskWorkflowState,
    build_provider_selection_review_operation,
    current_provider_selection_review_operation,
    provider_selection_awaits_user,
    set_review_operation,
    transition_provider_selection_review_operation,
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
    restore_task_record_if_unchanged_at,
    safe_task_path,
    task_record_lock,
    write_task,
    write_task_at,
)


ACTIVE_STATUSES = {"queued", "running", "analyzing", "executing"}
COMPLETED_STATUSES = {
    "completed",
    "partial_failed",
    "rolled_back",
    "failed",
    "invalid",
    "cancelled",
    "interrupted",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")




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
    expected_recovery_fingerprint: Optional[str] = None
    expected_revision: Optional[int] = None
    request_id: Optional[str] = None
    task_source_scope: Optional[TaskSourceScope] = None
    recognition_origin: Optional[object] = None
    current_tv_refresh: Optional[object] = None
    source_group: Optional[object] = None


Executor = Callable[[TaskJob], Awaitable[object]]


class TaskQueueManager:
    """识别与媒体执行各自串行，先持久接纳再交给对应工作线程。"""

    def __init__(
        self,
        *,
        task_root: Path = TASK_PATH,
        executor: Optional[Executor] = None,
        approved_executor: Optional[Executor] = None,
        execution_service: Optional[PlanExecutionService] = None,
        decision_state_root: Path = DECISION_STATE_PATH,
        provider_link_catalog_root: Optional[Path] = None,
        recovery_abandon_audit_root: Path = RECOVERY_ABANDON_AUDIT_PATH,
        plan_root: Path = PLAN_PATH,
        batch_scope_root: Optional[Path] = None,
    ) -> None:
        self.task_root = Path(task_root)
        self.executor = executor or self._execute_rename
        effective_plan_root = Path(plan_root)
        self.decision_state_root = Path(decision_state_root)
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
        self.provider_link_catalog_root = (
            Path(provider_link_catalog_root)
            if provider_link_catalog_root is not None
            else (
                effective_plan_root.parent / "provider_link_catalog"
                if isolated else Path(PROVIDER_LINK_CATALOG_PATH)
            )
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
            decision_state_root=self.decision_state_root,
            recovery_transaction_root=(
                effective_plan_root.parent / "recovery_transaction"
                if isolated else RECOVERY_TRANSACTION_PATH
            ),
            provider_link_catalog_root=self.provider_link_catalog_root,
        )
        self.approved_executor = approved_executor or self._execute_approved
        self.decision_state_store = DecisionStateStore(self.decision_state_root)
        from .rename.initial_movie_completion import RootSnapshotSource
        from .rename.target_routing import snapshot_from_config_manager
        from .config.config_manager import cm
        self.root_snapshot_source = RootSnapshotSource(lambda: snapshot_from_config_manager(cm))
        self.plan_store = PlanStore(plan_root)
        self.projection_commit_service = ProjectionCommitService(
            self.decision_state_store,
            self.plan_store,
            task_root=self.task_root,
        )
        self.batch_scope_store = TaskBatchScopeManifestStore(
            Path(batch_scope_root or (self.task_root.parent / "task_batch_scope"))
        )
        self.review_root = Path(execution_review_root)
        self.source_partition_store = SourcePartitionStore(
            self.task_root.parent / "source_partition",
            allowed_roots=(
                self.task_root,
                self.plan_store.root,
                self.decision_state_store.root,
                self.review_root,
                self.batch_scope_store.root,
                self.projection_commit_service.ownership_store.root,
                self.projection_commit_service.journal_root,
            ),
        )
        self.recovery_abandon_audit_root = Path(recovery_abandon_audit_root)
        self._queue: asyncio.Queue[TaskJob] = asyncio.Queue()
        self._execution_queue: asyncio.Queue[TaskJob] = asyncio.Queue()
        self._execution_worker_task = None
        self.active_execution_task_id = None
        self._record_updates = threading.RLock()
        self._pending: list[str] = []
        self._tv_refresh_accepted: dict[str, TaskJob] = {}
        self._preflighting: dict[str, dict] = {}
        self._plan_updates: set[str] = set()
        self._admission_lock = asyncio.Lock()
        self._admissions = set()
        self._disk_calls = set()
        self._provider_reads = {}
        self._selection_waits = {}
        self._withdrawn_selection_requests = set()
        self._start_lock = asyncio.Lock()
        self._admission_epoch = 0
        self._worker_task: Optional[asyncio.Task] = None
        self._started = False
        self._stopping = False
        self.active_task_id: Optional[str] = None

    def prepare_input_batch(self, paths, *, is_anime, is_movie, source, preview_only):
        """单个API输入保留完整范围，与GUI共用批次构造。"""
        from .rename.input_directory_discovery import collect_source
        if not paths:
            raise PlanExecutionError("批次至少需要一个来源")
        groups, seen = [], set()
        preview = bool(preview_only)
        for path in map(Path, paths):
            key = str(path.absolute()).casefold()
            if key in seen:
                raise PlanExecutionError("批次来源路径重复")
            seen.add(key)
            try:
                groups.append(collect_source(path))
            except OSError as exc:
                raise PlanExecutionError(f"任务来源不存在：{path}") from exc
        return self._prepare_batch(groups, is_anime=is_anime, is_movie=is_movie,
                                   source=source, preview_only=preview)

    def prepare_discovered_batch(self, discovery, *, is_anime, is_movie, source):
        from .rename.input_directory_discovery import discovery_task_groups
        return self._prepare_batch(discovery_task_groups(discovery), is_anime=is_anime,
            is_movie=is_movie, source=source, preview_only=False, discovery=discovery)

    @staticmethod
    def _prepare_batch(groups, *, is_anime, is_movie, source, preview_only, discovery=None):
        requests, jobs = [], []
        for group in groups:
            task_id = str(uuid.uuid4())
            requests.append(BatchTaskSourceRequest(task_id=task_id, input_scope=group.input_scope,
                scope_kind=group.scope_kind, source_identities=group.identities,
                media_domain='movie' if is_movie is True else 'tv'))
            jobs.append(TaskJob(task_id=task_id, path=group.path, is_anime=is_anime, is_movie=is_movie,
                source=source, preview_only=preview_only, source_group=group if discovery is not None else None,
                operation='await_source_confirmation' if group.reason else 'rename'))
        batch = dict(batch_id=f'ui-batch-{uuid.uuid4().hex}', task_requests=tuple(requests), jobs=tuple(jobs))
        if discovery is not None:
            batch['discovery'] = discovery
        return batch

    async def _disk(self, function, *args, **kwargs):
        """取消调用者也等待真实线程完成，磁盘线程可安全回主循环接纳。"""
        from .queue_runtime import stoppable_disk_call
        return await stoppable_disk_call(self, lambda: self._stopping, function, *args, **kwargs)

    def _recover_queue(self):
        self.task_root.mkdir(parents=True, exist_ok=True)
        from .rename.recognition_reset import recover
        recover(self)
        self.source_partition_store.recover_pending()
        self.projection_commit_service.recover_pending()
        self.batch_scope_store.recover_all_pending()
        self._recover_active_records()

    async def start(self) -> None:
        epoch = self._admission_epoch
        async with self._start_lock:
            if self._stopping:
                raise PlanExecutionError('任务队列正在停止，请等待当前任务安全收尾')
            if self._started:
                return
            await self._disk(self._recover_queue)
            if self._stopping or epoch != self._admission_epoch:
                raise PlanExecutionError('核对期间任务队列已停止，请重新添加')
            self._started = True
            self._worker_task = asyncio.create_task(self._worker(), name='bar-task-queue')
            self._execution_worker_task = asyncio.create_task(self._worker(media=True), name='bar-execution-queue')

    async def stop(self) -> None:
        self._admission_epoch += 1
        from .utils.task_changes import mark_changed
        mark_changed()
        self._started = False
        if not self._stopping:
            self._stopping = True
            async def finish():
                try:
                    # start 的恢复以及 admission 的持久写入均须实际结束。
                    async with self._start_lock:
                        pass
                    await asyncio.gather(*tuple(self._admissions), return_exceptions=True)
                    workers = ((self._worker_task, self.active_task_id),
                               (self._execution_worker_task, self.active_execution_task_id))
                    for worker, active in workers:
                        if worker is not None and active is None:
                            worker.cancel()
                    for worker, _active in workers:
                        if worker is not None:
                            try:
                                await worker
                            except asyncio.CancelledError:
                                pass
                    while self._disk_calls:
                        await asyncio.gather(*tuple(self._disk_calls), return_exceptions=True)
                finally:
                    self._worker_task = None
                    self._execution_worker_task = None
                    self._stopping = False
            self._stop_task = asyncio.create_task(finish())
        await asyncio.shield(self._stop_task)


    async def enqueue_batch(
        self,
        *,
        batch_id: str,
        task_requests: tuple[BatchTaskSourceRequest, ...],
        jobs: tuple[TaskJob, ...],
        discovery=None,
        admission_guard=None,
        on_admission_started=None,
    ) -> tuple[str, ...]:
        """Freeze one batch and all source reservations before any worker sees it."""

        admission_epoch = self._admission_epoch

        def check_admission():
            if admission_epoch != self._admission_epoch or self._stopping:
                raise PlanExecutionError('核对期间任务队列已停止，请重新添加')
            if admission_guard is not None and not admission_guard():
                raise PlanExecutionError('添加窗口已关闭或输入已改变，未继续接纳任务')

        def begin_admission():
            check_admission()
            # 仅表示即将进入可能写入的区域，不能据此宣称接纳成功。
            if on_admission_started is not None:
                on_admission_started()

        if discovery is not None:
            from .rename.input_directory_discovery import validate_discovery, discovery_task_groups
            # 仅校验冻结分组，持久接纳与队列发布仍在下方完成。
            await self._disk(validate_discovery, discovery)
            check_admission()
            if tuple(job.source_group for job in jobs) != discovery_task_groups(discovery):
                raise PlanExecutionError('实际入队组与完整预览不同，未添加任务')
            if not jobs:
                if task_requests:
                    raise PlanExecutionError('无需建卡的来源不得夹带任务请求')
                return ()  # All-misc observation, no queue start or durable writes.
        if not self._started:
            begin_admission()
            await self.start()
        requested_ids = {item.task_id for item in task_requests}
        job_ids = {item.task_id for item in jobs}
        if not jobs or requested_ids != job_ids or len(job_ids) != len(jobs):
            raise PlanExecutionError("批次任务与source scope不一致")
        if await self._disk(lambda: any(self._has_abandon_audit(identifier) for identifier in requested_ids)):
            raise PlanExecutionError('批次包含已结束任务的历史身份，请创建新的任务卡')

        from .rename.input_directory_discovery import SourceGroup
        grouped = [job for job in jobs if job.source_group is not None]
        if grouped:
            if discovery is None:
                raise PlanExecutionError('发现型任务必须保留完整来源预览，未添加任务')
            locations, physical = set(), set()
            for job in jobs:
                group = job.source_group
                request = next(item for item in task_requests if item.task_id == job.task_id)
                if (type(group) is not SourceGroup or request.input_scope != group.input_scope
                        or request.scope_kind != group.scope_kind or request.source_identities != group.identities
                        or job.path != group.path or job.operation != ('await_source_confirmation' if group.reason else 'rename')):
                    raise PlanExecutionError('发现组与实际入队来源不一致')
                # 全部分组必须互斥，不能重复分配同一物理文件。
                for identity in group.identities:
                    if identity.location_key in locations or identity.physical_key in physical:
                        raise PlanExecutionError('发现组来源重叠，未添加任务')
                    locations.add(identity.location_key)
                    physical.add(identity.physical_key)

        from .rename.library_assets import LibraryAssets
        assets = LibraryAssets(self.execution_service.rebuild_service, review_service=self.execution_service.review_service)
        repairs = await self._disk(assets.completed_sources, tuple(
            request for request in task_requests
            if next(job for job in jobs if job.task_id == request.task_id).operation != 'await_source_confirmation'))
        check_admission()
        if repairs and grouped and len(jobs) > 1:
            raise PlanExecutionError('部分来源已有登记资产，本批尚未添加；请先单独核查已有任务再选择其余来源')
        if repairs:
            for existing in repairs.values():
                view = await self._disk(assets.inspect_admission, existing)
                check_admission()
                if view.state not in {'missing', 'complete', 'choice'}:
                    raise PlanExecutionError(f'已有资产核查未通过，本批尚未添加：{view.detail}')
            fresh_jobs = tuple(job for job in jobs if job.task_id not in repairs)
            results = []
            repaired = []
            try:
                for job in jobs:
                    existing = repairs.get(job.task_id)
                    if existing is not None:
                        begin_admission()
                        await self._repair_completed_asset(existing)
                        repaired.append(existing)
                    results.append(existing or job.task_id)
                if fresh_jobs:
                    check_admission()
                    await self.enqueue_batch(batch_id=batch_id,
                        task_requests=tuple(r for r in task_requests if r.task_id not in repairs), jobs=fresh_jobs,
                        admission_guard=admission_guard, on_admission_started=on_admission_started)
            except Exception as exc:
                raise PlanExecutionError(f'本批未全部完成：已核查或生成修复计划 {len(repaired)} 项（均未执行）；请在任务卡核对后重试。原因：{exc}') from exc
            return tuple(results)

        cancelled = False
        async def admit():
            async with self._admission_lock:
                check_admission()
                if cancelled:
                    raise asyncio.CancelledError()
                begin_admission()
                previous_records = {}
                staged_ids = []
                written = {}
                offset = len(self._pending)
                def rollback():
                    for identifier in staged_ids:
                        previous = previous_records[identifier]
                        if previous is not None:
                            restore_task_record_if_unchanged_at(self.task_root, identifier, written[identifier], previous)
                        else:
                            with task_record_lock(self.task_root, identifier):
                                if read_task_at(self.task_root, identifier) == written[identifier]:
                                    safe_task_path(self.task_root, identifier).unlink(missing_ok=True)
                                    from .utils.task_changes import mark_changed
                                    mark_changed()
                    self.batch_scope_store.fail_closed(batch_id)
                def stage():
                    from .rename.content_policy import policy_for_admission
                    frozen_requests = tuple(policy_for_admission(self, request) for request in task_requests)
                    manifest = self.batch_scope_store.reserve_batch(
                        batch_id=batch_id,
                        task_requests=frozen_requests,
                    )
                    if manifest.state != "reservations_written":
                        raise PlanExecutionError("批次source reservation未进入待入队状态")
                    scopes = {item.task_id: item for item in manifest.task_source_scopes}
                    scoped_jobs = tuple(
                        replace(job, task_source_scope=scopes[job.task_id]) for job in jobs
                    )
                    try:
                        from .rename.automatic_recognition import AutomaticRecognitionOrigin
                        for job in scoped_jobs:
                            if job.source_group is not None and set(job.task_source_scope.source_identities) != set(job.source_group.identities):
                                raise PlanExecutionError('实际接纳范围与逐组确认文件不一致，已停止整批')
                        # Origin is produced inside the real admission barrier, before
                        # any provider observation or worker-visible item. Keep historical
                        # jobs and explicit reidentification on their existing contracts.
                        scoped_jobs = tuple(replace(job, recognition_origin=AutomaticRecognitionOrigin.at_admission(
                            self, job, batch_id=manifest.batch_id)) if (
                                job.operation == 'rename' and not job.preview_only and job.provider_selection is None
                                and job.task_source_scope.content_policy is not None
                                and not self.plan_store.path_for(job.task_id).exists()) else job for job in scoped_jobs)
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
                                "queue_position": offset + len(staged_ids) + 1,
                                "batch_id": manifest.batch_id,
                                "batch_scope_fingerprint": scope.batch_scope_fingerprint,
                                "reservation_lineage_id": scope.reservation_lineage_id,
                                "reservation_revision": scope.reservation_revision,
                                "task_source_scope": scope.to_dict(),
                                "error": None,
                            })
                            if job.recognition_origin is not None:
                                if type(job.recognition_origin) is not AutomaticRecognitionOrigin:
                                    raise PlanExecutionError('自动识别原作业类型无效')
                                payload['automatic_recognition_origin'] = job.recognition_origin.to_dict()
                                payload['recognition_job_id'] = job.recognition_origin.job_id
                            if job.source_group is not None:
                                payload['source_group_observation'] = job.source_group.to_dict()
                                payload['source_group_pending'] = bool(job.source_group.reason)
                                payload['queue_operation'] = job.operation
                            written[job.task_id] = dict(payload)
                            staged_ids.append(job.task_id)
                            self._write(job.task_id, payload)
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
                        if any(self._read(key) != value for key, value in written.items()):
                            raise PlanExecutionError('接纳期间任务已改变，未发布队列')
                        return scoped_jobs
                    except BaseException:
                        rollback()
                        raise
                from .queue_runtime import stoppable_disk_call, disk_call
                scoped_jobs = await stoppable_disk_call(self, lambda: cancelled or self._stopping, stage)
                try:
                    check_admission()
                    if cancelled:
                        raise asyncio.CancelledError()
                    # 此段不让出事件循环；只有完整 durable 接纳后才发布整批。
                    self._pending.extend(staged_ids)
                    for job in scoped_jobs:
                        if not job.preview_only:
                            self._queue.put_nowait(job)
                except BaseException:
                    self._pending[:] = [item for item in self._pending if item not in staged_ids]
                    # put_nowait 的故障也可能发生在已插入之后，撤回本批可见项。
                    retained = []
                    while not self._queue.empty():
                        queued = self._queue.get_nowait()
                        self._queue.task_done()
                        if queued.task_id not in staged_ids:
                            retained.append(queued)
                    for queued in retained:
                        asyncio.Queue.put_nowait(self._queue, queued)
                    await disk_call(rollback)
                    raise
                return tuple(job.task_id for job in scoped_jobs)
        admission = asyncio.create_task(admit())
        self._admissions.add(admission)
        admission.add_done_callback(self._admissions.discard)
        try:
            return await asyncio.shield(admission)
        except asyncio.CancelledError:
            cancelled = True
            # 接纳 owner 完成回滚前，不把取消作为完成返回。
            while not admission.done():
                try:
                    await asyncio.shield(admission)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not admission.cancelled():
                admission.exception()
            raise

    async def partition_task_source(
        self,
        *,
        parent_task_id: str,
        expected_plan_fingerprint: str,
        selected_entry_id: str,
        reason: str,
    ) -> str:
        """Retired: no alternate writer or automatic compatibility fallback."""
        raise PlanExecutionError('旧二分拆卡已停用，请使用“调整计划范围”；无法处理的旧任务请安全结束后重新添加。')

    async def partition_task_sources(
        self,
        *,
        parent_task_id: str,
        expected_plan_fingerprint: str,
        selected_entry_ids: Iterable[str],
        reason: str,
    ) -> str:
        """Retired: no alternate writer or automatic compatibility fallback."""
        raise PlanExecutionError('旧二分拆卡已停用，请使用“调整计划范围”；无法处理的旧任务请安全结束后重新添加。')


    def preview_source_regrouping(self, *, task_id: str, expected_plan_fingerprint: str, groups=None,
                                  selected_entry_ids=None, excluded_sources=None):
        """Preview all replacement groups without admission or any task write."""
        from .rename import source_regrouping
        before = source_regrouping.observe_current(self, task_id, expected_plan_fingerprint)
        preview = source_regrouping.project_preview(before[1], before[2], groups, selected_entry_ids, excluded_sources)
        if source_regrouping.validate_preview(preview, before[1], before[2], groups, selected_entry_ids, excluded_sources) is not preview:
            raise PlanExecutionError('完整分组预览校验已断开')
        if source_regrouping.observe_current(self, task_id, expected_plan_fingerprint) != before:
            raise PlanExecutionError('预览期间任务或来源登记已变化，请重新预览')
        return preview

    def create_regrouped_tasks(self, preview):
        """Confirm complete regrouping; create ordinary, unqueued pending cards."""
        from .rename.source_regrouping_creation import create
        return create(self, preview)

    async def create_and_start_regrouped_tasks(self, preview):
        """Finish replacement first, then admit confirmed video groups once."""
        from .rename.recognition_queue_admission import capture_workspace_open
        from .rename.utils import VIDEO_SUFFIX
        groups = preview.task_groups
        ids = await self._disk(self.create_regrouped_tasks, preview)
        if (type(ids) is not tuple or len(ids) != len(groups)
                or len(set(ids)) != len(ids) or not all(isinstance(i, str) and i for i in ids)):
            raise ValueError('未取得完整建卡结果，请回任务列表核对，不能视为完成')
        failures = {}
        for task_id, group in zip(ids, groups):
            if not any(Path(p).suffix.lower() in VIDEO_SUFFIX and p not in group['excluded_sources']
                       for p in group['sources']):
                continue
            try:
                def read_opened():
                    return capture_workspace_open(self, task_id, self._read(task_id))
                opened = await self._disk(read_opened)
                await self.confirm_source_group(task_id, opened_evidence=opened)
            except (ValueError, RuntimeError, OSError) as exc:
                # These are real new cards even if admission fails. Never
                # recreate the batch or retry cards already accepted.
                failures[task_id] = f'新卡已创建，但识别尚未启动：{exc}'
                await self._disk(self._update, task_id, error=failures[task_id])
        return ids, failures

    def _require_no_pending_partition(self, task_id: str) -> None:
        if self.source_partition_store.pending_for_task(task_id):
            raise PlanExecutionError(
                "source partition尚未完成；请重启恢复后再继续"
            )

    @staticmethod
    def _require_recognition_retry_safe(data: Mapping[str, object]) -> None:
        """Recognition must not replace an execution/recovery workflow."""
        if (
            data.get("execution_requires_manual_check")
            or data.get("execution_may_have_written_targets")
            or data.get("execution_status") in {
                "completed", "executing", "catalog_update_pending", "uncertain",
                "reapproval_required",
            }
            or data.get("workflow_status") in {"executing", "catalog_update_pending"}
            or data.get("execution_phase") == "media_boundary_entered"
            or (
                data.get("queue_operation") == "execute_approved"
                and data.get("execution_status") != "dry_run"
            )
        ):
            raise PlanExecutionError("该任务已有执行或恢复状态；请先核查，不能通过普通重试重新识别")

    async def resume_default_activation(self, task_id: str, expected_fingerprint: str) -> str:
        from .queue_runtime import transaction
        epoch = self._admission_epoch
        await self._disk(lambda: self._require_recognition_retry_safe(self._read(task_id)))
        if self._stopping or epoch != self._admission_epoch:
            raise PlanExecutionError('读取期间队列已停止，请刷新后重试')
        if not self._started:
            await self.start()
        async with self._admission_lock:
            return await transaction(self, self._resume_default_activation, task_id, expected_fingerprint, epoch)

    def _resume_default_activation(self, task_id, expected_fingerprint, epoch):
        from .rename.recognition_queue_admission import _authority_locks
        with _authority_locks(self, task_id):
            return self._resume_default_activation_locked(task_id, expected_fingerprint, epoch)

    def _resume_default_activation_locked(self, task_id, expected_fingerprint, epoch):
        from .rename.automatic_recognition import validate_resume
        from .rename.default_content import require_unexecuted_task
        from .utils.utils import task_record_snapshot_at, publish_task_and_accept_at
        from .queue_runtime import require_not_cancelled
        if self._stopping or epoch != self._admission_epoch:
            raise PlanExecutionError('队列状态已变化，请刷新后重试')
        self._require_no_pending_partition(task_id)
        if self.is_active(task_id) or task_id in self._pending or task_id in self._plan_updates:
            raise PlanExecutionError('任务已排队或正在运行，请等待当前操作')
        previous, current, _ = task_record_snapshot_at(self.task_root, task_id)
        require_unexecuted_task(current)
        plan = self.plan_store.load(task_id)
        state = self.decision_state_store.load(task_id)
        if PlanStore.fingerprint(plan) != expected_fingerprint:
            raise PlanExecutionError('自动续做的原计划已变化，请刷新')
        proof = validate_resume(self, plan, state)
        origin = proof.origin
        job = TaskJob(task_id=task_id, path=Path(origin.path), is_anime=origin.is_anime,
            is_movie=origin.is_movie, custom_name=origin.custom_name, custom_season_id=origin.requested_season,
            use_ai=origin.use_ai, source=origin.source, operation='resume_default_activation',
            expected_fingerprint=expected_fingerprint, expected_revision=state.revision,
            task_source_scope=TaskSourceScope.from_mapping(current.get('task_source_scope')),
            recognition_origin=origin)
        current.update(workflow_status='queued', queue_operation='resume_default_activation',
                       queue_position=len(self._pending) + 1, error=None)
        from .rename.recognition_queue_admission import _accept
        publish_task_and_accept_at(self.task_root, task_id, previous=previous, data=current,
            before_publish=require_not_cancelled,
            accept=lambda: _accept(self, job, expected_epoch=epoch))
        return task_id

    async def _run_plan_update(self, task_id, function, *args, allow_queued=False, **kwargs):
        """只登记正在进行的计划事务，不将其伪装成可执行作业。"""
        from .queue_runtime import transaction, on_owner, require_not_cancelled
        epoch = self._admission_epoch

        def require_current():
            require_not_cancelled()
            if (self._stopping or epoch != self._admission_epoch
                    or self.is_active(task_id)
                    or (task_id in self._pending and not allow_queued)
                    or task_id in self._preflighting):
                raise PlanExecutionError('任务正在核查、排队、执行或停止，请稍后再试')

        require_current()
        if task_id in self._plan_updates:
            raise PlanExecutionError('任务正在更新计划，请等待本次操作完成')
        self._plan_updates.add(task_id)

        def update():
            on_owner(require_current)
            return function(*args, **kwargs)

        try:
            return await transaction(self, update)
        finally:
            self._plan_updates.discard(task_id)

    async def _repair_completed_asset(self, task_id, *, expected_fingerprint=None):
        if not self._started:
            await self.start()
        service = self.execution_service.rebuild_service
        if expected_fingerprint is not None:
            return await self._run_plan_update(task_id, service.generate, task_id,
                                               expected_fingerprint=expected_fingerprint)
        from .rename.library_assets import LibraryAssets
        return await self._run_plan_update(task_id,
            LibraryAssets(service, review_service=self.execution_service.review_service).repair, task_id)

    async def confirm_asset_plan(self, task_id, expected_fingerprint, operation, *, recovery_preparation=None):
        """消费打开窗口时的观察；原确认服务仍重新核对计划和操作证据。"""
        from .rename.asset_operation import AssetOperation
        from .rename.asset_execution_recovery import AssetRecoveryPreparation
        if type(operation) is not AssetOperation or operation.to_dict()['mode'] not in {'repair', 'replace'}:
            raise PlanExecutionError('缺少当前补齐或换源观察，请重新核查')
        if recovery_preparation is not None and type(recovery_preparation) is not AssetRecoveryPreparation:
            raise PlanExecutionError('恢复核查版本无效，请重新核查')
        review = self.execution_service.review_service
        confirm = review.confirm_asset_repair if operation.to_dict()['mode'] == 'repair' else review.confirm_asset_replacement
        return await self._run_plan_update(task_id, confirm, task_id, expected_fingerprint,
            expected_operation_fingerprint=operation.fingerprint, recovery_preparation=recovery_preparation)

    async def reconcile_catalog_update(self, task_id):
        from .rename.execution_recovery import reconcile_catalog_update
        service = self.execution_service
        return await self._run_plan_update(task_id, reconcile_catalog_update, task_id,
            task_root=self.task_root, record_root=service.record_root, plan_store=self.plan_store,
            work_catalog=service.work_catalog, series_catalog=service.series_catalog,
            provider_link_catalog=service.provider_link_catalog, decision_state_root=self.decision_state_root,
            review_root=self.review_root, recovery_transaction_root=service.recovery_transaction_store.root)

    async def abandon_catalog_update(self, task_id, expected_plan_fingerprint):
        from .rename.execution_recovery import abandon_catalog_update_recovery
        return await self._run_plan_update(task_id, abandon_catalog_update_recovery, task_id,
            task_root=self.task_root, record_root=self.execution_service.record_root, plan_store=self.plan_store,
            review_root=self.review_root, decision_state_root=self.decision_state_root,
            audit_root=self.recovery_abandon_audit_root, expected_plan_fingerprint=expected_plan_fingerprint)

    async def generate_completed_rebuild(self, task_id, *, expected_fingerprint):
        """只使用打开核查窗口时的证据生成待批准修订。"""
        if not isinstance(expected_fingerprint, str) or not expected_fingerprint:
            raise PlanExecutionError('缺少原核查版本，请刷新后重新核对')
        return await self._repair_completed_asset(task_id, expected_fingerprint=expected_fingerprint)

    async def retry_current(self, task_id: str, *, source: str = "retry") -> str:
        epoch = self._admission_epoch
        current = await self._disk(self._read, task_id)
        if self._stopping or epoch != self._admission_epoch:
            raise PlanExecutionError('读取期间队列已停止，请刷新后重试')
        from .rename.input_directory_discovery import require_source_group_confirmed
        require_source_group_confirmed(current or {})
        if current and current.get('workflow_status') in {'completed', 'partial_failed', 'need_confirm', 'invalid', 'approved'}:
            from .rename.replacement_completion import has_completed_media
            completed = has_completed_media(current)
            state = await self._disk(lambda: self.decision_state_store.load_readonly(task_id)
                if self.decision_state_store.path_for(task_id).exists() else None)
            if self._stopping or epoch != self._admission_epoch:
                raise PlanExecutionError('读取期间队列已停止，请刷新后重试')
            if completed or (state is not None and state.completed_rebuild_observation is not None):
                try:
                    await self._repair_completed_asset(task_id)
                except (ValueError, RuntimeError, OSError) as exc:
                    raise PlanExecutionError(f'无法重建，请核查当前资产：{exc}') from exc
                return task_id
        from .rename.recognition_queue_admission import admit
        return await admit(self, task_id, mode='retry', request={'source': source})

    async def confirm_source_group(self, task_id, *, opened_evidence):
        from .rename.recognition_queue_admission import admit, WorkspaceOpenEvidence
        if type(opened_evidence) is not WorkspaceOpenEvidence:
            raise PlanExecutionError('缺少打开来源组时的版本，请刷新后核对')
        return await admit(self, task_id, mode='source_group', request={'source': 'source_group_confirmation'},
                           opened_evidence=opened_evidence)

    async def prepare_rolled_back_restart(self, task_id):
        from .rename.rolled_back_restart import prepare
        await self._disk(self._require_no_pending_partition, task_id)
        if self.is_active(task_id) or task_id in self._pending or task_id in self._plan_updates:
            raise PlanExecutionError('任务正在处理中，请稍后再核查')
        return await self._disk(prepare, self.execution_service, task_id)

    async def restart_rolled_back(self, task_id, preparation):
        from .rename.rolled_back_restart import restart
        await self._disk(self._require_no_pending_partition, task_id)
        return await self._run_plan_update(task_id, restart, self.execution_service, task_id, preparation)

    async def replan_failed_execution(self, task_id: str, expected_fingerprint: str) -> str:
        from .queue_runtime import transaction
        epoch = self._admission_epoch
        if not self._started:
            await self.start()
        if self._stopping or self.is_active(task_id) or task_id in self._pending or task_id in self._plan_updates:
            raise PlanExecutionError('任务正在处理中或队列正在停止')
        async with self._admission_lock:
            await transaction(self, self._retire_failed_execution, task_id, expected_fingerprint)
        if self._stopping or epoch != self._admission_epoch:
            raise PlanExecutionError('旧批准已退役；队列停止期间不自动接纳新作业')
        return await self.retry_current(task_id, source='execution_replan')

    def _retire_failed_execution(self, task_id, expected_fingerprint):
        self._require_no_pending_partition(task_id)
        from .rename.current_plan_retirement import retire_failed_current_tv
        if retire_failed_current_tv(self, task_id, expected_fingerprint):
            return

        def retire(data: dict) -> dict:
            if (not requires_execution_replan(data)
                    or data.get("execution_requested_fingerprint") != expected_fingerprint
                    or data.get("plan_fingerprint") != expected_fingerprint):
                raise PlanExecutionError("失败状态或计划已经变化，请刷新后核查")
            plan = self.execution_service.plan_store.load(task_id)
            if PlanStore.fingerprint(plan) != expected_fingerprint:
                raise PlanExecutionError("失败计划版本已经变化")
            ledger = safe_task_path(self.execution_service.record_root, task_id, suffix=".ledger.json")
            if os.path.lexists(ledger):
                raise PlanExecutionError("发现执行记录，必须先核查，不能直接重规划")
            scope = TaskSourceScope.from_mapping(data.get("task_source_scope") or {})
            self.batch_scope_store.require_active_scope(scope, batch_id=str(data.get("batch_id") or ""))
            history = list(data.get("execution_replan_history") or [])
            history.append({"fingerprint": expected_fingerprint, "error": data.get("error"),
                            "code": data.get("execution_failure_code"), "retired_at": utc_now()})
            # Removal invalidates the only approval authority. If the subsequent
            # task transaction fails, the failed old task remains unapproved.
            PlanReviewService(self.execution_service.plan_store, self.review_root).delete(task_id)
            data.update(workflow_status="need_confirm", queue_operation="rename",
                        execution_status=None, execution_phase=None,
                        execution_requested_fingerprint=None, execution_fingerprint=None,
                        execution_failure_code=None, execution_ledger=None,
                        execution_replan_history=history, error=None)
            return data

        if mutate_task_at(self.task_root, task_id, retire) is None:
            raise PlanExecutionError("任务不存在")
        return

    async def reenqueue_workspace(
        self, task_id: str, path: Path, *, is_anime: Optional[bool],
        is_movie: Optional[bool], custom_name: Optional[str],
        custom_season_id: Optional[int],
        preview_only: bool, opened_evidence=None,
    ) -> str:
        from .rename.recognition_queue_admission import admit
        return await admit(self, task_id, mode='workspace', request=dict(
            path=str(path), is_anime=is_anime, is_movie=is_movie,
            custom_name=custom_name, custom_season_id=custom_season_id,
            preview_only=preview_only),
            opened_evidence=opened_evidence)

    async def enqueue_approved(
        self, task_id: str, expected_fingerprint: str
    ) -> str:
        """Queue one exact approved Plan version for the single worker."""
        return await self._enqueue_execution(task_id, expected_fingerprint)

    async def enqueue_asset_recovery(self, task_id: str, expected_fingerprint: str) -> str:
        return await self._enqueue_execution(task_id, expected_fingerprint, mode='asset_recovery')

    async def enqueue_asset_rollback(self, task_id, expected_fingerprint, *, expected_recovery_fingerprint):
        return await self._enqueue_execution(task_id, expected_fingerprint, mode='asset_rollback',
            expected_recovery_fingerprint=expected_recovery_fingerprint)

    def execution_preflight_status(self, task_id):
        """Ephemeral UI feedback, never persisted decision or queue authority."""
        from time import perf_counter
        value = self._preflighting.get(task_id)
        if value is None:
            return None
        return dict(message='正在核查批准版本、登记及文件，请稍候',
                    elapsed_ms=round((perf_counter() - value['started']) * 1000),
                    mode=value['mode'])

    async def _enqueue_execution(self, task_id, expected_fingerprint, *, mode='approved_plan',
                                 expected_recovery_fingerprint=None):
        from time import perf_counter
        from .rename.queue_preflight import QueueAdmissionPreparation, prepare_queue_admission
        from .rename.file_lock import task_operation_lock, FileLockBusyError
        from .rename.source_partition import SourcePartitionError

        epoch = self._admission_epoch
        await self._disk(self._require_no_pending_partition, task_id)
        if (self._stopping or epoch != self._admission_epoch or task_id in self._preflighting or task_id in self._plan_updates
                or self.is_active(task_id) or task_id in self._pending):
            raise PlanExecutionError('任务正在核查、排队、执行或停止，请稍后再试')
        self._preflighting[task_id] = dict(mode=mode, started=perf_counter())

        def require_current_request():
            from .queue_runtime import require_not_cancelled
            require_not_cancelled()
            if epoch != self._admission_epoch or self._stopping:
                raise PlanExecutionError('预检已取消或队列已停止，请重新确认后提交')
            if self.is_active(task_id) or task_id in self._pending or task_id in self._plan_updates:
                raise PlanExecutionError('该任务已在队列或正在执行')

        try:
            # 三种执行入口共用预检占位；最终磁盘事务仍校验原epoch与任务快照。
            proof = await self._disk(prepare_queue_admission, self._read,
                self.execution_service, task_id, expected_fingerprint, mode=mode,
                expected_recovery_fingerprint=expected_recovery_fingerprint)
            if not isinstance(proof, QueueAdmissionPreparation):
                raise PlanExecutionError('预检已取消或未返回有效结果，任务没有入队')
            require_current_request()
            if proof.checked_plan is not None:
                plan_execution_contract.require_approved_execution_policy(
                    proof.checked_plan.execution_policy)
            if not self._started:
                await self.start()
            require_current_request()
            await self._disk(self._require_no_pending_partition, task_id)
            before = json.loads(proof.task_snapshot)
            recovery = mode == 'asset_recovery'
            rollback = mode == 'asset_rollback'
            job = TaskJob(task_id=task_id, path=None, source=mode,
                operation='execute_approved', expected_fingerprint=expected_fingerprint,
                request_id=uuid.uuid4().hex,
                expected_recovery_fingerprint=proof.rollback_fingerprint)

            # Use the same lock order as approval. Never restore stale bytes if
            # another writer changed/deleted the task while publication failed.
            from .queue_runtime import transaction, on_owner
            from .rename.recognition_queue_admission import _accept
            from .utils.utils import task_record_snapshot_at, publish_task_and_accept_at
            def commit():
                try:
                    with task_operation_lock(self.task_root, task_id,
                            operation='execution_admission', fingerprint=expected_fingerprint):
                        with self.source_partition_store.task_lock(task_id):
                            self._require_no_pending_partition(task_id)
                            on_owner(require_current_request)
                            def commit(current):
                                if current != before:
                                    raise PlanExecutionError('预检期间任务已变化，未加入队列；请刷新后重试')
                                return {**current, 'workflow_status': 'queued',
                                    'queue_position': len(self._pending) + 1,
                                    'queue_operation': 'execute_approved',
                                    'execution_requested_fingerprint': expected_fingerprint,
                                    'execution_requested_at': utc_now(), 'execution_phase': 'admitted',
                                    'execution_requires_manual_check': recovery,
                                    'execution_may_have_written_targets': recovery,
                                    'asset_recovery_requested': recovery, 'asset_rollback_requested': rollback,
                                    'execution_requested_recovery_fingerprint': proof.rollback_fingerprint,
                                    'execution_preflight_ms': proof.elapsed_ms,
                                    'completed_at': None, 'error': None}
                            previous_bytes, current, _ = task_record_snapshot_at(self.task_root, task_id)
                            queued = commit(current)
                            self._refresh_queue_positions()
                            from .rename.selection_wait import execution_receipt, remember_execution
                            receipt = execution_receipt(self, job, previous_bytes, queued) if mode == 'approved_plan' else None
                            def accept():
                                require_current_request()
                                _accept(self, job, expected_epoch=epoch)
                                remember_execution(self, job, receipt)
                            publish_task_and_accept_at(self.task_root, task_id,
                                previous=previous_bytes, data=queued,
                                before_publish=lambda: on_owner(require_current_request),
                                accept=lambda: on_owner(accept))
                except (FileLockBusyError, SourcePartitionError) as exc:
                    raise PlanExecutionError('任务正在被其他操作更新，未入队；请稍后重试') from exc
            await transaction(self, commit)
            logger.info(f'[任务队列] 已批准整理计划入队 {task_id}；'
                        f'fingerprint={expected_fingerprint[:12]}；'
                        f'preflight={mode}；elapsed_ms={proof.elapsed_ms}')
            return task_id
        finally:
            self._preflighting.pop(task_id, None)

    async def enqueue_reidentify(
        self, task_id: str, provider_selection: Dict[str, object],
        expected_revision: int, *, season_number: Optional[int] = None,
    ) -> str:
        from .rename.recognition_queue_admission import admit
        return await admit(self, task_id, mode='selected', request=dict(
            selection=provider_selection, expected_revision=expected_revision,
            season_number=season_number))

    def _prepare_reidentify_job(
        self,
        task_id: str,
        provider_selection: Dict[str, object],
        expected_revision: int,
        *,
        season_number: Optional[int] = None,
        current: dict,
        selection_mode: str = 'retry',
    ) -> tuple:
        """Pure candidate builder; shared admission owns all publication."""

        self._require_no_pending_partition(task_id)
        if self.is_active(task_id) or task_id in self._pending or task_id in self._plan_updates:
            raise PlanExecutionError("该任务正在处理中，请稍后再试")
        if not current:
            raise PlanExecutionError("任务记录不存在")
        self._require_recognition_retry_safe(current)
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
        from .rename.provider_selection_authority import selection_task_context
        from .rename.queued_work_selection import complete_kind
        replacement = complete_kind(self.plan_store.load(task_id), decision, provider_selection) if selection_mode == 'selected' else None
        current = selection_task_context(current, self.plan_store.load(task_id), decision, provider_selection)
        if season_number is not None:
            primary = decision.confirmed_work.get('primary_provider') or {}
            if (type(season_number) is not int or season_number < 0
                    or (decision.library_domain if decision.confirmed_work
                        else task_library_domain_from_task(current)) not in {'general', 'animation'}
                    or provider_candidate_group_kind(provider_selection) != 'tv_episodes'
                    or decision.confirmed_work and replacement != 'tv' and any(
                        str(provider_selection.get(key) or '') != str(primary.get(key) or '')
                        for key in ('namespace', 'entity_type', 'provider_id'))):
                raise PlanExecutionError('季号仅适用于本次选择的剧集，不能借此更换已确认作品')
        selection = {
            key: provider_selection.get(key)
            for key in (
                "namespace", "entity_type", "provider_id", "title",
                "original_title", "year", "poster_url", "source_url",
                "snapshot_at", "library_domain",
                "platform",
            )
        }
        if not all(selection.get(key) for key in (
            "namespace", "entity_type", "provider_id",
        )):
            raise PlanExecutionError("所选作品信息不完整")
        group_kind = provider_candidate_group_kind(selection) or ""
        capability = provider_selection_capability(
            selection,
            group_kind=group_kind,
            task_media_kind=task_media_kind_from_task(current),
            task_library_domain=task_library_domain_from_task(current),
        )
        if not capability.actionable:
            raise PlanExecutionError(capability.message)
        if group_kind == 'tv_episodes' and replacement != 'tv':
            from .rename.default_activation import require_supported_tv_selection
            try:
                require_supported_tv_selection(decision)
            except ValueError as exc:
                raise PlanExecutionError(str(exc)) from exc
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
        task_media_kind = task_media_kind_from_task(current)
        job = TaskJob(
            task_id=task_id,
            path=path,
            is_anime=current.get("is_anime"),
            is_movie=(
                True if task_media_kind == "movie"
                else False if task_media_kind == "tv"
                else current.get("is_movie")
            ),
            custom_name=None,
            custom_season_id=season_number if season_number is not None else (
                current.get("season_id") if current.get("season_source") == "explicit_user" else None),
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
            try:
                _operation, operation_context = (
                    current_provider_selection_review_operation(data)
                )
            except ValueError:
                operation_context = {}
            if operation_context.get("lifecycle") == "pending":
                raise PlanExecutionError("该任务正在处理中，请稍后再试")
            pending_operation = {
                "schema_version": 1,
                "request_id": request_id,
                "state": "pending",
                "candidate": selection,
                "expected_decision_revision": int(expected_revision),
                "requested_season_number": job.custom_season_id,
                "result_decision_revision": None,
                "failure_code": None,
                "reason_codes": [],
                "retryable": True,
                "requested_at": requested_at,
                "finished_at": None,
                "previous_workflow_status": previous_status,
            }
            data.update({
                "workflow_status": "queued",
                "queue_operation": "reidentify",
                "queue_position": len(self._pending) + 1,
                "reidentify_expected_revision": int(expected_revision),
                "reidentify_requested_at": requested_at,
                "started_at": None,
                "completed_at": None,
                "error": None,
            })
            data.pop('provider_selection_failure', None)
            return set_review_operation(
                data,
                build_provider_selection_review_operation(data, pending_operation),
            )

        return job, stage(dict(current))

    def cancel_queued(self, task_id: str, *, refresh_positions=True) -> bool:
        """Cancel a job that has not started; the worker will skip its stale item."""
        if self.is_active(task_id) or task_id not in self._pending:
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
        if refresh_positions:
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

    async def abandon_and_remove(self, task_id, *, record_root, plan_root, review_root):
        """等待安全结束的实际事务；仍允许取消真正尚未执行的作业。"""
        return await self._run_plan_update(task_id, self._abandon_and_remove, task_id,
            allow_queued=True, record_root=record_root, plan_root=plan_root, review_root=review_root)

    def _abandon_and_remove(
        self,
        task_id: str,
        *,
        record_root: Path,
        plan_root: Path,
        review_root: Path,
    ) -> None:
        """Close current admission and remove mutable UI state, never the ledger."""

        from .rename.file_lock import task_operation_lock, FileLockBusyError
        try:
            with task_operation_lock(self.task_root, task_id, operation='task_safe_end'), self.execution_service.recovery_transaction_store.lock(task_id):
                self._abandon_and_remove_locked(task_id, record_root=record_root,
                    plan_root=plan_root, review_root=review_root)
        except FileLockBusyError as exc:
            raise PlanExecutionError('任务正在执行、批准或安全结束，请稍后重试') from exc

    def _abandon_and_remove_locked(self, task_id, *, record_root, plan_root, review_root):
        from .rename.recovery_transaction import require_no_pending_detach
        require_no_pending_detach(plan_store=self.plan_store, task_root=self.task_root,
            decision_root=self.decision_state_store.root, task_id=task_id)
        self._require_no_pending_partition(task_id)
        if self.is_active(task_id):
            raise PlanExecutionError("任务正在执行，不能安全移除")
        current = self._read(task_id)
        if (current.get('execution_requires_manual_check') or current.get('execution_may_have_written_targets')
                or current.get('asset_rollback_phase') not in {None, '', 'rolled_back'}):
            raise PlanExecutionError('执行或回滚尚需核查；请完成原操作核查后再安全结束')
        transaction = self.execution_service.recovery_transaction_store.load_optional(task_id)
        if transaction is not None and transaction.phase != 'completed':
            raise PlanExecutionError('作品登记恢复尚未完成，请先在整理页面完成恢复，再删除任务')
        if self.decision_state_store.path_for(task_id).exists():
            state = self.decision_state_store.load_readonly(task_id)
            if state.recovery_state == 'catalog_detach_pending':
                raise PlanExecutionError('作品登记恢复尚未完成或阶段记录缺失，请先对账恢复，再删除任务')
        from .rename.rolled_back_restart import archive_evidence, current_evidence
        archive_evidence(self.execution_service, task_id, 'safe-abandon',
                         current_evidence(self.execution_service, task_id))
        if task_id in self._pending:
            if not self.cancel_queued(task_id, refresh_positions=False):
                raise PlanExecutionError("任务排队状态已变化，不能安全移除")
        else:
            self._retire_task_authorities(task_id, reason="user-safe-abandon")
        with task_record_lock(self.task_root, task_id):
            safe_task_path(self.task_root, task_id).unlink(missing_ok=True)
            from .utils.task_changes import mark_changed
            mark_changed()
        plan_store = PlanStore(Path(plan_root))
        try:
            # Keep immutable history and the mapping. Existing Catalog readers
            # may still need the current Plan as a read-only legacy adapter.
            # Removing approval below and retiring ownership revoke execution.
            PlanReviewService(plan_store, Path(review_root)).delete(task_id)
        except PlanStoreError:
            logger.warning(f"[任务] 拒绝删除非法 UUID 对应的整理计划：{task_id}")

    def is_active(self, task_id):
        return task_id in (self.active_task_id, self.active_execution_task_id)

    async def _worker(self, *, media=False) -> None:
        queue = self._execution_queue if media else self._queue
        active_field = 'active_execution_task_id' if media else 'active_task_id'
        while self._started:
            job = await queue.get()
            if not media and job.operation == 'execute_approved':
                self._execution_queue.put_nowait(job)
                continue
            try:
                # 接纳是主循环内存操作；磁盘锁释放后才允许消费者读取已发布作业。
                from .queue_runtime import PUBLICATION_TASK_NAME
                while publications := [item for item in tuple(self._admissions)
                                       if item.get_name() == PUBLICATION_TASK_NAME and not item.done()]:
                    await asyncio.gather(*(asyncio.shield(item) for item in publications),
                                         return_exceptions=True)
            except asyncio.CancelledError:
                queue.task_done()
                if media:
                    self._queue.task_done()
                raise
            if job.request_id in self._withdrawn_selection_requests:
                self._withdrawn_selection_requests.discard(job.request_id)
                queue.task_done()
                if media:
                    self._queue.task_done()
                continue
            self._selection_waits.pop(job.request_id, None)
            if job.operation == 'refresh_current_tv':
                from .rename.current_tv_refresh import drive
                try:
                    await drive(self, job)
                except Exception:
                    logger.exception('原作品刷新安全收尾失败，保留请求等待核查')
                finally:
                    queue.task_done()
                continue
            setattr(self, active_field, job.task_id)
            executor_started = False
            try:
                current = await self._disk(self._read, job.task_id)
                if not current or current.get("workflow_status") == "cancelled":
                    continue
                if job.task_id in self._pending:
                    self._pending.remove(job.task_id)
                setattr(self, active_field, job.task_id)
                await self._disk(self._refresh_queue_positions)
                if not await self._disk(self._update,
                    job.task_id,
                    workflow_status="running",
                    queue_position=0,
                    started_at=utc_now(),
                    error=None,
                ):
                    continue
                if job.operation == 'await_source_confirmation':
                    from .rename.input_directory_discovery import validate_pending_group
                    validate_pending_group(current, job.task_source_scope)
                    await self._disk(self._update, job.task_id, workflow_status='need_confirm',
                                 error=current['source_group_observation']['reason'])
                    continue
                from .rename.input_directory_discovery import require_source_group_confirmed
                require_source_group_confirmed(current)
                if job.operation == "reidentify":
                    await self._disk(self._validate_reidentify_revision, job)
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
                outcome = await executor(job)
                from .rename.ova_selection import OvaSeasonPending, TVSeasonPending, persist_pending
                if job.operation == "reidentify":
                    if type(outcome) in (OvaSeasonPending, TVSeasonPending):
                        await self._disk(persist_pending, self, job, outcome)
                    elif isinstance(outcome, ProviderSelectionAwaitingUserOutcome):
                        await self._disk(self._persist_reidentify_awaiting_user, job, outcome)
                    else:
                        await self._disk(self._complete_reidentify, job)
                elif isinstance(outcome, (ProviderSelectionAwaitingUserOutcome, OvaSeasonPending, TVSeasonPending)):
                    raise PlanExecutionError("作品仍需人工确认，请打开任务重新选择作品")
                current = await self._disk(self._read, job.task_id)
                if not current:
                    continue
                status = current.get("workflow_status")
                if status not in COMPLETED_STATUSES | {"need_confirm", "approved", "catalog_update_pending"}:
                    detail = outcome if isinstance(outcome, str) and outcome else "任务未发布明确的审核或执行结果，不能标记为完成"
                    raise PlanExecutionError(detail)
                if job.operation != "execute_approved" and status in {
                    "completed", "approved", "catalog_update_pending",
                }:
                    raise PlanExecutionError("识别流程不能发布执行完成或批准状态")
                if status in COMPLETED_STATUSES:
                    await self._disk(self._update, job.task_id, completed_at=utc_now())
                elif status == "need_confirm":
                    await self._disk(self._update,
                        job.task_id,
                        review_requested_at=utc_now(),
                        completed_at=None,
                    )
                elif status == "approved":
                    await self._disk(self._update,
                        job.task_id,
                        approved_at=utc_now(),
                        completed_at=None,
                    )
            except Exception as exc:  # worker boundary must remain alive
                logger.exception(f"[任务队列] 任务 {job.task_id} 执行失败")
                try:
                    if job.operation == "reidentify":
                        await self._disk(self._fail_reidentify, job, exc)
                    else:
                        current = await self._disk(self._read, job.task_id)
                        if current.get("execution_status") == "completed" and job.source != 'asset_rollback':
                            continue
                        changes: Dict[str, object] = {
                            "workflow_status": "failed",
                            "error": f"[任务队列] {exc}",
                            "completed_at": utc_now(),
                            "queue_position": None,
                        }
                        if job.operation == "execute_approved":
                            changes["execution_failure_code"] = getattr(exc, "code", None)
                            phase = current.get("execution_phase")
                            from .rename.execution_recovery import safe_approved_execution_retry
                            if await self._disk(safe_approved_execution_retry, current):
                                pass
                            elif phase in {
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
                        await self._disk(self._update, job.task_id, **changes)
                except Exception:
                    logger.exception(
                        f"[任务队列] 无法持久化任务失败状态，worker继续运行: "
                        f"{job.task_id}"
                    )
            finally:
                setattr(self, active_field, None)
                queue.task_done()
                if media:
                    self._queue.task_done()
                try:
                    await self._disk(self._refresh_queue_positions)
                except Exception:
                    logger.exception("[任务队列] 刷新队列位置失败，worker继续运行")

    async def _execute_rename(self, job: TaskJob) -> object:
        from .rename.queued_work_selection import run
        handled, outcome = await self._disk(run, self, job)
        if handled:
            return outcome
        renamer, operation, proof = await self._disk(self._prepare_rename, job)
        if operation is not None:
            return await self._disk(operation, renamer, proof)
        if job.recognition_origin is not None:
            from .rename.default_activation import initialize_worker, discover_initial_type
            if job.is_anime is None or job.is_movie is None:
                return await self._disk(discover_initial_type, renamer, job.recognition_origin)
            await self._disk(initialize_worker, renamer, job.recognition_origin)
        return await self._disk(
            renamer.process,
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


    def _prepare_rename(self, job: TaskJob):
        from .rename.input_directory_discovery import require_source_group_confirmed
        require_source_group_confirmed(self._read(job.task_id) or {})
        if job.path is None:
            raise RuntimeError("普通重命名任务缺少来源路径")
        renamer = Rename(
            decision_state_root=self.decision_state_root,
            provider_link_catalog_root=self.provider_link_catalog_root,
        )
        # Keep the composition-root postcondition explicit for test doubles:
        # every object returned by the constructor consumes this queue's roots.
        renamer.decision_state_root = self.decision_state_root
        renamer.provider_link_catalog_root = self.provider_link_catalog_root
        from .rename.initial_movie_selection import InitialMovieRequest
        # Freeze the originating job before background Provider observation.
        # The later commit may not repin itself to a newer pending request.
        renamer.initial_movie_request = (InitialMovieRequest.from_job(job)
            if job.operation == 'reidentify' else None)
        renamer.automatic_recognition_origin = job.recognition_origin
        if job.operation == 'resume_default_activation':
            from .rename.automatic_recognition import validate_resume
            from .rename.default_activation import complete_tv_observation, complete_movie_observation
            plan = self.plan_store.load(job.task_id)
            state = self.decision_state_store.load(job.task_id)
            if (PlanStore.fingerprint(plan) != job.expected_fingerprint or state.revision != job.expected_revision):
                raise PlanExecutionError('自动续做在排队期间已过期，未重新检索')
            proof = validate_resume(self, plan, state)
            if proof.origin != job.recognition_origin:
                raise PlanExecutionError('自动续做与原排队作业不一致')
            selected, _name, _info = proof.resolve()
            operation = complete_movie_observation if selected['entity_type'] == 'movie' else complete_tv_observation
            return renamer, operation, proof
        if job.recognition_origin is not None:
            from .rename.automatic_recognition import search_policy_value, validate_original_job
            from .rename.default_activation import initialize_worker, discover_initial_type
            # Capture configuration availability, never credentials, before
            # source publication and before the first Provider call. Later
            # observations cannot redefine which providers were expected.
            policy = search_policy_value(renamer._provider_search_policy(
                is_anime=job.is_anime, is_movie=job.is_movie))
            def freeze_scope(task):
                actual, _scope = validate_original_job(self, job.recognition_origin)
                if actual != task:
                    raise PlanExecutionError('自动查询范围冻结期间原作业已变化')
                if task.get('automatic_search_policy') is not None:
                    if task['automatic_search_policy'] != policy:
                        raise PlanExecutionError('自动查询配置已变化，请重新选择作品')
                elif self.plan_store.path_for(job.task_id).exists():
                    raise PlanExecutionError('既有自动计划缺原查询范围，不能事后补猜')
                task['automatic_search_policy'] = policy
                return task
            if mutate_task_at(self.task_root, job.task_id, freeze_scope) is None:
                raise PlanExecutionError('原自动作业已不存在')
        return renamer, None, None

    async def _execute_approved(self, job: TaskJob) -> object:
        if not job.expected_fingerprint:
            raise PlanExecutionError("批准执行任务缺少整理计划版本标识")
        if job.source == 'asset_rollback':
            return await run.io_bound(self.execution_service.rollback_asset_replacement,
                job.task_id, job.expected_fingerprint, expected_recovery_fingerprint=job.expected_recovery_fingerprint)
        if job.source == 'asset_recovery':
            from .rename.asset_resume import resume_asset
            return await run.io_bound(resume_asset, self.execution_service, job.task_id, job.expected_fingerprint)
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
        try:
            _operation, operation_context = (
                current_provider_selection_review_operation(
                    self._read(job.task_id)
                )
            )
        except ValueError as exc:
            raise PlanExecutionError("这次选择已被更新，请刷新后继续") from exc
        if (
            operation_context.get("request_id") != job.request_id
            or operation_context.get("lifecycle") != "pending"
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
            detail = str(self._read(job.task_id).get("error") or "").strip()
            raise PlanExecutionError(detail or "这次选择未能保存，请重试") from exc
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

        try:
            _operation, current_context = (
                current_provider_selection_review_operation(
                    self._read(job.task_id)
                )
            )
        except ValueError:
            current_context = {}
        if (
            current_context.get("request_id") == job.request_id
            and current_context.get("lifecycle") == "succeeded"
            and current_context.get("result_decision_revision") == state.revision
        ):
            return

        def promote(data: dict) -> Optional[dict]:
            try:
                _operation, operation_context = (
                    current_provider_selection_review_operation(data)
                )
            except ValueError:
                return None
            if (
                operation_context.get("request_id") != job.request_id
                or operation_context.get("lifecycle") != "pending"
            ):
                return None
            set_review_operation(
                data,
                transition_provider_selection_review_operation(
                    data,
                    {
                        "state": "succeeded",
                        "result_decision_revision": state.revision,
                        "result_plan_fingerprint": PlanStore.fingerprint(plan),
                        "failure_code": None,
                        "reason_codes": [],
                        "retryable": False,
                        "finished_at": utc_now(),
                    },
                    expected_fingerprint=_operation.fingerprint,
                ),
            )
            data["provider_selection"] = dict(job.provider_selection or {})
            data.pop("provider_selection_failure", None)
            return data

        if mutate_task_at(self.task_root, job.task_id, promote) is None:
            raise PlanExecutionError("这次选择已被较新的操作取代")

    def _persist_reidentify_awaiting_user(
        self,
        job: TaskJob,
        outcome: ProviderSelectionAwaitingUserOutcome,
    ) -> None:
        """Project one typed non-error process result into current authority."""

        if job.expected_revision is None or not job.request_id:
            raise PlanExecutionError("等待确认结果缺少请求关联")
        try:
            validated = ProviderSelectionAwaitingUserOutcome.from_mapping(
                outcome.to_dict()
            )
        except (TypeError, ValueError) as exc:
            raise PlanExecutionError("等待确认结果无法安全读取") from exc
        if (
            validated.task_id != job.task_id
            or dict(validated.candidate) != dict(job.provider_selection or {})
        ):
            raise PlanExecutionError("等待确认结果与本次选择不一致")
        try:
            state = self.decision_state_store.load(job.task_id)
            plan = self.plan_store.load(job.task_id)
        except (DecisionStateError, PlanStoreError) as exc:
            raise PlanExecutionError("等待确认前的任务内容无法安全读取") from exc
        fingerprint = self.decision_state_store.fingerprint(state)
        if (
            state.revision != job.expected_revision
            or plan.metadata.get("decision_state_revision") != state.revision
            or plan.metadata.get("decision_state_fingerprint") != fingerprint
        ):
            raise PlanExecutionError("等待确认前的任务内容已经变化")

        evidence = dict(validated.provider_subject_evidence)
        current_task = self._read(job.task_id)
        if not current_task:
            raise PlanExecutionError("等待确认前的任务记录不存在")
        draft_task = dict(current_task)
        for stale_key in (
            "provider_identity",
            "linked_provider_identities",
            "provider_link_snapshot",
            "provider_link_resolution",
        ):
            draft_task.pop(stale_key, None)
        draft_task["provider_candidates"] = (
            partition_provider_candidates_by_presentation(
                (dict(validated.candidate),)
            )
        )
        pending_intent = None
        if state.content_policy is not None:
            from .rename.pending_tv_selection import prepare as prepare_pending_tv
            pending_intent = prepare_pending_tv(self, job, validated)
        try:
            fresh_draft = plan if pending_intent is not None else build_inventory_review_draft(
                draft_task,
                # Re-identification creates a new unapproved policy snapshot.
                # Later review must preserve it, not refresh an older policy.
                dry_run=is_dry_run_enabled(),
            )
        except InventoryDraftError as exc:
            raise PlanExecutionError(
                f"无法建立重选后的安全审核草案：{exc}"
            ) from exc

        def persist(
            decision, projected_plan, data: dict
        ) -> Mapping[str, object]:
            try:
                operation, operation_context = (
                    current_provider_selection_review_operation(data)
                )
            except ValueError:
                raise PlanExecutionError("这次选择已被较新的操作取代")
            if (
                operation_context.get("request_id") != job.request_id
                or operation_context.get("lifecycle") != "pending"
                or int(
                    operation_context.get("expected_decision_revision", -1)
                ) != job.expected_revision
            ):
                raise PlanExecutionError("这次选择已被较新的操作取代")
            if pending_intent is not None:
                from .rename.pending_tv_selection import preview_target
                data['provider_selection_preview_target'] = preview_target(pending_intent.proof)
            operation_task = dict(data)
            operation_task["decision_state_revision"] = decision.revision
            operation_task["decision_state_fingerprint"] = (
                self.decision_state_store.fingerprint(decision)
            )
            result_plan_fingerprint = PlanStore.fingerprint(projected_plan)
            revised = transition_provider_selection_review_operation(
                operation_task,
                {
                    "state": "awaiting_user",
                    "expected_decision_revision": decision.revision,
                    "result_decision_revision": decision.revision,
                    "result_plan_fingerprint": result_plan_fingerprint,
                    "failure_code": None,
                    "reason_codes": list(evidence.get("reason_codes", ()) or ()),
                    "retryable": False,
                    "selected_subject_id": str(
                        evidence.get("selected_subject_id") or ""
                    ),
                    "evidence_fingerprint": str(
                        evidence.get("evidence_fingerprint") or ""
                    ),
                    "retry_requires_refresh": False,
                    "provider_subject_evidence": evidence,
                    "boundary_resolution_failure": None,
                    "finished_at": utc_now(),
                },
                expected_fingerprint=operation.fingerprint,
            )
            if (
                revised.state != TaskWorkflowState.ACTIONABLE
                or not revised.commands
            ):
                raise PlanExecutionError(
                    "等待确认结果没有形成可执行的审核命令"
                )
            set_review_operation(data, revised)
            data.pop("provider_selection_failure", None)
            # Provider re-identification always returns to the reviewable task
            # boundary.  ``invalid`` was the pre-selection inventory state,
            # not a truthful status for an actionable operation.
            data["workflow_status"] = "need_confirm"
            data["error"] = None
            data["queue_position"] = None
            data["completed_at"] = None
            data["provider_selection"] = dict(validated.candidate)
            for stale_key in (
                "provider_identity",
                "linked_provider_identities",
                "provider_link_snapshot",
                "provider_link_resolution",
            ):
                data.pop(stale_key, None)
            return data
        terminal = TaskTerminalMutation.prepare(
            self.task_root,
            job.task_id,
            persist,
        )
        try:
            if pending_intent is not None:
                from .rename.pending_tv_selection import commit as commit_pending_tv
                commit_pending_tv(pending_intent, terminal)
                return
            commit_provider_reselection_pending(
                self.decision_state_store,
                self.plan_store,
                ProviderReselectionPendingInput(
                    task_id=job.task_id,
                    expected_revision=job.expected_revision,
                ),
                fresh_draft,
                task_terminal=terminal,
            )
        except (ProjectionAuthorityError, ValueError) as exc:
            raise PlanExecutionError(
                "重选作品未能原子更新任务内容，请刷新后重试"
            ) from exc

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
        try:
            _operation, operation_context = (
                current_provider_selection_review_operation(data)
            )
        except ValueError:
            return
        if operation_context.get("lifecycle") != "pending":
            return
        candidate = operation_context.get("candidate")
        try:
            expected = int(operation_context.get("expected_decision_revision"))
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
            request_id=str(operation_context.get("request_id") or "") or None,
        )
        try:
            self._complete_reidentify(job)
        except (PlanExecutionError, OSError, RuntimeError):
            return

    async def wait_for_selection_result(
        self,
        task_id: str,
        *,
        timeout: Optional[float] = 60.0,
    ) -> dict:
        """任务变化后读取持久结果；取消窗口等待不取消作业。"""
        from .utils.task_changes import revision, wait_changed
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(float(timeout), 0)
        epoch = self._admission_epoch
        while True:
            observed = revision()
            try:
                _operation, operation_context = (
                    current_provider_selection_review_operation(
                        await self._disk(self._read, task_id)
                    )
                )
            except ValueError:
                if timeout is None:
                    raise ValueError("任务选择已更新，请查看当前结果。")
                operation_context = {}
            if operation_context.get("lifecycle") in {
                "failed", "awaiting_user", "awaiting_season", "succeeded", "interrupted",
            }:
                return dict(operation_context)
            if epoch != self._admission_epoch or self._stopping:
                raise ValueError('服务已停止，请重新打开任务核对已保存的状态。')
            remaining = None if deadline is None else max(0, deadline - loop.time())
            if not await wait_changed(observed, remaining):
                return dict(operation_context)

    def _fail_reidentify(self, job: TaskJob, exc: BaseException) -> None:
        message = str(exc).strip()
        from .rename.file_lock import is_lock_busy
        code = "selection_failed"
        if is_lock_busy(exc):
            code = "internal_busy"
        elif "网络" in message or "读取" in message:
            code = "provider_unavailable"
        elif "集号" in message or "分集内容" in message:
            code = "content_mapping_failed"
        elif "季度" in message:
            code = "relation_unresolved"
        elif "保存" in message or "持久" in message:
            code = "save_failed"
        elif "版本" in message or "更新" in message:
            code = "stale_request"

        def fail(data: dict) -> Optional[dict]:
            try:
                operation, operation_context = (
                    current_provider_selection_review_operation(data)
                )
            except ValueError:
                return None
            if (
                operation_context.get("request_id") != job.request_id
                or operation_context.get("lifecycle") != "pending"
            ):
                return None
            structured = data.get("provider_selection_failure")
            structured = dict(structured) if isinstance(structured, Mapping) else {}
            if code == "internal_busy":
                structured = {}
            reason_codes = tuple(
                str(item) for item in structured.get("reason_codes", ()) or ()
                if str(item)
            )
            failure_code = ('provider_unavailable'
                            if 'subject_candidate_transport_error' in reason_codes else code)
            diagnostic = structured.get('transport_diagnostic')
            transport_failure = isinstance(diagnostic, Mapping) and diagnostic.get('provider') == 'bangumi'
            if transport_failure:
                failure_code = ('provider_unavailable' if structured.get('retryable') else
                                'provider_' + str(diagnostic.get('code') or 'request_failed'))
            changes = {
                "state": "failed",
                "failure_code": failure_code,
                "reason_codes": list(reason_codes or (failure_code,)),
                "retryable": bool(structured.get('retryable')) if transport_failure else failure_code != "stale_request",
                "selected_subject_id": str(
                    structured.get("selected_subject_id") or ""
                ) or None,
                "evidence_fingerprint": str(
                    structured.get("evidence_fingerprint") or ""
                ) or None,
                "retry_requires_refresh": bool(
                    structured.get("retry_requires_refresh")
                ) or failure_code == 'provider_unavailable',
                "provider_subject_evidence": structured or None,
                "finished_at": utc_now(),
            }
            changes["boundary_resolution_failure"] = None
            set_review_operation(
                data,
                transition_provider_selection_review_operation(
                    data,
                    changes,
                    expected_fingerprint=operation.fingerprint,
                ),
            )
            previous_status = str(operation_context.get("previous_workflow_status") or "")
            data["workflow_status"] = (
                previous_status if previous_status in {"need_confirm", "invalid"}
                else "need_confirm"
            )
            data["error"] = message or "这次选择未生效"
            data["queue_position"] = None
            data["completed_at"] = None
            return data

        mutate_task_at(self.task_root, job.task_id, fail)

    def _has_abandon_audit(self, task_id: str) -> bool:
        safe_task_path(self.recovery_abandon_audit_root, task_id)
        # A retained historical Plan with no task is not an empty UUID slot.
        # Re-admission must use a new ID even if its retirement record is lost.
        if (not safe_task_path(self.task_root, task_id).exists()
                and self.plan_store.path_for(task_id).exists()):
            return True
        return any(
            path.is_file()
            for path in self.recovery_abandon_audit_root.glob(
                f"{task_id}.[0-9a-f]*.json"
            )
        )

    def _recover_active_records(self) -> None:
        for path in self.task_root.glob("*.json"):
            from .rename.current_tv_refresh import recover_interrupted
            if recover_interrupted(self, path.stem):
                continue
            from .rename.recovery_transaction import require_no_pending_detach
            try:
                require_no_pending_detach(plan_store=self.plan_store, task_root=self.task_root,
                    decision_root=self.decision_state_store.root, task_id=path.stem)
            except (ValueError, RuntimeError, OSError):
                continue  # Preserve the pending operation; its GUI owns recovery, not startup.
            self._repair_selection_projection(path.stem)
            data = self._read(path.stem)
            status = data.get("workflow_status")
            if (data.get('execution_status') == 'completed'
                    and data.get('execution_phase') in {'replacement_finalizing', 'replacement_cleanup_failed'}):
                # 启动只标记待收尾，不执行媒体操作。
                self._update(path.stem, workflow_status='partial_failed',
                    execution_phase='replacement_cleanup_failed', completed_at=None, queue_position=None,
                    execution_recovery_message='换源媒体已完成，请核查并继续收尾；不会重复执行媒体')
                continue
            if data.get("preview_only") and status in ACTIVE_STATUSES:
                if status == "queued":
                    self._pending.append(path.stem)
                continue
            if status in ACTIVE_STATUSES:
                try:
                    operation, operation_context = (
                        current_provider_selection_review_operation(data)
                    )
                except ValueError:
                    operation = None
                    operation_context = {}
                if operation_context.get("lifecycle") == "pending":
                    set_review_operation(
                        data,
                        transition_provider_selection_review_operation(
                            data,
                            {
                                "state": "interrupted",
                                "failure_code": "app_restarted",
                                "reason_codes": ["app_restarted"],
                                "retryable": True,
                                "finished_at": utc_now(),
                            },
                            expected_fingerprint=(
                                operation.fingerprint
                                if operation is not None else None
                            ),
                        ),
                    )
                if data.get("queue_operation") == "execute_approved":
                    phase = data.get("execution_phase")
                    if phase in {"admitted", "preparing"} and not data.get('asset_recovery_requested'):
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
        # 两条工作队列共用短记录更新保护，避免展示序号抢占状态提交。
        with self._record_updates:
            self._refresh_queue_positions_locked()

    def _refresh_queue_positions_locked(self) -> None:
        for position, task_id in enumerate(tuple(self._pending), start=1):
            observed = self._read(task_id)
            if not observed or observed.get("queue_position") == position:
                continue
            def update(data):
                if (data.get("queue_position") == position
                        or data.get("workflow_status") not in {None, "queued"}):
                    return None
                return {**data, "queue_position": position}
            try:
                mutate_task_at(self.task_root, task_id, update)
            except RuntimeError as exc:
                from .rename.file_lock import FileLockBusyError
                if not isinstance(exc.__cause__, FileLockBusyError):
                    raise
                # 序号只用于展示；任务自身正在更新时保留其状态，下次再刷新。

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
        with self._record_updates:
            return self._update_locked(task_id, **changes)

    def _update_locked(self, task_id: str, **changes: object) -> bool:
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
