import asyncio
from collections import Counter
import threading
import unittest
from unittest.mock import patch
from tests import test_planned_replacement_edit as existing

class OpeningFeedback(unittest.IsolatedAsyncioTestCase):
    fixture = existing.PlannedReplacementEdit.fixture
    describe = existing.PlannedReplacementEdit.describe
    revise = existing.PlannedReplacementEdit.revise
    observe = existing.PlannedReplacementEdit.observe
    confirm = existing.PlannedReplacementEdit.confirm
    create_page = existing.PlannedReplacementEdit.create_page
    replacement = existing.PlannedReplacementEdit.replacement

    async def test_prepare_reuses_same_asset_across_snapshot_state_and_recovery(self):
        from src.pages import plan_review_page as page
        from src.rename.asset_operation import AssetOperation
        root, service, plan, _ = await self.replacement()
        self.create_page(prepared=(root, service, plan, []))
        counts=Counter(); original=AssetOperation.__post_init__
        def count(value):
            counts[value.payload]+=1
            return original(value)
        with patch.object(AssetOperation,'__post_init__',count):
            page.prepare_plan_review_open(plan)
        self.assertTrue(counts)
        self.assertEqual({1},set(counts.values()),'one opening must validate each identical asset only once')

    async def test_loading_dialog_is_visible_before_background_read_finishes(self):
        from src.pages import plan_review_page as page
        root, service, plan, _ = await self.replacement()
        self.create_page(prepared=(root, service, plan, []))
        self.view.delete()
        snapshot = await asyncio.to_thread(page.prepare_plan_review_open, plan)
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        def blocked(_plan):
            entered.set()
            try:
                if not release.wait(5):raise TimeoutError('test worker was not released')
                return snapshot
            finally:finished.set()
        async def start():
            with self.client:await page.plan_review_page(plan.task_id)
        with patch.object(page,'prepare_plan_review_open',blocked):
            opening=asyncio.create_task(start())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,3))
                dialogs=[el for el in self.client.elements.values() if isinstance(el,page.PlanReviewDialog)]
                self.assertEqual(1,len(dialogs),'click must open the existing dialog before preparation completes')
                self.assertTrue(dialogs[0].value)
                self.assertFalse(release.is_set())
                texts=[getattr(el,'text','') for el in self.client.elements.values()]
                self.assertIn('正在读取整理计划',texts)
                self.assertNotIn('批准整理计划',texts)
                original_dialog = dialogs[0]
                with self.client:
                    await page.plan_review_page(plan.task_id)
                self.assertEqual([original_dialog], [el for el in self.client.elements.values()
                                                     if isinstance(el, page.PlanReviewDialog)])
                release.set()
                async with asyncio.timeout(5):
                    while not hasattr(original_dialog, 'snapshot'):
                        if opening.done():
                            await opening  # Unexpected preparation/render errors must surface.
                            self.fail('opening exited without rendering the plan')
                        await asyncio.sleep(.01)
                dialogs = [el for el in self.client.elements.values() if isinstance(el, page.PlanReviewDialog)]
                self.assertEqual([original_dialog], dialogs)
                self.assertTrue(original_dialog.value)
                texts = [getattr(el, 'text', '') for el in self.client.elements.values()]
                self.assertNotIn('正在读取整理计划', texts)
                self.assertIn('批准整理计划', texts)
                self.assertEqual(snapshot.rows, original_dialog.snapshot.rows)
                original_dialog._close_review()
                await asyncio.wait_for(opening, 3)
                self.assertTrue(original_dialog.is_deleted)
            finally:
                opening.cancel();release.set()
                await asyncio.gather(opening,return_exceptions=True)
                self.assertTrue(await asyncio.to_thread(finished.wait,3))

    async def aborted_open(self, mode):
        from src.pages import plan_review_page as page
        root, service, plan, _ = await self.replacement()
        self.create_page(prepared=(root, service, plan, []))
        self.view.delete()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        handlers_before = list(self.client.disconnect_handlers)
        if mode == 'other_document':
            self.client._socket_to_document_id['original-socket'] = 'document'

        def blocked(_plan):
            entered.set()
            try:
                if not release.wait(5):
                    raise TimeoutError('worker barrier not released')
                if mode == 'error':
                    raise OSError('fixture read failed')
                return object()
            finally:
                finished.set()

        async def start():
            with self.client:
                await page.plan_review_page(plan.task_id)

        with patch.object(page, 'prepare_plan_review_open', blocked), \
                patch.object(page.PlanReviewDialog, '_load_plan_view') as hydrate:
            opening = asyncio.create_task(start())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                dialog, = [el for el in self.client.elements.values() if isinstance(el, page.PlanReviewDialog)]
                if mode == 'close':
                    dialog._close_review()
                elif mode == 'close_and_finish':
                    release.set()
                    dialog._close_review()
                elif mode == 'disconnect':
                    for handler in self.client.disconnect_handlers:
                        if handler not in handlers_before:
                            handler()
                elif mode == 'other_document':
                    self.client._socket_to_document_id.clear()
                    self.client._socket_to_document_id['new-socket'] = 'different-document'
                    release.set()
                else:
                    release.set()
                await asyncio.wait_for(opening, 3)
                self.assertTrue(dialog.is_deleted)
                self.assertFalse(self.client._bar_review_openings)
                hydrate.assert_not_called()
                self.assertEqual(handlers_before, self.client.disconnect_handlers)
                if mode in {'close', 'disconnect'}:
                    self.assertFalse(finished.is_set(), 'closing must not wait for the background read')
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 3))
                await asyncio.sleep(0)
                hydrate.assert_not_called()  # A late result cannot reopen a deleted window.
                if mode == 'error':
                    self.assertTrue(any('读取整理计划失败' in text for text in self.notices))
            finally:
                release.set()
                opening.cancel()
                await asyncio.gather(opening, return_exceptions=True)
                self.assertTrue(await asyncio.to_thread(finished.wait, 3))

    async def test_close_returns_without_waiting_for_read(self):
        await self.aborted_open('close')

    async def test_close_wins_when_read_finishes_together(self):
        await self.aborted_open('close_and_finish')

    async def test_disconnect_discards_late_read(self):
        await self.aborted_open('disconnect')

    async def test_other_document_discards_read(self):
        await self.aborted_open('other_document')

    async def test_read_error_removes_loader_and_releases_open_marker(self):
        await self.aborted_open('error')
