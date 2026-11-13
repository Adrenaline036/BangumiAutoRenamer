"""Bounded repeated-work regressions; not real-media acceptance evidence."""
from tests.source_collection_fixture import collected_identity
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename import default_activation, default_content
from src.rename.physical_identity import canonical_location_key
from src.rename.projection_authority import SourceIdentity
from src.task_queue import TaskQueueManager


class PathWork(unittest.TestCase):
    def test_already_resolved_identity_is_not_resolved_twice(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'Film.mkv'
            path.write_bytes(b'owned component sample')
            expected = canonical_location_key(path)
            original = Path.resolve
            for factory in (lambda p: SourceIdentity.from_path(p, inventory_fingerprint='inventory'),
                            collected_identity):
                calls = []
                def resolve(item, *args, **kwargs):
                    calls.append(item)
                    return original(item, *args, **kwargs)
                with self.subTest(factory=factory), patch.object(Path, 'resolve', resolve):
                    identity = factory(path)
                    self.assertEqual(expected, identity.location_key)
                    self.assertEqual(1, len(calls), 'identity resolves an already resolved source again')

class PreparationWork(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_projects_basis_only_at_canonical_publication(self):
        from tests.test_scoped_inventory_workspace import ScopedInventoryWorkspaceTests
        from src.pages.initial_workspace import create_initial_workspace
        from src.rename import source_basis
        helper = ScopedInventoryWorkspaceTests('runTest')
        self.addCleanup(helper.doCleanups)
        async with helper.flow(automatic=False, admission_only=True,
                configure_source=helper.sources, admit=helper.admit) as flow:
            with patch.object(source_basis, 'project_source_basis',
                              wraps=source_basis.project_source_basis) as observer:
                await create_initial_workspace(flow.task_id, flow.manager._read(flow.task_id))
            helper.assert_scope(flow)
            self.assertEqual(1, observer.call_count,
                '首次清单只能由唯一发布入口绑定')

    async def test_same_stat_replacement_does_not_silently_replace_frozen_inventory(self):
        import os
        from tests.test_scoped_inventory_workspace import ScopedInventoryWorkspaceTests
        from src.pages.initial_workspace import create_initial_workspace
        from src.rename import source_basis
        helper = ScopedInventoryWorkspaceTests('runTest')
        self.addCleanup(helper.doCleanups)
        async with helper.flow(automatic=False, admission_only=True,
                configure_source=helper.sources, admit=helper.admit) as flow:
            selected = sorted(flow.source.glob('*S01E*.mkv'))[0]
            actual = source_basis.observe_admitted_basis
            def replaced(*args, **kwargs):
                basis = actual(*args, **kwargs)
                before = selected.stat()
                replacement = selected.with_suffix('.replacement')
                replacement.write_bytes(selected.read_bytes())
                os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
                os.replace(replacement, selected)
                self.assertEqual(before.st_size, selected.stat().st_size)
                self.assertEqual(before.st_mtime_ns, selected.stat().st_mtime_ns)
                self.assertNotEqual(before.st_ino, selected.stat().st_ino)
                return basis
            with patch.object(source_basis, 'observe_admitted_basis', replaced):
                await create_initial_workspace(flow.task_id, flow.manager._read(flow.task_id))
            helper.assert_scope(flow)

    async def test_fresh_basis_does_not_trigger_an_extra_initial_inventory_scan(self):
        from tests.test_scoped_inventory_workspace import ScopedInventoryWorkspaceTests
        from src.pages.initial_workspace import create_initial_workspace
        helper = ScopedInventoryWorkspaceTests('runTest')
        self.addCleanup(helper.doCleanups)
        async with helper.flow(automatic=False, admission_only=True,
                configure_source=helper.sources, admit=helper.admit) as flow:
            with patch.object(default_activation, 'inventory_movie_assets',
                              wraps=default_activation.inventory_movie_assets) as inventory:
                await create_initial_workspace(flow.task_id, flow.manager._read(flow.task_id))
            helper.assert_scope(flow)
            self.assertEqual(0, inventory.call_count, 'fresh typed source observations are reread for inventory')
            from src.rename.movie_assets import inventory_from_source_basis, inventory_movie_assets
            basis = flow.service.decision_state_store.load(flow.task_id).initial_source_basis
            paths = tuple(Path(row['source_path']) for row in basis.to_dict()['sources'])
            expected = inventory_movie_assets(Path(basis.to_dict()['source_root']), source_paths=paths)
            with patch.object(Path, 'stat', side_effect=AssertionError('pure projection reads files')):
                actual = inventory_from_source_basis(basis)
            self.assertEqual(expected, actual, 'classification, ordering and fingerprint must remain identical')
            for invalid in (None, basis.to_dict(), object()):
                with self.subTest(invalid=type(invalid)), self.assertRaises(RuntimeError):
                    inventory_from_source_basis(invalid)

    async def test_default_preview_checks_origin_only_once(self):
        from tests.test_default_content_initialization import DefaultInitializationContracts
        helper = DefaultInitializationContracts('runTest')
        self.addCleanup(helper.doCleanups)
        service, plan, *_ = await helper.ready(helper.root())
        with patch.object(default_content, '_origin', wraps=default_content._origin) as origin:
            helper.prepare(service, plan)
        self.assertEqual(1, origin.call_count, 'preview repeats the origin checked by its builder')

    async def test_initial_movie_uses_existing_workspace_not_discarded_legacy_draft(self):
        from tests.test_default_movie_completion import InitialMovieCompletionContracts
        from src.rename import process
        helper = InitialMovieCompletionContracts('runTest')
        self.addCleanup(helper.doCleanups)
        with patch.object(process, 'build_inventory_review_draft',
                          wraps=process.build_inventory_review_draft) as draft:
            _, result, before, _ = await helper.fixture(helper.root(), initialize_before_selection=True)
        self.assertTrue(before)
        self.assertTrue(result.state.confirmed_work)
        self.assertIsNone(result.state.movie_version_selection)
        self.assertEqual(0, draft.call_count, 'first movie handoff builds a legacy draft it never consumes')
