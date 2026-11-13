"""ERR048: series authority through real task creation, selection and persistence.

Provider replies and UI rendering are disposable collaborators. Decisions, Plans,
catalog writes and approval/execution are production code, never injected as a
successful downstream fixture. All writes, including hardlinks, stay in tempdirs.
"""
from contextlib import nullcontext
from tests._approval_stage_helpers import approve_dialog
import asyncio
import copy
from contextlib import ExitStack, nullcontext
from collections.abc import Mapping
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

from src.pages.add_task_page import AddTaskDialog
from src.pages.plan_review_page import PlanReviewDialog
from src.pages.review_operation_view import ReviewCommandApplyResult
from src.rename.catalog_binding import binding_from_decision
from src.rename.decision_state import DecisionStateStore
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_review import PlanReviewError, PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.series_catalog import SeriesCatalog
from src.task_queue import TaskQueueManager
from src.utils.utils import read_task_at
import src.rename.provider_link_decision as provider_decision
import tests.test_provider_subject_plan_wiring_red_contract as flow
from tests.test_r24_err047_distal_conflict_contract import failure
from tests.test_r24_phase_c_review_operation_migration_contract import current_provider_selection_context
from tests.test_r24_yama_special_order_dependence_incident_red_contract import _apply_boundary
import tests.test_r22_revised_c_c0_red_contract as boundary_fixture


MEMBERS = ('248175', '293049', '317613')


def expected_key(subject):
    return f"series-{uuid.uuid5(uuid.NAMESPACE_URL, 'bar:bangumi:subject:' + subject)}"


def provider_fixture(*, no_match=False):
    evidence = failure()
    rows = {row['bangumi_subject_id']: row
            for row in evidence['subject_observations']['subject_observations']}
    subjects = {str(s['provider_id']): dict(s, id=int(s['provider_id']),
        name_cn=s['title'], name=s.get('original_title', s['title']),
        date=s['air_date'], eps=s['episode_count'], type=2, platform='TV')
        for s in evidence['bangumi_subjects']}
    relations = {key: [] for key in subjects}
    for edge in evidence['bangumi_relations']:
        if edge['from'] in subjects and edge['to'] in subjects:
            relations[edge['from']].append((edge['to'], edge['relation']))
    if no_match:
        # Limit this control to the accepted three-subject segment; the distant
        # fourth conflict belongs to another work and is not silently absorbed.
        subjects = {key: subjects[key] for key in MEMBERS}
        relations = {key: [(other, relation) for other, relation in relations[key]
                     if other in MEMBERS] for key in MEMBERS}

    class Search(flow._ProviderSearch):
        def get_tv_info(self, query, _year=None):
            if no_match:
                return '', None
            for row in rows.values():
                for request in row['queries']:
                    if request['query'] == query and request['candidates']:
                        candidate = request['candidates'][0]
                        value = {'id': int(candidate['tmdb_tv_id']),
                            'name': candidate['title'], 'original_name': candidate['original_title'],
                            'first_air_date': candidate['first_air_date'],
                            'genres': [{'id': 16, 'name': 'Animation'}],
                            'seasons': copy.deepcopy(candidate['seasons']),
                            '_bar_search_alternatives': [{'id': int(candidate['tmdb_tv_id']),
                                'name': candidate['title'], 'original_name': candidate['original_title'],
                                'first_air_date': candidate['first_air_date'], 'genre_ids': [16]}]}
                        return value['name'], value
            return '', None
    return subjects, relations, Search


