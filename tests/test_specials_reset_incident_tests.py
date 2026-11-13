"""双侧资料与重新识别的事故合同；不接触真实媒体或网络。"""
import unittest
from copy import deepcopy
from types import SimpleNamespace
from src.rename.special_catalog import build_observation
from src.rename.special_selection import build_correspondence, validate_correspondence
from src.rename.recognition_queue_admission import _workspace_data
from src.rename.plan_execution import PlanExecutionError
from tests.test_special_catalog_contract import pair

class IncidentContracts(unittest.TestCase):
    def test_both_known_works_load_without_season_relation(self):
        value = pair().to_dict()
        observed = build_observation(value['provider_identity'], value['catalog'], relation=None)
        self.assertEqual({'bangumi', 'tmdb'}, {s['provider']['namespace'] for s in observed.to_dict()['catalog']})
        self.assertEqual('rows_available', observed.state)
        self.assertTrue(observed.displayable)
        self.assertTrue(observed.output_candidates_available)
        self.assertEqual([7], [row['episode_number'] for row in observed.episodes])
        self.assertEqual([1, 1], [len(source['rows']) for source in observed.to_dict()['catalog']])

    def test_saved_pair_survives_refreshed_rows(self):
        value = pair().to_dict()
        primary = SimpleNamespace(namespace='bangumi', entity_type='subject', provider_id='100', status='confirmed',
                                  to_dict=lambda: dict(value['provider_identity']))
        plan = SimpleNamespace(work_identity=SimpleNamespace(primary_provider=primary),
                               metadata={'provider_special_observation': value})
        decision = build_correspondence(plan, '9001', '100:1011')
        newer = deepcopy(value)
        newer['catalog'][1]['rows'][0]['title'] = '更新的标题'
        plan.metadata['provider_special_observation'] = build_observation(newer['provider_identity'], newer['catalog'],
            relation=newer['relation'], relation_reason='found').to_dict()
        self.assertEqual(7, validate_correspondence(plan, decision)['episode_number'])
        self.assertEqual(decision['tmdb_row']['title'], validate_correspondence(plan, decision)['title'])
        self.assertNotEqual('更新的标题', validate_correspondence(plan, decision)['title'])

    def test_saved_pair_tamper_and_work_change_are_rejected(self):
        value = pair().to_dict()
        primary = SimpleNamespace(namespace='bangumi', entity_type='subject', provider_id='100', status='confirmed',
                                  to_dict=lambda: dict(value['provider_identity']))
        plan = SimpleNamespace(work_identity=SimpleNamespace(primary_provider=primary),
                               metadata={'provider_special_observation': value})
        decision = build_correspondence(plan, '9001', '100:1011')
        for field, changed in (('episode_number', 99), ('provider_episode_id', '999999'), ('title', '篡改')):
            broken = deepcopy(decision)
            broken['tmdb_row'][field] = changed
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_correspondence(plan, broken)
        value['provider_identity']['provider_id'] = '999'
        with self.assertRaises(ValueError):
            validate_correspondence(plan, decision)
    def settings(self):
        return dict(path='D:/synthetic/source', preview_only=False, is_anime=False, is_movie=False,
                    name='原检索名', season_id=1), dict(path='D:/synthetic/source', preview_only=False,
                    is_anime=False, is_movie=True, custom_name='新检索名', custom_season_id=None)

    def test_manual_type_and_name_replace_confirmed_settings(self):
        current, request = self.settings()
        result = _workspace_data(current, request, SimpleNamespace(media_domain='tv'), True)
        self.assertIs(result['is_movie'], True)
        self.assertEqual('新检索名', result['name'])
        self.assertIsNone(result['season_id'])

    def test_same_settings_control(self):
        current, request = self.settings()
        request.update(is_movie=False, custom_name=current['name'], custom_season_id=1)
        result = _workspace_data(current, request, SimpleNamespace(media_domain='tv'), False)
        self.assertEqual(current['name'], result['name'])
        self.assertIs(result['is_movie'], False)

    def test_source_change_is_still_rejected(self):
        current, request = self.settings()
        request['path'] = 'D:/synthetic/another-source'
        with self.assertRaises(PlanExecutionError):
            _workspace_data(current, request, SimpleNamespace(media_domain='tv'), False)
