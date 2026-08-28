"""Phase A regression contracts for TV font content identity and legacy repair.

The provider transport is frozen/offline, while task creation, provider
selection, ``Rename.process/_process``, Plan persistence, approval, the queue
worker, transfer boundary and recovery stores use production code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
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
import tests.test_tv_font_series_association_red_contract as green_fixture
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerificationResult, PlanVerifier
from src.rename.rename_plan import PlanStatus
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


def _file_identity(path: Path) -> tuple[int, int, int, str]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_nlink, _sha256(path)


def _font_entry(plan, source: Path):
    return next(item for item in plan.entries if item.source_path == source)


def _asset_identity(asset: Mapping) -> Mapping:
    nested = asset.get("physical_identity")
    return nested if isinstance(nested, Mapping) else asset


def _asset_location(asset: Mapping) -> Mapping:
    nested = asset.get("location")
    return nested if isinstance(nested, Mapping) else asset


def _asset_content_id(asset: Mapping):
    return _asset_identity(asset).get("content_id")


def _asset_target_path(asset: Mapping):
    return _asset_location(asset).get("target_path")


def _asset_size(asset: Mapping):
    return _asset_identity(asset).get("size")


def _asset_sha256(asset: Mapping):
    return _asset_identity(asset).get("sha256")


def _asset_associations(asset: Mapping):
    return asset.get("associations") or ()


def _v2_asset_id(target: Path, *, size: int, digest: str) -> str:
    target_key = os.path.normcase(os.path.abspath(target))
    return hashlib.sha256(
        f"{target_key}\0{size}\0{digest}".encode("utf-8")
    ).hexdigest()


class TVFontContentIdentityRedContracts(unittest.IsolatedAsyncioTestCase):
    async def _run_flow(
        self,
        *,
        root: Path,
        source: Path,
        task_id: str,
        subject_id: str,
    ) -> SimpleNamespace:
        return await green_fixture.TVFontSeriesAssociationRedContracts._run_flow(
            self,
            root=root,
            source=source,
            task_id=task_id,
            subject_id=subject_id,
        )

    async def _approve_and_execute(
        self,
        *,
        root: Path,
        result: SimpleNamespace,
        fail_series_registration: bool = False,
    ):
        return await green_fixture.TVFontSeriesAssociationRedContracts._approve_and_execute(
            self,
            root=root,
            result=result,
            fail_series_registration=fail_series_registration,
        )

    async def _materialize_s1(
        self,
        root: Path,
        *,
        font_bytes: bytes = b"one-unified-series-font-package",
    ):
        return await green_fixture.TVFontSeriesAssociationRedContracts._materialize_s1(
            self,
            root,
            font_bytes=font_bytes,
        )

    def _approve_only(
        self,
        *,
        root: Path,
        result: SimpleNamespace,
    ) -> tuple[object, str]:
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
        return plan, fingerprint

    async def _enqueue_with_transfer_sentinel(
        self,
        *,
        root: Path,
        task_id: str,
        fingerprint: str,
        calls: list[str],
    ) -> dict:
        store = PlanStore(root / "plans")
        review = PlanReviewService(
            store,
            root / "reviews",
            root / "work_catalog",
        )

        def transfer_sentinel(*_args, **_kwargs):
            calls.append("TRANSFER_REACHED")
            raise AssertionError("TRANSFER_REACHED_PHASE_A_SENTINEL")

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
            transfer_factory=transfer_sentinel,
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
            with (
                patch("src.task_queue.logger.exception"),
                patch(
                    "src.rename.plan_execution.is_dry_run_enabled",
                    return_value=False,
                ),
            ):
                await manager.enqueue_approved(task_id, fingerprint)
                await asyncio.wait_for(manager._queue.join(), timeout=8)
        finally:
            await manager.stop()
        return json.loads(
            (root / "tasks" / f"{task_id}.json").read_text("utf-8")
        )

    async def _materialize_legacy_s1(self, root: Path):
        source = r1_fixture._make_season(
            root / "sources",
            "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
            "[VCB-Studio] Yama no Susume",
        )
        font = source / "[VCB-Studio] Yama no Susume [Fonts].7z"
        font.write_bytes(b"one-unified-series-font-package")
        with patch(
            "src.rename.process._freeze_tv_font_asset_contract",
            side_effect=lambda plan: plan,
        ):
            result = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-legacy-s1-production",
                subject_id="41658",
            )
        legacy_plan = PlanStore(root / "plans").load(result.plan.task_id)
        legacy_target = _font_entry(legacy_plan, font).target_path
        self.assertNotIn("associated_asset_manifest", legacy_plan.metadata)
        with patch.object(
            PlanExecutionService,
            "_validate_frozen_associated_assets",
            return_value=None,
        ), patch.object(
            PlanVerifier,
            "verify",
            return_value=PlanVerificationResult(PlanStatus.ACCEPTED, ()),
        ):
            # This test manufactures a pre-manifest historical completion so
            # that the backfill path has durable evidence to repair. Current
            # production Plans remain subject to the strict verifier gate.
            task, ledger, _service = await self._approve_and_execute(
                root=root,
                result=result,
            )
        self.assertEqual(
            "completed",
            task.get("execution_status"),
            task.get("error"),
        )
        self.assertEqual(PlanStore.fingerprint(legacy_plan), ledger.get("plan_fingerprint"))
        self.assertTrue(os.path.samefile(font, legacy_target))

        series_key = str(legacy_plan.metadata["series_key"])
        catalog_path = root / "series_catalog" / f"{series_key}.json"
        payload = json.loads(catalog_path.read_text("utf-8"))
        payload["schema_version"] = 1
        payload.pop("associated_assets", None)
        catalog_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return legacy_plan, ledger, font, legacy_target, catalog_path

    async def test_real_different_basename_retargets_to_content_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                legacy_plan,
                _legacy_ledger,
                _s1_font,
                s1_target,
                catalog_path,
            ) = await self._materialize_legacy_s1(root)
            catalog_before = json.loads(catalog_path.read_text("utf-8"))
            target_before = (
                s1_target.stat().st_dev,
                s1_target.stat().st_ino,
                s1_target.stat().st_nlink,
                _sha256(s1_target),
            )
            problems = []

            source = r1_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Second Season",
                ova=True,
            )
            s2_font = source / "[VCB-Studio] Yama no Susume Second Season [Fonts].7z"
            s2_font.write_bytes(s1_target.read_bytes())
            s2 = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-s2-real-basename",
                subject_id="86670",
            )
            entry = _font_entry(s2.plan, s2_font)
            catalog_after = json.loads(catalog_path.read_text("utf-8"))
            assets = catalog_after.get("associated_assets") or ()
            physical = assets[0] if len(assets) == 1 else {}
            associations = _asset_associations(physical)
            if not (
                catalog_before.get("schema_version") == 1
                and catalog_after.get("schema_version") == 3
                and catalog_after.get("revision") == catalog_before.get("revision") + 1
                and len(associations) == 1
                and associations[0].get("task_id") == legacy_plan.task_id
                and associations[0].get("season_number") == 1
            ):
                problems.append("real S2 production entry did not backfill the legacy S1 association")
            if s2.plan.metadata.get("series_catalog_revision") != catalog_after.get("revision"):
                problems.append("S2 Plan did not freeze the post-backfill Catalog revision")
            if entry.target_path != s1_target:
                problems.append("different basename was not retargeted to the unique content location")
            if (
                entry.collision_resolution is None
                or entry.collision_resolution.action != "reuse_existing"
            ):
                problems.append("different basename did not freeze reuse_existing")
            manifest = s2.plan.metadata.get("associated_asset_manifest")
            rows = manifest.get("assets") if isinstance(manifest, Mapping) else ()
            row = next(
                (
                    item for item in rows or ()
                    if isinstance(item, Mapping)
                    and item.get("source_path") == str(s2_font)
                ),
                None,
            )
            if not isinstance(row, Mapping) or row.get("target_path") != str(s1_target):
                problems.append("S2 manifest did not preserve S2 source and S1 canonical target")
            canonical_root = Path(str(legacy_plan.metadata["canonical_work_root"]))
            if os.path.lexists(canonical_root / "Associated" / s2_font.name):
                problems.append("planning created an unapproved second font location")
            if target_before != (
                s1_target.stat().st_dev,
                s1_target.stat().st_ino,
                s1_target.stat().st_nlink,
                _sha256(s1_target),
            ):
                problems.append("S2 planning changed the legacy S1 font target")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_legacy_backfill_requires_completed_evidence_and_replays_byte_stably(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, ledger, font, target, catalog_path = await self._materialize_legacy_s1(root)
            original = catalog_path.read_bytes()
            original_mtime = catalog_path.stat().st_mtime_ns
            revision = int(json.loads(original)["revision"])
            problems = []

            try:
                SeriesCatalog(root / "series_catalog").register_completed(
                    plan,
                    expected_revision=revision,
                )
            except SeriesCatalogError:
                pass
            else:
                problems.append("legacy font Plan was accepted without completed evidence")
            if catalog_path.read_bytes() != original:
                problems.append("failed legacy backfill changed Catalog bytes")
                catalog_path.write_bytes(original)

            tampered = dict(ledger)
            tampered["plan_fingerprint"] = "0" * 64
            invalid_ledgers = {
                "foreign fingerprint": tampered,
                "missing entry": {**ledger, "entries": []},
                "wrong transfer type": {
                    **ledger,
                    "entries": [
                        {
                            **item,
                            "transfer_type": "copy",
                        }
                        if item.get("source") == str(font) else item
                        for item in ledger.get("entries", ())
                    ],
                },
            }
            for label, invalid_ledger in invalid_ledgers.items():
                catalog_path.write_bytes(original)
                try:
                    SeriesCatalog(root / "series_catalog").register_completed(
                        plan,
                        expected_revision=revision,
                        completed_ledger=invalid_ledger,
                    )
                except (SeriesCatalogError, TypeError):
                    pass
                else:
                    problems.append(f"{label} completed evidence was accepted")
                if catalog_path.read_bytes() != original:
                    problems.append(f"{label} changed Catalog bytes")

            base_payload = json.loads(original)
            base_binding = dict(base_payload["season_bindings"][0])
            incomplete_binding_cases = {
                "missing task id": {
                    **base_binding,
                    "season_binding_id": "season-binding-missing-task",
                    "season_number": 2,
                    "task_id": "",
                },
                "duplicate task id": {
                    **base_binding,
                    "season_binding_id": "season-binding-duplicate-task",
                    "season_number": 2,
                },
            }
            for label, incomplete_binding in incomplete_binding_cases.items():
                malformed_payload = json.loads(original)
                malformed_payload["season_bindings"].append(incomplete_binding)
                catalog_path.write_text(
                    json.dumps(malformed_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                malformed_before = catalog_path.read_bytes()
                try:
                    SeriesCatalog(root / "series_catalog").backfill_legacy_completed(
                        str(plan.metadata["series_key"]),
                        ((plan, ledger),),
                    )
                except SeriesCatalogError:
                    pass
                else:
                    problems.append(f"{label} legacy binding was partially backfilled")
                if catalog_path.read_bytes() != malformed_before:
                    problems.append(f"{label} changed Catalog bytes")
            catalog_path.write_bytes(original)

            second_plan = replace(
                plan,
                task_id="tv-font-legacy-s2-atomic",
                metadata={
                    **dict(plan.metadata),
                    "season_binding_id": "season-binding-legacy-s2-atomic",
                    "season_number": 2,
                },
            )
            second_ledger = json.loads(json.dumps(ledger))
            second_ledger["task_id"] = second_plan.task_id
            second_ledger["plan_fingerprint"] = PlanStore.fingerprint(second_plan)
            second_ledger["entries"] = [
                {
                    **item,
                    "transfer_type": "copy" if item.get("source") == str(font) else item.get("transfer_type"),
                }
                for item in second_ledger.get("entries", ())
            ]
            catalog_path.write_bytes(original)
            try:
                SeriesCatalog(root / "series_catalog").backfill_legacy_completed(
                    str(plan.metadata["series_key"]),
                    ((plan, ledger), (second_plan, second_ledger)),
                )
            except SeriesCatalogError:
                pass
            else:
                problems.append("multi-task legacy backfill accepted later invalid evidence")
            if catalog_path.read_bytes() != original:
                problems.append("failed multi-task legacy backfill was not atomic")

            try:
                upgraded = SeriesCatalog(root / "series_catalog").register_completed(
                    plan,
                    expected_revision=revision,
                    completed_ledger=ledger,
                )
            except TypeError:
                problems.append("SeriesCatalog has no completed-evidence legacy backfill path")
            except SeriesCatalogError as exc:
                problems.append(f"valid legacy evidence was rejected: {exc}")
            else:
                payload = json.loads(catalog_path.read_text("utf-8"))
                assets = payload.get("associated_assets") or ()
                physical = assets[0] if len(assets) == 1 else {}
                associations = _asset_associations(physical)
                if not (
                    payload.get("schema_version") == 3
                    and upgraded.revision == revision + 1
                    and _asset_content_id(physical)
                    and _asset_target_path(physical) == str(target)
                    and _asset_size(physical) == font.stat().st_size
                    and _asset_sha256(physical) == _sha256(font)
                    and len(associations) == 1
                    and associations[0].get("task_id") == plan.task_id
                    and associations[0].get("season_number") == 1
                ):
                    problems.append("valid legacy evidence did not materialize one v3 S1 association")
                upgraded_bytes = catalog_path.read_bytes()
                upgraded_mtime = catalog_path.stat().st_mtime_ns
                try:
                    replay = SeriesCatalog(root / "series_catalog").register_completed(
                        plan,
                        expected_revision=upgraded.revision,
                        completed_ledger=ledger,
                    )
                except (SeriesCatalogError, TypeError) as exc:
                    problems.append(f"exact legacy replay was rejected: {exc}")
                else:
                    if (
                        replay.revision != upgraded.revision
                        or catalog_path.read_bytes() != upgraded_bytes
                        or catalog_path.stat().st_mtime_ns != upgraded_mtime
                    ):
                        problems.append("exact legacy replay was not byte-stable")
            if original_mtime <= 0:
                problems.append("legacy Catalog timestamp evidence was unavailable")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_font_manifest_is_a_closed_set_before_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = r1_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume",
            )
            font = source / "[VCB-Studio] Yama no Susume [Fonts].7z"
            font.write_bytes(b"closed-font-manifest")
            result = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-closed-manifest",
                subject_id="41658",
            )
            manifest = dict(result.plan.metadata["associated_asset_manifest"])
            rows = list(manifest["assets"])
            for altered_rows in ([], rows + rows):
                altered = replace(
                    result.plan,
                    metadata={
                        **dict(result.plan.metadata),
                        "associated_asset_manifest": {
                            **manifest,
                            "assets": altered_rows,
                        },
                    },
                )
                with self.assertRaises(PlanExecutionError):
                    PlanExecutionService._validate_frozen_associated_assets(altered)

    async def test_v3_content_identity_survives_series_relocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_roots = []
            for index, (folder, stem, subject) in enumerate((
                (
                    "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
                    "[VCB-Studio] Yama no Susume",
                    "41658",
                ),
                (
                    "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                    "[VCB-Studio] Yama no Susume Second Season",
                    "86670",
                ),
            )):
                isolated = root / f"deterministic-{index}"
                source = r1_fixture._make_season(
                    isolated / "sources",
                    folder,
                    stem,
                    ova=subject == "86670",
                )
                font = source / f"{stem} [Fonts].7z"
                font.write_bytes(b"same-content-across-order-and-basename")
                result = await self._run_flow(
                    root=isolated,
                    source=source,
                    task_id=f"tv-font-deterministic-{index}",
                    subject_id=subject,
                )
                entry = _font_entry(result.plan, font)
                plan_roots.append(
                    entry.target_path.relative_to(
                        Path(str(result.plan.metadata["canonical_work_root"]))
                    )
                )
            expected_relative = Path("Associated") / "向山进发 (2013) [Fonts].7z"
            problems = []
            if plan_roots != [expected_relative, expected_relative]:
                problems.append("deterministic target changed with basename, season or order")

            s1, _font, target = await self._materialize_s1(root)
            series_key = str(s1.plan.metadata["series_key"])
            catalog_path = root / "series_catalog" / f"{series_key}.json"
            before = json.loads(catalog_path.read_text("utf-8"))
            before_assets = before.get("associated_assets") or ()
            before_asset = before_assets[0] if len(before_assets) == 1 else {}
            before_content_id = _asset_content_id(before_asset)
            before_association_ids = {
                item.get("association_id")
                for item in _asset_associations(before_asset)
            }

            old_root = Path(str(before["canonical_root"]))
            new_root = root / "Anime" / "relocated-series"
            moved_target = new_root / target.relative_to(old_root)
            old_root.rename(new_root)
            SeriesCatalog(root / "series_catalog").relocate(
                series_key,
                new_root,
                allowed_root=root / "Anime",
                record_root=root / "records",
                task_root=root / "tasks",
            )
            after = json.loads(catalog_path.read_text("utf-8"))
            after_assets = after.get("associated_assets") or ()
            after_asset = after_assets[0] if len(after_assets) == 1 else {}
            if not before_content_id:
                problems.append("v3 content_id was not persisted")
            if before.get("schema_version") != 3:
                problems.append("SeriesCatalog did not persist schema v3")
            if before_content_id != _asset_content_id(after_asset):
                problems.append("relocation changed content identity")
            if before_association_ids != {
                item.get("association_id")
                for item in _asset_associations(after_asset)
            }:
                problems.append("relocation changed association identity")
            if _asset_target_path(after_asset) != str(moved_target):
                problems.append("relocation did not update only the canonical location")
            if not moved_target.is_file() or target.exists():
                problems.append("relocation did not move the verified physical target")

            different_root = root / "different-content"
            first, first_font, first_target = await self._materialize_s1(
                different_root,
                font_bytes=b"font-content-a",
            )
            second_source = r1_fixture._make_season(
                different_root / "sources",
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Second Season",
                ova=True,
            )
            second_font = second_source / "[VCB-Studio] Yama no Susume Second Season [Fonts].7z"
            second_font.write_bytes(b"font-content-b")
            second = await self._run_flow(
                root=different_root,
                source=second_source,
                task_id="tv-font-different-content-b",
                subject_id="86670",
            )
            second_entry = _font_entry(second.plan, second_font)
            second_task, _second_ledger, _service = await self._approve_and_execute(
                root=different_root,
                result=second,
            )
            first_digest = _sha256(first_font)
            second_digest = _sha256(second_font)
            first_expected = (
                Path(str(first.plan.metadata["canonical_work_root"]))
                / "Associated"
                / "向山进发 (2013) [Fonts].7z"
            )
            second_expected = (
                Path(str(second.plan.metadata["canonical_work_root"]))
                / "Associated"
                / f"向山进发 (2013) [{second_digest}] [Fonts].7z"
            )
            if (
                first_target == second_entry.target_path
                or second_entry.collision_resolution is not None
            ):
                problems.append("different font contents did not remain independent")
            if first_target != first_expected or second_entry.target_path != second_expected:
                problems.append("different contents did not use deterministic content locations")
            if second_task.get("execution_status") != "completed":
                problems.append("different-hash S2 did not complete through the real queue")
            different_series_key = str(first.plan.metadata["series_key"])
            different_catalog = json.loads(
                (
                    different_root
                    / "series_catalog"
                    / f"{different_series_key}.json"
                ).read_text("utf-8")
            )
            different_assets = different_catalog.get("associated_assets") or ()
            associations_by_hash = {
                _asset_sha256(asset): {
                    (
                        item.get("task_id"),
                        item.get("season_number"),
                        item.get("source_path"),
                    )
                    for item in _asset_associations(asset)
                }
                for asset in different_assets
                if isinstance(asset, Mapping)
            }
            if (
                different_catalog.get("schema_version") != 3
                or len(
                    {
                        _asset_content_id(asset)
                        for asset in different_assets
                        if isinstance(asset, Mapping) and _asset_content_id(asset)
                    }
                )
                != 2
                or {
                    _asset_sha256(asset)
                    for asset in different_assets
                    if isinstance(asset, Mapping)
                }
                != {first_digest, second_digest}
                or {
                    _asset_target_path(asset)
                    for asset in different_assets
                    if isinstance(asset, Mapping)
                }
                != {str(first_expected), str(second_expected)}
                or not first_expected.is_file()
                or not second_expected.is_file()
                or associations_by_hash != {
                    first_digest: {(first.plan.task_id, 1, str(first_font))},
                    second_digest: {(second.plan.task_id, 2, str(second_font))},
                }
            ):
                problems.append("same series did not persist two v3 content identities and locations")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_catalog_revision_change_stales_s2_before_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _s1, _font, _target = await self._materialize_s1(root)
            source = r1_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Second Season",
                ova=True,
            )
            s2_font = source / "[VCB-Studio] Yama no Susume [Fonts].7z"
            s2_font.write_bytes(b"one-unified-series-font-package")
            s2 = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-s2-stale-after-backfill",
                subject_id="86670",
            )
            plan, fingerprint = self._approve_only(root=root, result=s2)
            existing_before = {
                item.target_path
                for item in plan.entries
                if item.target_path.exists()
            }
            series_key = str(plan.metadata["series_key"])
            revision = int(plan.metadata["series_catalog_revision"])
            SeriesCatalog(root / "series_catalog").bind_season(
                series_key,
                expected_revision=revision,
                season_number=4,
                provider_identity=plan.work_identity.primary_provider.to_dict(),
                decision_source="legacy_backfill_revision_sentinel",
                binding_id="season-binding-revision-sentinel",
            )
            calls = []
            with self.assertRaises(PlanExecutionError):
                await self._enqueue_with_transfer_sentinel(
                    root=root,
                    task_id=plan.task_id,
                    fingerprint=fingerprint,
                    calls=calls,
                )
            task = json.loads(
                (root / "tasks" / f"{plan.task_id}.json").read_text("utf-8")
            )
            self.assertEqual([], calls)
            self.assertNotEqual("completed", task.get("execution_status"))
            self.assertEqual(
                existing_before,
                {
                    item.target_path
                    for item in plan.entries
                    if item.target_path.exists()
                },
            )
            self.assertFalse((root / "records" / f"{plan.task_id}.json").exists())
            self.assertFalse((root / "records" / f"{plan.task_id}.ledger.json").exists())

    async def test_font_hash_drift_is_rejected_before_transfer_factory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            problems = []
            source = r1_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume",
            )
            font = source / "[VCB-Studio] Yama no Susume [Fonts].7z"
            font.write_bytes(b"approved-font-package")
            result = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-source-drift-before-write",
                subject_id="41658",
            )
            plan, fingerprint = self._approve_only(root=root, result=result)
            manifest = plan.metadata.get("associated_asset_manifest")
            self.assertIsInstance(manifest, Mapping)
            font.write_bytes(b"changed-after-approval")
            calls = []
            task = await self._enqueue_with_transfer_sentinel(
                root=root,
                task_id=plan.task_id,
                fingerprint=fingerprint,
                calls=calls,
            )
            if calls:
                problems.append("approved source hash drift reached the transfer boundary")
            if task.get("execution_status") != "failed":
                problems.append("approved source hash drift did not fail the task")
            if any(item.target_path.exists() for item in plan.entries):
                problems.append("approved source hash drift created a target")
            if (root / "records" / f"{plan.task_id}.ledger.json").exists():
                problems.append("approved source hash drift created an execution ledger")

            reuse_root = root / "reuse-target-drift"
            _s1, _s1_font, target = await self._materialize_s1(reuse_root)
            source2 = r1_fixture._make_season(
                reuse_root / "sources",
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Second Season",
                ova=True,
            )
            s2_font = source2 / "[VCB-Studio] Yama no Susume [Fonts].7z"
            s2_font.write_bytes(target.read_bytes())
            s2 = await self._run_flow(
                root=reuse_root,
                source=source2,
                task_id="tv-font-target-drift-before-write",
                subject_id="86670",
            )
            s2_plan, s2_fingerprint = self._approve_only(root=reuse_root, result=s2)
            s2_source_before = (
                s2_font.stat().st_dev,
                s2_font.stat().st_ino,
                s2_font.stat().st_nlink,
                _sha256(s2_font),
            )
            catalog_path = next((reuse_root / "series_catalog").glob("series-*.json"))
            catalog_before = catalog_path.read_bytes()
            target.unlink()
            target.write_bytes(b"externally-replaced-after-approval")
            replaced_target = (
                target.stat().st_dev,
                target.stat().st_ino,
                target.stat().st_nlink,
                _sha256(target),
            )
            target_calls = []
            target_task = await self._enqueue_with_transfer_sentinel(
                root=reuse_root,
                task_id=s2_plan.task_id,
                fingerprint=s2_fingerprint,
                calls=target_calls,
            )
            if target_calls:
                problems.append("approved canonical target hash drift reached transfer")
            if target_task.get("execution_status") != "failed":
                problems.append("approved canonical target hash drift did not fail the task")
            if replaced_target != (
                target.stat().st_dev,
                target.stat().st_ino,
                target.stat().st_nlink,
                _sha256(target),
            ):
                problems.append("target drift rejection changed the external replacement")
            if s2_source_before != (
                s2_font.stat().st_dev,
                s2_font.stat().st_ino,
                s2_font.stat().st_nlink,
                _sha256(s2_font),
            ):
                problems.append("target drift rejection changed the S2 source")
            if catalog_path.read_bytes() != catalog_before:
                problems.append("target drift rejection changed SeriesCatalog")
            if (reuse_root / "records" / f"{s2_plan.task_id}.ledger.json").exists():
                problems.append("target drift rejection created an execution ledger")
            if any(
                item.target_path.exists()
                for item in s2_plan.entries
                if item.target_path != target
            ):
                problems.append("target drift rejection created an S2 target")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_recovery_preserves_one_v3_content_location_and_media_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s1, _s1_font, target = await self._materialize_s1(root)
            target_before = _file_identity(target)
            associated_root = target.parent
            associated_before = tuple(sorted(associated_root.rglob("*.7z")))
            source = r1_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Second Season",
                ova=True,
            )
            s2_font = source / "[VCB-Studio] Yama no Susume Second Season [Fonts].7z"
            s2_font.write_bytes(target.read_bytes())
            source_before = _file_identity(s2_font)
            s2 = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-s2-v3-recovery",
                subject_id="86670",
            )
            s2_entry = _font_entry(s2.plan, s2_font)
            registration_calls = []

            def fail_registration(_catalog, plan, *args, **kwargs):
                registration_calls.append((plan, args, kwargs))
                raise SeriesCatalogError("Phase A recovery injection")

            with (
                patch.object(
                    SeriesCatalog,
                    "register_completed",
                    new=fail_registration,
                ),
                patch("src.task_queue.logger.exception"),
            ):
                task, ledger, _service = await self._approve_and_execute(
                    root=root,
                    result=s2,
                )
            self.assertEqual("catalog_update_pending", task.get("execution_status"))
            self.assertEqual("completed", ledger.get("status"))
            problems = []
            if s2_entry.target_path != target:
                problems.append("different-basename S2 was not retargeted to the S1 location")
            if (
                s2_entry.collision_resolution is None
                or s2_entry.collision_resolution.action != "reuse_existing"
            ):
                problems.append("different-basename S2 did not freeze reuse_existing")
            font_ledger = next(
                (
                    item
                    for item in ledger.get("entries", ())
                    if item.get("source") == str(s2_font)
                ),
                {},
            )
            if (
                font_ledger.get("status") != "reused_identical_asset"
                or font_ledger.get("transfer_type") != "reuse_existing"
            ):
                problems.append("S2 execution did not record one safe font reuse")
            if len(registration_calls) != 1:
                problems.append("production registration boundary was not reached exactly once")
            else:
                called_plan, called_args, called_kwargs = registration_calls[0]
                evidence_candidates = [
                    called_kwargs.get("completed_ledger"),
                    *called_args,
                    *called_kwargs.values(),
                ]
                completed_evidence = next(
                    (
                        item for item in evidence_candidates
                        if isinstance(item, Mapping)
                        and item.get("status") == "completed"
                        and item.get("task_id") == s2.plan.task_id
                    ),
                    None,
                )
                if not (
                    called_plan.task_id == s2.plan.task_id
                    and isinstance(completed_evidence, Mapping)
                    and completed_evidence.get("status") == "completed"
                    and completed_evidence.get("task_id") == s2.plan.task_id
                    and completed_evidence.get("plan_fingerprint")
                    == PlanStore.fingerprint(s2.plan)
                    and completed_evidence.get("entries") == ledger.get("entries")
                ):
                    problems.append("production registration did not receive the matching completed ledger")

            if _file_identity(target) != target_before:
                problems.append("S2 execution changed the S1 canonical font")
            if _file_identity(s2_font) != source_before:
                problems.append("S2 execution changed or linked the independent source font")
            if tuple(sorted(associated_root.rglob("*.7z"))) != associated_before:
                problems.append("S2 execution created a second physical font location")

            pending_catalog_snapshot = root / "pending_series_catalog"
            shutil.copytree(root / "series_catalog", pending_catalog_snapshot)
            recovery = RecoveryService(
                catalog_root=root / "work_catalog",
                record_root=root / "records",
                series_catalog_root=root / "series_catalog",
            )
            recovery.rebuild_catalog(
                s2.state,
                s2.plan,
                WorkCatalog(root / "work_catalog"),
                SeriesCatalog(root / "series_catalog"),
            )
            if (
                _file_identity(target) != target_before
                or _file_identity(s2_font) != source_before
            ):
                problems.append("catalog recovery changed a font source or target")
            if tuple(sorted(associated_root.rglob("*.7z"))) != associated_before:
                problems.append("catalog recovery left more than one physical font location")

            series_key = str(s2.plan.metadata["series_key"])
            payload = json.loads(
                (root / "series_catalog" / f"{series_key}.json").read_text("utf-8")
            )
            assets = payload.get("associated_assets") or ()
            physical = assets[0] if len(assets) == 1 else {}
            associations = _asset_associations(physical)
            if payload.get("schema_version") != 3:
                problems.append("recovery did not persist SeriesCatalog v3")
            if not _asset_content_id(physical):
                problems.append("recovery did not persist content identity")
            if _asset_target_path(physical) != str(target):
                problems.append("recovery changed or duplicated the canonical location")
            if {item.get("task_id") for item in associations} != {
                s1.plan.task_id,
                s2.plan.task_id,
            }:
                problems.append("recovery did not retain S1 and add S2 associations")
            recovered_bytes = (
                root / "series_catalog" / f"{series_key}.json"
            ).read_bytes()
            try:
                recovery.rebuild_catalog(
                    s2.state,
                    s2.plan,
                    WorkCatalog(root / "work_catalog"),
                    SeriesCatalog(root / "series_catalog"),
                )
            except (ValueError, SeriesCatalogError) as exc:
                problems.append(f"exact recovery replay was rejected: {exc}")
            if (
                root / "series_catalog" / f"{series_key}.json"
            ).read_bytes() != recovered_bytes:
                problems.append("exact recovery replay changed Catalog bytes")

            foreign_records = root / "foreign_records"
            shutil.copytree(root / "records", foreign_records)
            foreign_ledger_path = foreign_records / f"{s2.plan.task_id}.ledger.json"
            foreign_ledger = json.loads(foreign_ledger_path.read_text("utf-8"))
            foreign_ledger["plan_fingerprint"] = "f" * 64
            foreign_ledger_path.write_text(
                json.dumps(foreign_ledger, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            foreign_catalog = root / "foreign_series_catalog"
            shutil.copytree(pending_catalog_snapshot, foreign_catalog)
            foreign_path = foreign_catalog / f"{series_key}.json"
            foreign_before = foreign_path.read_bytes()
            foreign_recovery = RecoveryService(
                catalog_root=root / "work_catalog",
                record_root=foreign_records,
                series_catalog_root=foreign_catalog,
            )
            try:
                foreign_recovery.rebuild_catalog(
                    s2.state,
                    s2.plan,
                    WorkCatalog(root / "work_catalog"),
                    SeriesCatalog(foreign_catalog),
                )
            except (ValueError, SeriesCatalogError):
                pass
            else:
                problems.append("foreign completed ledger was accepted for recovery")
            if foreign_path.read_bytes() != foreign_before:
                problems.append("foreign recovery changed Catalog bytes")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_v2_multiple_locations_for_one_content_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s1, _s1_font, target = await self._materialize_s1(root)
            series_key = str(s1.plan.metadata["series_key"])
            catalog_path = root / "series_catalog" / f"{series_key}.json"
            payload = json.loads(catalog_path.read_text("utf-8"))
            current_assets = payload.get("associated_assets") or ()
            self.assertEqual(1, len(current_assets))
            current_asset = current_assets[0]
            size = int(_asset_size(current_asset))
            digest = str(_asset_sha256(current_asset))
            original_asset = {
                "asset_id": _v2_asset_id(target, size=size, digest=digest),
                "role": "fonts",
                "scope": "series",
                "target_path": str(target),
                "size": size,
                "sha256": digest,
                "associations": [
                    dict(item) for item in _asset_associations(current_asset)
                ],
            }
            payload["schema_version"] = 2
            payload["associated_assets"] = [original_asset]
            catalog_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            damaged_payload = json.loads(json.dumps(payload))
            damaged_payload["associated_assets"][0]["asset_id"] = "0" * 64
            catalog_path.write_text(
                json.dumps(damaged_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            damaged_before = catalog_path.read_bytes()
            corrupt_source = r1_fixture._make_season(
                root / "corrupt-sources",
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Second Season",
                ova=True,
            )
            corrupt_font = corrupt_source / "[VCB-Studio] Yama no Susume Second Season [Fonts].7z"
            corrupt_font.write_bytes(target.read_bytes())
            corrupt_result = await self._run_flow(
                root=root,
                source=corrupt_source,
                task_id="tv-font-v2-corrupt-unique",
                subject_id="86670",
            )
            self.assertEqual("invalid", corrupt_result.task.get("workflow_status"))
            self.assertEqual(damaged_before, catalog_path.read_bytes())
            catalog_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            duplicate_target = target.parent / "duplicate-same-content.7z"
            shutil.copy2(target, duplicate_target)
            duplicate_association = dict(
                _asset_associations(current_asset)[0]
            )
            duplicate_association["association_id"] = hashlib.sha256(
                b"duplicate-v2-association"
            ).hexdigest()
            duplicate_asset = {
                **original_asset,
                "asset_id": _v2_asset_id(
                    duplicate_target,
                    size=size,
                    digest=digest,
                ),
                "target_path": str(duplicate_target),
                "associations": [duplicate_association],
            }

            # Each row is a valid v2 record by itself; ambiguity comes only from
            # combining two trusted locations for the same content.
            original_only = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            catalog = SeriesCatalog(root / "series_catalog")
            self.assertEqual(
                target,
                catalog.resolve_associated_location(
                    series_key,
                    scope="series",
                    role="fonts",
                    size=size,
                    digest=digest,
                ),
            )
            duplicate_only_payload = {
                **payload,
                "associated_assets": [duplicate_asset],
            }
            catalog_path.write_text(
                json.dumps(duplicate_only_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            duplicate_only = catalog.load(series_key)
            self.assertEqual(payload.get("revision"), duplicate_only.revision)
            self.assertEqual(
                duplicate_target,
                catalog.resolve_associated_location(
                    series_key,
                    scope="series",
                    role="fonts",
                    size=size,
                    digest=digest,
                ),
            )
            catalog_path.write_bytes(original_only)
            self.assertEqual(
                target,
                catalog.resolve_associated_location(
                    series_key,
                    scope="series",
                    role="fonts",
                    size=size,
                    digest=digest,
                ),
            )

            payload["associated_assets"].append(duplicate_asset)
            catalog_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            catalog_before = catalog_path.read_bytes()
            target_before = target.read_bytes()
            duplicate_before = duplicate_target.read_bytes()
            loaded = SeriesCatalog(root / "series_catalog").load(series_key)
            self.assertEqual(payload.get("revision"), loaded.revision)
            self.assertEqual(catalog_before, catalog_path.read_bytes())
            existing_targets = tuple(sorted(target.parent.rglob("*.7z")))

            source = r1_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Second Season",
                ova=True,
            )
            s2_font = source / "[VCB-Studio] Yama no Susume Second Season [Fonts].7z"
            s2_font.write_bytes(target.read_bytes())
            s2 = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-s2-multiple-location-conflict",
                subject_id="86670",
            )
            verification = PlanVerifier().verify(s2.plan)
            problems = []
            if verification.status.value != "invalid":
                problems.append("real S2 planning did not fail closed on two v2 locations")
            if s2.task.get("workflow_status") != "invalid":
                problems.append("ambiguous v2 location task remained approvable")
            if catalog_path.read_bytes() != catalog_before:
                problems.append("ambiguous v2 planning changed Catalog bytes")
            if tuple(sorted(target.parent.rglob("*.7z"))) != existing_targets:
                problems.append("ambiguous v2 planning created or removed a physical target")
            self.assertEqual(catalog_before, catalog_path.read_bytes())
            self.assertEqual(target_before, target.read_bytes())
            self.assertEqual(duplicate_before, duplicate_target.read_bytes())
            self.assertEqual([], problems, "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
