"""批准及执行交接的定向合同，使用隔离文件与阻塞预检。"""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, AsyncMock, patch

from src.pages import plan_review_page as page, data_table_page as dashboard
from src.rename import series_registration as registration
from src.rename.plan_store import PlanStore


class ExecutionFlow(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_returns_before_preflight_and_closes_both_windows(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def enqueue(*args):
            entered.set()
            await release.wait()
        view=NS(plan=NS(task_id='latency-task'),_close_review=Mock(),_refresh_execute_button=Mock())
        dialog=NS(close=Mock())
        with patch.object(page.task_queue,'enqueue_approved',side_effect=enqueue), patch.object(page,'notify'), patch.object(page,'get_task',return_value={}):
            job=asyncio.create_task(page.PlanReviewDialog._queue_execution(view,dialog,'a'*64))
            try:
                await asyncio.wait_for(entered.wait(),2)
                done,_=await asyncio.wait({job},timeout=.15)
                self.assertTrue(done,'确认回调仍等待预检结束')
                dialog.close.assert_called_once()
                view._close_review.assert_called_once()
            finally:
                release.set();await job;await asyncio.sleep(.05)


class ReadScope(unittest.TestCase):
    def test_unrelated_plan_is_not_constructed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'task').mkdir();(root/'plan').mkdir()
            identity=lambda n:NS(primary_provider=NS(namespace='tmdb',entity_type='tv',provider_id=n))
            current=NS(task_id='selected',work_identity=identity('1'),metadata={'season_number':1})
            store=PlanStore(root/'plan')
            constructed=[]
            def build(payload, task_id):
                constructed.append(task_id)
                return NS(task_id=task_id,work_identity=identity(payload['work_identity']['primary_provider']['provider_id']),metadata=payload['metadata'])
            for name,pid in [('selected','1'),('unrelated','2')]:
                (root/'task'/f'{name}.json').write_text(json.dumps({'uuid':name}),encoding='utf-8')
                (root/'plan'/f'{name}.json').write_text(json.dumps({'task_id':name,'work_identity':{'primary_provider':{'namespace':'tmdb','entity_type':'tv','provider_id':pid}},'metadata':{'season_number':1}}),encoding='utf-8')
            with patch.object(store,'_plan_from_payload',side_effect=build),patch.object(PlanStore,'fingerprint',return_value='same'),patch.object(registration,'is_series_plan',return_value=False):
                registration.validate_current_task_set(store,Mock(),root/'task',expected_plan=current)
            self.assertEqual(['selected'],constructed)

    def test_dashboard_second_client_reuses_unchanged_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tasks=root/'task';decisions=root/'decision';tasks.mkdir();decisions.mkdir()
            (tasks/'test.json').write_text('{"uuid":"test","workflow_status":"approved"}',encoding='utf-8')
            (decisions/'test.json').write_text('{}',encoding='utf-8')
            row={'uuid':'test','workflow_status':'approved','_display':{'title':'sample'}}
            with patch.object(dashboard,'get_task',side_effect=lambda task_id:json.loads((tasks/(task_id+'.json')).read_text('utf-8'))),patch.object(dashboard,'_display_task_rows',side_effect=lambda:dashboard.load_task_rows(tasks)),patch.object(dashboard,'TASK_PATH',tasks),patch.object(dashboard,'DECISION_STATE_PATH',decisions),patch.object(dashboard,'_read_card_decision',return_value=None) as read,patch.object(dashboard,'_card_projection',return_value=row):
                dashboard._prepare_dashboard(None,force=True)
                dashboard._prepare_dashboard(None,force=True)
                self.assertEqual(1,read.call_count,'不同客户端重复读取同一份决定记录')
