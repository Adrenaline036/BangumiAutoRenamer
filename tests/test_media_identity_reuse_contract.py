"""Media reuse needs a real hardlink, never a whole-file content scan."""
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename.asset_collision import AssetCollisionError, build_reuse_resolution, validate_reuse_resolution
from src.rename.rename_plan import CollisionResolution


class MediaIdentityReuseContracts(unittest.TestCase):

    def test_audio_reuse_and_target_replacement_never_read_content(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / 'source.flac', root / 'target.flac'
            source.write_bytes(b'media')
            os.link(source, target)
            with patch('src.rename.asset_collision.small_fingerprint', side_effect=AssertionError('media content read')):
                resolution = build_reuse_resolution(source, target)
                validate_reuse_resolution(source, target, resolution)
                target.rename(target.with_suffix('.saved'))
                target.write_bytes(b'media')
                with self.assertRaises(AssetCollisionError):
                    validate_reuse_resolution(source, target, resolution)


    def test_new_identity_does_not_accept_missing_kind_or_null_hash_proof(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / 'source.mkv', root / 'target.mkv'
            source.write_bytes(b'media'); os.link(source, target)
            resolution = build_reuse_resolution(source, target)
            raw = resolution.to_dict(); raw.pop('evidence_kind')
            with self.assertRaises((ValueError, TypeError)):
                CollisionResolution.from_dict(raw)
            for altered in (replace(resolution, target_inode=0),):
                with self.assertRaises(AssetCollisionError):
                    validate_reuse_resolution(source, target, altered)

    def test_collision_projector_uses_stat_only_for_real_hardlink(self):
        from src.rename.collision_projection import CollisionProbes, observe_path
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root/'source.mkv', root/'target.mkv'
            source.write_bytes(b'media'); os.link(source, target)
            with patch('src.rename.collision_projection.small_fingerprint', side_effect=AssertionError('media hash')):
                probes = CollisionProbes((observe_path(source), observe_path(target)))
                self.assertEqual(build_reuse_resolution(source, target), probes.reuse(source, target))

    def test_real_hardlink_reuse_roundtrips_without_content_reads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / 'source.mkv', root / 'target.mkv'
            source.write_bytes(b'media')
            os.link(source, target)
            with patch('src.rename.asset_collision.small_fingerprint', side_effect=AssertionError('media content read')):
                resolution = build_reuse_resolution(source, target)
                self.assertEqual('hardlink-identity-v1', resolution.evidence_kind)
                self.assertNotIn('source_sha256', resolution.to_dict())
                self.assertEqual(resolution, CollisionResolution.from_dict(resolution.to_dict()))
                validate_reuse_resolution(source, target, resolution)

    def test_equal_bytes_in_distinct_files_are_not_media_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / 'source.mkv', root / 'target.mkv'
            source.write_bytes(b'media')
            target.write_bytes(b'media')
            with patch('src.rename.asset_collision.small_fingerprint', side_effect=AssertionError('media content read')):
                self.assertIsNone(build_reuse_resolution(source, target))
