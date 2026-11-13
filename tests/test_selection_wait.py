"""作品选择的忙碌反馈、取消与原任务复用。"""
import asyncio
import unittest
from unittest.mock import patch
from src.pages import plan_review_page as page, provider_selection_work as work
from tests import test_planning_lifecycle_controls as lifecycle
from tests.test_content_choice_contract import records


class SelectionWaitContracts(unittest.IsolatedAsyncioTestCase):
    flow = lifecycle.PlanningLifecycleControls.flow
    admit_auto = lifecycle.PlanningLifecycleControls.admit_auto
    dialog = lifecycle.PlanningLifecycleControls.dialog
    candidate = staticmethod(lifecycle.PlanningLifecycleControls.candidate)

    async def test_busy_admission_click_is_registered_immediately(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view = self.dialog(f)
            before = records(f.root)
            await f.manager._admission_lock.acquire()
            submitted = asyncio.create_task(work.submit(view))
            try:
                done, _ = await asyncio.wait({submitted}, timeout=0.15)
                self.assertTrue(done, '点击反馈不得等待公共接纳锁')
                request = submitted.result()
                self.assertIs(request, f.manager._provider_reads[f.task_id])
                self.assertIn('等待', request.status)
                self.assertIs(request, await work.submit(view))
                self.assertTrue(await work.cancel(f.manager, f.task_id))
                self.assertFalse(request.admitted)
                f.manager._admission_lock.release()
                await asyncio.wait_for(asyncio.shield(request.worker), 3)
                self.assertTrue(f.manager._queue.empty())
                self.assertEqual(before, records(f.root))
                self.assertEqual('need_confirm', f.manager._read(f.task_id)['workflow_status'])
            finally:
                if not submitted.done():
                    submitted.cancel()
                    await asyncio.gather(submitted, return_exceptions=True)
                if f.manager._admission_lock.locked(): f.manager._admission_lock.release()
                view.delete()

    async def test_read_cancel_preserves_task_and_input_and_never_enqueues(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view = self.dialog(f)
            entered, release = asyncio.Event(), asyncio.Event()
            async def reader(_):
                entered.set(); await release.wait(); return self.candidate()
            before = records(f.root)
            try:
                with patch.object(page, '_read_manual_provider_url', side_effect=reader):
                    request = await work.submit(view)
                    await asyncio.wait_for(entered.wait(), 5)
                    self.assertTrue(await work.cancel(f.manager, f.task_id))
                    release.set()
                    await request.worker
                self.assertTrue(request.cancelled)
                self.assertEqual(before, records(f.root))
                self.assertTrue(f.manager._queue.empty())
                self.assertEqual('https://www.themoviedb.org/movie/8181', view.provider_url_input.value)
            finally:
                release.set()
                if 'request' in locals():
                    if not request.worker.done(): request.worker.cancel()
                    await asyncio.gather(request.worker, return_exceptions=True)
                view.delete()

    async def test_unblocked_submission_control_reaches_reader(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view = self.dialog(f)
            try:
                with patch.object(page, '_read_manual_provider_url', side_effect=ValueError('控制组读取完成')) as reader:
                    request = await work.submit(view)
                    await asyncio.wait_for(request.worker, 5)
                self.assertEqual(1, reader.await_count)
                self.assertIn('控制组读取完成', request.error)
            finally:
                view.delete()

    async def wait_admitted(self, request):
        for _ in range(500):
            if request.admitted:
                from src.pages import plan_review_page as current_page
                from src.queue_runtime import PUBLICATION_TASK_NAME
                await asyncio.gather(*(asyncio.shield(item) for item in tuple(current_page.task_queue._admissions)
                    if item.get_name() == PUBLICATION_TASK_NAME), return_exceptions=True)
                return
            if request.worker.done(): self.fail(request.error or request.status)
            await asyncio.sleep(0.01)
        self.fail('请求未入队')

    async def stop_idle_consumer(self, manager):
        manager._worker_task.cancel()
        await asyncio.gather(manager._worker_task, return_exceptions=True)
        manager._worker_task = None

    async def test_queued_cancel_preserves_authorities_and_retries_original_task(self):
        from src.pages.initial_workspace import create_initial_workspace
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            await create_initial_workspace(f.task_id, f.manager._read(f.task_id), media_type='movie')
            import json
            record_path = f.manager.task_root / (f.task_id + '.json')
            record_path.write_text(json.dumps(f.manager._read(f.task_id), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            view = self.dialog(f)
            await self.stop_idle_consumer(f.manager)
            before = records(f.root)
            try:
                with patch.object(page, '_read_manual_provider_url', return_value=self.candidate()):
                    request = await work.submit(view)
                    await self.wait_admitted(request)
                    self.assertTrue(await work.cancel(f.manager, f.task_id))
                    self.assertEqual(before, records(f.root))
                    self.assertNotIn(f.task_id, f.manager._pending)
                    self.assertEqual('invalid', f.manager._read(f.task_id)['workflow_status'])
                    called = []
                    execute = f.manager._execute_rename
                    async def executor(job):
                        called.append(job.request_id)
                        return await execute(job)
                    f.manager.executor = executor
                    # 同一任务的新请求与被撤回旧队列项同时存在。
                    retry = await work.submit(view)
                    await self.wait_admitted(retry)
                    f.manager._worker_task = asyncio.create_task(f.manager._worker())
                    await asyncio.wait_for(retry.worker, 15)
                    self.assertFalse(retry.error, retry.error)
                    self.assertEqual([retry.request_id], called)
                    self.assertNotEqual(request.request_id, retry.request_id)
            finally:
                if 'request' in locals() and not request.worker.done():
                    request.worker.cancel(); await asyncio.gather(request.worker, return_exceptions=True)
                view.delete()

    async def test_already_started_cannot_be_cancelled(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view = self.dialog(f)
            entered, release = asyncio.Event(), asyncio.Event()
            execute = f.manager._execute_rename
            async def executor(job):
                entered.set(); await release.wait(); return await execute(job)
            f.manager.executor = executor
            try:
                with patch.object(page, '_read_manual_provider_url', return_value=self.candidate()):
                    request = await work.submit(view)
                    await asyncio.wait_for(entered.wait(), 10)
                    before = records(f.root)
                    self.assertFalse(await work.cancel(f.manager, f.task_id))
                    self.assertEqual(before, records(f.root))
                    release.set(); await asyncio.wait_for(request.worker, 15)
                    self.assertFalse(request.error, request.error)
            finally:
                release.set()
                if 'request' in locals(): await asyncio.gather(request.worker, return_exceptions=True)
                view.delete()

    async def test_changed_second_click_is_explicitly_rejected(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view = self.dialog(f)
            entered, release = asyncio.Event(), asyncio.Event()
            async def reader(_):
                entered.set(); await release.wait(); raise ValueError('控制组结束')
            try:
                before = records(f.root)
                with patch.object(page, '_read_manual_provider_url', side_effect=reader) as read:
                    request = await work.submit(view)
                    await entered.wait()
                    view.provider_url_input.value = 'https://www.themoviedb.org/movie/999'
                    with self.assertRaisesRegex(ValueError, '取消'):
                        await work.submit(view)
                    self.assertTrue(request.url.endswith('/8181'))
                    self.assertTrue(view.provider_url_input.value.endswith('/999'))
                    self.assertIs(request, f.manager._provider_reads[f.task_id])
                    self.assertFalse(request.worker.done())
                    self.assertFalse(getattr(request, 'cancelled', False))
                    release.set(); await request.worker
                    self.assertEqual(1, read.await_count)
                    self.assertIn('控制组结束', request.error)
                    self.assertEqual(before, records(f.root))
                    self.assertTrue(f.manager._queue.empty())
                    self.assertEqual([], f.manager._pending)
            finally:
                release.set()
                if 'request' in locals(): await request.worker
                view.delete()

    async def test_real_link_callback_shows_wait_and_queue_projection_before_lock(self):
        from src.pages import data_table_page as dashboard
        from types import SimpleNamespace
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view = self.dialog(f)
            await f.manager._admission_lock.acquire()
            click = asyncio.create_task(view._select_manual_provider_url())
            try:
                await asyncio.sleep(0.1)
                request = f.manager._provider_reads[f.task_id]
                self.assertIn('等待', request.status)
                self.assertFalse(view.value)
                row = dashboard._card_projection(f.manager._read(f.task_id))
                published=[]
                key='selection-test'
                with patch.object(dashboard, 'task_queue', f.manager), patch.dict(dashboard._dashboard_views, {key: SimpleNamespace(publish=lambda *args: published.append(args))}):
                    dashboard._render_dashboard(key, ([row], (), {}))
                self.assertEqual([f.task_id], [x['uuid'] for x in published[0][0]['active']])
                self.assertEqual([], published[0][0]['review'])
                self.assertTrue(await work.cancel(f.manager, f.task_id))
                await asyncio.wait_for(click, 3)
                self.assertEqual('need_confirm', f.manager._read(f.task_id)['workflow_status'])
            finally:
                if not click.done(): click.cancel()
                await asyncio.gather(click, return_exceptions=True)
                f.manager._admission_lock.release(); view.delete()

    async def test_consumer_dequeued_waits_for_cancel_publication(self):
        import threading
        from src.rename import selection_wait
        from src.queue_runtime import PUBLICATION_TASK_NAME
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view = self.dialog(f)
            await self.stop_idle_consumer(f.manager)
            release_old, dequeued = asyncio.Event(), asyncio.Event()
            entered, release_cancel = threading.Event(), threading.Event()
            original = selection_wait._withdraw_record
            def delayed(*args):
                entered.set()
                if not release_cancel.wait(5): raise AssertionError('取消事务未释放')
                return original(*args)
            get = f.manager._queue.get
            async def observed_get():
                job = await get(); dequeued.set(); return job
            ran=[]
            async def forbidden(job): ran.append(job.request_id)
            f.manager.executor=forbidden
            try:
                with patch.object(page, '_read_manual_provider_url', return_value=self.candidate()):
                    request=await work.submit(view); await self.wait_admitted(request)
                blocker=asyncio.create_task(release_old.wait(),name=PUBLICATION_TASK_NAME)
                f.manager._admissions.add(blocker)
                blocker.add_done_callback(f.manager._admissions.discard)
                with patch.object(f.manager._queue,'get',side_effect=observed_get), patch.object(selection_wait,'_withdraw_record',side_effect=delayed):
                    f.manager._worker_task=asyncio.create_task(f.manager._worker())
                    await asyncio.wait_for(dequeued.wait(),3)
                    cancelling=asyncio.create_task(work.cancel(f.manager,f.task_id))
                    self.assertTrue(await asyncio.to_thread(entered.wait,3))
                    # 消费者已经等待旧publication；新取消事务必须也被等待。
                    release_old.set(); await asyncio.sleep(.05)
                    self.assertIsNone(f.manager.active_task_id)
                    self.assertEqual([],ran)
                    release_cancel.set()
                    self.assertTrue(await asyncio.wait_for(cancelling,3))
                    await asyncio.wait_for(f.manager._queue.join(),3)
                    self.assertEqual([],ran)
            finally:
                release_old.set();release_cancel.set()
                if 'request' in locals() and not request.worker.done():
                    request.worker.cancel();await asyncio.gather(request.worker,return_exceptions=True)
                view.delete()

    async def test_cancel_joins_real_reader_thread_and_reopen_restores_draft(self):
        import threading
        from src.queue_runtime import disk_call
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view=self.dialog(f)
            view.rerecognition_name.value='人工名称'
            view.rerecognition_season.value=7
            entered, release, finished=threading.Event(),threading.Event(),threading.Event()
            def read():
                entered.set()
                try:
                    if not release.wait(5): raise AssertionError('读取未释放')
                    return self.candidate()
                finally: finished.set()
            async def reader(_): return await disk_call(read)
            reopened=None
            try:
                with patch.object(page,'_read_manual_provider_url',side_effect=reader):
                    request=await work.submit(view)
                    self.assertTrue(await asyncio.to_thread(entered.wait,3))
                    cancelling=asyncio.create_task(work.cancel(f.manager,f.task_id))
                    await asyncio.sleep(.05)
                    self.assertFalse(cancelling.done())
                    self.assertTrue(request.cancelling)
                    release.set();self.assertTrue(await asyncio.wait_for(cancelling,3))
                    self.assertTrue(finished.is_set())
                    reopened=self.dialog(f)
                    with patch.object(page.ui,'timer'):
                        work.attach(reopened)
                    self.assertEqual('人工名称',reopened.rerecognition_name.value)
                    self.assertEqual(7,reopened.rerecognition_season.value)
                    self.assertEqual(view.provider_url_input.value,reopened.provider_url_input.value)
                    self.assertTrue(f.manager._queue.empty())
            finally:
                release.set();view.delete()
                if reopened is not None:reopened.delete()

    async def test_drift_blocks_withdrawal_without_overwriting_task(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view=self.dialog(f)
            await self.stop_idle_consumer(f.manager)
            try:
                with patch.object(page,'_read_manual_provider_url',return_value=self.candidate()):
                    request=await work.submit(view);await self.wait_admitted(request)
                f.manager._update(f.task_id,custom_name='稍后的人工修改')
                before=records(f.root)
                with self.assertRaisesRegex(ValueError,'变化'):
                    await work.cancel(f.manager,f.task_id)
                self.assertEqual(before,records(f.root))
                self.assertFalse(request.cancelled)
                self.assertIn('取消等待失败',request.status)
                self.assertIn(f.task_id,f.manager._pending)
            finally:
                if 'request' in locals():
                    request.worker.cancel();await asyncio.gather(request.worker,return_exceptions=True)
                view.delete()
