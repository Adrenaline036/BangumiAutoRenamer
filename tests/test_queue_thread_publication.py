"""后台事务的取消、停止、接纳线程与日志拥堵控制。"""
import asyncio
import logging
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.queue_runtime import transaction, on_owner
from src.rename.recognition_queue_admission import _accept, TaskAcceptanceUncertain
from src.rename.plan_execution import PlanExecutionError
from src.task_queue import TaskJob, TaskQueueManager
from src.log_dispatch import LogDispatch


def manager():
    return SimpleNamespace(_admissions=set(),_pending=[],_queue=asyncio.Queue(),
        _started=True,_stopping=False,_admission_epoch=1,active_task_id=None,
        _tv_refresh_accepted={},_plan_updates={},is_active=lambda task_id:False)


async def wait_entered(event):
    for _ in range(200):
        if event.is_set():return
        await asyncio.sleep(.005)
    raise AssertionError('工作线程未进入')


class ThreadPublication(unittest.IsolatedAsyncioTestCase):
    async def test_acceptance_callback_runs_on_owner(self):
        m=manager(); main=threading.get_ident(); seen=[]
        job=TaskJob(task_id='synthetic',path=None)
        def commit():
            self.assertNotEqual(main,threading.get_ident())
            on_owner(lambda:seen.append(threading.get_ident()))
            _accept(m,job,expected_epoch=1)
        await transaction(m,commit)
        self.assertEqual([main],seen)
        self.assertIs(job,m._queue.get_nowait());m._queue.task_done()
        self.assertEqual(set(),m._admissions)

    async def test_cancel_waits_then_refuses_acceptance(self):
        m=manager(); entered=threading.Event(); release=threading.Event()
        def commit():
            entered.set(); release.wait(2)
            _accept(m,TaskJob(task_id='synthetic',path=None),expected_epoch=1)
        task=asyncio.create_task(transaction(m,commit))
        try:
            await wait_entered(entered); task.cancel(); await asyncio.sleep(.03)
            self.assertFalse(task.done()); self.assertTrue(m._admissions)
        finally:release.set()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual([],m._pending);self.assertTrue(m._queue.empty())

    async def test_epoch_change_refuses_acceptance(self):
        m=manager(); entered=threading.Event(); release=threading.Event()
        def commit():
            entered.set();release.wait(2)
            _accept(m,TaskJob(task_id='synthetic',path=None),expected_epoch=1)
        task=asyncio.create_task(transaction(m,commit))
        await wait_entered(entered);m._admission_epoch=2;release.set()
        with self.assertRaises(PlanExecutionError):await task
        self.assertTrue(m._queue.empty())

    async def test_duplicate_publications_accept_once(self):
        m=manager()
        def commit():_accept(m,TaskJob(task_id='synthetic',path=None),expected_epoch=1)
        results=await asyncio.gather(transaction(m,commit),transaction(m,commit),return_exceptions=True)
        self.assertEqual(1,sum(isinstance(result,PlanExecutionError) for result in results))
        self.assertEqual(['synthetic'],m._pending);self.assertEqual(1,m._queue.qsize())

    async def test_insert_then_raise_is_quarantined_before_consumer(self):
        m=manager();put=m._queue.put_nowait
        def broken(job):put(job);raise RuntimeError('插入后抛错')
        with patch.object(m._queue,'put_nowait',broken):
            with self.assertRaises(TaskAcceptanceUncertain):
                await transaction(m,_accept,m,TaskJob(task_id='synthetic',path=None),expected_epoch=1)
        self.assertTrue(m._queue.empty());self.assertEqual(0,m._queue._unfinished_tasks)
        self.assertEqual(['synthetic'],m._pending)

    async def test_stop_waits_for_real_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            m=TaskQueueManager(task_root=root/'tasks',plan_root=root/'plans',
                decision_state_root=root/'decisions',recovery_abandon_audit_root=root/'abandon')
            m._started=True
            entered=threading.Event();release=threading.Event()
            def commit():
                entered.set();release.wait(2)
                _accept(m,TaskJob(task_id='synthetic',path=None),expected_epoch=0)
            task=asyncio.create_task(transaction(m,commit))
            await wait_entered(entered)
            stopping=asyncio.create_task(m.stop());await asyncio.sleep(.03)
            self.assertFalse(stopping.done())
            release.set()
            with self.assertRaises(PlanExecutionError):await task
            await stopping
            self.assertTrue(m._queue.empty());self.assertFalse(m._started)


class LogBackpressure(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_queue_prioritizes_error_and_reports_overflow(self):
        entered=threading.Event();release=threading.Event();seen=[]
        class Sink(logging.Handler):
            def emit(self,record):
                if not entered.is_set():entered.set();release.wait(2)
                seen.append(record.getMessage())
        dispatch=LogDispatch([Sink()],capacity=2)
        def emit(level,message):dispatch.handle(logging.LogRecord('probe',level,'',0,message,(),None))
        try:
            emit(logging.INFO,'first');await wait_entered(entered)
            emit(logging.INFO,'queued-one');emit(logging.INFO,'queued-two')
            begin=time.perf_counter();emit(logging.ERROR,'important-error');emit(logging.INFO,'overflow')
            self.assertLess(time.perf_counter()-begin,.05)
            self.assertLessEqual(dispatch.records.qsize(),2)
        finally:
            release.set();result=await asyncio.to_thread(dispatch.drain)
        self.assertTrue(result['stopped'])
        self.assertIn('important-error',seen)
        self.assertTrue(any('未保留的条数' in value for value in seen))
        emit(logging.ERROR,'after-close')
        self.assertEqual(1,dispatch.dropped['ERROR'])

    async def test_drain_timeout_does_not_block_event_loop(self):
        entered=threading.Event();release=threading.Event()
        class Sink(logging.Handler):
            def emit(self,record):entered.set();release.wait(2)
        dispatch=LogDispatch([Sink()],capacity=2)
        dispatch.handle(logging.LogRecord('probe',logging.ERROR,'',0,'hold',(),None))
        try:
            await wait_entered(entered)
            task=asyncio.create_task(asyncio.to_thread(dispatch.drain,.06))
            ticks=0
            while not task.done():await asyncio.sleep(.01);ticks+=1
            result=await task
            self.assertGreaterEqual(ticks,3);self.assertFalse(result['stopped'])
            self.assertEqual(1,result['pending'])
        finally:
            release.set();await asyncio.to_thread(dispatch.drain)

    async def test_product_root_uses_only_background_producer(self):
        import src.logger as module
        root=logging.getLogger()
        self.assertIn(module.producer_handler,root.handlers)
        self.assertNotIn(module.console_handler,root.handlers)
        self.assertNotIn(module.file_handler,root.handlers)
