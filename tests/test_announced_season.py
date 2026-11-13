"""已宣布续作不阻断当前季度的隔离验证。"""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from tests.test_series_single_side import SingleSideFlow, MEMBERS, evidence
from tests.test_r24_yama_special_order_dependence_incident_red_contract import _apply_boundary
from src.rename.provider_link_resolver import collect_subject_observations
from src.rename.provider_link_decision import resolve_provider_subject_cohort_segment
from src.rename.review_operation import build_review_operation

FUTURE = '900001'


def announced_evidence():
    subjects, relations, batch = evidence()
    subjects.append(dict(subjects[-1], provider_id=FUTURE, title='已宣布续作',
                         episode_count=0, air_date=''))
    relations.append({'from': MEMBERS[-1], 'to': FUTURE, 'relation': 'sequel'})
    rows = {r['bangumi_subject_id']: deepcopy(r) for r in batch['subject_observations']}
    rows[FUTURE] = dict(deepcopy(rows[MEMBERS[-1]]), bangumi_subject_id=FUTURE)
    batch = collect_subject_observations(selected_subject_id=MEMBERS[1],
        bangumi_subjects=subjects, bangumi_relations=relations,
        search_subject=lambda s: deepcopy(rows[str(s['provider_id'])]), max_subjects=5)
    return subjects, relations, batch


class AnnouncedFlow(SingleSideFlow):
    def __init__(self, root, singleton=False):
        super().__init__(root, manual=7, singleton=singleton)
        self.subjects[FUTURE] = dict(self.subjects[MEMBERS[-1]], provider_id=FUTURE,
            id=int(FUTURE), title='已宣布续作', name='已宣布续作', name_cn='已宣布续作',
            eps=0, episode_count=0, date=None, air_date='')
        self.relations[MEMBERS[-1]].append((FUTURE, 'sequel'))
        self.relations[FUTURE] = [(MEMBERS[-1], 'prequel')]
        self.observations[FUTURE] = dict(deepcopy(self.observations[MEMBERS[-1]]),
                                          bangumi_subject_id=FUTURE)


class AnnouncedSeasonTests(unittest.IsolatedAsyncioTestCase):
    def test_complete_prefix_retains_full_evidence(self):
        subjects, relations, batch = announced_evidence()
        before = deepcopy((subjects, relations, batch))
        for selected in (MEMBERS[0], MEMBERS[1], MEMBERS[-1]):
            for date, count in (('', 0), ('', None), ('2026-10-19', 0), ('', 12), ('2026-10-19', 12)):
                values = deepcopy(subjects)
                values[-1].update(air_date=date, episode_count=count)
                segment = resolve_provider_subject_cohort_segment(selected_subject_id=selected,
                    bangumi_subjects=values, bangumi_relations=relations, subject_observations=batch)
                expected = MEMBERS + ((FUTURE,) if count == 12 else ())
                self.assertEqual(expected, segment.segment_subject_ids)
                self.assertEqual(expected, tuple(dict(segment.resolution.season_assignments)))
                self.assertEqual(set(expected), {r['bangumi_subject_id'] for r in segment.resolution.derived_episode_ranges})
                self.assertEqual(json.loads(json.dumps(batch)), segment.working_batch)
                self.assertEqual(5, len(segment.resolution.subject_observations['subject_observations']))
        self.assertEqual(before, (subjects, relations, batch))

    def test_missing_current_middle_and_invalid_tail_are_rejected(self):
        for case in ('selected_future', 'selected_gap', 'root_gap', 'middle', 'negative', 'bool', 'string', 'branch', 'truncated'):
            subjects, relations, batch = announced_evidence()
            selected = MEMBERS[1]
            if case == 'selected_future': selected = FUTURE
            elif case in ('selected_gap', 'root_gap', 'middle'):
                missing_id = {'selected_gap': MEMBERS[1], 'root_gap': MEMBERS[0], 'middle': MEMBERS[2]}[case]
                next(s for s in subjects if s['provider_id'] == missing_id).update(episode_count=0, air_date='')
            elif case == 'negative': subjects[-1]['episode_count'] = -1
            elif case == 'bool': subjects[-1]['episode_count'] = False
            elif case == 'string': subjects[-1]['episode_count'] = '0'
            elif case == 'branch': relations.append({'from': MEMBERS[0], 'to': MEMBERS[2], 'relation': 'sequel'})
            else: batch['truncated'] = True
            with self.subTest(case=case), self.assertRaises(ValueError):
                resolve_provider_subject_cohort_segment(selected_subject_id=selected,
                    bangumi_subjects=subjects, bangumi_relations=relations, subject_observations=batch)

    async def test_existing_task_manual_season_and_shared_root(self):
        from src.rename.plan_store import PlanStore
        with tempfile.TemporaryDirectory() as tmp:
            async with AnnouncedFlow(Path(tmp)) as runtime:
                # 保留旧版本拒绝后的原卡，再通过当前入口重新选择。
                with patch('src.rename.provider_link_decision.resolve_provider_subject_cohort_segment',
                           side_effect=ValueError('续作链缺少可冻结的普通集范围')):
                    blocked = await runtime.add(MEMBERS[1], confirm=False)
                self.assertIsNone(blocked.state.pending_tv_selection)
                self.assertTrue(blocked.task.get('error'))
                original_id = blocked.task_id
                pending = await runtime.select(original_id, MEMBERS[1], confirm=False)
                self.assertIsNotNone(pending.state.pending_tv_selection, pending.task.get('error'))
                operation = build_review_operation(pending.task)[0]
                command = next(c for c in operation.commands if c.kind == 'confirm_cohort')
                runtime.review.root_snapshot_source = runtime.manager.root_snapshot_source
                stale = command.to_dict(); stale['expected_revision'] += 1
                before = pending.state.to_dict()
                with self.assertRaises(ValueError):
                    runtime.review.prepare_tv_boundary_completion(original_id,
                        PlanStore.fingerprint(pending.plan), review_command=stale)
                self.assertEqual(before, runtime.result(original_id).state.to_dict())
                _apply_boundary(pending)
                result = runtime.result(original_id)
                self.assertEqual(original_id, result.task_id)
                self.assertEqual(7, result.state.season_binding['season_number'])
                self.assertEqual('explicit_user', result.state.season_binding['decision_source'])
                self.assertTrue(result.plan.entries)
                self.assertTrue(all('Season 07' in str(e.target_path) for e in result.plan.entries))
                rows = list(result.state.provider_subject_boundary_decisions.values())
                self.assertTrue(rows)
                self.assertEqual(list(MEMBERS), list(rows[0]['cohort_subject_ids']))
                runtime.manual = 1
                first = await runtime.add(MEMBERS[0])
                self.assertEqual(first.state.series_binding['canonical_root'], result.state.series_binding['canonical_root'])
                self.assertEqual(result.state.to_dict(), runtime.result(original_id).state.to_dict())
                runtime.assert_sources_unchanged(self)

    async def test_single_complete_subject_with_announced_sequel_can_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with AnnouncedFlow(Path(tmp), singleton=True) as runtime:
                pending = await runtime.add(MEMBERS[-1], confirm=False)
                operation = build_review_operation(pending.task)[0]
                self.assertIn('separate_work', [c.kind for c in operation.commands])
                _apply_boundary(pending, same_work=False)
                result = runtime.result(pending.task_id)
                self.assertEqual(7, result.state.season_binding['season_number'])
                self.assertTrue(result.plan.entries)
                runtime.assert_sources_unchanged(self)
