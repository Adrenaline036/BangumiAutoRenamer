"""链接选择直接完成时，当前窗口采用已保存结果。"""
import tempfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import unittest
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.decision_state import DecisionStateStore
from src.rename.plan_store import PlanStore
from src.rename.review_operation import build_review_operation
from tests.test_series_single_side import SingleSideFlow, MEMBERS, flow

class LinkFlow(SingleSideFlow):
    interruption = None
    async def select(self, task_id, selected, *, confirm=True):
        before = self.result(task_id)
        candidate = flow._candidate(self.subjects, selected)
        view = SimpleNamespace(task_decision=before.state, plan=before.plan, task_data=before.task,
            provider_url_input=SimpleNamespace(value=f'https://bangumi.tv/subject/{selected}', is_deleted=False),
            client=nullcontext(), close=MagicMock(), decision_state_store=DecisionStateStore(self.decisions),
            _render_groups=MagicMock(), _render_completion_workflow=MagicMock(), _render_issues=MagicMock(),
            _render_entries=MagicMock(), _adopt_revision=MagicMock())
        self.view = view
        async def choose(value):
            await PlanReviewDialog._select_provider_candidate(view, value, season_number=self.manual)
        view._select_provider_candidate = choose
        original = self.manager.wait_for_selection_result
        async def wait(identifier, **kwargs):
            result = await original(identifier)
            if self.interruption == 'closed': view.exit_requested = True
            elif self.interruption == 'replaced': result = dict(result, request_id='another-request')
            elif self.interruption == 'fingerprint':
                from src.rename.review_operation import set_review_operation, transition_provider_selection_review_operation
                from src.utils.utils import write_task_at
                task = self.manager._read(identifier)
                set_review_operation(task, transition_provider_selection_review_operation(task,
                    {'result_plan_fingerprint': 'wrong'}))
                write_task_at(self.manager.task_root, identifier, task)
                result = dict(build_review_operation(self.manager._read(identifier))[0].context)
            return result
        with patch('src.pages.plan_review_page._read_manual_provider_url', new=AsyncMock(return_value=candidate)) as reader, \
                patch.object(self.manager, 'wait_for_selection_result', side_effect=wait):
            from src.pages.provider_selection_work import select
            await select(view, season_number=self.manual)
            reader.assert_awaited_once_with(view.provider_url_input.value)
        await self.joined()
        return self.result(task_id)

class SelectionSuccessRefresh(unittest.IsolatedAsyncioTestCase):
    async def test_link_success_updates_open_window_and_persists_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with LinkFlow(Path(tmp), singleton=True, manual=7) as runtime:
                result = await runtime.add(MEMBERS[-1], confirm=False)
                operation = build_review_operation(result.task)[0].context
                self.assertEqual('succeeded', operation.get('state'), result.task.get('error'))
                self.assertEqual(PlanStore.fingerprint(result.plan), operation.get('result_plan_fingerprint'))
                runtime.view._adopt_revision.assert_called_once_with(result.plan)
                runtime.view._render_completion_workflow.assert_called_once()
                self.assertEqual(result.state.revision, runtime.view.task_decision.revision)
                runtime.view.close.assert_not_called()
                runtime.assert_sources_unchanged(self)

    async def test_closed_replaced_or_wrong_fingerprint_does_not_adopt(self):
        for interruption in ('closed', 'replaced', 'fingerprint'):
            with self.subTest(interruption=interruption), tempfile.TemporaryDirectory() as tmp:
                async with LinkFlow(Path(tmp), singleton=True, manual=7) as runtime:
                    runtime.interruption = interruption
                    result = await runtime.add(MEMBERS[-1], confirm=False)
                    self.assertEqual('succeeded', build_review_operation(result.task)[0].context.get('state'))
                    if interruption == 'fingerprint':
                        self.assertEqual('wrong', build_review_operation(result.task)[0].context['result_plan_fingerprint'])
                    runtime.view._adopt_revision.assert_not_called()
                    runtime.view._render_completion_workflow.assert_not_called()

    async def test_link_awaiting_relation_updates_open_confirmation_area(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with LinkFlow(Path(tmp)) as runtime:
                result = await runtime.add(MEMBERS[-1], confirm=False)
                self.assertEqual('awaiting_user', build_review_operation(result.task)[0].context.get('state'))
                runtime.view._adopt_revision.assert_called_once_with(result.plan)
                runtime.view._render_completion_workflow.assert_called_once()
                runtime.view.close.assert_not_called()
