"""R24 Phase-A RED contracts for the versioned review-operation boundary.

The incident inputs preserve only sanitized state shape.  They exercise the
current queue persistence and mounted NiceGUI projection without contacting a
Provider or reading real task/media state.  Phase B promotes every incident
assertion to an ordinary regression contract.
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from src.pages.review_operation_view import (
    ReviewOperationCallbacks,
    build_review_operation_presentation,
    dispatch_review_command_once,
    render_review_operation,
)
from src.task_queue import TaskQueueManager
import tests.test_provider_subject_plan_wiring_red_contract as wiring_fixture
from tests.test_r24_phase_d_f7_f8_remediation_contract import (
    build_canonical_actionable_task,
)
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
    persist_provider_selection_attempt,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _capture_operation_ui(task: dict):
    callbacks = ReviewOperationCallbacks(
        show_provider_candidates=Mock(),
        close_review=Mock(),
        remove_provider_failure_task=Mock(),
        select_provider_candidate=Mock(),
        dispatch_review_command=Mock(),
    )
    captured = wiring_fixture.ui_fixture._CaptureUi()
    captured.card = captured.column
    render_review_operation(captured, task, callbacks)
    return callbacks, captured


def _business_actions(captured) -> set[str]:
    return {
        label for label in captured.callbacks
        if label in {"确认与相邻作品一起整理", "分开整理"}
    }


def _yama_date_locked_task() -> dict:
    return build_canonical_actionable_task()


class IncidentStateSequenceRedContracts(unittest.TestCase):
    def test_p1c_date_locked_reason_cannot_remove_actions_from_same_fingerprint_preview(self):
        task = _yama_date_locked_task()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.json"
            _write_json(path, task)
            restarted = json.loads(path.read_text("utf-8"))
        _dialog, date_locked = _capture_operation_ui(restarted)
        reference = copy.deepcopy(restarted)
        attempt = current_provider_selection_context(reference)
        attempt["reason_codes"] = [
            "subject_candidate_evidence_missing"
        ]
        persist_provider_selection_attempt(reference, attempt)
        _dialog, missing_candidate = _capture_operation_ui(reference)
        self.assertEqual(
            _business_actions(missing_candidate),
            _business_actions(date_locked),
            "reason code仍在决定业务权限，而不是只负责说明",
        )
        self.assertEqual(
            {"确认与相邻作品一起整理", "分开整理"},
            _business_actions(date_locked),
        )

    def test_double_ui_dispatch_uses_one_revision_cas_and_one_idempotency_key(self):
        task = _yama_date_locked_task()
        attempt = current_provider_selection_context(task)
        attempt["reason_codes"] = [
            "subject_candidate_evidence_missing"
        ]
        persist_provider_selection_attempt(task, attempt)
        presentation = build_review_operation_presentation(task)
        self.assertIsNotNone(presentation)
        command = next(
            action.payload for action in presentation.actions
            if action.key == "command-confirm_cohort"
        )
        apply_boundary = Mock(return_value="applied")
        dispatched: set[str] = set()
        dispatch_review_command_once(task, command, dispatched, apply_boundary)
        dispatch_review_command_once(task, command, dispatched, apply_boundary)
        self.assertEqual(
            1,
            apply_boundary.call_count,
            "双击直接重复调用页面私有callback，未经过command CAS/idempotency门",
        )


class TypedBoundaryAndMutationRedContracts(unittest.TestCase):
    def test_review_operation_is_a_versioned_typed_union_with_frozen_command_payload(self):
        source_root = Path(__file__).resolve().parents[1] / "src"
        class_names: set[str] = set()
        for path in source_root.rglob("*.py"):
            tree = ast.parse(path.read_text("utf-8"), filename=str(path))
            class_names.update(
                node.name for node in tree.body if isinstance(node, ast.ClassDef)
            )
        self.assertTrue(
            {"TaskWorkflowState", "ReviewOperation", "PreviewModel", "ReviewCommand"}
            .issubset(class_names),
            "typed ReviewOperation/command boundary尚未建立",
        )

    def test_len_one_and_reason_gating_mutations_are_absent_from_business_routing(self):
        queue_source = inspect.getsource(TaskQueueManager._fail_reidentify)
        ui_source = inspect.getsource(build_review_operation_presentation)
        self.assertNotIn("len(boundary_edges) == 1", queue_source)
        self.assertNotIn("len(boundary_edges) == 1", ui_source)
        self.assertNotIn(
            '"subject_candidate_evidence_missing" in reason_codes',
            ui_source,
        )

    def test_mounted_ui_dispatches_persisted_command_instead_of_private_builder_bypass(self):
        source = inspect.getsource(render_review_operation)
        self.assertNotIn("_apply_provider_subject_boundary_inline", source)
        self.assertIn("review_command", source)

    def test_page_and_queue_have_no_raw_review_operation_writer(self):
        repository = Path(__file__).resolve().parents[1]
        page_tree = ast.parse(
            (repository / "src" / "pages" / "plan_review_page.py").read_text("utf-8")
        )
        queue_tree = ast.parse(
            (repository / "src" / "task_queue.py").read_text("utf-8")
        )
        page_mutation_calls = [
            node for node in ast.walk(page_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"mutate_task_at", "write_task_at", "write_task"}
        ]
        queue_raw_writes = [
            node for node in ast.walk(queue_tree)
            if isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in {"selection_operation", "review_operation"}
        ]
        self.assertEqual([], page_mutation_calls, "UI仍直接写task JSON")
        self.assertEqual([], queue_raw_writes, "queue仍直接拼写raw operation dict")

    def test_six_mutation_obligations_are_named_for_ds_challenge(self):
        self.assertEqual(
            (
                "len_one_gating",
                "reason_permission_gating",
                "canonical_builder_bypass",
                "restart_payload_loss",
                "ui_direct_task_json_write",
                "page_queue_raw_dict_writer",
            ),
            (
                "len_one_gating",
                "reason_permission_gating",
                "canonical_builder_bypass",
                "restart_payload_loss",
                "ui_direct_task_json_write",
                "page_queue_raw_dict_writer",
            ),
        )


class FailClosedControls(unittest.TestCase):
    def test_ambiguous_multi_edge_has_safe_exit_but_no_advance_command(self):
        task = _yama_date_locked_task()
        attempt = current_provider_selection_context(task)
        attempt["reason_codes"] = [
            "subject_candidate_evidence_missing"
        ]
        edge = attempt["provider_subject_evidence"][
            "provider_subject_boundary_edges"
        ][0]
        attempt["provider_subject_evidence"][
            "provider_subject_boundary_edges"
        ] = [edge, {**edge, "to_subject_id": "another"}]
        persist_provider_selection_attempt(task, attempt)
        _dialog, captured = _capture_operation_ui(task)
        self.assertEqual(set(), _business_actions(captured))
        self.assertTrue({"换一个作品", "移除任务"}.issubset(captured.callbacks))

    def test_unknown_reason_stays_blocked_with_safe_exit(self):
        task = _yama_date_locked_task()
        attempt = current_provider_selection_context(task)
        attempt.pop("provider_subject_cohort_preview", None)
        attempt["provider_subject_evidence"] = {}
        attempt["reason_codes"] = ["unknown-r24-reason"]
        persist_provider_selection_attempt(task, attempt)
        _dialog, captured = _capture_operation_ui(task)
        self.assertEqual(set(), _business_actions(captured))
        self.assertTrue({"换一个作品", "移除任务", "关闭"}.issubset(captured.callbacks))


if __name__ == "__main__":
    unittest.main()
