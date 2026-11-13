"""Incident counts at current entrypoints; disposable tests, not GUI timing."""
from collections import Counter
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from src.rename import source_basis, initial_movie_completion, initial_tv_completion
from src.rename.movie_assets import inventory_movie_assets, MovieAssetInventoryError
from src.rename.task_batch_scope import TaskSourceScope
from tests import test_source_observation_batch as batch
from tests import test_initial_movie_completion_contract as movie
from tests import test_initial_tv_completion_contract as tv


class OperationCostContracts(unittest.IsolatedAsyncioTestCase):
    def test_explicit_inventory_rejects_intermediate_reparse_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / 'actual'
            target.mkdir()
            (target / 'episode.mkv').write_bytes(b'disposable')
            link = root / 'alias'
            if os.name == 'nt':
                result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                        capture_output=True)
                self.assertEqual(0, result.returncode, result.stderr)
            else:
                link.symlink_to(target, target_is_directory=True)
            try:
                leaf = link / 'episode.mkv'
                self.assertFalse(leaf.is_symlink(), 'the link is a parent, not the leaf')
                with self.assertRaises(MovieAssetInventoryError):
                    inventory_movie_assets(root, source_paths=(leaf,))
            finally:
                if os.name == 'nt':
                    os.rmdir(link)  # Remove only this disposable junction, never its target.
                else:
                    link.unlink()

    async def test_basis_projection_does_not_access_source_paths(self):
        from tests.test_inventory_reuse import source_access
        h=batch.BatchConsumers('runTest')
        self.addCleanup(h.doCleanups)
        _,_,state,plan,_,_=await h.selection()
        raw=state.initial_source_basis.to_dict()
        with source_access(plan.source_root) as calls:
            result=source_basis.project_source_basis(task_id=plan.task_id,source_root=plan.source_root,
                batch_id=raw['batch_id'],scope=TaskSourceScope.from_mapping(raw['task_source_scope']))
        self.assertEqual(state.initial_source_basis,result)
        self.assertEqual([],calls)

    def test_explicit_inventory_shares_parent_walks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = tuple(root / f'episode {index}.mkv' for index in range(6))
            for path in paths:
                path.write_bytes(b'disposable')
            expected = inventory_movie_assets(root, source_paths=paths)
            calls = Counter()
            actual = Path.lstat
            def counted(path, *args, **kwargs):
                calls[path] += 1
                return actual(path, *args, **kwargs)
            with patch.object(Path, 'lstat', counted):
                result = inventory_movie_assets(root, source_paths=paths)
            self.assertEqual(expected, result)
            self.assertLessEqual(calls[root], 4, 'existing root check plus a checked parent batch')

    async def test_movie_prepare_does_not_repeat_final_transaction_validation(self):
        h = movie.InitialMovieFullCompletionContracts('runTest')
        h.setUp()
        self.addCleanup(h.doCleanups)
        _, service, plan, primary, _ = await h.fixture()
        with patch.object(initial_movie_completion, '_validate_current',
                          wraps=initial_movie_completion._validate_current) as validation:
            intent = h.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
            prepare_calls = validation.call_count
            service.commit_initial_movie_completion(intent)
            total_calls = validation.call_count
        self.assertIs(type(intent), initial_movie_completion.InitialMovieCompletionIntent)
        self.assertEqual(1, total_calls - prepare_calls, 'locked commit must still validate authority')
        self.assertEqual(1, prepare_calls, 'commit repeats the boundary, not prepare tail')

    async def test_tv_prepare_does_not_build_an_unused_final_plan(self):
        h = tv.InitialTVCompletionContracts('runTest')
        self.addCleanup(h.doCleanups)
        preparing = False
        calls = []
        actual_prepare = initial_tv_completion.prepare
        actual_build = initial_tv_completion.build_initial_tv_completion
        def prepare(*args, **kwargs):
            nonlocal preparing
            preparing = True
            try:
                return actual_prepare(*args, **kwargs)
            finally:
                preparing = False
        def build(*args, **kwargs):
            calls.append(preparing)
            return actual_build(*args, **kwargs)
        with patch.object(initial_tv_completion, 'prepare', prepare), \
                patch.object(initial_tv_completion, 'build_initial_tv_completion', build):
            await h.fixture(anime=True)
        self.assertGreaterEqual(calls.count(False), 2, 'commit and locked validation still build')
        self.assertEqual(0, calls.count(True), 'prepare only needs observations, not a discarded final plan')
