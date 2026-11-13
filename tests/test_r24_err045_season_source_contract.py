"""ERR045: actual rules producer, persisted retry and GUI cohort consumer."""
import asyncio
from contextlib import ExitStack, nullcontext
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.pages.review_operation_view import ReviewCommandApplyResult
from src.rename.plan_execution import PlanExecutionError
from src.rename.plan_review import PlanReviewError, PlanReviewService, _season_decision_from_metadata, entry_id
from src.rename.plan_store import PlanStore
from src.utils.utils import read_task_at
import tests.test_provider_subject_plan_wiring_red_contract as fixture
from tests.test_r24_phase_c_review_operation_migration_contract import current_provider_selection_context
from tests.test_r24_yama_special_order_dependence_incident_red_contract import _apply_boundary, _FrozenYamaSpecialSearch


class SeasonSourceContract(unittest.TestCase):
    def test_legitimate_source_preserves_provenance(self):
        for source in ('provider_crosswalk', 'provider', 'explicit_user', 'inferred_path',
                       'legacy_explicit', 'legacy_placeholder'):
            with self.subTest(source=source):
                self.assertEqual((4, source), _season_decision_from_metadata(
                    {'season_number': 4, 'season_source': source}))

    def test_unknown_and_invalid_season_fail_closed(self):
        for metadata in ({'season_source': 'provider_unknown', 'season_number': 4},
                         {'season_source': 'provider', 'season_number': -1},
                         {'season_source': 'provider', 'season_number': 'oops'}):
            with self.subTest(metadata=metadata), self.assertRaises(PlanReviewError):
                _season_decision_from_metadata(metadata)

    def test_legacy_zero_never_becomes_explicit_special(self):
        self.assertEqual((None, 'legacy_placeholder'), _season_decision_from_metadata({'season_id': 0}))
        self.assertEqual((2, 'legacy_explicit'), _season_decision_from_metadata({'season_id': 2}))
        self.assertEqual((None, 'unset'), _season_decision_from_metadata({}))


