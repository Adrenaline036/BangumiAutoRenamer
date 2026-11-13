"""Read-only execution-ledger inspection and explicit operator acknowledgement."""

from __future__ import annotations

from .series_repair import is_repair

import json
import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Tuple

from ..utils.utils import mutate_task_at
from .plan_store import PlanStore, PlanStoreError
from .work_catalog import WorkCatalog, WorkCatalogError
from .series_catalog import SeriesCatalog, SeriesCatalogError
from .catalog_binding import binding_from_plan
from .provider_link_catalog import (
    ProviderLinkCatalog,
    ProviderLinkCatalogError,
    materialize_provider_link_snapshot,
)
from .recovery_service import _series_catalog_materializes_plan



class ExecutionRecoveryError(RuntimeError):
    """A recovery record could not be trusted or acknowledged."""


@dataclass(frozen=True)
class ExecutionRecoverySnapshot:
    task_id: str
    ledger_path: Path
    ledger_present: bool
    ledger_status: str
    plan_fingerprint: str
    entry_count: int
    entry_statuses: Tuple[Tuple[str, int], ...]
    error: str = ""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
ABANDON_EVENT_TYPE = "abandon_catalog_recovery"
ABANDON_CONFIRMATION_POLICY_VERSION = "catalog-update-abandon-v1"
ABANDON_CLEANUP_ALLOWLIST = (
    "current_plan",
    "current_review",
    "active_task",
)
REAPPROVAL_REQUIRED = "reapproval_required"


def requires_execution_replan(payload: Mapping[str, object]) -> bool:
    """A permanent pre-write failure cannot be retried with its old approval."""
    code = payload.get("execution_failure_code")
    legacy = not code and str(payload.get("error") or "").startswith((
        "[任务队列] 目标已存在，拒绝执行：", "[批准计划执行] 目标已存在，拒绝执行："))
    return bool(
        payload.get("workflow_status") == "failed"
        and payload.get("queue_operation") == "execute_approved"
        and payload.get("execution_phase") in {"failed_before_media", "failed_without_media_changes"}
        and payload.get("execution_may_have_written_targets") is False
        and payload.get("execution_requires_manual_check") is False
        and (code in {"target_conflict_requires_replan", "catalog_changed_requires_replan"} or legacy)
    )


def safe_approved_execution_retry(payload: Mapping[str, object]) -> bool:
    return bool(
        payload.get("workflow_status") == "failed"
        and payload.get("queue_operation") == "execute_approved"
        and payload.get("execution_phase") in {"failed_before_media", "failed_without_media_changes"}
        and payload.get("execution_requires_manual_check") is False
        and payload.get("execution_may_have_written_targets") is False
        and payload.get("execution_requested_fingerprint")
        and not requires_execution_replan(payload)
    )


def requires_execution_reapproval(payload: Mapping[str, object]) -> bool:
    """Return whether a checked execution failure must revisit its Plan."""

    fingerprint = str(payload.get("execution_requested_fingerprint") or "")
    return bool(
        payload.get("workflow_status") == "failed"
        and payload.get("queue_operation") == "execute_approved"
        and payload.get("execution_recovery_disposition")
        == REAPPROVAL_REQUIRED
        and bool(payload.get("execution_manual_check_acknowledged_at"))
        and not payload.get("execution_requires_manual_check")
        and payload.get("execution_may_have_written_targets") is not False
        and payload.get("execution_phase") in {None, "media_boundary_entered"}
        and _SHA256.fullmatch(fingerprint) is not None
    )


