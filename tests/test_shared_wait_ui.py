"""实际重建登记页面回调的等待取消和服务停止。"""
import asyncio
from contextlib import suppress
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.pages import plan_review_page as page
from src.rename.task_batch_scope import TaskBatchScopeManifestStore
from src.task_queue import TaskQueueManager
from tests.test_shared_wait import entered


class PageWait(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_rebuild_callback_joins_waiting_thread_without_write(self):
        await self.check_wait(False)

    async def test_stop_joins_rebuild_callback_without_write(self):
        await self.check_wait(True)

    async def check_wait(self, stopping):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = TaskBatchScopeManifestStore(root / 'shared')
            held, release, attempt, finished = (threading.Event() for _ in range(4))
            writes = []
            threads = []
            main_thread = threading.get_ident()
            def holder():
                with store._mutation_lock('holder'):
                    held.set()
                    release.wait(4)
            def rebuild(*args):
                threads.append(threading.get_ident())
                attempt.set()
                try:
                    with store._mutation_lock('rebuild'):
                        writes.append(True)
                finally:
                    finished.set()
            manager = TaskQueueManager(task_root=root/'tasks', plan_root=root/'plans',
                decision_state_root=root/'decisions', recovery_abandon_audit_root=root/'abandon')
            manager._started = True
            review = SimpleNamespace(plan=SimpleNamespace(metadata={}, task_id='synthetic'),
                task_decision=SimpleNamespace(revision=1),
                recovery_service=SimpleNamespace(rebuild_catalog=rebuild),
                review_service=SimpleNamespace(work_catalog=None), series_catalog=None,
                decision_command_service=Mock(), _adopt_revision=Mock())
            owner = asyncio.create_task(asyncio.to_thread(holder))
            await entered(held)
            with patch.object(page, 'notify'), patch('src.task_queue.task_queue', manager), patch.object(page, 'task_queue', manager):
                operation = asyncio.create_task(page.PlanReviewDialog._recover_missing_catalog(review))
                stopper = None
                try:
                    await entered(attempt)
                    self.assertNotEqual([main_thread], threads, '登记操作必须留在后台线程')
                    self.assertFalse(operation.done(), '取消前必须确实在排队等待')
                    self.assertFalse(finished.is_set())
                    if stopping:
                        stopper = asyncio.create_task(manager.stop())
                        done, _ = await asyncio.wait({stopper}, timeout=.7)
                        self.assertIn(stopper, done)
                    else:
                        operation.cancel()
                        done, _ = await asyncio.wait({operation}, timeout=.7)
                        self.assertIn(operation, done)
                    self.assertTrue(finished.is_set(), '返回前必须确认等待线程已退出')
                    self.assertEqual([], writes)
                    review.decision_command_service.update.assert_not_called()
                    self.assertFalse(release.is_set())
                finally:
                    release.set()
                    await owner
                    with suppress(asyncio.CancelledError):
                        await operation
                    if stopper is not None:
                        await stopper
                await entered(finished)
                self.assertEqual([], writes, '锁释放后也不得出现迟到写入')
