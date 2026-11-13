"""Same-source AddTask uses a new unapproved repair, preserving source ownership."""
from tests.source_collection_fixture import collected_files, collected_identity
import json
import tempfile
from pathlib import Path
import unittest

from src.rename.library_assets import LibraryAssets
from src.rename.task_batch_scope import BatchTaskSourceRequest
from src.task_queue import TaskQueueManager
from tests import test_completed_rebuild_contract as fixture


class AssetAdmissionContracts(unittest.IsolatedAsyncioTestCase):
    async def test_movie_versions_have_distinct_frozen_labels_in_library(self):
        from tests import test_completed_rebuild_work_contract as movie
        h = movie.CompletedMovieRebuildContracts('runTest')
        h.setUp()
        self.addCleanup(h.doCleanups)
        first, second = await h.pair()
        assets = LibraryAssets(h.rebuild_service()).list_assets()
        self.assertEqual(2, len(assets))
        self.assertEqual(2, len({row.version for row in assets}))
        from src.rename.library_assets import version_label
        self.assertEqual({version_label(first.plan), version_label(second.plan)}, {row.version for row in assets})
        self.assertTrue(all(row.version for row in assets))

    async def test_same_source_returns_original_owner_and_repairs_without_source_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _ = await fixture.CompletedRebuildContracts().complete(root)
            plan = result.plan
            source = Path(json.loads((root/'tasks'/f'{plan.task_id}.json').read_text('utf-8'))['path'])
            request = BatchTaskSourceRequest('new-request', (str(source.resolve()),), 'parent_directory',
                tuple(collected_identity(p) for p in collected_files(source)), 'tv')
            assets = LibraryAssets(fixture.recovery(root))
            self.assertEqual(plan.task_id, assets.completed_source(request))
            owned = fixture.snapshot_files(root/'source_ownership')
            plan.entries[0].target_path.unlink()
            revised = assets.repair(plan.task_id)
            self.assertEqual(plan.task_id, revised.task_id)
            self.assertFalse(revised.entries[0].target_path.exists())
            self.assertEqual('need_confirm', json.loads((root/'tasks'/f'{plan.task_id}.json').read_text('utf-8'))['workflow_status'])
            self.assertNotEqual(owned, {})
            self.assertIsNone(assets.completed_source(request))

    async def test_repeat_pending_repair_refreshes_without_executing_or_changing_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _ = await fixture.CompletedRebuildContracts().complete(root)
            result.plan.entries[0].target_path.unlink()
            assets = LibraryAssets(fixture.recovery(root))
            first = assets.repair(result.plan.task_id)
            second = assets.repair(result.plan.task_id)
            self.assertEqual(first.metadata['completed_rebuild_observation']['origin_plan_fingerprint'],
                             second.metadata['completed_rebuild_observation']['origin_plan_fingerprint'])
            self.assertFalse(second.entries[0].target_path.exists())
