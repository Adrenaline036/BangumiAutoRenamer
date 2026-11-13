"""Current content reviews use the canonical movie form, not legacy inline editors."""
from dataclasses import replace
import unittest

from tests.test_content_choice_contract import records
from tests import test_content_workbench_gui as gui_fixture


class MovieReviewControls(unittest.IsolatedAsyncioTestCase):
    async def prepared(self):
        helper = gui_fixture.ContentWorkbenchGui('runTest')
        helper.create_page(cleanup_owner=self)
        self.assertIsNotNone(helper.view.plan.metadata.get('content_selection'))
        return helper

    async def test_subtitle_issue_does_not_spawn_an_inline_primary_picker(self):
        helper = await self.prepared()
        view = helper.view
        before = records(helper.root)
        view.plan = replace(view.plan, metadata={**view.plan.metadata,
                            'mapping_source': 'inventory_smart_completion'})
        view.snapshot = replace(view.snapshot,
            issues=({'code': 'subtitle_video_not_content_version'},))
        with helper.client:
            # Both first display and a refresh must remain free of the old picker.
            for _ in range(2):
                view._render_completion_workflow()
                elements = tuple(view.completion_container.descendants())
                self.assertFalse(any(type(e).__name__ == 'Select' for e in elements))
                self.assertIn('保存位置', [getattr(e, 'text', '') for e in elements])
        self.assertEqual(before, records(helper.root))

    async def test_role_errors_keep_the_report_without_inline_actions(self):
        helper = await self.prepared()
        view = helper.view
        before = records(helper.root)
        original = view.snapshot
        row = next(row for row in original.rows if row['media_kind'] == 'video')
        for code in ('presentation_asset_role_unknown', 'episode_assignment_conflict'):
            with self.subTest(code=code), helper.client:
                view.snapshot = replace(original,
                    issues=({'code': code, 'source': row['source'], 'message': '待确认用途'},),
                    rows=tuple({**r, 'issue_codes': (code,)} if r['entry_id'] == row['entry_id']
                               else {**r, 'issue_codes': ()} for r in original.rows))
                view._render_issues()
                panels = [e for e in view.issues_container.descendants()
                          if 'bar-issue-summary' in e.classes]
                self.assertEqual(1, len(panels))
                elements = tuple(panels[0].descendants())
                text = ' '.join(str(getattr(e, 'text', '')) for e in elements)
                self.assertIn(code, text)
                self.assertIn('文件：', text)
                self.assertFalse(any(type(e).__name__ in {'Button', 'Select', 'Input'}
                                     for e in elements))
        self.assertEqual(before, records(helper.root))

    async def test_canonical_movie_editor_still_opens_once_and_cancel_is_read_only(self):
        helper = await self.prepared()
        view = helper.view
        before = records(helper.root)
        await helper.driver.click('电影正片版本编辑')
        selectors = [e for e in helper.client.elements.values()
                     if type(e).__name__ == 'Select' and e.label == '电影正片文件（可多选）']
        self.assertEqual(1, len(selectors))
        self.assertTrue(selectors[0].options)
        parent = selectors[0].parent_slot.parent
        while type(parent).__name__ != 'Dialog':
            parent = parent.parent_slot.parent
        self.assertTrue(parent.value)
        close = next(e for e in parent.descendants()
                     if type(e).__name__ == 'Button' and getattr(e, 'text', '') == '关闭')
        await helper.driver.click_element(close)
        self.assertFalse(parent.value)
        self.assertEqual(before, records(helper.root))


if __name__ == '__main__':
    unittest.main()
