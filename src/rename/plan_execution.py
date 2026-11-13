"""Fail-closed execution gate for a version-bound approved rename plan."""

from __future__ import annotations

from .series_repair import is_repair

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
from .path_safety import first_reparse_component
from .plan_verifier import PlanVerifier, tv_authority_issue
from .target_routing import inspect_plan_target_routing
from .projection_authority import (
    ProjectionAuthorityError,
    validate_plan_projection_receipt,
)
from .rename_plan import PlanStatus, RenamePlan, CollisionResolution
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
from .asset_operation import plain as _plain_asset
from .asset_collision import AssetCollisionError
from .recovery_transaction import (
    RecoveryTransactionError,
    RecoveryTransactionStore,
    transaction_blocks_execution,
)
from .rename_plan import is_movie_plan


_UNSET = object()






class PlanExecutionError(RuntimeError):
    """The approved plan could not be trusted or safely executed."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


DRY_RUN_PLAN_RETRY_CODE = "dry_run_plan_requires_retry"
DRY_RUN_PLAN_RETRY_MESSAGE = (
    "旧预演计划不能执行；请先点击“重试”生成真实整理计划"
)
MISSING_EXECUTION_POLICY_CODE = "execution_policy_missing"


def require_approved_execution_policy(policy) -> None:
    """Reject a Plan that is not eligible for production approved execution.

    The immutable Plan policy is the only authority. Runtime mode may tighten
    this later, but it must never upgrade an already-reviewed preview Plan.
    """

    if policy is None:
        raise PlanExecutionError(
            "整理计划缺少不可变执行策略，拒绝执行；请点击“重试”重新生成",
            code=MISSING_EXECUTION_POLICY_CODE,
        )
    if policy.dry_run:
        raise PlanExecutionError(
            DRY_RUN_PLAN_RETRY_MESSAGE,
            code=DRY_RUN_PLAN_RETRY_CODE,
        )


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
        from .completed_rebuild import CompletedRebuildService
        self.rebuild_service = CompletedRebuildService.from_execution_context(
            plan_store=self.plan_store, task_root=self.task_root, decision_root=self.decision_state_store.root,
            record_root=self.record_root, catalog_root=self.work_catalog.root,
            series_catalog_root=self.series_catalog.root, provider_link_catalog_root=self.provider_link_catalog.root)
        self.work_catalog.rebuild_context = self.rebuild_service.work_context()
        from .asset_actions import AssetActionService
        self.asset_action_service = AssetActionService(plan_store=self.plan_store,
            decision_store=self.decision_state_store, task_root=self.task_root, record_root=self.record_root,
            work_catalog=self.work_catalog, series_catalog=self.series_catalog)
        self.work_catalog.asset_action_context = self.asset_action_service
        self.series_catalog.asset_action_context = self.asset_action_service
        self.review_service.asset_recovery_context = self

    def prepare_asset_rollback(self, task_id, expected_fingerprint):
        from .asset_rollback import prepare
        return prepare(self, task_id, expected_fingerprint)

    def rollback_asset_replacement(self, task_id, expected_fingerprint, *, expected_recovery_fingerprint):
        from .asset_rollback import rollback
        return rollback(self, task_id, expected_fingerprint,
                        expected_recovery_fingerprint=expected_recovery_fingerprint)

    def validate_approved_authority(self, task_id: str, expected_fingerprint: str) -> RenamePlan:
        """Shared fixed approval proof; recovery separately validates physical progress."""
        try:
            plan = self.plan_store.load(task_id)
        except PlanStoreError as exc:
            raise PlanExecutionError(f"无法读取当前 Plan：{exc}") from exc
        current_fingerprint = self.plan_store.fingerprint(plan)
        if current_fingerprint != expected_fingerprint:
            raise PlanExecutionError("Plan 版本已变化，拒绝执行；请刷新并重新批准")
        from .selection_gate import validate_selection_for_plan, require_not_retired
        from ..utils.utils import read_task_at
        try:
            require_not_retired(task_id, self.plan_store.root, task_root=self.task_root,
                                plan_fingerprint=current_fingerprint)
            validate_selection_for_plan(read_task_at(self.task_root, task_id))
        except ValueError as exc:
            raise PlanExecutionError(str(exc)) from exc
        require_approved_execution_policy(plan.execution_policy)
        self._validate_recovery_transaction_gate(plan)
        try:
            validate_plan_projection_receipt(plan)
            state = self.decision_state_store.load(task_id)
        except ProjectionAuthorityError as exc:
            raise PlanExecutionError(f'分集投影凭据无效：{exc}') from exc
        except DecisionStateError as exc:
            raise PlanExecutionError(f'无法读取批准决定：{exc}') from exc
        if (plan.metadata.get('decision_state_fingerprint') != self.decision_state_store.fingerprint(state)
                or plan.metadata.get('decision_state_revision') != state.revision):
            raise PlanExecutionError('资产决定与批准计划不一致')
        from .content_selection import validate_plan_content
        validate_plan_content(plan, state)
        raw = plan.metadata.get('asset_operation')
        if (state.asset_operation.to_dict() if state.asset_operation is not None else None) != _plain_asset(raw):
            raise PlanExecutionError('资产操作与批准决定不一致')
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

        return plan

    def validate_approved(self, task_id: str, expected_fingerprint: str) -> RenamePlan:
        """Validate fixed approval and the still-unwritten physical preconditions."""
        from .content_policy import require_media_ready
        require_media_ready(self.plan_store.load(task_id))
        plan = self.validate_approved_authority(task_id, expected_fingerprint)
        from . import content_selection
        state = self.decision_state_store.load(task_id)
        if state.content_selection is not None:
            from .content_choice import validate_scope
            validate_scope(self, plan, state.content_selection)
            content_selection.validate_omission_manifest(state.content_selection, movie_selection=state.movie_version_selection)
        if self._read_task(task_id).get('asset_rollback_phase'):
            raise PlanExecutionError('该计划已开始回滚，不能重新执行；请完成回滚后重新生成整理计划')
        self.asset_action_service.validate(plan, self.decision_state_store.load(plan.task_id))
        if plan.metadata.get('completed_rebuild_observation') is not None:
            try:
                self.rebuild_service.validate_plan(plan)
            except (ValueError, RuntimeError, OSError, KeyError) as exc:
                raise PlanExecutionError(str(exc)) from exc
        from . import series_registration
        try:
            if series_registration.is_series_plan(plan):
                series_registration.validate_plan_series_registration(plan, self.decision_state_store.load(plan.task_id))
                series_registration.validate_current_task_set(self.plan_store, self.decision_state_store, self.task_root,
                    expected_plan=plan, record_root=self.record_root, series_catalog=self.series_catalog,
                    provider_catalog=self.provider_link_catalog)
                series_registration.validate_current_catalog(plan, self.series_catalog, self.provider_link_catalog)
        except (ValueError, OSError, RuntimeError) as exc:
            raise PlanExecutionError(str(exc), code=getattr(exc, "code", None)) from exc
        self._validate_provider_link_preflight(plan)

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
        from .recovery_transaction import require_no_pending_detach
        require_no_pending_detach(plan_store=self.plan_store, task_root=self.task_root,
            decision_root=self.decision_state_store.root, task_id=plan.task_id)

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
        inventory_routing = (
            plan.metadata.get("mapping_source") == "inventory_smart_completion"
        )
        if inventory_routing:
            _routing, routing_problems = inspect_plan_target_routing(plan)
            if routing_problems:
                codes = ", ".join(problem.code for problem in routing_problems)
                raise PlanExecutionError(f"冻结目标路由无效，拒绝执行：{codes}")
            if (
                plan.execution_policy is None
                or _path_key(plan.execution_policy.target_root)
                != _path_key(Path(str(plan.metadata.get("target_root") or "")))
            ):
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
            or (not inventory_routing and (
                _path_key(policy.target_root) != _path_key(Path(canonical_root))
                or _path_key(Path(str(plan.metadata.get("target_root") or "")))
                != _path_key(Path(canonical_root))
            ))
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
            if (catalog_revision != 0 or series_path.exists()) and not plan.metadata.get('series_registration_authority'):
                reject()
        else:
            try:
                current_series = self.series_catalog.load(series_key)
            except SeriesCatalogError as exc:
                raise PlanExecutionError(
                    "已保存的作品季度登记不可用，请重新匹配作品"
                ) from exc
            if (
                _path_key(current_series.canonical_root)
                != _path_key(Path(canonical_root))
            ):
                reject()
            if current_series.revision != catalog_revision:
                from .series_registration import validate_current_catalog
                validate_current_catalog(plan, self.series_catalog, self.provider_link_catalog)

        anchor = snapshot.get("anchor")
        if level in {"L2", "L3"} and anchor is None:
            from .provider_link_catalog import mapping_materializes_snapshot
            try:
                peers = [item for item in self.provider_link_catalog.list_mappings()
                         if item.local_series_key == series_key or item.mapping_id == mapping_id]
                if peers and (len(peers) != 1 or not mapping_materializes_snapshot(peers[0], snapshot)):
                    reject()
            except ProviderLinkCatalogError as exc:
                raise PlanExecutionError('作品季度关系无法核验，请重新生成计划') from exc
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

        # Reject immutable preview authority before even creating an execution
        # lock sidecar. The same gate is repeated under the lock below.
        try:
            safe_task_path(self.task_root, task_id)
        except ValueError as exc:
            raise PlanExecutionError(f"不安全的任务 ID：{task_id!r}") from exc
        try:
            preflight_plan = self.plan_store.load(task_id)
        except PlanStoreError as exc:
            raise PlanExecutionError(f"无法读取当前 Plan：{exc}") from exc
        from .content_policy import require_media_ready
        require_media_ready(preflight_plan)
        require_approved_execution_policy(preflight_plan.execution_policy)
        with self._execution_lock(task_id, expected_fingerprint):
            try:
                plan = self.plan_store.load(task_id)
            except PlanStoreError as exc:
                raise PlanExecutionError(f"无法读取当前 Plan：{exc}") from exc
            require_approved_execution_policy(plan.execution_policy)
            drift_result = self._replan_if_catalog_binding_drift(
                plan, expected_fingerprint
            )
            if drift_result is not None:
                return drift_result
            binding = binding_from_plan(plan)
            if binding is None:
                result = self._execute_locked(task_id, expected_fingerprint)
            elif binding.backend == "series":
                from .series_registration import CatalogStateChanged, validate_current_catalog
                try:
                    from .catalog_namespace import mutation_scope
                    with mutation_scope(self.series_catalog):
                        with self.series_catalog.lock(binding.key):
                            validate_current_catalog(plan, self.series_catalog, self.provider_link_catalog)
                            self.series_catalog.require_namespace_plan(plan, binding.key)
                        result = self._execute_locked(
                            task_id,
                            expected_fingerprint,
                        )
                except CatalogStateChanged as exc:
                    raise PlanExecutionError(str(exc), code=exc.code) from exc
                except SeriesCatalogError as exc:
                    raise PlanExecutionError(str(exc)) from exc
            else:
                work_key = binding.key
                try:
                    # Repair validates retained physical history under this lock;
                    # 多个登记文件会使此处等待较久。
                    from .catalog_namespace import mutation_scope
                    with mutation_scope(self.work_catalog, wait_seconds=60.0):
                        with self.work_catalog.lock(work_key):
                            stale_reason = self._movie_plan_stale_reason(plan, work_key)
                            if stale_reason is not None:
                                return self._rebase_for_reapproval(
                                    plan, expected_fingerprint, stale_reason
                                )
                            self._validate_catalog_preflight(plan, work_key)
                        result = self._execute_locked(
                            task_id, expected_fingerprint
                        )
                except CatalogExecutionFailure as exc:
                    self._persist_execution_failure(plan.task_id, exc)
                    raise
                except WorkCatalogError as exc:
                    raise PlanExecutionError(str(exc)) from exc
        from .replacement_completion import finish_replacement
        finish_replacement(self, task_id, expected_fingerprint)
        return result

    def _execute_locked(
        self,
        task_id: str,
        expected_fingerprint: str,
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
        # Reject untrusted/stale approval without changing the task. Physical
        # preconditions belong to the guarded preparing phase below, so their
        # rejection records a truthful no-media failure instead of escaping it.
        approved_plan = self.validate_approved_authority(task_id, expected_fingerprint)
        require_approved_execution_policy(approved_plan.execution_policy)
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
            execution_failure_code=None,
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
        transfer = None
        try:
            # This second read is deliberately adjacent to Trans: a Plan or
            # review change after queueing must still fail before media writes.
            plan = self.validate_approved(task_id, expected_fingerprint)
            policy = plan.execution_policy
            require_approved_execution_policy(policy)
            self._validate_runtime_safety_gate(plan)
            binding = binding_from_plan(plan)
            if binding is not None:
                from .catalog_namespace import require_mutation
                require_mutation(self.series_catalog if binding.backend == 'series' else self.work_catalog)
            operations = self._build_execution_operations(plan)
            self._validate_execution_paths(plan, operations=operations)
            from .media_file_identity import frozen_sources
            transfer_kwargs = {
                "source_identities": frozen_sources(plan) or None,
                "mode": policy.mode,
                "dry_run": False,
                "record_root": self.record_root,
            }
            if plan.metadata.get('generated_outputs') is not None:
                from .generated_outputs import approved_outputs
                transfer_kwargs['generated_outputs'] = approved_outputs(plan, expected_fingerprint)
            replacing = (plan.metadata.get('asset_operation') or {}).get('mode') == 'replace' or is_repair(plan.metadata.get('asset_operation'))
            if replacing or any(item["action"] in {"reuse_existing", "replace"} for item in operations):
                transfer_kwargs["operations"] = operations
            if replacing:
                from .asset_replacement import approved_batch
                transfer_kwargs['replacement_authority'] = approved_batch(plan, expected_fingerprint)
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
            if ledger_status != "completed":
                raise PlanExecutionError(
                    f"执行 ledger 未进入可信终态：{ledger_status}"
                )
        except Exception as exc:
            message = str(exc)
            residual = transfer_started and not (type(transfer) is Trans and transfer.media_unchanged)
            recovery_message = (
                "执行已进入文件操作边界，目标目录可能已有完整或部分结果；"
                "请先核对本次整理记录与目标文件，再决定重试或清理。"
                if residual
                else None
            )
            self._update_task(
                task_id,
                _allowed_statuses={"executing"},
                _expected_queue_operation=("execute_approved" if queued_execution else _UNSET),
                _expected_requested_fingerprint=(expected_fingerprint if queued_execution else _UNSET),
                _expected_execution_phase=("media_boundary_entered" if transfer_started else "preparing"),
                _expected_execution_fingerprint=expected_fingerprint,
                workflow_status="failed",
                execution_status="failed",
                execution_ledger=str(ledger_path),
                execution_completed_at=_utc_now(),
                execution_phase=(
                    "media_boundary_entered" if residual else
                    "failed_without_media_changes" if transfer_started else "failed_before_media"
                ),
                execution_requires_manual_check=residual,
                execution_may_have_written_targets=residual,
                execution_recovery_message=recovery_message,
                execution_failure_code=getattr(exc, "code", None),
                error=f"[批准计划执行] {message}",
            )
            if isinstance(exc, PlanExecutionError):
                raise
            raise PlanExecutionError(message) from exc

        if ledger_status != "completed":
            raise PlanExecutionError(f"执行 ledger 未进入可信终态：{ledger_status}")
        else:
            try:
                is_tv = any(
                    group.kind == "tv_episodes"
                    for group in plan.presentation_groups
                )
                from .catalog_namespace import require_mutation
                if is_tv and plan.metadata.get("series_key"):
                    require_mutation(self.series_catalog)
                    with self.series_catalog.lock(plan.metadata['series_key']):
                        self.series_catalog.register_completed(plan,
                            expected_revision=int(plan.metadata.get("series_catalog_revision") or 0),
                            completed_ledger=ledger)
                elif self._catalog_work_key(plan) is not None:
                    require_mutation(self.work_catalog)
                    with self.work_catalog.lock(self._catalog_work_key(plan)):
                        self.work_catalog.register_completed(plan, ledger,
                            expected_revision=int(plan.metadata.get("catalog_revision") or 0),
                            lock_held=True)
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
            from .replacement_completion import media_completion_fields
            self._update_task(
                task_id,
                **media_completion_fields(plan, completed_at),
                execution_status="completed",
                execution_ledger=str(ledger_path),
                execution_completed_at=completed_at,
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
    def _uses_current_source_authority(plan: RenamePlan) -> bool:
        """Recognize frozen package authority independently of the mapping recipe."""
        from .tv_planning_authority import (
            TV_PLANNING_AUTHORITY_POLICY_VERSION,
            TV_PLANNING_AUTHORITY_SCHEMA_VERSION,
        )

        typed_tv = plan.metadata.get("tv_planning_authority_required") is True
        if not typed_tv and plan.metadata.get("mapping_source") != "inventory_smart_completion":
            return False
        try:
            validate_plan_projection_receipt(plan)
        except (ProjectionAuthorityError, TypeError, ValueError) as exc:
            raise PlanExecutionError("分集投影凭据无效，请重新生成整理计划") from exc
        if typed_tv and (
            plan.metadata.get("tv_planning_authority_schema_version")
            != TV_PLANNING_AUTHORITY_SCHEMA_VERSION
            or plan.metadata.get("tv_planning_authority_policy_version")
            != TV_PLANNING_AUTHORITY_POLICY_VERSION
        ):
            raise PlanExecutionError("分集权威版本无效，请重新生成整理计划")
        return True

    @staticmethod
    def _validate_source_projection_authority(
        plan: RenamePlan,
        snapshot: Mapping[str, object],
        season_edges: tuple[Mapping[str, object], ...],
    ) -> None:
        if PlanExecutionService._uses_current_source_authority(plan):
            # Current TV/inventory authority owns the whole package, not merely the
            # selected provider subject's relative episode range. Its frozen
            # receipt/entry ownership is also checked by the final Verifier.
            return
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
    def _catalog_work_key(plan: RenamePlan) -> Optional[str]:
        binding = binding_from_plan(plan)
        return binding.key if binding is not None and binding.backend == "work" else None


    def _build_execution_operations(
        self,
        plan: RenamePlan,
    ) -> list[dict[str, object]]:
        operations = []
        from .asset_operation import AssetOperation
        intent = plan.metadata.get('asset_operation')
        replacements = {row['target']: row for row in AssetOperation.from_mapping(intent).to_dict()['replaces']} if intent else {}
        for entry in plan.entries:
            if str(entry.target_path) in replacements:
                row = replacements[str(entry.target_path)]
                if row['source'] != str(entry.source_path):
                    raise PlanExecutionError('换源来源与批准条目不一致')
                operations.append(dict(source=entry.source_path, target=entry.target_path, action='replace', replacement_evidence=row))
                continue
            if entry.collision_resolution is not None:
                operations.append({
                    "source": entry.source_path,
                    "target": entry.target_path,
                    "action": entry.collision_resolution.action,
                    "collision_resolution": entry.collision_resolution.to_dict(),
                })
                continue
            operations.append(dict(source=entry.source_path, target=entry.target_path,
                                   action='create', collision_resolution=None))
        return operations

    def _validate_catalog_preflight(self, plan: RenamePlan, work_key: str) -> None:
        self.work_catalog.require_namespace_plan(plan, work_key)
        if plan.metadata.get('completed_rebuild_observation') is not None:
            self.rebuild_service.validate_plan(plan)
            return
        from .movie_asset_scope import validate_append
        if validate_append(plan, self.work_catalog):
            return
        if (plan.metadata.get('asset_operation') or {}).get('mode') == 'replace':
            self.asset_action_service.validate(plan, self.decision_state_store.load(plan.task_id))
            return
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

        operation = plan.metadata.get("asset_operation") or {}
        if plan.metadata.get("completed_rebuild_observation") is not None or operation.get("mode") == "replace":
            # Selected-slot predicates validate these operations independently.
            return None
        if operation.get("mode") == "append" and operation.get("movie_scope") is not None:
            from .movie_asset_scope import validate_append
            try:
                validate_append(plan, self.work_catalog)
            except (ValueError, RuntimeError, OSError) as exc:
                raise PlanExecutionError(str(exc)) from exc
            return None
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
            work = plan.work_identity
            if work is not None and work.status == "confirmed":
                try:
                    group_kind = next(iter({group.kind for group in plan.presentation_groups}))
                    exact = self.work_catalog.find_exact(
                        work.primary_provider, group_kind=group_kind
                    )
                    if exact is not None:
                        return "同一作品已由另一项任务建立作品登记"
                except WorkCatalogError:
                    return None
            if (
                (self.work_catalog.root / f"{work_key}.json").exists()
            ):
                return "本整理计划的作品登记已在排队期间建立"

        if any(
            os.path.lexists(entry.target_path)
            and entry.collision_resolution is None
            and not any(row.get('target') == str(entry.target_path)
                        for row in (plan.metadata.get('asset_operation') or {}).get('replaces', ()))
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
        from .file_lock import task_operation_lock
        try:
            with task_operation_lock(self.task_root, task_id,
                    operation='plan_execution', fingerprint=fingerprint):
                yield
        except FileLockBusyError as exc:
            raise PlanExecutionError(
                "该任务已有执行者持有执行锁；拒绝并发执行，请稍后重试"
            ) from exc


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
        if ledger.get("schema_version") not in {1, 2, 3}:
            raise PlanExecutionError("执行 ledger schema_version 不受支持")
        if ledger.get("schema_version") == 3:
            from .asset_operation import AssetOperation
            validate_plan_projection_receipt(plan)
            raw = plan.metadata.get("asset_operation")
            if raw is None or (AssetOperation.from_mapping(raw).to_dict()["mode"] != "replace" and not is_repair(raw)):
                raise PlanExecutionError("换源 ledger 缺少已批准的资产操作权威")
        if ledger.get("task_id") != task_id:
            raise PlanExecutionError("执行 ledger task_id 不匹配")
        if ledger.get("plan_fingerprint") != expected_fingerprint:
            raise PlanExecutionError("执行 ledger Plan fingerprint 不匹配")
        ledger_status = ledger.get("status")
        if ledger_status == 'completed':
            from .generated_outputs import validate_completed
            validate_completed(plan, ledger)
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
        if ledger_status in {'rolled_back', 'rollback_failed', 'failed'}:
            allowed = {'planned', 'created', 'failed', 'rolled_back', 'rollback_failed', 'reused_identical_asset'}
            if any(row.get('status') not in allowed for row in raw_entries):
                raise PlanExecutionError('失败整理记录包含未知文件状态')
            return ledger
        if expected_dry_run:
            if any(entry.get("status") != "planned" for entry in raw_entries):
                raise PlanExecutionError("执行 ledger 条目状态不是 planned")
        else:
            for item, plan_entry in zip(raw_entries, plan.entries):
                if plan_entry.collision_resolution is not None:
                    expected = "reused_identical_asset"
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
                    if collision.evidence_kind == 'hardlink-identity-v1':
                        if (entry.get('evidence_kind') != collision.evidence_kind or any(
                                (entry.get(f'{prefix}_device'), entry.get(f'{prefix}_inode')) !=
                                (collision.target_device, collision.target_inode) for prefix in ('source', 'target'))
                                or not isinstance(entry.get('target_link_count'), int)
                                or entry['target_link_count'] < 2):
                            raise PlanExecutionError('执行记录缺少已批准的同一硬链接实体证明')
                    if (
                        entry.get("transfer_type") != "reuse_existing"
                        or entry.get('evidence_kind') != collision.evidence_kind
                        or entry.get('source_xxh3_128') != collision.source_xxh3_128
                        or entry.get('target_xxh3_128') != collision.target_xxh3_128
                        or entry.get("source_size") != collision.source_size
                        or entry.get("target_size") != collision.target_size
                        or entry.get('target_device') != collision.target_device
                        or entry.get('target_inode') != collision.target_inode
                    ):
                        raise PlanExecutionError("执行 ledger 缺少可信的同内容复用证明")
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
        require_approved_execution_policy(policy)
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
        from .media_file_identity import frozen_sources
        frozen_sources(plan)
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
            replacement = operation.get('action') == 'replace'
            if replacement:
                from .asset_operation import AssetOperation
                from .asset_replacement import ReplacementEvidence
                validate_plan_projection_receipt(plan)
                intent = AssetOperation.from_mapping(plan.metadata.get('asset_operation')).to_dict()
                evidence = ReplacementEvidence.from_dict(operation.get('replacement_evidence'))
                if (evidence.to_dict() not in intent['replaces'] or evidence.source != str(source)
                        or evidence.target != str(target)):
                    raise PlanExecutionError('运行换源操作与批准集合不一致')
                evidence.validate_current()
            collision = entry.collision_resolution
            if reuse and not target.is_file():
                raise PlanExecutionError(f"批准复用的既有目标已不存在：{target}")
            if not reuse and not replacement and collision is None and os.path.lexists(target):
                raise PlanExecutionError(f"目标已存在，拒绝执行：{target}",
                                         code="target_conflict_requires_replan")

    @staticmethod
    def _validate_frozen_source_identities(plan: RenamePlan) -> None:
        from .media_file_identity import frozen_sources, media_identity
        try:
            for source, expected in frozen_sources(plan).items():
                if media_identity(source) != expected:
                    raise ValueError(f'源文件identity漂移：{source}')
        except (ValueError, OSError) as exc:
            raise PlanExecutionError(str(exc)) from exc



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
