"""首次选择只在真实事务锁内生成一次，仍拒绝漂移和旁路。"""
from contextlib import contextmanager
import unittest
from unittest.mock import patch

from src.rename import initial_movie_selection as selection
from src.rename.projection_authority import ProjectionCommitService
from tests import test_default_movie_completion as fixture


class MetadataPublication(unittest.IsolatedAsyncioTestCase):
    async def test_first_selection_builds_once_inside_transaction(self):
        helper = fixture.InitialMovieCompletionContracts('runTest')
        self.addCleanup(helper.doCleanups)
        locked, calls = False, []
        original_lock = ProjectionCommitService._transaction_lock
        original_build = selection.build

        @contextmanager
        def lock(transaction, task_id):
            nonlocal locked
            with original_lock(transaction, task_id):
                prior, locked = locked, True
                try:
                    yield
                finally:
                    locked = prior

        def build(*args, **kwargs):
            calls.append(locked)
            return original_build(*args, **kwargs)

        with patch.object(ProjectionCommitService, '_transaction_lock', lock), \
                patch.object(selection, 'build', build):
            _, result, before, _ = await helper.fixture(helper.root(), initialize_before_selection=True)
        self.assertTrue(result.state.confirmed_work)
        self.assertEqual(before['state'].content_selection, result.state.content_selection)
        self.assertEqual([True], calls, '一次实际选择只能在真实事务锁内构造一次')
