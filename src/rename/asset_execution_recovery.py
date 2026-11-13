"""Read-only recovery of one original approved asset replacement transaction.

Recovery does not grant a new approval and does not perform media writes. The
ordinary executor owns locks and calls Trans with the returned capability. An
empty pre-journal orphan has no ownership witness: preserve it and offer a new
unapproved Plan, never silently adopt or delete the directory.
"""

from __future__ import annotations

from .series_repair import is_repair

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .asset_replacement import ApprovedReplacementBatch, ReplacementEvidence, safe_absolute, approved_batch
from .completed_history import read_bytes
from .plan_store import PlanStore
from .rename_plan import RenamePlan
from .trans import Trans


@dataclass(frozen=True)
class AssetRecoveryPreparation:
    state: str
    plan: RenamePlan
    capability: ApprovedReplacementBatch | None
    approval_fingerprint: str
    detail: str


class AssetExecutionRecovery:
    def __init__(self, execution_service):
        from .plan_execution import PlanExecutionService
        if not isinstance(execution_service, PlanExecutionService):
            raise ValueError("换源恢复必须使用明确存储上下文的执行服务")
        self.execution = execution_service

    def _catalog_current(self, plan, ledger):
        if ledger is None:
            return False
        scope = self.execution.asset_action_service._scope(plan)
        if scope[0] == "work":
            record = self.execution.work_catalog.load(scope[1])
            return self.execution.work_catalog.materializes_completed_plan(record, plan, ledger)
        from .recovery_service import _series_catalog_materializes_plan
        if (plan.metadata.get('asset_operation') or {}).get('policy')=='asset-operation-v3' and not self.execution.series_catalog._path(scope[1]).exists():
            return False
        match = self.execution.series_catalog.load(scope[1])
        if match.canonical_root != scope[3]:
            raise ValueError("换源恢复系列根漂移")
        return _series_catalog_materializes_plan(match, plan, completed_ledger=ledger)

    def _validate_previous(self, plan, operation, *, catalog_current):
        actions = self.execution.asset_action_service
        value = operation.to_dict()
        if value['policy']=='asset-operation-v3':
            from .season_replacement import validate
            validate(actions,plan,value,recovery=True,catalog_current=catalog_current)
            return
        if is_repair(value):
            from .series_repair import validate, root_identity
            if root_identity(value['repair_root']['path']) != value['repair_root']:
                raise ValueError('补齐媒体库目录身份已变化')
            if not catalog_current:
                validate(actions,plan,value,completed=True)
                return
        previous = value["previous_assets"]
        if not previous:
            raise ValueError("换源恢复缺少批准的旧资产闭包")
        if not catalog_current:
            scope, current = actions._previous(plan, expected=previous, completed=True)
            if scope[2] != value["slot_key"] or current != previous:
                raise ValueError("换源恢复旧Catalog槽位已变化")
        else:
            # Catalog may have committed before task finalization. Preserve
            # the original history proof even though the active slot is now new.
            scope = actions._scope(plan)
            _active_scope, active_rows = actions._previous(plan)
            active = {row["target_path"]: row for row in active_rows}
            new_targets = {str(entry.target_path) for entry in plan.entries}
            for row in previous:
                old, rows, ledger_hash = actions._history(row["task_id"], row["plan_fingerprint"])
                if (ledger_hash != row["ledger_sha256"] or not actions._belongs_to_scope(old, scope)
                        or row["slot_key"] != scope[2] or row["canonical_root"] != str(scope[3])):
                    raise ValueError("换源恢复原完成历史或槽位不一致")
                old_entry = rows.get((row["source_path"], row["target_path"]))
                if old_entry is None:
                    raise ValueError("换源恢复原条目不属于冻结完成Plan")
                identity = actions._registered_identity(old_entry)
                observed = row["target_evidence"]
                if observed is not None and (observed["device"], observed["inode"]) != identity:
                    raise ValueError("换源恢复原目标身份与登记不一致")
                if row["disposition"] == "retain" and row["target_path"] not in new_targets:
                    current = active.get(row["target_path"])
                    fields = ("task_id", "plan_fingerprint", "ledger_sha256", "source_path", "target_evidence")
                    if current is None or any(current[field] != row[field] for field in fields):
                        raise ValueError("换源恢复保留资产的活跃登记或冻结实体已变化")
        selected = [ReplacementEvidence.from_dict(item) for item in value["replaces"]]
        expected_targets = {row["target_path"] for row in previous if row["disposition"] == "replace"}
        if {item.target for item in selected} != expected_targets:
            raise ValueError("换源恢复条目集合与原资产闭包不一致")
        pairs = {(str(entry.source_path), str(entry.target_path)) for entry in plan.entries}
        for item in selected:
            row = next(row for row in previous if row["target_path"] == item.target)
            if ((item.source, item.target) not in pairs or item.target_root != str(scope[3])
                    or asdict(item.old_target) != row["target_evidence"]):
                raise ValueError("换源恢复精确目标与批准旧资产不一致")

    def inspect(self, task_id: str, expected_fingerprint: str) -> AssetRecoveryPreparation:
        """Recheck original approval, CAS, journal and physical identities only."""
        execution = self.execution
        try:
            plan = execution.validate_approved_authority(task_id, expected_fingerprint)
            state = execution.decision_state_store.load(task_id)
            operation = execution.asset_action_service.validate_operation_authority(plan, state)
            if operation is None or (operation.to_dict()["mode"] != "replace" and not is_repair(operation.to_dict())) or operation.to_dict().get("requires_choice"):
                raise ValueError("此Plan不是已明确批准的换源操作")
            execution._validate_frozen_source_identities(plan)
            approval = execution.review_service.load(task_id)
            approval_fp = hashlib.sha256(json.dumps(approval.to_dict(), sort_keys=True,
                                                   separators=(",", ":")).encode()).hexdigest()
            task = execution._read_task(task_id)
            if task.get('asset_rollback_phase'):
                raise ValueError('该操作已开始回滚，请使用回滚入口继续，不能恢复正向执行')
            for field in ("execution_requested_fingerprint", "execution_fingerprint"):
                if task.get(field) not in {None, "", expected_fingerprint}:
                    raise ValueError("换源恢复任务请求版本已漂移")
            capability = approved_batch(plan, expected_fingerprint)
            ledger_path = execution.record_root / f"{task_id}.ledger.json"
            ledger = json.loads(read_bytes(ledger_path)) if ledger_path.exists() else None
            if ledger is not None and (ledger.get("task_id"), ledger.get("plan_fingerprint")) != (task_id, expected_fingerprint):
                raise ValueError("当前ledger不属于原批准的换源事务")
            completed_ledger = ledger if ledger is not None and ledger.get("status") == "completed" else None
            if completed_ledger is not None:
                from .execution_recovery import _validate_completed_hardlink_ledger
                _validate_completed_hardlink_ledger(plan=plan, ledger=completed_ledger, fingerprint=expected_fingerprint)
            catalog_current = self._catalog_current(plan, completed_ledger)
            self._validate_previous(plan, operation, catalog_current=catalog_current)
            if capability is not None:
                operations = execution._build_execution_operations(plan)
                transfer = Trans(plan.as_mapping(), task_id, expected_fingerprint, mode="hardlink", dry_run=False,
                                 record_root=execution.record_root, operations=operations,
                                 replacement_authority=capability)
                transfer._replacement_batch()
                operations = transfer._replacement_operations(capability)
                directory = safe_absolute(capability.transaction_directory)
                if directory.exists() and not (directory / "journal.json").exists():
                    # Only a genuinely empty directory plus unchanged old targets
                    # proves this interruption preceded all media side effects.
                    if any(directory.iterdir()):
                        raise ValueError("换源journal缺失且事务目录不为空，禁止猜测恢复")
                    transfer._validate_replacement_state(capability, operations, None)
                    return AssetRecoveryPreparation("replan_required", plan, None, approval_fp,
                        "事务在首份日志前中断；目标尚未改变。保留该空目录，可安全弃本次并生成新的待批准计划。")
                journal = transfer._read_replacement_journal(capability, operations)
                transfer._validate_replacement_state(capability, operations, journal)
            elif completed_ledger is None:
                # No media was selected for overwrite. Existing partial-create
                # paths cannot be adopted without the ordinary recovery proof.
                if any(entry.target_path.exists() and entry.collision_resolution is None for entry in plan.entries):
                    raise ValueError("纯补齐事务存在未完成目标，需普通执行恢复核查")
                return AssetRecoveryPreparation("replan_required", plan, None, approval_fp,
                    "本次只补齐缺失文件且没有完成记录；目标未写，可生成新的待批准计划。")
            if completed_ledger is not None:
                recovery_state = "completed" if catalog_current else "catalog_pending"
                detail = "媒体与登记均已完成，仅继续任务收尾" if catalog_current else "媒体已完成，仅恢复Catalog登记"
            else:
                recovery_state, detail = "resume_media", "原批准、旧目标回滚证据与新旧文件身份一致，可继续同一事务"
            return AssetRecoveryPreparation(recovery_state, plan, capability, approval_fp, detail)
        except (OSError, RuntimeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"换源恢复核查未通过：{exc}") from exc

    def validate_preparation(self, preparation: AssetRecoveryPreparation) -> AssetRecoveryPreparation:
        if not isinstance(preparation, AssetRecoveryPreparation):
            raise ValueError("换源恢复准备必须是类型化结果")
        current = self.inspect(preparation.plan.task_id, PlanStore.fingerprint(preparation.plan))
        if current.approval_fingerprint != preparation.approval_fingerprint or current.capability != preparation.capability:
            raise ValueError("换源恢复准备之后批准或精确操作已变化")
        return current
