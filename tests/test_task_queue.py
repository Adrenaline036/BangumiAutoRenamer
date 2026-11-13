from tests.queue_fixture import TaskQueueManager as ControlledQueue
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.rename.decision_state import DecisionState, DecisionStateStore
from src.rename.content_policy import ContentPolicy
from src.task_queue import TaskQueueManager
from src.rename.plan_execution import PlanExecutionError
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import RenamePlan
from tests.projection_test_support import save_plan_fixture
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
)
from tests.source_collection_fixture import admit_task


def _source_file(root: Path, name: str) -> Path:
    path = root / "sources" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"queue-source")
    return path


def _isolated_manager(*, task_root: Path, **kwargs) -> TaskQueueManager:
    """Keep every real queue store under the case's disposable workspace."""
    kwargs.setdefault("plan_root", task_root / "plans")
    kwargs.setdefault("decision_state_root", task_root / "decisions")
    kwargs.setdefault("provider_link_catalog_root", task_root / "provider_links")
    kwargs.setdefault("recovery_abandon_audit_root", task_root / "abandon_audit")
    kwargs.setdefault("batch_scope_root", task_root / "task_batch_scope")
    if kwargs.pop("test_mode", False):
        return ControlledQueue(task_root=task_root, test_mode=True, **kwargs)
    return TaskQueueManager(task_root=task_root, **kwargs)


def _pending_workspace(manager, root, task_id):
    from src.rename.default_activation import ensure_workspace
    from src.rename.target_routing import TargetRootSnapshot, ROOT_KEYS
    roots = TargetRootSnapshot.capture({key: root/'targets'/key for key in ROOT_KEYS})
    from src.rename.plan_review import PlanReviewService
    service = PlanReviewService(manager.plan_store, root/'reviews', root/'catalog',
        task_root=manager.task_root, decision_state_root=manager.decision_state_root, record_root=root/'records')
    state, _ = ensure_workspace(service, task_id, roots=roots)
    return state


class TaskQueueTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _write_reidentify_task(root: Path, task_id: str) -> None:
        (root / f"{task_id}.json").write_text(
            json.dumps({
                "uuid": task_id,
                "path": "synthetic/source.mkv",
                "is_anime": True,
                "is_movie": False,
                "use_ai": False,
                "season_id": None,
                "workflow_status": "need_confirm",
                "preview_only": False,
            }),
            encoding="utf-8",
        )

    async def test_concurrent_reidentify_accepts_exactly_one_request(self):
        executed = []

        async def executor(job):
            executed.append(job.task_id)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            decisions = root / "decisions"
            task_id = "concurrent-reidentify"
            manager = _isolated_manager(
                task_root=root,
                decision_state_root=decisions,
                executor=executor,

            )
            await manager.start()
            await admit_task(manager, _source_file(root, 'selected.mkv'), task_id=task_id,
                                  is_anime=True, is_movie=False)
            await asyncio.wait_for(manager._queue.join(), timeout=3)
            executed.clear()
            state = _pending_workspace(manager, root, task_id)
            selection = {
                "namespace": "bangumi", "entity_type": "subject",
                "provider_id": "42", "title": "One Work", "platform": "TV",
            }

            results = await asyncio.gather(
                manager.enqueue_reidentify(task_id, selection, state.revision),
                manager.enqueue_reidentify(task_id, selection, state.revision),
                return_exceptions=True,
            )
            await asyncio.wait_for(manager._queue.join(), timeout=1)

            self.assertEqual(1, sum(result == task_id for result in results))
            self.assertEqual(1, sum(isinstance(result, PlanExecutionError) for result in results))
            self.assertEqual([task_id], executed)
            await manager.stop()

    async def test_reidentify_rechecks_revision_at_worker_boundary(self):
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()
        executed = []

        async def executor(job):
            executed.append(job.task_id)
            if job.task_id == "blocker":
                blocker_started.set()
                await release_blocker.wait()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            decisions = root / "decisions"
            task_id = "stale-reidentify"
            store = DecisionStateStore(decisions)
            manager = _isolated_manager(
                task_root=root,
                decision_state_root=decisions,
                executor=executor,

            )
            await manager.start()
            await admit_task(manager, _source_file(root, 'selected.mkv'), task_id=task_id,
                                  is_anime=True, is_movie=False)
            await asyncio.wait_for(manager._queue.join(), timeout=3)
            executed.clear()
            state = _pending_workspace(manager, root, task_id)
            previous_status = manager._read(task_id)["workflow_status"]
            await admit_task(manager, _source_file(root, "blocker.mkv"), task_id="blocker")
            await asyncio.wait_for(blocker_started.wait(), timeout=1)
            await manager.enqueue_reidentify(
                task_id,
                {
                    "namespace": "bangumi", "entity_type": "subject",
                    "provider_id": "42", "title": "One Work", "platform": "TV",
                },
                state.revision,
            )
            store.save(state, expected_revision=state.revision)
            release_blocker.set()
            await asyncio.wait_for(manager._queue.join(), timeout=1)

            task = json.loads((root / f"{task_id}.json").read_text("utf-8"))
            self.assertEqual(["blocker"], executed)
            self.assertEqual(previous_status, task["workflow_status"])
            operation = current_provider_selection_context(task)
            self.assertEqual("failed", operation["state"])
            self.assertEqual("stale_request", operation["failure_code"])
            self.assertIn("内容已更新", task["error"])
            await manager.stop()

    async def test_restart_never_replays_persisted_reidentify_request(self):
        from tests.test_content_choice_contract import migrated
        called = False
        async def executor(_job):
            nonlocal called
            called = True
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, _videos = migrated(root)
            task_id = plan.task_id
            task_path = service.task_root / f'{task_id}.json'
            task = json.loads(task_path.read_text('utf-8'))
            task.update(workflow_status='queued', queue_operation='reidentify',
                        reidentify_expected_revision=service.decision_state_store.load(task_id).revision)
            task_path.write_text(json.dumps(task), encoding='utf-8')
            files = (service.decision_state_store.path_for(task_id), service.plan_store.path_for(task_id))
            preserved = tuple(path.read_bytes() for path in files)
            manager = _isolated_manager(task_root=service.task_root, plan_root=service.plan_store.root,
                decision_state_root=service.decision_state_store.root, executor=executor)
            try:
                await manager.start()
                await asyncio.sleep(0)
                recovered = json.loads(task_path.read_text('utf-8'))
                self.assertFalse(called)
                self.assertEqual('interrupted', recovered['workflow_status'])
                self.assertEqual(preserved, tuple(path.read_bytes() for path in files))
            finally:
                await manager.stop()

    async def test_explicitly_abandoned_task_id_cannot_be_resurrected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            audit_root = root / "abandon_audit"
            audit_root.mkdir()
            task_id = "retired-recovery"
            (audit_root / f"{task_id}.{'a' * 64}.json").write_text(
                "{}", encoding="utf-8"
            )
            manager = _isolated_manager(
                task_root=root / "tasks",
                recovery_abandon_audit_root=audit_root,
                test_mode=True,
            )

            with self.assertRaisesRegex(PlanExecutionError, "已结束任务"):
                await admit_task(manager,
                    Path("synthetic/source"), task_id=task_id
                )

            self.assertFalse((root / "tasks" / f"{task_id}.json").exists())
            await manager.stop()

    async def test_enqueue_is_persisted_before_executor_finishes(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def executor(_job):
            started.set()
            await release.wait()
            manager._update(_job.task_id, workflow_status="need_confirm")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            manager = _isolated_manager(
                task_root=root, executor=executor
            )
            await manager.start()
            task_id = await admit_task(manager, _source_file(root, "source.mkv"))
            queued = json.loads((root / f"{task_id}.json").read_text("utf-8"))
            self.assertEqual(queued["workflow_status"], "queued")

            await asyncio.wait_for(started.wait(), timeout=1)
            running = json.loads((root / f"{task_id}.json").read_text("utf-8"))
            self.assertEqual(running["workflow_status"], "running")
            self.assertIsNotNone(running["started_at"])

            release.set()
            await asyncio.wait_for(manager._queue.join(), timeout=1)
            completed = json.loads((root / f"{task_id}.json").read_text("utf-8"))
            self.assertEqual(completed["workflow_status"], "need_confirm")
            await manager.stop()

    async def test_test_mode_never_calls_executor(self):
        called = False

        async def executor(_job):
            nonlocal called
            called = True

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            manager = _isolated_manager(
                task_root=root, executor=executor, test_mode=True
            )
            task_id = await admit_task(manager, Path("GUI-PREVIEW/source"))
            await asyncio.sleep(0)
            task = json.loads((root / f"{task_id}.json").read_text("utf-8"))
            self.assertFalse(called)
            self.assertTrue(task["preview_only"])
            self.assertEqual(task["workflow_status"], "queued")
            await manager.stop()

    async def test_selected_provider_identity_is_persisted_with_preview_job(self):
        selection = {
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": "330299",
            "title": "Synthetic TV Work",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            manager = _isolated_manager(task_root=root, test_mode=True)
            task_id = await admit_task(manager,
                Path("GUI-PREVIEW/source"), provider_selection=selection
            )
            task = json.loads((root / f"{task_id}.json").read_text("utf-8"))

            self.assertEqual(task["provider_selection"], selection)
            await manager.stop()

    async def test_unsafe_custom_task_id_is_rejected_before_persistence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            manager = _isolated_manager(task_root=root, test_mode=True)
            with self.assertRaisesRegex(ValueError, "Unsafe task_id"):
                await admit_task(manager,
                    Path("synthetic/source"), task_id="../escape"
                )
            self.assertEqual(manager._pending, [])
            self.assertFalse((root.parent / "escape.json").exists())
            await manager.stop()

    async def test_executor_exception_becomes_visible_failed_state(self):
        async def executor(_job):
            raise RuntimeError("synthetic failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            manager = _isolated_manager(
                task_root=root, executor=executor
            )
            await manager.start()
            task_id = await admit_task(manager, _source_file(root, "source.mkv"))
            await asyncio.wait_for(manager._queue.join(), timeout=1)
            task = json.loads((root / f"{task_id}.json").read_text("utf-8"))
            self.assertEqual(task["workflow_status"], "failed")
            self.assertIn("synthetic failure", task["error"])
            await manager.stop()

    async def test_need_confirm_is_paused_without_completed_timestamp(self):
        async def executor(job):
            task = json.loads(
                (root / f"{job.task_id}.json").read_text("utf-8")
            )
            task["workflow_status"] = "need_confirm"
            (root / f"{job.task_id}.json").write_text(
                json.dumps(task), encoding="utf-8"
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            manager = _isolated_manager(
                task_root=root, executor=executor
            )
            await manager.start()
            task_id = await admit_task(manager, _source_file(root, "source.mkv"))
            await asyncio.wait_for(manager._queue.join(), timeout=1)

            task = json.loads((root / f"{task_id}.json").read_text("utf-8"))
            self.assertEqual(task["workflow_status"], "need_confirm")
            self.assertIsNone(task["completed_at"])
            self.assertIsNotNone(task["review_requested_at"])
            await manager.stop()

    async def test_cancelled_queued_job_is_never_executed_or_recreated(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        executed = []

        async def executor(job):
            executed.append(job.task_id)
            first_started.set()
            await release_first.wait()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            manager = _isolated_manager(
                task_root=root, executor=executor
            )
            await manager.start()
            first = await admit_task(manager, _source_file(root, "first.mkv"))
            await asyncio.wait_for(first_started.wait(), timeout=1)
            second = await admit_task(manager, _source_file(root, "second.mkv"))

            self.assertTrue(manager.cancel_queued(second))
            (root / f"{second}.json").unlink()
            release_first.set()
            await asyncio.wait_for(manager._queue.join(), timeout=1)

            self.assertEqual(executed, [first])
            self.assertFalse((root / f"{second}.json").exists())
            await manager.stop()

    async def test_deleted_running_record_is_not_recreated_on_completion(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def executor(_job):
            started.set()
            await release.wait()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            manager = _isolated_manager(
                task_root=root, executor=executor
            )
            await manager.start()
            task_id = await admit_task(manager, _source_file(root, "source.mkv"))
            await asyncio.wait_for(started.wait(), timeout=1)
            (root / f"{task_id}.json").unlink()

            release.set()
            await asyncio.wait_for(manager._queue.join(), timeout=1)

            self.assertFalse((root / f"{task_id}.json").exists())
            await manager.stop()

    async def test_corrupt_or_mismatched_records_do_not_break_startup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            (root / "broken.json").write_text("{", encoding="utf-8")
            (root / "wrong.json").write_text(
                json.dumps(
                    {"uuid": "another", "workflow_status": "running"}
                ),
                encoding="utf-8",
            )

            manager = _isolated_manager(task_root=root, test_mode=True)
            await manager.start()

            self.assertEqual((root / "broken.json").read_text("utf-8"), "{")
            wrong = json.loads((root / "wrong.json").read_text("utf-8"))
            self.assertEqual(wrong["workflow_status"], "running")
            await manager.stop()

    async def test_restart_marks_non_preview_active_tasks_interrupted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            (root / "stale.json").write_text(
                json.dumps(
                    {
                        "uuid": "stale",
                        "workflow_status": "running",
                        "preview_only": False,
                    }
                ),
                encoding="utf-8",
            )
            manager = _isolated_manager(task_root=root, plan_root=root.parent / "plans",
                decision_state_root=root.parent / "decisions", test_mode=True)
            await manager.start()
            task = json.loads((root / "stale.json").read_text("utf-8"))
            self.assertEqual(task["workflow_status"], "interrupted")
            self.assertIn("应用重启", task["error"])
            await manager.stop()

    async def test_approved_plan_is_version_checked_then_uses_single_worker(self):
        validated = []
        executed = []

        class FakeExecutionService:
            def validate_approved(self, task_id, fingerprint):
                validated.append((task_id, fingerprint))
                return SimpleNamespace(
                    execution_policy=SimpleNamespace(dry_run=False)
                )

        async def approved_executor(job):
            self.assertIsNone(job.path)
            executed.append((job.task_id, job.expected_fingerprint))
            # The execution service owns this state; the queue must not invent it.
            manager._update(
                job.task_id, workflow_status="completed", execution_status="completed",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            task_id = "approved-task"
            fingerprint = "a" * 64
            (root / f"{task_id}.json").write_text(
                json.dumps(
                    {
                        "uuid": task_id,
                        "path": "synthetic/source",
                        "workflow_status": "approved",
                        "completed_at": None,
                    }
                ),
                encoding="utf-8",
            )
            manager = _isolated_manager(
                task_root=root,
                approved_executor=approved_executor,
                execution_service=FakeExecutionService(),

            )
            await manager.enqueue_approved(task_id, fingerprint)
            queued = json.loads(
                (root / f"{task_id}.json").read_text("utf-8")
            )
            self.assertEqual(queued["workflow_status"], "queued")
            self.assertEqual(queued["queue_operation"], "execute_approved")

            await asyncio.wait_for(manager._queue.join(), timeout=1)
            self.assertEqual(validated, [(task_id, fingerprint)])
            self.assertEqual(executed, [(task_id, fingerprint)])
            completed = json.loads(
                (root / f"{task_id}.json").read_text("utf-8")
            )
            self.assertEqual(completed["workflow_status"], "completed")
            await manager.stop()

    async def test_test_mode_rejects_approved_execution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = _isolated_manager(
                task_root=Path(temp_dir) / "tasks", test_mode=True
            )
            with self.assertRaisesRegex(PlanExecutionError, "测试模式"):
                await manager.enqueue_approved("task", "a" * 64)

    async def test_preview_record_cannot_enter_approved_execution_queue(self):
        validated = False

        class FakeExecutionService:
            def validate_approved(self, _task_id, _fingerprint):
                nonlocal validated
                validated = True

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            task_id = "preview-approved"
            (root / f"{task_id}.json").write_text(
                json.dumps(
                    {
                        "uuid": task_id,
                        "path": "GUI-PREVIEW/source",
                        "workflow_status": "approved",
                        "preview_only": True,
                    }
                ),
                encoding="utf-8",
            )
            manager = _isolated_manager(
                task_root=root,
                execution_service=FakeExecutionService(),

            )
            with self.assertRaisesRegex(PlanExecutionError, "预览任务"):
                await manager.enqueue_approved(task_id, "a" * 64)
            self.assertFalse(validated)
            self.assertEqual(manager._queue.qsize(), 0)
            await manager.stop()

    async def test_approved_dry_run_plan_cannot_enter_execution_queue(self):
        validated = []

        class FakeExecutionService:
            def validate_approved(self, task_id, fingerprint):
                validated.append((task_id, fingerprint))
                return SimpleNamespace(
                    execution_policy=SimpleNamespace(dry_run=True)
                )

        async def must_not_execute(_job):
            self.fail("dry-run approved Plan reached the worker")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            task_id = "old-plan-only-approved"
            fingerprint = "b" * 64
            task_path = root / f"{task_id}.json"
            task_path.write_text(
                json.dumps({
                    "uuid": task_id,
                    "path": "synthetic/source",
                    "workflow_status": "approved",
                    "completed_at": None,
                }),
                encoding="utf-8",
            )
            manager = _isolated_manager(
                task_root=root,
                approved_executor=must_not_execute,
                execution_service=FakeExecutionService(),

            )
            try:
                with self.assertRaisesRegex(
                    PlanExecutionError, "旧预演计划|重试生成真实计划"
                ):
                    await manager.enqueue_approved(task_id, fingerprint)
                self.assertEqual(validated, [(task_id, fingerprint)])
                self.assertEqual(manager._queue.qsize(), 0)
                self.assertFalse(manager._started)
                self.assertIsNone(manager._worker_task)
                persisted = json.loads(task_path.read_text("utf-8"))
                self.assertEqual(persisted["workflow_status"], "approved")
                self.assertNotIn("queue_operation", persisted)
            finally:
                await manager.stop()

    def test_queue_uses_the_canonical_approved_execution_policy_gate(self):
        import inspect

        source = inspect.getsource(TaskQueueManager._enqueue_execution)
        self.assertIn("require_approved_execution_policy", source)
        self.assertLess(
            source.index("require_approved_execution_policy"),
            source.index("await self.start()"),
        )

    async def test_queue_fails_closed_when_canonical_policy_gate_is_disconnected(self):
        class FakeExecutionService:
            def validate_approved(self, _task_id, _fingerprint):
                return SimpleNamespace(
                    execution_policy=SimpleNamespace(dry_run=False)
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            root.mkdir()
            task_id = "approved-gate-disconnected"
            fingerprint = "c" * 64
            task_path = root / f"{task_id}.json"
            task_path.write_text(
                json.dumps({
                    "uuid": task_id,
                    "path": "synthetic/source",
                    "workflow_status": "approved",
                    "completed_at": None,
                }),
                encoding="utf-8",
            )
            before = task_path.read_bytes()
            manager = _isolated_manager(
                task_root=root,
                execution_service=FakeExecutionService(),

            )
            disconnected = PlanExecutionError(
                "执行策略权威门已断开",
                code="execution_policy_gate_disconnected",
            )
            try:
                from unittest.mock import patch

                with patch(
                    "src.rename.plan_execution.require_approved_execution_policy",
                    create=True,
                    side_effect=disconnected,
                ) as gate:
                    with self.assertRaises(PlanExecutionError) as raised:
                        await manager.enqueue_approved(task_id, fingerprint)
                self.assertIs(raised.exception, disconnected)
                gate.assert_called_once()
                self.assertFalse(manager._started)
                self.assertIsNone(manager._worker_task)
                self.assertEqual(manager._queue.qsize(), 0)
                self.assertEqual(before, task_path.read_bytes())
            finally:
                await manager.stop()


if __name__ == "__main__":
    unittest.main()
