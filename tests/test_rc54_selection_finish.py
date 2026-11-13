"""A confirmed output season must finish its captured provider selection."""
import hashlib
import json
import unittest
import copy

from src.rename.review_operation import (build_provider_selection_review_operation,
    current_provider_selection_review_operation, set_review_operation)
from src.utils.utils import mutate_task_at, read_task_at
from tests import test_initial_tv_completion_contract as initial
from tests._media_flow_helpers import revise


class Rc54SelectionFinishContracts(unittest.IsolatedAsyncioTestCase):
    async def prepared(self):
        root, service, _, result, *_ = await initial.InitialTVCompletionContracts.fixture(
            self, anime=False, font_archive=True)
        plan = revise(service, result.plan, 'fonts')
        state = service.decision_state_store.load(plan.task_id)
        def pending(task):
            attempt = dict(state='failed', request_id='rc54-user-selection',
                expected_decision_revision=state.revision,
                evidence_fingerprint=hashlib.sha256(json.dumps(
                    state.to_dict()['provider_evidence'], sort_keys=True).encode()).hexdigest(),
                failure_code='provider_unavailable', reason_codes=['provider_unavailable'],
                retryable=False)
            set_review_operation(task, build_provider_selection_review_operation(task, attempt))
            return task
        mutate_task_at(service.task_root, plan.task_id, pending)
        return root, service, plan

    async def test_confirmed_season_finishes_existing_selection(self):
        _, service, plan = await self.prepared()
        plan = revise(service, plan, 'season', {'season_number': 2})
        task = read_task_at(service.task_root, plan.task_id)
        _, context = current_provider_selection_review_operation(task)
        self.assertEqual(2, task['season_number'])
        self.assertEqual('succeeded', context['lifecycle'])
        self.assertIsNone(context.get('failure_code'))


    async def test_finish_refuses_another_request_revision_or_evidence(self):
        from src.rename.review_operation import finish_provider_selection
        _, service, plan = await self.prepared()
        task = read_task_at(service.task_root, plan.task_id)
        expected, context = current_provider_selection_review_operation(task)
        for change in ({'request_id': 'later-request'}, {'expected_decision_revision': 999},
                       {'evidence_fingerprint': 'f' * 64}):
            with self.subTest(change=change):
                later = copy.deepcopy(task)
                set_review_operation(later, build_provider_selection_review_operation(later, {**context, **change}))
                before = copy.deepcopy(later)
                with self.assertRaises(ValueError):
                    finish_provider_selection(later, expected=expected, result_revision=1000,
                                              result_fingerprint='e' * 64)
                self.assertEqual(before, later)

    async def test_dialog_collapses_only_its_saved_selection(self):
        from tests import test_content_workbench_gui as gui
        from src.pages.tv_content_actions import revise as revise_view
        from src.rename.plan_store import PlanStore
        root, service, plan = await self.prepared()
        page = gui.ContentWorkbenchGui('runTest')
        page.create_page(prepared=(root, service, plan, []), cleanup_owner=self)
        page.view.provider_selection_expanded = True
        with page.view.client:
            await revise_view(page.view, mode='season', edit={'season_number': 2},
                expected_revision=page.view.task_decision.revision,
                expected_fingerprint=PlanStore.fingerprint(plan), notify=lambda *a, **k: None, message='完成')
        self.assertFalse(page.view.provider_selection_expanded)
        _, context = current_provider_selection_review_operation(page.view.task_data)
        self.assertEqual('succeeded', context['lifecycle'])
        self.assertEqual(2, page.view.task_data['season_number'])
