"""Stage B call-count regressions; component evidence, not live acceptance."""
from tests.queue_fixture import TaskQueueManager as ControlledQueue
import errno
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.rename import path_safety, input_directory_discovery as discovery
from src.rename import default_content
from src.task_queue import TaskQueueManager
from tests import test_default_content_initialization as defaults
from tests import test_r1_selection_operation as operations
from src.utils.utils import write_task_at
from src.task_queue import TaskJob


class PathReads(unittest.TestCase):
    def test_one_metadata_read_per_existing_path_component(self):
        path = Path(tempfile.gettempdir()).resolve() / 'stage-b-not-created'
        calls = []
        def observe(item, *args, **kwargs):
            calls.append(item)
            if item == path:
                raise FileNotFoundError(errno.ENOENT, 'component missing', str(item))
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0)
        with patch.object(Path, 'stat', observe):
            self.assertIsNone(path_safety.first_reparse_component(path))
        self.assertEqual(len(calls), len(set(calls)), 'same component read repeatedly')

    def test_dangling_symlink_and_windows_reparse_are_still_rejected(self):
        path = Path(tempfile.gettempdir()).resolve() / 'stage-b-link'
        for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)):
            def observe(item, *args, **kwargs):
                if item == path and kwargs.get('follow_symlinks', True):
                    raise FileNotFoundError(errno.ENOENT, 'dangling target', str(item))
                return SimpleNamespace(st_mode=mode if item == path else stat.S_IFDIR,
                    st_file_attributes=attributes if item == path else 0)
            with self.subTest(mode=mode), patch.object(Path, 'stat', observe):
                self.assertEqual(path, path_safety.first_reparse_component(path))

    def test_parent_reparse_and_permission_error_are_not_skipped(self):
        parent = Path(tempfile.gettempdir()).resolve() / 'stage-b-parent'
        path = parent / 'Film.mkv'
        for blocked in (False, True):
            def observe(item, *args, **kwargs):
                if item == parent and blocked:
                    raise PermissionError(errno.EACCES, 'denied', str(item))
                return SimpleNamespace(st_mode=stat.S_IFDIR,
                    st_file_attributes=0x400 if item == parent and not kwargs.get('follow_symlinks', True) else 0)
            with self.subTest(blocked=blocked), patch.object(Path, 'stat', observe):
                if blocked:
                    with self.assertRaises(PermissionError):
                        path_safety.first_reparse_component(path)
                else:
                    self.assertEqual(parent, path_safety.first_reparse_component(path))


