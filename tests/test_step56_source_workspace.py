"""Reported pre-selection scope and split feedback incidents, owned fixtures."""
import asyncio
import copy
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from nicegui import Client
from src.pages import plan_review_page as page
from src.pages.recognition_recovery_view import RecognitionRecoveryDialog
from src.rename import recognition_queue_admission as admission
from src.rename.plan_execution import PlanExecutionError
from tests import test_light_review_contract as light
from tests import test_source_regrouping_view_contract as views
from tests.test_workspace_retry_atomic import snapshot


class SourceWorkspace(unittest.IsolatedAsyncioTestCase):
    async def test_source_rows_are_lexical_and_never_fall_back_to_parent_scan(self):
        from src.pages.task_source_files_view import source_file_rows
        helper = light.SingleWorkspace('runTest')
        async with helper.flow(admission_only=True, admit=helper.admit_auto) as flow:
            task = flow.manager._read(flow.task_id)
            expected = {r['source_path'] for r in task['task_source_scope']['source_identities']}
            self.assertTrue(expected)
            with patch.multiple(Path, stat=unittest.mock.Mock(side_effect=AssertionError('no stat')),
                    lstat=unittest.mock.Mock(side_effect=AssertionError('no lstat')),
                    resolve=unittest.mock.Mock(side_effect=AssertionError('no resolve')),
                    iterdir=unittest.mock.Mock(side_effect=AssertionError('no enumeration')),
                    read_bytes=unittest.mock.Mock(side_effect=AssertionError('no content read'))):
                rows = source_file_rows(task)
                self.assertEqual(expected, {r['source'] for r in rows})
                self.assertTrue(all(not Path(r['file']).is_absolute() for r in rows))
                single = {**task, 'path': next(iter(expected))}
                self.assertEqual(expected, {r['source'] for r in source_file_rows(single)})
                with self.assertRaises(ValueError):
                    source_file_rows({**task, 'path': str(flow.root / 'unrelated')})

    async def test_ordinary_unselected_scope_visible_without_source_reads(self):
        helper = light.SingleWorkspace('runTest')
        async with helper.flow(admission_only=True, admit=helper.admit_auto) as flow:
            task = flow.manager._read(flow.task_id)
            expected = {r['source_path'] for r in task['task_source_scope']['source_identities']}
            self.assertTrue(expected, '普通卡fixture必须含真实已接纳来源')
            before = snapshot(flow.root)
            with Client.auto_index_client.content, patch.object(Path, 'rglob', side_effect=AssertionError('no discovery')):
                task = {**task, 'provider_candidates': {'tv_episodes': [dict(namespace='tmdb', entity_type='tv', provider_id='53918', title='向山进发')]}}
                dialog = page.PlanReviewDialog(None, task)
            try:
                self.assertTrue(hasattr(dialog, 'source_file_list'), '未选作品缺少本卡文件清单')
                self.assertEqual(expected, {r['source'] for r in dialog.source_file_list.snapshot.rows})
                self.assertIn(dialog.source_file_list.container, list(dialog.descendants()))
                elements = list(dialog.descendants())
                candidate_heading = next(e for e in elements if getattr(e,'text','') == '搜索备选 · 请核对后选择')
                self.assertLess(elements.index(candidate_heading), elements.index(dialog.source_file_list.container))
                self.assertIn(page.REVIEW_PAGE_SIZE, (10, 20, 50))
                self.assertIsNone(dialog.plan)
                self.assertFalse(hasattr(dialog, 'approve_selected_button'))
                self.assertFalse(dialog.approve_all_button.enabled)
                self.assertEqual(before, snapshot(flow.root))
            finally:
                dialog.delete()

    async def test_recovery_scope_visible_and_wrong_task_scope_refused(self):
        helper = light.SingleWorkspace('runTest')
        async with helper.flow(admission_only=True, admit=helper.admit_auto) as flow:
            task = flow.manager._read(flow.task_id)
            self.assertTrue(task['task_source_scope']['source_identities'])
            for valid in (True, False):
                data = copy.deepcopy(task)
                if not valid:
                    data['task_source_scope']['task_id'] = 'another-task'
                with Client.auto_index_client.content:
                    dialog = RecognitionRecoveryDialog(flow.task_id, data, ValueError('owned missing plan'))
                try:
                    self.assertTrue(hasattr(dialog, 'source_file_list'), '恢复页缺少同一清单入口')
                    if valid:
                        self.assertEqual(len(task['task_source_scope']['source_identities']), len(dialog.source_file_list.snapshot.rows))
                        self.assertEqual({r['source_path'] for r in task['task_source_scope']['source_identities']},
                                         {r['source'] for r in dialog.source_file_list.snapshot.rows})
                        self.assertIn(dialog.source_file_list.container, list(dialog.descendants()))
                        self.assertIn(page.REVIEW_PAGE_SIZE, (10, 20, 50))
                    else:
                        self.assertIsNone(dialog.source_file_list)
                        self.assertTrue(any('无法展示本任务文件清单' in str(getattr(e, 'text', '')) for e in dialog.descendants()))
                finally:
                    dialog.delete()

    async def test_readonly_prepare_runs_offloop_commit_stays_onloop(self):
        helper = light.SingleWorkspace('runTest')
        async with helper.flow(admission_only=True, admit=helper.admit_auto) as flow:
            original = admission._candidate
            main = threading.get_ident()
            threads = []
            window, ticks = [], []
            async def heartbeat():
                while True:
                    ticks.append(time.monotonic())
                    await asyncio.sleep(.005)
            def observed(*args, **kwargs):
                threads.append(threading.get_ident())
                if len(threads) == 1:
                    window.append(time.monotonic())
                    time.sleep(.15)
                    window.append(time.monotonic())
                return original(*args, **kwargs)
            ticker = asyncio.create_task(heartbeat())
            try:
                with patch.object(admission, '_candidate', side_effect=observed):
                    await flow.manager.retry_current(flow.task_id)
            finally:
                ticker.cancel()
                await asyncio.gather(ticker, return_exceptions=True)
            self.assertEqual(2, len(threads))
            self.assertNotEqual(main, threads[0], '准备观察仍占用界面线程')
            self.assertEqual(main, threads[1], '原子提交必须留在原事件循环')
            self.assertGreaterEqual(sum(window[0] < tick < window[1] for tick in ticks), 2,
                                    '准备即使在外线程也不得同步阻塞界面循环')
            await flow.manager._queue.join()

    async def test_cancel_guard_for_other_loop_consumers_remains(self):
        async def probe():
            task = asyncio.current_task()
            task.cancel()
            try:
                with self.assertRaisesRegex(PlanExecutionError, '已取消'):
                    admission._available(None, 'owned')
            finally:
                while task.cancelling():
                    task.uncancel()
        result = await asyncio.gather(asyncio.create_task(probe()), return_exceptions=True)
        if result[0] is not None and not isinstance(result[0], asyncio.CancelledError):
            raise result[0]

    async def test_stop_restart_and_cancel_during_prepare_do_not_publish(self):
        helper = light.SingleWorkspace('runTest')
        for action in ('stop', 'restart', 'cancel'):
            with self.subTest(action=action):
                async with helper.flow(admission_only=True, admit=helper.admit_auto) as flow:
                    original = admission._prepare_readonly
                    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
                    def delayed(*args, **kwargs):
                        entered.set()
                        release.wait(5)
                        try:
                            return original(*args, **kwargs)
                        finally:
                            finished.set()
                    before = snapshot(flow.root)
                    with patch.object(admission, '_prepare_readonly', side_effect=delayed):
                        pending = asyncio.create_task(flow.manager.retry_current(flow.task_id))
                        try:
                            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                            if action == 'cancel':
                                pending.cancel()
                            else:
                                await flow.manager.stop()
                                if action == 'restart':
                                    await flow.manager.start()
                            release.set()
                            with self.assertRaises(asyncio.CancelledError if action == 'cancel' else PlanExecutionError):
                                await pending
                        finally:
                            release.set()
                            self.assertTrue(await asyncio.to_thread(finished.wait, 5))
                    self.assertEqual(before, snapshot(flow.root))
                    self.assertFalse(flow.manager._pending)
                    self.assertEqual(0, flow.manager._queue.qsize())


