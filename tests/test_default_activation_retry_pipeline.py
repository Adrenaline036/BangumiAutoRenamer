"""Open O5 whole-producer regression, not a completed O6 admission control.
Actual canonical TV -> GUI approval -> queue prewrite failure -> GUI replan.
Both frozen original and candidate admission reach the same old producer gap.
No expectedFailure/skip; this must stay RED until the full producer is fixed.
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from tests import test_default_activation_contract as fixture_module

class DefaultActivationRetryPipeline(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixture_module.DefaultActivationProducerContracts()

    async def test_real_prewrite_execution_failure_replans_via_shared_admission(self):
        from tests import _media_flow_helpers as execution
        from src.rename.execution_recovery import requires_execution_replan
        from src.pages import data_table_page as dashboard
        async with self.fixture.flow() as flow:
            plan = flow.store.load(flow.task_id)
            fingerprint = flow.store.fingerprint(plan)
            target = next(e.target_path for e in plan.entries if e.media_kind == 'video')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'owned temporary obstruction for prewrite failure')
            task, ledger, service = await execution.MediaFlow()._approve_and_execute(
                root=flow.root, result=SimpleNamespace(plan=plan, task=flow.manager._read(flow.task_id)))
            self.assertTrue(requires_execution_replan(task), task.get('error'))
            self.assertEqual({}, ledger)
            target.unlink()  # Only the exact obstruction created by this test.
            flow.manager.execution_service = service
            flow.manager.review_root = service.review_service.review_root
            notices = Mock()
            with patch.multiple(dashboard, task_queue=flow.manager,
                    get_task=lambda task_id: flow.manager._read(task_id), notify=notices,
                    refresh_dashboard=Mock(), _review_tasks=Mock(), _history_tasks=Mock()):
                await dashboard._replan_failed_execution(flow.task_id)
                await asyncio.wait_for(flow.manager._queue.join(), 12)
            task = flow.manager._read(flow.task_id)
            self.assertTrue(task.get('execution_replan_history'))
            self.assertNotEqual(fingerprint, flow.store.fingerprint(flow.store.load(flow.task_id)))
            self.assertNotEqual('approved', task['workflow_status'])
            self.assertTrue(any(c.kwargs.get('type') == 'positive' for c in notices.call_args_list))
