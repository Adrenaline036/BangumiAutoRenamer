"""First failed auto lookup: real recovery callback and queue, frozen HTTP only."""
import asyncio
import json
import unittest
from unittest.mock import patch

from nicegui.client import Client

from src.pages import plan_review_page as page
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.provider_search import resolve_provider_item_url
from tests import test_default_activation_contract as activation
from tests.test_content_choice_contract import records
from tests.test_provider_search_v5 import _Session
from tests.source_collection_fixture import admit_task


def detail(movie=False, genres=None):
    return dict(id=8181 if movie else 4242,
        **({'title': '普通电影', 'original_title': 'Ordinary Movie', 'release_date': '2024-01-01'}
           if movie else {'name': '普通剧集', 'original_name': 'Ordinary Show', 'first_air_date': '2020-01-01'}),
        genres=[{'id': 18, 'name': 'Drama'}] if genres is None else genres)


class InitialAutoLinkContracts(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow

    async def admit_auto(self, manager, source, task_id):
        await admit_task(manager, source, task_id=task_id, is_anime=None, is_movie=None)

    async def test_actual_first_auto_page_does_not_try_typed_workspace_before_link(self):
        async with self.flow(admission_only=True, admit=self.admit_auto) as flow:
            class ClosedReview:
                exit_requested = True
                is_deleted = False
                def __await__(self):
                    async def closed():
                        return None
                    return closed().__await__()

                def delete(self):
                    pass

            with Client.auto_index_client.content, \
                    patch.object(page, 'PlanReviewDialog', return_value=ClosedReview()) as recovery, \
                    patch('src.pages.initial_workspace.create_initial_workspace',
                          side_effect=AssertionError('cannot build typed workspace before reading link')) as initial:
                await page.plan_review_page(flow.task_id)
            recovery.assert_called_once()
            initial.assert_not_called()

    async def test_save_with_pasted_link_uses_the_same_selection_action(self):
        from unittest.mock import AsyncMock
        async with self.flow(admission_only=True, admit=self.admit_auto) as flow:
            with Client.auto_index_client.content:
                dialog = PlanReviewDialog(None, flow.manager._read(flow.task_id))
            try:
                dialog.provider_url_input.value = 'https://www.themoviedb.org/movie/8181'
                with patch.object(dialog, '_select_manual_provider_url', new_callable=AsyncMock) as select, \
                        patch.object(page, '_reenqueue_from_workspace', new_callable=AsyncMock) as ordinary:
                    await dialog._restart_recognition()
                    select.assert_awaited_once()
                    ordinary.assert_not_awaited()
            finally:
                dialog.delete()

    async def test_first_link_finishes_with_a_readable_review_instead_of_closing_to_empty_card(self):
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as flow:
            with patch.object(PlanReviewDialog, '_load_plan_view', autospec=True) as submit:
                await self.invoke(flow, detail(movie=True))
            self.assertEqual(flow.task_id, submit.call_args.args[1].task_id)
            state = flow.service.decision_state_store.load(flow.task_id)
            plan = flow.store.load(flow.task_id)
            self.assertEqual('8181', state.confirmed_work['primary_provider']['provider_id'])
            self.assertEqual(flow.service.decision_state_store.fingerprint(state), plan.metadata['decision_state_fingerprint'])

    async def test_later_request_cannot_be_presented_as_this_link_result(self):
        async with self.flow(movie=True, admission_only=True, admit=self.admit_auto) as flow:
            original = flow.manager.wait_for_selection_result
            async def replaced(task_id, **kwargs):
                result = await original(task_id)
                return {**result, 'request_id': 'later-request'}
            with patch.object(flow.manager, 'wait_for_selection_result', side_effect=replaced), \
                    patch.object(PlanReviewDialog, '_load_plan_view', autospec=True) as submit:
                _session, messages = await self.invoke(flow, detail(movie=True))
            submit.assert_not_called()
            self.assertTrue(any('选择已更新' in text for text in messages), messages)

    async def test_pending_wait_timeout_keeps_waiting_for_the_same_selection(self):
        from src.rename.review_operation import current_provider_selection_review_operation
        async with self.flow(movie=True, admission_only=True, admit=self.admit_auto) as flow:
            original = flow.manager.wait_for_selection_result
            calls = []
            async def slow(task_id, **kwargs):
                calls.append(task_id)
                if len(calls) == 1:
                    _, context = current_provider_selection_review_operation(flow.manager._read(task_id))
                    return {**context, 'lifecycle': 'pending'}
                return await original(task_id)
            with patch.object(flow.manager, 'wait_for_selection_result', side_effect=slow), \
                    patch.object(PlanReviewDialog, '_load_plan_view', autospec=True) as submit:
                await self.invoke(flow, detail(movie=True))
            self.assertEqual(2, len(calls), 'a pending wait is not a completed plan')
            self.assertEqual(flow.task_id, submit.call_args.args[1].task_id)

    async def invoke(self, flow, payload, *, changed_path=False, bangumi=False):
        # The fixture's admission-only executor models an initial lookup which
        # has not produced a workspace. The user's selection uses the real worker.
        flow.manager.executor = flow.manager._execute_rename
        movie = flow.movie
        session = _Session(payload)
        def resolve(value):
            return resolve_provider_item_url(value, tmdb_api_key='fixture-only',
                bangumi_user_agent='BAR/test', session=session)
        with Client.auto_index_client.content:
            dialog = PlanReviewDialog(None, flow.manager._read(flow.task_id))
        try:
            dialog.provider_url_input.value = ('https://bgm.tv/subject/41658' if bangumi else
                f'https://www.themoviedb.org/{"movie/8181" if movie else "tv/4242"}')
            self.assertEqual('auto', dialog.rerecognition_media_type.value)
            if changed_path:
                dialog.rerecognition_path.value = str(flow.root / 'different-source')
            button = next(element for element in dialog.descendants()
                          if element._props.get('label') == '读取链接并选择此作品')
            callbacks = []
            with patch('nicegui.elements.button.handle_event', side_effect=lambda callback, _: callbacks.append(callback)):
                next(listener for listener in button._event_listeners.values()
                     if listener.type == 'click').handler(None)
            self.assertEqual([dialog._select_manual_provider_url], callbacks)
            with patch.object(page, '_read_manual_provider_url', side_effect=resolve), \
                    patch.object(page, 'notify') as notification:
                await callbacks[0]()
                await asyncio.wait_for(flow.manager._queue.join(), 12)
                messages = [str(call.args[0]) for call in notification.call_args_list]
            return session, messages
        finally:
            dialog.delete()

    async def test_auto_first_link_reaches_actual_tv_and_movie_work_without_type_picker(self):
        for movie, anime in ((False, False), (True, False), (False, True)):
            with self.subTest(movie=movie, anime=anime):
                async with self.flow(movie=movie, automatic=False, admission_only=True, admit=self.admit_auto) as flow:
                    source_before = {str(p): p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
                    peer = flow.root / 'tasks' / 'unrelated.json'
                    peer.write_text('{"uuid":"unrelated","workflow_status":"need_confirm"}', encoding='utf-8')
                    peer_before = peer.read_bytes()
                    session, messages = await self.invoke(flow, detail(movie, genres=[{'id': 16, 'name': 'Animation'}] if anime else None))
                    self.assertEqual(1, len(session.calls), messages)
                    self.assertTrue(flow.store.path_for(flow.task_id).exists(), messages)
                    state = flow.service.decision_state_store.load(flow.task_id)
                    self.assertTrue(state.confirmed_work, flow.manager._read(flow.task_id).get('error'))
                    self.assertEqual('animation' if anime else 'general', state.library_domain)
                    self.assertEqual(movie, flow.manager._read(flow.task_id)['is_movie'])
                    self.assertIsNotNone(state.content_selection)
                    for review_file in flow.manager.review_root.glob('*.json'):
                        review = json.loads(review_file.read_text(encoding='utf-8'))
                        self.assertEqual('draft', review['status'])
                        self.assertFalse(review['approved_entry_ids'])
                    self.assertEqual(peer_before, peer.read_bytes())
                    self.assertEqual(source_before, {str(p): p.read_bytes() for p in flow.source.rglob('*') if p.is_file()})
                    self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))
                    self.assertFalse(list((flow.root / 'records').glob('*.json')))
                    self.assertTrue(all(not entry.target_path.exists() for entry in flow.store.load(flow.task_id).entries))

    async def test_unknown_or_unsupported_link_reads_evidence_but_leaves_workspace_untouched(self):
        for genres in ([], [{'id': 18, 'name': 'Animation'}]):
            with self.subTest(genres=genres):
                async with self.flow(automatic=False, admission_only=True, admit=self.admit_auto) as flow:
                    before = records(flow.root)
                    session, messages = await self.invoke(flow, detail(genres=genres))
                    self.assertEqual(1, len(session.calls), messages)
                    self.assertEqual(before, records(flow.root))
                    self.assertFalse(flow.store.path_for(flow.task_id).exists())

    async def test_changed_source_cannot_create_workspace_from_a_valid_link(self):
        async with self.flow(automatic=False, admission_only=True, admit=self.admit_auto) as flow:
            before = records(flow.root)
            session, _ = await self.invoke(flow, detail(), changed_path=True)
            self.assertEqual(0, len(session.calls))
            self.assertEqual(before, records(flow.root))

    async def test_bangumi_tv_link_uses_actual_worker_and_subject_authority(self):
        from tests.test_r1_real_media_production_red_contract import YAMA_SUBJECTS
        async with self.flow(admission_only=True, admit=self.admit_auto) as flow:
            source_before = {str(p): p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
            session, messages = await self.invoke(flow, YAMA_SUBJECTS['41658'], bangumi=True)
            self.assertEqual(1, len(session.calls), messages)
            state = flow.service.decision_state_store.load(flow.task_id)
            self.assertTrue(state.confirmed_work, flow.manager._read(flow.task_id).get('error'))
            self.assertEqual('animation', state.library_domain)
            self.assertEqual('41658', state.confirmed_work['primary_provider']['provider_id'])
            self.assertIsNotNone(state.content_selection)
            self.assertEqual(source_before, {str(p): p.read_bytes() for p in flow.source.rglob('*') if p.is_file()})
            self.assertNotEqual('completed', flow.manager._read(flow.task_id).get('execution_status'))

    async def test_bangumi_movie_first_link_reaches_both_movie_domains(self):
        for anime in (True, False):
            with self.subTest(anime=anime):
                payload=dict(id=41658,type=2 if anime else 6,
                    platform='剧场版' if anime else '电影',name='Movie',date='2024-01-01')
                def configure(worker):
                    worker.bangumi_search._get_json=lambda _: payload
                async with self.flow(movie=True,admission_only=True,admit=self.admit_auto,
                                     configure_worker=configure) as flow:
                    _,messages=await self.invoke(flow,payload,bangumi=True)
                    state=flow.service.decision_state_store.load(flow.task_id)
                    self.assertEqual('bangumi',state.confirmed_work['primary_provider']['namespace'],messages)
                    self.assertEqual('animation' if anime else 'general',state.library_domain)
                    self.assertTrue(flow.manager._read(flow.task_id)['is_movie'])
                    self.assertNotEqual('completed',flow.manager._read(flow.task_id).get('execution_status'))

    async def test_bangumi_missing_type_does_not_create_workspace(self):
        for payload in ({'id': 41658, 'platform': 'TV', 'name': 'Unknown'},):
            with self.subTest(payload=payload):
                async with self.flow(admission_only=True, admit=self.admit_auto) as flow:
                    before = records(flow.root)
                    session, messages = await self.invoke(flow, payload, bangumi=True)
                    self.assertEqual(1, len(session.calls))
                    self.assertTrue(any('参考' in text or '无法确定' in text for text in messages), messages)
                    self.assertEqual(before, records(flow.root))

    async def test_damaged_or_partial_history_cannot_be_overwritten_as_first_link(self):
        for artifact in ('plan', 'decision', 'both'):
            with self.subTest(artifact=artifact):
                async with self.flow(admission_only=True, admit=self.admit_auto) as flow:
                    paths = []
                    if artifact in {'plan', 'both'}:
                        paths.append(flow.store.path_for(flow.task_id))
                    if artifact in {'decision', 'both'}:
                        paths.append(flow.service.decision_state_store.path_for(flow.task_id))
                    for path in paths:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text('{damaged-history', encoding='utf-8')
                    before = records(flow.root)
                    with patch('src.pages.initial_workspace.create_initial_workspace', side_effect=AssertionError('must not reinitialize')) as initial:
                        await self.invoke(flow, detail())
                    initial.assert_not_called()
                    self.assertEqual(before, records(flow.root))


class DirectLinkTypeObservationContracts(unittest.TestCase):
    def test_detail_retains_canonical_three_state_type_evidence(self):
        cases = [
            ('tmdb', detail(genres=[{'id': 18, 'name': 'Drama'}]), 'general'),
            ('tmdb', detail(genres=[{'id': 16, 'name': 'Animation'}]), 'animation'),
            ('tmdb', detail(genres=[]), 'unknown'),
            ('tmdb', detail(genres=[{'id': 18, 'name': 'Animation'}]), 'unknown'),
            ('bangumi', {'id': 4242, 'type': 2, 'platform': 'TV', 'name': 'Anime'}, 'animation'),
            ('bangumi', {'id': 4242, 'type': 6, 'platform': 'TV', 'name': 'Drama'}, 'general'),
            ('bangumi', {'id': 4242, 'platform': 'TV', 'name': 'Missing type'}, 'unknown'),
        ]
        for namespace, payload, domain in cases:
            with self.subTest(namespace=namespace, domain=domain, payload=payload):
                session = _Session(payload)
                candidate = resolve_provider_item_url(
                    'https://www.themoviedb.org/tv/4242' if namespace == 'tmdb' else 'https://bgm.tv/subject/4242',
                    tmdb_api_key='fixture-only', bangumi_user_agent='BAR/test', session=session)
                self.assertEqual(domain, candidate.get('library_domain'))
