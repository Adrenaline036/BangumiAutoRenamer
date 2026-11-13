"""普通任务锁继续由真实 OS 持锁，退役的借用能力不可再次注入。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from src.rename import file_lock as locks
from src.rename.source_partition import SourcePartitionStore


class PartitionLockLeaseContracts(unittest.TestCase):
    def test_current_task_lock_rejects_disconnected_acquisition(self):
        with tempfile.TemporaryDirectory() as root:
            store=SourcePartitionStore(Path(root)/'partitions',allowed_roots=(Path(root),))
            for fake in (None,object(),True):
                with self.subTest(fake=type(fake).__name__),patch.object(locks,'_acquire',return_value=fake):
                    with self.assertRaises((ValueError,RuntimeError)):
                        with store.task_lock('task'):self.fail('未持锁仍然继续')
            with store.task_lock('task'):
                self.assertTrue(locks._acquisition_states)
            self.assertFalse(locks._acquisition_states)
            self.assertFalse(locks._acquisition_proofs)

    def test_retired_scope_cannot_bypass_current_lock(self):
        with tempfile.TemporaryDirectory() as root:
            store=SourcePartitionStore(Path(root)/'partitions',allowed_roots=(Path(root),))
            self.assertFalse(hasattr(store,'resource_scope'))
            with self.assertRaises(TypeError):store.task_lock('task',partition_scope=object())
