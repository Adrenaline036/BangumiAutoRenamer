import re
import unittest
from pathlib import Path

from src.__version__ import __version__


class ReleaseVersionTests(unittest.TestCase):
    def test_pyproject_and_runtime_use_the_same_rc_identity(self):
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)

        self.assertIsNotNone(match)
        self.assertEqual(__version__, match.group(1))
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+(?:-rc\.\d+)?$")


if __name__ == "__main__":
    unittest.main()
