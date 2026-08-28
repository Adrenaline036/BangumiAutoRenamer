"""RED contracts for legacy approved-execution recovery reapproval.

The live rc.37 counterexample reached the real Dashboard but its visible
``核查`` action could not pass the backend acknowledgement gate.  These tests
freeze a truthful task-only transition back to the same Plan review without
granting ordinary retry, delete, or direct execution authority.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.pages import data_table_page, plan_review_page as review_page_module
from src.utils import utils as utils_module
from src.pages.plan_review_page import (
    PlanReviewDialog,
    build_plan_review_snapshot,
)
from src.rename.execution_recovery import (
    ExecutionRecoveryError,
    acknowledge_execution_recovery,
)
from src.rename.plan_execution import PlanExecutionError
from src.rename.plan_review import PlanReviewService, ReviewStatus, entry_id
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import ExecutionPolicy, PlanEntry, RenamePlan
from src.task_queue import TaskQueueManager
from src.utils.utils import (
    mutate_task_at,
    read_task_at,
    task_record_lock,
    write_task_at,
)
from tests.presentation_fixtures import with_tv_presentation
from tests.projection_test_support import save_plan_fixture


FINGERPRINT = "a" * 64


class _UiStub:
    def classes(self, *_args, **_kwargs):
        return self

    def props(self, *_args, **_kwargs):
        return self

    def tooltip(self, *_args, **_kwargs):
        return self

    def style(self, *_args, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _ButtonStub(_UiStub):
    def __init__(self):
        self.props_values: list[str] = []
        self.class_values: list[str] = []
        self.tooltip_values: list[str] = []

    def classes(self, value, *_args, **_kwargs):
        self.class_values.append(str(value))
        return self

    def props(self, value, *_args, **_kwargs):
        self.props_values.append(str(value))
        return self

    def tooltip(self, value, *_args, **_kwargs):
        self.tooltip_values.append(str(value))
        return self


class _DialogStub(_UiStub):
    def __init__(self):
        self.deleted = False
        self.submitted = False

    def open(self):
        return None

    def close(self):
        return None

    def submit(self, *_args, **_kwargs):
        self.submitted = True
        return None

    def delete(self):
        self.deleted = True

    def __await__(self):
        async def done():
            return None

        return done().__await__()


def _legacy_task(task_id: str, fingerprint: str = FINGERPRINT) -> dict:
    return {
        "uuid": task_id,
        "path": "synthetic/source",
        "name": "向山进发",
        "is_anime": True,
        "workflow_status": "failed",
        "queue_operation": "execute_approved",
        "execution_requested_fingerprint": fingerprint,
        "execution_phase": None,
        "error": "legacy pre-media incident",
    }


def _reapproval_task(task_id: str, fingerprint: str = FINGERPRINT) -> dict:
    return {
        **_legacy_task(task_id, fingerprint),
        "execution_requires_manual_check": False,
        "execution_may_have_written_targets": True,
        "execution_manual_check_acknowledged_at": "2026-08-22T12:00:00+00:00",
        "execution_recovery_disposition": "reapproval_required",
    }


def _make_approved_plan(root: Path, task_id: str):
    source = root / "source" / "Episode.mkv"
    target = root / "library" / "Season 01" / "Fixture S01E01.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic-video")
    plan = with_tv_presentation(
        RenamePlan(
            task_id=task_id,
            source_root=source.parent,
            entries=(
                PlanEntry(
                    source,
                    target,
                    season_id=1,
                    episode_id=1,
                    media_kind="video",
                    role="main_episode",
                    requires_review=True,
                ),
            ),
            expected_sources=(source,),
            metadata={"target_root": str(target.parents[1])},
            execution_policy=ExecutionPolicy(
                target_root=target.parents[1], dry_run=False
            ),
        )
    )
    store = PlanStore(root / "plan")
    plan = save_plan_fixture(store, plan)
    review = PlanReviewService(store, root / "review", root / "work_catalog")
    fingerprint = store.fingerprint(plan)
    review.approve_entries(
        task_id,
        fingerprint,
        tuple(entry_id(item) for item in plan.entries),
    )
    return plan, store, review, fingerprint, source, target


def _file_manifest_excluding(
    root: Path,
    *excluded: Path,
) -> dict[str, bytes]:
    excluded_paths = {
        path.resolve(strict=False)
        for path in excluded
    }
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
        and path.resolve(strict=False) not in excluded_paths
    }


class LegacyExecutionReapprovalRedContracts(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_ack_is_task_only_and_restart_preserves_reapproval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_root = root / "task"
            task_root.mkdir()
            task_id = "legacy-ack"
            write_task_at(task_root, task_id, _legacy_task(task_id))

            authority_paths = []
            for name in (
                "plan",
                "review",
                "decision",
                "series",
                "provider",
                "record",
                "recovery-audit",
                "work-catalog",
            ):
                path = root / name / f"{task_id}.json"
                path.parent.mkdir()
                path.write_bytes((name + "-authority").encode("utf-8"))
                authority_paths.append(path)
            source = root / "media" / "Episode.mkv"
            target = root / "library" / "Season 01" / "Episode.mkv"
            source.parent.mkdir()
            source.write_bytes(b"source-bytes")
            frozen = {path: path.read_bytes() for path in authority_paths + [source]}
            task_path = task_root / f"{task_id}.json"
            task_lock = task_root / f".{task_id}.json.lock"
            non_task_manifest = _file_manifest_excluding(
                root,
                task_path,
                task_lock,
            )

            locked_bytes = task_path.read_bytes()
            with task_record_lock(task_root, task_id):
                with self.assertRaises(ExecutionRecoveryError):
                    acknowledge_execution_recovery(task_id, task_root)
            self.assertEqual(locked_bytes, task_path.read_bytes())

            try:
                updated = acknowledge_execution_recovery(task_id, task_root)
            except ExecutionRecoveryError as exc:
                self.fail(
                    "legacy execution acknowledgement must reach the durable "
                    f"reapproval transition: {exc}"
                )

            self.assertEqual("failed", updated.get("workflow_status"))
            self.assertEqual(
                "reapproval_required",
                updated.get("execution_recovery_disposition"),
            )
            self.assertFalse(updated.get("execution_requires_manual_check"))
            self.assertIsNot(
                updated.get("execution_may_have_written_targets"), False
            )
            self.assertEqual(
                FINGERPRINT,
                updated.get("execution_requested_fingerprint"),
            )
            self.assertTrue(updated.get("execution_manual_check_acknowledged_at"))
            for path, content in frozen.items():
                self.assertEqual(content, path.read_bytes(), path)
            self.assertFalse(target.exists())
            self.assertEqual(
                non_task_manifest,
                _file_manifest_excluding(root, task_path, task_lock),
            )
            acknowledged_bytes = task_path.read_bytes()
            with self.assertRaises(ExecutionRecoveryError):
                acknowledge_execution_recovery(task_id, task_root)
            self.assertEqual(acknowledged_bytes, task_path.read_bytes())

            manager = TaskQueueManager(task_root=task_root, test_mode=False)
            await manager.start()
            await asyncio.sleep(0)
            restarted = read_task_at(task_root, task_id)
            pending = list(manager._pending)
            qsize = manager._queue.qsize()
            await manager.stop()
            self.assertEqual("failed", restarted.get("workflow_status"))
            self.assertEqual(
                "reapproval_required",
                restarted.get("execution_recovery_disposition"),
            )
            self.assertEqual([], pending)
            self.assertEqual(0, qsize)
            self.assertEqual(
                acknowledged_bytes,
                task_path.read_bytes(),
            )
            for path, content in frozen.items():
                self.assertEqual(content, path.read_bytes(), path)
            self.assertFalse(target.exists())
            self.assertEqual(
                non_task_manifest,
                _file_manifest_excluding(root, task_path, task_lock),
            )

            # A genuine media-boundary state has the same visible check action.
            # Completing that check must therefore reach the same conservative
            # reapproval exit, never an ordinary retry/delete state.
            boundary_id = "boundary-ack"
            boundary_path = task_root / f"{boundary_id}.json"
            write_task_at(
                task_root,
                boundary_id,
                {
                    **_legacy_task(boundary_id),
                    "execution_phase": "media_boundary_entered",
                    "execution_requires_manual_check": True,
                    "execution_may_have_written_targets": True,
                },
            )
            boundary_lock = task_root / f".{boundary_id}.json.lock"
            boundary_manifest = _file_manifest_excluding(
                root,
                boundary_path,
                boundary_lock,
            )
            boundary = acknowledge_execution_recovery(boundary_id, task_root)
            self.assertEqual(
                "reapproval_required",
                boundary.get("execution_recovery_disposition"),
            )
            self.assertFalse(boundary.get("execution_requires_manual_check"))
            self.assertIsNot(
                boundary.get("execution_may_have_written_targets"), False
            )
            self.assertEqual(
                boundary_manifest,
                _file_manifest_excluding(root, boundary_path, boundary_lock),
            )

    async def test_reapproval_card_only_opens_same_plan_review_with_truthful_hint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_id = "legacy-card"
            plan, store, _review, fingerprint, _source, _target = (
                _make_approved_plan(root, task_id)
            )
            task = _reapproval_task(task_id, fingerprint)
            current = {"task": task}
            labels: list[str] = []
            visible: list[str] = []
            buttons: list[tuple[str, object, _ButtonStub]] = []
            opened: list[tuple[RenamePlan, dict]] = []

            def record_button(label, *args, **kwargs):
                button = _ButtonStub()
                labels.append(str(label))
                visible.append(str(label))
                buttons.append((str(label), kwargs.get("on_click"), button))
                return button

            def record_text(value, *args, **kwargs):
                visible.append(str(value))
                return _UiStub()

            def capture_dialog(opened_plan, opened_task):
                opened.append((opened_plan, dict(opened_task)))
                return _DialogStub()

            with (
                patch.object(
                    data_table_page,
                    "get_task",
                    side_effect=lambda _task_id: dict(current["task"]),
                ),
                patch.object(
                    data_table_page.DecisionStateStore,
                    "load_optional",
                    return_value=None,
                ),
                patch.object(data_table_page, "_path_tree_button"),
                patch.object(data_table_page.ui, "card", return_value=_UiStub()),
                patch.object(data_table_page.ui, "row", return_value=_UiStub()),
                patch.object(data_table_page.ui, "column", return_value=_UiStub()),
                patch.object(data_table_page.ui, "badge", side_effect=record_text),
                patch.object(data_table_page.ui, "label", side_effect=record_text),
                patch.object(data_table_page.ui, "button", side_effect=record_button),
                patch.object(data_table_page, "notify") as notify,
                patch.object(
                    data_table_page,
                    "plan_review_page",
                    new=review_page_module.plan_review_page,
                ),
                patch.object(review_page_module, "PLAN_PATH", store.root),
                patch.object(
                    review_page_module,
                    "get_task",
                    side_effect=lambda _task_id: dict(current["task"]),
                ),
                patch.object(
                    review_page_module,
                    "_dialog_mount_context",
                    side_effect=nullcontext,
                ),
                patch.object(
                    review_page_module,
                    "PlanReviewDialog",
                    side_effect=capture_dialog,
                ),
            ):
                data_table_page._task_card(task)
                self.assertEqual(["重新审核"], labels)
                callback = buttons[0][1]
                result = callback()
                if inspect.isawaitable(result):
                    await result
                self.assertEqual(1, len(opened))
                self.assertEqual(task_id, opened[0][0].task_id)
                self.assertEqual(
                    "reapproval_required",
                    opened[0][1].get("execution_recovery_disposition"),
                )

                # A callback rendered from an older card must re-read authority
                # and refuse once the task enters a media-boundary state.
                current["task"] = {
                    **task,
                    "execution_recovery_disposition": None,
                    "execution_phase": "media_boundary_entered",
                    "execution_requires_manual_check": True,
                }
                stale_result = callback()
                if inspect.isawaitable(stale_result):
                    await stale_result
                self.assertEqual(1, len(opened))
                self.assertGreaterEqual(notify.call_count, 1)

            for _label, _callback, button in buttons:
                visible.extend(button.props_values)
                visible.extend(button.class_values)
                visible.extend(button.tooltip_values)
            rendered = "\n".join(visible)
            self.assertIn("已核查", rendered)
            self.assertIn("重新审核", rendered)
            self.assertNotIn("请核查后再选择处理方式", rendered)
            for forbidden in (
                "重试",
                "编辑",
                "删除",
                "ledger",
                "snapshot",
                "binding",
                "fingerprint",
                "revision",
                "series_key",
                "seriescatalog",
                "providerlinkcatalog",
                "schema_version",
                "canonical_root",
                "associated asset",
                "provider_evidence",
            ):
                self.assertNotIn(forbidden, rendered.casefold())

    async def test_reapproved_card_shows_current_ready_state_not_stale_error(self):
        task_id = "11111111-1111-4111-8111-111111111111"
        task = {
            **_reapproval_task(task_id),
            "workflow_status": "approved",
            "execution_recovery_disposition": None,
            "execution_reapproval_fingerprint": FINGERPRINT,
            "error": (
                "[任务队列] [WinError 5] SeriesCatalog execution ledger "
                "C:\\private\\task\\.approved.json.secret.tmp"
            ),
            "movie_inventory_reason": "MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION",
            "completed_at": "2026/8/21 7:33:45",
        }
        frozen = dict(task)
        real_task_card = data_table_page._task_card

        def render_card(payload: dict) -> tuple[str, dict[str, tuple[str, ...]]]:
            visible: list[str] = []
            buttons: list[_ButtonStub] = []
            text_classes: dict[str, list[str]] = {}

            class _TextStub(_UiStub):
                def __init__(self, text: str):
                    self.text = text

                def classes(self, value, *_args, **_kwargs):
                    text_classes.setdefault(self.text, []).append(str(value))
                    return self

            def record_button(label, *args, **kwargs):
                visible.append(str(label))
                button = _ButtonStub()
                buttons.append(button)
                return button

            def record_text(value, *args, **kwargs):
                text = str(value)
                visible.append(text)
                return _TextStub(text)

            with (
                patch.object(
                    data_table_page.DecisionStateStore,
                    "load_optional",
                    return_value=None,
                ),
                patch.object(data_table_page, "_path_tree_button"),
                patch.object(data_table_page.ui, "card", return_value=_UiStub()),
                patch.object(data_table_page.ui, "row", return_value=_UiStub()),
                patch.object(data_table_page.ui, "column", return_value=_UiStub()),
                patch.object(data_table_page.ui, "badge", side_effect=record_text),
                patch.object(data_table_page.ui, "label", side_effect=record_text),
                patch.object(data_table_page.ui, "button", side_effect=record_button),
            ):
                real_task_card(payload)
            for button in buttons:
                visible.extend(button.props_values)
                visible.extend(button.class_values)
                visible.extend(button.tooltip_values)
            return "\n".join(visible), {
                text: tuple(values) for text, values in text_classes.items()
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            task_root = Path(temp_dir)
            write_task_at(
                task_root,
                task_id,
                {
                    **_reapproval_task(task_id),
                    "error": task["error"],
                    "movie_inventory_reason": task["movie_inventory_reason"],
                    "completed_at": task["completed_at"],
                },
            )
            task_path = task_root / f"{task_id}.json"
            read_rows = data_table_page.load_task_rows

            rendered_cards: list[tuple[str, dict[str, tuple[str, ...]]]] = []
            routed_tasks: list[dict] = []

            def capture_card(payload: dict) -> None:
                routed_tasks.append(dict(payload))
                rendered_cards.append(render_card(payload))

            async def approve_and_close(_task_id: str) -> None:
                write_task_at(task_root, task_id, task)

            def refresh_review_section() -> None:
                data_table_page._review_tasks.func()

            with (
                patch.object(
                    data_table_page,
                    "get_task",
                    side_effect=lambda _task_id: read_task_at(task_root, task_id),
                ),
                patch.object(
                    data_table_page,
                    "plan_review_page",
                    new=AsyncMock(side_effect=approve_and_close),
                ) as review_page,
                patch.object(
                    data_table_page,
                    "load_task_rows",
                    side_effect=lambda: read_rows(task_root),
                ),
                patch.object(data_table_page.ui, "column", return_value=_UiStub()),
                patch.object(
                    data_table_page,
                    "_task_card",
                    side_effect=capture_card,
                ),
                patch.object(data_table_page._dashboard_summary, "refresh"),
                patch.object(
                    data_table_page._review_tasks,
                    "refresh",
                    side_effect=refresh_review_section,
                ) as review_refresh,
                patch.object(data_table_page._history_tasks, "refresh"),
            ):
                # The real reapproval callback must wait for the dialog, then
                # refresh the review section through _task_section -> _task_card.
                await data_table_page._open_execution_reapproval(task_id)
                self.assertEqual(1, len(rendered_cards))
                rendered_after_close, classes_after_close = rendered_cards[-1]
                self.assertEqual("approved", routed_tasks[-1]["workflow_status"])
                review_page.assert_awaited_once_with(task_id)
                review_refresh.assert_called_once_with()

                # A later process/browser refresh reloads the same card from disk
                # through the full review-section production route again.
                rendered_cards.clear()
                routed_tasks.clear()
                data_table_page._review_tasks.func()
                self.assertEqual(1, len(rendered_cards))
                rendered_after_restart, classes_after_restart = rendered_cards[-1]
                self.assertEqual("approved", routed_tasks[-1]["workflow_status"])

            frozen_bytes = task_path.read_bytes()
            self.assertEqual(task, read_task_at(task_root, task_id))
            self.assertEqual(frozen_bytes, task_path.read_bytes())

        self.assertEqual("已批准，尚未执行。", data_table_page.task_feedback_text(task))
        for rendered in (rendered_after_close, rendered_after_restart):
            self.assertIn("已批准，尚未执行。", rendered)
            for stale_or_internal in (
                "需要确认作品",
                "winerror",
                "seriescatalog",
                "ledger",
                "private",
                ".tmp",
            ):
                self.assertNotIn(stale_or_internal, rendered.casefold())
        self.assertEqual(frozen, task)

        dry_run = dict(task, preview_only=True)
        self.assertEqual(
            "已批准，尚未执行。",
            data_table_page.task_feedback_text(dry_run),
        )
        completed = dict(task, workflow_status="completed")
        self.assertNotEqual(
            "已批准，尚未执行。",
            data_table_page.task_feedback_text(completed),
        )
        safe_retry = dict(
            task,
            workflow_status="failed",
            execution_phase="failed_before_media",
            execution_recovery_disposition=None,
            execution_requires_manual_check=False,
            execution_reapproval_fingerprint=None,
        )
        self.assertEqual(
            "执行准备未完成，文件尚未处理。请稍后重新执行。",
            data_table_page.task_feedback_text(safe_retry),
        )
        self.assertEqual(
            "已核查，需重新审核。",
            data_table_page.task_feedback_text(_reapproval_task("reapproval-control")),
        )
        mismatched = dict(task, execution_reapproval_fingerprint="b" * 64)
        self.assertNotEqual(
            "已批准，尚未执行。",
            data_table_page.task_feedback_text(mismatched),
        )

        ordinary_failure = dict(
            task,
            workflow_status="failed",
            queue_operation="reidentify",
            execution_requested_fingerprint=None,
            execution_reapproval_fingerprint=None,
            execution_recovery_disposition=None,
            error="普通任务失败",
            movie_inventory_reason=None,
        )
        _, ordinary_classes = render_card(ordinary_failure)
        self.assertIn(
            "bar-error-text",
            " ".join(ordinary_classes["普通任务失败"]),
        )

        for ready_classes in (classes_after_close, classes_after_restart):
            class_names = " ".join(ready_classes["已批准，尚未执行。"])
            self.assertNotIn("bar-error-text", class_names)
            self.assertIn("bar-recovery-ready", class_names)

    async def test_reapproval_state_rejects_generic_stale_callbacks(self):
        task = _reapproval_task("legacy-stale")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = [root / name for name in ("task.json", "record.json", "record.ledger.json")]
            for path in paths:
                path.write_bytes(path.name.encode("utf-8"))
            before = {path: path.read_bytes() for path in paths}
            plan_store = MagicMock()
            review_service = MagicMock()
            with (
                patch.object(data_table_page, "get_task", return_value=task),
                patch.object(
                    data_table_page.task_queue,
                    "enqueue",
                    new=AsyncMock(),
                ) as enqueue,
                patch.object(
                    data_table_page,
                    "plan_review_page",
                    new=AsyncMock(),
                ) as generic_review,
                patch.object(
                    data_table_page,
                    "safe_task_path",
                    side_effect=paths,
                ),
                patch.object(data_table_page, "PlanStore", return_value=plan_store),
                patch.object(
                    data_table_page,
                    "PlanReviewService",
                    return_value=review_service,
                ),
                patch.object(data_table_page, "notify") as notify,
                patch.object(data_table_page, "refresh_dashboard"),
                patch.object(data_table_page._review_tasks, "refresh"),
                patch.object(data_table_page._history_tasks, "refresh"),
            ):
                await data_table_page._retry_task(task["uuid"])
                await data_table_page._open_review(task["uuid"])
                await data_table_page._delete_task(task["uuid"])

            enqueue.assert_not_awaited()
            generic_review.assert_not_awaited()
            plan_store.delete.assert_not_called()
            review_service.delete.assert_not_called()
            self.assertGreaterEqual(notify.call_count, 3)
            for path, content in before.items():
                self.assertTrue(path.exists(), path)
                self.assertEqual(content, path.read_bytes(), path)

    async def test_failed_legacy_approve_all_rebinds_current_plan_before_separate_execute(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_id = "legacy-approve"
            plan, store, review, fingerprint, source, target = _make_approved_plan(
                root, task_id
            )
            task_root = root / "task"
            task_root.mkdir()
            task = _reapproval_task(task_id, fingerprint)
            write_task_at(task_root, task_id, task)
            decision_path = root / "decision" / f"{task_id}.json"
            decision_path.parent.mkdir()
            decision_path.write_bytes(b"decision-authority")
            frozen_plan = store.path_for(task_id).read_bytes()
            frozen_decision = decision_path.read_bytes()
            frozen_source = source.read_bytes()

            enqueue = AsyncMock()
            captured: list[_DialogStub] = []
            confirm_buttons: list[tuple[str, object]] = []

            def capture_dialog(opened_plan, opened_task):
                dialog = _DialogStub()
                dialog.plan = opened_plan
                dialog.snapshot = build_plan_review_snapshot(opened_plan)
                dialog.review_service = review
                dialog.decision = review.load(task_id)
                dialog.task_data = dict(opened_task)
                dialog.review_badge = SimpleNamespace(text="")
                dialog._refresh_plan_header = MagicMock()
                dialog._render_entries = MagicMock()
                dialog._refresh_execute_button = MagicMock()
                dialog._close_review = MagicMock()
                dialog._approve = MethodType(PlanReviewDialog._approve, dialog)
                dialog._queue_execution = MethodType(
                    PlanReviewDialog._queue_execution, dialog
                )
                dialog.exit_requested = True
                captured.append(dialog)
                return dialog

            def record_confirm_button(label, *args, **kwargs):
                confirm_buttons.append((str(label), kwargs.get("on_click")))
                return _ButtonStub()

            with (
                patch.object(review_page_module, "PLAN_PATH", store.root),
                patch.object(review_page_module, "TASK_PATH", task_root),
                patch.object(utils_module, "TASK_PATH", task_root),
                patch.object(
                    review_page_module,
                    "get_task",
                    side_effect=lambda identifier: read_task_at(task_root, identifier),
                ),
                patch.object(
                    review_page_module,
                    "_dialog_mount_context",
                    side_effect=nullcontext,
                ),
                patch.object(
                    review_page_module,
                    "PlanReviewDialog",
                    side_effect=capture_dialog,
                ),
                patch.object(
                    review_page_module,
                    "write_task",
                    side_effect=lambda identifier, payload: write_task_at(
                        task_root, identifier, payload
                    ),
                ),
                patch.object(review_page_module, "notify"),
                patch.object(
                    review_page_module.ui, "dialog", return_value=_DialogStub()
                ),
                patch.object(review_page_module.ui, "card", return_value=_UiStub()),
                patch.object(review_page_module.ui, "row", return_value=_UiStub()),
                patch.object(review_page_module.ui, "label", return_value=_UiStub()),
                patch.object(
                    review_page_module.ui,
                    "button",
                    side_effect=record_confirm_button,
                ),
                patch.object(
                    review_page_module.task_queue,
                    "enqueue_approved",
                    new=enqueue,
                ),
            ):
                await review_page_module.plan_review_page(task_id)
                self.assertEqual(1, len(captured))
                dialog = captured[0]
                self.assertEqual(task_id, dialog.plan.task_id)
                self.assertEqual(
                    "reapproval_required",
                    dialog.task_data.get("execution_recovery_disposition"),
                )

                # A dialog opened before recovery acknowledgement must not
                # approve over the newer on-disk reapproval authority.
                authoritative_bytes = (
                    task_root / f"{task_id}.json"
                ).read_bytes()
                dialog.task_data = {
                    **task,
                    "workflow_status": "need_confirm",
                    "execution_recovery_disposition": None,
                }
                PlanReviewDialog._approve_all(dialog)
                self.assertEqual(
                    authoritative_bytes,
                    (task_root / f"{task_id}.json").read_bytes(),
                )
                enqueue.assert_not_awaited()

                # A state drift between approval persistence and the final
                # task transition must be observed under the shared task lock
                # and preserved instead of being overwritten by stale data.
                dialog.task_data = dict(task)

                def drift_then_mutate(identifier, mutation):
                    drifted = read_task_at(task_root, identifier)
                    drifted.update(
                        {
                            "workflow_status": "executing",
                            "selection_operation": {
                                "state": "concurrent-sentinel"
                            },
                        }
                    )
                    write_task_at(task_root, identifier, drifted)
                    return mutate_task_at(task_root, identifier, mutation)

                with patch.object(
                    review_page_module,
                    "mutate_task",
                    side_effect=drift_then_mutate,
                ):
                    PlanReviewDialog._approve_all(dialog)
                drifted = read_task_at(task_root, task_id)
                self.assertEqual("executing", drifted.get("workflow_status"))
                self.assertEqual(
                    {"state": "concurrent-sentinel"},
                    drifted.get("selection_operation"),
                )
                enqueue.assert_not_awaited()

                write_task_at(task_root, task_id, task)
                dialog.task_data = dict(task)
                PlanReviewDialog._approve_all(dialog)
                enqueue.assert_not_awaited()
                stored = read_task_at(task_root, task_id)
                approved = review.load(task_id)
                self.assertEqual("approved", stored.get("workflow_status"))
                self.assertEqual(
                    fingerprint,
                    stored.get("execution_reapproval_fingerprint"),
                )
                self.assertNotEqual(
                    "reapproval_required",
                    stored.get("execution_recovery_disposition"),
                )
                self.assertEqual(ReviewStatus.APPROVED, approved.status)
                self.assertEqual(fingerprint, approved.plan_fingerprint)

                dialog.task_data = stored
                PlanReviewDialog._confirm_execute(dialog)
                enqueue.assert_not_awaited()
                execute_callbacks = [
                    callback
                    for label, callback in confirm_buttons
                    if label == "确认入队执行"
                ]
                self.assertEqual(1, len(execute_callbacks))
                result = execute_callbacks[0]()
                if inspect.isawaitable(result):
                    await result

            enqueue.assert_awaited_once_with(task_id, fingerprint)
            self.assertEqual(frozen_plan, store.path_for(task_id).read_bytes())
            self.assertEqual(frozen_decision, decision_path.read_bytes())
            self.assertEqual(frozen_source, source.read_bytes())
            self.assertFalse(target.exists())
            self.assertFalse((root / "record").exists())
            self.assertFalse((root / "series_catalog").exists())

    async def test_legacy_dialog_and_ready_hint_are_truthful(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            task_root = Path(temp_dir)
            task = _legacy_task("legacy-dialog")
            write_task_at(task_root, task["uuid"], task)
            snapshot = SimpleNamespace(
                ledger_present=False,
                ledger_status="missing",
                entry_count=0,
                error="internal missing ledger diagnostic",
                plan_fingerprint="",
            )
            visible: list[str] = []
            buttons: list[tuple[str, object, _ButtonStub]] = []
            dialog = _DialogStub()

            def record_text(value, *args, **kwargs):
                visible.append(str(value))
                return _UiStub()

            def record_button(label, *args, **kwargs):
                button = _ButtonStub()
                visible.append(str(label))
                buttons.append((str(label), kwargs.get("on_click"), button))
                return button

            with (
                patch.object(data_table_page, "TASK_PATH", task_root),
                patch.object(
                    data_table_page,
                    "get_task",
                    side_effect=lambda identifier: read_task_at(
                        task_root, identifier
                    ),
                ),
                patch.object(
                    data_table_page,
                    "load_execution_recovery",
                    return_value=snapshot,
                ),
                patch.object(data_table_page.ui, "dialog", return_value=dialog),
                patch.object(data_table_page.ui, "card", return_value=_UiStub()),
                patch.object(data_table_page.ui, "column", return_value=_UiStub()),
                patch.object(data_table_page.ui, "row", return_value=_UiStub()),
                patch.object(data_table_page.ui, "label", side_effect=record_text),
                patch.object(data_table_page.ui, "button", side_effect=record_button),
                patch.object(data_table_page, "notify") as notify,
                patch.object(data_table_page, "refresh_dashboard"),
                patch.object(data_table_page._review_tasks, "refresh"),
                patch.object(data_table_page._history_tasks, "refresh"),
            ):
                await data_table_page._open_execution_recovery(task["uuid"])
                callbacks = [
                    callback
                    for label, callback, _button in buttons
                    if label == "我已完成核查"
                ]
                self.assertEqual(1, len(callbacks))
                result = callbacks[0]()
                if inspect.isawaitable(result):
                    await result

            updated = read_task_at(task_root, task["uuid"])
            hint = data_table_page.execution_recovery_action_hint(updated)
            notify_text = "\n".join(
                str(call.args[0]) for call in notify.call_args_list if call.args
            )
            for _label, _callback, button in buttons:
                visible.extend(button.props_values)
                visible.extend(button.class_values)
                visible.extend(button.tooltip_values)
            rendered = "\n".join(visible)
            self.assertIn("无法确认上次是否进入文件操作阶段", rendered)
            self.assertIn("未找到可用的历史执行记录", rendered)
            self.assertNotIn("曾进入文件操作边界", rendered)
            self.assertNotIn("已找到历史执行记录", rendered)
            self.assertTrue(dialog.submitted)
            self.assertEqual(
                "reapproval_required",
                updated.get("execution_recovery_disposition"),
            )
            self.assertIn("已核查", hint)
            self.assertIn("需重新审核", hint)
            self.assertIn("已核查", notify_text)
            self.assertIn("需重新审核", notify_text)
            all_visible = "\n".join((rendered, hint, notify_text)).casefold()
            for forbidden in (
                "重试",
                "编辑",
                "删除",
                "ledger",
                "snapshot",
                "binding",
                "fingerprint",
                "revision",
                "series_key",
                "seriescatalog",
                "providerlinkcatalog",
                "schema_version",
                "canonical_root",
                "associated asset",
                "provider_evidence",
            ):
                self.assertNotIn(forbidden, all_visible)

    async def test_acknowledgement_rejects_wrong_authority_states(self):
        cases = {
            "missing-fingerprint": {
                **_legacy_task("missing-fingerprint", ""),
            },
            "catalog-pending": {
                **_legacy_task("catalog-pending"),
                "workflow_status": "catalog_update_pending",
                "execution_requires_manual_check": True,
            },
            "active": {
                **_legacy_task("active"),
                "workflow_status": "executing",
                "execution_requires_manual_check": True,
            },
            "safe-pre-media": {
                **_legacy_task("safe-pre-media"),
                "execution_phase": "failed_before_media",
                "execution_requires_manual_check": True,
            },
            "media-boundary": {
                **_legacy_task("media-boundary"),
                "execution_phase": "media_boundary_entered",
                "execution_requires_manual_check": False,
            },
            "wrong-operation": {
                **_legacy_task("wrong-operation"),
                "queue_operation": "rename",
                "execution_requires_manual_check": True,
            },
            "completed": {
                **_legacy_task("completed"),
                "workflow_status": "completed",
                "execution_requires_manual_check": True,
            },
            "malformed-fingerprint": {
                **_legacy_task("malformed-fingerprint", "short"),
                "execution_requires_manual_check": True,
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rejected = []
            byte_stable = []
            for task_id, payload in cases.items():
                write_task_at(root, task_id, payload)
                before = (root / f"{task_id}.json").read_bytes()
                try:
                    acknowledge_execution_recovery(task_id, root)
                except ExecutionRecoveryError:
                    rejected.append(task_id)
                byte_stable.append(
                    before == (root / f"{task_id}.json").read_bytes()
                )
            self.assertEqual(list(cases), rejected)
            self.assertTrue(all(byte_stable))

    async def test_reapproval_required_cannot_directly_enqueue_approved(self):
        class FakeExecutionService:
            validation_calls: list[tuple[str, str]] = []

            @classmethod
            def validate_approved(cls, task_id, fingerprint):
                cls.validation_calls.append((task_id, fingerprint))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_task_at(root, "direct", _reapproval_task("direct"))
            task_path = root / "direct.json"
            frozen_task = task_path.read_bytes()
            approved_executor = AsyncMock()
            manager = TaskQueueManager(
                task_root=root,
                execution_service=FakeExecutionService(),
                approved_executor=approved_executor,
                test_mode=False,
            )
            try:
                with self.assertRaises(PlanExecutionError):
                    await manager.enqueue_approved("direct", FINGERPRINT)
                self.assertEqual([], FakeExecutionService.validation_calls)
                self.assertEqual([], list(manager._pending))
                self.assertEqual(0, manager._queue.qsize())
                self.assertEqual(frozen_task, task_path.read_bytes())
                approved_executor.assert_not_awaited()
            finally:
                await manager.stop()


if __name__ == "__main__":
    unittest.main()