class MultiTaskFlow:
    """One real queue/store set; no single-task helper or copied downstream state."""
    def __init__(self, root, *, no_match=False, source_factory=None):
        self.root = root
        self.tasks, self.plans = root / 'tasks', root / 'plans'
        self.reviews, self.decisions = root / 'reviews', root / 'decisions'
        self.subjects, self.relations, self.search_type = provider_fixture(no_match=no_match)
        self.sources = {}
        self.confirmation_states = []
        self.stack = ExitStack()
        self.source_factory = source_factory or flow._make_episode_source

    async def __aenter__(self):
        self.tasks.mkdir(parents=True)
        stack = self.stack
        stack.enter_context(patch.dict(os.environ, {'BAR_DRY_RUN': '0'}))
        stack.enter_context(patch.dict(flow.production_fixture.YAMA_SUBJECTS, self.subjects, clear=True))
        stack.enter_context(patch.dict(flow.production_fixture.YAMA_RELATIONS, self.relations, clear=True))
        stack.enter_context(patch('tests.test_provider_subject_plan_wiring_red_contract._ProviderSearch', self.search_type))
        renamer = flow._configured_renamer(self.root)
        self.renamer = renamer
        stack.enter_context(flow._root_config_patch({'bangumi_path': str(renamer.BANGUMI_PATH),
            'movie_path': str(renamer.MOVIE_PATH), 'anime_path': str(renamer.ANIME_PATH),
            'anime_movie_path': str(renamer.ANIME_MOVIE_PATH)}))
        paths = {'TASK_PATH': self.tasks, 'PLAN_PATH': self.plans,
            'PLAN_REVIEW_PATH': self.reviews, 'DECISION_STATE_PATH': self.decisions,
            'RECORD_PATH': self.root / 'records', 'SERIES_CATALOG_PATH': self.root / 'series_catalog',
            'WORK_CATALOG_PATH': self.root / 'work_catalog', 'RECOVERY_TRANSACTION_PATH': self.root / 'recovery'}
        for module in (flow.task_queue_module, flow.process_module, flow.review_page, flow.utils_module):
            for name, path in paths.items():
                if hasattr(module, name):
                    stack.enter_context(patch(f'{module.__name__}.{name}', path))
        stack.enter_context(patch('src.pages.plan_review_page._dialog_mount_context', return_value=nullcontext()))
        stack.enter_context(patch('src.pages.plan_review_page.RecognitionRecoveryDialog', flow.ui_fixture._NoopAnyDialog))
        stack.enter_context(patch('src.pages.plan_review_page.PlanReviewDialog', flow._ReviewCapture))
        stack.enter_context(patch('src.pages.plan_review_page.notify'))
        stack.enter_context(patch('src.pages.add_task_page.notify'))
        stack.enter_context(nullcontext())
        stack.enter_context(patch('src.task_queue.Rename', return_value=renamer))
        self.review = PlanReviewService(PlanStore(self.plans), self.reviews, self.root / 'work_catalog',
            decision_state_root=self.decisions, task_root=self.tasks,
            provider_link_catalog_root=self.root / 'provider_links')
        self.execution = PlanExecutionService(self.review.plan_store, self.review,
            task_root=self.tasks, record_root=self.root / 'records',
            catalog_root=self.root / 'work_catalog', series_catalog_root=self.root / 'series_catalog',
            decision_state_root=self.decisions, recovery_transaction_root=self.root / 'recovery',
            provider_link_catalog_root=self.root / 'provider_links')
        self.manager = TaskQueueManager(task_root=self.tasks, plan_root=self.plans,
            decision_state_root=self.decisions, provider_link_catalog_root=self.root / 'provider_links',
            recovery_abandon_audit_root=self.root / 'abandon', execution_service=self.execution)
        stack.enter_context(patch('src.pages.add_task_page.task_queue', self.manager))
        stack.enter_context(patch('src.pages.plan_review_page.task_queue', self.manager))
        await self.manager.start()
        return self

    async def __aexit__(self, *args):
        await self.manager.stop()
        self.stack.close()

    async def joined(self):
        await asyncio.wait_for(self.manager._queue.join(), timeout=15)

    def result(self, task_id):
        return SimpleNamespace(root=self.root, task_id=task_id,
            task=read_task_at(self.tasks, task_id), plan=PlanStore(self.plans).load(task_id),
            state=DecisionStateStore(self.decisions).load(task_id),
            tasks=self.tasks, plans=self.plans, reviews=self.reviews, decisions=self.decisions,
            manager=self.manager, renamer=self.renamer)

    async def select(self, task_id, selected, *, confirm=True):
        current = self.result(task_id)
        dialog = SimpleNamespace(task_decision=current.state, plan=current.plan,
            task_data=current.task, close=MagicMock())
        with ExitStack() as rendering:
            if current.state.confirmed_work:
                # Confirmed-work selection now uses the actual same-card form.
                # Supply its UI context so corruption reaches the real origin
                # check rather than failing on a missing fake-owner attribute.
                from nicegui import Client, ui
                from nicegui.page import page
                client = Client(page('/isolated-registration-reselection'), request=None)
                rendering.callback(client.delete)
                rendering.enter_context(client)
                dialog.client = client
                dialog.review_service = self.review
                dialog.work_correction_container = ui.column()
            await PlanReviewDialog._select_provider_candidate(dialog, flow._candidate(self.subjects, selected))
        await self.joined()
        current = self.result(task_id)
        context = current_provider_selection_context(current.task)
        evidence = (context.get('provider_subject_evidence') or {}) if context else {}
        if confirm and evidence.get('provider_subject_boundary_edges'):
            self.confirmation_states.append(context.get('state'))
            applied = _apply_boundary(current)
            if applied != ReviewCommandApplyResult.APPLIED:
                raise AssertionError(f'normal cohort confirmation failed: {applied}; {read_task_at(self.tasks, task_id).get("error")}')
        # The real work selection produces a review-ready media plan.
        return self.result(task_id)

    async def add(self, selected, *, confirm=True, source=None):
        if source is None:
            source = self.source_factory(self.root / 'sources' / selected,
                'Independent source ' + selected, tuple(range(1, 13)))
            self.sources[str(source)] = flow._source_manifest(source)
        elif self.sources[str(source)] != flow._source_manifest(source):
            raise AssertionError('existing source changed before re-admission')
        before = {p.stem for p in self.tasks.glob('*.json')}
        from nicegui.client import Client
        from nicegui.page import page
        client = Client(page('/isolated-registration-add'), request=None)
        try:
            with client:
                dialog = AddTaskDialog()
                try:
                    dialog.path, dialog.media_type = str(source), 'anime'
                    await dialog._next()
                    if not dialog._closed:
                        await dialog._submit()
                finally:
                    dialog.delete()
        finally:
            client.delete()
        await self.joined()
        created = {p.stem for p in self.tasks.glob('*.json')} - before
        if len(created) != 1:
            raise AssertionError(f'AddTask did not create exactly one new card: {created}')
        task_id = created.pop()
        await flow.review_page.plan_review_page(task_id)
        return await self.select(task_id, selected, confirm=confirm)

    async def approve(self, result):
        plan = PlanStore(self.plans).load(result.task_id)
        ids = tuple(entry_id(e) for e in plan.entries)
        dialog = SimpleNamespace(review_service=self.review, plan=plan, selected_ids=set(ids),
            task_data=read_task_at(self.tasks, result.task_id), decision=None,
            review_badge=SimpleNamespace(text=''), _refresh_plan_header=MagicMock(),
            _render_entries=MagicMock(), _refresh_execute_button=MagicMock())
        await approve_dialog(dialog, ids)
        if self.review.load_or_create(plan).status != 'approved':
            raise PlanReviewError('public approval did not approve current plan')
        return plan

    def assert_sources_unchanged(self, test):
        for path, before in self.sources.items():
            test.assertEqual(before, flow._source_manifest(Path(path)))


