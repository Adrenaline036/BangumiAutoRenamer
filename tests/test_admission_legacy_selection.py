"""旧首次链接在公共GUI调用链中拒绝，业务与队列保持原状。"""
import asyncio
import json
import unittest
from unittest.mock import patch

from nicegui.client import Client
from src.pages import plan_review_page as page, provider_selection_work
from src.rename import default_activation, source_basis
from src.rename.content_policy import _scope_binding
from src.rename.provider_search import resolve_provider_item_url
from tests.test_content_policy_contract import historical_admission
from tests import test_default_activation_contract as activation
from tests.test_initial_auto_link_contract import detail
from tests.test_provider_search_v5 import _Session
from tests.source_collection_fixture import admit_task
from tests.test_admission_inventory import workspace_files, assert_released_locks


class LegacySelection(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow

    async def admit_history(self, manager, source, task_id):
        with historical_admission():
            await admit_task(manager, source, task_id=task_id, is_anime=None, is_movie=None)

    async def test_legacy_first_link_rejects_without_queue_or_writes(self):
        async with self.flow(movie=True, admission_only=True, admit=self.admit_history) as flow:
            manager = flow.manager
            self.assertIsNone(_scope_binding(flow.service, flow.task_id).policy)
            self.assertFalse(flow.store.path_for(flow.task_id).exists())
            def queue_state():
                return (set(manager._pending), set(manager._admissions), manager.active_task_id,
                        manager._queue.qsize(), sorted(p.name for p in manager.task_root.glob('*.json')))
            before_queue = queue_state()
            before, locks_before = workspace_files(flow.root)
            session = _Session(detail(movie=True))
            def resolve(url):
                return resolve_provider_item_url(url, tmdb_api_key='fixture-only',
                    bangumi_user_agent='BAR/test', session=session)
            for attempt in range(2):
                with Client.auto_index_client.content:
                    dialog = page.PlanReviewDialog(None, manager._read(flow.task_id))
                try:
                    self.assertIsNone(dialog.plan)
                    self.assertFalse(dialog.approve_all_button.enabled)
                    self.assertFalse(dialog.execute_button.enabled)
                    safe_end = next(e for e in dialog.descendants() if e._props.get('label') == '安全结束任务')
                    self.assertTrue(safe_end.enabled)
                    dialog.provider_url_input.value = 'https://www.themoviedb.org/movie/8181'
                    with patch.object(page, '_read_manual_provider_url', side_effect=resolve), \
                            patch.object(page, 'notify') as notify, \
                            patch.object(manager, 'enqueue_reidentify', wraps=manager.enqueue_reidentify) as enqueue, \
                            patch.object(default_activation, 'inventory_movie_assets', wraps=default_activation.inventory_movie_assets) as scan, \
                            patch.object(source_basis, 'observe_admitted_basis', wraps=source_basis.observe_admitted_basis) as admitted, \
                            patch.object(source_basis, 'observe_initial_input', wraps=source_basis.observe_initial_input) as observe, \
                            patch.object(default_activation, 'ensure_workspace', wraps=default_activation.ensure_workspace) as entry:
                        work = await provider_selection_work.submit(dialog)
                        await asyncio.wait_for(work.worker, 10)
                        await provider_selection_work.watch(dialog, work)
                        entry.assert_called_once()
                        enqueue.assert_not_called()
                    self.assertIsNone(work.result)
                    self.assertFalse(work.admitted)
                    self.assertEqual(before_queue, queue_state())
                    self.assertEqual(before, workspace_files(flow.root)[0])
                    with self.subTest(attempt=attempt, check='readable reason'):
                        self.assertIn('来源记录不完整，无法建立整理计划', work.error)
                        self.assertIn('安全结束', work.status)
                        self.assertNotIn('冻结', work.status)
                        self.assertNotIn('投影', work.status)
                        self.assertEqual(work.status, dialog.initial_progress.text)
                    with self.subTest(attempt=attempt, check='no observation'):
                        self.assertEqual((0,0,0), (scan.call_count, admitted.call_count, observe.call_count))
                finally:
                    dialog.delete()
            print('GUI_REJECTION_FILES', json.dumps(dict(before=before, after=workspace_files(flow.root)[0],
                locks_before=locks_before, locks_after=workspace_files(flow.root)[1]), ensure_ascii=False))
            await assert_released_locks(self, flow.root, manager.task_root, flow.task_id)
            self.assertEqual(before, workspace_files(flow.root)[0])
