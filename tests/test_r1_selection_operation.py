from __future__ import annotations

import ast
import asyncio
import inspect
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from src.rename.decision_state import DecisionStateStore
from src.rename.plan_store import PlanStore
from src.rename.projection_authority import (
    RecognitionDecisionInput,
    commit_recognition_projection,
)
from src.rename.rename_plan import ProviderIdentity, RenamePlan, WorkIdentity
from src.task_queue import TaskJob, TaskQueueManager
from src.utils.utils import write_task_at
from tests.projection_test_support import save_plan_fixture


def _selection(provider_id: str = "86670") -> dict:
    return {
        "namespace": "bangumi",
        "entity_type": "subject",
        "provider_id": provider_id,
        "title": "向山进发 第二季",
        "poster_url": "https://lain.bgm.tv/pic/cover/l/test.jpg",
    }


def _authority_plan(root: Path, task_id: str, selection: dict) -> RenamePlan:
    provider = ProviderIdentity(
        status="confirmed",
        namespace=str(selection["namespace"]),
        entity_type=str(selection["entity_type"]),
        provider_id=str(selection["provider_id"]),
        title=str(selection.get("title") or "测试作品"),
        year=None,
        decision_source="explicit_user",
        poster_url=selection.get("poster_url"),
        source_url=selection.get("source_url"),
        snapshot_at=selection.get("snapshot_at"),
    )
    return RenamePlan(
        task_id=task_id,
        source_root=root,
        entries=(),
        work_identity=WorkIdentity(
            status="confirmed",
            domain="animation",
            title=provider.title or "测试作品",
            year=None,
            primary_provider=provider,
            domain_source="provider_selection",
        ),
    )


def _seed_authority(
    root: Path,
    task_id: str,
    authority_selection: dict | None = None,
) -> tuple[Path, Path, Path]:
    tasks = root / "tasks"
    decisions = root / "decisions"
    plans = root / "plans"
    source = root / "source.mkv"
    source.touch()
    state_store = DecisionStateStore(decisions)
    plan_store = PlanStore(plans)
    if authority_selection is None:
        save_plan_fixture(
            plan_store,
            RenamePlan(task_id=task_id, source_root=root, entries=()),
            state_store=state_store,
        )
    else:
        _commit_authority(
            state_store,
            plan_store,
            _authority_plan(root, task_id, authority_selection),
        )
    write_task_at(tasks, task_id, {
        "uuid": task_id,
        "path": str(source),
        "workflow_status": "need_confirm",
        "preview_only": False,
        "provider_selection": {"provider_id": "old"},
    })
    return tasks, decisions, plans


def _commit_authority(
    state_store: DecisionStateStore,
    plan_store: PlanStore,
    plan: RenamePlan,
):
    assert plan.work_identity is not None
    return commit_recognition_projection(
        state_store,
        plan_store,
        RecognitionDecisionInput(
            task_id=plan.task_id,
            work_identity=plan.work_identity,
        ),
        plan,
    )


