"""Wrong confirmed work corrected via real registered candidate/link actions."""
import asyncio
from dataclasses import replace
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nicegui import Client
from nicegui.page import page as nice_page
from tests import test_general_season_correction_contract as season
from tests.test_complex_library_special_red_contract import _CaptureUi
from tests.test_content_choice_contract import change, records as all_records
from src.pages import plan_review_page as page
from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from src.rename.plan_execution import PlanExecutionError


TARGET = dict(namespace='tmdb', entity_type='tv', provider_id='5252', title='Wrong cached label',
    library_domain='general', year=2021, source_url='https://www.themoviedb.org/tv/5252',
    snapshot_at='2026-09-14T00:00:00+00:00')

def records(root):
    # Reference labels now also exist for general TV. They are not decisions.
    return {k:v for k,v in all_records(root).items() if 'season_references/' not in k}
DETAIL = dict(id=5252, name='另一部真人剧集', original_name='Correct Other Show',
    first_air_date='2021-01-02', genres=[{'id': 18, 'name': 'Drama'}],
    seasons=[{'season_number': 0}, {'season_number': 3}, {'season_number': 4}])


class CaptureUi(_CaptureUi):
    card = _CaptureUi.column

    def link(self, label, _url, **kwargs):
        return self.label(label, **kwargs)


class GeneralWorkCorrectionContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from src.rename.season_reference import SeasonReferences
        mocked = patch('src.pages.season_reference_view.load',return_value=SeasonReferences(status='not_found'))
        mocked.start()
        self.addCleanup(mocked.stop)
    flow = season.GeneralSeasonCorrectionContracts.flow
    current = season.GeneralSeasonCorrectionContracts.current

    @asynccontextmanager
    async def owner(self):
        async with self.current() as (flow, plan, state):
            videos = [e.source_path for e in plan.entries if e.media_kind == 'video' and e.episode_id]
            plan = change(flow.service, plan, [videos[-1]])
            state = flow.service.decision_state_store.load(flow.task_id)
            main = next(e for e in plan.entries if e.media_kind == 'video' and e.episode_id)
            plan = flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, mode='role',
                edit={'entry_id': entry_id(main), 'role': 'main_candidate'})
            from tests.test_content_subtitle_lifecycle import apply
            subtitle = next(e for e in plan.entries if e.media_kind == 'subtitle')
            plan = apply(flow.service, plan, subtitle.source_path, owner=main.source_path)
            state = flow.service.decision_state_store.load(flow.task_id)
            plan = flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, mode='fonts', edit={})
            state = flow.service.decision_state_store.load(flow.task_id)
            flow.service.approve_entries(flow.task_id, PlanStore.fingerprint(plan), [entry_id(e) for e in plan.entries])
            client = Client(nice_page('/isolated-work-correction'), request=None)
            self.addCleanup(client.delete)
            ui = CaptureUi()
            owner = SimpleNamespace(client=client, plan=plan, task_decision=state, task_id=plan.task_id,
                review_service=flow.service, task_data=flow.manager._read(flow.task_id),
                exit_requested=False, work_correction_container=ui.column())
            owner._adopt_revision = lambda value: setattr(owner, 'plan', value)
            for method in ('_select_provider_candidate', '_select_manual_provider_url', '_render_provider_candidate'):
                setattr(owner, method, getattr(page.PlanReviewDialog, method).__get__(owner))
            # A physical peer must remain byte-identical; no second Task is created.
            peer = flow.manager.task_root / 'peer.json'
            peer.write_text('{"uuid":"peer","workflow_status":"need_confirm"}', encoding='utf-8')
            yield flow, owner, ui, peer

    async def correction_flow(self, entry, *, cached_domain='general'):
        async with self.owner() as (flow, owner, ui, peer):
            before = records(flow.root)
            previous, state = owner.plan, owner.task_decision
            old_scope = flow.manager._read(flow.task_id)['task_source_scope']
            source = {str(p): p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
            peer_bytes = peer.read_bytes()
            transport = SimpleNamespace(get_tv_info_by_id=lambda _id: ('另一部真人剧集', dict(DETAIL)),
                get_season_info=lambda *_:dict(episodes=[]))
            notices = []
            with patch.object(page, 'ui', ui), patch.object(page, 'notify', side_effect=lambda text, **kw: notices.append(text)), \
                    patch.object(page, '_read_manual_provider_url', return_value={**TARGET, 'library_domain': cached_domain}), \
                    patch('src.rename.get_info.Search', return_value=transport):
                with owner.client:
                    if entry == 'candidate':
                        owner._render_provider_candidate({**TARGET, 'library_domain': cached_domain})
                        self.assertNotIn('disable', ui.button_props['选择这个作品'])
                        await ui.callbacks['选择这个作品']()
                    else:
                        page.PlanReviewDialog._render_provider_link_input(owner)
                        owner.provider_url_input.value = TARGET['source_url']
                        await ui.callbacks['读取链接并选择此作品']()
                    self.assertEqual(before, records(flow.root), 'observing a new work cannot change the old plan')
                    self.assertIn('预览更正后的计划', list(ui.callbacks), notices)
                    self.assertIn('另一部真人剧集', ' '.join(ui.visible))
                    options, picker = ui.selects[-1]
                    self.assertEqual({0, 3, 4}, set(options), 'new work must not inherit old seasons 1/2')
                    picker.value = 3
                    await ui.callbacks['预览更正后的计划']()
                    self.assertEqual(before, records(flow.root))
                    self.assertIn('确认作品更正并生成新计划', list(ui.callbacks), notices)
                    with patch.object(transport, 'get_tv_info_by_id', side_effect=AssertionError('commit cannot refetch Provider')):
                        await ui.callbacks['确认作品更正并生成新计划']()
                    await asyncio.sleep(0)
            updated = flow.store.load(flow.task_id)
            after = flow.service.decision_state_store.load(flow.task_id)
            self.assertEqual('5252', updated.work_identity.primary_provider.provider_id, notices)
            self.assertEqual('另一部真人剧集', updated.work_identity.title)
            self.assertEqual(3, after.season_binding['season_number'])
            self.assertEqual(state.content_selection.to_dict()['excluded_sources'], after.content_selection.to_dict()['excluded_sources'])
            self.assertEqual(state.content_selection.to_dict()['associations'], after.content_selection.to_dict()['associations'])
            self.assertEqual(state.manual_role_decisions, after.manual_role_decisions)
            self.assertEqual(state.initial_source_basis, after.initial_source_basis)
            for old_entry in previous.entries:
                if old_entry.subtitle:
                    fresh = next(e for e in updated.entries if e.source_path == old_entry.source_path)
                    self.assertEqual('en', fresh.subtitle.primary_language)
                    self.assertEqual(old_entry.subtitle.associated_video_source_path, fresh.subtitle.associated_video_source_path)
                    self.assertEqual(old_entry.subtitle.applied_rule_fingerprint, fresh.subtitle.applied_rule_fingerprint)
            task = flow.manager._read(flow.task_id)
            self.assertEqual(old_scope, task['task_source_scope'])
            self.assertEqual(('另一部真人剧集', 3, 3, 'explicit_user'),
                (task['name'], task['season_id'], task['season_number'], task['season_source']))
            self.assertEqual('draft', flow.service.load(flow.task_id).status.value)
            self.assertFalse(flow.service.load(flow.task_id).approved_entry_ids)
            self.assertNotEqual(PlanStore.fingerprint(previous), PlanStore.fingerprint(updated))
            self.assertEqual(peer_bytes, peer.read_bytes())
            self.assertEqual(source, {str(p): p.read_bytes() for p in flow.source.rglob('*') if p.is_file()})
            self.assertTrue(all(not e.target_path.exists() for e in updated.entries))
            for fingerprint in (PlanStore.fingerprint(previous), PlanStore.fingerprint(updated)):
                with self.assertRaises(PlanExecutionError):
                    flow.manager.execution_service.validate_approved_authority(flow.task_id, fingerprint)

    async def test_registered_candidate_corrects_confirmed_work_with_preserved_choices(self):
        await self.correction_flow('candidate')

    async def test_registered_manual_link_corrects_confirmed_work_with_preserved_choices(self):
        await self.correction_flow('link')

    async def test_stale_candidate_domain_does_not_disable_verified_target_lookup(self):
        await self.correction_flow('candidate', cached_domain='animation')

    def observation(self, flow, plan, state, *, detail=None, candidate=None):
        transport = SimpleNamespace(get_tv_info_by_id=lambda _id: ('忽略transport显示名', dict(DETAIL if detail is None else detail)),
            get_season_info=lambda *_:dict(episodes=[]))
        with patch('src.rename.get_info.Search', return_value=transport):
            return flow.service.observe_work_correction(flow.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, candidate=dict(TARGET if candidate is None else candidate))

    async def test_wrong_incomplete_or_non_general_target_never_changes_original(self):
        async with self.current() as (flow, plan, state):
            before = records(flow.root)
            for delta in ({'id': 999}, {'id': True}, {'name': ''}, {'genres': []}, {'genres': [{'name': 'Drama'}]},
                          {'genres': [{'id': 16, 'name': 'Animation'}]}, {'genre_ids': [16]}, {'genre_ids': '18'},
                          {'seasons': []}, {'seasons': [{'season_number': True}]},
                          {'seasons': [{'season_number': 3}, {'season_number': 3}]}):
                with self.subTest(delta=delta), self.assertRaises((ValueError, RuntimeError)):
                    self.observation(flow, plan, state, detail={**DETAIL, **delta})
                self.assertEqual(before, records(flow.root))
            for delta in ({'entity_type': 'movie'}, {'namespace': 'bangumi'}, {'provider_id': '4242'}):
                with self.subTest(candidate=delta), self.assertRaises((ValueError, RuntimeError)):
                    self.observation(flow, plan, state, candidate={**TARGET, **delta})
                self.assertEqual(before, records(flow.root))
            observed = self.observation(flow, plan, state)
            for number in (True, -1, None, '3', 3.5):
                with self.subTest(season=number), self.assertRaises((ValueError, RuntimeError)):
                    flow.service.prepare_work_correction(observed, season_number=number, reason='明确更正')
                self.assertEqual(before, records(flow.root))

    async def test_frozen_request_drift_and_disconnected_builder_cannot_publish(self):
        from src.utils.utils import merge_task_at
        from src.rename.initial_movie_completion import RootSnapshotSource
        for fault in ('stale', 'source', 'task', 'execution', 'config', 'builder', 'missing_request', 'wrong_seasons', 'mixed_mode'):
            with self.subTest(fault=fault):
                async with self.current() as (flow, plan, state):
                    observed = self.observation(flow, plan, state)
                    intent = flow.service.prepare_work_correction(observed, season_number=3, reason='确认另一部作品')
                    if fault == 'stale':
                        flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(plan),
                            expected_revision=state.revision, mode='season', edit={'season_number': 2})
                    elif fault == 'source':
                        source = next(flow.source.glob('*.mkv'))
                        source.write_bytes(source.read_bytes() + b'fixture drift')
                    elif fault == 'task':
                        merge_task_at(flow.manager.task_root, flow.task_id, {'name': 'later task observation'})
                    elif fault == 'execution':
                        merge_task_at(flow.manager.task_root, flow.task_id, {'execution_status': 'partial_failed'})
                    elif fault == 'config':
                        roots = flow.service.root_snapshot_source.observe()
                        flow.service.root_snapshot_source = RootSnapshotSource(lambda: roots)
                    elif fault == 'missing_request':
                        intent = replace(intent, inputs=replace(intent.inputs, work_request=None))
                    elif fault == 'wrong_seasons':
                        intent = replace(intent, inputs=replace(intent.inputs,
                            seasons=replace(intent.inputs.seasons, available=(0, 1, 2))))
                    elif fault == 'mixed_mode':
                        intent = replace(intent, inputs=replace(intent.inputs, mode='initial'))
                    before = records(flow.root)
                    if fault == 'builder':
                        with patch('src.rename.initial_tv_completion.build_initial_tv_completion', return_value=None), \
                                self.assertRaises((ValueError, RuntimeError)):
                            flow.service.commit_work_correction(intent)
                    else:
                        with self.assertRaises((ValueError, RuntimeError)):
                            flow.service.commit_work_correction(intent)
                    self.assertEqual(before, records(flow.root))

    async def test_forged_decisions_and_execution_risk_stop_before_provider(self):
        from src.rename.work_correction import _original
        from src.utils.utils import merge_task_at
        async with self.current() as (flow, plan, state):
            # User-published old choices may be corrected; forged choices that
            # disagree with the current receipt must still stop before lookup.
            for delta in ({'source_episode_projection_decision': {'decision': 'relative'}},
                          {'provider_special_decisions': {'fixture': {'action': 'confirmed_official_special',
                              'provider_namespace': 'tmdb', 'provider_entity_type': 'tv', 'provider_id': '4242',
                              'season_number': 0, 'episode_number': 1, 'provider_episode_id': '111'}}},
                          {'provider_subject_boundary_decisions': {'fixture': {'choice': 'next'}}}):
                with self.subTest(delta=delta), self.assertRaises((ValueError, RuntimeError)):
                    _original(flow.service, plan, replace(state, **delta))
            merge_task_at(flow.manager.task_root, flow.task_id, {'execution_requires_manual_check': True})
            before = records(flow.root)
            with patch('src.rename.get_info.Search', side_effect=AssertionError('unsafe task must stop before Provider')), \
                    self.assertRaises((ValueError, RuntimeError)):
                flow.service.observe_work_correction(flow.task_id, PlanStore.fingerprint(plan),
                    expected_revision=state.revision, candidate=dict(TARGET))
            self.assertEqual(before, records(flow.root))

    async def test_review_write_failure_does_not_reuse_old_approval(self):
        async with self.owner() as (flow, owner, _ui, _peer):
            old = PlanStore.fingerprint(owner.plan)
            observed = self.observation(flow, owner.plan, owner.task_decision)
            intent = flow.service.prepare_work_correction(observed, season_number=3, reason='明确更正')
            with patch.object(flow.service, '_record_revision', side_effect=OSError('isolated review write failure')), \
                    self.assertRaises(OSError):
                flow.service.commit_work_correction(intent)
            current = flow.store.load(flow.task_id)
            self.assertNotEqual(old, PlanStore.fingerprint(current))
            self.assertEqual(old, flow.service.load(flow.task_id).plan_fingerprint)
            for fingerprint in (old, PlanStore.fingerprint(current)):
                with self.assertRaises(PlanExecutionError):
                    flow.manager.execution_service.validate_approved_authority(flow.task_id, fingerprint)
            self.assertTrue(all(not e.target_path.exists() for e in current.entries))

    async def test_real_dialog_without_candidates_still_exposes_manual_link(self):
        async with self.current() as (flow, plan, _state):
            client = Client(nice_page('/isolated-no-candidates'), request=None)
            self.addCleanup(client.delete)
            with client, patch.object(page, 'normalize_provider_candidates', return_value=[]):
                dialog = page.PlanReviewDialog(plan, flow.manager._read(flow.task_id))
                try:
                    dialog._show_provider_candidates()
                    self.assertFalse(dialog.provider_url_input.is_deleted)
                    self.assertFalse(dialog.work_correction_container.is_deleted)
                finally:
                    dialog.delete()

    async def test_closed_readonly_lookup_and_missing_decision_do_not_publish(self):
        import threading
        async with self.owner() as (flow, owner, ui, _peer):
            original = flow.service.observe_work_correction
            entered, release = threading.Event(), threading.Event()
            before = records(flow.root)
            def blocked(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise ValueError('bounded observation timeout')
                return original(*args, **kwargs)
            transport = SimpleNamespace(get_tv_info_by_id=lambda _id: (DETAIL['name'], dict(DETAIL)),
                get_season_info=lambda *_:dict(episodes=[]))
            with patch.object(page, 'ui', ui), patch.object(page, 'notify'), \
                    patch.object(flow.service, 'observe_work_correction', side_effect=blocked), \
                    patch('src.rename.get_info.Search', return_value=transport):
                with owner.client:
                    task = asyncio.create_task(owner._select_provider_candidate(dict(TARGET)))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    owner.exit_requested = True
                    release.set()
                    await asyncio.wait_for(task, 5)
                finally:
                    release.set()
                    await task
            self.assertEqual(before, records(flow.root))
            self.assertNotIn('预览更正后的计划', list(ui.callbacks))
            observed = self.observation(flow, owner.plan, owner.task_decision)
            intent = flow.service.prepare_work_correction(observed, season_number=3, reason='明确更正')
            flow.service.decision_state_store.path_for(flow.task_id).unlink()
            after_missing = records(flow.root)
            with self.assertRaises((ValueError, RuntimeError)):
                flow.service.commit_work_correction(intent)
            self.assertEqual(after_missing, records(flow.root))

    async def test_refreshed_original_corrects_and_new_revision_can_be_approved(self):
        async with self.current() as (flow, plan, state):
            await flow.manager.retry_current(flow.task_id)
            await asyncio.wait_for(flow.manager._queue.join(), 10)
            refreshed = flow.store.load(flow.task_id)
            state = flow.service.decision_state_store.load(flow.task_id)
            self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(refreshed))
            observed = self.observation(flow, refreshed, state)
            intent = flow.service.prepare_work_correction(observed, season_number=3, reason='刷新后明确更正')
            revised = flow.service.commit_work_correction(intent)
            state = flow.service.decision_state_store.load(flow.task_id)
            revised = flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(revised),
                expected_revision=state.revision, mode='fonts', edit={})
            fingerprint = PlanStore.fingerprint(revised)
            flow.service.approve_entries(flow.task_id, fingerprint, [entry_id(e) for e in revised.entries])
            self.assertEqual(fingerprint, PlanStore.fingerprint(
                flow.manager.execution_service.validate_approved_authority(flow.task_id, fingerprint)))
            self.assertTrue(all(not e.target_path.exists() for e in revised.entries))

    async def test_only_issued_observation_can_prepare_or_commit(self):
        import json
        from src.rename.content_associations import encode
        async with self.current() as (flow, plan, state):
            observed = self.observation(flow, plan, state)
            forged_info = json.loads(observed.detail_json)
            forged_info['seasons'].append({'season_number': 99})
            substituted = replace(observed, detail_json=encode(forged_info))
            before = records(flow.root)
            with self.assertRaises((ValueError, RuntimeError)):
                flow.service.prepare_work_correction(substituted, season_number=99, reason='非读取资料')
            self.assertEqual(before, records(flow.root))
            intent = flow.service.prepare_work_correction(observed, season_number=3, reason='真实资料')
            for clone in (substituted, replace(observed)):
                request = replace(intent.inputs.work_request, observation=clone)
                modified = replace(intent, inputs=replace(intent.inputs, work_request=request))
                with self.subTest(stage='commit'), self.assertRaises((ValueError, RuntimeError)):
                    flow.service.commit_work_correction(modified)
                self.assertEqual(before, records(flow.root))
            from copy import copy
            other = copy(flow.service)
            with self.assertRaises((ValueError, RuntimeError)):
                other.prepare_work_correction(observed, season_number=3, reason='其它服务')
            flow.service._work_correction_observations.clear()
            with self.assertRaises((ValueError, RuntimeError)):
                flow.service.commit_work_correction(intent)
            self.assertEqual(before, records(flow.root))
