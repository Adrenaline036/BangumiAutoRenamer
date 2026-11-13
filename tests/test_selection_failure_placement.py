"""失败结果更新原作品卡片，进度不进入操作区或覆盖后来请求。"""
import asyncio
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from nicegui import Client, ui
from nicegui.page import page as Page
from src.pages import plan_review_page as page, provider_selection_work as work
from src.rename.review_operation import build_provider_selection_review_operation, set_review_operation


def failed_task(request='request'):
    task = dict(uuid='task', workflow_status='need_confirm', path='synthetic-source')
    attempt = dict(schema_version=1, request_id=request, state='failed',
        candidate=dict(namespace='bangumi', entity_type='subject', provider_id='43951', title='所选作品', platform='TV'),
        expected_decision_revision=1, previous_workflow_status='need_confirm',
        requested_at='2026-10-29T00:00:00+00:00', retryable=True,
        failure_code='relation_unresolved', reason_codes=['subject_candidate_evidence_missing'])
    set_review_operation(task, build_provider_selection_review_operation(task, attempt))
    return task


class FailurePlacement(unittest.IsolatedAsyncioTestCase):
    def setup_view(self, *, request='request', initial=False):
        client = Client(Page('/selection-failure'), request=None)
        self.addCleanup(client.delete)
        with client:
            body = ui.column()
            with body:
                groups = ui.column()
                footer = ui.row()
        view = SimpleNamespace(client=client, body=body, groups_container=groups,
            plan=None if initial else SimpleNamespace(task_id='task', metadata={}), task_id='task',
            task_data={'uuid': 'task'}, _render_groups=Mock())
        setattr(view, 'initial_progress' if initial else 'selection_progress', Mock(is_deleted=False))
        done = asyncio.get_running_loop().create_future(); done.set_result(None)
        pending = SimpleNamespace(task_id='task', worker=done, admitted=True, request_id='request',
            error='[作品选择] 无法匹配季度：subject_candidate_evidence_missing',
            status='无法应用作品选择：[作品选择] 无法匹配季度：subject_candidate_evidence_missing',
            result=None, route='')
        manager = SimpleNamespace(active_task_id=None, _provider_reads={'task': pending},
            _read=Mock(return_value=failed_task(request)))
        return view, pending, manager, footer

    async def test_failed_request_refreshes_persisted_card_not_raw_footer(self):
        view, pending, manager, _ = self.setup_view()
        before = deepcopy(manager._read.return_value)
        rendered = []
        view._render_groups.side_effect = lambda: rendered.append(deepcopy(view.task_data))
        with patch.object(page, 'task_queue', manager), patch.object(page, 'notify') as notice:
            await work.watch(view, pending)
        view._render_groups.assert_called_once()
        self.assertEqual([before], rendered)
        self.assertEqual(before, view.task_data)
        self.assertEqual(before, manager._read.return_value)
        self.assertFalse(any('subject_candidate_evidence_missing' in str(c) for c in view.selection_progress.set_text.call_args_list))
        self.assertFalse(any('subject_candidate_evidence_missing' in str(c) for c in notice.call_args_list))

    async def test_initial_window_uses_same_chinese_reason_near_selection(self):
        view, pending, manager, _ = self.setup_view(initial=True)
        with patch.object(page, 'task_queue', manager), patch.object(page, 'notify') as notice:
            await work.watch(view, pending)
        self.assertTrue(any('缺少部分条目的候选资料' in str(c) for c in view.initial_progress.set_text.call_args_list))
        self.assertFalse(any('subject_candidate_evidence_missing' in str(c) for c in view.initial_progress.set_text.call_args_list))
        notice.assert_not_called()
        view._render_groups.assert_not_called()

    async def test_admitted_producer_carries_persisted_request_identity(self):
        from src.rename.review_operation import current_provider_selection_review_operation
        view, pending, manager, _ = self.setup_view()
        del pending.request_id
        task = dict(uuid='task', path='synthetic-source', is_anime=True, is_movie=False)
        pending.snapshot = SimpleNamespace(task_data=deepcopy(task), plan=None,
            task_decision=SimpleNamespace(revision=1))
        pending.epoch = 1
        pending.files = []
        manager._stopping = False
        manager._admission_epoch = 1
        manager._admissions = set()
        manager._read.return_value = task
        manager._require_recognition_retry_safe = Mock()
        persisted = failed_task()
        operation = current_provider_selection_review_operation(persisted)[1]
        async def enqueue(*args, **kwargs):
            manager._read.return_value = persisted
        manager.enqueue_reidentify = AsyncMock(side_effect=enqueue)
        manager.wait_for_selection_result = AsyncMock(return_value=operation)
        candidate = dict(operation['candidate'])
        with patch.object(page, 'task_queue', manager):
            await work._run(manager, pending, AsyncMock(), False, candidate,
                'anime', task['path'], None)
        manager.enqueue_reidentify.assert_awaited_once()
        manager.wait_for_selection_result.assert_awaited_once()
        self.assertTrue(pending.error)
        self.assertEqual(operation['request_id'], getattr(pending, 'request_id', None))

    async def test_later_persisted_request_is_not_replaced_by_old_failure(self):
        view, pending, manager, _ = self.setup_view(request='later')
        original = deepcopy(view.task_data)
        with patch.object(page, 'task_queue', manager), patch.object(page, 'notify') as notice:
            await work.watch(view, pending)
        view._render_groups.assert_not_called()
        self.assertEqual(original, view.task_data)
        notice.assert_not_called()

    async def test_closed_window_does_not_present_failure(self):
        view, pending, manager, _ = self.setup_view()
        view.exit_requested = True
        with patch.object(page, 'task_queue', manager), patch.object(page, 'notify') as notice:
            await work.watch(view, pending)
        view._render_groups.assert_not_called()
        notice.assert_not_called()

    async def test_window_closed_or_work_replaced_during_read_does_not_present(self):
        for interruption in ('close', 'replace'):
            with self.subTest(interruption=interruption):
                view, pending, manager, _ = self.setup_view()
                async def read(*args):
                    if interruption == 'close':
                        view.exit_requested = True
                    else:
                        manager._provider_reads['task'] = object()
                    return failed_task()
                with patch.object(page, 'task_queue', manager), patch.object(work.asyncio, 'to_thread', side_effect=read), patch.object(page, 'notify') as notice:
                    await work.watch(view, pending)
                view._render_groups.assert_not_called()
                self.assertEqual({'uuid': 'task'}, view.task_data)
                notice.assert_not_called()

    async def test_read_failure_keeps_old_task_and_reports_locally(self):
        from src.task_queue import TaskQueueManager
        for mode in ('exception', 'empty'):
            with self.subTest(mode=mode):
                view, pending, manager, _ = self.setup_view()
                manager._read.side_effect = (OSError('synthetic disk error') if mode == 'exception'
                    else lambda task_id: TaskQueueManager._read(SimpleNamespace(task_root=Path('synthetic-task-root')), task_id))
                with patch('src.task_queue.read_task_at', side_effect=OSError('synthetic disk error')), patch.object(page, 'task_queue', manager), patch.object(page, 'notify') as notice:
                    await work.watch(view, pending)
                view._render_groups.assert_not_called()
                self.assertEqual({'uuid': 'task'}, view.task_data)
                self.assertIn('暂时无法读取', view.selection_progress.set_text.call_args.args[0])
                notice.assert_not_called()

    async def test_reopened_progress_is_mounted_in_identity_group_before_footer(self):
        from tests import test_content_workbench_gui as workbench
        holder = workbench.ContentWorkbenchGui('runTest')
        holder.create_page(cleanup_owner=self)
        view = holder.view
        _, pending, manager, _ = self.setup_view()
        view.task_data = failed_task()
        with view.client:
            view._render_groups()
        with patch.object(page, 'task_queue', manager), patch.object(page.ui, 'timer'):
            work.attach(view)
        from tests.test_review_layout import within
        self.assertTrue(within(view.selection_progress, view.selection_region))
        self.assertFalse(within(view.selection_progress, view.actions_region))