class SplitWorkspace(unittest.IsolatedAsyncioTestCase):
    workspace = views.SourceRegroupingViewContracts.workspace
    flow = views.SourceRegroupingViewContracts.flow
    current = views.SourceRegroupingViewContracts.current
    video = views.SourceRegroupingViewContracts.video
    link_subtitle = views.SourceRegroupingViewContracts.link_subtitle
    click = views.SourceRegroupingViewContracts.click
    open_preview = views.SourceRegroupingViewContracts.open_preview

    async def test_confirm_shows_busy_through_real_creation_and_blocks_duplicate(self):
        async with self.workspace() as (flow, work):
            popup = self.open_preview(work)
            query, create = popup._query, flow.manager.create_and_start_regrouped_tasks
            entered, release = threading.Event(), threading.Event()
            creating, create_release = asyncio.Event(), asyncio.Event()
            calls = []
            def slow_query(*args):
                entered.set()
                release.wait(5)
                return query(*args)
            async def slow_create(preview):
                calls.append(preview)
                creating.set()
                await create_release.wait()
                return await create(preview)
            with patch.object(popup, '_query', side_effect=slow_query), \
                    patch.object(flow.manager, 'create_and_start_regrouped_tasks', side_effect=slow_create):
                with work.client:
                    pending = asyncio.create_task(popup.confirm())
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    self.assertTrue(hasattr(popup, 'progress'), '实际确认路径没有忙反馈')
                    self.assertTrue(popup.progress.visible)
                    self.assertFalse(popup.confirm_button.enabled)
                    release.set()
                    await asyncio.wait_for(creating.wait(), 5)
                    self.assertTrue(popup.progress.visible, '创建阶段不能提前解除忙状态')
                    self.assertFalse(popup.confirm_button.enabled)
                    await popup.confirm()
                    self.assertEqual(1, len(calls))
                finally:
                    release.set()
                    create_release.set()
                    await asyncio.wait_for(pending, 20)
                    await asyncio.wait_for(flow.manager._queue.join(), 20)
            self.assertEqual(len(popup.groups), len(popup.created_ids))
            self.assertGreater(len(popup.created_ids), 1)
            self.assertFalse(popup.progress.visible)

    async def test_check_shows_busy_disables_submit_and_restores(self):
        async with self.workspace() as (flow, work):
            popup = self.open_preview(work)
            original = popup._query
            entered, release = threading.Event(), threading.Event()
            def slow(*args):
                entered.set()
                release.wait(5)
                return original(*args)
            with patch.object(popup, '_query', side_effect=slow):
                pending = asyncio.create_task(popup.check())
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    self.assertTrue(hasattr(popup, 'progress'), '忙状态没有可见控件')
                    self.assertTrue(popup.progress.visible)
                    self.assertFalse(popup.confirm_button.enabled)
                    await popup.confirm()
                    self.assertFalse(popup.created_ids, '核对期间不能重复提交')
                finally:
                    release.set()
                    await pending
            self.assertFalse(popup.progress.visible)
            self.assertTrue(popup.confirm_button.enabled)

    async def test_new_split_card_lists_only_its_scope_before_selection(self):
        async with self.workspace() as (flow, work):
            popup = self.open_preview(work)
            ids = flow.manager.create_regrouped_tasks(popup.preview)
            groups = popup.preview.to_dict()['groups']
            self.assertGreaterEqual(len(ids), 2)
            self.assertEqual(len(groups), len(ids))
            for task_id, group in zip(ids, groups):
                task = flow.manager._read(task_id)
                with work.client:
                    dialog = RecognitionRecoveryDialog(task_id, task, ValueError('no plan'))
                try:
                    self.assertTrue(hasattr(dialog, 'source_file_list'), '新卡文件清单未接统一入口')
                    self.assertEqual(set(group['sources']), {r['source'] for r in dialog.source_file_list.snapshot.rows})
                    self.assertIn(dialog.source_file_list.container, list(dialog.descendants()))
                    self.assertIn(page.REVIEW_PAGE_SIZE, (10, 20, 50))
                    self.assertFalse(task.get('provider_selection'))
                finally:
                    dialog.delete()
