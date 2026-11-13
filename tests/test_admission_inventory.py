"""Public admission and first-link regressions with disposable inputs."""
from tests.queue_fixture import TaskQueueManager as ControlledQueue
import json
import hashlib
import asyncio
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from src.rename import process, default_activation
from src.rename.plan_store import PlanStore
from src.rename.plan_review import PlanReviewService
from src.rename.target_routing import ROOT_KEYS, TargetRootSnapshot
from src.task_queue import TaskQueueManager
from src.utils.utils import write_task_at
from tests import test_default_movie_completion as initial_movie


def workspace_files(root):
    """整个合成根逐文件留证；锁独立检查，运行器位于根外。"""
    business, locks = {}, {}
    for path in sorted(root.rglob('*')):
        if path.is_file():
            raw = path.read_bytes()
            row = (len(raw), hashlib.sha256(raw).hexdigest())
            (locks if path.suffix == '.lock' else business)[path.relative_to(root).as_posix()] = row
    return business, locks


async def assert_released_locks(test, root, task_root, task_id):
    from src.rename.file_lock import advisory_file_lock, task_operation_lock
    paths = sorted(root.rglob('*.lock'))
    def acquire():
        for path in paths:
            with advisory_file_lock(path, owner={'kind': 'admission-rejection-check'}):
                pass
        with task_operation_lock(task_root, task_id, operation='admission-rejection-check'):
            pass
    # 换线程重新取得相同锁，避免同线程重入掩盖未释放。
    await asyncio.wait_for(asyncio.to_thread(acquire), 5)
    print('REJECTION_LOCKS', json.dumps([p.relative_to(root).as_posix() for p in paths]))


