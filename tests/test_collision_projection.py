"""Actual disposable collisions, pure replay, and frozen absence/hash drift."""
from dataclasses import replace
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from src.rename.collision_projection import (CollisionProbes, MissingCollisionProbe,
    observe_collisions, project_collisions, verify_probes)
from src.rename.rename_plan import PlanEntry, PresentationAssetPlanContract, AssetRole
from src.rename.plan_review import _resolve_asset_collisions


class CollisionProjectionContracts(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        source = root / 'incoming' / 'Preview.mkv'
        target = root / 'work' / 'Extras' / 'Preview.mkv'
        source.parent.mkdir()
        target.parent.mkdir(parents=True)
        source.write_bytes(b'new bytes')
        target.write_bytes(b'old bytes')
        entry = PlanEntry(source, target, media_kind='video', presentation_asset=PresentationAssetPlanContract(
            'movie-continuous', AssetRole('extra'), None, None, 'shared'))
        return root, entry

    def test_frozen_projector_is_filesystem_free_and_has_no_missing_probe_fallback(self):
        root, entry = self.fixture()
        probes, expected = observe_collisions((entry,), root / 'work', package_label='incoming')
        with patch.object(Path, 'stat', side_effect=AssertionError('pure projector performed IO')), \
                patch.object(Path, 'lstat', side_effect=AssertionError('pure projector performed IO')), \
                patch.object(Path, 'open', side_effect=AssertionError('pure projector performed IO')):
            self.assertEqual(expected, project_collisions((entry,), root / 'work', probes, package_label='incoming'))
            with self.assertRaises(MissingCollisionProbe):
                project_collisions((entry,), root / 'work', CollisionProbes(()), package_label='incoming')
        self.assertEqual(entry.target_path.with_name('Preview - incoming.mkv'), expected.entries[0].target_path)
        self.assertEqual((), expected.unresolved)

    def test_target_appearance_and_physical_drift_reject_old_observation(self):
        for fault in ('source', 'target', 'backup', 'ancestor'):
            with self.subTest(fault=fault):
                root, entry = self.fixture()
                probes, expected = observe_collisions((entry,), root / 'work', package_label='incoming')
                if fault in ('source', 'target'):
                    path = entry.source_path if fault == 'source' else entry.target_path
                    path.write_bytes(b'changed size')
                elif fault == 'backup':
                    expected.entries[0].target_path.write_bytes(b'foreign target')
                else:
                    parent=entry.target_path.parent
                    parent.rename(root/'held-extras')
                    parent.mkdir()
                with self.assertRaises(ValueError):
                    verify_probes(probes)

    def test_real_hardlink_reuse_and_old_wrapper_projector_disconnect(self):
        root, entry = self.fixture()
        entry.target_path.rename(root/'held-target.mkv')
        os.link(entry.source_path,entry.target_path)
        probes, expected = observe_collisions((entry,), root / 'work')
        self.assertEqual('reuse_existing', expected.entries[0].collision_resolution.action)
        verify_probes(probes)
        with patch('src.rename.collision_projection.project_collisions', return_value=None):
            with self.assertRaisesRegex(ValueError, '投影器已断开'):
                _resolve_asset_collisions((entry,), root / 'work')
        forged = replace(probes.rows[0], exists=False)
        with self.assertRaises(ValueError):
            verify_probes(CollisionProbes((forged, *probes.rows[1:])))
