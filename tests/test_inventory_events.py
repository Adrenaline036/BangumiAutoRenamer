from tests.queue_fixture import TaskQueueManager as ControlledQueue
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.rename.input_directory_discovery import collect_source
from src.rename.process import Rename
from src.rename.task_batch_scope import BatchTaskSourceRequest, TaskBatchScopeManifestStore
from src.rename.review_operation import current_provider_selection_review_operation
from src.task_queue import TaskQueueManager
from src.utils import task_changes
from src.utils.utils import write_task_at
from tests.test_r24_phase_c_review_operation_migration_contract import _pending_attempt, persist_provider_selection_attempt


class InventoryEvents(unittest.IsolatedAsyncioTestCase):
    def root(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def scope(self):
        root = self.root()
        source = root / 'Season 02'
        source.mkdir()
        (source / 'Ordinary Show S02E01.mkv').write_bytes(b'fixture')
        (source / 'Ordinary Show S02E01.chs.ass').write_bytes(b'fixture')
        observed = collect_source(source)
        request = BatchTaskSourceRequest(task_id='sample', input_scope=observed.input_scope,
            scope_kind=observed.scope_kind, source_identities=observed.identities)
        store = TaskBatchScopeManifestStore(root / 'batch')
        manifest = store._materialize(batch_id='sample-batch', requests=(request,),
            batch_fingerprint=store._batch_fingerprint((request,)), state='active', revision=3)
        return source, manifest.task_source_scopes[0]

    async def test_quiet_dashboard_does_not_read_after_elapsed_time(self):
        refresh = Mock(return_value=True)
        timer = task_changes.DashboardRefresh(refresh)
        timer.checked_at = -999999
        for _ in range(5):
            value = timer.poll()
            if asyncio.isfuture(value) or asyncio.iscoroutine(value):
                await value
        print('QUIET_DASHBOARD', dict(reads=refresh.call_count))
        self.assertEqual(0, refresh.call_count)
        task_changes.mark_changed()
        value = timer.poll()
        if asyncio.isfuture(value) or asyncio.iscoroutine(value):
            await value
        self.assertEqual(1, refresh.call_count)

    async def test_selection_wait_reads_only_on_change_and_returns_persisted_result(self):
        root = self.root()
        manager = ControlledQueue(task_root=root / 'task', plan_root=root / 'plans',
            decision_state_root=root / 'decisions', recovery_abandon_audit_root=root / 'audit', test_mode=True)
        task = dict(uuid='sample', workflow_status='need_confirm')
        pending = _pending_attempt()
        persist_provider_selection_attempt(task, pending)
        write_task_at(manager.task_root, 'sample', task)
        async def finish():
            await asyncio.sleep(.35)
            persist_provider_selection_attempt(task, {**pending, 'state': 'succeeded',
                'result_decision_revision': 8, 'result_plan_fingerprint': 'a' * 64})
            await asyncio.to_thread(write_task_at, manager.task_root, 'sample', task)
        publisher = asyncio.create_task(finish())
        with patch.object(manager, '_read', wraps=manager._read) as reader:
            result = await manager.wait_for_selection_result('sample', timeout=1)
        await publisher
        _, stored = current_provider_selection_review_operation(manager._read('sample'))
        self.assertEqual(stored, result)
        self.assertEqual('succeeded', result['lifecycle'])
        self.assertEqual(8, result['result_decision_revision'])
        print('SELECTION_WAIT', dict(reads=reader.call_count, lifecycle=result['lifecycle']))
        self.assertEqual(2, reader.call_count, 'waiting must not poll task files')

    def test_scoped_title_uses_inventory_without_source_attributes(self):
        source, scope = self.scope()
        with patch.object(Path, 'is_dir', side_effect=AssertionError('source attribute reread')), \
                patch.object(Path, 'iterdir', side_effect=AssertionError('source directory reread')):
            candidates = Rename.discovery_title_candidates(source, task_source_scope=scope)
        self.assertEqual('Ordinary Show', candidates[0].title)

    def test_scoped_worker_dispatches_once_without_rescanning(self):
        source, scope = self.scope()
        worker = object.__new__(Rename)
        with patch.object(Path, 'is_dir', side_effect=AssertionError('source dispatcher reread')), \
                patch.object(Path, 'iterdir', side_effect=AssertionError('source directory reread')), \
                patch.object(worker, '_process', return_value='same admitted task') as process:
            result = worker.process(source, True, False, 'sample', task_source_scope=scope)
        self.assertEqual('same admitted task', result)
        process.assert_called_once()
        self.assertEqual(scope, process.call_args.kwargs['task_source_scope'])

    def manager_with_pending(self):
        root = self.root()
        manager = ControlledQueue(task_root=root / 'task', plan_root=root / 'plans',
            decision_state_root=root / 'decisions', recovery_abandon_audit_root=root / 'audit', test_mode=True)
        task = dict(uuid='sample', workflow_status='need_confirm')
        persist_provider_selection_attempt(task, _pending_attempt())
        write_task_at(manager.task_root, 'sample', task)
        return manager, task

    async def test_timeout_cancel_and_stop_preserve_task_and_release_waiters(self):
        manager, task = self.manager_with_pending()
        original = (manager.task_root / 'sample.json').read_bytes()
        result = await manager.wait_for_selection_result('sample', timeout=.02)
        self.assertNotIn(result['lifecycle'], {'failed', 'succeeded'})
        waiter = asyncio.create_task(manager.wait_for_selection_result('sample', timeout=None))
        while not task_changes._waiters:
            await asyncio.sleep(.001)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(task_changes._waiters)
        waiter = asyncio.create_task(manager.wait_for_selection_result('sample', timeout=None))
        while not task_changes._waiters:
            await asyncio.sleep(.001)
        await manager.stop()
        with self.assertRaisesRegex(ValueError, '服务已停止'):
            await asyncio.wait_for(waiter, 1)
        self.assertFalse(task_changes._waiters)
        self.assertEqual(original, (manager.task_root / 'sample.json').read_bytes())

    async def test_deleted_task_and_change_before_wait_registration_do_not_hang(self):
        from src.utils.utils import restore_task_record_at
        manager, task = self.manager_with_pending()
        waiter = asyncio.create_task(manager.wait_for_selection_result('sample', timeout=None))
        while not task_changes._waiters:
            await asyncio.sleep(.001)
        restore_task_record_at(manager.task_root, 'sample', None)
        with self.assertRaises(ValueError):
            await asyncio.wait_for(waiter, 1)
        self.assertFalse(task_changes._waiters)
        observed = task_changes.revision()
        await asyncio.to_thread(task_changes.mark_changed)
        self.assertTrue(await asyncio.wait_for(task_changes.wait_changed(observed), 1))
        self.assertFalse(task_changes._waiters)

    async def test_raw_reset_publication_and_failed_dashboard_read_retry(self):
        from src.rename.recognition_reset import _publishing
        manager, task = self.manager_with_pending()
        observed = task_changes.revision()
        with _publishing(manager.task_root.parent, 'sample'):
            pass
        self.assertGreater(task_changes.revision(), observed)
        refresh = AsyncMock(side_effect=[OSError('isolated read failure'), True])
        timer = task_changes.DashboardRefresh(refresh)
        task_changes.mark_changed()
        with self.assertRaises(OSError):
            await timer.poll()
        await timer.poll()
        self.assertFalse(timer.poll())
        self.assertEqual(2, refresh.await_count)

    async def test_previous_next_reuses_inventory_and_path_change_reobserves(self):
        from types import SimpleNamespace
        from nicegui import Client
        from nicegui.page import page
        from src.pages import add_task_page
        from src.rename.input_directory_discovery import discover_sources
        root = self.root()
        for name in ('Show S01', 'Show S02'):
            path = root / name
            path.mkdir()
            (path / (name + 'E01.mkv')).write_bytes(b'fixture')
        client = Client(page('/inventory-reuse'), request=None)
        self.addCleanup(client.delete)
        with client, patch.object(add_task_page.AddTaskDialog, '_submit', new=AsyncMock()), \
                patch('src.rename.input_directory_discovery.discover_sources', wraps=discover_sources) as scan:
            dialog = add_task_page.AddTaskDialog()
            dialog.path = str(root)
            await dialog._next()
            original = dialog.discovery
            self.assertIsNotNone(original)
            dialog._previous()
            await dialog._next()
            self.assertIs(original, dialog.discovery)
            self.assertEqual(1, scan.call_count)
            dialog._previous()
            dialog._path_changed(SimpleNamespace(value=str(root / 'Show S02')))
            await dialog._next()
            self.assertEqual(2, scan.call_count)
            self.assertEqual((root / 'Show S02',), dialog.discovery.roots)
            print('SOURCE_PREVIEW', dict(unchanged_steps=2, scans_before_path_change=1, scans_after_change=2))

    def test_wrong_task_scope_cannot_enter_worker(self):
        from src.rename.task_batch_scope import TaskBatchScopeError
        source, scope = self.scope()
        worker = object.__new__(Rename)
        with patch.object(worker, '_process') as process, self.assertRaises(TaskBatchScopeError):
            worker.process(source, True, False, 'other-task', task_source_scope=scope)
        process.assert_not_called()
