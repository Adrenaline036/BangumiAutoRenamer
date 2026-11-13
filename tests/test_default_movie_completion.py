"""First movie completion after v2: actual selection handoff, isolated media.

Provider transport is replayed. No real source, retained fixture or execution.
"""
from dataclasses import replace
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import _media_flow_helpers as movie_fixture
from tests import _media_flow_helpers as source_fixture
from tests.test_content_choice_contract import records
from src.rename.decision_state import DecisionStateStore, DecisionCommandService
from src.rename.plan_review import PlanReviewService
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.selection_gate import validate_selection_for_plan
from src.rename.target_routing import TargetRootSnapshot
from src.rename.projection_authority import TaskTerminalMutation, ProjectionCommitService, commit_projected_plan


class InitialMovieCompletionContracts(unittest.IsolatedAsyncioTestCase):
    def root(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / 'initial-movie-content'
        root.mkdir()
        return root

    def service(self, root):
        return PlanReviewService(PlanStore(root / 'plans'), root / 'reviews', root / 'work_catalog',
            decision_state_root=root / 'decisions', task_root=root / 'tasks', record_root=root / 'records')

    def initialize(self, service, plan):
        preview = service.prepare_default_content_initialization(plan.task_id, PlanStore.fingerprint(plan))
        return service.commit_default_content_initialization(preview)

    async def fixture(self, root, *, initialize_before_selection, at_observation=None):
        source, primary, _ = source_fixture._make_version(root / 'sources', '1080p')
        (source / 'README.txt').write_text('isolated ancillary content', encoding='utf-8')
        service = self.service(root)
        original = movie_fixture.PlanReviewDialog._select_provider_candidate
        before = {}
        async def selecting(dialog, candidate, **kwargs):
            if initialize_before_selection:
                self.assertFalse(dialog.task_decision.confirmed_work,
                                 'must begin at genuinely unresolved work, not after selection')
                # The default producer now runs before this actual selection
                # event. Do not inject the old initializer to fake that path.
                self.assertIsNotNone(dialog.task_decision.content_selection)
                initialized = dialog.plan
                state = service.decision_state_store.load(initialized.task_id)
                self.assertEqual('content-selection-v2', state.content_selection.to_dict()['policy'])
                self.assertFalse(state.confirmed_work)
                before.update(plan=initialized, state=state)
                dialog.plan, dialog.task_decision = initialized, state
                dialog.task_data = json.loads((root / 'tasks' / f'{initialized.task_id}.json').read_text('utf-8'))
            return await original(dialog, candidate, **kwargs)
        harness = movie_fixture.MediaFlow('runTest')
        from src.rename import process
        from src.task_queue import TaskQueueManager
        wait_result = TaskQueueManager.wait_for_selection_result
        async def bounded_wait(manager, task_id, **kwargs):
            return await wait_result(manager, task_id, timeout=0.3)
        adapt = process.adapt_new_recognition_observation
        observed, failures = [], []
        def observation(states, recognition, draft):
            result = adapt(states, recognition, draft)
            previous = states.load_optional(draft.task_id)
            if at_observation is not None and previous is not None and previous.content_selection is not None:
                observed.append(True)
                try:
                    at_observation(service, result[1], result[0])
                except BaseException as exc:
                    failures.append(exc)
                    raise
            return result
        with (patch.object(movie_fixture.PlanReviewDialog, '_select_provider_candidate', selecting),
              patch.object(process, 'adapt_new_recognition_observation', observation),
              patch.object(TaskQueueManager, 'wait_for_selection_result', bounded_wait)):
            result = await harness._run_provider_flow(root, source, 'first-movie-content')
        if failures:
            raise failures[0]
        if at_observation is not None:
            self.assertTrue(observed, 'real movie resolver observation boundary not reached')
        return service, result, before, primary

    @staticmethod
    def build_selection(module, service, intent):
        terminal = TaskTerminalMutation.prepare(service.task_root, intent.task_id, lambda _s, _p, task: task)
        return module.build(service, intent, service.plan_store.load(intent.task_id),
                            service.decision_state_store.load(intent.task_id), terminal)

    def handoff_module(self):
        self.assertIsNotNone(importlib.util.find_spec('src.rename.initial_movie_selection'),
                             'missing shared initial movie selection handoff')
        from src.rename import initial_movie_selection
        return initial_movie_selection

    async def test_unresolved_v2_real_selection_reaches_confirmed_movie_without_losing_choice(self):
        root = self.root()
        service, result, before, _ = await self.fixture(root, initialize_before_selection=True)
        self.assertTrue(before, 'actual public selection hook was not reached')
        validate_selection_for_plan(result.task)
        self.assertTrue(result.state.confirmed_work)
        self.assertEqual('movie', result.state.confirmed_work['primary_provider']['entity_type'])
        self.assertEqual(before['state'].initial_source_basis, result.state.initial_source_basis)
        old, new = before['state'].content_selection.to_dict(), result.state.content_selection.to_dict()
        for field in ('origin_plan_fingerprint', 'initial_source_basis_fingerprint', 'material_origins',
                      'identities', 'excluded_sources'):
            self.assertEqual(old[field], new[field], field)
        self.assertNotEqual(PlanStore.fingerprint(before['plan']), PlanStore.fingerprint(result.plan))
        self.assertIsNone(result.state.movie_version_selection)
        self.assertIsNone(result.state.layout_mode)
        self.assertNotIn('font_contribution', result.plan.metadata)
        self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(result.plan).issues})

    async def test_first_movie_completion_requires_new_typed_command_not_legacy_writer(self):
        root = self.root()
        service, result, before, primary = await self.fixture(root, initialize_before_selection=True)
        plan = service.plan_store.load(result.plan.task_id)
        self.assertEqual(before['state'].content_selection, service.decision_state_store.load(plan.task_id).content_selection)
        state = service.decision_state_store.load(plan.task_id)
        self.assertIsNone(state.movie_version_selection)
        method = getattr(service, 'prepare_initial_movie_completion', None)
        self.assertTrue(callable(method), 'missing typed first movie completion command for v2 inventory')
        # Full intent/observation schema is frozen in PhaseA, before GREEN.
        # This scaffold must not claim later canonical/journal obligations ran.

    async def test_existing_legacy_completion_remains_closed_for_v2(self):
        root = self.root()
        service, result, before, primary = await self.fixture(root, initialize_before_selection=True)
        plan = service.plan_store.load(result.plan.task_id)
        self.assertEqual(before['state'].content_selection, service.decision_state_store.load(plan.task_id).content_selection)
        snapshot = TargetRootSnapshot.capture(dict(bangumi_path=root / 'TV', movie_path=root / 'Movies',
            anime_path=root / 'Anime', anime_movie_path=root / 'AnimeMovies'))
        before = records(root)
        with self.assertRaisesRegex((ValueError, RuntimeError), '新内容|旧库存|统一内容'):
            service.complete_inventory_draft(plan.task_id, PlanStore.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id, group_kind='movie_continuous',
                target_root=root / 'AnimeMovies', root_snapshot=snapshot.to_dict(),
                primary_video_sources=(primary,), reason='must remain closed')
        self.assertEqual(before, records(root))

    async def test_handoff_public_and_direct_canonical_reject_missing_proof_and_forged_draft(self):
        root = self.root()
        def check(service, recognition, raw):
            module = self.handoff_module()
            intent = module.prepare(service, recognition)
            expected = self.build_selection(module, service, intent)
            state = service.decision_state_store.load(raw.task_id)
            terminal = TaskTerminalMutation.prepare(root / 'tasks', raw.task_id, lambda _s, _p, task: task)
            for attack in ('no_intent', 'disconnect', 'target', 'episode', 'subtitle', 'wrong_service'):
                with self.subTest(attack=attack):
                    before = records(root)
                    draft = expected.draft
                    if attack in ('target', 'episode'):
                        entry = next(e for e in draft.entries if e.media_kind == 'video')
                        edits = {'target_path': root / 'unexpected.mkv'} if attack == 'target' else {'episode_id': 777}
                        draft = replace(draft, entries=tuple(replace(e, **edits) if e == entry else e for e in draft.entries))
                    elif attack == 'subtitle':
                        entry = next(e for e in draft.entries if e.media_kind == 'subtitle')
                        draft = replace(draft, entries=tuple(replace(e, requires_review=not e.requires_review)
                            if e == entry else e for e in draft.entries))
                    if attack == 'no_intent':
                        with self.assertRaises((ValueError, RuntimeError)):
                            commit_projected_plan(service.decision_state_store, service.plan_store,
                                previous=state, candidate=expected.decision, draft_plan=draft, task_terminal=terminal)
                    else:
                        from contextlib import nullcontext
                        with (patch.object(module, 'build', return_value=None) if attack == 'disconnect' else nullcontext()):
                            with self.assertRaises((ValueError, RuntimeError)):
                                if attack == 'wrong_service':
                                    module.commit(service, intent, task_terminal=replace(terminal, task_root=root / 'wrong-tasks'))
                                else:
                                    commit_projected_plan(service.decision_state_store, service.plan_store,
                                        previous=state, candidate=expected.decision, draft_plan=draft,
                                        task_terminal=terminal, initial_movie_selection=intent)
                    self.assertEqual(before, records(root))
            with patch.object(module, 'build', return_value=None):
                before = records(root)
                with self.assertRaises((ValueError, RuntimeError)):
                    module.commit(service, intent, task_terminal=terminal)
                self.assertEqual(before, records(root))
        await self.fixture(root, initialize_before_selection=True, at_observation=check)

    async def test_handoff_operation_and_resolver_must_match_actual_pending_request(self):
        root = self.root()
        def check(service, recognition, raw):
            module = self.handoff_module()
            intent = module.prepare(service, recognition)
            terminal = TaskTerminalMutation.prepare(root / 'tasks', raw.task_id, lambda _s, _p, task: task)
            from src.rename.review_operation import current_provider_selection_review_operation
            task_path = root / 'tasks' / f'{raw.task_id}.json'
            task = json.loads(task_path.read_text('utf-8'))
            operation, context = current_provider_selection_review_operation(task)
            self.assertEqual('pending', context['lifecycle'])
            self.assertEqual(context['request_id'], intent.request_id)
            self.assertEqual(operation.fingerprint, intent.operation_fingerprint)
            for attack, changed in (
                    ('request', replace(intent, request_id='not-the-active-request')),
                    ('operation', replace(intent, operation_fingerprint='0' * 64)),
                    ('plan', replace(intent, plan_fingerprint='0' * 64)),
                    ('decision', replace(intent, decision_fingerprint='0' * 64)),
                    ('task', replace(intent, task_id='different-task'))):
                with self.subTest(attack=attack):
                    before = records(root)
                    with self.assertRaises((ValueError, RuntimeError)):
                        module.commit(service, changed, task_terminal=terminal)
                    self.assertEqual(before, records(root))
            other = replace(recognition, work_identity=replace(recognition.work_identity,
                primary_provider=replace(recognition.work_identity.primary_provider, provider_id='999999')))
            before = records(root)
            with self.assertRaises((ValueError, RuntimeError)):
                module.commit(service, module.prepare(service, other), task_terminal=terminal)
            self.assertEqual(before, records(root))
            pin = recognition.initial_movie_request
            for bad_pin in (None, replace(pin, task_id='wrong-task'),
                            replace(pin, expected_revision=pin.expected_revision + 1),
                            replace(pin, candidate_identity=('tmdb', 'movie', '999999'))):
                with self.subTest(pin=bad_pin):
                    with self.assertRaises((ValueError, RuntimeError)):
                        module.commit(service, module.prepare(service, replace(recognition, initial_movie_request=bad_pin)), task_terminal=terminal)
                    self.assertEqual(before, records(root))
            tv = replace(recognition, work_identity=replace(recognition.work_identity,
                primary_provider=replace(recognition.work_identity.primary_provider, entity_type='tv')))
            with self.assertRaises((ValueError, RuntimeError)):
                module.commit(service, module.prepare(service, tv), task_terminal=terminal)
            self.assertEqual(before, records(root))
        await self.fixture(root, initialize_before_selection=True, at_observation=check)

    async def test_unsupported_initial_intent_cannot_be_ignored_by_content_early_return(self):
        root = self.root()
        def check(service, recognition, raw):
            module = self.handoff_module()
            intent = module.prepare(service, recognition)
            expected = self.build_selection(module, service, intent)
            state = service.decision_state_store.load(raw.task_id)
            from src.rename.projection_authority import ProjectionAuthorityEnvelope, inventory_fingerprint_for_plan
            from src.rename.content_choice import validate_commit
            from src.rename.content_selection import ContentSelection, DEFAULT_FIELDS
            ordinary = state.content_selection.to_dict()
            ordinary['policy'] = 'content-selection-v1'
            for field in DEFAULT_FIELDS:
                ordinary.pop(field)
            ordinary['origin_plan_fingerprint'] = PlanStore.fingerprint(raw)
            ordinary['parent_plan_fingerprint'] = PlanStore.fingerprint(raw)
            legacy = ContentSelection.from_mapping(ordinary)
            envelope = ProjectionAuthorityEnvelope(state, expected.decision, expected.draft,
                inventory_fingerprint_for_plan(expected.draft), initial_movie_selection=intent)
            for attack, bad in (
                ('old_missing', replace(envelope, previous_decision=None)),
                ('choice_missing', replace(envelope, decision=replace(expected.decision, content_selection=None))),
                ('old_v1', replace(envelope, previous_decision=replace(state, content_selection=legacy))),
                ('new_v1', replace(envelope, decision=replace(expected.decision, content_selection=legacy))),
                ('mixed_movie', replace(envelope, movie_change=object())),
                ('mixed_archive', replace(envelope, media_archive=object()))):
                with self.subTest(attack=attack):
                    before = records(root)
                    with self.assertRaisesRegex(ValueError, '公共命令|首次作品交接不能'):
                        validate_commit(service, bad)
                    self.assertEqual(before, records(root))
        await self.fixture(root, initialize_before_selection=True, at_observation=check)

    async def test_ordinary_legacy_movie_selection_still_succeeds(self):
        from tests.test_content_policy_contract import historical_admission
        root = self.root()
        roots = TargetRootSnapshot.capture(dict(bangumi_path=root / 'TV', movie_path=root / 'Movies',
                                               anime_path=root / 'Anime', anime_movie_path=root / 'AnimeMovies'))
        with historical_admission(), patch('src.rename.target_routing.snapshot_from_config_manager', return_value=roots):
            service, result, _, _ = await self.fixture(root, initialize_before_selection=False)
        validate_selection_for_plan(result.task)
        self.assertTrue(result.state.confirmed_work)
        self.assertIsNone(result.state.content_selection)

    @staticmethod
    def replace_request(task_path, **changes):
        from src.rename.review_operation import set_review_operation, transition_provider_selection_review_operation
        task = json.loads(task_path.read_text('utf-8'))
        set_review_operation(task, transition_provider_selection_review_operation(task, changes))
        task_path.write_text(json.dumps(task, ensure_ascii=False), encoding='utf-8')

    async def test_handoff_rejects_later_real_revision_operation_and_source_scope_changes(self):
        for mutation in ('revision', 'operation', 'source'):
            with self.subTest(mutation=mutation):
                root = self.root()
                def check(service, recognition, raw):
                    module = self.handoff_module()
                    intent = module.prepare(service, recognition)
                    plan = service.plan_store.load(raw.task_id)
                    state = service.decision_state_store.load(raw.task_id)
                    terminal = TaskTerminalMutation.prepare(root / 'tasks', raw.task_id, lambda _s, _p, task: task)
                    if mutation == 'revision':
                        DecisionCommandService(service.decision_state_store, service.plan_store).update(
                            raw.task_id, expected_revision=state.revision, changes={}, planner=lambda _: plan,
                            task_terminal=terminal)
                    elif mutation == 'operation':
                        self.replace_request(root / 'tasks' / f'{raw.task_id}.json', state='failed')
                    else:
                        path = root / 'tasks' / f'{raw.task_id}.json'
                        task = json.loads(path.read_text('utf-8'))
                        task['task_source_scope']['reservation_revision'] += 1
                        path.write_text(json.dumps(task), encoding='utf-8')
                    before = records(root)
                    with self.assertRaises((ValueError, RuntimeError)):
                        module.commit(service, intent, task_terminal=terminal)
                    self.assertEqual(before, records(root))
                await self.fixture(root, initialize_before_selection=True, at_observation=check)

    async def test_handoff_terminal_race_preserves_new_request_and_never_reports_old_success(self):
        root = self.root()
        def check(service, recognition, raw):
            module = self.handoff_module()
            intent = module.prepare(service, recognition)
            terminal = TaskTerminalMutation.prepare(root / 'tasks', raw.task_id, lambda _s, _p, task: task)
            state = service.decision_state_store.load(raw.task_id)
            plan = service.plan_store.load(raw.task_id)
            changed = []
            task_path = root / 'tasks' / f'{raw.task_id}.json'
            def race(_transaction, phase):
                if phase == 'plan_written':
                    self.replace_request(task_path, request_id='newer-request-must-survive')
                    changed.append(task_path.read_bytes())
            with patch.object(ProjectionCommitService, '_checkpoint', race):
                with self.assertRaises((ValueError, RuntimeError)):
                    module.commit(service, intent, task_terminal=terminal)
            self.assertTrue(changed, 'must reach post-validation, pre-terminal CAS race')
            self.assertEqual(changed[-1], task_path.read_bytes())
            journal_path = root / 'projection_journal' / f'{raw.task_id}.json'
            journal = json.loads(journal_path.read_text('utf-8'))
            self.assertEqual(('plan_written', 'pending'), (journal['phase'], journal['status']))
            self.assertTrue(journal['rollback_ref'])
            self.assertNotEqual(state.revision, service.decision_state_store.load(raw.task_id).revision)
            conflict = records(root)
            fresh = ProjectionCommitService(service.decision_state_store, service.plan_store, task_root=root / 'tasks')
            for recovery in (lambda: fresh.recover(raw.task_id), fresh.recover_pending):
                with self.assertRaises((ValueError, RuntimeError)):
                    recovery()
                self.assertEqual(conflict, records(root))
            current = service.plan_store.load(raw.task_id)
            now = service.decision_state_store.load(raw.task_id)
            from src.rename.plan_review import entry_id
            from src.rename.plan_execution import PlanExecutionService
            with self.assertRaises((ValueError, RuntimeError)):
                service.approve_entries(raw.task_id, PlanStore.fingerprint(current), [entry_id(e) for e in current.entries])
            execution = PlanExecutionService(service.plan_store, service, task_root=root / 'tasks',
                record_root=root / 'records', catalog_root=root / 'work_catalog',
                decision_state_root=root / 'decisions', series_catalog_root=root / 'series_catalog')
            with self.assertRaisesRegex((ValueError, RuntimeError), 'content_policy_pending|来源尚未经过完整内容规划'):
                execution.execute(raw.task_id, PlanStore.fingerprint(current))
            self.assertEqual(conflict, records(root))
            with self.assertRaises((ValueError, RuntimeError)):
                DecisionCommandService(service.decision_state_store, service.plan_store).update(
                    raw.task_id, expected_revision=now.revision, changes={}, planner=lambda _: current,
                    task_terminal=TaskTerminalMutation.prepare(root / 'tasks', raw.task_id, lambda _s, _p, task: task))
            self.assertEqual(conflict, records(root), 'new no-op must not replace unresolved journal/backup')
        await self.fixture(root, initialize_before_selection=True, at_observation=check)
        from src.task_queue import TaskQueueManager
        manager = TaskQueueManager(task_root=root / 'tasks', plan_root=root / 'plans',
            decision_state_root=root / 'decisions')
        before = records(root)
        with self.assertRaisesRegex((ValueError, RuntimeError), 'projection.*recovery|恢复'):
            await manager.start()
        self.assertFalse(manager._started)
        self.assertIsNone(manager._worker_task)
        self.assertEqual(before, records(root))

    async def test_old_resolver_cannot_claim_new_same_provider_request(self):
        root = self.root()
        def check(service, recognition, raw):
            module = self.handoff_module()
            # Original resolver already ran; only request id changes, not the
            # Provider or expected Decision revision. Near-commit repinning is wrong.
            task_path = root / 'tasks' / f'{raw.task_id}.json'
            self.replace_request(task_path, request_id='new-same-provider-request')
            terminal = TaskTerminalMutation.prepare(root / 'tasks', raw.task_id, lambda _s, _p, task: task)
            before = records(root)
            with self.assertRaises((ValueError, RuntimeError)):
                module.commit(service, module.prepare(service, recognition), task_terminal=terminal)
            self.assertEqual(before, records(root))
        await self.fixture(root, initialize_before_selection=True, at_observation=check)

    async def test_handoff_journal_failure_preserves_pending_and_fresh_recovery(self):
        for exception, phase in ((RuntimeError, 'prepared'), (RuntimeError, 'plan_written'),
                                 (SystemExit, 'plan_written')):
            with self.subTest(exception=exception.__name__, phase=phase):
                root = self.root()
                def check(service, recognition, raw):
                    module = self.handoff_module()
                    intent = module.prepare(service, recognition)
                    terminal = TaskTerminalMutation.prepare(root / 'tasks', raw.task_id, lambda _s, _p, task: task)
                    before = {k: v for k, v in records(root).items() if not k.startswith('projection_journal/')}
                    reached = []
                    def fail(_transaction, window):
                        if window == phase:
                            reached.append(window)
                            raise exception('isolated handoff interruption')
                    with patch.object(ProjectionCommitService, '_checkpoint', fail):
                        with self.assertRaises(exception):
                            module.commit(service, intent, task_terminal=terminal)
                    self.assertTrue(reached)
                    if exception is SystemExit:
                        fresh = ProjectionCommitService(service.decision_state_store, service.plan_store, task_root=root / 'tasks')
                        self.assertEqual({raw.task_id: 'failed_closed'}, fresh.recover_pending())
                    after = records(root)
                    self.assertEqual(before, {k: after.get(k) for k in before})
                    self.assertFalse(service.decision_state_store.load(raw.task_id).confirmed_work)
                await self.fixture(root, initialize_before_selection=True, at_observation=check)
