"""隔离组件的网页调度与重复读写合同。"""
from tests.queue_fixture import TaskQueueManager as ControlledQueue
import asyncio
import inspect
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch
from contextlib import asynccontextmanager

from nicegui.client import Client
from nicegui.page import page


async def result(value):
    return await value if inspect.isawaitable(value) else value


@asynccontextmanager
async def heartbeat(test, intervals):
    ticks = []
    async def pulse():
        while True:
            ticks.append(time.perf_counter())
            await asyncio.sleep(.005)
    task = asyncio.create_task(pulse())
    await asyncio.sleep(0)
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    test.assertTrue(intervals, '必须消费本次慢读取')
    for start, end in intervals:
        test.assertGreaterEqual(sum(start < tick < end for tick in ticks), 3,
                                '慢读取期间网页心跳必须持续推进')


def slow_observation(calls, intervals, callback, *args, **kwargs):
    calls.append(threading.get_ident())
    start = time.perf_counter()
    time.sleep(.06)
    value = callback(*args, **kwargs)
    intervals.append((start, time.perf_counter()))
    return value


class Responsiveness(unittest.IsolatedAsyncioTestCase):
    async def test_batch_history_read_once_and_off_loop(self):
        from src.task_queue import TaskQueueManager
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manager = ControlledQueue(task_root=root/'task', plan_root=root/'plan',
                decision_state_root=root/'state', recovery_abandon_audit_root=root/'audit', test_mode=True)
            await manager.start()
            sources = []
            for number in range(3):
                path = root/f'Show {number}'
                path.mkdir()
                (path/'Show S01E01.mkv').write_bytes(b'fixture')
                sources.append(path)
            for number in range(4):
                (manager.task_root/f'old-{number}.json').write_text(json.dumps({
                    'uuid': f'old-{number}', 'workflow_status': 'cancelled'}), encoding='utf-8')
            batch = manager.prepare_input_batch(tuple(sources), is_anime=False,
                is_movie=False, source='ui', preview_only=True)
            original, reads, intervals = Path.read_text, [], []
            def read(path, *args, **kwargs):
                if path.parent == manager.task_root and path.name.startswith('old-'):
                    return slow_observation(reads, intervals, original, path, *args, **kwargs)
                return original(path, *args, **kwargs)
            try:
                async with heartbeat(self, intervals):
                    with patch.object(Path, 'read_text', read):
                        ids = await manager.enqueue_batch(**batch)
                self.assertEqual(3, len(ids))
                self.assertEqual(4, len(reads), '一次批次只读取一次历史集合')
                self.assertNotIn(threading.get_ident(), reads, '历史读取不得占用网页线程')
            finally:
                await manager.stop()

    async def test_unchanged_queue_positions_do_not_write(self):
        from src.task_queue import TaskQueueManager
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manager = ControlledQueue(task_root=root/'task', plan_root=root/'plan',
                decision_state_root=root/'state', recovery_abandon_audit_root=root/'audit', test_mode=True)
            manager._pending[:] = ['one', 'two']
            from src.utils import utils
            for key, position in [('one', 1), ('two', 2)]:
                manager._write(key, {'uuid': key, 'workflow_status': 'queued',
                                     'queue_position': position, 'retained': 'manual-choice'})
            with patch.object(utils, '_atomic_write_task_at', wraps=utils._atomic_write_task_at) as write:
                await result(manager._refresh_queue_positions())
                write.assert_not_called()
            manager._update('one', queue_position=7)
            with patch.object(utils, '_atomic_write_task_at', wraps=utils._atomic_write_task_at) as write:
                await result(manager._refresh_queue_positions())
                self.assertEqual(1, write.call_count)
            self.assertEqual(1, manager._read('one')['queue_position'])
            self.assertEqual('manual-choice', manager._read('one')['retained'])
            self.assertEqual(2, manager._read('two')['queue_position'])

    async def test_http_input_preparation_off_loop(self):
        from src import web
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = type('Request', (), {'form': AsyncMock(return_value={'path': str(root)})})()
            calls, intervals = [], []
            def prepare(*args, **kwargs):
                return slow_observation(calls, intervals, lambda: {'fixture': True})
            with patch.object(web.task_queue, 'prepare_input_batch', side_effect=prepare), \
                    patch.object(web.task_queue, 'enqueue_batch', new=AsyncMock(return_value=('test',))) as enqueue:
                async with heartbeat(self, intervals):
                    response = await web._send_task(request)
                enqueue.assert_awaited_once_with(fixture=True)
                self.assertEqual('test', response['task_id'])
            self.assertTrue(calls)
            self.assertNotIn(threading.get_ident(), calls, 'HTTP 扫描不得占用网页线程')

    async def test_first_dashboard_read_off_loop(self):
        from src.pages import data_table_page as dashboard
        calls, intervals = [], []
        rows = [{'uuid': 'observed-task', 'workflow_status': 'completed'}]
        def read():
            return slow_observation(calls, intervals, lambda: rows)
        client = Client(page('/first-dashboard-responsive'), request=None)
        try:
            with client, patch.object(dashboard, 'load_task_rows', side_effect=read), \
                    patch.object(dashboard, '_read_card_decision', return_value=None), \
                    patch.object(dashboard, '_task_section') as section:
                async with heartbeat(self, intervals):
                    await result(dashboard.create_table())
                    for _ in range(100):
                        if any(call.args and call.args[0] == rows for call in section.call_args_list):
                            break
                        await asyncio.sleep(.01)
                self.assertTrue(any(call.args and call.args[0] == rows for call in section.call_args_list))
            self.assertNotIn(threading.get_ident(), calls, '首次工作台读取不得占用网页线程')
        finally:
            client.delete()


from tests import test_planned_replacement_edit as existing


class RevisionResponsiveness(unittest.IsolatedAsyncioTestCase):
    fixture = existing.PlannedReplacementEdit.fixture
    describe = existing.PlannedReplacementEdit.describe
    revise = existing.PlannedReplacementEdit.revise
    observe = existing.PlannedReplacementEdit.observe
    confirm = existing.PlannedReplacementEdit.confirm
    create_page = existing.PlannedReplacementEdit.create_page
    replacement = existing.PlannedReplacementEdit.replacement

    async def test_adopt_prepares_snapshot_off_loop(self):
        from src.pages import plan_review_page as review
        root, service, plan, _ = await self.replacement()
        self.create_page(prepared=(root, service, plan, []))
        calls, intervals, snapshots = [], [], []
        original = review.build_plan_review_snapshot
        def prepare(*args, **kwargs):
            snapshot = slow_observation(calls, intervals, original, *args, **kwargs)
            snapshots.append(snapshot)
            return snapshot
        with self.client, patch.object(review, 'build_plan_review_snapshot', side_effect=prepare):
            async with heartbeat(self, intervals):
                await result(self.view._adopt_revision(plan))
            self.assertIs(snapshots[-1], self.view.snapshot)
        self.assertTrue(calls)
        self.assertNotIn(threading.get_ident(), calls, '公共收尾核验不得占用网页线程')
        self.assertEqual(plan, self.view.plan)
