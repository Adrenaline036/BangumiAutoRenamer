"""作品更正通过真实选择、重开与候选展开回调保留；只使用隔离任务。"""
import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from src.pages import plan_review_page as page, provider_selection_work as selection
from tests import test_cross_provider_work_correction as cross
from tests import test_content_workbench_gui as gui
from tests.test_content_choice_contract import records as all_records


def records(root):
    # 参考标签允许预热；计划、人工决定、来源与媒体仍须逐字节不变。
    return {k: v for k, v in all_records(root).items() if not k.startswith('season_references/')}


class WorkCorrectionLifecycle(unittest.IsolatedAsyncioTestCase):
    observe = cross.CrossProviderWorkCorrection.observe
    create_page = gui.ContentWorkbenchGui.create_page

    async def original(self, *, movie=False):
        from tests import test_planning_lifecycle_controls as lifecycle
        from tests import test_initial_auto_link_contract as initial
        helper = lifecycle.PlanningLifecycleControls('runTest')
        payload = initial.detail(movie=movie, genres=[{'id': 16, 'name': 'Animation'}])
        payload['seasons'] = [{'season_number': 1, 'episode_count': 2}]
        def configure(worker):
            lookup = lambda *args: (payload['title' if movie else 'name'], dict(payload))
            if movie:
                worker.search.get_movie_info = lookup
                worker.search.get_movie_info_by_id = lookup
            else:
                worker.search.get_tv_info = lookup
                worker.search.get_tv_info_by_id = lookup
        f = await self.enterAsyncContext(helper.flow(movie=movie, automatic=False,
            admission_only=True, admit=helper.admit_auto, configure_worker=configure))
        view = helper.dialog(f)
        view.provider_url_input.value = 'https://www.themoviedb.org/' + ('movie/8181' if movie else 'tv/4242')
        f.manager.executor = f.manager._execute_rename
        async def reader(url):
            return initial.resolve_provider_item_url(url, tmdb_api_key='fixture-only',
                bangumi_user_agent='BAR/test', session=initial._Session(payload))
        try:
            with patch.object(page, '_read_manual_provider_url', side_effect=reader):
                with view.client:
                    await view._select_manual_provider_url()
                request = f.manager._provider_reads[f.task_id]
                await asyncio.wait_for(request.worker, 20)
                await asyncio.wait_for(f.manager._queue.join(), 5)
            self.assertFalse(request.error, request.error)
            plan = f.store.load(f.task_id)
            self.assertEqual('tmdb', plan.work_identity.primary_provider.namespace)
            if movie:
                from src.rename.initial_movie_completion import RootSnapshotSource
                from src.rename.target_routing import ROOT_KEYS, TargetRootSnapshot
                f.service.root_snapshot_source = RootSnapshotSource(lambda: TargetRootSnapshot.capture(
                    {key: f.root / 'isolated-targets' / key for key in ROOT_KEYS}))
                videos = [str(e.source_path) for e in plan.entries if e.media_kind == 'video']
                intent = f.service.prepare_initial_movie_completion(plan.task_id, page.PlanStore.fingerprint(plan),
                    groups=[dict(sources=videos, label='1080p')], excluded_sources=[], reason='隔离电影控制')
                plan = f.service.commit_initial_movie_completion(intent)
            return f.root, f.service, plan
        finally:
            view.delete()

    @asynccontextmanager
    async def opened_correction(self, *, slow=False, fail=False, reverse=False, movie=False):
        root, service, plan = await self.original(movie=movie)
        if reverse:
            plan = cross.CrossProviderWorkCorrection.confirm(self, service, plan, cross.BANGUMI, 1)
        target = dict(cross.TMDB if reverse else cross.BANGUMI)
        method = 'observe_work_correction'
        if movie:
            target.update(platform='剧场版', library_domain='animation')
            method = 'prepare_movie_work_correction'
            detail = dict(id=53918, type=2, name='New Movie', name_cn='新的动画电影', platform='剧场版', date='2013-01-01')
            with patch('src.rename.provider_search.BangumiProviderClient._get_json', return_value=detail):
                observed = service.prepare_movie_work_correction(plan.task_id, page.PlanStore.fingerprint(plan),
                    expected_revision=service.decision_state_store.load(plan.task_id).revision, candidate=target)
        else:
            observed = self.observe(service, plan, target)
        self.create_page(prepared=(root, service, plan, []))
        notices = self.notices
        with self.client:
            self.view._show_provider_candidates()
            await self.view._select_provider_candidate(target)
        work = page.task_queue._provider_reads[plan.task_id]
        await asyncio.wait_for(work.worker, 15)
        self.assertEqual('movie' if movie else 'tv', work.route, work.error)
        self.assertFalse(self.view.value)
        self.assertEqual('bangumi' if reverse else 'tmdb', service.plan_store.load(plan.task_id).work_identity.primary_provider.namespace)
        entered, release = threading.Event(), threading.Event()
        if not slow:
            release.set()
        calls = []
        def observe(*args, **kwargs):
            calls.append(kwargs['candidate'])
            entered.set()
            if not release.wait(10):
                raise AssertionError('隔离读取未释放')
            if fail:
                raise ValueError('隔离作品资料读取失败')
            return observed
        opened, stop = asyncio.Event(), asyncio.Event()
        windows = []
        def hold(dialog):
            async def wait():
                windows.append(dialog)
                opened.set()
                await stop.wait()
                dialog._close_review()
            return wait().__await__()
        timers = []
        original_timer = page.ui.timer
        def timer(interval, callback, **kwargs):
            if interval == 0 and kwargs.get('once'):
                pending = asyncio.create_task(callback())
                timers.append(pending)
                return SimpleNamespace()
            return original_timer(interval, callback, **kwargs)
        opening = None
        try:
            with patch.object(service, method, side_effect=observe), \
                 patch.object(page.PlanReviewDialog, '__await__', hold), \
                 patch.object(page.ui, 'timer', side_effect=timer):
                with self.client:
                    opening = asyncio.create_task(page._open_plan_review_page(plan.task_id, self.client))
                await asyncio.wait_for(opened.wait(), 15)
                self.assertTrue(await asyncio.to_thread(entered.wait, 5), notices)
                yield SimpleNamespace(view=windows[0], work=work, release=release, timers=timers,
                    calls=calls, service=service, root=root, original=plan, target=target, notices=notices)
        finally:
            release.set()
            if timers:
                await asyncio.gather(*timers)
            stop.set()
            if opening is not None:
                await asyncio.wait_for(opening, 15)
            if not self.view.is_deleted:
                self.view.delete()

    def visible(self, element):
        return all(not p.is_deleted and getattr(p, 'visible', True)
            and (not isinstance(p, page.ui.dialog) or p.value)
            for p in element.ancestors(include_self=True))

    def text(self, view):
        return '\n'.join(str(getattr(e, 'text', '')) for e in view.descendants() if self.visible(e))

    async def finish(self, f):
        f.release.set()
        await asyncio.wait_for(asyncio.gather(*f.timers), 15)

    async def expand(self, f):
        buttons = [e for e in f.view.descendants()
            if type(e).__name__ == 'Button' and e.text == '选择／更换作品' and self.visible(e)]
        self.assertEqual(1, len(buttons))
        button = buttons[0]
        self.assertTrue(button.enabled)
        await self.driver.click_element(button)
        self.assertTrue(f.view.provider_selection_expanded)
        self.assertIn('选择正确的作品', self.text(f.view))
        self.assertIsNotNone(f.view.provider_url_input)
        self.assertTrue(self.visible(f.view.provider_url_input))
        self.assertTrue(f.view.provider_url_input.enabled)
        self.assertTrue(self.visible(f.view.provider_submit_button))
        self.assertTrue(f.view.provider_submit_button.enabled)
        self.assertIn('Bangumi 结果', self.text(f.view))
        self.assertIn('TMDB 结果', self.text(f.view))

    async def test_normal_reopen_can_confirm_bangumi_and_save_new_plan(self):
        async with self.opened_correction() as f:
            await self.finish(f)
            self.assertIn('Bangumi #53918', self.text(f.view))
            with self.client:
                cross.fill_manual_season(self.client, 3)
            await self.driver.click('预览更正后的计划')
            await self.driver.click('确认作品更正并生成新计划')
            changed = f.service.plan_store.load(f.original.task_id)
            self.assertEqual('bangumi', changed.work_identity.primary_provider.namespace, f.notices)
            self.assertEqual(3, changed.metadata['season_number'])
            self.assertFalse(f.service.load(changed.task_id).approved_entry_ids)
            self.assertNotIn('确认作品更正并生成新计划', self.text(f.view))
            self.assertTrue(all(not e.target_path.exists() for e in changed.entries))

    async def test_expand_during_read_keeps_correction(self):
        async with self.opened_correction(slow=True) as f:
            before = records(f.root)
            await self.expand(f)
            await self.finish(f)
            self.assertIn('Bangumi #53918', self.text(f.view))
            self.assertIn('预览更正后的计划', self.text(f.view))
            self.assertTrue(self.driver.button('预览更正后的计划').enabled)
            self.assertEqual(1, len(f.calls))
            self.assertEqual(before, records(f.root))

    async def test_expand_after_render_keeps_season_and_preview(self):
        async with self.opened_correction() as f:
            await self.finish(f)
            with self.client:
                cross.fill_manual_season(self.client, 3)
            await self.driver.click('预览更正后的计划')
            choice = f.view.work_season_choice
            before = records(f.root)
            await self.expand(f)
            await selection.watch(f.view, f.work)
            self.assertIn('Bangumi #53918', self.text(f.view))
            self.assertIn('确认作品更正并生成新计划', self.text(f.view))
            self.assertTrue(self.driver.button('确认作品更正并生成新计划').enabled)
            self.assertIs(choice, f.view.work_season_choice)
            self.assertEqual(3, choice())
            self.assertEqual(1, len(f.calls))
            self.assertEqual(before, records(f.root))

    async def test_read_failure_remains_visible_with_original_plan(self):
        async with self.opened_correction(fail=True) as f:
            before = records(f.root)
            await self.finish(f)
            await self.expand(f)
            self.assertIn('隔离作品资料读取失败', self.text(f.view))
            self.assertIsNot(getattr(f.view, '_provider_presented', None), f.work)
            self.assertEqual('tmdb', f.service.plan_store.load(f.original.task_id).work_identity.primary_provider.namespace)
            self.assertEqual(before, records(f.root))

    async def test_reverse_correction_still_confirms_tmdb(self):
        async with self.opened_correction(reverse=True) as f:
            await self.finish(f)
            await self.expand(f)
            self.assertIn('TMDB #53918', self.text(f.view))
            with self.client:
                cross.fill_manual_season(self.client, 2)
            await self.driver.click('预览更正后的计划')
            await self.driver.click('确认作品更正并生成新计划')
            changed = f.service.plan_store.load(f.original.task_id)
            self.assertEqual('tmdb', changed.work_identity.primary_provider.namespace, f.notices)
            self.assertFalse(f.service.load(changed.task_id).approved_entry_ids)
            self.assertTrue(all(not e.target_path.exists() for e in changed.entries))

    async def test_close_or_revision_drift_discards_late_result(self):
        from dataclasses import replace
        for fault in ('close', 'revision'):
            with self.subTest(fault=fault):
                async with self.opened_correction(slow=True) as f:
                    before = records(f.root)
                    if fault == 'close':
                        f.view._close_review()
                    else:
                        f.view.task_decision = replace(f.view.task_decision, revision=f.view.task_decision.revision + 1)
                    await self.finish(f)
                    self.assertNotIn('预览更正后的计划', self.text(f.view))
                    self.assertIsNot(getattr(f.view, '_provider_presented', None), f.work)
                    self.assertEqual(before, records(f.root))

    async def test_later_selection_wins_and_link_draft_stays_editable(self):
        async with self.opened_correction(slow=True) as f:
            before = records(f.root)
            await self.expand(f)
            self.assertEqual(f.work.draft.get('url') or f.work.url, f.view.provider_url_input.value)
            url = 'https://bgm.tv/subject/77777'
            f.view.provider_url_input.set_value(url)
            await self.expand(f)
            self.assertEqual(url, f.view.provider_url_input.value)
            target = {**f.target, 'provider_id': '77777', 'source_url': url}
            with patch.object(page, '_read_manual_provider_url', return_value=target):
                await self.driver.click_element(f.view.provider_submit_button)
                later = page.task_queue._provider_reads[f.original.task_id]
                await asyncio.wait_for(later.worker, 10)
            self.assertIsNot(later, f.work)
            self.assertEqual('77777', later.candidate['provider_id'])
            self.assertFalse(later.error, later.error)
            await self.finish(f)
            self.assertIsNot(getattr(f.view, '_provider_presented', None), f.work)
            self.assertEqual(before, records(f.root))

    async def test_movie_confirmation_survives_expand_and_commits(self):
        async with self.opened_correction(movie=True) as f:
            before = records(f.root)
            await self.finish(f)
            await self.expand(f)
            self.assertIn('新的动画电影', self.text(f.view))
            self.assertTrue(self.driver.button('确认更换作品').enabled)
            self.assertIs(getattr(f.view, '_provider_presented', None), f.work)
            self.assertEqual(before, records(f.root))
            await self.driver.click('确认更换作品')
            changed = f.service.plan_store.load(f.original.task_id)
            self.assertEqual('bangumi', changed.work_identity.primary_provider.namespace, f.notices)
            self.assertFalse(f.service.load(changed.task_id).approved_entry_ids)
            self.assertNotIn('确认更换作品', self.text(f.view))
            self.assertTrue(all(not e.target_path.exists() for e in changed.entries))
