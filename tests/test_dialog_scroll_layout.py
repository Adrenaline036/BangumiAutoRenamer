"""Fixed controls must live outside the scroll body in actual dialog trees."""
import asyncio
import unittest
from unittest.mock import patch
from nicegui import Client
from nicegui.page import page
from tests import test_content_workbench_gui as workbench


def ancestors(element):
    while element.parent_slot is not None:
        element = element.parent_slot.parent
        yield element


class DialogScrollLayout(unittest.IsolatedAsyncioTestCase):
    def create_page(self):
        # 仅本布局夹具等待后台选择落盘；共享助手仍保持原行为。
        page_module = workbench.review_page
        select = page_module.PlanReviewDialog._select_provider_candidate

        async def wait_selected(view, candidate, **kwargs):
            await select(view, candidate, **kwargs)
            manager = page_module.task_queue
            work = manager._provider_reads.get(view.plan.task_id)
            self.assertIsNotNone(work, '作品选择请求未接纳')
            await asyncio.wait_for(asyncio.shield(work.worker), 20)
            self.assertFalse(work.error, work.error)
            self.assertIsNotNone(work.result, '后台选择未产生结果')
            await asyncio.wait_for(manager._queue.join(), 5)
            state = manager.decision_state_store.load(view.plan.task_id)
            self.assertTrue(state.confirmed_work, '作品选择完成后必须已有持久确认记录')

        with patch.object(page_module.PlanReviewDialog, '_select_provider_candidate', wait_selected):
            return workbench.ContentWorkbenchGui.create_page(self)

    def fixed_controls(self, card, button):
        self.assertTrue(any('bar-modal-footer' in e.classes for e in ancestors(button)),
                        'main action must belong to the fixed footer')
        self.assertFalse(any('bar-modal-body' in e.classes for e in ancestors(button)),
                         'main action must not scroll with the content')
        children = card.default_slot.children
        self.assertIn('bar-modal-header', children[0].classes)
        self.assertIn('bar-modal-body', children[1].classes)
        self.assertIn('bar-modal-footer', children[-1].classes)

    async def test_plan_actions_stay_outside_content_after_load_and_refresh(self):
        self.create_page()
        for refreshed in (False, True):
            if refreshed:
                with self.client:
                    self.view._load_plan_view(self.view.plan)
            self.fixed_controls(self.view.body, self.view.approve_all_button)
            self.fixed_controls(self.view.body, self.view.execute_button)
            body = self.view.body.default_slot.children[1]
            self.assertIn(self.view.entries_container, tuple(body.descendants()))
            # 两种操作在首次加载与刷新后均只有一个入口。
            for button in (self.view.approve_all_button, self.view.execute_button):
                matches = [e for e in self.view.body.descendants()
                           if e._props.get('label') == button._props['label']]
                self.assertEqual([button], matches)

    async def test_config_heading_and_save_are_outside_content(self):
        from src.pages.config_page import ConfigPage
        client = Client(page('/isolated-scroll-layout'), request=None)
        self.addCleanup(client.delete)
        with client:
            dialog = ConfigPage()
        card = dialog.default_slot.children[0]
        save = next(e for e in card.descendants() if e._props.get('label') in {'保存', '验证界面（不保存）'})
        self.fixed_controls(card, save)
        titles = [e for e in card.default_slot.children[0].descendants() if getattr(e, 'text', '') == '配置']
        self.assertEqual(1, len(titles))
