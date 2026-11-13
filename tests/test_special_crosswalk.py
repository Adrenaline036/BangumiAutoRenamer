"""Real Bangumi response shape: raw official numbering is not output numbering."""
from types import SimpleNamespace
import unittest

from src.rename.provider_special_evidence import (
    observe_specials_for_provider, ProviderSpecialObservation,
)


PROVIDER = dict(namespace='bangumi', entity_type='subject', provider_id='86670')


def observe(numbers):
    rows = [dict(id=442284+i, subject_id=86670, type=1, sort=number,
                 name='Official special', name_cn=f'特别篇{i}', airdate='2014-08-20')
            for i, number in enumerate(numbers)]
    return observe_specials_for_provider(PROVIDER, client=SimpleNamespace(
        _get_json=lambda _path: dict(total=len(rows), offset=0, data=rows)))


class SpecialRawNumberIncidents(unittest.TestCase):
    def test_fraction_does_not_erase_whole_subject(self):
        observed = observe([6.5, 25])
        self.assertEqual('completed_with_rows', observed.state)
        saved = observed.to_dict()
        self.assertEqual(2, saved['schema_version'])
        self.assertEqual(['6.5', '25'], [r['source_number'] for r in saved['catalog'][0]['rows']])
        self.assertEqual((), observed.episodes, 'Bgm raw values cannot become TMDB output numbers')
        self.assertEqual(saved, ProviderSpecialObservation.from_mapping(saved).to_dict())

    def test_zero_is_preserved_without_inventing_episode_one(self):
        observed = observe([0, 25])
        self.assertEqual('completed_with_rows', observed.state)
        self.assertEqual('0', observed.to_dict()['catalog'][0]['rows'][0]['source_number'])
        self.assertEqual((), observed.episodes)

    def test_duplicate_sort_keeps_distinct_official_ids(self):
        observed = observe([13, 13])
        self.assertEqual('completed_with_rows', observed.state)
        rows = observed.to_dict()['catalog'][0]['rows']
        self.assertEqual(2, len({r['provider_episode_id'] for r in rows}))

    def test_lone_fraction_is_valid_raw_data(self):
        observed = observe([13.5])
        self.assertEqual('completed_with_rows', observed.state)
        self.assertEqual('13.5', observed.to_dict()['catalog'][0]['rows'][0]['source_number'])
        self.assertEqual((), observed.episodes)

    def test_lone_zero_is_valid_raw_data_without_output_number(self):
        observed = observe([0])
        self.assertEqual('completed_with_rows', observed.state)
        self.assertEqual('0', observed.to_dict()['catalog'][0]['rows'][0]['source_number'])
        self.assertEqual((), observed.episodes)

    def test_duplicate_id_is_not_complete_data(self):
        row = dict(id=123, subject_id=86670, type=1, sort=13)
        observed = observe_specials_for_provider(PROVIDER, client=SimpleNamespace(
            _get_json=lambda _path: dict(total=2, offset=0, data=[row, row])))
        self.assertNotIn(observed.state, {'completed_with_rows', 'completed_empty'})

    def test_existing_positive_integer_control(self):
        observed = observe([13, 25])
        self.assertEqual('completed_with_rows', observed.state)
        self.assertEqual([13, 25], [r['episode_number'] for r in observed.episodes])

    def test_existing_tmdb_integer_control(self):
        observed = observe_specials_for_provider(
            dict(namespace='tmdb', entity_type='tv', provider_id='53918'),
            client=SimpleNamespace(get_season_info=lambda *_args: {
                'episodes': [dict(id=9001, episode_number=1, name='Official OVA')]}))
        self.assertEqual('completed_with_rows', observed.state)
        self.assertEqual([1], [r['episode_number'] for r in observed.episodes])

    def test_truncated_list_must_not_be_reported_empty(self):
        observed = observe_specials_for_provider(PROVIDER, client=SimpleNamespace(
            _get_json=lambda _path: dict(total=2, offset=0, data=[])))
        self.assertNotIn(observed.state, {'completed_empty', 'completed_with_rows'})
