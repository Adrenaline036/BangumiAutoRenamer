"""实际换源回滚历史上的后台计划更新占用。"""
import asyncio
import inspect
import threading
import unittest
from unittest.mock import patch
from src.rename import rolled_back_restart
from src.rename.plan_store import PlanStore
from tests import test_simplify_rollback_restart as test_simplify_rollback_restart


class RollbackUpdateLifecycle(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, mode):
        helper = test_simplify_rollback_restart.RolledBackRestartContracts('runTest')
        helper.addCleanup = self.addCleanup
        helper.addAsyncCleanup = self.addAsyncCleanup
        await helper.asyncSetUp()
        manager = helper.queue
        task_id = helper.original.task_id
        before_task = helper.task()
        old_fp = helper.fp
        preparation = await manager.prepare_rolled_back_restart(task_id)
        original = rolled_back_restart.restart
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def restart(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('isolated restart barrier')
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()
        updating = stopping = None
        try:
            with patch.object(rolled_back_restart, 'restart', side_effect=restart):
                updating = asyncio.create_task(manager.restart_rolled_back(task_id, preparation))
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                if mode == 'exclusion':
                    before_attempts = manager._read(task_id)
                    with self.assertRaisesRegex(RuntimeError, '正在|排队|处理中'):
                        await manager.prepare_rolled_back_restart(task_id)
                    with self.assertRaisesRegex(RuntimeError, '正在|排队|处理中'):
                        await manager.restart_rolled_back(task_id, preparation)
                    with self.assertRaisesRegex(RuntimeError, '正在|排队|处理中'):
                        await manager.retry_current(task_id)
                    self.assertEqual(before_attempts, manager._read(task_id))
                    occupied = (set(manager._pending)
                                | set(getattr(manager, '_repairing', ()))
                                | set(getattr(manager, '_plan_updates', ())))
                    self.assertIn(task_id, occupied, '后台计划更新必须保有独立占用')
                cancelled = False
                if mode == 'safe_end':
                    task_path = manager.task_root / f'{task_id}.json'
                    before_end = task_path.read_bytes()
                    failure = None
                    with patch.object(manager, '_retire_task_authorities',
                                      wraps=manager._retire_task_authorities) as retire:
                        try:
                            result = manager.abandon_and_remove(task_id,
                                record_root=helper.root/'records', plan_root=helper.root/'plans',
                                review_root=helper.root/'reviews')
                            if inspect.isawaitable(result):
                                await result
                        except RuntimeError as exc:
                            failure = exc
                    self.assertEqual(before_end, task_path.read_bytes() if task_path.exists() else None,
                                     '安全结束不得删除或改写正在更新计划的原任务')
                    self.assertEqual(0, retire.call_count, '安全结束不得退役正在更新计划的来源权威')
                    self.assertIsInstance(failure, RuntimeError, '计划更新期间安全结束必须明确拒绝')
                if mode == 'queue_cancel':
                    before_cancel = manager._read(task_id)
                    with patch.object(manager, '_retire_task_authorities',
                                      wraps=manager._retire_task_authorities) as retire:
                        cancelled = manager.cancel_queued(task_id)
                    self.assertEqual(before_cancel, manager._read(task_id),
                                     '取消排队不得改写正在更新计划的任务记录')
                    self.assertEqual(0, retire.call_count,
                                     '取消排队不得退役正在更新计划的来源权威')
                if mode in {'cancel', 'cancel_twice'}:
                    updating.cancel()
                    if mode == 'cancel_twice':
                        await asyncio.sleep(.01)
                        updating.cancel()
                elif mode == 'stop':
                    stopping = asyncio.create_task(manager.stop())
                await asyncio.sleep(.05)
                pending = not (stopping if stopping is not None else updating).done()
                release.set()
                result = (await asyncio.gather(updating, return_exceptions=True))[0]
                if stopping is not None:
                    await stopping
                self.assertFalse(cancelled, '取消排队不能退役正在生成新计划的旧任务')
                self.assertTrue(pending, '实际修订尚未结束，调用或停止已返回')
                self.assertTrue(finished.is_set())
                if mode in {'cancel', 'cancel_twice'}:
                    self.assertIsInstance(result, asyncio.CancelledError)
                elif isinstance(result, BaseException):
                    raise result
            current = helper.service.plan_store.load(task_id)
            self.assertNotEqual(old_fp, PlanStore.fingerprint(current))
            self.assertEqual(old_fp, PlanStore.fingerprint(helper.service.plan_store.load_version(task_id, old_fp)))
            self.assertFalse(helper.service.review_service.load_or_create(current).approved_entry_ids)
            for key in ('uuid','name','season_id','season_number'):
                self.assertEqual(before_task.get(key), helper.task().get(key), key)
            self.assertFalse(manager._pending)
        finally:
            release.set()
            if updating is not None:
                await asyncio.gather(updating, return_exceptions=True)
            if stopping is not None:
                await asyncio.gather(stopping, return_exceptions=True)
            await manager.stop()

    async def test_normal_update_preserves_history(self):
        await self.exercise('normal')

    async def test_queue_cancel_does_not_retire_running_update(self):
        await self.exercise('queue_cancel')

    async def test_caller_cancel_waits_for_new_revision(self):
        await self.exercise('cancel')

    async def test_stop_waits_for_new_revision(self):
        await self.exercise('stop')

    async def test_repeated_cancel_waits_for_revision(self):
        await self.exercise('cancel_twice')

    async def test_update_excludes_duplicate_restart_and_recognition(self):
        await self.exercise('exclusion')

    async def test_safe_end_preserves_task_during_plan_update(self):
        await self.exercise('safe_end')
