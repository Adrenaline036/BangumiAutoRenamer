from __future__ import annotations
from tests.queue_fixture import TaskQueueManager as ControlledQueue

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
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
    persist_provider_selection_attempt,
)
from tests.source_collection_fixture import admit_task


def _selection(provider_id: str = "86670") -> dict:
    return {
        "namespace": "bangumi",
        "entity_type": "subject",
        "provider_id": provider_id,
        "title": "向山进发 第二季",
        "poster_url": "https://lain.bgm.tv/pic/cover/l/test.jpg",
        "platform": "TV",
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
    selected_is_movie = (
        isinstance(authority_selection, dict)
        and authority_selection.get("namespace") == "tmdb"
        and authority_selection.get("entity_type") == "movie"
    )
    write_task_at(tasks, task_id, {
        "uuid": task_id,
        "path": str(source),
        "workflow_status": "need_confirm",
        "preview_only": False,
        "provider_selection": {"provider_id": "old"},
        "is_anime": True,
        "is_movie": selected_is_movie,
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
        from tests.test_default_activation_contract import DefaultActivationProducerContracts
        harness = DefaultActivationProducerContracts('runTest')
        async with harness.flow(movie=True, automatic=False) as flow:
            plan, state = harness.current(flow)
            plan_path, decision_path = flow.store.path_for(flow.task_id), flow.service.decision_state_store.path_for(flow.task_id)
            before = (plan_path.read_bytes(), decision_path.read_bytes())
            previous = flow.manager._read(flow.task_id).get('provider_selection')
            async def fail(_job):
                raise RuntimeError("无法读取季度关系")
            flow.manager.executor = fail
            from tests.test_provider_subject_plan_wiring_red_contract import _root_config_patch
            from tests.test_r1_real_media_production_red_contract import _configured_renamer
            with _root_config_patch(flow.manager.root_snapshot_source.observe().to_dict()), patch(
                    'src.task_queue.Rename', side_effect=lambda **_: _configured_renamer(flow.root)):
                await flow.manager.enqueue_reidentify(flow.task_id, {"namespace": "tmdb", "entity_type": "movie", "provider_id": "8181", "title": "普通电影"}, state.revision)
                await asyncio.wait_for(flow.manager._queue.join(), timeout=12)
            task = flow.manager._read(flow.task_id)
            operation = current_provider_selection_context(task)
            self.assertEqual("failed", operation["state"])
            self.assertEqual("provider_unavailable", operation["failure_code"])
            self.assertEqual(previous, task.get("provider_selection"))
            self.assertEqual(before, (plan_path.read_bytes(), decision_path.read_bytes()))
            self.assertFalse(any(e.target_path.exists() for e in plan.entries))

    async def test_late_failure_cannot_overwrite_newer_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "selection-race"
            tasks, decisions, plans = _seed_authority(root, task_id)
            manager = ControlledQueue(
                task_root=tasks,
                decision_state_root=decisions,
                plan_root=plans,
                recovery_abandon_audit_root=root / "audit",
                test_mode=True,
            )
            current = json.loads((tasks / f"{task_id}.json").read_text("utf-8"))
            persist_provider_selection_attempt(current, {
                "request_id": "new-request",
                "state": "pending",
                "candidate": _selection("290980"),
            })
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

        operation = current_provider_selection_context(task)
        self.assertEqual("new-request", operation["request_id"])
        self.assertEqual("pending", operation["state"])

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
            persist_provider_selection_attempt(task, {
                "request_id": "request",
                "state": "pending",
                "candidate": selection,
                "expected_decision_revision": 0,
            })
            write_task_at(tasks, task_id, task)
            manager = ControlledQueue(
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
                task = {
                    "uuid": task_id,
                    "path": str(root / task_id),
                    "workflow_status": "running",
                    "preview_only": False,
                }
                persist_provider_selection_attempt(task, {
                    "request_id": task_id,
                    "state": operation_state,
                    "failure_code": "selection_failed" if operation_state == "failed" else None,
                })
                write_task_at(tasks, task_id, task)
            manager = ControlledQueue(
                task_root=tasks,
                decision_state_root=root / "decisions",
                plan_root=root / "plans",
                recovery_abandon_audit_root=root / "audit",
                test_mode=True,
            )
            manager._recover_active_records()
            pending = json.loads((tasks / "pending.json").read_text("utf-8"))
            failed = json.loads((tasks / "failed.json").read_text("utf-8"))

        self.assertEqual(
            "interrupted", current_provider_selection_context(pending)["state"]
        )
        self.assertEqual(
            "failed", current_provider_selection_context(failed)["state"]
        )

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
            persist_provider_selection_attempt(task, {
                "request_id": "request",
                "state": "pending",
                "candidate": selection,
                "expected_decision_revision": 0,
            })
            write_task_at(tasks, task_id, task)
            manager = ControlledQueue(
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
        self.assertEqual(
            "succeeded", current_provider_selection_context(repaired)["state"]
        )


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
            persist_provider_selection_attempt(task, {
                "request_id": "request",
                "state": "pending",
                "candidate": selection,
                "expected_decision_revision": 0,
            })
            write_task_at(tasks, task_id, task)
            manager = ControlledQueue(
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
        self.assertEqual(
            "succeeded", current_provider_selection_context(repaired)["state"]
        )


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
        # R03 fallback, not an in-place poster refresh: end the unsupported
        # unexecuted card and select the same ID on a genuinely new card.
        from tests.test_default_activation_contract import DefaultActivationProducerContracts
        from tests.test_provider_subject_plan_wiring_red_contract import _root_config_patch, _source_manifest
        from tests.test_r1_real_media_production_red_contract import _configured_renamer
        from src.rename.plan_execution import PlanExecutionError
        harness = DefaultActivationProducerContracts('runTest')
        async with harness.flow(movie=True) as flow:
            old, state = harness.current(flow)
            source_before = _source_manifest(flow.source)
            old_bytes = (flow.store.path_for(flow.task_id).read_bytes(),
                flow.service.decision_state_store.path_for(flow.task_id).read_bytes())
            fresh = {**old.work_identity.primary_provider.to_dict(),
                'poster_url': 'https://image.tmdb.org/t/p/w342/fresh.jpg',
                'source_url': 'https://www.themoviedb.org/movie/8181',
                'snapshot_at': '2026-09-15T00:00:00+00:00'}
            with self.assertRaisesRegex(PlanExecutionError, '安全结束.*重新添加'):
                await flow.manager.enqueue_reidentify(flow.task_id, fresh, state.revision)
            self.assertEqual(old_bytes, (flow.store.path_for(flow.task_id).read_bytes(),
                flow.service.decision_state_store.path_for(flow.task_id).read_bytes()))
            await flow.manager.abandon_and_remove(flow.task_id,
                record_root=flow.manager.execution_service.record_root,
                plan_root=flow.store.root, review_root=flow.root / 'reviews')
            self.assertFalse((flow.manager.task_root / (flow.task_id + '.json')).exists())
            with self.assertRaises(PlanExecutionError):
                await flow.manager.enqueue_reidentify(flow.task_id, fresh, state.revision)
            new_id = flow.task_id + '-recreated'
            def worker(**_):
                renamer = _configured_renamer(flow.root)
                lookup = renamer.search.get_movie_info
                renamer.search.get_movie_info = lambda *_args: ('', None)
                renamer.search.get_movie_info_by_id = lambda _id: lookup('Ordinary Movie')
                return renamer
            with _root_config_patch(flow.manager.root_snapshot_source.observe().to_dict()), patch(
                    'src.task_queue.Rename', side_effect=worker):
                await admit_task(flow.manager, flow.source, task_id=new_id, is_anime=False,
                    is_movie=True, custom_name='Ordinary Movie')
                await asyncio.wait_for(flow.manager._queue.join(), timeout=12)
                initial = flow.service.decision_state_store.load(new_id)
                self.assertFalse(initial.confirmed_work)
                await flow.manager.enqueue_reidentify(new_id, fresh, initial.revision)
                await asyncio.wait_for(flow.manager._queue.join(), timeout=12)
            task = flow.manager._read(new_id)
            plan = flow.store.load(new_id)
            current = flow.service.decision_state_store.load(new_id)
            self.assertEqual("succeeded", current_provider_selection_context(task)["state"], task.get("error"))
            self.assertEqual(fresh["provider_id"], plan.work_identity.primary_provider.provider_id)
            self.assertEqual(fresh["poster_url"], current.provider_evidence[0]["poster_url"])
            self.assertEqual(fresh["poster_url"], plan.work_identity.primary_provider.poster_url)
            self.assertFalse(flow.service.load_or_create(plan).approved_entry_ids)
            self.assertEqual(source_before, _source_manifest(flow.source))
            self.assertFalse(any(e.target_path.exists() for e in plan.entries))
            self.assertFalse(tuple(flow.manager.execution_service.record_root.glob('*.ledger.json')))

    async def test_revision_advance_with_wrong_provider_is_not_success(self):
        # Real successful first selection followed by a mismatched completion
        # request: a later revision alone is not proof of the requested identity.
        from dataclasses import replace
        from tests.test_default_activation_contract import DefaultActivationProducerContracts
        harness = DefaultActivationProducerContracts('runTest')
        async with harness.flow(movie=True, automatic=False) as flow:
            def protected():
                return {str(p.relative_to(flow.root)): p.read_bytes() for p in flow.root.rglob('*')
                        if p.is_file() and p.suffix != '.lock'}
            complete = flow.manager._complete_reidentify
            with patch.object(flow.manager, '_complete_reidentify', wraps=complete) as observed:
                plan, state = await harness.selected(flow)
            self.assertTrue(observed.call_args_list)
            job = observed.call_args_list[-1].args[0]
            self.assertGreater(state.revision, job.expected_revision)
            self.assertEqual('8181', plan.work_identity.primary_provider.provider_id)
            self.assertEqual('succeeded',
                current_provider_selection_context(flow.manager._read(flow.task_id))['state'])
            wrong = replace(job, provider_selection={**job.provider_selection, 'provider_id': '99999'})
            before = protected()
            from src.rename.plan_execution import PlanExecutionError
            with self.assertRaisesRegex(PlanExecutionError, '保存结果与本次选择不一致'):
                complete(wrong)
            self.assertEqual(before, protected())
            complete(job)
            self.assertEqual(before, protected(), 'actual successful completion must remain idempotent')
            self.assertFalse(flow.service.load_or_create(plan).approved_entry_ids)
            self.assertFalse(any(e.target_path.exists() for e in plan.entries))


if __name__ == "__main__":
    unittest.main()
