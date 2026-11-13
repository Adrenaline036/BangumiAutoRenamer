"""Incident-shaped GREEN contracts for ERR-20260901-016.

The positive reproduction keeps AddTask, review callback, queue worker,
``Rename._process`` and persistence on production code.  Only Provider
transport and media bytes are sanitized local fixtures.  All incident
assertions are ordinary tests; temporary RED wrappers are forbidden.
"""

from __future__ import annotations
from tests.queue_fixture import TaskQueueManager as ControlledQueue

import ast
import json
import inspect
from pathlib import Path
from types import SimpleNamespace
import tempfile
import textwrap
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import src.pages.data_table_page as data_table_page
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.plan_execution import PlanExecutionError
from src.task_queue import TaskQueueManager
import tests.test_provider_subject_plan_wiring_red_contract as production_fixture
import tests.test_r24_phase_c_review_operation_migration_contract as operation_fixture
import tests.test_r24_phase_d_f7_f8_remediation_contract as actionable_fixture


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "r24_err016_operation_lifecycle_incident.json"
)
def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _incident() -> dict[str, object]:
    return dict(_fixture()["incident"])


def _tmdb_anchor(case: dict[str, object]) -> dict[str, object]:
    anchor = dict(case["tmdb_anchor"])
    return {
        "id": int(anchor["provider_id"]),
        "name": str(anchor["title"]),
        "original_name": str(anchor["original_title"]),
        "first_air_date": str(anchor["first_air_date"]),
        "genres": [{"name": "Animation"}],
        "seasons": [
            {
                "season_number": int(row["season_number"]),
                "name": f"Season {int(row['season_number'])}",
                "air_date": str(row["air_date"]),
                "episode_count": int(row["episode_count"]),
            }
            for row in anchor["seasons"]
        ],
    }


def _actionable_task(task_id: str = "sanitized-err016-awaiting-user") -> dict:
    task = actionable_fixture.build_canonical_actionable_task()
    task["uuid"] = task_id
    task["error"] = None
    return task


class Err016IncidentFixtureContracts(unittest.TestCase):
    def test_fixture_is_sanitized_and_preserves_the_operation_shape(self):
        payload = _fixture()
        case = dict(payload["incident"])
        expected = dict(case["expected_waiting_outcome"])
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        self.assertEqual(
            "r24-err016-operation-lifecycle-incident/v1",
            payload["schema_version"],
        )
        self.assertEqual(
            ["pending", "awaiting_user", "succeeded"],
            list(dict(case["expected_lifecycle"]).values()),
        )
        self.assertEqual([0, 0, 1], case["expected_decision_revisions"])
        self.assertEqual(13, len(case["regular_episode_numbers"]))
        self.assertFalse(expected["task_error_present"])
        self.assertEqual(0, expected["queue_exception_count"])
        self.assertIn("\\", str(case["source_path_shape"]))
        self.assertIn("[", str(case["source_path_shape"]))
        self.assertIn("]", str(case["source_path_shape"]))

        for forbidden in (
            "d:\\\\",
            "c:\\\\",
            "api_key",
            "cookie",
            "password",
            ".env",
            "authorization:",
            "traceback (most recent call last)",
        ):
            self.assertNotIn(forbidden, serialized.casefold())
        self.assertEqual(
            {
                "real_absolute_path",
                "runtime_task_uuid",
                "credential_or_query_value",
                "raw_provider_body_or_headers",
                "complete_media_manifest",
                "raw_dom",
                "private_runtime_log",
            },
            set(dict(payload["privacy"])["omitted"]),
        )

    def test_red_wrapper_is_gone_and_production_seam_is_explicit(self):
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=__file__)
        wrappers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "incident_red_contract"
        ]
        self.assertEqual(0, len(wrappers))
        calls = [
            node.func
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        ]
        self.assertFalse(any(
            isinstance(call, ast.Attribute)
            and call.attr == "_fail_reidentify"
            for call in calls
        ))
        self.assertFalse(any(
            isinstance(call, ast.Attribute)
            and isinstance(call.value, ast.Name)
            and call.value.id == "requests"
            for call in calls
        ))
        decorators = [
            decorator
            for node in ast.walk(tree)
            for decorator in getattr(node, "decorator_list", ())
        ]
        self.assertFalse(any(
            isinstance(decorator, ast.Attribute)
            and decorator.attr in {"expectedFailure", "skip", "skipIf", "skipUnless"}
            for decorator in decorators
        ))
        helper_source = inspect.getsource(
            production_fixture.ProviderSubjectPlanWiringRedContracts
            ._run_real_add_select
        )
        helper_tree = ast.parse(textwrap.dedent(helper_source))
        helper_calls = {
            ast.unparse(node.func)
            for node in ast.walk(helper_tree)
            if isinstance(node, ast.Call)
        }
        self.assertTrue({
            "AddTaskDialog._submit",
            "PlanReviewDialog._select_provider_candidate",
            "manager._queue.join",
            "TaskQueueManager",
        }.issubset(helper_calls))


