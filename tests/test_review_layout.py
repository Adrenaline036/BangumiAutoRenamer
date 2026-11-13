"""审核页四区域、提示归属和季度输入在真实 NiceGUI 组件中保持稳定。"""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from nicegui import Client, ui
from nicegui.page import page as Page
from src.pages import plan_review_page as page
from src.pages.season_choice import season_choice
from tests import test_content_workbench_gui as workbench
from tests.test_content_choice_contract import records
from tests.test_selection_failure_placement import failed_task
from tests.test_series_single_side import SingleSideFlow, MEMBERS


class BeforeSelectionFlow(SingleSideFlow):
    async def select(self, task_id, selected, *, confirm=True):
        return self.result(task_id)


def within(element, parent):
    while element is not None:
        if element is parent:
            return True
        slot = getattr(element, 'parent_slot', None)
        element = slot.parent if slot is not None else None
    return False


class ReviewLayout(unittest.IsolatedAsyncioTestCase):
    def regions(self, view):
        self.assertTrue(hasattr(view, 'summary_region'), '缺少固定任务摘要区域')
        areas = [view.summary_region, view.selection_region, view.plan_region, view.actions_region]
        self.assertEqual(len(set(a.id for a in areas)), 4)
        self.assertTrue(all(within(a, view.body) for a in areas))
        self.assertLess(areas[0].id, areas[1].id)
        self.assertLess(areas[1].id, areas[2].id)
        self.assertLess(areas[2].id, areas[3].id)
        for member in ('header_title_value', 'header_season_value', 'header_recognition_value',
                       'header_entry_count_value', 'plan_badge', 'review_badge'):
            self.assertTrue(within(getattr(view, member), view.summary_region), member)
        source_labels = [e for e in view.client.elements.values()
                         if within(e, view.body) and str(getattr(e, 'text', '')).startswith('源目录：')]
        self.assertEqual(1, len(source_labels))
        self.assertTrue(within(source_labels[0], view.summary_region))

    async def test_existing_plan_regions_and_refresh_preserve_saved_records(self):
        holder = workbench.ContentWorkbenchGui('runTest')
        before = holder.create_page(cleanup_owner=self)
        view = holder.view
        self.regions(view)
        self.assertTrue(within(view.groups_container, view.selection_region))
        self.assertTrue(within(view.entries_container, view.plan_region))
        self.assertTrue(within(view.execute_button, view.actions_region))
        self.assertTrue(within(view.approve_all_button, view.actions_region))
        anchors = [view.summary_region.id, view.selection_region.id, view.plan_region.id, view.actions_region.id]
        view._show_provider_candidates()
        self.assertEqual(anchors, [view.summary_region.id, view.selection_region.id, view.plan_region.id, view.actions_region.id])
        self.assertTrue(within(view.provider_url_input, view.selection_region))
        self.assertEqual(before, records(holder.root))

    async def test_unselected_and_failed_task_share_regions_without_writing(self):
        holder = workbench.ContentWorkbenchGui('runTest')
        holder.create_page(cleanup_owner=self)
        before = records(holder.root)
        for task in [dict(uuid='empty', path='synthetic-source'), failed_task()]:
            with self.subTest(state=task.get('review_operation', 'unselected')):
                with holder.client:
                    view = page.PlanReviewDialog(None, task)
                self.regions(view)
                self.assertTrue(within(view.initial_progress, view.selection_region))
                self.assertTrue(within(view.provider_url_input, view.selection_region))
                self.assertFalse(view.approve_all_button.enabled)
                self.assertFalse(view.execute_button.enabled)
                view.delete()
        self.assertEqual(before, records(holder.root))

    async def test_failed_selection_remains_in_confirmation_region(self):
        holder = workbench.ContentWorkbenchGui('runTest')
        before = holder.create_page(cleanup_owner=self)
        view = holder.view
        view.task_data = {**view.task_data, **failed_task()}
        view._render_groups()
        self.regions(view)
        errors = [e for e in holder.client.elements.values() if getattr(e, 'text', '') == '这次选择未生效']
        self.assertEqual(1, len(errors))
        self.assertTrue(within(errors[0], view.selection_region))
        self.assertEqual(before, records(holder.root))

    async def test_unresolved_season_controls_belong_to_confirmation_region(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        async with BeforeSelectionFlow(root, singleton=True) as runtime:
            result = await runtime.add(MEMBERS[-1], confirm=False)
            service = runtime.review
        self.assertFalse(result.state.confirmed_work)
        holder = workbench.ContentWorkbenchGui('runTest')
        before = holder.create_page(prepared=(root, service, result.plan, []), cleanup_owner=self)
        view = holder.view
        view.task_data = {**view.task_data, **failed_task(), 'uuid': result.task_id}
        with holder.client:
            view._render_groups()
        self.assertTrue(hasattr(view, 'unresolved_season_input'), '事故夹具必须实际生成可编辑季号')
        self.assertTrue(hasattr(view, 'selection_region'), '缺少作品与季度确认区域')
        self.assertTrue(within(view.unresolved_season_input, view.selection_region))
        self.assertTrue(view.unresolved_season_input.enabled)
        self.assertEqual(before, records(root))

    async def test_season_mode_switch_keeps_controls_in_same_slot(self):
        client = Client(Page('/season-layout'), request=None)
        self.addCleanup(client.delete)
        with client:
            outer = ui.row()
            with outer:
                choice = season_choice(ui, [0, 1, 2], current=1)
        parent = choice.picker.parent_slot.parent
        self.assertIsNot(parent, outer, '季度控件不得直接散落在调用者横向容器')
        self.assertIs(parent, choice.custom.parent_slot.parent)
        self.assertTrue(choice.picker.visible and choice.custom.visible)
        self.assertTrue(choice.picker.enabled)
        self.assertFalse(choice.custom.enabled)
        choice.manual.set_value(True)
        choice.custom.set_value(7)
        self.assertTrue(choice.picker.visible and choice.custom.visible)
        self.assertFalse(choice.picker.enabled)
        self.assertTrue(choice.custom.enabled)
        self.assertEqual(7, choice())