class AdmissionInventory(unittest.IsolatedAsyncioTestCase):
    async def test_movie_first_link_uses_admitted_inventory(self):
        helper = initial_movie.InitialMovieCompletionContracts('runTest')
        self.addCleanup(helper.doCleanups)
        root = helper.root()
        started = time.perf_counter()
        active, scans = False, []
        original, scandir = process.Rename._process, os.scandir
        def selecting(renamer, *args, **kwargs):
            nonlocal active
            active = bool(kwargs.get('provider_selection'))
            try:
                return original(renamer, *args, **kwargs)
            finally:
                active = False
        def scan(path):
            if active and Path(path).is_relative_to(root/'sources'):
                scans.append(str(path))
            return scandir(path)
        with patch.object(process.Rename, '_process', selecting), patch.object(os, 'scandir', scan):
            service, result, before, _ = await helper.fixture(root, initialize_before_selection=True)
        print('FIRST_LINK_MEASURE', json.dumps(dict(seconds=time.perf_counter()-started, scans=len(scans))))
        self.assertTrue(result.state.confirmed_work)
        self.assertEqual(before['state'].initial_source_basis, result.state.initial_source_basis)
        self.assertEqual(before['state'].content_selection.to_dict()['excluded_sources'],
                         result.state.content_selection.to_dict()['excluded_sources'])
        self.assertEqual([], scans, 'first link must project the admitted inventory')

    async def test_legacy_workspace_rejects_before_observation(self):
        from src.rename import source_basis
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'source'; source.mkdir()
            (source/'Movie.mkv').write_bytes(b'isolated legacy inventory')
            write_task_at(root/'tasks', 'legacy', dict(uuid='legacy', path=str(source),
                is_anime=False, is_movie=True, workflow_status='need_confirm'))
            service = PlanReviewService(PlanStore(root/'plans'), root/'reviews', root/'catalog',
                decision_state_root=root/'decisions', task_root=root/'tasks', record_root=root/'records')
            roots = TargetRootSnapshot.capture({key: root/'targets'/key for key in ROOT_KEYS})
            before, locks_before = workspace_files(root)
            # 计数仅覆盖工作区入口，来源造景已完成。
            for attempt in range(2):
                with patch.object(default_activation, 'inventory_movie_assets', wraps=default_activation.inventory_movie_assets) as scan, \
                        patch.object(source_basis, 'observe_admitted_basis', wraps=source_basis.observe_admitted_basis) as admitted, \
                        patch.object(source_basis, 'observe_initial_input', wraps=source_basis.observe_initial_input) as observe:
                    with self.assertRaises(ValueError) as raised:
                        default_activation.ensure_workspace(service, 'legacy', roots=roots)
                with self.subTest(attempt=attempt, check='entry reason'):
                    self.assertIn('来源记录不完整，无法建立整理计划', str(raised.exception))
                with self.subTest(attempt=attempt, check='no observation'):
                    self.assertEqual((0, 0, 0), (scan.call_count, admitted.call_count, observe.call_count))
                self.assertEqual(before, workspace_files(root)[0])
            print('REJECTION_FILES', json.dumps(dict(before=before, after=workspace_files(root)[0],
                locks_before=locks_before, locks_after=workspace_files(root)[1]), ensure_ascii=False))
            await assert_released_locks(self, root, service.task_root, 'legacy')
            self.assertEqual(before, workspace_files(root)[0])

    async def test_current_workspace_reopens_without_observation_or_rewriting(self):
        from src.rename import source_basis
        from src.rename.content_policy import ContentPolicy
        helper = initial_movie.InitialMovieCompletionContracts('runTest')
        self.addCleanup(helper.doCleanups)
        root = helper.root()
        service, result, _, _ = await helper.fixture(root, initialize_before_selection=True)
        self.assertEqual(ContentPolicy.SAFE_WATCHABLE, result.state.content_policy)
        roots = TargetRootSnapshot.capture({key: root/'targets'/key for key in ROOT_KEYS})
        before, _ = workspace_files(root)
        with patch.object(default_activation, 'inventory_movie_assets', side_effect=AssertionError('reopen scanned')), \
                patch.object(source_basis, 'observe_admitted_basis', side_effect=AssertionError('reopen observed')):
            for _ in range(2):
                self.assertEqual((result.state, result.plan), default_activation.ensure_workspace(service, result.plan.task_id, roots=roots))
            with patch.object(service.decision_state_store, 'path_for', return_value=root/'missing-decision.json'):
                with self.assertRaisesRegex(ValueError, '不完整'):
                    default_activation.ensure_workspace(service, result.plan.task_id, roots=roots)
            from src.utils.utils import read_task_at
            observed = {**read_task_at(service.task_root, result.plan.task_id), 'path': str(root/'changed')}
            with self.assertRaisesRegex(ValueError, '首次链接只能'):
                default_activation.ensure_workspace(service, result.plan.task_id, roots=roots, observed_task=observed)
        self.assertEqual(before, workspace_files(root)[0])
        await assert_released_locks(self, root, service.task_root, result.plan.task_id)

    async def test_modern_workspace_requires_scope_binding_and_admitted_basis(self):
        from src.rename import source_basis, content_policy
        from src.utils import utils
        from tests.test_default_activation_contract import DefaultActivationProducerContracts
        helper = DefaultActivationProducerContracts('runTest')
        async with helper.flow(movie=True, admission_only=True) as flow:
            task = flow.manager._read(flow.task_id)
            before, _ = workspace_files(flow.root)
            roots = flow.manager.root_snapshot_source.observe()
            missing = {**task, 'task_source_scope': None}
            conditions = (
                patch.object(utils, 'read_task_at', return_value=missing),
                patch.object(content_policy, '_scope_binding', side_effect=ValueError('无效来源绑定')),
                patch.object(source_basis, 'observe_admitted_basis', return_value=None),
            )
            for index, condition in enumerate(conditions):
                with self.subTest(boundary=index), condition, patch.object(default_activation,
                        'inventory_movie_assets', side_effect=AssertionError('现代来源不得降级扫描')):
                    with self.assertRaises(ValueError):
                        default_activation.ensure_workspace(flow.service, flow.task_id, roots=roots)
                    self.assertEqual(before, workspace_files(flow.root)[0])

    def test_retired_single_admission_writers_are_absent(self):
        from src.pages import add_task_page
        self.assertFalse(hasattr(TaskQueueManager, 'enqueue'))
        self.assertFalse(hasattr(TaskQueueManager, 'enqueue_legacy_replan'))
        self.assertFalse(hasattr(add_task_page, 'pick_file'))

    async def test_modern_inventory_rejects_missing_basis_scope_and_disconnected_proof(self):
        from dataclasses import replace
        from src.rename import source_basis
        from tests.test_content_choice_contract import records
        helper = initial_movie.InitialMovieCompletionContracts('runTest')
        self.addCleanup(helper.doCleanups)
        root = helper.root()
        original = process.Rename._inventory_for_input
        tested = []
        def checked(renamer, task_id, path):
            from src.rename.default_activation import service_for_worker
            service = service_for_worker(renamer)
            state = service.decision_state_store.load(task_id)
            before = records(root)
            with patch.object(process, 'inventory_movie_assets', side_effect=AssertionError('modern fallback scanned')):
                with patch.object(source_basis, 'observe_initial_input', return_value=None):
                    with self.assertRaises(ValueError):
                        original(renamer, task_id, path)
                old = renamer.active_task_source_scope
                try:
                    renamer.active_task_source_scope = None
                    with self.assertRaises(ValueError):
                        original(renamer, task_id, path)
                finally:
                    renamer.active_task_source_scope = old
                from src.rename.decision_state import DecisionStateStore
                load = DecisionStateStore.load_optional
                with patch.object(DecisionStateStore, 'load_optional', lambda store, key:
                        replace(state, initial_source_basis=None) if key == task_id else load(store, key)):
                    with self.assertRaises((ValueError, RuntimeError)):
                        original(renamer, task_id, path)
                from src.rename import content_policy
                with patch.object(DecisionStateStore, 'load_optional', return_value=None), \
                        patch.object(content_policy, '_scope_binding', return_value=content_policy.PolicyBinding(task_id, None)):
                    with self.assertRaises(ValueError):
                        original(renamer, task_id, path)
                self.assertEqual(before, records(root))
                tested.append(task_id)
                return original(renamer, task_id, path)
        with patch.object(process.Rename, '_inventory_for_input', checked):
            _, result, _, _ = await helper.fixture(root, initialize_before_selection=True)
        self.assertEqual([result.plan.task_id], tested)
        self.assertTrue(result.state.confirmed_work)

    async def test_exact_file_selection_keeps_scope_without_sibling_scan(self):
        from tests.test_default_movie_completion import source_fixture, movie_fixture
        helper = initial_movie.InitialMovieCompletionContracts('runTest')
        self.addCleanup(helper.doCleanups)
        root = helper.root()
        _, primary, _ = source_fixture._make_version(root/'sources', '1080p')
        harness = movie_fixture.MediaFlow('runTest')
        with patch.object(process, 'inventory_movie_assets', side_effect=AssertionError('exact file rescanned')):
            result = await harness._run_provider_flow(root, primary, 'exact-selection')
        self.assertTrue(result.state.confirmed_work)
        self.assertEqual([str(primary)], [item['source_path'] for item in result.state.initial_source_basis.to_dict()['sources']])

    async def test_api_whole_input_and_gui_groups_share_one_collection(self):
        from src.rename import input_directory_discovery as discovery
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'source'; source.mkdir()
            for season in (1, 2):
                folder = source/f'Season {season:02}'; folder.mkdir()
                (folder/f'Show S{season:02}E01.mkv').write_bytes(b'synthetic admission')
            manager = ControlledQueue(task_root=root/'tasks', plan_root=root/'plans',
                decision_state_root=root/'decisions', recovery_abandon_audit_root=root/'abandon', test_mode=True)
            self.addAsyncCleanup(manager.stop)
            with patch.object(discovery, 'discover_sources', wraps=discovery.discover_sources) as scan:
                batch = manager.prepare_input_batch((source,), is_anime=True, is_movie=False,
                    source='webhook', preview_only=True)
                self.assertEqual(1, len(batch['jobs']))
                self.assertEqual(2, len(batch['task_requests'][0].source_identities))
                await manager.enqueue_batch(**batch)
                self.assertEqual(1, scan.call_count)
            observed = discovery.discover_sources((source,))
            gui = manager.prepare_discovered_batch(observed, is_anime=True, is_movie=False, source='ui')
            self.assertEqual(2, len(gui['jobs']))
            with self.assertRaises((ValueError, RuntimeError)):
                await manager.enqueue_batch(**gui)
