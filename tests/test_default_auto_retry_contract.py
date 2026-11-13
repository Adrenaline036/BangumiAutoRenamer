"""Real failed-auto card -> current GUI retry admission -> canonical plan."""
import asyncio
from dataclasses import replace
import unittest

from src.rename.automatic_recognition import AutomaticRecognitionOrigin, validate_original_job
from src.rename.recognition_queue_admission import capture_workspace_open
from tests import test_default_activation_contract as activation
from tests.source_collection_fixture import admit_task
from tests.workflow_gui_fixture import confirm_restart


class DefaultAutoRetryContracts(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow

    def configure(self, worker):
        self.available = False
        self.calls = []
        worker.bangumi_search = None
        for kind in ('tv', 'movie'):
            original = getattr(worker.search, 'get_' + kind + '_info')
            def query(*args, _original=original, _kind=kind):
                self.calls.append(_kind)
                return _original(*args) if self.available else ('', None)
            setattr(worker.search, 'get_' + kind + '_info', query)

    async def admit(self, manager, source, task_id):
        await admit_task(manager, source, task_id=task_id, is_anime=None, is_movie=None,
            custom_name='Ordinary Show')

    async def test_correct_name_or_retry_without_type_enters_current_auto_job(self):
        from src.pages import plan_review_page as page
        for mode, single_file in (('workspace', False), ('retry', False), ('workspace', True)):
            with self.subTest(mode=mode, single_file=single_file):
                async with self.flow(admit=self.admit, configure_worker=self.configure,
                        configure_source=(lambda root, source: next(source.glob('*S01E01*.mkv')))
                            if single_file else None) as flow:
                    before = flow.manager._read(flow.task_id)
                    old_origin = AutomaticRecognitionOrigin.from_mapping(before['automatic_recognition_origin'])
                    self.assertEqual('need_confirm', before['workflow_status'])
                    self.assertFalse(flow.store.path_for(flow.task_id).exists())
                    self.available = True
                    self.calls.clear()
                    sample_root = flow.source.parent if single_file else flow.source
                    source_bytes = {p: p.read_bytes() for p in sample_root.rglob('*') if p.is_file()}
                    if mode == 'workspace':
                        saved = await confirm_restart(lambda: page._reenqueue_from_workspace(flow.task_id, before,
                            path_value=flow.source, media_type='auto', name_value='Ordinary Show',
                            season_value=None, 
                            opened_evidence=capture_workspace_open(flow.manager, flow.task_id, before)))
                        self.assertTrue(saved)
                    else:
                        await flow.manager.retry_current(flow.task_id)
                    await asyncio.wait_for(flow.manager._queue.join(), 12)
                    task = flow.manager._read(flow.task_id)
                    self.assertEqual({'tv', 'movie'}, set(self.calls), task.get('error'))
                    self.assertTrue(flow.store.path_for(flow.task_id).exists(), task.get('error'))
                    state = flow.service.decision_state_store.load(flow.task_id)
                    self.assertTrue(state.confirmed_work, task.get('error'))
                    origin = AutomaticRecognitionOrigin.from_mapping(task['automatic_recognition_origin'])
                    self.assertNotEqual(old_origin.job_id, origin.job_id)
                    self.assertIsNone(origin.is_anime)
                    self.assertIsNone(origin.is_movie)
                    self.assertEqual(before['task_source_scope'], task['task_source_scope'])
                    self.assertEqual(task, validate_original_job(flow.service, origin)[0])
                    with self.assertRaises(ValueError):
                        validate_original_job(flow.service, old_origin)
                    self.assertEqual(source_bytes, {p: p.read_bytes() for p in sample_root.rglob('*') if p.is_file()})
                    self.assertEqual('need_confirm', task['workflow_status'])
                    self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))
                    self.assertTrue(all(not e.target_path.exists() for e in flow.store.load(flow.task_id).entries))

    async def test_retry_drift_or_incomplete_provider_batch_cannot_publish(self):
        from src.rename.provider_search import ProviderSearchStatus
        from src.rename.default_activation import service_for_worker
        from src.utils.utils import mutate_task_at
        for fault in ('job', 'partial', 'trimmed'):
            with self.subTest(fault=fault):
                def configure(worker):
                    self.configure(worker)
                    original = worker._provider_search_batch
                    def altered(*args, **kwargs):
                        batch = original(*args, **kwargs)
                        if not self.available:
                            return batch
                        if fault == 'job':
                            service = service_for_worker(worker)
                            mutate_task_at(service.task_root, 'default-activation',
                                lambda data: {**data, 'recognition_job_id': 'a-later-request'})
                        elif fault == 'partial':
                            return replace(batch, results=tuple(replace(row, status=ProviderSearchStatus.TIMEOUT)
                                if row.source == 'tmdb_movie' else row for row in batch.results))
                        else:
                            return replace(batch, policy=replace(batch.policy,
                                expected_sources=('tmdb_tv',), scheduled_sources=('tmdb_tv',)),
                                results=tuple(row for row in batch.results if row.source == 'tmdb_tv'))
                        return batch
                    worker._provider_search_batch = altered
                async with self.flow(admit=self.admit, configure_worker=configure) as flow:
                    self.available = True
                    self.calls.clear()
                    await flow.manager.retry_current(flow.task_id)
                    await asyncio.wait_for(flow.manager._queue.join(), 12)
                    self.assertEqual({'tv', 'movie'}, set(self.calls))
                    self.assertFalse(flow.store.path_for(flow.task_id).exists())
                    self.assertFalse(flow.service.decision_state_store.path_for(flow.task_id).exists())
                    self.assertTrue(flow.manager._read(flow.task_id).get('error'))

    async def test_stale_open_window_cannot_replace_a_later_request(self):
        from src.pages import plan_review_page as page
        from tests.test_content_choice_contract import records
        async with self.flow(admit=self.admit, configure_worker=self.configure) as flow:
            task = flow.manager._read(flow.task_id)
            opened = capture_workspace_open(flow.manager, flow.task_id, task)
            flow.manager._write(flow.task_id, {**task, 'name': 'a later manual request'})
            before = records(flow.root)
            self.available = True
            self.calls.clear()
            saved = await confirm_restart(lambda: page._reenqueue_from_workspace(flow.task_id, task,
                path_value=flow.source, media_type='auto', name_value='Ordinary Show',
                season_value=None,  opened_evidence=opened))
            self.assertFalse(saved)
            self.assertEqual([], self.calls)
            self.assertEqual(before, records(flow.root))
