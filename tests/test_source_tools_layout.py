from types import SimpleNamespace
import unittest
from nicegui import Client, ui
from nicegui.page import page
from src.pages.plan_file_list import PlanFileListMixin


def owner(empty=True):
    return SimpleNamespace(plan=SimpleNamespace(metadata={'mapping_source':'inventory_review_draft'},work_identity=None,entries=() if empty else (object(),)),
        task_decision=SimpleNamespace(confirmed_work={},layout_mode=None,movie_version_selection=None,tv_planning_authority=None),
        _open_source_regrouping=lambda:None)

class SourceToolsLayout(unittest.TestCase):
    def render(self,empty=True):
        client=Client(page('/isolated-tools'),request=None)
        with client:
            container=ui.column().classes('w-full')
            from src.pages.plan_review_page import PlanReviewDialog
            view=owner(empty)
            view.snapshot=SimpleNamespace(issues=[],work_identity={})
            with container:
                view.issues_container=ui.column().classes('w-full')
                PlanReviewDialog._render_issues(view)
                PlanFileListMixin._render_plan_tools(view)
        self.addCleanup(client.delete)
        return container

    def test_empty_notice_is_full_width_error_box_outside_buttons(self):
        container=self.render()
        labels=[e for e in container.descendants() if getattr(e,'text','').startswith('当前没有纳入的文件')]
        self.assertEqual(1,len(labels));notice=labels[0]
        self.assertIn('bar-error-title',notice._classes)
        card=notice.parent_slot.parent.parent_slot.parent
        self.assertIn('bar-issue-summary',card._classes)
        self.assertIn('w-full',card._classes)
        self.assertIs(container,card.parent_slot.parent.parent_slot.parent)
        tools=next(e for e in container.default_slot.children if 'bar-plan-tools' in e._classes)
        self.assertLess(list(container.descendants()).index(card),list(container.descendants()).index(tools))

    def test_two_buttons_are_equal_flex_in_one_nonwrapping_row(self):
        container=self.render()
        buttons=[e for e in container.descendants() if e._props.get('label') in ('调整计划范围','内容保留与排除')]
        self.assertEqual(2,len(buttons))
        self.assertIs(buttons[0].parent_slot,buttons[1].parent_slot)
        self.assertIn('no-wrap',buttons[0].parent_slot.parent._classes)
        for button in buttons:
            self.assertEqual('1 1 0',button._style.get('flex'))
            self.assertEqual('0',button._style.get('min-width'))
            self.assertTrue(button._props.get('icon'))

    def test_nonempty_source_has_no_empty_notice(self):
        container=self.render(False)
        self.assertFalse([e for e in container.descendants() if getattr(e,'text','').startswith('当前没有纳入的文件')])
