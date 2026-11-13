"""真实NiceGUI批准和确认回调；预检阻塞，不执行媒体。"""
import asyncio
import threading
import time
from unittest.mock import patch, Mock
import unittest
from nicegui import ui
from nicegui.client import Client
from nicegui.page import page as nice_page
from src.pages import data_table_page as dashboard
from tests import test_queued_work_selection as queued
from src.pages import plan_review_page as page, execution_wait


class ExecutionPage(unittest.IsolatedAsyncioTestCase):
    original=queued.QueuedWorkSelection.original
    create_page=queued.QueuedWorkSelection.create_page
    setup_work=queued.QueuedWorkSelection.setup_work

    async def test_approval_saves_then_confirmation_releases_page(self):
        root,service,plan=await self.setup_work()
        source={str(e.source_path):e.source_path.read_bytes() for e in plan.entries}
        other=Client(nice_page('/isolated-other-device'),request=None)
        self.addCleanup(other.delete)
        clients=[self.client,other]
        views={c.id:Mock() for c in clients}
        row=dashboard._card_projection(page.task_queue._read(plan.task_id))
        self.enterContext(patch.object(dashboard,'task_queue',page.task_queue))
        self.enterContext(patch.object(dashboard,'_dashboard_views',views))
        self.enterContext(patch.object(dashboard,'_dashboard_rows',{c.id:[row] for c in clients}))
        self.enterContext(patch.object(dashboard,'_dashboard_signatures',{}))
        execution=page.task_queue.execution_service
        with self.assertRaisesRegex(Exception,'尚未全部批准'):
            execution.validate_approved_authority(plan.task_id,page.PlanStore.fingerprint(plan))
        entered,release=threading.Event(),threading.Event()
        original=service.approve_entries
        def approve(*args,**kwargs):
            entered.set()
            if not release.wait(5):raise AssertionError('批准未释放')
            return original(*args,**kwargs)
        with patch.object(service,'approve_entries',side_effect=approve):
            with self.client:pending=asyncio.create_task(self.view._approve_all())
            await asyncio.to_thread(entered.wait,2)
            from src.pages.tv_content_actions import revision_progress
            self.assertTrue(revision_progress(self.view).approval)
            self.assertTrue(revision_progress(self.view).running)
            for view in views.values():self.assertTrue(view.publish.call_args.args[0]['review'][0]['_approval_wait'])
            self.assertNotEqual('approved',page.task_queue._read(plan.task_id)['workflow_status'])
            release.set();await pending
        self.assertEqual('approved',page.task_queue._read(plan.task_id)['workflow_status'])
        self.assertEqual('approved',service.load(plan.task_id).status.value)
        with self.assertRaisesRegex(Exception,'版本已变化'):
            execution.validate_approved_authority(plan.task_id,'0'*64)
        self.assertEqual(plan.task_id,execution.validate_approved_authority(plan.task_id,page.PlanStore.fingerprint(plan)).task_id)
        for view in views.values():self.assertEqual('approved',view.publish.call_args.args[0]['review'][0]['workflow_status'])
        self.assertEqual(source,{str(e.source_path):e.source_path.read_bytes() for e in plan.entries})
        gate=asyncio.Event();started=asyncio.Event()
        async def enqueue(*args):started.set();await gate.wait();raise ValueError('隔离预检失败')
        with patch.object(page.task_queue,'enqueue_approved',side_effect=enqueue):
            with self.client:
                self.view._confirm_execute()
                buttons=[e for e in self.client.elements.values() if e._props.get('label')=='确认入队执行']
                self.assertEqual(1,len(buttons),self.notices)
                confirm=next(e for e in self.client.elements.values() if isinstance(e,ui.dialog) and getattr(e,'queue_confirmation',None) is not None)
                begin=time.perf_counter()
                await self.driver.click_element(buttons[0])
                elapsed=time.perf_counter()-begin
            self.assertLess(elapsed,1)
            self.assertFalse(confirm.value)
            self.assertFalse(self.view.value)
            for view in views.values():self.assertTrue(view.publish.call_args.args[0]['active'][0]['_execution_wait'])
            await started.wait()
            work=execution_wait.registry(page.task_queue)[plan.task_id]
            self.assertFalse(work.worker.done())
            gate.set();await work.worker
            self.assertIn('隔离预检失败',work.error)
            for view in views.values():
                value=view.publish.call_args.args[0]
                self.assertFalse(value['active'])
                self.assertIn('隔离预检失败',value['review'][0]['_display']['feedback'])
        self.assertEqual('approved',page.task_queue._read(plan.task_id)['workflow_status'])
        self.assertTrue(all(not e.target_path.exists() for e in plan.entries))
        print(f'confirmation_callback_seconds={elapsed:.6f}')

    async def test_real_related_plan_conflict_predicate_remains(self):
        from src.rename.series_registration import inspect_cross_task_series_consistency
        first=await self.original()
        second=await self.original()
        self.assertNotEqual(first[2].metadata['canonical_work_root'],second[2].metadata['canonical_work_root'])
        self.assertEqual(first[2].work_identity.primary_provider.provider_id,second[2].work_identity.primary_provider.provider_id)
        self.assertTrue(inspect_cross_task_series_consistency([first[2],second[2]]))
