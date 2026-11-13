"""原窗口失败恢复与实际拆卡选择；只使用隔离媒体和冻结 Provider。"""
import asyncio
import inspect
import unittest
from unittest.mock import patch
from nicegui import Client
from nicegui.page import page as nice_page
from src.pages import plan_review_page as page
from src.rename.plan_store import PlanStore
from tests import test_default_activation_contract as activation
from tests import test_initial_auto_link_contract as initial_link


def flat_package(root, source):
    payload = next(source.glob('*S01E01*.mkv')).read_bytes()
    target = root / 'incoming' / 'Ordinary Show'
    target.mkdir(parents=True)
    for episode in (1, 2, 13, 14):
        (target / f'Ordinary Show [{episode:02d}].mkv').write_bytes(payload)
    return target


class SameWindowSeasonContracts(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow
    admit_auto = initial_link.InitialAutoLinkContracts.admit_auto

    async def click(self, dialog, label):
        button = next(e for e in dialog.descendants() if e._props.get('label') == label)
        self.assertTrue(button.enabled)
        callbacks = []
        with dialog.client, patch('nicegui.elements.button.handle_event', side_effect=lambda callback, _: callbacks.append(callback)):
            next(e for e in button._event_listeners.values() if e.type == 'click').handler(None)
        self.assertEqual(1, len(callbacks))
        with dialog.client:
            result = callbacks[0]()
            if inspect.isawaitable(result):
                await result

    async def select_and_continue(self, flow, dialog, number, *, expected_failure=True):
        identity = dialog.id
        dialog.provider_url_input.set_value('https://www.themoviedb.org/tv/4242')
        candidate = dict(namespace='tmdb', entity_type='tv', provider_id='4242', title='普通剧集',
            original_title='Ordinary Show', year=2020, library_domain='general',
            source_url='https://www.themoviedb.org/tv/4242', snapshot_at='2026-10-31T00:00:00Z')
        with patch.object(page, '_read_manual_provider_url', return_value=candidate):
            await self.click(dialog, '读取链接并选择此作品')
            await asyncio.wait_for(flow.manager._queue.join(), 12)
            task_id = dialog.task_id
            if expected_failure:
                from src.rename.review_operation import current_provider_selection_review_operation
                task = flow.manager._read(task_id)
                _, operation = current_provider_selection_review_operation(task)
                self.assertEqual('awaiting_season', operation['lifecycle'])
                self.assertIsNone(task.get('error'))
                self.assertFalse(flow.manager.decision_state_store.load(task_id).confirmed_work)
                self.assertTrue(hasattr(dialog, 'unresolved_season_input'), '失败后的原窗口必须提供可操作季号')
                self.assertTrue(dialog.unresolved_season_input.enabled)
                dialog.unresolved_season_input.set_value(number)
                await self.click(dialog, '确认季度编号')
                await asyncio.wait_for(flow.manager._queue.join(), 12)
            self.assertEqual(identity, dialog.id)
            self.assertFalse(dialog.is_deleted)
            state = flow.manager.decision_state_store.load(task_id)
            self.assertEqual('4242', state.confirmed_work['primary_provider']['provider_id'])
            self.assertEqual(number, state.season_binding['season_number'])
            plan = flow.store.load(task_id)
            self.assertEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(dialog.plan))
            self.assertEqual(state.revision, dialog.task_decision.revision)
            self.assertFalse(any(e.target_path.exists() for e in plan.entries if e.target_path != e.source_path))
            self.assertFalse(list((flow.root / 'records').glob('*.json')))

    async def initial(self, seasons, number, expected_failure=True):
        worker = []
        async with self.flow(automatic=False, admission_only=True, admit=self.admit_auto,
                             configure_source=flat_package, configure_worker=worker.append) as flow:
            flow.manager.executor = flow.manager._execute_rename
            worker[0].search.get_tv_info_by_id = lambda *_: ('普通剧集', {**initial_link.detail(), 'seasons': seasons})
            before = {p: p.read_bytes() for p in flow.source.iterdir()}
            client = Client(nice_page('/same-window-season'), request=None)
            with client:
                dialog = page.PlanReviewDialog(None, flow.manager._read(flow.task_id))
                dialog.open()
            try:
                await self.select_and_continue(flow, dialog, number, expected_failure=expected_failure)
                self.assertEqual(before, {p: p.read_bytes() for p in before})
            finally:
                client.delete()

    async def test_empty_seasons_failure_continues_in_original_dialog(self):
        await self.initial([], 4)

    async def test_multiple_seasons_failure_continues_in_original_dialog(self):
        await self.initial([{'season_number': 1}, {'season_number': 2}], 2)

    async def test_unique_season_still_completes_in_original_dialog(self):
        await self.initial([{'season_number': 1}], 1, False)

    async def test_regrouped_cards_select_work_and_manual_season_in_original_dialog(self):
        worker = []
        async with self.flow(automatic=False, configure_source=flat_package, configure_worker=worker.append) as flow:
            parent = flow.store.load(flow.task_id)
            videos = sorted((e.source_path for e in parent.entries if e.media_kind == 'video'), key=str)
            groups = [[str(p) for p in videos[:2]], [str(p) for p in videos[2:]]]
            preview = flow.manager.preview_source_regrouping(task_id=flow.task_id,
                expected_plan_fingerprint=PlanStore.fingerprint(parent), groups=groups)
            ids = flow.manager.create_regrouped_tasks(preview)
            before = {p: p.read_bytes() for p in flow.source.iterdir()}
            worker[0].search.get_tv_info_by_id = lambda *_: ('普通剧集', {**initial_link.detail(), 'seasons': [{'season_number': 1}, {'season_number': 2}]})
            for number, task_id in enumerate(ids, 1):
                client = Client(nice_page('/split-card-season'), request=None)
                from src.pages.recognition_recovery_view import RecognitionRecoveryDialog
                with client:
                    recovery = RecognitionRecoveryDialog(task_id, flow.manager._read(task_id), ValueError("来源待确认"))
                    recovery.open()
                await self.click(recovery, '继续识别来源')
                await asyncio.wait_for(flow.manager._queue.join(), 12)
                with client:
                    dialog = page.PlanReviewDialog(None, flow.manager._read(task_id))
                    dialog.open()
                try:
                    await self.select_and_continue(flow, dialog, number)
                    current = flow.store.load(task_id)
                    self.assertEqual(set(groups[number-1]), {str(e.source_path) for e in current.entries if e.media_kind == 'video'})
                finally:
                    client.delete()
            self.assertEqual(before, {p: p.read_bytes() for p in before})


