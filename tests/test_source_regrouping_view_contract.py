"""Actual review controls; all business/source fixtures are disposable."""
from contextlib import asynccontextmanager
import unittest
from unittest.mock import patch

from nicegui import Client
from nicegui.page import page as nice_page
from src.pages import plan_review_page as page
from tests.test_source_regrouping_preview_contract import SourceRegroupingPreviewContracts, five_seasons
from tests.test_partition_content_contract import records


def paginated_seasons(root, source):
    mixed = five_seasons(root, source)
    first = next((mixed / 'Season 1').glob('*.mkv'))
    for episode in range(2, 13):
        video = first.with_name(first.name.replace('E01', f'E{episode:02d}'))
        video.write_bytes(first.read_bytes())
        video.with_suffix('.en.srt').write_bytes(first.with_suffix('.en.srt').read_bytes())
    return mixed


class SourceRegroupingViewContracts(unittest.IsolatedAsyncioTestCase):
    flow = SourceRegroupingPreviewContracts.flow
    current = SourceRegroupingPreviewContracts.current
    video = SourceRegroupingPreviewContracts.video
    link_subtitle = SourceRegroupingPreviewContracts.link_subtitle

    @asynccontextmanager
    async def workspace(self, *, configure_worker=None, configure_source=five_seasons):
        async with self.flow(automatic=False, configure_source=configure_source, configure_worker=configure_worker) as flow:
            plan, _ = self.current(flow)
            plan = self.link_subtitle(flow, plan)
            client = Client(nice_page('/isolated-regrouping-view'), request=None)
            with client:
                work = page.PlanReviewDialog(plan, flow.manager._read(flow.task_id))
                work.open()
            try:
                yield flow, work
            finally:
                client.delete()

    def click(self, container, label):
        buttons = [e for e in container.descendants() if e._props.get('label') == label]
        self.assertEqual(1, len(buttons), f'缺少唯一可见入口：{label}')
        callbacks = []
        with patch('nicegui.elements.button.handle_event', side_effect=lambda cb, _: callbacks.append(cb)):
            next(e for e in buttons[0]._event_listeners.values() if e.type == 'click').handler(None)
        with container.client:
            return callbacks[0]()

    async def open_preview(self, work):
        with work.client:
            await self.click(work, '调整计划范围')
        return work.regrouping_dialog

    async def test_whole_group_selection_across_pages_counts_and_isolation(self):
        async with self.workspace(configure_source=paginated_seasons) as (flow, work):
            popup = await self.open_preview(work)
            before = records(flow.root)
            table, other = popup.tables[:2]
            card = table.parent_slot.parent
            self.assertGreater(len(table.rows), 10)
            self.assertEqual(10, table.pagination['rowsPerPage'])
            self.assertIn('header-selection', table.slots, '不能保留含义不清的本页全选表头')
            self.assertEqual('', table.slots['header-selection'].template or '')
            self.assertEqual([table.group_checkbox], table.slots['header-selection'].children)
            table.group_checkbox.value = True
            self.assertEqual(table.rows, table.selected)
            self.assertFalse(other.selected)
            table.pagination = {**table.pagination, 'page': 2}
            removed = table.rows[-1]
            from nicegui.events import GenericEventArguments
            event = next(e for e in table._event_listeners.values() if e.type == 'selection')
            event.handler(GenericEventArguments(sender=table, client=table.client,
                args={'added': False, 'keys': [removed['source']], 'rows': [removed]}))
            self.assertEqual(len(table.rows) - 1, len(table.selected))
            texts = [getattr(e, 'text', '') for e in card.descendants()]
            self.assertIn(f'已选 {len(table.rows) - 1} / 共 {len(table.rows)} 项', texts)
            other.group_checkbox.value = True
            table.group_checkbox.value = False
            self.assertFalse(table.selected)
            self.assertEqual(other.rows, other.selected)
            self.assertIn(f'已选 0 / 共 {len(table.rows)} 项',
                          [getattr(e, 'text', '') for e in card.descendants()])
            self.assertEqual(before, records(flow.root))

    async def test_full_group_move_submits_all_pages_then_cancel_preserves_task(self):
        async with self.workspace(configure_source=paginated_seasons) as (flow, work):
            popup = await self.open_preview(work)
            before = records(flow.root)
            table = popup.tables[0]
            expected = set(popup.groups[0]['sources']) | set(popup.groups[1]['sources'])
            all_sources = {p for g in popup.groups for p in g['sources']}
            excluded = {p for g in popup.groups for p in g['excluded_sources']}
            table.group_checkbox.value = True
            popup.destinations[0].value = 1
            await self.click(popup.tables[0].parent_slot.parent, '移动所选文件及配套')
            self.assertFalse(popup.error.text)
            self.assertIn(expected, [set(g['sources']) for g in popup.groups])
            self.assertEqual(all_sources, {p for g in popup.groups for p in g['sources']})
            self.assertEqual(excluded, {p for g in popup.groups for p in g['excluded_sources']})
            self.assertTrue(all(not t.selected for t in popup.tables))
            await popup.check()
            self.assertIn('核对通过', popup.summary.text)
            self.click(popup, '关闭')
            self.assertEqual(before, records(flow.root))

    async def test_group_controls_do_not_change_busy_closed_or_independent_preview(self):
        async with self.workspace() as (flow, work):
            popup = await self.open_preview(work)
            table = popup.tables[0]
            card = table.parent_slot.parent
            before = records(flow.root)
            popup.busy = True
            table.group_checkbox.value = True
            self.assertFalse(table.selected)
            table.group_checkbox.value = False
            popup.busy = False
            table.group_checkbox.value = True
            popup.close()
            table.group_checkbox.value = False
            self.assertEqual(table.rows, table.selected)
            from src.rename.plan_review import entry_id
            with work.client:
                await work._partition_exceptional_as_cross_work([entry_id(self.video(work.plan))])
            independent = work.regrouping_dialog
            self.assertIsNotNone(independent.selected_entry_ids)
            self.assertTrue(all(t.selection is None for t in independent.tables))
            self.assertFalse(any(e._props.get('label') in ('全选本组', '清空本组')
                                 for e in independent.descendants()))
            self.assertEqual(before, records(flow.root))

    async def test_explicit_confirmation_creates_cards_and_closes_old_workspace(self):
        async with self.workspace() as (flow, work):
            popup = await self.open_preview(work)
            popup.groups = [popup.groups[-1], *popup.groups[:-1]]
            popup.preview = popup._query([g['sources'] for g in popup.groups])
            popup._render()
            before = records(flow.root)
            self.assertFalse(any(getattr(e, 'text', '') == '我已核对分组，确认用新卡替换旧卡' for e in popup.descendants()))
            await self.click(popup, '确认创建新卡')
            self.assertFalse(flow.manager._read(flow.task_id))
            self.assertTrue(work.exit_requested)
            self.assertFalse(work.value)
            self.assertEqual(5, len(popup.created_ids))
            self.assertTrue(popup.result_dialog.value)
            labels = [getattr(e, 'text', '') for e in popup.result_dialog.descendants()]
            self.assertFalse(any(t.startswith('不整理的杂项 ·') for t in labels))
            for task_id, group in zip(popup.created_ids, popup.preview.task_groups):
                self.assertTrue(any(t.startswith(group['label'] + ' ·') for t in labels))
                self.assertEqual(set(group['sources']), {row['source_path'] for row in
                    flow.manager._read(task_id)['task_source_scope']['source_identities']})
            import asyncio
            await asyncio.wait_for(flow.manager._queue.join(), 30)
            self.assertEqual(5, sum(not flow.manager._read(key).get('source_group_pending')
                                   for key in popup.created_ids))
            self.assertFalse(flow.manager._pending)
            self.assertTrue(flow.manager._queue.empty())

    async def test_missing_creation_result_is_not_reported_as_success(self):
        async with self.workspace() as (flow, work):
            popup = await self.open_preview(work)
            before = records(flow.root)
            with patch.object(flow.manager, 'create_regrouped_tasks', return_value=None):
                await self.click(popup, '确认创建新卡')
            self.assertEqual(before, records(flow.root))
            self.assertTrue(popup.error.text)
            self.assertTrue(work.value)
            self.assertFalse(popup.created_ids)
            self.assertIsNone(popup.result_dialog)

    async def test_actual_entry_move_video_with_companions_and_reset_without_writes(self):
        async with self.workspace() as (flow, work):
            before = records(flow.root)
            with patch.object(flow.manager, 'partition_task_sources', side_effect=AssertionError('old writer')):
                popup = await self.open_preview(work)
                self.assertEqual(6, len(popup.groups))
                combined = set(popup.groups[0]['sources']) | set(popup.groups[1]['sources'])
                all_paths = {p for group in popup.groups for p in group['sources']}
                table = popup.tables[0]
                video = next(row for row in table.rows if row['kind'] == '视频')
                table.selected = [video]
                popup.destinations[0].value = 1
                await self.click(popup.tables[0].parent_slot.parent, '移动所选文件及配套')
                self.assertEqual(5, len(popup.groups))
                target = next(g for g in popup.groups if video['source'] in g['sources'])
                self.assertEqual(2, target['video_count'])
                self.assertEqual(combined, set(target['sources']))
                self.assertEqual(all_paths, {p for g in popup.groups for p in g['sources']})
                popup.tables[0].selected = [next(row for row in popup.tables[0].rows if row['source'] == video['source'])]
                popup.destinations[0].value = 'new'
                await self.click(popup.tables[0].parent_slot.parent, '移动所选文件及配套')
                self.assertEqual(6, len(popup.groups))
                await popup.check()
                await self.click(popup, '恢复默认分组')
                self.assertEqual(6, len(popup.groups))
                self.click(popup, '关闭')
                self.assertFalse(popup.value)
            self.assertEqual(before, records(flow.root))

    async def test_parent_close_disables_child_and_fresh_open_discards_unsaved_changes(self):
        async with self.workspace() as (flow, work):
            popup = await self.open_preview(work)
            before = records(flow.root)
            with work.client:
                work.close()
            await popup.check()
            self.assertFalse(popup.value)
            self.assertTrue(popup.closed)
            with work.client:
                work.open()
            reopened = await self.open_preview(work)
            self.assertIsNot(popup, reopened)
            self.assertTrue(popup.is_deleted)
            self.assertEqual(6, len(reopened.groups))
            self.assertEqual(before, records(flow.root))

    async def test_bad_companion_only_move_keeps_valid_preview_and_stale_task_rejected(self):
        async with self.workspace() as (flow, work):
            popup = await self.open_preview(work)
            original = popup.preview
            before = records(flow.root)
            table = popup.tables[0]
            table.selected = [next(row for row in table.rows if row['kind'] == '字幕')]
            popup.destinations[0].value = 1
            await self.click(popup.tables[0].parent_slot.parent, '移动所选文件及配套')
            self.assertEqual(original, popup.preview)
            self.assertTrue(popup.error.text)
            with patch.object(flow.manager, 'preview_source_regrouping', side_effect=ValueError('任务已变化')):
                await popup.check()
                self.assertIn('任务已变化', popup.error.text)
            self.assertEqual(before, records(flow.root))

    async def test_independent_callbacks_without_selection_do_not_open_whole_package(self):
        async with self.workspace() as (flow, work):
            before = records(flow.root)
            with patch.object(flow.manager, 'partition_task_sources', side_effect=AssertionError('old writer')):
                for callback in (lambda: work._partition_selected_as_cross_work(),
                                 lambda: work._partition_exceptional_as_cross_work(())):
                    with work.client:
                        await callback()
                    self.assertIsNone(getattr(work, 'regrouping_dialog', None))
            self.assertEqual(before, records(flow.root))

    async def test_disconnected_producer_and_closed_window_cannot_offer_success(self):
        async with self.workspace() as (flow, work):
            before = records(flow.root)
            with patch.object(flow.manager, 'preview_source_regrouping', return_value=None):
                await self.click(work, '调整计划范围')
                self.assertIsNone(getattr(work, 'regrouping_dialog', None))
            popup = await self.open_preview(work)
            old = popup.preview
            popup.close()
            with popup.client:
                await popup.move_selected(0)
            self.assertEqual(old, popup.preview)
            self.assertEqual(before, records(flow.root))
