"""Operation-local metadata regressions; disposable sources, not acceptance."""
from collections import Counter
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.rename import content_selection, source_basis, default_content, path_safety
from src.rename.projection_authority import ProjectionCommitService, SourceIdentity
from src.rename.task_batch_scope import TaskSourceScope
from tests import test_initial_source_basis as basis_fixture


class BatchConsumers(unittest.IsolatedAsyncioTestCase):
    root = basis_fixture.InitialSourceBasisContracts.root
    manager = basis_fixture.InitialSourceBasisContracts.manager
    request = basis_fixture.InitialSourceBasisContracts.request
    admission = basis_fixture.InitialSourceBasisContracts.admission
    pending = basis_fixture.InitialSourceBasisContracts.pending
    assert_policy = basis_fixture.InitialSourceBasisContracts.assert_policy

    def sources(self, root):
        folder = root / 'source'
        folder.mkdir()
        for index in range(6):
            (folder / f'Movie {index}.mkv').write_bytes(b'disposable source')
        return folder

    def counted(self):
        calls = Counter()
        actual = Path.lstat

        def observe(path, *args, **kwargs):
            calls[path] += 1
            return actual(path, *args, **kwargs)
        return calls, patch.object(Path, 'lstat', observe)

    async def selection(self):
        root = self.root()
        _, service, state, plan = await self.pending(root)
        choice, movie = default_content.build_default_initialization(service, plan, state)
        return root, service, state, plan, choice, movie

    async def test_retained_sources_use_frozen_identity_without_parent_checks(self):
        from tests.test_inventory_reuse import source_access
        _,_,_,plan,choice,_=await self.selection()
        self.assertEqual(6,len(choice.to_dict()['identities']))
        with source_access(plan.source_root) as calls:
            content_selection.validate_source_identities(choice,font_plan=plan)
        self.assertEqual([],calls)

    async def test_initial_basis_projects_without_observation_batches(self):
        from tests.test_inventory_reuse import source_access
        _,_,state,plan,_,_=await self.selection()
        raw=state.initial_source_basis.to_dict()
        with source_access(plan.source_root) as calls:
            actual=source_basis.project_source_basis(task_id=plan.task_id,source_root=plan.source_root,
                batch_id=raw['batch_id'],scope=TaskSourceScope.from_mapping(raw['task_source_scope']))
        self.assertEqual(state.initial_source_basis,actual)
        self.assertEqual([],calls)

    async def test_default_materials_reuse_frozen_sources_without_changing_choice(self):
        _, service, state, plan, choice, movie = await self.selection()
        from tests.test_inventory_reuse import source_access
        with source_access(plan.source_root) as calls:
            actual = default_content.build_default_initialization(service, plan, state)
        self.assertEqual((choice, movie), actual)
        self.assertEqual([],calls)

    async def test_omitted_and_retained_sources_both_keep_the_admitted_snapshot(self):
        from tests.test_inventory_reuse import source_access
        _,_,_,plan,choice,_=await self.selection()
        value=choice.to_dict()
        omitted=Path(value['identities'][0]['source_path'])
        value.update(excluded_sources=[str(omitted)],evidence=[dict(source_path=str(omitted),kind='omission-v1')])
        selected=content_selection.ContentSelection.from_mapping(value)
        omitted.rename(omitted.with_suffix('.held'))
        retained=Path(value['identities'][1]['source_path'])
        retained.rename(retained.with_suffix('.held'))
        with source_access(plan.source_root) as calls:
            content_selection.validate_source_identities(selected,font_plan=plan)
        self.assertEqual([],calls)
        with self.assertRaises((ValueError,OSError)):
            path_safety.observe_regular_files((retained,))

    async def test_source_freeze_keeps_existing_canonical_fields_and_duplicates(self):
        _, _, _, plan, _, _ = await self.selection()
        paths = (*plan.expected_sources, plan.expected_sources[0])
        expected = tuple(SourceIdentity.from_path(p, inventory_fingerprint='batch-test') for p in paths)
        self.assertEqual(expected, ProjectionCommitService._freeze_source_identities(
            paths, inventory_fingerprint='batch-test'))


    async def test_disconnected_projection_cannot_return_a_valid_choice_or_basis(self):
        root,service,state,plan,choice,_=await self.selection()
        from tests.test_content_choice_contract import records
        before=records(root);raw=state.initial_source_basis.to_dict()
        with patch.object(content_selection,'frozen_identities',return_value=None):
            with self.assertRaises((ValueError,TypeError,KeyError)):
                content_selection.validate_source_identities(choice,font_plan=plan)
            with self.assertRaises((ValueError,TypeError,KeyError)):
                default_content.build_default_initialization(service,plan,state)
        with patch.object(source_basis,'_basis_payload',return_value=None):
            with self.assertRaises((ValueError,TypeError,KeyError)):
                source_basis.project_source_basis(task_id=plan.task_id,source_root=plan.source_root,
                    batch_id=raw['batch_id'],scope=TaskSourceScope.from_mapping(raw['task_source_scope']))
        self.assertEqual(before,records(root))


