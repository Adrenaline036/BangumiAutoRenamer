import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from src.pages import execution_wait as wait, data_table_page as dashboard
from src.rename import series_registration as registration
from src.rename.plan_store import PlanStore
from tests import test_simplify_queue_preflight as preflight


class BackgroundControls(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=preflight.QueuePreflightContracts.asyncSetUp
    asyncTearDown=preflight.QueuePreflightContracts.asyncTearDown
    block=preflight.QueuePreflightContracts.block
    wait_entered=preflight.QueuePreflightContracts.wait_entered

    async def test_cancel_waits_for_real_thread_and_cannot_execute(self):
        before=self.path.read_bytes()
        self.service.validate_approved.side_effect=self.block(self.plan)
        with patch.object(wait,'changed'):
            work=wait.submit(self.manager,'task','a'*64)
            await self.wait_entered()
            with self.assertRaises(ValueError):wait.submit(self.manager,'task','a'*64)
            cancel=asyncio.create_task(wait.cancel(self.manager,'task'))
            await asyncio.sleep(.03)
            self.assertFalse(cancel.done())
            self.release.set()
            self.assertTrue(await cancel)
        self.assertEqual(before,self.path.read_bytes())
        self.assertEqual([],self.executed)
        self.assertTrue(work.cancelled)
        self.assertFalse(self.manager._pending)

    async def test_error_keeps_approval_and_projects_normal_review(self):
        before=self.path.read_bytes()
        self.service.validate_approved.side_effect=ValueError('测试来源变化')
        with patch.object(wait,'changed'):
            work=wait.submit(self.manager,'task','a'*64)
            await work.worker
        self.assertIn('测试来源变化',work.error)
        row={'uuid':'task','plan_fingerprint':'a'*64,'workflow_status':'approved','_display':{'title':'sample'}}
        projected=wait.overlay_rows(self.manager,[row])[0]
        self.assertTrue(projected['_selection_review'])
        self.assertIn('测试来源变化',projected['_display']['feedback'])
        self.assertEqual(before,self.path.read_bytes())
        self.assertEqual([],self.executed)
        row['plan_fingerprint']='b'*64
        self.assertNotIn('_selection_review',wait.overlay_rows(self.manager,[row])[0])

    async def test_closing_caller_does_not_cancel_real_admission(self):
        self.service.validate_approved.side_effect=self.block(self.plan)
        with patch.object(wait,'changed'):
            work=wait.submit(self.manager,'task','a'*64)
            await self.wait_entered()
            self.assertFalse(work.worker.done())
            self.release.set();await work.worker
        self.assertTrue(work.admitted)
        self.assertFalse(work.error)
        self.assertEqual(1,self.service.validate_approved.call_count)

    async def test_immediate_cancel_does_not_start_validation(self):
        with patch.object(wait,'changed'):
            work=wait.submit(self.manager,'task','a'*64)
            self.assertTrue(await wait.cancel(self.manager,'task'))
        self.service.validate_approved.assert_not_called()
        self.assertTrue(work.cancelled)

    async def test_cancel_admitted_execution_restores_exact_approval_and_skips_job(self):
        before=self.path.read_bytes()
        async def start_without_consumers():self.manager._started=True
        with patch.object(wait,'changed'),patch.object(self.manager,'start',side_effect=start_without_consumers):
            work=wait.submit(self.manager,'task','a'*64)
            await work.worker
            self.assertTrue(work.admitted)
            self.assertIn('task',self.manager._pending)
            self.assertTrue(await wait.cancel(self.manager,'task'))
        self.assertEqual(before,self.path.read_bytes())
        self.assertFalse(self.manager._pending)
        self.manager._worker_task=asyncio.create_task(self.manager._worker())
        self.manager._execution_worker_task=asyncio.create_task(self.manager._worker(media=True))
        await asyncio.wait_for(self.manager._queue.join(),2)
        await asyncio.wait_for(self.manager._execution_queue.join(),2)
        self.assertFalse(self.executed)
        self.assertEqual(before,self.path.read_bytes())

    async def test_started_execution_cannot_be_withdrawn(self):
        with patch.object(wait,'changed'):
            work=wait.submit(self.manager,'task','a'*64)
            self.manager.active_execution_task_id='task'
            self.assertFalse(await wait.cancel(self.manager,'task'))
            self.manager.active_execution_task_id=None
            await work.worker


class RelatedPlanControls(unittest.TestCase):
    def run_case(self, *, missing=False, drift=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'task').mkdir();(root/'plan').mkdir()
            identity=lambda:NS(primary_provider=NS(namespace='tmdb',entity_type='tv',provider_id='1'))
            current=NS(task_id='selected',work_identity=identity(),metadata={'season_number':1})
            store=PlanStore(root/'plan');constructed=[]
            for name in ('selected','related'):
                (root/'task'/f'{name}.json').write_text(json.dumps({'uuid':name}),encoding='utf-8')
                data={'task_id':name,'work_identity':{'primary_provider':{'namespace':'tmdb','entity_type':'tv','provider_id':'1'}},'metadata':{'season_number':1}}
                if missing and name=='related':data.pop('work_identity')
                (root/'plan'/f'{name}.json').write_text(json.dumps(data),encoding='utf-8')
            def build(payload, task_id):
                constructed.append(task_id)
                if drift and task_id=='selected':(root/'plan/selected.json').write_text('{}',encoding='utf-8')
                return NS(task_id=task_id,work_identity=identity(),metadata={'season_number':1})
            with patch.object(store,'_plan_from_payload',side_effect=build),patch.object(PlanStore,'fingerprint',return_value='same'),patch.object(registration,'is_series_plan',return_value=False):
                if drift:
                    with self.assertRaisesRegex(ValueError,'变化'):
                        registration.validate_current_task_set(store,Mock(),root/'task',expected_plan=current)
                else:registration.validate_current_task_set(store,Mock(),root/'task',expected_plan=current)
            self.assertEqual({'selected','related'},set(constructed))

    def test_related_is_loaded(self):self.run_case()
    def test_missing_identity_falls_back(self):self.run_case(missing=True)
    def test_related_drift_rejected(self):self.run_case(drift=True)


class DashboardControls(unittest.TestCase):
    def test_changed_task_and_decision_invalidate_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tasks=root/'task';decisions=root/'decision';tasks.mkdir();decisions.mkdir()
            path=tasks/'test.json';decision=decisions/'test.json'
            path.write_text('{"uuid":"test","workflow_status":"approved"}',encoding='utf-8');decision.write_text('{}',encoding='utf-8')
            row={'uuid':'test','workflow_status':'approved','_display':{'title':'sample'}}
            with patch.object(dashboard,'TASK_PATH',tasks),patch.object(dashboard,'DECISION_STATE_PATH',decisions),patch.object(dashboard,'get_task',side_effect=lambda _:json.loads(path.read_text('utf-8'))),patch.object(dashboard,'_read_card_decision',return_value=None) as read,patch.object(dashboard,'_card_projection',return_value=row):
                dashboard._prepare_dashboard(None,force=True)
                dashboard._prepare_dashboard(None,force=True)
                self.assertEqual(1,read.call_count)
                decision.write_text('{"changed":true}',encoding='utf-8')
                dashboard._prepare_dashboard(None,force=True)
                self.assertEqual(2,read.call_count)
                path.write_text('{"uuid":"test","workflow_status":"queued","new":true}',encoding='utf-8')
                dashboard._prepare_dashboard(None,force=True)
                self.assertEqual(3,read.call_count)

    def test_two_clients_receive_wait_and_terminal_state(self):
        from nicegui import Client
        from src.task_queue import task_queue
        row={'uuid':'sync-task','workflow_status':'approved','plan_fingerprint':'a'*64,'_display':{'title':'sample'}}
        clients={'one':nullcontext(),'two':nullcontext()}
        views={key:Mock() for key in clients}
        request=NS(fingerprint='a'*64,admitted=False,finished=False,cancelling=False,error='',cancelled=False)
        with patch.object(Client,'instances',clients),patch.object(dashboard,'_dashboard_views',views),patch.object(dashboard,'_dashboard_rows',{key:[row] for key in clients}),patch.object(dashboard,'_dashboard_signatures',{}),patch.object(task_queue,'_execution_waits',{'sync-task':request},create=True):
            dashboard.project_selection_change(task_queue)
            for view in views.values():
                published=view.publish.call_args.args[0]
                self.assertEqual('sync-task',published['active'][0]['uuid'])
                self.assertTrue(published['active'][0]['_execution_wait'])
            request.finished=True;request.error='预检失败'
            dashboard.project_selection_change(task_queue)
            for view in views.values():
                published=view.publish.call_args.args[0]
                self.assertFalse(published['active'])
                self.assertEqual('sync-task',published['review'][0]['uuid'])
