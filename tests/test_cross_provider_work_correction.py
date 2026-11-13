"""Human corrections of actual queue-produced plans across provider boundaries."""
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from tests import test_initial_tv_completion_contract as tv
from tests import test_r1_real_media_production_red_contract as transport
from tests.test_content_choice_contract import records
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import tv_authority_issue
from tests import test_content_workbench_gui as content_gui


TMDB = dict(namespace='tmdb', entity_type='tv', provider_id='53918', title='向山进发',
            year=2013, source_url='https://www.themoviedb.org/tv/53918', snapshot_at='2026-09-27T00:00:00Z')
BANGUMI = dict(namespace='bangumi', entity_type='subject', provider_id='53918', title='新的动画',
               platform='TV', year=2013, source_url='https://bgm.tv/subject/53918')


def fill_manual_season(client, number):
    controls = list(client.elements.values())
    mode = next(e for e in reversed(controls) if type(e).__name__ == 'Checkbox' and e.text == '手动填写季号')
    custom = next(e for e in reversed(controls) if type(e).__name__ == 'Number' and e._props.get('label') == '自定季号')
    mode.set_value(True)
    custom.set_value(number)


class CrossProviderWorkCorrection(unittest.IsolatedAsyncioTestCase):
    fixture = tv.InitialTVCompletionContracts.fixture
    create_page = content_gui.ContentWorkbenchGui.create_page

    async def original(self, *, tmdb=False, special=False, companions=True):
        if tmdb:
            with patch.object(transport, '_candidate', return_value=TMDB):
                result = await self.fixture(anime=True, selected_season=1, exceptional=special,
                                            companions=companions, font_archive=True)
            self.assertTrue(result[3].state.confirmed_work, result[3].task.get('error'))
            return result
        return await self.fixture(anime=True, exceptional=special, companions=companions, font_archive=True)

    def observe(self, service, plan, candidate):
        state = service.decision_state_store.load(plan.task_id)
        target_id = int(candidate['provider_id'])
        search = SimpleNamespace(get_tv_info_by_id=lambda identity: ('新的动画', dict(id=identity,
            name='新的动画', original_name='New Show', first_air_date='2013-01-01',
            genres=[{'id': 16, 'name': 'Animation'}], seasons=[{'season_number': 0}, {'season_number': 2}])),
            get_season_info=lambda *_: {'episodes': [{'id': 991, 'episode_number': 1, 'name': 'New SP'}]},
            get_tv_subject_observation_info=lambda *_: ('', None))
        def get_bangumi(_client, endpoint):
            if endpoint == f'/v0/subjects/{target_id}':
                return dict(id=target_id, type=2, name='New Show', name_cn='新的动画', platform='TV', date='2013-01-01')
            if endpoint.startswith('/v0/episodes?'):
                return dict(total=1, limit=100, offset=0, data=[dict(id=992, subject_id=target_id,
                    type=1, sort=13, name='Special', name_cn='新特别篇', airdate='2013-06-01')])
            raise AssertionError(f'correction must not fetch cross-provider relationships: {endpoint}')
        with patch('src.rename.get_info.Search', return_value=search), \
                patch('src.rename.provider_search.BangumiProviderClient.search_subjects', return_value=[]), \
                patch('src.rename.provider_search.BangumiProviderClient._get_json', get_bangumi):
            return service.observe_work_correction(plan.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, candidate=candidate)

    def confirm(self, service, plan, candidate, number, *, targets_absent=True):
        observed = self.observe(service, plan, candidate)
        before = records(service.plan_store.root.parent)
        intent = service.prepare_work_correction(observed, season_number=number, reason='人工纠正资料')
        self.assertEqual(before, records(service.plan_store.root.parent), 'preview is read only')
        with patch('src.rename.get_info.Search', side_effect=AssertionError('no provider call at commit')), \
                patch('src.rename.provider_search.BangumiProviderClient._get_json', side_effect=AssertionError('no refetch')):
            result = service.commit_work_correction(intent)
        self.assertIsNone(tv_authority_issue(result))
        self.assertFalse(service.load(result.task_id).approved_entry_ids)
        if targets_absent:
            self.assertTrue(all(not e.target_path.exists() for e in result.entries))
        return result

    async def test_existing_resolution_backed_bangumi_can_switch_to_tmdb(self):
        root, service, _, result, extras, *_ = await self.original()
        plan = result.plan
        self.assertEqual('bangumi', plan.work_identity.primary_provider.namespace)
        self.assertIsNotNone(plan.metadata['tv_completion_resolution'])
        changed = self.confirm(service, plan, {**TMDB, 'provider_id': '41658'}, 2)
        self.assertEqual(('tmdb', '41658'), (changed.work_identity.primary_provider.namespace,
                                           changed.work_identity.primary_provider.provider_id))
        self.assertIsNone(changed.metadata['tv_completion_resolution'])
        self.assertNotIn('provider_link_snapshot', changed.metadata)
        self.assertEqual(result.state.content_selection.associations,
                         service.decision_state_store.load(plan.task_id).content_selection.associations)
        self.assertIn(extras['font'], changed.expected_sources)

    async def test_direct_tmdb_can_switch_same_numeric_id_to_bangumi_and_correct_season(self):
        root, service, _, result, extras, *_ = await self.original(tmdb=True)
        self.assertEqual('tmdb', result.plan.work_identity.primary_provider.namespace)
        changed = self.confirm(service, result.plan, BANGUMI, 0)
        state = service.decision_state_store.load(changed.task_id)
        self.assertEqual('bangumi', changed.work_identity.primary_provider.namespace)
        self.assertEqual(0, state.season_binding['season_number'])
        changed = service.revise_tv_content(changed.task_id, PlanStore.fingerprint(changed),
            expected_revision=state.revision, mode='season', edit={'season_number': 2})
        state = service.decision_state_store.load(changed.task_id)
        self.assertEqual(2, state.season_binding['season_number'])
        self.assertIsNone(tv_authority_issue(changed))

    async def test_old_official_special_remains_pending_then_uses_new_official_rows(self):
        root, service, _, result, extras, *_ = await self.original(special=True)
        plan = result.plan
        identifier = entry_id(next(e for e in plan.entries if e.source_path == extras['exceptional']))
        plan = service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
            expected_revision=result.state.revision, mode='special', edit={
                'entry_id': identifier, 'action': 'confirmed_special_correspondence', 'tmdb_episode_id': '9001', 'bangumi_key': None})
        changed = self.confirm(service, plan, TMDB, 2)
        special = next(e for e in changed.entries if e.source_path == extras['exceptional'])
        self.assertNotIn(special.source_path, changed.ignored_sources)
        self.assertEqual('unknown', special.presentation_asset.asset_role.kind)
        state = service.decision_state_store.load(changed.task_id)
        changed = service.revise_tv_content(changed.task_id, PlanStore.fingerprint(changed),
            expected_revision=state.revision, mode='special', edit={
                'entry_id': identifier, 'action': 'confirmed_special_correspondence', 'tmdb_episode_id': '991', 'bangumi_key': None})
        for key in ('exceptional', 'audio', 'subtitle'):
            entry = next(e for e in changed.entries if e.source_path == extras[key])
            self.assertIn('S00E01', entry.target_path.name)

    async def gui_correction(self, *, tmdb):
        root, service, _, result, extras, *_ = await self.original(tmdb=tmdb)
        target = BANGUMI if tmdb else TMDB
        observed = self.observe(service, result.plan, target)
        from src.rename.season_reference import SeasonReferences
        self.enterContext(patch('src.pages.season_reference_view.load', return_value=SeasonReferences()))
        self.create_page(prepared=(root, service, result.plan, []))
        with self.client:
            self.view._show_provider_candidates()
        # Reference-only warming may persist labels; all decisions, plans and
        # source/media bytes must still remain unchanged during the preview.
        def authority_records():
            return {key: value for key, value in records(root).items()
                    if not key.startswith('season_references/')}
        before = authority_records()
        with patch.object(service, 'observe_work_correction', return_value=observed):
            with self.client:
                await self.view._select_provider_candidate(target)
        with self.client:
            fill_manual_season(self.client, 3)
        await self.driver.click('预览更正后的计划')
        self.assertEqual(before, authority_records())
        await self.driver.click('确认作品更正并生成新计划')
        changed = service.plan_store.load(result.plan.task_id)
        self.assertEqual(target['namespace'], changed.work_identity.primary_provider.namespace, self.notices)
        self.assertEqual(3, changed.metadata['season_number'])
        await self.driver.click('更改季度')
        with self.client:
            fill_manual_season(self.client, 2)
        await self.driver.click('确认季度并生成新计划')
        self.assertEqual(2, service.plan_store.load(changed.task_id).metadata['season_number'], self.notices)

    async def test_real_bangumi_to_tmdb_dialog_and_season_correction(self):
        await self.gui_correction(tmdb=False)

    async def test_real_tmdb_to_bangumi_dialog_and_season_correction(self):
        await self.gui_correction(tmdb=True)

    async def test_bangumi_official_special_remains_fixed_during_season_change(self):
        root, service, _, result, extras, *_ = await self.original(tmdb=True, special=True)
        changed = self.confirm(service, result.plan, BANGUMI, 1)
        identifier = entry_id(next(e for e in changed.entries if e.source_path == extras['exceptional']))
        state = service.decision_state_store.load(changed.task_id)
        changed = service.revise_tv_content(changed.task_id, PlanStore.fingerprint(changed),
            expected_revision=state.revision, mode='special', edit={
                'entry_id': identifier, 'action': 'descriptive_special', 'title': '新特别篇',
                'title_source': 'official', 'reference_key': 'bangumi:53918:992'})
        state = service.decision_state_store.load(changed.task_id)
        changed = service.revise_tv_content(changed.task_id, PlanStore.fingerprint(changed),
            expected_revision=state.revision, mode='season', edit={'season_number': 3})
        for key in ('exceptional', 'audio', 'subtitle'):
            entry = next(e for e in changed.entries if e.source_path == extras[key])
            self.assertIn('Season 00', str(entry.target_path))
            self.assertIn('新特别篇', entry.target_path.name)
            self.assertNotIn('S00E13', entry.target_path.name, 'Bangumi sort is not a TMDB episode number')

    async def test_manual_bangumi_zero_registration_survives_real_fixture_execution_and_rebuild(self):
        from tests import _media_flow_helpers as execution_fixture
        from src.rename.series_registration import AFFILIATION_POLICY, validate_plan_series_registration
        root, service, _, result, extras, *_ = await self.original(tmdb=True, companions=False)
        changed = self.confirm(service, result.plan, BANGUMI, 0)
        harness = execution_fixture.MediaFlow('runTest')
        task, ledger, execution = await harness._approve_and_execute(root=root, result=SimpleNamespace(plan=changed))
        self.assertEqual('completed', task.get('execution_status'), task.get('error'))
        self.assertEqual('completed', ledger['status'])
        completed = service.plan_store.load(changed.task_id)
        self.assertEqual(AFFILIATION_POLICY, validate_plan_series_registration(completed).policy_version)
        missing = next(e.target_path for e in completed.entries if e.media_kind == 'video')
        self.assertTrue(missing.is_relative_to(root))
        missing.unlink()  # Disposable target hardlink only; source stays intact.
        rebuild = execution.rebuild_service
        view = rebuild.inspect(changed.task_id)
        fresh = rebuild.generate(changed.task_id, expected_fingerprint=view.fingerprint)
        self.assertEqual(AFFILIATION_POLICY, validate_plan_series_registration(fresh).policy_version)
        self.assertFalse(missing.exists())

    async def test_manual_registration_policy_and_stale_or_disconnected_confirmation(self):
        from dataclasses import replace
        from src.rename.series_registration import SeriesRegistrationAuthority, AFFILIATION_POLICY, POLICY
        root, service, _, result, extras, *_ = await self.original(tmdb=True)
        changed = self.confirm(service, result.plan, BANGUMI, 0)
        proof = SeriesRegistrationAuthority.from_mapping(changed.metadata['series_registration_authority'])
        self.assertEqual(AFFILIATION_POLICY, proof.policy_version)
        for raw in (None, {}, {**proof.to_dict(), 'policy_version': 'unknown'},
                    {**proof.to_dict(), 'series_key': 'tampered'}):
            with self.subTest(damaged_registration=raw):
                self.assertIsNotNone(tv_authority_issue(replace(changed,
                    metadata={**changed.metadata, 'series_registration_authority': raw})))
        for policy in (POLICY, 'series-registration-v1'):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                SeriesRegistrationAuthority.from_mapping(replace(proof, policy_version=policy).to_dict())
        before = records(root)
        for number in (-1, True):
            with self.subTest(number=number), self.assertRaises((ValueError, RuntimeError)):
                service.revise_tv_content(changed.task_id, PlanStore.fingerprint(changed),
                    expected_revision=service.decision_state_store.load(changed.task_id).revision,
                    mode='season', edit={'season_number': number})
            self.assertEqual(before, records(root))
        observed = self.observe(service, changed, TMDB)
        intent = service.prepare_work_correction(observed, season_number=2, reason='再次选择')
        with patch('src.rename.initial_tv_completion.build_initial_tv_completion', return_value=None), \
                self.assertRaises((ValueError, RuntimeError)):
            service.commit_work_correction(intent)
        self.assertEqual(before, records(root))
        state = service.decision_state_store.load(changed.task_id)
        service.revise_tv_content(changed.task_id, PlanStore.fingerprint(changed),
            expected_revision=state.revision, mode='season', edit={'season_number': 3})
        after = records(root)
        with self.assertRaises((ValueError, RuntimeError)):
            service.commit_work_correction(intent)
        self.assertEqual(after, records(root))

    async def test_manual_policy_survives_actual_replacement_and_old_source_reentry(self):
        from tests import _media_flow_helpers as execution_fixture
        from src.rename.completed_source_reentry import generate_reentry
        from src.rename.series_registration import AFFILIATION_POLICY, validate_plan_series_registration
        root, service, _, result, extras, *_ = await self.original(tmdb=True, companions=False)
        old = self.confirm(service, result.plan, BANGUMI, 1)
        harness = execution_fixture.MediaFlow('runTest')
        task, _, execution = await harness._approve_and_execute(root=root, result=SimpleNamespace(plan=old))
        self.assertEqual('completed', task.get('execution_status'), task.get('error'))
        source, _ = execution_fixture.make_season(root / 'replacement',
            archive_bytes=b'excluded archive')
        for video in source.rglob('*.mkv'):
            with video.open('ab') as stream:
                stream.write(b'new fixture media')
        incoming = await harness._run_flow(root=root, source=source,
            task_id='replacement-manual-subject', subject_id='41658')
        corrected = self.confirm(service, incoming.plan, BANGUMI, 1, targets_absent=False)
        operation, _ = service.asset_actions().prepare_replacement(corrected)
        selected = service.confirm_asset_replacement(corrected.task_id, PlanStore.fingerprint(corrected),
            expected_operation_fingerprint=operation.fingerprint)
        task, _, execution = await harness._approve_and_execute(root=root, result=SimpleNamespace(plan=selected))
        self.assertEqual('completed', task.get('execution_status'), task.get('error'))
        fresh = generate_reentry(execution.rebuild_service, execution.review_service, old.task_id)
        self.assertEqual(AFFILIATION_POLICY, validate_plan_series_registration(fresh).policy_version)
        from src.rename.plan_execution import PlanExecutionError
        # The low-level reentry producer leaves the historical review intact;
        # neither that approval nor the new unapproved revision may execute.
        for fingerprint in (PlanStore.fingerprint(old), PlanStore.fingerprint(fresh)):
            with self.assertRaises(PlanExecutionError):
                execution.validate_approved_authority(fresh.task_id, fingerprint)