class SelectionFailurePersistenceContracts(unittest.IsolatedAsyncioTestCase):
    async def test_worker_failure_persists_reason_and_preserves_old_authority(self):
        async def fail(_job):
            raise RuntimeError("无法读取季度关系")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "selection-failure"
            tasks, decisions, plans = _seed_authority(root, task_id)
            decision_path = decisions / f"{task_id}.json"
            plan_path = plans / f"{task_id}.json"
            decision_before = decision_path.read_bytes()
            plan_before = plan_path.read_bytes()
            manager = TaskQueueManager(
                task_root=tasks,
                decision_state_root=decisions,
                plan_root=plans,
                recovery_abandon_audit_root=root / "audit",
                executor=fail,
                test_mode=False,
            )
            await manager.start()
            try:
                await manager.enqueue_reidentify(task_id, _selection(), 0)
                await asyncio.wait_for(manager._queue.join(), timeout=3)
            finally:
                await manager.stop()
            task = json.loads((tasks / f"{task_id}.json").read_text("utf-8"))
            decision_after = decision_path.read_bytes()
            plan_after = plan_path.read_bytes()

        operation = task["selection_operation"]
        self.assertEqual("failed", operation["state"])
        self.assertEqual("provider_unavailable", operation["failure_code"])
        self.assertEqual({"provider_id": "old"}, task["provider_selection"])
        self.assertEqual(decision_before, decision_after)
        self.assertEqual(plan_before, plan_after)

    async def test_late_failure_cannot_overwrite_newer_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "selection-race"
            tasks, decisions, plans = _seed_authority(root, task_id)
            manager = TaskQueueManager(
                task_root=tasks,
                decision_state_root=decisions,
                plan_root=plans,
                recovery_abandon_audit_root=root / "audit",
                test_mode=True,
            )
            current = json.loads((tasks / f"{task_id}.json").read_text("utf-8"))
            current["selection_operation"] = {
                "request_id": "new-request",
                "state": "pending",
                "candidate": _selection("290980"),
            }
            write_task_at(tasks, task_id, current)
            manager._fail_reidentify(
                TaskJob(
                    task_id=task_id,
                    path=root / "source.mkv",
                    provider_selection=_selection(),
                    operation="reidentify",
                    expected_revision=0,
                    request_id="old-request",
                ),
                RuntimeError("旧请求失败"),
            )
            task = json.loads((tasks / f"{task_id}.json").read_text("utf-8"))

        self.assertEqual("new-request", task["selection_operation"]["request_id"])
        self.assertEqual("pending", task["selection_operation"]["state"])

    async def test_wait_timeout_returns_pending_without_repairing_committed_authority(self):
        selection = _selection()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "selection-read-only-timeout"
            tasks, decisions, plans = _seed_authority(
                root, task_id, authority_selection=selection
            )
            _commit_authority(
                DecisionStateStore(decisions),
                PlanStore(plans),
                _authority_plan(root, task_id, selection),
            )
            task = json.loads((tasks / f"{task_id}.json").read_text("utf-8"))
            task["selection_operation"] = {
                "request_id": "request",
                "state": "pending",
                "candidate": selection,
                "expected_decision_revision": 0,
            }
            write_task_at(tasks, task_id, task)
            manager = TaskQueueManager(
                task_root=tasks,
                decision_state_root=decisions,
                plan_root=plans,
                recovery_abandon_audit_root=root / "audit",
                test_mode=True,
            )
            task_path = tasks / f"{task_id}.json"
            task_before = task_path.read_bytes()
            journals_before = tuple(
                sorted(manager.projection_commit_service.journal_root.glob("*.json"))
            )
            operation = await manager.wait_for_selection_result(
                task_id, timeout=0.1
            )
            task_after = task_path.read_bytes()
            journals_after = tuple(
                sorted(manager.projection_commit_service.journal_root.glob("*.json"))
            )

        self.assertEqual("pending", operation.get("state"))
        self.assertEqual(task_before, task_after)
        self.assertEqual(journals_before, journals_after)


