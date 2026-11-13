"""后台链接请求的关闭、并发及保存边界。"""
import asyncio
import unittest
from unittest.mock import patch
from nicegui.client import Client
from src.pages import plan_review_page as page, provider_selection_work as work
from tests import test_initial_auto_link_contract as initial
from tests.test_content_choice_contract import records


class PlanningLifecycleControls(unittest.IsolatedAsyncioTestCase):
    flow = initial.InitialAutoLinkContracts.flow
    admit_auto = initial.InitialAutoLinkContracts.admit_auto

    def dialog(self, flow, task_id=None):
        with Client.auto_index_client.content:
            view = page.PlanReviewDialog(None, flow.manager._read(task_id or flow.task_id))
            view.open()
        view.provider_url_input.value = 'https://www.themoviedb.org/movie/8181'
        return view

    @staticmethod
    def candidate():
        return initial.resolve_provider_item_url('https://www.themoviedb.org/movie/8181',
            tmdb_api_key='fixture-only', bangumi_user_agent='BAR/test',
            session=initial._Session(initial.detail(movie=True)))

    async def test_duplicate_click_and_draft_edit_keep_one_clicked_request(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view = self.dialog(f)
            entered, release = asyncio.Event(), asyncio.Event()
            async def reader(url):
                self.assertTrue(url.endswith('/8181'))
                entered.set(); await release.wait()
                return self.candidate()
            f.manager.executor = f.manager._execute_rename
            try:
                with patch.object(page, '_read_manual_provider_url', side_effect=reader) as read:
                    first = await work.submit(view)
                    await entered.wait()
                    view.provider_url_input.value = 'https://www.themoviedb.org/movie/999'
                    with self.assertRaisesRegex(ValueError, '取消'):
                        await work.submit(view)
                    release.set(); await asyncio.wait_for(first.worker, 20)
                self.assertFalse(first.error, first.error)
                self.assertEqual(1, read.await_count)
                self.assertEqual('8181', f.service.decision_state_store.load(f.task_id).confirmed_work['primary_provider']['provider_id'])
                self.assertNotIn(f.task_id, f.manager._provider_reads)
            finally:
                release.set(); view.delete()

    async def test_stopping_during_read_cannot_restart_queue_or_publish(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view = self.dialog(f)
            entered, release = asyncio.Event(), asyncio.Event()
            async def reader(_):
                entered.set(); await release.wait(); return self.candidate()
            before = records(f.root)
            try:
                with patch.object(page, '_read_manual_provider_url', side_effect=reader):
                    request = await work.submit(view); await asyncio.wait_for(entered.wait(), 5)
                    stopping = asyncio.create_task(f.manager.stop())
                    await asyncio.sleep(0)
                    self.assertTrue(f.manager._stopping)
                    release.set(); await asyncio.wait_for(stopping, 15)
                    await request.worker
                self.assertIn('停止', request.error)
                self.assertFalse(f.manager._started)
                self.assertTrue(f.manager._queue.empty())
                self.assertEqual(before, records(f.root))
            finally:
                release.set(); view.delete()

    async def test_saved_task_change_during_read_rejects_late_request(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view = self.dialog(f)
            entered, release = asyncio.Event(), asyncio.Event()
            async def reader(_):
                entered.set(); await release.wait(); return self.candidate()
            try:
                with patch.object(page, '_read_manual_provider_url', side_effect=reader):
                    request = await work.submit(view); await asyncio.wait_for(entered.wait(), 5)
                    f.manager._update(f.task_id, custom_season_id=7)
                    changed = records(f.root)
                    release.set(); await asyncio.wait_for(request.worker, 15)
                self.assertIn('变化', request.error)
                self.assertEqual(changed, records(f.root))
                self.assertFalse(f.store.path_for(f.task_id).exists())
            finally:
                release.set(); view.delete()

    async def test_reopened_window_observes_saved_result_without_second_read(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view = self.dialog(f)
            reopened = None
            entered, release = asyncio.Event(), asyncio.Event()
            async def reader(_):
                entered.set(); await release.wait(); return self.candidate()
            f.manager.executor = f.manager._execute_rename
            try:
                with patch.object(page, '_read_manual_provider_url', side_effect=reader) as read:
                    request = await work.submit(view); await asyncio.wait_for(entered.wait(), 5)
                    view._close_review()
                    reopened = self.dialog(f)
                    with patch.object(reopened, '_load_plan_view') as adopt:
                        observer = asyncio.create_task(work.watch(reopened, request))
                        release.set(); await asyncio.wait_for(observer, 20)
                        self.assertFalse(request.error, request.error)
                        adopt.assert_called_once()
                    self.assertEqual(1, read.await_count)
                    self.assertFalse(reopened.exit_requested)
            finally:
                release.set(); view.delete()
                if reopened is not None: reopened.delete()

    async def test_read_failure_remains_visible_and_can_be_retried(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view = self.dialog(f)
            before = records(f.root)
            try:
                with patch.object(page, '_read_manual_provider_url', side_effect=TimeoutError('读取超时')):
                    request = await work.submit(view); await request.worker
                self.assertIn('读取超时', request.error)
                self.assertEqual(before, records(f.root))
                with patch.object(page, 'notify') as notify:
                    await work.watch(view, request)
                    self.assertIn('读取超时', view.initial_progress.text)
                    notify.assert_not_called()
                f.manager.executor = f.manager._execute_rename
                with patch.object(page, '_read_manual_provider_url', return_value=self.candidate()):
                    retry = await work.submit(view); await asyncio.wait_for(retry.worker, 20)
                self.assertFalse(retry.error, retry.error)
                self.assertIsNot(request, retry)
            finally:
                view.delete()

    async def test_two_reads_overlap_and_plan_jobs_remain_fifo_single_worker(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            source = f.root / 'sources' / 'Second Movie'
            source.mkdir()
            (source / 'Second Movie.mkv').write_bytes(next(f.source.glob('*.mkv')).read_bytes())
            await self.admit_auto(f.manager, source, 'second-task')
            await f.manager._queue.join()
            first, second = self.dialog(f), self.dialog(f, 'second-task')
            release = asyncio.Event()
            reading = asyncio.Event()
            calls, active, peak, jobs = [], 0, 0, []
            async def reader(url):
                calls.append(url)
                if len(calls) == 2: reading.set()
                await release.wait(); return self.candidate()
            execute = f.manager._execute_rename
            async def executor(job):
                nonlocal active, peak
                active += 1; peak = max(peak, active); jobs.append(job.task_id)
                try: return await execute(job)
                finally: active -= 1
            f.manager.executor = executor
            admitted = []
            enqueue = f.manager.enqueue_reidentify
            async def admission(task_id, *args, **kwargs):
                await enqueue(task_id, *args, **kwargs); admitted.append(task_id)
            from src.rename import recognition_queue_admission as admission_module
            from contextlib import contextmanager
            import threading
            held, release_admission = threading.Event(), threading.Event()
            original_locks = admission_module._authority_locks
            @contextmanager
            def delayed_lock(manager, task_id):
                with original_locks(manager, task_id):
                    if not held.is_set():
                        held.set()
                        if not release_admission.wait(10): raise AssertionError('admission barrier timed out')
                    yield
            try:
                with patch.object(page, '_read_manual_provider_url', side_effect=reader), \
                        patch.object(admission_module, '_authority_locks', delayed_lock), \
                        patch.object(f.manager, 'enqueue_reidentify', side_effect=admission):
                    requests = [await work.submit(first), await work.submit(second)]
                    await asyncio.wait_for(reading.wait(), 10)
                    self.assertEqual([], jobs)
                    release.set()
                    self.assertTrue(await asyncio.to_thread(held.wait, 5))
                    await asyncio.sleep(0.15)
                    release_admission.set()
                    await asyncio.wait_for(asyncio.gather(*(r.worker for r in requests)), 30)
                self.assertEqual(['', ''], [r.error for r in requests])
                self.assertEqual(1, peak)
                self.assertEqual(admitted, jobs)
                self.assertEqual({f.task_id, 'second-task'}, set(jobs))
                for task_id in jobs:
                    self.assertTrue(all(not e.target_path.exists() for e in f.store.load(task_id).entries))
            finally:
                release.set(); release_admission.set(); first.delete(); second.delete()

    async def test_stop_during_workspace_finishes_preparation_without_late_admission(self):
        from src.pages import initial_workspace
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view = self.dialog(f)
            entered, release = asyncio.Event(), asyncio.Event()
            prepare = initial_workspace.create_initial_workspace
            async def delayed(*args, **kwargs):
                result = await prepare(*args, **kwargs)
                entered.set(); await release.wait()
                return result
            try:
                with patch.object(page, '_read_manual_provider_url', return_value=self.candidate()), \
                        patch.object(initial_workspace, 'create_initial_workspace', side_effect=delayed):
                    request = await work.submit(view); await asyncio.wait_for(entered.wait(), 15)
                    prepared = records(f.root)
                    stopping = asyncio.create_task(f.manager.stop()); await asyncio.sleep(0)
                    self.assertFalse(stopping.done())
                    release.set(); await asyncio.wait_for(stopping, 15); await request.worker
                self.assertIn('停止', request.error)
                self.assertEqual(prepared, records(f.root))
                self.assertFalse(f.manager._started)
                self.assertTrue(f.manager._queue.empty())
                self.assertFalse(f.service.decision_state_store.load(f.task_id).confirmed_work)
            finally:
                release.set(); view.delete()

    async def test_saved_authority_changes_during_read_are_not_overwritten(self):
        from src.pages.initial_workspace import create_initial_workspace
        from dataclasses import replace
        for change in ('plan', 'decision', 'task_missing', 'completed'):
            with self.subTest(change=change):
                async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
                    view = self.dialog(f)
                    entered, release = asyncio.Event(), asyncio.Event()
                    async def reader(_):
                        entered.set(); await release.wait(); return self.candidate()
                    try:
                        await create_initial_workspace(f.task_id, f.manager._read(f.task_id), media_type='movie')
                        view.task_data=f.manager._read(f.task_id)
                        with patch.object(page, '_read_manual_provider_url', side_effect=reader):
                            request = await work.submit(view); await asyncio.wait_for(entered.wait(), 5)
                            if change == 'plan':
                                old=f.store.load(f.task_id)
                                f.store.save(replace(old, revision_reason='人工修改'))
                            elif change == 'decision':
                                old=f.service.decision_state_store.load(f.task_id)
                                f.service.decision_state_store.save(replace(old, revision=old.revision+1), expected_revision=old.revision)
                            elif change == 'completed':
                                f.manager._update(f.task_id, execution_status='completed')
                            changed=records(f.root)
                            with patch.object(f.manager, '_read', return_value={}) if change == 'task_missing' else patch.object(page, 'notify'):
                                release.set(); await asyncio.wait_for(request.worker, 15)
                        self.assertIn('变化', request.error)
                        self.assertEqual(changed, records(f.root))
                        self.assertTrue(f.manager._queue.empty())
                    finally:
                        release.set(); view.delete()

    async def test_recovery_dialog_uses_same_background_request_after_close(self):
        from src.pages.recognition_recovery_view import RecognitionRecoveryDialog
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            with Client.auto_index_client.content:
                view=RecognitionRecoveryDialog(f.task_id,f.manager._read(f.task_id),ValueError('未产生计划'))
                view.open()
            view.provider_url.value='https://www.themoviedb.org/movie/8181'
            entered,release=asyncio.Event(),asyncio.Event()
            async def reader(_):
                entered.set();await release.wait();return self.candidate()
            f.manager.executor=f.manager._execute_rename
            try:
                with patch.object(page,'_read_manual_provider_url',side_effect=reader) as read:
                    observer=asyncio.create_task(view._select_url())
                    await asyncio.wait_for(entered.wait(),5)
                    request=work.current_work(f.manager,f.task_id)
                    view.close();self.assertTrue(view._closed)
                    release.set();await asyncio.wait_for(request.worker,20);await observer
                self.assertFalse(request.error,request.error)
                self.assertEqual(1,read.await_count)
                self.assertEqual('8181',f.store.load(f.task_id).work_identity.primary_provider.provider_id)
            finally:release.set();view.delete()
