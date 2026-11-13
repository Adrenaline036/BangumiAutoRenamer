"""Official-shape observations may enrich labels, never manufacture a match."""
from types import SimpleNamespace
import unittest

from src.rename.season_reference import (
    match_component, complete_component, _tmdb_references, _bangumi_references,
)


def subject(identity, title='Example', *, episodes=12, date='2013-01-01'):
    return dict(namespace='bangumi', entity_type='subject', provider_id=str(identity),
                title=title, original_title=title, platform='TV', episode_count=episodes, air_date=date)


def evidence():
    subjects = (subject(100), subject(101, 'Example Season 2', date='2014-01-01'))
    relations = (dict(**{'from': '100', 'to': '101'}, relation='续集'),)
    info = dict(id=50, name='Example', original_name='Example', first_air_date='2013-01-01', seasons=[
        dict(season_number=0, episode_count=99),
        dict(season_number=1, episode_count=12, air_date='2013-01-01'),
        dict(season_number=2, episode_count=12, air_date='2014-01-01')])
    return subjects, relations, info


class SeasonReferenceTests(unittest.TestCase):
    def test_known_tmdb_exact_identity_annotates_all_seasons_without_s00(self):
        subjects, relations, info = evidence()
        bgm = SimpleNamespace(search_animation=lambda *_a, **_k: [subjects[0]],
            load_crosswalk_evidence=lambda *_a, **_k: (subjects, relations))
        search = SimpleNamespace(get_tv_info_by_id=lambda _id: ('Example', info))
        result = _tmdb_references({'provider_id': '50'}, bgm, search)
        self.assertEqual({1: 'Example', 2: 'Example Season 2'}, result.names_by_season())
        with self.assertRaises(ValueError):
            _tmdb_references({'provider_id': '51'}, bgm, search)

    def test_split_cour_preserves_both_names_in_one_season(self):
        subjects, _, info = evidence()
        subjects = (subject(100, episodes=6), subject(102, 'Example Part 2', episodes=6), subjects[1])
        relations = tuple(dict(**{'from': a, 'to': b}, relation='续集') for a, b in [('100', '102'), ('102', '101')])
        bgm = SimpleNamespace(search_animation=lambda *_a, **_k: [subjects[0]],
            load_crosswalk_evidence=lambda *_a, **_k: (subjects, relations))
        result = _tmdb_references({'provider_id': '50'}, bgm,
                                 SimpleNamespace(get_tv_info_by_id=lambda _id: ('Example', info)))
        self.assertEqual('Example / Example Part 2', result.names_by_season()[1])

    def test_complete_budget_and_dangling_thirteenth_subject_never_match(self):
        subjects = tuple(subject(n) for n in range(12))
        edges = tuple(dict(**{'from': str(n), 'to': str(n + 1)}, relation='续集') for n in range(12))
        self.assertFalse(complete_component(subjects, edges))
        self.assertFalse(complete_component(subjects, edges[:-1]))
        self.assertFalse(complete_component(subjects[:2], edges[:2]))
        self.assertEqual((), match_component(subjects, edges, {}))

    def test_two_valid_components_are_ambiguous_not_first_result_wins(self):
        subjects, relations, info = evidence()
        other = tuple({**row, 'provider_id': str(int(row['provider_id']) + 100)} for row in subjects)
        bgm = SimpleNamespace(search_animation=lambda *_a, **_k: [subjects[0], other[0]],
            load_crosswalk_evidence=lambda identity, **_k: (subjects, relations) if identity == '100' else
            (other, (dict(**{'from': '200', 'to': '201'}, relation='续集'),)))
        result = _tmdb_references({'provider_id': '50'}, bgm,
                                 SimpleNamespace(get_tv_info_by_id=lambda _id: ('Example', info)))
        self.assertEqual('ambiguous', result.status)
        self.assertEqual({}, result.names_by_season())

    def test_fuzzy_or_missing_hits_do_not_become_matches(self):
        _, _, info = evidence()
        for hits in ([], [subject(9, 'Unrelated')], [subject(9, 'Example', date='1990-01-01')]):
            bgm = SimpleNamespace(search_animation=lambda *_a, **_k: hits,
                load_crosswalk_evidence=lambda *_a, **_k: (tuple(hits), ()))
            result = _tmdb_references({'provider_id': '50'}, bgm,
                                     SimpleNamespace(get_tv_info_by_id=lambda _id: ('Example', info)))
            self.assertEqual({}, result.names_by_season())

    def test_full_search_page_is_not_asserted_complete(self):
        _, _, info = evidence()
        bgm = SimpleNamespace(search_animation=lambda *_a, **_k: [subject(i) for i in range(12)])
        result = _tmdb_references({'provider_id': '50'}, bgm,
                                 SimpleNamespace(get_tv_info_by_id=lambda _id: ('Example', info)))
        self.assertEqual('incomplete', result.status)

    def test_bangumi_names_survive_optional_tmdb_failure(self):
        subjects, relations, info = evidence()
        bgm = SimpleNamespace(load_crosswalk_evidence=lambda *_a, **_k: (subjects, relations))
        def failed(*_args):
            raise ConnectionError('fixture unavailable')
        result = _bangumi_references({'provider_id': '100'}, bgm,
            SimpleNamespace(get_tv_subject_observation_info=failed))
        self.assertEqual(subjects, result.subjects)
        self.assertEqual('unavailable', result.status)
        self.assertEqual((), result.assignments)
        result = _bangumi_references({'provider_id': '100'}, bgm,
            SimpleNamespace(get_tv_subject_observation_info=lambda *_a: ('Example', info)))
        self.assertEqual((('100', 1), ('101', 2)), result.assignments)
