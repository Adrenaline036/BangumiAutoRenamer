"""O6 workspace admission: late rejection must not save half a request.

Actual queue admission and GUI callback methods; owned temporary stores and
Matroska files. Provider transport is frozen by the shared production fixture.
"""
import asyncio
import hashlib
import json
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.pages import plan_review_page as page
from src.pages.recognition_recovery_view import RecognitionRecoveryDialog
from src.rename.plan_execution import PlanExecutionError
from src.utils.utils import read_task_at, write_task_at
from tests import test_default_activation_contract as fixture_module
from tests.source_collection_fixture import admit_task
from tests.workflow_gui_fixture import hold_worker


def snapshot(root):
    result = {}
    for path in root.rglob('*'):
        if path.is_file() and path.suffix != '.lock':
            value = path.read_bytes()
            result[str(path.relative_to(root))] = (len(value), hashlib.sha256(value).hexdigest())
    return result


class WorkspaceRetryAtomicContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixture_module.DefaultActivationProducerContracts()

    async def test_requested_cancellation_is_checked_at_last_publication_boundary(self):
        from src.rename import recognition_queue_admission as admission
        from src.utils import utils
        for boundary in ('startup-swallowed', 'candidate', 'serialized'):
            with self.subTest(boundary=boundary):
                async with self.fixture.flow(admission_only=True) as flow:
                    await flow.manager.stop()
                    start, candidate, serialize = flow.manager.start, admission._candidate, utils._serialize_task_record
                    calls = []
                    async def operation():
                        async def starting():
                            await start()
                            if boundary == 'startup-swallowed':
                                asyncio.current_task().cancel()
                                try:
                                    await asyncio.sleep(0)
                                except asyncio.CancelledError:
                                    pass
                        def build(*args, **kwargs):
                            result = candidate(*args, **kwargs)
                            calls.append(True)
                            if boundary == 'candidate' and len(calls) == 2:
                                asyncio.current_task().cancel()
                            return result
                        def serialized(data):
                            value = serialize(data)
                            if boundary == 'serialized':
                                asyncio.current_task().cancel()
                            return value
                        with patch.object(flow.manager, 'start', side_effect=starting), \
                                patch.object(admission, '_candidate', side_effect=build), \
                                patch.object(utils, '_serialize_task_record', side_effect=serialized):
                            try:
                                with self.assertRaises(PlanExecutionError):
                                    await flow.manager.retry_current(flow.task_id)
                            finally:
                                # Test runner only; the product must not clear cancellation.
                                while asyncio.current_task().cancelling():
                                    asyncio.current_task().uncancel()
                    before = snapshot(flow.root)
                    task = asyncio.create_task(operation())
                    result = await asyncio.gather(task, return_exceptions=True)
                    # An undelivered cancellation can still cancel the task's return.
                    if result[0] is not None and not isinstance(result[0], asyncio.CancelledError):
                        raise result[0]
                    await flow.manager.stop()
                    self.assertEqual(before, snapshot(flow.root))
                    self.assertEqual(0, flow.manager._queue.qsize())
                    self.assertEqual((), tuple(flow.manager._pending))

    async def test_accepted_job_is_not_reported_rejected_on_lock_cleanup_failure(self):
        from src.rename import file_lock
        lock_kinds = ('task_record', None, 'decision_state', 'projection_commit', 'recognition_admission')
        for released_lock in lock_kinds:
            with self.subTest(released_lock=released_lock):
                async with self.fixture.flow(admission_only=True) as flow, hold_worker(flow.manager):
                    put, metadata = flow.manager._queue.put_nowait, file_lock._write_metadata
                    accepted, released, faults = [], [], []
                    def put_once(job):
                        put(job)
                        accepted.append(job)
                    def fail_released(handle, payload):
                        if accepted and payload.get('status') == 'released':
                            released.append(payload.get('kind'))
                            if payload.get('kind') == released_lock:
                                faults.append(True)
                                raise OSError('owned accepted lock cleanup fault')
                        return metadata(handle, payload)
                    with patch.object(flow.manager._queue, 'put_nowait', side_effect=put_once), \
                            patch.object(file_lock, '_write_metadata', side_effect=fail_released):
                        result = await flow.manager.retry_current(flow.task_id)
                    self.assertEqual([True], faults, released)
                    self.assertEqual(list(lock_kinds[:lock_kinds.index(released_lock)+1]), released)
                    self.assertEqual(flow.task_id, result)
                    self.assertEqual(1, len(accepted))
                    self.assertTrue(any(item is accepted[0] for item in flow.manager._queue._queue))
                    self.assertEqual((flow.task_id,), tuple(flow.manager._pending))
                    self.assertEqual('queued', flow.manager._read(flow.task_id)['workflow_status'])
                    with self.assertRaises(PlanExecutionError):
                        await flow.manager.retry_current(flow.task_id)

    async def test_before_acceptance_lock_error_still_refuses_without_business_write(self):
        from src.rename import file_lock
        async with self.fixture.flow(admission_only=True) as flow:
            metadata = file_lock._write_metadata
            def fail_released(handle, payload):
                if payload.get('status') == 'released':
                    raise OSError('owned prepare lock cleanup fault')
                return metadata(handle, payload)
            before = snapshot(flow.root)
            with patch.object(file_lock, '_write_metadata', side_effect=fail_released):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.retry_current(flow.task_id)
            self.assertEqual(before, snapshot(flow.root))
            self.assertEqual(0, flow.manager._queue.qsize())
            self.assertEqual((), tuple(flow.manager._pending))

    async def test_gui_reports_acceptance_after_reset_operation_lock_cleanup_error(self):
        from src.rename import file_lock
        for kind in ('plan', 'recovery'):
            with self.subTest(kind=kind):
                async with self.fixture.flow(automatic=False) as flow:
                    metadata, put = file_lock._write_metadata, flow.manager._queue.put_nowait
                    accepted, faults = [], []
                    def put_once(job):
                        put(job)
                        accepted.append(job)
                    def fail_released(handle, payload):
                        if accepted and payload.get('kind') == 'recognition_admission' and payload.get('status') == 'released':
                            faults.append(True)
                            raise OSError('owned GUI acceptance cleanup fault')
                        return metadata(handle, payload)
                    with patch.object(file_lock, '_write_metadata', side_effect=fail_released), \
                            patch.object(flow.manager._queue, 'put_nowait', side_effect=put_once):
                        close, notices, error = await self.call_gui(flow, flow.manager._read(flow.task_id), kind=kind)
                    self.assertIsNone(error)
                    self.assertEqual([True], faults)
                    close.assert_called_once()
                    self.assertTrue(any('任务已重新加入队列' in str(call) for call in notices.call_args_list))
                    self.assertEqual(1, len(accepted))
                    await flow.manager.stop()

    async def test_atomic_task_write_failure_restores_only_its_postimage(self):
        from src.utils import utils
        for after_replace in (False, True):
            with self.subTest(after_replace=after_replace):
                async with self.fixture.flow(admission_only=True) as flow:
                    original = utils._atomic_replace_task_bytes
                    calls = []
                    def fail_once(path, value):
                        calls.append(path)
                        if len(calls) == 1:
                            if after_replace:
                                original(path, value)
                            raise OSError('owned atomic write fault')
                        return original(path, value)
                    before = snapshot(flow.root)
                    with patch.object(utils, '_atomic_replace_task_bytes', side_effect=fail_once):
                        with self.assertRaises(PlanExecutionError):
                            await flow.manager.retry_current(flow.task_id)
                    self.assertEqual(before, snapshot(flow.root))
                    self.assertEqual(0, flow.manager._queue.qsize())
                    self.assertEqual((), tuple(flow.manager._pending))

    async def test_task_cas_rejects_normal_writer_after_candidate_validation(self):
        from src.rename import recognition_queue_admission as admission
        from src.utils.utils import merge_task_at
        async with self.fixture.flow(admission_only=True) as flow:
            original = admission._candidate
            calls = []
            expected = {}
            def candidate(*args, **kwargs):
                result = original(*args, **kwargs)
                calls.append(True)
                if len(calls) == 2:  # Commit revalidation, before Task lock/CAS.
                    merge_task_at(flow.manager.task_root, flow.task_id, {'name': 'later normal writer'})
                    expected.update(snapshot(flow.root))
                return result
            with patch.object(admission, '_candidate', side_effect=candidate):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.retry_current(flow.task_id)
            self.assertEqual(2, len(calls))
            self.assertEqual(expected, snapshot(flow.root))
            self.assertEqual(0, flow.manager._queue.qsize())

    async def test_unfinished_projection_journal_is_not_overwritten_by_retry(self):
        async with self.fixture.flow(automatic=False) as flow:
            path = flow.manager.projection_commit_service._journal_path(flow.task_id)
            payload = json.loads(path.read_text('utf-8'))
            payload['status'] = 'pending'
            path.write_text(json.dumps(payload), encoding='utf-8')  # Owned interrupted-state fixture.
            before = snapshot(flow.root)
            with self.assertRaises(PlanExecutionError):
                await flow.manager.retry_current(flow.task_id)
            self.assertEqual(before, snapshot(flow.root))

    async def test_initial_workspace_allows_compatible_search_changes(self):
        async with self.fixture.flow(admission_only=True) as flow:
            task = flow.manager._read(flow.task_id)
            task['use_ai'] = True
            write_task_at(flow.root/'tasks', flow.task_id, task)
            args = self.args(task, flow.manager)
            args.update(custom_name=' new initial search ', custom_season_id=2)
            jobs = []
            async def execute(job):
                jobs.append(job)
                return {'workflow_status': 'need_confirm'}
            flow.manager.executor = execute
            before_scope = task['task_source_scope']
            await flow.manager.reenqueue_workspace(flow.task_id, **args)
            queued = flow.manager._read(flow.task_id)
            self.assertEqual(before_scope, queued['task_source_scope'])
            self.assertEqual('new initial search', queued['name'])
            await asyncio.wait_for(flow.manager._queue.join(), 5)
            self.assertEqual(1, len(jobs))
            self.assertEqual(2, jobs[0].custom_season_id)
            self.assertTrue(jobs[0].use_ai)
            self.assertEqual(task['path'], str(jobs[0].path))

    async def test_workspace_dispatch_uses_normalized_name(self):
        from src.rename.decision_state import DecisionState
        from src.rename.content_policy import ContentPolicy
        for authority in ('plan', 'decision-only'):
            with self.subTest(authority=authority):
                async with self.fixture.flow(admission_only=authority == 'decision-only', automatic=False) as flow:
                    if authority == 'decision-only':
                        flow.manager.decision_state_store.save(DecisionState(task_id=flow.task_id,
                            content_policy=ContentPolicy.SAFE_WATCHABLE), expected_revision=-1)
                    task = flow.manager._read(flow.task_id)
                    task.update(name='', use_ai=None)
                    write_task_at(flow.root/'tasks', flow.task_id, task)
                    args = self.args(task, flow.manager)
                    args.update(custom_name='  ')
                    jobs = []
                    async def execute(job):
                        jobs.append(job)
                        return {'workflow_status': 'need_confirm'}
                    flow.manager.executor = execute
                    await flow.manager.reenqueue_workspace(flow.task_id, **args)
                    await asyncio.wait_for(flow.manager._queue.join(), 12)
                    self.assertEqual(1, len(jobs))
                    self.assertIsNone(jobs[0].custom_name)
                    self.assertIsNone(jobs[0].use_ai)

    async def test_authority_rejects_invalid_type_and_season_settings(self):
        from src.rename.decision_state import DecisionState
        from src.rename.content_policy import ContentPolicy
        for authority in ('plan', 'decision-only'):
            with self.subTest(authority=authority):
                async with self.fixture.flow(admission_only=authority == 'decision-only', automatic=False) as flow:
                    if authority == 'decision-only':
                        flow.manager.decision_state_store.save(DecisionState(task_id=flow.task_id,
                            content_policy=ContentPolicy.SAFE_WATCHABLE), expected_revision=-1)
                    task = flow.manager._read(flow.task_id)
                    for changes in ({'custom_season_id': -1}, {'custom_season_id': True},
                                    {'is_movie': 'unknown'}, {'is_anime': 1}):
                        with self.subTest(changes=changes):
                            before = snapshot(flow.root)
                            with self.assertRaises(PlanExecutionError):
                                await flow.manager.reenqueue_workspace(flow.task_id, **{**self.args(task, flow.manager), **changes})
                            self.assertEqual(before, snapshot(flow.root))

    async def test_plain_retry_uses_frozen_search_input_instead_of_display_name(self):
        for requested in (None, '等待识别', '人工固定标题'):
            with self.subTest(requested=requested):
                async def admit(manager, source, task_id):
                    await admit_task(manager, source, task_id=task_id,
                        is_anime=False, is_movie=False, custom_name=requested)
                async with self.fixture.flow(admission_only=True, admit=admit) as flow:
                    task = flow.manager._read(flow.task_id)
                    task['name'] = '识别后的展示名称'
                    write_task_at(flow.manager.task_root, flow.task_id, task)
                    jobs = []
                    async def execute(job):
                        jobs.append(job)
                        return {'workflow_status': 'need_confirm'}
                    flow.manager.executor = execute
                    await flow.manager.retry_current(flow.task_id)
                    await asyncio.wait_for(flow.manager._queue.join(), 12)
                    self.assertEqual(1, len(jobs))
                    self.assertEqual(requested, jobs[0].custom_name)
                    self.assertEqual(requested, jobs[0].recognition_origin.custom_name)

    async def test_legacy_missing_search_input_requires_explicit_workspace_confirmation(self):
        async with self.fixture.flow(admission_only=True) as flow:
            task = flow.manager._read(flow.task_id)
            task.pop('automatic_recognition_origin')
            write_task_at(flow.manager.task_root, flow.task_id, task)
            before = snapshot(flow.root)
            with self.assertRaises(PlanExecutionError):
                await flow.manager.retry_current(flow.task_id)
            self.assertEqual(before, snapshot(flow.root))
            self.assertEqual(0, flow.manager._queue.qsize())
            jobs = []
            async def execute(job):
                jobs.append(job)
                return {'workflow_status': 'need_confirm'}
            flow.manager.executor = execute
            args = self.args(task, flow.manager)
            args['custom_name'] = None
            await flow.manager.reenqueue_workspace(flow.task_id, **args)
            await asyncio.wait_for(flow.manager._queue.join(), 12)
            self.assertEqual(1, len(jobs))
            self.assertEqual(flow.task_id, jobs[0].task_id)
            self.assertIsNone(jobs[0].custom_name)

    async def test_reset_queue_failure_cannot_be_reported_accepted(self):
        for inserted in (False, True):
            with self.subTest(inserted=inserted):
                async with self.fixture.flow(admission_only=True) as flow, hold_worker(flow.manager):
                    task = flow.manager._read(flow.task_id)
                    put = flow.manager._queue.put_nowait
                    def fail(job):
                        if inserted:
                            put(job)
                        raise OSError('隔离测试入队异常')
                    with patch.object(flow.manager._queue, 'put_nowait', side_effect=fail):
                        with self.assertRaises(PlanExecutionError):
                            await flow.manager.reenqueue_workspace(flow.task_id, **self.args(task, flow.manager))
                    self.assertEqual(0, flow.manager._queue.qsize())
                    self.assertNotEqual('approved', flow.manager._read(flow.task_id)['workflow_status'])

    async def test_initial_workspace_cannot_change_preview_identity(self):
        async with self.fixture.flow(admission_only=True) as flow:
            task = flow.manager._read(flow.task_id)
            for changes in ({'preview_only': True},):
                with self.subTest(changes=changes):
                    before = snapshot(flow.root)
                    with self.assertRaises(PlanExecutionError):
                        await flow.manager.reenqueue_workspace(flow.task_id, **{**self.args(task, flow.manager), **changes})
                    self.assertEqual(before, snapshot(flow.root))

    async def test_old_gui_evidence_rejects_later_authority_without_task_change(self):
        from src.rename.recognition_queue_admission import capture_workspace_open
        from src.rename.decision_state import DecisionState
        from src.rename.content_policy import ContentPolicy
        from tests.test_content_choice_contract import change
        for kind in ('plan', 'recovery'):
            with self.subTest(caller=kind):
                async with self.fixture.flow(admission_only=kind == 'recovery', automatic=False) as flow:
                    task = flow.manager._read(flow.task_id)
                    task_path = flow.root/'tasks'/f'{flow.task_id}.json'
                    raw = task_path.read_bytes()
                    opened = capture_workspace_open(flow.manager, flow.task_id, task)
                    if kind == 'plan':
                        from src.rename.projection_authority import (
                            ProjectionAuthorityEnvelope, inventory_fingerprint_for_plan, _episode_rows_from_plan)
                        original = flow.store.load(flow.task_id)
                        state = flow.manager.decision_state_store.load(flow.task_id)
                        _state, revised = flow.manager.projection_commit_service.commit(
                            ProjectionAuthorityEnvelope(previous_decision=state, decision=state,
                                draft_plan=original, inventory_fingerprint=inventory_fingerprint_for_plan(original),
                                episode_ownership=_episode_rows_from_plan(original)))
                        self.assertNotEqual(flow.store.fingerprint(original), flow.store.fingerprint(revised))
                    else:
                        flow.manager.decision_state_store.save(DecisionState(task_id=flow.task_id,
                            content_policy=ContentPolicy.SAFE_WATCHABLE), expected_revision=-1)
                    self.assertEqual(raw, task_path.read_bytes())
                    before = snapshot(flow.root)
                    close, notices, error = await self.call_gui(flow, task, kind=kind, opened=opened)
                    self.assertIsNone(error)
                    close.assert_not_called()
                    self.assertEqual(before, snapshot(flow.root))
                    self.assertFalse(any(c.kwargs.get('type') == 'positive' for c in notices.call_args_list))

    async def test_disconnected_preparation_blocks_every_recognition_entry(self):
        from src.rename import recognition_queue_admission as admission
        from src.rename import recognition_reset
        async with self.fixture.flow(admission_only=True) as flow:
            task = flow.manager._read(flow.task_id)
            before = snapshot(flow.root)
            with patch.object(admission, '_prepare_readonly', return_value=None), \
                    patch.object(recognition_reset, '_prepare', return_value=None):
                for mode in ('retry', 'workspace', 'selected'):
                    with self.subTest(mode=mode), self.assertRaises(PlanExecutionError):
                        if mode == 'retry':
                            await flow.manager.retry_current(flow.task_id)
                        elif mode == 'workspace':
                            await flow.manager.reenqueue_workspace(flow.task_id, **self.args(task, flow.manager))
                        else:
                            await flow.manager.enqueue_reidentify(flow.task_id, {}, 0)
            self.assertEqual(before, snapshot(flow.root))
            self.assertEqual(0, flow.manager._queue.qsize())

    async def test_insert_then_raise_is_isolated_and_cannot_auto_retry_after_restart(self):
        async with self.fixture.flow(admission_only=True) as flow:
            sink = flow.manager._queue.put_nowait
            def insert_then_raise(job):
                sink(job)
                raise RuntimeError('nonstandard sink inserted then failed')
            with patch.object(flow.manager._queue, 'put_nowait', side_effect=insert_then_raise):
                with self.assertRaises(RuntimeError):
                    await flow.manager.retry_current(flow.task_id)
            task = flow.manager._read(flow.task_id)
            self.assertTrue(task['queue_admission_uncertain'])
            self.assertEqual('need_confirm', task['workflow_status'])
            self.assertEqual(0, flow.manager._queue.qsize())
            await asyncio.wait_for(flow.manager._queue.join(), 5)
            await flow.manager.stop()
            await flow.manager.start()
            before = snapshot(flow.root)
            with self.assertRaises(PlanExecutionError):
                await flow.manager.retry_current(flow.task_id)
            self.assertEqual(before, snapshot(flow.root))
            self.assertEqual(0, flow.manager._queue.qsize())

    async def test_selected_admission_excludes_actual_authority_writers(self):
        from src.rename.file_lock import task_operation_lock
        async with self.fixture.flow(automatic=False) as flow:
            state = flow.manager.decision_state_store.load(flow.task_id)
            selected = dict(namespace='tmdb', entity_type='tv', provider_id='4242', title='普通剧集')
            locks = (
                lambda: task_operation_lock(flow.manager.task_root, flow.task_id, operation='test'),
                lambda: flow.manager.projection_commit_service._transaction_lock(flow.task_id),
                lambda: flow.manager.decision_state_store._lock(flow.task_id),
                lambda: flow.manager.batch_scope_store._mutation_lock('test'),
            )
            for lock in locks:
                with self.subTest(lock=lock), lock():
                    before = snapshot(flow.root)
                    with self.assertRaises(PlanExecutionError):
                        await flow.manager.enqueue_reidentify(flow.task_id, selected, state.revision)
                    self.assertEqual(before, snapshot(flow.root))

    async def test_real_dashboard_retry_reaches_shared_admission_and_worker(self):
        from src.pages import data_table_page as dashboard
        async with self.fixture.flow(admission_only=True) as flow:
            executed = []
            async def executor(job):
                executed.append(job)
                return {'workflow_status': 'need_confirm'}
            flow.manager.executor = executor
            with patch.multiple(dashboard, task_queue=flow.manager,
                    get_task=lambda task_id: flow.manager._read(task_id),
                    notify=Mock(), refresh_dashboard=Mock()):
                await dashboard._retry_task(flow.task_id)
                await asyncio.wait_for(flow.manager._queue.join(), 5)
            self.assertEqual(1, len(executed))
            self.assertEqual(flow.task_id, executed[0].task_id)

    async def test_real_prewrite_execution_failure_reaches_shared_admission(self):
        from tests import _media_flow_helpers as execution
        from src.rename.execution_recovery import requires_execution_replan
        from src.pages import data_table_page as dashboard
        async with self.fixture.flow() as flow:
            plan = flow.store.load(flow.task_id)
            fingerprint = flow.store.fingerprint(plan)
            target = next(e.target_path for e in plan.entries if e.media_kind == 'video')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'owned temporary obstruction for prewrite failure')
            task, ledger, service = await execution.MediaFlow()._approve_and_execute(
                root=flow.root, result=SimpleNamespace(plan=plan, task=flow.manager._read(flow.task_id)))
            self.assertTrue(requires_execution_replan(task), task.get('error'))
            self.assertEqual({}, ledger)
            target.unlink()  # Only the exact obstruction created by this test.
            flow.manager.execution_service = service
            flow.manager.review_root = service.review_service.review_root
            notices = Mock()
            actual_worker = AsyncMock(wraps=flow.manager.executor)
            from src.rename import current_tv_refresh
            refresh_worker = AsyncMock(wraps=current_tv_refresh.drive)
            with patch.multiple(dashboard, task_queue=flow.manager,
                    get_task=lambda task_id: flow.manager._read(task_id), notify=notices,
                    refresh_dashboard=Mock()), \
                    patch.object(flow.manager, 'executor', new=actual_worker), \
                    patch.object(current_tv_refresh, 'drive', new=refresh_worker):
                await dashboard._replan_failed_execution(flow.task_id)
                await asyncio.wait_for(flow.manager._queue.join(), 12)
            task = flow.manager._read(flow.task_id)
            self.assertTrue(task.get('execution_replan_history'))
            actual_worker.assert_not_awaited()
            refresh_worker.assert_awaited_once()
            self.assertEqual('execution_replan', refresh_worker.await_args.args[1].source)
            self.assertFalse(service.review_service._path_helper.path_for(flow.task_id).exists())
            self.assertNotEqual(fingerprint, flow.store.fingerprint(flow.store.load(flow.task_id)))
            self.assertEqual('completed', task['current_tv_refresh']['status'])
            self.assertNotEqual('approved', task['workflow_status'])
            self.assertTrue(any(c.kwargs.get('type') == 'positive' for c in notices.call_args_list))

    @staticmethod
    def args(task, manager):
        from pathlib import Path
        from src.rename.recognition_queue_admission import capture_workspace_open
        return dict(path=Path(task['path']), is_anime=task.get('is_anime'),
            is_movie=task.get('is_movie'), custom_name=task.get('name'),
            custom_season_id=task.get('season_id'),
            preview_only=bool(task.get('preview_only')),
            opened_evidence=capture_workspace_open(manager, task['uuid'], task))

    async def call_gui(self, flow, task, *, kind, path=None, name=None, opened=None):
        from src.rename.recognition_queue_admission import capture_workspace_open
        value = lambda item: SimpleNamespace(value=item)
        source = path or task['path']
        requested_name = task.get('name') if name is None else name
        close = Mock()
        if kind == 'plan':
            dialog = SimpleNamespace(task_id=flow.task_id, plan=flow.store.load(flow.task_id), task_data=task,
                rerecognition_path=value(source), rerecognition_media_type=value('tv'),
                rerecognition_name=value(requested_name), rerecognition_season=value(task.get('season_id')),
                _close_review=close)
            callback = page.PlanReviewDialog._restart_recognition
        else:
            dialog = SimpleNamespace(task_id=flow.task_id, task_data=task,
                provider_url=value(''), path_input=value(source), media_type=value('tv'), name_input=value(requested_name),
                season_input=value(task.get('season_id')), close=close)
            callback = RecognitionRecoveryDialog._restart
        dialog.retry_open_evidence = opened or capture_workspace_open(flow.manager, flow.task_id, task)
        notices = Mock()
        error = None
        with patch.object(page, 'task_queue', flow.manager), patch.object(page, 'notify', notices):
            try:
                from tests.workflow_gui_fixture import confirm_restart
                await confirm_restart(lambda: callback(dialog))
            except (PlanExecutionError, ValueError, OSError) as exc:
                error = exc
        return close, notices, error

    async def test_both_gui_callbacks_preserve_rejected_request_and_open_window(self):
        for kind in ('plan', 'recovery'):
            with self.subTest(caller=kind):
                async with self.fixture.flow(admission_only=kind != 'plan', automatic=False) as flow:
                    task = flow.manager._read(flow.task_id)
                    task['execution_may_have_written_targets'] = True
                    write_task_at(flow.root/'tasks', flow.task_id, task)
                    original = json.loads(json.dumps(task))
                    before = snapshot(flow.root)
                    close, notices, error = await self.call_gui(flow, task, kind=kind,
                        name='must not persist an executed-task edit')
                    self.assertEqual(before, snapshot(flow.root), 'GUI wrote settings before the actual execution-risk gate')
                    self.assertEqual(original, task)
                    close.assert_not_called()
                    self.assertFalse(any(c.kwargs.get('type') == 'positive' for c in notices.call_args_list))
                    self.assertIsNone(error, 'expected rejection must be presented in the open dialog')

    async def test_both_gui_callbacks_cannot_mask_a_different_original_source(self):
        for kind in ('plan', 'recovery'):
            with self.subTest(caller=kind):
                async with self.fixture.flow(admission_only=kind != 'plan', automatic=False) as flow:
                    task = flow.manager._read(flow.task_id)
                    original = json.loads(json.dumps(task))
                    other = flow.root/'different-source'
                    other.mkdir()
                    before = snapshot(flow.root)
                    pending = tuple(flow.manager._pending)
                    close, notices, _ = await self.call_gui(flow, task, kind=kind, path=str(other))
                    self.assertEqual(before, snapshot(flow.root), 'GUI overwrote original source before backend comparison')
                    self.assertEqual(original, task)
                    self.assertEqual(pending, tuple(flow.manager._pending))
                    self.assertEqual(0, flow.manager._queue.qsize())
                    close.assert_not_called()
                    self.assertFalse(any(c.kwargs.get('type') == 'positive' for c in notices.call_args_list))

    async def test_both_gui_callbacks_preserve_later_task_from_stale_open_window(self):
        for kind in ('plan', 'recovery'):
            with self.subTest(caller=kind):
                async with self.fixture.flow(admission_only=kind != 'plan', automatic=False) as flow:
                    stale = flow.manager._read(flow.task_id)
                    original = json.loads(json.dumps(stale))
                    later = {**stale, 'name': 'later request owns this name', 'error': 'later state'}
                    write_task_at(flow.root/'tasks', flow.task_id, later)
                    before = snapshot(flow.root)
                    close, notices, _ = await self.call_gui(flow, stale, kind=kind)
                    self.assertEqual(before, snapshot(flow.root), 'stale dialog overwrote a later Task')
                    self.assertEqual(original, stale)
                    close.assert_not_called()
                    self.assertFalse(any(c.kwargs.get('type') == 'positive' for c in notices.call_args_list))

    async def test_workspace_missing_scope_rejects_before_metadata_write(self):
        async with self.fixture.flow(admission_only=True) as flow:
            task = flow.manager._read(flow.task_id)
            task.pop('task_source_scope')
            write_task_at(flow.root/'tasks', flow.task_id, task)
            before = snapshot(flow.root)
            arguments = self.args(task, flow.manager)
            arguments['custom_name'] = 'must not persist after rejection'
            with self.assertRaises(PlanExecutionError):
                await flow.manager.reenqueue_workspace(flow.task_id, **arguments)
            self.assertEqual(before, snapshot(flow.root))
            self.assertEqual((), tuple(flow.manager._pending))
            self.assertEqual(0, flow.manager._queue.qsize())

    async def test_workspace_cannot_route_completed_media_to_asset_repair(self):
        async with self.fixture.flow(admission_only=True) as flow:
            task = flow.manager._read(flow.task_id)
            task.update(execution_status='completed', workflow_status='completed')
            write_task_at(flow.root/'tasks', flow.task_id, task)
            from src.rename.library_assets import LibraryAssets
            before = snapshot(flow.root)
            with patch.object(LibraryAssets, 'repair', side_effect=AssertionError('wrong route')) as repair:
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.reenqueue_workspace(flow.task_id, **self.args(task, flow.manager))
            repair.assert_not_called()
            self.assertEqual(before, snapshot(flow.root))

    async def test_actual_retired_scope_cannot_save_workspace_settings(self):
        async with self.fixture.flow(admission_only=True) as flow:
            task = flow.manager._read(flow.task_id)
            flow.manager.batch_scope_store.retire_task(task['batch_id'], task_id=flow.task_id,
                expected_lineage_id=task['reservation_lineage_id'])
            before = snapshot(flow.root)
            args = self.args(task, flow.manager)
            args['custom_name'] = 'not saved for retired reservation'
            with self.assertRaises(PlanExecutionError):
                await flow.manager.reenqueue_workspace(flow.task_id, **args)
            self.assertEqual(before, snapshot(flow.root))

    async def test_confirmed_tv_manual_restart_preserves_identity_and_exact_old_plan(self):
        async with self.fixture.flow() as flow:
            task = flow.manager._read(flow.task_id)
            state = flow.service.decision_state_store.load(flow.task_id)
            self.assertTrue(state.confirmed_work)
            task['provider_selection'] = dict(state.confirmed_work['primary_provider'])
            write_task_at(flow.root/'tasks', flow.task_id, task)
            old_plan = flow.store.path_for(flow.task_id).read_bytes()
            old_state = flow.manager.decision_state_store.path_for(flow.task_id).read_bytes()
            args = self.args(task, flow.manager)
            args['custom_name'] = '明确选择的新名称'
            jobs = []
            async def execute(job):
                jobs.append(job)
                return {'workflow_status': 'need_confirm'}
            flow.manager.executor = execute
            self.assertEqual(flow.task_id, await flow.manager.reenqueue_workspace(flow.task_id, **args))
            await asyncio.wait_for(flow.manager._queue.join(), 12)
            self.assertEqual(1, len(jobs))
            self.assertEqual('明确选择的新名称', jobs[0].custom_name)
            current = flow.manager._read(flow.task_id)
            for key in ('uuid', 'path'):
                self.assertEqual(task[key], current[key])
            for key in ('source_identities', 'source_identity_set_fingerprint', 'media_domain', 'task_id'):
                self.assertEqual(task['task_source_scope'][key], current['task_source_scope'][key])
            self.assertNotEqual(task['batch_id'], current['batch_id'])
            from src.rename.recognition_reset import _record
            operation, record = _record(flow.root, flow.task_id)
            self.assertEqual(record['request_id'], json.loads((operation/'completed.json').read_bytes())['request_id'])
            self.assertEqual(old_plan, (operation/'retired'/'plan.json').read_bytes())
            self.assertEqual(old_state, (operation/'retired'/'decision.json').read_bytes())

    async def test_selected_wrong_media_kind_is_rejected_without_operation_write(self):
        async with self.fixture.flow(automatic=False) as flow:
            state = flow.service.decision_state_store.load(flow.task_id)
            before = snapshot(flow.root)
            with self.assertRaises(PlanExecutionError):
                await flow.manager.enqueue_reidentify(flow.task_id,
                    dict(namespace='tmdb', entity_type='movie', provider_id='8181'), state.revision)
            self.assertEqual(before, snapshot(flow.root))

    async def test_cancelled_startup_has_no_recognition_admission(self):
        async with self.fixture.flow(admission_only=True) as flow:
            await flow.manager.stop()
            entered = asyncio.Event()
            blocked = asyncio.Event()
            async def waiting_start():
                entered.set()
                await blocked.wait()
            before = snapshot(flow.root)
            with patch.object(flow.manager, 'start', side_effect=waiting_start):
                operation = asyncio.create_task(flow.manager.retry_current(flow.task_id))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=3)
                    operation.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await operation
                finally:
                    if not operation.done():
                        operation.cancel()
                        await asyncio.gather(operation, return_exceptions=True)
            self.assertEqual(before, snapshot(flow.root))
            self.assertEqual((), tuple(flow.manager._pending))
            self.assertEqual(0, flow.manager._queue.qsize())

    async def test_fresh_startup_marks_accepted_unrun_selection_interrupted_without_replay(self):
        from src.task_queue import TaskQueueManager
        async with self.fixture.flow(automatic=False) as flow:
            state = flow.service.decision_state_store.load(flow.task_id)
            await flow.manager.enqueue_reidentify(flow.task_id,
                dict(namespace='tmdb', entity_type='tv', provider_id='4242', title='普通剧集',
                     original_title='Ordinary Show', year=2020), state.revision)
            await flow.manager.stop()  # No event-loop yield to the worker before cancellation.
            seen = []
            async def unexpected(job):
                seen.append(job)
            fresh = TaskQueueManager(task_root=flow.root/'tasks', plan_root=flow.root/'plans',
                decision_state_root=flow.root/'decisions', provider_link_catalog_root=flow.root/'provider_links',
                recovery_abandon_audit_root=flow.root/'abandon',  executor=unexpected)
            try:
                await fresh.start()
                self.assertEqual(0, fresh._queue.qsize())
                self.assertEqual([], seen)
                self.assertEqual('interrupted', fresh._read(flow.task_id)['workflow_status'])
                self.assertEqual((), tuple(fresh._pending))
            finally:
                await fresh.stop()

    async def test_ordinary_retry_retains_real_completed_asset_diagnosis(self):
        from tests import _media_flow_helpers as execution
        from src.rename.library_assets import LibraryAssets
        async with self.fixture.flow() as flow:
            plan = flow.store.load(flow.task_id)
            task, ledger, execution_service = await execution.MediaFlow()._approve_and_execute(
                root=flow.root, result=SimpleNamespace(plan=plan, task=flow.manager._read(flow.task_id)))
            self.assertEqual('completed', task.get('execution_status'), task.get('error'))
            self.assertEqual('completed', ledger.get('status'))
            flow.manager.execution_service = execution_service
            original = LibraryAssets.repair
            reached = []
            def actual_repair(service, task_id):
                reached.append(task_id)
                return original(service, task_id)
            before = snapshot(flow.root)
            with patch.object(LibraryAssets, 'repair', new=actual_repair):
                self.assertEqual(flow.task_id, await flow.manager.retry_current(flow.task_id))
            self.assertEqual([flow.task_id], reached)
            self.assertEqual(before, snapshot(flow.root))
            self.assertEqual((), tuple(flow.manager._pending))

    async def test_actual_competing_business_locks_block_admission_before_any_write(self):
        from src.rename.file_lock import task_operation_lock
        for lock_kind in ('operation', 'projection', 'decision', 'batch'):
            with self.subTest(lock=lock_kind):
                async with self.fixture.flow(admission_only=True) as flow:
                    manager = flow.manager
                    locks = {
                        'operation': lambda: task_operation_lock(flow.root/'tasks', flow.task_id, operation='test-other-owner'),
                        'projection': lambda: manager.projection_commit_service._transaction_lock(flow.task_id),
                        'decision': lambda: manager.decision_state_store._lock(flow.task_id),
                        'batch': lambda: manager.batch_scope_store._mutation_lock('test-other-owner'),
                    }
                    before = snapshot(flow.root)
                    with locks[lock_kind]():
                        with self.assertRaises((PlanExecutionError, RuntimeError)):
                            await manager.retry_current(flow.task_id)
                    self.assertEqual(before, snapshot(flow.root))
                    self.assertEqual((), tuple(manager._pending))

    async def test_mismatched_projection_store_root_cannot_be_used_for_admission_locks(self):
        from src.rename.decision_state import DecisionStateStore
        async with self.fixture.flow(admission_only=True) as flow:
            before = snapshot(flow.root)
            with patch.object(flow.manager.projection_commit_service, 'state_store',
                              DecisionStateStore(flow.root/'unrelated-decisions')):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.retry_current(flow.task_id)
            self.assertEqual(before, snapshot(flow.root))
            self.assertEqual((), tuple(flow.manager._pending))

    async def test_workspace_pending_and_active_requests_cannot_save_new_settings(self):
        for active in (False, True):
            with self.subTest(active=active):
                async with self.fixture.flow(admission_only=True) as flow, \
                        (nullcontext() if active else hold_worker(flow.manager)):
                    entered, release = asyncio.Event(), asyncio.Event()
                    async def blocked(job):
                        entered.set()
                        await release.wait()
                        flow.manager._update(job.task_id, workflow_status='need_confirm')
                    flow.manager.executor = blocked
                    try:
                        await flow.manager.retry_current(flow.task_id)
                        if active:
                            await asyncio.wait_for(entered.wait(), timeout=3)
                        task = flow.manager._read(flow.task_id)
                        before = snapshot(flow.root)
                        pending = tuple(flow.manager._pending)
                        args = self.args(task, flow.manager)
                        args['custom_name'] = 'later click must not rewrite the admitted request'
                        with self.assertRaises(PlanExecutionError):
                            await flow.manager.reenqueue_workspace(flow.task_id, **args)
                        self.assertEqual(before, snapshot(flow.root))
                        self.assertEqual(pending, tuple(flow.manager._pending))
                    finally:
                        release.set()

    async def test_plain_retry_put_failure_restores_task_and_queue_exactly(self):
        async with self.fixture.flow(admission_only=True) as flow:
            before = snapshot(flow.root)
            with patch.object(flow.manager._queue, 'put_nowait', side_effect=RuntimeError('queue rejected')):
                with self.assertRaisesRegex(RuntimeError, 'queue rejected'):
                    await flow.manager.retry_current(flow.task_id)
            self.assertEqual(before, snapshot(flow.root))
            self.assertEqual((), tuple(flow.manager._pending))
            self.assertEqual(0, flow.manager._queue.qsize())

    async def test_plain_retry_put_failure_does_not_restore_over_later_task(self):
        async with self.fixture.flow(admission_only=True) as flow:
            expected = {}
            def fail(_job):
                later = flow.manager._read(flow.task_id)
                later.update(name='later writer', workflow_status='interrupted', queue_operation='later owner')
                # Deliberately non-cooperating external file replacement in
                # owned storage; normal Task writers have a separate lock test.
                path = flow.root/'tasks'/f'{flow.task_id}.json'
                path.write_text(json.dumps(later, ensure_ascii=False, indent=4), encoding='utf-8')
                expected.update(snapshot(flow.root))
                raise RuntimeError('queue rejected after later task')
            with patch.object(flow.manager._queue, 'put_nowait', side_effect=fail):
                with self.assertRaises(RuntimeError):
                    await flow.manager.retry_current(flow.task_id)
            self.assertEqual(expected, snapshot(flow.root))
            self.assertEqual((), tuple(flow.manager._pending))
            self.assertEqual(0, flow.manager._queue.qsize())

    async def test_actual_task_writer_is_excluded_until_queue_acceptance(self):
        async with self.fixture.flow(admission_only=True) as flow:
            from src.rename.file_lock import FileLockBusyError
            from src.utils.utils import merge_task_at
            before = snapshot(flow.root)
            excluded = []
            def compete(_job):
                try:
                    merge_task_at(flow.root/'tasks', flow.task_id, {'name': 'competing normal writer'})
                except RuntimeError as exc:
                    self.assertIsInstance(exc.__cause__, FileLockBusyError)
                    excluded.append(True)
                raise RuntimeError('queue rejected')
            with patch.object(flow.manager._queue, 'put_nowait', side_effect=compete):
                with self.assertRaises(RuntimeError):
                    await flow.manager.retry_current(flow.task_id)
            self.assertEqual([True], excluded, 'Task-record lock did not span write through queue acceptance')
            self.assertEqual(before, snapshot(flow.root))
            self.assertEqual((), tuple(flow.manager._pending))

    async def test_put_failure_never_resurrects_a_removed_task(self):
        async with self.fixture.flow(admission_only=True) as flow:
            expected = {}
            def remove(_job):
                (flow.root/'tasks'/f'{flow.task_id}.json').unlink()
                expected.update(snapshot(flow.root))
                raise RuntimeError('task removed externally')
            with patch.object(flow.manager._queue, 'put_nowait', side_effect=remove):
                with self.assertRaises(RuntimeError):
                    await flow.manager.retry_current(flow.task_id)
            self.assertEqual(expected, snapshot(flow.root))
            self.assertEqual((), tuple(flow.manager._pending))

    async def test_selected_retry_put_failure_preserves_task_plan_decision(self):
        async with self.fixture.flow(automatic=False) as flow:
            state = flow.service.decision_state_store.load(flow.task_id)
            self.assertFalse(state.confirmed_work)
            selected = dict(namespace='tmdb', entity_type='tv', provider_id='4242',
                title='普通剧集', original_title='Ordinary Show', year=2020,
                source_url='https://www.themoviedb.org/tv/4242', snapshot_at='2026-09-13T00:00:00Z')
            before = snapshot(flow.root)
            # Current implementation uses await put; the revised same-loop
            # commit may use put_nowait. Both are the actual queue sink.
            with patch.object(flow.manager._queue, 'put', new=AsyncMock(side_effect=RuntimeError('queue rejected'))), \
                    patch.object(flow.manager._queue, 'put_nowait', side_effect=RuntimeError('queue rejected')):
                with self.assertRaisesRegex(RuntimeError, 'queue rejected'):
                    await flow.manager.enqueue_reidentify(flow.task_id, selected, state.revision)
            self.assertEqual(before, snapshot(flow.root))
            self.assertEqual((), tuple(flow.manager._pending))
            self.assertEqual(0, flow.manager._queue.qsize())

    async def test_ordinary_unchanged_failed_retry_still_runs_original_queue(self):
        async with self.fixture.flow(admission_only=True) as flow:
            seen = []
            async def record(job):
                seen.append(job)
                flow.manager._update(job.task_id, workflow_status='need_confirm')
            flow.manager.executor = record
            task = flow.manager._read(flow.task_id)
            task.update(workflow_status='failed', error='earlier transport failure')
            write_task_at(flow.root/'tasks', flow.task_id, task)
            self.assertEqual(flow.task_id, await flow.manager.retry_current(flow.task_id))
            await asyncio.wait_for(flow.manager._queue.join(), timeout=5)
            self.assertEqual(1, len(seen))
            self.assertEqual(flow.source, seen[0].path)
            self.assertEqual(task['task_source_scope'], seen[0].task_source_scope.to_dict())
            self.assertEqual('need_confirm', read_task_at(flow.root/'tasks', flow.task_id)['workflow_status'])

    async def test_startup_later_task_is_not_silently_readmitted(self):
        async with self.fixture.flow(admission_only=True) as flow:
            await flow.manager.stop()
            start = flow.manager.start
            expected = {}
            async def start_and_change():
                await start()
                task = flow.manager._read(flow.task_id)
                task.update(name='later startup observation', error='preserve newer request')
                write_task_at(flow.root/'tasks', flow.task_id, task)
                expected.update(snapshot(flow.root))
            with patch.object(flow.manager, 'start', side_effect=start_and_change):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.retry_current(flow.task_id)
            self.assertEqual(expected, snapshot(flow.root))
            self.assertEqual(0, flow.manager._queue.qsize())
            self.assertEqual((), tuple(flow.manager._pending))

    async def test_startup_legal_no_plan_decision_writer_invalidates_preparation(self):
        from src.rename.decision_state import DecisionState
        from src.rename.content_policy import ContentPolicy
        async with self.fixture.flow(admission_only=True) as flow:
            self.assertFalse(flow.store.path_for(flow.task_id).exists())
            await flow.manager.stop()
            start = flow.manager.start
            expected = {}
            async def start_and_write_decision():
                await start()
                state = flow.manager.decision_state_store.save(
                    DecisionState(task_id=flow.task_id, content_policy=ContentPolicy.SAFE_WATCHABLE),
                    expected_revision=-1)
                self.assertEqual(0, state.revision)
                self.assertFalse(flow.store.path_for(flow.task_id).exists())
                expected.update(snapshot(flow.root))
            with patch.object(flow.manager, 'start', side_effect=start_and_write_decision):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.retry_current(flow.task_id)
            self.assertEqual(expected, snapshot(flow.root))
            self.assertEqual((), tuple(flow.manager._pending))

    async def test_startup_actual_batch_retirement_keeps_later_manifest(self):
        async with self.fixture.flow(admission_only=True) as flow:
            await flow.manager.stop()
            start = flow.manager.start
            expected = {}
            async def start_and_retire():
                await start()
                task = flow.manager._read(flow.task_id)
                flow.manager.batch_scope_store.retire_task(task['batch_id'], task_id=flow.task_id,
                    expected_lineage_id=task['reservation_lineage_id'])
                expected.update(snapshot(flow.root))
            with patch.object(flow.manager, 'start', side_effect=start_and_retire):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.retry_current(flow.task_id)
            self.assertEqual(expected, snapshot(flow.root))
            self.assertEqual((), tuple(flow.manager._pending))

    async def test_stop_during_startup_invalidates_the_original_retry(self):
        async with self.fixture.flow(admission_only=True) as flow:
            await flow.manager.stop()
            start = flow.manager.start
            before = snapshot(flow.root)
            async def start_and_stop():
                await start()
                await flow.manager.stop()
            with patch.object(flow.manager, 'start', side_effect=start_and_stop):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.retry_current(flow.task_id)
            self.assertEqual(before, snapshot(flow.root))
            self.assertEqual(0, flow.manager._queue.qsize())
            self.assertEqual((), tuple(flow.manager._pending))

    async def test_startup_canonical_later_plan_decision_remains_intact(self):
        from tests.test_content_choice_contract import change
        async with self.fixture.flow(automatic=False) as flow:
            await flow.manager.stop()
            start = flow.manager.start
            original = flow.store.load(flow.task_id)
            expected = {}
            async def start_and_revise():
                await start()
                selected = next(e.source_path for e in original.entries if e.media_kind == 'video')
                revised = change(flow.service, original, [selected])
                self.assertNotEqual(flow.store.fingerprint(original), flow.store.fingerprint(revised))
                expected.update(snapshot(flow.root))
            with patch.object(flow.manager, 'start', side_effect=start_and_revise):
                with self.assertRaises(PlanExecutionError):
                    await flow.manager.retry_current(flow.task_id)
            self.assertEqual(expected, snapshot(flow.root))
            self.assertEqual((), tuple(flow.manager._pending))

    async def test_put_failure_does_not_rewrite_other_pending_task_positions(self):
        async with self.fixture.flow(admission_only=True) as flow:
            # This control has a genuine second admission with a distinct
            # physical source; no forged scope or queue object is installed.
            other = flow.root/'second.mkv'
            other.write_bytes(b'owned second input')
            other_id = await admit_task(flow.manager, other, task_id='other-admitted', is_anime=False, is_movie=False)
            await asyncio.wait_for(flow.manager._queue.join(), timeout=5)
            # Preparation now yields. Hold the real consumer before it starts,
            # so its legitimate progress cannot be mistaken for a failed writer.
            await flow.manager.stop()
            release = asyncio.Event()
            worker = flow.manager._worker
            async def waiting_worker():
                await release.wait()
                await worker()
            with patch.object(flow.manager, '_worker', side_effect=waiting_worker):
                await flow.manager.start()
                try:
                    await flow.manager.retry_current(other_id)
                    other_task = flow.manager._read(other_id)
                    other_task['queue_position'] = 7  # A failed request must not rewrite this diagnostic.
                    write_task_at(flow.root/'tasks', other_id, other_task)
                    before = snapshot(flow.root)
                    pending = tuple(flow.manager._pending)
                    size = flow.manager._queue.qsize()
                    with patch.object(flow.manager._queue, 'put_nowait', side_effect=RuntimeError('reject new retry')):
                        with self.assertRaises(RuntimeError):
                            await flow.manager.retry_current(flow.task_id)
                    self.assertEqual(before, snapshot(flow.root))
                    self.assertEqual(pending, tuple(flow.manager._pending))
                    self.assertEqual(size, flow.manager._queue.qsize())
                finally:
                    release.set()

    async def test_legitimate_selected_retry_and_double_click_share_one_worker_job(self):
        async with self.fixture.flow(automatic=False) as flow:
            state = flow.service.decision_state_store.load(flow.task_id)
            selected = dict(namespace='tmdb', entity_type='tv', provider_id='4242',
                title='普通剧集', original_title='Ordinary Show', year=2020,
                source_url='https://www.themoviedb.org/tv/4242', snapshot_at='2026-09-13T00:00:00Z')
            first = await flow.manager.enqueue_reidentify(flow.task_id, selected, state.revision)
            before = snapshot(flow.root)
            with self.assertRaises(PlanExecutionError):
                await flow.manager.enqueue_reidentify(flow.task_id, selected, state.revision)
            self.assertEqual(before, snapshot(flow.root))
            self.assertEqual(flow.task_id, first)
            await asyncio.wait_for(flow.manager._queue.join(), timeout=12)
            plan = flow.store.load(flow.task_id)
            current = flow.service.decision_state_store.load(flow.task_id)
            self.assertTrue(current.confirmed_work)
            self.assertIsNotNone(plan.projection_receipt)
            self.assertNotEqual('approved', flow.manager._read(flow.task_id).get('workflow_status'))
