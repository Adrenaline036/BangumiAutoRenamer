"""Bangumi短暂故障、有限等待与原任务恢复的定向验证。"""
import asyncio
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

import requests
from src.rename.provider_transport import BangumiTransport, ProviderRequestError
from src.pages.data_table_page import task_feedback_text
from src.pages.review_operation_view import build_review_operation_presentation


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class RetryContracts(unittest.TestCase):
    def transport(self, outcomes, *, budget=20):
        clock = Clock()
        session = Mock()
        def get(*args, **kwargs):
            self.assertLessEqual(sum(kwargs['timeout']), budget - clock.now)
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                clock.now += kwargs['timeout'][1 if isinstance(outcome, requests.ReadTimeout) else 0]
                raise outcome
            return outcome
        session.get.side_effect = get
        transport = BangumiTransport(session, {}, clock=clock, sleeper=clock.sleep,
                                    request_budget=budget)
        return transport, session, clock

    def response(self, status=200, headers=None, payload=None):
        response = Mock(status_code=status, headers=headers or {})
        response.json.return_value = payload or {'id': 101}
        response.raise_for_status.side_effect = requests.HTTPError() if status >= 400 else None
        return response

    def test_two_connection_timeouts_then_success_within_budget(self):
        t, session, clock = self.transport([requests.ConnectTimeout(), requests.ConnectTimeout(), self.response()])
        self.assertEqual({'id': 101}, t.request('get', '/v0/subjects/101', stage='detail'))
        self.assertEqual(3, session.get.call_count)
        self.assertLessEqual(clock.now, 20)
        self.assertGreater(clock.waits[1], clock.waits[0])
        self.assertEqual(1, len({call.args[0] for call in session.get.call_args_list}))

    def test_continuous_timeout_is_bounded_and_typed(self):
        t, session, clock = self.transport([requests.ReadTimeout()] * 5)
        with self.assertRaises(ProviderRequestError) as caught:
            t.request('get', '/v0/subjects/101', stage='detail')
        self.assertLessEqual(session.get.call_count, 3)
        self.assertLessEqual(clock.now, 20)
        self.assertEqual('timeout', caught.exception.diagnostic['code'])
        self.assertEqual('read', caught.exception.diagnostic.get('timeout_kind'))

    def test_connection_timeout_kind_is_not_read_timeout(self):
        t, session, clock = self.transport([requests.ConnectTimeout()] * 5)
        with self.assertRaises(ProviderRequestError) as caught:
            t.request('get', '/v0/subjects/101', stage='detail')
        self.assertEqual('connect', caught.exception.diagnostic.get('timeout_kind'))
        self.assertLessEqual(session.get.call_count, 3)
        self.assertLessEqual(clock.now, 20)

    def test_successful_first_request_never_retries(self):
        t, session, clock = self.transport([self.response()])
        self.assertEqual({'id': 101}, t.request('get', '/v0/subjects/101', stage='detail'))
        self.assertEqual(1, session.get.call_count)
        self.assertEqual([], clock.waits)

    def test_nonretryable_responses_stop_once(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                t, session, clock = self.transport([self.response(status)])
                with self.assertRaises(ProviderRequestError):
                    t.request('get', '/v0/subjects/101', stage='detail')
                self.assertEqual(1, session.get.call_count)
                self.assertEqual([], clock.waits)

    def test_server_wait_is_respected_or_stops_without_early_retry(self):
        for delay in (2, 30):
            with self.subTest(delay=delay):
                t, session, clock = self.transport([self.response(429, {'Retry-After': str(delay)}), self.response()])
                if delay < 20:
                    self.assertEqual({'id': 101}, t.request('get', '/v0/subjects/101', stage='detail'))
                    self.assertGreaterEqual(sum(clock.waits), delay)
                else:
                    with self.assertRaises(ProviderRequestError):
                        t.request('get', '/v0/subjects/101', stage='detail')
                    self.assertEqual(1, session.get.call_count)

    def test_operation_deadline_prevents_another_request(self):
        t, session, clock = self.transport([self.response()])
        with t.operation():
            clock.now = 61
            with self.assertRaises(ProviderRequestError):
                t.request('get', '/v0/subjects/101', stage='detail')
        session.get.assert_not_called()


class FailureCopyContracts(unittest.TestCase):
    def test_timeout_is_visible_in_list_and_original_card(self):
        from src.rename.review_operation import build_provider_selection_review_operation, set_review_operation
        from src.rename.review_operation import current_provider_selection_review_operation
        from tests.test_dashboard_provider_stability import ProviderRecovery
        task = ProviderRecovery().failed_task()
        _, context = current_provider_selection_review_operation(task)
        context = dict(context)
        context['provider_subject_evidence'] = {'transport_diagnostic': {
            'provider': 'bangumi', 'stage': 'detail', 'code': 'timeout', 'attempts': 2}}
        set_review_operation(task, build_provider_selection_review_operation(task, context))
        before = deepcopy(task)
        self.assertIn('Bangumi', task_feedback_text(task))
        self.assertIn('超时', task_feedback_text(task))
        self.assertNotIn('换一个作品', task_feedback_text(task))
        self.assertIn('超时', build_review_operation_presentation(task).reason_text)
        self.assertEqual(before, task)


    def test_non_timeout_and_legacy_failure_are_not_reported_as_timeout(self):
        from tests.test_dashboard_provider_stability import ProviderRecovery
        from src.rename.review_operation import build_provider_selection_review_operation, set_review_operation, current_provider_selection_review_operation
        for diagnostic in (None, {'provider': 'bangumi', 'stage': 'detail', 'code': 'connection'}):
            with self.subTest(diagnostic=diagnostic):
                task = ProviderRecovery().failed_task()
                _, context = current_provider_selection_review_operation(task)
                context = dict(context)
                context['provider_subject_evidence'] = {'transport_diagnostic': diagnostic}
                set_review_operation(task, build_provider_selection_review_operation(task, context))
                self.assertNotIn('超时', task_feedback_text(task))
                self.assertNotIn('超时', build_review_operation_presentation(task).reason_text)


class OriginalTaskRetry(unittest.IsolatedAsyncioTestCase):
    async def test_original_button_retries_same_work_preserving_inputs_and_saved_state(self):
        await self.exercise()

    async def test_movie_failure_retains_diagnostic_and_retries_original_task(self):
        await self.exercise(movie=True)

    async def exercise(self, movie=False):
        from nicegui import Client
        from nicegui.page import page as Page
        from src.pages import plan_review_page as page
        from src.rename.review_operation import current_provider_selection_review_operation
        from tests.test_default_activation_contract import DefaultActivationProducerContracts
        from tests.test_provider_season_advice import client_for
        from tests.test_selection_season_same_window import SameWindowSeasonContracts
        helper = DefaultActivationProducerContracts('runTest')
        workers = []
        async with helper.flow(automatic=False, movie=movie, is_anime=True, requested_season=None if movie else 7,
                               configure_worker=workers.append) as flow:
            bgm = client_for(platforms={'101': '剧场版'} if movie else None)
            read = bgm._get_json.side_effect
            fail = True
            def request(endpoint, **kwargs):
                if fail:
                    raise ProviderRequestError(stage='detail', code='timeout', attempts=3, elapsed=16)
                return read(endpoint, **kwargs)
            bgm._get_json.side_effect = request
            workers[0].bangumi_search = bgm
            flow.manager.executor = flow.manager._execute_rename
            client = Client(Page('/bgm-retry'), request=None)
            plan, state = helper.current(flow)
            paths = [flow.store.path_for(flow.task_id), flow.service.decision_state_store.path_for(flow.task_id)]
            saved = {p: p.read_bytes() for p in paths}
            source = {p: p.read_bytes() for p in flow.source.iterdir() if p.is_file()}
            candidate = dict(namespace='bangumi', entity_type='subject', provider_id='101',
                             title='普通动画', library_domain='animation', platform='剧场版' if movie else 'TV',
                             source_url='https://bgm.tv/subject/101')
            try:
                with client:
                    dialog = page.PlanReviewDialog(plan, flow.manager._read(flow.task_id))
                    dialog.open()
                    dialog.provider_selection_expanded = True
                    dialog._render_groups()
                    dialog.provider_url_input.set_value('https://bgm.tv/subject/101')
                    dialog.rerecognition_name.set_value('尚未提交的手动名称')
                    dialog.rerecognition_season.set_value(9)
                input_values = (dialog.rerecognition_name.value, dialog.rerecognition_season.value,
                                dialog.rerecognition_media_type.value, dialog.rerecognition_path.value)
                original_id = dialog.id
                with client:
                    await dialog._select_provider_candidate(candidate)
                task = flow.manager._read(flow.task_id)
                _, failed = current_provider_selection_review_operation(task)
                self.assertEqual('failed', failed['lifecycle'])
                self.assertEqual('101', failed['candidate']['provider_id'])
                self.assertEqual(saved, {p: p.read_bytes() for p in paths})
                self.assertEqual('https://bgm.tv/subject/101', dialog.provider_url_input.value)
                self.assertEqual('timeout', failed['provider_subject_evidence']['transport_diagnostic']['code'])
                self.assertEqual(input_values, (dialog.rerecognition_name.value, dialog.rerecognition_season.value,
                                               dialog.rerecognition_media_type.value, dialog.rerecognition_path.value))
                fail = False
                await SameWindowSeasonContracts.click(self, dialog, '重新读取资料')
                _, recovered = current_provider_selection_review_operation(flow.manager._read(flow.task_id))
                self.assertEqual('succeeded', recovered['lifecycle'])
                self.assertNotEqual(failed['request_id'], recovered['request_id'])
                self.assertEqual(original_id, dialog.id)
                result = flow.service.decision_state_store.load(flow.task_id)
                self.assertEqual('101', result.confirmed_work['primary_provider']['provider_id'])
                if not movie:
                    self.assertEqual(7, result.season_binding['season_number'])
                self.assertEqual(input_values, (dialog.rerecognition_name.value, dialog.rerecognition_season.value,
                                               dialog.rerecognition_media_type.value, dialog.rerecognition_path.value))
                self.assertEqual(source, {p: p.read_bytes() for p in source})
                self.assertFalse(list((flow.root/'records').glob('*.json')))
                self.assertFalse(any(e.target_path.exists() for e in dialog.plan.entries if e.target_path != e.source_path))
            finally:
                client.delete()
