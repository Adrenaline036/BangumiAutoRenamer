"""RED contracts for the S1 Windows task-state incident.

These tests never touch real media.  Every contract below is an ordinary
GREEN regression and must pass without expected-failure wrappers.
"""

from __future__ import annotations

import asyncio
import errno
import inspect
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import src.utils.utils as task_utils
from src.pages import data_table_page
from src.rename.plan_execution import PlanExecutionError
from src.task_queue import TaskQueueManager
import tests.test_plan_execution as plan_execution_fixture


FINGERPRINT = "a" * 64


def _sharing_error(winerror: int) -> PermissionError:
    error = PermissionError(errno.EACCES, "task destination is busy")
    error.winerror = winerror
    return error


def _write_approved_task(root: Path, task_id: str) -> Path:
    path = root / f"{task_id}.json"
    path.write_text(
        json.dumps(
            {
                "uuid": task_id,
                "path": "synthetic/source",
                "workflow_status": "approved",
                "completed_at": None,
                "selection_operation": {
                    "state": "succeeded",
                    "result_decision_revision": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


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
    def open(self):
        return None

    def close(self):
        return None

    def submit(self, *_args, **_kwargs):
        return None

    def __await__(self):
        async def done():
            return None

        return done().__await__()


class TaskStateWindowsSyncRedContracts(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics only")
    def test_real_reader_release_allows_bounded_atomic_task_replace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_id = "reader-release"
            path = _write_approved_task(root, task_id)
            reader = path.open("r", encoding="utf-8")
            real_replace = os.replace
            first_failure = threading.Event()
            replace_attempts = 0

            def observed_replace(source, target):
                nonlocal replace_attempts
                replace_attempts += 1
                try:
                    return real_replace(source, target)
                except PermissionError:
                    if replace_attempts == 1:
                        first_failure.set()
                    raise

            def release_reader() -> None:
                first_failure.wait(timeout=1)
                reader.close()

            releaser = threading.Thread(target=release_reader, daemon=True)
            releaser.start()
            failure = None
            try:
                with patch.object(
                    task_utils.os, "replace", side_effect=observed_replace
                ):
                    task_utils._atomic_write_task_at(
                        root,
                        task_id,
                        {
                            "uuid": task_id,
                            "workflow_status": "queued",
                            "execution_phase": "admitted",
                        },
                    )
            except PermissionError as exc:
                failure = exc
            finally:
                releaser.join(timeout=1)
                if not reader.closed:
                    reader.close()

            payload = json.loads(path.read_text("utf-8"))
            self.assertIsNone(
                failure,
                f"short-lived Windows reader still aborted atomic task write: {failure}",
            )
            self.assertEqual("admitted", payload.get("execution_phase"))
            self.assertGreaterEqual(replace_attempts, 2)
            self.assertEqual([], list(root.glob("*.tmp")))

    def test_task_replace_retries_only_bounded_windows_sharing_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            real_replace = os.replace
            results = {}
            for winerror in (5, 32, 33):
                task_id = f"replace-{winerror}"
                path = _write_approved_task(root, task_id)
                attempts: list[int] = []

                def flaky_replace(source, target, *, code=winerror, seen=attempts):
                    seen.append(len(seen) + 1)
                    if len(seen) <= 2:
                        raise _sharing_error(code)
                    return real_replace(source, target)

                transient_failure = None
                with patch.object(task_utils.os, "replace", side_effect=flaky_replace):
                    try:
                        task_utils._atomic_write_task_at(
                            root,
                            task_id,
                            {
                                "uuid": task_id,
                                "workflow_status": "queued",
                                "execution_phase": "admitted",
                            },
                        )
                    except PermissionError as exc:
                        transient_failure = exc
                results[winerror] = (
                    transient_failure,
                    list(attempts),
                    json.loads(path.read_text("utf-8")).get("execution_phase"),
                )

            nonretry_task = "nonretry"
            nonretry_path = _write_approved_task(root, nonretry_task)
            original = nonretry_path.read_bytes()
            fatal_attempts = 0

            def fatal_replace(_source, _target):
                nonlocal fatal_attempts
                fatal_attempts += 1
                raise FileNotFoundError("invalid destination")

            with patch.object(task_utils.os, "replace", side_effect=fatal_replace):
                with self.assertRaises(FileNotFoundError):
                    task_utils._atomic_write_task_at(
                        root,
                        nonretry_task,
                        {"uuid": nonretry_task, "workflow_status": "queued"},
                    )

            exhaustion = {}
            for winerror in (5, 32, 33):
                exhausted_task = f"exhaust-{winerror}"
                exhausted_path = _write_approved_task(root, exhausted_task)
                exhausted_original = exhausted_path.read_bytes()
                exhausted_attempts = 0
                sleeps: list[float] = []
                monotonic_reads: list[float] = []
                clock = [0.0]
                original_error = _sharing_error(winerror)

                def always_busy(_source, _target):
                    nonlocal exhausted_attempts
                    exhausted_attempts += 1
                    raise original_error

                def fake_monotonic() -> float:
                    monotonic_reads.append(clock[0])
                    return clock[0]

                def fake_sleep(delay: float) -> None:
                    sleeps.append(float(delay))
                    clock[0] += float(delay)

                raised = None
                fake_time = SimpleNamespace(
                    monotonic=fake_monotonic,
                    sleep=fake_sleep,
                )
                with (
                    patch.object(task_utils.os, "replace", side_effect=always_busy),
                    patch.object(task_utils, "time", fake_time, create=True),
                ):
                    try:
                        task_utils._atomic_write_task_at(
                            root,
                            exhausted_task,
                            {"uuid": exhausted_task, "workflow_status": "queued"},
                        )
                    except PermissionError as exc:
                        raised = exc
                exhaustion[winerror] = {
                    "attempts": exhausted_attempts,
                    "sleeps": sleeps,
                    "monotonic_reads": monotonic_reads,
                    "elapsed": clock[0],
                    "raised_is_original": raised is original_error,
                    "bytes_unchanged": exhausted_path.read_bytes() == exhausted_original,
                }

            for winerror, (failure, attempts, phase) in results.items():
                self.assertIsNone(
                    failure,
                    f"retryable Windows error {winerror} was not retried: {failure}",
                )
                self.assertEqual(3, len(attempts))
                self.assertEqual("admitted", phase)
            self.assertEqual(1, fatal_attempts)
            self.assertEqual(original, nonretry_path.read_bytes())
            for winerror, result in exhaustion.items():
                self.assertEqual(
                    5,
                    result["attempts"],
                    f"Windows error {winerror} retry budget drifted",
                )
                self.assertEqual(4, len(result["sleeps"]))
                self.assertLessEqual(sum(result["sleeps"]), 1.0)
                self.assertGreaterEqual(
                    len(result["monotonic_reads"]),
                    result["attempts"],
                    "retry attempts were not bounded by the monotonic deadline",
                )
                self.assertLessEqual(result["elapsed"], 1.0)
                self.assertTrue(result["raised_is_original"])
                self.assertTrue(result["bytes_unchanged"])
            self.assertEqual([], list(root.glob("*.tmp")))

    def test_execution_persists_preparing_and_media_boundary_before_transfer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, _calls, task_root = (
                plan_execution_fixture.PlanExecutionTests()._approved_service(root)
            )
            original_factory = service.transfer_factory
            transfer_snapshots: list[dict] = []
            persisted_phases: list[str] = []
            original_atomic = task_utils._atomic_write_task_at

            def observe_atomic(write_root, task_id, payload):
                phase = payload.get("execution_phase")
                if phase:
                    persisted_phases.append(str(phase))
                return original_atomic(write_root, task_id, payload)

            def observing_factory(*args, **kwargs):
                transfer_snapshots.append(
                    json.loads(
                        (task_root / f"{plan.task_id}.json").read_text("utf-8")
                    )
                )
                return original_factory(*args, **kwargs)

            service.transfer_factory = observing_factory
            with patch.object(
                task_utils,
                "_atomic_write_task_at",
                side_effect=observe_atomic,
            ):
                service.execute(plan.task_id, fingerprint)

            final_task = json.loads(
                (task_root / f"{plan.task_id}.json").read_text("utf-8")
            )
            self.assertIn("preparing", persisted_phases)
            self.assertIn("media_boundary_entered", persisted_phases)
            self.assertLess(
                persisted_phases.index("preparing"),
                persisted_phases.index("media_boundary_entered"),
            )
            self.assertEqual(1, len(transfer_snapshots))
            self.assertEqual(
                "media_boundary_entered",
                transfer_snapshots[0].get("execution_phase"),
            )
            self.assertTrue(
                transfer_snapshots[0].get("execution_requires_manual_check")
            )
            self.assertEqual("completed", final_task.get("execution_phase"))
            self.assertFalse(final_task.get("execution_requires_manual_check"))

    def test_conditional_execution_update_preserves_fields_and_rejects_drift(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, _calls, task_root = (
                plan_execution_fixture.PlanExecutionTests()._approved_service(root)
            )
            task_path = task_root / f"{plan.task_id}.json"
            initial = json.loads(task_path.read_text("utf-8"))
            initial["selection_operation"] = {
                "state": "succeeded",
                "result_decision_revision": 1,
                "marker": "old",
            }
            task_path.write_text(json.dumps(initial), encoding="utf-8")

            read_happened = threading.Event()
            continue_write = threading.Event()
            original_read = service._read_task

            def blocked_read(task_id):
                payload = original_read(task_id)
                if threading.current_thread().name == "stale-task-writer":
                    read_happened.set()
                    continue_write.wait(timeout=1)
                return payload

            service._read_task = blocked_read
            writer_error: list[BaseException] = []

            def stale_writer() -> None:
                try:
                    service._update_task(
                        plan.task_id,
                        workflow_status="executing",
                        execution_fingerprint=fingerprint,
                    )
                except BaseException as exc:  # captured for deterministic cleanup
                    writer_error.append(exc)

            thread = threading.Thread(target=stale_writer, name="stale-task-writer")
            thread.start()
            if read_happened.wait(timeout=0.2):
                task_utils.merge_task_at(
                    task_root,
                    plan.task_id,
                    {
                        "selection_operation": {
                            "state": "succeeded",
                            "result_decision_revision": 2,
                            "marker": "new",
                        }
                    },
                )
                continue_write.set()
            else:
                thread.join(timeout=1)
                task_utils.merge_task_at(
                    task_root,
                    plan.task_id,
                    {
                        "selection_operation": {
                            "state": "succeeded",
                            "result_decision_revision": 2,
                            "marker": "new",
                        }
                    },
                )
            thread.join(timeout=1)
            continue_write.set()
            preserved = json.loads(task_path.read_text("utf-8"))

            drift_root = root / "drift"
            drift_root.mkdir()
            drift_service, _store, drift_plan, drift_fp, calls, drift_tasks = (
                plan_execution_fixture.PlanExecutionTests()._approved_service(drift_root)
            )
            drift_path = drift_tasks / f"{drift_plan.task_id}.json"
            drift_task = json.loads(drift_path.read_text("utf-8"))
            drift_task.update(
                {
                    "workflow_status": "running",
                    "queue_operation": "execute_approved",
                    "execution_requested_fingerprint": drift_fp,
                }
            )
            drift_path.write_text(json.dumps(drift_task), encoding="utf-8")
            original_validate = drift_service.validate_approved
            validation_calls = 0

            def mutate_after_validation(task_id, expected_fingerprint):
                nonlocal validation_calls
                result = original_validate(task_id, expected_fingerprint)
                validation_calls += 1
                if validation_calls == 1:
                    task_utils.merge_task_at(
                        drift_tasks,
                        task_id,
                        {"execution_requested_fingerprint": "f" * 64},
                    )
                return result

            drift_service.validate_approved = mutate_after_validation
            drift_rejected = False
            try:
                drift_service.execute(drift_plan.task_id, drift_fp)
            except PlanExecutionError:
                drift_rejected = True

            self.assertEqual([], writer_error)
            self.assertEqual("new", preserved["selection_operation"]["marker"])
            self.assertEqual(2, preserved["selection_operation"]["result_decision_revision"])
            self.assertTrue(drift_rejected, "fingerprint drift was overwritten")
            self.assertEqual([], calls, "transfer ran after request fingerprint drift")


class TaskStateWindowsAsyncRedContracts(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics only")
    async def test_permanent_reader_never_kills_worker_or_reaches_transfer(self):
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()
        safe_processed = asyncio.Event()
        approved_calls: list[str] = []

        async def normal_executor(job):
            if job.task_id == "blocker":
                blocker_started.set()
                await release_blocker.wait()

        async def approved_executor(job):
            approved_calls.append(job.task_id)
            if job.task_id == "safe-approved":
                safe_processed.set()

        class FakeExecutionService:
            @staticmethod
            def validate_approved(_task_id, _fingerprint):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            locked_path = _write_approved_task(root, "locked-approved")
            _write_approved_task(root, "safe-approved")
            manager = TaskQueueManager(
                task_root=root,
                executor=normal_executor,
                approved_executor=approved_executor,
                execution_service=FakeExecutionService(),
                test_mode=False,
            )
            worker_error = None
            reader = None
            try:
                await manager.start()
                await manager.enqueue(Path("synthetic/blocker"), task_id="blocker")
                await asyncio.wait_for(blocker_started.wait(), timeout=1)
                await manager.enqueue_approved("locked-approved", FINGERPRINT)
                reader = locked_path.open("r", encoding="utf-8")
                release_blocker.set()
                await asyncio.sleep(1.25)
                reader.close()
                reader = None

                await manager.enqueue_approved("safe-approved", FINGERPRINT)
                try:
                    await asyncio.wait_for(safe_processed.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
                if manager._worker_task is not None and manager._worker_task.done():
                    try:
                        worker_error = manager._worker_task.exception()
                    except asyncio.CancelledError:
                        worker_error = None
            finally:
                if reader is not None:
                    reader.close()
                try:
                    await manager.stop()
                except BaseException as exc:
                    worker_error = worker_error or exc

            self.assertNotIn("locked-approved", approved_calls)
            self.assertIn("safe-approved", approved_calls)
            self.assertIsNone(worker_error)
            self.assertNotIn("locked-approved", manager._pending)
            self.assertEqual([], list(root.glob("*.tmp")))

    async def test_queue_admission_failure_rolls_back_disk_pending_and_queue(self):
        class FakeExecutionService:
            @staticmethod
            def validate_approved(_task_id, _fingerprint):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_path = _write_approved_task(root, "approved-existing")
            approved_before = approved_path.read_bytes()
            manager = TaskQueueManager(
                task_root=root,
                execution_service=FakeExecutionService(),
                test_mode=False,
            )
            await manager.start()
            approved_put_snapshots: list[dict] = []

            async def fail_approved_put(_job):
                approved_put_snapshots.append(
                    json.loads(approved_path.read_text("utf-8"))
                )
                raise RuntimeError("queue put failed")

            with patch.object(
                manager._queue,
                "put",
                new=AsyncMock(side_effect=fail_approved_put),
            ):
                with self.assertRaises(RuntimeError):
                    await manager.enqueue_approved("approved-existing", FINGERPRINT)
            existing_pending = list(manager._pending)
            existing_after = approved_path.read_bytes()
            await manager.stop()

            fresh_root = root / "fresh"
            fresh_root.mkdir()
            fresh_manager = TaskQueueManager(task_root=fresh_root, test_mode=False)
            await fresh_manager.start()
            with patch.object(
                fresh_manager._queue,
                "put",
                new=AsyncMock(side_effect=RuntimeError("queue put failed")),
            ):
                with self.assertRaises(RuntimeError):
                    await fresh_manager.enqueue(
                        Path("synthetic/new"), task_id="new-admission"
                    )
            fresh_pending = list(fresh_manager._pending)
            fresh_task_exists = (fresh_root / "new-admission.json").exists()
            await fresh_manager.stop()

            write_failure_root = root / "write-failure-existing"
            write_failure_root.mkdir()
            write_failure_path = _write_approved_task(
                write_failure_root, "write-failure-approved"
            )
            write_failure_before = write_failure_path.read_bytes()
            write_failure_manager = TaskQueueManager(
                task_root=write_failure_root,
                execution_service=FakeExecutionService(),
                test_mode=False,
            )
            await write_failure_manager.start()
            existing_write_error = None
            with patch(
                "src.task_queue.write_task_at",
                side_effect=_sharing_error(5),
            ):
                try:
                    await write_failure_manager.enqueue_approved(
                        "write-failure-approved", FINGERPRINT
                    )
                except PermissionError as exc:
                    existing_write_error = exc
            existing_write_pending = list(write_failure_manager._pending)
            existing_write_qsize = write_failure_manager._queue.qsize()
            existing_worker_alive = bool(
                write_failure_manager._worker_task is not None
                and not write_failure_manager._worker_task.done()
            )
            await write_failure_manager.stop()

            new_write_root = root / "write-failure-new"
            new_write_root.mkdir()
            new_write_manager = TaskQueueManager(
                task_root=new_write_root,
                test_mode=False,
            )
            await new_write_manager.start()
            new_write_error = None
            with patch(
                "src.task_queue.write_task_at",
                side_effect=_sharing_error(32),
            ):
                try:
                    await new_write_manager.enqueue(
                        Path("synthetic/new-write"), task_id="new-write-failure"
                    )
                except PermissionError as exc:
                    new_write_error = exc
            new_write_pending = list(new_write_manager._pending)
            new_write_qsize = new_write_manager._queue.qsize()
            new_worker_alive = bool(
                new_write_manager._worker_task is not None
                and not new_write_manager._worker_task.done()
            )
            await new_write_manager.stop()

            refresh_calls: list[str] = []

            async def refresh_executor(job):
                refresh_calls.append(job.task_id)

            refresh_approved_root = root / "refresh-failure-approved"
            refresh_approved_root.mkdir()
            refresh_approved_path = _write_approved_task(
                refresh_approved_root, "refresh-failure-approved"
            )
            refresh_approved_before = refresh_approved_path.read_bytes()
            refresh_approved_manager = TaskQueueManager(
                task_root=refresh_approved_root,
                approved_executor=refresh_executor,
                execution_service=FakeExecutionService(),
                test_mode=False,
            )
            await refresh_approved_manager.start()
            with patch.object(
                refresh_approved_manager,
                "_refresh_queue_positions",
                side_effect=_sharing_error(5),
            ):
                with self.assertRaises(PermissionError):
                    await refresh_approved_manager.enqueue_approved(
                        "refresh-failure-approved", FINGERPRINT
                    )
            refresh_approved_pending = list(refresh_approved_manager._pending)
            refresh_approved_qsize = refresh_approved_manager._queue.qsize()
            refresh_approved_worker_alive = bool(
                refresh_approved_manager._worker_task is not None
                and not refresh_approved_manager._worker_task.done()
            )
            await refresh_approved_manager.stop()

            refresh_new_root = root / "refresh-failure-new"
            refresh_new_root.mkdir()
            refresh_new_manager = TaskQueueManager(
                task_root=refresh_new_root,
                approved_executor=refresh_executor,
                test_mode=False,
            )
            await refresh_new_manager.start()
            with patch.object(
                refresh_new_manager,
                "_refresh_queue_positions",
                side_effect=_sharing_error(32),
            ):
                with self.assertRaises(PermissionError):
                    await refresh_new_manager.enqueue(
                        Path("synthetic/refresh-new"),
                        task_id="refresh-failure-new",
                    )
            refresh_new_pending = list(refresh_new_manager._pending)
            refresh_new_qsize = refresh_new_manager._queue.qsize()
            refresh_new_worker_alive = bool(
                refresh_new_manager._worker_task is not None
                and not refresh_new_manager._worker_task.done()
            )
            await refresh_new_manager.stop()

            restart_root = root / "admitted-before-put-restart"
            restart_root.mkdir()
            admitted_snapshot = dict(approved_put_snapshots[0])
            restart_task_id = str(admitted_snapshot["uuid"])
            (restart_root / f"{restart_task_id}.json").write_text(
                json.dumps(admitted_snapshot), encoding="utf-8"
            )
            restarted_jobs: list[str] = []

            async def restarted_executor(job):
                restarted_jobs.append(job.task_id)

            restart_manager = TaskQueueManager(
                task_root=restart_root,
                approved_executor=restarted_executor,
                execution_service=FakeExecutionService(),
                test_mode=False,
            )
            await restart_manager.start()
            await asyncio.sleep(0)
            recovered_admission = json.loads(
                (restart_root / f"{restart_task_id}.json").read_text("utf-8")
            )
            restart_pending = list(restart_manager._pending)
            restart_qsize = restart_manager._queue.qsize()
            await restart_manager.stop()

            self.assertEqual(1, len(approved_put_snapshots))
            self.assertEqual(
                "admitted", admitted_snapshot.get("execution_phase")
            )
            self.assertEqual(
                "execute_approved", admitted_snapshot.get("queue_operation")
            )
            self.assertEqual(
                FINGERPRINT,
                admitted_snapshot.get("execution_requested_fingerprint"),
            )
            self.assertEqual(approved_before, existing_after)
            self.assertNotIn("approved-existing", existing_pending)
            self.assertNotIn("new-admission", fresh_pending)
            self.assertFalse(fresh_task_exists)
            self.assertIsNotNone(existing_write_error)
            self.assertEqual(write_failure_before, write_failure_path.read_bytes())
            self.assertNotIn("write-failure-approved", existing_write_pending)
            self.assertEqual(0, existing_write_qsize)
            self.assertTrue(existing_worker_alive)
            self.assertIsNotNone(new_write_error)
            self.assertFalse((new_write_root / "new-write-failure.json").exists())
            self.assertNotIn("new-write-failure", new_write_pending)
            self.assertEqual(0, new_write_qsize)
            self.assertTrue(new_worker_alive)
            self.assertEqual(
                refresh_approved_before, refresh_approved_path.read_bytes()
            )
            self.assertNotIn(
                "refresh-failure-approved", refresh_approved_pending
            )
            self.assertEqual(0, refresh_approved_qsize)
            self.assertTrue(refresh_approved_worker_alive)
            self.assertFalse(
                (refresh_new_root / "refresh-failure-new.json").exists()
            )
            self.assertNotIn("refresh-failure-new", refresh_new_pending)
            self.assertEqual(0, refresh_new_qsize)
            self.assertTrue(refresh_new_worker_alive)
            self.assertEqual([], refresh_calls)
            self.assertEqual("failed", recovered_admission.get("workflow_status"))
            self.assertEqual(
                "failed_before_media", recovered_admission.get("execution_phase")
            )
            self.assertFalse(
                recovered_admission.get("execution_requires_manual_check", False)
            )
            self.assertEqual(
                FINGERPRINT,
                recovered_admission.get("execution_requested_fingerprint"),
            )
            self.assertIsNone(recovered_admission.get("queue_position"))
            self.assertEqual([], restarted_jobs)
            self.assertEqual([], restart_pending)
            self.assertEqual(0, restart_qsize)

    async def test_restart_distinguishes_preparing_boundary_and_legacy_unknown(self):
        executed: list[str] = []

        async def approved_executor(job):
            executed.append(job.task_id)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tasks = {
                "preparing": {
                    "uuid": "preparing",
                    "workflow_status": "executing",
                    "queue_operation": "execute_approved",
                    "execution_phase": "preparing",
                    "execution_requested_fingerprint": FINGERPRINT,
                    "queue_position": 0,
                    "preview_only": False,
                },
                "boundary": {
                    "uuid": "boundary",
                    "workflow_status": "executing",
                    "queue_operation": "execute_approved",
                    "execution_phase": "media_boundary_entered",
                    "execution_requested_fingerprint": FINGERPRINT,
                    "execution_requires_manual_check": True,
                    "queue_position": 0,
                    "preview_only": False,
                },
                "legacy": {
                    "uuid": "legacy",
                    "workflow_status": "executing",
                    "queue_operation": "execute_approved",
                    "execution_requested_fingerprint": FINGERPRINT,
                    "queue_position": 0,
                    "preview_only": False,
                },
            }
            for task_id, payload in tasks.items():
                (root / f"{task_id}.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )

            manager = TaskQueueManager(
                task_root=root,
                approved_executor=approved_executor,
                test_mode=False,
            )
            await manager.start()
            await asyncio.sleep(0)
            recovered = {
                task_id: json.loads((root / f"{task_id}.json").read_text("utf-8"))
                for task_id in tasks
            }
            pending = list(manager._pending)
            qsize = manager._queue.qsize()
            await manager.stop()

            for task_id in ("preparing",):
                self.assertEqual("failed", recovered[task_id].get("workflow_status"))
                self.assertEqual(
                    "failed_before_media", recovered[task_id].get("execution_phase")
                )
                self.assertFalse(
                    recovered[task_id].get("execution_requires_manual_check", False)
                )
                self.assertEqual(
                    FINGERPRINT,
                    recovered[task_id].get("execution_requested_fingerprint"),
                )
                self.assertIsNone(recovered[task_id].get("queue_position"))
            self.assertEqual(
                "media_boundary_entered",
                recovered["boundary"].get("execution_phase"),
            )
            self.assertTrue(recovered["boundary"].get("execution_requires_manual_check"))
            self.assertTrue(recovered["legacy"].get("execution_requires_manual_check"))
            self.assertNotEqual(
                "failed_before_media", recovered["legacy"].get("execution_phase")
            )
            for task_id in ("boundary", "legacy"):
                self.assertEqual("failed", recovered[task_id].get("workflow_status"))
                self.assertEqual(
                    FINGERPRINT,
                    recovered[task_id].get("execution_requested_fingerprint"),
                )
                self.assertIsNone(recovered[task_id].get("queue_position"))
            self.assertEqual([], executed, "restart automatically replayed approved work")
            self.assertEqual([], pending)
            self.assertEqual(0, qsize)

    async def test_gui_pre_media_retry_is_exact_and_overrides_stale_feedback(self):
        task = {
            "uuid": "exact-retry",
            "path": "synthetic/source",
            "workflow_status": "failed",
            "queue_operation": "execute_approved",
            "execution_phase": "failed_before_media",
            "execution_requested_fingerprint": FINGERPRINT,
            "execution_requires_manual_check": False,
            "movie_inventory_reason": "MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION",
            "error": "internal storage failure",
        }
        rendered_buttons = []
        visible_strings: list[str] = []

        def record_button(label, *args, **kwargs):
            button = _ButtonStub()
            rendered_buttons.append((label, kwargs.get("on_click"), button))
            visible_strings.append(str(label))
            return button

        def record_text(value, *args, **kwargs):
            visible_strings.append(str(value))
            return _UiStub()

        with (
            patch.object(data_table_page, "get_task", return_value=task),
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
            patch.object(
                data_table_page.task_queue,
                "enqueue_approved",
                new=AsyncMock(),
            ) as enqueue_approved,
            patch.object(
                data_table_page.task_queue,
                "enqueue",
                new=AsyncMock(),
            ) as enqueue,
            patch.object(data_table_page, "notify"),
            patch.object(data_table_page, "refresh_dashboard"),
            patch.object(data_table_page._review_tasks, "refresh"),
            patch.object(data_table_page._history_tasks, "refresh"),
        ):
            data_table_page._task_card(task)
            retry_actions = [
                (callback, button)
                for label, callback, button in rendered_buttons
                if label == "重新执行"
            ]
            if len(retry_actions) == 1:
                result = retry_actions[0][0]()
                if inspect.isawaitable(result):
                    await result

        negative_cases = {
            "boundary": {
                **task,
                "uuid": "boundary",
                "execution_phase": "media_boundary_entered",
                "execution_requires_manual_check": True,
                "error": (
                    "SeriesCatalog ProviderLinkCatalog execution ledger "
                    "fingerprint revision schema_version canonical_root"
                ),
                "execution_recovery_message": (
                    "provider snapshot binding series_key associated asset "
                    "provider_evidence"
                ),
            },
            "manual": {
                **task,
                "uuid": "manual",
                "execution_requires_manual_check": True,
            },
            "legacy": {
                **task,
                "uuid": "legacy",
                "execution_phase": None,
            },
            "unknown": {
                **task,
                "uuid": "unknown",
                "execution_phase": "unknown",
            },
            "catalog": {
                **task,
                "uuid": "catalog",
                "workflow_status": "catalog_update_pending",
                "execution_phase": "media_boundary_entered",
                "execution_requires_manual_check": True,
                "error": (
                    "SeriesCatalog ProviderLinkCatalog execution_ledger "
                    "snapshot binding fingerprint schema_version"
                ),
                "execution_recovery_message": (
                    "ledger revision series_key canonical_root "
                    "associated asset provider_evidence"
                ),
            },
        }
        negative_labels: dict[str, list[str]] = {}
        for case_name, negative_task in negative_cases.items():
            labels: list[str] = []
            case_visible: list[str] = []
            case_buttons: list[_ButtonStub] = []

            def negative_button(label, *args, **kwargs):
                labels.append(str(label))
                case_visible.append(str(label))
                button = _ButtonStub()
                case_buttons.append(button)
                return button

            def negative_text(value, *args, **kwargs):
                case_visible.append(str(value))
                return _UiStub()

            with (
                patch.object(data_table_page, "get_task", return_value=negative_task),
                patch.object(
                    data_table_page.DecisionStateStore,
                    "load_optional",
                    return_value=None,
                ),
                patch.object(data_table_page, "_path_tree_button"),
                patch.object(data_table_page.ui, "card", return_value=_UiStub()),
                patch.object(data_table_page.ui, "row", return_value=_UiStub()),
                patch.object(data_table_page.ui, "column", return_value=_UiStub()),
                patch.object(data_table_page.ui, "badge", side_effect=negative_text),
                patch.object(data_table_page.ui, "label", side_effect=negative_text),
                patch.object(data_table_page.ui, "button", side_effect=negative_button),
            ):
                data_table_page._task_card(negative_task)
            for button in case_buttons:
                case_visible.extend(button.props_values)
                case_visible.extend(button.class_values)
                case_visible.extend(button.tooltip_values)
            visible_strings.extend(case_visible)
            negative_labels[case_name] = labels

        dialog_visible: list[str] = []
        dialog_task = negative_cases["boundary"]
        dialog_snapshot = SimpleNamespace(
            ledger_status="failed",
            entry_count=0,
            error=(
                "SeriesCatalog ProviderLinkCatalog schema_version canonical_root "
                "associated asset provider_evidence"
            ),
            plan_fingerprint=FINGERPRINT,
        )

        def dialog_text(value, *args, **kwargs):
            dialog_visible.append(str(value))
            return _UiStub()

        with (
            patch.object(data_table_page, "get_task", return_value=dialog_task),
            patch.object(
                data_table_page,
                "load_execution_recovery",
                return_value=dialog_snapshot,
            ),
            patch.object(data_table_page.ui, "dialog", return_value=_DialogStub()),
            patch.object(data_table_page.ui, "card", return_value=_UiStub()),
            patch.object(data_table_page.ui, "column", return_value=_UiStub()),
            patch.object(data_table_page.ui, "row", return_value=_UiStub()),
            patch.object(data_table_page.ui, "label", side_effect=dialog_text),
            patch.object(data_table_page.ui, "button", return_value=_ButtonStub()),
        ):
            await data_table_page._open_execution_recovery(dialog_task["uuid"])
        visible_strings.extend(dialog_visible)

        backend_calls: list[tuple[str, str]] = []

        async def approved_executor(job):
            backend_calls.append((job.task_id, str(job.expected_fingerprint)))

        class FakeExecutionService:
            validation_calls: list[tuple[str, str]] = []

            @classmethod
            def validate_approved(cls, task_id, fingerprint):
                cls.validation_calls.append((task_id, fingerprint))

        with tempfile.TemporaryDirectory() as temp_dir:
            backend_root = Path(temp_dir)
            backend_path = _write_approved_task(backend_root, "backend-retry")
            backend_task = json.loads(backend_path.read_text("utf-8"))
            backend_task.update(
                {
                    "workflow_status": "failed",
                    "queue_operation": "execute_approved",
                    "execution_phase": "failed_before_media",
                    "execution_requested_fingerprint": FINGERPRINT,
                    "execution_requires_manual_check": False,
                    "error": "structured pre-media failure",
                }
            )
            backend_path.write_text(json.dumps(backend_task), encoding="utf-8")
            backend_manager = TaskQueueManager(
                task_root=backend_root,
                approved_executor=approved_executor,
                execution_service=FakeExecutionService(),
                test_mode=False,
            )
            backend_error = None
            try:
                await backend_manager.start()
                await backend_manager.enqueue_approved("backend-retry", FINGERPRINT)
                await asyncio.wait_for(backend_manager._queue.join(), timeout=1)
            except PlanExecutionError as exc:
                backend_error = exc
            finally:
                await backend_manager.stop()

        self.assertEqual(
            "执行准备未完成，文件尚未处理。请稍后重新执行。",
            data_table_page.task_feedback_text(task),
        )
        self.assertEqual(1, len(retry_actions))
        self.assertEqual(
            ["重新执行"],
            [label for label, _callback, _button in rendered_buttons],
        )
        enqueue_approved.assert_awaited_once_with(task["uuid"], FINGERPRINT)
        enqueue.assert_not_awaited()
        self.assertIn(
            "unelevated color=green-7", retry_actions[0][1].props_values
        )
        for case_name, labels in negative_labels.items():
            self.assertEqual(["核查"], labels, case_name)
        for _label, _callback, button in rendered_buttons:
            visible_strings.extend(button.props_values)
            visible_strings.extend(button.class_values)
            visible_strings.extend(button.tooltip_values)
        visible = "\n".join(visible_strings).casefold()
        self.assertIn("历史执行记录无法完整读取，请谨慎核查目标文件。", dialog_visible)
        for internal_term in (
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
            self.assertNotIn(internal_term, visible)
        self.assertIsNone(backend_error)
        self.assertEqual(
            [("backend-retry", FINGERPRINT)], FakeExecutionService.validation_calls
        )
        self.assertEqual([("backend-retry", FINGERPRINT)], backend_calls)


if __name__ == "__main__":
    unittest.main()
