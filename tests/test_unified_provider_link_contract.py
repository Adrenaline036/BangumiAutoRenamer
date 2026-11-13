"""Review-page link entry: actual registered controls and late lookup results."""
import asyncio
from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, patch

from nicegui import Client
from nicegui.page import page as nice_page

from src.pages import plan_review_page as page
from tests import test_general_season_correction_contract as season
from tests.test_content_choice_contract import records as all_records


def records(root):
    # Only optional reference data may change after the view is closed.
    return {k:v for k,v in all_records(root).items() if not k.startswith('season_references/')}

from tests.test_general_work_correction_contract import TARGET
from tests.test_provider_search_v5 import _Session
from src.rename.provider_search import resolve_provider_item_url, ProviderSearchError


class UnifiedProviderLinkContracts(unittest.IsolatedAsyncioTestCase):
    flow = season.GeneralSeasonCorrectionContracts.flow
    current = season.GeneralSeasonCorrectionContracts.current

    @asynccontextmanager
    async def dialog(self):
        async with self.current() as (flow, plan, state):
            client = Client(nice_page('/isolated-unified-link'), request=None)
            with client, patch.object(page, 'normalize_provider_candidates', return_value=[]):
                dialog = page.PlanReviewDialog(plan, flow.manager._read(flow.task_id))
                dialog.task_decision = state
                dialog._show_provider_candidates()
            try:
                yield flow, dialog
            finally:
                if not dialog.is_deleted:
                    dialog.delete()
                client.delete()

    def controls(self, dialog):
        fields = [e for e in dialog.descendants()
                  if '作品详情页链接' in str(e._props.get('label', ''))]
        buttons = [e for e in dialog.descendants()
                   if e._props.get('label') == '读取链接并选择此作品']
        return fields, buttons

    def callback(self, button):
        callbacks = []
        with patch('nicegui.elements.button.handle_event', side_effect=lambda callback, _: callbacks.append(callback)):
            next(e for e in button._event_listeners.values() if e.type == 'click').handler(None)
        self.assertEqual(1, len(callbacks))
        return callbacks[0]

    async def test_actual_empty_candidates_has_one_link_entry_and_common_preview(self):
        async with self.dialog() as (_flow, dialog):
            fields, buttons = self.controls(dialog)
            self.assertEqual(1, len(fields), 'choosing a site must not precede pasting a link')
            self.assertEqual(1, len(buttons))
            self.assertFalse(dialog.work_correction_container.is_deleted)
            self.assertIs(fields[0].parent_slot.parent, dialog.work_correction_container.parent_slot.parent)

    async def test_actual_candidates_stay_in_two_limited_columns(self):
        async with self.dialog() as (_flow, dialog):
            candidates = [{**TARGET, 'provider_id': str(6000 + i), 'title': f'候选{i}',
                'namespace': namespace, 'entity_type': 'subject' if namespace == 'bangumi' else 'tv'}
                for namespace in ('bangumi', 'tmdb') for i in range(6)]
            with dialog.client, patch.object(page, 'normalize_provider_candidates', return_value=candidates):
                dialog._show_provider_candidates()
            self.assertEqual((1, 1), tuple(len(items) for items in self.controls(dialog)))
            selects = [e for e in dialog.descendants() if e._props.get('label') == '选择这个作品']
            self.assertEqual(8, len(selects))
            labels = [str(getattr(e, 'text', '')) for e in dialog.descendants()]
            self.assertIn('Bangumi 结果（最多 4 项）', labels)
            self.assertIn('TMDB 结果（最多 4 项）', labels)

    async def late_result(self, mode):
        async with self.dialog() as (flow, dialog):
            fields, buttons = self.controls(dialog)
            fields[-1].value = TARGET['source_url']
            callback = self.callback(buttons[-1])
            entered, release = asyncio.Event(), asyncio.Event()
            async def lookup(_value):
                entered.set()
                await release.wait()
                return dict(TARGET)
            before = records(flow.root)
            with (patch.object(page, '_read_manual_provider_url', side_effect=lookup) as reader,
                    patch.object(page, 'notify'), patch('src.pages.work_correction_view.show', new_callable=AsyncMock) as select):
                with dialog.client:
                    pending = asyncio.create_task(callback())
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    if mode == 'exit':
                        dialog.exit_requested = True
                    elif mode == 'close':
                        with dialog.client:
                            dialog.open()
                            dialog.close()
                    elif mode == 'disconnect':
                        self.assertTrue(dialog.client.disconnect_handlers)
                        for handler in tuple(dialog.client.disconnect_handlers):
                            dialog.client.safe_invoke(handler)
                    elif mode == 'delete':
                        dialog.delete()
                    elif mode == 'double':
                        await callback()
                    else:
                        self.fail(mode)
                    await asyncio.sleep(0)
                finally:
                    release.set()
                    await asyncio.wait_for(pending, 3)
                    request=flow.manager._provider_reads.get(flow.task_id)
                    if request is not None: await asyncio.wait_for(request.worker,3)
                self.assertEqual(before, records(flow.root))
                reader.assert_awaited_once()
                if mode == 'double':
                    select.assert_awaited_once()
                else:
                    select.assert_not_awaited()
                request=flow.manager._provider_reads.get(flow.task_id)
                self.assertTrue(request is None or request.worker.done())

    async def test_actual_closed_dialog_cannot_dispatch_late_link_result(self):
        await self.late_result('exit')

    async def test_closed_window_stops_presentation_and_duplicate_click_reuses_lookup(self):
        for mode in ('close', 'disconnect', 'delete', 'double'):
            with self.subTest(mode=mode):
                await self.late_result(mode)

    async def test_same_registered_input_uses_real_resolver_and_existing_type_gates(self):
        async with self.dialog() as (flow, dialog):
            fields, buttons = self.controls(dialog)
            callback = self.callback(buttons[0])
            cases = [
                ('https://www.themoviedb.org/tv/5252', dict(id=5252, name='另一部剧集', genres=[{'id': 18}]), True),
                ('https://www.themoviedb.org/movie/5252', dict(id=5252, title='另一部电影', genres=[{'id': 18}]), False),
                ('https://bgm.tv/subject/5252', dict(id=5252, type=2, platform='TV', name='动画'), False),
            ]
            before = records(flow.root)
            for url, payload, allowed in cases:
                with self.subTest(url=url):
                    session = _Session(payload)
                    def lookup(value):
                        return resolve_provider_item_url(value, tmdb_api_key='tmdb-fixture',
                            bangumi_token='bangumi-fixture', bangumi_user_agent='BAR/test', session=session)
                    fields[0].value = url
                    with dialog.client, patch.object(page, '_read_manual_provider_url', side_effect=lookup), \
                            patch.object(page, 'notify'), patch('src.pages.work_correction_view.show', new_callable=AsyncMock) as select:
                        await callback()
                    self.assertEqual(int(allowed), select.await_count)
                    self.assertEqual(1, len(session.calls))
                    call = str(session.calls[0])
                    self.assertNotIn('bangumi-fixture' if 'themoviedb.org' in url else 'tmdb-fixture', call)
                    self.assertEqual(before, records(flow.root))

    async def test_empty_invalid_or_disconnected_resolver_never_dispatches(self):
        async with self.dialog() as (flow, dialog):
            fields, buttons = self.controls(dialog)
            callback = self.callback(buttons[0])
            before = records(flow.root)
            for url, payload in (('', {}), ('https://example.invalid/tv/5252', {}),
                    ('https://www.themoviedb.org/search/5252', {}),
                    (TARGET['source_url'], {}), (TARGET['source_url'], [])):
                with self.subTest(url=url, payload=payload):
                    session = _Session(payload)
                    def lookup(value):
                        return resolve_provider_item_url(value, tmdb_api_key='fixture', bangumi_user_agent='BAR/test', session=session)
                    fields[0].value = url
                    with dialog.client, patch.object(page, '_read_manual_provider_url', side_effect=lookup), \
                            patch.object(page, 'notify'), patch('src.pages.work_correction_view.show', new_callable=AsyncMock) as select:
                        await callback()
                    select.assert_not_awaited()
                    if url != TARGET['source_url']:
                        self.assertFalse(session.calls)
                    self.assertEqual(before, records(flow.root))
            fields[0].value = TARGET['source_url']
            for failure in (ProviderSearchError('fixture resolver disconnected'), None):
                with dialog.client, patch.object(page, '_read_manual_provider_url', side_effect=failure, return_value=None), \
                        patch.object(page, 'notify'), patch('src.pages.work_correction_view.show', new_callable=AsyncMock) as select:
                    await callback()
                select.assert_not_awaited()
                request=flow.manager._provider_reads.get(flow.task_id)
                self.assertTrue(request is None or request.worker.done())
                self.assertEqual(before, records(flow.root))
