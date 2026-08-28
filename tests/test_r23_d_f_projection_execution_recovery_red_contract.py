"""R23 D/F contracts for projection, execution identity, and recovery."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.rename.decision_state import DecisionState, DecisionStateStore
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_store import PlanStore
from src.rename.projection_authority import (
    ProjectionAuthorityEnvelope,
    ProjectionAuthorityError,
    ProjectionCommitService,
    inventory_fingerprint_for_plan,
)
from src.rename.rename_plan import ExecutionPolicy, RenamePlan
from src.rename.trans import Trans
from src.task_queue import TaskQueueManager


def _plan(
    root: Path,
    task_id: str,
    *,
    create_source: bool = True,
) -> tuple[RenamePlan, Path]:
    source = root / "source" / "Episode 01.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    if create_source:
        source.write_bytes(b"frozen source")
    target_root = root / "target"
    plan = RenamePlan.from_mapping(
        task_id=task_id,
        source_root=source.parent,
        mapping={source: target_root / "Season 01" / "Fixture S01E01.mkv"},
        metadata={"target_root": str(target_root)},
        execution_policy=ExecutionPolicy(target_root=target_root),
    )
    return plan, source


def _envelope(plan: RenamePlan) -> ProjectionAuthorityEnvelope:
    return ProjectionAuthorityEnvelope(
        previous_decision=None,
        decision=DecisionState(task_id=plan.task_id),
        draft_plan=plan,
        inventory_fingerprint=inventory_fingerprint_for_plan(plan),
    )


def _service(root: Path) -> tuple[
    ProjectionCommitService, DecisionStateStore, PlanStore
]:
    state_store = DecisionStateStore(root / "decisions")
    plan_store = PlanStore(root / "plans")
    service = ProjectionCommitService(
        state_store,
        plan_store,
        journal_root=root / "journal",
        task_root=root / "tasks",
    )
    return service, state_store, plan_store


class ProjectionCompletenessIncidentRedContracts(unittest.TestCase):
    def test_d_missing_expected_source_blocks_before_first_authority_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, _source = _plan(root, "missing-source", create_source=False)
            service, state_store, plan_store = _service(root)
            with self.assertRaisesRegex(
                ProjectionAuthorityError, "source|来源|缺失|不存在"
            ):
                service.commit(_envelope(plan))
            self.assertIsNone(state_store.load_optional(plan.task_id))
            self.assertFalse(plan_store.path_for(plan.task_id).exists())
            self.assertIsNone(service.ownership_store.load_optional(plan.task_id))

    def test_d_ordinary_projected_source_is_revalidated_immediately_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            draft, source = _plan(root, "ordinary-drift")
            service, _state_store, _plan_store = _service(root)
            _decision, projected = service.commit(_envelope(draft))
            replacement = source.with_suffix(".replacement")
            replacement.write_bytes(b"different source identity and content")
            os.replace(replacement, source)
            with self.assertRaisesRegex(
                PlanExecutionError, "变化|漂移|identity|物理身份"
            ):
                PlanExecutionService._validate_execution_paths(projected)

    def test_control_complete_projection_writes_decision_plan_and_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, _source = _plan(root, "projection-control")
            service, state_store, plan_store = _service(root)
            decision, projected = service.commit(_envelope(plan))
            self.assertEqual(plan.task_id, decision.task_id)
            self.assertEqual(projected, plan_store.load(plan.task_id))
            self.assertIsNotNone(state_store.load_optional(plan.task_id))
            self.assertEqual(
                "active",
                service.ownership_store.load_optional(plan.task_id).state,
            )


class ProjectionBlindWindowIncidentRedContracts(unittest.TestCase):
    def test_f_restart_rolls_back_decision_written_before_journal_advance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, source = _plan(root, "decision-blind-window")
            before_hash = source.read_bytes()
            service, state_store, plan_store = _service(root)
            original = service._write_journal

            def crash_before_advance(
                task_id, phase, status, *, extra=None, **kwargs
            ):
                if phase == "decision_written":
                    raise KeyboardInterrupt("decision side effect without journal")
                return original(
                    task_id, phase, status, extra=extra, **kwargs
                )

            with patch.object(
                service, "_write_journal", side_effect=crash_before_advance
            ):
                with self.assertRaisesRegex(
                    KeyboardInterrupt, "decision side effect"
                ):
                    service.commit(_envelope(plan))

            restarted = ProjectionCommitService(
                state_store,
                plan_store,
                journal_root=root / "journal",
                task_root=root / "tasks",
            )
            self.assertEqual("failed_closed", restarted.recover(plan.task_id))
            self.assertIsNone(state_store.load_optional(plan.task_id))
            self.assertFalse(plan_store.path_for(plan.task_id).exists())
            self.assertEqual(before_hash, source.read_bytes())

    def test_f_restart_rolls_back_ownership_written_before_journal_advance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, source = _plan(root, "ownership-blind-window")
            before_hash = source.read_bytes()
            service, state_store, plan_store = _service(root)
            original = service._write_journal

            def crash_before_advance(
                task_id, phase, status, *, extra=None, **kwargs
            ):
                if phase == "ownership_claimed":
                    raise KeyboardInterrupt("ownership side effect without journal")
                return original(
                    task_id, phase, status, extra=extra, **kwargs
                )

            with patch.object(
                service, "_write_journal", side_effect=crash_before_advance
            ):
                with self.assertRaisesRegex(
                    KeyboardInterrupt, "ownership side effect"
                ):
                    service.commit(_envelope(plan))

            restarted = ProjectionCommitService(
                state_store,
                plan_store,
                journal_root=root / "journal",
                task_root=root / "tasks",
            )
            self.assertEqual("failed_closed", restarted.recover(plan.task_id))
            ownership = restarted.ownership_store.load_optional(plan.task_id)
            self.assertTrue(ownership is None or ownership.state != "active")
            self.assertIsNone(state_store.load_optional(plan.task_id))
            self.assertFalse(plan_store.path_for(plan.task_id).exists())
            self.assertEqual(before_hash, source.read_bytes())


class ExecutionIdentityAndIsolationIncidentRedContracts(unittest.TestCase):
    def test_d_zero_inode_cannot_be_recorded_as_proven_hardlink(self):
        source = MagicMock(spec=Path)
        target = MagicMock(spec=Path)
        source.stat.return_value = SimpleNamespace(
            st_dev=7, st_ino=0, st_nlink=2, st_size=10
        )
        target.stat.return_value = SimpleNamespace(
            st_dev=7, st_ino=0, st_nlink=2, st_size=10
        )
        target.is_symlink.return_value = False
        target.exists.return_value = True
        with self.assertRaisesRegex(
            OSError, "identity|inode|物理身份|unavailable|不可用"
        ):
            Trans._verify_result(source, target, "hardlink", {})

    def test_f_custom_queue_roots_do_not_construct_global_execution_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = TaskQueueManager(
                task_root=root / "tasks",
                decision_state_root=root / "decisions",
                recovery_abandon_audit_root=root / "abandon",
                plan_root=root / "plans",
                batch_scope_root=root / "batch",
                test_mode=True,
            )
            self.assertEqual(
                (root / "plans").resolve(),
                manager.execution_service.plan_store.root.resolve(),
            )
            self.assertNotEqual(
                manager.execution_service.plan_store.root.resolve(),
                Path(manager.execution_service.record_root).resolve(),
            )

    def test_control_real_local_hardlink_still_proves_positive_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
            target = root / "target.mkv"
            source.write_bytes(b"hardlink control")
            os.link(source, target)
            entry: dict[str, object] = {}
            Trans._verify_result(source, target, "hardlink", entry)
            self.assertEqual(source.stat().st_ino, target.stat().st_ino)
            self.assertGreaterEqual(int(entry["target_link_count"]), 2)


if __name__ == "__main__":
    unittest.main()
