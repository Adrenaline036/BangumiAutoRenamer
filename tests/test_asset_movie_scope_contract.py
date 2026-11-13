"""Movie slot isolation, with real temporary producer/approval/Trans history."""
import copy
import importlib
import unittest

from tests import test_completed_rebuild_work_contract as fixture


class MovieAssetScopeContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = fixture.CompletedMovieRebuildContracts("runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    async def test_unselected_target_directory_does_not_block_real_selected_repair(self):
        first, second = await self.h.pair()
        selected_targets = {entry.target_path for entry in first.plan.entries}
        unrelated = next(entry.target_path for entry in second.plan.entries if entry.target_path not in selected_targets)
        unrelated.unlink()
        unrelated.mkdir()
        marker = unrelated / "user-owned.txt"
        marker.write_bytes(b"unselected anomaly must not be touched")
        before = fixture.file_tree(unrelated)
        missing = next(entry.target_path for entry in first.plan.entries
                       if entry.presentation_asset and entry.presentation_asset.asset_role.kind == "content_version")
        missing.unlink()
        revised = self.h.generate(first)
        task, ledger, _ = await self.h.harness._approve_and_execute(root=self.h.root, result=revised)
        self.assertEqual("completed", task.get("execution_status"), task.get("error"))
        self.assertEqual(1, sum(row["status"] == "created" for row in ledger["entries"]))
        self.assertTrue(unrelated.is_dir())
        self.assertEqual(before, fixture.file_tree(unrelated))

    async def test_selected_and_root_physical_anomalies_still_block_repair(self):
        from pathlib import Path
        first, _second = await self.h.pair()
        main = next(entry.target_path for entry in first.plan.entries
                    if entry.presentation_asset and entry.presentation_asset.asset_role.kind == "content_version")
        root = Path(first.plan.metadata["canonical_work_root"])
        for kind, target in (("selected", main), ("root", root)):
            with self.subTest(kind=kind):
                backup = target.with_name(target.name + ".fixture-before")
                target.rename(backup)
                try:
                    if kind == "root":
                        target.write_bytes(b"foreign root file")
                    else:
                        target.mkdir()
                    snapshot = self.h.rebuild_service().inspect(first.plan.task_id)
                    self.assertEqual("blocked", snapshot.state, snapshot.detail)
                finally:
                    target.unlink() if kind == "root" else target.rmdir()
                    backup.rename(target)

    async def test_unselected_catalog_target_escape_remains_structurally_blocked(self):
        import json
        first, second = await self.h.pair()
        selected_targets = {str(entry.target_path) for entry in first.plan.entries}
        unrelated = next(str(entry.target_path) for entry in second.plan.entries
                         if str(entry.target_path) not in selected_targets)
        path = self.h.catalog_path(first.plan)
        payload = json.loads(path.read_text("utf-8"))
        row = next(row for row in payload["assets"] if row["target"] == unrelated)
        row["target"] = str(self.h.root / "outside-canonical-work.mkv")
        path.write_text(json.dumps(payload), encoding="utf-8")
        snapshot = self.h.rebuild_service().inspect(first.plan.task_id)
        self.assertEqual("blocked", snapshot.state, snapshot.detail)
        self.assertTrue(all(entry.target_path.is_file() for entry in second.plan.entries))

    async def test_missing_other_version_does_not_block_selected_repair(self):
        first, second = await self.h.pair()
        self.h.delete_materialization([first.plan])
        # Another card's missing source cannot be a dependency of this version.
        second.plan.entries[0].source_path.unlink()
        before = fixture.file_tree(self.h.root / "sources")
        revised = self.h.generate(first)
        self.h.execution_service().rebuild_service.validate_plan(revised.plan)
        self.assertEqual(before, fixture.file_tree(self.h.root / "sources"))
        proof = revised.plan.metadata["completed_rebuild_observation"]["work_proof"]
        self.assertEqual({first.plan.task_id}, {row["task_id"] for row in proof["peers"]})
        task, ledger, _ = await self.h.harness._approve_and_execute(root=self.h.root, result=revised)
        self.assertEqual("completed", task.get("execution_status"), task.get("error"))
        self.assertEqual("completed", ledger["status"])
        self.assertEqual(before, fixture.file_tree(self.h.root / "sources"))


    async def test_append_scope_rejects_same_slot_and_allows_unrelated_missing_version(self):
        completed = await self.h.single(with_font=False)
        catalog = self.h.rebuild_service().work
        module = importlib.import_module("src.rename.movie_asset_scope")
        with self.assertRaisesRegex(ValueError, "版本|槽位"):
            module.prepare_append(completed.plan, catalog)
        source, primary, font = fixture.pair_fixture._make_version(self.h.root / "sources", "2160p")
        font.unlink()
        self.h.delete_materialization([completed.plan])
        initial = await self.h.harness._run_provider_flow(self.h.root, source, "new-movie-version")
        second = await self.h.harness._complete_movie(root=self.h.root, result=initial, primary=primary)
        proof = module.prepare_append(second.plan, catalog)
        self.assertIsInstance(proof, module.FrozenMovieAssetScope)
        record = catalog.load(fixture.pair_fixture._work_key(completed.plan))
        module.validate_scope(second.plan, catalog, proof, payload=record)
        damaged = copy.deepcopy(record)
        damaged["canonical_root"] += "-foreign"
        with self.assertRaises(ValueError):
            module.validate_scope(second.plan, catalog, proof, payload=damaged)
        selected = next(e for e in second.plan.entries if e.presentation_asset is not None
                        and e.presentation_asset.asset_role.kind == "content_version")
        same_slot = copy.deepcopy(record)
        same_slot["versions"].append({**record["versions"][0], "target": str(selected.target_path),
            "source": str(selected.source_path), "version_ref": selected.presentation_asset.version_descriptor.version_ref})
        with self.assertRaisesRegex(ValueError, "版本|依赖"):
            module.validate_scope(second.plan, catalog, proof, payload=same_slot)
        unrelated = copy.deepcopy(record)
        unrelated["revision"] += 1
        module.validate_scope(second.plan, catalog, proof, payload=unrelated)
        task, ledger, _ = await self.h.harness._approve_and_execute(root=self.h.root, result=second)
        self.assertEqual("completed", task.get("execution_status"), task.get("error"))
        self.assertEqual("completed", ledger["status"])
        self.assertFalse(completed.plan.entries[0].target_path.exists())

    async def test_frozen_repair_survives_real_other_version_append(self):
        old = await self.h.single(with_font=False)
        old.plan.entries[0].target_path.unlink()
        repair = self.h.generate(old)
        source, primary, font = fixture.pair_fixture._make_version(self.h.root / "sources", "2160p")
        font.unlink()
        initial = await self.h.harness._run_provider_flow(self.h.root, source, "appended-after-repair")
        second = await self.h.harness._complete_movie(root=self.h.root, result=initial, primary=primary)
        task, _, _ = await self.h.harness._approve_and_execute(root=self.h.root, result=second)
        self.assertEqual("completed", task.get("execution_status"), task.get("error"))
        source_before = fixture.file_tree(self.h.root / "sources")
        second_before = {str(e.target_path): fixture.file_identity(e.target_path) for e in second.plan.entries}
        task, ledger, _ = await self.h.harness._approve_and_execute(root=self.h.root, result=repair)
        self.assertEqual("completed", task.get("execution_status"), task.get("error"))
        self.assertEqual("completed", ledger["status"])
        self.assertEqual(source_before, fixture.file_tree(self.h.root / "sources"))
        self.assertEqual(second_before, {str(e.target_path): fixture.file_identity(e.target_path) for e in second.plan.entries})

    async def test_explicit_replacement_registers_single_generation_and_replays(self):
        from tests import test_asset_actions_contract as action_fixture
        actions = action_fixture.AssetActionContracts("runTest")
        actions.fixture, actions.root = self.h, self.h.root
        old = await self.h.single(with_font=False)
        incoming = await actions.incoming()
        review = self.h.current(incoming.task_id).review
        operation, _draft = review.asset_actions().prepare_replacement(incoming)
        plan = review.confirm_asset_replacement(incoming.task_id, fixture.PlanStore.fingerprint(incoming),
            expected_operation_fingerprint=operation.fingerprint)
        current = self.h.current(plan.task_id)
        task, ledger, executor = await self.h.harness._approve_and_execute(root=self.h.root, result=current)
        self.assertEqual("completed", task.get("execution_status"), task.get("error"))
        self.assertEqual("completed", ledger["status"])
        catalog = executor.work_catalog
        key = fixture.pair_fixture._work_key(plan)
        record = catalog.load(key)
        self.assertEqual(1, len(record["versions"]))
        self.assertEqual(plan.task_id, record["versions"][0]["task_id"])
        self.assertTrue(record["asset_history"])
        before = self.h.catalog_path(plan).read_bytes()
        catalog.register_completed(plan, ledger, expected_revision=plan.metadata["catalog_revision"])
        self.assertEqual(before, self.h.catalog_path(plan).read_bytes())
        self.assertTrue(all(entry.source_path.is_file() for entry in old.plan.entries))

    async def test_replacement_with_omitted_subtitle_retires_companion_and_keeps_history(self):
        from tests import test_asset_actions_contract as action_fixture
        actions = action_fixture.AssetActionContracts("runTest")
        actions.fixture, actions.root = self.h, self.h.root
        old = await self.h.single(with_font=False)
        subtitle = next(e for e in old.plan.entries if e.media_kind == "subtitle")
        before_subtitle = fixture.file_identity(subtitle.target_path)
        source, primary, font = fixture.pair_fixture._make_version(self.h.root / "new-sources", "1080p")
        font.unlink()
        next(source.glob("*.ass")).unlink()  # Input selection before admission.
        primary.write_bytes(b"new release, old subtitle intentionally retained")
        initial = await self.h.harness._run_provider_flow(self.h.root, source, "replace-with-retained")
        incoming = await self.h.harness._complete_movie(root=self.h.root, result=initial, primary=primary,
            version_groups=[{'sources': [str(primary)], 'label': 'replacement-candidate'}])
        self.assertEqual('initial_movie_completion', incoming.plan.revision_action, (incoming.notices, incoming.form_messages))
        review = self.h.current(incoming.plan.task_id).review
        record = self.h.execution_service().work_catalog.load(fixture.pair_fixture._work_key(old.plan))
        selected = review.revise_movie_versions(incoming.plan.task_id, fixture.PlanStore.fingerprint(incoming.plan),
            groups=[{'sources': [str(primary)], 'label': record['versions'][0]['display_label']}],
            excluded_sources=[], replace_version_ref=record['versions'][0]['version_ref'])
        operation, _draft = review.asset_actions().prepare_replacement(selected)
        plan = review.confirm_asset_replacement(selected.task_id, fixture.PlanStore.fingerprint(selected),
            expected_operation_fingerprint=operation.fingerprint)
        task, ledger, executor = await self.h.harness._approve_and_execute(
            root=self.h.root, result=self.h.current(plan.task_id))
        self.assertEqual("completed", task.get("execution_status"), task.get("error"))
        record = executor.work_catalog.load(fixture.pair_fixture._work_key(plan))
        self.assertEqual([plan.task_id], [row["task_id"] for row in record["versions"]])
        self.assertFalse(subtitle.target_path.exists())
        self.assertFalse(any(row["target"] == str(subtitle.target_path) for row in record["assets"]))
        self.assertTrue(record["asset_history"])
        before_catalog = self.h.catalog_path(plan).read_bytes()
        executor.work_catalog.register_completed(plan, ledger, expected_revision=plan.metadata["catalog_revision"])
        self.assertEqual(before_catalog, self.h.catalog_path(plan).read_bytes())


if __name__ == "__main__":
    unittest.main()
