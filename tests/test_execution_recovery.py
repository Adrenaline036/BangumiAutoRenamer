import json
import tempfile
import unittest
from pathlib import Path

from src.rename.execution_recovery import (
    ExecutionRecoveryError,
    acknowledge_execution_recovery,
    load_execution_recovery,
)


class ExecutionRecoveryTests(unittest.TestCase):
    def test_public_docstrings_distinguish_record_and_task_roots(self):
        self.assertIn("record_root", load_execution_recovery.__doc__ or "")
        self.assertIn("RECORD_PATH", load_execution_recovery.__doc__ or "")
        self.assertIn("task_root", acknowledge_execution_recovery.__doc__ or "")
        self.assertIn("TASK_PATH", acknowledge_execution_recovery.__doc__ or "")

    def test_snapshot_reads_only_exact_task_ledger_and_summarizes_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_id = "recovery-task"
            (root / f"{task_id}.ledger.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "task_id": task_id,
                        "plan_fingerprint": "a" * 64,
                        "status": "rolled_back",
                        "entries": [
                            {"status": "rolled_back"},
                            {"status": "failed"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = load_execution_recovery(task_id, root)

            self.assertTrue(snapshot.ledger_present)
            self.assertEqual(snapshot.ledger_status, "rolled_back")
            self.assertEqual(snapshot.entry_count, 2)
            self.assertEqual(
                dict(snapshot.entry_statuses),
                {"failed": 1, "rolled_back": 1},
            )

    def test_missing_and_corrupt_ledgers_remain_visible_as_untrusted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing = load_execution_recovery("missing", root)
            self.assertEqual(missing.ledger_status, "missing")
            self.assertIn("不存在", missing.error)

            (root / "broken.ledger.json").write_text("{", encoding="utf-8")
            broken = load_execution_recovery("broken", root)
            self.assertEqual(broken.ledger_status, "untrusted")
            self.assertIn("无法信任", broken.error)

    def test_acknowledgement_clears_gate_but_preserves_write_risk_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_id = "ack-task"
            path = root / f"{task_id}.json"
            path.write_text(
                json.dumps(
                    {
                        "uuid": task_id,
                        "workflow_status": "failed",
                        "queue_operation": "execute_approved",
                        "execution_requested_fingerprint": "a" * 64,
                        "execution_phase": "media_boundary_entered",
                        "execution_requires_manual_check": True,
                        "execution_may_have_written_targets": True,
                    }
                ),
                encoding="utf-8",
            )

            updated = acknowledge_execution_recovery(task_id, root)

            self.assertFalse(updated["execution_requires_manual_check"])
            self.assertTrue(updated["execution_may_have_written_targets"])
            self.assertTrue(updated["execution_manual_check_acknowledged_at"])
            self.assertEqual(
                "reapproval_required",
                updated["execution_recovery_disposition"],
            )
            stored = json.loads(path.read_text("utf-8"))
            self.assertTrue(stored["execution_manual_check_acknowledged_at"])
            self.assertIn(
                "已完成目标文件核查",
                stored["execution_manual_check_acknowledgement"],
            )

    def test_acknowledgement_rejects_unsafe_or_unneeded_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(ExecutionRecoveryError):
                acknowledge_execution_recovery("../escape", root)

            (root / "plain.json").write_text(
                json.dumps({"uuid": "plain"}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ExecutionRecoveryError, "可核查"):
                acknowledge_execution_recovery("plain", root)


if __name__ == "__main__":
    unittest.main()
