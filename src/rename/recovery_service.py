"""Read-only recovery diagnosis for task decisions, Plans and hardlinks."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from .decision_state import DecisionState
from .plan_store import PlanStore
from .projection_authority import ProjectionAuthorityError
from .rename_plan import RenamePlan
from .work_catalog import WorkCatalog, WorkCatalogError
from .series_catalog import SeriesCatalog, SeriesCatalogError
from .asset_collision import AssetCollisionError, build_reuse_resolution, validate_reuse_resolution
from .catalog_binding import recovery_binding



@dataclass(frozen=True)
class RecoverySnapshot:
    state: str
    detail: str
    can_rebuild_catalog: bool = False
    can_rebuild_plan: bool = False
    requires_external_action: bool = False
    allowed_actions: tuple[str, ...] = ()


def untrusted_projection_snapshot() -> RecoverySnapshot:
    """A display-only conflict, never authority to repair historical records."""
    return RecoverySnapshot(
        "evidence_conflict",
        "当前整理计划缺少可信凭据或凭据已失效。已保留现有文件，"
        "暂停自动恢复；请先核对历史记录。",
        requires_external_action=True,
    )


class RecoveryService:
    def __init__(
        self, *, catalog_root: Path, record_root: Path,
        series_catalog_root: Optional[Path] = None,
        rebuild_service=None,
    ):
        self.catalog_root = Path(catalog_root)
        self.record_root = Path(record_root)
        self.rebuild_service = rebuild_service
        self.series_catalog_root = (
            Path(series_catalog_root) if series_catalog_root is not None else None
        )

    def _inspect_completed_media(self, plan, ledger):
        from .completed_rebuild import CompletedRebuildService
        if isinstance(self.rebuild_service, CompletedRebuildService):
            return self.rebuild_service.inspect_media(plan, ledger)
        from .completed_history import inspect_completed_media
        return inspect_completed_media(plan, ledger)

    def diagnose(
        self,
        decision: DecisionState,
        plan: RenamePlan,
        *,
        work_catalog: Optional[WorkCatalog] = None,
    ) -> RecoverySnapshot:
        missing_sources = [entry.source_path for entry in plan.entries if not entry.source_path.is_file()]
        if missing_sources:
            return RecoverySnapshot(
                "source_unavailable", f"{len(missing_sources)} 个源文件不存在或不可读取",
                requires_external_action=True,
                allowed_actions=("reselect_source",),
            )
        ledger = self._trusted_ledger(plan)
        if ledger is None:
            return RecoverySnapshot(
                "evidence_conflict", "没有与当前 Plan 匹配的 completed execution ledger",
                requires_external_action=True,
            )
        if plan.metadata.get('completed_rebuild_observation') is not None:
            try:
                from .completed_rebuild import CompletedRebuildService
                if not isinstance(self.rebuild_service, CompletedRebuildService):
                    raise ValueError('重建核查缺少明确的历史验证上下文')
                self.rebuild_service.validate_plan(plan)
            except (ValueError, RuntimeError, OSError, KeyError) as exc:
                return RecoverySnapshot('evidence_conflict', str(exc), requires_external_action=True)
        if plan.metadata.get('completed_rebuild_observation') is not None and all(e.target_path.is_file() for e in plan.entries):
            try:
                self._inspect_completed_media(plan, ledger)
                binding = recovery_binding(plan, decision)
                if binding is not None and binding.backend == 'work':
                    from .completed_rebuild_work import validate_work_rebuild_completed
                    catalog = work_catalog or WorkCatalog(self.catalog_root)
                    validate_work_rebuild_completed(plan, ledger, catalog, catalog.rebuild_context)
            except (ValueError, RuntimeError, OSError, KeyError) as exc:
                return RecoverySnapshot('evidence_conflict', str(exc), requires_external_action=True)
            return self._diagnose_catalog(decision, plan, completed_ledger=ledger, work_catalog=work_catalog)
        # A removed registered directory is a materialization observation,
        # not an instruction to discard Catalog identity. Actual generation
        # performs the full current task/ownership/peer closure again.
        if any(not entry.target_path.exists() for entry in plan.entries):
            try:
                self._inspect_completed_media(plan, ledger)
                binding = recovery_binding(plan, decision)
                proven = False
                if binding is not None and binding.backend == 'series' and self.series_catalog_root is not None:
                    match = SeriesCatalog(self.series_catalog_root).load(binding.key)
                    proven = _series_catalog_materializes_plan(match, plan, completed_ledger=ledger)
                elif binding is not None and binding.backend == 'work':
                    from .completed_rebuild_work import _origin_valid
                    catalog = work_catalog or WorkCatalog(self.catalog_root)
                    _origin_valid(catalog, catalog.load(binding.key), plan, ledger)
                    proven = True
                if proven:
                    return RecoverySnapshot('materialization_missing', '已登记的目标文件缺失；可核查并生成新的待批准计划',
                        can_rebuild_plan=True, allowed_actions=('rebuild_plan',))
            except (ValueError, RuntimeError, OSError, KeyError):
                pass  # Retain the existing detailed diagnostic, never grant writes.
        targets = [entry.target_path for entry in plan.entries]
        missing_targets = [path for path in targets if not path.is_file()]
        ledger_entries = ledger.get("entries")
        has_reused_target = any(
            entry.collision_resolution is not None for entry in plan.entries
        )
        if has_reused_target:
            if (
                not isinstance(ledger_entries, list)
                or len(ledger_entries) != len(plan.entries)
            ):
                return RecoverySnapshot(
                    "evidence_conflict", "completed execution record 条目数量不匹配",
                    requires_external_action=True,
                )
        else:
            ledger_entries = [None] * len(plan.entries)
        for entry, recorded in zip(plan.entries, ledger_entries):
            if not entry.target_path.is_file():
                continue
            if entry.collision_resolution is not None:
                if (
                    not isinstance(recorded, dict)
                    or recorded.get("source") != str(entry.source_path)
                    or recorded.get("target") != str(entry.target_path)
                    or recorded.get("status") != "reused_identical_asset"
                    or recorded.get("transfer_type") != "reuse_existing"
                ):
                    return RecoverySnapshot(
                        "evidence_conflict", "既有目标复用结果与已批准 Plan 不一致",
                        requires_external_action=True,
                    )
                try:
                    validate_reuse_resolution(
                        entry.source_path,
                        entry.target_path,
                        entry.collision_resolution,
                    )
                except AssetCollisionError as exc:
                    return RecoverySnapshot(
                        "evidence_conflict", str(exc),
                        requires_external_action=True,
                    )
                continue
            source_stat = entry.source_path.stat()
            target_stat = entry.target_path.stat()
            if (
                source_stat.st_dev != target_stat.st_dev
                or source_stat.st_ino != target_stat.st_ino
                or target_stat.st_nlink < 2
            ):
                return RecoverySnapshot(
                    "evidence_conflict", f"目标不再是源文件的 hardlink：{entry.target_path}",
                    requires_external_action=True,
                )
        catalog_snapshot = self._diagnose_catalog(
            decision,
            plan,
            completed_ledger=ledger,
            work_catalog=work_catalog,
        )
        if catalog_snapshot.state in {"evidence_conflict", "catalog_corrupt", "catalog_root_missing"}:
            return catalog_snapshot
        if missing_targets:
            if catalog_snapshot.state == "catalog_record_missing":
                return RecoverySnapshot(
                    "materialization_missing",
                    "作品登记记录与部分或全部目标 hardlink 同时缺失；"
                    "可从可信源文件和 completed ledger 生成新的未批准整理计划",
                    can_rebuild_plan=True,
                    allowed_actions=("rebuild_plan",),
                )
            return RecoverySnapshot(
                "materialization_missing", f"{len(missing_targets)} 个目标 hardlink 缺失",
                can_rebuild_plan=True,
                allowed_actions=("rebuild_plan",),
            )
        return catalog_snapshot

    def _diagnose_catalog(
        self,
        decision: DecisionState,
        plan: RenamePlan,
        *,
        completed_ledger: Optional[Mapping[str, object]] = None,
        work_catalog: Optional[WorkCatalog] = None,
    ) -> RecoverySnapshot:
        binding = recovery_binding(plan, decision)
        series_update_pending = False
        series_registration_conflict = False
        if binding is not None and binding.backend == "series" and self.series_catalog_root is not None:
            path = self.series_catalog_root / f"{binding.key}.json"
            try:
                from .catalog_namespace import writer_scope
                catalog = SeriesCatalog(self.series_catalog_root)
                with writer_scope(catalog, binding.key):
                    catalog.require_namespace_plan(plan, binding.key)
                    if not path.is_file():
                        return self._missing_catalog_record()
                    match = catalog.load(binding.key)
            except ProjectionAuthorityError:
                return untrusted_projection_snapshot()
            except (SeriesCatalogError, ValueError, OSError) as exc:
                return RecoverySnapshot(
                    "catalog_corrupt",
                    f"SeriesCatalog 记录损坏：{exc}",
                    requires_external_action=True,
                )
            canonical_root = match.canonical_root
            if (plan.metadata.get('completed_rebuild_observation') is not None
                and match.revision == binding.revision
                and _series_catalog_materializes_plan(match, plan, completed_ledger=completed_ledger)):
                series_update_pending = False
            elif match.revision == binding.revision:
                series_update_pending = True
            elif match.revision == binding.revision + 1:
                if not _series_catalog_materializes_plan(
                    match, plan, completed_ledger=completed_ledger
                ):
                    series_registration_conflict = True
            else:
                return RecoverySnapshot(
                    "catalog_corrupt",
                    "SeriesCatalog 版本与待恢复整理计划不一致",
                    requires_external_action=True,
                )
        elif binding is not None and binding.backend == "work":
            path = self.catalog_root / f"{binding.key}.json"
            if not path.is_file():
                return self._missing_catalog_record()
            try:
                catalog = work_catalog or WorkCatalog(self.catalog_root)
                payload = catalog.load(binding.key)
            except WorkCatalogError as exc:
                return RecoverySnapshot(
                    "catalog_corrupt",
                    f"WorkCatalog 记录损坏：{exc}",
                    requires_external_action=True,
                )
            canonical_root = Path(str(payload.get("canonical_root") or ""))
            revision = int(payload.get("revision") or 0)
            if not canonical_root.is_dir():
                return RecoverySnapshot(
                    "catalog_root_missing",
                    f"作品登记目录不存在：{canonical_root}",
                    requires_external_action=True,
                    allowed_actions=("relocate_catalog", "confirm_catalog_detach"),
                )
            if not catalog.compatible_base(
                payload, plan, allow_explicit_provider_append=True
            ):
                return RecoverySnapshot(
                    "catalog_corrupt",
                    "作品登记与当前整理计划的作品身份不一致",
                    requires_external_action=True,
                )
            if (plan.metadata.get('completed_rebuild_observation') is not None
                and revision == binding.revision and completed_ledger is not None
                and catalog.materializes_completed_plan(payload, plan, completed_ledger)):
                series_update_pending = False
            elif revision == binding.revision:
                series_update_pending = True
            elif revision == binding.revision + 1:
                if (
                    completed_ledger is None
                    or not catalog.materializes_completed_plan(
                        payload, plan, completed_ledger
                    )
                ):
                    series_registration_conflict = True
            else:
                series_registration_conflict = True
        else:
            return RecoverySnapshot(
                "healthy", "登记、ledger 和 hardlink 证明一致"
            )

        if not canonical_root.is_dir():
            return RecoverySnapshot(
                "catalog_root_missing",
                f"作品登记目录不存在：{canonical_root}",
                requires_external_action=True,
                allowed_actions=("relocate_catalog", "confirm_catalog_detach"),
            )
        if series_registration_conflict:
            return RecoverySnapshot(
                "catalog_corrupt",
                "作品登记已被其他任务更新，当前整理计划尚未登记",
                requires_external_action=True,
            )
        if series_update_pending:
            return RecoverySnapshot(
                "catalog_update_pending",
                "目标文件完整，但本季度登记尚未写入",
                can_rebuild_catalog=True,
                allowed_actions=("rebuild_catalog", "confirm_catalog_detach"),
            )
        return RecoverySnapshot("healthy", "登记、ledger 和 hardlink 证明一致")

    @staticmethod
    def _missing_catalog_record() -> RecoverySnapshot:
        return RecoverySnapshot(
            "catalog_record_missing",
            "目标 hardlink 完整，但本地作品登记记录缺失",
            can_rebuild_catalog=True,
            allowed_actions=("rebuild_catalog", "confirm_catalog_detach"),
        )

    def rebuild_missing_materialization_plan(
        self,
        decision: DecisionState,
        plan: RenamePlan,
        plan_store: PlanStore,
    ) -> RenamePlan:
        snapshot = self.diagnose(decision, plan)
        if snapshot.state != "materialization_missing":
            raise ValueError("只有 materialization_missing 可以重建未批准 Plan")
        metadata = dict(plan.metadata)
        metadata.update({
            "decision_state_revision": decision.revision,
            "decision_state_fingerprint": DecisionStateStoreFingerprint(decision),
            "recovery_reason": (
                "catalog_record_and_materialization_missing"
                if not self._catalog_record_exists(decision, plan)
                else "materialization_missing"
            ),
            "recovery_nonce": os.urandom(16).hex(),
        })
        revised_entries = []
        for entry in plan.entries:
            collision = None
            if entry.target_path.is_file():
                collision = build_reuse_resolution(
                    entry.source_path, entry.target_path
                )
                if collision is None:
                    raise ValueError(
                        f"既有目标与源文件证据不一致：{entry.target_path}"
                    )
            revised_entries.append(
                replace(entry, collision_resolution=collision)
            )
        # Derive only an unapproved recovery draft. The caller commits it with
        # the Decision revision through the canonical projection boundary.
        # Writing here as well created two Plan writes for one user action.
        return replace(plan, entries=tuple(revised_entries), metadata=metadata)

    def rebuild_catalog_detached_plan(
        self,
        decision: DecisionState,
        plan: RenamePlan,
        *,
        planner: Callable[[DecisionState, RenamePlan], RenamePlan],
    ) -> RenamePlan:
        """Delegate a detached Plan rebuild to the current inventory resolver.

        RecoveryService owns the recovery transition, but it deliberately does
        not know how configured library roots or inventory entries are mapped.
        The caller must supply the current planner and persist/approve the
        returned revision through the normal DecisionCommandService boundary.
        """

        if plan.metadata.get("recovery_reason") != "catalog_detached":
            raise ValueError("只有 catalog_detached 中间 Plan 可以重新规划")
        revised = planner(decision, plan)
        if revised.task_id != plan.task_id:
            raise ValueError("重规划返回了其他任务的 Plan")
        metadata = {
            key: value
            for key, value in dict(revised.metadata).items()
            if key not in {"recovery_reason", "recovery_nonce"}
        }
        return replace(revised, metadata=metadata)

    def _catalog_record_exists(
        self, decision: DecisionState, plan: RenamePlan
    ) -> bool:
        binding = recovery_binding(plan, decision)
        if binding is not None and binding.backend == "series" and self.series_catalog_root is not None:
            return (self.series_catalog_root / f"{binding.key}.json").is_file()
        if binding is not None and binding.backend == "work":
            return (self.catalog_root / f"{binding.key}.json").is_file()
        return True

    def rebuild_catalog(
        self,
        decision: DecisionState,
        plan: RenamePlan,
        work_catalog: WorkCatalog,
        series_catalog: Optional[SeriesCatalog] = None,
    ) -> dict:
        binding = recovery_binding(plan, decision)
        if binding is not None and binding.backend == "series":
            if series_catalog is None:
                raise ValueError("TV 登记恢复缺少 SeriesCatalog")
            with series_catalog.lock(binding.key):
                series_catalog.require_namespace_plan(plan, binding.key)
                snapshot = self.diagnose(decision, plan)
                ledger = self._trusted_ledger(plan)
                if ledger is None:
                    raise ValueError("execution ledger 不可信")
                if snapshot.state == "healthy":
                    current = series_catalog.load(binding.key)
                    if _series_catalog_materializes_plan(
                        current, plan, completed_ledger=ledger
                    ):
                        return current.__dict__
                if snapshot.state not in {
                    "catalog_record_missing", "catalog_update_pending"
                }:
                    raise ValueError(
                        "只有登记缺失或登记待更新可以从执行记录重建登记"
                    )
                return series_catalog.register_completed(
                    plan,
                    expected_revision=int(
                        plan.metadata.get("series_catalog_revision") or 0
                    ),
                    completed_ledger=ledger,
                ).__dict__
        if binding is None or binding.backend != "work":
            raise ValueError("当前整理计划没有可恢复的电影作品登记")
        with work_catalog.lock(binding.key):
            work_catalog.require_namespace_plan(plan, binding.key)
            snapshot = self.diagnose(
                decision, plan, work_catalog=work_catalog
            )
            ledger = self._trusted_ledger(plan)
            if ledger is None:
                raise ValueError("execution ledger 不可信")
            if snapshot.state == "healthy":
                current = work_catalog.load(binding.key)
                if work_catalog.materializes_completed_plan(current, plan, ledger):
                    return current
            if snapshot.state not in {
                "catalog_record_missing", "catalog_update_pending"
            }:
                raise ValueError("只有登记缺失或登记待更新可以从执行记录重建登记")
            return work_catalog.register_completed(
                plan,
                ledger,
                expected_revision=int(plan.metadata.get("catalog_revision") or 0),
                lock_held=True,
            )

    def _trusted_ledger(self, plan: RenamePlan) -> Optional[dict]:
        path = self.record_root / f"{plan.task_id}.ledger.json"
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            return None
        if payload.get("task_id") != plan.task_id:
            return None
        if payload.get("plan_fingerprint") != PlanStore.fingerprint(plan):
            return None
        return payload


def DecisionStateStoreFingerprint(state: DecisionState) -> str:
    # Local helper avoids giving RecoveryService write access to the store.
    from .decision_state import DecisionStateStore
    return DecisionStateStore.fingerprint(state)


def _series_catalog_materializes_plan(
    match,
    plan: RenamePlan,
    *,
    completed_ledger: Optional[Mapping[str, object]] = None,
) -> bool:
    metadata = plan.metadata
    season_number = metadata.get("season_number")
    binding_id = str(metadata.get("season_binding_id") or "")
    if (
        not binding_id
        or not isinstance(season_number, int)
        or plan.work_identity is None
    ):
        return False
    expected_provider = plan.work_identity.primary_provider.to_dict()
    from .season_identity import binding_contains
    expected_source = str(metadata.get("season_source") or "provider")
    binding_found = any(
        isinstance(item, Mapping)
        and binding_contains(item, binding_id)
        and item.get("season_number") == season_number
        and item.get("task_id") == plan.task_id
        and item.get("decision_source") == expected_source
        and isinstance(item.get("provider_identity"), Mapping)
        and dict(item["provider_identity"]) == expected_provider
        for item in match.season_bindings
    )
    if not binding_found:
        return False

    return True


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(Path(path)))
