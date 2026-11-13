"""当前持久任务经过失败、重新读取后的显示与权威保护。"""
import asyncio
from dataclasses import replace
import unittest
from unittest.mock import patch
from src.pages.review_operation_view import build_review_operation_presentation
from src.rename.review_operation import current_provider_selection_review_operation
from src.rename.provider_transport import ProviderRequestError
from src.utils.utils import mutate_task_at
from tests.test_default_activation_contract import DefaultActivationProducerContracts


class FailureRecoveryContracts(unittest.IsolatedAsyncioTestCase):
    async def test_typed_failure_survives_reload_preserves_authority_and_rejects_late_write(self):
        harness=DefaultActivationProducerContracts('runTest')
        async with harness.flow(movie=True,automatic=False) as flow:
            plan,state=harness.current(flow)
            plan_path=flow.store.path_for(flow.task_id)
            decision_path=flow.service.decision_state_store.path_for(flow.task_id)
            before=(plan_path.read_bytes(),decision_path.read_bytes())
            jobs=[]
            failure=ProviderRequestError(stage='relations',code='timeout',attempts=2,elapsed=20)
            async def fail(job):
                jobs.append(job)
                def update(data):
                    data['provider_selection_failure']={
                        'reason_codes':['provider_transport_timeout'],
                        'transport_diagnostic':failure.diagnostic,
                        'retryable':failure.retryable,'retry_requires_refresh':True}
                    return data
                mutate_task_at(flow.manager.task_root,flow.task_id,update)
                raise RuntimeError(str(failure))
            flow.manager.executor=fail
            from tests.test_provider_subject_plan_wiring_red_contract import _root_config_patch
            from tests.test_r1_real_media_production_red_contract import _configured_renamer
            candidate={'namespace':'tmdb','entity_type':'movie','provider_id':'8181','title':'普通电影'}
            with _root_config_patch(flow.manager.root_snapshot_source.observe().to_dict()),patch(
                    'src.task_queue.Rename',side_effect=lambda **_:_configured_renamer(flow.root)):
                await flow.manager.enqueue_reidentify(flow.task_id,candidate,state.revision)
                await asyncio.wait_for(flow.manager._queue.join(),12)
            after=flow.manager._read(flow.task_id)
            _,context=current_provider_selection_review_operation(after)
            self.assertEqual('failed',context['lifecycle'])
            self.assertEqual('provider_unavailable',context['failure_code'])
            self.assertTrue(context['retry_requires_refresh'])
            self.assertEqual('relations',context['provider_subject_evidence']['transport_diagnostic']['stage'])
            actions={a.key:a for a in build_review_operation_presentation(after).actions}
            self.assertEqual('8181',actions['refresh'].payload['provider_id'])
            self.assertEqual(before,(plan_path.read_bytes(),decision_path.read_bytes()))
            task_before=(flow.manager.task_root/f'{flow.task_id}.json').read_bytes()
            flow.manager._fail_reidentify(replace(jobs[0],request_id='expired'),RuntimeError('old'))
            self.assertEqual(task_before,(flow.manager.task_root/f'{flow.task_id}.json').read_bytes())
            from src.task_queue import TaskQueueManager
            fresh=TaskQueueManager(task_root=flow.manager.task_root,plan_root=flow.store.root,
                decision_state_root=flow.service.decision_state_store.root)
            self.assertEqual(after,fresh._read(flow.task_id))
            self.assertFalse(any(e.target_path.exists() for e in plan.entries))
