"""Real default TV workspace, scoped season edit and the registered GUI action."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_default_activation_contract as activation
from tests.test_content_choice_contract import records, change
from tests.test_complex_library_special_red_contract import _CaptureUi
from src.pages import plan_review_page as page
from src.rename.plan_store import PlanStore
from src.rename.plan_review import PlanReviewService, entry_id


def season_transport(worker):
    original = worker.search.get_tv_info
    def lookup(*args):
        name, info = original(*args)
        if info and info.get('id') == 4242:
            info = {**info, 'seasons': [{'season_number': number, 'episode_count': 2}
                                      for number in (0, 1, 2)]}
        return name, info
    worker.search.get_tv_info = lookup


def companions(_root, source):
    video = next(source.glob('*S01E01*.mkv'))
    video.with_suffix('.chs.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\n测试\n', encoding='utf-8')
    video.with_suffix('.mka').write_bytes(b'isolated external audio')
    return source


class GeneralSeasonCorrectionContracts(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow

    @asynccontextmanager
    async def current(self):
        async with self.flow(configure_worker=season_transport, configure_source=companions, requested_season=1) as flow:
            service = PlanReviewService(flow.store, flow.manager.review_root,
                flow.manager.execution_service.work_catalog.root,
                decision_state_root=flow.manager.decision_state_store.root,
                task_root=flow.manager.task_root, record_root=flow.manager.execution_service.record_root,
                provider_link_catalog_root=flow.manager.provider_link_catalog_root,
                root_snapshot_source=flow.service.root_snapshot_source)
            service.series_catalog = flow.manager.execution_service.series_catalog
            flow.service = service
            plan = flow.store.load(flow.task_id)
            state = service.decision_state_store.load(flow.task_id)
            self.assertTrue(state.confirmed_work, flow.manager._read(flow.task_id).get('error'))
            self.assertEqual([0, 1, 2], plan.to_dict()['metadata']['tv_completion_seasons']['available'])
            yield flow, plan, state

    async def test_season_change_preserves_content_and_invalidates_previous_approval(self):
        async with self.current() as (flow, plan, state):
            videos = [e.source_path for e in plan.entries if e.media_kind == 'video']
            plan = change(flow.service, plan, [videos[-1]])
            state = flow.service.decision_state_store.load(flow.task_id)
            main = next(e for e in plan.entries if e.media_kind == 'video')
            plan = flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, mode='role', edit={'entry_id': entry_id(main), 'role': 'main_candidate'})
            state = flow.service.decision_state_store.load(flow.task_id)
            self.assertTrue(state.manual_role_decisions)
            relations = state.content_selection.to_dict()['associations']['relations']
            self.assertEqual({'subtitle', 'audio'}, {row['kind'] for row in relations if row['state'] == 'linked'})
            self.assertNotIn('asset_operation', plan.metadata)
            plan = flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, mode='fonts', edit={})
            state = flow.service.decision_state_store.load(flow.task_id)
            fingerprint = PlanStore.fingerprint(plan)
            flow.service.approve_entries(flow.task_id, fingerprint, [entry_id(e) for e in plan.entries])
            before_source = {str(p): p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
            peer = flow.manager.task_root / 'peer.json'
            peer.write_text('{"uuid":"peer","workflow_status":"need_confirm"}', encoding='utf-8')
            before_peer = peer.read_bytes()
            for number in (2, 0):
                updated = flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(plan),
                    expected_revision=state.revision, mode='season', edit={'season_number': number})
                after = flow.service.decision_state_store.load(flow.task_id)
                self.assertEqual(number, after.season_binding['season_number'])
                self.assertEqual('explicit_user', after.season_binding['decision_source'])
                self.assertEqual(state.confirmed_work, after.confirmed_work)
                self.assertEqual(state.content_selection.to_dict()['excluded_sources'], after.content_selection.to_dict()['excluded_sources'])
                self.assertEqual(state.content_selection.to_dict()['associations'], after.content_selection.to_dict()['associations'])
                self.assertEqual(state.manual_role_decisions, after.manual_role_decisions)
                self.assertEqual(number, flow.manager._read(flow.task_id)['season_id'])
                self.assertEqual(number, flow.manager._read(flow.task_id)['season_number'])
                self.assertEqual('draft', flow.service.load(flow.task_id).status.value)
                self.assertFalse(flow.service.load(flow.task_id).approved_entry_ids)
                self.assertTrue(all(f'Season {number:02d}' in str(e.target_path) for e in updated.entries if e.media_kind == 'video'),
                    [(number, e.role, e.season_id, str(e.target_path)) for e in updated.entries if e.media_kind == 'video'])
                self.assertTrue(all(not e.target_path.exists() for e in updated.entries))
                self.assertEqual(before_peer, peer.read_bytes())
                self.assertEqual(before_source, {str(p): p.read_bytes() for p in flow.source.rglob('*') if p.is_file()})
                plan, state = updated, after
            with self.assertRaises(Exception):
                flow.manager.execution_service.validate_approved(flow.task_id, fingerprint)

    async def test_registered_season_button_revises_without_reidentification_or_provider_lookup(self):
        async with self.current() as (flow, plan, state):
            owner = SimpleNamespace(plan=plan, task_decision=state, review_service=flow.service,
                task_data=flow.manager._read(flow.task_id), exit_requested=False)
            owner._adopt_revision = lambda value: setattr(owner, 'plan', value)
            for method in ('_general_tv_provider', '_change_general_tv_season', '_select_provider_candidate'):
                setattr(owner, method, getattr(page.PlanReviewDialog, method).__get__(owner))
            ui = _CaptureUi()
            notices = []
            with patch.object(page, 'ui', ui), \
                    patch('src.pages.general_season_view.notify', side_effect=lambda text, **kw: notices.append(text)), \
                    patch('src.rename.get_info.Search', side_effect=AssertionError('season edit must use frozen provider facts')), \
                    patch.object(flow.manager, 'enqueue_reidentify', side_effect=AssertionError('must not reidentify work')):
                page.PlanReviewDialog._render_general_tv_season(owner)
                await ui.callbacks['更改季度']()
                options, picker = ui.selects[-1]
                self.assertEqual([0, 1, 2], list(options))
                picker.value = 2
                await ui.callbacks['确认季度并生成新计划']()
                await asyncio.sleep(0)
                current_records = records(flow.root)
                picker.value = 0
                await ui.callbacks['确认季度并生成新计划']()
                self.assertEqual(current_records, records(flow.root), 'old open-window callback cannot revise a later plan')
            self.assertEqual(2, flow.service.decision_state_store.load(flow.task_id).season_binding['season_number'], notices)
            self.assertEqual(2, owner.task_decision.season_binding['season_number'])
            self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(owner.plan))

    async def test_invalid_or_stale_season_commands_do_not_change_records(self):
        async with self.current() as (flow, plan, state):
            before = records(flow.root)
            for edit in ({'season_number': True}, {'season_number': -1}, {'season_number': None},
                         {'season_number': 1.5}, {'season_number': '2'},
                         {'season_number': 2, 'provider_id': 'other'}):
                with self.subTest(edit=edit), self.assertRaises((ValueError, RuntimeError)):
                    flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(plan),
                        expected_revision=state.revision, mode='season', edit=edit)
                self.assertEqual(before, records(flow.root))
            with self.assertRaises((ValueError, RuntimeError)):
                flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(plan),
                    expected_revision=state.revision - 1, mode='season', edit={'season_number': 2})
            self.assertEqual(before, records(flow.root))
            with self.assertRaises((ValueError, RuntimeError)):
                flow.service.revise_tv_content(flow.task_id, '0' * 64,
                    expected_revision=state.revision, mode='season', edit={'season_number': 2})
            self.assertEqual(before, records(flow.root))

    async def test_drift_execution_and_disconnected_projector_cannot_publish_season(self):
        from src.utils.utils import merge_task_at
        from src.rename.tv_completion_observations import TVSeasonObservation
        for fault in ('source', 'execution', 'projector', 'provider'):
            with self.subTest(fault=fault):
                async with self.current() as (flow, plan, state):
                    if fault == 'source':
                        video = next(e.source_path for e in plan.entries if e.media_kind == 'video')
                        video.unlink()
                    if fault == 'execution':
                        merge_task_at(flow.manager.task_root, flow.task_id, {'execution_status': 'completed'})
                    before = records(flow.root)
                    if fault in {'projector', 'provider'}:
                        target, value = ('src.rename.tv_video_projection.project_tv_videos', None) if fault == 'projector' else (
                            'src.rename.tv_completion_observations.TVSeasonObservation.from_mapping',
                            TVSeasonObservation(('tmdb', 'tv', '999'), (0, 1, 2)))
                        with patch(target, return_value=value), \
                                self.assertRaises((ValueError, RuntimeError)):
                            flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(plan),
                                expected_revision=state.revision, mode='season', edit={'season_number': 2})
                    else:
                        with self.assertRaises((ValueError, RuntimeError)):
                            flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(plan),
                                expected_revision=state.revision, mode='season', edit={'season_number': 2})
                    self.assertEqual(before, records(flow.root))

    async def test_existing_explicit_extra_is_preserved_and_animation_can_change_season(self):
        async with self.current() as (flow, plan, state):
            video = next(e for e in plan.entries if e.media_kind == 'video')
            plan = flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, mode='special',
                edit={'entry_id': entry_id(video), 'action': 'unmatched_extra', 'provider_episode_id': None})
            state = flow.service.decision_state_store.load(flow.task_id)
            self.assertTrue(state.provider_special_decisions)
            updated = flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, mode='season', edit={'season_number': 2})
            after = flow.service.decision_state_store.load(flow.task_id)
            self.assertEqual(state.provider_special_decisions, after.provider_special_decisions)
            self.assertEqual('extra', next(e for e in updated.entries if e.source_path == video.source_path).presentation_asset.asset_role.kind)
        from tests import test_initial_tv_completion_contract as television
        fixture = television.InitialTVCompletionContracts('runTest')
        self.addCleanup(fixture.doCleanups)
        root, service, _, result, *_ = await fixture.fixture(anime=True, selected_season=1)
        self.assertEqual('animation', result.state.library_domain)
        revised = service.revise_tv_content(result.plan.task_id, PlanStore.fingerprint(result.plan),
            expected_revision=result.state.revision, mode='season', edit={'season_number': 2})
        after = service.decision_state_store.load(revised.task_id)
        self.assertEqual(2, after.season_binding['season_number'])
        self.assertEqual(result.state.confirmed_work, after.confirmed_work)