def load_execution_recovery(
    task_id: str, record_root: Path
) -> ExecutionRecoverySnapshot:
    """Summarize the exact ledger below ``record_root``.

    ``record_root`` must be the directory containing execution ledger files
    (normally ``RECORD_PATH``), not the task-record directory. The ledger path
    is derived only from ``task_id`` and never from a stored path field.
    Schema 1, 2 and 3 share these summary fields; readable metadata does not
    authorize recovery or replace action-specific validation.
    """

    ledger_path = _ledger_path(task_id, record_root)
    if not ledger_path.exists():
        return ExecutionRecoverySnapshot(
            task_id=str(task_id),
            ledger_path=ledger_path,
            ledger_present=False,
            ledger_status="missing",
            plan_fingerprint="",
            entry_count=0,
            entry_statuses=(),
            error="历史执行记录不存在；必须直接核对目标目录。",
        )
    try:
        with open(ledger_path, "r", encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload, Mapping):
            raise ValueError("历史执行记录不是对象")
        if payload.get("schema_version") not in (1, 2, 3):
            raise ValueError("历史执行记录版本不受支持")
        if payload.get("task_id") != str(task_id):
            raise ValueError("历史执行记录与任务不匹配")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise ValueError("历史执行记录条目无效")
        counts: dict[str, int] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError("历史执行记录条目损坏")
            status = str(entry.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        fingerprint = payload.get("plan_fingerprint")
        return ExecutionRecoverySnapshot(
            task_id=str(task_id),
            ledger_path=ledger_path,
            ledger_present=True,
            ledger_status=str(payload.get("status") or "unknown"),
            plan_fingerprint=(
                fingerprint if isinstance(fingerprint, str) else ""
            ),
            entry_count=len(entries),
            entry_statuses=tuple(sorted(counts.items())),
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return ExecutionRecoverySnapshot(
            task_id=str(task_id),
            ledger_path=ledger_path,
            ledger_present=True,
            ledger_status="untrusted",
            plan_fingerprint="",
            entry_count=0,
            entry_statuses=(),
            error=f"历史执行记录无法信任：{exc}",
        )


def acknowledge_execution_recovery(task_id: str, task_root: Path) -> dict:
    """Acknowledge recovery in the task JSON below ``task_root``.

    ``task_root`` must be the directory containing task state files (normally
    ``TASK_PATH``), not the execution-ledger directory. The failed workflow
    state, phase, and historical write-risk are intentionally preserved. The
    only safe next action is an explicit reapproval of the current Plan.
    """

    _task_path(task_id, task_root)
    try:
        def transition(payload: dict) -> dict:
            if payload.get("uuid") != str(task_id):
                raise ValueError("任务记录损坏或身份不匹配")
            if payload.get("workflow_status") != "failed":
                raise ValueError("任务当前不处于可核查的失败状态")
            if payload.get("queue_operation") != "execute_approved":
                raise ValueError("任务不是已批准计划的执行记录")
            if payload.get("execution_status") == "catalog_update_pending":
                raise ValueError("作品登记待恢复任务需使用专用操作")
            if payload.get("execution_recovery_disposition") is not None:
                raise ValueError("本次核查已经确认，请继续重新审核")
            fingerprint = str(
                payload.get("execution_requested_fingerprint") or ""
            )
            if _SHA256.fullmatch(fingerprint) is None:
                raise ValueError("当前整理版本标识无效")
            phase = payload.get("execution_phase")
            if phase == "media_boundary_entered":
                if payload.get("execution_requires_manual_check") is not True:
                    raise ValueError("文件核查状态与执行阶段不一致")
            elif phase is not None:
                raise ValueError("当前执行阶段不能通过此入口确认")
            payload.update(
                {
                    "execution_requires_manual_check": False,
                    "execution_may_have_written_targets": True,
                    "execution_manual_check_acknowledged_at": _utc_now(),
                    "execution_manual_check_acknowledgement": (
                        "操作者已完成目标文件核查"
                    ),
                    "execution_recovery_disposition": REAPPROVAL_REQUIRED,
                }
            )
            return payload

        updated = mutate_task_at(Path(task_root), str(task_id), transition)
        if updated is None:
            raise ValueError("任务记录不存在")
        return updated
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ExecutionRecoveryError(f"无法确认人工核查：{exc}") from exc


def reconcile_catalog_update(
    task_id: str,
    *,
    task_root: Path,
    record_root: Path,
    plan_store: PlanStore,
    work_catalog: WorkCatalog,
    series_catalog: Optional[SeriesCatalog] = None,
    provider_link_catalog: Optional[ProviderLinkCatalog] = None,
    decision_state_root: Optional[Path] = None,
    review_root: Optional[Path] = None,
    recovery_transaction_root: Optional[Path] = None,
) -> dict:
    """Rebuild pending Work/Series facts from one completed trusted ledger."""

    ledger_path = _ledger_path(task_id, record_root)
    try:
        initial_plan = plan_store.load(task_id)
        rebuild_service = None
        asset_recovery = None
        replacing = (initial_plan.metadata.get("asset_operation") or {}).get("mode") == "replace" or is_repair(initial_plan.metadata.get("asset_operation"))
        if replacing:
            if (decision_state_root is None or review_root is None or recovery_transaction_root is None
                    or series_catalog is None or provider_link_catalog is None):
                raise ValueError("换源登记恢复缺少明确审核、身份、系列或Provider目录")
            from .plan_execution import PlanExecutionService
            from .plan_review import PlanReviewService
            from .asset_execution_recovery import AssetExecutionRecovery
            review = PlanReviewService(plan_store, review_root, work_catalog.root,
                decision_state_root=decision_state_root, task_root=task_root,
                record_root=record_root, provider_link_catalog_root=provider_link_catalog.root)
            execution = PlanExecutionService(plan_store, review, task_root=task_root,
                record_root=record_root, catalog_root=work_catalog.root, series_catalog_root=series_catalog.root,
                decision_state_root=decision_state_root, provider_link_catalog_root=provider_link_catalog.root,
                recovery_transaction_root=recovery_transaction_root)
            # One validated instance is shared by both post-ledger consumers.
            work_catalog.asset_action_context = execution.asset_action_service
            series_catalog.asset_action_context = execution.asset_action_service
            from .replacement_completion import uses_completion_policy
            if uses_completion_policy(initial_plan.metadata.get('asset_operation')):
                from .asset_resume import resume_asset
                AssetExecutionRecovery(execution).inspect(task_id, plan_store.fingerprint(initial_plan))
                resume_asset(execution, task_id, plan_store.fingerprint(initial_plan),
                    expected_execution_status='catalog_update_pending')
                from ..utils.utils import read_task_at
                return read_task_at(Path(task_root), task_id)
            asset_recovery = AssetExecutionRecovery(execution)
        if initial_plan.metadata.get("completed_rebuild_observation") is not None:
            if (decision_state_root is None or series_catalog is None
                    or provider_link_catalog is None):
                raise ValueError("重建登记恢复缺少明确的身份、系列或资料关联目录")
            from .completed_rebuild import CompletedRebuildService
            rebuild_service = CompletedRebuildService.from_execution_context(
                plan_store=plan_store, task_root=task_root,
                decision_root=decision_state_root, record_root=record_root,
                catalog_root=work_catalog.root, series_catalog_root=series_catalog.root,
                provider_link_catalog_root=provider_link_catalog.root,
            )
        binding = binding_from_plan(initial_plan)
        if binding is not None and binding.backend == "series":
            if series_catalog is None or provider_link_catalog is None:
                raise ValueError("电视剧登记恢复缺少作品季度关系")
            with series_catalog.lock(binding.key):
                series_catalog.require_namespace_plan(initial_plan, binding.key)
                result = _reconcile_series_catalog_update_locked(
                    task_id=str(task_id),
                    task_root=Path(task_root),
                    ledger_path=ledger_path,
                    plan_store=plan_store,
                    series_catalog=series_catalog,
                    provider_link_catalog=provider_link_catalog,
                    locked_series_key=binding.key,
                    rebuild_service=rebuild_service,
                    asset_recovery=asset_recovery,
                )
            return result
        work_key = str(
            initial_plan.metadata.get("catalog_work_key")
            or initial_plan.metadata.get("proposed_work_key")
            or ""
        )
        if not work_key:
            raise ValueError("整理计划缺少电影作品登记标识")
        if rebuild_service is not None:
            # Restarted recovery must reconstruct authority from the supplied
            # roots, not depend on an executor's former in-memory callback.
            work_catalog = WorkCatalog(work_catalog.root, rebuild_context=rebuild_service.work_context())
        with work_catalog.lock(work_key):
            work_catalog.require_namespace_plan(initial_plan, work_key)
            result = _reconcile_catalog_update_locked(
                task_id=str(task_id),
                task_root=Path(task_root),
                ledger_path=ledger_path,
                plan_store=plan_store,
                work_catalog=work_catalog,
                locked_work_key=work_key,
                rebuild_service=rebuild_service,
                asset_recovery=asset_recovery,
            )
        return result
    except (
        OSError, json.JSONDecodeError, RuntimeError, TypeError, ValueError,
        PlanStoreError, WorkCatalogError, SeriesCatalogError,
        ProviderLinkCatalogError,
    ) as exc:
        raise ExecutionRecoveryError(f"无法恢复作品登记：{exc}") from exc


def _validate_rebuild_reconciliation_plan(
    plan, *, rebuild_service, task_root, ledger_path, plan_store,
    catalog_root, backend,
) -> None:
    """A locked recovery consumer cannot turn a marker into authority."""
    if plan.metadata.get("completed_rebuild_observation") is None:
        return
    from .completed_rebuild import CompletedRebuildService
    if not isinstance(rebuild_service, CompletedRebuildService):
        raise ValueError("登记恢复缺少可信重建服务，拒绝接受重建计划")
    service_catalog = rebuild_service.series if backend == "series" else rebuild_service.work
    for actual, expected in (
        (rebuild_service.plans.root, plan_store.root),
        (rebuild_service.tasks, task_root),
        (rebuild_service.records, Path(ledger_path).parent),
        (service_catalog.root, catalog_root),
    ):
        if Path(actual).resolve() != Path(expected).resolve():
            raise ValueError("可信重建服务与当前恢复目录不一致")
    rebuild_service.validate_plan(plan)


def _validate_asset_reconciliation(plan, context, task_root, ledger_path, plan_store, catalog_root, backend):
    if (plan.metadata.get("asset_operation") or {}).get("mode") != "replace" and not is_repair(plan.metadata.get("asset_operation")):
        return
    from .asset_execution_recovery import AssetExecutionRecovery
    if not isinstance(context, AssetExecutionRecovery):
        raise ValueError("换源登记恢复缺少原批准事务的类型化核查")
    execution = context.execution
    catalog = execution.series_catalog if backend == "series" else execution.work_catalog
    for actual, expected in ((execution.task_root, task_root), (execution.record_root, Path(ledger_path).parent),
                             (execution.plan_store.root, plan_store.root), (catalog.root, catalog_root)):
        if Path(actual).resolve() != Path(expected).resolve():
            raise ValueError("换源恢复上下文与登记消费者目录不一致")
    preparation = context.inspect(plan.task_id, PlanStore.fingerprint(plan))
    if preparation.state not in {"catalog_pending", "completed"}:
        raise ValueError("换源媒体尚未完成，不能跳过事务切换直接登记")


def _reconcile_series_catalog_update_locked(
    *,
    task_id: str,
    task_root: Path,
    ledger_path: Path,
    plan_store: PlanStore,
    series_catalog: SeriesCatalog,
    provider_link_catalog: ProviderLinkCatalog,
    locked_series_key: str,
    rebuild_service=None,
    asset_recovery=None,
) -> dict:
    """Replay only post-ledger TV facts while holding the exact series lock."""

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    result: dict[str, object] = {}

    def transition(task: dict) -> dict:
        if task.get("uuid") != task_id:
            raise ValueError("任务记录损坏或 task_id 不匹配")
        if (
            task.get("workflow_status") not in {
                "failed", "catalog_update_pending"
            }
            or task.get("execution_status") != "catalog_update_pending"
        ):
            raise ValueError("任务不处于作品登记待恢复状态")
        plan = plan_store.load(task_id)
        binding = binding_from_plan(plan)
        if (
            binding is None
            or binding.backend != "series"
            or binding.key != locked_series_key
        ):
            raise ValueError("整理计划的电视剧作品登记标识已变化")
        fingerprint = plan_store.fingerprint(plan)
        for field in (
            "plan_fingerprint",
            "execution_requested_fingerprint",
            "execution_fingerprint",
        ):
            value = task.get(field)
            if value not in {None, "", fingerprint}:
                raise ValueError("任务记录与当前整理版本不匹配")
        if task.get("queue_operation") not in {None, "", "execute_approved"}:
            raise ValueError("任务操作类型与作品登记恢复不一致")
        _validate_rebuild_reconciliation_plan(
            plan, rebuild_service=rebuild_service, task_root=task_root,
            ledger_path=ledger_path, plan_store=plan_store,
            catalog_root=series_catalog.root, backend="series",
        )
        _validate_asset_reconciliation(plan, asset_recovery, task_root, ledger_path, plan_store, series_catalog.root, "series")
        _validate_completed_hardlink_ledger(
            plan=plan,
            ledger=ledger,
            fingerprint=fingerprint,
        )

        expected_revision = int(
            plan.metadata.get("series_catalog_revision") or 0
        )
        try:
            current = series_catalog.load(locked_series_key)
        except SeriesCatalogError:
            if expected_revision != 0:
                raise
            current = None
        replacing = (plan.metadata.get("asset_operation") or {}).get("mode") == "replace" or is_repair(plan.metadata.get("asset_operation"))
        if current is None or current.revision == expected_revision or replacing:
            current = series_catalog.register_completed(
                plan,
                expected_revision=expected_revision,
                completed_ledger=ledger,
            )
        elif not (
            current.revision == expected_revision + 1
            and _series_catalog_materializes_plan(
                current, plan, completed_ledger=ledger
            )
        ):
            raise ValueError("SeriesCatalog版本或内容与待恢复计划不一致")

        snapshot = plan.metadata.get("provider_link_snapshot")
        if isinstance(snapshot, Mapping):
            level = str(snapshot.get("automation_level") or "")
            if level in {"L1", "L2", "L3"}:
                provider_mapping = materialize_provider_link_snapshot(
                    provider_link_catalog, snapshot
                )
                result["provider_link"] = provider_mapping.to_dict()
        completed_at = _utc_now()
        revised = dict(task)
        from .replacement_completion import media_completion_fields
        revised.update({
            **media_completion_fields(plan, completed_at),
            "execution_status": "completed",
            "execution_completed_at": completed_at,
            "execution_requires_manual_check": False,
            "execution_may_have_written_targets": False,
            "execution_recovery_message": None,
            "catalog_reconciled_at": completed_at,
            "error": None,
        })
        result["series"] = current.__dict__
        return revised

    updated = mutate_task_at(Path(task_root), task_id, transition)
    if updated is None or "series" not in result:
        raise ValueError("任务记录不存在")
    return dict(result)


def _validate_completed_hardlink_ledger(
    *, plan, ledger: object, fingerprint: str
) -> None:
    if not isinstance(ledger, dict) or ledger.get("status") != "completed":
        raise ValueError("历史执行记录不是已完成状态")
    if (
        ledger.get("task_id") != plan.task_id
        or ledger.get("plan_fingerprint") != fingerprint
    ):
        raise ValueError("历史执行记录与当前整理版本不匹配")
    from .generated_outputs import validate_completed
    validate_completed(plan, ledger)
    entries = ledger.get("entries")
    if not isinstance(entries, list) or len(entries) != len(plan.entries):
        raise ValueError("历史执行记录条目数量不匹配")
    rows_by_pair: dict[tuple[str, str], list[dict]] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("历史执行记录存在损坏条目")
        pair = (str(item.get("source") or ""), str(item.get("target") or ""))
        rows_by_pair.setdefault(pair, []).append(item)
    replacements = {}
    replacement_operation = False
    cleaning = False
    raw_operation = plan.metadata.get("asset_operation")
    if raw_operation is not None:
        from .asset_operation import AssetOperation
        from .asset_replacement import approved_batch, ReplacementEvidence
        from .projection_authority import validate_plan_projection_receipt
        from .trans import Trans
        operation = AssetOperation.from_mapping(raw_operation).to_dict()
        replacements = {row["target"]: ReplacementEvidence.from_dict(row) for row in operation["replaces"]}
        replacement_operation = operation['mode'] == 'replace' or is_repair(operation)
        if replacement_operation:
            validate_plan_projection_receipt(plan)
            if (ledger.get("schema_version") != 3 or ledger.get("mode") != "hardlink"
                    or ledger.get("dry_run") is not False or plan.execution_policy is None
                    or plan.execution_policy.mode != "hardlink" or not plan.execution_policy.preserve_source
                    or plan.execution_policy.dry_run):
                raise ValueError("换源完成记录缺少严格schema3硬链接合同")
            operations = []
            for entry in plan.entries:
                evidence = replacements.get(str(entry.target_path))
                operations.append(dict(source=str(entry.source_path), target=str(entry.target_path),
                    action="replace" if evidence else "reuse_existing" if entry.collision_resolution else "create",
                    replacement_evidence=evidence.to_dict() if evidence else None,
                    collision_resolution=entry.collision_resolution.to_dict() if entry.collision_resolution else None))
            capability = approved_batch(plan, fingerprint)
            transfer = Trans(plan.as_mapping(), plan.task_id, fingerprint, mode="hardlink", dry_run=False,
                             record_root=Path(plan.metadata["canonical_work_root"]), operations=operations,
                             replacement_authority=capability)
            operations = transfer._replacement_operations(capability)
            journal = transfer._read_replacement_journal(capability, operations)
            if journal is None or journal.get("status") != "completed":
                raise ValueError("换源完成记录缺少对应完成journal")
            transfer._validate_replacement_state(capability, operations, journal)
            from .replacement_completion import validate_cleanup
            cleaning = validate_cleanup(capability, operations, journal, ledger=ledger)
    if ledger.get("schema_version") == 3 and not replacement_operation:
        raise ValueError("schema3不能脱离类型化换源操作")
    from .media_file_identity import frozen_sources, verify_recorded_target
    sources = frozen_sources(plan)
    for entry_index, plan_entry in enumerate(plan.entries):
        pair = (str(plan_entry.source_path), str(plan_entry.target_path))
        rows = rows_by_pair.get(pair, [])
        if len(rows) != 1:
            raise ValueError("历史执行记录与整理条目不匹配")
        item = rows[0]
        source = plan_entry.source_path
        target = plan_entry.target_path
        replacement = replacements.get(str(target))
        if replacement is not None:
            from .asset_replacement import require_file, validate_ledger_file_fields
            from .asset_operation import plain
            backup = capability.transaction_directory / f"old-{entry_index}"
            if (replacement.source != str(source)
                    or item.get("action") != "replace" or item.get("status") != "created"
                    or item.get("transfer_type") != "hardlink"
                    or item.get("replacement_evidence") != plain(replacement.to_dict())
                    or item.get("replacement_backup") != str(backup)):
                raise ValueError("换源ledger条目与批准操作或回滚位置不一致")
            if not cleaning:
                require_file(backup, replacement.old_target)
            require_file(source, replacement.new_source)
            actual = require_file(target, replacement.new_source)
            validate_ledger_file_fields(item, actual)
            if target.stat().st_nlink < 2:
                raise ValueError("换源目标不再满足硬链接身份")
            continue
        collision = plan_entry.collision_resolution
        if collision is not None:
            if any((
                item.get("status") != "reused_identical_asset",
                item.get("action") != "reuse_existing",
                item.get("transfer_type") != "reuse_existing",
            )):
                raise ValueError("历史执行记录缺少同内容复用结果")
        elif any((item.get('status') != 'created', item.get('action') != 'create',
                  item.get('transfer_type') != 'hardlink')):
            raise ValueError('历史执行记录缺少可复核的硬链接结果')
        verify_recorded_target(plan, plan_entry, item, sources=sources)


def _reconcile_catalog_update_locked(
    *,
    task_id: str,
    task_root: Path,
    ledger_path: Path,
    plan_store: PlanStore,
    work_catalog: WorkCatalog,
    locked_work_key: str,
    rebuild_service=None,
    asset_recovery=None,
) -> dict:
    """Re-read and reconcile exact authority while holding the work lock."""

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    result: dict[str, dict] = {}

    def transition(task: dict) -> dict:
        if task.get("uuid") != str(task_id):
            raise ValueError("任务记录损坏或 task_id 不匹配")
        plan = plan_store.load(task_id)
        is_rebuild = plan.metadata.get("completed_rebuild_observation") is not None
        is_replacement = (plan.metadata.get("asset_operation") or {}).get("mode") == "replace" or is_repair(plan.metadata.get("asset_operation"))
        # The queue preserves execution_status but records workflow=failed
        # after the exact post-media registration exception. Only a typed
        # rebuild completion below can authorize this recovery transition.
        pending_workflows = {"catalog_update_pending", "failed"} if is_rebuild or is_replacement else {"catalog_update_pending"}
        if (
            task.get("workflow_status") not in pending_workflows
            or task.get("execution_status") != "catalog_update_pending"
        ):
            raise ValueError("任务不处于作品登记待恢复状态")

        current_work_key = str(
            plan.metadata.get("catalog_work_key")
            or plan.metadata.get("proposed_work_key")
            or ""
        )
        if current_work_key != locked_work_key:
            raise ValueError("整理计划的电影作品登记标识已变化")
        fingerprint = plan_store.fingerprint(plan)
        for field in (
            "plan_fingerprint",
            "execution_requested_fingerprint",
            "execution_fingerprint",
        ):
            value = task.get(field)
            if value not in {None, "", fingerprint}:
                raise ValueError("任务记录与当前整理版本不匹配")
        queue_operation = task.get("queue_operation")
        if queue_operation not in {None, "", "execute_approved"}:
            raise ValueError("任务操作类型与作品登记恢复不一致")
        _validate_rebuild_reconciliation_plan(
            plan, rebuild_service=rebuild_service, task_root=task_root,
            ledger_path=ledger_path, plan_store=plan_store,
            catalog_root=work_catalog.root, backend="work",
        )
        _validate_asset_reconciliation(plan, asset_recovery, task_root, ledger_path, plan_store, work_catalog.root, "work")
        if not is_rebuild:
            _validate_completed_hardlink_ledger(plan=plan, ledger=ledger, fingerprint=fingerprint)
        # For a rebuild the canonical register_completed no-op validates the
        # typed observation, exact whole-record history and every physical row,
        # 核对当前物理记录，完成标记不能替代这些证据。
        record = work_catalog.register_completed(
            plan,
            ledger,
            expected_revision=int(plan.metadata.get("catalog_revision") or 0),
            lock_held=True,
        )
        completed_at = _utc_now()
        revised = dict(task)
        from .replacement_completion import media_completion_fields
        revised.update({
            **media_completion_fields(plan, completed_at),
            "execution_status": "completed",
            "execution_completed_at": completed_at,
            "execution_requires_manual_check": False,
            "execution_may_have_written_targets": False,
            "execution_recovery_message": None,
            "catalog_reconciled_at": completed_at,
            "error": None,
        })
        result["record"] = record
        return revised

    updated = mutate_task_at(Path(task_root), str(task_id), transition)
    if updated is None or "record" not in result:
        raise ValueError("任务记录不存在")
    return result["record"]


def abandon_catalog_update_recovery(
    task_id: str,
    *,
    task_root: Path,
    record_root: Path,
    plan_store: PlanStore,
    review_root: Path,
    decision_state_root: Path,
    audit_root: Path,
    expected_plan_fingerprint: str,
) -> dict:
    """Remove only active recovery surfaces while preserving all evidence."""

    fingerprint = str(expected_plan_fingerprint or "")
    if _SHA256.fullmatch(fingerprint) is None:
        raise ExecutionRecoveryError("整理版本标识无效")
    task_path = _task_path(task_id, task_root)
    ledger_path = _ledger_path(task_id, record_root)
    record_path = _task_path(task_id, record_root)
    review_path = _task_path(task_id, review_root)
    # Validate the preserved DecisionState path without ever reading or writing it.
    _task_path(task_id, decision_state_root)
    audit_path = _abandon_audit_path(task_id, fingerprint, audit_root)

    if audit_path.exists():
        audit = _load_abandon_audit(audit_path, task_id, fingerprint)
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            if (
                not isinstance(ledger, dict)
                or ledger.get("schema_version") != 1
                or ledger.get("status") != "completed"
                or ledger.get("task_id") != str(task_id)
                or ledger.get("plan_fingerprint") != fingerprint
            ):
                raise ValueError("历史执行记录在审计后发生变化")
            if _sha256_file(ledger_path) != audit.get("ledger_sha256"):
                raise ValueError("历史执行记录摘要在审计后发生变化")
            expected_record_hash = audit.get("record_sha256")
            current_record_hash = (
                _sha256_file(record_path) if record_path.exists() else None
            )
            if current_record_hash != expected_record_hash:
                raise ValueError("任务历史记录在审计后发生变化")
            # Cleanup is intentionally idempotent.  Revalidate every active
            # surface that still exists, while allowing a previous successful
            # (or partially successful) cleanup to have removed some of them.
            if task_path.exists():
                task = json.loads(task_path.read_text(encoding="utf-8"))
                if not isinstance(task, dict) or task.get("uuid") != str(task_id):
                    raise ValueError("任务记录损坏或身份不匹配")
                if (
                    task.get("workflow_status")
                    != audit.get("old_workflow_status")
                    or task.get("execution_status")
                    != audit.get("old_execution_status")
                ):
                    raise ValueError("任务状态在审计后发生变化")
            plan_path = plan_store.path_for(task_id)
            if plan_path.exists():
                plan = plan_store.load(task_id)
                if plan_store.fingerprint(plan) != fingerprint:
                    raise ValueError("整理版本在审计后发生变化")
        except (
            OSError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            PlanStoreError,
        ) as exc:
            raise ExecutionRecoveryError(
                f"无法安全重用历史审计：{exc}"
            ) from exc
    else:
        try:
            task = json.loads(task_path.read_text(encoding="utf-8"))
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            plan = plan_store.load(task_id)
            actual_fingerprint = plan_store.fingerprint(plan)
            if not isinstance(task, dict) or task.get("uuid") != str(task_id):
                raise ValueError("任务记录损坏或身份不匹配")
            if task.get("workflow_status") != "failed":
                raise ValueError("任务不处于失败恢复状态")
            if task.get("execution_status") != "catalog_update_pending":
                raise ValueError("任务不处于作品登记待恢复状态")
            if actual_fingerprint != fingerprint:
                raise ValueError("整理版本已经变化，请刷新后重试")
            if not isinstance(ledger, dict) or ledger.get("status") != "completed":
                raise ValueError("历史执行记录不是已完成状态")
            if (
                ledger.get("schema_version") != 1
                or ledger.get("task_id") != str(task_id)
                or ledger.get("plan_fingerprint") != fingerprint
            ):
                raise ValueError("历史执行记录与当前任务不匹配")
            entries = ledger.get("entries")
            if not isinstance(entries, list) or len(entries) != len(plan.entries):
                raise ValueError("历史执行记录条目数量不匹配")
            audit = {
                "schema_version": 1,
                "event_type": ABANDON_EVENT_TYPE,
                "task_id": str(task_id),
                "old_workflow_status": str(task.get("workflow_status") or ""),
                "old_execution_status": str(task.get("execution_status") or ""),
                "plan_fingerprint": fingerprint,
                "ledger_sha256": _sha256_file(ledger_path),
                "record_sha256": (
                    _sha256_file(record_path) if record_path.exists() else None
                ),
                "confirmed_at": _utc_now(),
                "confirmation_policy_version": (
                    ABANDON_CONFIRMATION_POLICY_VERSION
                ),
                "cleanup_allowlist": list(ABANDON_CLEANUP_ALLOWLIST),
            }
            _exclusive_atomic_json_write(audit_path, audit)
        except (
            OSError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            PlanStoreError,
        ) as exc:
            raise ExecutionRecoveryError(f"无法安全移除任务：{exc}") from exc

    try:
        review_path.unlink(missing_ok=True)
        plan_store.path_for(task_id).unlink(missing_ok=True)
        task_path.unlink(missing_ok=True)
        from ..utils.task_changes import mark_changed
        mark_changed()
    except (OSError, PlanStoreError) as exc:
        raise ExecutionRecoveryError(
            "操作记录已保留，但任务列表清理未完成；可以再次尝试"
        ) from exc
    return audit


def _abandon_audit_path(
    task_id: str, fingerprint: str, root: Path
) -> Path:
    trusted = _task_path(task_id, root)
    return trusted.with_name(f"{task_id}.{fingerprint}.json")


def _load_abandon_audit(
    path: Path, task_id: str, fingerprint: str
) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("历史审计记录格式无效")
        if (
            payload.get("schema_version") != 1
            or payload.get("event_type") != ABANDON_EVENT_TYPE
            or payload.get("task_id") != str(task_id)
            or payload.get("plan_fingerprint") != fingerprint
            or payload.get("confirmation_policy_version")
            != ABANDON_CONFIRMATION_POLICY_VERSION
            or payload.get("cleanup_allowlist")
            != list(ABANDON_CLEANUP_ALLOWLIST)
            or _SHA256.fullmatch(str(payload.get("ledger_sha256") or "")) is None
        ):
            raise ValueError("历史审计记录与当前任务不匹配")
        record_hash = payload.get("record_sha256")
        if record_hash is not None and _SHA256.fullmatch(str(record_hash)) is None:
            raise ValueError("历史审计记录摘要无效")
        return payload
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ExecutionRecoveryError(f"无法信任历史审计记录：{exc}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exclusive_atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "x", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = _load_abandon_audit(
                path,
                str(payload.get("task_id") or ""),
                str(payload.get("plan_fingerprint") or ""),
            )
            if existing != payload:
                raise ExecutionRecoveryError("历史审计记录发生冲突")
    finally:
        temporary.unlink(missing_ok=True)


def _task_path(task_id: str, root: Path) -> Path:
    try:
        return PlanStore(Path(root)).path_for(str(task_id))
    except PlanStoreError as exc:
        raise ExecutionRecoveryError(str(exc)) from exc


def _ledger_path(task_id: str, root: Path) -> Path:
    trusted_task_path = _task_path(task_id, root)
    return trusted_task_path.with_name(f"{task_id}.ledger.json")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
