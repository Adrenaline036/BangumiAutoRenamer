"""Shared inventory observation/projection seam; no publication authority.

All files belong to disposable fixtures. Queue controls use the existing real
producer with frozen transport; the pure projector never opens a source/store.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename import inventory_plan
from src.rename import default_content
from src.rename.movie_assets import inventory_movie_assets
from src.rename.plan_store import PlanStore
from tests import test_inventory_plan as legacy_inventory
from tests import test_default_activation_contract as activation


@contextmanager
def no_file_io():
    with ExitStack() as stack:
        for name in ('builtins.open', 'os.stat', 'os.lstat', 'os.scandir'):
            stack.enter_context(patch(name,
                side_effect=AssertionError('pure inventory projection attempted direct I/O: ' + name)))
        for name in ('is_file', 'is_dir', 'stat', 'lstat', 'resolve', 'open',
                     'read_bytes', 'read_text', 'iterdir', 'glob', 'rglob'):
            stack.enter_context(patch.object(Path, name,
                side_effect=AssertionError('pure inventory projection attempted file I/O: ' + name)))
        yield


class InventoryWorkspacePreparationContracts(unittest.TestCase):
    def task(self, root, *, single=False, mixed=False):
        source = root / 'source'
        source.mkdir()
        first = source / 'Feature.mkv'
        first.write_bytes(b'owned inventory fixture')
        if not single:
            (source / 'Feature.en.srt').write_text('owned subtitle', encoding='utf-8')
            (source / 'Feature [CM].mkv').write_bytes(b'owned commercial')
            (source / 'README.txt').write_text('owned instructions', encoding='utf-8')
        source = first if single else source
        helper = legacy_inventory.InventoryPlanTests(methodName='runTest')
        task = (helper._mixed_task(source, root / 'library', root / 'library' / 'Draft')
                if mixed else helper._task(source, root / 'library'))
        return task

    def api(self):
        for name in ('observe_inventory_workspace', 'project_inventory_workspace', 'InventoryWorkspaceObservation'):
            self.assertTrue(hasattr(inventory_plan, name), 'missing shared inventory preparation API: ' + name)
        return inventory_plan.observe_inventory_workspace, inventory_plan.project_inventory_workspace

    def test_existing_builder_has_no_file_io_after_inventory_and_ownership_observation(self):
        # This reaches today's real assembly (not merely a missing future API).
        # No ownership claims: all necessary location checks are part of the
        # inventory observation; building the placeholder must need no rescan.
        with tempfile.TemporaryDirectory() as raw, ExitStack() as guards:
            task = self.task(Path(raw), single=True)
            compare = inventory_plan.compare_movie_asset_inventory
            def compared(*args, **kwargs):
                result = compare(*args, **kwargs)
                guards.enter_context(no_file_io())
                return result
            with patch.object(inventory_plan, 'compare_movie_asset_inventory', side_effect=compared):
                draft = inventory_plan.build_inventory_review_draft(task, dry_run=True)
            self.assertEqual(1, len(draft.entries))
            self.assertIsNone(draft.projection_receipt)

    def test_observation_projects_without_task_records_or_further_file_io(self):
        observe, project = self.api()
        for single, mixed in ((True, False), (False, False), (False, True)):
            with self.subTest(single=single, mixed=mixed), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                task = self.task(root, single=single, mixed=mixed)
                expected = inventory_plan.build_inventory_review_draft(task, dry_run=True)
                observation = observe(task, dry_run=True)
                self.assertIs(type(observation), inventory_plan.InventoryWorkspaceObservation)
                self.assertFalse((root / 'tasks').exists())
                with no_file_io():
                    actual = project(observation)
                self.assertEqual(expected.to_dict(), actual.to_dict())
                self.assertIsNone(actual.projection_receipt)
                self.assertTrue(all(e.requires_review for e in actual.entries))
                self.assertFalse((root / 'library').exists())

    def test_observation_has_no_mutable_task_alias_and_does_not_reapply_default_filter(self):
        observe, project = self.api()
        with tempfile.TemporaryDirectory() as raw:
            task = self.task(Path(raw), mixed=True)
            observation = observe(task, dry_run=True)
            expected = project(observation)
            task['provider_candidates'].clear()
            task['name'] = 'later caller data'
            task['movie_inventory']['entries'].clear()
            with self.assertRaises((FrozenInstanceError, AttributeError)):
                observation.source_root = Path(raw) / 'other'
            with no_file_io():
                actual = project(observation)
            self.assertEqual(expected.to_dict(), actual.to_dict())
            self.assertTrue(any('[CM]' in p.name for p in actual.expected_sources))
            self.assertTrue(any(p.name == 'README.txt' for p in actual.expected_sources))
            self.assertFalse(actual.ignored_sources, 'this layer must not redo content selection')

    def test_fresh_adapter_rejects_drift_but_frozen_draft_is_not_a_live_proof(self):
        observe, project = self.api()
        with tempfile.TemporaryDirectory() as raw:
            task = self.task(Path(raw), single=True)
            observation = observe(task, dry_run=True)
            Path(task['path']).write_bytes(b'a later different source payload')
            with self.assertRaisesRegex(inventory_plan.InventoryDraftError, '漂移'):
                inventory_plan.build_inventory_review_draft(task, dry_run=True)
            with no_file_io():
                stale_draft = project(observation)
            self.assertIsNone(stale_draft.projection_receipt)
            self.assertTrue(all(e.requires_review for e in stale_draft.entries))

    def test_projector_rejects_untyped_input(self):
        _, project = self.api()
        for value in (None, {}, True, object()):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                project(value)

    def test_invalid_observation_shape_and_missing_task_inputs_are_rejected(self):
        observe, _ = self.api()
        with tempfile.TemporaryDirectory() as raw:
            task = self.task(Path(raw))
            observation = observe(task, dry_run=True)
            for changes in ({'task_json': '[]'}, {'task_json': '{"unexpected":1}'},
                            {'files': []}, {'files': ((object(), Path(raw)),)},
                            {'dry_run': []},
                            {'files': ((replace(observation.files[0][0], relative_path=[]), observation.files[0][1]),)},
                            {'source_root': str(raw)}):
                with self.subTest(changes=tuple(changes)), self.assertRaises(ValueError):
                    replace(observation, **changes)
            for value in (None, True, {}, {**task, 'draft_target_root': ''}):
                with self.subTest(value=type(value).__name__), self.assertRaises(inventory_plan.InventoryDraftError):
                    observe(value, dry_run=True)

    def test_fixed_single_and_mixed_placeholder_contract_not_only_same_builder_oracle(self):
        observe, project = self.api()
        for single in (True, False):
            with self.subTest(single=single), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                task = self.task(root, single=single, mixed=not single)
                draft = project(observe(task, dry_run=True))
                names = {'Feature.mkv'} if single else {'Feature.mkv', 'Feature [CM].mkv', 'Feature.en.srt', 'README.txt'}
                self.assertEqual({root / 'source' / name for name in names}, set(draft.expected_sources))
                initial = root / 'library' if single else root / 'library' / 'Draft'
                self.assertEqual({initial / 'ManualReview' / name for name in names}, {e.target_path for e in draft.entries})
                self.assertEqual(['movie-continuous'] if single else ['movie-continuous', 'tv-episodes'],
                                 [group.group_id for group in draft.presentation_groups])
                for key, value in {'target_root': str(root / 'library'), 'mapping_source': 'inventory_review_draft',
                        'canonical_work_root': str(initial), 'season_source': 'unset',
                        'presentation_assignment_state': 'draft' if single else 'placeholder',
                        'inventory_fingerprint': task['movie_inventory']['fingerprint']}.items():
                    self.assertEqual(value, draft.metadata[key], key)
                self.assertIsNone(draft.projection_receipt)
                self.assertEqual('', draft.revision_parent_fingerprint)
                self.assertFalse(draft.ignored_sources)
                if not single:
                    from src.rename.subtitle_labels import DimensionResolution
                    subtitle = next(e.subtitle for e in draft.entries if e.media_kind == 'subtitle')
                    self.assertIsNone(subtitle.associated_video_source_path)
                    self.assertEqual({root / 'source' / name for name in ('Feature.mkv', 'Feature [CM].mkv')},
                                     set(subtitle.association_candidates))
                    self.assertEqual(DimensionResolution.CONFLICT, subtitle.association_state)

    def test_ordinary_adapter_cannot_bypass_disconnected_shared_projector(self):
        self.api()
        with tempfile.TemporaryDirectory() as raw:
            task = self.task(Path(raw))
            with patch.object(inventory_plan, 'project_inventory_workspace',
                              side_effect=RuntimeError('shared projector disconnected')) as called:
                with self.assertRaisesRegex(RuntimeError, 'shared projector disconnected'):
                    inventory_plan.build_inventory_review_draft(task, dry_run=True)
            self.assertEqual(1, called.call_count)
            for invalid in (None, object()):
                with patch.object(inventory_plan, 'project_inventory_workspace', return_value=invalid):
                    with self.assertRaises(inventory_plan.InventoryDraftError):
                        inventory_plan.build_inventory_review_draft(task, dry_run=True)


class InventoryWorkspaceProducerContracts(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow
    current = activation.DefaultActivationProducerContracts.current

    async def test_real_queue_disconnected_or_invalid_projector_never_publishes_workspace(self):
        self.assertTrue(callable(default_content.build_initial_default))
        for mode in ('raises', 'none', 'object'):
            with self.subTest(mode=mode):
                kwargs = ({'side_effect': RuntimeError('shared projector disconnected')}
                          if mode == 'raises' else {'return_value': None if mode == 'none' else object()})
                with patch.object(default_content, 'build_initial_default', **kwargs) as called:
                    async with self.flow(automatic=False) as flow:
                        self.assertEqual(1, called.call_count)
                        self.assertFalse(flow.commits, 'queue must not fall back to another canonical producer')
                        self.assertFalse(flow.store.path_for(flow.task_id).exists())
                        self.assertFalse(flow.service.decision_state_store.path_for(flow.task_id).exists())
                        task = flow.manager._read(flow.task_id)
                        self.assertTrue(task.get('error'))
                        self.assertNotEqual('completed', task.get('workflow_status'))

    async def test_real_default_queue_uses_shared_projector_before_canonical_publication(self):
        self.assertTrue(callable(default_content.build_initial_default))
        with patch.object(default_content, 'build_initial_default',
                          wraps=default_content.build_initial_default) as called:
            async with self.flow(automatic=False) as flow:
                plan, state = self.current(flow)
                self.assertEqual(2, called.call_count, 'prepare and commit validation consume the same builder')
                self.assertEqual(called.call_args_list[0], called.call_args_list[1])
                self.assertTrue(flow.commits)
                self.assertIsNotNone(state.content_selection)
                self.assertFalse(state.confirmed_work)
                self.assertIsNotNone(plan.projection_receipt)
                self.assertEqual(flow.task_id, called.call_args.args[0].observation.task_id)

    async def test_admitted_basis_uses_complete_scope_and_conflicting_owner_does_not_shrink_it(self):
        self.assertTrue(hasattr(inventory_plan, 'observe_inventory_workspace'), 'missing shared observation')
        async with self.flow(automatic=False) as flow:
            plan, state = self.current(flow)
            task = {**flow.manager._read(flow.task_id),
                    'movie_inventory': inventory_movie_assets(plan.source_root,
                        source_paths=tuple(Path(r['source_path']) for r in state.initial_source_basis.to_dict()['sources'])).to_dict()}
            observe = inventory_plan.observe_inventory_workspace
            observation = observe(task, dry_run=True, source_basis=state.initial_source_basis)
            with no_file_io():
                draft = inventory_plan.project_inventory_workspace(observation)
            self.assertEqual(set(plan.expected_sources), set(draft.expected_sources))
            before = PlanStore.fingerprint(plan)
            task['source_ownership_snapshot'] = {'claims': [dict(state='active', task_id='other-task',
                location_key=str(draft.expected_sources[0].resolve()))]}
            with self.assertRaisesRegex(inventory_plan.InventoryDraftError, 'ownership冲突'):
                observe(task, dry_run=True, source_basis=state.initial_source_basis)
            self.assertEqual(before, PlanStore.fingerprint(flow.store.load(flow.task_id)))