class SeriesRegistrationIncidentTests(unittest.IsolatedAsyncioTestCase):
    def assert_registration(self, result, membership):
        self.assertIsNotNone(result.state.series_binding)
        proof = result.plan.to_dict()['metadata'].get('series_registration_authority')
        self.assertIsInstance(proof, dict)
        self.assertTrue(proof)
        self.assertEqual(proof, result.state.to_dict()['series_binding'].get('registration_authority'))
        self.assertEqual([list(row) for row in membership], proof.get('season_membership'))
        self.assertEqual(membership[0][0], proof.get('anchor_provider_id'))
        self.assertEqual('need_confirm', result.task['workflow_status'])
        self.assertFalse(result.task.get('error'))
        self.assertEqual([], list(PlanVerifier().verify(result.plan).issues))

    async def test_independent_selected_subjects_share_segment_key_and_proof(self):
        # Independent stores force each selected subject through the producer;
        # inherited peer authority cannot hide the selected-subject fallback.
        for selected in MEMBERS:
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as tmp:
                async with MultiTaskFlow(Path(tmp)) as runtime:
                    result = await runtime.add(selected)
                    self.assert_registration(result, tuple(zip(MEMBERS, (1, 2, 3))))
                    self.assertEqual(expected_key(MEMBERS[0]), binding_from_decision(result.state).key)
                    proof = result.plan.to_dict()['metadata'].get('series_registration_authority')
                    self.assertIsInstance(proof, Mapping, 'a key alone is not a frozen registration proof')
                    self.assertEqual(proof, result.state.series_binding['registration_authority'])
                    self.assertEqual([], list(PlanVerifier().verify(result.plan).issues))
                    await runtime.approve(result)
                    runtime.assert_sources_unchanged(self)

    async def test_reverse_multicard_order_and_restart_share_series(self):
        for order in (MEMBERS, tuple(reversed(MEMBERS))):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as tmp:
                async with MultiTaskFlow(Path(tmp)) as runtime:
                    results = [await runtime.add(selected) for selected in order]
                    await runtime.manager.stop()
                    await runtime.manager.start()
                    current = [runtime.result(r.task_id) for r in results]
                    self.assertEqual({expected_key(MEMBERS[0])},
                        {binding_from_decision(r.state).key for r in current})
                    self.assertEqual({1, 2, 3}, {r.plan.metadata['season_number'] for r in current})
                    self.assertEqual(3, len(list(runtime.tasks.glob('*.json'))))
                    for result in current:
                        await runtime.approve(result)
                    runtime.assert_sources_unchanged(self)
                    self.assertFalse((runtime.root / 'Anime').exists())

    async def test_optional_metadata_absence_does_not_create_a_cohort_list_key(self):
        for selected in MEMBERS:
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as tmp:
                async with MultiTaskFlow(Path(tmp), no_match=True) as runtime:
                    result = await runtime.add(selected)
                    self.assertEqual(['awaiting_user'], runtime.confirmation_states)
                    self.assert_registration(result, tuple(zip(MEMBERS, (1, 2, 3))))
                    self.assertEqual(expected_key(MEMBERS[0]), binding_from_decision(result.state).key)
                    self.assertIsNotNone(result.plan.metadata.get('series_registration_authority'))
                    self.assertEqual([], list(PlanVerifier().verify(result.plan).issues))
                    await runtime.approve(result)

    async def test_registration_proof_cannot_be_deleted_tampered_or_metadata_only(self):
        from src.rename.series_registration import SeriesRegistrationAuthority, validate_plan_series_registration
        with tempfile.TemporaryDirectory() as tmp:
            async with MultiTaskFlow(Path(tmp)) as runtime:
                result = await runtime.add(MEMBERS[0])
                proof = result.plan.to_dict()['metadata']['series_registration_authority']
                authority = SeriesRegistrationAuthority.from_mapping(proof)
                self.assertEqual(proof, authority.to_dict())
                validate_plan_series_registration(result.plan, result.state)
                for changed in (None, {}, {**proof, 'series_key': expected_key('999999')},
                                {**proof, 'policy_version': 'obsolete'},
                                {**proof, 'schema_version': 0},
                                {**proof, 'fingerprint': '0' * 64},
                                {**proof, 'anchor_provider_id': MEMBERS[1]},
                                {**proof, 'season_membership': [[MEMBERS[0], 99]]}):
                    damaged = replace(result.plan, metadata={**result.plan.metadata,
                        'series_registration_authority': changed})
                    with self.subTest(changed=changed), self.assertRaises(ValueError):
                        validate_plan_series_registration(damaged, result.state)
                with self.assertRaises(ValueError):
                    validate_plan_series_registration(replace(result.plan, projection_receipt=None), result.state)
                with self.assertRaises(ValueError):
                    validate_plan_series_registration(replace(result.plan, projection_receipt=
                        replace(result.plan.projection_receipt, fingerprint='0' * 64)), result.state)
                runtime.assert_sources_unchanged(self)

    async def test_canonical_builder_disconnect_cannot_leave_approvable_plan(self):
        from src.rename import series_registration
        with tempfile.TemporaryDirectory() as tmp:
            async with MultiTaskFlow(Path(tmp)) as runtime:
                with patch('src.rename.series_registration.build_series_registration_authority',
                           side_effect=ValueError('registration authority disconnected')) as builder:
                    result = await runtime.add(MEMBERS[0], confirm=False)
                    context = current_provider_selection_context(result.task)
                    if context and context.get('state') == 'awaiting_user':
                        _apply_boundary(result)
                    self.assertGreater(builder.call_count, 0)
                    with self.assertRaises((PlanReviewError, ValueError)):
                        await runtime.approve(runtime.result(result.task_id))
                    self.assertEqual((), runtime.review.load_or_create(runtime.result(result.task_id).plan).approved_entry_ids)
                    self.assertNotIn(read_task_at(runtime.tasks, result.task_id)['workflow_status'],
                                     ('approved', 'queued', 'running', 'completed'))
                runtime.assert_sources_unchanged(self)
                self.assertFalse((runtime.root / 'Anime').exists())

    async def test_first_registration_does_not_stale_other_preapproved_season(self):
        await self._assert_catalog_sequence(no_match=False)

    async def test_no_tmdb_cohort_can_add_a_not_yet_materialized_member(self):
        await self._assert_catalog_sequence(no_match=True)

    async def _assert_catalog_sequence(self, *, no_match):
        with tempfile.TemporaryDirectory() as tmp:
            async with MultiTaskFlow(Path(tmp), no_match=no_match) as runtime:
                first, second = [await runtime.add(subject) for subject in MEMBERS[:2]]
                first_plan, old_peer_plan = await runtime.approve(first), await runtime.approve(second)
                first_fp, old_peer_fp = map(PlanStore.fingerprint, (first_plan, old_peer_plan))
                self.assertEqual(0, binding_from_decision(first.state).revision)
                self.assertEqual(0, binding_from_decision(second.state).revision)
                # Real transfer path; only tiny synthetic episode bytes in this
                # TemporaryDirectory can ever be hardlinked by the fixture.
                await runtime.manager.enqueue_approved(first.task_id, first_fp)
                await runtime.joined()
                first_task = read_task_at(runtime.tasks, first.task_id)
                self.assertEqual('completed', first_task['execution_status'])
                self.assertEqual('partial_failed', first_task['workflow_status'])
                for entry in first_plan.entries:
                    self.assertTrue(entry.source_path.samefile(entry.target_path))
                first_bytes = (runtime.tasks / f'{first.task_id}.json').read_bytes()
                approval_path = runtime.review._path_helper.path_for(second.task_id)
                approval_bytes = approval_path.read_bytes()
                runtime.execution.validate_approved(second.task_id, old_peer_fp)
                if not no_match:
                    self.assertEqual(first_plan.metadata['provider_link_snapshot']['mapping_id'],
                        old_peer_plan.metadata['provider_link_snapshot']['mapping_id'])
                    # A stored link conflict must be caught before this season
                    # writes media, rather than failing at Catalog publication.
                    link_path = next((runtime.root / 'provider_links').glob('*.json'))
                    link_bytes = link_path.read_bytes()
                    try:
                        damaged_link = json.loads(link_bytes)
                        damaged_link['local_series_key'] = 'series-00000000-0000-0000-0000-000000000001'
                        link_path.write_text(json.dumps(damaged_link), encoding='utf-8')
                        with self.assertRaises((PlanExecutionError, ValueError)):
                            runtime.execution.validate_approved(second.task_id, old_peer_fp)
                        self.assertTrue(all(not e.target_path.exists() for e in old_peer_plan.entries))
                    finally:
                        link_path.write_bytes(link_bytes)
                self.assertEqual(approval_bytes, approval_path.read_bytes(),
                    'unrelated season materialization must not change the approval')
                await runtime.manager.stop()
                await runtime.manager.start()
                new_peer_plan = old_peer_plan
                new_peer_fp = old_peer_fp
                match = SeriesCatalog(runtime.root / 'series_catalog').load(binding_from_decision(first.state).key)
                self.assertGreater(match.revision, 0)
                self.assertEqual('approved', runtime.review.load_or_create(new_peer_plan).status)
                runtime.execution.validate_approved(second.task_id, new_peer_fp)
                await runtime.manager.enqueue_approved(second.task_id, new_peer_fp)
                await runtime.joined()
                second_task = read_task_at(runtime.tasks, second.task_id)
                self.assertEqual('completed', second_task['execution_status'], second_task.get('error'))
                self.assertEqual('partial_failed', second_task['workflow_status'])
                for entry in new_peer_plan.entries:
                    self.assertTrue(entry.source_path.samefile(entry.target_path))
                catalog = SeriesCatalog(runtime.root / 'series_catalog')
                registered = [catalog.find_by_provider({'namespace': 'bangumi', 'entity_type': 'subject',
                    'provider_id': subject}) for subject in MEMBERS[:2]]
                self.assertTrue(all(item is not None for item in registered),
                    'registration must index later seasons, not only the first selected provider')
                self.assertEqual(registered[0].series_key, registered[1].series_key)
                self.assertEqual(first_bytes, (runtime.tasks / f'{first.task_id}.json').read_bytes())
                runtime.assert_sources_unchanged(self)

    async def test_unrelated_invalid_peer_does_not_block_selected_season(self):
        from src.rename.series_registration import inspect_cross_task_series_consistency
        with tempfile.TemporaryDirectory() as tmp:
            async with MultiTaskFlow(Path(tmp)) as runtime:
                first, second = [await runtime.add(subject) for subject in MEMBERS[:2]]
                self.assertEqual((), tuple(inspect_cross_task_series_consistency((first.plan, second.plan))))
                # Negative-only storage corruption, never a fabricated successful
                # Plan/Decision or an accepted source of registration authority.
                path = runtime.plans / f'{second.task_id}.json'
                raw = json.loads(path.read_text('utf-8'))
                raw['metadata']['series_registration_authority'] = None
                path.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
                await runtime.approve(first)
                with self.assertRaises((PlanReviewError, ValueError)):
                    await runtime.approve(runtime.result(second.task_id))
                runtime.execution.validate_approved(first.task_id, PlanStore.fingerprint(first.plan))
                before = path.read_bytes()
                await runtime.select(second.task_id, MEMBERS[1])
                self.assertEqual(before, path.read_bytes(), 'reselection must not silently repair corrupted authority')
                await runtime.manager.abandon_and_remove(second.task_id, record_root=runtime.root/'records',
                    plan_root=runtime.plans, review_root=runtime.reviews)
                repaired = await runtime.add(MEMBERS[1], source=second.plan.source_root)
                self.assertNotEqual(second.task_id, repaired.task_id)
                self.assertFalse(runtime.review.load_or_create(repaired.plan).approved_entry_ids)
                self.assertIsInstance(repaired.plan.metadata['series_registration_authority'], Mapping)
                self.assertEqual('approved', runtime.review.approve_entries(first.task_id,
                    PlanStore.fingerprint(first.plan), [entry_id(e) for e in first.plan.entries]).status)
                runtime.assert_sources_unchanged(self)

    async def test_unrelated_same_title_is_not_merged_by_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MultiTaskFlow(Path(tmp), no_match=True)
            original = runtime.subjects[MEMBERS[0]]
            runtime.subjects = {MEMBERS[0]: original,
                '999991': dict(original, id=999991, provider_id='999991')}
            runtime.relations = {key: [] for key in runtime.subjects}
            async with runtime:
                first, second = [await runtime.add(subject) for subject in runtime.subjects]
                self.assert_registration(first, ((MEMBERS[0], 1),))
                self.assert_registration(second, (('999991', 1),))
                self.assertEqual(first.plan.work_identity.title, second.plan.work_identity.title)
                self.assertEqual(expected_key(MEMBERS[0]), binding_from_decision(first.state).key)
                self.assertEqual(expected_key('999991'), binding_from_decision(second.state).key)
                self.assertNotEqual(binding_from_decision(first.state).key, binding_from_decision(second.state).key)
                runtime.assert_sources_unchanged(self)

    async def test_two_valid_plans_with_same_anchor_but_different_roots_conflict(self):
        from src.rename.series_registration import inspect_cross_task_series_consistency
        with tempfile.TemporaryDirectory() as tmp:
            plans = []
            for folder in ('one-library', 'another-library'):
                async with MultiTaskFlow(Path(tmp) / folder) as runtime:
                    result = await runtime.add(MEMBERS[0])
                    self.assertEqual([], list(PlanVerifier().verify(result.plan).issues))
                    plans.append(result.plan)
            self.assertEqual(plans[0].metadata['series_key'], plans[1].metadata['series_key'])
            self.assertNotEqual(plans[0].metadata['canonical_work_root'], plans[1].metadata['canonical_work_root'])
            self.assertTrue(inspect_cross_task_series_consistency(plans))

    async def test_two_valid_different_segments_cannot_claim_one_subject(self):
        from src.rename.series_registration import inspect_cross_task_series_consistency
        with tempfile.TemporaryDirectory() as tmp:
            async with MultiTaskFlow(Path(tmp) / 'cohort') as runtime:
                cohort = (await runtime.add(MEMBERS[0])).plan
            singleton_runtime = MultiTaskFlow(Path(tmp) / 'singleton', no_match=True)
            singleton_runtime.subjects = {MEMBERS[1]: singleton_runtime.subjects[MEMBERS[1]]}
            singleton_runtime.relations = {MEMBERS[1]: []}
            async with singleton_runtime:
                singleton = (await singleton_runtime.add(MEMBERS[1])).plan
            self.assertNotEqual(cohort.metadata['series_key'], singleton.metadata['series_key'])
            for plan in (cohort, singleton):
                self.assertEqual([], list(PlanVerifier().verify(plan).issues))
            self.assertTrue(inspect_cross_task_series_consistency((cohort, singleton)))

    async def test_four_season_direct_resolution_uses_first_bangumi_anchor(self):
        for subject in ('290980', '216310', '86670', '41658'):
            with self.subTest(subject=subject), tempfile.TemporaryDirectory() as tmp:
                runtime = MultiTaskFlow(Path(tmp))
                runtime.subjects, runtime.relations = flow.YAMA_SUBJECTS, flow.YAMA_RELATIONS
                runtime.search_type = flow._ProviderSearch
                async with runtime:
                    result = await runtime.add(subject)
                    membership = (('41658', 1), ('86670', 2), ('216310', 3), ('290980', 4))
                    self.assert_registration(result, membership)
                    self.assertEqual(dict(membership)[subject], result.plan.metadata['season_number'])
                    self.assertEqual(expected_key('41658'), binding_from_decision(result.state).key)
                    self.assertIsInstance(result.plan.metadata.get('series_registration_authority'), Mapping)
                    runtime.assert_sources_unchanged(self)

    async def test_explicit_separation_uses_selected_singleton_not_tmdb_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with MultiTaskFlow(Path(tmp)) as runtime:
                result = await runtime.add(MEMBERS[1], confirm=False)
                context = current_provider_selection_context(result.task)
                self.assertEqual('awaiting_user', context['state'])
                applied = _apply_boundary(result, same_work=False)
                self.assertEqual(ReviewCommandApplyResult.APPLIED, applied)
                separated = runtime.result(result.task_id)
                self.assert_registration(separated, ((MEMBERS[1], 1),))
                self.assertEqual(expected_key(MEMBERS[1]), binding_from_decision(separated.state).key)
                await runtime.approve(separated)
                runtime.assert_sources_unchanged(self)

    async def test_current_proof_validator_disconnect_blocks_approval_execution(self):
        from src.rename import series_registration
        with tempfile.TemporaryDirectory() as tmp:
            async with MultiTaskFlow(Path(tmp)) as runtime:
                result = await runtime.add(MEMBERS[0])
                approved = await runtime.approve(result)
                fingerprint = PlanStore.fingerprint(approved)
                with patch('src.rename.series_registration.validate_plan_series_registration',
                           side_effect=ValueError('registration proof disconnected')) as validator:
                    with self.assertRaises((PlanReviewError, ValueError)):
                        runtime.review.approve_entries(result.task_id, fingerprint,
                            [entry_id(e) for e in approved.entries])
                    after_review = validator.call_count
                    self.assertGreater(after_review, 0)
                    with self.assertRaises((PlanExecutionError, ValueError)):
                        runtime.execution.validate_approved(result.task_id, fingerprint)
                    after_execution = validator.call_count
                    self.assertGreater(after_execution, after_review)
                runtime.assert_sources_unchanged(self)
                self.assertFalse((runtime.root / 'Anime').exists())



if __name__ == '__main__':
    unittest.main()
