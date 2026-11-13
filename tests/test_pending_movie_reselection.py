"""Pending movie re-selection through the real page/queue/worker/transaction.

Frozen provider transport and disposable media only; never approve or execute.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.pages.initial_workspace import create_initial_workspace
from src.pages.plan_review_page import PlanReviewDialog
from src.rename import initial_movie_selection as selection
from src.rename.plan_store import PlanStore
from src.rename.projection_authority import TaskTerminalMutation
from src.rename.review_operation import current_provider_selection_review_operation
from tests import test_default_activation_contract as activation
from tests import test_r1_real_media_production_red_contract as transport
from tests.test_content_choice_contract import change, records


TMDB = dict(namespace='tmdb', entity_type='movie', provider_id='533514',
    title='紫罗兰永恒花园 剧场版', original_title='劇場版 ヴァイオレット・エヴァーガーデン',
    year=2020, library_domain='anime', group_kind='movie_continuous')
BGM = dict(namespace='bangumi', entity_type='subject', provider_id='242216',
    title='剧场版 紫罗兰永恒花园', original_title=TMDB['original_title'], year=2020,
    platform='剧场版', library_domain='anime', group_kind='movie_continuous')
SUBJECT = dict(id=242216, type=2, platform='剧场版', name=BGM['original_title'],
    name_cn=BGM['title'], date='2020-09-18', eps=1, images={})
MOVIE = dict(id=533514, title=TMDB['title'], original_title=TMDB['original_title'],
    release_date='2020-09-18', genres=[{'name': 'Animation'}])


class PendingMovieReselectionContracts(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def flow(self, *, first_link=True):
        def configure(worker):
            worker.search.get_movie_info_by_id = lambda _id: (TMDB['title'], dict(MOVIE))
        harness = activation.DefaultActivationProducerContracts('runTest')
        with (patch.dict(transport.YAMA_SUBJECTS, {'242216': SUBJECT}),
              patch.dict(transport.YAMA_RELATIONS, {'242216': []}),
              patch('requests.sessions.Session.request', side_effect=AssertionError('Live network forbidden'))):
            async with harness.flow(movie=True, is_anime=True, ambiguous=True,
                    automatic=first_link, admission_only=first_link, configure_worker=configure) as flow:
                if first_link:
                    await create_initial_workspace(flow.task_id, flow.manager._read(flow.task_id))
                    flow.manager.executor = flow.manager._execute_rename
                else:
                    # Initial no-match used the real automatic producer. Only
                    # by-ID transport is replaced after that observation.
                    from src.task_queue import Rename
                    configure(Rename())
                yield flow

    async def select(self, flow, candidate):
        dialog = SimpleNamespace(plan=flow.store.load(flow.task_id),
            task_decision=flow.service.decision_state_store.load(flow.task_id),
            task_data=flow.manager._read(flow.task_id))
        await PlanReviewDialog._select_provider_candidate(dialog, dict(candidate))
        await asyncio.wait_for(flow.manager._queue.join(), 12)
        return flow.store.load(flow.task_id)

    def assert_selected(self, flow, candidate):
        plan = flow.store.load(flow.task_id)
        state = flow.service.decision_state_store.load(flow.task_id)
        _, context = current_provider_selection_review_operation(flow.manager._read(flow.task_id))
        self.assertEqual('succeeded', context.get('lifecycle'), flow.manager._read(flow.task_id).get('error'))
        provider = plan.work_identity.primary_provider
        self.assertEqual((candidate['namespace'], candidate['entity_type'], candidate['provider_id']),
                         (provider.namespace, provider.entity_type, provider.provider_id))
        self.assertIsNone(state.layout_mode)
        self.assertIsNone(state.movie_version_selection)
        selection.validate_successful_selection(flow.service, plan, state)
        return plan, state

    async def sequence(self, sequence, *, first_link=True, complete=False):
        async with self.flow(first_link=first_link) as flow:
            initial = flow.store.load(flow.task_id)
            self.assertEqual(first_link, initial.metadata.get('automatic_recognition_origin') is None)
            # A real human exclusion must survive changing the provider.
            changed = change(flow.service, initial, [flow.source / 'Unknown.mkv'])
            basis, choice = changed.initial_source_basis, changed.metadata['content_selection']
            previous_revision = flow.service.decision_state_store.load(flow.task_id).revision
            for index, candidate in enumerate(sequence, 1):
                await self.select(flow, candidate)
                _, actual = current_provider_selection_review_operation(flow.manager._read(flow.task_id))
                print(f'SELECTION_STEP={index}; provider={candidate["namespace"]}:{candidate["provider_id"]}; '
                      f'lifecycle={actual.get("lifecycle")}; '
                      f'revision={flow.service.decision_state_store.load(flow.task_id).revision}; '
                      f'first_link={first_link}', flush=True)
                plan, state = self.assert_selected(flow, candidate)
                self.assertEqual(previous_revision + 1, state.revision)
                previous_revision = state.revision
                self.assertEqual(basis, plan.initial_source_basis)
                self.assertEqual(choice, plan.metadata['content_selection'])
                self.assertEqual(initial.metadata.get('automatic_recognition_origin'),
                                 plan.metadata.get('automatic_recognition_origin'))
            if complete:
                # Reloaded durable plan, then real canonical completion. This
                # catches a fix which selects successfully but breaks history.
                plan = flow.store.load(flow.task_id)
                primary = flow.source / 'Ordinary Movie [1080p].mkv'
                intent = flow.service.prepare_initial_movie_completion(flow.task_id, PlanStore.fingerprint(plan),
                    groups=[dict(sources=[str(primary)], label='1080p')],
                    excluded_sources=[str(e.source_path) for e in plan.entries
                        if e.media_kind == 'video' and e.source_path != primary], reason='确认本次正片')
                result = flow.service.commit_initial_movie_completion(intent)
                self.assertEqual('movie_versions',
                                 flow.service.decision_state_store.load(flow.task_id).layout_mode)
                self.assertIn(flow.source / 'Unknown.mkv', result.ignored_sources)
            for folder in ('Anime', 'AnimeMovies', 'TV', 'Movies'):
                self.assertFalse(any(p.is_file() for p in (flow.root / folder).rglob('*')))

    async def test_first_bangumi_selection_control(self):
        await self.sequence([BGM])

    async def test_first_tmdb_selection_control(self):
        await self.sequence([TMDB])

    async def test_tmdb_bangumi_retry_then_confirm_content(self):
        await self.sequence([TMDB, BGM, BGM], complete=True)

    async def test_bangumi_tmdb_then_confirm_content(self):
        await self.sequence([BGM, TMDB], complete=True)

    async def test_same_provider_reselection(self):
        await self.sequence([TMDB, TMDB])

    async def test_automatic_origin_survives_reselection(self):
        await self.sequence([TMDB, BGM], first_link=False, complete=True)

    async def test_disconnected_command_cannot_publish_or_fallback(self):
        async with self.flow() as flow:
            await self.select(flow, TMDB)
            plan, state = self.assert_selected(flow, TMDB)
            with patch.object(selection, 'build', return_value=None) as disconnected:
                await self.select(flow, BGM)
            disconnected.assert_called()
            self.assertEqual(plan, flow.store.load(flow.task_id))
            self.assertEqual(state, flow.service.decision_state_store.load(flow.task_id))
            _, context = current_provider_selection_review_operation(flow.manager._read(flow.task_id))
            self.assertEqual('failed', context.get('lifecycle'))
            await self.select(flow, BGM)
            self.assert_selected(flow, BGM)

    async def test_execution_trace_and_stale_revision_cannot_enqueue(self):
        async with self.flow() as flow:
            await self.select(flow, TMDB)
            plan, state = self.assert_selected(flow, TMDB)
            before = records(flow.root)
            with self.assertRaises((ValueError, RuntimeError)):
                await flow.manager.enqueue_reidentify(flow.task_id, BGM, state.revision - 1)
            self.assertEqual(before, records(flow.root))
            flow.manager._update(flow.task_id, execution_may_have_written_targets=True,
                                 execution_requires_manual_check=True)
            before = records(flow.root)
            with self.assertRaises((ValueError, RuntimeError)):
                await flow.manager.enqueue_reidentify(flow.task_id, BGM, state.revision)
            self.assertEqual(before, records(flow.root))
            self.assertEqual(plan, flow.store.load(flow.task_id))

    async def test_reselection_checks_request_sources_and_execution_again_inside_worker(self):
        async with self.flow() as flow:
            await self.select(flow, TMDB)
            reached = []
            original = selection.prepare
            def checked(service, recognition):
                intent = original(service, recognition)
                reached.append(intent)
                before = records(flow.root)
                def publish(command):
                    terminal = TaskTerminalMutation.prepare(service.task_root, command.task_id,
                        lambda _s, _p, task: task)
                    return selection.commit(service, command, task_terminal=terminal)
                for changes in ({'request_id': 'later-request'}, {'operation_fingerprint': '0' * 64},
                        {'plan_fingerprint': '0' * 64}, {'decision_fingerprint': '0' * 64},
                        {'basis_fingerprint': '0' * 64}):
                    with self.subTest(changes=changes), self.assertRaises((ValueError, RuntimeError)):
                        publish(replace(intent, **changes))
                    self.assertEqual(before, records(flow.root))
                pin = recognition.initial_movie_request
                for bad in (replace(pin, request_id='old-worker-request'),
                            replace(pin, expected_revision=pin.expected_revision - 1)):
                    with self.assertRaises((ValueError, RuntimeError)):
                        publish(original(service, replace(recognition, initial_movie_request=bad)))
                    self.assertEqual(before, records(flow.root))
                wrong_domain = replace(recognition.work_identity, domain='general')
                with self.assertRaises((ValueError, RuntimeError), msg='domain drift must reject'):
                    publish(original(service, replace(recognition, work_identity=wrong_domain)))
                task_path = flow.root / 'tasks' / f'{flow.task_id}.json'
                raw = task_path.read_bytes()
                try:
                    task = json.loads(raw)
                    task['execution_may_have_written_targets'] = True
                    task_path.write_text(json.dumps(task), encoding='utf-8')
                    with self.assertRaises((ValueError, RuntimeError), msg='execution trace must reject inside worker'):
                        publish(intent)
                finally:
                    task_path.write_bytes(raw)
                try:
                    task = json.loads(raw)
                    task['task_source_scope']['reservation_revision'] += 1
                    task_path.write_text(json.dumps(task), encoding='utf-8')
                    with self.assertRaises((ValueError, RuntimeError), msg='source scope drift must reject'):
                        publish(intent)
                finally:
                    task_path.write_bytes(raw)
                self.assertEqual(before, records(flow.root))
                return intent
            with patch.object(selection, 'prepare', side_effect=checked):
                await self.select(flow, BGM)
            self.assertEqual(1, len(reached), 'real second-selection worker must run the typed command')
            self.assert_selected(flow, BGM)

    async def test_older_selection_history_is_required_after_intervening_content_edit(self):
        async with self.flow() as flow:
            await self.select(flow, TMDB)
            first, _ = self.assert_selected(flow, TMDB)
            changed = change(flow.service, first, [flow.source / 'Unknown.mkv'])
            await self.select(flow, BGM)
            plan, state = self.assert_selected(flow, BGM)
            self.assertEqual(changed.metadata['content_selection'], plan.metadata['content_selection'])
            before = records(flow.root)
            original = flow.store.load_version
            def missing(task_id, fingerprint):
                if fingerprint == first.revision_parent_fingerprint:
                    raise ValueError('oldest actual selection parent missing')
                return original(task_id, fingerprint)
            with patch.object(flow.store, 'load_version', side_effect=missing):
                with self.assertRaisesRegex(ValueError, 'oldest actual'):
                    selection.validate_successful_selection(flow.service, plan, state)
            self.assertEqual(before, records(flow.root))

    async def test_pending_boundary_does_not_consume_frozen_human_decisions(self):
        async with self.flow() as flow:
            await self.select(flow, TMDB)
            plan, state = self.assert_selected(flow, TMDB)
            eligible = getattr(selection, 'pending_movie_selection', None)
            self.assertTrue(callable(eligible), 'missing shared pending-movie boundary')
            self.assertTrue(eligible(plan, state))
            before = records(flow.root)
            for changes in ({'layout_mode': 'movie_versions'}, {'layout_mode': 'tv_episodes'},
                    {'season_binding': {}}, {'series_binding': {}}, {'catalog_link_intent': {}},
                    {'source_episode_projection': {}}, {'source_episode_projection_decision': {}},
                    {'manual_role_decisions': {'video': 'bonus'}},
                    {'provider_special_decisions': {'video': {'action': 'unmatched_extra'}}},
                    {'provider_subject_boundary_decisions': {'subject': {}}},
                    {'recovery_state': 'catalog_missing'}):
                with self.subTest(changes=changes):
                    self.assertFalse(eligible(plan, replace(state, **changes)))
            self.assertFalse(eligible(replace(plan, metadata={**plan.metadata,
                                     'font_contribution': {}}), state))
            tv_work = replace(plan.work_identity, primary_provider=replace(
                plan.work_identity.primary_provider, entity_type='tv'))
            self.assertFalse(eligible(replace(plan, work_identity=tv_work),
                                     replace(state, confirmed_work=tv_work.to_dict())))
            self.assertEqual(before, records(flow.root))


if __name__ == '__main__':
    unittest.main()
