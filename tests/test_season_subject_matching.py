"""条目播出年份与完整候选证据；公开响应重放，不访问真实任务。"""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import unicodedata
import unittest
from unittest.mock import Mock, patch

import requests
from src.rename.get_info import Search, TMDBRequestError
from src.rename.process import Rename
from src.rename.provider_link_resolver import collect_subject_observations, resolve_provider_link, _candidate_from_observation

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/season_subject_illya.json').read_bytes())
IDS = ('43951', '83402', '113134', '147466')


def canonical(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


class FrozenTransport:
    """保留官方响应形状；标点变体模拟为同一响应，实网另行验证。"""
    def __init__(self):
        self.search_calls, self.detail_calls, self.alias_calls = [], [], []
        self.effective_search_calls = []
        self.detail = {63576: deepcopy(FIXTURE['tv_detail'])}
        self.aliases = {63576: {'results': deepcopy(FIXTURE['aliases'])}}
        self.override = None
        self.search_error = None
        self.alias_error = None
        self.total_pages = None
        self.metadata = {}

    def search(self):
        owner = self
        class Response:
            def tv(self, **params):
                owner.search_calls.append(deepcopy(params))
                effective = {key: value for key, value in params.items() if value is not None}
                owner.effective_search_calls.append(deepcopy(effective))
                if owner.search_error:
                    raise owner.search_error
                mode = 'year' if 'year' in effective else 'first_air_date_year' if 'first_air_date_year' in effective else 'without_year'
                matches = [r for r in FIXTURE['search_rows'] if canonical(r['query']) == canonical(params['query']) and r['filter_mode'] == mode]
                if not matches:
                    raise AssertionError('fixture has no query response')
                row = matches[0]
                values = owner.override(params) if callable(owner.override) else owner.override
                self.results = deepcopy(row['candidates'] if values is None else values)
                self.total_results = len(self.results)
                self.total_pages = owner.total_pages if owner.total_pages is not None else row['total_pages']
                self.__dict__.update(owner.metadata)
        return Response()

    def television(self, identity):
        owner = self
        class Television:
            def __init__(self):
                self.id = identity
            def info(self, **params):
                owner.detail_calls.append(identity)
                self.__dict__.update(deepcopy(owner.detail[identity]))
                return deepcopy(owner.detail[identity])
            def alternative_titles(self):
                owner.alias_calls.append(identity)
                if owner.alias_error:
                    raise owner.alias_error
                return deepcopy(owner.aliases[identity])
        return Television()

    def context(self):
        stack = ExitStack()
        stack.enter_context(patch('src.rename.get_info.tmdb.Search', self.search))
        stack.enter_context(patch('src.rename.get_info.tmdb.TV', self.television))
        stack.enter_context(patch('src.rename.get_info.logger', Mock()))
        stack.enter_context(patch('src.rename.get_info.sleep'))
        return stack


def worker():
    value = object.__new__(Rename)
    value.search = object.__new__(Search)
    value.active_provider_selection = {'snapshot_at': '2026-10-29T00:00:00+00:00'}
    return value


def subject(identifier):
    return next(row for row in FIXTURE['bangumi_subjects'] if row['provider_id'] == identifier)


def batch_for(value, selected='43951', limit=5):
    return collect_subject_observations(selected_subject_id=selected,
        bangumi_subjects=FIXTURE['bangumi_subjects'], bangumi_relations=FIXTURE['bangumi_relations'],
        search_subject=value._observe_tmdb_tv_subject, max_subjects=limit)


def resolve(batch, selected='43951'):
    return resolve_provider_link(selected_provider=dict(namespace='bangumi', entity_type='subject', provider_id=selected),
        bangumi_subjects=FIXTURE['bangumi_subjects'], bangumi_relations=FIXTURE['bangumi_relations'], subject_observations=batch)


class SeasonSubjectMatching(unittest.TestCase):
    def test_subject_search_uses_airing_year(self):
        transport = FrozenTransport()
        with transport.context():
            Search.get_tv_subject_observation_info(object.__new__(Search), subject('83402')['title'], 2014)
        self.assertTrue(transport.search_calls)
        self.assertTrue(all(p.get('year') == 2014 and 'first_air_date_year' not in p for p in transport.search_calls))

    def test_ordinary_search_keeps_first_air_year(self):
        transport = FrozenTransport()
        with transport.context():
            name, info = Search.get_tv_info(object.__new__(Search), subject('43951')['original_title'], 2013)
        self.assertEqual(1, len(transport.search_calls))
        self.assertTrue(name)
        self.assertEqual(63576, info['id'])
        self.assertTrue(all(p.get('first_air_date_year') == 2013 and 'year' not in p for p in transport.search_calls))

    def test_unknown_subject_year_applies_no_year_filter(self):
        for year in (None, 0):
            with self.subTest(year=year):
                transport = FrozenTransport()
                with transport.context():
                    name, info = Search.get_tv_subject_observation_info(object.__new__(Search), subject('43951')['original_title'], year)
                self.assertTrue(transport.effective_search_calls)
                self.assertTrue(name)
                self.assertEqual(63576, info['id'])
                self.assertTrue(all('first_air_date_year' not in p and 'year' not in p for p in transport.effective_search_calls))
        transport = FrozenTransport()
        row = deepcopy(subject('43951'))
        row['air_date'] = ''
        with transport.context():
            observation = worker()._observe_tmdb_tv_subject(row)
        self.assertEqual(2, len(observation['queries']))
        self.assertTrue(transport.effective_search_calls)
        self.assertTrue(all(q['year'] is None and q['outcome'] == 'ok' for q in observation['queries']))

    def test_four_selections_have_subject_owned_candidates_and_reviewable_mapping(self):
        expected = dict(zip(IDS, (1, 2, 3, 4)))
        for selected in IDS:
            with self.subTest(selected=selected):
                transport = FrozenTransport()
                with transport.context():
                    batch = batch_for(worker(), selected)
                for observation in batch['subject_observations']:
                    self.assertTrue(any(c['match_tier'] == 'exact' for q in observation['queries'] for c in q['candidates']), observation['bangumi_subject_id'])
                result = resolve(batch, selected)
                self.assertEqual('reviewable', result.review_disposition, result.reason_codes)
                self.assertEqual(expected, dict(result.season_assignments))
                self.assertTrue(result.requires_plan_review)

    def test_nonwinning_candidate_gets_its_own_seasons_and_can_reveal_a_tie(self):
        transport = FrozenTransport()
        base = dict(id=63576, name='魔法少女☆伊莉雅', original_name=subject('43951')['original_title'], first_air_date='2013-07-06', genre_ids=[16])
        other = dict(id=99999, name='Another work', original_name='Another work', first_air_date='2010-01-01', genre_ids=[16])
        transport.override = [base, other]
        transport.detail[99999] = {**deepcopy(transport.detail[63576]), **other}
        transport.aliases[99999] = deepcopy(transport.aliases[63576])
        with transport.context():
            observation = worker()._observe_tmdb_tv_subject(subject('83402'))
        self.assertIn(99999, transport.detail_calls)
        self.assertIn(99999, transport.alias_calls)
        candidate, problem = _candidate_from_observation(observation)
        self.assertIsNone(candidate)
        self.assertEqual(('subject_candidate_tie',), problem.reason_codes)

    def test_required_alias_failure_or_malformed_payload_does_not_become_complete(self):
        for bad in ('timeout', 'invalid'):
            with self.subTest(bad=bad):
                transport = FrozenTransport()
                if bad == 'timeout':
                    transport.alias_error = requests.ReadTimeout('synthetic')
                else:
                    transport.aliases[63576] = {'results': 'not-a-list'}
                with transport.context():
                    observation = worker()._observe_tmdb_tv_subject(subject('43951'))
                self.assertTrue(transport.alias_calls)
                self.assertEqual(2, len(observation['queries']))
                expected = 'transport_error' if bad == 'timeout' else 'invalid_response'
                self.assertTrue(all(q['outcome'] == expected and not q['candidates'] for q in observation['queries']))
                candidate, problem = _candidate_from_observation(observation)
                self.assertIsNone(candidate)
                self.assertEqual(('subject_candidate_' + expected,), problem.reason_codes)
                self.assertEqual('manual_required' if bad == 'timeout' else 'blocked', problem.review_disposition)

    def test_detail_identity_mismatch_is_rejected(self):
        transport = FrozenTransport()
        transport.detail[63576]['id'] = 99999
        with transport.context(), self.assertRaises(TMDBRequestError):
            Search.get_tv_subject_observation_info(object.__new__(Search), subject('43951')['original_title'], 2013)

    def test_special_counterpart_retains_search_titles_when_detail_is_localized(self):
        from src.rename.special_crosswalk import _counterpart
        row = subject('43951')
        transport = FrozenTransport()
        transport.detail[63576].update(name='Canonical localized title', original_name='Canonical original title')
        transport.override = [dict(id=63576, name=row['title'], original_name=row['original_title'],
            first_air_date='2013-07-06', genre_ids=[16])]
        bangumi = Mock()
        bangumi._get_json.return_value = dict(name=row['original_title'], name_cn=row['title'], date=row['air_date'])
        with transport.context():
            hits, state = _counterpart(dict(namespace='bangumi', entity_type='subject', provider_id='43951'),
                bangumi, object.__new__(Search))
        self.assertEqual('found', state)
        self.assertEqual((dict(namespace='tmdb', entity_type='tv', provider_id='63576'),), hits)

    def test_invalid_pagination_metadata_cannot_claim_complete_evidence(self):
        for metadata in ({'total_pages': None}, {'total_pages': '2'}, {'total_pages': False},
                         {'total_results': -1}, {'total_results': 0}, {'total_pages': 0}):
            with self.subTest(metadata=metadata):
                transport = FrozenTransport()
                transport.metadata = metadata
                with transport.context():
                    observation = worker()._observe_tmdb_tv_subject(subject('43951'))
                self.assertEqual(2, len(observation['queries']))
                self.assertTrue(all(q['outcome'] == 'invalid_response' and not q['candidates'] for q in observation['queries']))
                candidate, problem = _candidate_from_observation(observation)
                self.assertIsNone(candidate)
                self.assertEqual(('subject_candidate_invalid_response',), problem.reason_codes)

    def test_candidate_budget_and_provider_pagination_remain_incomplete(self):
        for case in ('budget', 'pages'):
            with self.subTest(case=case):
                transport = FrozenTransport()
                base = dict(id=63576, name='魔法少女☆伊莉雅', original_name=subject('43951')['original_title'], first_air_date='2013-07-06', genre_ids=[16])
                if case == 'budget':
                    transport.override = [{**base, 'id':63576+i} for i in range(9)]
                    for i in range(1, 9):
                        transport.detail[63576+i] = {**deepcopy(transport.detail[63576]), 'id':63576+i}
                        transport.aliases[63576+i] = deepcopy(transport.aliases[63576])
                else:
                    transport.total_pages = 2
                with transport.context():
                    _, info = Search.get_tv_subject_observation_info(object.__new__(Search), subject('43951')['original_title'], 2013)
                self.assertTrue(info['_bar_search_truncated'])
                self.assertLessEqual(len(set(transport.detail_calls)), 8)
                self.assertLessEqual(len(set(transport.alias_calls)), 8)

    def test_no_match_and_network_failure_never_create_candidates(self):
        for failure in (False, True):
            transport = FrozenTransport()
            transport.override = []
            if failure:
                transport.search_error = requests.ReadTimeout('synthetic')
            with transport.context():
                observation = worker()._observe_tmdb_tv_subject(subject('43951'))
            self.assertTrue(all(not q['candidates'] for q in observation['queries']))
            self.assertEqual({'transport_error' if failure else 'no_match'}, {q['outcome'] for q in observation['queries']})

    def test_legacy_batch_remains_unchanged_and_missing_or_tampered_evidence_is_rejected(self):
        legacy = deepcopy(FIXTURE['legacy_batch'])
        before = deepcopy(legacy)
        result = resolve(legacy)
        self.assertEqual(('subject_candidate_evidence_missing',), result.reason_codes)
        self.assertEqual(before, legacy)
        transport = FrozenTransport()
        with transport.context():
            short = batch_for(worker(), limit=1)
        self.assertNotEqual('reviewable', resolve(short).review_disposition)
        tampered = deepcopy(legacy)
        tampered['selected_subject_id'] = '147466'
        self.assertEqual(('subject_evidence_fingerprint_mismatch',), resolve(tampered).reason_codes)

    def test_new_query_policy_is_recorded_and_bound_by_fingerprint(self):
        transport = FrozenTransport()
        with transport.context():
            batch = batch_for(worker())
        self.assertEqual(4, len(batch['subject_observations']))
        for observation in batch['subject_observations']:
            self.assertEqual(2, len(observation['queries']))
            self.assertTrue(all(q.get('query_policy') == 'tmdb-subject-airing-year-v1' for q in observation['queries']))
        tampered = deepcopy(batch)
        tampered['subject_observations'][0]['queries'][0]['query_policy'] = 'changed'
        self.assertEqual(('subject_evidence_fingerprint_mismatch',), resolve(tampered).reason_codes)

    def test_incompatible_counts_or_dates_do_not_become_reviewable(self):
        for field in ('count', 'date'):
            with self.subTest(field=field):
                transport = FrozenTransport()
                for season in transport.detail[63576]['seasons']:
                    if season['season_number'] > 0:
                        season['episode_count' if field == 'count' else 'air_date'] = 99 if field == 'count' else '2099-01-01'
                with transport.context():
                    batch = batch_for(worker())
                self.assertNotEqual('reviewable', resolve(batch).review_disposition)

    def test_legacy_explicit_no_match_review_mapping_is_preserved(self):
        observations = {r['bangumi_subject_id']: r for r in FIXTURE['legacy_batch']['subject_observations']}
        before = deepcopy(observations)
        for selected in IDS[1:]:
            batch = collect_subject_observations(selected_subject_id=selected,
                bangumi_subjects=FIXTURE['bangumi_subjects'], bangumi_relations=FIXTURE['bangumi_relations'],
                search_subject=lambda row: deepcopy(observations[row['provider_id']]), max_subjects=5)
            result = resolve(batch, selected)
            self.assertEqual('reviewable', result.review_disposition)
            self.assertTrue(result.requires_plan_review)
            self.assertEqual(dict(zip(IDS, (1, 2, 3, 4))), dict(result.season_assignments))
        self.assertEqual(before, observations)

    def test_different_exact_ids_across_languages_remain_conflicted(self):
        transport = FrozenTransport()
        first = dict(id=63576, name=subject('43951')['title'], original_name=subject('43951')['original_title'], first_air_date='2013-07-06', genre_ids=[16])
        second = {**first, 'id': 99999}
        transport.override = lambda params: [first if canonical(params['query']) == canonical(subject('43951')['original_title']) else second]
        transport.detail[99999] = {**deepcopy(transport.detail[63576]), 'id': 99999}
        transport.aliases[99999] = deepcopy(transport.aliases[63576])
        with transport.context():
            observation = worker()._observe_tmdb_tv_subject(subject('43951'))
        candidate, problem = _candidate_from_observation(observation)
        self.assertIsNone(candidate)
        self.assertIsNotNone(problem)
        self.assertEqual(('locale_candidate_conflict',), problem.reason_codes)

    def test_empty_incomplete_search_does_not_become_explicit_no_match(self):
        transport = FrozenTransport()
        transport.override, transport.total_pages = [], 2
        with transport.context():
            observation = worker()._observe_tmdb_tv_subject(subject('83402'))
        self.assertEqual(2, len(observation['queries']))
        self.assertTrue(all(q['truncated'] or q['outcome'] == 'invalid_response' for q in observation['queries']))
        candidate, problem = _candidate_from_observation(observation)
        self.assertIsNone(candidate)
        self.assertIsNotNone(problem)
        self.assertIn(problem.reason_codes, (('subject_query_truncated',), ('subject_candidate_invalid_response',)))

    def test_title_match_outside_every_airing_year_cannot_be_exact(self):
        transport = FrozenTransport()
        row = deepcopy(subject('43951'))
        row['air_date'] = '2030-01-01'
        with transport.context():
            observation = worker()._observe_tmdb_tv_subject(row)
        candidates = [c for q in observation['queries'] for c in q['candidates']]
        self.assertTrue(candidates)
        self.assertTrue(all(c['match_tier'] == 'fuzzy' for c in candidates))
        self.assertTrue(all(c['score_components']['year_compatible'] is False for c in candidates))
        candidate, problem = _candidate_from_observation(observation)
        self.assertIsNone(candidate)
        self.assertEqual(('subject_candidate_confidence_insufficient',), problem.reason_codes)

    def test_live_action_with_exact_title_cannot_be_exact_animation(self):
        transport = FrozenTransport()
        transport.override = [dict(id=63576, name=subject('43951')['title'],
            original_name=subject('43951')['original_title'], first_air_date='2013-07-06', genre_ids=[18])]
        transport.detail[63576]['genres'] = [{'id': 18, 'name': 'Drama'}]
        with transport.context():
            observation = worker()._observe_tmdb_tv_subject(subject('43951'))
        candidates = [c for q in observation['queries'] for c in q['candidates']]
        self.assertTrue(candidates)
        self.assertTrue(all(c['match_tier'] == 'fuzzy' for c in candidates))
        self.assertTrue(all(c['score_components']['animation_confirmed'] is False for c in candidates))
        candidate, problem = _candidate_from_observation(observation)
        self.assertIsNone(candidate)
        self.assertEqual(('subject_candidate_confidence_insufficient',), problem.reason_codes)

    def test_production_selection_uses_complete_search_and_preserves_confirmation(self):
        import tempfile
        for selected in IDS:
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as directory:
                value = worker()
                value.decision_state_root = Path(directory) / 'decisions'
                value.provider_link_catalog_root = Path(directory) / 'provider_links'
                value.active_consumer_task_id = 'preserved-task-id'
                row = subject(selected)
                value.bangumi_search = Mock()
                value.bangumi_search._get_json.return_value = dict(id=int(selected), type=2,
                    platform='TV', name=row['original_title'], name_cn=row['title'], date=row['air_date'])
                value.bangumi_search.load_crosswalk_evidence.return_value = (
                    FIXTURE['bangumi_subjects'], FIXTURE['bangumi_relations'])
                transport = FrozenTransport()
                with transport.context():
                    name, info, resolution = value._resolve_bangumi_provider_selection(row)
                self.assertTrue(name)
                self.assertEqual(63576, info['id'])
                self.assertEqual(selected, info['_bar_primary_provider']['provider_id'])
                self.assertTrue(resolution.requires_plan_review)
                self.assertEqual(dict(zip(IDS, (1, 2, 3, 4))), dict(resolution.season_assignments))
                self.assertTrue(transport.alias_calls)
                self.assertTrue(all('first_air_date_year' not in p for p in transport.search_calls))

    def test_disconnected_subject_search_cannot_fall_back_to_ordinary_search(self):
        import tempfile
        from src.rename.process import ProviderSearchError
        with tempfile.TemporaryDirectory() as directory:
            value = worker()
            value.decision_state_root = Path(directory) / 'decisions'
            value.provider_link_catalog_root = Path(directory) / 'provider_links'
            value.active_consumer_task_id = 'preserved-task-id'
            row = subject('43951')
            value.bangumi_search = Mock()
            value.bangumi_search._get_json.return_value = dict(id=43951, type=2,
                platform='TV', name=row['original_title'], name_cn=row['title'], date=row['air_date'])
            value.bangumi_search.load_crosswalk_evidence.return_value = (
                FIXTURE['bangumi_subjects'], FIXTURE['bangumi_relations'])
            ordinary = Mock(return_value=('apparently exact', deepcopy(FIXTURE['tv_detail'])))
            with patch.object(Search, 'get_tv_subject_observation_info', side_effect=TMDBRequestError('disconnected')), \
                    patch.object(Search, 'get_tv_info', ordinary):
                with self.assertRaises(ProviderSearchError):
                    value._resolve_bangumi_provider_selection(row)
            ordinary.assert_not_called()
            self.assertIn('subject_candidate_transport_error', value.active_provider_link_failure['reason_codes'])
