"""A real dialog must settle the already persisted pending-movie selection."""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
import unittest

from src.pages import plan_review_page as page
from src.rename.plan_store import PlanStore
from tests import test_pending_movie_reselection as pending
from tests import test_content_workbench_gui as workbench


class MovieReselectionGui(unittest.IsolatedAsyncioTestCase):
    flow = pending.PendingMovieReselectionContracts.flow

    @asynccontextmanager
    async def dialog(self):
        async with self.flow() as flow:
            gui = workbench.ContentWorkbenchGui('runTest')
            gui.create_page(prepared=(flow.root, flow.service, flow.store.load(flow.task_id), []),
                            cleanup_owner=flow)
            with patch.object(page, 'task_queue', flow.manager):
                yield flow, gui

    def labels(self, gui):
        return [str(getattr(e, 'text', e._props.get('label', ''))) for e in gui.view.descendants()]

    async def choose(self, gui, candidate, *, card=False):
        with gui.client:
            if card:
                with patch.object(page, 'normalize_provider_candidates', return_value=[dict(candidate)]):
                    gui.view._show_provider_candidates()
                    await gui.driver.click('选择这个作品')
                return
            gui.view._show_provider_candidates()
            # Invoke the same registered link control, with only its network
            # transport frozen; dialog adoption/renderers are production code.
            gui.view.provider_url_input.value = (
                'https://bangumi.tv/subject/242216' if candidate['namespace'] == 'bangumi'
                else 'https://www.themoviedb.org/movie/533514')
            with patch.object(page, '_read_manual_provider_url', return_value=dict(candidate)):
                await gui.driver.click('读取链接并选择此作品')

    async def test_full_dialog_tmdb_bangumi_repeat_and_reverse_settle(self):
        async with self.dialog() as (flow, gui):
            for candidate in (pending.TMDB, pending.BGM, pending.BGM, pending.TMDB):
                with self.subTest(provider=candidate['namespace']):
                    await self.choose(gui, candidate)
                    await asyncio.wait_for(flow.manager._queue.join(), 12)
                    saved = flow.store.load(flow.task_id)
                    self.assertEqual(candidate['provider_id'], saved.work_identity.primary_provider.provider_id)
                    self.assertEqual(PlanStore.fingerprint(saved), PlanStore.fingerprint(gui.view.plan))
                    self.assertFalse(gui.view.provider_selection_expanded, getattr(flow.manager._provider_reads.get(flow.task_id), 'error', str(gui.notices)))
                    labels = self.labels(gui)
                    self.assertIn('当前已选作品', labels)
                    self.assertFalse(any('没有找到正确的' in label for label in labels))
                    primary = [e for e in gui.view.descendants()
                               if e._props.get('label') == '正片选择与标签编辑']
                    self.assertEqual(1, len(primary))
                    self.assertFalse(primary[0]._props.get('disable', False))
            with gui.client:
                await gui.driver.click('正片选择与标签编辑')
            pickers = [e for e in gui.client.elements.values()
                       if e._props.get('label') == '电影正片文件（可多选）']
            self.assertEqual(1, len(pickers))
            self.assertIsNone(flow.service.decision_state_store.load(flow.task_id).layout_mode)

    async def test_failed_queue_attempt_keeps_selection_and_old_plan(self):
        async with self.dialog() as (flow, gui):
            await self.choose(gui, pending.TMDB)
            before = flow.store.load(flow.task_id)
            with patch.object(pending.selection, 'build', return_value=None):
                await self.choose(gui, pending.BGM)
            self.assertEqual(before, flow.store.load(flow.task_id))
            self.assertEqual(before, gui.view.plan)
            self.assertTrue(gui.view.provider_selection_expanded)
            self.assertTrue(any(e._props.get('label') == '读取链接并选择此作品'
                                for e in gui.view.descendants()))

    async def test_closed_dialog_does_not_adopt_queued_result(self):
        for mode in ('close', 'disconnect', 'delete'):
            with self.subTest(mode=mode):
                async with self.dialog() as (flow, gui):
                    before = gui.view.plan
                    original = flow.manager.wait_for_selection_result
                    async def wait(task_id, **kwargs):
                        result = await original(task_id)
                        with gui.client:
                            if mode == 'close':
                                gui.view.close()
                            elif mode == 'delete':
                                gui.view.delete()
                            else:
                                for handler in tuple(gui.client.disconnect_handlers):
                                    gui.client.safe_invoke(handler)
                        return result
                    with patch.object(flow.manager, 'wait_for_selection_result', side_effect=wait):
                        await self.choose(gui, pending.TMDB)
                    self.assertEqual(before, gui.view.plan)
                    self.assertTrue(gui.view.provider_selection_expanded)
                    self.assertEqual('confirmed', flow.store.load(flow.task_id).work_identity.status)

    async def test_later_failed_request_is_not_adopted_as_this_success(self):
        from src.rename.review_operation import build_provider_selection_review_operation, set_review_operation
        async with self.dialog() as (flow, gui):
            before = gui.view.plan
            original = flow.manager.wait_for_selection_result
            async def wait(task_id, **kwargs):
                result = await original(task_id)
                task = flow.manager._read(task_id)
                newer = {**result, 'request_id':'a-later-request', 'state':'failed',
                         'failure_code':'provider_unavailable', 'reason_codes':['provider_unavailable']}
                set_review_operation(task, build_provider_selection_review_operation(task, newer))
                flow.manager._update(task_id, review_operation=task['review_operation'])
                return result
            with patch.object(flow.manager, 'wait_for_selection_result', side_effect=wait):
                await self.choose(gui, pending.TMDB)
            self.assertEqual(before, gui.view.plan)
            self.assertTrue(gui.view.provider_selection_expanded)
            actual = flow.manager._read(flow.task_id)['review_operation']['context']
            self.assertEqual('a-later-request', actual['request_id'])
            self.assertEqual('provider_unavailable', actual['failure_code'])

    async def test_card_uses_same_success_and_disconnect_handling(self):
        async with self.dialog() as (flow, gui):
            await self.choose(gui, pending.TMDB, card=True)
            self.assertFalse(gui.view.provider_selection_expanded, getattr(flow.manager._provider_reads.get(flow.task_id), 'error', str(gui.notices)))
            before = gui.view.plan
            original = flow.manager.wait_for_selection_result
            async def wait(task_id, **kwargs):
                result = await original(task_id)
                for handler in tuple(gui.client.disconnect_handlers):
                    gui.client.safe_invoke(handler)
                return result
            with patch.object(flow.manager, 'wait_for_selection_result', side_effect=wait):
                await self.choose(gui, pending.BGM, card=True)
            self.assertEqual(before, gui.view.plan)
            self.assertEqual('242216', flow.store.load(flow.task_id).work_identity.primary_provider.provider_id)

    async def test_incompatible_candidates_cannot_enqueue_even_by_callback(self):
        async with self.dialog() as (flow, gui):
            before = flow.store.load(flow.task_id)
            for changes in ({'platform':'TV'}, {'platform':''}, {'library_domain':'general'}):
                candidate = {**pending.BGM, **changes}
                with self.subTest(changes=changes), gui.client:
                    with patch.object(page, 'normalize_provider_candidates', return_value=[candidate]):
                        gui.view._show_provider_candidates()
                    button = gui.driver.button('选择这个作品')
                    self.assertFalse(button.enabled)
                    with patch.object(flow.manager, 'enqueue_reidentify', new_callable=AsyncMock) as enqueue:
                        await gui.view._select_provider_candidate(candidate)
                        enqueue.assert_not_awaited()
                    self.assertEqual(before, flow.store.load(flow.task_id))

    async def test_missing_operation_and_changed_view_cannot_adopt(self):
        for mode in ('missing', 'view'):
            with self.subTest(mode=mode):
                async with self.dialog() as (flow, gui):
                    from dataclasses import replace
                    before = gui.view.plan
                    original = flow.manager.wait_for_selection_result
                    async def wait(task_id, **kwargs):
                        result = await original(task_id)
                        if mode == 'missing':
                            flow.manager._update(task_id, review_operation=None)
                        else:
                            gui.view.plan = replace(before, revision_reason='newer view')
                        return result
                    with patch.object(flow.manager, 'wait_for_selection_result', side_effect=wait):
                        await self.choose(gui, pending.TMDB)
                    self.assertTrue(gui.view.provider_selection_expanded)
                    self.assertEqual(before if mode == 'missing' else replace(before, revision_reason='newer view'),
                                     gui.view.plan)


class CandidateTypeWarning(unittest.TestCase):
    def test_bangumi_movie_and_tv_are_classified_by_shared_capability(self):
        for flag, candidate in ((True, pending.BGM),
                               (False, {**pending.BGM, 'platform':'TV'}),
                               (True, pending.TMDB)):
            with self.subTest(flag=flag, candidate=candidate):
                self.assertEqual('', page.candidate_type_warning(flag, [candidate]))

    def test_wrong_and_unknown_type_still_warn(self):
        for candidate in ({**pending.BGM, 'platform':'TV'},
                          {**pending.BGM, 'platform':''},
                          dict(namespace='tmdb', entity_type='tv', provider_id='1')):
            with self.subTest(candidate=candidate):
                self.assertIn('没有找到正确的电影匹配', page.candidate_type_warning(True, [candidate]))
