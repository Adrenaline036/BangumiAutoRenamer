"""接纳后的生产入口只消费冻结清单；全部来源为隔离合成文件。"""
from contextlib import contextmanager, ExitStack
from dataclasses import replace
import builtins
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from src.rename import content_selection as content
from src.rename.default_activation import ensure_workspace
from src.rename.input_directory_discovery import discover_sources, validate_discovery, validate_pending_group
from src.rename.plan_review import PlanReviewService
from src.rename.plan_store import PlanStore
from src.rename.task_batch_scope import TaskSourceScope
from tests.test_content_policy_contract import ContentPolicyContracts


@contextmanager
def source_access(root):
    """拦截源目录访问，允许隔离的任务记录与目标路径检查。"""
    root = Path(root)
    calls = []
    def checked(name, function):
        def invoke(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                candidate = Path(os.fsdecode(path))
                if candidate == root or root in candidate.parents:
                    calls.append((name, str(candidate)))
                    raise AssertionError('采集后访问源文件：' + name + ' ' + str(candidate))
            return function(path, *args, **kwargs)
        return invoke
    with ExitStack() as stack:
        for owner, names in ((Path, ('stat','lstat','resolve','open')), (os, ('stat','lstat','scandir','open')),
                             (builtins, ('open',)), (io, ('open',))):
            for name in names:
                stack.enter_context(patch.object(owner, name, checked(name, getattr(owner, name))))
        yield calls


class InventoryReuseContracts(unittest.IsolatedAsyncioTestCase):
    root = ContentPolicyContracts.root
    sources = ContentPolicyContracts.sources
    manager = ContentPolicyContracts.manager
    admission = ContentPolicyContracts.admission

    async def setup_admitted(self):
        root = self.root()
        manager, task = await self.admission(root, mode='single')
        service = PlanReviewService(PlanStore(root/'plans'), root/'review', root/'work_catalog',
            decision_state_root=root/'decision_state', task_root=root/'task', record_root=root/'records')
        return root, manager, task, service

    def workspace(self, manager, task, service):
        return ensure_workspace(service, task['uuid'], roots=manager.root_snapshot_source.observe())

    async def test_control_initial_workspace_uses_real_admission_and_publication(self):
        root, manager, task, service = await self.setup_admitted()
        state, plan = self.workspace(manager, task, service)
        self.assertEqual(task['task_source_scope'], state.initial_source_basis.to_dict()['task_source_scope'])
        self.assertIsNotNone(plan.projection_receipt)
        self.assertEqual(state.content_selection, content.from_plan(plan))
        self.assertTrue(service.plan_store.path_for(task['uuid']).exists())

    async def test_first_workspace_does_not_reopen_admitted_sources(self):
        root, manager, task, service = await self.setup_admitted()
        with source_access(root/'source') as calls:
            state, plan = self.workspace(manager, task, service)
        self.assertEqual([], calls)
        self.assertEqual({r['source_path'] for r in task['task_source_scope']['source_identities']},
                         {str(p) for p in plan.expected_sources})
        self.assertIsNotNone(state.content_selection)

    async def test_content_identity_check_uses_frozen_plan(self):
        root, manager, task, service = await self.setup_admitted()
        state, plan = self.workspace(manager, task, service)
        with source_access(root/'source') as calls:
            content.validate_source_identities(state.content_selection, font_plan=plan)
        self.assertEqual([], calls)

    async def test_changed_selection_identity_is_rejected_without_source_access(self):
        root, manager, task, service = await self.setup_admitted()
        state, plan = self.workspace(manager, task, service)
        raw = state.content_selection.to_dict()
        raw['identities'][0]['size'] += 1
        forged = content.ContentSelection.from_mapping(raw)
        with source_access(root/'source'):
            with self.assertRaisesRegex(ValueError, '身份|来源|清单'):
                content.validate_source_identities(forged, font_plan=plan)
        self.assertEqual(state, service.decision_state_store.load(task['uuid']))

    async def test_discovery_admission_validation_does_not_scan_twice(self):
        root = self.root()
        source = self.sources(root)
        discovery = discover_sources((source,))
        with source_access(source) as calls:
            validate_discovery(discovery)
        self.assertEqual([], calls)

    async def test_pending_group_confirmation_uses_admitted_identities(self):
        root, manager, task, service = await self.setup_admitted()
        discovery = discover_sources((root/'source',))
        group = replace(discovery.groups[0], reason='人工确认来源范围')
        task.update(source_group_pending=True, source_group_observation=group.to_dict())
        scope = TaskSourceScope.from_mapping(task['task_source_scope'])
        with source_access(root/'source') as calls:
            validate_pending_group(task, scope)
        self.assertEqual([], calls)
        bad = dict(task, source_group_observation={**group.to_dict(), 'identities':[]})
        with self.assertRaisesRegex(ValueError, '范围'):
            validate_pending_group(bad, scope)

    async def test_content_change_preview_preserves_original_choices_without_source_access(self):
        from src.rename.plan_review import entry_id
        root, manager, task, service = await self.setup_admitted()
        state, plan = self.workspace(manager, task, service)
        video = next(e for e in plan.entries if e.media_kind == 'video')
        with source_access(root/'source') as calls:
            preview = service.prepare_content_change(task['uuid'], PlanStore.fingerprint(plan),
                selected_entry_ids=[entry_id(video)], action='exclude', reason='人工排除')
        self.assertEqual([], calls)
        self.assertIn(str(video.source_path), preview.to_dict()['selection']['excluded_sources'])
        self.assertEqual(state, service.decision_state_store.load(task['uuid']))

    async def test_legacy_content_migration_preview_uses_persisted_scope(self):
        from tests.test_err060_role_entry import movie_dialog
        root = self.root()
        dialog, _ = movie_dialog(root)
        service, plan = dialog.review_service, dialog.plan
        with source_access(plan.source_root) as calls:
            preview = service.prepare_content_migration(plan.task_id, PlanStore.fingerprint(plan), reason='保留原选择')
        self.assertEqual([], calls)
        self.assertEqual(set(map(str, plan.ignored_sources)), set(preview.to_dict()['selection']['excluded_sources']))

    async def test_grouped_queue_and_confirmation_do_not_reobserve_snapshot(self):
        import asyncio
        from src.rename.recognition_queue_admission import capture_workspace_open
        root = self.root()
        source = root/'source'
        for name in ('Season 1/Show 01.mkv', 'SPs/Show SP.mkv', 'Shared.ass'):
            p = source/name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'isolated source')
        manager = self.manager(root)
        scans = []
        original = os.scandir
        def count(path):
            scans.append(str(path))
            return original(path)
        with patch('os.scandir', side_effect=count):
            discovery = discover_sources((source,))
        self.assertEqual(3, len(scans))
        self.assertEqual(3, len(set(scans)))
        # 采集后变化不属于原清单。只操作本测试创建的文件。
        (source/'New.mkv').write_bytes(b'new unobserved source')
        (source/'SPs/Show SP.mkv').rename(root/'held-special.mkv')
        with source_access(source) as calls:
            batch = manager.prepare_discovered_batch(discovery, is_anime=True, is_movie=False, source='ui')
            ids = await manager.enqueue_batch(**batch)
            await asyncio.wait_for(manager._queue.join(), 5)
            pending = [i for i in ids if manager._read(i).get('source_group_pending')]
            self.assertEqual(2, len(pending))
            for task_id in pending:
                task = manager._read(task_id)
                self.assertEqual('need_confirm', task['workflow_status'], task.get('error'))
                opened = capture_workspace_open(manager, task_id, task)
                await manager.confirm_source_group(task_id, opened_evidence=opened)
                await asyncio.wait_for(manager._queue.join(), 5)
        self.assertEqual([], calls)
        saved = {row['source_path'] for task_id in ids for row in manager._read(task_id)['task_source_scope']['source_identities']}
        original_files = {row.source_path for g in discovery.groups for row in g.identities}
        self.assertEqual(original_files, saved)
        fresh = discover_sources((source,))
        self.assertNotEqual(original_files, {row.source_path for g in fresh.groups for row in g.identities})

    async def test_regrouped_children_preserve_complete_original_manifest_without_source_access(self):
        root, manager, task, service = await self.setup_admitted()
        state, plan = self.workspace(manager, task, service)
        sources = [r['source_path'] for r in state.initial_source_basis.to_dict()['sources']]
        videos = [p for p in sources if p.endswith('.mkv')]
        groups = [[p for p in sources if p != videos[-1]], [videos[-1]]]
        with source_access(root/'source') as calls:
            preview = manager.preview_source_regrouping(task_id=plan.task_id,
                expected_plan_fingerprint=PlanStore.fingerprint(plan), groups=groups)
            ids = manager.create_regrouped_tasks(preview)
        self.assertEqual([], calls)
        saved = [row['source_path'] for task_id in ids for row in manager._read(task_id)['task_source_scope']['source_identities']]
        self.assertEqual(sorted(sources), sorted(saved))
        self.assertEqual(len(saved),len(set(saved)))

    async def test_content_save_and_reopen_use_original_inventory_without_source_access(self):
        from src.rename.plan_review import entry_id
        from src.rename.plan_verifier import PlanVerifier
        root, manager, task, service = await self.setup_admitted()
        state, plan = self.workspace(manager, task, service)
        video = next(e for e in plan.entries if e.media_kind == 'video')
        with source_access(root/'source') as calls:
            preview = service.prepare_content_change(task['uuid'], PlanStore.fingerprint(plan),
                selected_entry_ids=[entry_id(video)], action='exclude', reason='人工排除')
            revised = service.commit_content_change(preview)
            reopened = PlanReviewService(PlanStore(root/'plans'), root/'review', root/'work_catalog',
                decision_state_root=root/'decision_state', task_root=root/'task', record_root=root/'records')
            saved = reopened.decision_state_store.load(task['uuid'])
            self.assertEqual(revised, reopened.plan_store.load(task['uuid']))
        self.assertEqual([], calls)
        self.assertEqual(state.initial_source_basis, saved.initial_source_basis)
        self.assertIn(str(video.source_path), saved.content_selection.to_dict()['excluded_sources'])
        self.assertTrue(PlanVerifier().verify(revised).issues)
        self.assertEqual(task['task_source_scope'], manager._read(task['uuid'])['task_source_scope'])


    async def test_forged_discovery_cannot_publish_trimmed_or_changed_source_groups(self):
        root = self.root(); source = self.sources(root)
        observed = discover_sources((source,))
        group = observed.groups[0]
        forged = replace(group.identities[0], size=group.identities[0].size+1)
        variants = (replace(observed, groups=observed.groups[:-1]),
                    replace(observed, groups=(replace(group, identities=(forged,*group.identities[1:])),*observed.groups[1:])),
                    replace(observed, groups=(replace(group,path=root),*observed.groups[1:])))
        manager = self.manager(root)
        for value in variants:
            with self.subTest(value=value), source_access(source):
                with self.assertRaises(ValueError):
                    batch = manager.prepare_discovered_batch(value, is_anime=True,is_movie=False,source='ui')
                    await manager.enqueue_batch(**batch)
            self.assertFalse(list(manager.task_root.glob('*.json')))

    async def test_disconnected_initial_projection_cannot_publish_a_plan(self):
        from src.rename import source_basis
        root, manager, task, service = await self.setup_admitted()
        with source_access(root/'source'), patch.object(source_basis,'project_source_basis',return_value=None):
            with self.assertRaises((ValueError,RuntimeError,TypeError)):
                self.workspace(manager, task, service)
        self.assertFalse(service.plan_store.path_for(task['uuid']).exists())
        self.assertFalse(service.decision_state_store.path_for(task['uuid']).exists())

    async def test_scope_change_after_preview_rejects_save_without_source_access(self):
        from src.rename.plan_review import entry_id
        root, manager, task, service = await self.setup_admitted()
        state, plan = self.workspace(manager, task, service)
        video = next(e for e in plan.entries if e.media_kind=='video')
        preview = service.prepare_content_change(task['uuid'], PlanStore.fingerprint(plan),
            selected_entry_ids=[entry_id(video)],action='exclude',reason='人工排除')
        raw = manager._read(task['uuid'])['task_source_scope']
        raw['source_identities'][0]['size'] += 1
        manager._update(task['uuid'],task_source_scope=raw)
        with source_access(root/'source'), self.assertRaises((ValueError,RuntimeError)):
            service.commit_content_change(preview)
        self.assertEqual(state, service.decision_state_store.load(task['uuid']))
        self.assertEqual(plan, service.plan_store.load(task['uuid']))


    async def test_execution_identity_check_still_rejects_same_size_replacement(self):
        from src.rename.plan_execution import PlanExecutionService,PlanExecutionError
        root,manager,task,service=await self.setup_admitted()
        _,plan=self.workspace(manager,task,service)
        PlanExecutionService._validate_frozen_source_identities(plan)
        source=next(e.source_path for e in plan.entries if e.media_kind=='video')
        before=source.stat(); data=source.read_bytes()
        source.rename(root/'held-original.mkv')
        source.write_bytes(data)
        os.utime(source,ns=(before.st_atime_ns,before.st_mtime_ns))
        self.assertNotEqual(before.st_ino,source.stat().st_ino)
        with self.assertRaises(PlanExecutionError):
            PlanExecutionService._validate_frozen_source_identities(plan)
