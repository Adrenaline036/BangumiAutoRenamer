"""慢磁盘、慢日志与短断线的事故形状回归。"""
import asyncio
from contextlib import contextmanager
from functools import partial
from pathlib import Path
import logging
import runpy
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from src.task_queue import TaskQueueManager


class ProbeStop(RuntimeError):
    pass


class ConnectionStability(unittest.IsolatedAsyncioTestCase):
    async def observe_live_loop(self, operation):
        ticks=[]
        async def tick():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(.005)
        ticker=asyncio.create_task(tick())
        await asyncio.sleep(.01)
        before=len(ticks)
        try:
            result=await operation
            self.assertGreater(len(ticks)-before,3,'工作期间界面调度停止，不能同步等待工作线程')
            return result
        finally:
            ticker.cancel()
            try:await ticker
            except asyncio.CancelledError:pass

    async def check_read_boundary(self, method, **kwargs):
        seen = []
        main = threading.get_ident()
        def slow(*args, **kw):
            seen.append(threading.get_ident())
            time.sleep(.12)
            raise ProbeStop('隔离读取到此停止')
        manager = object.__new__(TaskQueueManager)
        manager.__dict__.update(_read=slow, _require_no_pending_partition=slow, _admissions=set(),
                                _admission_epoch=0, _stopping=False, _disk_calls=set())
        async def invoke():
            with self.assertRaises(ProbeStop):
                await method(manager, **kwargs)
        await self.observe_live_loop(invoke())
        self.assertTrue(seen)
        self.assertNotIn(main, seen, '同步磁盘边界阻塞了界面事件循环')

    async def test_worker_preparation_read_is_off_loop(self):
        await self.check_read_boundary(TaskQueueManager._execute_rename,
                                      job=SimpleNamespace(task_id='synthetic'))

    async def test_retry_read_is_off_loop(self):
        await self.check_read_boundary(TaskQueueManager.retry_current, task_id='synthetic')

    async def test_execution_partition_check_is_off_loop(self):
        await self.check_read_boundary(TaskQueueManager._enqueue_execution,
                                      task_id='synthetic', expected_fingerprint='fixed')

    async def test_current_work_claim_is_off_loop(self):
        from src.rename import current_tv_refresh as refresh
        seen=[]; main=threading.get_ident()
        @contextmanager
        def slow(*args):
            seen.append(threading.get_ident()); time.sleep(.12)
            raise ProbeStop('隔离事务停止')
            yield
        manager=SimpleNamespace(_stopping=False, _disk_calls=set(),
                                _tv_refresh_accepted={}, active_task_id=None)
        manager._disk=partial(TaskQueueManager._disk, manager)
        job=SimpleNamespace(task_id='synthetic', request_id='synthetic')
        with patch('src.rename.recognition_queue_admission._authority_locks', slow):
            await self.observe_live_loop(refresh.drive(manager, job))
        self.assertTrue(seen)
        self.assertNotIn(main,seen)

    async def test_recognition_commit_is_off_loop(self):
        from src.rename import recognition_queue_admission as admission
        prepared=object.__new__(admission.RecognitionAdmissionPreparation)
        object.__setattr__(prepared,'epoch',0)
        manager=object.__new__(TaskQueueManager)
        manager.__dict__.update(_provider_reads={},_admission_epoch=0,_admissions=set(),
            _admission_lock=asyncio.Lock(),_stopping=False,_started=True,
            active_task_id=None,active_execution_task_id=None,_pending=[],_queue=asyncio.Queue(),_plan_updates={},
            _disk_calls=set())
        seen=[]; main=threading.get_ident()
        def slow(*args):
            seen.append(threading.get_ident()); time.sleep(.12); return 'synthetic'
        with patch.object(admission,'_prepare_readonly',return_value=prepared), patch.object(admission,'commit',slow):
            self.assertEqual('synthetic',await self.observe_live_loop(admission.admit(manager,'synthetic',mode='retry',request={})))
        self.assertNotIn(main,seen)

    async def test_slow_log_sink_does_not_block_producer(self):
        import src.logger as module
        seen=[]; main=threading.get_ident()
        root=logging.getLogger()
        saved=root.handlers[:]
        # 测试框架的根handler不属于产品；显式装入产品生产者再测试实际输出链。
        producer=getattr(module,'producer_handler',module.console_handler)
        root.handlers=[producer]
        def slow(record):
            seen.append(threading.get_ident()); time.sleep(.15)
        try:
            with patch.object(module.console_handler,'emit',slow):
                begin=time.perf_counter()
                module.logger.error('连接回归合成日志')
                elapsed=time.perf_counter()-begin
                for _ in range(100):
                    if seen:break
                    await asyncio.sleep(.01)
                await asyncio.sleep(.18)
        finally:root.handlers=saved
        self.assertTrue(seen)
        self.assertLess(elapsed,.08)
        self.assertNotIn(main,seen)

    async def test_connection_policy_keeps_short_disconnect_session(self):
        ui=SimpleNamespace(run=Mock())
        previous=sys.path[:]
        try:
            with patch.dict(sys.modules,{'src.web':SimpleNamespace(ui=ui)}):
                runpy.run_module('src.start',run_name='src.start_probe')
        finally:sys.path[:]=previous
        ui.run.assert_called_once()
        self.assertEqual(30,ui.run.call_args.kwargs.get('reconnect_timeout'),
                         '实际启动参数没有绑定30秒会话恢复策略')

    async def test_disk_cancellation_waits_for_real_thread_control(self):
        entered=threading.Event(); release=threading.Event(); finished=threading.Event()
        def work():
            entered.set(); release.wait(2); finished.set()
        task=asyncio.create_task(TaskQueueManager._disk(SimpleNamespace(_stopping=False, _disk_calls=set()), work))
        try:
            while not entered.is_set():await asyncio.sleep(.01)
            task.cancel(); await asyncio.sleep(.04)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(finished.is_set())

    async def test_background_read_control_keeps_loop_live(self):
        entered=threading.Event(); release=threading.Event()
        def work():entered.set(); release.wait(2)
        task=asyncio.create_task(TaskQueueManager._disk(SimpleNamespace(_stopping=False, _disk_calls=set()), work))
        try:
            while not entered.is_set():await asyncio.sleep(.01)
            ticks=0
            for _ in range(5):await asyncio.sleep(.01); ticks+=1
            self.assertEqual(5,ticks)
            self.assertFalse(task.done())
        finally:release.set(); await task
