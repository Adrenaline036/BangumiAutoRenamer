"""ERR056 relationship lifecycle through offline real task/Plan producers.

Only Provider transports and UI rendering come from existing fixtures. Every
task, confirmation, approval, ledger and rebuild below is production-generated
inside a TemporaryDirectory; no historical business data or Provider is used.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from src.rename.plan_store import PlanStore
from src.rename.plan_review import PlanReviewError
from src.rename.plan_verifier import PlanVerifier
from src.rename.successor_authority import CohortAuthorityReader, successor_authority_root
from tests.test_r24_err048_series_registration_contract import MultiTaskFlow, MEMBERS, expected_key
from tests.test_r24_err048_cohort_coexistence_contract import snapshot
from tests.test_r24_phase_c_review_operation_migration_contract import current_provider_selection_context
from tests.test_r24_yama_special_order_dependence_incident_red_contract import _apply_boundary
from src.pages.review_operation_view import ReviewCommandApplyResult


class IncompleteMetadataFlow(MultiTaskFlow):
    """Complete three-subject TV chain; TMDB only knows its first season."""
    def __init__(self, root, *, anchor_middle=False):
        super().__init__(root)
        self.subjects = {key: self.subjects[key] for key in MEMBERS}
        self.relations = {key: [(other, relation) for other, relation in self.relations[key]
                          if other in MEMBERS] for key in MEMBERS}
        # Real Bangumi subject endpoints expose both directions; the base
        # distal fixture lists forward graph edges, insufficient when discovery
        # begins at the middle subject rather than the chain's first member.
        for first, second in zip(MEMBERS, MEMBERS[1:]):
            if (first, '前传') not in self.relations[second]:
                self.relations[second].append((first, '前传'))
        original_search = self.search_type
        first_subject, middle_subject = (self.subjects[key] for key in MEMBERS[:2])

        class Search(original_search):
            def get_tv_info(self, query, _year=None):
                if anchor_middle:
                    # A second transport shape: only the selected middle TV
                    # subject has an exact one-season metadata result. Other
                    # members are honest no-match, never overlaid observations.
                    if query not in {middle_subject['title'], middle_subject['original_title']}:
                        return '', None
                    name, info = super().get_tv_info(first_subject['original_title'], _year)
                else:
                    name, info = super().get_tv_info(query, _year)
                if info is not None:
                    info = deepcopy(info)
                    info['seasons'] = [season for season in info['seasons']
                                       if season['season_number'] == 1]
                    if anchor_middle:
                        info.update(name=middle_subject['title'], original_name=middle_subject['original_title'],
                                    first_air_date=middle_subject['air_date'])
                        for season in info['seasons']:
                            season['air_date'] = middle_subject['air_date']
                        info['_bar_search_alternatives'] = [{key: info[key] for key in
                            ('id', 'name', 'original_name', 'first_air_date')}]
                        name = info['name']
                return name, info
        self.search_type = Search


class CohortLifecycleContracts(unittest.IsolatedAsyncioTestCase):
    def assert_inherited(self, runtime, result, subject):
        self.assertEqual(subject, result.state.confirmed_work.get('primary_provider', {}).get('provider_id'))
        self.assertEqual(expected_key(MEMBERS[0]), result.state.series_binding['series_key'])
        self.assertEqual(MEMBERS.index(subject)+1, result.plan.metadata['season_number'])
        self.assertEqual([], list(PlanVerifier().verify(result.plan).issues))
        self.assertFalse(result.task.get('error'))
        self.assertEqual('need_confirm', result.task['workflow_status'])

    async def test_safe_task_removal_does_not_remove_confirmed_relationship(self):
        with tempfile.TemporaryDirectory() as temporary:
            async with MultiTaskFlow(Path(temporary)) as runtime:
                producer = await runtime.add(MEMBERS[0])
                facts = snapshot(successor_authority_root(runtime.decisions))
                confirmations = len(runtime.confirmation_states)
                historical_plan = PlanStore(runtime.plans).path_for(producer.task_id)
                historical_bytes = historical_plan.read_bytes()
                await runtime.manager.abandon_and_remove(producer.task_id,
                    record_root=runtime.root/'records', plan_root=runtime.plans,
                    review_root=runtime.reviews)
                self.assertFalse((runtime.tasks/f'{producer.task_id}.json').exists())
                self.assertEqual(historical_bytes, historical_plan.read_bytes())
                with self.assertRaisesRegex(PlanReviewError, 'did not approve'):
                    await runtime.approve(producer)
                await runtime.manager.stop()
                await runtime.manager.start()
                consumer = await runtime.add(MEMBERS[1], confirm=False)
                self.assert_inherited(runtime, consumer, MEMBERS[1])
                self.assertEqual(confirmations, len(runtime.confirmation_states))
                self.assertEqual(facts, snapshot(successor_authority_root(runtime.decisions)))
                await runtime.approve(consumer)
                self.assertFalse((runtime.root/'Anime').exists())
                runtime.assert_sources_unchanged(self)

    async def test_same_task_retry_and_reselection_keep_relationship_for_new_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            async with MultiTaskFlow(Path(temporary)) as runtime:
                producer = await runtime.add(MEMBERS[0])
                facts = snapshot(successor_authority_root(runtime.decisions))
                original_fp = PlanStore.fingerprint(producer.plan)
                await runtime.manager.retry_current(producer.task_id)
                await runtime.joined()
                producer = await runtime.select(producer.task_id, MEMBERS[0], confirm=False)
                self.assert_inherited(runtime, producer, MEMBERS[0])
                self.assertNotEqual(original_fp, PlanStore.fingerprint(producer.plan))
                consumer = await runtime.add(MEMBERS[2], confirm=False)
                self.assert_inherited(runtime, consumer, MEMBERS[2])
                self.assertEqual(facts, snapshot(successor_authority_root(runtime.decisions)))
                for result in (producer, consumer):
                    await runtime.approve(result)
                runtime.assert_sources_unchanged(self)

    async def test_actual_completed_rebuild_does_not_revoke_relationship(self):
        from tests.test_completed_rebuild_contract import recovery
        from src.rename.plan_execution import PlanExecutionError
        with tempfile.TemporaryDirectory() as temporary:
            async with MultiTaskFlow(Path(temporary)) as runtime:
                producer = await runtime.add(MEMBERS[0])
                plan = await runtime.approve(producer)
                await runtime.manager.enqueue_approved(producer.task_id, PlanStore.fingerprint(plan))
                await runtime.joined()
                completed = runtime.result(producer.task_id)
                self.assertEqual('completed', completed.task['execution_status'], completed.task.get('error'))
                self.assertEqual('partial_failed', completed.task['workflow_status'])
                self.assertIn('检查', completed.task['font_error'])
                self.assertTrue((runtime.root/'records'/f'{producer.task_id}.ledger.json').is_file())
                facts = snapshot(successor_authority_root(runtime.decisions))
                missing = plan.entries[0].target_path
                missing.unlink()
                service = recovery(runtime.root)
                view = service.inspect(producer.task_id)
                self.assertEqual('missing', view.state)
                fresh = service.generate(producer.task_id, expected_fingerprint=view.fingerprint)
                self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(fresh))
                self.assertEqual('draft', runtime.review.load_or_create(fresh).status)
                for fingerprint in (PlanStore.fingerprint(plan), PlanStore.fingerprint(fresh)):
                    with self.assertRaises(PlanExecutionError):
                        runtime.execution.validate_approved(producer.task_id, fingerprint)
                lookup = CohortAuthorityReader.from_decision_root(runtime.decisions).lookup(
                    MEMBERS[1], consumer_task_id='independent-next-season')
                self.assertEqual('matched', lookup.status, lookup.reason_code)
                consumer = await runtime.add(MEMBERS[1], confirm=False)
                self.assert_inherited(runtime, consumer, MEMBERS[1])
                await runtime.approve(consumer)
                self.assertFalse(missing.exists(), 'inheriting a relationship cannot repair media')
                self.assertEqual(facts, snapshot(successor_authority_root(runtime.decisions)))
                runtime.assert_sources_unchanged(self)


class NativeCohortEntryContracts(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_tmdb_has_explicit_three_season_preview_from_first_or_middle(self):
        for selected in MEMBERS[:2]:
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as temporary:
                async with IncompleteMetadataFlow(Path(temporary), anchor_middle=selected == MEMBERS[1]) as runtime:
                    result = await runtime.add(selected, confirm=False)
                    context = current_provider_selection_context(result.task)
                    self.assertIsNotNone(context, result.task.get('error'))
                    self.assertEqual('awaiting_user', context['state'], result.task.get('error'))
                    evidence = context['provider_subject_evidence']
                    original_batch = deepcopy(evidence['subject_observations'])
                    edges = evidence['provider_subject_boundary_edges']
                    self.assertEqual(2 if selected == MEMBERS[1] else 1, len(edges),
                        {'edges': edges, 'relations': evidence['bangumi_relations']})
                    from src.rename.provider_link_decision import resolve_provider_subject_cohort_segment
                    preview = resolve_provider_subject_cohort_segment(
                        selected_subject_id=selected, subject_observations=original_batch,
                        bangumi_subjects=evidence['bangumi_subjects'],
                        bangumi_relations=evidence['bangumi_relations'])
                    self.assertEqual(tuple(zip(MEMBERS, (1, 2, 3))), preview.resolution.season_assignments)
                    self.assertFalse(preview.resolution.tmdb_tv_id)
                    self.assertEqual(original_batch, preview.working_batch,
                        'incomplete exact metadata must not be rewritten as no-match')
                    self.assertEqual(ReviewCommandApplyResult.APPLIED, _apply_boundary(result))
                    confirmed = runtime.result(result.task_id)
                    self.assertEqual(dict(preview.resolution.season_assignments),
                        dict(confirmed.plan.metadata['series_registration_authority']['season_membership']))
                    self.assertIsNone(confirmed.state.provider_link_binding)
                    self.assertEqual(expected_key(MEMBERS[0]), confirmed.state.series_binding['series_key'])
                    self.assertEqual(MEMBERS.index(selected)+1, confirmed.plan.metadata['season_number'])
                    self.assertEqual([], list(PlanVerifier().verify(confirmed.plan).issues))
                    await runtime.approve(confirmed)
                    self.assertFalse((runtime.root/'Anime').exists())
                    runtime.assert_sources_unchanged(self)


class CohortMappingControls(unittest.TestCase):
    def test_exact_eighty_six_partition_keeps_two_subjects_in_one_season(self):
        from tests import test_provider_subject_segmentation_red_contract as fixture
        from src.rename.provider_link_decision import resolve_provider_subject_cohort_segment
        observations = tuple(fixture._observation(str(subject['provider_id']),
            fixture._query(str(subject['original_title']), 'ja-JP', candidate=fixture.TMDB_EIGHTY_SIX))
            for subject in fixture.EIGHTY_SIX)
        batch = fixture._batch('331887', observations)
        before = deepcopy(batch)
        segment = resolve_provider_subject_cohort_segment(selected_subject_id='331887',
            subject_observations=batch, bangumi_subjects=fixture.EIGHTY_SIX,
            bangumi_relations=(fixture._relation('302189', '331887'),))
        self.assertEqual('many_subjects_one_season', segment.resolution.mapping_shape)
        self.assertEqual({'302189': 1, '331887': 1}, dict(segment.resolution.season_assignments))
        self.assertEqual([(1, 11), (12, 23)], [(row['episode_start'], row['episode_end'])
            for row in segment.resolution.derived_episode_ranges])
        self.assertEqual(before, batch)
