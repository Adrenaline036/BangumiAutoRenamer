"""Replacement previews preserve cancellation, ownership and explicit confirmation."""
import asyncio
import threading
import unittest
from unittest.mock import patch
from tests import test_initial_tv_completion_contract as initial
from tests import test_content_workbench_gui as gui
from tests.test_content_choice_contract import records
from src.rename.plan_store import PlanStore
from src.rename.asset_actions import AssetActionService


class PendingReplacement(unittest.IsolatedAsyncioTestCase):
    async def fixture(self, checked=False):
        root,service,_,result,*_=await initial.InitialTVCompletionContracts.fixture(self,anime=False)
        plan=result.plan
        target=next(e.target_path for e in plan.entries if e.media_kind=='video')
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'previous video')
        extra=target.parent/'old.ass';extra.write_bytes(b'previous subtitle')
        holder=gui.ContentWorkbenchGui('runTest')
        holder.create_page(prepared=(root,service,plan,[]),cleanup_owner=self)
        worker=getattr(holder.view,'_season_reference_task',None)
        if worker:await worker
        return root,service,holder,target,extra


    async def test_gui_one_click_checks_once_then_freezes_replace(self):
        root,service,h,target,extra=await self.fixture()
        with patch.object(AssetActionService,'prepare_replacement',autospec=True,side_effect=AssetActionService.prepare_replacement) as prepare:
            await h.driver.click('换源')
        self.assertEqual(1,prepare.call_count)
        button=h.driver.button('生成计划')
        for ancestor in button.ancestors(include_self=True):
            self.assertFalse(ancestor.is_deleted, type(ancestor).__name__)
            self.assertIs(ancestor,ancestor.client.elements.get(ancestor.id))
        await h.driver.click('生成计划')
        plan=service.plan_store.load(h.view.plan.task_id)
        self.assertEqual('replace',plan.metadata['asset_operation']['mode'])
        self.assertEqual('replace',service.decision_state_store.load(plan.task_id).asset_operation.to_dict()['mode'])
        self.assertFalse(service.load_or_create(plan).approved_entry_ids)
        self.assertIn(str(extra),{r['target'] for r in plan.metadata['asset_operation']['retirements']})
        self.assertEqual(b'previous video',target.read_bytes())
        self.assertEqual(b'previous subtitle',extra.read_bytes())




    async def test_late_confirmation_on_closed_preview_is_not_dispatched(self):
        root,service,h,target,extra=await self.fixture(checked=True)
        await h.driver.click('换源')
        button=h.driver.button('生成计划')
        await h.driver.click('取消')
        before=records(root)
        with patch.object(service,'confirm_asset_replacement') as confirm:
            await h.driver.click_element(button)
        self.assertEqual(0,confirm.call_count)
        self.assertEqual(before,records(root))

    async def test_helper_rejects_button_in_deleted_ancestor(self):
        from nicegui import ui
        root,service,h,target,extra=await self.fixture(checked=True)
        with h.client:
            with ui.dialog() as dialog:
                container=ui.column()
            dialog.open();dialog.delete()
            with container:ui.button('已删除父级控制')
        self.assertTrue(dialog.value)  # Deletion does not close its Python value.
        with self.assertRaises(AssertionError):h.driver.button('已删除父级控制')

    async def test_deleted_preview_late_confirmation_is_not_dispatched(self):
        root,service,h,target,extra=await self.fixture(checked=True)
        await h.driver.click('换源')
        button=h.driver.button('生成计划')
        dialog=next(a for a in button.ancestors() if type(a).__name__=='Dialog')
        dialog.delete()
        before=records(root)
        with patch.object(service,'confirm_asset_replacement') as confirm:
            # Deliberately dispatch a stale callback, bypassing normal lookup.
            await h.driver.click_element(button)
        self.assertEqual(0,confirm.call_count)
        self.assertEqual(before,records(root))


    async def test_close_during_scope_read_never_renders_confirmation(self):
        for how in ('close','owner_close','disconnect'):
            with self.subTest(how=how):
                root,service,h,target,extra=await self.fixture(checked=True)
                entered,release=threading.Event(),threading.Event()
                original=AssetActionService.prepare_replacement
                def slow(action,*a,**kw):
                    entered.set()
                    if not release.wait(10):raise RuntimeError('scope barrier timeout')
                    return original(action,*a,**kw)
                ids=set(h.client.elements);before=records(root)
                with patch.object(AssetActionService,'prepare_replacement',autospec=True,side_effect=slow) as prepare,patch.object(service,'confirm_asset_replacement') as confirm:
                    waiting=asyncio.create_task(h.driver.click('换源'))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait,3))
                        if how=='close':
                            dialogs=[e for k,e in h.client.elements.items() if k not in ids and type(e).__name__=='Dialog']
                            self.assertEqual(1,len(dialogs));dialogs[0].close()
                        elif how=='owner_close':h.view.close()
                        else:
                            for handler in h.client.disconnect_handlers:handler()
                    finally:
                        release.set();await asyncio.wait_for(waiting,15)
                self.assertEqual(1,prepare.call_count);self.assertEqual(0,confirm.call_count)
                self.assertFalse(any(getattr(e,'text','')=='生成计划' for k,e in h.client.elements.items() if k not in ids))
                self.assertEqual(before,records(root))
                self.assertEqual(b'previous video',target.read_bytes())
                self.assertEqual(b'previous subtitle',extra.read_bytes())
