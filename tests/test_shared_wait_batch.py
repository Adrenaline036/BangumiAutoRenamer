"""真实批次接纳在共享等待取消后恢复原任务及人工输入。"""
import asyncio
from contextlib import suppress
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.task_queue import TaskQueueManager
from tests.test_shared_wait import entered


class BatchWait(unittest.IsolatedAsyncioTestCase):
    async def test_partial_admission_cancel_restores_original_record(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media = root/'source.mkv'
            media.write_bytes(b'isolated sample')
            manager = TaskQueueManager(task_root=root/'tasks', plan_root=root/'plans',
                decision_state_root=root/'decisions', recovery_abandon_audit_root=root/'abandon')
            manager._started = True
            batch = await asyncio.to_thread(manager.prepare_input_batch, (media,),
                is_anime=False, is_movie=False, preview_only=True, source='ui')
            identifier = batch['jobs'][0].task_id
            manager._write(identifier, {'uuid':identifier, 'name':'人工保留标题',
                'season_id':4, 'workflow_status':'failed', 'provider_selection':{'subject_id':'123'}})
            record = manager.task_root/f'{identifier}.json'
            before = record.read_bytes()
            held, release, attempt, cancelled = (threading.Event() for _ in range(4))
            original = manager.batch_scope_store.mark_tasks_enqueued
            def holder():
                with manager.batch_scope_store._mutation_lock('other-task'):
                    held.set()
                    release.wait(4)
            def marking(*args, **kwargs):
                owner = threading.Thread(target=holder)
                owner.start()
                self.assertTrue(held.wait(2))
                attempt.set()
                try:
                    return original(*args, **kwargs)
                except Exception:
                    cancelled.set()
                    raise
                finally:
                    # 回滚需要等持锁任务完成，不得强行释放其他任务的锁。
                    pass
            operation = None
            with patch.object(manager.batch_scope_store, 'mark_tasks_enqueued', marking):
                try:
                    operation = asyncio.create_task(manager.enqueue_batch(**batch))
                    await entered(attempt)
                    self.assertNotEqual(before, record.read_bytes())
                    operation.cancel()
                    await asyncio.sleep(.2)
                    self.assertTrue(cancelled.is_set(), '已写任务后的第二次锁等待必须观察取消')
                    self.assertFalse(operation.done(), '回滚未结束前不得报告取消完成')
                finally:
                    release.set()
                    if operation is not None:
                        with suppress(asyncio.CancelledError):
                            await operation
            self.assertEqual(before, record.read_bytes())
            self.assertFalse(manager._pending)
            self.assertTrue(manager._queue.empty())
            with manager.batch_scope_store._mutation_lock('retry'):
                pass
