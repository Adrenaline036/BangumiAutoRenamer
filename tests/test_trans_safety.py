import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.rename.trans import Trans
from src.rename.asset_collision import build_reuse_resolution


class TransSafetyTests(unittest.TestCase):
    def test_approved_identical_target_is_reused_without_creating_hardlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "new-source.7z"
            target = root / "library" / "fonts.7z"
            target.parent.mkdir()
            source.write_bytes(b"identical-fonts")
            target.write_bytes(b"identical-fonts")
            resolution = build_reuse_resolution(source, target)
            self.assertIsNotNone(resolution)
            transfer = Trans(
                {source: target},
                "reuse-identical",
                "f" * 64,
                mode="hardlink",
                dry_run=False,
                record_root=root / "records",
                operations=({
                    "source": source,
                    "target": target,
                    "action": "reuse_existing",
                    "collision_resolution": resolution.to_dict(),
                },),
            )

            self.assertIsNone(transfer.trans_file())
            self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)
            ledger = json.loads(
                (root / "records" / "reuse-identical.ledger.json").read_text("utf-8")
            )
            self.assertEqual("completed", ledger["status"])
            self.assertEqual("reused_identical_asset", ledger["entries"][0]["status"])
            self.assertEqual("reuse_existing", ledger["entries"][0]["transfer_type"])
    def test_hardlink_cross_filesystem_fails_in_preflight_with_clear_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mkv"
            target = root / "library" / "target.mkv"
            source.write_bytes(b"video")
            trans = Trans({source: target}, "cross-device")
            trans.mode = "链接"
            ledger = {"entries": [{"status": "planned", "error": None}]}

            with patch.object(
                trans,
                "_existing_parent_device",
                return_value=source.stat().st_dev + 1,
            ):
                message = trans._preflight(ledger)

            self.assertIn("不在同一文件系统", message)
            self.assertIn("无法创建硬链接", message)
            self.assertFalse(target.exists())

    def _read_ledger(self, record_dir: Path, task_id: str) -> dict:
        return json.loads(
            (record_dir / f"{task_id}.ledger.json").read_text(
                encoding="utf-8"
            )
        )

    @patch("src.rename.trans.cm.get_config", return_value="链接")
    def test_hardlink_failure_never_falls_back_to_symlink(self, _config):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mkv"
            target = root / "output" / "target.mkv"
            records = root / "records"
            source.write_bytes(b"media")
            records.mkdir()

            with (
                patch("src.rename.trans.RECORD_PATH", records),
                patch("src.rename.trans.os.link", side_effect=OSError("denied")),
                patch("src.rename.trans.os.symlink") as symlink,
            ):
                result = Trans({source: target}, "hardlink-fail").trans_file()

            self.assertIn("denied", result)
            symlink.assert_not_called()
            self.assertFalse(target.exists())
            ledger = self._read_ledger(records, "hardlink-fail")
            self.assertEqual(ledger["status"], "failed")
            self.assertEqual(ledger["entries"][0]["status"], "failed")

    @patch("src.rename.trans.cm.get_config", return_value="链接")
    def test_fake_hardlink_is_rejected_by_inode_verification(self, _config):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mkv"
            target = root / "output" / "target.mkv"
            records = root / "records"
            source.write_bytes(b"media")
            records.mkdir()

            def fake_link(src, dst):
                Path(dst).write_bytes(Path(src).read_bytes())

            with (
                patch("src.rename.trans.RECORD_PATH", records),
                patch("src.rename.trans.os.link", side_effect=fake_link),
            ):
                result = Trans({source: target}, "inode-fail").trans_file()

            self.assertIn("inode", result.casefold())
            self.assertFalse(target.exists())

    @patch("src.rename.trans.cm.get_config", return_value="链接")
    def test_partial_batch_is_rolled_back_when_later_item_fails(self, _config):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.mkv"
            second = root / "second.mkv"
            first_target = root / "output" / "first.mkv"
            second_target = root / "output" / "second.mkv"
            records = root / "records"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            records.mkdir()
            real_link = os.link
            calls = 0

            def fail_second(src, dst):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("second failed")
                real_link(src, dst)

            with (
                patch("src.rename.trans.RECORD_PATH", records),
                patch("src.rename.trans.os.link", side_effect=fail_second),
            ):
                result = Trans(
                    {first: first_target, second: second_target},
                    "rollback",
                ).trans_file()

            self.assertIn("second failed", result)
            self.assertFalse(first_target.exists())
            self.assertFalse(second_target.exists())
            self.assertTrue(first.exists())
            ledger = self._read_ledger(records, "rollback")
            self.assertEqual(ledger["status"], "rolled_back")
            self.assertEqual(ledger["entries"][0]["status"], "rolled_back")

    @patch("src.rename.trans.cm.get_config", return_value="链接")
    def test_existing_target_is_never_overwritten(self, _config):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mkv"
            target = root / "target.mkv"
            records = root / "records"
            source.write_bytes(b"source")
            target.write_bytes(b"keep")
            records.mkdir()

            with patch("src.rename.trans.RECORD_PATH", records):
                result = Trans({source: target}, "existing").trans_file()

            self.assertIn("已存在", result)
            self.assertEqual(target.read_bytes(), b"keep")

    @patch("src.rename.trans.cm.get_config", return_value="复制")
    def test_partial_current_copy_is_removed_when_copy_raises(self, _config):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mkv"
            target = root / "output" / "target.mkv"
            records = root / "records"
            source.write_bytes(b"complete source")
            records.mkdir()

            def partial_copy(_source, destination):
                Path(destination).write_bytes(b"partial")
                raise OSError("copy interrupted")

            with (
                patch("src.rename.trans.RECORD_PATH", records),
                patch(
                    "src.rename.trans.shutil.copy2", side_effect=partial_copy
                ),
            ):
                result = Trans({source: target}, "partial-copy").trans_file()

            self.assertIn("copy interrupted", result)
            # Ownership cannot be proven when the transfer primitive raises.
            # Preserve the partial/foreign path for manual reconciliation
            # instead of risking deletion of another executor's target.
            self.assertTrue(target.exists())
            self.assertTrue(source.exists())

    @patch("src.rename.trans.cm.get_config", return_value="链接")
    def test_failed_link_does_not_delete_foreign_target(self, _config):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mkv"
            target = root / "output" / "target.mkv"
            records = root / "records"
            source.write_bytes(b"source")
            records.mkdir()

            def foreign_race(_source, destination):
                Path(destination).write_bytes(b"foreign")
                raise FileExistsError("foreign target won")

            with (
                patch("src.rename.trans.RECORD_PATH", records),
                patch("src.rename.trans.os.link", side_effect=foreign_race),
            ):
                result = Trans({source: target}, "foreign-race").trans_file()

            self.assertIn("foreign target won", result)
            self.assertEqual(b"foreign", target.read_bytes())
            self.assertEqual(b"source", source.read_bytes())

    @patch("src.rename.trans.cm.get_config", return_value="链接")
    def test_directory_source_is_rejected_in_preflight(self, _config):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source-directory"
            target = root / "output" / "target.mkv"
            records = root / "records"
            source.mkdir()
            records.mkdir()

            with patch("src.rename.trans.RECORD_PATH", records):
                result = Trans({source: target}, "directory-source").trans_file()

            self.assertIn("不是普通文件", result)
            self.assertFalse(target.exists())
            ledger = self._read_ledger(records, "directory-source")
            self.assertEqual(ledger["status"], "failed")
            self.assertEqual(ledger["entries"][0]["status"], "failed")

    @patch("src.rename.trans.cm.get_config", return_value="链接")
    def test_empty_mapping_is_rejected(self, _config):
        with tempfile.TemporaryDirectory() as temp_dir:
            records = Path(temp_dir) / "records"
            records.mkdir()

            with patch("src.rename.trans.RECORD_PATH", records):
                result = Trans({}, "empty").trans_file()

            self.assertIn("映射为空", result)
            ledger = self._read_ledger(records, "empty")
            self.assertEqual(ledger["status"], "failed")

    @patch("src.rename.trans.cm.get_config", return_value="链接")
    def test_unsafe_task_id_cannot_escape_record_root(self, _config):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            records = root / "records"
            source = root / "source.mkv"
            target = root / "target.mkv"
            records.mkdir()
            source.write_bytes(b"media")

            with patch("src.rename.trans.RECORD_PATH", records):
                with self.assertRaisesRegex(ValueError, "Unsafe task_id"):
                    Trans({source: target}, "../escape").trans_file()

            self.assertFalse((root / "escape.json").exists())
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
