"""Graceful queue shutdown with actual NiceGUI thread cancellation semantics."""
import asyncio
from contextlib import suppress
from pathlib import Path
import tempfile
import threading
import unittest

from nicegui import run
from src.task_queue import TaskQueueManager
from src.rename.plan_execution import PlanExecutionError
from tests.source_collection_fixture import admit_task


class QueueShutdownContract(unittest.IsolatedAsyncioTestCase):
    async def test_idle_stop_returns_without_running_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            manager=TaskQueueManager(task_root=root/'tasks',plan_root=root/'plans',
                decision_state_root=root/'decisions',recovery_abandon_audit_root=root/'abandon')
            await manager.start()
            await asyncio.wait_for(manager.stop(),2)
            self.assertIsNone(manager._worker_task)

    async def test_active_stop_waits_for_thread_and_does_not_take_next_job(self):
        await self._active_case(cancel_waiter=False)

    async def test_cancelling_stop_waiter_does_not_cancel_work_or_allow_restart(self):
        await self._active_case(cancel_waiter=True)

    async def _active_case(self, *, cancel_waiter):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            entered,release,finished=threading.Event(),threading.Event(),threading.Event()
            ran=[]
            def work(job):
                ran.append(job.task_id);entered.set()
                if not release.wait(5):raise TimeoutError('bounded synthetic thread')
                manager._update(job.task_id,workflow_status='need_confirm')
                finished.set()
            async def executor(job):return await run.io_bound(work,job)
            manager=TaskQueueManager(task_root=root/'tasks',plan_root=root/'plans',
                decision_state_root=root/'decisions',recovery_abandon_audit_root=root/'abandon',executor=executor)
            for n in (1,2):(root/f'{n}.mkv').write_bytes(b'isolated')
            first=await admit_task(manager, root/'1.mkv',is_anime=False,is_movie=False)
            waiters=[]
            try:
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait,2),3))
                second=await admit_task(manager, root/'2.mkv',is_anime=False,is_movie=False)
                stopper=asyncio.create_task(manager.stop());waiters.append(stopper)
                await asyncio.sleep(.02)
                if cancel_waiter:
                    stopper.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(asyncio.shield(stopper),.2)
                self.assertFalse(finished.is_set())
                self.assertEqual(2,manager._queue._unfinished_tasks,'active work must not be acknowledged early')
                self.assertEqual(first,manager.active_task_id)
                self.assertEqual('running',manager._read(first)['workflow_status'])
                with self.assertRaisesRegex(PlanExecutionError,'停止'):
                    await manager.start()
                another=asyncio.create_task(manager.stop());waiters.append(another)
                await asyncio.sleep(.02)
                self.assertFalse(another.done())
                release.set()
                await asyncio.wait_for(another,3)
                if not cancel_waiter:await asyncio.wait_for(stopper,3)
                self.assertTrue(finished.is_set())
                self.assertEqual([first],ran)
                self.assertEqual('need_confirm',manager._read(first)['workflow_status'])
                self.assertEqual('queued',manager._read(second)['workflow_status'])
                self.assertEqual(1,manager._queue._unfinished_tasks)
                self.assertIsNone(manager._worker_task)
            finally:
                # Even the incident RED must release its bounded real thread
                # and then cancel only the idle synthetic queue waiter.
                release.set()
                await asyncio.to_thread(finished.wait,2)
                await asyncio.sleep(.02)
                worker=manager._worker_task
                if worker is not None and not worker.done():worker.cancel()
                if worker is not None:
                    with suppress(asyncio.CancelledError):await asyncio.wait_for(worker,2)
                for waiter in waiters:
                    if not waiter.done():waiter.cancel()
                    with suppress(asyncio.CancelledError):await waiter
