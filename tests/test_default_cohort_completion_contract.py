"""Default-content cohort completion through actual admission and UI events.

Only Provider transport is frozen. All stores and source bytes are disposable;
no real application startup, media library, or network is used.
"""
import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nicegui.client import Client
from nicegui.page import page as nice_page

from src.pages.plan_review_page import PlanReviewDialog as RealPlanReviewDialog
from src.pages.review_operation_view import build_review_operation_presentation
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.projection_authority import validate_plan_projection_receipt
from tests.test_content_choice_contract import change, identifier
from src.rename.rename_plan import SubtitleOverrideScope
from tests.test_err056_cohort import IncompleteMetadataFlow
from tests.test_r24_err048_series_registration_contract import MEMBERS
from tests.test_r24_phase_c_review_operation_migration_contract import current_provider_selection_context
from tests import _gui_helpers as gui_driver
from tests import test_provider_subject_plan_wiring_red_contract as source_fixture


def compact_sources(root, title, _episodes):
    source = source_fixture._make_episode_source(root, title, (1, 2))
    for video in source.glob('*.mkv'):
        video.with_suffix('.sc.ass').write_bytes(b'[Script Info]\nScriptType: v4.00+\n')
    (source/'README.txt').write_text('disposable package information', encoding='utf-8')
    (source/'Series [Fonts].zip').write_bytes(b'excluded archive')
    return source


