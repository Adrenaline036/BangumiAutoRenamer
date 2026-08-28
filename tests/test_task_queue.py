import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from src.rename.decision_state import DecisionStateStore
from src.task_queue import TaskQueueManager
from src.rename.plan_execution import PlanExecutionError
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import RenamePlan
from tests.projection_test_support import save_plan_fixture


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
            root = Path(temp_dir)
            decisions = root / "decisions"
            task_id = "concurrent-reidentify"
            self._write_reidentify_task(root, task_id)
            state = DecisionStateStore(decisions).create(task_id)
            manager = TaskQueueManager(
                task_root=root,
                decision_state_root=decisions,
                executor=executor,
                test_mode=False,
            )
            await manager.start()
            selection = {
                "namespace": "tmdb", "entity_type": "tv",
                "provider_id": "42", "title": "One Work",
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
            root = Path(temp_dir)
            decisions = root / "decisions"
            task_id = "stale-reidentify"
            self._write_reidentify_task(root, task_id)
            store = DecisionStateStore(decisions)
            state = store.create(task_id)
            manager = TaskQueueManager(
                task_root=root,
                decision_state_root=decisions,
                executor=executor,
                test_mode=False,
            )
            await manager.start()
            await manager.enqueue(Path("synthetic/blocker"), task_id="blocker")
            await asyncio.wait_for(blocker_started.wait(), timeout=1)
            await manager.enqueue_reidentify(
                task_id,
                {
                    "namespace": "tmdb", "entity_type": "tv",
                    "provider_id": "42", "title": "One Work",
                },
                state.revision,
            )
            store.save(state, expected_revision=state.revision)
            release_blocker.set()
            await asyncio.wait_for(manager._queue.join(), timeout=1)

            task = json.loads((root / f"{task_id}.json").read_text("utf-8"))
            self.assertEqual(["blocker"], executed)
            self.assertEqual("need_confirm", task["workflow_status"])
            self.assertEqual("failed", task["selection_operation"]["state"])
            self.assertEqual("stale_request", task["selection_operation"]["failure_code"])
            self.assertIn("内容已更新", task["error"])
            await manager.stop()

    async def test_restart_never_replays_persisted_reidentify_request(self):
        called = False

        async def executor(_job):
            nonlocal called
            called = True

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            decisions = root / "decisions"
            plans = root / "plans"
            task_id = "restart-reidentify"
            self._write_reidentify_task(root, task_id)
            task_path = root / f"{task_id}.json"
            task = json.loads(task_path.read_text("utf-8"))
            task.update({
                "workflow_status": "queued",
                "queue_operation": "reidentify",
                "reidentify_expected_revision": 0,
            })
            task_path.write_text(json.dumps(task), encoding="utf-8")
            state_path = DecisionStateStore(decisions).create(task_id)
            save_plan_fixture(PlanStore(plans), RenamePlan(
                task_id=task_id, source_root=root / "source", entries=(),
            ))
            decision_file = DecisionStateStore(decisions).path_for(task_id)
            plan_file = PlanStore(plans).path_for(task_id)
            preserved = (decision_file.read_bytes(), plan_file.read_bytes())

            manager = TaskQueueManager(
                task_root=root,
                decision_state_root=decisions,
                executor=executor,
                test_mode=False,
            )
            await manager.start()
            await asyncio.sleep(0)

            recovered = json.loads(task_path.read_text("utf-8"))
            self.assertFalse(called)
            self.assertEqual("interrupted", recovered["workflow_status"])
            self.assertEqual(preserved[0], decision_file.read_bytes())
            self.assertEqual(preserved[1], plan_file.read_bytes())
            self.assertEqual(0, state_path.revision)
            await manager.stop()

    async def test_explicitly_abandoned_task_id_cannot_be_resurrected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audit_root = root / "abandon_audit"
            audit_root.mkdir()
            task_id = "retired-recovery"
            (audit_root / f"{task_id}.{'a' * 64}.json").write_text(
                "{}", encoding="utf-8"
            )
            manager = TaskQueueManager(
                task_root=root / "tasks",
                recovery_abandon_audit_root=audit_root,
                test_mode=True,
            )

            with self.assertRaisesRegex(PlanExecutionError, "已经移除"):
                await manager.enqueue(
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

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = TaskQueueManager(
                task_root=root, executor=executor, test_mode=False
            )
            await manager.start()
            task_id = await manager.enqueue(Path("synthetic/source"))
            queued = json.loads((root / f"{task_id}.json").read_text("utf-8"))
            self.assertEqual(queued["workflow_status"], "queued")

            await asyncio.wait_for(started.wait(), timeout=1)
            running = json.loads((root / f"{task_id}.json").read_text("utf-8"))
            self.assertEqual(running["workflow_status"], "running")
            self.assertIsNotNone(running["started_at"])

            release.set()
            await asyncio.wait_for(manager._queue.join(), timeout=1)
            completed = json.loads((root / f"{task_id}.json").read_text("utf-8"))
            self.assertEqual(completed["workflow_status"], "completed")
            await manager.stop()

    async def test_test_mode_never_calls_executor(self):
        called = False

        async def executor(_job):
            nonlocal called
            called = True

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = TaskQueueManager(
                task_root=root, executor=executor, test_mode=True
            )
            task_id = await manager.enqueue(Path("GUI-PREVIEW/source"))
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
            root = Path(temp_dir)
            manager = TaskQueueManager(task_root=root, test_mode=True)
            task_id = await manager.enqueue(
                Path("GUI-PREVIEW/source"), provider_selection=selection
            )
            task = json.loads((root / f"{task_id}.json").read_text("utf-8"))

            self.assertEqual(task["provider_selection"], selection)
            await manager.stop()

    async def test_unsafe_custom_task_id_is_rejected_before_persistence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = TaskQueueManager(task_root=root, test_mode=True)
            with self.assertRaisesRegex(ValueError, "Unsafe task_id"):
                await manager.enqueue(
                    Path("synthetic/source"), task_id="../escape"
                )
            self.assertEqual(manager._pending, [])
            self.assertFalse((root.parent / "escape.json").exists())
            await manager.stop()

    async def test_executor_exception_becomes_visible_failed_state(self):
        async def executor(_job):
            raise RuntimeError("synthetic failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = TaskQueueManager(
                task_root=root, executor=executor, test_mode=False
            )
            await manager.start()
            task_id = await manager.enqueue(Path("synthetic/source"))
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
            root = Path(temp_dir)
            manager = TaskQueueManager(
                task_root=root, executor=executor, test_mode=False
            )
            await manager.start()
            task_id = await manager.enqueue(Path("synthetic/source"))
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
            root = Path(temp_dir)
            manager = TaskQueueManager(
                task_root=root, executor=executor, test_mode=False
            )
            await manager.start()
            first = await manager.enqueue(Path("synthetic/first"))
            await asyncio.wait_for(first_started.wait(), timeout=1)
            second = await manager.enqueue(Path("synthetic/second"))

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
            root = Path(temp_dir)
            manager = TaskQueueManager(
                task_root=root, executor=executor, test_mode=False
            )
            await manager.start()
            task_id = await manager.enqueue(Path("synthetic/source"))
            await asyncio.wait_for(started.wait(), timeout=1)
            (root / f"{task_id}.json").unlink()

            release.set()
            await asyncio.wait_for(manager._queue.join(), timeout=1)

            self.assertFalse((root / f"{task_id}.json").exists())
            await manager.stop()

    async def test_corrupt_or_mismatched_records_do_not_break_startup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "broken.json").write_text("{", encoding="utf-8")
            (root / "wrong.json").write_text(
                json.dumps(
                    {"uuid": "another", "workflow_status": "running"}
                ),
                encoding="utf-8",
            )

            manager = TaskQueueManager(task_root=root, test_mode=True)
            await manager.start()

            self.assertEqual((root / "broken.json").read_text("utf-8"), "{")
            wrong = json.loads((root / "wrong.json").read_text("utf-8"))
            self.assertEqual(wrong["workflow_status"], "running")
            await manager.stop()

    async def test_restart_marks_non_preview_active_tasks_interrupted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
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
            manager = TaskQueueManager(task_root=root, test_mode=True)
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

        async def approved_executor(job):
            self.assertIsNone(job.path)
            executed.append((job.task_id, job.expected_fingerprint))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
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
            manager = TaskQueueManager(
                task_root=root,
                approved_executor=approved_executor,
                execution_service=FakeExecutionService(),
                test_mode=False,
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
            manager = TaskQueueManager(
                task_root=Path(temp_dir), test_mode=True
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
            root = Path(temp_dir)
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
            manager = TaskQueueManager(
                task_root=root,
                execution_service=FakeExecutionService(),
                test_mode=False,
            )
            with self.assertRaisesRegex(PlanExecutionError, "预览任务"):
                await manager.enqueue_approved(task_id, "a" * 64)
            self.assertFalse(validated)
            self.assertEqual(manager._queue.qsize(), 0)
            await manager.stop()


if __name__ == "__main__":
    unittest.main()
