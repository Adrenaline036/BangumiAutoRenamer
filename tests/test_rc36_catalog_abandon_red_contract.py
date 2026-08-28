"""Behaviour-level regressions for abandoning Catalog update recovery."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.pages import data_table_page
from src.rename.decision_state import DecisionStateStore
from src.rename.execution_recovery import ExecutionRecoveryError
from src.rename.plan_review import PlanReviewService
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import RenamePlan
from tests.projection_test_support import save_plan_fixture


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _visible_literals(function) -> tuple[str, ...]:
    tree = ast.parse(inspect.getsource(function))
    return tuple(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


class Rc36CatalogAbandonRedContracts(unittest.TestCase):
    def test_dashboard_callback_removes_card_and_refreshes_views(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "dashboard-abandon"
            plan_store = PlanStore(root / "plans")
            plan = RenamePlan(
                task_id=task_id,
                source_root=root / "source",
                entries=(),
            )
            plan = save_plan_fixture(plan_store, plan)
            fingerprint = plan_store.fingerprint(plan)
            _write_json(
                root / "tasks" / f"{task_id}.json",
                {
                    "uuid": task_id,
                    "workflow_status": "failed",
                    "execution_status": "catalog_update_pending",
                    "execution_requires_manual_check": True,
                },
            )
            _write_json(
                root / "records" / f"{task_id}.ledger.json",
                {
                    "schema_version": 1,
                    "task_id": task_id,
                    "plan_fingerprint": fingerprint,
                    "status": "completed",
                    "entries": [],
                },
            )
            confirm = SimpleNamespace(submit=Mock())
            recovery = SimpleNamespace(submit=Mock())
            review_refresh = Mock()
            history_refresh = Mock()
            with (
                patch.object(data_table_page, "TASK_PATH", root / "tasks"),
                patch.object(data_table_page, "RECORD_PATH", root / "records"),
                patch.object(data_table_page, "PLAN_PATH", root / "plans"),
                patch.object(data_table_page, "PLAN_REVIEW_PATH", root / "reviews"),
                patch.object(data_table_page, "DECISION_STATE_PATH", root / "decisions"),
                patch.object(
                    data_table_page,
                    "RECOVERY_ABANDON_AUDIT_PATH",
                    root / "audit",
                ),
                patch.object(data_table_page, "notify") as notify,
                patch.object(data_table_page, "refresh_dashboard") as refresh,
                patch.object(
                    data_table_page._review_tasks,
                    "refresh",
                    review_refresh,
                ),
                patch.object(
                    data_table_page._history_tasks,
                    "refresh",
                    history_refresh,
                ),
            ):
                data_table_page._abandon_catalog_update(
                    confirm, recovery, task_id, fingerprint
                )

            self.assertFalse((root / "tasks" / f"{task_id}.json").exists())
            self.assertTrue(
                (root / "records" / f"{task_id}.ledger.json").exists()
            )
            self.assertEqual(1, len(list((root / "audit").glob("*.json"))))
            confirm.submit.assert_called_once_with(True)
            recovery.submit.assert_called_once_with(True)
            notify.assert_called_once()
            refresh.assert_called_once()
            review_refresh.assert_called_once()
            history_refresh.assert_called_once()

    def test_abandon_removes_only_active_surfaces_and_is_idempotent(self):
        from src.rename import execution_recovery

        command = getattr(
            execution_recovery,
            "abandon_catalog_update_recovery",
            None,
        )
        self.assertTrue(
            callable(command),
            "缺少 catalog_update_pending 的专用安全放弃命令",
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "abandon-catalog-update"
            source = root / "media" / "source.mkv"
            target = root / "library" / "target.mkv"
            source.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            source.write_bytes(b"source-media-must-not-change")
            target.write_bytes(b"target-media-must-not-change")

            plan_store = PlanStore(root / "plans")
            plan = RenamePlan.from_mapping(
                task_id=task_id,
                source_root=source,
                mapping={source: target},
                expected_sources=(source,),
                metadata={"target_root": str(target.parent)},
            )
            decisions = DecisionStateStore(root / "decisions")
            plan = save_plan_fixture(
                plan_store,
                plan,
                state_store=decisions,
            )
            fingerprint = plan_store.fingerprint(plan)
            history_path = (
                root / "plans" / "history" / task_id / f"{fingerprint}.json"
            )
            review = PlanReviewService(
                plan_store,
                root / "reviews",
                decision_state_root=decisions.root,
                task_root=root / "tasks",
            )
            review.load_or_create(plan)

            task_path = root / "tasks" / f"{task_id}.json"
            ledger_path = root / "records" / f"{task_id}.ledger.json"
            record_path = root / "records" / f"{task_id}.json"
            _write_json(
                task_path,
                {
                    "uuid": task_id,
                    "workflow_status": "failed",
                    "execution_status": "catalog_update_pending",
                    "execution_requires_manual_check": True,
                },
            )
            _write_json(
                ledger_path,
                {
                    "schema_version": 1,
                    "task_id": task_id,
                    "plan_fingerprint": fingerprint,
                    "status": "completed",
                    "entries": [
                        {
                            "source": str(source),
                            "target": str(target),
                            "status": "created",
                            "transfer_type": "hardlink",
                        }
                    ],
                },
            )
            _write_json(record_path, {"task_id": task_id, "kind": "history"})
            work_catalog = root / "work_catalog" / "sentinel.json"
            series_catalog = root / "series_catalog" / "sentinel.json"
            _write_json(work_catalog, {"revision": 7})
            _write_json(series_catalog, {"revision": 11})

            preserved = {
                path: _hash_file(path)
                for path in (
                    source,
                    target,
                    ledger_path,
                    record_path,
                    history_path,
                    decisions.path_for(task_id),
                    work_catalog,
                    series_catalog,
                )
            }
            kwargs = {
                "task_root": root / "tasks",
                "record_root": root / "records",
                "plan_store": plan_store,
                "review_root": root / "reviews",
                "decision_state_root": root / "decisions",
                "audit_root": root / "recovery_abandon_audit",
                "expected_plan_fingerprint": fingerprint,
            }
            first = command(task_id, **kwargs)
            second = command(task_id, **kwargs)

            self.assertEqual(first, second)
            self.assertFalse(task_path.exists())
            self.assertFalse(plan_store.path_for(task_id).exists())
            self.assertFalse((root / "reviews" / f"{task_id}.json").exists())
            self.assertTrue(history_path.exists())
            for path, digest in preserved.items():
                self.assertTrue(path.exists(), f"必须保留：{path}")
                self.assertEqual(digest, _hash_file(path), f"不得改写：{path}")
            audit_files = list((root / "recovery_abandon_audit").glob("*.json"))
            self.assertEqual(1, len(audit_files))
            audit = json.loads(audit_files[0].read_text(encoding="utf-8"))
            self.assertEqual("abandon_catalog_recovery", audit["event_type"])
            self.assertEqual(task_id, audit["task_id"])
            self.assertEqual(fingerprint, audit["plan_fingerprint"])
            self.assertEqual(_hash_file(ledger_path), audit["ledger_sha256"])
            self.assertEqual(_hash_file(record_path), audit["record_sha256"])
            self.assertEqual(
                ["current_plan", "current_review", "active_task"],
                audit["cleanup_allowlist"],
            )

    def test_abandon_rejects_wrong_status_or_fingerprint_without_deletion(self):
        from src.rename import execution_recovery

        command = getattr(
            execution_recovery,
            "abandon_catalog_update_recovery",
            None,
        )
        self.assertTrue(callable(command), "缺少专用安全放弃命令")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "abandon-rejects-stale"
            plan_store = PlanStore(root / "plans")
            plan = RenamePlan(
                task_id=task_id,
                source_root=root / "source",
                entries=(),
            )
            plan = save_plan_fixture(plan_store, plan)
            fingerprint = plan_store.fingerprint(plan)
            task_path = root / "tasks" / f"{task_id}.json"
            ledger_path = root / "records" / f"{task_id}.ledger.json"
            _write_json(
                task_path,
                {
                    "uuid": task_id,
                    "workflow_status": "failed",
                    "execution_status": "completed",
                },
            )
            _write_json(
                ledger_path,
                {
                    "schema_version": 1,
                    "task_id": task_id,
                    "plan_fingerprint": fingerprint,
                    "status": "completed",
                    "entries": [],
                },
            )
            before = {
                task_path: _hash_file(task_path),
                ledger_path: _hash_file(ledger_path),
                plan_store.path_for(task_id): _hash_file(
                    plan_store.path_for(task_id)
                ),
            }
            common = {
                "task_root": root / "tasks",
                "record_root": root / "records",
                "plan_store": plan_store,
                "review_root": root / "reviews",
                "decision_state_root": root / "decisions",
                "audit_root": root / "audit",
            }
            with self.assertRaises(ExecutionRecoveryError):
                command(
                    task_id,
                    expected_plan_fingerprint=fingerprint,
                    **common,
                )
            _write_json(
                task_path,
                {
                    "uuid": task_id,
                    "workflow_status": "failed",
                    "execution_status": "catalog_update_pending",
                },
            )
            before[task_path] = _hash_file(task_path)
            with self.assertRaises(ExecutionRecoveryError):
                command(
                    task_id,
                    expected_plan_fingerprint="0" * 64,
                    **common,
                )

            for path, digest in before.items():
                self.assertTrue(path.exists())
                self.assertEqual(digest, _hash_file(path))
            self.assertFalse((root / "audit").exists())

    def test_recovery_dialog_uses_plain_language_and_offers_safe_exit(self):
        literals = (
            _visible_literals(data_table_page._open_execution_recovery)
            + _visible_literals(data_table_page._confirm_abandon_catalog_update)
            + _visible_literals(data_table_page._abandon_catalog_update)
        )
        visible = "\n".join(literals)
        lowered = visible.casefold()
        for internal_term in ("ledger", "snapshot", "binding", "plan", "l0", "l1", "l2"):
            self.assertNotIn(
                internal_term,
                lowered,
                f"恢复界面不得暴露内部术语：{internal_term}",
            )
        self.assertIn("放弃恢复", visible)
        self.assertIn("不会修改媒体文件或作品登记", visible)
        self.assertIn("操作记录仍会保留", visible)
        self.assertIn("flat", visible)


if __name__ == "__main__":
    unittest.main()
