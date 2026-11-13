"""Content evidence on explicit media intents, isolated from font-only repair."""

from tests._historical_replacement import historical_replacement
import os
import hashlib
import json
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from tests import test_completed_rebuild_work_contract as completed
from tests import _media_flow_helpers as fonts
from tests.test_content_choice_contract import change


def records(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for folder in
            ('plans', 'decisions', 'reviews', 'records', 'tasks', 'work_catalog',
             'series_catalog', 'font_supply', 'task_batch_scope', 'source_ownership', 'projection_journal')
            for p in (root / folder).rglob('*') if p.is_file() and not p.name.endswith('.lock')}


def alter_same_stat(path):
    raw, st = path.read_bytes(), path.stat()
    path.write_bytes(bytes(byte ^ 1 for byte in raw))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    return raw, st


def restore_bytes(path, saved):
    raw, st = saved
    path.write_bytes(raw)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


class ContentLifecycleContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = completed.CompletedMovieRebuildContracts('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        source, primary, self.font_package = completed.pair_fixture._make_version(self.root / 'sources', '1080p')
        self.omitted = source / 'README.txt'
        self.omitted.write_bytes(b'non-watchable package instructions')
        initial = await self.fixture.harness._run_provider_flow(self.root, source, 'content-lifecycle')
        result = await self.fixture.harness._complete_movie(root=self.root, result=initial, primary=primary)
        self.review = result.review
        plan = result.plan
        self.assertIsNotNone(result.state.content_selection)
        self.assertIn(self.omitted, plan.ignored_sources,
            'new safe-content producer excludes the non-watchable README')
        task, ledger, self.execution = await self.fixture.harness._approve_and_execute(
            root=self.root, result=self.fixture.current(plan.task_id))
        self.assertEqual('completed', task['execution_status'], task.get('error'))
        self.assertEqual('completed', ledger['status'])
        self.plan = self.fixture.current(plan.task_id).plan
        self.task_id = plan.task_id
        self.rebuild = self.fixture.rebuild_service()
        self.assertIn(self.omitted, self.plan.ignored_sources)
        self.assertFalse(any(e.source_path == self.omitted for e in self.plan.entries))

    def missing(self):
        entry = next(e for e in self.plan.entries if e.media_kind == 'video'
                     and e.presentation_asset.asset_role.kind == 'content_version')
        self.assertTrue(entry.target_path.is_relative_to(self.root))
        entry.target_path.unlink()  # Exact disposable hardlink target, source retained.
        return entry



    async def test_missing_media_preflight_ignores_omitted_contents_without_writes(self):
        self.missing()
        saved = alter_same_stat(self.omitted)
        before = records(self.root)
        view = self.rebuild.inspect(self.task_id)
        self.assertEqual('missing', view.state, view.detail)
        self.assertEqual(before, records(self.root))
        restore_bytes(self.omitted, saved)
        self.assertEqual('missing', self.rebuild.inspect(self.task_id).state)

    async def test_omitted_content_changes_do_not_block_explicit_media_rebuild(self):
        self.missing()
        view = self.rebuild.inspect(self.task_id)
        self.assertEqual('missing', view.state, view.detail)
        alter_same_stat(self.omitted)
        plan = self.rebuild.generate(self.task_id, expected_fingerprint=view.fingerprint)
        self.assertIn(self.omitted, plan.ignored_sources)

    async def test_rebuild_keeps_manifest_gate_but_does_not_read_omitted_content(self):
        from src.rename import completed_rebuild as module
        self.missing()
        view = self.rebuild.inspect(self.task_id)
        original = module.build_reuse_resolution
        self.assert_disconnected(
            lambda: self.rebuild.generate(self.task_id, expected_fingerprint=view.fingerprint),
            lambda: self.rebuild.generate(self.task_id, expected_fingerprint=view.fingerprint),
            'src.rename.completed_rebuild.build_reuse_resolution', original)
        reached = []
        saved = self.omitted.read_bytes(), self.omitted.stat()
        def preparing(*args, **kwargs):
            result = original(*args, **kwargs)
            if not reached:
                reached.append(True)
                alter_same_stat(self.omitted)
            return result
        before = records(self.root)
        with patch('src.rename.completed_rebuild.build_reuse_resolution', side_effect=preparing):
            plan = self.rebuild.generate(self.task_id, expected_fingerprint=view.fingerprint)
        self.assertTrue(reached, 'must reach the real candidate-preparation window')
        self.assertIn(self.omitted, plan.ignored_sources)
        restore_bytes(self.omitted, saved)

    def assert_disconnected(self, preflight, execute, hook, original, *, after_calls=1):
        from src.rename import content_selection
        self.assertTrue(callable(getattr(content_selection, 'validate_media_intent', None)))
        real_gate = content_selection.validate_media_intent
        before = records(self.root)
        with patch('src.rename.content_selection.validate_media_intent',
                side_effect=ValueError('内容媒体门断开')) as guard:
            # Public producers may wrap the detail in a refresh instruction.
            # The called guard and unchanged records prove the contract.
            with self.assertRaises((ValueError, RuntimeError)):
                preflight()
            guard.assert_called()
        self.assertEqual(before, records(self.root))
        calls, blocked = [], []
        def prepare(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            return result
        def check(*args, **kwargs):
            if len(calls) >= after_calls:
                blocked.append(True)
                raise ValueError('归档前内容媒体门断开')
            return real_gate(*args, **kwargs)
        before = records(self.root)
        with patch(hook, side_effect=prepare), patch('src.rename.content_selection.validate_media_intent', side_effect=check):
            with self.assertRaisesRegex((ValueError, RuntimeError), '归档前内容媒体门断开'):
                execute()
        self.assertTrue(blocked, 'must call the shared gate after actual preparation')
        self.assertEqual(before, records(self.root))

    def replacement_case(self):
        from tests.test_asset_actions_contract import AssetActionContracts
        case = AssetActionContracts('runTest')
        case.fixture, case.root = self.fixture, self.root
        return case

    def later_revision(self):
        """A valid public Decision revision, not forged persisted bytes."""
        from src.rename.decision_state import DecisionCommandService
        from src.rename.projection_authority import TaskTerminalMutation
        current = self.review.plan_store.load(self.task_id)
        state = self.review.decision_state_store.load(self.task_id)
        revised, plan = DecisionCommandService(self.review.decision_state_store, self.review.plan_store).update(
            self.task_id, expected_revision=state.revision, changes={}, planner=lambda _: current,
            task_terminal=TaskTerminalMutation.prepare(self.root / 'tasks', self.task_id,
                lambda _state, _plan, task: task))
        self.assertEqual(state.revision + 1, revised.revision)
        return plan

    def assert_later_revision_preserved(self, execute, hook, original):
        later = []
        def prepared(*args, **kwargs):
            result = original(*args, **kwargs)
            if not later:
                later.append(None)
                self.later_revision()
                later[0] = records(self.root)
            return result
        with patch(hook, side_effect=prepared):
            with self.assertRaises((ValueError, RuntimeError)):
                execute()
        self.assertTrue(later, 'must reach candidate preparation and commit a valid later revision')
        self.assertEqual(later[0], records(self.root), 'a stale media intent must not add history')

    async def test_rebuild_later_revision_rejects_before_history(self):
        from src.rename import completed_rebuild as module
        self.missing()
        view = self.rebuild.inspect(self.task_id)
        self.assert_later_revision_preserved(
            lambda: self.rebuild.generate(self.task_id, expected_fingerprint=view.fingerprint),
            'src.rename.completed_rebuild.build_reuse_resolution', module.build_reuse_resolution)

    async def test_restart_later_revision_rejects_before_history(self):
        from src.rename import rolled_back_restart as module
        from src.rename import plan_review
        plan, _ = await self.rolled_back_content()
        self.task_id = plan.task_id
        self.review = self.execution.review_service
        preparation = module.prepare(self.execution, self.task_id)
        self.assert_later_revision_preserved(
            lambda: module.restart(self.execution, self.task_id, preparation),
            'src.rename.plan_review._resolve_asset_collisions', plan_review._resolve_asset_collisions)

    async def test_reentry_later_revision_rejects_before_history(self):
        from src.rename import completed_source_reentry as module
        case = self.replacement_case()
        await case.complete_replacement(await case.incoming(valid_media=True, keep_font=True))
        self.assert_later_revision_preserved(
            lambda: module.generate_reentry(self.rebuild, self.review, self.task_id),
            'src.rename.plan_review.PlanReviewService._finalize_completed_movie_plan',
            self.review._finalize_completed_movie_plan)

    def assert_archive_serializes_revision(self, execute, hook, original, *, no_plan=False):
        blocked, reached = [], []
        def archive(*args, **kwargs):
            reached.append(True)
            before = records(self.root)
            try:
                if no_plan:
                    from src.rename.decision_state import DecisionCommandService
                    state = self.review.decision_state_store.load(self.task_id)
                    DecisionCommandService(self.review.decision_state_store, self.review.plan_store).update(
                        self.task_id, expected_revision=state.revision, changes={})
                else:
                    self.later_revision()
            except (ValueError, RuntimeError) as exc:
                if not no_plan:
                    self.assertIn('transaction is already active', str(exc))
                blocked.append(True)
            self.assertEqual(before, records(self.root),
                'a competing canonical revision cannot commit inside the archive boundary')
            return original(*args, **kwargs)
        with patch(hook, side_effect=archive):
            result = execute()
        self.assertTrue(reached, 'must reach the actual archive writer')
        self.assertTrue(blocked, 'must reject the competing canonical transaction')
        self.assertIsNotNone(result)

    async def test_rebuild_archive_and_revision_share_transaction(self):
        from src.rename import completed_rebuild as module
        self.missing()
        view = self.rebuild.inspect(self.task_id)
        self.assert_archive_serializes_revision(
            lambda: self.rebuild.generate(self.task_id, expected_fingerprint=view.fingerprint),
            'src.rename.completed_rebuild.archive_exact', module.archive_exact)

    async def test_restart_archive_and_revision_share_transaction(self):
        from src.rename import rolled_back_restart as module
        plan, _ = await self.rolled_back_content()
        self.task_id = plan.task_id
        self.review = self.execution.review_service
        preparation = module.prepare(self.execution, self.task_id)
        self.assert_archive_serializes_revision(
            lambda: module.restart(self.execution, self.task_id, preparation),
            'src.rename.rolled_back_restart.archive_evidence', module.archive_evidence)

    async def test_reentry_archive_and_revision_share_transaction(self):
        from src.rename import completed_source_reentry as module
        case = self.replacement_case()
        await case.complete_replacement(await case.incoming(valid_media=True, keep_font=True))
        self.assert_archive_serializes_revision(
            lambda: module.generate_reentry(self.rebuild, self.review, self.task_id),
            'src.rename.completed_source_reentry.archive_exact', module.archive_exact)

    async def test_rebuild_archive_rejects_no_plan_decision_writer(self):
        from src.rename import completed_rebuild as module
        self.missing()
        view = self.rebuild.inspect(self.task_id)
        self.assert_archive_serializes_revision(
            lambda: self.rebuild.generate(self.task_id, expected_fingerprint=view.fingerprint),
            'src.rename.completed_rebuild.archive_exact', module.archive_exact, no_plan=True)

    async def test_restart_archive_rejects_no_plan_decision_writer(self):
        from src.rename import rolled_back_restart as module
        plan, _ = await self.rolled_back_content()
        self.task_id = plan.task_id
        self.review = self.execution.review_service
        preparation = module.prepare(self.execution, self.task_id)
        self.assert_archive_serializes_revision(
            lambda: module.restart(self.execution, self.task_id, preparation),
            'src.rename.rolled_back_restart.archive_evidence', module.archive_evidence, no_plan=True)

    async def test_reentry_archive_rejects_no_plan_decision_writer(self):
        from src.rename import completed_source_reentry as module
        case = self.replacement_case()
        await case.complete_replacement(await case.incoming(valid_media=True, keep_font=True))
        self.assert_archive_serializes_revision(
            lambda: module.generate_reentry(self.rebuild, self.review, self.task_id),
            'src.rename.completed_source_reentry.archive_exact', module.archive_exact, no_plan=True)

    def assert_archive_conflict_preserves_records(self, execute, hook, original, *, restart=False):
        injected = []
        def archive(*args, **kwargs):
            if restart:
                from src.rename.rolled_back_restart import audit_payload
                import hashlib
                context, task_id, action, files = args
                key = hashlib.sha256(audit_payload(task_id, action, files)).hexdigest()
                destination = context.record_root / 'operation-audit' / task_id / f'{key}.json'
            else:
                root, task_id, fingerprint, _ledger, _mapping = args
                destination = Path(root) / 'history' / task_id / f'{fingerprint}.ledger.json'
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b'fixture: conflicting immutable history')
            injected.append(records(self.root))
            return original(*args, **kwargs)
        with patch(hook, side_effect=archive):
            with self.assertRaises((ValueError, RuntimeError)):
                execute()
        self.assertEqual(1, len(injected), 'must reach the original immutable writer')
        self.assertEqual(injected[0], records(self.root), 'conflict must not overwrite history or commit state')

    async def test_rebuild_archive_conflict_cannot_commit_or_overwrite(self):
        from src.rename import completed_rebuild as module
        self.missing()
        view = self.rebuild.inspect(self.task_id)
        self.assert_archive_conflict_preserves_records(
            lambda: self.rebuild.generate(self.task_id, expected_fingerprint=view.fingerprint),
            'src.rename.completed_rebuild.archive_exact', module.archive_exact)

    async def test_restart_archive_conflict_cannot_commit_or_overwrite(self):
        from src.rename import rolled_back_restart as module
        plan, _ = await self.rolled_back_content()
        self.task_id = plan.task_id
        self.review = self.execution.review_service
        preparation = module.prepare(self.execution, self.task_id)
        self.assert_archive_conflict_preserves_records(
            lambda: module.restart(self.execution, self.task_id, preparation),
            'src.rename.rolled_back_restart.archive_evidence', module.archive_evidence, restart=True)

    async def test_archive_intent_rejects_missing_forged_or_disconnected_transport(self):
        from src.rename.decision_state import DecisionCommandService
        self.missing()
        original = DecisionCommandService._commit_projection
        captured = []
        for fault in ('omitted', 'task', 'plan', 'action', 'evidence', 'context', 'disconnect'):
            with self.subTest(fault=fault):
                view = self.rebuild.inspect(self.task_id)
                before = records(self.root)
                reached = []
                def submit(command, *args, **kwargs):
                    intent = kwargs.get('media_archive')
                    self.assertIsNotNone(intent, 'media archive must enter the canonical transaction')
                    reached.append(True)
                    captured.append((command, intent))
                    if fault == 'omitted':
                        kwargs['media_archive'] = None
                        # The persisted transition still resets execution;
                        # hiding its action string cannot suppress the gate.
                        args = (*args[:2], replace(args[2], revision_action='ordinary_revision'))
                    elif fault == 'task':
                        kwargs['media_archive'] = replace(intent, task_id='another-task')
                    elif fault == 'plan':
                        kwargs['media_archive'] = replace(intent, plan_fingerprint='f' * 64)
                    elif fault == 'action':
                        kwargs['media_archive'] = replace(intent, action='restart_after_rollback')
                    elif fault == 'evidence':
                        kwargs['media_archive'] = replace(intent, ledger_bytes=b'changed-ledger')
                    elif fault == 'context':
                        current_root = self.rebuild.records
                        self.rebuild.records = self.root / 'unregistered-record-root'
                        try:
                            return original(command, *args, **kwargs)
                        finally:
                            self.rebuild.records = current_root
                    elif fault == 'disconnect':
                        with patch('src.rename.media_archive.validate_archive_commit', return_value=None):
                            return original(command, *args, **kwargs)
                    return original(command, *args, **kwargs)
                with patch('src.rename.decision_state.DecisionCommandService._commit_projection', submit):
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.rebuild.generate(self.task_id, expected_fingerprint=view.fingerprint)
                self.assertTrue(reached)
                self.assertEqual(before, records(self.root))
        command, intent = captured[0]
        state = self.review.decision_state_store.load(self.task_id)
        before = records(self.root)
        with self.assertRaises((ValueError, RuntimeError)):
            command.update(self.task_id, expected_revision=state.revision, changes={}, media_archive=intent)
        self.assertEqual(before, records(self.root))

    async def test_archived_history_survives_commit_interruption_and_retries_identically(self):
        self.missing()
        histories = None
        for stage in ('media_history_archived', 'prepared', 'decision_written', 'ownership_claimed', 'plan_written'):
            with self.subTest(stage=stage):
                view = self.rebuild.inspect(self.task_id)
                before = records(self.root)
                protected = {key: value for key, value in before.items()
                    if not key.startswith(('projection_journal/', 'records/history/'))}
                reached = []
                def checkpoint(_service, phase):
                    if phase == stage:
                        reached.append(phase)
                        raise RuntimeError('媒体历史提交中断:' + stage)
                with patch('src.rename.projection_authority.ProjectionCommitService._checkpoint', checkpoint):
                    with self.assertRaisesRegex(RuntimeError, '媒体历史提交中断:' + stage):
                        self.rebuild.generate(self.task_id, expected_fingerprint=view.fingerprint)
                self.assertEqual([stage], reached)
                after = records(self.root)
                actual = {key: value for key, value in after.items()
                    if not key.startswith(('projection_journal/', 'records/history/'))}
                self.assertEqual(protected, {key: actual.get(key) for key in protected})
                # Original rollback restores current but deliberately retains
                # an immutable Plan history already published at plan_written.
                for key in actual.keys() - protected.keys():
                    self.assertEqual('plan_written', stage)
                    self.assertTrue(key.startswith(f'plans/history/{self.task_id}/'))
                    fingerprint = Path(key).stem
                    stored = self.review.plan_store.load_version(self.task_id, fingerprint)
                    self.assertEqual(fingerprint, PlanStore.fingerprint(stored))
                now = {key: value for key, value in after.items() if key.startswith('records/history/')}
                self.assertTrue(now)
                if histories is not None:
                    self.assertEqual(histories, now)
                histories = now
        view = self.rebuild.inspect(self.task_id)
        result = self.rebuild.generate(self.task_id, expected_fingerprint=view.fingerprint)
        self.assertNotEqual(PlanStore.fingerprint(self.plan), PlanStore.fingerprint(result))
        self.assertEqual(histories, {key: value for key, value in records(self.root).items()
            if key.startswith('records/history/')})

    @historical_replacement
    async def rolled_back_content(self):
        case = self.replacement_case()
        incoming = await case.incoming(valid_media=True, keep_font=True)
        review = self.execution.review_service
        plan = incoming
        self.assertIsNotNone(review.decision_state_store.load(plan.task_id).content_selection)
        omitted = next(e.source_path for e in plan.entries if e.source_path.suffix == '.mka')
        plan = change(review, plan, [omitted])
        plan = await case.complete_replacement(plan)
        preparation = self.execution.prepare_asset_rollback(plan.task_id, PlanStore.fingerprint(plan))
        self.execution.rollback_asset_replacement(plan.task_id, PlanStore.fingerprint(plan),
            expected_recovery_fingerprint=preparation.fingerprint)
        return plan, omitted

    async def test_restart_keeps_manifest_gate_without_omitted_content_audit(self):
        from src.rename import rolled_back_restart as module
        plan, omitted = await self.rolled_back_content()
        saved = alter_same_stat(omitted)
        before = records(self.root)
        with self.subTest(window='preflight'):
            module.prepare(self.execution, plan.task_id)
            self.assertEqual(before, records(self.root))
        restore_bytes(omitted, saved)
        prepared = module.prepare(self.execution, plan.task_id)
        original = module._inspect
        self.assert_disconnected(lambda: module.prepare(self.execution, plan.task_id),
            lambda: module.restart(self.execution, plan.task_id, prepared),
            'src.rename.rolled_back_restart._inspect', original, after_calls=2)
        calls = []
        def inspected(*args):
            result = original(*args)
            calls.append(True)
            if len(calls) == 2:
                alter_same_stat(omitted)
            return result
        before = records(self.root)
        with self.subTest(window='last_prepare'), patch('src.rename.rolled_back_restart._inspect', side_effect=inspected):
            module.restart(self.execution, plan.task_id, prepared)
            self.assertGreaterEqual(len(calls), 2)
        restore_bytes(omitted, saved)

    async def test_reentry_keeps_manifest_gate_without_omitted_content_audit(self):
        from src.rename import completed_source_reentry as module
        case = self.replacement_case()
        await case.complete_replacement(await case.incoming(valid_media=True, keep_font=True))
        saved = alter_same_stat(self.omitted)
        before = records(self.root)
        with self.subTest(window='preflight'):
            module.inspect_reentry(self.rebuild, self.review, self.task_id)
            self.assertEqual(before, records(self.root))
        restore_bytes(self.omitted, saved)
        original = self.review._finalize_completed_movie_plan
        self.assert_disconnected(lambda: module.inspect_reentry(self.rebuild, self.review, self.task_id),
            lambda: module.generate_reentry(self.rebuild, self.review, self.task_id),
            'src.rename.plan_review.PlanReviewService._finalize_completed_movie_plan', original)
        reached = []
        def finalized(*args):
            result = original(*args)
            reached.append(True)
            alter_same_stat(self.omitted)
            return result
        before = records(self.root)
        with self.subTest(window='candidate'), patch('src.rename.plan_review.PlanReviewService._finalize_completed_movie_plan',
                side_effect=finalized):
            module.generate_reentry(self.rebuild, self.review, self.task_id)
            self.assertTrue(reached)
        restore_bytes(self.omitted, saved)

    async def test_media_gate_checks_decision_scope_and_availability_not_old_inode(self):
        from src.rename import content_selection
        self.assertTrue(callable(getattr(content_selection, 'validate_media_intent', None)))
        gate = content_selection.validate_media_intent
        state = self.review.decision_state_store.load(self.task_id)
        gate(self.plan, state, task_root=self.root / 'tasks')
        before = records(self.root)
        with self.assertRaises((ValueError, RuntimeError)):
            gate(self.plan, replace(state, library_domain='general'), task_root=self.root / 'tasks')
        self.assertEqual(before, records(self.root))
        task_path = self.root / 'tasks' / f'{self.task_id}.json'
        raw = task_path.read_bytes()
        task = json.loads(raw)
        task['task_source_scope']['reservation_revision'] += 1
        task_path.write_text(json.dumps(task), encoding='utf-8')
        changed = records(self.root)
        with self.assertRaises((ValueError, RuntimeError)):
            gate(self.plan, state, task_root=self.root / 'tasks')
        self.assertEqual(changed, records(self.root))
        task_path.write_bytes(raw)
        source = next(e.source_path for e in self.plan.entries if e.media_kind == 'video')
        st, data = source.stat(), source.read_bytes()
        held = source.with_suffix('.content-held')
        self.assertFalse(held.exists())
        source.rename(held)
        source.write_bytes(data)
        os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertNotEqual(st.st_ino, source.stat().st_ino)
        gate(self.plan, state, task_root=self.root / 'tasks')
        source.unlink()
        with self.assertRaises((ValueError, RuntimeError)):
            gate(self.plan, state, task_root=self.root / 'tasks')
        held.rename(source)
        # A source listed among MKV font inputs remains media, never a font waiver.
        value = state.content_selection.to_dict()
        other = next(e.source_path for e in self.plan.entries if e.media_kind == 'video')
        next(row for row in value['associations']['sources'] if row['path'] == str(other))['kind'] = 'font'
        next(row for row in value['materials'] if row['source_path'] == str(other))['role'] = 'font'
        with self.assertRaises((ValueError, RuntimeError)):
            # A main video has subtitle/audio owners; their typed association
            # may reject the forged font role before the media gate is reached.
            forged = content_selection.ContentSelection.from_mapping(value)
            fake_plan = replace(self.plan, metadata={**self.plan.metadata,
                'content_selection': forged.to_dict(), 'content_selection_fingerprint': forged.fingerprint})
            gate(fake_plan, replace(state, content_selection=forged), task_root=self.root / 'tasks')
        self.assertEqual(before, records(self.root))

    async def test_media_intent_shared_gate_rejects_disconnected_producer(self):
        from src.rename import content_selection
        self.assertTrue(callable(getattr(content_selection, 'validate_media_intent', None)),
                        'explicit media intents require one shared content evidence gate')
        self.missing()
        before = records(self.root)
        with patch('src.rename.content_selection.validate_media_intent',
                side_effect=ValueError('内容媒体门断开')) as guard:
            view = self.rebuild.inspect(self.task_id)
            self.assertEqual('blocked', view.state)
            self.assertIn('内容媒体门断开', view.detail)
            guard.assert_called()
        self.assertEqual(before, records(self.root))

