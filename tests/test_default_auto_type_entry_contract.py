"""The real AddTask None/None admission must reach provider discovery."""
import asyncio
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from src.pages.add_task_page import media_flags
from src.rename.automatic_recognition import AutomaticRecognitionOrigin, validate_original_job
from src.rename.plan_store import PlanStore
from tests import test_default_activation_contract as activation


class DefaultAutoTypeEntryContracts(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow

    async def admit_auto(self, manager, source, task_id):
        anime, movie = media_flags('auto')
        self.assertIsNone(anime)
        self.assertIsNone(movie)
        prepared = manager.prepare_input_batch((source,), is_anime=anime, is_movie=movie,
             source='ui', preview_only=False)
        ids = await manager.enqueue_batch(**prepared)
        # The production batch generates IDs; expose the single actual one to
        # this test without changing the queued job or stored task.
        self.actual_task_id = ids[0]
        self.source_before = {str(path): path.read_bytes() for path in source.rglob('*') if path.is_file()}
        self.peer_before = {'uuid': 'isolated-peer', 'workflow_status': 'need_confirm', 'name': '另一张卡'}
        manager._write('isolated-peer', self.peer_before)

    def instrument(self, worker, *, missing_genre=False, no_match=False, conflict=False, animation=False):
        self.calls = []
        worker.bangumi_search = None  # this fixture explicitly configures TMDB only
        for kind in ('tv', 'movie'):
            method = getattr(worker.search, 'get_' + kind + '_info')
            def observed(*args, _kind=kind, _method=method):
                self.calls.append(_kind)
                if no_match:
                    return '', None
                name, info = _method(*args)
                if info and missing_genre:
                    info = {**info, 'genres': [], 'genre_ids': []}
                if info and conflict:
                    info = {**info, 'genres': [{'id': 18, 'name': '动画'}]}
                if info and animation:
                    info = {**info, 'genres': [{'id': 16, 'name': '动画'}]}
                return name, info
            setattr(worker.search, 'get_' + kind + '_info', observed)

    async def test_default_auto_tv_and_movie_reach_complete_unapproved_plan(self):
        for movie, animation in ((False, False), (True, False), (True, True)):
            with self.subTest(movie=movie, animation=animation):
                async with self.flow(movie=movie, admit=self.admit_auto,
                                     configure_worker=lambda worker: self.instrument(worker, animation=animation)) as flow:
                    task_id = self.actual_task_id
                    task = flow.manager._read(task_id)
                    self.assertEqual({'tv', 'movie'}, set(self.calls), task.get('error'))
                    self.assertTrue(flow.store.path_for(task_id).exists(), task.get('error'))
                    plan = flow.store.load(task_id)
                    from src.rename.plan_verifier import PlanVerifier
                    self.assertEqual(set() if movie else set(),
                                     {i.code for i in PlanVerifier().verify(plan).issues})
                    state = flow.service.decision_state_store.load(task_id)
                    self.assertTrue(state.confirmed_work, task.get('error'))
                    self.assertEqual('animation' if animation else 'general', state.library_domain)
                    self.assertEqual('movie' if movie else 'tv', state.confirmed_work['primary_provider']['entity_type'])
                    self.assertEqual(movie, task['is_movie'])
                    self.assertIs(task['is_anime'], animation)
                    origin = AutomaticRecognitionOrigin.from_mapping(task['automatic_recognition_origin'])
                    self.assertIsNone(origin.is_anime)
                    self.assertIsNone(origin.is_movie)
                    self.assertEqual(origin.to_dict(), plan.metadata['automatic_recognition_origin'])
                    self.assertTrue(plan.entries)
                    self.assertTrue(all(not entry.target_path.exists() for entry in plan.entries))
                    self.assertEqual(self.source_before,
                        {str(path): path.read_bytes() for path in flow.source.rglob('*') if path.is_file()})
                    self.assertEqual(self.peer_before, flow.manager._read('isolated-peer'))
                    self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))
                    if flow.manager.review_root.exists():
                        import json
                        for review in flow.manager.review_root.glob('*.json'):
                            self.assertFalse(json.loads(review.read_text(encoding='utf-8')).get('approved_entry_ids'))
                    self.assertIsNotNone(state.content_selection)
                    if movie:
                        self.assertIsNotNone(state.movie_version_selection)
                    else:
                        self.assertEqual('initial_tv_completion', plan.revision_action)
                        self.assertEqual(1, state.season_binding['season_number'])
                        self.assertTrue(all(entry.season_id == 1 for entry in plan.entries if entry.media_kind == 'video'))

    async def test_unknown_type_or_no_match_stays_visible_without_guessing_a_workspace(self):
        for reason in ('missing_genre', 'no_match', 'conflict', 'animation'):
            with self.subTest(reason=reason):
                async with self.flow(admit=self.admit_auto,
                        configure_worker=lambda worker: self.instrument(worker, **{reason: True})) as flow:
                    task_id = self.actual_task_id
                    task = flow.manager._read(task_id)
                    self.assertEqual({'tv', 'movie'}, set(self.calls), task.get('error'))
                    self.assertEqual('need_confirm', task['workflow_status'])
                    self.assertTrue(task.get('error'))
                    self.assertIsNone(task['is_anime'])
                    self.assertIsNone(task['is_movie'])
                    self.assertFalse(flow.store.path_for(task_id).exists())
                    self.assertFalse(flow.service.decision_state_store.path_for(task_id).exists())
                    self.assertEqual(self.peer_before, flow.manager._read('isolated-peer'))

    async def test_source_job_drift_and_disconnected_resolver_cannot_publish(self):
        from src.rename import automatic_recognition
        for fault in ('source', 'job', 'resolver'):
            with self.subTest(fault=fault):
                def configure(worker):
                    self.instrument(worker)
                    original = worker._provider_search_batch
                    def changed(*args, **kwargs):
                        batch = original(*args, **kwargs)
                        task_id = self.actual_task_id
                        if fault == 'source':
                            from pathlib import Path
                            first = Path(next(iter(self.source_before)))
                            first.write_bytes(first.read_bytes() + b'actual-source-drift')
                        elif fault == 'job':
                            from src.utils.utils import mutate_task_at
                            from src.rename.default_activation import service_for_worker
                            service = service_for_worker(worker)
                            mutate_task_at(service.task_root, task_id,
                                lambda task: {**task, 'recognition_job_id': 'later-recognition-job'})
                        return batch
                    worker._provider_search_batch = changed
                from contextlib import nullcontext
                disconnected = (patch.object(automatic_recognition.AutomaticRecognition, 'resolve',
                    return_value=None) if fault == 'resolver' else nullcontext())
                with disconnected:
                    async with self.flow(admit=self.admit_auto, configure_worker=configure) as flow:
                        self.assertEqual({'tv', 'movie'}, set(self.calls))
                        self.assertFalse(flow.store.path_for(self.actual_task_id).exists())
                        self.assertFalse(flow.service.decision_state_store.path_for(self.actual_task_id).exists())
                        self.assertEqual(self.peer_before, flow.manager._read('isolated-peer'))

    async def test_movie_identity_result_is_bound_and_resumes_in_a_fresh_queue_without_search(self):
        from src.task_queue import TaskQueueManager
        from tests.test_r1_real_media_production_red_contract import _configured_renamer
        from tests.test_content_choice_contract import records
        reached = []
        def interrupt(transaction, envelope, terminal, forward):
            if envelope.draft_plan.revision_action == 'initial_movie_completion' and not reached:
                reached.append(True)
                raise ValueError('isolated interruption after movie identity')
            return forward()
        async with self.flow(movie=True, admit=self.admit_auto,
                configure_worker=self.instrument, hook=interrupt) as flow:
            task_id = self.actual_task_id
            self.assertEqual([True], reached)
            task = flow.manager._read(task_id)
            origin = AutomaticRecognitionOrigin.from_mapping(task['automatic_recognition_origin'])
            plan = flow.store.load(task_id)
            state = flow.service.decision_state_store.load(task_id)
            self.assertTrue(state.confirmed_work)
            self.assertIsNone(state.movie_version_selection)
            self.assertEqual(task, validate_original_job(flow.service, origin)[0])
            plan_path = flow.store.path_for(task_id)
            old_plan = plan_path.read_bytes()
            for fault in ('wrong_domain', 'non_boolean', 'later_job', 'receipt', 'proof'):
                with self.subTest(fault=fault):
                    changed = dict(task)
                    if fault == 'wrong_domain':
                        changed['is_anime'] = True
                    elif fault == 'non_boolean':
                        changed['is_anime'] = 0
                    elif fault == 'later_job':
                        changed['recognition_job_id'] = 'a-later-user-request'
                    elif fault == 'receipt':
                        value = json.loads(old_plan)
                        value['projection_receipt'] = None
                        plan_path.write_text(json.dumps(value), encoding='utf-8')
                    else:
                        value = json.loads(old_plan)
                        value['metadata']['automatic_recognition']['origin']['job_id'] = 'forged-result'
                        plan_path.write_text(json.dumps(value), encoding='utf-8')
                    flow.manager._write(task_id, changed)
                    before = records(flow.root)
                    try:
                        with self.assertRaises((ValueError, RuntimeError)):
                            validate_original_job(flow.service, origin)
                        self.assertEqual(before, records(flow.root))
                    finally:
                        plan_path.write_bytes(old_plan)
                        flow.manager._write(task_id, task)
            await flow.manager.stop()
            fresh = TaskQueueManager(task_root=flow.manager.task_root, plan_root=flow.store.root,
                decision_state_root=flow.service.decision_state_store.root,
                provider_link_catalog_root=flow.manager.provider_link_catalog_root)
            worker = _configured_renamer(flow.root)
            with patch('src.task_queue.Rename', return_value=worker), patch.object(worker, '_provider_search_batch',
                    side_effect=AssertionError('resume queried Provider')):
                await fresh.start()
                try:
                    await fresh.resume_default_activation(task_id, PlanStore.fingerprint(plan))
                    await asyncio.wait_for(fresh._queue.join(), 12)
                finally:
                    await fresh.stop()
            self.assertIsNotNone(flow.service.decision_state_store.load(task_id).movie_version_selection,
                                 fresh._read(task_id).get('error'))
            from src.rename.plan_verifier import PlanVerifier
            self.assertEqual('need_confirm', fresh._read(task_id)['workflow_status'],
                             (fresh._read(task_id).get('error'), PlanVerifier().verify(flow.store.load(task_id))))
            self.assertEqual(origin.to_dict(), fresh._read(task_id)['automatic_recognition_origin'])

    async def test_partial_provider_failure_or_trimmed_scope_cannot_confirm_work(self):
        from src.rename.provider_search import ProviderSearchStatus
        for fault in ('partial', 'trimmed'):
            with self.subTest(fault=fault):
                def configure(worker):
                    self.instrument(worker)
                    original = worker._provider_search_batch
                    def changed(*args, **kwargs):
                        batch = original(*args, **kwargs)
                        if fault == 'partial':
                            return replace(batch, results=tuple(replace(row, status=ProviderSearchStatus.TIMEOUT)
                                if row.source == 'tmdb_movie' else row for row in batch.results))
                        return replace(batch, policy=replace(batch.policy, expected_sources=('tmdb_tv',),
                            scheduled_sources=('tmdb_tv',)), results=tuple(row for row in batch.results if row.source == 'tmdb_tv'))
                    worker._provider_search_batch = changed
                async with self.flow(admit=self.admit_auto, configure_worker=configure) as flow:
                    task = flow.manager._read(self.actual_task_id)
                    self.assertEqual({'tv', 'movie'}, set(self.calls))
                    self.assertFalse(flow.store.path_for(self.actual_task_id).exists())
                    self.assertFalse(flow.service.decision_state_store.path_for(self.actual_task_id).exists())
                    self.assertTrue(task.get('error'))
                    if fault == 'partial':
                        self.assertEqual('need_confirm', task['workflow_status'])
