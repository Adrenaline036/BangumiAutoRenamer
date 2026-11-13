"""真实共享锁长等待、取消与停止，全部使用隔离文件。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
import tempfile
import threading
import time
import unittest

from src.rename.task_batch_scope import TaskBatchScopeManifestStore
from src.queue_runtime import disk_call, transaction
from src.task_queue import TaskQueueManager


async def entered(event):
    for _ in range(400):
        if event.is_set():
            return
        await asyncio.sleep(.005)
    raise AssertionError('后台线程未进入')


class LongWait(unittest.TestCase):
    def test_real_wait_over_ten_seconds_then_succeeds(self):
        with tempfile.TemporaryDirectory() as folder:
            first=TaskBatchScopeManifestStore(Path(folder))
            second=TaskBatchScopeManifestStore(Path(folder))
            held=threading.Event();release=threading.Event();attempt=threading.Event()
            def holder():
                with first._mutation_lock('holder'):
                    held.set();release.wait(15)
            def waiter():
                attempt.set()
                with second._mutation_lock('waiter'):
                    return 'acquired'
            with ThreadPoolExecutor(2) as pool:
                owner=pool.submit(holder);self.assertTrue(held.wait(2))
                waiting=pool.submit(waiter);self.assertTrue(attempt.wait(2))
                begin=time.monotonic()
                try:
                    while time.monotonic()-begin<10.5:
                        time.sleep(.02)
                    self.assertFalse(waiting.done(),'正常排队超过十秒不能失败')
                finally:
                    release.set();owner.result(2)
                self.assertEqual('acquired',waiting.result(2))


class WaitCancellation(unittest.IsolatedAsyncioTestCase):
    async def run_wait(self, *, stop=False):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=TaskBatchScopeManifestStore(root/'shared')
            held=threading.Event();release=threading.Event();attempt=threading.Event();body=[]
            finished=threading.Event()
            def holder():
                with store._mutation_lock('holder'):
                    held.set();release.wait(4)
            def waiter():
                attempt.set()
                try:
                    with TaskBatchScopeManifestStore(root/'shared')._mutation_lock('waiter'):
                        body.append('entered')
                finally:
                    finished.set()
            owner=asyncio.create_task(asyncio.to_thread(holder));await entered(held)
            manager=TaskQueueManager(task_root=root/'tasks',plan_root=root/'plans',
                decision_state_root=root/'decisions',recovery_abandon_audit_root=root/'abandon')
            manager._started=True
            waiting=asyncio.create_task(transaction(manager,waiter) if stop else disk_call(waiter))
            stopper=None
            try:
                await entered(attempt);await asyncio.sleep(.03)
                self.assertFalse(waiting.done())
                if stop:
                    stopper=asyncio.create_task(manager.stop())
                    done,_=await asyncio.wait({stopper},timeout=.7)
                    self.assertIn(stopper,done,'停止服务应退出未获锁等待')
                    self.assertTrue(waiting.done())
                    self.assertIsInstance(waiting.exception(),RuntimeError)
                else:
                    waiting.cancel()
                    done,_=await asyncio.wait({waiting},timeout=.7)
                    self.assertIn(waiting,done,'取消不应等待持锁者完成')
                    with self.assertRaises(asyncio.CancelledError):await waiting
                self.assertTrue(finished.is_set(), '返回前实际等待线程必须已退出')
                self.assertEqual([],body)
                self.assertFalse(release.is_set())
                self.assertEqual([],manager._pending)
                self.assertTrue(manager._queue.empty())
                self.assertFalse(manager._admissions)
            finally:
                release.set();await owner
                with suppress(asyncio.CancelledError,RuntimeError):await waiting
                if stopper is not None:await stopper
            await entered(finished)
            self.assertEqual([],body, '释放原持锁者后也不得出现取消任务的延迟写入')
            with store._mutation_lock('after'):
                pass

    async def test_cancel_waiting_thread_without_releasing_holder(self):
        await self.run_wait()

    async def test_stop_waiting_admission_without_releasing_holder(self):
        await self.run_wait(stop=True)

    async def test_cancel_after_entering_body_waits_for_safe_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=TaskBatchScopeManifestStore(root/'shared')
            held=threading.Event();release=threading.Event();completed=[]
            def writing():
                with store._mutation_lock('write'):
                    held.set();release.wait(3)
                    (root/'result.txt').write_text('completed',encoding='utf-8')
                    completed.append(True)
            running=asyncio.create_task(disk_call(writing));await entered(held)
            try:
                running.cancel();await asyncio.sleep(.1)
                self.assertFalse(running.done(),'持锁写入必须真实收尾')
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):await running
            self.assertEqual([True],completed)
            self.assertEqual('completed',(root/'result.txt').read_text('utf-8'))
            with store._mutation_lock('after'):pass
