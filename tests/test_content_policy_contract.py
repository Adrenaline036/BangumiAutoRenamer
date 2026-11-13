"""New policy admission and frozen inheritance, using disposable real stores.

No Provider transport, application startup, or real media. Historical v1 setup
uses the existing explicit reserve fixture, never a global policy-disable flag.
"""
from tests.source_collection_fixture import collected_files, collected_identity
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.task_queue import TaskJob, TaskQueueManager
from src.rename.plan_execution import PlanExecutionError
from src.rename.task_batch_scope import (
    BatchTaskSourceRequest, TaskSourceScope, TaskBatchScopeManifestStore,
)
from src.rename.decision_state import DecisionState, DecisionStateStore, DecisionCommandService
from src.rename.projection_authority import (
    commit_recognition_projection, UnresolvedRecognitionInput,
)
from src.rename.plan_store import PlanStore
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_verifier import PlanVerifier
from src.rename.inventory_plan import build_inventory_review_draft
from src.rename.movie_assets import inventory_movie_assets
from tests.projection_test_support import reserve_active_batch_fixture
from tests.test_content_choice_contract import records
from tests.source_collection_fixture import admit_existing, admit_task


POLICY = 'safe-watchable-content-v1'


@contextmanager
def historical_admission():
    """Construct explicit historical records for old completion controls only.

    Both reserve/retire and subsequent admission run normally; this is not a
    mocked policy result or a global product mode. Never used for new-task tests.
    """
    original = TaskQueueManager.enqueue_batch
    async def enqueue(manager, *, batch_id, task_requests, jobs):
        for request in task_requests:
            if not manager._read(request.task_id):
                old = reserve_active_batch_fixture(manager.batch_scope_store,
                    batch_id='historical-' + request.task_id, task_requests=(request,))
                scope = old.task_source_scopes[0]
                manager._write(request.task_id, dict(uuid=request.task_id,
                    path=request.input_scope[0], workflow_status='need_confirm',
                    batch_id=old.batch_id, task_source_scope=scope.to_dict()))
                manager.batch_scope_store.retire_task(old.batch_id, task_id=request.task_id,
                    expected_lineage_id=scope.reservation_lineage_id)
        return await original(manager, batch_id=batch_id, task_requests=task_requests, jobs=jobs)
    with patch.object(TaskQueueManager, 'enqueue_batch', enqueue):
        yield