class AdmissionReads(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_reuses_the_identity_it_just_observed(self):
        from src.rename import source_basis
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            source = root/'Film.mkv'
            source.write_bytes(b'component source')
            expected = discovery.collect_source(source).identities[0].to_dict()
            calls = []
            original = path_safety.first_reparse_component
            def check(path):
                calls.append(path)
                return original(path)
            with patch.object(discovery, 'first_reparse_component', check), \
                    patch.object(source_basis, 'first_reparse_component', check):
                observed = discovery.discover_sources((root,))
            self.assertEqual(expected, observed.groups[0].identities[0].to_dict())
            self.assertEqual(0, calls.count(source), 'DirEntry observation must not rewalk the leaf parent chain')
            self.assertEqual(1, calls.count(root), 'source root safety check must remain')

    async def test_prepare_and_admission_reuse_the_single_discovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root/'incoming'; source.mkdir()
            (source/'Film.mkv').write_bytes(b'component source')
            manager = ControlledQueue(task_root=root/'tasks', plan_root=root/'plans',
                decision_state_root=root/'decisions', recovery_abandon_audit_root=root/'abandon', test_mode=True)
            self.addAsyncCleanup(manager.stop)
            with patch.object(discovery, 'discover_sources', wraps=discovery.discover_sources) as scan:
                observed = discovery.discover_sources((source,))
                batch = manager.prepare_discovered_batch(observed, is_anime=None, is_movie=None,
                     source='ui')
                self.assertEqual(1, scan.call_count, 'preparation repeats the full scan')
                await manager.enqueue_batch(**batch)
                self.assertEqual(1, scan.call_count, '普通接纳不能重新扫描来源')

    async def test_file_added_after_prepare_requires_explicit_refresh_to_join_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root/'incoming'; source.mkdir()
            (source/'Film.mkv').write_bytes(b'component source')
            manager = ControlledQueue(task_root=root/'tasks', plan_root=root/'plans',
                decision_state_root=root/'decisions', recovery_abandon_audit_root=root/'abandon', test_mode=True)
            self.addAsyncCleanup(manager.stop)
            batch = manager.prepare_discovered_batch(discovery.discover_sources((source,)),
                is_anime=None, is_movie=None,  source='ui')
            (source/'new.srt').write_bytes(b'new companion')
            ids=await manager.enqueue_batch(**batch)
            saved=manager._read(ids[0])['task_source_scope']['source_identities']
            self.assertEqual([str(source/'Film.mkv')],[row['source_path'] for row in saved])
            self.assertEqual(2,len(discovery.discover_sources((source,)).identities))


class DefaultBuilds(unittest.IsolatedAsyncioTestCase):
    async def test_tv_completion_does_not_repeat_unused_semantic_builds(self):
        from src.rename import initial_tv_completion as tv
        from tests.test_initial_tv_completion_contract import InitialTVCompletionContracts
        helper = InitialTVCompletionContracts('runTest')
        self.addCleanup(helper.doCleanups)
        with patch.object(tv, '_semantic', wraps=tv._semantic) as build:
            _, _, _, result, *_ = await helper.fixture(anime=True, companions=True)
        helper.assert_success(result)
        self.assertLessEqual(build.call_count, 5, 'validation repeats an unused complete TV build')

    async def test_preview_and_canonical_commit_each_build_once(self):
        helper = defaults.DefaultInitializationContracts('runTest')
        self.addCleanup(helper.doCleanups)
        root = helper.root()
        service, plan, *_ = await helper.ready(root)
        with patch.object(default_content, 'build_default_initialization',
                          wraps=default_content.build_default_initialization) as build:
            preview = helper.prepare(service, plan)
            helper.commit(service, preview)
        self.assertEqual(2, build.call_count, 'preview+canonical only; no middle prepare')

    async def test_repeated_commit_rejects_changed_preview_and_decision(self):
        helper = defaults.DefaultInitializationContracts('runTest')
        self.addCleanup(helper.doCleanups)
        root = helper.root()
        service, plan, *_ = await helper.ready(root)
        plan = helper.commit(service, helper.prepare(service, plan))
        preview = helper.prepare(service, plan)
        for field in ('decision_fingerprint', 'selection'):
            raw = preview.to_dict()
            if field == 'selection':
                raw[field]['reason'] = 'altered preview'
            else:
                raw[field] = '0' * 64
            before = defaults.records(root)
            with self.assertRaises(ValueError):
                helper.commit(service, type(preview).from_mapping(raw))
            self.assertEqual(before, defaults.records(root))


class FailureReasons(unittest.IsolatedAsyncioTestCase):
    async def test_transport_code_overrides_relation_wording_but_not_real_relation(self):
        for reasons, expected in ((['subject_candidate_transport_error'], 'provider_unavailable'),
                                  (['no_reliable_relation'], 'relation_unresolved')):
            with self.subTest(reasons=reasons), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); task_id = 'failure-reason'
                tasks, decisions, plans = operations._seed_authority(root, task_id)
                manager = ControlledQueue(task_root=tasks, decision_state_root=decisions,
                    plan_root=plans, recovery_abandon_audit_root=root/'audit', test_mode=True)
                current = manager._read(task_id)
                operations.persist_provider_selection_attempt(current, dict(request_id='request',
                    state='pending', candidate=operations._selection()))
                current['provider_selection_failure'] = dict(reason_codes=reasons)
                write_task_at(tasks, task_id, current)
                manager._fail_reidentify(TaskJob(task_id=task_id, path=root/'source.mkv',
                    provider_selection=operations._selection(), operation='reidentify',
                    expected_revision=0, request_id='request'), RuntimeError('没有找到可靠的季度匹配'))
                operation = operations.current_provider_selection_context(manager._read(task_id))
                self.assertEqual(expected, operation['failure_code'])
                self.assertEqual(reasons, operation['reason_codes'])

    async def test_new_request_clears_previous_attempt_transport_evidence(self):
        from tests.test_default_activation_contract import DefaultActivationProducerContracts
        from tests.test_provider_subject_plan_wiring_red_contract import _root_config_patch
        from tests.test_r1_real_media_production_red_contract import _configured_renamer
        harness = DefaultActivationProducerContracts('runTest')
        async with harness.flow(movie=True, automatic=False) as flow:
            _, state = harness.current(flow)
            flow.manager._update(flow.task_id, provider_selection_failure={
                'reason_codes': ['subject_candidate_transport_error']})
            observed = []
            async def fail(job):
                observed.append(flow.manager._read(job.task_id).get('provider_selection_failure'))
                raise RuntimeError('保存失败')
            flow.manager.executor = fail
            with _root_config_patch(flow.manager.root_snapshot_source.observe().to_dict()), patch(
                    'src.task_queue.Rename', side_effect=lambda **_: _configured_renamer(flow.root)):
                await flow.manager.enqueue_reidentify(flow.task_id, dict(namespace='tmdb',
                    entity_type='movie', provider_id='8181', title='普通电影'), state.revision)
                await flow.manager._queue.join()
            self.assertEqual([None], observed)
            self.assertEqual('save_failed', operations.current_provider_selection_context(
                flow.manager._read(flow.task_id))['failure_code'])
