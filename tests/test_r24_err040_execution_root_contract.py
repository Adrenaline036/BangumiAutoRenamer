"""ERR040: public inventory producer through approval and real temporary links."""
from tests._approval_stage_helpers import approve_dialog
import asyncio
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from src.pages.plan_review_page import PlanReviewDialog
from src.pages.review_operation_view import ReviewCommandApplyResult
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.projection_authority import ProjectionAuthorityError
from src.task_queue import TaskQueueManager
from src.utils.utils import read_task_at
import tests.test_provider_subject_plan_wiring_red_contract as fixture
from tests.test_r24_bangumi_primary_optional_tmdb_incident_red_contract import _fixture as bangumi_fixture
from tests.test_r24_yama_special_order_dependence_incident_red_contract import _apply_boundary


class ExecutionRootContract(unittest.IsolatedAsyncioTestCase):
    async def prepare(self, root, *, bangumi_only=False):
        flow = root / "flow"
        subjects, relations, selected_id = fixture.EIGHTY_SIX_SUBJECTS, fixture.EIGHTY_SIX_RELATIONS, "331887"
        if bangumi_only:
            selected = bangumi_fixture()["incident"]["selected_subject"]
            selected_id = str(selected["provider_id"])
            subjects = {selected_id: {**selected, "id": int(selected_id), "type": 2,
                "name_cn": selected["title"], "name": selected["original_title"],
                "date": selected["air_date"], "eps": 4,
                "images": {"large": fixture.production_fixture.POSTER_URL}}}
            relations = {selected_id: ()}
        source = fixture._make_episode_source(root / "source", "Ordinary Show" if bangumi_only else "86",
            tuple(range(1, 5 if bangumi_only else 24)))
        before = fixture._source_manifest(source)
        harness = fixture.ProviderSubjectPlanWiringRedContracts(methodName="runTest")
        with patch.dict(os.environ, {"BAR_DRY_RUN": "0"}):
            result = await harness._run_real_add_select(root=flow, source=source,
                subjects=subjects, relations=relations, selected_subject_id=selected_id,
                fail_subject=None if bangumi_only else ("第2部分", "第2クール"),
                failure_mode="no_match")
            if not bangumi_only:
                self.assertEqual(ReviewCommandApplyResult.APPLIED, _apply_boundary(result),
                    read_task_at(result.tasks, result.task_id).get("error"))
        store = PlanStore(result.plans)
        plan = store.load(result.task_id)
        if bangumi_only:
            self.assertIsNone(plan.metadata.get("provider_link_snapshot"))
        else:
            self.assertEqual("inventory_smart_completion", plan.metadata["mapping_source"])
            self.assertIsInstance(plan.metadata["provider_link_snapshot"], Mapping)
            self.assertNotEqual(Path(plan.metadata["canonical_work_root"]), plan.execution_policy.target_root)
        self.assertTrue(Path(plan.metadata["canonical_work_root"]).is_relative_to(plan.execution_policy.target_root))
        review = PlanReviewService(store, result.reviews, flow / "work_catalog",
            decision_state_root=result.decisions, task_root=result.tasks)
        self.assertEqual([], list(PlanVerifier().verify(plan).issues))
        dialog = SimpleNamespace(review_service=review, plan=plan,
            selected_ids={entry_id(e) for e in plan.entries},
            task_data=read_task_at(result.tasks, result.task_id), decision=None,
            review_badge=SimpleNamespace(text=""), _refresh_plan_header=MagicMock(),
            _render_entries=MagicMock(), _refresh_execute_button=MagicMock())
        with patch("src.utils.utils.TASK_PATH", result.tasks), patch("src.pages.plan_review_page.notify"):
            await approve_dialog(dialog, tuple(entry_id(e) for e in plan.entries))
        self.assertEqual("approved", read_task_at(result.tasks, result.task_id)["workflow_status"])
        service = PlanExecutionService(store, review, task_root=result.tasks,
            record_root=flow / "records", catalog_root=flow / "work_catalog",
            series_catalog_root=flow / "series_catalog", decision_state_root=result.decisions,
            recovery_transaction_root=flow / "recovery", provider_link_catalog_root=flow / "provider_links")
        manager = TaskQueueManager(task_root=result.tasks, execution_service=service,
            decision_state_root=result.decisions, provider_link_catalog_root=flow / "provider_links",
            recovery_abandon_audit_root=flow / "abandon", plan_root=result.plans)
        return result, plan, service, manager, source, before

    async def test_distinct_roots_survive_reload_config_change_and_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, plan, service, manager, source, before = await self.prepare(Path(tmp))
            fingerprint = PlanStore.fingerprint(plan)
            # No current config/provider read may change this frozen approved Plan.
            with patch("src.rename.target_routing.snapshot_from_config_manager",
                       side_effect=AssertionError("execution read live root configuration")):
                self.assertEqual(fingerprint, PlanStore.fingerprint(service.validate_approved(plan.task_id, fingerprint)))
                try:
                    with patch.dict(os.environ, {"BAR_DRY_RUN": "0"}):
                        await manager.enqueue_approved(plan.task_id, fingerprint)
                        await asyncio.wait_for(manager._queue.join(), timeout=15)
                finally:
                    await manager.stop()
            task = read_task_at(result.tasks, plan.task_id)
            self.assertEqual("completed", task["execution_status"], task.get("error"))
            self.assertEqual("partial_failed", task["workflow_status"])
            self.assertIn("检查", task["font_error"])
            self.assertEqual(before, fixture._source_manifest(source))
            for entry in plan.entries:
                self.assertTrue(entry.source_path.samefile(entry.target_path))
                self.assertGreaterEqual(entry.source_path.stat().st_nlink, 2)
                self.assertEqual(entry.source_path.read_bytes(), entry.target_path.read_bytes())
            ledger = json.loads((result.root / "records" / f"{plan.task_id}.ledger.json").read_text("utf-8"))
            self.assertTrue(ledger)
            self.assertTrue(tuple((result.root / "series_catalog").glob("*.json")))

    async def test_inventory_receipt_cannot_be_removed_or_disconnected(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, plan, service, manager, source, before = await self.prepare(Path(tmp))
            with self.assertRaisesRegex(PlanExecutionError, "分集投影凭据无效"):
                service._validate_provider_link_preflight(replace(plan, projection_receipt=None))
            with patch("src.rename.plan_execution.validate_plan_projection_receipt",
                       side_effect=ProjectionAuthorityError("receipt disconnected")) as reader:
                with self.assertRaisesRegex(PlanExecutionError, "分集投影凭据无效"):
                    service.validate_approved(plan.task_id, PlanStore.fingerprint(plan))
                reader.assert_called_once()
            self.assertEqual(before, fixture._source_manifest(source))
            self.assertFalse(any(e.target_path.exists() for e in plan.entries))

    async def test_real_bangumi_only_producer_reaches_approved_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, plan, service, manager, source, before = await self.prepare(
                Path(tmp), bangumi_only=True)
            self.assertEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(
                service.validate_approved(plan.task_id, PlanStore.fingerprint(plan))))
            service._validate_execution_paths(plan)
            self.assertEqual(before, fixture._source_manifest(source))
            self.assertFalse(any(e.target_path.exists() for e in plan.entries))

    async def test_missing_provider_snapshot_cannot_bypass_frozen_root_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, plan, service, manager, source, before = await self.prepare(Path(tmp))
            # Negative adapter probe: not a fabricated successful Bangumi Plan.
            for snapshot in (None, "invalid"):
                damaged = replace(plan, metadata={**plan.metadata,
                    "provider_link_snapshot": snapshot, "target_root_snapshot": None})
                with self.assertRaisesRegex(PlanExecutionError, "target_root_snapshot_missing"):
                    service._validate_provider_link_preflight(damaged)
            self.assertEqual(before, fixture._source_manifest(source))
            self.assertFalse(any(e.target_path.exists() for e in plan.entries))

    async def test_source_identity_drift_rejected_without_legacy_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, plan, service, manager, source, before = await self.prepare(Path(tmp), bangumi_only=True)
            self.assertIsNone(plan.metadata.get("source_episode_projection"))
            victim = plan.entries[0].source_path
            previous = victim.stat()
            # Replace a disposable source at equal length, not merely its mtime.
            replacement = victim.with_suffix('.replacement')
            replacement.write_bytes(b'X' * previous.st_size)
            os.replace(replacement, victim)
            self.assertNotEqual(previous.st_ino, victim.stat().st_ino)
            from src.rename.trans import Trans
            from src.rename.media_file_identity import frozen_sources
            service._validate_execution_paths(plan)
            error=Trans(plan.as_mapping(),plan.task_id,'frozen-identity',mode='hardlink',dry_run=False,record_root=Path(tmp)/'drift-records',source_identities=frozen_sources(plan)).trans_file()
            self.assertIsNotNone(error)
            self.assertFalse(any(e.target_path.exists() for e in plan.entries))

    async def test_tampered_roots_and_disconnected_predicate_fail_before_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, plan, service, manager, source, before = await self.prepare(Path(tmp))
            for changes, code in (
                ({"target_root_snapshot": None}, "target_root_snapshot_missing"),
                ({"target_root_config_fingerprint": "0" * 64}, "target_root_snapshot_invalid"),
                ({"target_root": str(Path(tmp) / "other")}, "target_root_route_mismatch"),
                ({"canonical_work_root": str(Path(tmp) / "escape")}, "target_root_route_mismatch"),
                ({"mapping_source": "legacy"}, "作品季度登记信息"),
            ):
                with self.subTest(changes=tuple(changes)):
                    damaged = replace(plan, metadata={**plan.metadata, **changes})
                    with self.assertRaisesRegex(PlanExecutionError, code):
                        service._validate_provider_link_preflight(damaged)
            with patch("src.rename.target_routing.inspect_frozen_target_routing",
                       side_effect=RuntimeError("verifier routing disconnected")) as predicate:
                with self.assertRaisesRegex(RuntimeError, "verifier routing disconnected"):
                    PlanVerifier().verify(plan)
                predicate.assert_called_once()
            task_before = (result.tasks / f"{plan.task_id}.json").read_bytes()
            try:
                for disconnect in (False, True):
                    guard = patch("src.rename.target_routing.inspect_frozen_target_routing",
                        side_effect=RuntimeError("routing disconnected")) if disconnect else nullcontext()
                    with guard as predicate:
                        with self.assertRaises((PlanExecutionError, RuntimeError)):
                            await manager.enqueue_approved(plan.task_id,
                                PlanStore.fingerprint(plan) if disconnect else "0" * 64)
                        if disconnect:
                            predicate.assert_called_once()
                    self.assertEqual(task_before, (result.tasks / f"{plan.task_id}.json").read_bytes())
                    self.assertEqual(0, manager._queue.qsize())
            finally:
                await manager.stop()
            self.assertEqual(before, fixture._source_manifest(source))
            self.assertFalse(any(e.target_path.exists() for e in plan.entries))


if __name__ == "__main__":
    unittest.main()
