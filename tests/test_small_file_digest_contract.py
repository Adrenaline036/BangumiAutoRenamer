"""Companion collision comparison uses an explicit light digest, never media SHA."""
from pathlib import Path
import json
import os
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import patch

from src.rename.asset_collision import build_reuse_resolution, validate_reuse_resolution, AssetCollisionError
from src.rename.rename_plan import CollisionResolution
from src.rename.file_digest import digest_bytes


class SmallFileDigestContracts(unittest.TestCase):
    def test_kind_mixing_and_foreign_equal_target_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'a.ass'; target=root/'b.ass'
            source.write_bytes(b'content'); target.write_bytes(b'content')
            value=build_reuse_resolution(source,target)
            for altered in (replace(value,source_xxh3_128=None),):
                with self.assertRaises(AssetCollisionError):
                    validate_reuse_resolution(source,target,altered)
            raw=value.to_dict(); raw['source_sha256']='a'*64
            with self.assertRaises(ValueError): CollisionResolution.from_dict(raw)
            target.rename(root/'saved.ass'); target.write_bytes(b'content')
            with self.assertRaises(AssetCollisionError): validate_reuse_resolution(source,target,value)

    def test_real_small_replacement_ledger_and_rollback(self):
        from src.rename.asset_replacement import observe_replacement, ApprovedReplacementBatch
        from src.rename.trans import Trans
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); library=root/'library'; library.mkdir()
            source=root/'new.ass'; old=root/'old.ass'; target=library/'episode.ass'
            source.write_bytes(b'new subtitle'); old.write_bytes(b'old subtitle'); os.link(old,target)
            evidence=observe_replacement(source,target,library)
            authority=ApprovedReplacementBatch('small-replace','a'*64,(evidence,),
                        target_root=str(library),library_roots=(str(library),))
            transfer=Trans({source:target},'small-replace','a'*64,mode='hardlink',dry_run=False,
                record_root=root/'records',replacement_authority=authority,
                operations=[dict(source=str(source),target=str(target),action='replace',replacement_evidence=evidence.to_dict())])
            self.assertIsNone(transfer.trans_file())
            ledger=json.loads((root/'records/small-replace.ledger.json').read_text())
            self.assertEqual(digest_bytes(b'new subtitle'),ledger['entries'][0]['source_xxh3_128'])
            self.assertEqual('content-xxh3-128-v1',ledger['entries'][0]['evidence_kind'])
            transfer.recover_replacements(rollback=True)
            self.assertEqual(old.stat().st_ino,target.stat().st_ino)

    def test_independent_equal_subtitle_reuse_and_changed_bytes_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'a.ass'; target=root/'b.ass'
            source.write_bytes(b'subtitle'); target.write_bytes(b'subtitle')
            resolution=build_reuse_resolution(source,target)
            data=resolution.to_dict()
            self.assertEqual('content-xxh3-128-v1',data['evidence_kind'])
            self.assertEqual(digest_bytes(b'subtitle'),data['source_xxh3_128'])
            self.assertNotIn('source_sha256',data)
            self.assertEqual(resolution,CollisionResolution.from_dict(data))
            validate_reuse_resolution(source,target,resolution)
            target.write_bytes(b'changed!')
            with self.assertRaises(AssetCollisionError):
                validate_reuse_resolution(source,target,resolution)


    def test_retired_digest_records_are_rejected(self):
        from src.rename.asset_replacement import _fingerprint
        old = dict(action='reuse_existing', source_size=5, source_sha256='a'*64,
                   target_size=5, target_sha256='a'*64, target_device=1, target_inode=2)
        with self.assertRaises(ValueError): CollisionResolution.from_dict(old)
        with self.assertRaises(ValueError): _fingerprint(dict(size=5, sha256='a'*64, device=1, inode=2))
