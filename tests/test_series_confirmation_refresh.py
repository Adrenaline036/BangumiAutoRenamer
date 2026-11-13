"""系列确认在同一窗口消费最新结果与已登记作品身份。"""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.pages.plan_review_page import PlanReviewDialog
from src.rename.decision_state import DecisionStateStore
from src.rename.series_catalog import SeriesCatalog
from src.rename.successor_authority import CohortAuthorityReader, CohortAuthorityDraft, SuccessorAuthorityError
from tests.test_series_single_side import SingleSideFlow, MEMBERS, flow
from tests.test_r24_yama_special_order_dependence_incident_red_contract import _apply_boundary


class VisibleFlow(SingleSideFlow):
    interruption = None

    async def select(self, task_id, selected, *, confirm=True):
        current = self.result(task_id)
        view = SimpleNamespace(task_decision=current.state, plan=current.plan, task_data=current.task,
            close=MagicMock(), decision_state_store=DecisionStateStore(self.decisions),
            _render_groups=MagicMock(), _render_completion_workflow=MagicMock(),
            _render_issues=MagicMock(), _render_entries=MagicMock(), _adopt_revision=MagicMock())
        self.view = view
        self.original_revision = current.state.revision
        wait = self.manager.wait_for_selection_result

        async def finish(identifier, **kwargs):
            result = await wait(identifier, **kwargs)
            if self.interruption == 'closed':
                view.exit_requested = True
            elif self.interruption == 'new_request':
                result['request_id'] = 'another-request'
            return result

        with patch.object(self.manager, 'wait_for_selection_result', side_effect=finish):
            await PlanReviewDialog._select_provider_candidate(view, flow._candidate(self.subjects, selected))
        await self.joined()
        return self.result(task_id)


class SeriesConfirmationContracts(unittest.IsolatedAsyncioTestCase):
    async def test_open_window_adopts_new_pending_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with VisibleFlow(Path(tmp)) as runtime:
                result = await runtime.add(MEMBERS[-1], confirm=False)
                self.assertIsNotNone(result.state.pending_tv_selection)
                self.assertEqual(runtime.original_revision + 1, result.state.revision)
                self.assertEqual(result.state.revision, runtime.view.task_decision.revision)
                runtime.view._adopt_revision.assert_called_once_with(result.plan)
                runtime.view._render_completion_workflow.assert_called_once()
                runtime.view.close.assert_not_called()

    async def test_closed_or_replaced_request_does_not_update_window(self):
        for interrupted in ('closed', 'new_request'):
            with self.subTest(interrupted=interrupted), tempfile.TemporaryDirectory() as tmp:
                async with VisibleFlow(Path(tmp)) as runtime:
                    runtime.interruption = interrupted
                    await runtime.add(MEMBERS[-1], confirm=False)
                    runtime.view._adopt_revision.assert_not_called()
                    runtime.view._render_completion_workflow.assert_not_called()

    async def test_registered_series_identity_is_used_by_cohort(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with SingleSideFlow(Path(tmp)) as runtime:
                from src.rename.review_operation import build_review_operation
                from src.rename.plan_store import PlanStore
                from src.rename.initial_tv_completion import build_initial_tv_completion
                pending_first = await runtime.add(MEMBERS[0], confirm=False)
                runtime.review.root_snapshot_source = runtime.manager.root_snapshot_source
                command = next(c for c in build_review_operation(pending_first.task)[0].commands if c.kind == 'confirm_cohort')
                intent = runtime.review.prepare_tv_boundary_completion(pending_first.task_id,
                    PlanStore.fingerprint(pending_first.plan), review_command=command.to_dict())
                built = build_initial_tv_completion(pending_first.plan, pending_first.state, intent)
                first = SimpleNamespace(state=built.decision, plan=built.draft)
                # 构造隔离的既有登记输入，不执行媒体整理。
                binding = first.state.series_binding
                record = dict(schema_version=3, series_key=binding['series_key'], revision=1,
                    canonical_root=binding['canonical_root'], canonical_display_name=first.plan.work_identity.title,
                    domain='animation', provider_evidence=[first.plan.work_identity.primary_provider.to_dict()],
                    season_bindings=[], associated_assets=[],
                    series_registration_authority=json.loads(json.dumps(first.plan.to_dict()['metadata']['series_registration_authority'])))
                catalog = SeriesCatalog(runtime.root / 'series_catalog')
                catalog._validate_namespace_record(record, binding['series_key'])
                catalog.root.mkdir(exist_ok=True)
                path = catalog.root / (binding['series_key'] + '.json')
                path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
                original = path.read_bytes()
                for key in ('title', 'name_cn'):
                    runtime.subjects[MEMBERS[0]][key] = '资料库更新后的系列标题'
                pending = await runtime.add(MEMBERS[-1], confirm=False)
                _apply_boundary(pending)
                result = runtime.result(pending.task_id)
                self.assertEqual(first.state.confirmed_work['title'], result.state.confirmed_work['title'])
                self.assertEqual(binding['canonical_root'], result.state.series_binding['canonical_root'])
                self.assertEqual(original, path.read_bytes())
                authority = next(row for row in CohortAuthorityReader.from_decision_root(runtime.decisions).store.list_current()
                    if row.source_task_id == result.task_id)
                self.assertEqual(result.state.confirmed_work, dict(authority.work_identity))
                draft = CohortAuthorityDraft(
                    boundary_decision=next(iter(result.state.provider_subject_boundary_decisions.values())),
                    work_identity={**result.state.confirmed_work, 'title': '不属于当前决定'},
                    resolution=authority.project(MEMBERS[-1])[2], provider_snapshot=authority.provider_snapshot,
                    title_selection=authority.title_selection)
                with self.assertRaisesRegex(SuccessorAuthorityError, 'authority_work_identity_mismatch'):
                    draft.bind(result.state, authority_revision=authority.authority_revision + 1)
                runtime.assert_sources_unchanged(self)
