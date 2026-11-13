"""Public correction flows: provider suggestions must not veto explicit edits."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_general_season_correction_contract as seasons
from tests import test_general_work_correction_contract as works
from tests.test_content_choice_contract import records
from tests.test_content_subtitle_lifecycle import apply
from src.pages import general_season_view, work_correction_view
from src.rename.plan_review import PlanReviewError, entry_id
from src.rename.plan_store import PlanStore


class CaptureUi(seasons._CaptureUi):
    def __init__(self):
        super().__init__()
        self.inputs = {}

    def number(self, *args, **kwargs):
        node = super().number(*args, **kwargs)
        self.inputs[kwargs.get('label')] = node
        return node

    def input(self, *args, **kwargs):
        node = super().input(*args, **kwargs)
        self.inputs[kwargs.get('label')] = node
        return node

    def checkbox(self, *args, **kwargs):
        node = super().checkbox(*args, **kwargs)
        self.inputs[args[0] if args else kwargs.get('text')] = node
        return node


class ManualFirstCorrections(unittest.IsolatedAsyncioTestCase):
    flow = seasons.GeneralSeasonCorrectionContracts.flow
    current = seasons.GeneralSeasonCorrectionContracts.current


    async def test_custom_season_survives_reload_approval_and_repeated_change(self):
        async with self.current() as (flow, plan, state):
            plan = plan
            old_fingerprint = PlanStore.fingerprint(plan)
            flow.service.approve_entries(flow.task_id, old_fingerprint, [entry_id(e) for e in plan.entries])
            original_sources = {str(p): p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
            associations = state.content_selection.to_dict()['associations']
            for number in (9, 99, 2):
                state = flow.service.decision_state_store.load(flow.task_id)
                plan = flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(plan),
                    expected_revision=state.revision, mode='season', edit={'season_number': number})
                state = flow.service.decision_state_store.load(flow.task_id)
                self.assertEqual(number, state.season_binding['season_number'])
                self.assertEqual('explicit_user', state.season_binding['decision_source'])
                self.assertEqual([0, 1, 2], plan.to_dict()['metadata']['tv_completion_seasons']['available'])
                self.assertEqual(associations, state.content_selection.to_dict()['associations'])
                self.assertFalse(flow.service.load(flow.task_id).approved_entry_ids)
                for entry in plan.entries:
                    if entry.media_kind in ('video', 'subtitle') or entry.role == 'external_audio':
                        self.assertIn(f'Season {number:02d}', str(entry.target_path))
                fingerprint = PlanStore.fingerprint(plan)
                flow.service.approve_entries(flow.task_id, fingerprint, [entry_id(e) for e in plan.entries])
                accepted = flow.manager.execution_service.validate_approved_authority(flow.task_id, fingerprint)
                self.assertEqual(fingerprint, PlanStore.fingerprint(accepted))
            with self.assertRaises(Exception):
                flow.manager.execution_service.validate_approved_authority(flow.task_id, old_fingerprint)
            self.assertEqual(original_sources, {str(p): p.read_bytes() for p in flow.source.rglob('*') if p.is_file()})
            self.assertTrue(all(not e.target_path.exists() for e in plan.entries))

    async def test_registered_custom_season_control_and_stale_callback(self):
        async with self.current() as (flow, plan, state):
            ui = CaptureUi()
            owner = SimpleNamespace(plan=plan, task_decision=state, review_service=flow.service,
                general_season_container=ui.column(), exit_requested=False)
            owner._general_tv_provider = lambda: general_season_view.general_tv_provider(owner)
            owner._adopt_revision = lambda p: setattr(owner, 'plan', p)
            with patch.object(general_season_view, 'notify'), \
                    patch('src.rename.get_info.Search', side_effect=AssertionError('edit must not fetch Provider')):
                await general_season_view.change_general_tv_season(owner, ui)
                self.assertIn('自定季号', ui.inputs)
                ui.inputs['手动填写季号'].value = True
                ui.inputs['自定季号'].value = 9
                await ui.callbacks['确认季度并生成新计划']()
                await asyncio.sleep(0)
                self.assertEqual(9, owner.task_decision.season_binding['season_number'])
                before = records(flow.root)
                ui.inputs['自定季号'].value = 99
                await ui.callbacks['确认季度并生成新计划']()
                self.assertEqual(before, records(flow.root))


    async def test_work_correction_custom_season_and_no_reason_input(self):
        async with self.current() as (flow, plan, state):
            ui = CaptureUi()
            from nicegui.client import Client
            from nicegui.page import page
            client = Client(page('/manual-first-test'), request=None)
            self.addCleanup(client.delete)
            owner = SimpleNamespace(task_id=plan.task_id, client=client, plan=plan, task_decision=state,
                review_service=flow.service, work_correction_container=ui.column(), exit_requested=False)
            owner._adopt_revision = lambda p: setattr(owner, 'plan', p)
            transport = SimpleNamespace(get_season_info=lambda *_: {'episodes': []}, get_tv_info_by_id=lambda _: ('另一部真人剧集', dict(works.DETAIL)))
            with patch('src.rename.get_info.Search', return_value=transport):
                with client:
                    await work_correction_view.show(owner, dict(works.TARGET), ui=ui, notify=lambda *a, **k: None)
                    self.assertNotIn('更正理由', ui.inputs)
                    self.assertIn('自定季号', ui.inputs)
                    ui.inputs['手动填写季号'].value = True
                    ui.inputs['自定季号'].value = 9
                    await ui.callbacks['预览更正后的计划']()
                    self.assertIn('确认作品更正并生成新计划', ui.callbacks)
                    with patch.object(transport, 'get_tv_info_by_id', side_effect=AssertionError('commit cannot refetch')):
                        await ui.callbacks['确认作品更正并生成新计划']()
                    await asyncio.sleep(0)
            after = flow.service.decision_state_store.load(flow.task_id)
            self.assertEqual('5252', after.confirmed_work['primary_provider']['provider_id'])
            self.assertEqual(9, after.season_binding['season_number'])
