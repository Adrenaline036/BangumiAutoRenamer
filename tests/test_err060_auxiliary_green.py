"""Full source inventory without restoring non-watchable delivery requirements."""
from tests.source_collection_fixture import collected_files
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.rename.movie_assets import MovieAssetInventoryError, inventory_movie_assets
from src.rename.process import Rename
from src.rename.utils import canonical_extras_target
from src.task_queue import TaskQueueManager
from tests import _media_flow_helpers as movie_fixture


class AuxiliaryContracts(unittest.IsolatedAsyncioTestCase):
    async def test_movie_inventory_completion_preserves_nested_shared_auxiliary_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, primary, _ = movie_fixture._make_omoide(root / 'sources')
            auxiliary = []
            for relative in ('CDs/Disc 1/track.flac', 'CDs/Disc 2/track.flac',
                             'Scans/A/page.jpg', 'Scans/B/page.jpg',
                             primary.with_suffix('.png').name):
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'opaque-auxiliary')
                auxiliary.append(path)
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in auxiliary}
            self.assertTrue(set(auxiliary) <= set(collected_files(source)))
            case = movie_fixture.MediaFlow('runTest')
            initial = await case._run_provider_flow(root / 'flow', source, 'auxiliary')
            completed = await case._complete_movie(root=root / 'flow', result=initial, primary=primary)
            from src.rename.plan_verifier import PlanVerifier
            from src.rename.content_policy import require_media_ready
            require_media_ready(completed.plan)
            self.assertFalse(PlanVerifier().verify(completed.plan).issues)
            revision = completed.review.decision_state_store.load(completed.plan.task_id).revision
            from src.rename.plan_store import PlanStore
            from src.rename.plan_review import entry_id
            completed.review.approve_entries(completed.plan.task_id, PlanStore.fingerprint(completed.plan),
                [entry_id(e) for e in completed.plan.entries])
            default_excluded = set(auxiliary)
            self.assertTrue(default_excluded.isdisjoint(e.source_path for e in completed.plan.entries))
            self.assertTrue(default_excluded <= set(completed.plan.ignored_sources))
            # The default is editable: explicitly restore the requested picture.
            from tests.test_content_choice_contract import change
            picture = primary.with_suffix('.png')
            completed.plan = change(completed.review, completed.plan, [picture], action='restore')
            default_excluded.remove(picture)
            self.assertTrue(default_excluded <= set(completed.plan.ignored_sources))
            plan = completed.plan
            self.assertEqual(revision + 1, completed.review.decision_state_store.load(plan.task_id).revision)
            entries = {e.source_path: e for e in plan.entries}
            work_root = Path(plan.metadata['canonical_work_root'])
            self.assertTrue(set(auxiliary) <= set(plan.expected_sources))
            self.assertTrue(default_excluded.isdisjoint(entries))
            self.assertTrue(default_excluded <= set(plan.ignored_sources))
            # Preserve the existing unresolved companion's projection guard;
            # this does not approve it or restore any known CD/Scans material.
            for path in set(auxiliary) - default_excluded:
                entry = entries[path]
                self.assertEqual(canonical_extras_target(work_root, path.relative_to(source)), entry.target_path,
                                 entry.to_dict())
                self.assertEqual('shared', entry.presentation_asset.owner_version_ref)
                self.assertEqual('associated', entry.presentation_asset.asset_role.kind)
                self.assertNotEqual('subtitle_archive', entry.role)
                self.assertIsNone(entry.presentation_asset.version_descriptor)
                self.assertEqual('associated', entry.media_kind)
            audio = entries[primary.with_suffix('.mka')]
            self.assertEqual(entries[primary].target_path.with_suffix('.mka'), audio.target_path)
            self.assertNotEqual('shared', audio.presentation_asset.owner_version_ref)
            self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in auxiliary})
            self.assertFalse(work_root.exists())
            require_media_ready(plan)
            self.assertFalse(PlanVerifier().verify(plan).issues)
            self.assertFalse(completed.review.load_or_create(plan).approved_entry_ids)
            from src.rename.content_policy import require_content_complete
            forged = replace(plan, entries=tuple(replace(e, target_path=work_root / 'wrong.png')
                if e.source_path == picture else e for e in plan.entries))
            with self.assertRaisesRegex(ValueError, '唯一版本投影'):
                require_content_complete(forged)
            with patch('src.rename.inventory_completion_projection.project_auxiliary', return_value=None):
                with self.assertRaisesRegex(ValueError, '投影器已断开'):
                    change(completed.review, plan, [picture])
            self.assertEqual(PlanStore.fingerprint(plan),
                PlanStore.fingerprint(completed.review.plan_store.load(plan.task_id)))
            excluded = change(completed.review, plan, [picture])
            restored = change(completed.review, excluded, [picture], action='restore')
            require_media_ready(restored)
            self.assertEqual(entries[picture].target_path,
                next(e.target_path for e in restored.entries if e.source_path == picture))

    async def test_restored_unknown_archive_remains_blocked(self):
        from src.rename.content_policy import require_media_ready
        from src.rename.plan_review import PlanReviewError, entry_id
        from src.rename.plan_store import PlanStore
        from tests.test_content_choice_contract import change
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, primary, _ = movie_fixture._make_omoide(root / 'sources')
            archive = source / 'Scans' / 'unclassified.7z'
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b'unknown archive, never unpack')
            case = movie_fixture.MediaFlow('runTest')
            initial = await case._run_provider_flow(root / 'flow', source, 'unknown-auxiliary')
            completed = await case._complete_movie(root=root / 'flow', result=initial, primary=primary)
            completed.plan = change(completed.review, completed.plan, [archive], action='exclude')
            require_media_ready(completed.plan)
            restored = change(completed.review, completed.plan, [archive], action='restore')
            entry = next(e for e in restored.entries if e.source_path == archive)
            self.assertEqual('unknown', entry.presentation_asset.asset_role.kind)
            with self.assertRaisesRegex(ValueError, '未知用途'):
                require_media_ready(restored)
            with self.assertRaises(PlanReviewError):
                completed.review.approve_entries(restored.task_id, PlanStore.fingerprint(restored),
                    [entry_id(e) for e in restored.entries])
            self.assertEqual(b'unknown archive, never unpack', archive.read_bytes())
            self.assertFalse(any(e.target_path.exists() for e in restored.entries))

    def test_traditional_route_retains_deep_auxiliaries_without_expanding_video_scope(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'source'
            paths = []
            for relative in ('Series [01].mkv', 'CDs/A/track.flac', 'CDs/B/track.flac',
                             'Scans/booklet.png', 'Scans/deep/page.webp', 'Other/deep/Series [02].mkv'):
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                paths.append(path)
            renamer = object.__new__(Rename)
            renamer.R = {}
            with patch.object(renamer, '_prepare_subtitle_plan'):
                result = renamer._process_traditional(source, 'Series', root / 'target', 3)
            self.assertIsNone(result)
            for path in paths[1:-1]:
                self.assertEqual(canonical_extras_target(root / 'target' / 'Season 03', path.relative_to(source)), renamer.R[path])
            self.assertNotIn(paths[-1], renamer.R)
            self.assertEqual(4, len({renamer.R[p] for p in paths[1:-1]}))
            self.assertFalse((root / 'target').exists())

    def test_auxiliary_inventory_limits_and_links_remain_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'Scans').mkdir()
            (root / 'Scans' / 'page.png').touch()
            with self.assertRaises(MovieAssetInventoryError):
                inventory_movie_assets(root, max_entries=1)
            with patch('src.rename.movie_assets._safe_lstat', side_effect=MovieAssetInventoryError('UNTRUSTED_FILESYSTEM_LINK', 'Scans')):
                with self.assertRaises(MovieAssetInventoryError):
                    inventory_movie_assets(root)
        for relative in ('../outside.png', '/outside.png', 'Extras'):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                canonical_extras_target(Path('D:/target'), Path(relative))


if __name__ == '__main__':
    unittest.main()