class ContentPolicyContracts(unittest.IsolatedAsyncioTestCase):
    def root(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def sources(self, root):
        source = root / 'source'
        source.mkdir()
        for name in ('Movie.mkv', 'Movie.sc.ass', 'Other.mkv'):
            (source / name).write_bytes(b'isolated policy fixture')
        return source

    def manager(self, root):
        async def observed_job(_job):
            # Transport ends at admission; no real renamer or Provider.
            manager._update(_job.task_id, workflow_status='need_confirm')
            return None
        manager = TaskQueueManager(task_root=root / 'task', plan_root=root / 'plans',
            decision_state_root=root / 'decision_state', batch_scope_root=root / 'task_batch_scope',
            recovery_abandon_audit_root=root / 'abandon', executor=observed_job)
        # Modern partition preparation reads the same four-root snapshot as
        # the application.  Bind it to disposable roots here rather than
        # falling through to the machine configuration in this fixture.
        from src.rename.initial_movie_completion import RootSnapshotSource
        from src.rename.target_routing import TargetRootSnapshot, ROOT_KEYS
        roots = TargetRootSnapshot.capture({key: root / 'isolated-roots' / key for key in ROOT_KEYS})
        manager.root_snapshot_source = RootSnapshotSource(lambda: roots)
        self.addAsyncCleanup(manager.stop)
        return manager

    def request(self, source, task_id='policy-task'):
        return BatchTaskSourceRequest(task_id, (str(source),), 'parent_directory',
            tuple(collected_identity(p)
                  for p in collected_files(source)), 'movie')

    async def admission(self, root, *, mode='direct'):
        source = self.sources(root)
        manager = self.manager(root)
        if mode == 'single':
            task_id = await admit_task(manager, source, task_id='policy-task', is_anime=True,
                                            is_movie=True, preview_only=False)
        elif mode == 'batch':
            args = manager.prepare_input_batch((source,), is_anime=True, is_movie=True,
                 source='ui', preview_only=False)
            task_id, = await manager.enqueue_batch(**args)
        else:
            request = self.request(source)
            # Omitting the new field, even with a legacy-looking label, must
            # not create a fresh legacy task through the public direct route.
            task_id, = await manager.enqueue_batch(batch_id='policy-batch', task_requests=(request,),
                jobs=(TaskJob(request.task_id, source, is_anime=True, is_movie=True,
                              source='legacy_replan' if mode == 'legacy_label' else 'ui'),))
        await asyncio.wait_for(manager._queue.join(), 5)
        task = json.loads((root / 'task' / f'{task_id}.json').read_text('utf-8'))
        return manager, task

    def assert_policy(self, value):
        self.assertEqual(POLICY, value.get('content_policy'), value)

    async def pending(self, root, *, workspace=False, disconnect=None):
        manager, task = await self.admission(root)
        self.assert_policy(task['task_source_scope'])
        task.update(workflow_status='need_confirm', preview_only=False,
            movie_inventory=inventory_movie_assets(Path(task['path'])).to_dict(),
            draft_target_root=str(root / 'library'), provider_identity=None)
        manager._write(task['uuid'], task)
        draft = build_inventory_review_draft(task, dry_run=False)
        states = DecisionStateStore(root / 'decision_state')
        plans = PlanStore(root / 'plans')
        if workspace:
            from src.pages import plan_review_page as page
            from src.pages.initial_workspace import create_initial_workspace
            from src.rename.target_routing import TargetRootSnapshot, ROOT_KEYS
            roots = TargetRootSnapshot.capture({key: root / 'isolated-roots' / key for key in ROOT_KEYS})
            with patch.multiple(page, PLAN_PATH=plans.root, DECISION_STATE_PATH=states.root,
                    TASK_PATH=root / 'task', task_queue=manager,
                    get_task=manager._read, is_dry_run_enabled=lambda: False), \
                    patch('src.rename.target_routing.snapshot_from_config_manager', return_value=roots):
                if disconnect:
                    with patch('src.rename.content_policy.validate_commit', return_value=None):
                        before = records(root)
                        with self.assertRaisesRegex((ValueError, RuntimeError, TypeError), '策略|policy'):
                            await create_initial_workspace(task['uuid'], task)
                        self.assertEqual(before, records(root))
                        return
                state, plan = await create_initial_workspace(task['uuid'], task)
        else:
            state, plan = commit_recognition_projection(states, plans,
                UnresolvedRecognitionInput(task_id=task['uuid']), draft)
        service = PlanReviewService(plans, root / 'reviews', root / 'work_catalog',
            decision_state_root=states.root, task_root=root / 'task')
        return manager, service, state, plan

    async def test_actual_initial_workspace_pending_and_disconnected_validator_rejects(self):
        for disconnect in (False, True):
            with self.subTest(disconnect=disconnect):
                result = await self.pending(self.root(), workspace=True, disconnect=disconnect)
                if not disconnect:
                    manager, service, state, plan = result
                    self.assert_policy(state.to_dict())
                    self.assert_policy(plan.metadata)
                    self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(plan).issues})

    async def test_safe_retired_replan_cannot_reset_policy(self):
        root = self.root()
        manager, task = await self.admission(root)
        self.assert_policy(task['task_source_scope'])
        scope = TaskSourceScope.from_mapping(task['task_source_scope'])
        manager.batch_scope_store.retire_task(task['batch_id'], task_id=task['uuid'],
            expected_lineage_id=scope.reservation_lineage_id)
        source = Path(task['path'])
        request = self.request(source)
        await admit_existing(manager, TaskJob(task['uuid'], source, is_anime=True, is_movie=True),
            source_identities=request.source_identities, input_scope=request.input_scope, media_domain='movie')
        await asyncio.wait_for(manager._queue.join(), 5)
        self.assert_policy(manager._read(task['uuid'])['task_source_scope'])

    async def test_bare_decision_create_cannot_bypass_new_scope_birth(self):
        root = self.root()
        manager, task = await self.admission(root)
        self.assert_policy(task['task_source_scope'])
        before = records(root)
        with self.assertRaisesRegex((RuntimeError, ValueError), '策略|policy'):
            manager.decision_state_store.create(task['uuid'])
        self.assertEqual(before, records(root))

    async def test_public_preview_then_real_single_or_direct_batch_gets_safe_policy(self):
        for direct in (False, True):
            with self.subTest(direct=direct):
                root = self.root()
                source = self.sources(root)
                manager = self.manager(root)
                task_id = 'preview-to-real'
                await admit_task(manager, source, task_id=task_id, is_anime=True,
                                      is_movie=True, preview_only=True)
                preview = manager._read(task_id)
                self.assertTrue(preview['preview_only'])
                self.assertIsNone(preview.get('task_source_scope'))
                if direct:
                    await manager.enqueue_batch(batch_id='preview-to-real-batch',
                        task_requests=(self.request(source, task_id),),
                        jobs=(TaskJob(task_id, source, is_anime=True, is_movie=True, source='legacy_replan'),))
                else:
                    await admit_task(manager, source, task_id=task_id, is_anime=True,
                                          is_movie=True, preview_only=False)
                await asyncio.wait_for(manager._queue.join(), 5)
                self.assertFalse(manager._read(task_id)['preview_only'])
                self.assert_policy(manager._read(task_id)['task_source_scope'])

    async def test_fresh_single_batch_direct_and_legacy_label_get_safe_policy(self):
        for mode in ('single', 'batch', 'direct', 'legacy_label'):
            with self.subTest(mode=mode):
                root = self.root()
                manager, task = await self.admission(root, mode=mode)
                raw = task['task_source_scope']
                self.assert_policy(raw)
                self.assertEqual(2, raw['schema_version'])
                scope = TaskSourceScope.from_mapping(raw)
                self.assertEqual(raw, scope.to_dict())
                manager.batch_scope_store.require_active_scope(scope)
                manifest = manager.batch_scope_store.load(task['batch_id'])
                self.assert_policy(manifest.requests[0].to_dict())

    async def test_new_scope_and_request_cannot_lose_or_smuggle_policy(self):
        root = self.root()
        manager, task = await self.admission(root)
        self.assert_policy(task['task_source_scope'])
        scope = TaskSourceScope.from_mapping(task['task_source_scope'])
        request = manager.batch_scope_store.load(task['batch_id']).requests[0]
        for original, parser in ((scope.to_dict(), TaskSourceScope.from_mapping),
                                  (request.to_dict(), BatchTaskSourceRequest.from_mapping)):
            for attack in ('missing', 'unknown', 'bool', 'legacy_schema'):
                with self.subTest(kind=parser.__qualname__, attack=attack):
                    value = dict(original)
                    if attack == 'missing':
                        value.pop('content_policy')
                    elif attack == 'legacy_schema':
                        value['schema_version'] = 1
                    else:
                        value['content_policy'] = True if attack == 'bool' else 'future-unknown'
                    with self.assertRaises((ValueError, RuntimeError, TypeError)):
                        parser(value)
        # A complete downgrade, not just a malformed schema2, must fail against
        # the actual already-active manifest, without replacing any records.
        downgraded = dict(scope.to_dict())
        downgraded.pop('content_policy')
        downgraded['schema_version'] = 1
        before = records(root)
        with self.assertRaises((ValueError, RuntimeError)):
            manager.batch_scope_store.require_active_scope(TaskSourceScope.from_mapping(downgraded))
        self.assertEqual(before, records(root))

    def test_legacy_request_scope_and_decision_roundtrip_does_not_add_none_fields(self):
        root = self.root()
        request = self.request(self.sources(root), 'historical')
        value = request.to_dict()
        self.assertNotIn('content_policy', value)
        self.assertNotIn('schema_version', value)
        self.assertEqual(value, BatchTaskSourceRequest.from_mapping(value).to_dict())
        store = TaskBatchScopeManifestStore(root / 'task_batch_scope')
        manifest = reserve_active_batch_fixture(store, batch_id='historical', task_requests=(request,))
        scope = manifest.task_source_scopes[0]
        self.assertEqual(1, scope.schema_version)
        self.assertNotIn('content_policy', scope.to_dict())
        self.assertEqual(scope.to_dict(), TaskSourceScope.from_mapping(scope.to_dict()).to_dict())
        state = DecisionState(task_id='historical')
        self.assertNotIn('content_policy', state.to_dict())
        restored = DecisionState.from_dict(state.to_dict())
        self.assertEqual(state.to_dict(), restored.to_dict())

    async def test_legacy_replan_requires_real_history_not_entrypoint_name(self):
        for historical in (False, True):
            with self.subTest(historical=historical):
                root = self.root()
                source = self.sources(root)
                manager = self.manager(root)
                request = self.request(source)
                if historical:
                    manifest = reserve_active_batch_fixture(manager.batch_scope_store,
                        batch_id='old-policy-batch', task_requests=(request,))
                    scope = manifest.task_source_scopes[0]
                    manager._write(request.task_id, dict(uuid=request.task_id, path=str(source),
                        workflow_status='need_confirm', batch_id=manifest.batch_id,
                        task_source_scope=scope.to_dict()))
                    # The old task's real reservation must be explicitly retired
                    # before its historical replan, as the existing protocol requires.
                    manager.batch_scope_store.retire_task(manifest.batch_id, task_id=request.task_id,
                        expected_lineage_id=scope.reservation_lineage_id)
                await admit_existing(manager, TaskJob(request.task_id, source,
                    is_anime=True, is_movie=True), source_identities=request.source_identities,
                    input_scope=request.input_scope, media_domain='movie')
                await asyncio.wait_for(manager._queue.join(), 5)
                raw = manager._read(request.task_id)['task_source_scope']
                if historical:
                    self.assertNotIn('content_policy', raw)
                else:
                    self.assert_policy(raw)

    async def test_first_canonical_and_ordinary_revision_keep_policy_pending(self):
        root = self.root()
        manager, service, state, plan = await self.pending(root)
        self.assert_policy(state.to_dict())
        self.assert_policy(plan.metadata)
        self.assertIsNotNone(plan.projection_receipt)
        self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(plan).issues})
        commands = DecisionCommandService(service.decision_state_store, service.plan_store)
        revised, projected = commands.update(plan.task_id, expected_revision=state.revision,
            changes={}, planner=lambda _: plan)
        self.assert_policy(revised.to_dict())
        self.assert_policy(projected.metadata)
        self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(projected).issues})
        before = records(root)
        with self.assertRaisesRegex((ValueError, RuntimeError), '默认|策略|policy'):
            service.approve_entries(plan.task_id, PlanStore.fingerprint(projected),
                                    [entry_id(e) for e in projected.entries])
        self.assertEqual(before, records(root))

    async def test_legacy_content_migration_does_not_unlock_new_policy(self):
        root = self.root()
        manager, service, state, plan = await self.pending(root)
        from src.rename.rename_plan import WorkIdentity
        from src.rename.projection_authority import RecognitionDecisionInput
        from tests.test_isolated_sample_acceptance import routing_snapshot_for
        work = WorkIdentity.from_dict(dict(status='confirmed', domain='animation',
            domain_source='manual_library_selection', title='Policy Movie', year=2026,
            primary_provider=dict(status='confirmed', namespace='tmdb', entity_type='movie',
                provider_id='100', title='Policy Movie', year=2026, decision_source='manual_selection')))
        state, plan = commit_recognition_projection(service.decision_state_store, service.plan_store,
            RecognitionDecisionInput(plan.task_id, work), plan)
        target = root / 'library' / 'AnimeMovies'
        primary = root / 'source' / 'Movie.mkv'
        plan = service.complete_inventory_draft(plan.task_id, PlanStore.fingerprint(plan),
            group_id=plan.presentation_groups[0].group_id, group_kind='movie_continuous', target_root=target,
            reason='明确电影观看形式', work_domain='animation', primary_video_sources=[primary],
            movie_version_groups=[dict(sources=[str(primary)], label='完整电影')],
            excluded_movie_sources=[str(root / 'source' / 'Other.mkv')],
            root_snapshot=routing_snapshot_for(target, domain='animation', group_kind='movie_continuous'))
        preview = service.prepare_content_migration(plan.task_id, PlanStore.fingerprint(plan),
                                                    reason='旧内容迁移不代表完成默认初始化')
        revised = service.commit_content_change(preview)
        self.assertIsNotNone(service.decision_state_store.load(plan.task_id).content_selection)
        self.assert_policy(revised.metadata)
        self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(revised).issues})
        before = records(root)
        with self.assertRaisesRegex((ValueError, RuntimeError), '默认|策略|policy'):
            service.approve_entries(revised.task_id, PlanStore.fingerprint(revised),
                                    [entry_id(e) for e in revised.entries])
        self.assertEqual(before, records(root))

    async def test_canonical_rejects_cleared_policy_and_task_scope_downgrade(self):
        from src.rename.projection_authority import commit_projected_plan
        for attack in ('candidate', 'plan', 'task_scope'):
            with self.subTest(attack=attack):
                root = self.root()
                manager, service, state, plan = await self.pending(root)
                candidate, draft = state, plan
                if attack == 'candidate':
                    candidate = replace(state, content_policy=None)
                elif attack == 'plan':
                    metadata = dict(plan.metadata)
                    metadata.pop('content_policy')
                    draft = replace(plan, metadata=metadata)
                else:
                    task = manager._read(plan.task_id)
                    task['task_source_scope'].pop('content_policy')
                    task['task_source_scope']['schema_version'] = 1
                    manager._write(plan.task_id, task)
                before = records(root)
                with self.assertRaisesRegex((ValueError, RuntimeError), '默认|策略|policy|scope'):
                    commit_projected_plan(service.decision_state_store, service.plan_store,
                        previous=state, candidate=candidate, draft_plan=draft)
                self.assertEqual(before, records(root))

    async def test_pending_policy_rejects_retired_binary_partition_before_any_handoff(self):
        root = self.root()
        manager, service, state, plan = await self.pending(root)
        selected = next(e for e in plan.entries if e.source_path.name == 'Other.mkv')
        before = records(root)
        with patch.object(manager.source_partition_store, 'prepare',
                          side_effect=AssertionError('retired binary entry reached transaction prepare')) as prepare:
            with self.assertRaisesRegex(PlanExecutionError, '旧二分拆卡.*调整计划范围'):
                await manager.partition_task_sources(parent_task_id=plan.task_id,
                    expected_plan_fingerprint=PlanStore.fingerprint(plan),
                    selected_entry_ids=[entry_id(selected)], reason='明确独立作品')
        prepare.assert_not_called()
        self.assertEqual(before, records(root))
        self.assert_policy(manager._read(plan.task_id)['task_source_scope'])
        self.assert_policy(service.plan_store.load(plan.task_id).metadata)
        self.assert_policy(service.decision_state_store.load(plan.task_id).to_dict())
        self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(plan).issues})


    async def test_legacy_partition_and_forged_new_transition(self):
        for historical, attack in ((True, False), (False, True)):
            with self.subTest(historical=historical, attack=attack):
                root = self.root()
                if historical:
                    with historical_admission():
                        manager, task = await self.admission(root)
                    task.update(movie_inventory=inventory_movie_assets(Path(task['path'])).to_dict(),
                        draft_target_root=str(root / 'library'), provider_identity=None)
                    manager._write(task['uuid'], task)
                    state, plan = commit_recognition_projection(manager.decision_state_store, manager.plan_store,
                        UnresolvedRecognitionInput(task_id=task['uuid']),
                        build_inventory_review_draft(task, dry_run=False))
                else:
                    manager, service, state, plan = await self.pending(root)
                source = next(e for e in plan.entries if e.source_path.name == 'Other.mkv')
                kwargs = dict(parent_task_id=plan.task_id, expected_plan_fingerprint=PlanStore.fingerprint(plan),
                    selected_entry_ids=[entry_id(source)], reason='明确独立作品')
                if attack:
                    from src.rename.projection_authority import ProjectionCommitService
                    before = records(root)
                    with patch.object(ProjectionCommitService, 'commit_source_partition_revision',
                                      side_effect=AssertionError('retired binary writer reached commit')) as commit:
                        with self.assertRaisesRegex(PlanExecutionError, '旧二分拆卡.*调整计划范围'):
                            await manager.partition_task_sources(**kwargs)
                    commit.assert_not_called()
                    after = records(root)
                    self.assertEqual(before, after)
                else:
                    before = records(root)
                    with self.assertRaisesRegex(PlanExecutionError, '旧二分拆卡.*调整计划范围'):
                        await manager.partition_task_sources(**kwargs)
                    self.assertEqual(before, records(root))

    async def test_no_plan_reselection_and_recovery_writer_preserve_or_reject_policy(self):
        from src.rename.projection_authority import ProviderReselectionPendingInput
        from src.rename import recovery_transaction
        for mode in ('no_plan', 'reselection', 'recovery_writer'):
            for attack in ('none', 'clear', 'scope'):
                with self.subTest(mode=mode, attack=attack):
                    root = self.root()
                    manager, service, state, plan = await self.pending(root)
                    if attack == 'scope':
                        task = manager._read(plan.task_id)
                        task['task_source_scope'].pop('content_policy')
                        task['task_source_scope']['schema_version'] = 1
                        manager._write(plan.task_id, task)
                    before = records(root)
                    def submit():
                        if mode == 'no_plan':
                            changes = {'content_policy': None} if attack == 'clear' else {}
                            return DecisionCommandService(service.decision_state_store, service.plan_store).update(
                                plan.task_id, expected_revision=state.revision, changes=changes)
                        if mode == 'reselection':
                            transform = (lambda s: replace(s, content_policy=None)) if attack == 'clear' else None
                            return commit_recognition_projection(service.decision_state_store, service.plan_store,
                                ProviderReselectionPendingInput(plan.task_id, state.revision), plan,
                                state_transform=transform)
                        candidate = replace(state, recovery_state='healthy',
                            **({'content_policy': None} if attack == 'clear' else {}))
                        return recovery_transaction.commit_projected_plan(service.decision_state_store,
                            service.plan_store, previous=state, candidate=candidate, draft_plan=plan)
                    if attack != 'none':
                        with self.assertRaisesRegex((ValueError, RuntimeError), '策略|policy|scope'):
                            submit()
                        self.assertEqual(before, records(root))
                    else:
                        revised, projected = submit()
                        self.assert_policy(revised.to_dict())
                        if projected:
                            self.assert_policy(projected.metadata)

    async def test_public_media_execute_and_validate_reject_pending_not_missing_approval(self):
        root = self.root()
        manager, service, state, plan = await self.pending(root)
        execution = manager.execution_service
        for call in (execution.validate_approved, execution.execute):
            with self.subTest(entry=call.__name__):
                before = records(root)
                with self.assertRaisesRegex((ValueError, RuntimeError), '默认内容|content_policy_pending'):
                    call(plan.task_id, PlanStore.fingerprint(plan))
                self.assertEqual(before, records(root))


    async def test_pending_journal_four_windows_restore_and_retry_same_policy(self):
        for stage in ('prepared', 'decision_written', 'ownership_claimed', 'plan_written'):
            with self.subTest(stage=stage):
                root = self.root()
                manager, service, state, plan = await self.pending(root)
                commands = DecisionCommandService(service.decision_state_store, service.plan_store)
                before = {k: v for k, v in records(root).items()
                          if not k.startswith('projection_journal/')}
                reached = []
                def interrupted(_service, phase):
                    if phase == stage:
                        reached.append(phase)
                        raise RuntimeError('policy-journal:' + stage)
                with patch('src.rename.projection_authority.ProjectionCommitService._checkpoint', interrupted):
                    with self.assertRaisesRegex(RuntimeError, 'policy-journal:' + stage):
                        commands.update(plan.task_id, expected_revision=state.revision,
                            changes={}, planner=lambda _: plan)
                self.assertEqual([stage], reached)
                after = records(root)
                self.assertEqual(before, {k: after.get(k) for k in before})
                revised, projected = commands.update(plan.task_id, expected_revision=state.revision,
                    changes={}, planner=lambda _: plan)
                self.assert_policy(revised.to_dict())
                self.assert_policy(projected.metadata)
                self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(projected).issues})

    async def test_process_interruption_new_service_recovers_pending_and_retries(self):
        from src.rename.projection_authority import ProjectionCommitService
        root = self.root()
        manager, service, state, plan = await self.pending(root)
        commands = DecisionCommandService(service.decision_state_store, service.plan_store)
        before = {k: v for k, v in records(root).items() if not k.startswith('projection_journal/')}
        def exit_after_decision(_service, phase):
            if phase == 'decision_written':
                raise SystemExit('isolated process interruption')
        with patch.object(ProjectionCommitService, '_checkpoint', exit_after_decision):
            with self.assertRaises(SystemExit):
                commands.update(plan.task_id, expected_revision=state.revision, changes={}, planner=lambda _: plan)
        journal = root / 'projection_journal' / f'{plan.task_id}.json'
        self.assertEqual('pending', json.loads(journal.read_text('utf-8'))['status'])
        fresh = ProjectionCommitService(DecisionStateStore(service.decision_state_store.root),
            PlanStore(service.plan_store.root), task_root=root / 'task')
        self.assertEqual({plan.task_id: 'failed_closed'}, fresh.recover_pending())
        after = records(root)
        self.assertEqual(before, {k: after.get(k) for k in before})
        restored = fresh.state_store.load(plan.task_id)
        self.assert_policy(restored.to_dict())
        revised, projected = DecisionCommandService(fresh.state_store, fresh.plan_store).update(
            plan.task_id, expected_revision=restored.revision, changes={}, planner=lambda _: fresh.plan_store.load(plan.task_id))
        self.assert_policy(revised.to_dict())
        self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(projected).issues})
