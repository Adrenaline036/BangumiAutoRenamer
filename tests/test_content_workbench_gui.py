"""The complete local review page reaches typed content commands via real events."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nicegui import ui
from nicegui.client import Client
from nicegui.page import page as nice_page
from src.pages import plan_review_page as review_page
from src.rename.plan_store import PlanStore
from tests.test_content_choice_contract import change, migrated, records
from tests.test_err060_role_entry import movie_dialog
from tests import _gui_helpers as gui_fixture


class ContentWorkbenchGui(unittest.IsolatedAsyncioTestCase):
    def create_page(self, *, legacy=False, with_font=False, prepared=None, cleanup_owner=None):
        # A helper IsolatedAsyncioTestCase has no running cleanup dispatcher.
        # When borrowed by another test, its running parent owns these resources.
        owner = self if cleanup_owner is None else cleanup_owner
        temp = owner.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temp)
        if prepared is not None:
            self.root, self.service, self.plan, self.videos = prepared
        elif legacy or with_font:
            holder, self.videos = movie_dialog(self.root, with_font=with_font)
            self.service, self.plan = holder.review_service, holder.plan
            if not legacy:
                self.plan = self.service.commit_content_change(self.service.prepare_content_migration(
                    self.plan.task_id, PlanStore.fingerprint(self.plan), reason='核对旧内容'))
        else:
            self.service, self.plan, self.videos = migrated(self.root)
        service, root = self.service, self.root
        # These GUI contracts are offline. Tests of reference acquisition supply
        # their own Mock; unrelated pages start with an already observed record.
        from unittest.mock import Mock
        from src.pages import season_reference_view
        if not isinstance(season_reference_view.load, Mock):
            from src.rename.season_reference import SeasonReferences
            from src.rename.season_reference_record import record_path, save, from_observation
            owner.enterContext(patch.object(season_reference_view,'load',return_value=SeasonReferences()))
            if self.plan.work_identity is not None:
                provider=self.plan.work_identity.primary_provider.to_dict()
                if (provider['namespace'],provider['entity_type']) in {('bangumi','subject'),('tmdb','tv')}:
                    result=from_observation(provider,self.plan.metadata.get('tv_completion_special')) or SeasonReferences()
                    save(record_path(service.task_root,self.plan.task_id,provider),provider,result)
        def task(_task_id):
            return json.loads((service.task_root / f'{self.plan.task_id}.json').read_text('utf-8'))
        from src.task_queue import TaskQueueManager
        from src.rename.plan_execution import PlanExecutionService
        execution = PlanExecutionService(service.plan_store, service,
            task_root=service.task_root, record_root=service.record_root,
            catalog_root=service.work_catalog.root, series_catalog_root=service.series_catalog.root,
            decision_state_root=service.decision_state_store.root,
            recovery_transaction_root=root/'recovery_transactions', provider_link_catalog_root=root/'provider_link_catalog')
        manager = TaskQueueManager(task_root=service.task_root, plan_root=service.plan_store.root, execution_service=execution,
            decision_state_root=service.decision_state_store.root,
            recovery_abandon_audit_root=root/'recovery_abandon_audit')
        owner.addAsyncCleanup(manager.stop)
        owner.enterContext(patch.multiple(review_page,
            PLAN_PATH=service.plan_store.root, TASK_PATH=service.task_root,
            DECISION_STATE_PATH=service.decision_state_store.root, PLAN_REVIEW_PATH=service.review_root,
            WORK_CATALOG_PATH=root/'work_catalog', SERIES_CATALOG_PATH=root/'series_catalog',
            RECORD_PATH=root/'record', RECOVERY_TRANSACTION_PATH=root/'recovery_transactions',
            PlanReviewService=lambda *_a, **_kw: service, get_task=task,
            task_queue=manager))
        self.notices = []
        owner.enterContext(patch.object(review_page, 'notify', lambda text, **_kw: self.notices.append(text)))
        self.client = Client(nice_page('/isolated-content-workbench'), request=None)
        owner.addCleanup(self.client.delete)
        self.driver = gui_fixture.GuiDriver('runTest')
        self.driver.client = self.client
        # Opening the existing parent page may create its old draft-review
        # record. The workbench zero-write baseline starts after this real UI.
        with self.client:
            self.view = review_page.PlanReviewDialog(self.plan, task(self.plan.task_id))
            self.view.open()
        async def finish_reference():
            worker=getattr(self.view,'_season_reference_task',None)
            if worker is not None:
                await asyncio.wait_for(asyncio.shield(worker),30)
        owner.addAsyncCleanup(finish_reference)
        return records(root)

    def source_box(self, source):
        label = source.relative_to(self.plan.source_root).as_posix()
        choices = [e for e in self.client.elements.values()
            if type(e).__name__ == 'Checkbox' and getattr(e, 'text', '') == label]
        self.assertEqual(1, len(choices), label)
        return choices[0]

    def text(self):
        return ' '.join(str(getattr(e, 'text', '')) for e in self.client.elements.values())

    async def test_complete_review_page_opens_and_cancels_without_content_writes(self):
        before = self.create_page()
        await self.driver.click('内容保留与排除')
        self.assertFalse(any(e._props.get('label') == '本次修改理由'
                             for e in self.client.elements.values()))
        self.source_box(self.videos[0])
        self.source_box(self.videos[3])  # Original omitted source must remain visible.
        self.assertEqual(before, records(self.root))
        await self.driver.click_element(self.view._content_workbench.close_button)
        self.assertEqual(before, records(self.root))
        self.assertFalse(self.notices)

    async def test_unmatched_audio_is_excluded_before_content_review(self):
        from tests import test_err059_delivery_incidents as incident
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture, result, videos = await incident.DeliveryIncidents('runTest').movie(root, orphan=True, fonts=False)
        completed = await fixture._complete_movie(root=root, result=result, primary=videos)
        orphan = videos[0].parent / 'Unmatched.mka'
        before = self.create_page(prepared=(root, completed.review, completed.plan, videos))
        self.assertIn(orphan, completed.plan.ignored_sources)
        self.assertNotIn(orphan, {e.source_path for e in completed.plan.entries})
        self.assertNotIn('external_audio_projection_invalid', {i['code'] for i in self.view.snapshot.issues})
        await self.driver.click('内容保留与排除')
        await self.driver.click_element(self.view._content_workbench.close_button)
        self.assertEqual(before, records(root))
        self.assertEqual(b'audio', orphan.read_bytes())


    async def test_batch_exclusion_and_restore_preserve_independent_subtitle_choice(self):
        before = self.create_page()
        video = self.videos[1]
        subtitle = video.with_suffix('.sc.ass')
        await self.driver.click('内容保留与排除')
        self.source_box(video).value = True
        self.source_box(subtitle).value = True
        await self.driver.click('排除整理')
        current = self.service.plan_store.load(self.plan.task_id)
        self.assertTrue({video, subtitle, video.with_suffix('.mka')} <= set(current.ignored_sources))
        choice = self.service.decision_state_store.load(self.plan.task_id).content_selection
        self.assertIn(str(subtitle), choice.to_dict()['excluded_sources'])
        self.assertNotIn(str(video.with_suffix('.mka')), choice.to_dict()['excluded_sources'])
        self.source_box(video).value = True
        await self.driver.click('纳入整理')
        current = self.service.plan_store.load(self.plan.task_id)
        self.assertNotIn(video, current.ignored_sources)
        self.assertNotIn(video.with_suffix('.mka'), current.ignored_sources)
        self.assertIn(subtitle, current.ignored_sources)
        self.assertFalse(self.service.load_or_create(current).approved_entry_ids)


    async def test_old_open_workbench_cannot_apply_to_a_later_plan(self):
        self.create_page()
        await self.driver.click('内容保留与排除')
        later = change(self.service, self.plan, [self.videos[0].with_suffix('.sc.ass')])
        with self.client:
            await self.view._adopt_revision(later)
        self.source_box(self.videos[1]).value = True
        before = records(self.root)
        await self.driver.click('排除整理')
        self.assertEqual(before, records(self.root))
        self.assertEqual(PlanStore.fingerprint(later), PlanStore.fingerprint(self.service.plan_store.load(self.plan.task_id)))
        labels = ' '.join(str(getattr(e, 'text', '')) for e in self.client.elements.values())
        self.assertIn('计划已更新，请关闭并重新打开内容工作区', labels)


    async def test_direct_drift_and_disconnected_commands_never_use_old_writers(self):
        for mode in ('revision', 'source', 'prepare_missing', 'commit_missing'):
            with self.subTest(mode=mode):
                self.create_page()
                await self.driver.click('内容保留与排除')
                self.source_box(self.videos[1]).value = True
                before = records(self.root)
                original = self.service.prepare_content_change
                def prepare(*args, **kwargs):
                    nonlocal before
                    if mode == 'prepare_missing':
                        return None
                    result = original(*args, **kwargs)
                    if mode == 'revision':
                        change(self.service, self.plan, [self.videos[0].with_suffix('.sc.ass')])
                        before = records(self.root)
                    elif mode == 'source':
                        self.videos[0].unlink()
                    return result
                with patch.object(self.service, 'ignore_plan_entry', side_effect=AssertionError('old writer')) as old, \
                     patch.object(self.service, 'recomplete_inventory_with_role_decisions', side_effect=AssertionError('old role')) as role, \
                     patch.object(self.service, 'prepare_content_change', side_effect=prepare):
                    if mode == 'commit_missing':
                        with patch.object(self.service, 'commit_content_change', return_value=None):
                            await self.driver.click('排除整理')
                    else:
                        await self.driver.click('排除整理')
                    self.assertEqual(before, records(self.root))
                    old.assert_not_called()
                    role.assert_not_called()
                    self.assertFalse(self.notices)


    async def test_real_prepare_and_commit_workers_remain_owned_until_terminal(self):
        for phase, mode in (('prepare', 'failure'), ('commit', 'failure'),
                            ('prepare', 'closed'), ('commit', 'closed'), ('prepare', 'cancelled'),
                            ('commit', 'cancelled'), ('commit', 'stale'),
                            ('commit', 'late_success')):
            with self.subTest(phase=phase, mode=mode):
                self.create_page()
                await self.driver.click('内容保留与排除')
                self.source_box(self.videos[1]).value = True
                name = 'prepare_content_change' if phase == 'prepare' else 'commit_content_change'
                caption = '排除整理'
                original = getattr(self.service, name)
                entered, release = threading.Event(), threading.Event()
                calls, thread_ids = [], []
                main = threading.get_ident()
                def blocked(*args, **kwargs):
                    calls.append(1); thread_ids.append(threading.get_ident())
                    entered.set()
                    if thread_ids[-1] != main and not release.wait(5):
                        raise RuntimeError('bounded GUI test barrier elapsed')
                    if mode == 'failure':
                        raise ValueError('本次后台内容核查失败')
                    result = original(*args, **kwargs)
                    if mode == 'late_success' and len(calls) == 1:
                        change(self.service, result, [self.videos[0].with_suffix('.sc.ass')])
                    return result
                before = records(self.root)
                with patch.object(self.service, name, side_effect=blocked):
                    waiter = asyncio.create_task(self.driver.click(caption))
                    try:
                        for _ in range(100):
                            if entered.is_set():
                                break
                            await asyncio.sleep(.01)
                        self.assertTrue(entered.is_set())
                        self.assertNotEqual(main, thread_ids[0])
                        self.assertFalse(waiter.done())
                        self.assertFalse(self.driver.button(caption).enabled)
                        await self.driver.click(caption)
                        self.assertEqual(1, len(calls))
                        if mode == 'closed':
                            await self.driver.click_element(self.view._content_workbench.close_button)
                        elif mode == 'cancelled':
                            waiter.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await waiter
                            self.assertIsNotNone(getattr(self.view, '_content_choice_worker', None))
                        elif mode == 'stale':
                            later = change(self.service, self.plan, [self.videos[0].with_suffix('.sc.ass')])
                            with self.client:
                                await self.view._adopt_revision(later)
                            before = records(self.root)
                    finally:
                        release.set()
                        if not waiter.cancelled():
                            await asyncio.wait_for(waiter, 5)
                        for _ in range(100):
                            if getattr(self.view, '_content_choice_worker', None) is None:
                                break
                            await asyncio.sleep(.01)
                    self.assertIsNone(getattr(self.view, '_content_choice_worker', None))
                if mode in {'failure', 'stale'}:
                    self.assertEqual(before, records(self.root))
                    self.assertFalse(self.notices)
                if mode == 'failure':
                    await self.driver.click(caption)
                    self.assertNotEqual(before, records(self.root))
                if mode in {'cancelled', 'closed'}:
                    self.assertNotEqual(before, records(self.root))
                if mode == 'late_success':
                    self.assertNotEqual(before, records(self.root))
                    self.assertEqual(PlanStore.fingerprint(self.plan), PlanStore.fingerprint(self.view.plan))
                    self.assertIn('已提交', self.text())
                    self.assertIn('后续', self.text())
                    self.assertFalse(self.notices)

    async def test_ten_rows_and_first_last_controls_preserve_selection_without_writes(self):
        from src.pages import content_review_view
        before = self.create_page()
        await self.driver.click('内容保留与排除')
        work = self.view._content_workbench
        for action, pane in work.panels.items():
            self.assertEqual(10, work.page_sizes[action])
            self.assertEqual([5, 10, 15, 20], pane['size'].options)
            self.assertNotIn('；', pane['label'].text)
            self.assertIn('  已选择', pane['label'].text)
        work.close()
        # One row per page makes both ends reachable with this small real plan.
        with patch.object(content_review_view, 'PAGE_SIZE', 1), patch.object(content_review_view, 'PAGE_OPTIONS', (1, 5, 10, 15, 20)):
            await self.driver.click('内容保留与排除')
        work = self.view._content_workbench
        for action, pane in work.panels.items():
            with self.subTest(action=action):
                self.assertFalse(pane['first'].enabled)
                work.select_current_page(True, action)
                selected = set(work.selected)
                await self.driver.click_element(pane['last'])
                self.assertEqual(max(0, len(work.column_rows(action))-1), work.pages[action])
                self.assertFalse(pane['last'].enabled)
                self.assertEqual(selected, work.selected)
                await self.driver.click_element(pane['first'])
                self.assertEqual(0, work.pages[action])
                work.set_busy(True)
                for key in ('first', 'previous', 'following', 'last', 'size', 'button'):
                    self.assertFalse(pane[key].enabled)
                work.set_busy(False)
        self.assertEqual(before, records(self.root))
        work.close()

    async def test_pagination_keeps_selections_across_pages(self):
        from src.pages import content_review_view
        self.create_page()
        with patch.object(content_review_view, 'PAGE_SIZE', 1), patch.object(content_review_view, 'PAGE_OPTIONS', (1, 12, 25, 50, 100)):
            await self.driver.click('内容保留与排除')
            audio = self.videos[0].with_suffix('.mka')
            subtitle = self.videos[0].with_suffix('.sc.ass')
            workbench = self.view._content_workbench
            pages = {row['source']: index for index, row in enumerate(workbench.column_rows('exclude'))}
            self.assertNotEqual(pages[audio], pages[subtitle])

            async def show(source):
                while workbench.pages['exclude'] != pages[source]:
                    await self.driver.click_element(workbench.panels['exclude']['following' if workbench.pages['exclude'] < pages[source] else 'previous'])

            await show(audio)
            self.source_box(audio).value = True
            await show(subtitle)
            self.source_box(subtitle).value = True
            await show(audio)
            self.assertTrue(self.source_box(audio).value)
            await self.driver.click('排除整理')
        current = self.service.plan_store.load(self.plan.task_id)
        self.assertTrue({audio, subtitle} <= set(current.ignored_sources))