class SelectionRestartAndCommitContracts(unittest.TestCase):
    def test_restart_interrupts_only_pending_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / "tasks"
            tasks.mkdir()
            for task_id, operation_state in (("pending", "pending"), ("failed", "failed")):
                write_task_at(tasks, task_id, {
                    "uuid": task_id,
                    "path": str(root / task_id),
                    "workflow_status": "running",
                    "preview_only": False,
                    "selection_operation": {
                        "request_id": task_id,
                        "state": operation_state,
                        "failure_code": "selection_failed" if operation_state == "failed" else None,
                    },
                })
            manager = TaskQueueManager(
                task_root=tasks,
                decision_state_root=root / "decisions",
                plan_root=root / "plans",
                recovery_abandon_audit_root=root / "audit",
                test_mode=True,
            )
            manager._recover_active_records()
            pending = json.loads((tasks / "pending.json").read_text("utf-8"))
            failed = json.loads((tasks / "failed.json").read_text("utf-8"))

        self.assertEqual("interrupted", pending["selection_operation"]["state"])
        self.assertEqual("failed", failed["selection_operation"]["state"])

    def test_outcome_write_failure_does_not_rollback_committed_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "selection-outcome-fault"
            selection = _selection()
            tasks, decisions, plans = _seed_authority(
                root, task_id, authority_selection=selection
            )
            state_store = DecisionStateStore(decisions)
            plan_store = PlanStore(plans)
            _commit_authority(
                state_store,
                plan_store,
                _authority_plan(root, task_id, selection),
            )
            task = json.loads((tasks / f"{task_id}.json").read_text("utf-8"))
            task["selection_operation"] = {
                "request_id": "request",
                "state": "pending",
                "candidate": selection,
                "expected_decision_revision": 0,
            }
            write_task_at(tasks, task_id, task)
            manager = TaskQueueManager(
                task_root=tasks,
                decision_state_root=decisions,
                plan_root=plans,
                recovery_abandon_audit_root=root / "audit",
                test_mode=True,
            )
            decision_before = (decisions / f"{task_id}.json").read_bytes()
            plan_before = (plans / f"{task_id}.json").read_bytes()
            job = TaskJob(
                task_id=task_id,
                path=root / "source.mkv",
                provider_selection=selection,
                operation="reidentify",
                expected_revision=0,
                request_id="request",
            )
            with patch("src.task_queue.mutate_task_at", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    manager._complete_reidentify(job)
            manager._repair_selection_projection(task_id)
            repaired = json.loads((tasks / f"{task_id}.json").read_text("utf-8"))
            decision_after = (decisions / f"{task_id}.json").read_bytes()
            plan_after = (plans / f"{task_id}.json").read_bytes()

        self.assertEqual(decision_before, decision_after)
        self.assertEqual(plan_before, plan_after)
        self.assertEqual("succeeded", repaired["selection_operation"]["state"])


class SelectionStartupRepairContracts(unittest.IsolatedAsyncioTestCase):
    async def test_start_repairs_committed_authority_after_pending_journal_recovery(self):
        selection = _selection()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "selection-startup-outcome-repair"
            tasks, decisions, plans = _seed_authority(
                root, task_id, authority_selection=selection
            )
            _commit_authority(
                DecisionStateStore(decisions),
                PlanStore(plans),
                _authority_plan(root, task_id, selection),
            )
            task = json.loads((tasks / f"{task_id}.json").read_text("utf-8"))
            task["selection_operation"] = {
                "request_id": "request",
                "state": "pending",
                "candidate": selection,
                "expected_decision_revision": 0,
            }
            write_task_at(tasks, task_id, task)
            manager = TaskQueueManager(
                task_root=tasks,
                decision_state_root=decisions,
                plan_root=plans,
                recovery_abandon_audit_root=root / "audit",
                test_mode=True,
            )
            order: list[str] = []
            original_recover = manager.projection_commit_service.recover_pending
            original_repair = manager._repair_selection_projection

            def traced_recover():
                order.append("recover_pending")
                return original_recover()

            def traced_repair(repair_task_id):
                order.append(f"repair:{repair_task_id}")
                return original_repair(repair_task_id)

            with (
                patch.object(
                    manager.projection_commit_service,
                    "recover_pending",
                    side_effect=traced_recover,
                ),
                patch.object(
                    manager,
                    "_repair_selection_projection",
                    side_effect=traced_repair,
                ),
            ):
                await manager.start()
                await manager.stop()
            repaired = json.loads((tasks / f"{task_id}.json").read_text("utf-8"))

        self.assertEqual("recover_pending", order[0])
        self.assertIn(f"repair:{task_id}", order)
        self.assertLess(order.index("recover_pending"), order.index(f"repair:{task_id}"))
        self.assertEqual("succeeded", repaired["selection_operation"]["state"])


class SelectionWriterAllowlistContracts(unittest.TestCase):
    def test_live_waiter_has_no_repair_or_outcome_writer_path(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(TaskQueueManager)))
        callers = {
            "_repair_selection_projection": set(),
            "_complete_reidentify": set(),
        }
        class_node = next(
            node for node in tree.body if isinstance(node, ast.ClassDef)
        )
        for function in class_node.body:
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                if isinstance(target, ast.Attribute) and target.attr in callers:
                    callers[target.attr].add(function.name)

        self.assertEqual(
            {"_recover_active_records"},
            callers["_repair_selection_projection"],
        )
        self.assertEqual(
            {"_worker", "_repair_selection_projection"},
            callers["_complete_reidentify"],
        )


