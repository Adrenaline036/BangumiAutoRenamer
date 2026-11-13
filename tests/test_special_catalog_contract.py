"""Raw completeness, offline scope proofs and discriminating correspondence evidence."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.rename.special_catalog import read_source, build_observation, suggestions, fingerprint
from src.rename.provider_special_evidence import ProviderSpecialObservation, ProviderSpecialEvidenceError
from src.rename.season_reference import match_component, tmdb_snapshot
from tests.test_season_reference import evidence


def pair(*, bgm_rows=None, tmdb_rows=None):
    subjects, relations, info = evidence()
    tmdb = tmdb_snapshot(info, '50')
    relation = dict(subjects=subjects, relations=relations, tmdb=tmdb,
                    assignments=match_component(subjects, relations, tmdb))
    bgm_provider = dict(namespace='bangumi', entity_type='subject', provider_id='100')
    tmdb_provider = dict(namespace='tmdb', entity_type='tv', provider_id='50')
    bgm_rows = bgm_rows if bgm_rows is not None else [dict(id=1011, subject_id=100, type=1, sort=6.5,
        name='Special title', name_cn='特别篇', airdate='2013-03-01', duration='00:24:00', desc='Official Bgm overview')]
    tmdb_rows = tmdb_rows if tmdb_rows is not None else [dict(id=9001, season_number=0, episode_number=7,
        name='特别篇', air_date='2013-03-01', runtime=24, overview='Official TMDB overview')]
    left = read_source(bgm_provider, client=SimpleNamespace(_get_json=lambda _: dict(total=len(bgm_rows), offset=0, data=bgm_rows)))
    right = read_source(tmdb_provider, client=SimpleNamespace(get_season_info=lambda *_: dict(episodes=tmdb_rows)))
    return build_observation(bgm_provider, [left, right], relation=relation, relation_reason='found')


class SpecialCatalogContracts(unittest.TestCase):
    def test_real_observation_adapter_limits_bangumi_to_selected_season(self):
        from src.rename.special_crosswalk import observe
        from src.rename.season_reference import SeasonReferences
        from src.rename.special_catalog import encode
        value = pair().to_dict()
        relation = value['relation']
        reference = SeasonReferences(tuple(relation['subjects']), tuple(map(tuple, relation['assignments'])),
                                     'found', encode(relation))
        calls = []
        def get(path):
            calls.append(path)
            self.assertIn('subject_id=100&', path)
            return dict(total=1, offset=0, data=[dict(id=1011, subject_id=100, type=1, sort=6.5)])
        tmdb = SimpleNamespace(get_season_info=lambda *_: dict(episodes=[dict(id=9001, episode_number=7)]))
        with patch('src.rename.provider_special_evidence.bangumi_client', return_value=SimpleNamespace(_get_json=get)), \
                patch('src.rename.get_info.Search', return_value=tmdb), \
                patch('src.rename.season_reference._tmdb_references', return_value=reference):
            observed = observe(dict(namespace='tmdb', entity_type='tv', provider_id='50'), 1)
        self.assertEqual(1, len(calls))
        self.assertEqual({'50', '100'}, {s['provider']['provider_id'] for s in observed.to_dict()['catalog']})
        self.assertEqual(observed.to_dict(), ProviderSpecialObservation.from_mapping(observed.to_dict()).to_dict())

    def test_adapter_failed_counterpart_does_not_erase_primary_or_mean_no_match(self):
        from src.rename.special_crosswalk import observe
        from src.rename.season_reference import SeasonReferences
        root = dict(namespace='bangumi', entity_type='subject', provider_id='100')
        bgm = SimpleNamespace(_get_json=lambda _: dict(total=1, offset=0,
            data=[dict(id=1011, subject_id=100, type=1, sort=6.5)]))
        for status in ('unavailable', 'not_found', 'ambiguous', 'incomplete', 'unsupported'):
            with self.subTest(status=status), \
                    patch('src.rename.provider_special_evidence.bangumi_client', return_value=bgm), \
                    patch('src.rename.get_info.Search', return_value=SimpleNamespace()), \
                    patch('src.rename.season_reference._bangumi_references', return_value=SeasonReferences(status=status)):
                observed = observe(root, 1)
            self.assertEqual(status, observed.to_dict()['relation_reason'])
            self.assertEqual('6.5', observed.to_dict()['catalog'][0]['rows'][0]['source_number'])
            self.assertEqual({}, suggestions(observed))

    def test_raw_roundtrip_and_relation_or_raw_mutation_rejected(self):
        observation = pair()
        value = observation.to_dict()
        self.assertEqual('6.5', value['catalog'][0]['rows'][0]['source_number'])
        self.assertEqual('Official Bgm overview', value['catalog'][0]['rows'][0]['overview'])
        self.assertEqual(value, ProviderSpecialObservation.from_mapping(value).to_dict())
        self.assertEqual(7, observation.episodes[0]['episode_number'])
        value['catalog'][0]['rows'][0]['title'] = 'tampered'
        self.assertEqual('特别篇', observation.to_dict()['catalog'][0]['rows'][0]['title'])
        with self.assertRaises(ProviderSpecialEvidenceError):
            ProviderSpecialObservation.from_mapping(value)
        value = observation.to_dict()
        value['relation']['assignments'][0][1] = 99
        value['observation_fingerprint'] = fingerprint({k: v for k, v in value.items() if k != 'observation_fingerprint'})
        with self.assertRaises(ProviderSpecialEvidenceError):
            ProviderSpecialObservation.from_mapping(value)

    def test_title_and_date_unique_pair_is_suggestion_not_number_arithmetic(self):
        self.assertEqual({'100:1011': '9001'}, suggestions(pair()))
        base = dict(id=9001, episode_number=7, name='特别篇', air_date='2013-03-01', runtime=24)
        for rows in ([{**base, 'air_date': '2014-03-01'}],
                     [{**base, 'air_date': '', 'runtime': None}],
                     [base, {**base, 'id': 9002, 'episode_number': 8}],
                     [{**base, 'name': 'Unrelated'}]):
            with self.subTest(rows=rows):
                self.assertEqual({}, suggestions(pair(tmdb_rows=rows)))

    def test_partial_root_preserves_valid_rows_and_complete_auxiliary_candidates(self):
        good = dict(id=1011, subject_id=100, type=1, sort=6.5, name='特别篇', airdate='2013-03-01')
        observation = pair(bgm_rows=[good, {**good, 'id': 1012, 'sort': True}])
        self.assertEqual('incomplete', observation.state)
        self.assertTrue(observation.displayable)
        self.assertEqual(1, len(observation.to_dict()['catalog'][0]['rows']))
        self.assertEqual(1, len(observation.episodes))
        self.assertEqual({}, suggestions(observation))
        self.assertEqual(observation.to_dict(), ProviderSpecialObservation.from_mapping(observation.to_dict()).to_dict())

    def test_all_corrupt_and_duplicate_identity_never_become_empty(self):
        good = dict(id=1011, subject_id=100, type=1, sort=6.5)
        for rows in ([{**good, 'sort': float('nan')}], [good, good],
                     [good, {**good, 'subject_id': 999}], [{**good, 'subject_id': 999}, good], [{**good, 'subject_id': 999}],
                     [{**good, 'sort': -1}], [{**good, 'sort': float('inf')}], [None]):
            with self.subTest(rows=rows):
                observation = pair(bgm_rows=rows)
                source = observation.to_dict()['catalog'][0]
                self.assertEqual('incomplete', source['state'])
                self.assertFalse(source['rows'])
                self.assertTrue(source['issues'])
                self.assertEqual({}, suggestions(observation))

    def test_missing_number_is_reference_only_and_duplicate_sort_not_duplicate_id(self):
        rows = [dict(id=1011+i, subject_id=100, type=1, sort=n, name='Special') for i, n in enumerate((None, 0, 6.5, 6.5))]
        source = pair(bgm_rows=rows).to_dict()['catalog'][0]
        self.assertTrue(source['complete'])
        self.assertEqual([None, '0', '6.5', '6.5'], [row['source_number'] for row in source['rows']])

    def test_total_and_page_boundary_are_counted_on_arrived_rows(self):
        provider = dict(namespace='bangumi', entity_type='subject', provider_id='100')
        calls = []
        def response(path):
            offset = int(path.rsplit('=', 1)[1]); calls.append(offset)
            return dict(total=1000, offset=offset, data=[dict(id=i+1, subject_id=100, type=1, sort=6.5) for i in range(offset, offset+100)])
        source = read_source(provider, client=SimpleNamespace(_get_json=response))
        self.assertTrue(source['complete'])
        self.assertEqual(1000, len(source['rows']))
        self.assertEqual(list(range(0, 1000, 100)), calls)

    def test_foreign_source_cannot_be_added_even_with_new_fingerprint(self):
        value = pair().to_dict()
        value['catalog'][1]['provider']['provider_id'] = '999'
        value['observation_fingerprint'] = fingerprint({k: v for k, v in value.items() if k != 'observation_fingerprint'})
        with self.assertRaises(ProviderSpecialEvidenceError):
            ProviderSpecialObservation.from_mapping(value)

    def test_auxiliary_failure_is_not_absence_and_cannot_autopair(self):
        value = pair().to_dict()
        source = value['catalog'][1]
        source.update(complete=False, state='unavailable_or_error', reason='unavailable', rows=[],
                      issues=[dict(provider_episode_id='', index=None, reason='unavailable')])
        observation = build_observation(value['provider_identity'], value['catalog'], relation=value['relation'], relation_reason='found')
        self.assertEqual('completed_with_rows', observation.state)
        self.assertEqual((), observation.episodes)
        self.assertEqual({}, suggestions(observation))
