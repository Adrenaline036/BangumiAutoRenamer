"""Wrong-type recovery through real UI safe-end and fresh queue admission."""
import asyncio
from contextlib import asynccontextmanager
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from nicegui import Client, ui
from nicegui.page import page as nice_page
from src.pages import plan_review_page as page, data_table_page as dashboard
from src.rename.plan_store import PlanStore
from tests import test_partition_content_contract as fixtures
from tests import test_source_regrouping_view_contract as views
from tests import test_initial_auto_link_contract as links
from tests.source_collection_fixture import admit_task


class WrongWorkRestartContracts(unittest.IsolatedAsyncioTestCase):
    flow = fixtures.PartitionContentContracts.flow
    current = fixtures.PartitionContentContracts.current
    click = views.SourceRegroupingViewContracts.click
    invoke = links.InitialAutoLinkContracts.invoke

    @asynccontextmanager
    async def workspace(self, **kwargs):
        async with self.flow(**kwargs) as flow:
            plan, state = self.current(flow)
            self.assertTrue(state.confirmed_work)
            client = Client(nice_page('/owned-wrong-work'), request=None)
            try:
                with client:
                    work = page.PlanReviewDialog(plan, flow.manager._read(flow.task_id))
                    work.open()
                with patch.multiple(dashboard, task_queue=flow.manager, get_task=flow.manager._read,
                        RECORD_PATH=flow.manager.execution_service.record_root, PLAN_PATH=flow.store.root,
                        PLAN_REVIEW_PATH=flow.manager.review_root, notify=Mock(), refresh_dashboard=Mock()), \
                        patch.object(dashboard._review_tasks, 'refresh'), patch.object(dashboard._history_tasks, 'refresh'):
                    yield flow, work
            finally:
                client.delete()

    async def press(self, container, label):
        with container.client:
            return await self.click(container, label)

    async def test_wrong_type_candidate_refuses_without_writes_and_has_visible_rebuild_exit(self):
        async with self.workspace() as (flow, work):
            candidate = dict(namespace='tmdb', entity_type='movie', provider_id='8181',
                             title='普通电影', library_domain='general')
            before = fixtures.records(flow.root)
            with work.client:
                work._show_provider_candidates()
                work.provider_url_input.value = 'https://www.themoviedb.org/movie/8181'
            # Rendering first can create the ordinary empty Review draft.
            before = fixtures.records(flow.root)
            with patch.object(page, '_read_manual_provider_url', new_callable=AsyncMock, return_value=candidate), \
                    patch.object(flow.manager, 'enqueue_reidentify', new_callable=AsyncMock) as enqueue:
                await self.press(work, '读取链接并选择此作品')
                enqueue.assert_not_awaited()
            self.assertEqual(before, fixtures.records(flow.root))
            labels = ' '.join(str(getattr(e, 'text', '')) for e in work.descendants())
            self.assertIn('安全结束', labels)
            self.assertIn('重新添加', labels)
            await self.press(work, '安全结束任务')
            self.assertTrue(flow.manager._read(flow.task_id))
            self.assertFalse(work.exit_requested)

    async def test_unchecked_cancel_and_refused_safe_end_preserve_workspace(self):
        async with self.workspace() as (flow, work):
            before = fixtures.records(flow.root)
            await self.press(work, '安全结束任务')
            await self.press(work.client.content, '确认安全结束')
            self.assertEqual(before, fixtures.records(flow.root))
            self.click(work.client.content, '取消')
            self.assertTrue(work.value)
            # Remove closed confirmation elements to exercise a fresh opening.
            for e in list(work.client.elements.values()):
                if isinstance(e, ui.dialog) and e is not work and not e.value:
                    e.delete()
            await self.press(work, '安全结束任务')
            checkbox = next(e for e in work.client.elements.values()
                if getattr(e, 'text', '') == '我确认结束任务，不删除媒体与历史证据')
            checkbox.value = True
            with patch.object(flow.manager, 'abandon_and_remove', side_effect=ValueError('owned refusal')):
                await self.press(work.client.content, '确认安全结束')
            self.assertEqual(before, fixtures.records(flow.root))
            self.assertTrue(work.value)
            self.assertFalse(work.exit_requested)

    async def test_safe_end_then_same_source_fresh_opposite_type_is_unapproved(self):
        for original_movie in (False, True):
            with self.subTest(original_movie=original_movie):
                async with self.workspace(movie=original_movie) as (flow, work):
                    source_before = {p: p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
                    peer = flow.root / 'tasks' / 'unrelated.json'
                    peer.write_text('{"uuid":"unrelated","workflow_status":"need_confirm"}', encoding='utf-8')
                    peer_before = peer.read_bytes()
                    await self.press(work, '安全结束任务')
                    checkbox = next(e for e in work.client.elements.values()
                        if getattr(e, 'text', '') == '我确认结束任务，不删除媒体与历史证据')
                    checkbox.value = True
                    await self.press(work.client.content, '确认安全结束')
                    self.assertFalse(flow.manager._read(flow.task_id))
                    self.assertTrue(work.exit_requested)
                    self.assertFalse(work.value)
                    new_id = 'corrected-work'
                    await admit_task(flow.manager, flow.source, task_id=new_id, is_anime=False,
                        is_movie=not original_movie,
                        custom_name='Ordinary Show' if original_movie else 'Ordinary Movie')
                    await asyncio.wait_for(flow.manager._queue.join(), 12)
                    state = flow.service.decision_state_store.load(new_id)
                    self.assertTrue(state.confirmed_work, flow.manager._read(new_id).get('error'))
                    self.assertEqual('tv' if original_movie else 'movie',
                        state.confirmed_work['primary_provider']['entity_type'])
                    plan = flow.store.load(new_id)
                    self.assertNotEqual(work.plan.task_id, plan.task_id)
                    for file in flow.manager.review_root.glob('*.json'):
                        self.assertFalse(json.loads(file.read_text('utf-8'))['approved_entry_ids'])
                    self.assertEqual(peer_before, peer.read_bytes())
                    self.assertEqual(source_before, {p: p.read_bytes() for p in source_before})
                    self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))
                    self.assertTrue(all(not e.target_path.exists() for e in plan.entries))

    async def test_same_source_auto_no_match_then_actual_first_movie_or_bangumi_link(self):
        for bangumi in (False, True):
            with self.subTest(bangumi=bangumi):
                workers = []
                async with self.workspace(configure_worker=workers.append) as (flow, work):
                    before = {p: p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
                    await self.press(work, '安全结束任务')
                    next(e for e in work.client.elements.values() if
                        getattr(e, 'text', '') == '我确认结束任务，不删除媒体与历史证据').value = True
                    await self.press(work.client.content, '确认安全结束')
                    worker = workers[0]
                    old_tv, old_movie = worker.search.get_tv_info, worker.search.get_movie_info
                    worker.search.get_tv_info = lambda *_: ('', None)
                    worker.search.get_movie_info = lambda *_: ('', None)
                    new_id = 'fresh-auto-link'
                    await admit_task(flow.manager, flow.source, task_id=new_id, is_anime=None,
                        is_movie=None)
                    await asyncio.wait_for(flow.manager._queue.join(), 12)
                    self.assertFalse(flow.store.path_for(new_id).exists(), 'real search-no-hit must leave no Plan')
                    self.assertFalse(flow.service.decision_state_store.path_for(new_id).exists())
                    # End the genuine no-hit phase; preserve provider-specific
                    # by-ID lookup (Bangumi may need its own associated TV).
                    worker.search.get_tv_info = old_tv
                    worker.search.get_movie_info = old_movie
                    fresh = SimpleNamespace(**{**vars(flow), 'task_id': new_id, 'movie': not bangumi})
                    if bangumi:
                        from tests.test_r1_real_media_production_red_contract import YAMA_SUBJECTS
                        payload = YAMA_SUBJECTS['41658']
                    else:
                        payload = links.detail(movie=True)
                    session, messages = await self.invoke(fresh, payload, bangumi=bangumi)
                    state = flow.service.decision_state_store.load(new_id)
                    self.assertTrue(state.confirmed_work, (messages, flow.manager._read(new_id).get('error')))
                    self.assertEqual('41658' if bangumi else '8181',
                        state.confirmed_work['primary_provider']['provider_id'])
                    self.assertEqual('animation' if bangumi else 'general', state.library_domain)
                    self.assertEqual(1, len(session.calls))
                    self.assertFalse(flow.manager._read(flow.task_id))
                    self.assertEqual(before, {p: p.read_bytes() for p in before})
                    self.assertTrue(all(not e.target_path.exists() for e in flow.store.load(new_id).entries))
                    self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))
                    for file in flow.manager.review_root.glob('*.json'):
                        self.assertFalse(json.loads(file.read_text('utf-8'))['approved_entry_ids'])
