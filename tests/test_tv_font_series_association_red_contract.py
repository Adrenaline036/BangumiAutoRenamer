"""Production contracts for TV font archives across completed seasons.

The provider transport stays frozen/offline, but task creation, provider
selection, ``Rename.process/_process``, Plan persistence, approval, the queue
worker, ``Trans`` and catalog registration all use production code.

The contracts run as ordinary tests after the GREEN implementation checkpoint.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import replace
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import src.pages.plan_review_page as review_page
import src.utils.utils as utils_module
import tests.test_r1_real_media_production_red_contract as r1_fixture
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.plan_execution import PlanExecutionService
from src.rename.plan_review import PlanReviewError, PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.recovery_service import RecoveryService
from src.rename.series_catalog import SeriesCatalog, SeriesCatalogError
from src.rename.work_catalog import WorkCatalog
from src.task_queue import TaskQueueManager


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TVFontSeriesAssociationRedContracts(unittest.IsolatedAsyncioTestCase):
    async def _run_flow(
        self,
        *,
        root: Path,
        source: Path,
        task_id: str,
        subject_id: str,
    ) -> SimpleNamespace:
        harness = SimpleNamespace(
            _NoopReviewDialog=(
                r1_fixture.R1RealQueueWorkerContracts._NoopReviewDialog
            )
        )
        with (
            patch(
                "src.rename.process.SERIES_CATALOG_PATH",
                root / "series_catalog",
            ),
            patch("src.rename.process.RECORD_PATH", root / "records"),
            patch("src.rename.process.is_dry_run_enabled", return_value=False),
        ):
            return await r1_fixture.R1RealQueueWorkerContracts._run_flow(
                harness,
                root=root,
                source=source,
                task_id=task_id,
                candidate=r1_fixture._candidate(subject_id),
            )

    async def _approve_and_execute(
        self,
        *,
        root: Path,
        result: SimpleNamespace,
        fail_series_registration: bool = False,
    ) -> tuple[dict, dict, PlanExecutionService]:
        store = PlanStore(root / "plans")
        review = PlanReviewService(
            store,
            root / "reviews",
            root / "work_catalog",
        )
        plan = store.load(result.plan.task_id)
        fingerprint = store.fingerprint(plan)
        dialog = SimpleNamespace(
            review_service=review,
            plan=plan,
            selected_ids={entry_id(item) for item in plan.entries},
            task_data=dict(result.task),
            decision=None,
            review_badge=SimpleNamespace(text=""),
            _refresh_plan_header=MagicMock(),
            _render_entries=MagicMock(),
            _refresh_execute_button=MagicMock(),
        )
        with (
            patch.object(utils_module, "TASK_PATH", root / "tasks"),
            patch.object(review_page, "notify"),
        ):
            PlanReviewDialog._approve(
                dialog,
                tuple(entry_id(item) for item in plan.entries),
            )

        service = PlanExecutionService(
            store,
            review,
            task_root=root / "tasks",
            record_root=root / "records",
            catalog_root=root / "work_catalog",
            series_catalog_root=root / "series_catalog",
            decision_state_root=root / "decisions",
            recovery_transaction_root=root / "recovery_transactions",
            provider_link_catalog_root=root / "provider_links",
        )
        manager = TaskQueueManager(
            task_root=root / "tasks",
            execution_service=service,
            decision_state_root=root / "decisions",
            recovery_abandon_audit_root=root / "abandon_audit",
            plan_root=root / "plans",
            test_mode=False,
        )
        await manager.start()
        try:
            registration_patch = (
                patch.object(
                    service.series_catalog,
                    "register_completed",
                    side_effect=SeriesCatalogError(
                        "RED synthetic SeriesCatalog failure"
                    ),
                )
                if fail_series_registration
                else patch.object(
                    service.series_catalog,
                    "register_completed",
                    wraps=service.series_catalog.register_completed,
                )
            )
            expected_failure_log = (
                patch("src.task_queue.logger.exception")
                if fail_series_registration
                else nullcontext()
            )
            with (
                registration_patch,
                expected_failure_log as failure_log,
                patch(
                    "src.rename.plan_execution.is_dry_run_enabled",
                    return_value=False,
                ),
            ):
                await manager.enqueue_approved(plan.task_id, fingerprint)
                await asyncio.wait_for(manager._queue.join(), timeout=8)
            if fail_series_registration:
                self.assertEqual(1, failure_log.call_count)
        finally:
            await manager.stop()

        task = json.loads(
            (root / "tasks" / f"{plan.task_id}.json").read_text("utf-8")
        )
        ledger_path = root / "records" / f"{plan.task_id}.ledger.json"
        ledger = (
            json.loads(ledger_path.read_text("utf-8"))
            if ledger_path.is_file()
            else {}
        )
        return task, ledger, service

    async def _materialize_s1(
        self,
        root: Path,
        *,
        font_bytes: bytes = b"one-unified-series-font-package",
    ) -> tuple[SimpleNamespace, Path, Path]:
        source = r1_fixture._make_season(
            root / "sources",
            "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
            "[VCB-Studio] Yama no Susume",
        )
        font = source / "[VCB-Studio] Yama no Susume [Fonts].7z"
        font.write_bytes(font_bytes)
        result = await self._run_flow(
            root=root,
            source=source,
            task_id="tv-font-s1-production",
            subject_id="41658",
        )
        task, _ledger, _service = await self._approve_and_execute(
            root=root,
            result=result,
        )
        self.assertEqual("completed", task.get("execution_status"))
        font_entry = next(
            item for item in result.plan.entries if item.source_path == font
        )
        self.assertTrue(os.path.samefile(font, font_entry.target_path))
        return result, font, font_entry.target_path

    async def test_real_s1_s2_reuse_keeps_inode_and_materializes_two_associations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s1, _s1_font, target = await self._materialize_s1(root)
            target_before = target.stat()
            target_hash_before = _sha256(target)

            source = r1_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Second Season",
                ova=True,
            )
            s2_font = (
                source
                / "[VCB-Studio] Yama no Susume [Fonts].7z"
            )
            s2_font.write_bytes(b"one-unified-series-font-package")
            source_before = s2_font.stat()
            s2 = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-s2-production",
                subject_id="86670",
            )
            s2_entry = next(
                item for item in s2.plan.entries if item.source_path == s2_font
            )
            problems = []
            if (
                s2_entry.collision_resolution is None
                or s2_entry.collision_resolution.action != "reuse_existing"
            ):
                problems.append("S2 production Plan did not freeze reuse_existing")

            manifest = s2.plan.metadata.get("associated_asset_manifest")
            manifest_assets = (
                manifest.get("assets") if isinstance(manifest, Mapping) else None
            )
            manifest_row = next(
                (
                    item
                    for item in manifest_assets or ()
                    if isinstance(item, Mapping)
                    and item.get("source_path") == str(s2_font)
                ),
                None,
            )
            if not (
                isinstance(manifest, Mapping)
                and manifest.get("schema_version") == 1
                and isinstance(manifest_assets, (list, tuple))
                and isinstance(manifest_row, Mapping)
                and manifest_row.get("role") == "fonts"
                and manifest_row.get("season_number") == 2
                and manifest_row.get("target_path") == str(target)
                and manifest_row.get("source_size") == s2_font.stat().st_size
                and manifest_row.get("source_sha256") == _sha256(s2_font)
            ):
                problems.append(
                    "S2 Plan did not persist the authoritative frozen font source manifest"
                )

            s2_task, s2_ledger, _service = await self._approve_and_execute(
                root=root,
                result=s2,
            )
            if s2_task.get("execution_status") != "completed":
                problems.append(
                    "S2 execution ended as "
                    f"{s2_task.get('execution_status')}: {s2_task.get('error')}"
                )
            target_after = target.stat()
            source_after = s2_font.stat()
            if (
                (target_after.st_dev, target_after.st_ino, target_after.st_nlink)
                != (target_before.st_dev, target_before.st_ino, target_before.st_nlink)
                or target_after.st_size != target_before.st_size
                or _sha256(target) != target_hash_before
            ):
                problems.append("reuse changed the existing target inode/bytes/nlink")
            if (
                (source_after.st_dev, source_after.st_ino, source_after.st_nlink)
                != (source_before.st_dev, source_before.st_ino, source_before.st_nlink)
                or os.path.samefile(s2_font, target)
            ):
                problems.append("reuse changed or hardlinked the independent S2 source")
            font_ledger = next(
                (
                    item
                    for item in s2_ledger.get("entries", ())
                    if item.get("source") == str(s2_font)
                ),
                {},
            )
            if (
                font_ledger.get("status") != "reused_identical_asset"
                or font_ledger.get("transfer_type") != "reuse_existing"
            ):
                problems.append("S2 execution history did not record the reuse action")

            series_key = str(s2.plan.metadata.get("series_key") or "")
            if series_key != str(s1.plan.metadata.get("series_key") or ""):
                problems.append("S1 and S2 did not converge on one series key")
            catalog_path = root / "series_catalog" / f"{series_key}.json"
            payload = json.loads(catalog_path.read_text("utf-8"))
            assets = payload.get("associated_assets")
            physical = next(
                (
                    item
                    for item in assets or ()
                    if isinstance(item, dict)
                    and item.get("target_path") == str(target)
                ),
                None,
            )
            associations = (
                physical.get("associations")
                if isinstance(physical, dict)
                else None
            )
            if not (
                payload.get("schema_version") == 3
                and isinstance(assets, list)
                and len(assets) == 1
                and isinstance(physical, dict)
                and physical.get("role") == "fonts"
                and physical.get("scope") == "series"
                and physical.get("size") == target.stat().st_size
                and physical.get("sha256") == _sha256(target)
                and isinstance(associations, list)
                and {item.get("season_number") for item in associations} == {1, 2}
                and {item.get("task_id") for item in associations}
                == {s1.plan.task_id, s2.plan.task_id}
                and len(
                    {
                        item.get("association_id")
                        for item in associations
                        if item.get("association_id")
                    }
                )
                == 2
            ):
                problems.append(
                    "SeriesCatalog v3 did not persist one physical font asset with S1/S2 associations"
                )

            old_root = Path(str(payload.get("canonical_root") or ""))
            new_root = root / "Anime" / "向山进发 relocated (2013)"
            moved_target = new_root / target.relative_to(old_root)
            old_root.rename(new_root)
            try:
                relocated = SeriesCatalog(root / "series_catalog").relocate(
                    series_key,
                    new_root,
                    allowed_root=root / "Anime",
                    record_root=root / "records",
                    task_root=root / "tasks",
                )
            except SeriesCatalogError as exc:
                problems.append(f"series relocation rejected valid reused font evidence: {exc}")
            else:
                moved_payload = json.loads(catalog_path.read_text("utf-8"))
                moved_assets = moved_payload.get("associated_assets") or ()
                if not (
                    relocated.canonical_root == new_root
                    and not target.exists()
                    and moved_target.is_file()
                    and _sha256(moved_target) == target_hash_before
                    and len(moved_assets) == 1
                    and moved_assets[0].get("target_path") == str(moved_target)
                    and moved_assets[0].get("asset_id") == physical.get("asset_id")
                ):
                    problems.append(
                        "series relocation left the reused physical font asset at the old root"
                    )

            self.assertEqual([], problems, "\n".join(problems))

    async def test_completed_plan_replay_is_idempotent_and_wrong_season_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s1, s1_font, _target = await self._materialize_s1(root)
            series_key = str(s1.plan.metadata.get("series_key") or "")
            catalog_path = root / "series_catalog" / f"{series_key}.json"
            payload = json.loads(catalog_path.read_text("utf-8"))
            revision = int(payload.get("revision") or 0)
            problems = []

            manifest = s1.plan.metadata.get("associated_asset_manifest")
            rows = manifest.get("assets") if isinstance(manifest, Mapping) else None
            row = next(
                (
                    item
                    for item in rows or ()
                    if isinstance(item, Mapping)
                    and item.get("source_path") == str(s1_font)
                ),
                None,
            )
            if not (
                isinstance(manifest, Mapping)
                and manifest.get("schema_version") == 1
                and isinstance(row, Mapping)
                and row.get("source_size") == s1_font.stat().st_size
                and row.get("source_sha256") == _sha256(s1_font)
                and row.get("season_number") == 1
            ):
                problems.append("completed S1 Plan lacks its frozen font source manifest")

            conflict_root = root / "conflict_catalog"
            conflict_root.mkdir()
            shutil.copy2(catalog_path, conflict_root / catalog_path.name)
            conflicting_plan = replace(
                s1.plan,
                metadata={**dict(s1.plan.metadata), "season_number": 2},
            )
            try:
                SeriesCatalog(conflict_root).register_completed(
                    conflicting_plan,
                    expected_revision=revision,
                )
            except SeriesCatalogError:
                pass
            else:
                problems.append(
                    "same completed task/source was accepted with a conflicting season"
                )

            before_replay = catalog_path.read_bytes()
            replay = SeriesCatalog(root / "series_catalog").register_completed(
                s1.plan,
                expected_revision=revision,
            )
            if (
                replay.revision != revision
                or catalog_path.read_bytes() != before_replay
            ):
                problems.append("exact completed-Plan replay was not a byte-stable no-op")

            legacy_root = root / "legacy_series_catalog"
            legacy_root.mkdir()
            legacy_payload = dict(payload)
            legacy_payload["schema_version"] = 1
            legacy_payload.pop("associated_assets", None)
            legacy_path = legacy_root / catalog_path.name
            legacy_path.write_text(
                json.dumps(legacy_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            legacy_catalog = SeriesCatalog(legacy_root)
            upgraded = legacy_catalog.register_completed(
                s1.plan,
                expected_revision=revision,
            )
            upgraded_payload = json.loads(legacy_path.read_text("utf-8"))
            upgraded_bytes = legacy_path.read_bytes()
            replayed_upgrade = legacy_catalog.register_completed(
                s1.plan,
                expected_revision=upgraded.revision,
            )
            if not (
                upgraded_payload.get("schema_version") == 3
                and len(upgraded_payload.get("season_bindings") or ()) == 1
                and len(upgraded_payload.get("associated_assets") or ()) == 1
                and replayed_upgrade.revision == upgraded.revision
                and legacy_path.read_bytes() == upgraded_bytes
            ):
                problems.append(
                    "trusted v1 Catalog upgrade did not preserve binding/add one asset/replay byte-stably"
                )
            self.assertEqual([], problems, "\n".join(problems))

    async def test_reused_font_catalog_failure_stays_recoverable_from_frozen_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _s1, _s1_font, target = await self._materialize_s1(root)
            target_before = target.stat()
            target_hash = _sha256(target)

            source = r1_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Second Season",
                ova=True,
            )
            s2_font = (
                source
                / "[VCB-Studio] Yama no Susume [Fonts].7z"
            )
            s2_font.write_bytes(b"one-unified-series-font-package")
            s2_source_before = s2_font.stat()
            s2 = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-s2-catalog-recovery",
                subject_id="86670",
            )
            task, ledger, _service = await self._approve_and_execute(
                root=root,
                result=s2,
                fail_series_registration=True,
            )
            self.assertEqual("catalog_update_pending", task.get("execution_status"))
            self.assertEqual(
                "completed",
                ledger.get("status"),
                task.get("error"),
            )

            recovery = RecoveryService(
                catalog_root=root / "work_catalog",
                record_root=root / "records",
                series_catalog_root=root / "series_catalog",
            )
            problems = []
            foreign_root = root / "foreign_series_catalog"
            shutil.copytree(root / "series_catalog", foreign_root)
            SeriesCatalog(foreign_root).bind_season(
                str(s2.plan.metadata["series_key"]),
                expected_revision=int(s2.plan.metadata["series_catalog_revision"]),
                season_number=4,
                provider_identity=s2.plan.work_identity.primary_provider.to_dict(),
                decision_source="foreign_task",
                binding_id="season-binding-foreign-task",
            )
            foreign_snapshot = RecoveryService(
                catalog_root=root / "work_catalog",
                record_root=root / "records",
                series_catalog_root=foreign_root,
            ).diagnose(s2.state, s2.plan)
            if foreign_snapshot.state != "catalog_corrupt":
                problems.append(
                    "foreign revision+1 was mistaken for this Plan's completed registration"
                )

            snapshot = recovery.diagnose(s2.state, s2.plan)
            if not (
                snapshot.can_rebuild_catalog
                and "rebuild_catalog" in snapshot.allowed_actions
            ):
                problems.append(
                    f"legitimate reuse recovery was diagnosed as {snapshot.state}"
                )
            try:
                recovery.rebuild_catalog(
                    s2.state,
                    s2.plan,
                    WorkCatalog(root / "work_catalog"),
                    SeriesCatalog(root / "series_catalog"),
                )
            except (ValueError, SeriesCatalogError) as exc:
                problems.append(f"SeriesCatalog recovery rejected frozen reuse: {exc}")

            payload = json.loads(
                (
                    root
                    / "series_catalog"
                    / f"{s2.plan.metadata['series_key']}.json"
                ).read_text("utf-8")
            )
            assets = payload.get("associated_assets") or ()
            associations = [
                association
                for asset in assets
                if isinstance(asset, dict)
                for association in asset.get("associations", ())
                if isinstance(association, dict)
            ]
            if not any(
                item.get("task_id") == s2.plan.task_id
                and item.get("season_number") == 2
                for item in associations
            ):
                problems.append("recovery did not materialize the pending S2 association")

            target_after = target.stat()
            s2_source_after = s2_font.stat()
            if (
                (target_after.st_dev, target_after.st_ino, target_after.st_nlink)
                != (target_before.st_dev, target_before.st_ino, target_before.st_nlink)
                or _sha256(target) != target_hash
                or (
                    s2_source_after.st_dev,
                    s2_source_after.st_ino,
                    s2_source_after.st_nlink,
                )
                != (
                    s2_source_before.st_dev,
                    s2_source_before.st_ino,
                    s2_source_before.st_nlink,
                )
            ):
                problems.append("catalog recovery changed media or font link state")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_different_same_name_font_uses_independent_content_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _s1, _s1_font, target = await self._materialize_s1(root)
            target_before = target.read_bytes()
            catalog_path = next((root / "series_catalog").glob("series-*.json"))
            catalog_before = catalog_path.read_bytes()

            source = r1_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Second Season",
                ova=True,
            )
            conflicting_font = (
                source
                / "[VCB-Studio] Yama no Susume [Fonts].7z"
            )
            conflicting_font.write_bytes(b"different-font-package")
            s2 = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-s2-different-content",
                subject_id="86670",
            )
            verification = PlanVerifier().verify(s2.plan)
            self.assertEqual("accepted", verification.status.value)
            font_entry = next(
                item for item in s2.plan.entries
                if item.source_path == conflicting_font
            )
            expected_target = (
                target.parent
                / f"向山进发 (2013) [{_sha256(conflicting_font)}] [Fonts].7z"
            )
            self.assertEqual(expected_target, font_entry.target_path)
            self.assertIsNone(font_entry.collision_resolution)
            self.assertEqual(target_before, target.read_bytes())
            self.assertEqual(catalog_before, catalog_path.read_bytes())
            self.assertFalse(os.path.samefile(conflicting_font, target))

            task, ledger, _service = await self._approve_and_execute(
                root=root,
                result=s2,
            )
            self.assertEqual("completed", task.get("execution_status"), task.get("error"))
            self.assertTrue(os.path.samefile(conflicting_font, expected_target))
            font_ledger = next(
                item for item in ledger.get("entries", ())
                if item.get("source") == str(conflicting_font)
            )
            self.assertEqual("hardlink", font_ledger.get("transfer_type"))
            payload = json.loads(catalog_path.read_text("utf-8"))
            self.assertEqual(3, payload.get("schema_version"))
            self.assertEqual(2, len(payload.get("associated_assets") or ()))


if __name__ == "__main__":
    unittest.main()
