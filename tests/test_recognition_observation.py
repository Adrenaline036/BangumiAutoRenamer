"""New observation adapter boundaries; disposable admission, no Provider I/O."""
from dataclasses import replace
import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_content_policy_contract as fixture
from tests.test_content_choice_contract import records
from src.rename.projection_authority import RecognitionDecisionInput, commit_recognition_projection, TaskTerminalMutation
from src.rename.rename_plan import WorkIdentity, ProviderIdentity
from src.rename.decision_state import DecisionCommandService
from tests import test_r1_real_media_production_red_contract as queue_fixture
from tests import _media_flow_helpers as movie_fixture
from tests import _media_flow_helpers as source_fixture


class RecognitionObservationContracts(unittest.IsolatedAsyncioTestCase):
    root = fixture.ContentPolicyContracts.root
    sources = fixture.ContentPolicyContracts.sources
    manager = fixture.ContentPolicyContracts.manager
    request = fixture.ContentPolicyContracts.request
    admission = fixture.ContentPolicyContracts.admission
    pending = fixture.ContentPolicyContracts.pending
    assert_policy = fixture.ContentPolicyContracts.assert_policy

    def adapter(self):
        self.assertIsNotNone(importlib.util.find_spec('src.rename.recognition_observation'))
        from src.rename.recognition_observation import adapt_new_recognition_observation
        return adapt_new_recognition_observation

    def recognition(self, plan):
        return RecognitionDecisionInput(plan.task_id, WorkIdentity(status='confirmed',
            title='Fixture', year=2020, domain='animation',
            primary_provider=ProviderIdentity(status='confirmed', namespace='tmdb', entity_type='movie',
                provider_id='123', title='Fixture', year=2020, decision_source='explicit_user')))

    async def test_absent_fields_only_and_current_record_pin(self):
        root = self.root()
        _, service, state, plan = await self.pending(root)
        raw = replace(plan, metadata={k: v for k, v in plan.metadata.items()
                                    if k not in ('content_policy', 'initial_source_basis')})
        adapter = self.adapter()
        with patch('src.rename.source_basis.project_source_basis', side_effect=AssertionError('rescan')):
            draft, recognition = adapter(service.decision_state_store, self.recognition(raw), raw)
        self.assertEqual(state.initial_source_basis.to_dict(), draft.to_dict()['metadata']['initial_source_basis'])
        self.assertEqual(plan.metadata['content_policy'], draft.metadata['content_policy'])
        self.assertEqual(state.initial_source_basis, recognition.candidate(state).initial_source_basis)
        DecisionCommandService(service.decision_state_store, service.plan_store).update(
            plan.task_id, expected_revision=state.revision, changes={}, planner=lambda _: plan)
        before = records(root)
        with self.assertRaisesRegex((ValueError, RuntimeError), 'stale|变化|过期'):
            commit_recognition_projection(service.decision_state_store, service.plan_store, recognition, draft)
        self.assertEqual(before, records(root))

    async def test_explicit_conflicts_and_wrong_task_rejected_without_writes(self):
        root = self.root()
        _, service, state, plan = await self.pending(root)
        adapter = self.adapter()
        for key, value in (('content_policy', None), ('content_policy', 'wrong'),
                           ('initial_source_basis', None), ('initial_source_basis', {}),
                           ('initial_source_basis_fingerprint', '0' * 64)):
            with self.subTest(key=key, value=value):
                before = records(root)
                with self.assertRaises((ValueError, RuntimeError)):
                    adapter(service.decision_state_store, self.recognition(plan),
                            replace(plan, metadata={**plan.metadata, key: value}))
                self.assertEqual(before, records(root))
        with self.assertRaises((ValueError, RuntimeError)):
            adapter(service.decision_state_store, replace(self.recognition(plan), task_id='other'), plan)

    async def test_actual_movie_adapter_success_disconnect_conflict_wrong_task_and_drift(self):
        from src.rename import process
        from src.rename.selection_gate import validate_selection_for_plan
        from src.rename.projection_authority import validate_plan_projection_receipt
        adapter = self.adapter()
        for movie in (True,):
            for attack in ('healthy', 'disconnect', 'conflict', 'wrong_task', 'drift'):
                with self.subTest(movie=movie, attack=attack):
                    root = self.root()
                    if movie:
                        source, _, _ = source_fixture._make_version(root / 'sources', '1080p')
                    else:
                        source = queue_fixture._make_season(root / 'sources',
                            '[VCB-Studio] Yama no Susume [Ma10p_1080p]', '[VCB-Studio] Yama no Susume')
                        for video in source.glob('*.mkv'):
                            video.rename(video.with_suffix('.mp4'))
                    seen = []
                    protected = []
                    def observed(store, recognition, raw):
                        state = store.load_optional(raw.task_id)
                        if state is None:
                            return adapter(store, recognition, raw)
                        seen.append(raw.task_id)
                        if attack == 'disconnect':
                            return raw, recognition
                        if attack == 'conflict':
                            raw = replace(raw, metadata={**raw.metadata, 'content_policy': None})
                        if attack == 'wrong_task':
                            recognition = replace(recognition, task_id='wrong-task')
                        adapted = adapter(store, recognition, raw)
                        if attack == 'drift':
                            plans = movie_fixture.PlanStore(root / 'plans')
                            current = plans.load(raw.task_id)
                            _, newer = DecisionCommandService(store, plans).update(raw.task_id,
                                expected_revision=state.revision, changes={}, planner=lambda _: current,
                                task_terminal=TaskTerminalMutation.prepare(root / 'tasks', raw.task_id,
                                    lambda _state, _plan, task: task))
                            protected.append(plans.fingerprint(newer))
                        return adapted
                    with (patch.object(process, 'adapt_new_recognition_observation', observed),
                          patch.object(process, 'SERIES_CATALOG_PATH', root / 'series_catalog'),
                          patch.object(process, 'RECORD_PATH', root / 'records')):
                        if movie:
                            harness = movie_fixture.MediaFlow('runTest')
                            result = await harness._run_provider_flow(root, source, 'entry-task')
                        else:
                            harness = SimpleNamespace(_NoopReviewDialog=queue_fixture.R1RealQueueWorkerContracts._NoopReviewDialog)
                            result = await queue_fixture.R1RealQueueWorkerContracts._run_flow(harness,
                                root=root, source=source, task_id='entry-task',
                                candidate=queue_fixture._candidate('41658'), is_anime=True, is_movie=False)
                    self.assertTrue(seen, 'actual process producer did not reach adapter')
                    if attack == 'healthy':
                        validate_selection_for_plan(result.task)
                        validate_plan_projection_receipt(result.plan)
                        self.assertEqual(result.initial_state.initial_source_basis, result.state.initial_source_basis)
                        self.assertEqual(result.decision_store.fingerprint(result.state),
                                         result.plan.metadata['decision_state_fingerprint'])
                    else:
                        with self.assertRaises((ValueError, RuntimeError)):
                            validate_selection_for_plan(result.task)
                        if attack == 'drift':
                            self.assertTrue(protected, 'later real revision was not reached')
                            self.assertEqual(protected[-1], result.plan_store.fingerprint(result.plan))

    async def test_new_tv_complete_producer_does_not_reenter_legacy_observation_adapter(self):
        from src.rename import process
        from src.rename.selection_gate import validate_selection_for_plan
        from src.rename.projection_authority import validate_plan_projection_receipt
        root = self.root()
        source = queue_fixture._make_season(root/'sources',
            '[VCB-Studio] Yama no Susume [Ma10p_1080p]', '[VCB-Studio] Yama no Susume')
        for video in source.glob('*.mkv'):
            video.rename(video.with_suffix('.mp4'))
        before = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
        harness = SimpleNamespace(_NoopReviewDialog=queue_fixture.R1RealQueueWorkerContracts._NoopReviewDialog)
        with patch.object(process, 'adapt_new_recognition_observation',
                          side_effect=AssertionError('new TV reached retired adapter')) as legacy, \
                patch.object(process, 'SERIES_CATALOG_PATH', root/'series_catalog'), \
                patch.object(process, 'RECORD_PATH', root/'records'):
            result = await queue_fixture.R1RealQueueWorkerContracts._run_flow(harness,
                root=root, source=source, task_id='entry-task',
                candidate=queue_fixture._candidate('41658'), is_anime=True, is_movie=False)
        legacy.assert_not_called()
        validate_selection_for_plan(result.task)
        validate_plan_projection_receipt(result.plan)
        self.assertTrue(result.state.confirmed_work, result.task.get('error'))
        self.assertEqual('initial_tv_completion', result.plan.revision_action)
        self.assertEqual(result.initial_state.initial_source_basis, result.state.initial_source_basis)
        self.assertEqual(before, {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()})

    async def test_canonical_does_not_autofill_direct_clears(self):
        root = self.root()
        _, service, _, plan = await self.pending(root)
        for key in ('content_policy', 'initial_source_basis'):
            with self.subTest(key=key):
                raw = replace(plan, metadata={k: v for k, v in plan.metadata.items() if k != key})
                before = records(root)
                with self.assertRaises((ValueError, RuntimeError)):
                    commit_recognition_projection(service.decision_state_store, service.plan_store,
                                                  self.recognition(raw), raw)
                self.assertEqual(before, records(root))

    async def test_actual_empty_and_legacy_stores_do_not_fabricate_policy_or_basis(self):
        from src.rename.decision_state import DecisionStateStore
        root = self.root()
        _, _, _, plan = await self.pending(root)
        raw = replace(plan, metadata={})
        store = DecisionStateStore(root / 'legacy-context' / 'decisions')
        adapter = self.adapter()
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    store.create(plan.task_id)
                before = records(root)
                adapted, recognition = adapter(store, self.recognition(raw), raw)
                self.assertEqual({}, adapted.to_dict()['metadata'])
                candidate = recognition.candidate(store.load_optional(plan.task_id))
                self.assertIsNone(candidate.content_policy)
                self.assertIsNone(candidate.initial_source_basis)
                self.assertEqual(before, records(root))
