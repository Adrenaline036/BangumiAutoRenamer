"""Actual admitted TV input is distinct from the layout directory; no live I/O."""
import asyncio
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.task_queue import TaskQueueManager
from tests import test_default_activation_contract as activation_fixture
from tests import test_r1_real_media_production_red_contract as transport
from src.rename import source_basis


def single(root, source):
    return next(p for p in source.glob('*.mkv') if 'E01' in p.name or '1080p' in p.name)


def conflicting(root, source):
    directory = source.rename(source.with_name('Season 02'))
    return single(root, directory)


def active_records(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for name in ('tasks', 'plans', 'decisions', 'task_batch_scope')
            for p in (root/name).rglob('*.json') if 'history' not in p.relative_to(root).parts}


class TVInputPathContracts(unittest.IsolatedAsyncioTestCase):
    flow = activation_fixture.DefaultActivationProducerContracts.flow
    current = activation_fixture.DefaultActivationProducerContracts.current
    selected = activation_fixture.DefaultActivationProducerContracts.selected

    def proof(self, flow):
        producer = getattr(source_basis, 'observe_initial_input', None)
        self.assertTrue(callable(producer), 'missing shared actual admission input observation')
        plan, state = self.current(flow)
        return producer(flow.service, plan, state)

    async def test_manual_and_auto_single_file_complete_without_using_parent_season(self):
        for automatic in (False, True):
            for source_factory in (single, conflicting):
                with self.subTest(automatic=automatic, source=source_factory.__name__):
                    async with self.flow(automatic=automatic, configure_source=source_factory) as flow:
                        before = flow.source.read_bytes()
                        plan, state = self.current(flow) if automatic else await self.selected(flow)
                        self.assertTrue(state.confirmed_work, flow.manager._read(flow.task_id).get('error'))
                        self.assertEqual(1, state.season_binding['season_number'])
                        self.assertEqual('inferred_path', state.season_binding['decision_source'])
                        self.assertEqual(flow.source.parent, plan.source_root)
                        proof = self.proof(flow)
                        self.assertEqual(flow.source, proof.observation_path)
                        self.assertEqual(plan.source_root, proof.layout_root)
                        self.assertEqual(before, flow.source.read_bytes())
                        self.assertEqual(set(),
                                         {i.code for i in PlanVerifier().verify(plan).issues})
                        self.assertFalse(any(e.target_path.exists() for e in plan.entries))

    async def test_directory_and_explicit_season_and_single_movie_controls(self):
        for mode in ('directory', 'explicit', 'movie'):
            with self.subTest(mode=mode):
                async with self.flow(movie=mode == 'movie', configure_source=None if mode == 'directory' else single,
                                     requested_season=1 if mode == 'explicit' else None) as flow:
                    plan, state = self.current(flow)
                    self.assertTrue(state.confirmed_work, flow.manager._read(flow.task_id).get('error'))
                    self.assertEqual(flow.source if mode == 'directory' else flow.source.parent, plan.source_root)
                    if mode != 'movie':
                        self.assertEqual('explicit_user' if mode == 'explicit' else 'provider_unique',
                                         state.season_binding['decision_source'])

    async def test_initial_input_rejects_missing_request_and_task_path_replacement(self):
        async with self.flow(automatic=False, configure_source=single) as flow:
            proof = self.proof(flow)
            before = active_records(flow.root)
            task_path = flow.root/'tasks'/f'{flow.task_id}.json'
            original = task_path.read_bytes()
            try:
                task = json.loads(original)
                task['path'] = str(flow.source.with_name('Other S02E01.mkv'))
                task_path.write_text(json.dumps(task), encoding='utf-8')
                changed = active_records(flow.root)
                with self.assertRaises((ValueError, RuntimeError)):
                    self.proof(flow)
                self.assertEqual(changed, active_records(flow.root))
            finally:
                task_path.write_bytes(original)
            from src.rename.task_batch_scope import TaskBatchScopeManifestStore
            with patch.object(TaskBatchScopeManifestStore, 'load', side_effect=ValueError('request missing')):
                with self.assertRaisesRegex(ValueError, 'request missing'):
                    self.proof(flow)
            self.assertEqual(before, active_records(flow.root))
            self.assertEqual(proof, self.proof(flow))
            task = flow.manager._read(flow.task_id)
            path = flow.manager.batch_scope_store.path_for(task['batch_id'])
            original = path.read_bytes()
            try:
                manifest = json.loads(original)
                manifest['requests'][0]['input_scope'] = [str(flow.source.parent/'Foreign.mkv')]
                path.write_text(json.dumps(manifest), encoding='utf-8')
                later = active_records(flow.root)
                with self.assertRaises((ValueError, RuntimeError)):
                    self.proof(flow)
                self.assertEqual(later, active_records(flow.root))
            finally:
                path.write_bytes(original)

    async def test_canonical_rechecks_frozen_input_and_preserves_later_task(self):
        reached = []
        def hook(transaction, envelope, terminal, forward):
            if envelope.draft_plan.revision_action != 'initial_tv_completion':
                return forward()
            intent = envelope.initial_tv_completion
            proof = getattr(intent.inputs, 'source_input', None)
            self.assertIsNotNone(proof, 'TV input proof missing from canonical transport')
            root = transaction.plan_store.root.parent
            before = active_records(root)
            for altered in (None, replace(proof, observation_path=proof.layout_root),
                            replace(proof, layout_root=proof.layout_root.parent)):
                bad = replace(intent, inputs=replace(intent.inputs, source_input=altered))
                with self.assertRaises((ValueError, RuntimeError, TypeError)):
                    forward(replace(envelope, initial_tv_completion=bad))
                self.assertEqual(before, active_records(root))
            with patch.object(source_basis, 'observe_initial_input', return_value=None):
                with self.assertRaises((ValueError, RuntimeError, TypeError)):
                    forward()
            self.assertEqual(before, active_records(root))
            path = root/'tasks'/f'{intent.inputs.task_id}.json'
            original = path.read_bytes()
            try:
                task = json.loads(original)
                task['path'] = str(proof.layout_root/'Foreign S02E01.mkv')
                path.write_text(json.dumps(task), encoding='utf-8')
                later = active_records(root)
                with self.assertRaises((ValueError, RuntimeError)):
                    forward()
                self.assertEqual(later, active_records(root))
            finally:
                path.write_bytes(original)
            task = json.loads(original)
            from src.rename.task_batch_scope import TaskBatchScopeManifestStore
            manifest_path = TaskBatchScopeManifestStore(root/'task_batch_scope').path_for(task['batch_id'])
            original_manifest = manifest_path.read_bytes()
            try:
                manifest = json.loads(original_manifest)
                manifest['requests'][0]['input_scope'] = [str(proof.layout_root/'Foreign.mkv')]
                manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
                later = active_records(root)
                with self.assertRaises((ValueError, RuntimeError)):
                    forward()
                self.assertEqual(later, active_records(root))
            finally:
                manifest_path.write_bytes(original_manifest)
            reached.append(True)
            return forward()
        async with self.flow(configure_source=single, hook=hook) as flow:
            self.assertEqual([True], reached, flow.manager._read(flow.task_id).get('error'))
            self.assertTrue(self.current(flow)[1].confirmed_work)

    async def test_valid_active_request_with_same_files_cannot_replace_historical_input(self):
        from src.rename.task_batch_scope import TaskSourceScope
        async with self.flow(automatic=False) as flow:
            original = self.proof(flow)
            task = flow.manager._read(flow.task_id)
            scope = TaskSourceScope.from_mapping(task['task_source_scope'])
            store = flow.manager.batch_scope_store
            old = store.load(task['batch_id'])
            request = next(r for r in old.requests if r.task_id == flow.task_id)
            store.retire_task(task['batch_id'], task_id=flow.task_id,
                              expected_lineage_id=scope.reservation_lineage_id)
            # Real reservation history, not a manifest with an invalid hash:
            # same physical files, different directory-vs-exact request meaning.
            request = replace(request, scope_kind='exact_file',
                              input_scope=tuple(i.source_path for i in scope.source_identities))
            fresh = store.reserve_batch(batch_id='different-input-contract', task_requests=(request,))
            new_scope = fresh.task_source_scopes[0]
            task.update(batch_id=fresh.batch_id, task_source_scope=new_scope.to_dict())
            flow.manager._write(flow.task_id, task)
            fresh = store.mark_tasks_enqueued(fresh.batch_id, expected_revision=fresh.revision, task_ids=(flow.task_id,))
            store.activate_batch(fresh.batch_id, expected_revision=fresh.revision)
            store.require_active_scope(new_scope, batch_id=fresh.batch_id)
            self.assertEqual(scope.source_identity_set_fingerprint, new_scope.source_identity_set_fingerprint)
            plan, state = self.current(flow)
            from src.rename.default_content import _origin
            self.assertEqual(original.basis_fingerprint, _origin(flow.service, plan, state).fingerprint)
            later = active_records(flow.root)
            with self.assertRaisesRegex(ValueError, '历史请求|输入路径'):
                self.proof(flow)
            self.assertEqual(later, active_records(flow.root))

    async def test_single_file_auto_fresh_resume_uses_original_observation_without_provider(self):
        reached = []
        def hook(transaction, envelope, terminal, forward):
            if envelope.draft_plan.revision_action == 'initial_tv_completion' and not reached:
                reached.append(True)
                raise ValueError('stop before disposable completion')
            return forward()
        async with self.flow(configure_source=single, hook=hook) as flow:
            self.assertEqual([True], reached, flow.manager._read(flow.task_id).get('error'))
            plan, state = self.current(flow)
            self.assertFalse(state.confirmed_work)
            await flow.manager.stop()
            fresh = TaskQueueManager(task_root=flow.root/'tasks', plan_root=flow.root/'plans',
                decision_state_root=flow.root/'decisions', provider_link_catalog_root=flow.root/'provider_links',
                recovery_abandon_audit_root=flow.root/'abandon')
            worker = transport._configured_renamer(flow.root)
            with ExitStack() as patches:
                patches.enter_context(patch('src.task_queue.Rename', return_value=worker))
                for method in ('_provider_search_batch', 'process'):
                    patches.enter_context(patch.object(worker, method, side_effect=AssertionError('Provider re-query')))
                for method in ('get_tv_info', 'get_movie_info', 'get_tv_info_by_id', 'get_movie_info_by_id', 'get_season_info'):
                    if hasattr(worker.search, method):
                        patches.enter_context(patch.object(worker.search, method, side_effect=AssertionError('Provider re-query')))
                await fresh.start()
                try:
                    await fresh.resume_default_activation(flow.task_id, PlanStore.fingerprint(plan))
                    await asyncio.wait_for(fresh._queue.join(), 12)
                finally:
                    await fresh.stop()
            self.assertTrue(self.current(flow)[1].confirmed_work, fresh._read(flow.task_id).get('error'))

    async def test_regrouped_single_with_companion_and_multiple_video_input_order(self):
        from src.rename.task_batch_scope import TaskSourceScope
        from src.rename.recognition_queue_admission import capture_workspace_open
        def companions(root, source):
            single(root, source).with_suffix('.mka').write_bytes(b'disposable paired audio')
            (source/'Ordinary Show S01E03 [1080p].mkv').write_bytes(single(root, source).read_bytes())
            return source
        def worker(value):
            self.worker = value
        for count, reverse in ((1, False), (2, False), (2, True)):
            with self.subTest(count=count, reverse=reverse):
                async with self.flow(automatic=False, configure_source=companions, configure_worker=worker) as flow:
                    plan, state = self.current(flow)
                    selected = sorted((e for e in plan.entries if e.media_kind == 'video' and 'S01E' in e.source_path.name),
                                      key=lambda e: e.source_path.name)[:count]
                    expected_path = selected[0].source_path if count == 1 else flow.source
                    expected_sources = {str(e.source_path) for e in selected}
                    expected_sources.add(str(single(flow.root, flow.source).with_suffix('.mka')))
                    all_sources = {r['source_path'] for r in state.content_selection.to_dict()['identities']}
                    group = sorted(expected_sources, reverse=reverse)
                    preview = flow.manager.preview_source_regrouping(task_id=flow.task_id,
                        expected_plan_fingerprint=PlanStore.fingerprint(plan),
                        groups=[group, sorted(all_sources - expected_sources)])
                    before = {Path(p): Path(p).read_bytes() for p in all_sources}
                    with patch.object(flow.manager, 'partition_task_sources', side_effect=AssertionError('retired writer')):
                        ids = flow.manager.create_regrouped_tasks(preview)
                    child = next(t for t in ids if str(selected[0].source_path) in {
                        i.source_path for i in TaskSourceScope.from_mapping(flow.manager._read(t)['task_source_scope']).source_identities})
                    self.worker.search.get_tv_info = lambda *_: self.worker.search.get_tv_info_by_id('4242')
                    opened = capture_workspace_open(flow.manager, child, flow.manager._read(child))
                    await flow.manager.confirm_source_group(child, opened_evidence=opened)
                    await asyncio.wait_for(flow.manager._queue.join(), 12)
                    child_plan = flow.store.load(child)
                    child_state = flow.service.decision_state_store.load(child)
                    proof = source_basis.observe_initial_input(flow.service, child_plan, child_state)
                    self.assertEqual(expected_path, proof.observation_path)
                    self.assertEqual(flow.source, proof.layout_root)
                    scope = TaskSourceScope.from_mapping(flow.manager._read(child)['task_source_scope'])
                    self.assertEqual(expected_sources, {i.source_path for i in scope.source_identities})
                    self.assertFalse(flow.manager._read(flow.task_id))
                    self.assertEqual(before, {p: p.read_bytes() for p in before})
