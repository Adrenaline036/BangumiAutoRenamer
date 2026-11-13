"""每个菜单合同在独立进程和数据根中运行，避免全量测试共享状态。"""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class MenuDialogLifetimeTests(unittest.TestCase):
    def run_probe(self, method):
        with tempfile.TemporaryDirectory() as raw:
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8',
                str(Path(__file__).with_name('menu_dialog_probe.py')), method, raw],
                capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_recovery_menu_dialog_survives_menu_close(self):
        self.run_probe(self._testMethodName)

    def test_library_menu_dialog_survives_menu_close(self):
        self.run_probe(self._testMethodName)



if __name__ == '__main__':
    unittest.main()