class Err016OperationLifecycleIncidentRedContracts(
    unittest.IsolatedAsyncioTestCase
):
    async def _run_real_incident(
        self,
        root: Path,
        *,
        failure_mode: str | None,
    ):
        case = _incident()
        source = production_fixture._make_episode_source(
            root / "source",
            str(case["source_stem"]),
            tuple(int(item) for item in case["regular_episode_numbers"]),
        )
        harness = production_fixture.ProviderSubjectPlanWiringRedContracts(
            methodName="runTest"
        )
        failed_terms = (
            tuple(str(item) for item in case["same_work_successor_query_terms"])
            if failure_mode is not None
            else None
        )
        with (
            patch.dict(
                production_fixture.YAMA_TMDB,
                _tmdb_anchor(case),
                clear=True,
            ),
            patch("src.task_queue.logger.exception") as logged,
        ):
            result = await harness._run_real_add_select(
                root=root / "flow",
                source=source,
                subjects=production_fixture.YAMA_SUBJECTS,
                relations=production_fixture.YAMA_RELATIONS,
                selected_subject_id=str(
                    dict(case["selected_work"])["provider_id"]
                ),
                fail_subject=failed_terms,
                failure_mode=failure_mode or "transport",
            )
        return harness, result, logged

    async def test_legitimate_cohort_wait_is_a_persisted_non_error_outcome(self):
        case = _incident()
        expected = dict(case["expected_waiting_outcome"])
        with tempfile.TemporaryDirectory() as directory:
            harness, result, logged = await self._run_real_incident(
                Path(directory), failure_mode="no_match"
            )
        operation = operation_fixture.current_provider_selection_context(
            result.task
        )
        problems = harness._real_path_problems(result)
        if operation.get("state") != "awaiting_user":
            problems.append(
                f"legitimate cohort wait persisted {operation.get('state')!r}, not awaiting_user"
            )
        if result.task.get("workflow_status") != expected["workflow_status"]:
            problems.append(
                f"workflow status is {result.task.get('workflow_status')!r}"
            )
        if bool(result.task.get("error")) != expected["task_error_present"]:
            problems.append("legitimate cohort wait persisted a task error")
        if logged.call_count != expected["queue_exception_count"]:
            problems.append(
                f"legitimate cohort wait logged {logged.call_count} queue exception(s)"
            )
        if result.state.revision != result.initial_state.revision + 1:
            problems.append("waiting step did not supersede the old Decision revision")
        if result.state.confirmed_work or result.state.provider_link_binding:
            problems.append("waiting step retained stale work/provider authority")
        if result.business_json or result.target_files or not result.source_unchanged:
            problems.append("Plan-only waiting step produced a business/media side effect")
        self.assertEqual([], problems)

    async def test_waiter_returns_persisted_awaiting_user_without_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "sanitized-err016-waiter"
            task_root = root / "tasks"
            task_root.mkdir(parents=True)
            (task_root / f"{task_id}.json").write_text(
                json.dumps(_actionable_task(task_id), ensure_ascii=False),
                encoding="utf-8",
            )
            manager = ControlledQueue(
                task_root=task_root,
                decision_state_root=root / "decisions",
                recovery_abandon_audit_root=root / "abandon",
                plan_root=root / "plans",
                test_mode=True,
            )
            started = time.perf_counter()
            operation = await manager.wait_for_selection_result(
                task_id, timeout=0.30
            )
            elapsed = time.perf_counter() - started
        self.assertEqual("awaiting_user", operation.get("state"))
        self.assertLess(
            elapsed,
            0.15,
            f"awaiting_user was returned only after timeout ({elapsed:.3f}s)",
        )

    async def test_generic_retry_does_not_bypass_an_awaiting_user_operation(self):
        task = _actionable_task("sanitized-err016-retry")
        queue = SimpleNamespace(retry_current=AsyncMock())
        with (
            patch("src.pages.data_table_page.get_task", return_value=task),
            patch("src.pages.data_table_page.task_queue", queue),
            patch("src.pages.data_table_page.notify"),
            patch("src.pages.data_table_page.refresh_dashboard"),
            patch("src.pages.data_table_page._review_tasks.refresh"),
            patch("src.pages.data_table_page._history_tasks.refresh"),
        ):
            await data_table_page._retry_task(str(task["uuid"]))
        queue.retry_current.assert_not_awaited()
        self.assertIn("仍需确认", data_table_page.task_feedback_text(task))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_root = root / "tasks"
            task_root.mkdir(parents=True)
            (task_root / f"{task['uuid']}.json").write_text(
                json.dumps(task, ensure_ascii=False),
                encoding="utf-8",
            )
            manager = ControlledQueue(
                task_root=task_root,
                decision_state_root=root / "decisions",
                recovery_abandon_audit_root=root / "abandon",
                plan_root=root / "plans",
                test_mode=True,
            )
            with self.assertRaises(PlanExecutionError):
                await manager.retry_current(str(task["uuid"]), source="retry")

    async def test_safe_remove_accepts_the_rendered_awaiting_user_state(self):
        task = _actionable_task("sanitized-err016-remove")
        dialog = SimpleNamespace(
            task_data=task,
            plan=SimpleNamespace(task_id=str(task["uuid"])),
            _close_review=Mock(),
        )
        queue = SimpleNamespace(abandon_and_remove=AsyncMock())
        with (
            patch("src.pages.plan_review_page.task_queue", queue),
            patch("src.pages.plan_review_page.notify"),
            patch("src.pages.data_table_page.refresh_dashboard"),
            patch("src.pages.data_table_page._review_tasks.refresh"),
        ):
            await PlanReviewDialog._remove_provider_failure_task(dialog)
        queue.abandon_and_remove.assert_called_once()
        dialog._close_review.assert_called_once_with()

    async def test_transport_error_remains_failed_and_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            harness, result, logged = await self._run_real_incident(
                Path(directory), failure_mode="transport"
            )
        operation = operation_fixture.current_provider_selection_context(
            result.task
        )
        self.assertEqual([], harness._real_path_problems(result))
        self.assertEqual("failed", operation.get("state"))
        self.assertTrue(result.task.get("error"))
        self.assertEqual(1, logged.call_count)
        self.assertEqual(result.initial_state.revision, result.state.revision)
        self.assertTrue(result.source_unchanged)
        self.assertEqual((), result.target_files)

    async def test_ordinary_provider_success_still_advances_authority_without_error(self):
        with tempfile.TemporaryDirectory() as directory:
            harness, result, logged = await self._run_real_incident(
                Path(directory), failure_mode=None
            )
        operation = operation_fixture.current_provider_selection_context(
            result.task
        )
        self.assertEqual([], harness._real_path_problems(result))
        self.assertEqual("succeeded", operation.get("state"))
        self.assertGreater(result.state.revision, result.initial_state.revision)
        self.assertFalse(result.task.get("error"))
        self.assertEqual(0, logged.call_count)
        self.assertTrue(result.source_unchanged)
        self.assertEqual((), result.target_files)


class Err016RestartAndCommandControls(unittest.TestCase):
    def test_restart_preserves_awaiting_user_authority_and_safe_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "sanitized-err016-restart"
            task_root = root / "tasks"
            task_root.mkdir(parents=True)
            (task_root / f"{task_id}.json").write_text(
                json.dumps(_actionable_task(task_id), ensure_ascii=False),
                encoding="utf-8",
            )
            restarted = ControlledQueue(
                task_root=task_root,
                decision_state_root=root / "decisions",
                recovery_abandon_audit_root=root / "abandon",
                plan_root=root / "plans",
                test_mode=True,
            )._read(task_id)
        operation = operation_fixture.current_provider_selection_context(restarted)
        review_operation = restarted["review_operation"]
        self.assertEqual("awaiting_user", operation.get("state"))
        self.assertFalse(restarted.get("error"))
        self.assertEqual("actionable", review_operation.get("state"))
        self.assertEqual(
            ["confirm_cohort", "separate_work"],
            [item["kind"] for item in review_operation.get("commands", ())],
        )


if __name__ == "__main__":
    unittest.main()
