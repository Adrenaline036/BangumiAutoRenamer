"""读取中的实际审核窗口沿用正常窗口的固定区域。"""
from tests.test_dialog_scroll_layout import DialogScrollLayout, ancestors


class LoadingDialogLayout(DialogScrollLayout):
    async def test_loading_content_belongs_to_fixed_regions(self):
        from src.pages import plan_review_page as page
        self.create_page()
        self.fixed_controls(self.view.body, self.view.approve_all_button)
        with self.client:
            loading = page.PlanReviewDialog(None, dict(self.view.task_data), loading=True)
        self.addCleanup(loading.delete)
        elements = list(loading.body.descendants())
        close = next(e for e in elements if e._props.get('label') == '关闭')
        title = next(e for e in elements if getattr(e, 'text', '') == '整理任务')
        message = next(e for e in elements if getattr(e, 'text', '') == '正在读取整理计划')
        progress = next(e for e in elements if e._props.get('indeterminate'))
        self.fixed_controls(loading.body, close)
        self.assertEqual(3, len(loading.body.default_slot.children))
        header, body, footer = loading.body.default_slot.children
        self.assertIn(header, tuple(ancestors(title)))
        self.assertIn(body, tuple(ancestors(message)))
        self.assertIn(body, tuple(ancestors(progress)))
        self.assertIn(footer, tuple(ancestors(close)))
        self.assertNotIn('bar-modal-footer', close.classes)
