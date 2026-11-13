import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import tmdbsimple as tmdb

from src.rename.get_info import (
    Search,
    TMDBRequestError,
    TMDB_REQUEST_TIMEOUT,
    _search_query_variants,
    _select_search_target,
)
from src.rename.cleaner import SearchTitleCandidate
from src.rename.process import Rename


class TMDBNetworkHandlingTests(unittest.TestCase):
    @patch('src.rename.get_info.cm.get_config', return_value='test-key')
    def test_search_sets_an_explicit_tmdb_timeout(self, _mocked_config):
        with patch.object(tmdb, 'REQUESTS_TIMEOUT', None):
            Search()
            self.assertEqual(TMDB_REQUEST_TIMEOUT, tmdb.REQUESTS_TIMEOUT)

    @patch('src.rename.get_info.cm.get_config', return_value='stored-key')
    def test_runtime_tmdb_key_overrides_persisted_config(self, _mocked_config):
        with patch.dict('os.environ', {'TMDB_API_KEY': 'runtime-key'}):
            search = Search()
        self.assertEqual('runtime-key', search.TMDB_KEY)

    @patch('src.rename.get_info.sleep')
    @patch('src.rename.get_info.tmdb.Search')
    def test_tv_network_failure_retries_once_without_final_sleep(
        self,
        search_factory,
        mocked_sleep,
    ):
        search_factory.return_value.tv.side_effect = requests.ConnectionError(
            'offline'
        )
        search = object.__new__(Search)

        with self.assertRaises(TMDBRequestError):
            search.get_tv_info('Series', 0)

        self.assertEqual(2, search_factory.return_value.tv.call_count)
        mocked_sleep.assert_called_once_with(2)

    def test_numeric_anime_query_reranks_exact_tmdb_result_ahead_of_rank_one(self):
        wrong = {
            'id': 71789,
            'name': 'Eighty-Sixed',
            'original_name': 'Eighty-Sixed',
            'first_air_date': '2017-04-18',
        }
        correct = {
            'id': 100565,
            'name': '86－不存在的战区－',
            'original_name': '86―エイティシックス―',
            'first_air_date': '2021-04-11',
        }

        selected = _select_search_target(
            '86-不存在的战区-',
            [wrong, correct],
        )

        self.assertEqual(100565, selected['id'])

    def test_all_number_english_title_adds_numeric_discovery_query(self):
        self.assertEqual(
            ('EIGHTY SIX', 'EIGHTYSIX', '86'),
            tuple(_search_query_variants('EIGHTY SIX')),
        )
        self.assertNotIn('1', _search_query_variants('One Piece'))
        self.assertNotIn('1999', _search_query_variants('Class of 1999'))

    @patch('src.rename.get_info.tmdb.TV')
    @patch('src.rename.get_info.tmdb.Search')
    def test_tv_search_fetches_reranked_numeric_anime_id(
        self,
        search_factory,
        tv_factory,
    ):
        results = [
            {
                'id': 71789,
                'name': 'Eighty-Sixed',
                'original_name': 'Eighty-Sixed',
                'first_air_date': '2017-04-18',
            },
            {
                'id': 100565,
                'name': '86－不存在的战区－',
                'original_name': '86―エイティシックス―',
                'first_air_date': '2021-04-11',
            },
        ]
        fake_search = SimpleNamespace(results=results)
        fake_search.tv = Mock()
        search_factory.return_value = fake_search
        fake_tv = SimpleNamespace(id=100565)
        fake_tv.info = Mock()
        tv_factory.return_value = fake_tv
        search = object.__new__(Search)

        name, info = search.get_tv_info('86-不存在的战区-', 0)

        self.assertEqual('86－不存在的战区－', name)
        tv_factory.assert_called_once_with(100565)
        self.assertEqual(100565, info['_bar_search_alternatives'][1]['id'])

    @patch('src.rename.get_info.tmdb.TV')
    @patch('src.rename.get_info.tmdb.Search')
    def test_tv_search_uses_punctuation_variant_when_first_query_only_has_wrong_id(
        self,
        search_factory,
        tv_factory,
    ):
        wrong_search = SimpleNamespace(results=[{
            'id': 71789,
            'name': 'Eighty-Sixed',
            'original_name': 'Eighty-Sixed',
            'first_air_date': '2017-04-18',
        }])
        wrong_search.tv = Mock()
        correct_search = SimpleNamespace(results=[{
            'id': 100565,
            'name': '86－不存在的战区－',
            'original_name': '86―エイティシックス―',
            'first_air_date': '2021-04-11',
        }])
        correct_search.tv = Mock()
        search_factory.side_effect = [wrong_search, correct_search]
        fake_tv = SimpleNamespace(id=100565)
        fake_tv.info = Mock()
        tv_factory.return_value = fake_tv
        search = object.__new__(Search)

        name, info = search.get_tv_info('86-不存在的战区-', 0)

        self.assertEqual('86－不存在的战区－', name)
        tv_factory.assert_called_once_with(100565)
        self.assertEqual(
            ['86-不存在的战区-', '86 不存在的战区'],
            [
                wrong_search.tv.call_args.kwargs['query'],
                correct_search.tv.call_args.kwargs['query'],
            ],
        )
        self.assertEqual(
            ('86-不存在的战区-', '86 不存在的战区', '86不存在的战区'),
            tuple(_search_query_variants('86-不存在的战区-')),
        )
        self.assertEqual(100565, info['id'])

    @patch('src.rename.get_info.tmdb.TV')
    @patch('src.rename.get_info.tmdb.Search')
    def test_tv_subject_evidence_queries_all_variants_and_preserves_truncation(
        self,
        search_factory,
        tv_factory,
    ):
        wrong = SimpleNamespace(
            results=[{
                'id': 71789,
                'name': 'Eighty-Sixed',
                'original_name': 'Eighty-Sixed',
                'first_air_date': '2017-04-18',
            }],
            total_results=2,
            total_pages=1,
        )
        correct = SimpleNamespace(results=[{
            'id': 100565,
            'name': '86－不存在的战区－',
            'original_name': '86―エイティシックス―',
            'first_air_date': '2021-04-11',
        }])
        duplicate = SimpleNamespace(results=[dict(correct.results[0])])
        for item in (wrong, correct, duplicate):
            item.tv = Mock()
            if not hasattr(item, 'total_results'):
                item.total_results = len(item.results)
            if not hasattr(item, 'total_pages'):
                item.total_pages = 1
        search_factory.side_effect = [wrong, correct, duplicate]
        details = {item['id']: {**item, 'genres': [], 'seasons': []}
                   for item in (wrong.results[0], correct.results[0])}
        tv_factory.side_effect = lambda identity: SimpleNamespace(
            id=identity, info=Mock(return_value=details[identity]),
            alternative_titles=Mock(return_value={'results': []}))
        search = object.__new__(Search)

        name, info = search.get_tv_subject_observation_info(
            '86-不存在的战区-', 0
        )

        self.assertEqual('86－不存在的战区－', name)
        self.assertEqual(3, search_factory.call_count)
        self.assertIs(True, info['_bar_search_truncated'])
        self.assertEqual(
            {71789, 100565},
            {item['id'] for item in info['_bar_search_alternatives']},
        )

    @patch('src.rename.get_info.tmdb.TV')
    @patch('src.rename.get_info.tmdb.Search')
    def test_tv_subject_evidence_marks_aggregate_over_thirty_as_truncated(
        self,
        search_factory,
        tv_factory,
    ):
        searches = []
        for batch in range(3):
            search = SimpleNamespace(results=[{
                'id': batch * 11 + index + 1,
                'name': 'Exact Anime' if batch == 0 and index == 0 else (
                    f'Alternative {batch}-{index}'
                ),
                'original_name': '',
                'first_air_date': '2020-01-01',
            } for index in range(11)])
            search.tv = Mock()
            search.total_results = len(search.results)
            search.total_pages = 1
            searches.append(search)
        search_factory.side_effect = searches
        details = {item['id']: {**item, 'genres': [], 'seasons': []}
                   for response in searches for item in response.results}
        tv_factory.side_effect = lambda identity: SimpleNamespace(
            id=identity, info=Mock(return_value=details[identity]),
            alternative_titles=Mock(return_value={'results': []}))
        search = object.__new__(Search)

        name, info = search.get_tv_subject_observation_info(
            'Exact Anime!', 2020
        )

        self.assertEqual('Exact Anime', name)
        self.assertEqual(3, search_factory.call_count)
        self.assertIs(True, info['_bar_search_truncated'])
        self.assertEqual(30, len(info['_bar_search_alternatives']))

    @patch('src.rename.get_info.tmdb.TV')
    @patch('src.rename.get_info.tmdb.Search')
    def test_tv_search_uses_provider_aliases_for_localized_numeric_anime(
        self,
        search_factory,
        tv_factory,
    ):
        results = [
            {
                'id': 71789,
                'name': 'Eighty-Sixed',
                'original_name': 'Eighty-Sixed',
                'first_air_date': '2017-04-18',
            },
            {
                'id': 100565,
                'name': '86－不存在的战区－',
                'original_name': '86―エイティシックス―',
                'first_air_date': '2021-04-11',
            },
        ]
        fake_search = SimpleNamespace(results=results)
        fake_search.tv = Mock()
        search_factory.return_value = fake_search

        wrong_aliases = SimpleNamespace(
            alternative_titles=Mock(return_value={'results': []})
        )
        correct_aliases = SimpleNamespace(
            alternative_titles=Mock(return_value={
                'results': [{'title': '86 EIGHTY-SIX'}],
            })
        )
        detailed_tv = SimpleNamespace(id=100565)
        detailed_tv.info = Mock()
        tv_factory.side_effect = [wrong_aliases, correct_aliases, detailed_tv]
        search = object.__new__(Search)

        name, info = search.get_tv_info('EIGHTY SIX', 0)

        self.assertEqual('86－不存在的战区－', name)
        self.assertEqual(100565, info['id'])
        self.assertEqual(
            (71789, 100565, 100565),
            tuple(call.args[0] for call in tv_factory.call_args_list),
        )
        self.assertIn(
            '86 EIGHTY-SIX',
            info['_bar_search_alternative_titles'],
        )

    def test_unrelated_rank_one_result_without_year_is_rejected(self):
        renamer = object.__new__(Rename)
        name, info, issue = renamer._search_title_candidates(
            Mock(return_value=(
                'Eighty-Sixed',
                {
                    'id': 71789,
                    'name': 'Eighty-Sixed',
                    'original_name': 'Eighty-Sixed',
                    'first_air_date': '2017-04-18',
                },
            )),
            (SearchTitleCandidate('86-不存在的战区-', 0, 'clean title'),),
            '电视剧',
        )

        self.assertEqual('', name)
        self.assertIsNone(info)
        self.assertIn('置信度过低', issue)











    def test_exact_candidate_stops_without_extra_network_queries(self):
        renamer = object.__new__(Rename)
        search_method = Mock(
            return_value=(
                'Lucky Star',
                {
                    'id': 1,
                    'name': 'Lucky Star',
                    'first_air_date': '2007-04-08',
                },
            )
        )
        candidates = (
            SearchTitleCandidate('Lucky Star', 2007, 'structured'),
            SearchTitleCandidate('Lucky Star 2007', 0, 'fallback'),
        )

        name, info, issue = renamer._search_title_candidates(
            search_method,
            candidates,
            '电视剧',
        )

        self.assertEqual('Lucky Star', name)
        self.assertEqual(1, info['id'])
        self.assertIsNone(issue)
        search_method.assert_called_once_with('Lucky Star', 2007)

    def test_candidate_search_has_a_hard_limit_of_four_queries(self):
        renamer = object.__new__(Rename)
        search_method = Mock(return_value=('', None))
        candidates = tuple(
            SearchTitleCandidate(f'Candidate {index}', 0, 'fallback')
            for index in range(6)
        )

        renamer._search_title_candidates(search_method, candidates, '电视剧')

        self.assertEqual(4, search_method.call_count)

    def test_provider_rank_can_bridge_romanized_and_localized_titles(self):
        renamer = object.__new__(Rename)
        search_method = Mock(
            return_value=(
                '乱马1/2',
                {
                    'id': 57706,
                    'name': '乱马1/2',
                    'original_name': 'らんま1/2',
                    'first_air_date': '1989-04-15',
                },
            )
        )

        name, info, issue = renamer._search_title_candidates(
            search_method,
            (SearchTitleCandidate('Ranma ½', 0, 'clean metadata'),),
            '电视剧',
        )

        self.assertEqual('乱马1/2', name)
        self.assertEqual(57706, info['id'])
        self.assertIsNone(issue)

    def test_same_title_across_years_requires_an_explicit_year(self):
        renamer = object.__new__(Rename)
        search_method = Mock(
            return_value=(
                '乱马1/2',
                {
                    'id': 259140,
                    'name': '乱马1/2',
                    'original_name': 'らんま1/2',
                    'first_air_date': '2024-10-06',
                    '_bar_search_name': '乱马1/2',
                    '_bar_search_original_name': 'らんま1/2',
                    '_bar_search_alternatives': [
                        {
                            'id': 259140,
                            'name': '乱马1/2',
                            'original_name': 'らんま1/2',
                            'first_air_date': '2024-10-06',
                        },
                        {
                            'id': 57706,
                            'name': '乱马1/2',
                            'original_name': 'らんま1/2',
                            'first_air_date': '1989-04-15',
                        },
                    ],
                },
            )
        )

        name, info, issue = renamer._search_title_candidates(
            search_method,
            (SearchTitleCandidate('Ranma ½', 0, 'clean metadata'),),
            '电视剧',
        )

        self.assertEqual('', name)
        self.assertIsNone(info)
        self.assertIn('1989/2024', issue)
        self.assertEqual('乱马1/2', renamer.last_type_candidates['tv']['name'])
        self.assertEqual(
            2,
            len(renamer.last_type_candidates['tv']['info']['_bar_search_alternatives']),
        )

    def test_explicit_year_allows_same_title_provider_result(self):
        renamer = object.__new__(Rename)
        info = {
            'id': 57706,
            'name': '乱马1/2',
            'original_name': 'らんま1/2',
            'first_air_date': '1989-04-15',
            '_bar_search_alternatives': [
                {
                    'id': 57706,
                    'name': '乱马1/2',
                    'original_name': 'らんま1/2',
                    'first_air_date': '1989-04-15',
                },
                {
                    'id': 259140,
                    'name': '乱马1/2',
                    'original_name': 'らんま1/2',
                    'first_air_date': '2024-10-06',
                },
            ],
        }

        name, selected, issue = renamer._search_title_candidates(
            Mock(return_value=('乱马1/2', info)),
            (SearchTitleCandidate('Ranma ½', 1989, 'manual year'),),
            '电视剧',
        )

        self.assertEqual('乱马1/2', name)
        self.assertEqual(57706, selected['id'])
        self.assertIsNone(issue)


if __name__ == '__main__':
    unittest.main()
