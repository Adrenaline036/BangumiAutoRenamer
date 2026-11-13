"""登记读取权限与后台执行互斥的反例。"""
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from src.rename.series_catalog import SeriesCatalog
from src.rename.work_catalog import WorkCatalog
from src.rename.catalog_namespace import mutation_scope, require_mutation, require_write, writer_scope


class ExecutionCatalogGuards(unittest.TestCase):
    def catalogs(self, root):
        return [(SeriesCatalog(root/'series'), 'series-00000000-0000-0000-0000-000000000001'),
                (WorkCatalog(root/'work'), 'work-00000000000000000000000000000001')]

    def test_media_guard_allows_readers_but_excludes_writers_and_second_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            for catalog,key in self.catalogs(Path(directory)):
                with mutation_scope(catalog):
                    def reader():
                        with catalog.lock(key, read_only=True):
                            return catalog.namespace_snapshot(key)
                    def writer():
                        with self.assertRaisesRegex(RuntimeError, '已有整理'):
                            with catalog.lock(key):self.fail('并发写入不能取得保护')
                        with self.assertRaisesRegex(RuntimeError, '已有整理'):
                            with mutation_scope(catalog):self.fail('第二执行不能取得保护')
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        self.assertEqual((),pool.submit(reader).result(3).records)
                        pool.submit(writer).result(3)

    def test_readonly_cannot_write_or_upgrade_even_when_same_thread_owns_media_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            for catalog,key in self.catalogs(Path(directory)):
                with mutation_scope(catalog), catalog.lock(key,read_only=True):
                    with self.assertRaisesRegex(RuntimeError,'只读'):
                        if type(catalog) is SeriesCatalog:catalog._atomic_write(key,{})
                        else:catalog.write_locked(key,{})
                    with self.assertRaisesRegex(RuntimeError,'只读'):
                        with writer_scope(catalog,key):self.fail('读取租约不能升级')
                    self.assertFalse(catalog._path(key).exists())

    def test_mutation_exception_releases_os_guard_and_expired_lease_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for catalog,key in self.catalogs(Path(directory)):
                with self.assertRaisesRegex(ValueError,'probe'):
                    with mutation_scope(catalog):raise ValueError('probe')
                with self.assertRaisesRegex(RuntimeError,'缺少实际'):
                    require_mutation(catalog)
                with catalog.lock(key):require_write(catalog,key)


class QueueStopGuards(unittest.IsolatedAsyncioTestCase):
    async def test_stop_waits_for_both_active_lanes_and_rejects_same_task_activity(self):
        import asyncio
        from src.task_queue import TaskQueueManager,TaskJob
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);entered=[asyncio.Event(),asyncio.Event()];release=[asyncio.Event(),asyncio.Event()]
            async def work(job,index):
                entered[index].set();await release[index].wait()
                manager._update(job.task_id,workflow_status='need_confirm')
            async def media(job):return await work(job,0)
            async def identify(job):return await work(job,1)
            manager=TaskQueueManager(task_root=root/'tasks',plan_root=root/'plans',
                decision_state_root=root/'decisions',recovery_abandon_audit_root=root/'abandon',
                executor=identify,approved_executor=media)
            await manager.start()
            try:
                for name,op in [('media','execute_approved'),('identify','rename')]:
                    manager._write(name,{'uuid':name,'workflow_status':'queued'})
                    manager._pending.append(name);manager._queue.put_nowait(TaskJob(name,None,operation=op))
                await asyncio.wait_for(asyncio.gather(*(x.wait() for x in entered)),3)
                self.assertTrue(manager.is_active('media'));self.assertTrue(manager.is_active('identify'))
                self.assertFalse(manager.cancel_queued('media'));self.assertFalse(manager.cancel_queued('identify'))
                stop=asyncio.create_task(manager.stop())
                await asyncio.sleep(0.05);self.assertFalse(stop.done())
                release[0].set();await asyncio.sleep(0.05);self.assertFalse(stop.done())
                release[1].set();await asyncio.wait_for(stop,3)
                self.assertIsNone(manager._worker_task);self.assertIsNone(manager._execution_worker_task)
                await asyncio.wait_for(manager._queue.join(),1)
            finally:
                for event in release:event.set()
                await manager.stop()
