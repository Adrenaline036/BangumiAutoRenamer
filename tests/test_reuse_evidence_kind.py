"""同一文件的复用决定保持其冻结证据类型。"""
import os
from pathlib import Path
import tempfile
import unittest
from src.rename.asset_collision import build_reuse_resolution, validate_reuse_resolution, AssetCollisionError
from src.rename.collision_projection import CollisionProbes, MissingCollisionProbe, observe_path
from src.rename.media_file_identity import MEDIA_IDENTITY
from src.rename.small_file_identity import SMALL_CONTENT


class ReuseEvidenceKindContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def pair(self, suffix, *, linked):
        source, target = self.root/('source'+suffix), self.root/('target'+suffix)
        source.write_bytes(b'isolated fixture bytes')
        if linked:
            os.link(source, target)
        else:
            target.write_bytes(source.read_bytes())
        return source, target

    def test_same_inode_small_file_keeps_content_evidence_in_both_producers(self):
        source, target = self.pair('.ass', linked=True)
        direct = build_reuse_resolution(source, target)
        probes = CollisionProbes(tuple(observe_path(p, full_hash=True) for p in (source,target)))
        projected = probes.reuse(source, target)
        for resolution in (direct, projected):
            with self.subTest(resolution=resolution):
                self.assertEqual(SMALL_CONTENT, resolution.evidence_kind)
                self.assertTrue(resolution.source_xxh3_128)
                self.assertEqual(resolution.source_xxh3_128, resolution.target_xxh3_128)
                validate_reuse_resolution(source, target, resolution)

    def test_small_file_without_frozen_content_cannot_use_inode_as_fallback(self):
        source, target = self.pair('.ass', linked=True)
        probes = CollisionProbes(tuple(observe_path(p) for p in (source,target)))
        with self.assertRaises(MissingCollisionProbe):
            probes.reuse(source,target)

    def test_media_requires_same_inode_without_content_fallback(self):
        source, target = self.pair('.mkv', linked=True)
        resolution = build_reuse_resolution(source,target)
        self.assertEqual(MEDIA_IDENTITY,resolution.evidence_kind)
        self.assertIsNone(resolution.source_xxh3_128)
        validate_reuse_resolution(source,target,resolution)
        different = self.root/'another.mkv'
        different.write_bytes(source.read_bytes())
        self.assertIsNone(build_reuse_resolution(source,different))

    def test_small_file_content_or_entity_drift_remains_rejected(self):
        source, target = self.pair('.ass', linked=False)
        resolution = build_reuse_resolution(source,target)
        self.assertEqual(SMALL_CONTENT,resolution.evidence_kind)
        validate_reuse_resolution(source,target,resolution)
        target.write_bytes(b'changed fixture bytes!')
        self.assertIsNone(build_reuse_resolution(source,target))
        with self.assertRaises(AssetCollisionError):
            validate_reuse_resolution(source,target,resolution)
