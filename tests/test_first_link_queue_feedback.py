"""后台准备和接纳提示只反映实际完成阶段。"""
import asyncio
from unittest.mock import patch
from tests.test_planning_lifecycle_controls import PlanningLifecycleControls
from src.pages import initial_workspace, plan_review_page as page, provider_selection_work as work
from src.rename.plan_execution import PlanExecutionError
from tests.test_content_choice_contract import records
import unittest

class FirstLinkQueueFeedback(unittest.IsolatedAsyncioTestCase):
    flow = PlanningLifecycleControls.flow
    admit_auto = PlanningLifecycleControls.admit_auto
    dialog = PlanningLifecycleControls.dialog
    candidate = staticmethod(PlanningLifecycleControls.candidate)

    async def test_preparation_and_admission_feedback_match_actual_stage(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view=self.dialog(f)
            preparing, prepared, running, finish = (asyncio.Event() for _ in range(4))
            original=initial_workspace.create_initial_workspace
            async def delayed(*a, **kw):
                preparing.set(); await prepared.wait(); return await original(*a, **kw)
            async def executor(job):
                running.set(); await finish.wait(); return await f.manager._execute_rename(job)
            f.manager.executor=executor
            try:
                with patch.object(page, '_read_manual_provider_url', return_value=self.candidate()), \
                        patch.object(initial_workspace, 'create_initial_workspace', side_effect=delayed):
                    request=await work.submit(view)
                    observer=asyncio.create_task(work.watch(view,request))
                    await asyncio.wait_for(preparing.wait(),5)
                    await asyncio.sleep(.25)
                    self.assertIn('正在准备任务',view.initial_progress.text)
                    self.assertFalse(request.admitted)
                    prepared.set(); await asyncio.wait_for(running.wait(),15)
                    await asyncio.sleep(.25)
                    self.assertTrue(request.admitted)
                    self.assertIn('正在后台生成整理计划',view.initial_progress.text)
                    view._close_review(); finish.set()
                    await asyncio.wait_for(request.worker,20); await observer
                    self.assertFalse(request.error,request.error)
            finally:
                prepared.set();finish.set();view.delete()

    async def test_failed_admission_never_reports_accepted(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as f:
            view=self.dialog(f); snapshots=[]
            def reject(*a,**kw):
                snapshots.append(records(f.root)); raise PlanExecutionError('本次后台提交失败')
            try:
                with patch.object(page, '_read_manual_provider_url', return_value=self.candidate()), \
                        patch.object(f.manager, 'enqueue_reidentify', side_effect=reject):
                    request=await work.submit(view);await request.worker
                self.assertFalse(request.admitted)
                self.assertIn('本次后台提交失败',request.error)
                self.assertEqual(snapshots[0],records(f.root))
                self.assertTrue(f.manager._queue.empty())
            finally:view.delete()
