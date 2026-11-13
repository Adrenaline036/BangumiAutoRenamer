"""Real temporary hardlinks for the approved, recoverable replacement writer."""

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.rename.trans import Trans


class AssetReplacementContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.source = self.root / "new.mkv"
        self.old_source = self.root / "old.mkv"
        self.source.write_bytes(b"new selected movie version")
        self.old_source.write_bytes(b"old selected movie version")
        self.target = self.library / "selected.mkv"
        os.link(self.old_source, self.target)
        self.other = self.library / "unselected.mkv"
        self.other.write_bytes(b"another version")
        self.user_file = self.library / "user.txt"
        self.user_file.write_bytes(b"user notes")
        self.records = self.root / "records"

    def transfer(self, *, authority=True):
        api = importlib.import_module("src.rename.asset_replacement")
        evidence = api.observe_replacement(self.source, self.target, self.library)
        capability = api.ApprovedReplacementBatch("replacement-test", "a" * 64, (evidence,),
            target_root=str(self.library), library_roots=(str(self.library),))
        return Trans(
            {self.source: self.target}, "replacement-test", "a" * 64,
            mode="hardlink", dry_run=False, record_root=self.records,
            operations=[dict(source=str(self.source), target=str(self.target),
                             action="replace", replacement_evidence=evidence.to_dict())],
            replacement_authority=capability if authority else None,
        )

    def assert_preserved(self):
        self.assertEqual(self.old_source.read_bytes(), b"old selected movie version")
        self.assertEqual(self.source.read_bytes(), b"new selected movie version")
        self.assertEqual(self.other.read_bytes(), b"another version")
        self.assertEqual(self.user_file.read_bytes(), b"user notes")

    def test_selected_version_replaces_with_old_physical_identity_retained(self):
        transfer = self.transfer()
        self.assertIsNone(transfer.trans_file())
        self.assertEqual(self.target.stat().st_ino, self.source.stat().st_ino)
        backups = list(self.root.glob(".bar-rollback/*/old-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_ino, self.old_source.stat().st_ino)
        self.assert_preserved()

    def test_media_replace_and_backup_rollback_do_not_read_file_content(self):
        with patch('src.rename.asset_replacement.fingerprint_file', side_effect=AssertionError('media content read')):
            transfer = self.transfer()
            self.assertIsNone(transfer.trans_file())
            transfer.recover_replacements(rollback=True)
        self.assertEqual(self.target.stat().st_ino, self.old_source.stat().st_ino)
        self.assert_preserved()


    def test_raw_action_without_typed_approval_is_rejected_before_any_write(self):
        transfer = self.transfer(authority=False)
        with self.assertRaisesRegex(ValueError, "批准|authority"):
            transfer.trans_file()
        self.assertFalse(self.records.exists())
        self.assertEqual(self.target.stat().st_ino, self.old_source.stat().st_ino)

    def test_ordinary_create_still_refuses_existing_target(self):
        transfer = Trans({self.source: self.target}, "ordinary", mode="hardlink",
                         dry_run=False, record_root=self.records)
        self.assertIsNotNone(transfer.trans_file())
        self.assertEqual(self.target.stat().st_ino, self.old_source.stat().st_ino)

    def test_new_source_drift_rejected_before_write(self):
        transfer = self.transfer()
        self.source.write_bytes(b"changed after approval")
        with self.assertRaises(ValueError):
            transfer.trans_file()
        self.assertEqual(self.target.stat().st_ino, self.old_source.stat().st_ino)
        self.assertFalse(self.records.exists())

    def test_old_target_drift_rejected_before_write(self):
        transfer = self.transfer()
        self.target.unlink()
        self.target.write_bytes(b"unknown replacement by user")
        with self.assertRaises(ValueError):
            transfer.trans_file()
        self.assertEqual(self.target.read_bytes(), b"unknown replacement by user")

    def test_completed_retry_and_explicit_rollback_are_idempotent(self):
        transfer = self.transfer()
        self.assertIsNone(transfer.trans_file())
        fresh = Trans(transfer.R, transfer.uuid, transfer.plan_fingerprint,
                      mode="hardlink", dry_run=False, record_root=self.records,
                      operations=transfer.operations,
                      replacement_authority=transfer.replacement_authority)
        self.assertIsNone(fresh.trans_file())
        fresh.recover_replacements(rollback=True)
        fresh.recover_replacements(rollback=True)
        self.assertEqual(self.target.stat().st_ino, self.old_source.stat().st_ino)
        self.assert_preserved()

    def test_process_death_after_publish_is_recovered_from_actual_identity(self):
        transfer = self.transfer()
        real_replace = os.replace

        def die_after_publish(source, target):
            real_replace(source, target)
            if Path(target) == self.target:
                raise SystemExit("simulated process death")

        with patch("src.rename.trans.os.replace", side_effect=die_after_publish):
            with self.assertRaises(SystemExit):
                transfer.trans_file()
        self.assertEqual(self.target.stat().st_ino, self.source.stat().st_ino)
        self.assertIsNone(transfer.trans_file())
        ledger = json.loads((self.records / "replacement-test.ledger.json").read_text("utf-8"))
        self.assertEqual(ledger["status"], "completed")
        self.assert_preserved()

    def test_media_publish_failure_restores_old_target(self):
        transfer = self.transfer()
        real_replace = os.replace

        def fail_publish(source, target):
            if Path(target) == self.target:
                raise OSError("publish failed")
            return real_replace(source, target)

        with patch("src.rename.trans.os.replace", side_effect=fail_publish):
            self.assertIsNotNone(transfer.trans_file())
        self.assertEqual(self.target.stat().st_ino, self.old_source.stat().st_ino)
        self.assert_preserved()

    def test_unknown_target_after_crash_is_not_overwritten_by_recovery(self):
        transfer = self.transfer()
        self.assertIsNone(transfer.trans_file())
        self.target.unlink()
        self.target.write_bytes(b"foreign")
        with self.assertRaises(ValueError):
            transfer.recover_replacements(rollback=True)
        self.assertEqual(self.target.read_bytes(), b"foreign")

    def test_restart_after_each_durable_write_boundary(self):
        for boundary in range(1, 8):
            with self.subTest(boundary=boundary):
                fixture = type(self)("runTest")
                fixture.setUp()
                try:
                    transfer = fixture.transfer()
                    real_write = Trans._atomic_json_write
                    calls = 0

                    def crash_after_write(path, payload):
                        nonlocal calls
                        real_write(path, payload)
                        calls += 1
                        if calls == boundary:
                            raise SystemExit("process terminated after durable write")

                    with patch("src.rename.trans.Trans._atomic_json_write", side_effect=crash_after_write):
                        with self.assertRaises(SystemExit):
                            transfer.trans_file()
                    self.assertIsNone(transfer.trans_file())
                    self.assertEqual(fixture.target.stat().st_ino, fixture.source.stat().st_ino)
                    fixture.assert_preserved()
                finally:
                    fixture.doCleanups()

    def test_interrupted_mixed_batch_resumes_create_reuse_and_replace(self):
        transfer = self.transfer()
        created_source = self.root / "extra.mkv"
        created_source.write_bytes(b"extra")
        created_target = self.library / "extra.mkv"
        reused_source = self.root / "reused.mkv"
        reused_source.write_bytes(b"reuse")
        reused_target = self.library / "reused.mkv"
        os.link(reused_source, reused_target)
        from src.rename.asset_collision import build_reuse_resolution
        resolution = build_reuse_resolution(reused_source, reused_target)
        mixed = [dict(source=str(created_source), target=str(created_target), action="create"),
                 dict(source=str(reused_source), target=str(reused_target), action="reuse_existing",
                      collision_resolution=resolution.to_dict()), *transfer.operations]
        transfer.operations = tuple(mixed)
        real_replace = os.replace

        def crash_after_publish(source, target):
            real_replace(source, target)
            if Path(target) == self.target:
                raise SystemExit("power loss")

        with patch("src.rename.trans.os.replace", side_effect=crash_after_publish):
            with self.assertRaises(SystemExit):
                transfer.trans_file()
        self.assertIsNone(transfer.trans_file())
        self.assertEqual(created_target.stat().st_ino, created_source.stat().st_ino)
        self.assertEqual(reused_target.read_bytes(), b"reuse")
        transfer.recover_replacements(rollback=True)
        self.assertFalse(created_target.exists())
        self.assertEqual(reused_target.read_bytes(), b"reuse")
        self.assertEqual(self.target.stat().st_ino, self.old_source.stat().st_ino)

    def test_operation_scope_and_plan_mismatch_cannot_reuse_capability(self):
        transfer = self.transfer()
        transfer.operations = tuple([{**transfer.operations[0], "target": str(self.other)}])
        with self.assertRaises(ValueError):
            transfer.trans_file()
        transfer = self.transfer()
        transfer.plan_fingerprint = "b" * 64
        with self.assertRaises(ValueError):
            transfer.trans_file()
        self.assertFalse(self.records.exists())

    def test_cross_device_and_parent_witness_tampering_are_rejected(self):
        from dataclasses import replace
        transfer = self.transfer()
        evidence = transfer.replacement_authority.evidence[0]
        for changed in (
            replace(evidence, new_source=replace(evidence.new_source, device=evidence.new_source.device + 1)),
            replace(evidence, parent_chain=evidence.parent_chain[:-1]),
        ):
            with self.subTest(evidence=changed):
                with self.assertRaises(ValueError):
                    changed.validate_current()

    def test_cross_device_explanation_is_chinese_and_has_no_side_effects(self):
        from dataclasses import replace
        transfer = self.transfer()
        evidence = transfer.replacement_authority.evidence[0]
        before = (self.source.read_bytes(), self.target.read_bytes())
        changed = replace(evidence, new_source=replace(evidence.new_source, device=evidence.new_source.device+1))
        with self.assertRaisesRegex(ValueError, '同一文件系统.*原有文件未替换'):
            changed.validate_current()
        self.assertEqual(before, (self.source.read_bytes(), self.target.read_bytes()))
        self.assertFalse(self.records.exists())

    def test_ordinary_hardlink_reuse_preserves_existing_target(self):
        from src.rename.asset_collision import build_reuse_resolution
        self.target.unlink()
        os.link(self.source, self.target)
        resolution = build_reuse_resolution(self.source, self.target)
        identity = self.target.stat().st_ino
        transfer = Trans({self.source: self.target}, "reuse-control", mode="hardlink",
                         dry_run=False, record_root=self.records,
                         operations=[dict(source=str(self.source), target=str(self.target),
                                          action="reuse_existing", collision_resolution=resolution.to_dict())])
        self.assertIsNone(transfer.trans_file())
        self.assertEqual(self.target.stat().st_ino, identity)

    def test_replacement_guard_disconnect_prevents_media_writes(self):
        transfer = self.transfer()
        with patch("src.rename.asset_replacement.ApprovedReplacementBatch.validate_operations",
                   side_effect=ValueError("authority disconnected")):
            with self.assertRaisesRegex(ValueError, "disconnected"):
                transfer.trans_file()
        self.assertFalse(self.records.exists())
        self.assertEqual(self.target.stat().st_ino, self.old_source.stat().st_ino)

    def test_real_parent_junction_after_approval_is_rejected(self):
        transfer = self.transfer()
        moved = self.root / "moved-library"
        self.library.rename(moved)
        try:
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(str(moved), str(self.library))
            else:
                self.library.symlink_to(moved, target_is_directory=True)
            try:
                with self.assertRaisesRegex(ValueError, "reparse|symlink|重解析|符号链接"):
                    transfer.trans_file()
                self.assertFalse(self.records.exists())
            finally:
                if os.name == "nt":
                    self.library.rmdir()
                else:
                    self.library.unlink()
        finally:
            moved.rename(self.library)
        self.assert_preserved()

    def test_foreign_file_appearing_after_prepare_is_preserved(self):
        transfer = self.transfer()
        real_write = Trans._atomic_json_write

        def foreign_after_prepare(path, payload):
            real_write(path, payload)
            if payload.get("entries", [{}])[0].get("status") == "prepared_link":
                self.target.unlink()
                self.target.write_bytes(b"user file appeared before publish")

        with patch("src.rename.trans.Trans._atomic_json_write", side_effect=foreign_after_prepare):
            self.assertIsNotNone(transfer.trans_file())
        self.assertEqual(self.target.read_bytes(), b"user file appeared before publish")
        self.assertEqual(next(self.root.glob(".bar-rollback/*/old-*")).read_bytes(),
                         b"old selected movie version")
        self.assert_preserved()

    def test_live_os_lock_blocks_competing_retry(self):
        from src.rename.file_lock import advisory_file_lock, FileLockBusyError
        transfer = self.transfer()
        with advisory_file_lock(self.records / ".asset-replacement.lock",
                                owner={"purpose": "asset-replacement"}, require_single_link=True):
            with self.assertRaises(FileLockBusyError):
                transfer.trans_file()
            self.assertEqual(self.target.stat().st_ino, self.old_source.stat().st_ino)
        self.assertIsNone(transfer.trans_file())

    def test_foreign_transaction_directory_is_not_adopted(self):
        transfer = self.transfer()
        directory = transfer.replacement_authority.transaction_directory
        directory.mkdir(parents=True)
        foreign = directory / "journal.json"
        foreign.write_bytes(b"user file")
        with self.assertRaises(ValueError):
            transfer.trans_file()
        self.assertEqual(foreign.read_bytes(), b"user file")
        self.assertEqual(self.target.stat().st_ino, self.old_source.stat().st_ino)

    def test_one_foreign_target_does_not_prevent_restoring_other_switched_file(self):
        api = importlib.import_module("src.rename.asset_replacement")
        transfer = self.transfer()
        source_two, target_two = self.root / "new-sub.ass", self.library / "selected.ass"
        source_two.write_bytes(b"new subtitle")
        target_two.write_bytes(b"old subtitle")
        evidence_two = api.observe_replacement(source_two, target_two, self.library)
        transfer.replacement_authority = api.ApprovedReplacementBatch(
            transfer.uuid, transfer.plan_fingerprint,
            (*transfer.replacement_authority.evidence, evidence_two),
            target_root=str(self.library), library_roots=(str(self.library),))
        transfer.operations = (*transfer.operations,
                               dict(source=str(source_two), target=str(target_two), action="replace",
                                    replacement_evidence=evidence_two.to_dict()))
        real_write = Trans._atomic_json_write
        injected = False

        def interfere_after_first_switch(path, payload):
            nonlocal injected
            real_write(path, payload)
            if (not injected and payload.get("entries", [{}])[0].get("status") == "completed"):
                injected = True
                target_two.unlink()
                target_two.write_bytes(b"foreign subtitle")

        with patch("src.rename.trans.Trans._atomic_json_write", side_effect=interfere_after_first_switch):
            self.assertIsNotNone(transfer.trans_file())
        self.assertEqual(target_two.read_bytes(), b"foreign subtitle")
        self.assertEqual(self.target.stat().st_ino, self.old_source.stat().st_ino)


if __name__ == "__main__":
    unittest.main()
