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
from .rename_plan import RenamePlan
from .work_catalog import WorkCatalog, WorkCatalogError
from .series_catalog import SeriesCatalog, SeriesCatalogError
from .asset_collision import (
    AssetCollisionError,
    build_reuse_resolution,
    fingerprint_file,
    validate_reuse_resolution,
)
from .catalog_binding import recovery_binding
from .utils import is_font_archive
from .font_archive_contract import (
    FontArchiveContractError,
    validate_completed_movie_font_assets,
)


@dataclass(frozen=True)
class RecoverySnapshot:
    state: str
    detail: str
    can_rebuild_catalog: bool = False
    can_rebuild_plan: bool = False
    requires_external_action: bool = False
    allowed_actions: tuple[str, ...] = ()


class RecoveryService:
    def __init__(
        self, *, catalog_root: Path, record_root: Path,
        series_catalog_root: Optional[Path] = None,
    ):
        self.catalog_root = Path(catalog_root)
        self.record_root = Path(record_root)
        self.series_catalog_root = (
            Path(series_catalog_root) if series_catalog_root is not None else None
        )

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
        try:
            validate_completed_movie_font_assets(plan, ledger)
        except FontArchiveContractError as exc:
            return RecoverySnapshot(
                "evidence_conflict", str(exc), requires_external_action=True
            )
        catalog_snapshot = self._diagnose_catalog(
            decision,
            plan,
            completed_ledger=ledger,
            work_catalog=work_catalog,
        )
        if catalog_snapshot.state in {"catalog_corrupt", "catalog_root_missing"}:
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
            if not path.is_file():
                return self._missing_catalog_record()
            try:
                match = SeriesCatalog(self.series_catalog_root).load(binding.key)
            except SeriesCatalogError as exc:
                return RecoverySnapshot(
                    "catalog_corrupt",
                    f"SeriesCatalog 记录损坏：{exc}",
                    requires_external_action=True,
                )
            canonical_root = match.canonical_root
            if match.revision == binding.revision:
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
            if revision == binding.revision:
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
    expected_source = str(metadata.get("season_source") or "provider")
    binding_found = any(
        isinstance(item, Mapping)
        and item.get("season_binding_id") == binding_id
        and item.get("season_number") == season_number
        and item.get("task_id") == plan.task_id
        and item.get("decision_source") == expected_source
        and isinstance(item.get("provider_identity"), Mapping)
        and dict(item["provider_identity"]) == expected_provider
        for item in match.season_bindings
    )
    if not binding_found:
        return False

    manifest = metadata.get("associated_asset_manifest")
    if manifest is None:
        tv_schema = metadata.get("tv_planning_authority_schema_version")
        current_tv_authority = (
            metadata.get("tv_planning_authority_required") is True
            or metadata.get("tv_authority_required") is True
            or (
                isinstance(tv_schema, int)
                and not isinstance(tv_schema, bool)
                and tv_schema > 0
            )
        )
        if current_tv_authority:
            return False
        font_entries = [
            entry for entry in plan.entries if is_font_archive(entry.source_path)
        ]
        if not font_entries:
            return True
        if (
            len(font_entries) != 1
            or not isinstance(completed_ledger, Mapping)
            or completed_ledger.get("status") != "completed"
            or completed_ledger.get("task_id") != plan.task_id
            or completed_ledger.get("plan_fingerprint") != PlanStore.fingerprint(plan)
        ):
            return False
        ledger_entries = completed_ledger.get("entries")
        if not isinstance(ledger_entries, list) or len(ledger_entries) != len(plan.entries):
            return False
        entry = font_entries[0]
        recorded = next(
            (
                item for item in ledger_entries
                if isinstance(item, Mapping)
                and item.get("source") == str(entry.source_path)
                and item.get("target") == str(entry.target_path)
            ),
            None,
        )
        if not isinstance(recorded, Mapping):
            return False
        try:
            source_evidence = fingerprint_file(entry.source_path)
            target_evidence = fingerprint_file(entry.target_path)
        except (AssetCollisionError, OSError):
            return False
        if (
            source_evidence.size != target_evidence.size
            or source_evidence.sha256 != target_evidence.sha256
        ):
            return False
        if entry.collision_resolution is None:
            try:
                target_nlink = entry.target_path.stat().st_nlink
            except OSError:
                return False
            if (
                recorded.get("status") not in {None, "created"}
                or recorded.get("transfer_type") != "hardlink"
                or source_evidence.device != target_evidence.device
                or source_evidence.inode != target_evidence.inode
                or target_nlink < 2
            ):
                return False
        elif (
            recorded.get("status") != "reused_identical_asset"
            or recorded.get("transfer_type") != "reuse_existing"
        ):
            return False
        rows = ({
            "source_path": str(entry.source_path),
            "target_path": str(entry.target_path),
            "source_size": source_evidence.size,
            "source_sha256": source_evidence.sha256,
        },)
    else:
        if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
            return False
        rows = manifest.get("assets")
        if not isinstance(rows, (list, tuple)):
            return False
    fingerprint = PlanStore.fingerprint(plan)
    for row in rows:
        if not isinstance(row, Mapping):
            return False
        source = _path_key(Path(str(row.get("source_path") or "")))
        target = _path_key(Path(str(row.get("target_path") or "")))
        size = row.get("source_size")
        digest = str(row.get("source_sha256") or "")
        association_found = False
        for asset in match.associated_assets:
            if (
                not isinstance(asset, Mapping)
                or _path_key(Path(str(asset.get("target_path") or ""))) != target
                or asset.get("size") != size
                or asset.get("sha256") != digest
            ):
                continue
            associations = asset.get("associations")
            if not isinstance(associations, list):
                return False
            association_found = any(
                isinstance(item, Mapping)
                and item.get("task_id") == plan.task_id
                and item.get("season_number") == season_number
                and item.get("plan_fingerprint") == fingerprint
                and _path_key(Path(str(item.get("source_path") or ""))) == source
                and item.get("source_size") == size
                and item.get("source_sha256") == digest
                for item in associations
            )
            if association_found:
                break
        if not association_found:
            return False
    return True


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(Path(path)))
