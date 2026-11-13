from __future__ import annotations

import ast
import inspect
from pathlib import Path
import unittest
from unittest.mock import patch

from src.rename import review_operation as review_authority


ROOT = Path(__file__).resolve().parents[1]


def _pending_attempt() -> dict:
    return {
        "schema_version": 1,
        "request_id": "phase-c-request",
        "state": "pending",
        "candidate": {
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": "83121",
            "title": "ordinary control",
        },
        "expected_decision_revision": 7,
        "result_decision_revision": None,
        "failure_code": None,
        "reason_codes": [],
        "retryable": True,
        "requested_at": "2026-08-29T00:00:00+00:00",
        "finished_at": None,
        "previous_workflow_status": "need_confirm",
    }


def current_provider_selection_context(task: dict) -> dict:
    """Test-side projection of the persisted typed authority."""

    operation, _unused = review_authority.build_review_operation(task)
    return dict(operation.context)


def persist_provider_selection_attempt(task: dict, attempt: dict) -> dict:
    """Test fixture builder that follows the current production writer."""

    return review_authority.set_review_operation(
        task,
        review_authority.build_provider_selection_review_operation(task, attempt),
    )


class R24PhaseCReviewOperationMigrationContract(unittest.TestCase):
    def test_current_schema_is_self_validating_v2(self):
        self.assertEqual(2, review_authority.REVIEW_OPERATION_SCHEMA_VERSION)
        builder = getattr(
            review_authority, "build_provider_selection_review_operation", None
        )
        self.assertTrue(callable(builder), "current typed producer is missing")
        if not callable(builder):
            return
        operation = builder({}, _pending_attempt())
        persisted = operation.to_dict()
        parsed = review_authority.ReviewOperation.from_mapping(persisted)
        self.assertEqual(operation, parsed)
        persisted["fingerprint"] = "0" * 64
        with self.assertRaises(ValueError):
            review_authority.ReviewOperation.from_mapping(persisted)

    def test_current_setter_rejects_legacy_mapping_and_removes_old_key(self):
        builder = getattr(
            review_authority, "build_provider_selection_review_operation", None
        )
        self.assertTrue(callable(builder), "current typed producer is missing")
        if not callable(builder):
            return
        attempt = _pending_attempt()
        task = {"uuid": "phase-c-task", "selection_operation": dict(attempt)}
        with self.assertRaises(TypeError):
            review_authority.set_review_operation(task, attempt)
        review_authority.set_review_operation(task, builder(task, attempt))
        self.assertIn("review_operation", task)
        self.assertNotIn("selection_operation", task)

    def test_legacy_only_record_is_stale_blocked_not_actionable(self):
        task = {"uuid": "legacy-task", "selection_operation": _pending_attempt()}
        operation, _ = review_authority.build_review_operation(task)
        self.assertEqual(
            review_authority.TaskWorkflowState.BLOCKED, operation.state
        )
        self.assertEqual((), operation.commands)
        self.assertIn("legacy_review_operation_stale", operation.blocking_reasons)

    def test_selection_operation_is_only_read_by_canonical_stale_adapter(self):
        offenders: list[str] = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            if path.name == "review_operation.py":
                continue
            tree = ast.parse(path.read_text("utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "selection_operation":
                    offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
        self.assertEqual([], offenders)

    def test_canonical_module_has_no_current_legacy_writer(self):
        source = inspect.getsource(review_authority.set_review_operation)
        self.assertNotIn('task.update({"selection_operation"', source)
        self.assertNotIn('task["selection_operation"]', source)

    def test_disconnect_of_current_accessor_fails_closed(self):
        builder = getattr(
            review_authority, "build_provider_selection_review_operation", None
        )
        self.assertTrue(callable(builder), "current typed producer is missing")
        if not callable(builder):
            return
        task = {"uuid": "phase-c-task"}
        review_authority.set_review_operation(task, builder(task, _pending_attempt()))
        with patch.object(
            review_authority.ReviewOperation,
            "from_mapping",
            side_effect=ValueError("canonical accessor disconnected"),
        ):
            operation, _ = review_authority.build_review_operation(task)
        self.assertEqual(review_authority.TaskWorkflowState.BLOCKED, operation.state)
        self.assertEqual((), operation.commands)
        self.assertIn("review_operation_invalid", operation.blocking_reasons)

    def test_plan_store_still_has_one_production_save_primitive(self):
        calls: list[str] = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text("utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "save"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "plan_store"
                ):
                    calls.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(["src/rename/projection_authority.py"], calls)


if __name__ == "__main__":
    unittest.main()
