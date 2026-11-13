"""ERR037 incident outcomes: real queue, isolated data, no Provider/media writes."""
import asyncio
from dataclasses import replace
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.rename.plan_execution import PlanExecutionError
from src.rename.process import Rename
from src.task_queue import TaskQueueManager
from src.utils.utils import merge_task_at, read_task_at, write_task_at
from tests.source_collection_fixture import admit_task


class RetryOutcomeContract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tasks = self.root / "task"
        self.source = self.root / "source.mkv"
        self.source.write_bytes(b"source-remains-unchanged")

    def manager(self, executor=None, **kwargs):
        result = TaskQueueManager(
            task_root=self.tasks, plan_root=self.root / "plan",
            decision_state_root=self.root / "decision_state",
            executor=executor,  **kwargs,
        )
        self.addAsyncCleanup(result.stop)
        return result

    async def drain(self, manager):
        await asyncio.wait_for(manager._queue.join(), timeout=3)

    async def test_real_error_reply_cannot_become_completed(self):
        async def executor(job):
            renamer = Rename(decision_state_root=self.root / "decision_state")
            renamer.active_provider_link_failure = None
            with patch("src.rename.process.TASK_PATH", self.tasks):
                return renamer.error_reply(
                    job.task_id, "subject_candidate_transport_error", self.source,
                    True, False,
                )
        manager = self.manager(executor)
        task_id = await admit_task(manager, self.source, is_anime=True, is_movie=False)
        await self.drain(manager)
        task = read_task_at(self.tasks, task_id)
        self.assertIn(task["workflow_status"], {"failed", "need_confirm", "invalid"})
        self.assertIn("subject_candidate_transport_error", task["error"])
        self.assertEqual(b"source-remains-unchanged", self.source.read_bytes())
        self.assertFalse(any((self.root / "record").glob("*.json")))

    async def test_noop_and_legacy_string_return_fail_closed(self):
        for index, outcome in enumerate((None, "provider_failure")):
            with self.subTest(outcome=outcome):
                async def executor(_job):
                    return outcome
                manager = self.manager(executor)
                source = self.root / f"noop-{index}.mkv"
                source.write_bytes(b"unique-noop-source")
                task_id = await admit_task(manager, source)
                await self.drain(manager)
                self.assertEqual("failed", read_task_at(self.tasks, task_id)["workflow_status"])
                await manager.stop()

    async def test_planning_cannot_publish_completed(self):
        async def executor(job):
            merge_task_at(self.tasks, job.task_id, {"workflow_status": "completed"})
        manager = self.manager(executor)
        task_id = await admit_task(manager, self.source)
        await self.drain(manager)
        self.assertEqual("failed", read_task_at(self.tasks, task_id)["workflow_status"])

    async def test_explicit_review_state_remains_pending(self):
        async def executor(job):
            merge_task_at(self.tasks, job.task_id, {"workflow_status": "need_confirm"})
        manager = self.manager(executor)
        task_id = await admit_task(manager, self.source)
        await self.drain(manager)
        task = read_task_at(self.tasks, task_id)
        self.assertEqual("need_confirm", task["workflow_status"])
        self.assertIsNone(task["completed_at"])

    async def test_legacy_text_preserves_explicit_pending_or_invalid_state(self):
        for index, status in enumerate(("need_confirm", "invalid")):
            with self.subTest(status=status):
                async def executor(job):
                    merge_task_at(self.tasks, job.task_id, {"workflow_status": status})
                    return "need_confirm" if status == "need_confirm" else "inventory_blocked"
                manager = self.manager(executor)
                source = self.root / f"pending-{index}.mkv"
                source.write_bytes(b"pending-control")
                task_id = await admit_task(manager, source)
                await self.drain(manager)
                self.assertEqual(status, read_task_at(self.tasks, task_id)["workflow_status"])
                await manager.stop()

    async def test_error_reply_preserves_prior_work_identity_and_plan(self):
        self.tasks.mkdir()
        task_id = "existing-selection"
        write_task_at(self.tasks, task_id, {
            "uuid": task_id, "path": str(self.source), "name": "Selected Work",
            "season_id": 1, "is_anime": True, "is_movie": False,
            "workflow_status": "running", "plan_fingerprint": "old-preview",
        })
        renamer = Rename(decision_state_root=self.root / "decision_state")
        renamer.active_provider_link_failure = None
        with patch("src.rename.process.TASK_PATH", self.tasks):
            renamer.error_reply(task_id, "transport_failed", self.source)
        task = read_task_at(self.tasks, task_id)
        self.assertEqual("Selected Work", task["name"])
        self.assertEqual(1, task["season_id"])
        self.assertIs(task["is_anime"], True)
        self.assertIs(task["is_movie"], False)
        self.assertEqual("old-preview", task["plan_fingerprint"])
        self.assertNotIn(task["workflow_status"], {"running", "completed"})

    async def admitted(self):
        async def executor(job):
            merge_task_at(self.tasks, job.task_id, {"workflow_status": "need_confirm"})
        manager = self.manager(executor)
        source = self.root / f"admitted-{len(list(self.root.glob('admitted-*.mkv')))}.mkv"
        source.write_bytes(b"unique-admitted-source")
        task_id = await admit_task(manager, source, is_anime=True, is_movie=False)
        await self.drain(manager)
        return manager, task_id

    async def test_retry_rejects_execution_history_before_any_mutation(self):
        cases = (
            {"execution_status": "completed"},
            {"execution_requires_manual_check": True},
            {"execution_may_have_written_targets": True},
            {"workflow_status": "catalog_update_pending"},
            {"execution_phase": "media_boundary_entered"},
        )
        for state in cases:
            with self.subTest(state=state):
                manager, task_id = await self.admitted()
                merge_task_at(self.tasks, task_id, state)
                before = read_task_at(self.tasks, task_id)
                with self.assertRaises(PlanExecutionError):
                    await manager.retry_current(task_id)
                self.assertEqual(before, read_task_at(self.tasks, task_id))
                await manager.stop()

    async def test_retry_resets_old_operation_and_timestamps(self):
        manager, task_id = await self.admitted()
        merge_task_at(self.tasks, task_id, {
            "queue_operation": "reidentify", "completed_at": "old", "started_at": "old",
        })
        await manager.retry_current(task_id)
        queued = read_task_at(self.tasks, task_id)
        self.assertEqual("rename", queued["queue_operation"])
        self.assertIsNone(queued["completed_at"])
        self.assertIsNone(queued["started_at"])
        await self.drain(manager)

    async def test_selected_retry_uses_canonical_reidentify_admission(self):
        from tests.test_default_activation_contract import DefaultActivationProducerContracts
        harness = DefaultActivationProducerContracts('runTest')
        async with harness.flow(movie=True) as flow:
            before = flow.manager._read(flow.task_id)
            with patch("src.rename.recognition_queue_admission.admit", new_callable=AsyncMock) as admit:
                admit.return_value = flow.task_id
                self.assertEqual(flow.task_id, await flow.manager.retry_current(flow.task_id))
                admit.assert_awaited_once_with(flow.manager, flow.task_id, mode='retry', request={'source': 'retry'})
            self.assertEqual(before, flow.manager._read(flow.task_id))

    async def test_approved_executor_cannot_synthesize_completion(self):
        class Service:
            def validate_approved(self, *_args):
                return SimpleNamespace(
                    execution_policy=SimpleNamespace(dry_run=False)
                )
        async def noop(_job):
            return None
        manager = self.manager(approved_executor=noop, execution_service=Service())
        self.tasks.mkdir(exist_ok=True)
        task_id = "approved-noop"
        write_task_at(self.tasks, task_id, {"uuid": task_id, "workflow_status": "approved"})
        await manager.enqueue_approved(task_id, "a" * 64)
        await self.drain(manager)
        self.assertEqual("failed", read_task_at(self.tasks, task_id)["workflow_status"])

    async def test_failed_selected_retry_does_not_restore_legacy_false_completion(self):
        from tests.test_default_activation_contract import DefaultActivationProducerContracts
        harness = DefaultActivationProducerContracts('runTest')
        async with harness.flow(movie=True, automatic=False) as flow:
            plan, state = harness.current(flow)
            merge_task_at(flow.manager.task_root, flow.task_id, {
                "workflow_status": "completed", "completed_at": "old-false-success",
                "provider_selection": {"namespace": "tmdb", "entity_type": "movie", "provider_id": "8181", "title": "普通电影"}})
            before = (flow.store.path_for(flow.task_id).read_bytes(),
                      flow.service.decision_state_store.path_for(flow.task_id).read_bytes())
            async def failed(job):
                with patch("src.rename.process.TASK_PATH", flow.manager.task_root):
                    return Rename(decision_state_root=flow.manager.decision_state_root).error_reply(
                        job.task_id, "transport_failed", flow.source)
            flow.manager.executor = failed
            await flow.manager.retry_current(flow.task_id)
            await asyncio.wait_for(flow.manager._queue.join(), timeout=12)
            task = flow.manager._read(flow.task_id)
            self.assertEqual("need_confirm", task["workflow_status"])
            self.assertIsNone(task["completed_at"])
            self.assertIn("transport_failed", task["error"])
            self.assertEqual(before, (flow.store.path_for(flow.task_id).read_bytes(),
                      flow.service.decision_state_store.path_for(flow.task_id).read_bytes()))

    async def test_workspace_retry_guard_runs_before_metadata_write(self):
        manager, task_id = await self.admitted()
        merge_task_at(self.tasks, task_id, {"execution_may_have_written_targets": True})
        before = read_task_at(self.tasks, task_id)
        with self.assertRaises(PlanExecutionError):
            await manager.reenqueue_workspace(
                task_id, Path(before["path"]), is_anime=False, is_movie=True,
                custom_name="Changed", custom_season_id=2,  preview_only=False,
            )
        self.assertEqual(before, read_task_at(self.tasks, task_id))

    async def test_real_selected_retry_regenerates_unapproved_policy_or_preserves_old_plan(self):
        import tests.test_provider_subject_plan_wiring_red_contract as fixture
        from tests.test_r24_phase_c_review_operation_migration_contract import current_provider_selection_context

        original_stop = TaskQueueManager.stop
        for failure in (False, True):
            with self.subTest(transport_failure=failure):
                root = self.root / ("failure-flow" if failure else "success-flow")
                source = fixture._make_episode_source(root / "source", "86", tuple(range(12, 24)))
                observations = {}

                async def retry_before_stop(manager):
                    task_id = next(manager.task_root.glob("*.json")).stem
                    old = manager.plan_store.load(task_id)
                    old_state = manager.decision_state_store.load(task_id)
                    old_bytes = (root / "flow" / "plans" / f"{task_id}.json").read_bytes()
                    before = read_task_at(manager.task_root, task_id)
                    observations["old_preview"] = old.execution_policy.dry_run
                    try:
                        with patch.dict(os.environ, {"BAR_DRY_RUN": "0"}):
                            from contextlib import nullcontext
                            fault = patch("src.rename.current_tv_refresh._run",
                                side_effect=RuntimeError("subject_candidate_transport_error")) if failure else nullcontext()
                            with fault as reached:
                                await manager.retry_current(task_id)
                                await asyncio.wait_for(manager._queue.join(), timeout=12)
                                if failure:
                                    reached.assert_called_once()
                        plan = manager.plan_store.load(task_id)
                        state = manager.decision_state_store.load(task_id)
                        task = read_task_at(manager.task_root, task_id)
                        self.assertEqual(before["task_source_scope"], task["task_source_scope"])
                        self.assertEqual(before["batch_id"], task["batch_id"])
                        self.assertNotEqual("completed", task["workflow_status"])
                        self.assertIsNone(task["completed_at"], (task["workflow_status"], task.get("error")))
                        context = task["current_tv_refresh"]
                        if failure:
                            self.assertEqual("failed", context["status"])
                            self.assertEqual(old_bytes, (root / "flow" / "plans" / f"{task_id}.json").read_bytes())
                            self.assertEqual(old_state.revision, state.revision)
                            self.assertIn("subject_candidate_transport_error", task["error"])
                        else:
                            self.assertEqual("completed", context["status"])
                            self.assertGreater(state.revision, old_state.revision)
                            self.assertEqual(replace(old.execution_policy, dry_run=False), plan.execution_policy)
                            self.assertEqual("invalid", task["workflow_status"])
                            from src.rename.plan_verifier import PlanVerifier
                            self.assertEqual(set(), {i.code for i in PlanVerifier().verify(plan).issues})
                            self.assertEqual("hardlink", plan.execution_policy.mode)
                            self.assertFalse(any(entry.target_path.exists() for entry in plan.entries))
                    finally:
                        await original_stop(manager)

                harness = fixture.ProviderSubjectPlanWiringRedContracts(methodName="runTest")
                with patch.dict(os.environ, {"BAR_DRY_RUN": "1"}), patch.object(TaskQueueManager, "stop", retry_before_stop):
                    result = await harness._run_real_add_select(
                        root=root / "flow", source=source,
                        subjects=fixture.EIGHTY_SIX_SUBJECTS,
                        relations=fixture.EIGHTY_SIX_RELATIONS,
                        selected_subject_id="331887",
                    )
                self.assertTrue(observations["old_preview"])
                self.assertTrue(result.source_unchanged)
                self.assertFalse(result.business_json)
                self.assertFalse(result.target_files)


if __name__ == "__main__":
    unittest.main()
