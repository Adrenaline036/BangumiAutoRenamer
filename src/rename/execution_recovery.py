"""Read-only execution-ledger inspection and explicit operator acknowledgement."""

from __future__ import annotations

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
from .asset_collision import AssetCollisionError, validate_reuse_resolution
from .font_archive_contract import (
    FontArchiveContractError,
    validate_completed_movie_font_assets,
)


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
        if payload.get("schema_version") != 1:
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
) -> dict:
    """Rebuild pending Work/Series facts from one completed trusted ledger."""

    ledger_path = _ledger_path(task_id, record_root)
    try:
        initial_plan = plan_store.load(task_id)
        binding = binding_from_plan(initial_plan)
        if binding is not None and binding.backend == "series":
            if series_catalog is None or provider_link_catalog is None:
                raise ValueError("电视剧登记恢复缺少作品季度关系")
            with series_catalog.lock(binding.key):
                return _reconcile_series_catalog_update_locked(
                    task_id=str(task_id),
                    task_root=Path(task_root),
                    ledger_path=ledger_path,
                    plan_store=plan_store,
                    series_catalog=series_catalog,
                    provider_link_catalog=provider_link_catalog,
                    locked_series_key=binding.key,
                )
        work_key = str(
            initial_plan.metadata.get("catalog_work_key")
            or initial_plan.metadata.get("proposed_work_key")
            or ""
        )
        if not work_key:
            raise ValueError("整理计划缺少电影作品登记标识")
        with work_catalog.lock(work_key):
            return _reconcile_catalog_update_locked(
                task_id=str(task_id),
                task_root=Path(task_root),
                ledger_path=ledger_path,
                plan_store=plan_store,
                work_catalog=work_catalog,
                locked_work_key=work_key,
            )
    except (
        OSError, json.JSONDecodeError, RuntimeError, TypeError, ValueError,
        PlanStoreError, WorkCatalogError, SeriesCatalogError,
        ProviderLinkCatalogError, FontArchiveContractError,
    ) as exc:
        raise ExecutionRecoveryError(f"无法恢复作品登记：{exc}") from exc


def _reconcile_series_catalog_update_locked(
    *,
    task_id: str,
    task_root: Path,
    ledger_path: Path,
    plan_store: PlanStore,
    series_catalog: SeriesCatalog,
    provider_link_catalog: ProviderLinkCatalog,
    locked_series_key: str,
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
        if current is None or current.revision == expected_revision:
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
        revised.update({
            "workflow_status": "completed",
            "execution_status": "completed",
            "execution_phase": "completed",
            "completed_at": completed_at,
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
    entries = ledger.get("entries")
    if not isinstance(entries, list) or len(entries) != len(plan.entries):
        raise ValueError("历史执行记录条目数量不匹配")
    rows_by_pair: dict[tuple[str, str], list[dict]] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("历史执行记录存在损坏条目")
        pair = (str(item.get("source") or ""), str(item.get("target") or ""))
        rows_by_pair.setdefault(pair, []).append(item)
    for plan_entry in plan.entries:
        pair = (str(plan_entry.source_path), str(plan_entry.target_path))
        rows = rows_by_pair.get(pair, [])
        if len(rows) != 1:
            raise ValueError("历史执行记录与整理条目不匹配")
        item = rows[0]
        source = plan_entry.source_path
        target = plan_entry.target_path
        collision = plan_entry.collision_resolution
        if collision is not None:
            if any((
                item.get("status") != "reused_identical_asset",
                item.get("action") != "reuse_existing",
                item.get("transfer_type") != "reuse_existing",
            )):
                raise ValueError("历史执行记录缺少同内容复用结果")
            try:
                validate_reuse_resolution(source, target, collision)
            except AssetCollisionError as exc:
                raise ValueError(str(exc)) from exc
            continue
        if any((
            item.get("status") != "created",
            item.get("action") != "create",
            item.get("transfer_type") != "hardlink",
            not source.is_file(),
            not target.is_file(),
        )):
            raise ValueError("历史执行记录缺少可复核的硬链接文件")
        source_stat = source.stat()
        target_stat = target.stat()
        if (
            source_stat.st_dev != target_stat.st_dev
            or source_stat.st_ino != target_stat.st_ino
            or target_stat.st_nlink < 2
        ):
            raise ValueError("现存文件不再满足 hardlink 证明")


def _reconcile_catalog_update_locked(
    *,
    task_id: str,
    task_root: Path,
    ledger_path: Path,
    plan_store: PlanStore,
    work_catalog: WorkCatalog,
    locked_work_key: str,
) -> dict:
    """Re-read and reconcile exact authority while holding the work lock."""

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    result: dict[str, dict] = {}

    def transition(task: dict) -> dict:
        if task.get("uuid") != str(task_id):
            raise ValueError("任务记录损坏或 task_id 不匹配")
        if (
            task.get("workflow_status") != "catalog_update_pending"
            or task.get("execution_status") != "catalog_update_pending"
        ):
            raise ValueError("任务不处于作品登记待恢复状态")

        plan = plan_store.load(task_id)
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
        if not isinstance(ledger, dict) or ledger.get("status") != "completed":
            raise ValueError("历史执行记录不是已完成状态")
        if (
            ledger.get("task_id") != str(task_id)
            or ledger.get("plan_fingerprint") != fingerprint
        ):
            raise ValueError("历史执行记录与当前整理版本不匹配")
        entries = ledger.get("entries")
        if not isinstance(entries, list) or len(entries) != len(plan.entries):
            raise ValueError("历史执行记录条目数量不匹配")
        rows_by_pair: dict[tuple[str, str], list[dict]] = {}
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("历史执行记录存在损坏条目")
            pair = (str(item.get("source") or ""), str(item.get("target") or ""))
            rows_by_pair.setdefault(pair, []).append(item)
        for plan_entry in plan.entries:
            pair = (str(plan_entry.source_path), str(plan_entry.target_path))
            rows = rows_by_pair.get(pair, [])
            if len(rows) != 1:
                raise ValueError("历史执行记录与整理条目不匹配")
            item = rows[0]
            source = plan_entry.source_path
            target = plan_entry.target_path
            collision = plan_entry.collision_resolution
            if collision is not None:
                if any((
                    item.get("status") != "reused_identical_asset",
                    item.get("action") != "reuse_existing",
                    item.get("transfer_type") != "reuse_existing",
                )):
                    raise ValueError("历史执行记录缺少同内容复用结果")
                try:
                    validate_reuse_resolution(source, target, collision)
                except AssetCollisionError as exc:
                    raise ValueError(str(exc)) from exc
                continue
            if any((
                item.get("status") != "created",
                item.get("action") != "create",
                item.get("transfer_type") != "hardlink",
                not source.is_file(),
                not target.is_file(),
            )):
                raise ValueError("历史执行记录缺少可复核的硬链接文件")
            source_stat = source.stat()
            target_stat = target.stat()
            if (
                source_stat.st_dev != target_stat.st_dev
                or source_stat.st_ino != target_stat.st_ino
                or target_stat.st_nlink < 2
            ):
                raise ValueError("现存文件不再满足 hardlink 证明")
        validate_completed_movie_font_assets(plan, ledger)
        record = work_catalog.register_completed(
            plan,
            ledger,
            expected_revision=int(plan.metadata.get("catalog_revision") or 0),
            lock_held=True,
        )
        completed_at = _utc_now()
        revised = dict(task)
        revised.update({
            "workflow_status": "completed",
            "execution_status": "completed",
            "completed_at": completed_at,
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
