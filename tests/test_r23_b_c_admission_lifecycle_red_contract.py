"""R23 B/C contracts for admission, identity, and lifecycle authority."""

from __future__ import annotations
from tests.queue_fixture import TaskQueueManager as ControlledQueue
from tests.source_collection_fixture import collected_identity

import ast
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.pages import data_table_page, plan_review_page
from src.rename.plan_execution import PlanExecutionError
from src.rename.projection_authority import (
    ProjectionAuthorityError,
    SourceIdentity as ProjectionSourceIdentity,
)
from src.rename.rename_plan import RenamePlan
from src.rename.task_batch_scope import (
    TaskBatchScopeError,
    TaskBatchScopeManifestStore,
)
from src.rename.tv_planning_authority import TVPlanningAuthorityError
from src.task_queue import TaskQueueManager
import src.web as web_module
from tests.projection_test_support import reserve_active_batch_fixture


def _manager(root: Path) -> TaskQueueManager:
    return ControlledQueue(
        task_root=root / "tasks",
        decision_state_root=root / "decisions",
        recovery_abandon_audit_root=root / "abandon",
        plan_root=root / "plans",
        batch_scope_root=root / "task_batch_scope",
        test_mode=True,
    )


def _write(path: Path, content: bytes = b"media") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _called_attributes(callable_object) -> set[str]:
    tree = ast.parse(inspect.getsource(callable_object))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


class TypedAdmissionIncidentRedContracts(unittest.IsolatedAsyncioTestCase):
    async def test_b_same_batch_distinct_hardlink_aliases_fail_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _write(root / "first.mkv")
            second = root / "second.mkv"
            os.link(first, second)
            manager = _manager(root)
            prepared = manager.prepare_input_batch(
                (first, second),
                is_anime=True,
                is_movie=False,
                source="ui",
                preview_only=True,
            )
            blocked = False
            try:
                await manager.enqueue_batch(**prepared)
            except (PlanExecutionError, TaskBatchScopeError):
                blocked = True
            finally:
                await manager.stop()
            self.assertTrue(blocked, "same physical file acquired two task owners")
            self.assertEqual([], list((root / "tasks").glob("*.json")))

    async def test_b_task_write_failure_leaves_no_active_ghost_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _write(root / "first.mkv", b"first")
            second = _write(root / "second.mkv", b"second")
            manager = _manager(root)
            prepared = manager.prepare_input_batch(
                (first, second),
                is_anime=True,
                is_movie=False,
                source="ui",
                preview_only=True,
            )
            jobs = prepared["jobs"]
            original_write = manager._write

            def fail_second(task_id: str, data: dict) -> None:
                if task_id == jobs[1].task_id:
                    raise OSError("injected second task write failure")
                original_write(task_id, data)

            try:
                with patch.object(manager, "_write", side_effect=fail_second):
                    with self.assertRaisesRegex(OSError, "second task write"):
                        await manager.enqueue_batch(**prepared)
            finally:
                await manager.stop()
            manifest = manager.batch_scope_store.load(str(prepared["batch_id"]))
            self.assertEqual("failed_closed", manifest.state)
            self.assertEqual([], list((root / "tasks").glob("*.json")))

    async def test_f_restart_does_not_promote_reservation_without_task_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(root / "source.mkv")
            manager = _manager(root)
            prepared = manager.prepare_input_batch(
                (source,),
                is_anime=True,
                is_movie=False,
                source="ui",
                preview_only=True,
            )

            def crash_after_reservations(phase: str) -> None:
                if phase == "reservations_written":
                    raise KeyboardInterrupt("injected reservation crash")

            crashing_store = TaskBatchScopeManifestStore(
                root / "task_batch_scope", _phase_hook=crash_after_reservations
            )
            with self.assertRaisesRegex(KeyboardInterrupt, "reservation crash"):
                crashing_store.reserve_batch(
                    batch_id=str(prepared["batch_id"]),
                    task_requests=prepared["task_requests"],
                )

            restarted = _manager(root)
            try:
                await restarted.start()
            finally:
                await restarted.stop()
            recovered = restarted.batch_scope_store.load(
                str(prepared["batch_id"])
            )
            self.assertEqual("failed_closed", recovered.state)
            self.assertEqual((), recovered.reservations)

    def test_control_distinct_inputs_reach_one_active_batch(self):
        async def exercise() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = _write(root / "first.mkv", b"first")
                second = _write(root / "second.mkv", b"second")
                manager = _manager(root)
                prepared = manager.prepare_input_batch(
                    (first, second),
                    is_anime=True,
                    is_movie=False,
                    source="ui",
                    preview_only=True,
                )
                try:
                    task_ids = await manager.enqueue_batch(**prepared)
                finally:
                    await manager.stop()
                self.assertEqual(2, len(task_ids))
                self.assertEqual(
                    "active",
                    manager.batch_scope_store.load(
                        str(prepared["batch_id"])
                    ).state,
                )

        import asyncio

        asyncio.run(exercise())


