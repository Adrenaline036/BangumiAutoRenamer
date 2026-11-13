"""共享登记排队与原任务重试，使用隔离文件和有限线程等待。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from src.rename import file_lock
from src.rename.task_batch_scope import TaskBatchScopeManifestStore
from src.rename.provider_link_catalog import ProviderLinkCatalog
from src.rename.projection_authority import SourceOwnershipStore
from src.rename.series_catalog import SeriesCatalog
from src.rename.catalog_namespace import locked, mutation_scope

class SharedQueueContracts(unittest.TestCase):
    def cases(self, root):
        batch=TaskBatchScopeManifestStore(root/'batch')
        links=ProviderLinkCatalog(root/'links')
        ownership=SourceOwnershipStore(root/'ownership')
        catalog=SeriesCatalog(root/'catalog')
        return [('batch',lambda:batch._mutation_lock('test')),
                ('links',links.lock),('ownership',ownership._mutation_lock),
                ('catalog',lambda:locked(catalog,'series-00000000-0000-0000-0000-000000000001',read_only=True)),
                ('catalog-mutation',lambda:mutation_scope(catalog))]

    def test_shared_stores_wait_for_other_task_in_same_process(self):
        with tempfile.TemporaryDirectory() as folder:
            for (name,guard),(_,other_guard) in zip(self.cases(Path(folder)),self.cases(Path(folder))):
                with self.subTest(store=name):
                    held=threading.Event();release=threading.Event();attempted=threading.Event()
                    def hold():
                        with guard():held.set();release.wait(3)
                    def acquire():
                        attempted.set()
                        with other_guard():return 'acquired'
                    with ThreadPoolExecutor(2) as pool:
                        first=pool.submit(hold);self.assertTrue(held.wait(2))
                        second=pool.submit(acquire);self.assertTrue(attempted.wait(2))
                        try:
                            deadline=time.monotonic()+.6
                            while time.monotonic()<deadline:
                                self.assertFalse(second.done(),'共享登记应等待另一任务结束')
                                time.sleep(.01)
                        finally:release.set()
                        first.result(2);self.assertEqual('acquired',second.result(2))

    def test_no_contention_controls(self):
        with tempfile.TemporaryDirectory() as folder:
            for name,guard in self.cases(Path(folder)):
                with self.subTest(store=name):
                    with guard():pass
                    with guard():pass

    def test_reentry_keeps_os_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            resource=file_lock.LockResource(None,Path(folder)/'test.lock','probe')
            with file_lock.serialized_resource_guard(resource):
                with self.assertRaises(file_lock.FileLockBusyError):
                    with file_lock.serialized_resource_guard(resource):pass
            with file_lock.serialized_resource_guard(resource):
                with self.assertRaises(file_lock.FileLockBusyError):
                    with file_lock.advisory_file_lock(resource.path):pass

    def test_raising_body_releases_shared_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            store=TaskBatchScopeManifestStore(Path(folder))
            with self.assertRaisesRegex(ValueError,'body'):
                with store._mutation_lock('test'):raise ValueError('body')
            with ThreadPoolExecutor(1) as pool:
                def again():
                    with store._mutation_lock('again'):return True
                self.assertTrue(pool.submit(again).result(2))

    def test_other_process_is_excluded_and_release_allows_retry(self):
        child = '''import os,sys
fd=os.open(sys.argv[1],os.O_CREAT|os.O_EXCL|os.O_RDWR)
os.write(fd,b'\\0');os.lseek(fd,0,os.SEEK_SET)
if os.name=='nt':
    import msvcrt
    msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
else:
    import fcntl
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
print('LOCKED',flush=True)
sys.stdin.readline()
os.close(fd)
'''
        with tempfile.TemporaryDirectory() as folder:
            resource=file_lock.LockResource(None,Path(folder)/'process.lock','process')
            process=subprocess.Popen([sys.executable,'-B','-c',child,str(resource.path)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                with ThreadPoolExecutor(1) as pool:
                    self.assertEqual('LOCKED',pool.submit(process.stdout.readline).result(3).strip())
                with self.assertRaises(file_lock.FileLockBusyError):
                    with file_lock.serialized_resource_guard(resource):self.fail('跨进程锁不能被绕过')
            finally:
                process.communicate('release\n',timeout=3)
            self.assertEqual(0,process.returncode)
            with file_lock.serialized_resource_guard(resource):pass

    def test_busy_cause_is_typed_and_cycle_safe(self):
        error=ValueError('读取失败')
        self.assertFalse(file_lock.is_lock_busy(error))
        error.__cause__=file_lock.FileLockBusyError('共享登记忙')
        self.assertTrue(file_lock.is_lock_busy(error))
        error.__cause__=error
        self.assertFalse(file_lock.is_lock_busy(error))

class QueueFeedbackContracts(unittest.TestCase):
    def test_retry_projection_keeps_stale_nonretryable_and_pending_gates(self):
        from copy import deepcopy
        from src.rename.provider_recovery import can_refresh_selection
        context=dict(lifecycle='failed',candidate={'provider_id':'101'},failure_code='selection_failed',
                     retryable=True,retry_requires_refresh=False)
        before=deepcopy(context)
        self.assertTrue(can_refresh_selection(context))
        self.assertEqual(before,context)
        for changes in ({'retryable':False},{'failure_code':'stale_request'},
                        {'lifecycle':'pending'},{'candidate':None}):
            with self.subTest(changes=changes):
                self.assertFalse(can_refresh_selection({**context,**changes}))

    def test_busy_selection_has_specific_message_and_original_retry(self):
        from tests.test_dashboard_provider_stability import ProviderRecovery
        from src.rename.review_operation import current_provider_selection_review_operation,build_provider_selection_review_operation,set_review_operation
        from src.rename.provider_recovery import can_refresh_selection
        from src.pages.data_table_page import task_feedback_text
        from src.pages.review_operation_view import build_review_operation_presentation
        task=ProviderRecovery().failed_task()
        _,ctx=current_provider_selection_review_operation(task);ctx=dict(ctx)
        ctx.update(failure_code='internal_busy',retryable=True,retry_requires_refresh=False,provider_subject_evidence=None)
        set_review_operation(task,build_provider_selection_review_operation(task,ctx))
        self.assertTrue(can_refresh_selection(ctx))
        self.assertIn('排队',task_feedback_text(task))
        self.assertIn('排队',build_review_operation_presentation(task).reason_text)

class EventLoopContracts(unittest.IsolatedAsyncioTestCase):
    async def test_event_loop_never_waits_for_shared_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            resource=file_lock.LockResource(None,Path(folder)/'test.lock','probe')
            held=threading.Event();release=threading.Event()
            def hold():
                with file_lock.serialized_resource_guard(resource):held.set();release.wait(2)
            worker=asyncio.create_task(asyncio.to_thread(hold))
            while not held.is_set():await asyncio.sleep(.005)
            try:
                begin=time.monotonic()
                with self.assertRaises(file_lock.FileLockBusyError):
                    with file_lock.serialized_resource_guard(resource):pass
                self.assertLess(time.monotonic()-begin,.2)
            finally:release.set();await worker