class InitialFailureReadGuards(unittest.IsolatedAsyncioTestCase):
    async def test_closed_or_replaced_initial_failure_does_not_load_saved_plan(self):
        from unittest.mock import AsyncMock, Mock
        from tests import test_selection_failure_placement as placement
        from src.pages import provider_selection_work as work
        for interruption in ('close', 'replace_work', 'replace_request', 'read_error'):
            with self.subTest(interruption=interruption):
                view, pending, manager, _ = placement.FailurePlacement.setup_view(self, initial=True)
                view._load_plan_view = Mock()
                def read(*args):
                    if interruption == 'close':
                        view.exit_requested = True
                    elif interruption == 'replace_work':
                        manager._provider_reads['task'] = object()
                    elif interruption == 'read_error':
                        raise OSError('隔离读取失败')
                    return object(), object(), placement.failed_task('later' if interruption == 'replace_request' else 'request')
                with patch.object(page, 'task_queue', manager), patch.object(work, '_read_result', side_effect=read), \
                        patch.object(page, '_prepare_visible_plan_dialog', new_callable=AsyncMock) as prepare:
                    await work.watch(view, pending)
                prepare.assert_not_awaited()
                view._load_plan_view.assert_not_called()
                self.assertEqual({'uuid': 'task'}, view.task_data)
                if interruption == 'read_error':
                    self.assertIn('暂时无法读取', view.initial_progress.set_text.call_args.args[0])

    async def test_initial_failure_hydration_keeps_request_and_window_guard(self):
        from unittest.mock import AsyncMock, Mock
        from tests import test_selection_failure_placement as placement
        from src.pages import provider_selection_work as work
        for interruption in ('close', 'replace'):
            with self.subTest(interruption=interruption):
                view, pending, manager, _ = placement.FailurePlacement.setup_view(self, initial=True)
                view._load_plan_view = Mock()
                saved = object()
                async def prepare(actual, plan, client, *, is_current):
                    self.assertIs(actual, view)
                    self.assertIs(plan, saved)
                    self.assertTrue(is_current())
                    if interruption == 'close':
                        view.exit_requested = True
                    else:
                        manager._provider_reads['task'] = object()
                    self.assertFalse(is_current())
                    return False
                with patch.object(page, 'task_queue', manager), \
                        patch.object(work, '_read_result', return_value=(saved, object(), placement.failed_task())), \
                        patch.object(page, '_prepare_visible_plan_dialog', new_callable=AsyncMock, side_effect=prepare) as load:
                    await work.watch(view, pending)
                load.assert_awaited_once()
                view._load_plan_view.assert_not_called()
