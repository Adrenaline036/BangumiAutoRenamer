"""Explicit batches use real admission/worker and owned disposable MKVs."""
from tests.source_collection_fixture import collected_identity
import os
from contextlib import ExitStack
from dataclasses import replace
import json
import tempfile
from pathlib import Path
import unittest
import traceback
from unittest.mock import Mock, patch

from src.rename import content_selection
from src.rename.plan_store import PlanStore
from src.rename.source_basis import observe_initial_input
from src.rename.task_batch_scope import BatchTaskSourceRequest
from src.rename.movie_assets import inventory_movie_assets, compare_movie_asset_inventory, MovieAssetInventoryError
from src.task_queue import TaskJob
from tests import test_default_activation_contract as activation
from tests.test_partition_content_contract import records


class ScopedInventoryWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow

    @staticmethod
    def sources(root, source, *, single=False):
        videos = sorted(source.glob('*S01E*.mkv'))
        (source / 'Subtitles').mkdir()
        (source / 'Subtitles' / (videos[0].stem + '.en.srt')).write_text(
            '1\n00:00:00,000 --> 00:00:00,500\nTest\n', encoding='utf-8')
        (source / 'README-other-season.txt').write_text('not admitted', encoding='utf-8')
        (source / '[Fonts] unselected.zip').write_bytes(b'not a package; never inspect')
        return videos[0] if single else source

    @staticmethod
    async def admit(manager, source, task_id):
        root = source.parent if source.is_file() else source
        videos = (source,) if source.is_file() else tuple(sorted(root.glob('*S01E*.mkv')))
        paths = (*videos, *sorted((root / 'Subtitles').glob('*.srt')))
        request = BatchTaskSourceRequest(task_id=task_id, input_scope=tuple(map(str, paths)),
            scope_kind='exact_file', media_domain='tv',
            source_identities=tuple(collected_identity(p) for p in paths))
        await manager.enqueue_batch(batch_id='scoped-inventory', task_requests=(request,),
            jobs=(TaskJob(task_id=task_id, path=source, is_anime=False, is_movie=False,
                          custom_name='Ordinary Show', use_ai=False),))

    def current(self, flow):
        self.assertTrue(flow.store.path_for(flow.task_id).exists(),
            'actual workspace missing: ' + str(flow.manager._read(flow.task_id).get('error')))
        return flow.store.load(flow.task_id), flow.service.decision_state_store.load(flow.task_id)

    def assert_scope(self, flow):
        plan, state = self.current(flow)
        task = flow.manager._read(flow.task_id)
        expected = {Path(i['source_path']) for i in task['task_source_scope']['source_identities']}
        self.assertTrue(flow.commits, 'canonical publication was not observed')
        self.assertEqual(expected, {e.source_path for e in flow.commits[0][1].entries},
            'first canonical draft must cover exactly the admitted materials')
        self.assertIsNotNone(state.content_selection)
        materials = content_selection.material_entries(plan, state.content_selection)
        self.assertEqual(expected, {e.source_path for e in materials})
        self.assertEqual(expected, {Path(i['source_path']) for i in state.initial_source_basis.to_dict()['sources']})
        self.assertFalse(state.confirmed_work)
        self.assertIsNone(plan.series_scope)
        proof = observe_initial_input(flow.service, plan, state)
        self.assertEqual(flow.source, proof.observation_path)
        self.assertEqual(plan.source_root, proof.layout_root)
        content_selection.validate_plan_content(plan, state)
        return plan, state

    async def test_real_batch_multiple_files_never_includes_other_parent_materials(self):
        async with self.flow(automatic=False, configure_source=self.sources, admit=self.admit) as flow:
            self.assert_scope(flow)

    async def test_real_batch_single_video_preserves_explicit_nested_subtitle(self):
        async with self.flow(automatic=False,
                configure_source=lambda r, s: self.sources(r, s, single=True), admit=self.admit) as flow:
            plan, _ = self.assert_scope(flow)
            self.assertTrue(any(e.media_kind == 'subtitle' for e in plan.entries))

    async def test_real_link_workspace_subset_does_not_use_recursive_scan(self):
        from src.pages.initial_workspace import create_initial_workspace
        async with self.flow(automatic=False, admission_only=True,
                configure_source=self.sources, admit=self.admit) as flow:
            self.assertFalse(flow.store.path_for(flow.task_id).exists())
            # Admission already finished; no recursive directory operation is
            # allowed in the new source-workspace producer/comparer.
            actual_scandir = os.scandir
            def no_source_walk(path):
                if Path(path).is_relative_to(flow.source):
                    raise AssertionError('recursive source scan forbidden')
                return actual_scandir(path)
            with patch('src.rename.movie_assets.os.scandir', side_effect=no_source_walk):
                await create_initial_workspace(flow.task_id, flow.manager._read(flow.task_id))
            self.assert_scope(flow)

    async def test_unadmitted_sibling_change_before_link_is_not_source_drift(self):
        from src.pages.initial_workspace import create_initial_workspace
        async with self.flow(automatic=False, admission_only=True,
                configure_source=self.sources, admit=self.admit) as flow:
            (flow.source / 'Unadmitted.mkv').write_bytes(b'not in active reservation')
            await create_initial_workspace(flow.task_id, flow.manager._read(flow.task_id))
            self.assert_scope(flow)

    async def test_selected_source_change_does_not_replace_the_admitted_snapshot(self):
        from src.pages.initial_workspace import create_initial_workspace
        async with self.flow(automatic=False, admission_only=True,
                configure_source=self.sources, admit=self.admit) as flow:
            selected = sorted(flow.source.glob('*S01E*.mkv'))[0]
            selected.write_bytes(b'changed after actual admission')
            await create_initial_workspace(flow.task_id, flow.manager._read(flow.task_id))
            self.assert_scope(flow)

    async def test_whole_no_match_worker_never_walks_or_stats_unadmitted_sources(self):
        with ExitStack() as guards:
            async def admit(manager, source, task_id):
                await self.admit(manager, source, task_id)
                actual_scandir, actual_stat, actual_lstat = os.scandir, Path.stat, Path.lstat
                forbidden = {source / p for p in ('README.txt', 'README-other-season.txt',
                    '[Fonts] unselected.zip', 'Ordinary Show [Trailer].mkv')}
                def scandir(path):
                    if Path(path).is_relative_to(source):
                        raise AssertionError('whole worker scanned source directory')
                    return actual_scandir(path)
                def checked(method):
                    def stat(path, *args, **kwargs):
                        if path in forbidden:
                            calls = ' > '.join(f.name for f in traceback.extract_stack(limit=10))
                            raise AssertionError('whole worker inspected unadmitted file: ' + calls)
                        return method(path, *args, **kwargs)
                    return stat
                guards.enter_context(patch('os.scandir', side_effect=scandir))
                guards.enter_context(patch.object(Path, 'stat', checked(actual_stat)))
                guards.enter_context(patch.object(Path, 'lstat', checked(actual_lstat)))
            async with self.flow(automatic=False, configure_source=self.sources, admit=admit) as flow:
                try:
                    self.assert_scope(flow)
                    task = flow.manager._read(flow.task_id)
                    self.assertEqual('need_confirm', task['workflow_status'], task.get('error'))
                    self.assertNotIn('whole worker', str(task.get('error')))
                    expected = {Path(i['source_path']) for i in task['task_source_scope']['source_identities']}
                    actual = {flow.source / i['relative_path'] for i in task['movie_inventory']['entries']}
                    self.assertEqual(expected, actual, 'no-match worker replaced frozen task inventory')
                finally:
                    guards.close()  # Owned fixture cleanup may walk its directory.

    async def test_existing_single_file_workspace_never_calls_old_no_match_inventory_writer(self):
        old_reply = Mock(side_effect=AssertionError('old inventory writer called'))
        def worker(value):
            value._movie_inventory_reply = old_reply
        async with self.flow(automatic=False,
                configure_source=lambda r, s: sorted(s.glob('*S01E*.mkv'))[0], configure_worker=worker) as flow:
            self.assertFalse(old_reply.called, 'actual no-match worker reached retired inventory writer')
            plan, state = self.current(flow)
            self.assertIsNotNone(state.content_selection)
            self.assertEqual('need_confirm', flow.manager._read(flow.task_id)['workflow_status'])

    async def test_actual_canonical_rejects_missing_extra_wrong_root_and_duplicate_drafts(self):
        reached = []
        def check(transaction, envelope, terminal, forward):
            if envelope.previous_decision is not None:
                return forward()
            reached.append(True)
            plan = envelope.draft_plan
            extra = plan.source_root / 'README-other-season.txt'
            before = records(transaction.plan_store.root.parent)
            for label, altered in (
                ('missing', replace(plan, entries=plan.entries[:-1], expected_sources=plan.expected_sources[:-1])),
                ('moved-to-ignored', replace(plan, entries=plan.entries[:-1],
                    ignored_sources=(*plan.ignored_sources, plan.entries[-1].source_path))),
                ('extra', replace(plan, entries=(*plan.entries, replace(plan.entries[0], source_path=extra)),
                    expected_sources=(*plan.expected_sources, extra))),
                ('duplicate', replace(plan, entries=(*plan.entries, plan.entries[0]))),
                ('wrong-root', replace(plan, source_root=plan.source_root.parent)),
                ('wrong-task', replace(plan, task_id='another-task')),
            ):
                with self.subTest(label=label):
                    with self.assertRaises((ValueError, RuntimeError, TypeError)):
                        forward(replace(envelope, draft_plan=altered))
                    self.assertEqual(before, records(transaction.plan_store.root.parent))
            return forward()
        async with self.flow(automatic=False, configure_source=self.sources, admit=self.admit, hook=check) as flow:
            self.assert_scope(flow)
            self.assertEqual([True], reached)

    async def test_disconnected_or_forged_observer_preserves_all_business_records(self):
        from src.pages.initial_workspace import create_initial_workspace
        import src.rename.source_basis as source_basis
        async with self.flow(automatic=False, admission_only=True,
                configure_source=self.sources, admit=self.admit) as flow:
            actual = source_basis.observe_admitted_basis(flow.service, flow.task_id)
            raw = actual.to_dict()
            forged = source_basis.InitialSourceBasis.from_mapping({**raw, 'batch_id': 'unrelated-batch'})
            before = records(flow.root)
            for label, observation in (('missing', None), ('raw', raw), ('forged', forged)):
                with self.subTest(label=label), patch.object(source_basis, 'observe_admitted_basis', return_value=observation):
                    with self.assertRaises((ValueError, RuntimeError, TypeError)):
                        await create_initial_workspace(flow.task_id, flow.manager._read(flow.task_id))
                    self.assertEqual(before, records(flow.root))
            await create_initial_workspace(flow.task_id, flow.manager._read(flow.task_id))
            self.assert_scope(flow)

    async def test_actual_scope_retirement_in_canonical_window_is_preserved(self):
        from src.pages.initial_workspace import create_initial_workspace
        from src.rename.projection_authority import ProjectionCommitService
        async with self.flow(automatic=False, admission_only=True,
                configure_source=self.sources, admit=self.admit) as flow:
            original = ProjectionCommitService.commit  # Actual fixture observer still forwards canonical.
            later = []
            def retire(transaction, envelope, *, task_terminal=None):
                task = flow.manager._read(flow.task_id)
                flow.manager.batch_scope_store.retire_task(task['batch_id'], task_id=flow.task_id,
                    expected_lineage_id=task['reservation_lineage_id'])
                later.append(records(flow.root))
                return original(transaction, envelope, task_terminal=task_terminal)
            with patch.object(ProjectionCommitService, 'commit', retire):
                with self.assertRaises((ValueError, RuntimeError)):
                    await create_initial_workspace(flow.task_id, flow.manager._read(flow.task_id))
            self.assertEqual(later[-1], records(flow.root))
            self.assertFalse(flow.store.path_for(flow.task_id).exists())

    async def test_existing_workspace_reuses_choices_without_inventory_observation(self):
        from src.pages.initial_workspace import create_initial_workspace
        from tests.test_content_choice_contract import change
        async with self.flow(automatic=False, configure_source=self.sources, admit=self.admit) as flow:
            plan, _state = self.assert_scope(flow)
            changed = change(flow.service, plan, [plan.entries[0].source_path])
            before = records(flow.root)
            with patch('src.rename.source_basis.observe_admitted_basis',
                    side_effect=AssertionError('existing workspace reobserved admission')) as observer, \
                    patch('src.rename.default_activation.inventory_movie_assets',
                    side_effect=AssertionError('existing workspace rescanned inventory')) as inventory:
                _state, current = await create_initial_workspace(flow.task_id, flow.manager._read(flow.task_id))
            self.assertEqual(PlanStore.fingerprint(changed), PlanStore.fingerprint(current))
            self.assertFalse(observer.called or inventory.called)
            self.assertEqual(before, records(flow.root))

    async def test_changed_request_or_task_in_publication_window_preserves_later_bytes(self):
        from src.pages.initial_workspace import create_initial_workspace
        from src.rename.projection_authority import ProjectionCommitService
        for mode in ('request', 'task'):
            with self.subTest(mode=mode):
                async with self.flow(automatic=False, admission_only=True,
                        configure_source=self.sources, admit=self.admit) as flow:
                    original = ProjectionCommitService.commit
                    later = []
                    def changed(transaction, envelope, *, task_terminal=None):
                        task = flow.manager._read(flow.task_id)
                        if mode == 'request':
                            path = flow.manager.batch_scope_store.path_for(task['batch_id'])
                            manifest = json.loads(path.read_text('utf-8'))
                            manifest['requests'][0]['input_scope'] = [str(flow.source.parent / 'Foreign.mkv')]
                            path.write_text(json.dumps(manifest), encoding='utf-8')
                        else:
                            flow.manager._update(flow.task_id, name='later user change')
                        later.append(records(flow.root))
                        return original(transaction, envelope, task_terminal=task_terminal)
                    with patch.object(ProjectionCommitService, 'commit', changed):
                        with self.assertRaises((ValueError, RuntimeError)):
                            await create_initial_workspace(flow.task_id, flow.manager._read(flow.task_id))
                    self.assertEqual(later[-1], records(flow.root))
                    self.assertFalse(flow.store.path_for(flow.task_id).exists())

    async def test_no_match_feedback_cannot_overwrite_a_later_task_record(self):
        import src.utils.utils as task_utils
        from src.rename.automatic_recognition import AutomaticRecognitionOrigin
        from src.rename.default_activation import preserve_unresolved_observation
        observations, workers = [], []
        def configure(worker):
            original = worker._provider_search_batch
            def capture(*args, **kwargs):
                batch = original(*args, **kwargs)
                observations.append(batch)
                return batch
            worker._provider_search_batch = capture
            workers.append(worker)
        async with self.flow(automatic=False, configure_source=self.sources, admit=self.admit,
                configure_worker=configure) as flow:
            self.assert_scope(flow)
            task = flow.manager._read(flow.task_id)
            origin = AutomaticRecognitionOrigin.from_mapping(task['automatic_recognition_origin'])
            original = task_utils.mutate_task_at
            later = []
            def race(root, task_id, mutation):
                original(root, task_id, lambda t: {**t, 'name': 'later feedback owner'})
                later.append(records(flow.root))
                return original(root, task_id, mutation)
            with patch.object(task_utils, 'mutate_task_at', race):
                with self.assertRaisesRegex(ValueError, '后来记录'):
                    preserve_unresolved_observation(workers[0], origin, observations[0], 'no match')
            self.assertEqual(later[-1], records(flow.root))

    async def test_explicit_inventory_ownership_conflict_is_not_silently_filtered(self):
        from src.rename.inventory_plan import build_inventory_review_draft, InventoryDraftError
        from src.rename.source_basis import observe_admitted_basis
        async with self.flow(automatic=False, admission_only=True,
                configure_source=self.sources, admit=self.admit) as flow:
            task = flow.manager._read(flow.task_id)
            basis = observe_admitted_basis(flow.service, flow.task_id)
            paths = tuple(Path(i['source_path']) for i in basis.to_dict()['sources'])
            task.update(movie_inventory=inventory_movie_assets(flow.source, source_paths=paths).to_dict(),
                draft_target_root=str(flow.root / 'library'),
                source_ownership_snapshot={'claims': [{'task_id': 'another', 'state': 'active',
                    'location_key': str(paths[0])}]})
            before = records(flow.root)
            with self.assertRaisesRegex(InventoryDraftError, 'ownership'):
                build_inventory_review_draft(task, dry_run=True, source_basis=basis)
            self.assertEqual(before, records(flow.root))