class BatchMetadata(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.paths = tuple(self.root / f'片 {i}.mkv' for i in range(6))
        for path in self.paths:
            path.write_bytes(b'disposable')

    def test_counts_all_parents_leaves_duplicates_and_read_only_result(self):
        actual = Path.lstat
        for count in (1, 2, 6):
            with self.subTest(count=count):
                calls = Counter()
                paths = (*self.paths[:count], self.paths[0])
                def counted(path, *args, **kwargs):
                    calls[path] += 1
                    return actual(path, *args, **kwargs)
                with patch.object(Path, 'lstat', counted):
                    observed = path_safety.observe_regular_files(iter(paths))
                self.assertEqual(set(paths), set(observed))
                for path in set(paths):
                    self.assertEqual(1, calls[path])
                    self.assertEqual(actual(path), observed[path])
                for parent in self.paths[0].parents:
                    self.assertEqual(2, calls[parent])
                self.assertEqual(len(set(paths)) + 2 * len(self.paths[0].parents), sum(calls.values()))
                with self.assertRaises(TypeError):
                    observed[self.paths[0]] = None

    def test_separate_batches_detect_same_size_mtime_replacement(self):
        path = self.paths[0]
        before = path_safety.observe_regular_files((path,))[path]
        replacement = self.root / 'replacement.mkv'
        replacement.write_bytes(b'disposable')
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(path)
        after = path_safety.observe_regular_files((path,))[path]
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))
        self.assertNotEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        path.unlink()
        with self.assertRaises((OSError, ValueError)):
            path_safety.observe_regular_files((path,))

    def test_parent_replaced_mid_batch_is_rejected_not_cached_across_calls(self):
        actual = Path.lstat
        count = 0
        def changed(path, *args, **kwargs):
            nonlocal count
            result = actual(path, *args, **kwargs)
            if path == self.root:
                count += 1
                if count == 2:
                    return SimpleNamespace(st_mode=result.st_mode, st_dev=result.st_dev,
                                           st_ino=result.st_ino + 1)
            return result
        with patch.object(Path, 'lstat', changed), self.assertRaisesRegex(ValueError, '父目录已变化'):
            path_safety.observe_regular_files(self.paths)
        self.assertEqual(6, len(path_safety.observe_regular_files(self.paths)))

    def test_unrelated_sibling_change_does_not_invalidate_directory(self):
        actual = Path.lstat
        def siblings(path, *args, **kwargs):
            if path == self.paths[-1]:
                (self.root / 'unrelated.txt').write_bytes(b'unrelated sibling')
            return actual(path, *args, **kwargs)
        with patch.object(Path, 'lstat', siblings):
            self.assertEqual(6, len(path_safety.observe_regular_files(self.paths)))

    def test_parent_and_leaf_links_reparse_nonfiles_and_io_errors_reject(self):
        actual = Path.lstat
        for blocked in (self.root, self.paths[0]):
            for late in (False, True) if blocked == self.root else (False,):
                for fault in ('link', 'reparse', 'nonfile', 'missing', 'denied'):
                    with self.subTest(parent=blocked == self.root, late=late, fault=fault):
                        calls = 0
                        def failed(path, *args, **kwargs):
                            nonlocal calls
                            if path == blocked:
                                calls += 1
                                if not late or calls == 2:
                                    if fault == 'missing':
                                        raise FileNotFoundError('disposable missing')
                                    if fault == 'denied':
                                        raise PermissionError('disposable denied')
                                    mode = stat.S_IFLNK if fault == 'link' else (
                                        stat.S_IFDIR if path == self.paths[0] else stat.S_IFREG)
                                    return SimpleNamespace(st_mode=mode, st_file_attributes=0x400 if fault == 'reparse' else 0)
                            return actual(path, *args, **kwargs)
                        with patch.object(Path, 'lstat', failed), self.assertRaises((ValueError, OSError)):
                            path_safety.observe_regular_files(self.paths)

    def test_empty_and_invalid_paths(self):
        self.assertEqual({}, path_safety.observe_regular_files(()))
        for path in (Path('relative.mkv'), self.root / '..' / 'jump.mkv', self.root):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                path_safety.observe_regular_files((path,))


if __name__ == '__main__':
    unittest.main()
