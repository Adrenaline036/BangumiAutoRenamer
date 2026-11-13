"""Full default activation gate; actual queue/services, disposable sources only.

Provider transport is frozen. No default helper injection in ProducerContracts;
separate readiness fixtures deliberately test only the downstream consumer.
"""
import asyncio
from contextlib import asynccontextmanager, AsyncExitStack, ExitStack, nullcontext
from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.rename import content_selection
from src.rename.content_policy import require_media_ready
from src.rename.decision_state import DecisionStateStore
from src.rename.plan_store import PlanStore
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_verifier import PlanVerifier
from src.rename.projection_authority import ProjectionCommitService
from src.rename.target_routing import TargetRootSnapshot
from src.task_queue import TaskQueueManager
from tests import test_r1_real_media_production_red_contract as transport
from tests import _media_flow_helpers as containers
from tests.test_content_choice_contract import change
import src.pages.plan_review_page as page
import src.rename.process as process
import src.utils.utils as utilities
from tests.source_collection_fixture import admit_task


class DefaultActivationProducerContracts(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def flow(self, *, movie=False, automatic=True, ambiguous=False, hook=None, admission_only=False,
                   is_anime=False, configure_worker=None, configure_source=None, requested_season=None, admit=None,
                   queue_timeout=12, public_hook=None):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            async_cleanup = AsyncExitStack()
            root = Path(directory)
            title = 'Ordinary Movie' if movie else 'Ordinary Show'
            source = root / 'sources' / title
            source.mkdir(parents=True)
            sample, _ = containers.make_season(root / 'container-fixture')
            payload = next(sample.glob('*.mkv'))
            video_names = [f'{title} [1080p].mkv'] if movie else [
                f'{title} S01E01 [1080p].mkv', f'{title} S01E02 [1080p].mkv']
            for name in video_names:
                shutil.copyfile(payload, source / name)
            (source / 'README.txt').write_text('disposable package instructions', encoding='utf-8')
            shutil.copyfile(payload, source / f'{title} [Trailer].mkv')
            if ambiguous:
                for name in ('Unknown.mkv', 'SPs/Unnumbered.mkv', 'CD/Unnumbered.mkv'):
                    target = source / name
                    target.parent.mkdir(exist_ok=True)
                    shutil.copyfile(payload, target)
                (source / 'Unrelated.srt').write_text('unmatched subtitle', encoding='utf-8')
            task_id = 'default-activation'
            if configure_source is not None:
                source = configure_source(root, source)
            async def admitted_only(job):
                # Separate first-link fixture: actual reservation/admission,
                # no recognition worker or workspace has ever run for this job.
                manager._update(job.task_id, workflow_status='need_confirm')
            manager = TaskQueueManager(task_root=root / 'tasks', plan_root=root / 'plans',
                decision_state_root=root / 'decisions', provider_link_catalog_root=root / 'provider_links',
                recovery_abandon_audit_root=root / 'abandon',
                executor=admitted_only if admission_only else None)
            worker = transport._configured_renamer(root)
            if configure_worker is not None:
                configure_worker(worker)
            snapshot = TargetRootSnapshot.capture({'bangumi_path': worker.BANGUMI_PATH,
                'movie_path': worker.MOVIE_PATH, 'anime_path': worker.ANIME_PATH,
                'anime_movie_path': worker.ANIME_MOVIE_PATH})
            from src.rename.initial_movie_completion import RootSnapshotSource
            manager.root_snapshot_source = RootSnapshotSource(lambda: snapshot)
            if not automatic:
                # Only transport changes: by-ID lookup remains the real frozen
                # provider adapter, while the first search genuinely has no hit.
                old_tv, old_movie = worker.search.get_tv_info, worker.search.get_movie_info
                worker.search.get_tv_info = lambda *_args: ('', None)
                worker.search.get_movie_info = lambda *_args: ('', None)
                worker.search.get_tv_info_by_id = lambda provider_id: old_tv('Ordinary Show')
                worker.search.get_movie_info_by_id = lambda provider_id: old_movie('Ordinary Movie')
            stack.enter_context(patch('src.task_queue.Rename', return_value=worker))
            stack.enter_context(patch.multiple(process, TASK_PATH=root / 'tasks', PLAN_PATH=root / 'plans',
                SERIES_CATALOG_PATH=root / 'series_catalog', RECORD_PATH=root / 'records'))
            stack.enter_context(patch.multiple(page, TASK_PATH=root / 'tasks', PLAN_PATH=root / 'plans',
                DECISION_STATE_PATH=root / 'decisions', task_queue=manager, get_task=manager._read,
                notify=Mock(), is_dry_run_enabled=lambda: False))
            stack.enter_context(patch.object(utilities, 'TASK_PATH', root / 'tasks'))
            stack.enter_context(patch('src.rename.target_routing.snapshot_from_config_manager', return_value=snapshot))
            commits, hook_errors = [], []
            original_commit = ProjectionCommitService.commit
            def commit(transaction, envelope, *, task_terminal=None):
                def forward(candidate=envelope):
                    return original_commit(transaction, candidate, task_terminal=task_terminal)
                try:
                    result = hook(transaction, envelope, task_terminal, forward) if hook is not None else forward()
                except AssertionError as error:
                    hook_errors.append(error)
                    raise
                commits.append((result[0], result[1]))
                return result
            stack.enter_context(patch.object(ProjectionCommitService, 'commit', commit))
            from src.rename import public_revision
            original_public = public_revision.commit
            def publish(service, command, *, task_terminal, persist=True):
                def forward(candidate=command):
                    return original_public(service, candidate, task_terminal=task_terminal, persist=persist)
                try:
                    return public_hook(service, command, task_terminal, forward) if public_hook else forward()
                except AssertionError as error:
                    hook_errors.append(error)
                    raise
            stack.enter_context(patch.object(public_revision, 'commit', publish))
            await manager.start()
            try:
                if admit is None:
                    await admit_task(manager, source, task_id=task_id, is_anime=is_anime, is_movie=movie,
                                          custom_name=title, custom_season_id=requested_season)
                else:
                    await admit(manager, source, task_id)
                await asyncio.wait_for(manager._queue.join(), queue_timeout)
                if hook_errors:
                    raise hook_errors[0]
                service = PlanReviewService(PlanStore(root / 'plans'), root / 'reviews', root / 'work_catalog',
                    decision_state_root=root / 'decisions', task_root=root / 'tasks',
                    record_root=root / 'records', provider_link_catalog_root=root / 'provider_links')
                from src.rename.initial_movie_completion import RootSnapshotSource
                service.root_snapshot_source = RootSnapshotSource(lambda: snapshot)
                context = SimpleNamespace(root=root, source=source, task_id=task_id, manager=manager,
                    service=service, store=service.plan_store, commits=commits, movie=movie,
                    enterContext=stack.enter_context, addCleanup=stack.callback,
                    addAsyncCleanup=async_cleanup.push_async_callback)
                yield context
            finally:
                try:
                    await async_cleanup.aclose()
                finally:
                    await manager.stop()

    async def open_page(self, flow):
        with patch.object(page, 'PlanReviewDialog', transport.R1RealQueueWorkerContracts._NoopReviewDialog), \
                patch.object(page, '_dialog_mount_context', return_value=nullcontext()):
            await page.plan_review_page(flow.task_id)

    def current(self, flow):
        task = flow.manager._read(flow.task_id)
        self.assertTrue(flow.store.path_for(flow.task_id).is_file(),
                        f'production did not publish source workspace: {task.get("error")}')
        return flow.store.load(flow.task_id), flow.service.decision_state_store.load(flow.task_id)

    def assert_default(self, flow):
        plan, state = self.current(flow)
        self.assertIsNotNone(state.content_selection, 'actual producer never initialized default content')
        self.assertEqual(content_selection.DEFAULT_POLICY, state.content_selection.to_dict()['policy'])
        content_selection.validate_plan_content(plan, state)
        self.assertEqual(state.initial_source_basis.to_dict(), plan.to_dict()['metadata']['initial_source_basis'])
        self.assertTrue({flow.source / 'README.txt', flow.source / (
            'Ordinary Movie [Trailer].mkv' if flow.movie else 'Ordinary Show [Trailer].mkv')}
            <= set(plan.ignored_sources), 'explicit non-watchable files must not require role decisions')
        return plan, state

    async def selected(self, flow):
        plan, state = self.current(flow)
        dialog = SimpleNamespace(plan=plan, task_decision=state, task_data=flow.manager._read(flow.task_id), close=Mock())
        movie = flow.movie
        candidate = dict(namespace='tmdb', entity_type='movie' if movie else 'tv',
            provider_id='8181' if movie else '4242', title='普通电影' if movie else '普通剧集',
            original_title='Ordinary Movie' if movie else 'Ordinary Show', year=2024 if movie else 2020,
            source_url='https://www.themoviedb.org/movie/8181' if movie else 'https://www.themoviedb.org/tv/4242',
            snapshot_at='2026-09-13T00:00:00Z')
        original_wait = TaskQueueManager.wait_for_selection_result
        async def bounded(manager, task_id, **kwargs):
            return await original_wait(manager, task_id, timeout=0.3)
        with patch.object(TaskQueueManager, 'wait_for_selection_result', bounded):
            await page.PlanReviewDialog._select_provider_candidate(dialog, candidate)
        await asyncio.wait_for(flow.manager._queue.join(), 12)
        return self.current(flow)

    async def automatic(self, *, movie):
        async with self.flow(movie=movie) as flow:
            # No page/manual selection/default initializer is called here.
            plan, state = self.assert_default(flow)
            self.assertTrue(state.confirmed_work, flow.manager._read(flow.task_id).get('error'))
            self.assertEqual('movie_versions' if movie else 'tv_episodes', state.layout_mode,
                             flow.manager._read(flow.task_id).get('error'))
            self.assertTrue(flow.commits, 'actual canonical publication observer not reached')
            first_state, first_plan = flow.commits[0]
            self.assertFalse(first_state.confirmed_work, 'old semantic writer ran before neutral source workspace')
            self.assertIsNotNone(first_state.initial_source_basis)
            self.assertNotEqual('initial_movie_selection', plan.revision_action,
                                'automatic recognition must not invent a manual selection receipt')
            require_media_ready(plan)
            self.assertEqual(set() if movie else set(),
                             {i.code for i in PlanVerifier().verify(plan).issues})
            before = PlanStore.fingerprint(plan)
            await self.open_page(flow)
            self.assertEqual(before, PlanStore.fingerprint(flow.store.load(flow.task_id)))

    async def test_actual_tv_queue_automatically_defaults_and_completes_without_selection(self):
        await self.automatic(movie=False)

    async def test_actual_movie_queue_automatically_defaults_and_completes_without_selection(self):
        await self.automatic(movie=True)

    async def test_actual_no_match_then_manual_selection_needs_no_default_helper(self):
        for movie in (False, True):
            with self.subTest(movie=movie):
                async with self.flow(movie=movie, automatic=False) as flow:
                    await self.open_page(flow)
                    plan, state = self.assert_default(flow)
                    self.assertFalse(state.confirmed_work)
                    basis = state.initial_source_basis
                    with self.assertRaises(ValueError):
                        require_media_ready(plan)
                    plan, state = await self.selected(flow)
                    self.assertTrue(state.confirmed_work, flow.manager._read(flow.task_id).get('error'))
                    self.assertEqual(basis, state.initial_source_basis)
                    self.assertIsNotNone(state.content_selection)

    async def test_actual_link_recovery_workspace_has_default_editable_unresolved_content(self):
        from src.pages.initial_workspace import create_initial_workspace
        async with self.flow(automatic=False, admission_only=True) as flow:
            self.assertFalse(flow.store.path_for(flow.task_id).exists(), 'control requires no earlier workspace')
            state, plan = await create_initial_workspace(flow.task_id, flow.manager._read(flow.task_id))
            actual, saved = self.assert_default(flow)
            self.assertEqual(plan, actual)
            self.assertEqual(state, saved)
            self.assertFalse(state.confirmed_work)

    async def test_existing_no_match_workspace_is_idempotent_when_opened_from_link_entry(self):
        from src.pages.initial_workspace import create_initial_workspace
        async with self.flow(automatic=False) as flow:
            plan, state = self.assert_default(flow)
            for _ in range(2):
                current_state, current_plan = await create_initial_workspace(flow.task_id, flow.manager._read(flow.task_id))
                self.assertEqual(state, current_state)
                self.assertEqual(plan, current_plan)

    async def test_unknown_sp_and_cd_video_stay_visible_not_silently_ready(self):
        async with self.flow(automatic=False, ambiguous=True) as flow:
            await self.open_page(flow)
            plan, _ = self.assert_default(flow)
            for path in ('Unknown.mkv', 'SPs/Unnumbered.mkv', 'CD/Unnumbered.mkv', 'Unrelated.srt'):
                self.assertIn(flow.source / path, {e.source_path for e in plan.entries})
                self.assertNotIn(flow.source / path, set(plan.ignored_sources))
            with self.assertRaises(ValueError):
                require_media_ready(plan)

    async def test_opening_again_cannot_reset_explicit_exclusion_or_default_revision(self):
        async with self.flow(automatic=False, ambiguous=True) as flow:
            await self.open_page(flow)
            plan, _ = self.assert_default(flow)
            updated = change(flow.service, plan, [flow.source / 'Unknown.mkv'])
            fingerprint = PlanStore.fingerprint(updated)
            revision = flow.service.decision_state_store.load(flow.task_id).revision
            for _ in range(2):
                await self.open_page(flow)
                current, state = self.current(flow)
                self.assertEqual(fingerprint, PlanStore.fingerprint(current))
                self.assertEqual(revision, state.revision)
                self.assertIn(flow.source / 'Unknown.mkv', current.ignored_sources)

    async def test_actual_auto_commit_requires_original_job_and_canonical_provider_proof(self):
        from tests.test_content_choice_contract import records
        from src.rename.projection_authority import validate_plan_projection_receipt
        reached = []
        def check(service, command, terminal, forward):
            reached.append(command.task_id)
            recognition = command.inputs.recognition
            proof = recognition.automatic_recognition
            self.assertIsNotNone(proof, 'actual automatic producer has no typed canonical provenance')
            origin = proof.origin
            root = service.plan_store.root.parent
            before = records(root)
            for label, altered in (
                ('missing', lambda: None),
                ('boolean', lambda: True),
                ('wrong-task', lambda: replace(proof, origin=replace(origin, task_id='another-task'))),
                ('later-job', lambda: replace(proof, origin=replace(origin, job_id='later-recognition-job'))),
                ('wrong-scope', lambda: replace(proof, origin=replace(origin, scope_fingerprint='0' * 64))),
                # 父Plan、Decision与revision是同一绑定，反例保持类型结构有效。
                ('wrong-original-plan', lambda: replace(proof, origin=replace(origin, plan_fingerprint='0' * 64,
                    decision_fingerprint='1' * 64, original_revision=0))),
                ('wrong-original-decision', lambda: replace(proof, origin=replace(origin, decision_fingerprint='0' * 64,
                    plan_fingerprint='1' * 64, original_revision=0))),
                ('different-season-intent', lambda: replace(proof, origin=replace(origin, requested_season=2))),
            ):
                with self.subTest(label=label):
                    candidate = replace(command, inputs=replace(command.inputs,
                        recognition=replace(recognition, automatic_recognition=altered())))
                    expected_function = 'current_provider_selection_review_operation' if label == 'missing' else 'validate_pending_recognition'
                    expected_message = ('current provider selection authority is unavailable' if label == 'missing'
                                        else '自动完成缺少原已保存观察，不能伪造人工选择')
                    try:
                        forward(candidate)
                    except ValueError as error:
                        frames, trace = [], error.__traceback__
                        while trace:
                            frames.append(trace.tb_frame.f_code.co_name)
                            trace = trace.tb_next
                        self.assertEqual(expected_message, str(error))
                        self.assertEqual(expected_function, frames[-1])
                        print('PROOF_REJECTION', json.dumps(dict(field=label, function=frames[-1],
                              message=str(error)), ensure_ascii=False))
                    else:
                        self.fail('altered original proof was accepted: ' + label)
                    self.assertEqual(before, records(root))
            return forward()
        async with self.flow(public_hook=check) as flow:
            self.assertEqual([flow.task_id], reached, 'actual public completion command not reached')
            plan, state = self.assert_default(flow)
            self.assertTrue(state.confirmed_work)
            self.assertEqual('initial_tv_completion', plan.revision_action)
            self.assertEqual(DecisionStateStore.fingerprint(state), plan.metadata['decision_state_fingerprint'])
            validate_plan_projection_receipt(plan)
            self.assertEqual('need_confirm', flow.manager._read(flow.task_id)['workflow_status'])
            self.assertFalse(flow.service.load_or_create(plan).approved_entry_ids)

    async def test_disconnected_shared_candidate_resolver_never_auto_confirms(self):
        from src.rename import provider_search
        with patch.object(provider_search, 'decide_provider_candidates', return_value=None) as resolver:
            async with self.flow() as flow:
                self.assertTrue(resolver.called, 'candidate resolver control not reached')
                state = flow.service.decision_state_store.load_optional(flow.task_id)
                self.assertFalse(state is not None and state.confirmed_work)
                self.assertNotEqual('completed', flow.manager._read(flow.task_id).get('workflow_status'))

    async def test_original_batch_failure_missing_and_multiple_candidates_cannot_publish(self):
        from src.rename.automatic_recognition import observation_draft
        from src.rename.content_associations import encode
        from src.rename.provider_search import fuse_provider_candidates
        from tests.test_content_choice_contract import records
        reached = []
        def check(transaction, envelope, terminal, forward):
            if envelope.draft_plan.revision_action != 'automatic_observation':
                return forward()
            proof = envelope.automatic_recognition
            before = records(transaction.plan_store.root.parent)
            current = transaction.plan_store.load(envelope.decision.task_id)
            for variant in ('missing-source', 'error', 'timeout', 'multiple'):
                with self.subTest(variant=variant):
                    batch = json.loads(proof.batch_json)
                    row = next(row for row in batch['results'] if row['source'] == 'tmdb_tv' and row['candidates'])
                    if variant == 'missing-source':
                        batch['results'].remove(row)
                        # Missing rows are rejected by the typed parser, before
                        # the canonical boundary; do not count as its evidence.
                        with self.assertRaises(ValueError):
                            replace(proof, batch_json=encode(batch))
                    else:
                        if variant == 'multiple':
                            row['candidates'].append({**row['candidates'][0], 'provider_id': '999999'})
                            batch['candidates'] = fuse_provider_candidates(proof.query,
                                (item for row in batch['results'] for item in row['candidates']),
                                year=proof.year, animation=proof.origin.is_anime)
                            self.assertEqual(2, sum(bool(c.get('match_eligible')) for c in batch['candidates']))
                        else:
                            row['status'] = variant
                        altered = replace(proof, batch_json=encode(batch))
                        draft = observation_draft(current, envelope.previous_decision, altered)
                        reached.append(variant)
                        with self.assertRaises((ValueError, RuntimeError)):
                            forward(replace(envelope, automatic_recognition=altered, draft_plan=draft))
                    self.assertEqual(before, records(transaction.plan_store.root.parent))
            return forward()
        async with self.flow(hook=check) as flow:
            self.assertEqual(['error', 'timeout', 'multiple'], reached)
            self.assertTrue(self.assert_default(flow)[1].confirmed_work)

    async def test_trimmed_policy_and_failed_provider_cannot_publish_automatic_observation(self):
        from src.rename.provider_search import ProviderSearchStatus
        observed, reached, saved = [], [], []
        def configure(worker):
            worker.search.get_tv_info = lambda *_a: ('', None)
            worker.bangumi_search.search_animation = lambda *_a, **_kw: []
            original = worker._provider_search_batch
            def trimmed(*args, **kwargs):
                batch = original(*args, **kwargs)
                failed = replace(batch, results=tuple(replace(row, status=ProviderSearchStatus.TIMEOUT)
                    if row.source == 'tmdb_tv' else row for row in batch.results))
                observed.append(failed)
                return replace(failed, policy=replace(failed.policy, expected_sources=('tmdb_movie',),
                    scheduled_sources=('tmdb_movie',)), results=tuple(row for row in failed.results
                        if row.source == 'tmdb_movie'))
            worker._provider_search_batch = trimmed
        def check(transaction, envelope, terminal, forward):
            if envelope.draft_plan.revision_action != 'automatic_observation':
                return forward()
            reached.append(True)
            plan_path = transaction.plan_store.path_for(envelope.decision.task_id)
            state_path = transaction.state_store.path_for(envelope.decision.task_id)
            before = (plan_path.read_bytes(), state_path.read_bytes())
            try:
                return forward()
            finally:
                saved.append(before == (plan_path.read_bytes(), state_path.read_bytes()))
        async with self.flow(movie=True, is_anime=True, configure_worker=configure, hook=check) as flow:
            self.assertEqual(1, len(observed))
            self.assertEqual({'bangumi', 'tmdb_tv', 'tmdb_movie'}, set(observed[0].policy.expected_sources))
            self.assertIn(ProviderSearchStatus.TIMEOUT, {row.status for row in observed[0].results})
            self.assertEqual([True], reached, 'altered legal batch never reached canonical observation')
            self.assertEqual([True], saved, 'trimming the frozen Provider scope published a new Plan/Decision')
            plan, state = self.assert_default(flow)
            self.assertFalse(state.confirmed_work)
            self.assertIsNone(plan.metadata.get('automatic_recognition'))

    async def test_full_frozen_animation_movie_scope_can_complete_without_manual_selection(self):
        def configure(worker):
            worker.search.get_tv_info = lambda *_a: ('', None)
            worker.bangumi_search.search_animation = lambda *_a, **_kw: []
        async with self.flow(movie=True, is_anime=True, configure_worker=configure) as flow:
            plan, state = self.assert_default(flow)
            self.assertTrue(state.confirmed_work, 'ordinary complete three-source animation movie control')
            self.assertEqual({'bangumi', 'tmdb_tv', 'tmdb_movie'},
                set(plan.metadata['automatic_search_policy']['expected_sources']))

    async def test_default_and_completion_journal_crash_preserve_prior_then_fresh_retry(self):
        import base64
        import hashlib
        from concurrent.futures import ThreadPoolExecutor
        from tests.test_content_choice_contract import records
        from src.rename.projection_authority import validate_plan_projection_receipt
        for stage in ('content_default', 'initial_tv_completion'):
            for phase in ('prepared', 'decision_written', 'ownership_claimed', 'plan_written', 'task_terminal_written'):
                with self.subTest(stage=stage, phase=phase):
                    reached = []
                    def interrupted(transaction, task_id, forward, command=None):
                        root = transaction.plan_store.root.parent
                        before = records(root)
                        parent = transaction.state_store.load_optional(task_id)
                        journal_name = f'projection_journal/{task_id}.json'
                        def checkpoint(_transaction, actual):
                            if actual == phase:
                                reached.append((stage, phase))
                                raise SystemExit('disposable default activation crash')
                        with patch.object(ProjectionCommitService, '_checkpoint', checkpoint):
                            with self.assertRaises(SystemExit):
                                forward()
                        pending = json.loads((root / journal_name).read_bytes())
                        self.assertEqual('pending', pending['status'])
                        self.assertEqual(phase, pending['phase'])
                        fresh = ProjectionCommitService(transaction.state_store, transaction.plan_store,
                                                        task_root=transaction.task_root)
                        # A different thread must acquire every public lock after the failed call unwinds.
                        def reacquire():
                            with (command.context.publication_lock(fresh, command) if command else nullcontext()):
                                with fresh._transaction_lock(task_id):
                                    return True
                        with ThreadPoolExecutor(max_workers=1) as executor:
                            self.assertTrue(executor.submit(reacquire).result(timeout=5))
                        self.assertEqual({task_id: 'failed_closed'}, fresh.recover_pending())
                        after = records(root)
                        self.assertEqual({k: v for k, v in before.items() if k != journal_name},
                                         {k: after.get(k) for k in before if k != journal_name})
                        for name in (f'plans/{task_id}.json', f'decisions/{task_id}.json', f'tasks/{task_id}.json'):
                            self.assertEqual(before.get(name), after.get(name))
                        journal = json.loads(after[journal_name])
                        self.assertEqual(2, journal['schema_version'])
                        self.assertEqual(task_id, journal['task_id'])
                        self.assertEqual('failed_closed', journal['status'])
                        self.assertEqual(phase, journal['phase'])
                        self.assertEqual(pending['attempt_id'], journal['attempt_id'])
                        self.assertEqual(['task', 'source'], journal['lock_order'])
                        self.assertEqual('task:' + task_id, journal['task_lock_key'])
                        rollback_name = f"projection_journal/backups/{task_id}.{journal['attempt_id']}.rollback.json"
                        self.assertEqual(rollback_name.removeprefix('projection_journal/'), journal['rollback_ref'])
                        raw = after[rollback_name]
                        self.assertEqual(hashlib.sha256(raw).hexdigest(), journal['rollback_sha256'])
                        rollback = json.loads(raw)
                        self.assertEqual(1, rollback['schema_version'])
                        self.assertEqual(task_id, rollback['task_id'])
                        for field, name in (('previous_plan', f'plans/{task_id}.json'),
                                            ('previous_decision', f'decisions/{task_id}.json')):
                            self.assertEqual(json.loads(before[name]) if name in before else None, rollback[field])
                        task_bytes = before.get(f'tasks/{task_id}.json')
                        self.assertEqual(task_bytes, base64.b64decode(rollback['previous_task_bytes_b64']))
                        self.assertEqual(hashlib.sha256(task_bytes).hexdigest(), rollback['previous_task_sha256'])
                        added = sorted(set(after) - set(before))
                        histories = []
                        for name in added:
                            if name in (journal_name, rollback_name):
                                continue
                            path = Path(name)
                            self.assertEqual(('plans', 'history', task_id), path.parts[:-1], name)
                            self.assertEqual('.json', path.suffix)
                            payload = json.loads(after[name])
                            self.assertEqual(task_id, payload['task_id'])
                            self.assertEqual(PlanStore.fingerprint_payload(payload), path.stem)
                            historical = fresh.plan_store.load_version(task_id, path.stem)
                            validate_plan_projection_receipt(historical)
                            current = json.loads(after[f'plans/{task_id}.json']) if f'plans/{task_id}.json' in after else None
                            self.assertNotEqual(path.stem, PlanStore.fingerprint_payload(current) if current else None)
                            histories.append(name)
                        result = forward()
                        self.assertEqual(0 if parent is None else parent.revision + 1, result[0].revision)
                        if parent is not None:
                            old, new = parent.content_selection.to_dict(), result[0].content_selection.to_dict()
                            for key in ('excluded_sources', 'material_origins', 'scope_fingerprint', 'identities'):
                                self.assertEqual(old[key], new[key])
                        print('RECOVERY_WINDOW', json.dumps(dict(stage=stage, phase=phase, locks_reacquired=True,
                              restored=True, retry_revision=result[0].revision, added=added, histories=histories)))
                        return result
                    def default_hook(transaction, envelope, terminal, forward):
                        if stage == 'content_default' and envelope.draft_plan.revision_action == stage and not reached:
                            return interrupted(transaction, envelope.decision.task_id, forward)
                        return forward()
                    def public_hook(service, command, terminal, forward):
                        if stage == 'initial_tv_completion' and not reached:
                            transaction = ProjectionCommitService(service.decision_state_store, service.plan_store,
                                                                  task_root=service.task_root)
                            return interrupted(transaction, command.task_id, forward, command)
                        return forward()
                    async with self.flow(hook=default_hook, public_hook=public_hook) as flow:
                        self.assertEqual([(stage, phase)], reached, 'producer never reached the intended journal stage')
                        plan, state = self.assert_default(flow)
                        self.assertTrue(state.confirmed_work)
                        self.assertEqual('initial_tv_completion', plan.revision_action)
                        self.assertEqual(DecisionStateStore.fingerprint(state), plan.metadata['decision_state_fingerprint'])
                        validate_plan_projection_receipt(plan)
                        self.assertEqual('need_confirm', flow.manager._read(flow.task_id)['workflow_status'])
                        self.assertFalse(flow.service.load_or_create(plan).approved_entry_ids)

    async def test_default_committed_completion_failed_resumes_frozen_job_in_new_queue_without_provider(self):
        reached = []
        def fail_once(service, command, terminal, forward):
            if not reached:
                reached.append(True)
                raise ValueError('isolated completion failure after default persisted')
            return forward()
        async with self.flow(public_hook=fail_once) as flow:
            self.assertEqual([True], reached, 'failure window after production default was never reached')
            plan, state = self.assert_default(flow)
            self.assertFalse(state.confirmed_work)

            frozen_proof = plan.metadata['automatic_recognition']
            old_choice = state.content_selection
            await flow.manager.stop()
            fresh = TaskQueueManager(task_root=flow.root / 'tasks', plan_root=flow.root / 'plans',
                decision_state_root=flow.root / 'decisions', provider_link_catalog_root=flow.root / 'provider_links',
                recovery_abandon_audit_root=flow.root / 'abandon')
            resume = getattr(fresh, 'resume_default_activation', None)
            self.assertTrue(callable(resume), 'missing persisted original-job continuation entry')
            # A genuinely different worker, not the fixture's earlier Rename
            # object hidden behind its outer constructor patch.
            fresh_worker = transport._configured_renamer(flow.root)
            blocked = AssertionError('resume re-queried Provider transport')
            with ExitStack() as disconnected:
                constructor = disconnected.enter_context(patch('src.task_queue.Rename', return_value=fresh_worker))
                provider = disconnected.enter_context(patch.object(process.Rename, '_task_type_from_provider_batch',
                              side_effect=blocked))
                for method in ('_provider_search_batch', 'process'):
                    disconnected.enter_context(patch.object(fresh_worker, method, side_effect=blocked))
                for method in ('get_tv_info', 'get_movie_info', 'get_tv_info_by_id', 'get_movie_info_by_id',
                               'get_season_info'):
                    if hasattr(fresh_worker.search, method):
                        disconnected.enter_context(patch.object(fresh_worker.search, method, side_effect=blocked))
                await fresh.start()
                try:
                    await resume(flow.task_id, PlanStore.fingerprint(plan))
                    await asyncio.wait_for(fresh._queue.join(), 12)
                finally:
                    await fresh.stop()
                provider.assert_not_called()
                constructor.assert_called_once()
            revised = flow.store.load(flow.task_id)
            updated = flow.service.decision_state_store.load(flow.task_id)
            self.assertTrue(updated.confirmed_work)
            self.assertEqual(old_choice.to_dict()['excluded_sources'], updated.content_selection.to_dict()['excluded_sources'])
            self.assertEqual(old_choice.to_dict()['material_origins'], updated.content_selection.to_dict()['material_origins'])
            self.assertEqual('initial_tv_completion', revised.revision_action)
            self.assertEqual(frozen_proof, revised.metadata['automatic_recognition'])
            self.assertEqual('need_confirm', fresh._read(flow.task_id)['workflow_status'])
            self.assertFalse(flow.service.load_or_create(revised).approved_entry_ids)
            from src.rename.projection_authority import validate_plan_projection_receipt
            validate_plan_projection_receipt(revised)
            self.assertEqual(DecisionStateStore.fingerprint(updated), revised.metadata['decision_state_fingerprint'])

    async def test_movie_resume_after_identity_or_completion_failure_uses_original_job_in_fresh_queue(self):
        for stage in ('automatic_movie_selection', 'initial_movie_completion'):
            with self.subTest(stage=stage):
                reached = []
                def fail_once(transaction, envelope, terminal, forward):
                    if envelope.draft_plan.revision_action == stage and not reached:
                        reached.append(stage)
                        raise ValueError('disposable movie interruption')
                    return forward()
                async with self.flow(movie=True, hook=fail_once) as flow:
                    self.assertEqual([stage], reached)
                    plan, state = self.assert_default(flow)
                    self.assertEqual(stage == 'initial_movie_completion', bool(state.confirmed_work))
                    self.assertIsNone(state.movie_version_selection)
                    # An actual user edit after the failure must survive a new
                    # worker. It must not cause the identity to be published twice.
                    video = flow.source / 'Ordinary Movie [1080p].mkv'
                    if stage == 'initial_movie_completion':
                        plan = change(flow.service, plan, [video])
                    old = flow.service.decision_state_store.load(flow.task_id).content_selection.to_dict()
                    await flow.manager.stop()
                    fresh = TaskQueueManager(task_root=flow.root / 'tasks', plan_root=flow.root / 'plans',
                        decision_state_root=flow.root / 'decisions',
                        provider_link_catalog_root=flow.root / 'provider_links',
                        recovery_abandon_audit_root=flow.root / 'abandon')
                    worker = transport._configured_renamer(flow.root)
                    with ExitStack() as disconnected:
                        constructor = disconnected.enter_context(patch('src.task_queue.Rename', return_value=worker))
                        for method in ('process', '_provider_search_batch'):
                            disconnected.enter_context(patch.object(worker, method,
                                side_effect=AssertionError('movie resume queried Provider')))
                        for method in ('get_tv_info', 'get_movie_info', 'get_tv_info_by_id',
                                       'get_movie_info_by_id', 'get_season_info'):
                            if hasattr(worker.search, method):
                                disconnected.enter_context(patch.object(worker.search, method,
                                    side_effect=AssertionError('movie resume queried Provider')))
                        await fresh.start()
                        try:
                            await fresh.resume_default_activation(flow.task_id, PlanStore.fingerprint(plan))
                            await asyncio.wait_for(fresh._queue.join(), 12)
                        finally:
                            await fresh.stop()
                        constructor.assert_called_once()
                    revised = flow.store.load(flow.task_id)
                    updated = flow.service.decision_state_store.load(flow.task_id)
                    self.assertTrue(updated.confirmed_work, fresh._read(flow.task_id).get('error'))
                    self.assertEqual('need_confirm', fresh._read(flow.task_id)['workflow_status'],
                                     fresh._read(flow.task_id).get('error'))
                    self.assertEqual(stage == 'automatic_movie_selection', updated.movie_version_selection is not None)
                    self.assertEqual(old['excluded_sources'], updated.content_selection.to_dict()['excluded_sources'])
                    self.assertEqual(old['material_origins'], updated.content_selection.to_dict()['material_origins'])
                    self.assertEqual(stage == 'initial_movie_completion', video in revised.ignored_sources)
                    self.assertEqual(1, sum(p.revision_action == 'automatic_movie_selection' for _, p in flow.commits))

    async def test_automatic_tv_completion_does_not_turn_ambiguous_sp_into_accepted_extra(self):
        async with self.flow(ambiguous=True) as flow:
            plan, state = self.assert_default(flow)
            self.assertTrue(state.confirmed_work, flow.manager._read(flow.task_id).get('error'))
            by_source = {entry.source_path: entry for entry in plan.entries}
            for relative in ('SPs/Unnumbered.mkv', 'CD/Unnumbered.mkv', 'Unknown.mkv'):
                self.assertIn(flow.source / relative, by_source)
                self.assertEqual('unknown', by_source[flow.source / relative].presentation_asset.asset_role.kind)
            with self.assertRaises(ValueError):
                require_media_ready(plan)

    async def test_movie_resume_rejects_changed_job_corrupt_observation_and_stale_plan_without_writes(self):
        from tests.test_content_choice_contract import records
        reached = []
        def interrupt(transaction, envelope, terminal, forward):
            if envelope.draft_plan.revision_action == 'initial_movie_completion':
                reached.append(True)
                raise ValueError('disposable pending version')
            return forward()
        async with self.flow(movie=True, hook=interrupt) as flow:
            self.assertEqual([True], reached)
            plan, _ = self.assert_default(flow)
            await flow.manager.stop()
            fresh = TaskQueueManager(task_root=flow.root / 'tasks', plan_root=flow.root / 'plans',
                decision_state_root=flow.root / 'decisions', provider_link_catalog_root=flow.root / 'provider_links',
                recovery_abandon_audit_root=flow.root / 'abandon')
            await fresh.start()
            try:
                original_task = fresh._read(flow.task_id)
                path = flow.store.path_for(flow.task_id)
                original_bytes = path.read_bytes()
                for variant in ('later-job', 'missing-observation', 'changed-query', 'stale-plan'):
                    with self.subTest(variant=variant):
                        task = dict(original_task)
                        value = json.loads(original_bytes)
                        if variant == 'later-job':
                            task['recognition_job_id'] = 'actual-later-job'
                        elif variant == 'missing-observation':
                            value['metadata'].pop('automatic_recognition')
                        elif variant == 'changed-query':
                            value['metadata']['automatic_recognition']['query'] = 'Another Work'
                        fresh._write(flow.task_id, task)
                        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
                        before = records(flow.root)
                        with self.assertRaises((ValueError, RuntimeError)):
                            await fresh.resume_default_activation(flow.task_id,
                                '0' * 64 if variant == 'stale-plan' else PlanStore.fingerprint(plan))
                        self.assertEqual(before, records(flow.root))
                        self.assertFalse(fresh._pending)
                        self.assertTrue(fresh._queue.empty())
                path.write_bytes(original_bytes)
                fresh._write(flow.task_id, original_task)
            finally:
                await fresh.stop()


class DefaultActivationReadinessContracts(unittest.IsolatedAsyncioTestCase):
    async def test_new_content_manual_render_and_inline_exclusion_use_structured_outlets(self):
        from tests.test_complex_library_special_red_contract import _CaptureUi
        from tests.test_content_choice_contract import records
        from src.pages import content_review_view
        root, service, _, result = await self.tv()
        owner = SimpleNamespace(plan=result.plan, selected_ids={'first', 'second'},
            review_service=service, _render_presentation_override=Mock(),
            _render_override=Mock(), _render_subtitle_override=Mock())
        before = records(root)
        with patch.object(page, 'ui', _CaptureUi()), patch.object(page, 'notify'), \
                patch.object(content_review_view, 'open_content_workbench') as opened:
            page.PlanReviewDialog._render_manual_corrections(owner)
            owner._render_presentation_override.assert_not_called()
            owner._render_override.assert_not_called()
            owner._render_subtitle_override.assert_not_called()
            await page.PlanReviewDialog._ignore_selected_entry(owner)
            opened.assert_called_once_with(owner, ui=page.ui, notify=page.notify)
        self.assertEqual(before, records(root))

    async def test_confirmed_tv_reselection_rejects_before_request_without_writes(self):
        from tests.test_content_choice_contract import records
        root, service, _, result = await self.tv()
        queue = TaskQueueManager(task_root=root/'tasks', plan_root=root/'plans',
            decision_state_root=root/'decisions', provider_link_catalog_root=root/'provider_links',
            recovery_abandon_audit_root=root/'abandon_audit')
        primary = dict(result.state.confirmed_work['primary_provider'])
        candidate = {**primary, 'title': result.plan.work_identity.title,
            'library_domain': 'general', 'snapshot_at': '2026-09-13T00:00:00Z'}
        await queue.start()
        try:
            before = records(root)
            with patch('src.task_queue.Rename', side_effect=AssertionError('unsupported request reached worker')):
                for variant in ('same', 'different', 'season', 'stale'):
                    with self.subTest(variant=variant):
                        selected = {**candidate, 'provider_id': '9876'} if variant == 'different' else candidate
                        with self.assertRaises(page.PlanExecutionError):
                            await queue.enqueue_reidentify(result.plan.task_id, selected,
                                result.state.revision - (1 if variant == 'stale' else 0),
                                **({'season_number': 2} if variant == 'season' else {}))
                        self.assertEqual(before, records(root))
                        self.assertTrue(queue._queue.empty())
                        self.assertFalse(queue._pending)
        finally:
            await queue.stop()

    async def test_registered_partition_outlet_does_not_depend_on_removed_form(self):
        from nicegui import ui
        from nicegui.client import Client
        from nicegui.page import page as gui_page
        from tests._gui_helpers import GuiDriver
        from tests.test_content_choice_contract import records
        root, service, _, result = await self.tv()
        client = Client(gui_page('/isolated-content-partition'), request=None)
        self.addCleanup(client.delete)
        driver = GuiDriver('runTest')
        driver.client = client
        selected = {entry_id(e) for e in result.plan.entries if e.media_kind == 'video'}
        owner = SimpleNamespace(plan=result.plan, selected_ids=selected, review_service=service,
            snapshot=SimpleNamespace(issues=[{'code': 'mixed_presentation_requires_partition',
                'severity': 'error', 'message': 'mixed works require explicit partition'}], rows=[], work_identity={}),
            _adopt_revision=Mock(), _open_selected_source_regrouping=AsyncMock())
        for name in ('_partition_selected_as_cross_work', '_partition_exceptional_as_cross_work'):
            setattr(owner, name, getattr(page.PlanReviewDialog, name).__get__(owner))
        owner._exceptional_entry_ids = page.PlanReviewDialog._exceptional_entry_ids
        queue = TaskQueueManager(task_root=root/'tasks', plan_root=root/'plans',
            decision_state_root=root/'decisions', provider_link_catalog_root=root/'provider_links',
            recovery_abandon_audit_root=root/'abandon_audit')
        before = records(root)
        with client, patch.object(page, 'ui', ui), patch.object(page, 'notify'), \
                patch.object(page, 'task_queue', queue), \
                patch.object(queue, 'partition_task_sources', wraps=queue.partition_task_sources) as reached, \
                patch('src.pages.asset_actions_view.render_asset_choice'):
            owner.issues_container = ui.column()
            page.PlanReviewDialog._render_issues(owner)
            await driver.click('作为独立作品整理')
            owner._open_selected_source_regrouping.assert_awaited_once_with(tuple(sorted(selected)))
            reached.assert_not_awaited()
        # The existing content partition contract may refuse; no alternate writer.
        self.assertEqual(before, records(root))

    async def test_first_movie_keeps_unique_parsed_subtitle_without_manual_choice(self):
        import tempfile
        from tests._media_flow_helpers import MediaFlow
        from tests._media_flow_helpers import _make_version
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, primary, _ = _make_version(root / 'sources', '1080p')
            helper = MediaFlow('runTest')
            initial = await helper._run_provider_flow(root, source, 'first-subtitle')
            completed = await helper._complete_movie(root=root, result=initial, primary=primary)
            self.assertEqual('initial_movie_completion', completed.plan.revision_action, completed.notices)
            links = completed.state.content_selection.associations.to_dict()['relations']
            subtitles = [row for row in links if row['kind'] == 'subtitle']
            self.assertTrue(subtitles)
            self.assertTrue(all(row['owners'] == [str(primary)] and row['source'] == 'typed_subtitle'
                                for row in subtitles))
            self.assertFalse(completed.plan.subtitle_override_rules)
            require_media_ready(completed.plan)

    async def tv(self, *, extra_assets=False):
        from tests.test_initial_tv_completion_contract import InitialTVCompletionContracts
        helper = InitialTVCompletionContracts('runTest')
        self.addCleanup(helper.doCleanups)
        original = transport._make_season
        def sources(*args, **kwargs):
            source = original(*args, **kwargs)
            (source / 'UnclassifiedResources.7z').write_bytes(b'excluded archive')
            (source / 'Unrelated.mka').write_bytes(b'disposable audio association source')
            return source
        with patch.object(transport, '_make_season', sources if extra_assets else original):
            root, service, before, result, *_ = await helper.fixture(anime=False)
        self.assertEqual('initial_tv_completion', result.plan.revision_action)
        return root, service, before, result

    async def movie(self):
        from tests.test_initial_movie_completion_contract import InitialMovieFullCompletionContracts
        helper = InitialMovieFullCompletionContracts('runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        root, service, plan, primary, created = await helper.fixture()
        plan = change(service, plan, [created['subtitle']])
        from tests.test_content_subtitle_lifecycle import apply
        for source in [e.source_path for e in plan.entries if e.media_kind == 'subtitle']:
            plan = apply(service, plan, source, owner=primary)
        intent = helper.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
        complete = service.commit_initial_movie_completion(intent)
        self.assertEqual('initial_movie_completion', complete.revision_action)
        return root, service, plan, SimpleNamespace(plan=complete, store=service.plan_store,
            state=service.decision_state_store.load(complete.task_id),
            task=json.loads((root / 'tasks' / f'{complete.task_id}.json').read_text('utf-8')))

    async def test_completed_new_tv_has_derived_ready_and_public_approval(self):
        _, service, _, result = await self.tv()
        require_media_ready(result.plan)
        decision = service.approve_entries(result.plan.task_id, PlanStore.fingerprint(result.plan),
                                          [entry_id(e) for e in result.plan.entries])
        self.assertEqual({entry_id(e) for e in result.plan.entries}, set(decision.approved_entry_ids))

    async def test_completed_new_movie_has_derived_ready_and_public_approval(self):
        _, service, _, result = await self.movie()
        require_media_ready(result.plan)
        decision = service.approve_entries(result.plan.task_id, PlanStore.fingerprint(result.plan),
                                          [entry_id(e) for e in result.plan.entries])
        self.assertEqual({entry_id(e) for e in result.plan.entries}, set(decision.approved_entry_ids))

    async def test_incomplete_workspace_and_forged_ready_never_pass_public_approval(self):
        _, service, before, result = await self.tv()
        saved_path = service.plan_store.path_for(result.plan.task_id)
        original = saved_path.read_bytes()
        for label, plan in (
            ('unresolved', before['plan']),
            ('self-reported-ready', replace(result.plan, metadata={**result.plan.metadata, 'ready': True})),
            ('missing-policy', replace(result.plan, metadata={k: v for k, v in result.plan.metadata.items() if k != 'content_policy'})),
            ('missing-basis', replace(result.plan, metadata={k: v for k, v in result.plan.metadata.items() if k != 'initial_source_basis'})),
            ('missing-selection', replace(result.plan, metadata={k: v for k, v in result.plan.metadata.items() if k != 'content_selection'})),
        ):
            with self.subTest(label=label):
                try:
                    # Actual corrupted persisted candidate, not just a stale
                    # fingerprint that is rejected before reading the content.
                    saved_path.write_text(json.dumps(plan.to_dict()), encoding='utf-8')
                    with self.assertRaises((ValueError, RuntimeError)):
                        service.approve_entries(plan.task_id, PlanStore.fingerprint(plan),
                                               [entry_id(e) for e in plan.entries])
                finally:
                    saved_path.write_bytes(original)
        self.assertFalse(service.load_or_create(result.plan).approved_entry_ids)

    async def test_new_default_movie_executes_real_disposable_media_without_legacy_admission(self):
        from tests._media_flow_helpers import MediaFlow
        root, service, _, result = await self.movie()
        result.review = service
        runner = MediaFlow('runTest')
        task, ledger, _ = await runner._approve_and_execute(root=root, result=result)
        self.assertEqual('completed', task.get('execution_status'))
        self.assertEqual('completed', ledger['status'])
        for entry in result.plan.entries:
            self.assertTrue(entry.target_path.is_relative_to(root))
            self.assertTrue(entry.target_path.samefile(entry.source_path))

    async def test_first_movie_registered_gui_uses_typed_completion_off_ui_thread(self):
        import threading
        from nicegui import ui
        from nicegui.client import Client
        from nicegui.page import page as gui_page
        from src.pages.asset_choice_forms import render_movie_choices, show_movie_versions
        from tests._gui_helpers import GuiDriver
        from tests.test_initial_movie_completion_contract import InitialMovieFullCompletionContracts
        helper = InitialMovieFullCompletionContracts('runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        root, service, plan, primary, _ = await helper.fixture()
        client = Client(gui_page('/isolated-default-first-movie'), request=None)
        self.addCleanup(client.delete)
        driver = GuiDriver('runTest')
        driver.client = client
        owner = SimpleNamespace(plan=plan, task_decision=service.decision_state_store.load(plan.task_id),
            task_data=json.loads((root / 'tasks' / f'{plan.task_id}.json').read_text('utf-8')),
            review_service=service, client=client, movie_primary_video_input=SimpleNamespace(value=[str(primary)]),
            _render_movie_primary_picker=Mock(),
            _completion_domain=lambda: 'animation', _inventory_draft_for_completion=lambda: plan,
            _adopt_revision=lambda updated: setattr(owner, 'plan', updated))
        owner._complete_movie_with_primary = lambda group_id: page.PlanReviewDialog._complete_movie_with_primary(owner, group_id)
        owner._video_options = lambda: {str(primary): primary.name}
        def edit_versions(dialog, group_id):
            show_movie_versions(dialog, group_id, ui=ui, notify=Mock(), complete_movie=Mock(side_effect=AssertionError('old writer')))
        ui_thread = threading.get_ident()
        entered, release = threading.Event(), threading.Event()
        threads = []
        original = service.prepare_initial_movie_completion
        def observed(*args, **kwargs):
            threads.append(threading.get_ident())
            entered.set()
            release.wait(2)
            return original(*args, **kwargs)
        with client, patch.object(page, 'notify'), \
                patch('src.rename.target_routing.snapshot_from_config_manager', return_value=service.root_snapshot_source.observe()), \
                patch.object(service, 'prepare_initial_movie_completion', side_effect=observed), \
                patch.object(service, 'complete_inventory_draft', side_effect=ValueError('legacy completion forbidden')) as legacy:
            render_movie_choices(owner, plan.presentation_groups[0], None,
                {str(primary): primary.name}, [str(primary)], ui=ui, edit_versions=edit_versions)
            owner._render_movie_primary_picker.assert_not_called()
            await driver.click('选择电影正片')
            pickers = [e for e in client.elements.values()
                       if isinstance(e, ui.select) and e._props.get('label') == '电影正片文件（可多选）']
            self.assertEqual(1, len(pickers))
            pickers[0].set_value([str(primary)])
            first = asyncio.create_task(driver.click('保存正片版本'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2),
                                'registered first movie button never entered typed completion')
                self.assertNotEqual(ui_thread, threads[0], 'movie source/font I/O blocked GUI thread')
                second = asyncio.create_task(driver.click('保存正片版本'))
                await asyncio.sleep(0.02)
                self.assertEqual(1, len(threads), 'double click launched a second completion')
                release.set()
                await asyncio.wait_for(asyncio.gather(first, second), 5)
                legacy.assert_not_called()
                updated = service.plan_store.load(plan.task_id)
                self.assertEqual('initial_movie_completion', updated.revision_action)
            finally:
                release.set()
                await first







    async def test_new_default_movie_missing_media_rebuild_preserves_exclusions(self):
        from tests._media_flow_helpers import MediaFlow
        root, service, _, result = await self.movie()
        result.review = service
        runner = MediaFlow('runTest')
        task, ledger, execution = await runner._approve_and_execute(root=root, result=result)
        self.assertEqual('completed', ledger['status'])
        self.assertEqual('completed', task.get('execution_status'))
        current = service.plan_store.load(result.plan.task_id)
        missing = next(e for e in current.entries if e.media_kind == 'video')
        self.assertTrue(missing.target_path.is_relative_to(root))
        self.assertTrue(missing.target_path.samefile(missing.source_path))
        missing.target_path.unlink()  # Only this test's disposable output link.
        view = execution.rebuild_service.inspect(current.task_id)
        self.assertEqual('missing', view.state, view.detail)
        rebuilt = execution.rebuild_service.generate(current.task_id, expected_fingerprint=view.fingerprint)
        self.assertEqual(set(current.ignored_sources), set(rebuilt.ignored_sources))
        self.assertFalse(service.load_or_create(rebuilt).approved_entry_ids)
        self.assertTrue(missing.source_path.is_file())
        self.assertFalse(missing.target_path.exists(), 'plan-only rebuild executed media')


