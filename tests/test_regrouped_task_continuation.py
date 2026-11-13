"""Regrouped cards use the ordinary UI; only owned synthetic sources/state."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch
import unittest

from nicegui import Client
from nicegui.page import page as nice_page
from src.pages import plan_review_page as page, data_table_page as dashboard
from src.pages.recognition_recovery_view import RecognitionRecoveryDialog
from src.rename.plan_store import PlanStore
from tests import test_source_regrouping_view_contract as views
from tests import test_partition_content_contract as fixtures
from tests.test_source_regrouping_preview_contract import five_seasons


class RegroupedContinuationContracts(unittest.IsolatedAsyncioTestCase):
    workspace = views.SourceRegroupingViewContracts.workspace
    flow = fixtures.PartitionContentContracts.flow
    current = fixtures.PartitionContentContracts.current
    video = fixtures.PartitionContentContracts.video
    link_subtitle = fixtures.PartitionContentContracts.link_subtitle
    click = views.SourceRegroupingViewContracts.click
    open_preview = views.SourceRegroupingViewContracts.open_preview

    async def press(self, container, label):
        with container.client:
            return await self.click(container, label)

    async def test_results_open_selected_new_card_via_ordinary_workspace(self):
        async with self.workspace() as (flow, work):
            popup = self.open_preview(work)
            await self.click(popup, '确认创建新卡')
            selected = popup.created_ids[1]
            with patch.object(page, 'plan_review_page', new_callable=AsyncMock) as open_page:
                await self.press(popup.result_dialog, '打开第2张新卡')
                open_page.assert_awaited_once_with(selected)
            self.assertFalse(popup.result_dialog.value)
            await asyncio.wait_for(flow.manager._queue.join(), 30)
            self.assertFalse(flow.manager._pending)

    async def test_new_card_real_page_confirms_sources_and_produces_unapproved_plan(self):
        def configure(worker):
            self.worker = worker
        async with self.flow(automatic=False, configure_source=five_seasons, configure_worker=configure) as flow:
            plan, _ = self.current(flow)
            preview = flow.manager.preview_source_regrouping(task_id=flow.task_id,
                expected_plan_fingerprint=PlanStore.fingerprint(plan))
            ids = flow.manager.create_regrouped_tasks(preview)
            selected = ids[0]
            self.worker.search.get_tv_info = lambda *_: self.worker.search.get_tv_info_by_id('4242')
            client = Client(nice_page('/owned-regrouped-continuation'), request=None)
            pending = None
            try:
                async def open_page():
                    with client:
                        await page.plan_review_page(selected)
                pending = asyncio.create_task(open_page())
                for _ in range(10):
                    await asyncio.sleep(0)
                    dialogs = [e for e in client.elements.values() if isinstance(e, RecognitionRecoveryDialog)]
                    if dialogs:
                        break
                self.assertEqual(1, len(dialogs))
                dialog = dialogs[0]
                await self.press(dialog, '继续识别来源')
                await asyncio.wait_for(flow.manager._queue.join(), 12)
                await asyncio.wait_for(pending, 3)
                self.assertTrue(flow.store.path_for(selected).exists())
                self.assertEqual('need_confirm', flow.manager._read(selected)['workflow_status'])
                review_path = flow.manager.review_root / f'{selected}.json'
                if review_path.exists():
                    review = json.loads(review_path.read_text('utf-8'))
                    self.assertEqual('draft', review['status'])
                    self.assertFalse(review['approved_entry_ids'])
                self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))
                self.assertFalse(flow.manager._read(ids[1]).get('plan_fingerprint'))
            finally:
                if pending:
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                client.delete()

    async def test_failed_continue_keeps_dialog_and_safe_end_deletes_only_selected_card(self):
        async with self.workspace() as (flow, work):
            popup = self.open_preview(work)
            with patch.object(flow.manager, 'confirm_source_group', side_effect=ValueError('owned admission failure')):
                await self.click(popup, '确认创建新卡')
            selected, peer = popup.created_ids[:2]
            peer_before = flow.manager._read(peer)
            source_before = {p: p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
            with work.client:
                dialog = RecognitionRecoveryDialog(selected, flow.manager._read(selected), ValueError('no plan yet'))
                dialog.open()
            before = fixtures.records(flow.root)
            with patch.object(flow.manager, 'confirm_source_group', side_effect=ValueError('owned temporary failure')):
                await self.press(dialog, '继续识别来源')
            self.assertTrue(dialog.value)
            self.assertEqual(before, fixtures.records(flow.root))
            with patch.multiple(dashboard, task_queue=flow.manager, get_task=flow.manager._read,
                    RECORD_PATH=flow.manager.execution_service.record_root, PLAN_PATH=flow.store.root,
                    PLAN_REVIEW_PATH=flow.manager.review_root, notify=Mock(), refresh_dashboard=Mock()), \
                    patch.object(dashboard._review_tasks, 'refresh'), patch.object(dashboard._history_tasks, 'refresh'):
                await self.press(dialog, '安全结束任务')
                confirmations = [e for e in work.client.elements.values()
                    if getattr(e, 'text', '') == '我确认结束任务，不删除媒体与历史证据']
                self.assertEqual(1, len(confirmations))
                self.assertTrue(flow.manager._read(selected))
                confirmations[0].value = True
                with patch.object(flow.manager, 'abandon_and_remove', side_effect=ValueError('owned safe end refused')):
                    await self.press(work.client.content, '确认安全结束')
                self.assertTrue(dialog.value)
                self.assertTrue(flow.manager._read(selected))
                await self.press(work.client.content, '确认安全结束')
            self.assertFalse(flow.manager._read(selected))
            self.assertFalse(dialog.value)
            self.assertEqual(peer_before, flow.manager._read(peer))
            self.assertEqual(source_before, {p: p.read_bytes() for p in source_before})
