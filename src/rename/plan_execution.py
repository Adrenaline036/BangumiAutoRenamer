"""Fail-closed execution gate for a version-bound approved rename plan."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

from ..config.config_manager import cm
from ..utils.path import (
    DECISION_STATE_PATH,
    RECORD_PATH,
    RECOVERY_TRANSACTION_PATH,
    SERIES_CATALOG_PATH,
    TASK_PATH,
    WORK_CATALOG_PATH,
    PROVIDER_LINK_CATALOG_PATH,
)
from ..utils.utils import mutate_task_at, safe_task_path
from .file_lock import FileLockBusyError, advisory_file_lock
from .plan_review import (
    PlanReviewError,
    PlanReviewService,
    ReviewStatus,
    entry_id,
)
from .plan_store import PlanStore, PlanStoreError
from .physical_identity import (
    PhysicalIdentityUnavailable,
    canonical_location_key,
    canonical_physical_key,
)
from .path_safety import first_reparse_component
from .plan_verifier import PlanVerifier, tv_authority_issue
from .rename_plan import PlanStatus, RenamePlan
from .trans import Trans, is_dry_run_enabled
from .work_catalog import (
    WorkCatalog,
    WorkCatalogError,
    catalog_record_fingerprint,
)
from .series_catalog import SeriesCatalog, SeriesCatalogError
from .decision_state import DecisionStateError, DecisionStateStore
from .provider_link_catalog import (
    ProviderLinkCatalog,
    ProviderLinkCatalogError,
    materialize_provider_link_snapshot,
    season_edges_from_snapshot,
)
from .provider_link_transaction import anchor_binding_blocks_execution
from .catalog_binding import (
    CatalogBinding,
    binding_from_decision,
    binding_from_plan,
)
from .asset_collision import (
    AssetCollisionError,
    build_reuse_resolution,
    fingerprint_file,
    validate_reuse_resolution,
)
from .recovery_transaction import (
    RecoveryTransactionError,
    RecoveryTransactionStore,
    transaction_blocks_execution,
)
from .utils import is_font_archive
from .font_archive_contract import (
    FontArchiveContractError,
    is_movie_plan,
    is_supported_font_archive,
    validate_movie_font_manifest,
)
from .work_intent import (
    parse_content_key,
    WorkIntentError,
    WORK_FONT_APPEND_POLICY,
    WORK_FONT_CONDITIONAL_ACTION,
    WORK_INTENT_POLICY_VERSION,
)


_UNSET = object()


def _conditional_font_assets(plan: RenamePlan) -> dict[tuple[str, str], Mapping[str, object]]:
    raw = plan.metadata.get("work_font_intent")
    if not isinstance(raw, Mapping) or any((
        raw.get("policy_version") != WORK_INTENT_POLICY_VERSION,
        raw.get("asset_action") != WORK_FONT_CONDITIONAL_ACTION,
        raw.get("append_policy") != WORK_FONT_APPEND_POLICY,
    )):
        return {}
    assets = raw.get("assets")
    if not isinstance(assets, (list, tuple)):
        return {}
    result = {}
    for item in assets:
        if not isinstance(item, Mapping):
            return {}
        pair = (
            os.path.normcase(os.path.abspath(str(item.get("source_path") or ""))),
            os.path.normcase(os.path.abspath(str(item.get("target_path") or ""))),
        )
        if (
            not all(pair)
            or item.get("action") != WORK_FONT_CONDITIONAL_ACTION
        ):
            return {}
        try:
            parse_content_key(item.get("content_key"))
        except WorkIntentError:
            return {}
        result[pair] = item
    return result


def _conditional_asset_for_entry(
    plan: RenamePlan,
    entry,
) -> Optional[Mapping[str, object]]:
    return _conditional_font_assets(plan).get((
        os.path.normcase(os.path.abspath(entry.source_path)),
        os.path.normcase(os.path.abspath(entry.target_path)),
    ))


class PlanExecutionError(RuntimeError):
    """The approved plan could not be trusted or safely executed."""


class CatalogExecutionFailure(PlanExecutionError):
    """A structured pre-write catalog failure suitable for durable recovery."""

    def __init__(self, message: str, *, code: str, binding: CatalogBinding):
        super().__init__(message)
        self.code = code
        self.binding = binding


@dataclass(frozen=True)
class PlanExecutionResult:
    task_id: str
    plan_fingerprint: str
    ledger_path: Path
    ledger_status: str


TransferFactory = Callable[..., Trans]
CatalogDriftPlanner = Callable[[object, RenamePlan], RenamePlan]


class PlanExecutionService:
    """Validate approval and Plan version immediately before invoking Trans."""

    def __init__(
        self,
        plan_store: PlanStore,
        review_service: PlanReviewService,
        *,
        task_root: Path = TASK_PATH,
        record_root: Path = RECORD_PATH,
        transfer_factory: TransferFactory = Trans,
        catalog_root: Path = WORK_CATALOG_PATH,
        series_catalog_root: Path = SERIES_CATALOG_PATH,
        decision_state_root: Path = DECISION_STATE_PATH,
        recovery_transaction_root: Path = RECOVERY_TRANSACTION_PATH,
        provider_link_catalog_root: Path = PROVIDER_LINK_CATALOG_PATH,
        catalog_drift_planner: Optional[CatalogDriftPlanner] = None,
    ) -> None:
        self.plan_store = plan_store
        self.review_service = review_service
        self.task_root = Path(task_root)
        self.record_root = Path(record_root)
        self.transfer_factory = transfer_factory
        self.work_catalog = WorkCatalog(catalog_root)
        self.series_catalog = SeriesCatalog(series_catalog_root)
        self.decision_state_store = DecisionStateStore(decision_state_root)
        self.recovery_transaction_store = RecoveryTransactionStore(
            recovery_transaction_root
        )
        self.provider_link_catalog = ProviderLinkCatalog(
            provider_link_catalog_root
        )
        self.catalog_drift_planner = (
            catalog_drift_planner or self.review_service.replan_for_catalog_drift
        )

    def validate_approved(
        self, task_id: str, expected_fingerprint: str
    ) -> RenamePlan:
        """Return the exact approved Plan, or fail before any media operation."""

        try:
            plan = self.plan_store.load(task_id)
        except PlanStoreError as exc:
            raise PlanExecutionError(f"无法读取当前 Plan：{exc}") from exc
        current_fingerprint = self.plan_store.fingerprint(plan)
        if current_fingerprint != expected_fingerprint:
            raise PlanExecutionError("Plan 版本已变化，拒绝执行；请刷新并重新批准")
        self._validate_recovery_transaction_gate(plan)
        self._validate_provider_link_preflight(plan)

        try:
            decision = self.review_service.load(task_id)
        except PlanReviewError as exc:
            raise PlanExecutionError(f"无法读取审核决定：{exc}") from exc
        if decision.status != ReviewStatus.APPROVED:
            raise PlanExecutionError("当前 Plan 尚未全部批准，拒绝执行")
        if decision.plan_fingerprint != current_fingerprint:
            raise PlanExecutionError("审核决定不属于当前 Plan 版本，拒绝执行")

        expected_ids = {entry_id(entry) for entry in plan.entries}
        approved_ids = set(decision.approved_entry_ids)
        if (
            not expected_ids
            or approved_ids != expected_ids
            or len(decision.approved_entry_ids) != len(expected_ids)
        ):
            raise PlanExecutionError("批准条目与当前 Plan 不完整一致，拒绝执行")

        verification = PlanVerifier().verify(plan)
        if verification.status == PlanStatus.INVALID or verification.issues:
            codes = ", ".join(issue.code for issue in verification.issues)
            raise PlanExecutionError(f"最终 Verifier 未通过，拒绝执行：{codes}")
        if verification.status not in {
            PlanStatus.ACCEPTED,
            PlanStatus.NEED_CONFIRM,
        }:
            raise PlanExecutionError(
                f"最终 Verifier 返回不可执行状态：{verification.status.value}"
            )
        return plan

    def _validate_recovery_transaction_gate(self, plan: RenamePlan) -> None:
        """Block before media writes when detach evidence is incomplete."""

        if plan.metadata.get("recovery_reason") == "catalog_detached":
            raise PlanExecutionError(
                "catalog_detached_replan_required：作品登记已解除，"
                "必须重新生成整理计划"
            )

        try:
            state = self.decision_state_store.load_optional(plan.task_id)
        except DecisionStateError as exc:
            raise PlanExecutionError(f"DecisionState 不可信，拒绝执行：{exc}") from exc
        try:
            transaction = self.recovery_transaction_store.load_optional(
                plan.task_id
            )
        except RecoveryTransactionError as exc:
            raise PlanExecutionError(f"恢复事务记录损坏，拒绝执行：{exc}") from exc

        if state is not None and state.recovery_state == "catalog_detach_pending":
            if transaction is None:
                raise PlanExecutionError(
                    "Catalog detach 事务记录缺失；必须先执行人工对账恢复"
                )
            if transaction.phase == "completed":
                raise PlanExecutionError(
                    "Catalog detach 事务与 DecisionState 状态不一致；拒绝执行"
                )

        current_decision_fingerprint = (
            DecisionStateStore.fingerprint(state) if state is not None else ""
        )
        plan_decision_fingerprint = str(
            plan.metadata.get("decision_state_fingerprint") or ""
        )
        if transaction_blocks_execution(
            transaction,
            current_decision_fingerprint=current_decision_fingerprint,
            plan_decision_fingerprint=plan_decision_fingerprint,
        ):
            raise PlanExecutionError(
                "存在未完成的 Catalog detach 事务，或 DecisionState 与 Plan "
                "fingerprint 不一致；拒绝执行"
            )

    def _validate_provider_link_preflight(self, plan: RenamePlan) -> None:
        def reject() -> None:
            raise PlanExecutionError(
                "作品季度登记信息不完整或已变化，请重新生成并批准整理计划"
            )

        if tv_authority_issue(plan) is not None:
            reject()
        snapshot = plan.metadata.get("provider_link_snapshot")
        if not isinstance(snapshot, Mapping):
            return

        level = str(snapshot.get("automation_level") or "")
        series_key = str(snapshot.get("series_key") or "")
        mapping_id = str(snapshot.get("mapping_id") or "")
        evidence_fingerprint = str(snapshot.get("evidence_fingerprint") or "")
        season_number = plan.metadata.get("season_number")
        season_binding_id = str(plan.metadata.get("season_binding_id") or "")
        canonical_root = str(plan.metadata.get("canonical_work_root") or "")
        catalog_revision = plan.metadata.get("series_catalog_revision")
        policy = plan.execution_policy
        catalog_binding = binding_from_plan(plan)
        if (
            level not in {"L0", "L1", "L2", "L3"}
            or not series_key
            or not mapping_id
            or not evidence_fingerprint
            or str(plan.metadata.get("series_key") or "") != series_key
            or not season_binding_id
            or not isinstance(season_number, int)
            or not canonical_root
            or not isinstance(catalog_revision, int)
            or catalog_revision < 0
            or policy is None
            or _path_key(policy.target_root) != _path_key(Path(canonical_root))
            or _path_key(Path(str(plan.metadata.get("target_root") or "")))
            != _path_key(Path(canonical_root))
            or catalog_binding is None
            or catalog_binding.backend != "series"
            or catalog_binding.key != series_key
            or catalog_binding.revision != catalog_revision
            or not any(group.kind == "tv_episodes" for group in plan.presentation_groups)
        ):
            reject()

        try:
            season_edges = season_edges_from_snapshot(snapshot)
        except ProviderLinkCatalogError:
            reject()
        if not any(
            edge.get("season_number") == season_number for edge in season_edges
        ):
            reject()
        if (
            int(snapshot.get("schema_version") or 1) >= 2
            and snapshot.get("mapping_shape") == "many_subjects_one_season"
        ):
            self._validate_source_projection_authority(plan, snapshot, season_edges)
        try:
            state = self.decision_state_store.load_optional(plan.task_id)
        except DecisionStateError as exc:
            raise PlanExecutionError(
                "作品季度登记信息无法读取，请重新生成整理计划"
            ) from exc
        if state is None:
            reject()
        series_binding = state.series_binding
        season_binding = state.season_binding
        provider_binding = state.provider_link_binding
        if (
            not isinstance(series_binding, Mapping)
            or str(series_binding.get("series_key") or "") != series_key
            or str(series_binding.get("canonical_root") or "") != canonical_root
            or series_binding.get("catalog_revision") != catalog_revision
            or not isinstance(season_binding, Mapping)
            or str(season_binding.get("season_binding_id") or "")
            != season_binding_id
            or season_binding.get("season_number") != season_number
            or not isinstance(provider_binding, Mapping)
            or str(provider_binding.get("mapping_id") or "") != mapping_id
            or str(provider_binding.get("series_key") or "") != series_key
            or str(provider_binding.get("evidence_fingerprint") or "")
            != evidence_fingerprint
            or str(plan.metadata.get("decision_state_fingerprint") or "")
            != DecisionStateStore.fingerprint(state)
        ):
            reject()

        series_path = self.series_catalog.root / f"{series_key}.json"
        if level in {"L2", "L3"}:
            if catalog_revision != 0 or series_path.exists():
                reject()
        else:
            try:
                current_series = self.series_catalog.load(series_key)
            except SeriesCatalogError as exc:
                raise PlanExecutionError(
                    "已保存的作品季度登记不可用，请重新匹配作品"
                ) from exc
            if (
                current_series.revision != catalog_revision
                or _path_key(current_series.canonical_root)
                != _path_key(Path(canonical_root))
            ):
                reject()

        anchor = snapshot.get("anchor")
        if level in {"L2", "L3"} and anchor is None:
            return
        if not isinstance(anchor, Mapping):
            reject()
        mapping_id = str(anchor.get("mapping_id") or "")
        if not mapping_id:
            reject()
        try:
            current = self.provider_link_catalog.load(mapping_id)
        except ProviderLinkCatalogError as exc:
            raise PlanExecutionError(
                "已保存的作品季度关系不可用，请重新匹配作品"
            ) from exc
        if anchor_binding_blocks_execution(snapshot, current):
            raise PlanExecutionError(
                "作品季度关系已变化，请重新生成并批准整理计划"
            )

    def execute(
        self, task_id: str, expected_fingerprint: str
    ) -> PlanExecutionResult:
        """Execute once after a second, immediate version/approval verification."""

        with self._execution_lock(task_id, expected_fingerprint):
            try:
                plan = self.plan_store.load(task_id)
            except PlanStoreError as exc:
                raise PlanExecutionError(f"无法读取当前 Plan：{exc}") from exc
            drift_result = self._replan_if_catalog_binding_drift(
                plan, expected_fingerprint
            )
            if drift_result is not None:
                return drift_result
            binding = binding_from_plan(plan)
            if binding is None:
                return self._execute_locked(task_id, expected_fingerprint)
            if binding.backend == "series":
                try:
                    with self.series_catalog.lock(binding.key):
                        return self._execute_locked(
                            task_id,
                            expected_fingerprint,
                            series_catalog_lock_held=True,
                        )
                except SeriesCatalogError as exc:
                    raise PlanExecutionError(str(exc)) from exc
            work_key = binding.key
            try:
                with self.work_catalog.lock(
                    work_key,
                    wait_seconds=(
                        5.0 if plan.metadata.get("work_font_intent") is not None else 0.0
                    ),
                ):
                    stale_reason = self._movie_plan_stale_reason(plan, work_key)
                    if stale_reason is not None:
                        return self._rebase_for_reapproval(
                            plan, expected_fingerprint, stale_reason
                        )
                    self._validate_catalog_preflight(plan, work_key)
                    return self._execute_locked(
                        task_id, expected_fingerprint, catalog_lock_held=True
                    )
            except CatalogExecutionFailure as exc:
                self._persist_execution_failure(plan.task_id, exc)
                raise
            except WorkCatalogError as exc:
                raise PlanExecutionError(str(exc)) from exc

    def _execute_locked(
        self,
        task_id: str,
        expected_fingerprint: str,
        *,
        catalog_lock_held: bool = False,
        series_catalog_lock_held: bool = False,
    ) -> PlanExecutionResult:
        task = self._read_task(task_id)
        if task.get("preview_only"):
            raise PlanExecutionError("测试预览任务禁止执行")
        if (
            task.get("execution_status") == "completed"
            and task.get("execution_fingerprint") == expected_fingerprint
        ):
            raise PlanExecutionError("该 Plan 版本已经执行完成，拒绝重复执行")
        status = task.get("workflow_status")
        queued_execution = (
            status in {"queued", "running"}
            and task.get("queue_operation") == "execute_approved"
            and task.get("execution_requested_fingerprint")
            == expected_fingerprint
        )
        if status != "approved" and not queued_execution:
            raise PlanExecutionError("任务状态与批准执行请求不一致，拒绝执行")
        approved_plan = self.validate_approved(task_id, expected_fingerprint)
        if approved_plan.execution_policy is None:
            raise PlanExecutionError("Plan 缺少不可变执行策略，拒绝执行")
        self._validate_runtime_safety_gate(approved_plan)

        transition_guards = {"_allowed_statuses": {status}}
        if queued_execution:
            transition_guards.update(
                {
                    "_expected_queue_operation": "execute_approved",
                    "_expected_requested_fingerprint": expected_fingerprint,
                }
            )
        self._update_task(
            task_id,
            **transition_guards,
            workflow_status="executing",
            execution_status="executing",
            execution_phase="preparing",
            execution_fingerprint=expected_fingerprint,
            execution_started_at=_utc_now(),
            execution_completed_at=None,
            execution_requires_manual_check=False,
            execution_may_have_written_targets=False,
            execution_recovery_message=None,
            plan_rebase_message=None,
            error=None,
        )

        try:
            ledger_path = safe_task_path(
                self.record_root, task_id, suffix=".ledger.json"
            )
        except ValueError as exc:
            raise PlanExecutionError(f"不安全的任务 ID：{task_id!r}") from exc
        transfer_started = False
        try:
            # This second read is deliberately adjacent to Trans: a Plan or
            # review change after queueing must still fail before media writes.
            plan = self.validate_approved(task_id, expected_fingerprint)
            policy = plan.execution_policy
            if policy is None:
                raise PlanExecutionError("Plan 缺少不可变执行策略，拒绝执行")
            self._validate_runtime_safety_gate(plan)
            operations = self._build_execution_operations(plan)
            self._validate_execution_paths(plan, operations=operations)
            transfer_kwargs = {
                "mode": policy.mode,
                "dry_run": policy.dry_run,
                "record_root": self.record_root,
            }
            if any(item["action"] == "reuse_existing" for item in operations):
                transfer_kwargs["operations"] = operations
            self._update_task(
                task_id,
                _allowed_statuses={"executing"},
                _expected_queue_operation=(
                    "execute_approved" if queued_execution else _UNSET
                ),
                _expected_requested_fingerprint=(
                    expected_fingerprint if queued_execution else _UNSET
                ),
                _expected_execution_phase="preparing",
                _expected_execution_fingerprint=expected_fingerprint,
                execution_phase="media_boundary_entered",
                execution_requires_manual_check=True,
                execution_may_have_written_targets=True,
            )
            transfer_started = True
            transfer = self.transfer_factory(
                plan.as_mapping(), task_id, expected_fingerprint, **transfer_kwargs
            )
            # From this point onward an exception cannot prove that no target
            # was created. Persist an explicit reconciliation requirement
            # instead of leaving operators to infer it from a generic failure.
            transfer_error = transfer.trans_file()
            ledger = self._load_ledger(
                ledger_path, task_id, expected_fingerprint, plan
            )
            ledger_status = str(ledger.get("status"))
            if transfer_error:
                raise PlanExecutionError(str(transfer_error))
            if ledger_status not in {"completed", "dry_run"}:
                raise PlanExecutionError(
                    f"执行 ledger 未进入可信终态：{ledger_status}"
                )
        except Exception as exc:
            message = str(exc)
            recovery_message = (
                "执行已进入文件操作边界，目标目录可能已有完整或部分结果；"
                "请先核对本次整理记录与目标文件，再决定重试或清理。"
                if transfer_started
                else None
            )
            self._update_task(
                task_id,
                workflow_status="failed",
                execution_status="failed",
                execution_ledger=str(ledger_path),
                execution_completed_at=_utc_now(),
                execution_phase=(
                    "media_boundary_entered"
                    if transfer_started
                    else "failed_before_media"
                ),
                execution_requires_manual_check=transfer_started,
                execution_may_have_written_targets=transfer_started,
                execution_recovery_message=recovery_message,
                error=f"[批准计划执行] {message}",
            )
            if isinstance(exc, PlanExecutionError):
                raise
            raise PlanExecutionError(message) from exc

        if ledger_status == "dry_run":
            self._update_task(
                task_id,
                workflow_status="approved",
                execution_status="dry_run",
                execution_ledger=str(ledger_path),
                execution_completed_at=_utc_now(),
                execution_phase="completed",
                completed_at=None,
                execution_requires_manual_check=False,
                execution_may_have_written_targets=False,
                execution_recovery_message=None,
                error=None,
            )
        else:
            try:
                is_tv = any(
                    group.kind == "tv_episodes"
                    for group in plan.presentation_groups
                )
                if is_tv and plan.metadata.get("series_key"):
                    if not series_catalog_lock_held:
                        raise SeriesCatalogError("SeriesCatalog 执行锁未持有")
                    self.series_catalog.register_completed(
                        plan,
                        expected_revision=int(
                            plan.metadata.get("series_catalog_revision") or 0
                        ),
                        completed_ledger=ledger,
                    )
                elif self._catalog_work_key(plan) is not None:
                    self.work_catalog.register_completed(
                        plan,
                        ledger,
                        expected_revision=int(
                            plan.metadata.get("catalog_revision") or 0
                        ),
                        lock_held=catalog_lock_held,
                    )
                self._materialize_provider_link(plan)
            except (
                SeriesCatalogError, WorkCatalogError, ProviderLinkCatalogError,
                TypeError, ValueError,
            ) as exc:
                recovery_message = (
                    "硬链接已完成，但作品登记尚未更新。"
                    "请使用登记恢复功能按本次整理记录重建；修复前禁止向此作品追加版本。"
                )
                self._update_task(
                    task_id,
                    workflow_status="catalog_update_pending",
                    execution_status="catalog_update_pending",
                    execution_ledger=str(ledger_path),
                    execution_completed_at=_utc_now(),
                    execution_phase="media_boundary_entered",
                    execution_requires_manual_check=True,
                    execution_may_have_written_targets=True,
                    execution_recovery_message=recovery_message,
                    error=f"[作品登记] {exc}",
                )
                raise PlanExecutionError(recovery_message) from exc
            completed_at = _utc_now()
            self._clear_execution_failure(plan.task_id)
            self._update_task(
                task_id,
                workflow_status="completed",
                execution_status="completed",
                execution_ledger=str(ledger_path),
                execution_completed_at=completed_at,
                execution_phase="completed",
                completed_at=completed_at,
                execution_requires_manual_check=False,
                execution_may_have_written_targets=False,
                execution_recovery_message=None,
                error=None,
            )
        return PlanExecutionResult(
            task_id=task_id,
            plan_fingerprint=expected_fingerprint,
            ledger_path=ledger_path,
            ledger_status=ledger_status,
        )

    def _materialize_provider_link(self, plan: RenamePlan) -> None:
        snapshot = plan.metadata.get("provider_link_snapshot")
        if not isinstance(snapshot, dict) and not hasattr(snapshot, "get"):
            return
        level = str(snapshot.get("automation_level") or "")
        if level == "L0":
            return
        materialize_provider_link_snapshot(self.provider_link_catalog, snapshot)

    @staticmethod
    def _validate_source_projection_authority(
        plan: RenamePlan,
        snapshot: Mapping[str, object],
        season_edges: tuple[Mapping[str, object], ...],
    ) -> None:
        projection = plan.metadata.get("source_episode_projection")
        if not isinstance(projection, Mapping):
            raise PlanExecutionError("分集投影信息缺失，请重新生成整理计划")
        selected_subject = str(snapshot.get("selected_subject_id") or "")
        selected = [
            edge for edge in season_edges
            if str(edge.get("provider_id") or "") == selected_subject
        ]
        if len(selected) != 1:
            raise PlanExecutionError("所选作品的分集范围不唯一")
        selected_edge = selected[0]
        if (
            projection.get("target_episode_start") != selected_edge.get("episode_start")
            or projection.get("target_episode_end") != selected_edge.get("episode_end")
            or projection.get("inventory_scope") != "complete"
        ):
            raise PlanExecutionError("分集投影与所选作品范围不一致")
        rows = projection.get("episode_mapping")
        if not isinstance(rows, (list, tuple)):
            raise PlanExecutionError("分集投影条目无效")
        video_sources = {
            _path_key(entry.source_path)
            for entry in plan.entries if entry.media_kind == "video"
        }
        row_sources = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise PlanExecutionError("分集投影条目损坏")
            source_path = str(row.get("source_path") or "")
            source_episode = row.get("source_episode")
            target_episode = row.get("target_episode")
            if (
                not source_path
                or not isinstance(source_episode, int)
                or not isinstance(target_episode, int)
                or target_episode < int(selected_edge["episode_start"])
                or target_episode > int(selected_edge["episode_end"])
            ):
                raise PlanExecutionError("分集投影条目超出批准范围")
            key = _path_key(Path(source_path))
            if key in row_sources:
                raise PlanExecutionError("分集投影包含重复源文件")
            row_sources.add(key)
            for field in (
                "source_size", "source_mtime_ns", "source_device", "source_inode"
            ):
                value = row.get(field)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise PlanExecutionError("分集投影缺少源文件身份")
        if row_sources != video_sources:
            raise PlanExecutionError("分集投影未完整覆盖主视频")

    @staticmethod
    def _validate_source_projection_identity(plan: RenamePlan) -> None:
        snapshot = plan.metadata.get("provider_link_snapshot")
        if (
            not isinstance(snapshot, Mapping)
            or int(snapshot.get("schema_version") or 1) < 2
            or snapshot.get("mapping_shape") != "many_subjects_one_season"
        ):
            return
        projection = plan.metadata.get("source_episode_projection")
        if not isinstance(projection, Mapping):
            raise PlanExecutionError("分集投影信息缺失，请重新生成整理计划")
        rows = projection.get("episode_mapping")
        if not isinstance(rows, (list, tuple)):
            raise PlanExecutionError("分集投影条目无效")
        for row in rows:
            if not isinstance(row, Mapping):
                raise PlanExecutionError("分集投影条目损坏")
            source = Path(str(row.get("source_path") or ""))
            try:
                stat = source.stat()
            except OSError as exc:
                raise PlanExecutionError(f"源文件无法读取：{source}") from exc
            expected = (
                row.get("source_size"), row.get("source_mtime_ns"),
                row.get("source_device"), row.get("source_inode"),
            )
            current = (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)
            if current != expected:
                raise PlanExecutionError(
                    f"源文件在批准后发生变化，请重新生成整理计划：{source}"
                )

    @staticmethod
    def _catalog_work_key(plan: RenamePlan) -> Optional[str]:
        binding = binding_from_plan(plan)
        return binding.key if binding is not None and binding.backend == "work" else None

    def _conditional_catalog_matches(
        self,
        plan: RenamePlan,
        work_key: str,
        record: Mapping[str, object],
    ) -> bool:
        raw = plan.metadata.get("work_font_intent")
        assets = _conditional_font_assets(plan)
        if (
            not isinstance(raw, Mapping)
            or not assets
            or raw.get("work_key") != work_key
            or not self.work_catalog.compatible_base(record, plan)
        ):
            return False
        frozen = record.get("work_intent")
        compared = (
            "schema_version", "policy_version", "work_key", "group_kind",
            "content_revision", "primary_content_key",
            "primary_decision_source", "snapshot_fingerprint", "append_policy",
        )
        if (
            not isinstance(frozen, Mapping)
            or any(frozen.get(key) != raw.get(key) for key in compared)
            or tuple(frozen.get("content_keys", ()))
            != tuple(raw.get("content_keys", ()))
        ):
            return False
        root = Path(str(record.get("canonical_root") or ""))
        if not root.is_dir():
            return False
        for row in tuple(record.get("versions", ())) + tuple(record.get("assets", ())):
            if not isinstance(row, Mapping) or not Path(
                str(row.get("target") or "")
            ).is_file():
                return False
        registered_assets = tuple(
            item for item in record.get("assets", ())
            if isinstance(item, Mapping)
        )
        for item in assets.values():
            try:
                size, digest = parse_content_key(item.get("content_key"))
            except WorkIntentError:
                return False
            target = Path(str(item.get("target_path") or ""))
            matches = [
                row for row in registered_assets
                if _path_key(Path(str(row.get("target") or ""))) == _path_key(target)
                and row.get("content_key") == f"sha256:{size}:{digest}"
                and row.get("size") == size
                and str(row.get("sha256") or "").casefold() == digest
            ]
            if len(matches) != 1 or not target.is_file():
                return False
            try:
                evidence = fingerprint_file(target)
            except AssetCollisionError:
                return False
            if evidence.size != size or evidence.sha256 != digest:
                return False
        return True

    def _build_execution_operations(
        self,
        plan: RenamePlan,
    ) -> list[dict[str, object]]:
        """Resolve only approved conditional font actions under the work lock."""

        work_key = self._catalog_work_key(plan)
        record = None
        if work_key and (self.work_catalog.root / f"{work_key}.json").is_file():
            record = self.work_catalog.load(work_key)
        operations = []
        for entry in plan.entries:
            if entry.collision_resolution is not None:
                operations.append({
                    "source": entry.source_path,
                    "target": entry.target_path,
                    "action": entry.collision_resolution.action,
                    "collision_resolution": entry.collision_resolution.to_dict(),
                })
                continue
            intent_asset = _conditional_asset_for_entry(plan, entry)
            if intent_asset is None or not os.path.lexists(entry.target_path):
                operations.append({
                    "source": entry.source_path,
                    "target": entry.target_path,
                    "action": "create",
                    "collision_resolution": None,
                })
                continue
            if (
                work_key is None
                or record is None
                or not self._conditional_catalog_matches(plan, work_key, record)
            ):
                raise PlanExecutionError(
                    "电影字体目标虽已存在，但没有匹配的已完成作品登记"
                )
            try:
                collision = build_reuse_resolution(
                    entry.source_path, entry.target_path
                )
                size, digest = parse_content_key(intent_asset.get("content_key"))
            except (AssetCollisionError, WorkIntentError) as exc:
                raise PlanExecutionError(str(exc)) from exc
            if (
                collision.source_size != size
                or collision.source_sha256 != digest
            ):
                raise PlanExecutionError("电影字体内容与批准的 work_intent 不一致")
            operations.append({
                "source": entry.source_path,
                "target": entry.target_path,
                "action": "reuse_existing",
                "collision_resolution": collision.to_dict(),
                "conditional_authority": {
                    "policy": WORK_FONT_APPEND_POLICY,
                    "work_key": work_key,
                    "catalog_schema_version": record.get("schema_version"),
                    "catalog_revision": record.get("revision"),
                    "catalog_fingerprint": catalog_record_fingerprint(record),
                    "content_key": intent_asset.get("content_key"),
                    "target_path": str(entry.target_path),
                },
            })
        return operations

    def _validate_catalog_preflight(self, plan: RenamePlan, work_key: str) -> None:
        catalog_key = plan.metadata.get("catalog_work_key")
        path = self.work_catalog.root / f"{work_key}.json"
        if catalog_key:
            try:
                record = self.work_catalog.load(work_key)
            except WorkCatalogError as exc:
                raise PlanExecutionError(f"既有作品登记不可用：{exc}") from exc
            if int(record.get("revision", -1)) != int(
                plan.metadata.get("catalog_revision") or 0
            ):
                raise PlanExecutionError("既有作品登记已变化，请重新生成整理计划")
            if _path_key(Path(str(record.get("canonical_root")))) != _path_key(
                Path(str(plan.metadata.get("canonical_work_root")))
            ):
                raise PlanExecutionError("作品登记目录与已批准整理计划不一致")
            if not Path(str(record.get("canonical_root"))).is_dir():
                raise CatalogExecutionFailure(
                    "作品登记目录不存在；请点击“处理作品登记”，选择“原目录已移动”"
                    "或“原目录已删除”",
                    code="catalog_root_missing",
                    binding=CatalogBinding(
                        "work", work_key, int(record.get("revision") or 0)
                    ),
                )
            missing = [
                str(item.get("target"))
                for item in record.get("versions", [])
                if not Path(str(item.get("target"))).is_file()
            ]
            missing.extend(
                str(item.get("target"))
                for item in record.get("assets", [])
                if not Path(str(item.get("target"))).is_file()
            )
            if missing:
                raise PlanExecutionError(
                    f"作品登记已有 {len(missing)} 个文件缺失，禁止继续追加"
                )
        elif path.exists():
            try:
                record = self.work_catalog.load(work_key)
            except WorkCatalogError as exc:
                raise PlanExecutionError(f"既有作品登记不可用：{exc}") from exc
            if not self._conditional_catalog_matches(plan, work_key, record):
                raise PlanExecutionError("拟创建的本地作品键已存在，请重新生成整理计划")

    def _replan_if_catalog_binding_drift(
        self, plan: RenamePlan, expected_fingerprint: str
    ) -> Optional[PlanExecutionResult]:
        try:
            decision = self.decision_state_store.load_optional(plan.task_id)
        except DecisionStateError as exc:
            raise PlanExecutionError(f"DecisionState 不可信，拒绝执行：{exc}") from exc
        if decision is None:
            return None
        projected = binding_from_plan(plan)
        current = binding_from_decision(decision)
        if projected is None or current is None or projected == current:
            return None
        try:
            revised = self.catalog_drift_planner(decision, plan)
            if revised.task_id != plan.task_id:
                raise PlanReviewError("登记漂移重规划返回了其他任务")
            revised = self.decision_state_store.project_plan(decision, revised)
            revised = replace(
                revised,
                revision_parent_fingerprint=expected_fingerprint,
                revision_action="catalog_binding_drift_replan",
                revision_reason="执行前登记投影与当前 DecisionState 不一致",
            )
            self.review_service.persist_unapproved_revision(revised)
        except (DecisionStateError, PlanReviewError, TypeError, ValueError) as exc:
            raise PlanExecutionError(f"登记身份已变化且无法安全重规划：{exc}") from exc
        new_fingerprint = self.plan_store.fingerprint(revised)
        verification = PlanVerifier().verify(revised)
        self._update_task(
            plan.task_id,
            workflow_status=(
                "invalid" if verification.status == PlanStatus.INVALID else "need_confirm"
            ),
            plan_status=verification.status.value,
            plan_entry_count=len(revised.entries),
            plan_fingerprint=new_fingerprint,
            execution_status="reapproval_required",
            execution_requested_fingerprint=None,
            execution_completed_at=None,
            execution_requires_manual_check=False,
            execution_may_have_written_targets=False,
            execution_recovery_message=None,
            completed_at=None,
            plan_rebase_message=(
                "作品登记身份在批准后发生变化，系统已按当前决策重新计算目标；"
                "尚未创建、覆盖或删除任何文件。请审核新版整理计划。"
            ),
            error=None,
        )
        return PlanExecutionResult(
            task_id=plan.task_id,
            plan_fingerprint=new_fingerprint,
            ledger_path=self.record_root / f"{plan.task_id}.ledger.json",
            ledger_status="reapproval_required",
        )

    def _persist_execution_failure(
        self, task_id: str, failure: CatalogExecutionFailure
    ) -> None:
        try:
            state = self.decision_state_store.load(task_id)
            payload = {
                "code": failure.code,
                "catalog_backend": failure.binding.backend,
                "catalog_key": failure.binding.key,
            }
            revised = replace(
                state,
                recovery_state=failure.code,
                execution_failure=payload,
            )
            self.decision_state_store.save(
                revised, expected_revision=state.revision
            )
        except DecisionStateError as exc:
            raise PlanExecutionError(
                f"{failure}；且无法持久化结构化恢复身份：{exc}"
            ) from exc

    def _clear_execution_failure(self, task_id: str) -> None:
        try:
            state = self.decision_state_store.load_optional(task_id)
            if state is None or state.execution_failure is None:
                return
            self.decision_state_store.save(
                replace(
                    state,
                    recovery_state="healthy",
                    execution_failure=None,
                ),
                expected_revision=state.revision,
            )
        except DecisionStateError as exc:
            raise PlanExecutionError(f"执行完成但无法清除旧恢复失败：{exc}") from exc

    def _movie_plan_stale_reason(
        self, plan: RenamePlan, work_key: str
    ) -> Optional[str]:
        """Explain catalog/target facts that appeared after Plan approval."""

        catalog_key = plan.metadata.get("catalog_work_key")
        if isinstance(catalog_key, str) and catalog_key:
            try:
                record = self.work_catalog.load(catalog_key)
            except WorkCatalogError:
                return None  # the normal preflight emits the precise hard failure
            if int(record.get("revision", -1)) != int(
                plan.metadata.get("catalog_revision") or 0
            ):
                return "作品登记在排队期间已被另一项任务更新"
        else:
            compatible_sibling = False
            work = plan.work_identity
            if work is not None and work.status == "confirmed":
                try:
                    group_kind = next(iter({group.kind for group in plan.presentation_groups}))
                    exact = self.work_catalog.find_exact(
                        work.primary_provider, group_kind=group_kind
                    )
                    if exact is not None:
                        record = self.work_catalog.load(exact.work_key)
                        compatible_sibling = self._conditional_catalog_matches(
                            plan, work_key, record
                        )
                        if not compatible_sibling:
                            return "同一作品已由另一项任务建立作品登记"
                except WorkCatalogError:
                    return None
            if (
                (self.work_catalog.root / f"{work_key}.json").exists()
                and not compatible_sibling
            ):
                return "本整理计划的作品登记已在排队期间建立"

        if any(
            os.path.lexists(entry.target_path)
            and entry.collision_resolution is None
            and _conditional_asset_for_entry(plan, entry) is None
            for entry in plan.entries
        ):
            return "整理目标在批准后发生变化"
        return None

    def _rebase_for_reapproval(
        self,
        plan: RenamePlan,
        expected_fingerprint: str,
        stale_reason: str,
    ) -> PlanExecutionResult:
        """Create a fresh review revision and stop before the write boundary."""

        reason = f"{stale_reason}；执行前重建作品关联与目标碰撞证据"
        try:
            revised = self.review_service.rebase_movie_plan_to_current_catalog(
                plan.task_id,
                expected_fingerprint,
                reason=reason,
            )
        except PlanReviewError as exc:
            raise PlanExecutionError(f"整理计划已过期且无法自动重建：{exc}") from exc
        verification = PlanVerifier().verify(revised)
        new_fingerprint = self.plan_store.fingerprint(revised)
        workflow_status = (
            "invalid" if verification.status == PlanStatus.INVALID else "need_confirm"
        )
        message = (
            "作品目录在排队期间发生变化，系统已生成新版整理计划；"
            "尚未创建、覆盖或删除任何文件。请审核新版计划后重新批准。"
        )
        self._update_task(
            plan.task_id,
            workflow_status=workflow_status,
            plan_status=verification.status.value,
            plan_entry_count=len(revised.entries),
            plan_fingerprint=new_fingerprint,
            execution_status="reapproval_required",
            execution_requested_fingerprint=None,
            execution_completed_at=None,
            execution_requires_manual_check=False,
            execution_may_have_written_targets=False,
            execution_recovery_message=None,
            completed_at=None,
            plan_rebase_message=message,
            error=None,
        )
        return PlanExecutionResult(
            task_id=plan.task_id,
            plan_fingerprint=new_fingerprint,
            ledger_path=self.record_root / f"{plan.task_id}.ledger.json",
            ledger_status="reapproval_required",
        )

    @contextmanager
    def _execution_lock(self, task_id: str, fingerprint: str):
        """Atomically allow one process to execute one task at a time."""

        try:
            safe_task_path(self.task_root, task_id)
        except ValueError as exc:
            raise PlanExecutionError(f"不安全的任务 ID：{task_id!r}") from exc
        lock_root = self.task_root / ".execution-locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        lock_path = lock_root / f"{task_id}.lock"
        try:
            with advisory_file_lock(
                lock_path,
                owner={
                    "kind": "plan_execution",
                    "task_id": task_id,
                    "plan_fingerprint": fingerprint,
                },
            ):
                yield
        except FileLockBusyError as exc:
            raise PlanExecutionError(
                "该任务已有执行者持有执行锁；拒绝并发执行，请稍后重试"
            ) from exc

    def _conditional_ledger_row_valid(
        self,
        plan: RenamePlan,
        plan_entry,
        row: Mapping[str, object],
    ) -> bool:
        asset = _conditional_asset_for_entry(plan, plan_entry)
        authority = row.get("conditional_authority")
        if not isinstance(asset, Mapping) or not isinstance(authority, Mapping):
            return False
        work_key = self._catalog_work_key(plan)
        try:
            size, digest = parse_content_key(asset.get("content_key"))
            if work_key is None:
                return False
            record = self.work_catalog.load(work_key)
            source = fingerprint_file(plan_entry.source_path)
            target = fingerprint_file(plan_entry.target_path)
        except (WorkIntentError, WorkCatalogError, AssetCollisionError):
            return False
        return all((
            row.get("status") == "reused_identical_asset",
            row.get("action") == "reuse_existing",
            row.get("transfer_type") == "reuse_existing",
            authority.get("policy") == WORK_FONT_APPEND_POLICY,
            authority.get("work_key") == work_key,
            authority.get("catalog_schema_version") == record.get("schema_version"),
            authority.get("catalog_revision") == record.get("revision"),
            authority.get("catalog_fingerprint") == catalog_record_fingerprint(record),
            authority.get("content_key") == asset.get("content_key"),
            _path_key(Path(str(authority.get("target_path") or "")))
            == _path_key(plan_entry.target_path),
            source.size == size,
            source.sha256 == digest,
            target.size == size,
            target.sha256 == digest,
            row.get("source_size") == size,
            row.get("source_sha256") == digest,
            row.get("target_size") == size,
            row.get("target_sha256") == digest,
        ))

    def _load_ledger(
        self,
        path: Path,
        task_id: str,
        expected_fingerprint: str,
        plan: RenamePlan,
    ) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as file:
                ledger = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanExecutionError(f"无法读取执行 ledger：{exc}") from exc
        if not isinstance(ledger, dict):
            raise PlanExecutionError("执行 ledger 根节点不是对象")
        if ledger.get("schema_version") not in {1, 2}:
            raise PlanExecutionError("执行 ledger schema_version 不受支持")
        if ledger.get("task_id") != task_id:
            raise PlanExecutionError("执行 ledger task_id 不匹配")
        if ledger.get("plan_fingerprint") != expected_fingerprint:
            raise PlanExecutionError("执行 ledger Plan fingerprint 不匹配")
        ledger_status = ledger.get("status")
        policy = plan.execution_policy
        if policy is None:
            raise PlanExecutionError("执行 ledger 对应 Plan 缺少执行策略")
        if ledger.get("mode") != policy.mode:
            raise PlanExecutionError("执行 ledger mode 与已批准策略不一致")
        expected_dry_run = ledger_status == "dry_run"
        if ledger.get("dry_run") is not policy.dry_run:
            raise PlanExecutionError("执行 ledger dry_run 与已批准策略不一致")
        if expected_dry_run is not policy.dry_run:
            raise PlanExecutionError("执行 ledger dry_run 与终态不一致")
        raw_entries = ledger.get("entries")
        if not isinstance(raw_entries, list):
            raise PlanExecutionError("执行 ledger entries 不是列表")
        expected_pairs = [
            (_path_key(source), _path_key(target))
            for source, target in plan.as_mapping().items()
        ]
        actual_pairs = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                raise PlanExecutionError("执行 ledger 条目不是对象")
            source = entry.get("source")
            target = entry.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                raise PlanExecutionError("执行 ledger 条目缺少源或目标路径")
            actual_pairs.append((_path_key(Path(source)), _path_key(Path(target))))
        if actual_pairs != expected_pairs:
            raise PlanExecutionError("执行 ledger 映射与已批准 Plan 不一致")
        if expected_dry_run:
            if any(entry.get("status") != "planned" for entry in raw_entries):
                raise PlanExecutionError("执行 ledger 条目状态不是 planned")
        else:
            for item, plan_entry in zip(raw_entries, plan.entries):
                if plan_entry.collision_resolution is not None:
                    expected = "reused_identical_asset"
                elif item.get("status") == "reused_identical_asset":
                    if not self._conditional_ledger_row_valid(
                        plan, plan_entry, item
                    ):
                        raise PlanExecutionError(
                            "执行 ledger 的条件复用权威与已批准作品意图不一致"
                        )
                    continue
                else:
                    expected = "created"
                if item.get("status") != expected:
                    raise PlanExecutionError(
                        "执行 ledger 条目状态与批准的碰撞处置不一致"
                    )
        if not expected_dry_run:
            for entry, plan_entry in zip(raw_entries, plan.entries):
                collision = plan_entry.collision_resolution
                if collision is not None:
                    if (
                        entry.get("transfer_type") != "reuse_existing"
                        or entry.get("source_size") != collision.source_size
                        or entry.get("source_sha256") != collision.source_sha256
                        or entry.get("target_size") != collision.target_size
                        or entry.get("target_sha256") != collision.target_sha256
                    ):
                        raise PlanExecutionError("执行 ledger 缺少可信的同内容复用证明")
                    continue
                if entry.get("status") == "reused_identical_asset":
                    if not self._conditional_ledger_row_valid(
                        plan, plan_entry, entry
                    ):
                        raise PlanExecutionError(
                            "执行 ledger 缺少可信的作品级条件复用证明"
                        )
                    continue
                if entry.get("transfer_type") != "hardlink":
                    raise PlanExecutionError("执行 ledger 不是已批准的 hardlink")
                proof_values = (
                    entry.get("source_device"),
                    entry.get("target_device"),
                    entry.get("source_inode"),
                    entry.get("target_inode"),
                    entry.get("target_link_count"),
                )
                if (
                    any(
                        not isinstance(value, int) or isinstance(value, bool)
                        for value in proof_values
                    )
                    or entry["source_device"] < 0
                    or entry["target_device"] < 0
                    or entry["source_inode"] <= 0
                    or entry["target_inode"] <= 0
                    or entry.get("source_device") != entry.get("target_device")
                    or entry.get("source_inode") != entry.get("target_inode")
                    or entry["target_link_count"] < 2
                ):
                    raise PlanExecutionError("执行 ledger 缺少可信 hardlink inode 证明")
        return ledger

    @staticmethod
    def _validate_runtime_safety_gate(plan: RenamePlan) -> None:
        """Let live controls tighten an approved policy, never weaken it."""

        policy = plan.execution_policy
        if policy is None:
            raise PlanExecutionError("Plan 缺少不可变执行策略，拒绝执行")
        if cm.get_config("mode") != "链接":
            raise PlanExecutionError(
                "运行时重命名模式已不再是链接；拒绝绕过已批准 hardlink 策略"
            )
        if is_dry_run_enabled() and not policy.dry_run:
            raise PlanExecutionError(
                "运行时 BAR_DRY_RUN 已启用；紧急禁写闸门阻断真实执行"
            )

    @staticmethod
    def _validate_execution_paths(
        plan: RenamePlan,
        *,
        operations: Optional[list[Mapping[str, object]]] = None,
    ) -> None:
        """Recheck live filesystem boundaries immediately before Trans."""

        configured_root = plan.metadata.get("target_root")
        if not isinstance(configured_root, str) or not configured_root.strip():
            raise PlanExecutionError("Plan 缺少可信 target_root，拒绝执行")
        target_root = Path(configured_root)
        reparse = first_reparse_component(target_root)
        if reparse is not None:
            raise PlanExecutionError(
                f"目标根目录包含junction/reparse重解析点，拒绝执行：{reparse}"
            )
        policy = plan.execution_policy
        if policy is None or policy.mode != "hardlink" or not policy.preserve_source:
            raise PlanExecutionError("Plan 执行策略不是保留源文件的 hardlink")
        if _path_key(policy.target_root) != _path_key(target_root):
            raise PlanExecutionError("Plan 执行策略 target_root 不匹配")
        PlanExecutionService._validate_frozen_associated_assets(plan)
        PlanExecutionService._validate_source_projection_identity(plan)
        for entry in plan.entries:
            source = entry.source_path
            if not source.exists():
                raise PlanExecutionError(f"源文件不存在，拒绝执行：{source}")
            if not source.is_file():
                raise PlanExecutionError(f"源路径不是普通文件，拒绝执行：{source}")
            if not _resolved_within(source, plan.source_root):
                raise PlanExecutionError(
                    f"源文件真实路径越出 Plan 源根目录：{source}"
                )
        PlanExecutionService._validate_frozen_source_identities(plan)
        frozen_operations = operations or [
            {
                "action": (
                    entry.collision_resolution.action
                    if entry.collision_resolution is not None else "create"
                )
            }
            for entry in plan.entries
        ]
        if len(frozen_operations) != len(plan.entries):
            raise PlanExecutionError("运行时操作数量与已批准 Plan 不一致")
        for entry, operation in zip(plan.entries, frozen_operations):
            source = entry.source_path
            target = entry.target_path
            if not _resolved_within(target, target_root):
                raise PlanExecutionError(
                    f"目标文件真实路径越出 Plan 目标根目录：{target}"
                )
            reuse = operation.get("action") == "reuse_existing"
            collision = entry.collision_resolution
            if reuse and not target.is_file():
                raise PlanExecutionError(f"批准复用的既有目标已不存在：{target}")
            if not reuse and collision is None and os.path.lexists(target):
                raise PlanExecutionError(f"目标已存在，拒绝执行：{target}")

    @staticmethod
    def _validate_frozen_source_identities(plan: RenamePlan) -> None:
        rows = plan.metadata.get("source_identity_manifest")
        if rows is None:
            if plan.metadata.get("projection_authority_required") is True:
                raise PlanExecutionError("Plan 缺少冻结来源物理身份")
            return
        if not isinstance(rows, (list, tuple)):
            raise PlanExecutionError("Plan 冻结来源物理身份格式无效")
        frozen = {
            str(row.get("location_key") or "").casefold(): row
            for row in rows
            if isinstance(row, Mapping)
        }
        for entry in plan.entries:
            source = entry.source_path
            try:
                stat_result = source.stat()
                location_key = canonical_location_key(source)
                physical_key = canonical_physical_key(stat_result)
            except (OSError, PhysicalIdentityUnavailable) as exc:
                raise PlanExecutionError(
                    f"源文件物理身份不可用或已变化：{source}"
                ) from exc
            expected = frozen.get(location_key)
            if expected is None or (
                str(expected.get("physical_key") or "").casefold(),
                int(expected.get("size", -1)),
                int(expected.get("mtime_ns", -1)),
            ) != (
                physical_key.casefold(),
                int(stat_result.st_size),
                int(stat_result.st_mtime_ns),
            ):
                raise PlanExecutionError(
                    f"源文件在批准后发生identity漂移：{source}"
                )

    @staticmethod
    def _validate_frozen_associated_assets(plan: RenamePlan) -> None:
        if is_movie_plan(plan):
            try:
                validate_movie_font_manifest(plan, verify_current=True)
            except FontArchiveContractError as exc:
                raise PlanExecutionError(str(exc)) from exc
            return
        if not any(group.kind == "tv_episodes" for group in plan.presentation_groups):
            return
        manifest = plan.metadata.get("associated_asset_manifest")
        font_pairs = [
            (_path_key(entry.source_path), _path_key(entry.target_path))
            for entry in plan.entries
            if is_font_archive(entry.source_path) or (
                entry.role == "font"
                and entry.media_kind == "associated"
                and is_supported_font_archive(entry.source_path)
            )
        ]
        if len(set(font_pairs)) != len(font_pairs):
            raise PlanExecutionError("Plan 字体条目重复")
        if manifest is None:
            if font_pairs and plan.metadata.get("tv_authority_required") is True:
                raise PlanExecutionError("Plan 缺少完整字体来源清单")
            return
        if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
            raise PlanExecutionError("Plan 字体来源清单格式无效")
        rows = manifest.get("assets")
        if not isinstance(rows, (list, tuple)):
            raise PlanExecutionError("Plan 字体来源清单资产无效")
        entries = {
            (_path_key(entry.source_path), _path_key(entry.target_path)): entry
            for entry in plan.entries
        }
        seen_pairs = set()
        season_number = plan.metadata.get("season_number")
        for row in rows:
            if not isinstance(row, Mapping):
                raise PlanExecutionError("Plan 字体来源清单条目无效")
            source = Path(str(row.get("source_path") or ""))
            target = Path(str(row.get("target_path") or ""))
            size = row.get("source_size")
            digest = str(row.get("source_sha256") or "")
            entry = entries.get((_path_key(source), _path_key(target)))
            pair = (_path_key(source), _path_key(target))
            if (
                row.get("role") != "fonts"
                or row.get("scope") != "series"
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or entry is None
                or row.get("season_number") != season_number
                or pair in seen_pairs
            ):
                raise PlanExecutionError("Plan 字体来源清单与批准条目不一致")
            seen_pairs.add(pair)
            try:
                source_evidence = fingerprint_file(source)
                if entry.collision_resolution is not None:
                    _source, target_evidence = validate_reuse_resolution(
                        source,
                        target,
                        entry.collision_resolution,
                    )
                    if (
                        target_evidence.size != size
                        or target_evidence.sha256 != digest
                    ):
                        raise PlanExecutionError(
                            "批准复用的字体位置已变化，请重新生成整理计划"
                        )
            except AssetCollisionError as exc:
                raise PlanExecutionError(str(exc)) from exc
            if source_evidence.size != size or source_evidence.sha256 != digest:
                raise PlanExecutionError(
                    "字体源文件已变化，请重新生成并批准整理计划"
                )
        if seen_pairs != set(font_pairs):
            raise PlanExecutionError("Plan 字体来源清单未覆盖全部批准字体条目")

    def _read_task(self, task_id: str) -> dict:
        try:
            path = safe_task_path(self.task_root, task_id)
        except ValueError as exc:
            raise PlanExecutionError(f"不安全的任务 ID：{task_id!r}") from exc
        if not path.exists():
            raise PlanExecutionError("任务记录不存在，拒绝执行")
        try:
            with open(path, "r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanExecutionError(f"无法读取任务记录：{exc}") from exc
        if not isinstance(payload, dict) or payload.get("uuid") != task_id:
            raise PlanExecutionError("任务记录损坏或 task_id 不匹配")
        return payload

    def _update_task(
        self,
        task_id: str,
        *,
        _allowed_statuses: Optional[set[str]] = None,
        _expected_queue_operation: object = _UNSET,
        _expected_requested_fingerprint: object = _UNSET,
        _expected_execution_phase: object = _UNSET,
        _expected_execution_fingerprint: object = _UNSET,
        **changes: object,
    ) -> None:
        def update(payload: dict) -> dict:
            if (
                _allowed_statuses is not None
                and str(payload.get("workflow_status")) not in _allowed_statuses
            ):
                raise PlanExecutionError("任务状态已变化，拒绝覆盖执行状态")
            expected_fields = (
                ("queue_operation", _expected_queue_operation),
                (
                    "execution_requested_fingerprint",
                    _expected_requested_fingerprint,
                ),
                ("execution_phase", _expected_execution_phase),
                ("execution_fingerprint", _expected_execution_fingerprint),
            )
            for field, expected in expected_fields:
                if expected is not _UNSET and payload.get(field) != expected:
                    raise PlanExecutionError("批准执行身份已变化，拒绝覆盖任务状态")
            payload.update(changes)
            return payload

        if mutate_task_at(self.task_root, task_id, update) is None:
            raise PlanExecutionError("任务记录不存在，拒绝更新执行状态")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path_key(path: Path) -> str:
    return os.path.normpath(str(path)).casefold()


def _resolved_within(path: Path, root: Path) -> bool:
    candidate = _path_key(path.resolve(strict=False))
    anchor = _path_key(root.resolve(strict=False))
    try:
        return os.path.commonpath((candidate, anchor)) == anchor
    except ValueError:
        return False