class BangumiSpecialObservationBoundaries(unittest.TestCase):
    def test_complete_empty_error_truncation_wrong_owner_and_raw_fractional_sort(self):
        from src.rename.provider_special_evidence import observe_specials_for_provider
        provider = dict(namespace='bangumi', entity_type='subject', provider_id='123')
        row = dict(id=456, subject_id=123, type=1, sort=13, name='SP')
        good = dict(total=1, offset=0, data=[row])
        for payload in (None, {**good, 'total': 2}, {**good, 'offset': 1},
                        {**good, 'data': [{**row, 'subject_id': 999}]},
                        {**good, 'data': [{**row, 'type': 0}]},
                        dict(total=2, offset=0, data=[row, row])):
            with self.subTest(payload=payload):
                client = SimpleNamespace(_get_json=lambda _: payload)
                observed = observe_specials_for_provider(provider, client=client)
                partial_valid = payload == {**good, 'total': 2}
                self.assertEqual('rows_available' if partial_valid else 'awaiting_data', observed.state)
                source = observed.to_dict()['sources']['bangumi:subject:123']
                self.assertEqual('unavailable_or_error' if payload is None else 'incomplete',
                    source['last_attempt']['state'])
                self.assertFalse(source['snapshot']['complete'])
                self.assertEqual(['456'] if partial_valid else [],
                    [item['provider_episode_id'] for item in source['snapshot']['rows']])
        fractional = SimpleNamespace(_get_json=lambda _: {**good, 'data': [{**row, 'sort': 13.5}]})
        observed = observe_specials_for_provider(provider, client=fractional)
        self.assertEqual('rows_available', observed.state)
        self.assertEqual('13.5', observed.to_dict()['sources']['bangumi:subject:123']['snapshot']['rows'][0]['source_number'])
        empty = SimpleNamespace(_get_json=lambda _: dict(total=0, offset=0, data=[]))
        self.assertEqual('completed_empty', observe_specials_for_provider(provider, client=empty).state)
        client = SimpleNamespace(_get_json=lambda _: good)
        result = observe_specials_for_provider(provider, client=client)
        self.assertEqual((13, '456'), (result.episodes[0]['episode_number'], result.episodes[0]['provider_episode_id']))
