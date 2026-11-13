"""同一窗口重连，以及子窗口关闭后的父窗口状态同步。"""
import asyncio
import threading
import unittest
from unittest.mock import patch

from src.pages import plan_review_page as page
from src.rename.plan_store import PlanStore
from tests import test_content_workbench_gui as fixture
from tests.test_content_choice_contract import change


class ReviewRefreshLifecycle(unittest.IsolatedAsyncioTestCase):
    create_page = fixture.ContentWorkbenchGui.create_page
    source_box = fixture.ContentWorkbenchGui.source_box

    async def opening(self, mode):
        self.create_page()
        self.view.delete()
        snapshot = await asyncio.to_thread(page.prepare_plan_review_open, self.plan)
        entered, release = threading.Event(), threading.Event()
        self.client._socket_to_document_id['old-socket'] = 'same-document'
        with self.client:
            dialog = page.PlanReviewDialog(None, page.get_task(self.plan.task_id))
            dialog.open()
        def read(_plan):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('定向验证未释放读取屏障')
            return snapshot
        async def prepare():
            with self.client:
                return await page._prepare_visible_plan_dialog(dialog, self.plan, self.client,
                    is_current=lambda: dialog.plan is None)
        with patch.object(page, 'prepare_plan_review_open', read):
            task = asyncio.create_task(prepare())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                self.client._socket_to_document_id.clear()
                if mode == 'closed':
                    dialog._close_review()
                elif not mode.startswith('gap'):
                    self.client._socket_to_document_id['new-socket'] = (
                        'other-document' if mode == 'different' else 'same-document')
                release.set()
                if mode.startswith('gap'):
                    await asyncio.sleep(.05)
                    self.assertFalse(task.done(), '等待原页面重连时不能提前消费读取结果')
                    self.assertIsNone(dialog.plan)
                    self.assertFalse(any(getattr(e, 'text', '') == '当前作品信息'
                                         for e in self.client.elements.values()))
                    self.client._socket_to_document_id['new-socket'] = (
                        'other-document' if mode == 'gap_different' else 'same-document')
                    for handler in list(self.client.connect_handlers):
                        handler()
                adopted = await asyncio.wait_for(task, 3)
                if mode in ('same', 'gap'):
                    self.assertTrue(adopted, '同一页面的连接编号变化不能丢弃已保存结果')
                    self.assertTrue(dialog.value)
                    self.assertIsNotNone(dialog.plan)
                    self.assertEqual(PlanStore.fingerprint(self.plan), PlanStore.fingerprint(dialog.plan))
                    self.assertTrue(any(getattr(e, 'text', '') == '当前作品信息'
                                        for e in self.client.elements.values()))
                else:
                    self.assertFalse(adopted)
                    self.assertIsNone(dialog.plan)
                    self.assertFalse(any(getattr(e, 'text', '') == '当前作品信息'
                                         for e in self.client.elements.values()))
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if not dialog.is_deleted:
                    dialog.delete()

    async def test_same_document_reconnect_updates_initial_window(self):
        await self.opening('same')

    async def test_result_waits_for_same_document_reconnect(self):
        await self.opening('gap')

    async def test_different_document_after_wait_is_rejected(self):
        await self.opening('gap_different')

    async def test_other_document_and_explicit_close_do_not_adopt(self):
        for mode in ('different', 'closed'):
            with self.subTest(mode=mode):
                await self.opening(mode)

    async def content(self, mode):
        self.create_page()
        before = PlanStore.fingerprint(self.view.plan)
        def rendered_files():
            return ' '.join(str(getattr(element, 'text', ''))
                            for element in self.view.entries_container.descendants())
        self.assertIn(self.videos[1].name, rendered_files())
        await self.driver.click('内容保留与排除')
        workbench = self.view._content_workbench
        self.source_box(self.videos[1]).value = True
        entered, release = threading.Event(), threading.Event()
        original = self.service.commit_content_change
        def commit(*args, **kwargs):
            result = original(*args, **kwargs)
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('定向验证未释放提交屏障')
            return result
        with patch.object(self.service, 'commit_content_change', side_effect=commit):
            task = asyncio.create_task(self.driver.click('排除整理'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                self.assertTrue(workbench.close_button.enabled)
                await self.driver.click_element(workbench.close_button)
                if mode == 'parent_closed':
                    self.view._close_review()
                elif mode == 'newer':
                    current = self.service.plan_store.load(self.plan.task_id)
                    later = change(self.service, current, [self.videos[0].with_suffix('.sc.ass')])
                    self.view.task_decision = self.service.decision_state_store.load(self.plan.task_id)
                    with self.client:
                        await self.view._adopt_revision(later)
                release.set()
                await asyncio.wait_for(task, 3)
                await asyncio.sleep(0)
                current = self.service.plan_store.load(self.plan.task_id)
                self.assertNotEqual(before, PlanStore.fingerprint(current))
                self.assertTrue(workbench.closed)
                if mode == 'parent_closed':
                    self.assertEqual(before, PlanStore.fingerprint(self.view.plan))
                else:
                    self.assertEqual(str(len(current.entries)), self.view.header_entry_count_value.text)
                    self.assertIn(f'共 {len(current.entries)} 个文件', rendered_files())
                    self.assertNotIn(self.videos[1].name, rendered_files())
                    self.assertEqual(PlanStore.fingerprint(current), PlanStore.fingerprint(self.view.plan),
                                     '关闭子窗口后，仍打开的整理窗口必须显示已保存结果')
                    self.assertTrue(self.view.value)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_closed_content_child_updates_open_parent(self):
        await self.content('child_closed')

    async def test_closed_parent_and_newer_revision_are_preserved(self):
        for mode in ('parent_closed', 'newer'):
            with self.subTest(mode=mode):
                await self.content(mode)
