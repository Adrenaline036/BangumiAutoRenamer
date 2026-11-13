"""已保存计划中的候选经过界面按钮提交，证据与旧计划保持完整。"""
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import tempfile
import unittest

from src.pages.plan_review_page import PlanReviewDialog, normalize_provider_candidates
from src.rename.rename_plan import RenamePlan
from src.rename.plan_store import PlanStore
from src.rename.review_operation import build_review_operation
from tests.test_series_single_side import SingleSideFlow, MEMBERS
from tests._gui_helpers import GuiDriver


class FrozenCandidateContracts(unittest.TestCase):
    def test_saved_plan_candidates_are_independent_json_snapshots(self):
        candidate = dict(namespace='bangumi', entity_type='subject', provider_id='43951',
            title='Fate/kaleid liner', match_reasons=['title'],
            provider_subject_evidence={'selected_subject_id': '43951',
                'subjects': [{'provider_id': '43951', 'aliases': ['Illya']}]})
        for payload in (candidate, [candidate]):
            with self.subTest(legacy=isinstance(payload, dict)):
                plan = RenamePlan('existing-id', Path('source'), (),
                    metadata={'provider_candidates': {'tv': payload}})
                # 和磁盘重新加载相同的版本化往返，不能只测试普通dict。
                plan = RenamePlan.from_dict(json.loads(json.dumps(plan.to_dict())))
                before = plan.to_dict()
                fingerprint = PlanStore.fingerprint(plan)
                rows = normalize_provider_candidates(dict(plan.metadata))
                copied = deepcopy(rows[0])
                self.assertEqual(dict(candidate, group_kind='tv'), copied)
                json.dumps(copied)
                rows[0]['provider_subject_evidence']['subjects'][0]['aliases'].append('changed')
                rows[0]['match_reasons'].append('changed')
                self.assertEqual(before, plan.to_dict())
                self.assertEqual(fingerprint, PlanStore.fingerprint(plan))
                self.assertEqual(dict(candidate, group_kind='tv'), copied)

    def test_plain_and_empty_inputs_keep_original_contract(self):
        value = {'provider_candidates': {'tv': [{'provider_id': '43951', 'reasons': ['a']}]}}
        before = deepcopy(value)
        self.assertEqual([dict(provider_id='43951', reasons=['a'], group_kind='tv')],
            normalize_provider_candidates(value))
        self.assertEqual(before, value)
        for value in ({}, {'provider_candidates': None}, {'provider_candidates': []}):
            self.assertEqual([], normalize_provider_candidates(value))


class CandidateButtonFlow(SingleSideFlow):
    async def select(self, task_id, selected, *, confirm=True):
        from nicegui import Client
        from nicegui.page import page
        from src.rename.provider_search import fuse_provider_candidates
        from tests.test_series_single_side import flow
        # Provider观察夹具经真实排序与Plan序列化；提交仍走原任务、原计划和真实队列。
        before = self.result(task_id)
        raw = flow._candidate(self.subjects, selected)
        ranked = fuse_provider_candidates(raw['title'], [raw], animation=True)[0]
        observation = RenamePlan(task_id, before.plan.source_root, (),
            metadata={'provider_candidates': {'work': [ranked]}})
        observation = RenamePlan.from_dict(json.loads(json.dumps(observation.to_dict())))
        self.observation = observation
        self.observation_before = observation.to_dict()
        self.before = before
        self.old_plan_payload = before.plan.to_dict()
        self.old_fingerprint = PlanStore.fingerprint(before.plan)
        rows = normalize_provider_candidates(dict(observation.metadata))
        candidate = next(row for row in rows if str(row.get('provider_id')) == selected
                         and row.get('namespace') == 'bangumi')
        client = Client(page('/frozen-provider-candidate'), request=None)
        view = SimpleNamespace(plan=before.plan, task_decision=before.state, task_data=before.task,
            client=client, close=MagicMock(), _render_groups=MagicMock(),
            _render_issues=MagicMock(), _render_entries=MagicMock(),
            _render_completion_workflow=MagicMock(), _adopt_revision=MagicMock())
        self.view = view
        async def choose(value):
            await PlanReviewDialog._select_provider_candidate(view, value, season_number=self.manual)
        view._select_provider_candidate = choose
        driver = GuiDriver(); driver.client = client
        try:
            with client, patch('src.pages.plan_review_page.notify') as notices, \
                    patch.object(self.manager, 'enqueue_reidentify', wraps=self.manager.enqueue_reidentify) as enqueue:
                PlanReviewDialog._render_provider_candidate(view, candidate)
                await driver.click('选择这个作品')
                self.notices = notices.call_args_list
                self.enqueued = enqueue.await_count
            await self.joined()
        finally:
            client.delete()
        return self.result(task_id)


class FrozenCandidateButtonContracts(unittest.IsolatedAsyncioTestCase):
    async def test_frozen_ranked_candidate_button_reaches_queue_and_saved_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with CandidateButtonFlow(Path(tmp), singleton=True, manual=7) as runtime:
                result = await runtime.add(MEMBERS[-1], confirm=False)
                self.assertFalse(any('mappingproxy' in str(call) for call in runtime.notices), runtime.notices)
                self.assertEqual(1, runtime.enqueued)
                self.assertEqual(runtime.observation_before, runtime.observation.to_dict())
                operation = build_review_operation(result.task)[0].context
                self.assertEqual('succeeded', operation.get('state'), result.task.get('error'))
                self.assertEqual(runtime.before.task_id, result.task_id)
                self.assertEqual(runtime.before.task.get('path'), result.task.get('path'))
                # 首次人工选作品会把“等待识别”更新为所选标题，任务身份保持。
                self.assertEqual(result.state.confirmed_work['title'], result.task.get('name'))
                self.assertEqual(runtime.old_plan_payload, runtime.before.plan.to_dict())
                self.assertEqual(runtime.old_fingerprint, PlanStore.fingerprint(runtime.before.plan))
                self.assertEqual(runtime.old_plan_payload,
                    runtime.manager.plan_store.load_version(result.task_id, runtime.old_fingerprint).to_dict())
                self.assertEqual(7, result.state.season_binding['season_number'])
                self.assertEqual(MEMBERS[-1], result.state.confirmed_work['primary_provider']['provider_id'])
                self.assertEqual(PlanStore.fingerprint(result.plan), operation.get('result_plan_fingerprint'))
                runtime.view._adopt_revision.assert_called_once_with(result.plan)
                runtime.assert_sources_unchanged(self)
