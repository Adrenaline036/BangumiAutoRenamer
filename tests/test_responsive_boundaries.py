"""后台读写的取消、发布和过期页面边界；仅隔离样本。"""
from tests.queue_fixture import TaskQueueManager as ControlledQueue
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from nicegui.client import Client
from nicegui.page import page
from tests.test_ui_responsiveness import heartbeat, slow_observation
from src.task_queue import TaskQueueManager


class AdmissionBoundaries(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = ControlledQueue(task_root=self.root/'task', plan_root=self.root/'plan',
            decision_state_root=self.root/'state', recovery_abandon_audit_root=self.root/'audit', test_mode=True)
        await self.manager.start()
        source = self.root/'Show S01E01.mkv'
        source.write_bytes(b'isolated')
        self.batch = self.manager.prepare_input_batch((source,), is_anime=False, is_movie=False,
            source='ui', preview_only=True)
        self.identifier = self.batch['jobs'][0].task_id
        self.entered, self.release = threading.Event(), threading.Event()

    async def asyncTearDown(self):
        self.release.set()
        await self.manager.stop()

    async def exercise(self, operation):
        original = self.manager.batch_scope_store.activate_batch
        def activate(*args, **kwargs):
            self.entered.set()
            if not self.release.wait(3):
                raise TimeoutError('isolated admission release')
            return original(*args, **kwargs)
        with patch.object(self.manager.batch_scope_store, 'activate_batch', side_effect=activate):
            admission = asyncio.create_task(self.manager.enqueue_batch(**self.batch))
            try:
                self.assertTrue(await asyncio.to_thread(self.entered.wait, 2))
                self.assertEqual([], self.manager._pending)
                self.assertEqual(0, self.manager._queue.qsize())
                stopper = None
                if operation == 'cancel':
                    admission.cancel()
                else:
                    stopper = asyncio.create_task(self.manager.stop())
                await asyncio.sleep(.03)
                self.assertFalse(admission.done())
                if stopper:
                    self.assertFalse(stopper.done())
                self.release.set()
                outcome = await asyncio.gather(admission, return_exceptions=True)
                self.assertIsInstance(outcome[0], BaseException)
                if stopper:
                    await stopper
                self.assertEqual([], self.manager._pending)
                self.assertEqual(0, self.manager._queue.qsize())
                self.assertFalse((self.manager.task_root/(self.identifier+'.json')).exists())
                self.assertEqual('failed_closed', self.manager.batch_scope_store.load(self.batch['batch_id']).state)
                await self.manager.start()
            finally:
                self.release.set()
                await asyncio.gather(admission, return_exceptions=True)

    async def test_cancel_waits_for_rollback_and_never_publishes(self):
        await self.exercise('cancel')

    async def test_stop_waits_for_rollback_then_restart_is_safe(self):
        await self.exercise('stop')

    async def test_publish_failure_after_insert_rolls_back_entire_batch(self):
        self.batch['jobs'] = tuple(replace(job, preview_only=False) for job in self.batch['jobs'])
        original = self.manager._queue.put_nowait
        def put(job):
            original(job)
            raise RuntimeError('publish fault after insertion')
        with patch.object(self.manager._queue, 'put_nowait', side_effect=put):
            with self.assertRaisesRegex(RuntimeError, 'publish fault'):
                await self.manager.enqueue_batch(**self.batch)
        self.assertEqual(0, self.manager._queue.qsize())
        self.assertEqual(0, self.manager._queue._unfinished_tasks)
        self.assertEqual([], self.manager._pending)
        self.assertFalse((self.manager.task_root/(self.identifier+'.json')).exists())
        self.assertEqual('failed_closed', self.manager.batch_scope_store.load(self.batch['batch_id']).state)

    async def test_slow_durable_admission_keeps_heartbeat_and_publishes_complete_records(self):
        original = self.manager.batch_scope_store.activate_batch
        calls, intervals = [], []
        def activate(*args, **kwargs):
            self.assertEqual([], self.manager._pending)
            return slow_observation(calls, intervals, original, *args, **kwargs)
        with patch.object(self.manager.batch_scope_store, 'activate_batch', side_effect=activate):
            async with heartbeat(self, intervals):
                ids = await self.manager.enqueue_batch(**self.batch)
        self.assertEqual((self.identifier,), ids)
        self.assertEqual([self.identifier], self.manager._pending)
        self.assertEqual('active', self.manager.batch_scope_store.load(self.batch['batch_id']).state)
        self.assertEqual('queued', self.manager._read(self.identifier)['workflow_status'])
        self.assertNotIn(threading.get_ident(), calls)

    async def test_partial_record_failure_restores_records_and_keeps_other_queue_items(self):
        from src.task_queue import TaskJob
        untouched = TaskJob(task_id='unrelated', path=None)
        self.manager._queue.put_nowait(untouched)
        original = self.manager._write
        def write(identifier, payload):
            original(identifier, payload)
            raise RuntimeError('record fault after write')
        with patch.object(self.manager, '_write', side_effect=write):
            with self.assertRaisesRegex(RuntimeError, 'record fault'):
                await self.manager.enqueue_batch(**self.batch)
        self.assertFalse((self.manager.task_root/(self.identifier+'.json')).exists())
        self.assertIs(untouched, self.manager._queue.get_nowait())
        self.manager._queue.task_done()
        self.assertEqual('failed_closed', self.manager.batch_scope_store.load(self.batch['batch_id']).state)

    async def test_later_task_change_is_preserved_and_blocks_publication(self):
        original = self.manager.batch_scope_store.activate_batch
        def activate(*args, **kwargs):
            result = original(*args, **kwargs)
            self.manager._update(self.identifier, workflow_status='cancelled', retained='human')
            return result
        with patch.object(self.manager.batch_scope_store, 'activate_batch', side_effect=activate):
            with self.assertRaisesRegex(Exception, '任务已改变'):
                await self.manager.enqueue_batch(**self.batch)
        self.assertEqual('human', self.manager._read(self.identifier)['retained'])
        self.assertEqual([], self.manager._pending)
        self.assertEqual(0, self.manager._queue.qsize())


class PickerBoundaries(unittest.IsolatedAsyncioTestCase):
    async def test_directory_read_background_and_closed_result_not_applied(self):
        from src.component import local_file_picker as picker
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root/'child').mkdir()
            (root/'episode.mkv').write_bytes(b'isolated')
            client = Client(page('/responsive-picker'), request=None)
            calls, intervals = [], []
            original = picker._directory_rows
            def rows(*args):
                return slow_observation(calls, intervals, original, *args)
            try:
                with client, patch.object(picker, '_directory_rows', side_effect=rows):
                    async with heartbeat(self, intervals):
                        dialog = picker.local_file_picker(str(root), upper_limit=str(root))
                        await dialog._initial_load
                    self.assertEqual(['文件夹', '文件'], [r['kind'] for r in dialog.grid.options['rowData']])
                    self.assertNotIn(threading.get_ident(), calls)
                    before = list(dialog.grid.options['rowData'])
                    refresh = asyncio.create_task(dialog.update_grid())
                    await asyncio.sleep(.01)
                    dialog.close()
                    await refresh
                    self.assertEqual(before, dialog.grid.options['rowData'])
            finally:
                client.delete()
