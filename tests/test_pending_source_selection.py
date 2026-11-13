"""待确认来源组必须在任何作品读取或后台任务之前被阻断。"""
from types import SimpleNamespace
import asyncio
import unittest
from unittest.mock import Mock, patch

from src.pages import plan_review_page as page
from src.pages.provider_selection_work import submit


class PendingSourceSelectionContracts(unittest.IsolatedAsyncioTestCase):
    async def test_current_pending_group_rejects_stale_view_and_selected_candidate(self):
        for view_pending in (False, True):
            for selected in (None, {'namespace': 'tmdb', 'entity_type': 'movie', 'provider_id': '100'}):
                with self.subTest(view_pending=view_pending, selected=selected):
                    manager = SimpleNamespace(
                        _read=Mock(return_value={'uuid': 'pending', 'source_group_pending': True}),
                        _admission_epoch=1, _admission_lock=asyncio.Lock(), _provider_reads={}, _stopping=False, active_task_id=None, _pending=set(),
                        plan_store=Mock(), decision_state_store=Mock())
                    manager.plan_store.path_for.return_value.exists.return_value = False
                    manager.decision_state_store.path_for.return_value.exists.return_value = False
                    view = SimpleNamespace(task_id='pending', task_data={
                        'uuid': 'pending', 'source_group_pending': view_pending})
                    with patch.object(page, 'task_queue', manager), self.assertRaisesRegex(ValueError, '来源'):
                        await submit(view, selected)
                    manager.plan_store.path_for.assert_not_called()
                    manager.decision_state_store.path_for.assert_not_called()
                    self.assertEqual({}, manager._provider_reads)

    async def test_pending_view_requires_refresh_after_current_group_was_confirmed(self):
        manager = SimpleNamespace(_read=Mock(return_value={'uuid': 'pending', 'source_group_pending': False}),
            _admission_epoch=1, _admission_lock=asyncio.Lock(), _provider_reads={}, _stopping=False, active_task_id=None, _pending=set(),
            plan_store=Mock(), decision_state_store=Mock())
        view = SimpleNamespace(task_id='pending', task_data={'uuid': 'pending', 'source_group_pending': True})
        with patch.object(page, 'task_queue', manager), self.assertRaisesRegex(ValueError, '来源'):
            await submit(view)
        manager.plan_store.path_for.assert_not_called()
        manager.decision_state_store.path_for.assert_not_called()
        self.assertEqual({}, manager._provider_reads)
