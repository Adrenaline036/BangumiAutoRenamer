"""ERR038: full-package public planning path; only temporary synthetic media."""
import asyncio
from contextlib import ExitStack, nullcontext
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.pages.review_operation_view import ReviewCommandApplyResult
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_review import PlanReviewService
from src.rename.plan_store import PlanStore
from src.rename.projection_authority import ProjectionAuthorityError
from src.task_queue import TaskQueueManager
from src.utils.utils import read_task_at
import tests.test_provider_subject_plan_wiring_red_contract as fixture
from tests.test_r24_phase_c_review_operation_migration_contract import current_provider_selection_context
from tests.test_r24_yama_special_order_dependence_incident_red_contract import _apply_boundary, _configured_result_roots


class RetryPolicyContract(unittest.IsolatedAsyncioTestCase):
    async def run_sequence(self, old_dry, new_dry, *, disconnect=False, execute=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flow = root / "flow"
            source = fixture._make_episode_source(root / "source", "86", tuple(range(1, 24)))
            original_stop = TaskQueueManager.stop
            observed = {}

            async def exercise_retry(manager):
                task_id = next(manager.task_root.glob("*.json")).stem
                result = SimpleNamespace(root=flow, task_id=task_id, tasks=manager.task_root,
                    plans=flow / "plans", decisions=flow / "decisions", reviews=flow / "reviews",
                    manager=manager, renamer=renamer)
                # The planning fixture patched global roots only while creating
                # the manager. Execution must use explicit isolated stores too.
                manager.execution_service = PlanExecutionService(
                    manager.plan_store,
                    PlanReviewService(manager.plan_store, result.reviews,
                        flow / 'work_catalog', decision_state_root=result.decisions,
                        task_root=result.tasks),
                    task_root=result.tasks, record_root=flow / 'records',
                    catalog_root=flow / 'work_catalog', series_catalog_root=flow / 'series_catalog',
                    decision_state_root=result.decisions, recovery_transaction_root=flow / 'recovery',
                    provider_link_catalog_root=flow / 'provider_links')
                def reload():
                    result.task = read_task_at(manager.task_root, task_id)
                    result.plan = manager.plan_store.load(task_id)
                    result.state = manager.decision_state_store.load(task_id)
                    return result.plan
                try:
                    original = reload()
                    self.assertEqual("awaiting_user", current_provider_selection_context(result.task)["state"])
                    self.assertIs(original.execution_policy.dry_run, old_dry)
                    self.assertEqual(ReviewCommandApplyResult.APPLIED, _apply_boundary(result))
                    old = reload()
                    self.assertEqual("succeeded", current_provider_selection_context(result.task)["state"])
                    old_bytes = (result.plans / f"{task_id}.json").read_bytes()
                    old_fp = PlanStore.fingerprint(old)
                    old_rev = manager.decision_state_store.load(task_id).revision
                    lineage = result.task["task_source_scope"]
                    batch = result.task["batch_id"]
                    old_policy = old.execution_policy.to_dict()
                    # Refresh consumes the shared frozen TV authority builder;
                    # the legacy process ownership helper is no longer its gate.
                    disconnect_target = ("src.rename.tv_planning_authority.TVPlanningAuthorityBuilder.build"
                        if disconnect == 'ownership' else "src.rename.trans.is_dry_run_enabled")
                    disconnected = patch(disconnect_target,
                        side_effect=RuntimeError("policy authority disconnected")) if disconnect else nullcontext()
                    with patch.dict(os.environ, {"BAR_DRY_RUN": "1" if new_dry else "0"}), disconnected as reader:
                        await manager.retry_current(task_id)
                        await asyncio.wait_for(manager._queue.join(), timeout=12)
                    draft = reload()
                    context = current_provider_selection_context(result.task)
                    self.assertEqual(lineage, result.task["task_source_scope"])
                    self.assertEqual(batch, result.task["batch_id"])
                    self.assertNotEqual("completed", result.task["workflow_status"])
                    if disconnect:
                        reader.assert_called_once()
                        self.assertEqual("failed", result.task['current_tv_refresh']['status'])
                        self.assertEqual("failed", result.task['workflow_status'])
                        self.assertEqual(old_bytes, (result.plans / f"{task_id}.json").read_bytes())
                        self.assertEqual(old_rev, manager.decision_state_store.load(task_id).revision)
                        return
                    self.assertEqual("succeeded", context["state"], result.task.get('error'))
                    self.assertIsNone(result.task.get("completed_at"))
                    observed["draft_dry_run"] = draft.execution_policy.dry_run
                    new_policy = {**old_policy, 'dry_run': new_dry}
                    self.assertEqual(new_policy, draft.execution_policy.to_dict())
                    self.assertGreater(manager.decision_state_store.load(task_id).revision, old_rev)
                    self.assertNotEqual(old_fp, PlanStore.fingerprint(draft))
                    # Stop/restart the same worker over disk state; no outcome/Plan injection.
                    await original_stop(manager)
                    before_restart = (result.plans / f"{task_id}.json").read_bytes()
                    with patch.dict(os.environ, {"BAR_DRY_RUN": "0" if new_dry else "1"}):
                        await manager.start()
                        reload()
                        self.assertEqual("succeeded", current_provider_selection_context(result.task)["state"])
                    self.assertEqual(before_restart, (result.plans / f"{task_id}.json").read_bytes())
                    completed = reload()
                    observed["completed_dry_run"] = completed.execution_policy.dry_run
                    self.assertEqual(new_policy, completed.execution_policy.to_dict())
                    if new_dry:
                        from src.rename.plan_execution import require_approved_execution_policy
                        with self.assertRaises(PlanExecutionError) as denied:
                            require_approved_execution_policy(completed.execution_policy)
                        self.assertEqual('dry_run_plan_requires_retry', denied.exception.code)
                    self.assertEqual("succeeded", current_provider_selection_context(result.task)["state"])
                    self.assertEqual("invalid", result.task["workflow_status"])
                    from src.rename.plan_verifier import PlanVerifier
                    self.assertEqual(set(), {i.code for i in PlanVerifier().verify(completed).issues})
                    self.assertEqual("hardlink", completed.execution_policy.mode)
                    self.assertTrue(completed.execution_policy.preserve_source)
                    self.assertEqual("331887", completed.work_identity.primary_provider.provider_id)
                    self.assertEqual(set(range(1, 24)), {e.episode_id for e in completed.entries})
                    self.assertEqual({1}, {e.season_id for e in completed.entries})
                    owners = completed.metadata['task_episode_ownership']
                    self.assertEqual(23, len(owners))
                    self.assertEqual({episode: '302189' if episode <= 11 else '331887'
                                      for episode in range(1, 24)},
                        {row['target_episode']: row['bangumi_subject_id'] for row in owners})
                    authority = manager.decision_state_store.load(task_id).tv_planning_authority
                    self.assertEqual({'302189': 11, '331887': 12},
                        {row['bangumi_subject_id']: len(row['sources'])
                         for row in authority.episode_range_ownership})
                    history = manager.plan_store.load_version(task_id, old_fp)
                    self.assertEqual(old_policy, history.execution_policy.to_dict())
                    review = PlanReviewService(manager.plan_store, result.reviews,
                        decision_state_root=result.decisions, task_root=result.tasks)
                    self.assertEqual("draft", review.load_or_create(completed).status)
                    with self.assertRaises(PlanExecutionError):
                        manager.execution_service.validate_approved(task_id, old_fp)
                    with self.assertRaises(PlanExecutionError):
                        manager.execution_service.validate_approved(task_id, PlanStore.fingerprint(completed))
                    self.assertFalse(any(e.target_path.exists() for e in completed.entries))
                    if execute:
                        from tests.test_provider_subject_execution_catalog_red_contract import ProviderSubjectExecutionCatalogRedContracts
                        approval = ProviderSubjectExecutionCatalogRedContracts(methodName='runTest')
                        _store, _review, approved, fingerprint = await approval._approve(result)
                        self.assertEqual('inventory_smart_completion', approved.metadata['mapping_source'])
                        self.assertNotIn('source_episode_projection', approved.metadata)
                        self.assertEqual('approved', read_task_at(result.tasks, task_id)['workflow_status'])
                        if execute is not True:
                            # Fault injection only: no downstream snapshot is
                            # fabricated for a successful execution.
                            injected = approved
                            error_type = (PlanExecutionError, ValueError)
                            error_pattern = '分集投影凭据无效'
                            guard = nullcontext()
                            if execute == 'missing_receipt':
                                injected = replace(approved, projection_receipt=None)
                                error_type = ProjectionAuthorityError
                                error_pattern = '^Plan has no ProjectionReceipt$'
                            elif execute == 'typed_drift':
                                injected = replace(approved, metadata={**approved.metadata,
                                    'tv_planning_authority_schema_version': 0})
                                error_type = ProjectionAuthorityError
                                error_pattern = ('^ProjectionReceipt does not match Plan fields: '
                                    'tv_planning_authority_schema_version$')
                            elif execute == 'disconnect':
                                guard = patch('src.rename.plan_execution.validate_plan_projection_receipt',
                                    side_effect=ValueError('canonical authority disconnected'))
                                error_pattern = 'canonical authority disconnected'
                            elif execute == 'source_drift':
                                approved.entries[0].source_path.write_bytes(b'changed after approval')
                                error_pattern = '源文件|来源.*变化|identity漂移|Verifier'
                            if injected is not approved:
                                # Simulate corruption on disk, so admission
                                # exercises the real loader, not a load mock.
                                manager.plan_store.path_for(task_id).write_text(
                                    json.dumps(injected.to_dict()), encoding='utf-8')
                            with guard:
                                # Queue and direct execution must both fail;
                                # neither may make a transfer/ledger/Catalog.
                                if execute == 'source_drift':
                                    with patch.dict(os.environ, {'BAR_DRY_RUN': '0'}):
                                        with self.assertRaisesRegex(PlanExecutionError, error_pattern):
                                            manager.execution_service.execute(task_id, fingerprint)
                                else:
                                    with self.assertRaisesRegex(error_type, error_pattern):
                                        await manager.enqueue_approved(task_id, PlanStore.fingerprint(injected))
                                    with self.assertRaisesRegex(error_type, error_pattern):
                                        manager.execution_service.execute(task_id, PlanStore.fingerprint(injected))
                            self.assertFalse(any(e.target_path.exists() for e in approved.entries))
                            self.assertFalse(tuple((flow / 'records').glob('*.ledger.json')))
                            self.assertFalse(tuple((flow / 'series_catalog').glob('*.json')))
                            return
                        with patch.dict(os.environ, {'BAR_DRY_RUN': '0'}):
                            await manager.enqueue_approved(task_id, fingerprint)
                            await asyncio.wait_for(manager._queue.join(), timeout=20)
                        task = read_task_at(result.tasks, task_id)
                        self.assertEqual('completed', task['execution_status'], task.get('error'))
                        self.assertEqual('partial_failed', task['workflow_status'])
                        self.assertIn('检查', task['font_error'])
                        for entry in approved.entries:
                            self.assertTrue(entry.target_path.samefile(entry.source_path))
                            self.assertGreaterEqual(entry.target_path.stat().st_nlink, 2)
                        self.assertTrue(tuple((flow / 'records').glob('*.ledger.json')))
                        catalog = manager.execution_service.series_catalog.load(approved.metadata['series_key'])
                        self.assertEqual(approved.metadata['canonical_work_root'], str(catalog.canonical_root))
                finally:
                    await original_stop(manager)

            harness = fixture.ProviderSubjectPlanWiringRedContracts(methodName="runTest")
            before = fixture._source_manifest(source)
            with patch.dict(os.environ, {"BAR_DRY_RUN": "1" if old_dry else "0"}):
                result = await harness._run_real_add_select(root=flow, source=source,
                    subjects=fixture.EIGHTY_SIX_SUBJECTS, relations=fixture.EIGHTY_SIX_RELATIONS,
                    selected_subject_id="331887", fail_subject=("第2部分", "第2クール"),
                    failure_mode="no_match")
                # The first public flow stops normally. Reopen its persisted
                # state with isolated external providers, without intercepting stop.
                renamer = fixture._configured_renamer(flow,
                    fail_subject=("第2部分", "第2クール"), failure_mode="no_match")
                with ExitStack() as stack:
                    stack.enter_context(patch.dict(fixture.production_fixture.YAMA_SUBJECTS,
                        fixture.EIGHTY_SIX_SUBJECTS, clear=True))
                    stack.enter_context(patch.dict(fixture.production_fixture.YAMA_RELATIONS,
                        fixture.EIGHTY_SIX_RELATIONS, clear=True))
                    stack.enter_context(patch("src.task_queue.Rename", return_value=renamer))
                    for name, target in {"TASK_PATH": result.tasks, "PLAN_PATH": result.plans,
                        "SERIES_CATALOG_PATH": flow / "series_catalog", "RECORD_PATH": flow / "records"}.items():
                        stack.enter_context(patch(f"src.rename.process.{name}", target))
                    stack.enter_context(_configured_result_roots(result))
                    await result.manager.start()
                    await exercise_retry(result.manager)
            if execute != 'source_drift':
                self.assertEqual(before, fixture._source_manifest(source))
            for name in (() if execute is True else ("records", "provider_links", "series_catalog", "work_catalog", "recovery", "abandon",
                         "Anime", "AnimeMovies")):
                self.assertFalse(tuple(p for p in (flow / name).rglob("*.*")
                    if p.is_file() and not p.name.endswith('.lock')), name)
            if not disconnect:
                self.assertEqual({"draft_dry_run": new_dry, "completed_dry_run": new_dry}, observed)

    async def test_preview_to_execution_full_package(self):
        await self.run_sequence(True, False)

    async def test_err058_full_package_retry_then_real_execution(self):
        await self.run_sequence(False, False, execute=True)

    async def test_err058_missing_receipt_is_not_a_legacy_fallback(self):
        await self.run_sequence(False, False, execute='missing_receipt')

    async def test_err058_typed_authority_drift_is_not_accepted(self):
        await self.run_sequence(False, False, execute='typed_drift')

    async def test_err058_disconnected_receipt_blocks_execution(self):
        await self.run_sequence(False, False, execute='disconnect')

    async def test_err058_changed_source_blocks_before_transfer(self):
        await self.run_sequence(False, False, execute='source_drift')

    async def test_execution_to_preview_full_package(self):
        await self.run_sequence(False, True)

    async def test_preview_control_stays_preview(self):
        await self.run_sequence(True, True)

    async def test_execution_control_stays_execution(self):
        await self.run_sequence(False, False)

    async def test_failed_policy_observation_cannot_publish_refresh(self):
        await self.run_sequence(True, False, disconnect=True)

    async def test_disconnected_source_ownership_cannot_publish_new_plan(self):
        await self.run_sequence(True, False, disconnect='ownership')


if __name__ == "__main__":
    unittest.main()
