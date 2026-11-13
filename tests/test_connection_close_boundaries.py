"""连接关闭策略与页面慢磁盘、请求生命周期控制。"""
import asyncio
import inspect
import runpy
import sys
import threading
import time
from types import SimpleNamespace
from contextlib import nullcontext
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.pages import data_table_page as dashboard, provider_selection_work as work
from src.pages import plan_review_page as page
from src.queue_runtime import disk_call
from tests import test_planning_lifecycle_controls as lifecycle


class CurrentResultReads(unittest.IsolatedAsyncioTestCase):
    flow = lifecycle.PlanningLifecycleControls.flow
    admit_auto = lifecycle.PlanningLifecycleControls.admit_auto
    dialog = lifecycle.PlanningLifecycleControls.dialog
    candidate = staticmethod(lifecycle.PlanningLifecycleControls.candidate)

    async def test_worker_rechecks_operation_after_final_disk_read(self):
        from tests.test_selection_failure_placement import failed_task
        original = work._read_result
        for replaced in (False, True):
            with self.subTest(replaced=replaced):
                async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
                    view = self.dialog(f)
                    f.manager.executor = f.manager._execute_rename
                    reads = []
                    def result(manager, task_id):
                        packet = original(manager, task_id)
                        reads.append(packet)
                        return (*packet[:2], failed_task('later')) if replaced else packet
                    try:
                        with patch.object(page, '_read_manual_provider_url', return_value=self.candidate()), \
                                patch.object(work, '_read_result', side_effect=result):
                            request = await work.submit(view)
                            await asyncio.wait_for(request.worker, 20)
                        self.assertEqual(1, len(reads))
                        if replaced:
                            self.assertIn('任务选择已更新', request.error)
                            self.assertIsNone(request.result)
                        else:
                            self.assertFalse(request.error, request.error)
                            self.assertIsNotNone(request.result)
                            self.assertEqual('succeeded', request.operation['lifecycle'])
                    finally:
                        view.delete()


class StartupPolicy(unittest.TestCase):
    def start(self, platform):
        policy=asyncio.get_event_loop_policy(); paths=sys.path[:]
        ui=SimpleNamespace(run=Mock())
        try:
            with patch('sys.platform',platform), patch.dict(sys.modules,{'src.web':SimpleNamespace(ui=ui)}):
                runpy.run_module('src.start',run_name='connection_start_probe')
            return ui.run.call_args.kwargs, asyncio.get_event_loop_policy(), policy
        finally:
            asyncio.set_event_loop_policy(policy);sys.path[:]=paths

    def test_windows_entry_selects_socket_loop(self):
        kwargs, policy, _=self.start('win32')
        self.assertEqual('none',kwargs.get('loop'))
        loop=policy.new_event_loop()
        try:
            self.assertIsInstance(loop,asyncio.SelectorEventLoop)
            self.assertIsNone(loop.get_exception_handler())
            with patch.object(loop,'default_exception_handler') as handler:
                error=RuntimeError('unrelated failure')
                loop.call_exception_handler({'message':'synthetic','exception':error})
                handler.assert_called_once()
                self.assertIs(error,handler.call_args.args[0]['exception'])
        finally:loop.close()

    def test_linux_entry_keeps_default_loop_and_session_policy(self):
        kwargs, policy, original=self.start('linux')
        self.assertEqual('auto',kwargs.get('loop','auto'))
        self.assertIs(policy,original)
        self.assertEqual(30,kwargs['reconnect_timeout'])


