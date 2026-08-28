import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.component.local_file_picker import (
    _is_visible_entry,
    _safe_picker_root,
    _within_root,
)


class LocalFilePickerTests(unittest.TestCase):
    def test_nas_application_and_system_directories_are_always_hidden(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("@APPSTORE", "@docker", "lost+found"):
                path = root / name
                path.mkdir()
                self.assertFalse(_is_visible_entry(path, show_hidden_files=True))

    def test_dot_entries_follow_hidden_file_option(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            hidden = Path(temp_dir) / ".cache"
            hidden.mkdir()
            self.assertFalse(_is_visible_entry(hidden, show_hidden_files=False))
            self.assertTrue(_is_visible_entry(hidden, show_hidden_files=True))

    def test_linux_picker_opens_directly_at_configured_mount(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("src.component.local_file_picker.platform.system", return_value="Linux"):
                self.assertEqual(
                    _safe_picker_root("~", temp_dir),
                    Path(temp_dir).resolve(),
                )

    def test_path_cannot_escape_picker_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child = root / "Media"
            child.mkdir()
            self.assertTrue(_within_root(child, root))
            self.assertFalse(_within_root(root.parent, root))


if __name__ == "__main__":
    unittest.main()
