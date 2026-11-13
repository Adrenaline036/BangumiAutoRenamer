"""共享等待派发覆盖及取消后的锁释放。"""
import ast
from pathlib import Path
import tempfile
import unittest

from src.rename import file_lock


class WaitRoutes(unittest.TestCase):
    def test_pages_cannot_bypass_managed_disk_dispatch(self):
        source = Path(file_lock.__file__).parents[1]
        direct = []
        for path in (source/'pages').glob('*.py'):
            tree = ast.parse(path.read_text('utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = ast.unparse(node.func)
                if name == 'asyncio.to_thread' or name == 'run.io_bound' and not (
                        path.name == 'add_task_page.py' and ast.unparse(node.args[0]) == 'discover_sources'):
                    direct.append(f'{path.name}:{node.lineno}')
        self.assertEqual([], direct, '共享登记后台操作须登记到停止边界')

    def test_wait_cancel_is_not_external_lock_retry(self):
        self.assertFalse(issubclass(file_lock.LockWaitCancelledError, file_lock.FileLockBusyError))
        self.assertFalse(file_lock.is_lock_busy(file_lock.LockWaitCancelledError('cancel')))
