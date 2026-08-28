import json
import os
import tempfile
import unittest
from pathlib import Path

from src.rename.file_lock import (
    FileLockBusyError,
    advisory_file_lock,
    read_lock_metadata,
)


class AdvisoryFileLockTests(unittest.TestCase):
    def test_legacy_exclusive_create_residue_does_not_block(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.lock"
            path.write_text("old-process-token", encoding="ascii")
            with advisory_file_lock(path, owner={"kind": "test"}) as owner:
                self.assertEqual(os.getpid(), owner["pid"])
            metadata = read_lock_metadata(path)
            self.assertEqual("released", metadata["status"])
            self.assertEqual("test", metadata["kind"])
            self.assertIn("released_at", metadata)

    def test_live_owner_cannot_be_stolen(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "active.lock"
            with advisory_file_lock(path):
                with self.assertRaises(FileLockBusyError):
                    with advisory_file_lock(path):
                        pass

    def test_released_lock_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "released.lock"
            with advisory_file_lock(path) as first:
                first_token = first["lock_token"]
            with advisory_file_lock(path) as second:
                self.assertNotEqual(first_token, second["lock_token"])

    def test_metadata_contains_pid_timestamp_and_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "metadata.lock"
            with advisory_file_lock(path, owner={"task_id": "task-one"}) as owner:
                self.assertEqual(os.getpid(), owner["pid"])
            raw = path.read_bytes()
            payload = json.loads(raw[1:].decode("utf-8"))
            self.assertEqual(os.getpid(), payload["pid"])
            self.assertEqual("task-one", payload["task_id"])
            self.assertTrue(payload["acquired_at"])
            self.assertEqual(32, len(payload["lock_token"]))


if __name__ == "__main__":
    unittest.main()
