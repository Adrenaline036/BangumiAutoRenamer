"""Read-only replacement selection anchored in real completed Catalog history."""

import importlib
import json
import unittest
from pathlib import Path
from dataclasses import replace

from src.rename.decision_state import DecisionStateStore
from src.rename.plan_store import PlanStore
from src.rename.series_catalog import SeriesCatalog
from src.rename.work_catalog import WorkCatalog
from tests import test_completed_rebuild_work_contract as completed_fixture
from tests.test_completed_rebuild_work_contract import file_tree, pair_fixture


class AssetActionContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = completed_fixture.CompletedMovieRebuildContracts("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def service(self):
        return importlib.import_module("src.rename.asset_actions").AssetActionService(
            plan_store=PlanStore(self.root / "plans"),
            decision_store=DecisionStateStore(self.root / "decisions"),
            task_root=self.root / "tasks", record_root=self.root / "records",
            work_catalog=WorkCatalog(self.root / "work_catalog"),
            series_catalog=SeriesCatalog(self.root / "series_catalog"))

    async def incoming(self, resolution="1080p", *, omit_companion=False, generation="new-release", valid_media=False, keep_font=False):
        source, primary, font = pair_fixture._make_version(self.root / generation, resolution)
        if not keep_font:
            font.unlink()
        marker = f"new release of the selected movie: {generation}".encode()
        primary.write_bytes((primary.read_bytes() if valid_media else b'') + marker)
        if omit_companion:
            companions = tuple(source.glob('*.mka'))
            self.assertEqual(1, len(companions))
            companions[0].unlink()  # Disposable incoming source, before admission.
        initial = await self.fixture.harness._run_provider_flow(self.root, source, generation)
        # Explicitly name the incoming unapproved version before selecting the
        # existing replacement target; same-spec labels are not identity.
        completed = await self.fixture.harness._complete_movie(root=self.root, result=initial, primary=primary,
            version_groups=[{'sources': [str(primary)], 'label': resolution + ' ' + generation}])
        plan = completed.plan
        if omit_companion:
            self.assertFalse(any(e.source_path.suffix == '.mka' for e in plan.entries))
            self.assertTrue(any(e.media_kind == 'subtitle' for e in plan.entries))
        # This fixture explicitly models the operator selecting an existing
        # version. Production must never infer replacement from resolution.
        key = plan.metadata.get('catalog_work_key') or plan.metadata.get('proposed_work_key')
        catalog = WorkCatalog(self.root / 'work_catalog')
        if key and (catalog.root / f'{key}.json').exists():
            rows = [row for row in catalog.load(key)['versions']
                    if str(row.get('display_label', '')).casefold() == resolution.casefold()]
            refs = {row['version_ref'] for row in rows}
            if len(refs) == 1:
                plan = completed.review.revise_movie_versions(plan.task_id, PlanStore.fingerprint(plan),
                    groups=[{'sources': [str(primary)], 'label': rows[0]['display_label']}],
                    excluded_sources=[], replace_version_ref=next(iter(refs)))
        return plan

    async def complete_replacement(self, incoming):
        review = self.fixture.execution_service().review_service
        operation, _draft = review.asset_actions().prepare_replacement(incoming)
        plan = review.confirm_asset_replacement(incoming.task_id, PlanStore.fingerprint(incoming),
            expected_operation_fingerprint=operation.fingerprint)
        current = self.fixture.current(plan.task_id)
        task, ledger, _service = await self.fixture.harness._approve_and_execute(root=self.root, result=current)
        self.assertEqual(task["execution_status"], "completed", task.get("error"))
        self.assertEqual(ledger["status"], "completed")
        return plan

    async def test_two_replacements_and_repair_never_resurrect_retired_companion(self):
        old = await self.fixture.single(with_font=False)
        companion = next(entry for entry in old.plan.entries if entry.source_path.suffix == '.mka')
        await self.complete_replacement(await self.incoming(omit_companion=True))
        incoming = await self.incoming(omit_companion=True, generation="third-release")
        operation, _draft = self.service().prepare_replacement(incoming)
        row = next((row for row in operation.to_dict()["previous_assets"]
                    if row["target_path"] == str(companion.target_path)), None)
        self.assertIsNone(row, 'ERR060整版本换源后旧特典不再是活跃资产')
        final = await self.complete_replacement(incoming)
        self.assertFalse(companion.target_path.exists())
        main = next(entry for entry in final.entries if entry.presentation_asset is not None
                    and entry.presentation_asset.asset_role.kind == "content_version")
        main.target_path.unlink()
        rebuilt = self.fixture.generate(self.fixture.current(final.task_id))
        task, ledger, _service = await self.fixture.harness._approve_and_execute(root=self.root, result=rebuilt)
        self.assertEqual(task["execution_status"], "completed", task.get("error"))
        self.assertEqual(sum(row["status"] == "created" for row in ledger["entries"]), 1)
        self.assertFalse(companion.target_path.exists())

    async def test_second_replacement_does_not_adopt_or_delete_foreign_retired_path(self):
        old = await self.fixture.single(with_font=False)
        companion = next(entry for entry in old.plan.entries if entry.source_path.suffix == '.mka')
        await self.complete_replacement(await self.incoming(omit_companion=True))
        incoming = await self.incoming(omit_companion=True, generation="third-release")
        self.assertFalse(companion.target_path.exists())
        companion.target_path.write_bytes(b"unregistered companion")
        operation, _ = self.service().prepare_replacement(incoming)
        self.assertNotIn(str(companion.target_path), {r['target_path'] for r in operation.to_dict()['previous_assets']})
        await self.complete_replacement(incoming)
        self.assertEqual(companion.target_path.read_bytes(), b"unregistered companion")

    async def retained_series(self):
        from tests import _media_flow_helpers as tv_fixture
        harness = tv_fixture.MediaFlow("runTest")
        old, _archive = await harness._materialize_s1(self.root)
        companion = next(entry for entry in old.plan.entries if entry.media_kind == "subtitle")
        for generation in ("second-tv-release", "third-tv-release"):
            source, archive = tv_fixture.make_season(self.root / generation)
            archive.unlink()
            next(path for path in source.rglob("*") if path.name == companion.source_path.name).unlink()
            for video in source.rglob("*.mkv"):
                with video.open("ab") as stream:
                    stream.write(generation.encode())
            incoming = await harness._run_flow(root=self.root, source=source,
                task_id=generation, subject_id="41658")
            if generation == "third-tv-release":
                return harness, old, companion, incoming
            review = self.fixture.execution_service().review_service
            operation, _draft = review.asset_actions().prepare_replacement(incoming.plan)
            plan = review.confirm_asset_replacement(incoming.plan.task_id, PlanStore.fingerprint(incoming.plan),
                expected_operation_fingerprint=operation.fingerprint)
            task, ledger, _service = await harness._approve_and_execute(root=self.root,
                result=self.fixture.current(plan.task_id))
            self.assertEqual(task["execution_status"], "completed", task.get("error"))
            self.assertEqual(ledger["status"], "completed")

    async def test_two_series_replacements_keep_old_omitted_subtitle_retired(self):
        harness, old, companion, incoming = await self.retained_series()
        self.assertFalse(companion.target_path.exists())
        operation, _draft = self.service().prepare_replacement(incoming.plan)
        rows = operation.to_dict()["previous_assets"]
        self.assertFalse(any(row['target_path'] == str(companion.target_path) for row in rows))
        review = self.fixture.execution_service().review_service
        plan = review.confirm_asset_replacement(incoming.plan.task_id, PlanStore.fingerprint(incoming.plan),
            expected_operation_fingerprint=operation.fingerprint)
        task, _ledger, _service = await harness._approve_and_execute(root=self.root,
            result=self.fixture.current(plan.task_id))
        self.assertEqual(task["execution_status"], "completed", task.get("error"))
        self.assertFalse(companion.target_path.exists())

    async def test_second_series_replacement_uses_current_directory_not_old_ledger(self):
        _harness, old, companion, incoming = await self.retained_series()
        self.assertFalse(companion.target_path.exists())
        path = self.root / "records" / 'second-tv-release.ledger.json'
        payload = json.loads(path.read_text("utf-8"))
        payload['entries'][0]['target'] = str(self.root / 'foreign-target.mkv')
        path.write_text(json.dumps(payload), encoding="utf-8")
        before = file_tree(self.root)
        operation, _ = self.service().prepare_replacement(incoming.plan)
        value = operation.to_dict()
        self.assertEqual('asset-operation-v3', value['policy'])
        self.assertEqual([], value['previous_assets'])
        self.assertNotIn(str(self.root / 'foreign-target.mkv'),
                         {row['target'] for row in value['season_directory']['files']})
        self.assertEqual(before, file_tree(self.root))
        self.assertFalse(companion.target_path.exists())

    async def test_real_completed_movie_prepare_is_readonly_and_exact(self):
        old = await self.fixture.single(with_font=False)
        incoming = await self.incoming()
        before = file_tree(self.root)
        operation, draft = self.service().prepare_replacement(incoming)
        value = operation.to_dict()
        self.assertEqual(value["mode"], "replace")
        # Equal audio bytes in a separately downloaded file no longer imply
        # reuse: the approved version's paired audio is switched with it.
        self.assertEqual({row["target"] for row in value["replaces"]},
                         {str(entry.target_path) for entry in incoming.entries
                          if entry.presentation_asset is not None
                          and (entry.presentation_asset.asset_role.kind == "content_version"
                               or entry.source_path.suffix == '.mka')})
        self.assertTrue(value["previous_assets"])
        self.assertEqual(before, file_tree(self.root))
        self.assertIsNone(draft.projection_receipt)
        self.assertEqual(draft.task_id, incoming.task_id)

    async def test_foreign_target_content_is_not_authorized(self):
        old = await self.fixture.single(with_font=False)
        incoming = await self.incoming()
        target = old.plan.entries[0].target_path
        target.unlink()
        target.write_bytes(b"unknown user bytes")
        with self.assertRaises(ValueError):
            self.service().prepare_replacement(incoming)
        self.assertEqual(target.read_bytes(), b"unknown user bytes")

    async def test_different_movie_version_remains_append(self):
        await self.fixture.single(with_font=False)
        incoming = await self.incoming("2160p")
        operation, _draft = self.service().prepare_replacement(incoming)
        self.assertEqual(operation.to_dict()["mode"], "append")
        self.assertEqual(operation.to_dict()["replaces"], [])

    async def test_missing_historical_ledger_fails_closed(self):
        old = await self.fixture.single(with_font=False)
        incoming = await self.incoming()
        (self.root / "records" / f"{old.plan.task_id}.ledger.json").unlink()
        with self.assertRaises(ValueError):
            self.service().prepare_replacement(incoming)

    async def test_only_selected_target_collision_message_is_removed(self):
        old = await self.fixture.single(with_font=False)
        incoming = await self.incoming()
        selected = next(entry for entry in incoming.entries if entry.presentation_asset is not None
                        and entry.presentation_asset.asset_role.kind == "content_version")
        source = str(selected.source_path)
        target = str(selected.target_path)
        incoming = replace(incoming, metadata={**incoming.metadata,
            "asset_collision_unresolved": (
                {"code": "target_exists", "source_path": source, "target_path": target},
                {"code": "font_evidence_unavailable", "source_path": source, "target_path": target},
                {"code": "target_exists", "source_path": "unknown", "target_path": "elsewhere"})})
        # Observation does not mint a receipt: the real source Plan was already
        # loaded; this test injects only presentation diagnostics for filtering.
        service = self.service()
        _operation, draft = service.prepare_replacement(incoming)
        issues = draft.metadata["asset_collision_unresolved"]
        self.assertEqual(len(issues), 2)
        self.assertEqual(issues[0]["code"], "font_evidence_unavailable")

    def commit(self, incoming):
        from src.rename.decision_state import DecisionCommandService
        service = self.service()
        operation, draft = service.prepare_replacement(incoming)
        current = service.decisions.load(incoming.task_id)
        return DecisionCommandService(service.decisions, service.plans).update(
            incoming.task_id, expected_revision=current.revision,
            changes={"asset_operation": operation}, planner=lambda _state: draft,
            task_terminal=self.fixture.execution_service().review_service._revision_task_terminal(incoming.task_id))

    async def test_materialized_tv_actual_commit_rejects_wrong_anchor_and_disconnection(self):
        from src.rename import initial_tv_completion, public_revision
        from src.rename.tv_completion_observations import FrozenTVResolution
        from tests.test_content_choice_contract import records
        from tests import _media_flow_helpers as tv_fixture
        from unittest.mock import patch
        for fault in ('anchor', 'disconnect'):
            with self.subTest(fault=fault):
                root = self.root / ('l0-' + fault)
                harness = tv_fixture.MediaFlow('runTest')
                await harness._materialize_s1(root)
                source, _ = tv_fixture.make_season(root / 'incoming')
                reached, preserved = [], []
                original = public_revision.commit
                def intercept(service, intent, *, task_terminal, persist=True):
                    if intent.task_id != 'l0-probe' or type(intent) is not initial_tv_completion.InitialTVCompletionIntent:
                        return original(service, intent, task_terminal=task_terminal, persist=persist)
                    reached.append(True)
                    resolution = intent.inputs.resolution.resolution()
                    self.assertEqual('L0', resolution.automation_level)
                    self.assertIsNone(resolution.subject_observations, 'must not fake a fresh search batch')
                    before = records(root)
                    if fault == 'anchor':
                        changed = FrozenTVResolution.capture(replace(resolution, anchor_mapping_id='not-the-frozen-anchor'))
                        forged = replace(intent, inputs=replace(intent.inputs, resolution=changed))
                        with self.assertRaises(ValueError):
                            original(service, forged, task_terminal=task_terminal, persist=persist)
                    else:
                        with patch.object(initial_tv_completion, 'build_initial_tv_completion', return_value=None), self.assertRaises(ValueError):
                            original(service, intent, task_terminal=task_terminal, persist=persist)
                    preserved.append(before == records(root))
                    raise ValueError('isolated L0 negative completed')
                with patch.object(public_revision, 'commit', intercept):
                    result = await harness._run_flow(root=root, source=source, task_id='l0-probe', subject_id='41658')
                self.assertEqual([True], reached, 'actual L0 canonical not reached')
                self.assertEqual([True], preserved)
                self.assertFalse(result.state.confirmed_work)

    async def test_offline_old_source_and_damaged_registered_inode_allow_explicit_observation(self):
        old = await self.fixture.single(with_font=False)
        incoming = await self.incoming()
        old_video = next(entry for entry in old.plan.entries if entry.presentation_asset is not None
                         and entry.presentation_asset.asset_role.kind == "content_version")
        old_video.target_path.write_bytes(b"damaged bytes intentionally selected for replacement")
        for entry in old.plan.entries:
            entry.source_path.unlink()
        operation, _draft = self.service().prepare_replacement(incoming)
        selected = next(row for row in operation.to_dict()["replaces"] if row["target"] == str(old_video.target_path))
        self.assertEqual(selected["old_target"]["size"], old_video.target_path.stat().st_size)

    async def test_typed_committed_replacement_revalidates_and_rejects_later_target_drift(self):
        await self.fixture.single(with_font=False)
        state, plan = self.commit(await self.incoming())
        self.assertEqual(self.service().validate(plan, state), state.asset_operation)
        selected = state.asset_operation.to_dict()["replaces"][0]
        from pathlib import Path
        Path(selected["target"]).write_bytes(b"changed after approved observation")
        with self.assertRaises(ValueError):
            self.service().validate(plan, state)

    async def test_frozen_ledger_tampering_rejects_even_if_plan_and_targets_still_match(self):
        old = await self.fixture.single(with_font=False)
        state, plan = self.commit(await self.incoming())
        path = self.root / "records" / f"{old.plan.task_id}.ledger.json"
        payload = json.loads(path.read_text("utf-8"))
        payload["unapproved_extra"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "闭包|代际"):
            self.service().validate(plan, state)

    async def test_missing_new_companion_is_previewed_for_retirement_without_writes(self):
        old = await self.fixture.single(with_font=False)
        companion = next(e for e in old.plan.entries if e.source_path.suffix == '.mka')
        incoming = await self.incoming(omit_companion=True)
        operation, draft = self.service().prepare_replacement(incoming)
        retired = [row for row in operation.to_dict()["previous_assets"] if row["disposition"] == "retire"]
        self.assertTrue(retired, 'ERR060旧版本专属且新版本未带附件必须预览退场')
        self.assertFalse(draft.metadata['asset_replacement_retained'])
        from pathlib import Path
        missing_companion = next(row for row in retired if row['target_path'] == str(companion.target_path))
        self.assertTrue(Path(missing_companion["target_path"]).is_file())

    async def test_new_slot_cannot_claim_raw_replacement_metadata(self):
        await self.fixture.single(with_font=False)
        incoming = await self.incoming()
        operation, _draft = self.service().prepare_replacement(incoming)
        tampered = replace(incoming, metadata={**incoming.metadata, "asset_operation": operation.to_dict()})
        state = self.service().decisions.load(incoming.task_id)
        with self.assertRaises(ValueError):
            self.service().validate(tampered, state)

    async def test_completed_guard_checks_new_targets_not_old_source_availability(self):
        from src.rename.asset_replacement import approved_batch, ReplacementEvidence
        from src.rename.trans import Trans
        await self.fixture.single(with_font=False)
        state, plan = self.commit(await self.incoming())
        service = self.service()
        with self.assertRaises(ValueError):
            service.validate(plan, state, completed=True)
        evidence = tuple(ReplacementEvidence.from_dict(row) for row in state.asset_operation.to_dict()["replaces"])
        selected = {item.target: item for item in evidence}
        operations = []
        for entry in plan.entries:
            item = selected.get(str(entry.target_path))
            operations.append(dict(source=str(entry.source_path), target=str(entry.target_path),
                action="replace" if item else "reuse_existing" if entry.collision_resolution else "create",
                replacement_evidence=item.to_dict() if item else None,
                collision_resolution=entry.collision_resolution.to_dict() if entry.collision_resolution else None))
        transfer = Trans(plan.as_mapping(), plan.task_id, PlanStore.fingerprint(plan), mode="hardlink",
                         dry_run=False, record_root=self.root / "records", operations=operations,
                         replacement_authority=approved_batch(plan, PlanStore.fingerprint(plan)))
        self.assertIsNone(transfer.trans_file())
        self.assertEqual(service.validate(plan, state, completed=True), state.asset_operation)

    async def test_real_series_completion_resolves_only_selected_season(self):
        from tests import _media_flow_helpers as tv_fixture
        harness = tv_fixture.MediaFlow("runTest")
        old, _archive = await harness._materialize_s1(self.root)
        source, _archive = tv_fixture.make_season(self.root / "new-release")
        new_video = next(source.rglob("*.mkv"))
        # Keep a valid media container while changing the frozen release bytes.
        with new_video.open("ab") as stream:
            stream.write(b"new release marker")
        incoming = await harness._run_flow(root=self.root, source=source,
                                          task_id="new-tv-release", subject_id="41658")
        self.assertTrue(incoming.state.confirmed_work, incoming.task.get('error'))
        before = file_tree(self.root)
        operation, _draft = self.service().prepare_replacement(incoming.plan)
        self.assertEqual(operation.to_dict()["mode"], "replace")
        self.assertTrue(operation.to_dict()["slot_key"].endswith(":season:1"))
        value = operation.to_dict()
        self.assertEqual([], value['previous_assets'])
        self.assertEqual({row['task_id'] for row in value['catalog_before']['rows']['bindings']},
                         {old.plan.task_id})
        self.assertTrue(all(Path(row['target']).parent.name == 'Season 01'
                            for row in value['season_directory']['files']))
        self.assertEqual(before, file_tree(self.root))

    async def test_two_completed_versions_select_only_requested_version(self):
        first, second = await self.fixture.pair()
        incoming = await self.incoming()
        before = {str(entry.target_path): entry.target_path.read_bytes() for entry in second.plan.entries}
        operation, _draft = self.service().prepare_replacement(incoming)
        self.assertEqual({row["task_id"] for row in operation.to_dict()["previous_assets"]}, {first.plan.task_id})
        self.assertNotIn(second.plan.task_id, {row["task_id"] for row in operation.to_dict()["previous_assets"]})
        from pathlib import Path
        self.assertEqual(before, {path: Path(path).read_bytes() for path in before})

    async def test_selected_catalog_generation_change_blocks_frozen_action(self):
        old = await self.fixture.single(with_font=False)
        state, plan = self.commit(await self.incoming())
        catalog_path = self.fixture.catalog_path(old.plan)
        catalog = json.loads(catalog_path.read_text("utf-8"))
        catalog["versions"][0]["display_label"] = "changed selected asset generation"
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "闭包|代际"):
            self.service().validate(plan, state)

    async def test_all_old_targets_missing_still_selects_replacement_generation(self):
        from src.rename.trans import Trans
        old = await self.fixture.single(with_font=False)
        incoming = await self.incoming()
        for entry in old.plan.entries:
            entry.target_path.unlink()
        state, plan = self.commit(incoming)
        value = state.asset_operation.to_dict()
        self.assertEqual(value["mode"], "replace")
        self.assertEqual(value["replaces"], [])
        self.assertTrue(value["previous_assets"])
        self.assertTrue(all(row["target_evidence"] is None for row in value["previous_assets"]))
        self.assertTrue(all(entry.collision_resolution is None for entry in plan.entries))
        self.service().validate(plan, state)
        self.assertIsNone(Trans(plan.as_mapping(), plan.task_id, PlanStore.fingerprint(plan),
                                mode="hardlink", dry_run=False, record_root=self.root / "records").trans_file())
        self.service().validate(plan, state, completed=True)


if __name__ == "__main__":
    unittest.main()
