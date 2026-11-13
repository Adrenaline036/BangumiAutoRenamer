"""Queue-boundary doubles only; real execution/recovery controls are separate."""
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.task_queue import TaskQueueManager
from src.rename.asset_execution_recovery import AssetRecoveryPreparation
from src.rename.asset_rollback import AssetRollbackPreparation
from src.rename.asset_operation import AssetOperation
from src.rename.plan_execution import PlanExecutionError
from src.utils.utils import mutate_task_at


class QueuePreflightContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'task.json'
        self.path.write_text(json.dumps(dict(uuid='task', workflow_status='approved')), encoding='utf-8')
        self.plan = SimpleNamespace(execution_policy=SimpleNamespace(dry_run=False))
        # These typed values represent backend results at the queue boundary,
        # never an approved Plan or permission for the real executor.
        self.rollback = AssetRollbackPreparation(AssetOperation.encode(dict(
            policy='asset-rollback-v1', task_id='task', plan_fingerprint='a'*64,
            operation_fingerprint='b'*64, catalog_expected={}, catalog_before={},
            shared_dependencies={})))
        self.recovery = AssetRecoveryPreparation('recoverable', self.plan, None, 'a'*64, 'test boundary')
        self.service = SimpleNamespace(validate_approved=Mock(return_value=self.plan),
            prepare_asset_rollback=Mock(return_value=self.rollback))
        self.recovery_service = SimpleNamespace(inspect=Mock(return_value=self.recovery),
            validate_preparation=Mock(return_value=self.recovery))
        self.executed = []
        async def execute(job):
            self.executed.append(job)
        self.manager = TaskQueueManager(task_root=self.root, execution_service=self.service,
            approved_executor=execute)
        self.stack = ExitStack()
        self.stack.enter_context(patch('src.rename.asset_execution_recovery.AssetExecutionRecovery',
            return_value=self.recovery_service))
        self.addCleanup(self.stack.close)
        self.release = threading.Event()
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.main_thread = threading.get_ident()

    async def asyncTearDown(self):
        self.release.set()
        await self.manager.stop()

    def block(self, result):
        def read(*_):
            if threading.get_ident() == self.main_thread:
                raise AssertionError('媒体校验仍在事件循环线程')
            self.entered.set()
            try:
                if not self.release.wait(3):
                    raise AssertionError('测试未释放后台预检')
                return result
            finally:
                self.finished.set()
        return read

    async def wait_entered(self):
        self.assertTrue(await asyncio.to_thread(self.entered.wait, 2), '后台预检没有开始')

    async def test_normal_heavy_validation_allows_other_event_loop_callbacks(self):
        self.service.validate_approved.side_effect = self.block(self.plan)
        pending = asyncio.create_task(self.manager.enqueue_approved('task', 'a'*64))
        try:
            await self.wait_entered()
            self.assertFalse(pending.done())
            self.release.set()
            self.assertEqual('task', await pending)
        finally:
            self.release.set()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_task_changed_by_preflight_cannot_be_overwritten(self):
        def inspect(*_):
            mutate_task_at(self.root, 'task', lambda t: {**t, 'workflow_status': 'cancelled', 'sentinel': 'new'})
            return self.plan
        self.service.validate_approved.side_effect = inspect
        with self.assertRaises(PlanExecutionError):
            await self.manager.enqueue_approved('task', 'a'*64)
        self.assertEqual('cancelled', json.loads(self.path.read_text('utf-8'))['workflow_status'])
        self.assertEqual(0, self.manager._queue.qsize())

    async def test_task_removed_during_preflight_is_not_recreated(self):
        def inspect(*_):
            self.path.rename(self.path.with_suffix(".removed"))
            return self.plan
        self.service.validate_approved.side_effect = inspect
        with self.assertRaises((ValueError, RuntimeError, OSError)):
            await self.manager.enqueue_approved('task', 'a'*64)
        self.assertFalse(self.path.exists())
        self.assertEqual(0, self.manager._queue.qsize())

    async def test_duplicate_click_does_not_repeat_preflight_or_enqueue(self):
        self.service.validate_approved.side_effect = self.block(self.plan)
        pending = asyncio.create_task(self.manager.enqueue_approved('task', 'a'*64))
        try:
            await self.wait_entered()
            with self.assertRaises(PlanExecutionError):
                await self.manager.enqueue_approved('task', 'a'*64)
            self.assertEqual(1, self.service.validate_approved.call_count)
            self.release.set()
            await pending
        finally:
            self.release.set()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_stop_during_first_public_rollback_await_does_not_restart_queue(self):
        mutate_task_at(self.root, 'task', lambda t: {**t, 'workflow_status': 'completed'})
        self.service.prepare_asset_rollback.side_effect = self.block(self.rollback)
        before = self.path.read_bytes()
        pending = asyncio.create_task(self.manager.enqueue_asset_rollback('task', 'a'*64,
            expected_recovery_fingerprint=self.rollback.fingerprint))
        try:
            await self.wait_entered()
            stopping = asyncio.create_task(self.manager.stop())
            await asyncio.sleep(.03)
            self.assertFalse(stopping.done(), '服务应等待实际预检线程收尾')
            self.release.set()
            await stopping
            with self.assertRaises(PlanExecutionError):
                await pending
            self.assertFalse(self.manager._started)
            self.assertEqual(before, self.path.read_bytes())
            self.assertEqual(0, self.manager._queue.qsize())
        finally:
            self.release.set()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_cancelled_preflight_never_publishes_queue_job(self):
        self.service.validate_approved.side_effect = self.block(self.plan)
        before = self.path.read_bytes()
        pending = asyncio.create_task(self.manager.enqueue_approved('task', 'a'*64))
        try:
            await self.wait_entered()
            pending.cancel()
            await asyncio.sleep(.03)
            self.assertFalse(pending.done(), '取消等待方也应先完成真实线程收尾')
            self.release.set()
            await asyncio.gather(pending, return_exceptions=True)
            self.assertTrue(await asyncio.to_thread(self.finished.wait, 2))
            self.assertEqual(before, self.path.read_bytes())
            self.assertFalse(self.manager._started)
            self.assertEqual(0, self.manager._queue.qsize())
        finally:
            self.release.set()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_recovery_checks_never_run_on_event_loop_thread(self):
        mutate_task_at(self.root, 'task', lambda t: {**t, 'workflow_status': 'partial_failed'})
        threads = []
        def inspect(*_):
            threads.append(threading.get_ident())
            return self.recovery
        def validate(*_):
            threads.append(threading.get_ident())
            return self.recovery
        self.recovery_service.inspect.side_effect = inspect
        self.recovery_service.validate_preparation.side_effect = validate
        await self.manager.enqueue_asset_recovery('task', 'a'*64)
        self.assertTrue(threads)
        self.assertNotIn(self.main_thread, threads)

    async def test_partition_changed_during_preflight_is_rechecked(self):
        pending = False
        def inspect(*_):
            nonlocal pending
            pending = True
            return self.plan
        self.service.validate_approved.side_effect = inspect
        before = self.path.read_bytes()
        with patch.object(self.manager.source_partition_store, 'pending_for_task', side_effect=lambda _: pending):
            with self.assertRaises((ValueError, RuntimeError)):
                await self.manager.enqueue_approved('task', 'a'*64)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(0, self.manager._queue.qsize())

    async def test_stopped_and_restarted_worker_rejects_old_recovery_epoch(self):
        mutate_task_at(self.root, 'task', lambda t: {**t, 'workflow_status': 'partial_failed'})
        await self.manager.start()
        self.recovery_service.inspect.side_effect = self.block(self.recovery)
        before = self.path.read_bytes()
        pending = asyncio.create_task(self.manager.enqueue_asset_recovery('task', 'a'*64))
        try:
            await self.wait_entered()
            stopping = asyncio.create_task(self.manager.stop())
            await asyncio.sleep(.03)
            self.assertFalse(stopping.done(), '重启前应完成实际预检线程收尾')
            self.release.set()
            await stopping
            await self.manager.start()
            with self.assertRaises(PlanExecutionError):
                await pending
            self.assertEqual(before, self.path.read_bytes())
            self.assertEqual(0, self.manager._queue.qsize())
        finally:
            self.release.set()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_cancelled_io_result_none_is_not_a_preparation(self):
        before = self.path.read_bytes()
        async def cancelled(*_, **__):
            return None
        with patch.object(self.manager, '_disk', side_effect=cancelled):
            with self.assertRaises(PlanExecutionError):
                await self.manager.enqueue_approved('task', 'a'*64)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(self.manager._started)

    async def test_failed_queue_publication_preserves_later_task_update(self):
        def fail(_):
            mutate_task_at(self.root, 'task', lambda t: {**t, 'workflow_status': 'cancelled', 'sentinel': 'later'})
            raise RuntimeError('queue publication failed')
        with patch.object(self.manager._queue, 'put_nowait', side_effect=fail):
            with self.assertRaises(RuntimeError):
                await self.manager.enqueue_approved('task', 'a'*64)
        current = json.loads(self.path.read_text('utf-8'))
        self.assertEqual('later', current['sentinel'])
        self.assertEqual('cancelled', current['workflow_status'])
        self.assertEqual(0, self.manager._queue.qsize())


if __name__ == '__main__': unittest.main()
