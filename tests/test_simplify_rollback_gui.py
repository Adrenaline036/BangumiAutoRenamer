"""Production GUI callbacks; UI rendering is a test double, not browser proof."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from src.pages import asset_recovery_view as page


class Node:
    def __init__(self, *args, **kwargs):
        self.opened = False
        self.value = False
        self.text = ''
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def classes(self, *_): return self
    def props(self, *_, **kwargs): return self
    def style(self, *_, **kwargs): return self
    def open(self): self.opened = True
    def close(self): self.opened = False
    def disable(self): pass
    def enable(self): pass
    def set_visibility(self, *_): pass
    def set_text(self, text): self.text = text


class RollbackGuiContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(page, "dialog_card", Node))
        self.enterContext(patch("src.pages.data_table_page.dialog_card", Node))

    def widgets(self):
        self.buttons = {}
        self.dialog = Node()
        self.check = Node()
        def button(label, **kwargs):
            self.buttons[label] = kwargs['on_click']
            return Node()
        return SimpleNamespace(dialog=lambda: self.dialog, card=Node, row=Node,
            label=lambda _: Node(), linear_progress=Node, button=button, checkbox=lambda _: self.check)

    async def test_preview_requires_explicit_click_and_deduplicates_pending_command(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def restart(*_):
            entered.set()
            await release.wait()
        queue = SimpleNamespace(prepare_rolled_back_restart=AsyncMock(return_value=object()),
            restart_rolled_back=AsyncMock(side_effect=restart), enqueue_approved=AsyncMock())
        refresh = Mock()
        with patch.object(page, 'ui', self.widgets()), patch.object(page, 'notify'):
            await page.open_rolled_back_restart(queue, 'task', refresh)
            queue.restart_rolled_back.assert_not_called()
            first = asyncio.create_task(self.buttons['生成新整理计划']())
            await entered.wait()
            await self.buttons['生成新整理计划']()
            self.assertEqual(1, queue.restart_rolled_back.call_count)
            release.set()
            await first
        queue.enqueue_approved.assert_not_called()
        self.assertFalse(self.dialog.opened)
        refresh.assert_called_once()

    async def test_safe_end_needs_checkbox_and_preserves_dialog_on_failure(self):
        from src.pages import data_table_page as dashboard
        end = AsyncMock(return_value=False)
        with patch.object(dashboard, 'ui', self.widgets()), patch.object(dashboard, '_submit_safe_end', end, create=True):
            await dashboard._confirm_safe_end('task')
            await self.buttons['确认安全结束']()
            end.assert_not_called()
            self.check.value = True
            await self.buttons['确认安全结束']()
            self.assertTrue(self.dialog.opened)
            end.return_value = True
            await self.buttons['确认安全结束']()
            self.assertFalse(self.dialog.opened)

    async def test_legacy_delete_callbacks_cannot_submit_without_confirmation(self):
        from src.pages import data_table_page as dashboard
        task = dict(asset_rollback_phase='rolled_back', execution_status='rolled_back')
        commit = AsyncMock(return_value=True)
        with patch.object(dashboard, 'get_task', return_value=task), patch.object(
                dashboard, 'ui', self.widgets()), patch.object(
                dashboard, '_submit_safe_end', commit, create=True):
            await dashboard._delete_task('task')
            commit.assert_not_called()
            await dashboard.handle_delete(SimpleNamespace(args={'row': {'uuid': 'task'}}))
            commit.assert_not_called()
            self.check.value = True
            await self.buttons['确认安全结束']()
            commit.assert_awaited_once_with('task')

    async def test_old_card_callbacks_redirect_to_explicit_restart_preview(self):
        from src.pages import data_table_page as dashboard
        task = dict(asset_rollback_phase='rolled_back', execution_status='rolled_back')
        preview = AsyncMock()
        with patch.object(dashboard, 'get_task', return_value=task), patch.object(
                dashboard, '_restart_after_rollback', preview), patch.object(dashboard, 'plan_review_page', AsyncMock()) as old:
            await dashboard._open_review('task')
            await dashboard._retry_task('task')
        self.assertEqual(2, preview.call_count)
        old.assert_not_called()


if __name__ == '__main__': unittest.main()