class ExplicitInventoryBoundaryTests(unittest.TestCase):
    def test_explicit_limits_and_paths_and_disc_ancestors_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            root.mkdir()
            video = root / 'one.mkv'
            video.write_bytes(b'disposable metadata-only sample')
            outside = root.parent / 'outside.mkv'
            outside.write_bytes(b'outside')
            disc = root / 'BDMV' / 'STREAM' / '00001.m2ts'
            disc.parent.mkdir(parents=True)
            disc.write_bytes(b'disc')
            for label, paths, limit in (
                ('empty', (), 10), ('duplicate', (video, video), 10),
                ('outside', (outside,), 10), ('relative', (Path('one.mkv'),), 10),
                ('traversal', (root / '..' / 'outside.mkv',), 10),
                ('directory', (root,), 10), ('limit', (video, disc), 1), ('disc', (disc,), 10),
            ):
                with self.subTest(label=label), self.assertRaises(MovieAssetInventoryError):
                    inventory_movie_assets(root, source_paths=paths, max_entries=limit)
            link = root / 'linked.mkv'
            # Windows test host supports local symlinks; failure to create one
            # is evidence failure, not a silently skipped link contract.
            link.symlink_to(outside)
            with self.assertRaises(MovieAssetInventoryError):
                inventory_movie_assets(root, source_paths=(link,))
            first = inventory_movie_assets(root, source_paths=(video,))
            (root / 'another.mkv').write_bytes(b'not admitted')
            self.assertTrue(compare_movie_asset_inventory(root, first, source_paths=(video,)).unchanged)
            video.write_bytes(b'changed')
            self.assertFalse(compare_movie_asset_inventory(root, first, source_paths=(video,)).unchanged)
