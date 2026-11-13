"""Approved temporary replacement crashes, not manufactured pending ledgers."""

from tests._approval_stage_helpers import approve_dialog
import importlib
import asyncio
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.rename.asset_replacement import approved_batch, ReplacementEvidence
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.trans import Trans
from tests import test_asset_actions_contract as action_fixture


class AssetExecutionRecoveryContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = action_fixture.AssetActionContracts("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    async def approved(self, *, omit_companion=False):
        old = await self.fixture.fixture.single(with_font=False)
        self.previous_companion = next(e for e in old.plan.entries if e.source_path.suffix == '.mka')
        service = self.fixture.fixture.execution_service()
        incoming = await self.fixture.incoming(omit_companion=omit_companion)
        operation, _draft = service.review_service.asset_actions().prepare_replacement(incoming)
        plan = service.review_service.confirm_asset_replacement(incoming.task_id, PlanStore.fingerprint(incoming),
            expected_operation_fingerprint=operation.fingerprint)
        state = service.decision_state_store.load(plan.task_id)
        fingerprint = PlanStore.fingerprint(plan)
        from src.pages.plan_review_page import PlanReviewDialog
        dialog = SimpleNamespace(review_service=service.review_service, plan=plan,
            task_data=json.loads((self.root / "tasks" / f"{plan.task_id}.json").read_text("utf-8")),
            review_badge=SimpleNamespace(text=""), _refresh_plan_header=lambda: None,
            _render_entries=lambda: None, _refresh_execute_button=lambda: None)
        dialog._approve = PlanReviewDialog._approve.__get__(dialog)
        with patch("src.utils.utils.TASK_PATH", self.root / "tasks"), patch("src.pages.plan_review_page.notify"):
            await approve_dialog(dialog, tuple(entry_id(entry) for entry in plan.entries))
        self.assertEqual(service.review_service.load(plan.task_id).status.value, "approved")
        evidence = tuple(ReplacementEvidence.from_dict(row) for row in state.asset_operation.to_dict()["replaces"])
        transfer = Trans(plan.as_mapping(), plan.task_id, fingerprint, mode="hardlink", dry_run=False,
                         record_root=self.root / "records", operations=service._build_execution_operations(plan),
                         replacement_authority=approved_batch(plan, fingerprint))
        return plan, service, transfer

    def recovery(self, service):
        return importlib.import_module("src.rename.asset_execution_recovery").AssetExecutionRecovery(service)

    def interrupt_after_publish(self, transfer):
        selected = Path(transfer.replacement_authority.evidence[0].target)
        real_replace = os.replace

        def crash(source, target):
            real_replace(source, target)
            if Path(target) == selected:
                raise SystemExit("process died after media switch")

        with patch("src.rename.trans.os.replace", side_effect=crash):
            with self.assertRaises(SystemExit):
                transfer.trans_file()

    async def test_published_target_resumes_same_original_approval(self):
        plan, service, transfer = await self.approved()
        self.interrupt_after_publish(transfer)
        result = self.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan))
        self.assertEqual(result.state, "resume_media")
        self.assertEqual(result.capability, transfer.replacement_authority)
        self.assertIsNone(transfer.trans_file())
        result = self.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan))
        self.assertEqual(result.state, "catalog_pending")

    async def test_missing_journal_after_publish_cannot_authorize_retry(self):
        plan, service, transfer = await self.approved()
        self.interrupt_after_publish(transfer)
        (transfer.replacement_authority.transaction_directory / "journal.json").unlink()
        with self.assertRaises(ValueError):
            self.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan))

    async def test_empty_prejournal_orphan_offers_replan_without_deleting_directory(self):
        plan, service, transfer = await self.approved()
        directory = transfer.replacement_authority.transaction_directory
        directory.mkdir(parents=True)
        result = self.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan))
        self.assertEqual(result.state, "replan_required")
        self.assertIsNone(result.capability)
        self.assertTrue(directory.is_dir())
        self.assertEqual(list(directory.iterdir()), [])

    async def test_prejournal_failure_replans_only_with_typed_inspection_then_approves_new_revision(self):
        from src.rename.plan_execution import PlanExecutionError
        from src.rename.plan_review import PlanReviewError
        plan, service, transfer = await self.approved()
        directory = transfer.replacement_authority.transaction_directory
        targets = {entry.target_path: action_fixture.completed_fixture.file_identity(entry.target_path)
                   for entry in plan.entries}
        sources = {entry.source_path: action_fixture.completed_fixture.file_identity(entry.source_path)
                   for entry in plan.entries}
        with patch("src.rename.trans.Trans._write_replacement_journal",
                   side_effect=RuntimeError("process failed before first journal publication")):
            with self.assertRaises(PlanExecutionError):
                service.execute(plan.task_id, PlanStore.fingerprint(plan))
        task = json.loads((self.root / "tasks" / f"{plan.task_id}.json").read_text("utf-8"))
        self.assertEqual(task["workflow_status"], "failed")
        self.assertTrue(task["execution_requires_manual_check"])
        self.assertTrue(directory.is_dir())
        self.assertEqual(list(directory.iterdir()), [])
        preparation = self.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan))
        self.assertEqual(preparation.state, "replan_required")
        operation, _draft = service.review_service.asset_actions().prepare_replacement(plan)
        approval = service.review_service.load(plan.task_id).to_dict()
        with self.assertRaises(PlanReviewError):
            service.review_service.confirm_asset_replacement(plan.task_id, PlanStore.fingerprint(plan),
                expected_operation_fingerprint=operation.fingerprint)
        self.assertEqual(approval, service.review_service.load(plan.task_id).to_dict())
        self.assertEqual(plan, service.plan_store.load(plan.task_id))
        revised = service.review_service.confirm_asset_replacement(plan.task_id, PlanStore.fingerprint(plan),
            expected_operation_fingerprint=operation.fingerprint, recovery_preparation=preparation)
        self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(revised))
        self.assertEqual(service.review_service.load(plan.task_id).status.value, "draft")
        self.assertEqual(service.review_service.load(plan.task_id).approved_entry_ids, ())
        with self.assertRaises(PlanExecutionError):
            service.execute(revised.task_id, PlanStore.fingerprint(revised))
        self.assertEqual(targets, {path: action_fixture.completed_fixture.file_identity(path) for path in targets})
        self.assertEqual(sources, {path: action_fixture.completed_fixture.file_identity(path) for path in sources})
        self.assertTrue(directory.is_dir())
        self.assertEqual(list(directory.iterdir()), [])
        task, ledger, _service = await self.fixture.fixture.harness._approve_and_execute(root=self.root,
            result=self.fixture.fixture.current(revised.task_id))
        self.assertEqual(task["execution_status"], "completed", task.get("error"))
        self.assertEqual(ledger["plan_fingerprint"], PlanStore.fingerprint(revised))
        self.assertEqual(sources, {path: action_fixture.completed_fixture.file_identity(path) for path in sources})
        self.assertTrue(directory.is_dir())
        self.assertEqual(list(directory.iterdir()), [])

    async def test_foreign_target_after_crash_is_preserved(self):
        plan, service, transfer = await self.approved()
        self.interrupt_after_publish(transfer)
        target = Path(transfer.replacement_authority.evidence[0].target)
        target.unlink()
        target.write_bytes(b"foreign user file")
        with self.assertRaises(ValueError):
            self.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan))
        self.assertEqual(target.read_bytes(), b"foreign user file")

    async def test_changed_approval_is_not_reconstructed_from_journal(self):
        plan, service, transfer = await self.approved()
        self.interrupt_after_publish(transfer)
        approval_path = service.review_service._path_helper.path_for(plan.task_id)
        value = json.loads(approval_path.read_text("utf-8"))
        value["approved_entry_ids"] = []
        approval_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan))

    async def test_projection_guard_disconnect_prevents_recovery(self):
        plan, service, transfer = await self.approved()
        self.interrupt_after_publish(transfer)
        with patch("src.rename.plan_execution.validate_plan_projection_receipt",
                   side_effect=ValueError("receipt disconnected")):
            with self.assertRaises(ValueError):
                self.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan))

    async def queue_catalog_failure(self, *, omit_companion=False):
        from src.task_queue import TaskQueueManager
        from src.rename.work_catalog import WorkCatalogError
        plan, service, _transfer = await self.approved(omit_companion=omit_companion)
        manager = TaskQueueManager(task_root=self.root / "tasks", execution_service=service,
            decision_state_root=self.root / "decisions", recovery_abandon_audit_root=self.root / "abandon_audit",
            plan_root=self.root / "plans")
        await manager.start()
        try:
            with patch("src.rename.work_catalog.WorkCatalog.register_completed",
                       side_effect=WorkCatalogError("temporary Catalog outage")):
                await manager.enqueue_approved(plan.task_id, PlanStore.fingerprint(plan))
                await asyncio.wait_for(manager._queue.join(), timeout=10)
        finally:
            await manager.stop()
        task = json.loads((self.root / "tasks" / f"{plan.task_id}.json").read_text("utf-8"))
        self.assertEqual(task["execution_status"], "catalog_update_pending", task.get("error"))
        self.assertEqual(task["workflow_status"], "failed")
        return plan, service

    def reconcile(self, plan, service):
        from src.rename.execution_recovery import reconcile_catalog_update
        return reconcile_catalog_update(plan.task_id, task_root=service.task_root, record_root=service.record_root,
            plan_store=service.plan_store, work_catalog=service.work_catalog, series_catalog=service.series_catalog,
            provider_link_catalog=service.provider_link_catalog, decision_state_root=service.decision_state_store.root,
            review_root=service.review_service.review_root,
            recovery_transaction_root=service.recovery_transaction_store.root)

    async def test_real_queue_catalog_pending_recovers_and_completed_state_needs_no_media(self):
        plan, service = await self.queue_catalog_failure()
        self.assertEqual(self.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan)).state, "catalog_pending")
        self.reconcile(plan, service)
        self.assertEqual(self.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan)).state, "completed")
        task = json.loads((self.root / "tasks" / f"{plan.task_id}.json").read_text("utf-8"))
        self.assertEqual(task["execution_status"], "completed")

    async def test_public_catalog_recovery_does_not_bypass_disconnected_authority(self):
        plan, service = await self.queue_catalog_failure()
        with patch("src.rename.plan_execution.PlanExecutionService.validate_approved_authority",
                   side_effect=ValueError("approval guard disconnected")):
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                self.reconcile(plan, service)
        task = json.loads((self.root / "tasks" / f"{plan.task_id}.json").read_text("utf-8"))
        self.assertEqual(task["execution_status"], "catalog_update_pending")

    async def test_completed_catalog_recovery_still_checks_retained_companion(self):
        plan, service = await self.queue_catalog_failure(omit_companion=True)
        self.reconcile(plan, service)
        retained = next(row for row in plan.metadata["asset_operation"]["previous_assets"]
                        if row["target_path"] == str(self.previous_companion.target_path))
        target = Path(retained["target_path"])
        self.assertFalse(target.exists())
        target.write_bytes(b"foreign after Catalog committed before finalization")
        with self.assertRaisesRegex(ValueError, "外来|不同实体"):
            self.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan))
        self.assertEqual(target.read_bytes(), b"foreign after Catalog committed before finalization")

    async def test_normal_queue_resumes_post_publish_interruption(self):
        from src.task_queue import TaskQueueManager
        from src.rename.plan_execution import PlanExecutionError
        plan, service, transfer = await self.approved()
        selected = Path(transfer.replacement_authority.evidence[0].target)
        old_bytes = selected.read_bytes()
        real_replace = os.replace

        def crash(source, target):
            real_replace(source, target)
            if Path(target) == selected:
                raise SystemExit("media worker disconnected after publication")

        class DisconnectedTransfer(Trans):
            def trans_file(self):
                try:
                    return super().trans_file()
                except SystemExit as exc:
                    # Process death escapes Trans rollback; the caller records
                    # an unavailable worker through its real failure boundary.
                    raise RuntimeError(str(exc)) from exc

        service.transfer_factory = DisconnectedTransfer
        with patch("src.rename.trans.os.replace", side_effect=crash):
            with self.assertRaises(PlanExecutionError):
                service.execute(plan.task_id, PlanStore.fingerprint(plan))
        service.transfer_factory = Trans
        task_path = self.root / "tasks" / f"{plan.task_id}.json"
        failed = json.loads(task_path.read_text("utf-8"))
        self.assertEqual(failed["workflow_status"], "failed")
        self.assertTrue(failed["execution_requires_manual_check"])
        backups = tuple(transfer.replacement_authority.transaction_directory.glob("old-*"))
        self.assertTrue(any(path.read_bytes() == old_bytes for path in backups))
        from src.rename.plan_review import PlanReviewError
        approval = service.review_service.load(plan.task_id).to_dict()
        operation = service.decision_state_store.load(plan.task_id).asset_operation
        with self.assertRaisesRegex(PlanReviewError, "核查"):
            service.review_service.confirm_asset_replacement(plan.task_id, PlanStore.fingerprint(plan),
                expected_operation_fingerprint=operation.fingerprint)
        preparation = self.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan))
        self.assertEqual(preparation.state, "resume_media")
        with self.assertRaisesRegex(PlanReviewError, "安全重新生成"):
            service.review_service.confirm_asset_replacement(plan.task_id, PlanStore.fingerprint(plan),
                expected_operation_fingerprint=operation.fingerprint, recovery_preparation=preparation)
        self.assertEqual(approval, service.review_service.load(plan.task_id).to_dict())
        self.assertEqual(plan, service.plan_store.load(plan.task_id))
        manager = TaskQueueManager(task_root=self.root / "tasks", execution_service=service,
            decision_state_root=self.root / "decisions", recovery_abandon_audit_root=self.root / "abandon_audit",
            plan_root=self.root / "plans")
        await manager.start()
        try:
            await manager.enqueue_asset_recovery(plan.task_id, PlanStore.fingerprint(plan))
            await asyncio.wait_for(manager._queue.join(), timeout=10)
        finally:
            await manager.stop()
        completed = json.loads(task_path.read_text("utf-8"))
        self.assertEqual(completed['workflow_status'], 'completed')
        self.assertEqual(completed['execution_status'], 'completed')
        self.assertFalse(completed["execution_requires_manual_check"])
        self.assertEqual(self.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan)).state, "completed")
        self.assertEqual(selected.read_bytes(), Path(transfer.replacement_authority.evidence[0].source).read_bytes())
        journal = json.loads((transfer.replacement_authority.transaction_directory / "journal.json").read_text("utf-8"))
        self.assertEqual(journal['cleanup']['status'], 'cleanup_completed')
        self.assertEqual(completed['replacement_completed_fingerprint'], PlanStore.fingerprint(plan))


if __name__ == "__main__":
    unittest.main()
