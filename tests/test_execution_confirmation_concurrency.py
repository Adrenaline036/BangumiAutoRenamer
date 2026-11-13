"""隔离真实执行期间，其他任务读取登记并保存人工决定。"""
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests.test_default_activation_contract import DefaultActivationReadinessContracts
from tests._media_flow_helpers import MediaFlow
from src.rename.plan_store import PlanStore
from src.rename.trans import Trans
from src.rename.work_affiliation import catalog_choices


class ExecutionConfirmationContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.helper = DefaultActivationReadinessContracts('runTest')
        self.addCleanup(self.helper.doCleanups)

    async def during_execute(self, movie):
        factory = self.helper.movie if movie else self.helper.tv
        root, service, _, result = await factory()
        peer_root, peer, _, peer_result = await factory()
        if movie:
            peer.work_catalog = service.work_catalog
        else:
            peer.series_catalog = service.series_catalog
        original = Trans.trans_file
        observed = []
        errors = []
        def operate():
            plan = peer.plan_store.load(peer_result.plan.task_id)
            rows = catalog_choices(peer, plan.task_id, PlanStore.fingerprint(plan))
            self.assertEqual([], rows)
            state = peer.decision_state_store.load(plan.task_id)
            if not movie:
                updated = peer.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                    expected_revision=state.revision, mode='season', edit={'season_number': 2})
                self.assertEqual(2, updated.metadata['season_number'])
                self.assertEqual(PlanStore.fingerprint(updated),
                    PlanStore.fingerprint(peer.plan_store.load(plan.task_id)))
            else:
                from src.rename.movie_work_correction import rows_for
                updated = peer.revise_movie_versions(plan.task_id, PlanStore.fingerprint(plan),
                    groups=rows_for(state), excluded_sources=state.movie_version_selection.to_dict()['excluded_sources'])
                self.assertEqual(PlanStore.fingerprint(updated),
                    PlanStore.fingerprint(peer.plan_store.load(plan.task_id)))
            observed.append(True)
        def transfer(instance):
            with ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    pool.submit(operate).result(timeout=10)
                except Exception as exc:
                    errors.append(str(exc))
            return original(instance)
        with patch.object(Trans, 'trans_file', transfer):
            task, ledger, execution = await MediaFlow('runTest')._approve_and_execute(root=root, result=result)
        self.assertEqual('completed', task.get('execution_status'), task.get('error'))
        self.assertEqual('completed', ledger.get('status'))
        self.assertEqual([], errors, '执行期间另一任务被登记锁阻断')
        self.assertEqual([True], observed)

    async def test_tv_execution_allows_other_card_catalog_and_season_save(self):
        await self.during_execute(False)

    async def test_movie_execution_allows_other_movie_catalog_read(self):
        await self.during_execute(True)


class QueueLaneContracts(unittest.IsolatedAsyncioTestCase):
    async def test_execution_wait_does_not_block_recognition_and_execution_stays_serial(self):
        import asyncio
        import tempfile
        from pathlib import Path
        from src.task_queue import TaskQueueManager, TaskJob
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entered, release, recognized = asyncio.Event(), asyncio.Event(), asyncio.Event()
            executions = []
            async def media(job):
                executions.append(job.task_id)
                if job.task_id == 'media-one':
                    entered.set()
                    await release.wait()
                manager._update(job.task_id, workflow_status='need_confirm')
            async def identify(job):
                recognized.set()
                manager._update(job.task_id, workflow_status='need_confirm')
            manager = TaskQueueManager(task_root=root/'tasks', plan_root=root/'plans',
                decision_state_root=root/'decisions', recovery_abandon_audit_root=root/'abandon',
                executor=identify, approved_executor=media)
            logger = patch('src.task_queue.logger.exception')
            errors = logger.start()
            self.addCleanup(logger.stop)
            await manager.start()
            try:
                for name, operation in [('media-one','execute_approved'), ('media-two','execute_approved'), ('identify','rename')]:
                    manager._write(name, {'uuid':name, 'workflow_status':'queued', 'queue_operation':operation})
                    manager._pending.append(name)
                    manager._queue.put_nowait(TaskJob(name, None, operation=operation))
                await asyncio.wait_for(entered.wait(), 3)
                try:
                    await asyncio.wait_for(recognized.wait(), 1)
                    self.assertEqual(['media-one'], executions)
                finally:
                    release.set()
                    await asyncio.wait_for(manager._queue.join(), 4)
                self.assertEqual(['media-one', 'media-two'], executions)
            finally:
                release.set()
                await manager.stop()
            self.assertEqual([], errors.call_args_list)
