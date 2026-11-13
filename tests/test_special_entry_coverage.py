"""Dual special catalogs must survive first selection and later work correction."""
import json
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from src.rename.plan_store import PlanStore
from src.rename.season_reference import _reference, match_component, tmdb_snapshot
from src.rename.provider_search import BangumiProviderClient
from tests import test_initial_tv_completion_contract as tv_fixture
from tests import test_r1_real_media_production_red_contract as transport


def reference(subject_id='41658', tmdb_id='53918'):
    subjects = (dict(namespace='bangumi', entity_type='subject', provider_id=subject_id,
        title='Example', original_title='Example', platform='TV', episode_count=13,
        air_date='2013-01-03'),)
    info = dict(id=int(tmdb_id), name='Example', original_name='Example', first_air_date='2013-01-03',
        seasons=[dict(season_number=1, episode_count=13, air_date='2013-01-03')])
    tmdb = tmdb_snapshot(info)
    return _reference(subjects, (), tmdb, match_component(subjects, (), tmdb))


def raw(subject_id):
    return dict(total=2, offset=0, data=[
        dict(id=442284, subject_id=int(subject_id), type=1, sort=6.5, name='Official half episode'),
        dict(id=510001, subject_id=int(subject_id), type=1, sort=25, name='Official extra')])


