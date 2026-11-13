"""共享来源登记的线程调度仍须经过真实文件锁。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.rename import file_lock
from src.rename.projection_authority import (
    SourceOwnershipStore, ProjectionCommitService, ProjectionAuthorityError,
    SourceIdentity, SourceOwnershipConflict,
)


class SharedSourceStoreContracts(unittest.TestCase):
    def test_cancel_wait_preserves_owner_and_allows_later_request(self):
        with TemporaryDirectory() as temporary, ThreadPoolExecutor(1) as pool:
            store = SourceOwnershipStore(Path(temporary))
            def enter(cancel=False):
                with file_lock.cancellable_wait(lambda: cancel):
                    with SourceOwnershipStore(Path(temporary))._mutation_lock():
                        return True
            with store._mutation_lock():
                with self.assertRaises(file_lock.LockWaitCancelledError):
                    pool.submit(enter, True).result(2)
                with self.assertRaises(file_lock.FileLockBusyError):
                    with file_lock.advisory_file_lock(store._lock_resource().path):
                        self.fail('取消等待不能释放持有者的锁')
            self.assertTrue(pool.submit(enter).result(2))

    def test_waiter_rechecks_real_source_conflict_after_owner_commits(self):
        with TemporaryDirectory() as temporary, ThreadPoolExecutor(2) as pool:
            root = Path(temporary)
            source = root/'source.mkv'
            source.write_bytes(b'one source')
            identity = SourceIdentity.from_path(source, inventory_fingerprint='a'*64)
            first, second = SourceOwnershipStore(root/'owners'), SourceOwnershipStore(root/'owners')
            written, release, attempted = threading.Event(), threading.Event(), threading.Event()
            original = first._write
            def write(record):
                original(record)
                written.set()
                if not release.wait(5):
                    raise AssertionError('持锁屏障未释放')
            def claim(store, task_id):
                if task_id == 'second':
                    attempted.set()
                return store.claim(task_id=task_id, decision_revision=0,
                    inventory_fingerprint='a'*64, identities=(identity,))
            with patch.object(first, '_write', side_effect=write):
                owner = pool.submit(claim, first, 'first')
                self.assertTrue(written.wait(5))
                waiter = pool.submit(claim, second, 'second')
                try:
                    self.assertTrue(attempted.wait(5))
                    time.sleep(.1)
                    self.assertFalse(waiter.done())
                finally:
                    release.set()
                record = owner.result(5)
                with self.assertRaises(SourceOwnershipConflict):
                    waiter.result(5)
            self.assertEqual(record, first.load_optional('first'))
            self.assertIsNone(first.load_optional('second'))

    def test_local_gate_does_not_replace_os_acquisition_proof(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'source.mkv'
            source.write_bytes(b'one source')
            identity = SourceIdentity.from_path(source, inventory_fingerprint='a'*64)
            store = SourceOwnershipStore(root/'owners')
            with patch.object(file_lock, '_acquire', return_value=None):
                with self.assertRaises(ValueError):
                    store.claim(task_id='task', decision_revision=0,
                        inventory_fingerprint='a'*64, identities=(identity,))
            self.assertIsNone(store.load_optional('task'))
            with store._mutation_lock():
                self.assertTrue(file_lock._acquisition_states)

    def test_distinct_store_instances_wait_for_same_source_registry(self):
        with TemporaryDirectory() as temporary, ThreadPoolExecutor(2) as pool:
            first = SourceOwnershipStore(Path(temporary))
            second = SourceOwnershipStore(Path(temporary) / '.')
            entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
            def hold():
                with first._mutation_lock():
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('持锁屏障未释放')
            def follow():
                attempted.set()
                with second._mutation_lock():
                    return 'entered'
            owner = pool.submit(hold)
            self.assertTrue(entered.wait(5))
            waiter = pool.submit(follow)
            try:
                self.assertTrue(attempted.wait(5))
                time.sleep(.1)
                self.assertFalse(waiter.done(), '正常的跨任务登记应在后台等待')
            finally:
                release.set()
                owner.result(5)
            self.assertEqual('entered', waiter.result(5))

    def test_different_registries_do_not_block_each_other(self):
        with TemporaryDirectory() as temporary, ThreadPoolExecutor(1) as pool:
            first = SourceOwnershipStore(Path(temporary) / 'one')
            second = SourceOwnershipStore(Path(temporary) / 'two')
            def enter():
                with second._mutation_lock():
                    return True
            with first._mutation_lock():
                self.assertTrue(pool.submit(enter).result(2))

    def test_same_thread_reentry_is_rejected(self):
        with TemporaryDirectory() as temporary:
            store = SourceOwnershipStore(Path(temporary))
            with store._mutation_lock():
                started = time.monotonic()
                with self.assertRaises(file_lock.FileLockBusyError):
                    with SourceOwnershipStore(Path(temporary))._mutation_lock():
                        self.fail('同线程不可重入')
                self.assertLess(time.monotonic()-started, .5)

    def test_os_lock_remains_required_when_local_gate_is_free(self):
        with TemporaryDirectory() as temporary:
            store = SourceOwnershipStore(Path(temporary))
            with file_lock.advisory_file_lock(store._lock_resource().path):
                with self.assertRaises(file_lock.FileLockBusyError):
                    with store._mutation_lock():
                        self.fail('本地调度门不能代替操作系统锁')

    def test_exception_releases_gate_and_os_lock(self):
        with TemporaryDirectory() as temporary, ThreadPoolExecutor(1) as pool:
            store = SourceOwnershipStore(Path(temporary))
            with self.assertRaisesRegex(ValueError, 'inside'):
                with store._mutation_lock():
                    raise ValueError('inside')
            def enter():
                with SourceOwnershipStore(Path(temporary))._mutation_lock():
                    return True
            self.assertTrue(pool.submit(enter).result(2))

    def test_inner_busy_error_is_not_mislabeled_as_projection_collision(self):
        with TemporaryDirectory() as temporary:
            service = SimpleNamespace(_lock_resource=lambda task_id:
                file_lock.LockResource(None, Path(temporary)/'projection.lock', task_id))
            expected = file_lock.FileLockBusyError('another-store')
            with self.assertRaises(file_lock.FileLockBusyError) as caught:
                with ProjectionCommitService._transaction_lock(service, 'task'):
                    raise expected
            self.assertIs(expected, caught.exception)

    def test_real_projection_collision_still_has_projection_error(self):
        with TemporaryDirectory() as temporary:
            resource = file_lock.LockResource(None, Path(temporary)/'projection.lock', 'task')
            service = SimpleNamespace(_lock_resource=lambda _: resource)
            with file_lock.resource_guard(resource):
                with self.assertRaisesRegex(ProjectionAuthorityError, 'projection transaction'):
                    with ProjectionCommitService._transaction_lock(service, 'task'):
                        self.fail('已有真实事务锁不可绕过')


class EventLoopSourceStoreContracts(unittest.IsolatedAsyncioTestCase):
    async def test_busy_registry_never_waits_on_ui_event_loop(self):
        with TemporaryDirectory() as temporary:
            store = SourceOwnershipStore(Path(temporary))
            entered, release = threading.Event(), threading.Event()
            def hold():
                with store._mutation_lock():
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('持锁屏障未释放')
            owner = asyncio.create_task(asyncio.to_thread(hold))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                started = time.monotonic()
                with self.assertRaises(file_lock.FileLockBusyError):
                    with SourceOwnershipStore(Path(temporary))._mutation_lock():
                        self.fail('事件循环不可同步等待共享锁')
                self.assertLess(time.monotonic()-started, .5)
            finally:
                release.set()
                await owner

    async def test_add_task_without_scan_is_rejected_before_preparation(self):
        from src.pages import add_task_page as page
        from nicegui.client import Client
        from nicegui.page import page as nice_page
        client = Client(nice_page('/queue-admission-negative'), request=None)
        try:
            with client, patch.object(page, 'notify') as notify, \
                    patch.object(page.task_queue, 'prepare_discovered_batch') as prepare:
                dialog = page.AddTaskDialog()
                dialog.path = 'unscanned-source'
                try:
                    await dialog._submit()
                    prepare.assert_not_called()
                    self.assertIn('请先完成来源扫描', str(notify.call_args))
                finally:
                    dialog.delete()
        finally:
            client.delete()
