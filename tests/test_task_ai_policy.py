import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.rename.process import Rename


class TaskAIPolicyTests(unittest.TestCase):
    def test_task_can_disable_ai_without_mutating_global_config(self):
        renamer = object.__new__(Rename)
        renamer._process = Mock()

        with patch(
            "src.config.config_manager.cm.set_config"
        ) as set_config:
            renamer.process(Path("episode.mkv"), use_ai=False)

        set_config.assert_not_called()
        self.assertFalse(renamer._process.call_args.kwargs["use_ai"])

    def test_task_default_keeps_configured_ai_policy(self):
        renamer = object.__new__(Rename)
        renamer._process = Mock()

        renamer.process(Path("episode.mkv"))

        self.assertIsNone(renamer._process.call_args.kwargs["use_ai"])


if __name__ == "__main__":
    unittest.main()