class CanonicalPhysicalIdentityIncidentRedContracts(unittest.TestCase):
    def test_b_batch_and_projection_use_one_physical_identity_serialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(root / "source.mkv")
            manager = _manager(root)
            batch = collected_identity(source)
            projection = ProjectionSourceIdentity.from_path(
                source, inventory_fingerprint="a" * 64
            )
            self.assertEqual(batch.location_key, projection.location_key)
            self.assertEqual(batch.physical_key, projection.physical_key)

    def test_b_projection_identity_rejects_zero_inode_as_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            source = _write(Path(directory) / "source.mkv")
            unavailable = SimpleNamespace(
                st_dev=1,
                st_ino=0,
                st_size=source.stat().st_size,
                st_mtime_ns=source.stat().st_mtime_ns,
            )
            with patch.object(Path, "stat", return_value=unavailable):
                with self.assertRaisesRegex(
                    ProjectionAuthorityError, "identity|inode|物理身份|不可用"
                ):
                    ProjectionSourceIdentity.from_path(
                        source, inventory_fingerprint="b" * 64
                    )

    def test_control_batch_identity_has_positive_inode_on_local_ntfs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(root / "source.mkv")
            identity = collected_identity(source)
            self.assertRegex(identity.physical_key, r"inode:[1-9][0-9]*$")


class LegacyAdmissionDisconnectIncidentRedContracts(unittest.TestCase):
    def test_b_all_three_production_producers_disconnect_legacy_enqueue(self):
        callers = {
            "webhook": web_module._send_task,
            "dashboard_retry": data_table_page._retry_task,
            "workspace_replan": plan_review_page._reenqueue_from_workspace,
        }
        offenders = [
            name for name, caller in callers.items()
            if "enqueue" in _called_attributes(caller)
        ]
        self.assertEqual([], offenders)

    def test_b_plan_stage_cannot_create_an_implicit_reservation(self):
        called = _called_attributes(plan_review_page._collect_tv_task_source_scope)
        self.assertNotIn("reserve_batch", called)


