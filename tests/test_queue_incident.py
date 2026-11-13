"""原窗口保存待选季度与另一任务接纳争用共享批次锁。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

class QueueIncidentContracts(unittest.IsolatedAsyncioTestCase):
    async def test_pending_season_survives_other_task_admission_lock(self):
        from tests.test_selection_season_same_window import SameWindowSeasonContracts
        from src.rename import ova_selection
        from src.rename.recognition_queue_admission import _authority_locks
        helper=SameWindowSeasonContracts('runTest');original=ova_selection.persist_pending;observed=[]
        def competing(manager,job,outcome):
            held=threading.Event();release=threading.Event()
            def other_task():
                with _authority_locks(manager,'other-task'):
                    held.set();release.wait(3)
            with ThreadPoolExecutor(1) as pool:
                holder=pool.submit(other_task)
                assert held.wait(2)
                timer=threading.Timer(.15,release.set);timer.start()
                try:
                    result=original(manager,job,outcome);observed.append(job.task_id);return result
                finally:
                    release.set();timer.join();holder.result(2)
        with patch.object(ova_selection,'persist_pending',side_effect=competing):
            await helper.initial([],4)
        self.assertEqual(1,len(observed))
