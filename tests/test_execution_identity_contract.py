"""执行目标必须匹配冻结身份；使用合成硬链接核对。"""
import os,sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.test_r23_d_f_projection_execution_recovery_red_contract import _plan, _service, _envelope
from src.rename.plan_execution import PlanExecutionService, PlanExecutionError

class ExecutionIdentityContracts(unittest.TestCase):
    def check(self, mode, reject):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft, source = _plan(root, 'identity-probe')
            service, _, _ = _service(root)
            _, plan = service.commit(_envelope(draft))
            original = source.stat()
            if mode == 'replace':
                alternate = source.with_suffix('.incoming')
                alternate.write_bytes(b'X' * original.st_size)
                os.replace(alternate, source)
                self.assertNotEqual(original.st_ino, source.stat().st_ino)
            elif mode == 'size':
                source.write_bytes(b'X' * (original.st_size + 1))
            elif mode == 'mtime':
                os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns + 1000000000))
            elif mode == 'same_size_content':
                source.write_bytes(b'X' * original.st_size)
                os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
            elif mode == 'missing':
                source.rename(source.with_suffix(".held"))
            real_open = Path.open
            def no_media_open(path, *args, **kwargs):
                if path == source:
                    frame=sys._getframe(1)
                    while frame:
                        name=frame.f_code.co_filename.replace('\\','/')
                        if name.endswith('/runner.py'):break
                        if '/runtime/src/' in name:raise AssertionError('执行不应读取媒体内容')
                        frame=frame.f_back
                return real_open(path, *args, **kwargs)
            from src.rename.trans import Trans
            from src.rename.media_file_identity import frozen_sources
            with patch.object(Path, 'open', no_media_open):
                PlanExecutionService._validate_execution_paths(plan)
                transfer = Trans(plan.as_mapping(),plan.task_id,'identity-fp',mode='hardlink',
                                 dry_run=False,record_root=root/'records',source_identities=frozen_sources(plan))
                error = transfer.trans_file()
            if reject:
                self.assertIsNotNone(error)
                self.assertFalse(any(entry.target_path.exists() for entry in plan.entries))
            else:
                self.assertIsNone(error)
                self.assertTrue(all(entry.target_path.exists() for entry in plan.entries))
    def test_physical_replacement_rejected(self): self.check('replace', True)
    def test_size_change_rejected(self): self.check('size', True)
    def test_missing_rejected(self): self.check('missing', True)
    def test_unchanged_accepted(self): self.check('unchanged', False)
    def test_mtime_only_accepted(self): self.check('mtime', False)
    def test_no_same_size_content_audit(self): self.check('same_size_content', False)

if __name__ == '__main__':
    unittest.main(verbosity=2)