class LifecycleAuthorityIncidentRedContracts(
    unittest.IsolatedAsyncioTestCase
):
    async def test_f_cancel_then_restart_cannot_leave_active_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(root / "source.mkv")
            manager = _manager(root)
            prepared = manager.prepare_input_batch(
                (source,),
                is_anime=True,
                is_movie=False,
                source="ui",
                preview_only=True,
            )
            task_id = (await manager.enqueue_batch(**prepared))[0]
            self.assertTrue(manager.cancel_queued(task_id))
            await manager.stop()

            restarted = _manager(root)
            try:
                await restarted.start()
            finally:
                await restarted.stop()
            manifest = restarted.batch_scope_store.load(
                str(prepared["batch_id"])
            )
            self.assertNotEqual("active", manifest.state)
            self.assertNotIn(task_id, restarted._pending)

    async def test_c_retry_preserves_current_batch_scope_and_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(root / "source.mkv")
            manager = _manager(root)
            prepared = manager.prepare_input_batch(
                (source,),
                is_anime=True,
                is_movie=False,
                source="ui",
                preview_only=True,
            )
            try:
                task_id = (await manager.enqueue_batch(**prepared))[0]
                before = manager._read(task_id)
                with (
                    patch.object(data_table_page, "task_queue", manager),
                    patch.object(
                        data_table_page,
                        "get_task",
                        side_effect=lambda value: manager._read(value),
                    ),
                    patch.object(data_table_page, "notify"),
                    patch.object(data_table_page, "refresh_dashboard"),
                    patch.object(data_table_page._review_tasks, "refresh"),
                    patch.object(data_table_page._history_tasks, "refresh"),
                ):
                    await data_table_page._retry_task(task_id)
                after = manager._read(task_id)
            finally:
                await manager.stop()
            for key in (
                "batch_id",
                "batch_scope_fingerprint",
                "reservation_lineage_id",
                "reservation_revision",
                "task_source_scope",
            ):
                self.assertEqual(before.get(key), after.get(key), key)

    async def test_c_delete_retires_reservation_and_ownership_but_keeps_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(root / "source.mkv")
            manager = _manager(root)
            prepared = manager.prepare_input_batch(
                (source,),
                is_anime=True,
                is_movie=False,
                source="ui",
                preview_only=True,
            )
            record_root = root / "record"
            record_root.mkdir()
            try:
                task_id = (await manager.enqueue_batch(**prepared))[0]
                manager.projection_commit_service.ownership_store.claim(
                    task_id=task_id,
                    decision_revision=0,
                    inventory_fingerprint="c" * 64,
                    identities=(
                        ProjectionSourceIdentity.from_path(
                            source, inventory_fingerprint="c" * 64
                        ),
                    ),
                )
                ledger = record_root / f"{task_id}.ledger.json"
                ledger.write_text('{"status":"completed"}', encoding="utf-8")
                with (
                    patch.object(data_table_page, "task_queue", manager),
                    patch.object(
                        data_table_page,
                        "get_task",
                        side_effect=lambda value: manager._read(value),
                    ),
                    patch.object(data_table_page, "TASK_PATH", manager.task_root),
                    patch.object(data_table_page, "RECORD_PATH", record_root),
                    patch.object(data_table_page, "PLAN_PATH", root / "plans"),
                    patch.object(
                        data_table_page, "PLAN_REVIEW_PATH", root / "reviews"
                    ),
                    patch.object(data_table_page, "notify"),
                    patch.object(data_table_page, "refresh_dashboard"),
                    patch.object(data_table_page._review_tasks, "refresh"),
                    patch.object(data_table_page._history_tasks, "refresh"),
                ):
                    await data_table_page._delete_task(task_id)
            finally:
                await manager.stop()
            manifest = manager.batch_scope_store.load(str(prepared["batch_id"]))
            ownership = (
                manager.projection_commit_service.ownership_store.load_optional(
                    task_id
                )
            )
            self.assertNotEqual("active", manifest.state)
            self.assertTrue(ownership is None or ownership.state != "active")
            self.assertTrue(ledger.is_file(), "immutable execution audit was deleted")
            failure_remove_source = inspect.getsource(
                plan_review_page.PlanReviewDialog._remove_provider_failure_task
            )
            self.assertIn("abandon_and_remove", failure_remove_source)
            self.assertNotIn("unlink(", failure_remove_source)

    async def test_c_abandoned_batch_scope_is_rejected_by_plan_consumer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(root / "source" / "Episode 01.mkv")
            manager = _manager(root)
            prepared = manager.prepare_input_batch(
                (source,),
                is_anime=True,
                is_movie=False,
                source="ui",
                preview_only=True,
            )
            manifest = reserve_active_batch_fixture(
                manager.batch_scope_store,
                batch_id=str(prepared["batch_id"]),
                task_requests=prepared["task_requests"],
            )
            scope = manifest.task_source_scopes[0]
            manager.batch_scope_store.transition(
                manifest.batch_id,
                expected_revision=manifest.revision,
                target_state="abandoned",
            )
            plan = RenamePlan.from_mapping(
                task_id=scope.task_id,
                source_root=source.parent,
                mapping={source: root / "target" / "S01E01.mkv"},
            )
            with patch.object(
                plan_review_page, "DECISION_STATE_PATH", root / "decisions"
            ):
                with self.assertRaisesRegex(
                    TVPlanningAuthorityError, "active|lineage|reservation|归属"
                ):
                    plan_review_page._collect_tv_task_source_scope(
                        plan,
                        {"task_source_scope": scope.to_dict()},
                        expected_revision=1,
                        request_fingerprint="d" * 64,
                    )

    def test_control_active_batch_can_transition_to_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(root / "source.mkv")
            manager = _manager(root)
            prepared = manager.prepare_input_batch(
                (source,),
                is_anime=True,
                is_movie=False,
                source="ui",
                preview_only=True,
            )
            manifest = reserve_active_batch_fixture(
                manager.batch_scope_store,
                batch_id=str(prepared["batch_id"]),
                task_requests=prepared["task_requests"],
            )
            completed = manager.batch_scope_store.transition(
                manifest.batch_id,
                expected_revision=manifest.revision,
                target_state="completed",
            )
            self.assertEqual("completed", completed.state)


if __name__ == "__main__":
    unittest.main()