class SpecialEntryCoverage(unittest.IsolatedAsyncioTestCase):
    def check_catalog(self, value, subject_id, tmdb_id):
        self.assertEqual(2, value['schema_version'], 'first/corrected Plan must retain both official catalogs')
        sources = {row['provider']['namespace']: row for row in value['catalog']}
        self.assertEqual({'bangumi', 'tmdb'}, set(sources))
        self.assertEqual(subject_id, sources['bangumi']['provider']['provider_id'])
        self.assertEqual(tmdb_id, sources['tmdb']['provider']['provider_id'])
        self.assertEqual(['6.5', '25'], [r['source_number'] for r in sources['bangumi']['rows']])
        self.assertEqual('found', value['relation_reason'])

    async def test_initial_public_selection_retains_dual_raw_specials(self):
        fixture = tv_fixture.InitialTVCompletionContracts('runTest')
        self.addCleanup(fixture.doCleanups)
        original = BangumiProviderClient._get_json
        def get(client, endpoint):
            if endpoint.startswith('/v0/episodes'):
                return raw('41658')
            return original(client, endpoint)
        with patch.object(BangumiProviderClient, '_get_json', get), \
                patch('src.rename.season_reference._bangumi_references', return_value=reference()):
            _, service, _, result, *_ = await fixture.fixture(anime=True)
        self.assertTrue(result.state.confirmed_work, result.task.get('error'))
        self.check_catalog(result.plan.metadata['tv_completion_special'], '41658', '53918')
        reopened = service.plan_store.load(result.plan.task_id)
        self.assertEqual(result.plan.metadata['tv_completion_special'], reopened.metadata['tv_completion_special'])
        self.assertFalse(service.load_or_create(reopened).approved_entry_ids)

    async def test_general_work_correction_keeps_both_catalogs_and_commits_offline(self):
        fixture = tv_fixture.InitialTVCompletionContracts('runTest')
        self.addCleanup(fixture.doCleanups)
        _, service, _, result, *_ = await fixture.fixture(anime=False)
        def get(_client, endpoint):
            if endpoint.startswith('/v0/episodes'):return raw('5337')
            if endpoint == '/v0/subjects/5337':
                return dict(id=5337, type=6, platform='欧美剧', name='Example', date='2013-01-03')
            raise AssertionError(endpoint)
        search = SimpleNamespace(get_season_info=lambda *_: dict(episodes=[dict(id=9001, episode_number=2)]))
        with patch.object(BangumiProviderClient, '_get_json', get), \
                patch('src.rename.get_info.Search', return_value=search), \
                patch('src.rename.season_reference._bangumi_references', return_value=reference('5337', '50')):
            observed = service.observe_work_correction(result.plan.task_id, PlanStore.fingerprint(result.plan),
                expected_revision=result.state.revision,
                candidate=dict(namespace='bangumi', entity_type='subject', provider_id='5337'))
        self.check_catalog(json.loads(observed.special_json), '5337', '50')
        with patch.object(BangumiProviderClient, '_get_json', side_effect=AssertionError('commit lookup')), \
                patch('src.rename.get_info.Search', side_effect=AssertionError('commit lookup')):
            intent = service.prepare_work_correction(observed, season_number=1, reason='人工更正')
            changed = service.commit_work_correction(intent)
        self.check_catalog(changed.metadata['tv_completion_special'], '5337', '50')
        self.assertFalse(service.load(changed.task_id).approved_entry_ids)

    def test_existing_explicit_dual_observation_control(self):
        from src.rename.special_crosswalk import observe
        with patch('src.rename.provider_special_evidence.bangumi_client',
                   return_value=SimpleNamespace(_get_json=lambda _: raw('41658'))), \
                patch('src.rename.get_info.Search', return_value=SimpleNamespace(
                    get_season_info=lambda *_: dict(episodes=[dict(id=9001, episode_number=2)]))), \
                patch('src.rename.season_reference._bangumi_references', return_value=reference()):
            value = observe(dict(namespace='bangumi', entity_type='subject', provider_id='41658'), 1)
        self.check_catalog(value.to_dict(), '41658', '53918')

    def test_known_relation_is_reused_without_search_and_counterpart_survives_primary_failure(self):
        from src.rename.special_crosswalk import observe
        from src.rename.special_catalog import suggestions
        bgm = SimpleNamespace(_get_json=lambda _: raw('41658'))
        tmdb = SimpleNamespace(get_season_info=lambda *_: dict(episodes=[dict(id=9001, episode_number=2)]))
        provider = dict(namespace='bangumi', entity_type='subject', provider_id='41658')
        with patch('src.rename.season_reference._bangumi_references', side_effect=AssertionError('duplicate search')) as search:
            value = observe(provider, 1, reference=reference(), bgm=bgm, tmdb=tmdb)
            self.check_catalog(value.to_dict(), '41658', '53918')
            with patch.object(bgm, '_get_json', side_effect=TimeoutError('isolated source failure')):
                partial = observe(provider, 1, reference=reference(), bgm=bgm, tmdb=tmdb)
        search.assert_not_called()
        self.assertEqual('unavailable_or_error', partial.state, 'root failure remains explicit')
        self.assertEqual('9001', partial.episodes[0]['provider_episode_id'])
        self.assertFalse(partial.to_dict()['catalog'][0]['complete'])
        self.assertEqual({}, suggestions(partial))
        with patch.object(bgm, '_get_json', side_effect=TimeoutError('Bgm failed')), \
                patch.object(tmdb, 'get_season_info', side_effect=TimeoutError('TMDB failed')):
            failed = observe(provider, 1, reference=reference(), bgm=bgm, tmdb=tmdb)
        self.assertFalse(failed.displayable)
        self.assertFalse(failed.output_candidates_available)

    async def test_failed_primary_specials_do_not_discard_available_counterpart_at_correction_or_refresh(self):
        from src.rename.provider_special_evidence import ProviderSpecialObservation
        fixture = tv_fixture.InitialTVCompletionContracts('runTest')
        self.addCleanup(fixture.doCleanups)
        _, service, _, result, *_ = await fixture.fixture(anime=False)
        def get(_client, endpoint):
            if endpoint.startswith('/v0/episodes'):raise TimeoutError('primary specials unavailable')
            if endpoint == '/v0/subjects/5337':
                return dict(id=5337, type=6, platform='欧美剧', name='Example', date='2013-01-03')
            raise AssertionError(endpoint)
        search = SimpleNamespace(get_season_info=lambda *_: dict(episodes=[dict(id=9001, episode_number=2)]))
        with patch.object(BangumiProviderClient, '_get_json', get), \
                patch('src.rename.get_info.Search', return_value=search), \
                patch('src.rename.season_reference._bangumi_references', return_value=reference('5337', '50')):
            observed = service.observe_work_correction(result.plan.task_id, PlanStore.fingerprint(result.plan),
                expected_revision=result.state.revision,
                candidate=dict(namespace='bangumi', entity_type='subject', provider_id='5337'))
        partial = ProviderSpecialObservation.from_mapping(json.loads(observed.special_json))
        self.assertEqual('unavailable_or_error', partial.state)
        self.assertTrue(partial.displayable)
        self.assertEqual('9001', partial.episodes[0]['provider_episode_id'])
        with patch.object(BangumiProviderClient, '_get_json', side_effect=AssertionError('commit lookup')):
            changed = service.commit_work_correction(service.prepare_work_correction(
                observed, season_number=1, reason='采用可用特别篇资料'))
        with patch('src.rename.special_crosswalk.observe', return_value=partial):
            refreshed = service.revise_tv_content(changed.task_id, PlanStore.fingerprint(changed),
                expected_revision=service.decision_state_store.load(changed.task_id).revision,
                mode='special_refresh', edit={})
        self.assertEqual(partial.to_dict(), refreshed.to_dict()['metadata']['tv_completion_special'])
        self.assertFalse(service.load(refreshed.task_id).approved_entry_ids)