class DefaultCohortCompletionContracts(unittest.IsolatedAsyncioTestCase):
    def persisted(self, runtime):
        roots = (runtime.plans, runtime.decisions, runtime.tasks,
                 runtime.manager.projection_commit_service.cohort_authority_store.root)
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for root in roots for path in root.rglob('*.json')}

    def command(self, result, kind='confirm_cohort'):
        actions = build_review_operation_presentation(result.task).actions
        return next(dict(action.payload) for action in actions if action.payload and action.payload.get('kind') == kind)

    async def pending(self, runtime, selected, *, edit=False):
        runtime.source_factory = compact_sources
        original = runtime.select
        before = {}

        async def select(task_id, subject, *, confirm=True):
            current = runtime.result(task_id)
            if edit:
                videos = sorted(e.source_path for e in current.plan.entries if e.media_kind == 'video')
                subtitle = videos[0].with_suffix('.sc.ass')
                runtime.review.apply_subtitle_override_rule(current.plan.task_id, PlanStore.fingerprint(current.plan),
                    identifier(current.plan, subtitle), scope=SubtitleOverrideScope.FILE, primary_language='zh', script='Hans',
                    associated_video_source_path=videos[0], reason='人工确认字幕归属与简体语言')
                current = runtime.result(task_id)
                # These are actual user content choices before work identity,
                # not downstream Plans forged to manufacture the incident.
                change(runtime.review, current.plan, [videos[-1]], action='exclude')
                current = runtime.result(task_id)
            before.update(plan=current.plan, state=current.state,
                scope=current.task['task_source_scope'], task=current.task)
            return await original(task_id, subject, confirm=False)

        runtime.select = select
        result = await runtime.add(selected, confirm=False)
        context = current_provider_selection_context(result.task)
        self.assertEqual('awaiting_user', context.get('lifecycle'),
            {'lifecycle': context.get('lifecycle'), 'failure_code': context.get('failure_code'),
             'error': result.task.get('error')})
        self.assertEqual('need_confirm', result.task['workflow_status'])
        self.assertEqual(before['state'].content_selection, result.state.content_selection)
        self.assertEqual(before['state'].initial_source_basis, result.state.initial_source_basis)
        self.assertEqual(before['scope'], result.task['task_source_scope'])
        self.assertEqual(before['plan'].expected_sources, result.plan.expected_sources)
        self.assertEqual(before['plan'].ignored_sources, result.plan.ignored_sources)
        self.assertEqual(before['plan'].subtitle_override_rules, result.plan.subtitle_override_rules)
        self.assertEqual({}, dict(result.state.confirmed_work))
        validate_plan_projection_receipt(result.plan)
        proof = result.state.to_dict().get('pending_tv_selection')
        self.assertIsInstance(proof, dict, 'Pending evidence must be Decision authority, not task-only data')
        self.assertEqual(proof, result.plan.to_dict()['metadata'].get('pending_tv_selection'))
        self.assertEqual(context['request_id'], proof['request_id'])
        self.assertTrue({'content_policy_pending'} <= {i.code for i in PlanVerifier().verify(result.plan).issues})
        runtime.assert_sources_unchanged(self)
        return result, before

    async def test_first_and_middle_pending_preserve_default_content_and_source_basis(self):
        for selected, middle in ((MEMBERS[0], False), (MEMBERS[1], True)):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as directory:
                async with IncompleteMetadataFlow(Path(directory), anchor_middle=middle) as runtime:
                    await self.pending(runtime, selected, edit=True)

    async def test_registered_confirmation_completes_native_cohort_without_legacy_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            async with IncompleteMetadataFlow(Path(directory)) as runtime:
                result, before = await self.pending(runtime, MEMBERS[0], edit=True)
                presentation = build_review_operation_presentation(result.task)
                choices = [action for action in presentation.actions
                    if action.payload is not None and action.payload.get('kind') == 'confirm_cohort']
                self.assertEqual(1, len(choices), 'The actual pending operation must expose one cohort command')
                client = Client(nice_page('/isolated-default-cohort'), request=None)
                self.addCleanup(client.delete)
                driver = gui_driver.GuiDriver('runTest')
                driver.client = client
                from src.pages import plan_review_page as page
                runtime.review.root_snapshot_source = runtime.manager.root_snapshot_source
                with patch.object(page, 'PlanReviewDialog', RealPlanReviewDialog), patch.object(page, 'PlanReviewService', lambda *_a, **_kw: runtime.review):
                    with client:
                        dialog = RealPlanReviewDialog(result.plan, result.task)
                        dialog.open()
                try:
                    notices = []
                    with patch.object(runtime.review, 'upsert_presentation_group',
                            side_effect=AssertionError('new cohort must not use legacy multi-step writer')), patch.object(
                            page, 'notify', lambda message, **kw: notices.append((str(message), kw.get('type')))), patch.object(
                            page, 'PlanReviewDialog', RealPlanReviewDialog):
                        await asyncio.wait_for(driver.click(choices[0].label), 15)
                    updated = runtime.result(result.task_id)
                    self.assertTrue(updated.state.confirmed_work, notices)
                    self.assertFalse([message for message, kind in notices if kind == 'negative'], notices)
                    self.assertEqual(MEMBERS[0], updated.state.confirmed_work['primary_provider']['provider_id'])
                    self.assertIsNone(updated.state.to_dict().get('pending_tv_selection'))
                    self.assertEqual(before['scope'], updated.task['task_source_scope'])
                    self.assertEqual(before['state'].content_selection.to_dict()['excluded_sources'],
                                     updated.state.content_selection.to_dict()['excluded_sources'])
                    self.assertEqual(before['plan'].subtitle_override_rules, updated.plan.subtitle_override_rules)
                    self.assertLessEqual({i.code for i in PlanVerifier().verify(updated.plan).issues}, set())
                    checked = runtime.review.plan_store.load(updated.task_id)
                    self.assertFalse(PlanVerifier().verify(checked).issues)
                    validate_plan_projection_receipt(updated.plan)
                    self.assertNotEqual(PlanStore.fingerprint(result.plan), PlanStore.fingerprint(updated.plan))
                    self.assertFalse(runtime.review.load_or_create(updated.plan).approved_entry_ids)
                    runtime.assert_sources_unchanged(self)
                    self.assertFalse(any(e.target_path.exists() for e in updated.plan.entries))
                finally:
                    dialog.delete()

    async def test_public_boundary_rejects_forgery_disconnect_and_missing_pair_without_writes(self):
        from src.rename import initial_tv_completion as tv
        from src.rename import tv_boundary_completion as boundary
        from src.rename.projection_authority import ProjectionCommitService
        with tempfile.TemporaryDirectory() as directory:
            async with IncompleteMetadataFlow(Path(directory)) as runtime:
                result, _ = await self.pending(runtime, MEMBERS[0])
                service = runtime.review
                service.root_snapshot_source = runtime.manager.root_snapshot_source
                command = self.command(result)
                intent = service.prepare_tv_boundary_completion(result.task_id, PlanStore.fingerprint(result.plan), review_command=command)
                frozen = self.persisted(runtime)
                bad = replace(intent, inputs=replace(intent.inputs, seasons=replace(intent.inputs.seasons, available=(99,))))
                with self.assertRaises(ValueError):
                    service.commit_tv_boundary_completion(bad)
                self.assertEqual(frozen, self.persisted(runtime))
                for target in ('build_initial_tv_completion', 'validate_commit'):
                    with self.subTest(disconnect=target), patch.object(tv, target, return_value=None):
                        with self.assertRaises((ValueError, RuntimeError)):
                            service.commit_tv_boundary_completion(intent)
                    self.assertEqual(frozen, self.persisted(runtime))
                original = ProjectionCommitService.commit
                def altered(transaction, envelope, **kwargs):
                    return original(transaction, replace(envelope, cohort_authority=None), **kwargs)
                with patch.object(ProjectionCommitService, 'commit', altered):
                    with self.assertRaises((ValueError, RuntimeError)):
                        service.commit_tv_boundary_completion(intent)
                self.assertEqual(frozen, self.persisted(runtime))
                updated = service.commit_tv_boundary_completion(intent)
                final = runtime.result(result.task_id)
                self.assertEqual('succeeded', current_provider_selection_context(final.task)['lifecycle'])
                self.assertTrue(list(runtime.manager.projection_commit_service.cohort_authority_store.root.glob('*.json')))
                self.assertEqual(set(), {i.code for i in PlanVerifier().verify(updated).issues})
                after = self.persisted(runtime)
                with self.assertRaises((ValueError, RuntimeError)):
                    service.commit_tv_boundary_completion(intent)
                self.assertEqual(after, self.persisted(runtime))

    async def test_pending_cannot_be_cleared_by_direct_or_generic_writers_and_cancel_preserves_choices(self):
        from src.rename.projection_authority import commit_projected_plan, persist_plan_revision
        with tempfile.TemporaryDirectory() as directory:
            async with IncompleteMetadataFlow(Path(directory)) as runtime:
                result, before = await self.pending(runtime, MEMBERS[1], edit=True)
                service = runtime.review
                service.root_snapshot_source = runtime.manager.root_snapshot_source
                fingerprint = PlanStore.fingerprint(result.plan)
                command = self.command(result)
                frozen = self.persisted(runtime)
                state = result.state
                for operation in (
                    lambda: service.decision_state_store.save(replace(state, pending_tv_selection=None), expected_revision=state.revision),
                    lambda: persist_plan_revision(service.decision_state_store, service.plan_store, result.plan),
                    lambda: commit_projected_plan(service.decision_state_store, service.plan_store, previous=state,
                        candidate=replace(state, pending_tv_selection=None), draft_plan=result.plan),
                ):
                    with self.assertRaises((ValueError, RuntimeError)):
                        operation()
                    self.assertEqual(frozen, self.persisted(runtime))
                with self.assertRaises((ValueError, RuntimeError)):
                    await runtime.manager.enqueue_reidentify(result.task_id,
                        dict(state.pending_tv_selection.to_dict()['outcome']['candidate']), state.revision)
                self.assertEqual(frozen, self.persisted(runtime))
                request_id = state.pending_tv_selection.to_dict()['request_id']
                plan = service.cancel_tv_boundary_completion(result.task_id, fingerprint, request_id=request_id)
                final = runtime.result(result.task_id)
                self.assertEqual('cancelled', current_provider_selection_context(final.task)['lifecycle'])
                self.assertIsNone(final.state.pending_tv_selection)
                self.assertEqual(before['state'].content_selection, final.state.content_selection)
                self.assertEqual(before['plan'].subtitle_override_rules, plan.subtitle_override_rules)
                after = self.persisted(runtime)
                with self.assertRaises((ValueError, RuntimeError)):
                    service.prepare_tv_boundary_completion(result.task_id, fingerprint, review_command=command)
                self.assertEqual(after, self.persisted(runtime))

    async def test_first_middle_and_separate_commands_publish_actual_terminal(self):
        for subject, middle, kind in ((MEMBERS[0], False, 'confirm_cohort'),
                                      (MEMBERS[1], True, 'confirm_cohort'),
                                      (MEMBERS[0], False, 'separate_work')):
            with self.subTest(subject=subject, kind=kind), tempfile.TemporaryDirectory() as directory:
                async with IncompleteMetadataFlow(Path(directory), anchor_middle=middle) as runtime:
                    result, _ = await self.pending(runtime, subject)
                    service = runtime.review
                    service.root_snapshot_source = runtime.manager.root_snapshot_source
                    intent = service.prepare_tv_boundary_completion(result.task_id, PlanStore.fingerprint(result.plan),
                        review_command=self.command(result, kind))
                    updated = service.commit_tv_boundary_completion(intent)
                    final = runtime.result(result.task_id)
                    self.assertEqual('succeeded', current_provider_selection_context(final.task)['lifecycle'])
                    self.assertEqual(subject, final.state.confirmed_work['primary_provider']['provider_id'])
                    self.assertEqual(2 if middle else 1, final.state.season_binding['season_number'])
                    self.assertIsNone(final.state.pending_tv_selection)
                    self.assertEqual(set(), {i.code for i in PlanVerifier().verify(updated).issues})
                    authority = list(runtime.manager.projection_commit_service.cohort_authority_store.root.glob('*.json'))
                    self.assertEqual(kind == 'confirm_cohort', bool(authority))

    async def test_full_boundary_journal_recovery_restores_parent_and_cohort_in_fresh_instance(self):
        from src.rename.projection_authority import ProjectionCommitService
        for phase in ('prepared', 'decision_written', 'ownership_claimed', 'plan_written', 'task_terminal_written'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                async with IncompleteMetadataFlow(Path(directory)) as runtime:
                    result, _ = await self.pending(runtime, MEMBERS[0])
                    service = runtime.review
                    service.root_snapshot_source = runtime.manager.root_snapshot_source
                    intent = service.prepare_tv_boundary_completion(result.task_id, PlanStore.fingerprint(result.plan),
                        review_command=self.command(result))
                    before = self.persisted(runtime)
                    reached = []
                    def crash(transaction, actual):
                        if actual == phase:
                            reached.append(actual)
                            raise SystemExit('isolated cohort interruption')
                    with patch.object(ProjectionCommitService, '_checkpoint', crash):
                        with self.assertRaises(SystemExit):
                            service.commit_tv_boundary_completion(intent)
                    self.assertEqual([phase], reached)
                    fresh = ProjectionCommitService(service.decision_state_store, service.plan_store, task_root=service.task_root)
                    self.assertEqual({result.task_id: 'failed_closed'}, fresh.recover_pending())
                    after = self.persisted(runtime)
                    self.assertEqual(before, {key: after.get(key) for key in before})
                    self.assertTrue(all(Path(key).is_relative_to(runtime.plans/'history') for key in after.keys() - before.keys()))
                    self.assertEqual('awaiting_user', current_provider_selection_context(runtime.result(result.task_id).task)['lifecycle'])

    async def test_later_task_and_root_drift_preserve_latest_bytes(self):
        from src.rename.initial_movie_completion import RootSnapshotSource
        from src.utils.utils import replace_task_record_at_if_sha256, task_record_snapshot_at
        with tempfile.TemporaryDirectory() as directory:
            async with IncompleteMetadataFlow(Path(directory)) as runtime:
                result, _ = await self.pending(runtime, MEMBERS[0])
                service = runtime.review
                roots = runtime.manager.root_snapshot_source
                service.root_snapshot_source = roots
                intent = service.prepare_tv_boundary_completion(result.task_id, PlanStore.fingerprint(result.plan),
                    review_command=self.command(result))
                before = self.persisted(runtime)
                changed = replace(roots.observe(), anime_path=runtime.root/'new-animation-root')
                with patch.object(service, 'root_snapshot_source', RootSnapshotSource(lambda: changed)):
                    with self.assertRaises((ValueError, RuntimeError)):
                        service.commit_tv_boundary_completion(intent)
                self.assertEqual(before, self.persisted(runtime))
                _raw, task, fingerprint = task_record_snapshot_at(service.task_root, result.task_id)
                task['later_user_note'] = 'disposable later edit must survive'
                replace_task_record_at_if_sha256(service.task_root, result.task_id, expected_sha256=fingerprint, data=task)
                latest = self.persisted(runtime)
                with self.assertRaises((ValueError, RuntimeError)):
                    service.commit_tv_boundary_completion(intent)
                self.assertEqual(latest, self.persisted(runtime))

    async def test_task_cas_conflict_after_cohort_publication_restores_only_owned_cohort(self):
        from src.rename.projection_authority import ProjectionCommitService
        from src.utils.utils import task_record_snapshot_at, replace_task_record_at_if_sha256
        with tempfile.TemporaryDirectory() as directory:
            async with IncompleteMetadataFlow(Path(directory)) as runtime:
                result, _ = await self.pending(runtime, MEMBERS[0])
                service = runtime.review
                service.root_snapshot_source = runtime.manager.root_snapshot_source
                intent = service.prepare_tv_boundary_completion(result.task_id, PlanStore.fingerprint(result.plan),
                    review_command=self.command(result))
                reached = []
                expected_task = []
                def race(transaction, phase):
                    if phase == 'decision_written':
                        self.assertTrue(list(transaction.cohort_authority_store.root.glob('*.json')))
                        reached.append(phase)
                        _raw, task, fingerprint = task_record_snapshot_at(service.task_root, result.task_id)
                        task['later_user_note'] = 'independent task edit after cohort publication'
                        expected_task.append(replace_task_record_at_if_sha256(service.task_root, result.task_id,
                            expected_sha256=fingerprint, data=task))
                with patch.object(ProjectionCommitService, '_checkpoint', race):
                    with self.assertRaises((ValueError, RuntimeError)):
                        service.commit_tv_boundary_completion(intent)
                self.assertEqual(['decision_written'], reached)
                self.assertEqual(expected_task[0], task_record_snapshot_at(service.task_root, result.task_id)[2])
                self.assertFalse(list(runtime.manager.projection_commit_service.cohort_authority_store.root.glob('*.json')))

    async def test_pending_canonical_disconnect_and_journal_recovery_before_queue_terminal(self):
        from src.rename import pending_tv_selection as pending
        from src.rename.projection_authority import ProjectionCommitService
        with tempfile.TemporaryDirectory() as directory:
            async with IncompleteMetadataFlow(Path(directory)) as runtime:
                forward = pending.commit
                reached = []
                def probe(intent, terminal):
                    before = self.persisted(runtime)
                    forged = pending.PendingTVSelection.from_mapping({**intent.proof.to_dict(), 'basis_fingerprint': '0' * 64})
                    with self.assertRaises((ValueError, RuntimeError)):
                        forward(replace(intent, proof=forged), terminal)
                    self.assertEqual(before, self.persisted(runtime))
                    original_commit = ProjectionCommitService.commit
                    for missing in ('intent', 'context'):
                        def missing_transport(transaction, envelope, **kwargs):
                            if missing == 'intent':
                                envelope = replace(envelope, pending_tv_selection=None)
                            else:
                                transaction.pending_tv_selection_context = None
                            return original_commit(transaction, envelope, **kwargs)
                        with patch.object(ProjectionCommitService, 'commit', missing_transport):
                            with self.assertRaises((ValueError, RuntimeError)):
                                forward(intent, terminal)
                        self.assertEqual(before, self.persisted(runtime))
                    with patch.object(pending, 'validate_commit', return_value=None):
                        with self.assertRaises((ValueError, RuntimeError)):
                            forward(intent, terminal)
                    self.assertEqual(before, self.persisted(runtime))
                    with patch.object(pending, 'preview_target', return_value=None):
                        with self.assertRaises((ValueError, RuntimeError)):
                            forward(intent, terminal)
                    after_preview_failure = self.persisted(runtime)
                    self.assertEqual(before, {key: after_preview_failure.get(key) for key in before})
                    self.assertTrue(all(Path(key).is_relative_to(runtime.plans/'history')
                                        for key in after_preview_failure.keys() - before.keys()))
                    for phase in ('prepared', 'plan_written', 'task_terminal_written'):
                        def crash(transaction, actual):
                            if actual == phase:
                                reached.append(phase)
                                raise SystemExit('isolated pending crash')
                        with patch.object(ProjectionCommitService, '_checkpoint', crash):
                            with self.assertRaises(SystemExit):
                                forward(intent, terminal)
                        fresh = ProjectionCommitService(runtime.manager.decision_state_store, runtime.manager.plan_store,
                                                        task_root=runtime.tasks)
                        self.assertEqual({terminal.task_id: 'failed_closed'}, fresh.recover_pending())
                        after = self.persisted(runtime)
                        self.assertEqual(before, {key: after.get(key) for key in before})
                        self.assertTrue(all(Path(key).is_relative_to(runtime.plans/'history') for key in after.keys() - before.keys()))
                    return forward(intent, terminal)
                with patch.object(pending, 'commit', probe):
                    await self.pending(runtime, MEMBERS[0], edit=True)
                self.assertEqual(['prepared', 'plan_written', 'task_terminal_written'], reached)

    async def test_newer_cohort_publication_is_preserved_when_older_transaction_fails(self):
        from src.rename.projection_authority import ProjectionCommitService
        from src.rename.successor_authority import CohortAuthority
        from src.utils.utils import task_record_snapshot_at, replace_task_record_at_if_sha256
        with tempfile.TemporaryDirectory() as directory:
            async with IncompleteMetadataFlow(Path(directory)) as runtime:
                result, _ = await self.pending(runtime, MEMBERS[0])
                service = runtime.review
                service.root_snapshot_source = runtime.manager.root_snapshot_source
                intent = service.prepare_tv_boundary_completion(result.task_id, PlanStore.fingerprint(result.plan),
                    review_command=self.command(result))
                newer = []
                def race(transaction, phase):
                    if phase != 'decision_written':
                        return
                    store = transaction.cohort_authority_store
                    import json
                    path = next(store.root.glob('*.json'))
                    current = CohortAuthority.from_dict(json.loads(path.read_text('utf-8')))
                    values = dict(current.__dict__)
                    values.pop('authority_fingerprint')
                    values['authority_revision'] += 1
                    actual = store.publish_prepared(CohortAuthority.create(**values),
                        expected_previous_fingerprint=current.authority_fingerprint)
                    newer.append((path, hashlib.sha256(path.read_bytes()).hexdigest(), actual.authority_fingerprint))
                    _raw, task, fp = task_record_snapshot_at(service.task_root, result.task_id)
                    task['later_user_note'] = 'later task and independently published cohort'
                    replace_task_record_at_if_sha256(service.task_root, result.task_id, expected_sha256=fp, data=task)
                with patch.object(ProjectionCommitService, '_checkpoint', race):
                    with self.assertRaises((ValueError, RuntimeError)):
                        service.commit_tv_boundary_completion(intent)
                self.assertEqual(1, len(newer))
                self.assertEqual(newer[0][1], hashlib.sha256(newer[0][0].read_bytes()).hexdigest())
                fresh = ProjectionCommitService(service.decision_state_store, service.plan_store, task_root=service.task_root)
                with self.assertRaises((ValueError, RuntimeError)):
                    fresh.recover_pending()
                self.assertEqual(newer[0][1], hashlib.sha256(newer[0][0].read_bytes()).hexdigest())

    async def test_source_and_catalog_drift_reject_without_changing_authority(self):
        for mode in ('source', 'provider_catalog'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                async with IncompleteMetadataFlow(Path(directory)) as runtime:
                    result, _ = await self.pending(runtime, MEMBERS[0])
                    service = runtime.review
                    service.root_snapshot_source = runtime.manager.root_snapshot_source
                    intent = service.prepare_tv_boundary_completion(result.task_id, PlanStore.fingerprint(result.plan),
                        review_command=self.command(result))
                    if mode == 'source':
                        path = next(e.source_path for e in result.plan.entries if e.media_kind == 'video')
                        path.write_bytes(path.read_bytes() + b'isolated changed media')
                    else:
                        catalog = service.provider_link_catalog.root
                        catalog.mkdir(parents=True, exist_ok=True)
                        (catalog/'provider-link-later-invalid-record.json').write_text('{}', encoding='utf-8')
                    before = self.persisted(runtime)
                    with self.assertRaises((ValueError, RuntimeError)):
                        service.commit_tv_boundary_completion(intent)
                    self.assertEqual(before, self.persisted(runtime))

    async def test_gui_close_and_cancel_waiter_do_not_cancel_or_adopt_late_commit(self):
        import threading
        from src.pages import plan_review_page as page
        with tempfile.TemporaryDirectory() as directory:
            async with IncompleteMetadataFlow(Path(directory)) as runtime:
                result, _ = await self.pending(runtime, MEMBERS[0])
                runtime.review.root_snapshot_source = runtime.manager.root_snapshot_source
                command = next(action for action in build_review_operation_presentation(result.task).actions
                               if action.payload and action.payload.get('kind') == 'confirm_cohort')
                client = Client(nice_page('/isolated-cohort-late'), request=None)
                self.addCleanup(client.delete)
                driver = gui_driver.GuiDriver('runTest')
                driver.client = client
                started, release = threading.Event(), threading.Event()
                # Only a started writer must drain; read-only preparation is tested separately.
                original = runtime.review.commit_tv_boundary_completion
                def waiting(*args, **kwargs):
                    started.set()
                    if not release.wait(5):
                        raise ValueError('isolated GUI release timeout')
                    return original(*args, **kwargs)
                notices = []
                with patch.object(page, 'PlanReviewDialog', RealPlanReviewDialog), patch.object(
                        page, 'PlanReviewService', lambda *_a, **_kw: runtime.review), patch.object(
                        page, 'notify', lambda message, **kw: notices.append(str(message))), patch.object(
                        runtime.review, 'commit_tv_boundary_completion', waiting):
                    with client:
                        dialog = RealPlanReviewDialog(result.plan, result.task)
                        dialog.open()
                    click = asyncio.create_task(driver.click(command.label))
                    try:
                        self.assertTrue(await asyncio.to_thread(started.wait, 5))
                        self.assertIsNotNone(dialog._tv_content_worker)
                        dialog.close()
                        click.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await click
                        notices_before = list(notices)
                        worker = dialog._tv_content_worker
                        release.set()
                        await asyncio.wait_for(asyncio.shield(worker), 10)
                        await asyncio.sleep(0)
                        self.assertEqual(notices_before, notices)
                        self.assertEqual(PlanStore.fingerprint(result.plan), PlanStore.fingerprint(dialog.plan))
                        final = runtime.result(result.task_id)
                        self.assertEqual('succeeded', current_provider_selection_context(final.task)['lifecycle'])
                        self.assertIsNone(final.state.pending_tv_selection)
                        self.assertIsNone(dialog._tv_content_worker)
                    finally:
                        release.set()
                        worker = getattr(dialog, '_tv_content_worker', None)
                        if worker is not None:
                            await asyncio.wait_for(asyncio.shield(worker), 10)
                        dialog.delete()

    async def test_registered_cancel_button_preserves_manual_subtitle_and_content(self):
        from src.pages import plan_review_page as page
        with tempfile.TemporaryDirectory() as directory:
            async with IncompleteMetadataFlow(Path(directory)) as runtime:
                result, before = await self.pending(runtime, MEMBERS[0], edit=True)
                runtime.review.root_snapshot_source = runtime.manager.root_snapshot_source
                client = Client(nice_page('/isolated-cohort-cancel'), request=None)
                self.addCleanup(client.delete)
                driver = gui_driver.GuiDriver('runTest')
                driver.client = client
                notices = []
                with patch.object(page, 'PlanReviewDialog', RealPlanReviewDialog), patch.object(
                        page, 'PlanReviewService', lambda *_a, **_kw: runtime.review), patch.object(
                        page, 'notify', lambda message, **kw: notices.append((str(message), kw.get('type')))):
                    with client:
                        dialog = RealPlanReviewDialog(result.plan, result.task)
                        dialog.open()
                    try:
                        await driver.click('取消本次季度关系确认')
                        self.assertFalse([text for text, kind in notices if kind == 'negative'], notices)
                        final = runtime.result(result.task_id)
                        self.assertEqual('cancelled', current_provider_selection_context(final.task)['lifecycle'])
                        self.assertIsNone(final.state.pending_tv_selection)
                        self.assertEqual(before['state'].content_selection, final.state.content_selection)
                        self.assertEqual(before['plan'].subtitle_override_rules, final.plan.subtitle_override_rules)
                    finally:
                        # Cancellation can adopt a revision and schedule font work too.
                        dialog.delete()

    async def test_pending_preview_uses_current_frozen_roots_without_rewriting_historical_snapshot(self):
        from src.utils.utils import task_record_snapshot_at, replace_task_record_at_if_sha256
        from tests.test_provider_subject_plan_wiring_red_contract import _root_config_patch
        for mode in ('missing_old_snapshot', 'non_animation_root_changed'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                async with IncompleteMetadataFlow(Path(directory)) as runtime:
                    original = runtime.select
                    expected = {}
                    async def select(task_id, subject, *, confirm=True):
                        _raw, task, fp = task_record_snapshot_at(runtime.tasks, task_id)
                        roots = runtime.manager.root_snapshot_source.observe()
                        if mode == 'missing_old_snapshot':
                            task.pop('draft_root_snapshot', None)
                            task.pop('draft_root_config_fingerprint', None)
                            replace_task_record_at_if_sha256(runtime.tasks, task_id, expected_sha256=fp, data=task)
                        else:
                            roots = replace(roots, movie_path=runtime.root/'new-general-movies')
                        expected.update(roots=roots, old_roots=task.get('draft_root_snapshot'),
                                        old_fp=task.get('draft_root_config_fingerprint'))
                        with _root_config_patch(roots.to_dict()):
                            return await original(task_id, subject, confirm=False)
                    runtime.select = select
                    runtime.source_factory = compact_sources
                    result = await runtime.add(MEMBERS[0], confirm=False)
                    request = current_provider_selection_context(result.task)
                    self.assertEqual('awaiting_user', request.get('lifecycle'), result.task.get('error'))
                    proof = result.state.pending_tv_selection.to_dict()
                    self.assertEqual(expected['roots'].to_dict(), proof['roots'])
                    preview = request['provider_subject_cohort_preview']['request']
                    self.assertEqual(proof['roots_fingerprint'], preview['target_root_config_fingerprint'])
                    self.assertEqual(expected['old_roots'], result.task.get('draft_root_snapshot'))
                    self.assertEqual(expected['old_fp'], result.task.get('draft_root_config_fingerprint'))
                    runtime.review.root_snapshot_source = runtime.manager.root_snapshot_source
                    with _root_config_patch(expected['roots'].to_dict()):
                        intent = runtime.review.prepare_tv_boundary_completion(result.task_id, PlanStore.fingerprint(result.plan),
                            review_command=self.command(result))
                        completed = runtime.review.commit_tv_boundary_completion(intent)
                    self.assertEqual(set(), {i.code for i in PlanVerifier().verify(completed).issues})
                    final = runtime.result(result.task_id)
                    self.assertEqual('succeeded', current_provider_selection_context(final.task)['lifecycle'])
                    self.assertEqual(expected['old_roots'], final.task.get('draft_root_snapshot'))
                    self.assertEqual(expected['old_fp'], final.task.get('draft_root_config_fingerprint'))
