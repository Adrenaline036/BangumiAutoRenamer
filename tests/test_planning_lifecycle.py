"""关闭观察窗口不撤销已提交的后台作品选择。"""
import asyncio
import unittest
from unittest.mock import patch
from nicegui.client import Client
from src.pages import plan_review_page as page, initial_workspace
from src.rename.plan_store import PlanStore
from tests import test_initial_auto_link_contract as initial
from tests.test_initial_auto_link_contract import detail
from tests.test_provider_search_v5 import _Session
from src.rename.provider_search import resolve_provider_item_url

class PlanningLifecycle(unittest.IsolatedAsyncioTestCase):
    flow = initial.InitialAutoLinkContracts.flow
    admit_auto = initial.InitialAutoLinkContracts.admit_auto

    async def check_close(self, stage, cancel=False):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as flow:
            with Client.auto_index_client.content:
                dialog=page.PlanReviewDialog(None, flow.manager._read(flow.task_id))
                dialog.open()
            dialog.provider_url_input.value='https://www.themoviedb.org/movie/8181'
            entered, release = asyncio.Event(), asyncio.Event()
            source_before={str(p):p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
            async def resolve(value):
                if stage=='read':
                    entered.set(); await release.wait()
                return resolve_provider_item_url(value,tmdb_api_key='fixture-only',
                    bangumi_user_agent='BAR/test',session=_Session(detail(movie=True)))
            original=initial_workspace.create_initial_workspace
            async def prepare(*args,**kwargs):
                if stage=='prepare':
                    entered.set(); await release.wait()
                return await original(*args,**kwargs)
            execute=flow.manager._execute_rename
            async def executor(job):
                if stage=='queue':
                    entered.set(); await release.wait()
                return await execute(job)
            flow.manager.executor=executor
            try:
                with patch.object(page,'notify'), patch.object(page,'_read_manual_provider_url',side_effect=resolve), \
                        patch.object(initial_workspace,'create_initial_workspace',side_effect=prepare), \
                        patch.object(dialog,'_load_plan_view') as adopt:
                    waiter=asyncio.create_task(dialog._select_manual_provider_url())
                    await asyncio.wait_for(entered.wait(),10)
                    dialog._close_review()
                    if cancel:
                        waiter.cancel()
                        await asyncio.gather(waiter,return_exceptions=True)
                    release.set()
                    if not cancel:await asyncio.wait_for(waiter,20)
                    # Observe the durable outcome, independent of the background handle's shape.
                    deadline = asyncio.get_running_loop().time() + 8
                    while asyncio.get_running_loop().time() < deadline:
                        path = flow.service.decision_state_store.path_for(flow.task_id)
                        if path.exists() and flow.service.decision_state_store.load_readonly(flow.task_id).confirmed_work:
                            break
                        await asyncio.sleep(0.05)
                    await asyncio.wait_for(flow.manager._queue.join(), 20)
                    self.assertTrue(flow.service.decision_state_store.path_for(flow.task_id).exists(), '后台请求未建立workspace')
                    state=flow.service.decision_state_store.load_readonly(flow.task_id)
                    self.assertEqual('8181',(state.confirmed_work.get('primary_provider') or {}).get('provider_id'))
                    plan=flow.store.load(flow.task_id)
                    self.assertEqual(flow.service.decision_state_store.fingerprint(state),plan.metadata['decision_state_fingerprint'])
                    self.assertTrue(all(not entry.target_path.exists() for entry in plan.entries))
                    adopt.assert_not_called()
                self.assertEqual(source_before,{str(p):p.read_bytes() for p in flow.source.rglob('*') if p.is_file()})
            finally:
                release.set(); dialog.delete()

    async def test_close_during_link_read_continues_to_saved_plan(self):
        await self.check_close('read')

    async def test_cancel_window_wait_does_not_cancel_link_work(self):
        await self.check_close('read',cancel=True)

    async def test_close_during_workspace_preparation_continues(self):
        await self.check_close('prepare')

    async def test_close_after_admission_keeps_actual_queue_running(self):
        await self.check_close('queue')

    async def test_confirmed_boundary_survives_close_during_preparation(self):
        from tests.test_tv_boundary_affiliation import TVBoundaryAffiliationContracts
        case = TVBoundaryAffiliationContracts('runTest')
        await case.readonly_exit('close')