class RulesRetryIncident(unittest.IsolatedAsyncioTestCase):
    async def run_sequence(self, new_dry, *, disconnect=False, unknown_source=False):
        # Retry refreshes the persisted TV work; it does not re-search Providers
        # or reinterpret the saved season. A new unapproved revision deliberately
        # freezes the current dry-run setting; the old approval stays stale.
        from src.rename import initial_tv_completion
        from src.rename.tv_completion_observations import TVSeasonObservation
        from src.rename.initial_movie_completion import RootSnapshotSource
        from src.rename.target_routing import TargetRootSnapshot
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flow = root / 'flow'
            source = fixture._make_episode_source(root / 'source', 'Yama no Susume Next Summit', tuple(range(1, 13)))
            before = fixture._source_manifest(source)
            harness = fixture.ProviderSubjectPlanWiringRedContracts(methodName='runTest')
            with patch.dict(os.environ, {'BAR_DRY_RUN': '1'}):
                result = await harness._run_real_add_select(root=flow, source=source,
                    subjects=fixture.YAMA_SUBJECTS, relations=fixture.YAMA_RELATIONS,
                    selected_subject_id='290980')
            manager = result.manager
            original = manager.plan_store.load(result.task_id)
            self.assertEqual('inventory_smart_completion', original.metadata['mapping_source'])
            self.assertEqual('provider_crosswalk', original.metadata['season_source'])
            self.assertTrue(original.execution_policy.dry_run)
            snapshot = TargetRootSnapshot.from_dict(original.metadata['target_root_snapshot'])
            manager.root_snapshot_source = RootSnapshotSource(lambda: snapshot)
            review = PlanReviewService(manager.plan_store, result.reviews, flow / 'work_catalog',
                decision_state_root=result.decisions, task_root=result.tasks,
                root_snapshot_source=manager.root_snapshot_source)
            old_fp = PlanStore.fingerprint(original)
            self.assertEqual('approved', review.approve_entries(result.task_id, old_fp,
                [entry_id(e) for e in original.entries]).status)
            old_state = manager.decision_state_store.load(result.task_id)
            old_task = read_task_at(result.tasks, result.task_id)
            plan_path, state_path = manager.plan_store.path_for(result.task_id), manager.decision_state_store.path_for(result.task_id)
            frozen_bytes = (plan_path.read_bytes(), state_path.read_bytes())
            actual_prepare = initial_tv_completion.prepare
            reached = []
            def prepare(service, recognition, **kwargs):
                reached.append(kwargs['mode'])
                if disconnect:
                    raise ValueError('current TV completion disconnected')
                if unknown_source:
                    # Invalid actual frozen observation at its current reader,
                    # not a raw metadata note or a fabricated accepted Plan.
                    kwargs['seasons'] = TVSeasonObservation.from_mapping({
                        **kwargs['seasons'].to_dict(), 'available': ['unknown']})
                return actual_prepare(service, recognition, **kwargs)
            with fixture._root_config_patch(snapshot.to_dict()), patch('src.task_queue.Rename',
                    side_effect=AssertionError('current TV refresh searched Providers')), patch.object(
                    initial_tv_completion, 'prepare', prepare):
                await manager.start()
                try:
                    with patch.dict(os.environ, {'BAR_DRY_RUN': '1' if new_dry else '0'}):
                        await manager.retry_current(result.task_id)
                        await asyncio.wait_for(manager._queue.join(), timeout=12)
                finally:
                    await manager.stop()
            self.assertEqual(['refresh'], reached)
            current = manager.plan_store.load(result.task_id)
            task = read_task_at(result.tasks, result.task_id)
            state = manager.decision_state_store.load(result.task_id)
            self.assertEqual(old_task['task_source_scope'], task['task_source_scope'])
            self.assertEqual(old_task['batch_id'], task['batch_id'])
            if disconnect or unknown_source:
                self.assertEqual('failed', task['current_tv_refresh']['status'])
                self.assertEqual(frozen_bytes, (plan_path.read_bytes(), state_path.read_bytes()))
                self.assertEqual(old_state.revision, state.revision)
                self.assertIn('disconnected' if disconnect else '季度', task['error'])
            else:
                self.assertEqual('completed', task['current_tv_refresh']['status'])
                self.assertEqual('invalid', task['workflow_status'])
                from src.rename.plan_verifier import PlanVerifier
                self.assertEqual(set(), {i.code for i in PlanVerifier().verify(current).issues})
                self.assertGreater(state.revision, old_state.revision)
                self.assertEqual('draft', review.load_or_create(current).status)
                self.assertEqual('290980', current.work_identity.primary_provider.provider_id)
                self.assertEqual({4}, {entry.season_id for entry in current.entries})
                self.assertEqual(set(range(1, 13)), {entry.episode_id for entry in current.entries})
                self.assertEqual(replace(original.execution_policy, dry_run=new_dry), current.execution_policy)
                self.assertEqual('provider_crosswalk', current.metadata['season_source'])
            saved = (plan_path.read_bytes(), state_path.read_bytes())
            with patch.dict(os.environ, {'BAR_DRY_RUN': '0' if new_dry else '1'}):
                await manager.start()
                try:
                    with self.assertRaises(PlanExecutionError):
                        manager.execution_service.validate_approved(result.task_id, old_fp)
                finally:
                    await manager.stop()
            self.assertEqual(saved, (plan_path.read_bytes(), state_path.read_bytes()))
            self.assertEqual('provider_crosswalk', manager.plan_store.load_version(result.task_id, old_fp).metadata['season_source'])
            self.assertFalse(any(e.target_path.exists() for e in current.entries))
            self.assertEqual(before, fixture._source_manifest(source))
            for name in ('records', 'provider_links', 'series_catalog', 'work_catalog', 'recovery', 'abandon', 'Anime', 'AnimeMovies'):
                self.assertFalse(tuple(p for p in (flow / name).rglob('*')
                                       if p.is_file() and p.suffix != '.lock'), name)

    async def test_rules_retry_restart_execution_plan(self):
        await self.run_sequence(False)

    async def test_rules_retry_restart_preview_plan(self):
        await self.run_sequence(True)

    async def test_disconnect_shared_source_contract_blocks_confirmation(self):
        await self.run_sequence(False, disconnect=True)

    async def test_unknown_source_consumer_cannot_publish(self):
        await self.run_sequence(False, unknown_source=True)
