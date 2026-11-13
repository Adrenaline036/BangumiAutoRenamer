"""完成资产修复在真实队列中的取消、停止及重复提交边界。"""
import asyncio
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.task_queue import TaskQueueManager
from src.rename.plan_execution import PlanExecutionError


class CompletedRepairLifecycle(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, route, mode, *, mixed=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = []
            for name in ('existing', 'fresh') if mixed else ('existing',):
                source = root / name
                source.mkdir()
                (source / 'Example.mkv').write_bytes(b'isolated input')
                sources.append(source)
            consumed = []
            async def executor(job):
                consumed.append(job.task_id)
                queue._update(job.task_id, workflow_status='need_confirm')
            queue = TaskQueueManager(task_root=root/'tasks', plan_root=root/'plans',
                decision_state_root=root/'decisions', recovery_abandon_audit_root=root/'abandon', executor=executor)
            queue._write('retained-task', dict(uuid='retained-task', name='保留的作品名称', season_id=3,
                path=str(sources[0]), workflow_status='completed', execution_status='completed',
                completed_at='2026-01-01T00:00:00Z'))
            task_path = queue.task_root / 'retained-task.json'
            original_task = task_path.read_bytes()
            batch = queue.prepare_input_batch(tuple(sources), is_anime=False, is_movie=True,
                                              source='ui', preview_only=False)
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            repaired = []
            marker = root / 'repair-finished.txt'
            def pause():
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('isolated repair barrier')
            def inspect(task_id):
                if mode == 'cancel_inspection':
                    pause()
                return SimpleNamespace(state='missing')
            def repair(task_id):
                repaired.append(task_id)
                try:
                    pause()
                    if mode == 'failure':
                        raise OSError('isolated repair failure')
                    marker.write_text(task_id, encoding='utf-8')
                finally:
                    finished.set()
            assets = SimpleNamespace(completed_sources=lambda requests: {
                request.task_id: 'retained-task' for request in requests
                if request.task_id == batch['jobs'][0].task_id},
                                     inspect_admission=inspect, repair=repair)
            async def invoke():
                if route == 'retry':
                    return await queue.retry_current('retained-task')
                return await queue.enqueue_batch(**batch)
            admission = stopping = None
            await queue.start()
            try:
                with patch('src.rename.library_assets.LibraryAssets', return_value=assets):
                    admission = asyncio.create_task(invoke())
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    self.assertFalse(queue.cancel_queued('retained-task'),
                                     '后台修复不能被当作尚未执行的排队作业取消')
                    self.assertEqual(original_task, task_path.read_bytes())
                    if mode in {'cancel', 'cancel_twice', 'cancel_inspection'}:
                        admission.cancel()
                    if mode == 'cancel_twice':
                        await asyncio.sleep(.01)
                        admission.cancel()
                    if mode == 'stop':
                        stopping = asyncio.create_task(queue.stop())
                    if mode == 'duplicate':
                        with self.assertRaises(PlanExecutionError):
                            await asyncio.wait_for(invoke(), 2)
                    await asyncio.sleep(.05)
                    observed_pending = not (stopping if mode == 'stop' else admission).done()
                    release.set()
                    if mode.startswith('cancel'):
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(admission, 5)
                    elif mode == 'failure' or mode == 'stop' and mixed:
                        with self.assertRaises(PlanExecutionError):
                            await asyncio.wait_for(admission, 5)
                    else:
                        value = await asyncio.wait_for(admission, 5)
                        expected = 'retained-task' if route == 'retry' else (
                            'retained-task', *tuple(job.task_id for job in batch['jobs'][1:]))
                        self.assertEqual(expected, value)
                    if stopping is not None:
                        await asyncio.wait_for(stopping, 5)
                    if mode != 'cancel_inspection':
                        self.assertTrue(observed_pending, '修复尚未结束，调用或停止却已返回')
                        self.assertTrue(finished.is_set())
                        self.assertEqual(['retained-task'], repaired)
                    else:
                        self.assertEqual([], repaired, '取消只读检查后不应开始修复')
                    await asyncio.wait_for(queue._queue.join(), 5)
                    if mixed and mode == 'normal':
                        self.assertEqual([batch['jobs'][1].task_id], consumed)
                    else:
                        self.assertEqual([], consumed)
                        self.assertFalse(any((queue.task_root/f'{job.task_id}.json').exists() for job in batch['jobs']))
                    self.assertFalse(queue._pending)
                    self.assertFalse(queue._admissions)
                    self.assertEqual(original_task, task_path.read_bytes())
            finally:
                release.set()
                if admission is not None:
                    await asyncio.gather(admission, return_exceptions=True)
                if stopping is not None:
                    await asyncio.gather(stopping, return_exceptions=True)
                if repaired:
                    await asyncio.to_thread(finished.wait, 5)
                await queue.stop()

    async def test_normal_repair_finishes_and_preserves_original_task(self):
        for route in ('batch', 'retry'):
            with self.subTest(route=route):
                await self.exercise(route, 'normal')

    async def test_cancel_waits_for_repair_and_propagates(self):
        for route in ('batch', 'retry'):
            with self.subTest(route=route):
                await self.exercise(route, 'cancel')

    async def test_repeated_cancel_cannot_release_running_repair(self):
        for route in ('batch', 'retry'):
            with self.subTest(route=route):
                await self.exercise(route, 'cancel_twice')

    async def test_stop_waits_for_started_repair(self):
        for route in ('batch', 'retry'):
            with self.subTest(route=route):
                await self.exercise(route, 'stop')

    async def test_duplicate_repair_is_rejected_while_original_runs(self):
        for route in ('batch', 'retry'):
            with self.subTest(route=route):
                await self.exercise(route, 'duplicate')

    async def test_failure_releases_slot_and_preserves_original_task(self):
        for route in ('batch', 'retry'):
            with self.subTest(route=route):
                await self.exercise(route, 'failure')

    async def test_cancel_during_inspection_never_repairs(self):
        await self.exercise('batch', 'cancel_inspection')

    async def test_mixed_batch_publishes_fresh_only_after_repair(self):
        await self.exercise('batch', 'normal', mixed=True)

    async def test_cancelled_mixed_batch_does_not_publish_fresh(self):
        await self.exercise('batch', 'cancel', mixed=True)

    async def test_stopped_mixed_batch_does_not_publish_fresh(self):
        await self.exercise('batch', 'stop', mixed=True)
