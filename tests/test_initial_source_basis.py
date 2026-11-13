"""Incident-shaped complete source observations, never real media or Provider I/O."""
import asyncio
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_content_policy_contract as policy_fixture
from tests.test_content_choice_contract import records
from src.task_queue import TaskJob
from src.rename.decision_state import DecisionCommandService, DecisionState
from src.rename.task_batch_scope import TaskSourceScope
from src.rename.projection_authority import (
    commit_recognition_projection, commit_projected_plan, UnresolvedRecognitionInput,
    ProjectionCommitService, ProviderReselectionPendingInput,
)
from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from src.rename.plan_verifier import PlanVerifier
from tests.source_collection_fixture import admit_existing


class InitialSourceBasisContracts(unittest.IsolatedAsyncioTestCase):
    # Borrow helpers, not test classes/runner ownership or business predicates.
    root = policy_fixture.ContentPolicyContracts.root
    manager = policy_fixture.ContentPolicyContracts.manager
    request = policy_fixture.ContentPolicyContracts.request
    admission = policy_fixture.ContentPolicyContracts.admission
    pending = policy_fixture.ContentPolicyContracts.pending
    assert_policy = policy_fixture.ContentPolicyContracts.assert_policy

    def sources(self, root):
        source = root / 'source'
        for name in ('Movie.mkv', 'Movie.sc.ass', 'Other.mkv', 'Movie.mka',
                     'README.txt', 'Scans/deep/cover.jpg', 'OST/deep/track.flac'):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'isolated basis fixture')
        return source

    def basis(self, state, plan):
        raw = state.to_dict().get('initial_source_basis')
        self.assertIsNotNone(raw, 'canonical safe task lacks complete initial_source_basis')
        basis = state.initial_source_basis
        self.assertEqual(raw, basis.to_dict())
        self.assertEqual(raw, plan.to_dict()['metadata']['initial_source_basis'])
        self.assertEqual(basis.fingerprint, plan.projection_receipt.authority['initial_source_basis_fingerprint'])
        return basis

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('src.rename.source_basis'),
                             'missing canonical complete-source observation producer')
        from src.rename import source_basis
        return source_basis

    def revise(self, service, state, plan):
        return DecisionCommandService(service.decision_state_store, service.plan_store).update(
            plan.task_id, expected_revision=state.revision, changes={}, planner=lambda _: plan)

    async def test_complete_scope_not_draft_or_recursive_root_and_neutral_associations(self):
        root = self.root()
        manager, service, state, plan = await self.pending(root, workspace=True)
        basis = self.basis(state, plan)
        task = manager._read(plan.task_id)
        scope = TaskSourceScope.from_mapping(task['task_source_scope'])
        raw = basis.to_dict()
        self.assertEqual(scope.to_dict(), raw['task_source_scope'])
        self.assertEqual({i.source_path for i in scope.source_identities},
                         {r['source_path'] for r in raw['sources']})
        self.assertIn(str(root / 'source/Scans/deep/cover.jpg'),
                      {r['source_path'] for r in raw['sources']})
        self.assertTrue(all(r['hash_evidence'] == {'state': 'not_observed'} for r in raw['sources']))
        relations = {Path(r['source_path']).name: r for r in raw['associations']['relations']}
        self.assertEqual('unresolved', relations['Movie.sc.ass']['state'])
        self.assertEqual([str(root / 'source/Movie.mkv')], relations['Movie.mka']['owners'])
        self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(plan).issues})
        self.assertFalse(list(root.glob('**/*source_basis*.json')))

    async def test_first_commit_preserves_admitted_snapshot_until_explicit_source_refresh(self):
        original = policy_fixture.commit_recognition_projection
        def changed(states, plans, recognition, draft, **kwargs):
            (draft.source_root/'README.txt').write_bytes(b'changed after admission')
            return original(states,plans,recognition,draft,**kwargs)
        root=self.root()
        with patch.object(policy_fixture,'commit_recognition_projection',changed):
            manager,service,state,plan=await self.pending(root)
        expected=manager._read(plan.task_id)['task_source_scope']['source_identities']
        self.assertEqual({r['source_path']:r['size'] for r in expected},
                         {r['source_path']:r['size'] for r in self.basis(state,plan).to_dict()['sources']})

    async def test_actual_workspace_rejects_disconnected_producer_without_business_writes(self):
        module = self.module()
        for effect in ('none', 'exception'):
            with self.subTest(effect=effect):
                root = self.root()
                snapshots = []
                def disconnected(*args, **kwargs):
                    snapshots.append(records(root))
                    if effect == 'exception':
                        raise ValueError('basis producer disconnected')
                    return None
                with patch.object(module, 'project_source_basis',
                                  side_effect=disconnected) as producer:
                    with self.assertRaisesRegex((ValueError, RuntimeError, TypeError), 'basis|来源'):
                        await self.pending(root, workspace=True)
                    self.assertTrue(producer.called)
                    self.assertEqual(snapshots[-1], records(root))

    async def test_ordinary_revision_does_not_reobserve_and_metadata_cannot_lie(self):
        root = self.root()
        manager, service, state, plan = await self.pending(root)
        basis = self.basis(state, plan)
        module = self.module()
        with patch.object(module, 'project_source_basis', side_effect=AssertionError('media reobserved')) as observer:
            revised, projected = self.revise(service, state, plan)
            self.assertEqual(basis, self.basis(revised, projected))
            observer.assert_not_called()
        before = records(root)
        forged = replace(projected, metadata={**projected.metadata, 'initial_source_basis': {}})
        with self.assertRaisesRegex((ValueError, RuntimeError), 'basis|来源'):
            self.revise(service, revised, forged)
        self.assertEqual(before, records(root))

    async def test_existing_basis_same_source_replan_preserves_original_observation(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                root = self.root()
                manager, service, state, plan = await self.pending(root)
                basis = self.basis(state, plan)
                task = manager._read(plan.task_id)
                scope = TaskSourceScope.from_mapping(task['task_source_scope'])
                manager.batch_scope_store.retire_task(task['batch_id'], task_id=plan.task_id,
                    expected_lineage_id=scope.reservation_lineage_id)
                if changed:
                    (root / 'source/README.txt').write_bytes(b'a different source identity')
                request = self.request(root / 'source', plan.task_id)
                await admit_existing(manager, TaskJob(plan.task_id, root / 'source', is_anime=True, is_movie=True),
                    source_identities=request.source_identities, input_scope=request.input_scope, media_domain='movie')
                await asyncio.wait_for(manager._queue.join(), 5)
                self.assertNotEqual(task['batch_id'], manager._read(plan.task_id)['batch_id'])
                before = records(root)
                with patch.object(self.module(), 'project_source_basis', side_effect=AssertionError('replan rescanned')):
                    if changed:
                        with self.assertRaisesRegex((ValueError, RuntimeError), 'basis|来源|scope'):
                            self.revise(service, state, plan)
                        self.assertEqual(before, records(root))
                    else:
                        revised, projected = self.revise(service, state, plan)
                        self.assertEqual(basis, self.basis(revised, projected))

    async def test_strict_payload_and_existing_basis_cannot_be_cleared_or_changed(self):
        root = self.root()
        manager, service, state, plan = await self.pending(root)
        basis = self.basis(state, plan)
        for attack in ('episode', 'target', 'role', 'hash', 'missing', 'duplicate', 'extra', 'kind'):
            with self.subTest(attack=attack):
                raw = basis.to_dict()
                if attack in ('episode', 'target', 'role'):
                    raw['sources'][0][attack] = 'invented-authority'
                elif attack == 'hash':
                    raw['sources'][0]['hash_evidence'] = {'state': 'verified', 'sha256': '0' * 64}
                elif attack == 'missing':
                    raw['sources'].pop()
                elif attack == 'duplicate':
                    raw['sources'].append(raw['sources'][0])
                elif attack == 'extra':
                    raw['sources'][0]['source_path'] = str(root / 'sibling/Other.mkv')
                else:
                    raw['sources'][0]['kind'] = 'video' if raw['sources'][0]['kind'] != 'video' else 'other'
                before = records(root)
                with self.assertRaises((ValueError, RuntimeError)):
                    candidate = replace(state, initial_source_basis=type(basis).from_mapping(raw))
                    commit_projected_plan(service.decision_state_store, service.plan_store,
                        previous=state, candidate=candidate, draft_plan=plan)
                self.assertEqual(before, records(root))
        before = records(root)
        with self.assertRaisesRegex((ValueError, RuntimeError), 'basis|来源'):
            commit_projected_plan(service.decision_state_store, service.plan_store, previous=state,
                candidate=replace(state, initial_source_basis=None), draft_plan=plan)
        self.assertEqual(before, records(root))

    async def test_no_plan_reselection_and_recovery_preserve_observation(self):
        from src.rename import recovery_transaction
        for mode in ('no_plan', 'reselection', 'recovery_writer'):
            with self.subTest(mode=mode):
                root = self.root()
                manager, service, state, plan = await self.pending(root)
                basis = self.basis(state, plan)
                if mode == 'no_plan':
                    revised, projected = DecisionCommandService(service.decision_state_store, service.plan_store).update(
                        plan.task_id, expected_revision=state.revision, changes={})
                elif mode == 'reselection':
                    revised, projected = commit_recognition_projection(service.decision_state_store, service.plan_store,
                        ProviderReselectionPendingInput(plan.task_id, state.revision), plan)
                else:
                    revised, projected = recovery_transaction.commit_projected_plan(service.decision_state_store,
                        service.plan_store, previous=state, candidate=replace(state, recovery_state='healthy'), draft_plan=plan)
                self.assertEqual(basis, revised.initial_source_basis)
                if projected:
                    self.basis(revised, projected)

    async def test_current_policy_partition_preserves_parent_basis_before_retired_writer_rejection(self):
        root = self.root()
        manager, service, state, plan = await self.pending(root)
        original = self.basis(state, plan)
        selected = next(e for e in plan.entries if e.source_path.name == 'Other.mkv')
        before = records(root)
        with self.assertRaisesRegex(RuntimeError, '旧二分拆卡.*预览并调整分组'):
            await manager.partition_task_sources(parent_task_id=plan.task_id,
                expected_plan_fingerprint=PlanStore.fingerprint(plan), selected_entry_ids=[entry_id(selected)],
                reason='明确独立作品')
        self.assertEqual(before, records(root))
        revised = service.decision_state_store.load(plan.task_id)
        projected = service.plan_store.load(plan.task_id)
        basis = self.basis(revised, projected)
        self.assertEqual(original, basis)
        self.assertEqual(manager._read(plan.task_id)['task_source_scope'], basis.to_dict()['task_source_scope'])

    async def test_process_interruption_restores_basis_then_allows_retry(self):
        root = self.root()
        manager, service, state, plan = await self.pending(root)
        basis = self.basis(state, plan)
        before = {k: v for k, v in records(root).items() if not k.startswith('projection_journal/')}
        def interrupted(_service, stage):
            if stage == 'decision_written':
                raise SystemExit('basis journal incident')
        with patch.object(ProjectionCommitService, '_checkpoint', interrupted):
            with self.assertRaises(SystemExit):
                self.revise(service, state, plan)
        fresh = ProjectionCommitService(service.decision_state_store, service.plan_store, task_root=root / 'task')
        self.assertEqual({plan.task_id: 'failed_closed'}, fresh.recover_pending())
        after = records(root)
        self.assertEqual(before, {k: after.get(k) for k in before})
        revised, projected = self.revise(service, fresh.state_store.load(plan.task_id), fresh.plan_store.load(plan.task_id))
        self.assertEqual(basis, self.basis(revised, projected))

    def test_legacy_roundtrip_does_not_install_basis(self):
        state = DecisionState(task_id='old-basis-free')
        self.assertNotIn('initial_source_basis', state.to_dict())
        self.assertEqual(state, DecisionState.from_dict(state.to_dict()))


    async def test_incomplete_first_draft_rejected_while_basis_observation_remains_complete(self):
        module = self.module()
        root = self.root()
        original = policy_fixture.commit_recognition_projection
        def reduced(states, plans, recognition, draft, **kwargs):
            # Added after admission: a root scan would wrongly absorb this file.
            (draft.source_root / 'not-reserved.txt').write_bytes(b'outside reserved set')
            video = next(e for e in draft.entries if e.source_path.name == 'Movie.mkv')
            sub = next(e for e in draft.entries if e.source_path.name == 'Movie.sc.ass')
            sub = replace(sub, subtitle=replace(sub.subtitle,
                associated_video_source_path=video.source_path))
            incomplete = replace(draft, entries=(video, sub), expected_sources=(video.source_path, sub.source_path))
            from types import SimpleNamespace
            observer = SimpleNamespace(plan_store=plans, task_root=root / 'task')
            with patch.object(Path, 'rglob', side_effect=AssertionError('basis root recursion')):
                basis = module.observe_admitted_basis(observer, draft.task_id)
            raw = basis.to_dict()
            self.assertNotIn(str(root / 'source/not-reserved.txt'), {r['source_path'] for r in raw['sources']})
            self.assertIn(str(root / 'source/README.txt'), {r['source_path'] for r in raw['sources']})
            self.assertEqual([], next(r for r in raw['associations']['relations']
                                     if r['source_path'].endswith('Movie.sc.ass'))['owners'])
            before = records(root)
            with self.assertRaisesRegex(ValueError, '完整|来源'):
                original(states, plans, recognition, incomplete, **kwargs)
            self.assertEqual(before, records(root))
            # Complete ordinary canonical input still succeeds. This explicitly
            # tightens new neutral inventory; it does not manufacture history.
            return original(states, plans, recognition, draft, **kwargs)
        with patch.object(policy_fixture, 'commit_recognition_projection', reduced):
            manager, service, state, plan = await self.pending(root)
        raw = self.basis(state, plan).to_dict()
        self.assertNotIn(str(root / 'source/not-reserved.txt'), {r['source_path'] for r in raw['sources']})
        self.assertIn(str(root / 'source/README.txt'), {r['source_path'] for r in raw['sources']})
        self.assertEqual([], next(r for r in raw['associations']['relations']
                                 if r['source_path'].endswith('Movie.sc.ass'))['owners'])

    async def test_first_candidate_cross_scope_basis_rebuilt_and_rejected(self):
        root = self.root()
        manager, service, state, plan = await self.pending(root)
        basis = self.basis(state, plan)
        another = self.root()
        original = policy_fixture.commit_recognition_projection
        def forged(states, plans, recognition, draft, **kwargs):
            before = records(another)
            with self.assertRaisesRegex((ValueError, RuntimeError), 'basis|来源|scope'):
                original(states, plans, recognition, draft,
                         state_transform=lambda candidate: replace(candidate, initial_source_basis=basis))
            self.assertEqual(before, records(another))
            raise RuntimeError('cross scope basis rejected')
        with patch.object(policy_fixture, 'commit_recognition_projection', forged):
            with self.assertRaisesRegex(RuntimeError, 'cross scope basis rejected'):
                await self.pending(another)

    async def test_missing_original_replan_history_is_not_equivalence_proof(self):
        root = self.root()
        manager, service, state, plan = await self.pending(root)
        basis = self.basis(state, plan)
        task = manager._read(plan.task_id)
        scope = TaskSourceScope.from_mapping(task['task_source_scope'])
        manager.batch_scope_store.retire_task(task['batch_id'], task_id=plan.task_id,
            expected_lineage_id=scope.reservation_lineage_id)
        request = self.request(root / 'source', plan.task_id)
        await admit_existing(manager, TaskJob(plan.task_id, root / 'source', is_anime=True, is_movie=True),
            source_identities=request.source_identities, input_scope=request.input_scope, media_domain='movie')
        await asyncio.wait_for(manager._queue.join(), 5)
        original = type(manager.batch_scope_store).load
        def absent(store, batch_id):
            if batch_id == task['batch_id']:
                raise ValueError('basis original history missing')
            return original(store, batch_id)
        before = records(root)
        with patch.object(type(manager.batch_scope_store), 'load', absent):
            with self.assertRaisesRegex((ValueError, RuntimeError), 'basis|来源|history'):
                self.revise(service, state, plan)
        self.assertEqual(before, records(root))

    async def test_direct_writer_cannot_introduce_clear_or_change_basis(self):
        root = self.root()
        manager, service, state, plan = await self.pending(root)
        basis = self.basis(state, plan)
        store = service.decision_state_store
        for mode in ('clear', 'new', 'no_plan'):
            with self.subTest(mode=mode):
                before = records(root)
                with self.assertRaises((ValueError, RuntimeError)):
                    if mode == 'clear':
                        store.save(replace(state, initial_source_basis=None), expected_revision=state.revision)
                    elif mode == 'new':
                        store.save(replace(state, task_id='unreserved-task'), expected_revision=-1)
                    else:
                        DecisionCommandService(store, service.plan_store).update(plan.task_id,
                            expected_revision=state.revision, changes={'initial_source_basis': None})
                self.assertEqual(before, records(root))

    async def test_runtime_failure_windows_restore_basis(self):
        for stage in ('prepared', 'decision_written', 'ownership_claimed', 'plan_written'):
            with self.subTest(stage=stage):
                root = self.root()
                manager, service, state, plan = await self.pending(root)
                self.basis(state, plan)
                before = {k: v for k, v in records(root).items() if not k.startswith('projection_journal/')}
                def fail(_service, current):
                    if current == stage:
                        raise RuntimeError('basis journal failure')
                with patch.object(ProjectionCommitService, '_checkpoint', fail):
                    with self.assertRaisesRegex(RuntimeError, 'basis journal failure'):
                        self.revise(service, state, plan)
                after = records(root)
                self.assertEqual(before, {k: after.get(k) for k in before})

    async def test_pending_policy_cannot_reach_retired_binary_child_publication(self):
        root = self.root()
        manager, service, state, plan = await self.pending(root)
        self.basis(state, plan)
        selected = next(e for e in plan.entries if e.source_path.name == 'Other.mkv')
        options = dict(parent_task_id=plan.task_id, expected_plan_fingerprint=PlanStore.fingerprint(plan),
                       selected_entry_ids=[entry_id(selected)], reason='明确独立作品')
        before = records(root)
        with patch.object(ProjectionCommitService, 'commit_source_partition_revision',
                          side_effect=AssertionError('retired binary writer reached commit')) as commit:
            with self.assertRaisesRegex(RuntimeError, '旧二分拆卡.*预览并调整分组'):
                await manager.partition_task_sources(**options)
        commit.assert_not_called()
        self.assertEqual(before, records(root))

    async def test_projection_keeps_admitted_facts_and_independent_font_source(self):
        module = self.module()
        root = self.root()
        manager, task = await self.admission(root)
        scope = TaskSourceScope.from_mapping(task['task_source_scope'])
        original = module.observe_content_associations
        def mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            (root / 'source/README.txt').write_bytes(b'changed inside observation window')
            return result
        with patch.object(module, 'observe_content_associations', mutate):
            frozen = module.project_source_basis(task_id=task['uuid'], source_root=task['path'],
                batch_id=task['batch_id'], scope=scope)
        self.assertEqual(scope.to_dict(), frozen.to_dict()['task_source_scope'])
        another = self.root()
        original_sources = self.sources
        def with_font(directory):
            source = original_sources(directory)
            (source / 'Independent.ttf').write_bytes(b'independent unreadable font observation')
            return source
        with patch.object(self, 'sources', with_font):
            manager, service, state, plan = await self.pending(another)
        raw = self.basis(state, plan).to_dict()
        self.assertEqual('other', next(r['kind'] for r in raw['sources'] if r['source_path'].endswith('Independent.ttf')))
        self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(plan).issues})

    async def test_prepared_journal_proof_cannot_be_missing_stale_or_corrupt(self):
        module = self.module()
        original = module.validate_state_write
        for attack in ('task', 'phase', 'candidate', 'backup_hash', 'backup_path', 'old_attempt'):
            with self.subTest(attack=attack):
                root = self.root()
                reached = []
                def forged(store, current, candidate):
                    if candidate.initial_source_basis is None:
                        return original(store, current, candidate)
                    journal_path = root / 'projection_journal' / f'{candidate.task_id}.json'
                    data = journal_path.read_bytes()
                    raw = json.loads(data)
                    field, value = {'task': ('task_id', 'wrong-task'), 'phase': ('phase', 'completed'),
                        'candidate': ('current_decision_fingerprint', '0' * 64),
                        'backup_hash': ('rollback_sha256', '0' * 64),
                        'backup_path': ('rollback_ref', '../outside.json'),
                        'old_attempt': ('status', 'completed')}[attack]
                    raw[field] = value
                    journal_path.write_text(json.dumps(raw), encoding='utf-8')
                    reached.append(attack)
                    try:
                        return original(store, current, candidate)
                    finally:
                        journal_path.write_bytes(data)
                with patch.object(module, 'validate_state_write', forged):
                    with self.assertRaisesRegex((ValueError, RuntimeError), 'basis|来源'):
                        await self.pending(root)
                self.assertEqual([attack], reached)
                self.assertFalse((root / 'decision_state/policy-task.json').exists())
                self.assertFalse((root / 'plans/policy-task.json').exists())

    async def test_actual_pre_basis_safe_snapshot_needs_full_canonical_not_plan_only(self):
        from src.rename.projection_authority import persist_plan_revision
        module = self.module()
        root = self.root()
        # Explicit pre-B1 historical fixture only: execute the old S4A producer
        # before this new observation step; no production legacy bypass exists.
        with patch.object(module, 'bind_initial_source_basis', side_effect=lambda _s, envelope, **_: envelope):
            manager, service, state, plan = await self.pending(root)
        self.assertIsNone(state.initial_source_basis)
        before = records(root)
        with self.assertRaisesRegex((ValueError, RuntimeError), '来源|basis'):
            persist_plan_revision(service.decision_state_store, service.plan_store, plan)
        self.assertEqual(before, records(root))
        revised, projected = self.revise(service, state, plan)
        self.basis(revised, projected)
        self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(projected).issues})

    async def test_existing_basis_cannot_move_plan_source_root(self):
        root = self.root()
        manager, service, state, plan = await self.pending(root)
        self.basis(state, plan)
        before = records(root)
        with self.assertRaisesRegex((ValueError, RuntimeError), 'basis|来源|source'):
            self.revise(service, state, replace(plan, source_root=root))
        self.assertEqual(before, records(root))

    async def test_first_task_and_draft_root_cannot_jointly_drift_from_admission(self):
        root = self.root()
        original = policy_fixture.commit_recognition_projection
        module = self.module()
        def forged(states, plans, recognition, draft, **kwargs):
            path = root / 'task' / f'{draft.task_id}.json'
            task = json.loads(path.read_text('utf-8'))
            task['path'] = str(root)
            path.write_text(json.dumps(task), encoding='utf-8')
            before = records(root)
            with patch.object(module, 'project_source_basis', wraps=module.project_source_basis) as observer:
                with self.assertRaisesRegex((ValueError, RuntimeError), 'basis|来源|root'):
                    original(states, plans, recognition, replace(draft, source_root=root), **kwargs)
                observer.assert_not_called()
            self.assertEqual(before, records(root))
            raise RuntimeError('admission root drift rejected')
        with patch.object(policy_fixture, 'commit_recognition_projection', forged):
            with self.assertRaisesRegex(RuntimeError, 'admission root drift rejected'):
                await self.pending(root)
