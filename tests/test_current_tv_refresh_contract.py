"""Current TV refresh through real admission/worker and owned media fixtures.

Incident-shaped RED: no synthetic executable Plan or fabricated Decision.
Provider transport is frozen only for creating the original actual TV Plan.
"""
import asyncio
import json
import unittest
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.rename.plan_execution import PlanExecutionError
from src.rename.plan_review import PlanReviewError, entry_id
from src.utils import utils
from tests import test_default_activation_contract as activation_fixture
from tests import test_workspace_retry_atomic as workspace_fixture


class CurrentTVRefreshContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = activation_fixture.DefaultActivationProducerContracts()


    async def failed_before_media(self, flow):
        from tests._media_flow_helpers import MediaFlow
        from src.rename.execution_recovery import requires_execution_replan
        plan = flow.store.load(flow.task_id)
        target = next(e.target_path for e in plan.entries if e.media_kind == 'video')
        self.assertTrue(target.is_relative_to(flow.root))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'owned prewrite obstruction')
        task, ledger, service = await MediaFlow()._approve_and_execute(
            root=flow.root, result=SimpleNamespace(plan=plan, task=flow.manager._read(flow.task_id)))
        self.assertTrue(requires_execution_replan(task), task.get('error'))
        self.assertEqual({}, ledger)
        target.unlink()  # Exact file created above, no media hardlink.
        flow.manager.execution_service = service
        flow.manager.review_root = service.review_service.review_root
        return plan, service

    async def test_retry_issues_same_work_not_new_recognition(self):
        from src.rename import recognition_queue_admission as admission
        async with self.fixture.flow() as flow:
            with patch.object(admission, '_accept_on_owner', wraps=admission._accept_on_owner) as accepted:
                await flow.manager.retry_current(flow.task_id)
                self.assertEqual(1, accepted.call_count)
                job = accepted.call_args.args[1]
            self.assertEqual('refresh_current_tv', job.operation)
            self.assertIsNone(job.recognition_origin, 'must not forge the first automatic job')

    async def test_actual_worker_refreshes_without_constructing_a_new_recognizer(self):
        async with self.fixture.flow() as flow:
            old = flow.store.load(flow.task_id)
            state = flow.service.decision_state_store.load(flow.task_id)
            forbidden = Mock(side_effect=AssertionError('same work refresh entered recognition'))
            with patch('src.task_queue.Rename', forbidden):
                await flow.manager.retry_current(flow.task_id)
                await asyncio.wait_for(flow.manager._queue.join(), 8)
            current = flow.store.load(flow.task_id)
            self.assertNotEqual(flow.store.fingerprint(old), flow.store.fingerprint(current),
                                flow.manager._read(flow.task_id).get('error'))
            forbidden.assert_not_called()
            after = flow.service.decision_state_store.load(flow.task_id)
            self.assertEqual(state.confirmed_work, after.confirmed_work)
            self.assertEqual(state.season_binding, after.season_binding)
            self.assertEqual(state.content_selection.to_dict()['excluded_sources'],
                             after.content_selection.to_dict()['excluded_sources'])
            self.assertEqual(state.initial_source_basis, after.initial_source_basis)
            from src.rename.plan_verifier import PlanVerifier
            self.assertEqual(set(), {i.code for i in PlanVerifier().verify(current).issues})
            self.assertEqual('need_confirm', flow.manager._read(flow.task_id)['workflow_status'])

    async def test_pending_old_plan_denied_then_actual_new_plan_can_be_approved(self):
        async with self.fixture.flow() as flow:
            old = flow.store.load(flow.task_id)
            fingerprint = flow.store.fingerprint(old)
            await flow.manager.retry_current(flow.task_id)
            with self.assertRaises(PlanReviewError):
                flow.service.approve_entries(flow.task_id, fingerprint, [entry_id(e) for e in old.entries])
            await asyncio.wait_for(flow.manager._queue.join(), 12)
            current = flow.store.load(flow.task_id)
            current = current
            new_fingerprint = flow.store.fingerprint(current)
            self.assertNotEqual(fingerprint, new_fingerprint)
            decision = flow.service.approve_entries(flow.task_id, new_fingerprint,
                [entry_id(e) for e in current.entries])
            self.assertEqual(new_fingerprint, decision.plan_fingerprint)
            # An ordinary subsequent public semantic revision inherits the receipt.
            state = flow.service.decision_state_store.load(flow.task_id)
            video = next(e for e in current.entries if e.media_kind == 'video')
            revised = flow.service.revise_tv_content(flow.task_id, new_fingerprint,
                expected_revision=state.revision, mode='role',
                edit={'entry_id': entry_id(video), 'role': 'main_candidate'})
            revised = revised
            flow.service.approve_entries(flow.task_id, flow.store.fingerprint(revised),
                [entry_id(e) for e in revised.entries])

    async def test_copied_job_cannot_consume_original_pending_or_receipt(self):
        from src.rename.current_tv_refresh import drive
        async with self.fixture.flow() as flow:
            release = asyncio.Event()
            async def held(manager, job):
                await release.wait()
                return await drive(manager, job)
            with patch('src.rename.current_tv_refresh.drive', held):
                try:
                    await flow.manager.retry_current(flow.task_id)
                    original = next(iter(flow.manager._tv_refresh_accepted.values()))
                    before = workspace_fixture.snapshot(flow.root)
                    await drive(flow.manager, replace(original))
                    self.assertEqual(before, workspace_fixture.snapshot(flow.root))
                    self.assertIs(original, flow.manager._tv_refresh_accepted[original.request_id])
                    self.assertIn(flow.task_id, flow.manager._pending)
                finally:
                    release.set()
                await asyncio.wait_for(flow.manager._queue.join(), 12)
            self.assertEqual('completed', flow.manager._read(flow.task_id)['current_tv_refresh']['status'])

    async def test_plan_only_and_pure_revision_cannot_add_remove_or_replace_refresh_marker(self):
        from src.rename.projection_authority import (persist_plan_revision, ProjectionCommitService,
                                                     FinalPlanProjector, validate_plan_projection_receipt)
        async with self.fixture.flow() as flow:
            for operation in ('add', 'remove', 'replace'):
                if operation == 'remove':
                    await flow.manager.retry_current(flow.task_id)
                    await asyncio.wait_for(flow.manager._queue.join(), 12)
                old = flow.store.load(flow.task_id)
                metadata = dict(old.metadata)
                if operation == 'remove':
                    metadata.pop('current_tv_refresh')
                else:
                    metadata['current_tv_refresh'] = dict(request_fingerprint='1' * 64,
                                                         parent_plan_fingerprint=flow.store.fingerprint(old))
                draft = replace(old, metadata=metadata)
                before = workspace_fixture.snapshot(flow.root)
                for route in ('pure', 'public_terminal', 'actual_root_without_terminal'):
                    with self.subTest(operation=operation, route=route):
                        with self.assertRaises((ValueError, RuntimeError)):
                            if route == 'pure':
                                FinalPlanProjector.revise(old, draft)
                            elif route == 'public_terminal':
                                persist_plan_revision(flow.service.decision_state_store, flow.store, draft,
                                    task_terminal=flow.service._revision_task_terminal(flow.task_id))
                            else:
                                ProjectionCommitService(flow.service.decision_state_store, flow.store,
                                    task_root=flow.manager.task_root).commit_revision(
                                        flow.service.decision_state_store.load(flow.task_id), draft)
                        self.assertEqual(before, workspace_fixture.snapshot(flow.root))
                with self.assertRaises((ValueError, RuntimeError)):
                    validate_plan_projection_receipt(draft)
            # A real unchanged-marker Plan-only revision remains legal.
            old = flow.store.load(flow.task_id)
            revised = persist_plan_revision(flow.service.decision_state_store, flow.store,
                replace(old, revision_reason='owned unchanged refresh marker'),
                task_terminal=flow.service._revision_task_terminal(flow.task_id))
            self.assertEqual(old.metadata['current_tv_refresh'], revised.metadata['current_tv_refresh'])
            validate_plan_projection_receipt(revised)
            revised = revised
            flow.service.approve_entries(flow.task_id, flow.store.fingerprint(revised),
                [entry_id(e) for e in revised.entries])

    async def test_orphan_completed_marker_cannot_pass_either_public_consumer(self):
        async with self.fixture.flow() as flow:
            await flow.manager.retry_current(flow.task_id)
            await asyncio.wait_for(flow.manager._queue.join(), 12)
            current = flow.store.load(flow.task_id)
            fingerprint = flow.store.fingerprint(current)
            flow.service.approve_entries(flow.task_id, fingerprint, [entry_id(e) for e in current.entries])
            flow.manager.execution_service.review_service = flow.service
            # Actual matching approval positive control, and no new media scan
            # in the shared authority proof used by font-only repair.
            from tests.test_inventory_reuse import source_access
            with source_access(current.source_root):
                flow.manager.execution_service.validate_approved_authority(flow.task_id, fingerprint)
            task = flow.manager._read(flow.task_id)
            task.pop('current_tv_refresh')
            utils.write_task_at(flow.manager.task_root, flow.task_id, task)
            with self.assertRaises(PlanReviewError):
                flow.service.approve_entries(flow.task_id, fingerprint, [entry_id(e) for e in current.entries])
            with self.assertRaises(PlanExecutionError):
                flow.manager.execution_service.validate_approved_authority(flow.task_id, fingerprint)

    async def test_fresh_manager_finishes_same_retirement_witness_after_review_failure(self):
        from src.rename.current_plan_retirement import _path
        from src.task_queue import TaskQueueManager
        async with self.fixture.flow() as flow:
            plan, execution = await self.failed_before_media(flow)
            fingerprint = flow.store.fingerprint(plan)
            with patch('src.task_queue.PlanReviewService.delete', side_effect=OSError('owned Review fault')):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.replan_failed_execution(flow.task_id, fingerprint)
            original = _path(flow.store.root, flow.task_id, fingerprint).read_bytes()
            await flow.manager.stop()
            fresh = TaskQueueManager(task_root=flow.manager.task_root, plan_root=flow.store.root,
                decision_state_root=flow.service.decision_state_store.root,
                provider_link_catalog_root=flow.manager.provider_link_catalog_root,
                execution_service=execution, recovery_abandon_audit_root=flow.root / 'abandon')
            fresh.review_root = execution.review_service.review_root
            fresh.root_snapshot_source = flow.manager.root_snapshot_source
            try:
                await fresh.start()
                await fresh.replan_failed_execution(flow.task_id, fingerprint)
                await asyncio.wait_for(fresh._queue.join(), 12)
                self.assertEqual(original, _path(flow.store.root, flow.task_id, fingerprint).read_bytes())
                self.assertEqual('completed', fresh._read(flow.task_id)['current_tv_refresh']['status'])
                self.assertEqual(1, len(fresh._read(flow.task_id)['execution_replan_history']))
            finally:
                await fresh.stop()

    async def test_wrong_root_fingerprint_and_linked_witness_are_preserved_and_denied(self):
        import os
        from src.rename import current_plan_retirement as retirement
        async with self.fixture.flow() as flow:
            plan, execution = await self.failed_before_media(flow)
            fingerprint = flow.store.fingerprint(plan)
            with patch('src.task_queue.PlanReviewService.delete', side_effect=OSError('owned Review fault')):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.replan_failed_execution(flow.task_id, fingerprint)
            path = retirement._path(flow.store.root, flow.task_id, fingerprint)
            original = path.read_bytes()
            for corrupt in ('roots_fingerprint', 'plan_fingerprint', 'link'):
                with self.subTest(corrupt=corrupt):
                    alias = None
                    if corrupt == 'link':
                        alias = path.with_name('owned-extra-link.json')
                        os.link(path, alias)
                    else:
                        value = json.loads(original)
                        value[corrupt] = 'b' * 64
                        if corrupt == 'plan_fingerprint':
                            value['execution_requested_fingerprint'] = 'b' * 64
                        path.write_text(retirement._encode(value), encoding='utf-8')
                    before = workspace_fixture.snapshot(flow.root)
                    with self.assertRaises(PlanExecutionError):
                        await flow.manager.replan_failed_execution(flow.task_id, fingerprint)
                    with self.assertRaises(PlanReviewError):
                        execution.review_service.approve_entries(flow.task_id, fingerprint, [entry_id(e) for e in plan.entries])
                    with self.assertRaises(PlanExecutionError):
                        execution.validate_approved_authority(flow.task_id, fingerprint)
                    self.assertEqual(before, workspace_fixture.snapshot(flow.root))
                    if alias is not None:
                        alias.unlink()  # Only this fixture's exact additional link.
                    path.write_bytes(original)

    async def test_deleting_request_does_not_restore_old_approval(self):
        from src.rename.current_tv_refresh import drive
        async with self.fixture.flow() as flow:
            old = flow.store.load(flow.task_id)
            release = asyncio.Event()
            async def held(manager, job):
                await release.wait()
                return await drive(manager, job)
            with patch('src.rename.current_tv_refresh.drive', held):
                try:
                    await flow.manager.retry_current(flow.task_id)
                    task = flow.manager._read(flow.task_id)
                    task.pop('current_tv_refresh')
                    utils.write_task_at(flow.manager.task_root, flow.task_id, task)
                    with self.assertRaises(PlanReviewError):
                        flow.service.approve_entries(flow.task_id, flow.store.fingerprint(old),
                            [entry_id(e) for e in old.entries])
                finally:
                    release.set()
                await asyncio.wait_for(flow.manager._queue.join(), 12)
            if flow.task_id in flow.manager._pending:
                flow.manager._pending.remove(flow.task_id)

    async def test_stop_waits_for_real_refresh_thread_even_when_worker_await_is_cancelled(self):
        from src.rename import current_tv_refresh as refresh
        async with self.fixture.flow() as flow:
            entered, release = threading.Event(), threading.Event()
            original = refresh._run
            def barrier(context):
                entered.set()
                if not release.wait(8):
                    raise AssertionError('owned thread barrier expired')
                return original(context)
            stop = None
            with patch.object(refresh, '_run', side_effect=barrier):
                try:
                    await flow.manager.retry_current(flow.task_id)
                    self.assertTrue(await asyncio.to_thread(entered.wait, 6))
                    worker = flow.manager._worker_task
                    worker.cancel()  # adversarial caller, not manager.stop's behavior
                    stop = asyncio.create_task(flow.manager.stop())
                    await asyncio.sleep(0)
                    self.assertFalse(stop.done())
                    self.assertEqual(flow.task_id, flow.manager.active_task_id)
                finally:
                    release.set()
                    if stop is not None:
                        await asyncio.wait_for(stop, 12)
            self.assertEqual('completed', flow.manager._read(flow.task_id)['current_tv_refresh']['status'])
            self.assertIsNone(flow.manager.active_task_id)

    async def test_late_task_change_is_not_overwritten_by_refresh_thread(self):
        from src.rename import current_tv_refresh as refresh
        async with self.fixture.flow() as flow:
            entered, release = threading.Event(), threading.Event()
            original = refresh._run
            def barrier(context):
                entered.set()
                if not release.wait(8):
                    raise AssertionError('owned thread barrier expired')
                return original(context)
            with patch.object(refresh, '_run', side_effect=barrier):
                try:
                    await flow.manager.retry_current(flow.task_id)
                    self.assertTrue(await asyncio.to_thread(entered.wait, 6))
                    task = flow.manager._read(flow.task_id)
                    task['name'] = 'Owned later request, preserve exactly'
                    utils.write_task_at(flow.manager.task_root, flow.task_id, task)
                    before = workspace_fixture.snapshot(flow.root)
                finally:
                    release.set()
                await asyncio.wait_for(flow.manager._queue.join(), 12)
            self.assertEqual(before, workspace_fixture.snapshot(flow.root))

    async def test_real_refresh_journal_crashes_recover_in_fresh_manager_without_replay(self):
        from src.rename.projection_authority import ProjectionCommitService
        from src.task_queue import TaskQueueManager
        for phase in ('prepared', 'decision_written', 'plan_written', 'task_terminal_written'):
            with self.subTest(phase=phase):
                async with self.fixture.flow() as flow:
                    old = flow.store.load(flow.task_id)
                    state = flow.service.decision_state_store.load(flow.task_id)
                    reached = []
                    def crash(transaction, actual):
                        if actual == phase and getattr(transaction, '_current_tv_refresh_request', None) is not None:
                            reached.append(actual)
                            raise SystemExit('owned refresh crash fixture')
                    with patch.object(ProjectionCommitService, '_checkpoint', crash):
                        await flow.manager.retry_current(flow.task_id)
                        await asyncio.wait_for(flow.manager._queue.join(), 12)
                    self.assertEqual([phase], reached)
                    self.assertEqual('pending', flow.manager.projection_commit_service._read_journal(flow.task_id)['status'])
                    await flow.manager.stop()
                    fresh = TaskQueueManager(task_root=flow.manager.task_root, plan_root=flow.store.root,
                        decision_state_root=flow.service.decision_state_store.root,
                        provider_link_catalog_root=flow.manager.provider_link_catalog_root,
                        recovery_abandon_audit_root=flow.root / 'abandon')
                    fresh.root_snapshot_source = flow.manager.root_snapshot_source
                    forbidden = Mock(side_effect=AssertionError('fresh manager replayed recognizer'))
                    try:
                        with patch('src.task_queue.Rename', forbidden):
                            await fresh.start()
                            self.assertEqual({}, fresh._tv_refresh_accepted)
                            self.assertEqual(0, fresh._queue.qsize())
                            self.assertEqual(old, flow.store.load(flow.task_id))
                            self.assertEqual(state, fresh.decision_state_store.load(flow.task_id))
                            self.assertEqual('interrupted', fresh._read(flow.task_id)['current_tv_refresh']['status'])
                            self.assertEqual('failed_closed', fresh.projection_commit_service._read_journal(flow.task_id)['status'])
                            await fresh.retry_current(flow.task_id)
                            await asyncio.wait_for(fresh._queue.join(), 12)
                            self.assertEqual('completed', fresh._read(flow.task_id)['current_tv_refresh']['status'])
                            forbidden.assert_not_called()
                    finally:
                        await fresh.stop()

    async def test_completed_commit_signal_does_not_replay_or_overwrite_success(self):
        from src.rename.projection_authority import ProjectionCommitService
        async with self.fixture.flow() as flow:
            reached = []
            def signal(transaction, phase):
                if phase == 'completed' and getattr(transaction, '_current_tv_refresh_request', None) is not None:
                    reached.append(phase)
                    raise SystemExit('owned thread signal after real completion')
            with patch.object(ProjectionCommitService, '_checkpoint', signal):
                await flow.manager.retry_current(flow.task_id)
                await asyncio.wait_for(flow.manager._queue.join(), 12)
            self.assertEqual(['completed'], reached)
            task = flow.manager._read(flow.task_id)
            self.assertEqual('completed', task['current_tv_refresh']['status'])
            current = flow.store.load(flow.task_id)
            from src.rename.plan_verifier import PlanVerifier
            self.assertEqual(set(), {i.code for i in PlanVerifier().verify(current).issues})
            current = current
            self.assertEqual('need_confirm', flow.manager._read(flow.task_id)['workflow_status'])
            flow.service.approve_entries(flow.task_id, flow.store.fingerprint(current),
                                         [entry_id(e) for e in current.entries])

    async def test_withdrawn_failed_plan_cannot_be_reapproved_after_admission_refusal(self):
        async with self.fixture.flow() as flow:
            plan, service = await self.failed_before_media(flow)
            fingerprint = flow.store.fingerprint(plan)
            with patch.object(flow.manager._queue, 'put_nowait', side_effect=OSError('owned refusal')):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.replan_failed_execution(flow.task_id, fingerprint)
            self.assertEqual(fingerprint, flow.store.fingerprint(flow.store.load(flow.task_id)))
            self.assertTrue(flow.manager._read(flow.task_id).get('execution_replan_history'))
            with self.assertRaises(PlanReviewError):
                service.review_service.approve_entries(flow.task_id, fingerprint,
                    [entry_id(entry) for entry in plan.entries])

    async def test_withdrawal_survives_task_publication_fault_before_request_is_saved(self):
        async with self.fixture.flow() as flow:
            plan, service = await self.failed_before_media(flow)
            fingerprint = flow.store.fingerprint(plan)
            original = utils._atomic_replace_task_bytes
            task_path = utils.safe_task_path(flow.manager.task_root, flow.task_id)
            faults = []
            def fail_retirement(path, value):
                if path == task_path and json.loads(value).get('execution_replan_history'):
                    faults.append(True)
                    raise OSError('owned retirement Task write failure')
                return original(path, value)
            with patch.object(utils, '_atomic_replace_task_bytes', side_effect=fail_retirement):
                with self.assertRaises((OSError, PlanExecutionError)):
                    await flow.manager.replan_failed_execution(flow.task_id, fingerprint)
            self.assertTrue(faults, 'must reach actual Task write after approval withdrawal')
            with self.assertRaises(PlanReviewError):
                service.review_service.approve_entries(flow.task_id, fingerprint,
                    [entry_id(entry) for entry in plan.entries])

    async def test_completed_media_is_not_a_current_tv_refresh_source(self):
        # The existing completed-media route remains asset repair, never this operation.
        async with self.fixture.flow() as flow:
            task = flow.manager._read(flow.task_id)
            task.update(execution_status='completed', workflow_status='completed')
            utils.write_task_at(flow.manager.task_root, flow.task_id, task)
            before = workspace_fixture.snapshot(flow.root)
            with self.assertRaises(PlanExecutionError):
                await flow.manager.reenqueue_workspace(flow.task_id, **workspace_fixture.WorkspaceRetryAtomicContracts.args(task))
            self.assertEqual(before, workspace_fixture.snapshot(flow.root))

    async def test_retirement_publication_failure_preserves_task_and_existing_review(self):
        async with self.fixture.flow() as flow:
            plan, service = await self.failed_before_media(flow)
            before = workspace_fixture.snapshot(flow.root)
            with patch('src.rename.current_plan_retirement._publish', side_effect=OSError('owned witness fault')):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.replan_failed_execution(flow.task_id, flow.store.fingerprint(plan))
            self.assertEqual(before, workspace_fixture.snapshot(flow.root))
            self.assertEqual(flow.store.fingerprint(plan), service.review_service.load(flow.task_id).plan_fingerprint)

    async def test_same_witness_completes_retirement_after_task_write_failure(self):
        from src.rename.current_plan_retirement import _path
        async with self.fixture.flow() as flow:
            plan, service = await self.failed_before_media(flow)
            fingerprint = flow.store.fingerprint(plan)
            original = utils._atomic_replace_task_bytes
            task_path = utils.safe_task_path(flow.manager.task_root, flow.task_id)
            def fail_retirement(path, value):
                if path == task_path and json.loads(value).get('execution_replan_history'):
                    raise OSError('owned Task fault')
                return original(path, value)
            with patch.object(utils, '_atomic_replace_task_bytes', side_effect=fail_retirement):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.replan_failed_execution(flow.task_id, fingerprint)
            witness = _path(flow.store.root, flow.task_id, fingerprint)
            original_witness = witness.read_bytes()
            # No fabricated witness or Task: retry the same public command.
            with patch.object(flow.manager._queue, 'put_nowait', side_effect=OSError('owned queue refusal')):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.replan_failed_execution(flow.task_id, fingerprint)
            self.assertEqual(original_witness, witness.read_bytes())
            self.assertEqual(1, len(flow.manager._read(flow.task_id)['execution_replan_history']))
            with self.assertRaises(PlanReviewError):
                service.review_service.approve_entries(flow.task_id, fingerprint,
                    [entry_id(entry) for entry in plan.entries])

    async def test_corrupt_retirement_is_preserved_and_blocks_both_consumers(self):
        from src.rename.current_plan_retirement import _path
        async with self.fixture.flow() as flow:
            plan, service = await self.failed_before_media(flow)
            fingerprint = flow.store.fingerprint(plan)
            with patch('src.task_queue.PlanReviewService.delete', side_effect=OSError('owned review fault')):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.replan_failed_execution(flow.task_id, fingerprint)
            witness = _path(flow.store.root, flow.task_id, fingerprint)
            witness.write_bytes(b'{owned interrupted record')
            before = workspace_fixture.snapshot(flow.root)
            with self.assertRaises(PlanExecutionError):
                await flow.manager.replan_failed_execution(flow.task_id, fingerprint)
            with self.assertRaises(PlanReviewError):
                service.review_service.approve_entries(flow.task_id, fingerprint,
                    [entry_id(entry) for entry in plan.entries])
            with self.assertRaises(PlanExecutionError):
                service.validate_approved_authority(flow.task_id, fingerprint)
            self.assertEqual(before, workspace_fixture.snapshot(flow.root))

    async def test_existing_approval_is_not_executable_when_review_withdrawal_write_fails(self):
        async with self.fixture.flow() as flow:
            plan, service = await self.failed_before_media(flow)
            fingerprint = flow.store.fingerprint(plan)
            # A genuine existing approved record, not a fabricated approval.
            self.assertEqual(fingerprint, service.review_service.load(flow.task_id).plan_fingerprint)
            with patch('src.task_queue.PlanReviewService.delete', side_effect=OSError('owned review unlink fault')):
                with self.assertRaises((OSError, PlanExecutionError)):
                    await flow.manager.replan_failed_execution(flow.task_id, fingerprint)
            # Shared public execution-authority validation; no media execution.
            with self.assertRaises(PlanExecutionError):
                service.validate_approved_authority(flow.task_id, fingerprint)

    async def test_role_rebuild_preserves_real_gui_selected_episode_projection(self):
        from tests import test_initial_tv_completion_contract as tv_fixture
        from src.rename.plan_review import PlanReviewService
        helper = tv_fixture.InitialTVCompletionContracts()
        original = PlanReviewService.revise_tv_content
        selected = []
        def observe(service, *args, **kwargs):
            result = original(service, *args, **kwargs)
            if kwargs.get('mode') == 'episode':
                selected.append((service, result))
            return result
        try:
            with patch.object(PlanReviewService, 'revise_tv_content', autospec=True, side_effect=observe):
                await helper.test_new_content_split_cour_gui_choice_preserves_source_owners_and_rejects_stale_action()
            self.assertEqual(1, len(selected), 'must use the actual successful GUI episode selection')
            service, plan = selected[0]
            state = service.decision_state_store.load(plan.task_id)
            self.assertEqual('absolute', state.source_episode_projection_decision['decision'])
            video = next(entry for entry in plan.entries if entry.media_kind == 'video')
            revised = service.revise_tv_content(plan.task_id, service.plan_store.fingerprint(plan),
                expected_revision=state.revision, mode='role',
                edit={'entry_id': entry_id(video), 'role': 'main_candidate'})
            after = service.decision_state_store.load(plan.task_id)
            self.assertEqual(state.source_episode_projection_decision, after.source_episode_projection_decision)
            self.assertEqual(state.source_episode_projection, after.source_episode_projection)
            self.assertEqual(plan.metadata['source_episode_projection'], revised.metadata['source_episode_projection'])
            self.assertEqual(state.content_selection.associations, after.content_selection.associations)
            before = workspace_fixture.snapshot(service.plan_store.root.parent)
            from src.rename import provider_link_decision as provider_projection
            freeze = provider_projection.freeze_source_episode_projection
            def missing_candidate(*args, **kwargs):
                draft, automatic = freeze(*args, **kwargs)
                metadata = dict(draft.metadata)
                metadata.pop('source_episode_projection_candidates', None)
                return replace(draft, metadata=metadata), None
            with patch.object(provider_projection, 'freeze_source_episode_projection', side_effect=missing_candidate):
                with self.assertRaises((ValueError, RuntimeError)):
                    service.revise_tv_content(plan.task_id, service.plan_store.fingerprint(revised),
                        expected_revision=after.revision, mode='role',
                        edit={'entry_id': entry_id(video), 'role': 'main_candidate'})
            self.assertEqual(before, workspace_fixture.snapshot(service.plan_store.root.parent))
            from src.task_queue import TaskQueueManager
            manager = TaskQueueManager(task_root=service.task_root, plan_root=service.plan_store.root,
                decision_state_root=service.decision_state_store.root,
                provider_link_catalog_root=service.provider_link_catalog.root,
                recovery_abandon_audit_root=service.plan_store.root.parent / 'abandon')
            manager.root_snapshot_source = service.root_snapshot_source
            try:
                with patch('src.task_queue.Rename', side_effect=AssertionError('manual episode refresh searched again')):
                    await manager.retry_current(plan.task_id)
                    await asyncio.wait_for(manager._queue.join(), 12)
                refreshed = service.plan_store.load(plan.task_id)
                refreshed_state = service.decision_state_store.load(plan.task_id)
                self.assertEqual('completed', manager._read(plan.task_id)['current_tv_refresh']['status'])
                self.assertEqual(after.source_episode_projection_decision, refreshed_state.source_episode_projection_decision)
                self.assertEqual(after.source_episode_projection, refreshed_state.source_episode_projection)
                self.assertEqual(after.content_selection.associations, refreshed_state.content_selection.associations)
                self.assertEqual(2, len([e for e in refreshed.entries if e.media_kind == 'subtitle']))
                for subtitle in (e for e in refreshed.entries if e.media_kind == 'subtitle'):
                    owner = next(e for e in refreshed.entries if e.source_path == subtitle.subtitle.associated_video_source_path)
                    self.assertEqual((owner.season_id, owner.episode_id), (subtitle.season_id, subtitle.episode_id))
                    self.assertTrue(subtitle.target_path.name.startswith(owner.target_path.stem + '.'))
                # A changed source cannot be silently reinterpreted as a fresh choice.
                changed = next(e.source_path for e in refreshed.entries if e.media_kind == 'video')
                changed.write_bytes(changed.read_bytes() + b'owned source drift')
                before = workspace_fixture.snapshot(service.plan_store.root.parent)
                with self.assertRaises((ValueError, RuntimeError)):
                    service.revise_tv_content(plan.task_id, service.plan_store.fingerprint(refreshed),
                        expected_revision=refreshed_state.revision, mode='special',
                        edit={'entry_id': entry_id(video), 'action': 'unmatched_extra', 'provider_episode_id': None})
                self.assertEqual(before, workspace_fixture.snapshot(service.plan_store.root.parent))
            finally:
                await manager.stop()
        finally:
            helper.doCleanups()
