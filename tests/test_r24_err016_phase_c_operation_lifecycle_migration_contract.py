"""Phase-C anti-bypass contracts for ERR-20260901-016.

The tests keep all task state in temporary directories.  They do not start
NiceGUI, contact a Provider, or touch media.  Their purpose is to prove that
only the typed ``awaiting_user`` outcome can expose cohort commands and that
every retry/workspace entry rejects that state before it writes anything.
"""

from __future__ import annotations
from tests.queue_fixture import TaskQueueManager as ControlledQueue

import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import src.pages.data_table_page as data_table_page
import src.pages.plan_review_page as plan_review_page
from src.pages.review_operation_view import build_review_operation_presentation
from src.rename.plan_execution import PlanExecutionError
from src.rename import review_operation as review_authority
from src.task_queue import TaskQueueManager
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
    persist_provider_selection_attempt,
)
from tests.test_r24_phase_d_f7_f8_remediation_contract import (
    build_canonical_actionable_task,
)


ROOT = Path(__file__).resolve().parents[1]


def _awaiting_task(task_id: str) -> dict[str, object]:
    task = build_canonical_actionable_task()
    task.update({
        "uuid": task_id,
        "path": "D:/SANITIZED/source",
        "error": None,
    })
    return task


class Err016PhaseCOperationAuthorityContracts(unittest.TestCase):
    def test_failed_attempt_with_complete_evidence_cannot_grant_commands(self):
        task = _awaiting_task("err016-phase-c-failed")
        failed = current_provider_selection_context(task)
        failed.update({
            "state": "failed",
            "failure_code": "provider_unavailable",
            "reason_codes": ["provider_unavailable"],
        })
        persist_provider_selection_attempt(task, failed)

        operation, _unused = review_authority.build_review_operation(task)
        self.assertEqual(review_authority.TaskWorkflowState.BLOCKED, operation.state)
        self.assertEqual((), operation.commands)
        self.assertFalse(review_authority.provider_selection_awaits_user(task))

    def test_awaiting_user_presentation_is_not_rendered_as_a_failed_choice(self):
        task = _awaiting_task("err016-phase-c-presentation")
        presentation = build_review_operation_presentation(task)

        self.assertIsNotNone(presentation)
        self.assertEqual("还需确认作品关系", presentation.heading)
        self.assertIn("已记录", presentation.reason_text)
        self.assertNotIn("未生效", presentation.heading + presentation.reason_text)
        self.assertTrue(review_authority.provider_selection_awaits_user(task))

    def test_shared_waiting_predicate_has_only_the_reviewed_entry_consumers(self):
        callers: list[str] = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text("utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute)
                    else ""
                )
                if name == "provider_selection_awaits_user":
                    callers.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(
            [
                "src/pages/data_table_page.py",
                "src/pages/data_table_page.py",
                "src/pages/plan_review_page.py",
                "src/rename/recognition_queue_admission.py",
            ],
            callers,
        )

    def test_legacy_failure_handler_has_one_worker_caller_only(self):
        callers: list[str] = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text("utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "_fail_reidentify":
                    callers.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(["src/task_queue.py"], callers)


class Err016PhaseCEntryGuards(unittest.IsolatedAsyncioTestCase):
    async def test_queue_workspace_reentry_rejects_before_persisting_changes(self):
        task = _awaiting_task("err016-phase-c-queue-workspace")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_root = root / "tasks"
            task_root.mkdir(parents=True)
            task_path = task_root / f"{task['uuid']}.json"
            task_path.write_text(json.dumps(task, ensure_ascii=False), "utf-8")
            before = task_path.read_bytes()
            manager = ControlledQueue(
                task_root=task_root,
                decision_state_root=root / "decisions",
                recovery_abandon_audit_root=root / "abandon",
                plan_root=root / "plans",
                test_mode=True,
            )

            with self.assertRaises(PlanExecutionError):
                await manager.reenqueue_workspace(
                    str(task["uuid"]),
                    Path(str(task["path"])),
                    is_anime=False,
                    is_movie=False,
                    custom_name="must-not-persist",
                    custom_season_id=9,
                    preview_only=True,
                )
            after = task_path.read_bytes()

        self.assertEqual(before, after)

    async def test_ui_workspace_reentry_rejects_before_local_or_queue_write(self):
        task = _awaiting_task("err016-phase-c-ui-workspace")
        before = copy.deepcopy(task)
        queue = SimpleNamespace(reenqueue_workspace=AsyncMock())
        save = Mock()

        with (
            patch("src.pages.plan_review_page.task_queue", queue),
            patch("src.pages.plan_review_page._save_task_record", save),
            patch("src.pages.plan_review_page.notify"),
        ):
            accepted = await plan_review_page._reenqueue_from_workspace(
                str(task["uuid"]),
                task,
                path_value=task["path"],
                media_type="tv",
                name_value="must-not-persist",
                season_value=9,
            )

        self.assertFalse(accepted)
        self.assertEqual(before, task)
        save.assert_not_called()
        queue.reenqueue_workspace.assert_not_awaited()

    async def test_dashboard_retry_uses_the_same_fail_closed_predicate(self):
        task = _awaiting_task("err016-phase-c-dashboard")
        queue = SimpleNamespace(retry_current=AsyncMock())
        with (
            patch("src.pages.data_table_page.get_task", return_value=task),
            patch("src.pages.data_table_page.task_queue", queue),
            patch("src.pages.data_table_page.notify"),
        ):
            await data_table_page._retry_task(str(task["uuid"]))
        queue.retry_current.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
