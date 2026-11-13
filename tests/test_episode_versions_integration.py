"""版本选择通过原作品刷新、批准执行和既有技术版本路径。"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from tests import test_episode_versions_contract as fixture
from tests import _media_flow_helpers as media
from src.rename.plan_verifier import PlanVerifier


class EpisodeVersionsIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_approval_execution_catalog_and_source_integrity(self):
        import hashlib
        import json
        import os
        f = fixture.EpisodeVersionsContracts('runTest')
        async with f.flow(automatic=False, configure_source=fixture.package) as flow:
            plan, _ = await f.selected(flow)
            plan = f.save(flow, plan)
            expected = {str(e.source_path): (e.target_path, e.presentation_asset) for e in plan.entries}
            digests = {e.source_path: hashlib.sha256(e.source_path.read_bytes()).hexdigest() for e in plan.entries}
            await flow.manager.retry_current(flow.task_id)
            await asyncio.wait_for(flow.manager._queue.join(), 15)
            refreshed = flow.store.load(plan.task_id)
            self.assertNotEqual(flow.store.fingerprint(plan), flow.store.fingerprint(refreshed), flow.manager._read(flow.task_id).get('error'))
            self.assertEqual(expected, {str(e.source_path): (e.target_path, e.presentation_asset) for e in refreshed.entries})
            self.assertEqual([], list(PlanVerifier().verify(refreshed).issues))
            task, ledger, service = await media.MediaFlow('runTest')._approve_and_execute(
                root=flow.root, result=SimpleNamespace(plan=refreshed))
            self.assertEqual('completed', task.get('execution_status'), task.get('error'))
            self.assertEqual('completed', ledger.get('status'))
            record = json.loads(service.series_catalog._path(refreshed.metadata['series_key']).read_text('utf-8'))
            self.assertEqual(1, len(record['season_bindings']))
            for e in refreshed.entries:
                self.assertTrue(os.path.samefile(e.source_path, e.target_path))
                self.assertEqual(digests[e.source_path], hashlib.sha256(e.source_path.read_bytes()).hexdigest())
                self.assertEqual(digests[e.source_path], hashlib.sha256(e.target_path.read_bytes()).hexdigest())

    async def test_existing_resolution_groups_and_ambiguous_duplicates(self):
        from src.rename.tv_video_projection import TVVideoInput, project_tv_videos
        from pathlib import Path
        f = fixture.EpisodeVersionsContracts('runTest')
        async with f.flow(automatic=False) as flow:
            plan, _ = await f.selected(flow)
            entry = next(e for e in plan.entries if e.media_kind == 'video')
            def project(names):
                videos = tuple(replace(entry, source_path=plan.source_root / n) for n in names)
                return project_tv_videos(TVVideoInput(videos, plan.source_root, 'Show',
                    plan.presentation_groups[0].group_id, Path(plan.metadata['canonical_work_root']), 1))
            result = project(('Show [12] [1080p].mkv', 'Show [12] [2160p].mkv'))
            self.assertFalse(result.episode_conflicts)
            self.assertEqual({12}, {r.episode_id for r in result.mappings})
            self.assertEqual(2, len({r.target for r in result.mappings}))
            for names in (('Show S01E01 [1080p].mkv', 'Alternate S01E01 [1080p].mkv'),
                          ('Show S01E01 [1080p 2160p].mkv', 'Show S01E01 [720p].mkv')):
                result = project(names)
                self.assertTrue(result.episode_conflicts)
                self.assertTrue(all(not r.is_content and r.episode_id is None for r in result.mappings))