class SelectionAuthorityReadbackContracts(unittest.IsolatedAsyncioTestCase):
    async def test_same_provider_reselection_refreshes_poster_before_success(self):
        old = {
            "namespace": "tmdb", "entity_type": "movie", "provider_id": "763096",
            "title": "向山进发 回忆的礼物", "poster_url": "https://old.invalid/poster.jpg",
        }
        fresh = {
            **old,
            "poster_url": "https://image.tmdb.org/t/p/w342/fresh.jpg",
            "source_url": "https://www.themoviedb.org/movie/763096",
            "snapshot_at": "2026-08-20T06:30:00+08:00",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "selection-poster-refresh"
            tasks, decisions, plans = _seed_authority(
                root, task_id, authority_selection=old
            )

            async def commit_fresh(_job):
                _commit_authority(
                    DecisionStateStore(decisions),
                    PlanStore(plans),
                    _authority_plan(root, task_id, fresh),
                )

            manager = TaskQueueManager(
                task_root=tasks,
                decision_state_root=decisions,
                plan_root=plans,
                recovery_abandon_audit_root=root / "audit",
                executor=commit_fresh,
                test_mode=False,
            )
            await manager.start()
            try:
                await manager.enqueue_reidentify(task_id, fresh, 0)
                await asyncio.wait_for(manager._queue.join(), timeout=3)
            finally:
                await manager.stop()
            task = json.loads((tasks / f"{task_id}.json").read_text("utf-8"))
            state = DecisionStateStore(decisions).load(task_id)
            plan = PlanStore(plans).load(task_id)

        self.assertEqual("succeeded", task["selection_operation"]["state"])
        self.assertEqual(fresh["poster_url"], state.provider_evidence[0]["poster_url"])
        self.assertEqual(
            fresh["poster_url"], plan.work_identity.primary_provider.poster_url
        )

    async def test_revision_advance_with_wrong_provider_is_not_success(self):
        selected = _selection("86670")
        wrong = _selection("290980")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "selection-wrong-readback"
            tasks, decisions, plans = _seed_authority(root, task_id)

            async def commit_wrong(_job):
                _commit_authority(
                    DecisionStateStore(decisions),
                    PlanStore(plans),
                    _authority_plan(root, task_id, wrong),
                )

            manager = TaskQueueManager(
                task_root=tasks,
                decision_state_root=decisions,
                plan_root=plans,
                recovery_abandon_audit_root=root / "audit",
                executor=commit_wrong,
                test_mode=False,
            )
            await manager.start()
            try:
                await manager.enqueue_reidentify(task_id, selected, 0)
                await asyncio.wait_for(manager._queue.join(), timeout=3)
            finally:
                await manager.stop()
            task = json.loads((tasks / f"{task_id}.json").read_text("utf-8"))

        self.assertEqual("failed", task["selection_operation"]["state"])
        self.assertNotEqual(
            "succeeded", task["selection_operation"]["state"]
        )


if __name__ == "__main__":
    unittest.main()