class SlowPageReads(unittest.IsolatedAsyncioTestCase):
    async def live_during(self, operation):
        ticks=[]; running=True
        async def tick():
            while running:
                ticks.append(time.monotonic());await asyncio.sleep(.005)
        ticker=asyncio.create_task(tick());await asyncio.sleep(.01)
        count=len(ticks)
        try:
            result=await operation
            self.assertGreaterEqual(len(ticks)-count,3,'磁盘等待期间事件循环停止')
            return result
        finally:
            running=False;await ticker

    async def test_dashboard_action_reads_leave_loop_live(self):
        names=('_open_review','_retry_task','_retry_approved_execution',
               '_replan_failed_execution','_delete_task','_submit_safe_end','_open_execution_reapproval')
        for name in names:
            seen=[];main=threading.get_ident()
            def slow(*args):
                seen.append(threading.get_ident());time.sleep(.08)
                raise OSError('synthetic unavailable task')
            async def call():
                with self.assertRaises(OSError):await getattr(dashboard,name)('task')
            with self.subTest(name=name),patch.object(dashboard,'get_task',slow):
                await self.live_during(call())
                self.assertTrue(seen);self.assertNotIn(main,seen)

    async def test_provider_check_leaves_loop_live_and_rejects_read_failure(self):
        main=threading.get_ident();seen=[]
        def slow(*args):
            seen.append(threading.get_ident());time.sleep(.1);raise OSError('synthetic unreadable')
        manager=SimpleNamespace(_stopping=False,_admission_epoch=1,_read=slow,
                                _admissions=set(),_provider_reads={})
        pending=SimpleNamespace(task_id='task',epoch=1,result=None,error='',status='')
        reader=AsyncMock()
        await self.live_during(work._run(manager,pending,reader,False,None,'anime','source',None))
        self.assertIn('unreadable',pending.error);reader.assert_not_called()
        self.assertTrue(seen);self.assertNotIn(main,seen)

    async def test_safe_end_uses_background_canonical_service(self):
        seen=[];main=threading.get_ident()
        def finish(*args,**kwargs):
            seen.append(threading.get_ident());time.sleep(.1)
        manager=SimpleNamespace(cancel_queued=Mock(side_effect=AssertionError('UI不能先取消持久任务')),
                                abandon_and_remove=Mock(side_effect=finish))
        with patch.object(dashboard,'get_task',return_value={'uuid':'task','workflow_status':'queued'}), \
                patch.object(dashboard,'task_queue',manager),patch.object(dashboard,'notify'), \
                patch.object(dashboard,'refresh_dashboard'):
            self.assertTrue(await self.live_during(dashboard._submit_safe_end('task')))
        manager.cancel_queued.assert_not_called();manager.abandon_and_remove.assert_called_once()
        self.assertTrue(seen);self.assertNotIn(main,seen)

    def setup_submit(self):
        task=dict(uuid='task',path='source',is_anime=True,is_movie=False)
        manager=SimpleNamespace(_read=Mock(return_value=task),_admission_epoch=1,
            _admission_lock=asyncio.Lock(),_stopping=False,_provider_reads={},_pending=set(),active_task_id=None,
            _admissions=set(),plan_store=Mock(),decision_state_store=Mock())
        manager.plan_store.path_for.return_value.exists.return_value=False
        manager.decision_state_store.path_for.return_value.exists.return_value=False
        view=SimpleNamespace(task_id='task',task_data=task,plan=None,task_decision=None,
            provider_url_input=SimpleNamespace(value='https://bangumi.tv/subject/43951'))
        return view,manager

    async def test_submit_prepares_off_loop_and_keeps_clicked_input(self):
        view,manager=self.setup_submit();main=threading.get_ident();seen=[]
        task=dict(view.task_data)
        def slow(*args):
            seen.append(threading.get_ident());time.sleep(.09);return task
        manager._read.side_effect=slow
        async def edit():
            await asyncio.sleep(.02);view.provider_url_input.value='https://bangumi.tv/subject/999'
        edit_task=asyncio.create_task(edit())
        with patch.object(page,'task_queue',manager),patch.object(work,'_run',new=AsyncMock()) as run:
            pending=await self.live_during(work.submit(view))
            await pending.worker;await edit_task
        self.assertTrue(seen);self.assertNotIn(main,seen)
        self.assertTrue(pending.url.endswith('/43951'));run.assert_awaited_once()

    async def test_submit_stopped_during_preparation_never_registers_worker(self):
        view,manager=self.setup_submit();task=dict(view.task_data)
        def slow(*args):time.sleep(.1);return task
        manager._read.side_effect=slow
        async def stop():
            await asyncio.sleep(.02);manager._stopping=True;manager._admission_epoch+=1
        stopper=asyncio.create_task(stop())
        with patch.object(page,'task_queue',manager),patch.object(work,'_run',new=AsyncMock()) as run:
            with self.assertRaisesRegex(ValueError,'停止'):
                await work.submit(view)
            await stopper
        self.assertEqual({},manager._provider_reads);self.assertEqual(set(),manager._admissions)
        run.assert_not_called()

    async def test_epoch_changed_without_stopping_never_registers(self):
        view,manager=self.setup_submit();task=dict(view.task_data)
        def slow(*args):time.sleep(.1);return task
        manager._read.side_effect=slow
        async def restart():
            await asyncio.sleep(.02);manager._admission_epoch+=1
        changer=asyncio.create_task(restart())
        with patch.object(page,'task_queue',manager),patch.object(work,'_run',new=AsyncMock()) as run:
            with self.assertRaisesRegex(ValueError,'停止'):await work.submit(view)
            await changer
        self.assertFalse(manager._stopping);self.assertEqual({},manager._provider_reads)
        self.assertEqual(set(),manager._admissions);run.assert_not_called()

    async def test_concurrent_submit_registers_one_worker(self):
        view,manager=self.setup_submit();release=asyncio.Event()
        async def blocked(*args):await release.wait()
        with patch.object(page,'task_queue',manager),patch.object(work,'_run',new=AsyncMock(side_effect=blocked)) as run:
            try:
                first,second=await asyncio.gather(work.submit(view),work.submit(view))
                self.assertIs(first,second)
                self.assertEqual(1,len(manager._provider_reads));self.assertEqual(1,len(manager._admissions))
            finally:
                release.set()
                await asyncio.gather(*[p.worker for p in manager._provider_reads.values()])
        run.assert_awaited_once()

    async def test_cancelled_submit_waits_for_read_then_never_registers(self):
        view,manager=self.setup_submit();entered=threading.Event();release=threading.Event();finished=threading.Event()
        def slow(*args):
            entered.set();release.wait(2);finished.set();return dict(view.task_data)
        manager._read.side_effect=slow
        with patch.object(page,'task_queue',manager),patch.object(work,'_run',new=AsyncMock()) as run:
            request=asyncio.create_task(work.submit(view))
            try:
                for _ in range(100):
                    if entered.is_set():break
                    await asyncio.sleep(.005)
                self.assertTrue(entered.is_set());request.cancel();await asyncio.sleep(.03)
                self.assertFalse(request.done())
                await asyncio.sleep(.1)
                self.assertFalse(finished.is_set());self.assertFalse(request.done())
            finally:release.set()
            with self.assertRaises(asyncio.CancelledError):await request
            self.assertTrue(finished.is_set())
        self.assertEqual({},manager._provider_reads);run.assert_not_called()

    async def test_current_pending_source_rejected_before_provider_or_store_read(self):
        view,manager=self.setup_submit()
        manager._read.return_value=dict(view.task_data,source_group_pending=True)
        with patch.object(page,'task_queue',manager),patch.object(page,'_read_manual_provider_url',new=AsyncMock()) as read:
            with self.assertRaisesRegex(ValueError,'来源'):await work.submit(view)
        read.assert_not_called();manager.plan_store.path_for.assert_not_called()
        self.assertEqual({},manager._provider_reads)

    async def test_success_read_rechecks_window_worker_and_persisted_operation(self):
        from src.rename.review_operation import current_provider_selection_review_operation
        from tests.test_selection_failure_placement import failed_task
        for interruption in ('closed', 'worker', 'persisted'):
            with self.subTest(interruption=interruption):
                done=asyncio.get_running_loop().create_future();done.set_result(None)
                original=failed_task('original')
                operation=current_provider_selection_review_operation(original)[1]
                pending=SimpleNamespace(task_id='task',worker=done,error='',admitted=True,
                    result=(Mock(),Mock()),route='',status='',operation=operation)
                manager=SimpleNamespace(_provider_reads={'task':pending},active_task_id=None)
                view=SimpleNamespace(client=nullcontext(),plan=None,_adopt_revision=Mock())
                async def read(*args):
                    await asyncio.sleep(.01)
                    if interruption=='closed':view.exit_requested=True
                    if interruption=='worker':manager._provider_reads['task']=object()
                    return (*pending.result, failed_task('later') if interruption=='persisted' else original)
                with patch.object(page,'task_queue',manager),patch.object(work,'disk_call',side_effect=read), \
                        patch.object(page,'_prepare_visible_plan_dialog',new=AsyncMock()) as prepare,patch.object(page,'notify') as notice:
                    await work.watch(view,pending)
                prepare.assert_not_called();notice.assert_not_called();view._adopt_revision.assert_not_called()

    async def test_disk_cancellation_control_waits_for_thread(self):
        entered=threading.Event();release=threading.Event()
        def slow():entered.set();release.wait(1)
        operation=asyncio.create_task(disk_call(slow))
        try:
            for _ in range(100):
                if entered.is_set():break
                await asyncio.sleep(.005)
            self.assertTrue(entered.is_set());operation.cancel();await asyncio.sleep(.03)
            self.assertFalse(operation.done())
        finally:release.set()
        with self.assertRaises(asyncio.CancelledError):await operation
