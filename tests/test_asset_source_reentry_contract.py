"""Exact completed-source reentry through public movie selection and batch admission."""
from tests.source_collection_fixture import collected_files, collected_identity
import json
import unittest

from src.rename.library_assets import LibraryAssets
from src.rename.plan_store import PlanStore
from src.rename.task_batch_scope import BatchTaskSourceRequest
from src.task_queue import TaskJob, TaskQueueManager
from tests import test_asset_actions_contract as fixture
from tests.test_completed_rebuild_work_contract import file_tree, file_identity


class CompletedSourceReentryContracts(unittest.IsolatedAsyncioTestCase):
    def select_registered_version(self, pending, registered):
        """Simulate choosing the known current version in the public dropdown."""
        descriptor = next(e.presentation_asset.version_descriptor for e in registered.plan.entries
                          if e.presentation_asset and e.presentation_asset.version_descriptor)
        rows = pending.review.work_catalog.load(registered.plan.metadata['catalog_work_key'])['versions']
        self.assertIn(descriptor.version_ref, {row['version_ref'] for row in rows})
        groups = pending.plan.metadata['movie_version_selection']['groups']
        return pending.review.revise_movie_versions(pending.plan.task_id, PlanStore.fingerprint(pending.plan),
            groups=[dict(sources=row['sources'], label=descriptor.display_label) for row in groups],
            excluded_sources=pending.plan.metadata['movie_version_selection']['excluded_sources'],
            replace_version_ref=descriptor.version_ref)

    def setUp(self):
        self.h = fixture.AssetActionContracts("runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.root = self.h.root

    async def replaced_pair(self, *, with_font=False):
        old = await self.h.fixture.single(with_font=with_font)
        incoming = await self.h.incoming()
        review = self.h.fixture.current(incoming.task_id).review
        operation, _ = review.asset_actions().prepare_replacement(incoming)
        plan = review.confirm_asset_replacement(incoming.task_id, PlanStore.fingerprint(incoming),
            expected_operation_fingerprint=operation.fingerprint)
        task, ledger, executor = await self.h.fixture.harness._approve_and_execute(
            root=self.root, result=self.h.fixture.current(plan.task_id))
        self.assertEqual("completed", task.get("execution_status"), task.get("error"))
        self.assertEqual("completed", ledger["status"])
        return old, self.h.fixture.current(plan.task_id), executor

    async def test_list_only_current_and_does_not_require_online_source_hashes(self):
        old, current, executor = await self.replaced_pair()
        assets = LibraryAssets(executor.rebuild_service)
        current.plan.entries[0].source_path.unlink()  # Offline source; listing is not an audit.
        before = file_tree(self.root)
        rows = assets.list_assets()
        self.assertEqual([current.plan.task_id], [row.task_id for row in rows])
        self.assertEqual(before, file_tree(self.root))

    async def test_same_source_batch_reentry_is_unapproved_then_public_replace_completes(self):
        old, current, executor = await self.replaced_pair()
        source = old.plan.source_root
        source_before = file_tree(self.root / "sources")
        other_source_before = file_tree(current.plan.source_root)
        self.assertTrue(other_source_before)
        target_before = {str(e.target_path): file_identity(e.target_path) for e in current.plan.entries}
        ledger_bytes = (self.root / "records" / f"{old.plan.task_id}.ledger.json").read_bytes()
        mapping_bytes = (self.root / "records" / f"{old.plan.task_id}.json").read_bytes()
        request = BatchTaskSourceRequest("reentry-request", (str(source.resolve()),), "parent_directory",
            tuple(collected_identity(p) for p in collected_files(source)), "movie")
        manager = TaskQueueManager(task_root=self.root / "tasks", execution_service=executor,
            plan_root=self.root / "plans", decision_state_root=self.root / "decisions")
        result = await manager.enqueue_batch(batch_id="reentry-batch", task_requests=(request,),
            jobs=(TaskJob(request.task_id, source, is_anime=True, is_movie=True),))
        self.assertEqual((old.plan.task_id,), result)
        self.assertFalse((self.root / "tasks" / "reentry-request.json").exists())
        pending = self.h.fixture.current(old.plan.task_id)
        self.assertEqual("need_confirm", pending.task["workflow_status"])
        self.assertEqual(old.task["task_source_scope"], pending.task["task_source_scope"])
        self.assertTrue(pending.state.asset_operation.to_dict()["requires_choice"])
        self.assertEqual([], pending.state.asset_operation.to_dict()["replaces"])
        self.assertIsNone(pending.task.get("approved_at"))
        archive = self.root / "records" / "history" / old.plan.task_id
        self.assertEqual(ledger_bytes, (archive / f"{PlanStore.fingerprint(old.plan)}.ledger.json").read_bytes())
        self.assertEqual(mapping_bytes, (archive / f"{PlanStore.fingerprint(old.plan)}.mapping.json").read_bytes())
        self.assertEqual(target_before, {str(e.target_path): file_identity(e.target_path) for e in current.plan.entries})
        revised = self.select_registered_version(pending, current)
        operation, _ = pending.review.asset_actions().prepare_replacement(revised)
        selected = pending.review.confirm_asset_replacement(old.plan.task_id, PlanStore.fingerprint(revised),
            expected_operation_fingerprint=operation.fingerprint)
        task, ledger, _ = await self.h.fixture.harness._approve_and_execute(
            root=self.root, result=self.h.fixture.current(selected.task_id))
        self.assertEqual("completed", task.get("execution_status"), task.get("error"))
        self.assertEqual("completed", ledger["status"])
        self.assertEqual(source_before, file_tree(self.root / "sources"))
        self.assertEqual(other_source_before, file_tree(current.plan.source_root))
        self.assertEqual([old.plan.task_id], [r.task_id for r in LibraryAssets(executor.rebuild_service).list_assets()])

    async def test_absent_history_does_not_authorize_superseded_reentry(self):
        old, current, executor = await self.replaced_pair()
        path = executor.work_catalog._path(current.plan.metadata["catalog_work_key"])
        record = json.loads(path.read_text("utf-8"))
        record.pop("asset_history")  # Corruption fixture, never inferred retirement.
        path.write_text(json.dumps(record), encoding="utf-8")
        assets = LibraryAssets(executor.rebuild_service, review_service=executor.review_service)
        before = file_tree(self.root)
        self.assertEqual("blocked", assets.inspect_admission(old.plan.task_id).state)
        with self.assertRaises(ValueError):
            assets.repair(old.plan.task_id)
        self.assertEqual(before, file_tree(self.root))


    async def test_old_completion_drift_rejects_before_archiving_or_new_plan(self):
        old, _current, executor = await self.replaced_pair()
        path = self.root / "records" / f"{old.plan.task_id}.ledger.json"
        ledger = json.loads(path.read_text("utf-8"))
        ledger["plan_fingerprint"] = "0" * 64
        path.write_text(json.dumps(ledger), encoding="utf-8")
        assets = LibraryAssets(executor.rebuild_service, review_service=executor.review_service)
        before = file_tree(self.root)
        self.assertEqual("blocked", assets.inspect_admission(old.plan.task_id).state)
        with self.assertRaises(ValueError):
            assets.repair(old.plan.task_id)
        self.assertEqual(before, file_tree(self.root))

    async def test_tv_font_source_reentry_preserves_other_season(self):
        from tests import _media_flow_helpers as tv
        harness = tv.MediaFlow("runTest")
        old, _archive = await harness._materialize_s1(self.root)
        source2, _ = tv.make_season(self.root, second=True, archive_bytes=b'excluded archive')
        second = await harness._run_flow(root=self.root, source=source2,
            task_id="reentry-season-two", subject_id="86670")
        done, _, executor = await harness._approve_and_execute(root=self.root, result=second)
        second_before = {str(e.target_path): file_identity(e.target_path) for e in second.plan.entries}
        second_ledger = (self.root / "records" / f"{second.plan.task_id}.ledger.json").read_bytes()
        source, _ = tv.make_season(self.root / "replacement-tv",
            archive_bytes=b'excluded archive')
        for video in source.rglob("*.mkv"):
            with video.open("ab") as stream:
                stream.write(b"replacement season one")
        incoming = await harness._run_flow(root=self.root, source=source,
            task_id="replacement-season-one", subject_id="41658")
        review = executor.review_service
        operation, _ = review.asset_actions().prepare_replacement(incoming.plan)
        selected = review.confirm_asset_replacement(incoming.plan.task_id, PlanStore.fingerprint(incoming.plan),
            expected_operation_fingerprint=operation.fingerprint)
        done, _, executor = await harness._approve_and_execute(root=self.root,
            result=self.h.fixture.current(selected.task_id))
        sources_before = (file_tree(old.plan.source_root), file_tree(source), file_tree(source2))
        request = BatchTaskSourceRequest("tv-reentry-request", (str(old.plan.source_root.resolve()),),
            "parent_directory", tuple(collected_identity(p)
                for p in collected_files(old.plan.source_root)), "tv")
        manager = TaskQueueManager(task_root=self.root / "tasks", execution_service=executor,
            plan_root=self.root / "plans", decision_state_root=self.root / "decisions")
        ids = await manager.enqueue_batch(batch_id="tv-reentry", task_requests=(request,),
            jobs=(TaskJob(request.task_id, old.plan.source_root, is_anime=True, is_movie=False),))
        self.assertEqual((old.plan.task_id,), ids)
        pending = self.h.fixture.current(old.plan.task_id)
        self.assertEqual("need_confirm", pending.task["workflow_status"])
        self.assertEqual(old.task["task_source_scope"], pending.task["task_source_scope"])
        self.assertIsNone(pending.state.asset_operation)
        self.assertIsNone(pending.task.get("approved_at"))
        operation, _ = pending.review.asset_actions().prepare_replacement(pending.plan)
        selected = pending.review.confirm_asset_replacement(old.plan.task_id, PlanStore.fingerprint(pending.plan),
            expected_operation_fingerprint=operation.fingerprint)
        done, ledger, _ = await harness._approve_and_execute(root=self.root,
            result=self.h.fixture.current(selected.task_id))
        self.assertEqual("completed", done.get("execution_status"), done.get("error"))
        self.assertEqual("completed", ledger["status"])
        self.assertEqual(second_before, {str(e.target_path): file_identity(e.target_path) for e in second.plan.entries})
        self.assertEqual(second_ledger, (self.root / "records" / f"{second.plan.task_id}.ledger.json").read_bytes())
        self.assertEqual(sources_before, (file_tree(old.plan.source_root), file_tree(source), file_tree(source2)))
        self.assertEqual({old.plan.task_id, second.plan.task_id},
            {r.task_id for r in LibraryAssets(executor.rebuild_service).list_assets()})


if __name__ == "__main__":
    unittest.main()
