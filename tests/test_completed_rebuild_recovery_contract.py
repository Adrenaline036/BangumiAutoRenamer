"""Real temporary movie rebuild -> registration fault -> restarted recovery."""

from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from src.rename.execution_recovery import ExecutionRecoveryError, reconcile_catalog_update
from src.rename.plan_store import PlanStore
from src.rename.provider_link_catalog import ProviderLinkCatalog
from src.rename.series_catalog import SeriesCatalog
from src.rename.work_catalog import WorkCatalog, WorkCatalogError
from tests import test_completed_rebuild_work_contract as movie
from tests import test_completed_rebuild_contract as tv


class CompletedRebuildRegistrationRecoveryContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = movie.CompletedMovieRebuildContracts("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def recover(self, task_id, **overrides):
        # These are intentionally fresh instances with no runtime context;
        # public recovery must construct its own exact dependency closure.
        kwargs = dict(task_root=self.root / "tasks", record_root=self.root / "records",
                      plan_store=PlanStore(self.root / "plans"),
                      work_catalog=WorkCatalog(self.root / "work_catalog"),
                      series_catalog=SeriesCatalog(self.root / "series_catalog"),
                      provider_link_catalog=ProviderLinkCatalog(self.root / "provider_links"),
                      decision_state_root=self.root / "decisions")
        kwargs.update(overrides)
        return reconcile_catalog_update(task_id, **kwargs)

    def unchanged_state(self):
        return ({str(p): p.read_bytes() for p in self.root.rglob("*.json")},
                movie.file_tree(self.root / "sources"),
                {str(p): movie.file_identity(p) for p in self.root.rglob("*")
                 if p.is_file() and p.suffix not in {".json", ".lock"}})

    async def pending(self, *, shared_font=False):
        initial = await self.fixture.pair() if shared_font else [await self.fixture.single()]
        catalog_bytes = self.fixture.catalog_path(initial[0].plan).read_bytes()
        self.fixture.delete_materialization([item.plan for item in initial])
        revised = [self.fixture.generate(item) for item in initial]
        if shared_font:
            first_task, _, _ = await self.fixture.harness._approve_and_execute(
                root=self.root, result=revised[0])
            self.assertEqual("completed", first_task.get("execution_status"), first_task.get("error"))
        current = revised[-1]
        fired = []

        def fail_registration(plan, ledger):
            self.assertEqual(current.plan.task_id, plan.task_id)
            self.assertEqual("completed", ledger.get("status"))
            fired.append(PlanStore.fingerprint(plan))
            raise WorkCatalogError("test-rebuild-registration-boundary-fault")

        task, ledger, _ = await self.fixture.harness._approve_and_execute(
            root=self.root, result=current, before_register=fail_registration)
        self.assertEqual([PlanStore.fingerprint(current.plan)], fired)
        self.assertEqual("catalog_update_pending", task.get("execution_status"), task.get("error"))
        self.assertIn("作品登记尚未更新", str(task.get("error")))
        self.assertEqual("completed", ledger.get("status"))
        self.assertEqual(catalog_bytes, self.fixture.catalog_path(current.plan).read_bytes())
        return current, ledger, catalog_bytes

    async def pending_series(self):
        initial, executor = await tv.CompletedRebuildContracts("runTest").complete(self.root)
        old = executor.plan_store.load(initial.plan.task_id)
        old.entries[0].target_path.rename(self.root / "retained-missing-target.mkv")
        service = tv.recovery(self.root)
        snapshot = service.inspect(old.task_id)
        self.assertEqual("missing", snapshot.state, snapshot.detail)
        fresh = service.generate(old.task_id, expected_fingerprint=snapshot.fingerprint)
        current = SimpleNamespace(plan=fresh, task=movie.read_json(
            self.root / "tasks" / f"{old.task_id}.json"))
        # The production fixture injects only the registration exception;
        # approval, Trans, queue and all persisted pending state remain real.
        task, ledger, _ = await tv.fixture.MediaFlow("runTest")._approve_and_execute(
            root=self.root, result=current, fail_series_registration=True)
        self.assertEqual(("failed", "catalog_update_pending"),
                         (task.get("workflow_status"), task.get("execution_status")))
        self.assertEqual("completed", ledger.get("status"))
        self.assertEqual(PlanStore.fingerprint(fresh), ledger.get("plan_fingerprint"))
        self.assertEqual("media_boundary_entered", task.get("execution_phase"))
        self.assertIn("作品登记尚未更新", str(task.get("error")))
        self.assertEqual(1, sum(row["status"] == "created" for row in ledger["entries"]))
        self.assertGreater(sum(row["status"] == "reused_identical_asset" for row in ledger["entries"]), 0)
        return fresh, ledger

    async def test_series_recovery_calls_typed_guard_and_disconnect_is_readonly(self):
        plan, _ = await self.pending_series()
        before = self.unchanged_state()
        caught = None
        with patch("src.rename.completed_rebuild.CompletedRebuildService.validate_plan",
                   side_effect=ValueError("test-series-rebuild-authority-disconnected")) as guard:
            try:
                self.recover(plan.task_id)
            except ExecutionRecoveryError as exc:
                caught = exc
        self.assertEqual(1, guard.call_count, "Series public recovery bypassed the typed rebuild authority")
        self.assertIsNotNone(caught)
        self.assertIn("test-series-rebuild-authority-disconnected", str(caught))
        self.assertEqual(before, self.unchanged_state())

    async def test_series_restarted_recovery_obeys_catalog_lock_and_preserves_materialization(self):
        plan, _ = await self.pending_series()
        catalogs = {p: movie.read_json(p) for p in (self.root / "series_catalog").glob("*.json")}
        media = {e.target_path: movie.file_identity(e.target_path) for e in plan.entries}
        records = movie.file_tree(self.root / "records")
        before_busy = self.unchanged_state()
        # This is real OS-lock contention, not a mocked lock or callback.
        with SeriesCatalog(self.root / "series_catalog").lock(plan.metadata["series_key"]):
            with self.assertRaisesRegex(ExecutionRecoveryError, "另一个任务更新|不支持嵌套取得另一租约"):
                self.recover(plan.task_id)
        self.assertEqual(before_busy, self.unchanged_state())
        with patch("src.rename.trans.Trans.trans_file", side_effect=AssertionError("media rerun")):
            self.recover(plan.task_id)
        for path,previous in catalogs.items():
            current=movie.read_json(path)
            for key,value in previous.items():
                if key not in {'revision','series_registration_authority','generated_outputs'}:
                    self.assertEqual(value,current[key],key)
            self.assertEqual(previous['revision']+1,current['revision'])
            self.assertTrue(current['series_registration_authority']['materialized_mapping'])
            self.assertEqual({PlanStore.fingerprint(plan)},{r['plan_fingerprint'] for r in current['generated_outputs']})
        stable={p:movie.file_identity(p) for p in catalogs}
        with self.assertRaisesRegex(ExecutionRecoveryError,"不处于作品登记待恢复状态"):
            self.recover(plan.task_id)
        self.assertEqual(stable,{p:movie.file_identity(p) for p in catalogs})
        self.assertEqual(records, movie.file_tree(self.root / "records"))
        self.assertEqual(media, {e.target_path: movie.file_identity(e.target_path) for e in plan.entries})
        task = movie.read_json(self.root / "tasks" / f"{plan.task_id}.json")
        self.assertEqual(("completed", "completed", "completed"),
                         (task["workflow_status"], task["execution_status"], task["execution_phase"]))

    async def test_series_recovery_rejects_missing_archive_and_private_untyped_entry(self):
        from src.rename.execution_recovery import _reconcile_series_catalog_update_locked

        plan, _ = await self.pending_series()
        catalog = SeriesCatalog(self.root / "series_catalog")
        before = self.unchanged_state()
        with catalog.lock(plan.metadata["series_key"]), self.assertRaisesRegex(ValueError, "可信重建"):
            _reconcile_series_catalog_update_locked(task_id=plan.task_id,
                task_root=self.root / "tasks", ledger_path=self.root / "records" / f"{plan.task_id}.ledger.json",
                plan_store=PlanStore(self.root / "plans"), series_catalog=catalog,
                provider_link_catalog=ProviderLinkCatalog(self.root / "provider_links"),
                locked_series_key=plan.metadata["series_key"])
        self.assertEqual(before, self.unchanged_state())
        origin = plan.metadata["completed_rebuild_observation"]["origin_plan_fingerprint"]
        archive = self.root / "records" / "history" / plan.task_id / f"{origin}.ledger.json"
        self.assertTrue(archive.is_file())
        archive.unlink()
        before = self.unchanged_state()
        with self.assertRaises(ExecutionRecoveryError):
            self.recover(plan.task_id)
        self.assertEqual(before, self.unchanged_state())

    async def test_work_public_recovery_also_calls_typed_plan_guard(self):
        current, _, _ = await self.pending()
        before = self.unchanged_state()
        caught = None
        with patch("src.rename.completed_rebuild.CompletedRebuildService.validate_plan",
                   side_effect=ValueError("test-work-rebuild-authority-disconnected")) as guard:
            try:
                self.recover(current.plan.task_id)
            except ExecutionRecoveryError as exc:
                caught = exc
        self.assertEqual(1, guard.call_count, "Work public recovery bypassed the typed Plan guard")
        self.assertIsNotNone(caught)
        self.assertIn("test-work-rebuild-authority-disconnected", str(caught))
        self.assertEqual(before, self.unchanged_state())

    async def test_restarted_movie_registration_recovery_is_catalog_and_media_noop(self):
        current, ledger, catalog_bytes = await self.pending()
        source_before = movie.file_tree(self.root / "sources")
        target_before = {e.target_path: movie.file_identity(e.target_path) for e in current.plan.entries}
        ledger_path = self.root / "records" / f"{current.plan.task_id}.ledger.json"
        ledger_before = ledger_path.read_bytes()
        result = self.recover(current.plan.task_id)
        self.assertEqual(result, movie.read_json(self.fixture.catalog_path(current.plan)))
        task = movie.read_json(self.root / "tasks" / f"{current.plan.task_id}.json")
        self.assertEqual("completed", task["workflow_status"])
        self.assertEqual("completed", task["execution_status"])
        self.assertEqual("completed", task["execution_phase"])
        self.assertFalse(task["execution_requires_manual_check"])
        self.assertEqual(catalog_bytes, self.fixture.catalog_path(current.plan).read_bytes())
        self.assertEqual(ledger_before, ledger_path.read_bytes())
        self.assertEqual(source_before, movie.file_tree(self.root / "sources"))
        self.assertEqual(target_before, {e.target_path: movie.file_identity(e.target_path) for e in current.plan.entries})
        self.assertEqual("complete", self.fixture.rebuild_service().inspect(current.plan.task_id).state)
        before_repeat = self.unchanged_state()
        with self.assertRaises(ExecutionRecoveryError):
            self.recover(current.plan.task_id)
        self.assertEqual(before_repeat, self.unchanged_state())


    async def test_recovery_rejects_forged_ledger_catalog_history_and_disconnected_authority(self):
        current, _, _ = await self.pending()
        task_id = current.plan.task_id
        ledger_path = self.root / "records" / f"{task_id}.ledger.json"
        catalog_path = self.fixture.catalog_path(current.plan)
        origin_fp = current.plan.metadata["completed_rebuild_observation"]["origin_plan_fingerprint"]
        history_path = self.root / "records" / "history" / task_id / f"{origin_fp}.ledger.json"
        for path, mutate in (
            (ledger_path, lambda value: value["entries"][0].update(target_inode=0)),
            (catalog_path, lambda value: value["versions"][0].update(technical_variant_key="foreign")),
            (history_path, lambda value: value["entries"][0].update(source="foreign")),
        ):
            original = path.read_bytes()
            value = json.loads(original)
            mutate(value)
            path.write_text(json.dumps(value), encoding="utf-8")
            before = self.unchanged_state()
            with self.subTest(evidence=path.name), self.assertRaises(ExecutionRecoveryError):
                self.recover(task_id)
            self.assertEqual(before, self.unchanged_state())
            path.write_bytes(original)
        before = self.unchanged_state()
        for missing in ("decision_state_root", "series_catalog", "provider_link_catalog"):
            with self.subTest(missing_root=missing), self.assertRaises(ExecutionRecoveryError):
                self.recover(task_id, **{missing: None})
            self.assertEqual(before, self.unchanged_state())
        with patch("src.rename.completed_rebuild.CompletedRebuildService.work_context", return_value=None) as disconnected:
            with self.assertRaises(ExecutionRecoveryError):
                self.recover(task_id)
            self.assertTrue(disconnected.called)
        self.assertEqual(before, self.unchanged_state())
        self.recover(task_id)


if __name__ == "__main__":
    unittest.main()
