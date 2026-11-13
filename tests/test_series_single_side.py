"""动画系列单侧资料与人工季度的隔离合同。"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.pages.plan_review_page import PlanReviewDialog
from src.rename.provider_link_resolver import collect_subject_observations
from src.rename.provider_link_decision import resolve_provider_subject_cohort_segment
from src.rename.season_identity import resolve_initial_tv_season
from src.rename.tv_completion_observations import TVSeasonObservation
from src.rename.rename_plan import WorkIdentity, ProviderIdentity
from src.rename.review_operation import build_review_operation
from tests.test_r24_err048_series_registration_contract import MultiTaskFlow, expected_key
from tests.test_r24_err047_distal_conflict_contract import failure
from tests.test_r24_yama_special_order_dependence_incident_red_contract import _apply_boundary
import tests.test_provider_subject_plan_wiring_red_contract as flow

MEMBERS = ('248175', '293049', '317613', '425211')


def evidence(mode='no_match', selected=MEMBERS[-1]):
    value = failure(selected)
    subjects = [s for s in value['bangumi_subjects'] if str(s['provider_id']) in MEMBERS]
    relations = [dict(from_=a, to=b) for a, b in zip(MEMBERS, MEMBERS[1:])]
    relations = [{'from': r['from_'], 'to': r['to'], 'relation': 'sequel'} for r in relations]
    rows = {r['bangumi_subject_id']: deepcopy(r) for r in value['subject_observations']['subject_observations']}
    for subject, row in rows.items():
        if mode == 'mixed' and subject != MEMBERS[-1]:
            continue
        for query in row['queries']:
            query.update(outcome='transport_error' if mode == 'transport' else 'no_match', candidates=[], truncated=False)
    if mode == 'tie':
        original = failure(selected)['subject_observations']['subject_observations']
        chosen = next(r for r in original if r['bangumi_subject_id'] == selected)
        candidates = {str(c['tmdb_tv_id']): c for q in chosen['queries'] for c in q['candidates']}
        assert len(candidates) == 2
        for query in rows[selected]['queries']:
            query.update(outcome='ok', candidates=[dict(c, match_tier='exact') for c in candidates.values()])
    batch = collect_subject_observations(selected_subject_id=selected, bangumi_subjects=subjects,
        bangumi_relations=relations, search_subject=lambda s: deepcopy(rows[str(s['provider_id'])]), max_subjects=4)
    return subjects, relations, batch


class SingleSideFlow(MultiTaskFlow):
    def __init__(self, root, mode='no_match', manual=None, singleton=False):
        super().__init__(root, no_match=True, source_factory=lambda root, name, numbers:
            flow._make_episode_source(root, name, (1, 2), subtitles=False))
        subjects, relations, batch = evidence(mode)
        self.subjects = {str(s['provider_id']): dict(s, id=int(s['provider_id']), name_cn=s['title'],
            name=s.get('original_title', s['title']), date=s['air_date'], eps=s['episode_count'], type=2, platform='TV') for s in subjects}
        self.relations = {s: [] for s in self.subjects}
        for edge in relations:
            self.relations[edge['from']].append((edge['to'], 'sequel'))
            self.relations[edge['to']].append((edge['from'], 'prequel'))
        self.observations = {r['bangumi_subject_id']: r for r in batch['subject_observations']}
        if singleton:
            self.subjects = {MEMBERS[-1]: self.subjects[MEMBERS[-1]]}
            self.relations = {MEMBERS[-1]: []}
        self.manual = manual

    async def __aenter__(self):
        await super().__aenter__()
        self.stack.enter_context(patch.object(self.renamer, '_observe_tmdb_tv_subject',
            side_effect=lambda s: deepcopy(self.observations[str(s['provider_id'])])))
        return self

    async def select(self, task_id, selected, *, confirm=True):
        current = self.result(task_id)
        dialog = SimpleNamespace(task_decision=current.state, plan=current.plan,
            task_data=current.task, close=MagicMock(), _render_groups=MagicMock())
        await PlanReviewDialog._select_provider_candidate(dialog, flow._candidate(self.subjects, selected),
            season_number=self.manual)
        await self.joined()
        current = self.result(task_id)
        if confirm and current.state.pending_tv_selection is not None:
            commands = build_review_operation(current.task)[0].commands
            if not any(c.kind == 'confirm_cohort' for c in commands):
                raise AssertionError('缺少共同系列确认命令：' + str([c.kind for c in commands]))
            _apply_boundary(current)
        return self.result(task_id)


class SeriesSingleSideContracts(unittest.IsolatedAsyncioTestCase):
    def test_complete_chain_does_not_depend_on_tmdb_outcome(self):
        for mode in ('no_match', 'mixed', 'tie', 'transport'):
            with self.subTest(mode=mode):
                subjects, relations, batch = evidence(mode)
                before = deepcopy(batch)
                segment = resolve_provider_subject_cohort_segment(selected_subject_id=MEMBERS[-1],
                    bangumi_subjects=subjects, bangumi_relations=relations, subject_observations=batch)
                self.assertEqual(MEMBERS, segment.segment_subject_ids)
                self.assertEqual(dict(zip(MEMBERS, (1, 2, 3, 4))), dict(segment.resolution.season_assignments))
                self.assertFalse(segment.resolution.tmdb_tv_id)
                self.assertEqual(before, batch)

    def test_manual_season_overrides_reference_mapping(self):
        subject = MEMBERS[-1]
        subjects, relations, batch = evidence()
        resolution = resolve_provider_subject_cohort_segment(selected_subject_id=subject,
            bangumi_subjects=subjects, bangumi_relations=relations, subject_observations=batch).resolution
        work = WorkIdentity(status='confirmed', domain='animation', title='系列', year=2019,
            primary_provider=ProviderIdentity(status='confirmed', namespace='bangumi', entity_type='subject',
                provider_id=subject, title='第四部分', year=2023, decision_source='explicit_user'), domain_source='provider_selection')
        seasons = TVSeasonObservation(('bangumi', 'subject', subject), (1, 2, 3, 4))
        self.assertEqual((7, 'explicit_user'), resolve_initial_tv_season(seasons, work, Path('source'), 7, resolution))
        self.assertEqual((4, 'provider_crosswalk'), resolve_initial_tv_season(seasons, work, Path('source'), None, resolution))
        for invalid in (-1, True):
            with self.assertRaises(ValueError):
                resolve_initial_tv_season(seasons, work, Path('source'), invalid, resolution)

    async def test_bangumi_only_manual_season_keeps_same_series_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with SingleSideFlow(Path(tmp)) as runtime:
                runtime.manual = 7
                pending = await runtime.add(MEMBERS[-1], confirm=False)
                self.assertIsNotNone(pending.state.pending_tv_selection, pending.task.get('error'))
                _apply_boundary(pending)
                last = runtime.result(pending.task_id)
                runtime.manual = None
                first = await runtime.add(MEMBERS[0])
                self.assertEqual(first.state.series_binding['series_key'], last.state.series_binding['series_key'])
                self.assertEqual(first.state.series_binding['canonical_root'], last.state.series_binding['canonical_root'])
                self.assertEqual(expected_key(MEMBERS[0]), last.state.series_binding['series_key'])
                self.assertEqual(7, last.state.season_binding['season_number'])
                self.assertEqual('explicit_user', last.state.season_binding['decision_source'])
                self.assertEqual(MEMBERS[-1], last.state.confirmed_work['primary_provider']['provider_id'])
                self.assertFalse(last.state.linked_provider_identities)
                self.assertTrue(last.plan.entries)
                self.assertTrue(any('Season 07' in str(e.target_path) for e in last.plan.entries))
                reloaded = runtime.result(last.task_id)
                self.assertEqual(last.state.to_dict(), reloaded.state.to_dict())
                runtime.assert_sources_unchanged(self)

    async def test_first_middle_last_entry_keeps_native_series(self):
        for selected in (MEMBERS[0], MEMBERS[1], MEMBERS[-1]):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as tmp:
                async with SingleSideFlow(Path(tmp)) as runtime:
                    result = await runtime.add(selected)
                    self.assertEqual(expected_key(MEMBERS[0]), result.state.series_binding['series_key'])
                    self.assertEqual(MEMBERS.index(selected) + 1, result.state.season_binding['season_number'])
                    self.assertFalse(result.state.linked_provider_identities)

    async def test_tied_candidates_offer_native_series_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with SingleSideFlow(Path(tmp), mode='tie') as runtime:
                result = await runtime.add(MEMBERS[-1], confirm=False)
                kinds = [c.kind for c in build_review_operation(result.task)[0].commands]
                self.assertIn('confirm_cohort', kinds)
                self.assertEqual(2, kinds.count('select_identity'))
                _apply_boundary(result)
                result = runtime.result(result.task_id)
                self.assertEqual(expected_key(MEMBERS[0]), result.state.series_binding['series_key'])
                self.assertFalse(result.state.linked_provider_identities)

    async def test_mixed_and_transport_keep_series_root(self):
        for mode in ('mixed', 'transport'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                async with SingleSideFlow(Path(tmp), mode=mode, manual=4) as runtime:
                    last = await runtime.add(MEMBERS[-1])
                    runtime.manual = 1
                    first = await runtime.add(MEMBERS[0])
                    self.assertEqual(first.state.series_binding['canonical_root'], last.state.series_binding['canonical_root'])
                    self.assertEqual(expected_key(MEMBERS[0]), last.state.series_binding['series_key'])
                    self.assertEqual(4, last.state.season_binding['season_number'])
                    self.assertFalse(last.state.linked_provider_identities)
                    runtime.assert_sources_unchanged(self)

    async def test_singleton_can_continue_without_tmdb(self):
        from src.rename.plan_store import PlanStore
        for mode in ('transport', 'tie', 'no_match'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                async with SingleSideFlow(Path(tmp), mode=mode, manual=7, singleton=True) as runtime:
                    result = await runtime.add(MEMBERS[-1], confirm=False)
                    if mode != 'no_match':
                        operation = build_review_operation(result.task)[0]
                        command = next(c for c in operation.commands if c.kind == 'separate_work')
                        from src.pages.review_operation_identity_view import command_action_spec
                        self.assertEqual('仅使用 Bangumi 资料继续', command_action_spec(command)[1])
                        _apply_boundary(result, same_work=False)
                        result = runtime.result(result.task_id)
                    self.assertEqual(7, result.state.season_binding['season_number'])
                    self.assertFalse(result.state.linked_provider_identities)
                    self.assertTrue(result.plan.entries)

    def test_incomplete_or_branched_bangumi_is_rejected(self):
        for mode in ('missing_subject', 'branch', 'truncated', 'boundary'):
            subjects, relations, batch = evidence('transport')
            if mode == 'missing_subject': subjects.pop(0)
            elif mode == 'branch': relations.append(dict(**{'from': MEMBERS[0], 'to': MEMBERS[2], 'relation': 'sequel'}))
            elif mode == 'truncated': batch['truncated'] = True
            else: batch['boundary_complete'] = False
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                resolve_provider_subject_cohort_segment(selected_subject_id=MEMBERS[-1],
                    bangumi_subjects=subjects, bangumi_relations=relations, subject_observations=batch)

    async def test_old_command_and_missing_edges_cannot_apply(self):
        from src.rename.review_operation import build_provider_selection_review_operation
        from src.rename.plan_store import PlanStore
        with tempfile.TemporaryDirectory() as tmp:
            async with SingleSideFlow(Path(tmp), mode='tie') as runtime:
                result = await runtime.add(MEMBERS[-1], confirm=False)
                operation = build_review_operation(result.task)[0]
                command = next(c for c in operation.commands if c.kind == 'confirm_cohort')
                context = deepcopy(dict(operation.context))
                context['provider_subject_evidence']['provider_subject_boundary_edges'] = []
                disconnected = build_provider_selection_review_operation(result.task, context)
                self.assertFalse(any(c.kind == 'confirm_cohort' for c in disconnected.commands))
                runtime.review.root_snapshot_source = runtime.manager.root_snapshot_source
                before = result.state.to_dict()
                stale = command.to_dict(); stale['expected_revision'] += 1
                with self.assertRaises(ValueError):
                    runtime.review.prepare_tv_boundary_completion(result.task_id,
                        PlanStore.fingerprint(result.plan), review_command=stale)
                self.assertEqual(before, runtime.result(result.task_id).state.to_dict())
                _apply_boundary(result)
                current = runtime.result(result.task_id)
                with self.assertRaises(ValueError):
                    runtime.review.prepare_tv_boundary_completion(result.task_id,
                        PlanStore.fingerprint(current.plan), review_command=command)
