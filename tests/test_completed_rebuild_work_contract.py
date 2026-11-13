"""Completed movie materialization recovery through real temporary execution.

Only provider transport and UI rendering use the existing production fixture.
The fixture creates its own task, Decision, receipted Plan, approval, Trans
ledger and WorkCatalog. No completed record or projection receipt is injected.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from src.rename.decision_state import DecisionStateStore
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_review import PlanReviewService, ReviewStatus
from src.rename.plan_store import PlanStore
from src.rename.recovery_service import RecoveryService
from tests import _media_flow_helpers as movie_fixture
from tests import _media_flow_helpers as pair_fixture


def file_identity(path: Path) -> tuple[int, int, int, int, str]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest())


def file_tree(root: Path) -> dict[str, tuple[int, int, int, int, str]]:
    return {str(path.relative_to(root)): file_identity(path)
            for path in root.rglob("*") if path.is_file()}


def read_json(path: Path):
    return json.loads(path.read_text("utf-8"))


class CompletedMovieRebuildContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "movie-rebuild"
        self.harness = movie_fixture.MediaFlow("runTest")
        self.pair_harness = pair_fixture.MediaFlow("runTest")

    def rebuild_service(self):
        service_type = importlib.import_module(
            "src.rename.completed_rebuild"
        ).CompletedRebuildService
        return service_type(
            plan_store=PlanStore(self.root / "plans"),
            decision_store=DecisionStateStore(self.root / "decisions"),
            task_root=self.root / "tasks", record_root=self.root / "records",
            catalog_root=self.root / "work_catalog",
            series_catalog_root=self.root / "series_catalog",
            provider_link_catalog_root=self.root / "provider_links",
        )

    def execution_service(self):
        store = PlanStore(self.root / "plans")
        review = PlanReviewService(store, self.root / "reviews", self.root / "work_catalog",
                                   decision_state_root=self.root / "decisions",
                                   task_root=self.root / "tasks", record_root=self.root / "records",
                                   provider_link_catalog_root=self.root / "provider_links")
        return PlanExecutionService(
            store, review, task_root=self.root / "tasks", record_root=self.root / "records",
            catalog_root=self.root / "work_catalog", series_catalog_root=self.root / "series_catalog",
            decision_state_root=self.root / "decisions",
            recovery_transaction_root=self.root / "recovery_transactions",
            provider_link_catalog_root=self.root / "provider_links",
        )

    def current(self, task_id):
        store = PlanStore(self.root / "plans")
        return SimpleNamespace(
            plan=store.load(task_id), store=store,
            state=DecisionStateStore(self.root / "decisions").load(task_id),
            task=read_json(self.root / "tasks" / f"{task_id}.json"),
            review=PlanReviewService(store, self.root / "reviews", self.root / "work_catalog",
                                    decision_state_root=self.root / "decisions",
                                    task_root=self.root / "tasks", record_root=self.root / "records",
                                    provider_link_catalog_root=self.root / "provider_links"),
        )

    async def single(self, *, with_font=True):
        source, primary, font = pair_fixture._make_version(self.root / "sources", "1080p")
        if not with_font:
            font.rename(self.root / "retained-input-font.zip")  # 合成源移出任务范围，完整保留。
        initial = await self.harness._run_provider_flow(self.root, source, "rebuild-movie")
        completed = await self.harness._complete_movie(root=self.root, result=initial, primary=primary,
            version_groups=[{'sources': [str(primary)], 'label': '1080p'}])
        task, ledger, _service = await self.harness._approve_and_execute(
            root=self.root, result=completed)
        self.assertEqual("completed", task.get("execution_status"), task.get("error"))
        self.assertEqual("completed", ledger.get("status"))
        # Use the real retained audio companion, not a forbidden CM restoration.
        audio = [e for e in completed.plan.entries if e.source_path.suffix == '.mka']
        self.assertEqual(1, len(audio))
        self.assertTrue(audio[0].target_path.is_file())
        self.assertFalse(any('CM01' in e.source_path.name for e in completed.plan.entries))
        return self.current(completed.plan.task_id)

    async def pair(self):
        _harness, _packages, _before, completed = await self.pair_harness._completed_pair(self.root)
        result = []
        for resolution in ("1080p", "2160p"):
            task, ledger, _service = await self.harness._approve_and_execute(
                root=self.root, result=completed[resolution])
            self.assertEqual("completed", task.get("execution_status"), task.get("error"))
            self.assertEqual("completed", ledger.get("status"))
            result.append(self.current(completed[resolution].plan.task_id))
        self.assertEqual(pair_fixture._work_key(result[0].plan), pair_fixture._work_key(result[1].plan))
        return result

    def delete_materialization(self, plans):
        roots = {Path(plan.metadata["canonical_work_root"]).resolve() for plan in plans}
        for root in roots:
            self.assertTrue(root.is_relative_to(self.root.resolve()))
            self.assertNotEqual(self.root.resolve(), root)
            # 保留完整合成目录，模拟目标缺失，不删除测试素材。
            members=list(root.rglob('*'))
            for path in members:
                self.assertTrue(path.is_relative_to(root))
                self.assertFalse(path.is_symlink())
            import uuid
            root.rename(self.root / ('retained-target-' + uuid.uuid4().hex))

    def catalog_path(self, plan):
        return self.root / "work_catalog" / f"{pair_fixture._work_key(plan)}.json"

    def assert_media_complete(self, plan):
        ledger = read_json(self.root / "records" / f"{plan.task_id}.ledger.json")
        self.assertEqual("completed", ledger["status"])
        self.assertEqual(PlanStore.fingerprint(plan), ledger["plan_fingerprint"])
        for entry in plan.entries:
            self.assertTrue(entry.target_path.is_file())
            self.assertEqual(entry.source_path.read_bytes(), entry.target_path.read_bytes())
            row = next(item for item in ledger["entries"] if item["source"] == str(entry.source_path))
            if row["transfer_type"] == "hardlink":
                self.assertEqual(entry.source_path.stat().st_ino, entry.target_path.stat().st_ino)
                self.assertEqual(entry.source_path.stat().st_dev, entry.target_path.stat().st_dev)
                self.assertGreaterEqual(entry.target_path.stat().st_nlink, 2)
            else:
                self.assertEqual("reuse_existing", row["transfer_type"])
                self.assertEqual("reused_identical_asset", row["status"])
        return ledger

    def generate(self, old):
        service = self.rebuild_service()
        before_inspect = file_tree(self.root)
        snapshot = service.inspect(old.plan.task_id)
        self.assertEqual(before_inspect, file_tree(self.root), "inspect changed files")
        self.assertEqual("missing", snapshot.state, snapshot.detail)
        self.assertGreater(snapshot.missing_count, 0)
        before_sources = file_tree(self.root / "sources")
        before_targets = {str(entry.target_path): file_identity(entry.target_path)
                          for entry in old.plan.entries if entry.target_path.is_file()}
        catalog_bytes = self.catalog_path(old.plan).read_bytes()
        revised = service.generate(old.plan.task_id, expected_fingerprint=snapshot.fingerprint)
        self.assertEqual(before_sources, file_tree(self.root / "sources"))
        self.assertEqual(before_targets, {str(entry.target_path): file_identity(entry.target_path)
                                         for entry in old.plan.entries if entry.target_path.is_file()})
        self.assertEqual(catalog_bytes, self.catalog_path(old.plan).read_bytes())
        current = self.current(old.plan.task_id)
        old_fp = PlanStore.fingerprint(old.plan)
        self.assertEqual(revised, current.plan)
        self.assertNotEqual(old_fp, PlanStore.fingerprint(revised))
        self.assertEqual(old_fp, revised.revision_parent_fingerprint)
        self.assertEqual(old.plan, current.store.load_version(old.plan.task_id, old_fp))
        self.assertEqual(old.plan.work_identity, revised.work_identity)
        self.assertEqual(old.plan.expected_sources, revised.expected_sources)
        self.assertEqual(old.plan.presentation_groups, revised.presentation_groups)
        self.assertEqual([(entry.source_path, entry.target_path, entry.presentation_asset) for entry in old.plan.entries],
                         [(entry.source_path, entry.target_path, entry.presentation_asset) for entry in revised.entries])
        self.assertEqual(old.state.confirmed_work, current.state.confirmed_work)
        self.assertGreater(current.state.revision, old.state.revision)
        for field in ("batch_id", "task_source_scope", "path"):
            self.assertEqual(old.task.get(field), current.task.get(field), field)
        self.assertEqual("need_confirm", current.task["workflow_status"])
        review = current.review.load_or_create(revised)
        self.assertEqual(ReviewStatus.DRAFT, review.status)
        self.assertEqual((), review.approved_entry_ids)
        with self.assertRaises(PlanExecutionError):
            self.execution_service().execute(old.plan.task_id, old_fp)
        return current

    def assert_archived_bytes(self, payload):
        self.assertTrue(any(path.parent != self.root / "records" and path.read_bytes() == payload
                            for path in self.root.rglob("*.json")),
                        "original execution evidence bytes were lost")

    async def test_ordinary_movie_execution_registers_and_stale_repeat_never_transfers(self):
        completed = await self.single()
        ledger = self.assert_media_complete(completed.plan)
        catalog = self.execution_service().work_catalog
        self.assertTrue(catalog.materializes_completed_plan(catalog.load(pair_fixture._work_key(completed.plan)),
                                                           completed.plan, ledger))
        sources = file_tree(self.root / "sources")
        targets = {entry.target_path: file_identity(entry.target_path) for entry in completed.plan.entries}
        records = file_tree(self.root / "records")
        catalogs = {path: file_identity(path) for path in (self.root / "work_catalog").glob("*.json")}
        # Its committed slot is no longer an empty append generation. A repeat
        # cannot silently create another revision or execute that approval twice.
        with self.assertRaises(PlanExecutionError):
            self.execution_service().execute(completed.plan.task_id, PlanStore.fingerprint(completed.plan))
        self.assertEqual(PlanStore.fingerprint(completed.plan),
                         PlanStore.fingerprint(completed.store.load(completed.plan.task_id)))
        self.assertEqual(sources, file_tree(self.root / "sources"))
        self.assertEqual(records, file_tree(self.root / "records"))
        self.assertEqual(catalogs, {path: file_identity(path) for path in (self.root / "work_catalog").glob("*.json")})
        for path, value in targets.items():
            self.assertEqual(value, file_identity(path))

    async def test_old_diagnosis_reaches_deleted_movie_root_incident(self):
        completed = await self.single(with_font=False)
        self.delete_materialization([completed.plan])
        before = file_tree(self.root)
        snapshot = RecoveryService(catalog_root=self.root / "work_catalog",
                                   record_root=self.root / "records",
                                   series_catalog_root=self.root / "series_catalog").diagnose(
                                       completed.state, completed.plan)
        self.assertEqual(before, file_tree(self.root))
        self.assertEqual("materialization_missing", snapshot.state,
                         "valid original WorkCatalog must permit a new unapproved rebuild")

    async def test_work_inspection_covers_selected_version_without_loading_unrelated_peers(self):
        from src.rename.completed_rebuild_work import inspect_work_rebuild
        pair = await self.pair()
        catalog = self.execution_service().work_catalog
        sources_before = file_tree(self.root / "sources")
        catalog_before = self.catalog_path(pair[0].plan).read_bytes()
        original = {item.plan.task_id: (item.plan, read_json(
            self.root / "records" / f"{item.plan.task_id}.ledger.json")) for item in pair}

        def peer_loader(task_id, fingerprint=None):
            plan, ledger = original[task_id]
            if fingerprint is not None:
                self.assertEqual(fingerprint, PlanStore.fingerprint(plan))
            PlanExecutionService._validate_frozen_source_identities(plan)
            self.assertEqual(PlanStore.fingerprint(plan), ledger["plan_fingerprint"])
            self.assertEqual("completed", ledger["status"])
            return plan, ledger

        self.delete_materialization([item.plan for item in pair])
        before = file_tree(self.root)
        proof = inspect_work_rebuild(catalog, pair[0].plan, peer_loader=peer_loader)
        self.assertEqual(before, file_tree(self.root))
        self.assertEqual({pair[0].plan.task_id}, {row["task_id"] for row in proof["peers"]})
        self.assertEqual({os.path.normcase(os.path.abspath(entry.target_path))
                          for entry in pair[0].plan.entries}, set(proof["missing_targets"]))
        self.assertEqual(sources_before, file_tree(self.root / "sources"))
        self.assertEqual(catalog_before, self.catalog_path(pair[0].plan).read_bytes())

        # The selected version's ownership cannot disappear into another task.
        payload = read_json(self.catalog_path(pair[0].plan))
        payload["assets"][0]["associations"][0]["task_id"] = "unknown-owner"
        self.catalog_path(pair[0].plan).write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises((ValueError, KeyError)):
            inspect_work_rebuild(catalog, pair[0].plan, peer_loader=peer_loader)

    async def test_single_movie_rebuild_preserves_catalog_and_original_history_on_second_rebuild(self):
        old = await self.single()
        catalog_bytes = self.catalog_path(old.plan).read_bytes()
        ledger_path = self.root / "records" / f"{old.plan.task_id}.ledger.json"
        mapping_path = self.root / "records" / f"{old.plan.task_id}.json"
        original_ledger, original_mapping = ledger_path.read_bytes(), mapping_path.read_bytes()
        for _generation in range(2):
            self.delete_materialization([old.plan])
            current = self.generate(old)
            self.assert_archived_bytes(original_ledger)
            task, _ledger, _service = await self.harness._approve_and_execute(root=self.root, result=current)
            self.assertEqual("completed", task.get("execution_status"), task.get("error"))
            current = self.current(old.plan.task_id)
            self.assert_media_complete(current.plan)
            self.assertEqual(catalog_bytes, self.catalog_path(current.plan).read_bytes())
            self.assert_archived_bytes(original_ledger)
            self.assert_archived_bytes(original_mapping)
            self.assertEqual("complete", self.rebuild_service().inspect(current.plan.task_id).state)
            from src.rename.recovery_service import RecoveryService
            from src.rename.plan_review import PlanReviewService
            review = PlanReviewService(PlanStore(self.root/'plans'), self.root/'reviews',
                catalog_root=self.root/'work_catalog', decision_state_root=self.root/'decisions',
                task_root=self.root/'tasks', record_root=self.root/'records')
            diagnosis = RecoveryService(catalog_root=self.root/'work_catalog', record_root=self.root/'records',
                rebuild_service=review.rebuild_service).diagnose(
                review.decision_state_store.load_readonly(current.plan.task_id), current.plan,
                work_catalog=review.work_catalog)
            self.assertEqual('healthy', diagnosis.state, diagnosis.detail)
            if _generation == 1:
                origin_fp=current.plan.metadata['completed_rebuild_observation']['origin_plan_fingerprint']
                origin_mapping=self.root/'records'/'history'/current.plan.task_id/f'{origin_fp}.mapping.json'
                held=self.root/'held-origin-mapping.json'
                origin_mapping.rename(held)
                try:
                    self.assertEqual('blocked', self.rebuild_service().inspect(current.plan.task_id).state)
                finally:
                    held.rename(origin_mapping)
            old = current

    async def test_partial_movie_rebuild_reuses_intact_media_without_transfer(self):
        old = await self.single()
        removed = next(entry for entry in old.plan.entries if entry.source_path.suffix == ".mkv")
        removed.target_path.rename(self.root/'held-completed-video.mkv')
        intact = {entry.target_path: file_identity(entry.target_path)
                  for entry in old.plan.entries if entry.target_path.is_file()}
        current = self.generate(old)
        for entry in current.plan.entries:
            self.assertEqual(entry.target_path in intact, entry.collision_resolution is not None)
        task, ledger, _service = await self.harness._approve_and_execute(root=self.root, result=current)
        ledger_path=self.root/'records'/f'{current.plan.task_id}.ledger.json'
        original=ledger_path.read_bytes()
        # 当前已批准复用凭据仍必须逐项核对，不得借清理旧摘要字段放行伪造记录。
        for field,bad in (('evidence_kind','foreign'),('source_size',-1),('source_xxh3_128','f'*32),('target_inode',0)):
            forged=json.loads(original)
            next(row for row in forged['entries'] if row['status']=='reused_identical_asset')[field]=bad
            ledger_path.write_text(json.dumps(forged),encoding='utf-8')
            try:
                with self.assertRaises(PlanExecutionError):
                    _service._load_ledger(ledger_path,current.plan.task_id,PlanStore.fingerprint(current.plan),current.plan)
            finally:
                ledger_path.write_bytes(original)
        self.assertEqual("completed", task.get("execution_status"), task.get("error"))
        self.assertEqual(1, sum(row["status"] == "created" for row in ledger["entries"]))
        for path, identity in intact.items():
            self.assertEqual(identity, file_identity(path))

    async def test_rebuild_catalog_noop_rejects_missing_context_and_forged_new_ledger(self):
        from src.rename.work_catalog import WorkCatalog, WorkCatalogError

        old = await self.single()
        self.delete_materialization([old.plan])
        current = self.generate(old)
        task, ledger, service = await self.harness._approve_and_execute(root=self.root, result=current)
        self.assertEqual("completed", task.get("execution_status"), task.get("error"))
        plan = current.store.load(current.plan.task_id)
        catalog = service.work_catalog
        record = catalog.load(pair_fixture._work_key(plan))
        original_bytes = self.catalog_path(plan).read_bytes()
        sources = file_tree(self.root / "sources")
        targets = {entry.target_path: file_identity(entry.target_path) for entry in plan.entries}
        self.assertTrue(catalog.materializes_completed_plan(record, plan, ledger))

        disconnected = WorkCatalog(self.root / "work_catalog")
        self.assertFalse(disconnected.materializes_completed_plan(record, plan, ledger))
        with self.assertRaises(WorkCatalogError):
            disconnected.register_completed(plan, ledger, expected_revision=record["revision"])

        tampered = []
        for field in ("source_device", "source_inode", "target_device", "target_inode", "target_link_count"):
            missing = copy.deepcopy(ledger)
            missing["entries"][0].pop(field)
            tampered.append(missing)
        wrong_identity = copy.deepcopy(ledger)
        wrong_identity["entries"][0]["target_inode"] += 1
        tampered.append(wrong_identity)
        wrong_type = copy.deepcopy(ledger)
        wrong_type["entries"][0]["target_inode"] = str(wrong_type["entries"][0]["target_inode"])
        tampered.append(wrong_type)
        wrong_action = copy.deepcopy(ledger)
        wrong_action["entries"][0]["action"] = "reuse_existing"
        tampered.append(wrong_action)
        wrong_plan = copy.deepcopy(ledger)
        wrong_plan["plan_fingerprint"] = PlanStore.fingerprint(old.plan)
        tampered.append(wrong_plan)
        for index, candidate in enumerate(tampered):
            with self.subTest(tamper=index):
                self.assertFalse(catalog.materializes_completed_plan(record, plan, candidate))
                with self.assertRaises(WorkCatalogError):
                    catalog.register_completed(plan, candidate, expected_revision=record["revision"])
        self.assertEqual(original_bytes, self.catalog_path(plan).read_bytes())
        self.assertEqual(sources, file_tree(self.root / "sources"))
        self.assertEqual(targets, {entry.target_path: file_identity(entry.target_path) for entry in plan.entries})

    async def test_two_versions_rebuild_out_of_order_preserves_catalog(self):
        pair = await self.pair()
        catalog_bytes = self.catalog_path(pair[0].plan).read_bytes()
        self.delete_materialization([item.plan for item in pair])
        generations = [self.generate(item) for item in reversed(pair)]
        results = []
        for item in generations:
            task, ledger, _service = await self.harness._approve_and_execute(root=self.root, result=item)
            self.assertEqual("completed", task.get("execution_status"), task.get("error"))
            self.assertEqual(catalog_bytes, self.catalog_path(item.plan).read_bytes())
            self.assert_media_complete(self.current(item.plan.task_id).plan)

    async def test_two_versions_approved_together_rebuild_under_shared_work_lock(self):
        pair = await self.pair()
        catalog_bytes = self.catalog_path(pair[0].plan).read_bytes()
        self.delete_materialization([item.plan for item in pair])
        generations = [self.generate(item) for item in pair]
        approved = [(item, await self.pair_harness._approve_without_execute(self.root, item)) for item in generations]
        outcomes = await asyncio.gather(*(
            asyncio.to_thread(self.execution_service().execute, item.plan.task_id, fingerprint)
            for item, fingerprint in approved), return_exceptions=True)
        self.assertFalse([str(value) for value in outcomes if isinstance(value, BaseException)])
        self.assertEqual(["completed", "completed"], [value.ledger_status for value in outcomes])
        for item in generations:
            ledger = self.assert_media_complete(self.current(item.plan.task_id).plan)
        self.assertEqual(catalog_bytes, self.catalog_path(pair[0].plan).read_bytes())

    async def test_same_revision_catalog_tamper_invalidates_inspection_without_writes(self):
        old = await self.single()
        self.delete_materialization([old.plan])
        service = self.rebuild_service()
        snapshot = service.inspect(old.plan.task_id)
        self.assertEqual("missing", snapshot.state)
        path = self.catalog_path(old.plan)
        payload = read_json(path)
        payload["versions"][0]["technical_variant_key"] = "unapproved-other-version"
        path.write_text(json.dumps(payload), encoding="utf-8")
        before = file_tree(self.root)
        with self.assertRaises(ValueError):
            service.generate(old.plan.task_id, expected_fingerprint=snapshot.fingerprint)
        self.assertEqual(before, file_tree(self.root))

    async def test_forged_ledger_blocks_selected_card_but_not_other_version(self):
        pair = await self.pair()
        self.delete_materialization([item.plan for item in pair])
        ledger_path = self.root / "records" / f"{pair[0].plan.task_id}.ledger.json"
        payload = read_json(ledger_path)
        payload["entries"][0]["source"] = str(self.root / "sources" / "unowned.mkv")
        ledger_path.write_text(json.dumps(payload), encoding="utf-8")
        service = self.rebuild_service()
        before = file_tree(self.root)
        snapshot = service.inspect(pair[1].plan.task_id)
        self.assertEqual("missing", snapshot.state)
        broken = service.inspect(pair[0].plan.task_id)
        self.assertEqual("blocked", broken.state)
        with self.assertRaises(ValueError):
            service.generate(pair[0].plan.task_id, expected_fingerprint=broken.fingerprint)
        self.assertEqual(before, file_tree(self.root))

    async def test_existing_foreign_movie_target_blocks_rebuild_and_is_preserved(self):
        old = await self.single()
        self.delete_materialization([old.plan])
        target = old.plan.entries[0].target_path
        target.parent.mkdir(parents=True)
        target.write_bytes(b"unrelated-user-media")
        service = self.rebuild_service()
        before = file_tree(self.root)
        snapshot = service.inspect(old.plan.task_id)
        self.assertEqual("blocked", snapshot.state)
        with self.assertRaises(ValueError):
            service.generate(old.plan.task_id, expected_fingerprint=snapshot.fingerprint)
        self.assertEqual(before, file_tree(self.root))


if __name__ == "__main__":
    unittest.main()
