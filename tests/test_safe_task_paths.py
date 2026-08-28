import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.utils import utils


class SafeTaskPathTests(unittest.TestCase):
    def test_safe_task_path_rejects_traversal_and_unsafe_suffix(self):
        root = Path("tasks")
        with self.assertRaisesRegex(ValueError, "Unsafe task_id"):
            utils.safe_task_path(root, "../escape")
        with self.assertRaisesRegex(ValueError, "Unsafe task suffix"):
            utils.safe_task_path(root, "task", "/ledger.json")

    def test_write_and_read_task_use_exact_safe_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(utils, "TASK_PATH", root):
                utils.write_task("safe-task", {"uuid": "safe-task"})
                loaded = utils.get_task("safe-task")

            self.assertEqual(loaded["uuid"], "safe-task")
            self.assertTrue((root / "safe-task.json").exists())


if __name__ == "__main__":
    unittest.main()
