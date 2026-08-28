import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.rename.trans import Trans


class TransDryRunTests(unittest.TestCase):
    @patch('src.rename.trans.cm.get_config', return_value='链接')
    def test_dry_run_records_mapping_without_creating_target(
        self,
        _mocked_config,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source.mkv'
            target = root / 'output' / 'target.mkv'
            record_dir = root / 'records'
            source.write_bytes(b'test-media')
            record_dir.mkdir()

            with (
                patch.dict('os.environ', {'BAR_DRY_RUN': 'true'}),
                patch('src.rename.trans.RECORD_PATH', record_dir),
            ):
                result = Trans({source: target}, 'dry-run-task').trans_file()

            self.assertIsNone(result)
            self.assertFalse(target.exists())
            self.assertFalse(target.parent.exists())
            record = json.loads(
                (record_dir / 'dry-run-task.json').read_text(encoding='utf-8')
            )
            self.assertEqual(str(target), record[str(source)])

    @patch('src.rename.trans.cm.get_config', return_value='链接')
    def test_false_dry_run_value_keeps_normal_transfer(
        self,
        _mocked_config,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source.mkv'
            target = root / 'output' / 'target.mkv'
            record_dir = root / 'records'
            source.write_bytes(b'test-media')
            record_dir.mkdir()

            with (
                patch.dict('os.environ', {'BAR_DRY_RUN': 'false'}),
                patch('src.rename.trans.RECORD_PATH', record_dir),
            ):
                result = Trans({source: target}, 'normal-task').trans_file()

            self.assertIsNone(result)
            self.assertTrue(target.exists())
            self.assertEqual(source.stat().st_ino, target.stat().st_ino)
