"""持续刷新与既有作品选择失败的业务回归场景。"""
import asyncio
from copy import deepcopy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from nicegui import Client
from nicegui.page import page
from src.pages import data_table_page as dashboard
from src.pages.review_operation_view import build_review_operation_presentation
from src.rename.provider_search import BangumiProviderClient, ProviderSearchError
from src.rename.review_operation import build_provider_selection_review_operation, set_review_operation
from src.task_queue import TaskQueueManager
from src.utils.task_changes import mark_changed


class ListStability(unittest.IsolatedAsyncioTestCase):
    async def test_continuous_changes_publish_completed_read(self):
        client = Client(page('/continuous-list'), request=None)
        self.addCleanup(client.delete)
        calls = []
        def prepare(*args, **kwargs):
            calls.append(True)
            mark_changed()
            return [], ('snapshot',), {}
        with client, patch.object(dashboard, '_prepare_dashboard', side_effect=prepare), \
                patch.object(dashboard, '_render_dashboard', return_value=True) as render:
            await dashboard.refresh_dashboard_background(force=True)
        self.assertGreaterEqual(render.call_count, 1, '完整读取不能因持续变化而永远不显示')
        self.assertLessEqual(len(calls), 2, '一次刷新必须有界，后续变化由下一轮接续')


class ProviderRecovery(unittest.TestCase):
    def failed_task(self):
        task = {'uuid': 'retained-task', 'workflow_status': 'running', 'path': 'synthetic-source'}
        attempt = dict(schema_version=1, request_id='request', state='pending',
                       candidate={'namespace': 'bangumi', 'entity_type': 'subject',
                                  'provider_id': '1', 'title': '保留的作品', 'platform': 'TV'},
                       expected_decision_revision=1, previous_workflow_status='need_confirm',
                       requested_at='2026-10-28T00:00:00+00:00', reason_codes=[], retryable=True)
        attempt.update(state='failed', failure_code='provider_unavailable',
                       retry_requires_refresh=False, reason_codes=['provider_unavailable'])
        set_review_operation(task, build_provider_selection_review_operation(task, attempt))
        return task

    def test_existing_retryable_failure_offers_same_work_refresh_without_mutation(self):
        task = self.failed_task()
        before = deepcopy(task)
        presentation = build_review_operation_presentation(task)
        actions = {a.key: a for a in presentation.actions}
        self.assertIn('refresh', actions, '暂时失败应提供重新获取资料，而非要求换作品')
        self.assertEqual('1', actions['refresh'].payload['provider_id'])
        self.assertEqual(before, task, '读取恢复提示不得迁移或修改旧任务')

    def test_timeout_then_success_keeps_selected_request(self):
        response = Mock(status_code=200, headers={})
        response.json.return_value = {'id': 1}
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.side_effect = [requests.ReadTimeout(), response]
        client = BangumiProviderClient(user_agent='local-contract', session=session, sleeper=lambda _: None)
        self.assertEqual({'id': 1}, client._get_json('/v0/subjects/1'))
        self.assertEqual(2, session.get.call_count)

    def test_failed_selection_does_not_instruct_season_confirmation(self):
        from nicegui import ui
        from src.pages.plan_review_page import PlanReviewDialog
        client = Client(page('/failed-selection-summary'), request=None)
        self.addCleanup(client.delete)
        with client:
            view = SimpleNamespace(groups_container=ui.column(),
                _render_selection_operation=lambda: None,
                plan=SimpleNamespace(metadata={}), task_data=self.failed_task(),
                task_decision=None, provider_selection_expanded=False,
                snapshot=SimpleNamespace(work_identity={'status': 'unresolved'},
                                         presentation_groups=()))
            PlanReviewDialog._render_groups(view)
        labels = [str(getattr(element, 'text', '')) for element in client.elements.values()]
        self.assertTrue(any('保留的作品' in label for label in labels))
        self.assertFalse(any('请继续本次处理或季度确认' in label for label in labels), labels)

    def test_non_retryable_not_found_is_not_requested_twice(self):
        response = Mock(status_code=404, headers={})
        response.raise_for_status.side_effect = requests.HTTPError('not found')
        session = Mock()
        session.get.return_value = response
        client = BangumiProviderClient(user_agent='local-contract', session=session, sleeper=lambda _: None)
        with self.assertRaises(ProviderSearchError):
            client._get_json('/v0/subjects/1')
        self.assertEqual(1, session.get.call_count)

    def test_rate_limit_does_not_shorten_server_wait(self):
        response = Mock(status_code=429, headers={'Retry-After': '30'})
        response.raise_for_status.side_effect = requests.HTTPError('limited')
        session = Mock()
        session.get.return_value = response
        sleeps = []
        client = BangumiProviderClient(user_agent='local-contract', session=session, sleeper=sleeps.append)
        with self.assertRaises(ProviderSearchError):
            client._get_json('/v0/subjects/1')
        self.assertTrue(session.get.call_count == 1 or sum(sleeps) >= 30,
                        '预算不足可以停止，不能提前重试')
