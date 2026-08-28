"""Phase C contracts for full projection-entry migration and task recovery.

The tests keep all state below ``TemporaryDirectory``.  They deliberately
exercise persisted task/Decision/ownership/Plan state and startup recovery;
no provider, browser, media target, Catalog or ledger is touched.
"""

from __future__ import annotations

import ast
from dataclasses import replace
import importlib
import json
from pathlib import Path
import tempfile
import unittest

from src.rename.decision_state import DecisionState, DecisionStateStore
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import RenamePlan
from src.rename.series_catalog import SeriesCatalog, SeriesCatalogError
from src.rename.work_catalog import WorkCatalog, WorkCatalogError
from src.task_queue import TaskQueueManager
from src.utils.utils import read_task_at, write_task_at


ROOT = Path(__file__).parents[1]


def _projection_module():
    return importlib.import_module("src.rename.projection_authority")


def _production_call_names() -> list[tuple[str, str, int]]:
    rows: list[tuple[str, str, int]] = []
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                continue
            rows.append((path.relative_to(ROOT).as_posix(), name, node.lineno))
    return rows


def _simple_plan(root: Path, task_id: str) -> RenamePlan:
    source = root / "source" / "Episode 01.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"phase-c-projection-source")
    return RenamePlan.from_mapping(
        task_id=task_id,
        source_root=source.parent,
        mapping={source: root / "target" / "Season 01" / "Fixture S01E01.mkv"},
        metadata={"season_number": 1},
    )


class PhaseCFullEntryMigrationContractTests(unittest.TestCase):
    def test_phase_b_authority_inventory_count_correction_is_frozen(self):
        module = _projection_module()
        self.assertEqual(31, len(module.AUTHORITY_FINGERPRINT_FIELDS))
        self.assertEqual(8, len(module.CORRELATION_ONLY_FIELDS))
        self.assertTrue(
            set(module.AUTHORITY_FINGERPRINT_FIELDS).isdisjoint(
                module.CORRELATION_ONLY_FIELDS
            )
        )

    def test_plan_store_has_one_production_storage_primitive_caller(self):
        all_plan_save = []
        for path in (ROOT / "src").rglob("*.py"):
            text = path.read_text("utf-8")
            if "plan_store.save(" in text:
                all_plan_save.extend(
                    (path.relative_to(ROOT).as_posix(), number)
                    for number, line in enumerate(text.splitlines(), start=1)
                    if "plan_store.save(" in line
                )
        self.assertEqual(1, len(all_plan_save))
        self.assertEqual(
            "src/rename/projection_authority.py",
            all_plan_save[0][0],
        )

    def test_reverse_and_phase_b_write_adapters_are_removed_from_production(self):
        forbidden = {
            "commit_legacy_draft",
            "persist_receipted_revision",
            "synchronize_plan",
            "synchronize_from_plan",
            "synchronized_candidate",
        }
        hits = [row for row in _production_call_names() if row[1] in forbidden]
        self.assertEqual([], hits, f"legacy projection authority is reachable: {hits}")

    def test_all_projection_entry_families_bind_a_task_terminal_mutation(self):
        expected_paths = {
            ROOT / "src" / "rename" / "process.py",
            ROOT / "src" / "rename" / "plan_review.py",
            ROOT / "src" / "pages" / "plan_review_page.py",
            ROOT / "src" / "rename" / "recovery_transaction.py",
        }
        covered = {
            path.relative_to(ROOT).as_posix()
            for path in expected_paths
            if "TaskTerminalMutation" in path.read_text("utf-8")
        }
        self.assertEqual(
            {path.relative_to(ROOT).as_posix() for path in expected_paths},
            covered,
        )

    def test_every_gui_decision_projection_update_binds_task_terminal(self):
        path = ROOT / "src" / "pages" / "plan_review_page.py"
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and function.attr == "update"
                and isinstance(function.value, ast.Attribute)
                and function.value.attr == "decision_command_service"
            ):
                continue
            keywords = {item.arg for item in node.keywords if item.arg is not None}
            if "planner" in keywords and "task_terminal" not in keywords:
                missing.append(node.lineno)
        self.assertEqual(
            [],
            missing,
            f"GUI projection updates without task terminal mutation: {missing}",
        )

    def test_persisted_provider_observation_is_not_promoted_to_decision(self):
        source = (ROOT / "src" / "pages" / "plan_review_page.py").read_text(
            "utf-8"
        )
        self.assertNotIn("persisted_inventory_observation", source)

    def test_receipt_rejects_post_projection_target_tamper(self):
        module = _projection_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _simple_plan(root, "phase-c-receipt-tamper")
            state_store = DecisionStateStore(root / "decision_state")
            plan_store = PlanStore(root / "plan")
            envelope = module.ProjectionAuthorityEnvelope(
                previous_decision=None,
                decision=DecisionState(task_id=plan.task_id),
                draft_plan=plan,
                inventory_fingerprint=module.inventory_fingerprint_for_plan(plan),
            )
            _decision, projected = module.ProjectionCommitService(
                state_store, plan_store
            ).commit(envelope)
            entry = projected.entries[0]
            tampered = replace(
                projected,
                entries=(
                    replace(entry, target_path=entry.target_path.with_name("Changed.mkv")),
                ),
            )
            codes = {issue.code for issue in PlanVerifier().verify(tampered).issues}
            self.assertIn("projection_receipt_mismatch", codes)

    def test_review_disposition_does_not_mutate_source_inventory_identity(self):
        module = _projection_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _simple_plan(root, "phase-c-ignore-disposition")
            ignored = replace(
                plan,
                entries=(),
                ignored_sources=plan.expected_sources,
            )
            self.assertEqual(
                module.inventory_fingerprint_for_plan(plan),
                module.inventory_fingerprint_for_plan(ignored),
                "review disposition must not masquerade as source inventory drift",
            )

    def test_task_terminal_crash_is_recovered_before_queue_record_repair(self):
        module = _projection_module()
        self.assertTrue(
            hasattr(module, "TaskTerminalMutation"),
            "Phase C task terminal mutation contract is missing",
        )
        self.assertTrue(
            hasattr(module.ProjectionCommitService, "recover_pending"),
            "startup cannot scan pending projection journals",
        )

    def test_task_queue_startup_calls_projection_recovery_before_active_repair(self):
        source = (ROOT / "src" / "task_queue.py").read_text("utf-8")
        tree = ast.parse(source)
        start = next(
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "start"
        )
        calls = []
        for node in ast.walk(start):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                calls.append((node.func.attr, node.lineno))
        recovery = next(
            (line for name, line in calls if name == "recover_pending"), None
        )
        repair = next(
            (line for name, line in calls if name == "_recover_active_records"), None
        )
        self.assertIsNotNone(recovery, "queue startup does not scan projection journal")
        self.assertIsNotNone(repair)
        self.assertLess(recovery, repair)

    def test_catalog_consumers_independently_reject_tampered_receipt(self):
        module = _projection_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _simple_plan(root, "phase-c-catalog-tamper")
            _decision, projected = module.ProjectionCommitService(
                DecisionStateStore(root / "decision_state"),
                PlanStore(root / "plan"),
            ).commit(module.ProjectionAuthorityEnvelope(
                previous_decision=None,
                decision=DecisionState(task_id=plan.task_id),
                draft_plan=plan,
                inventory_fingerprint=module.inventory_fingerprint_for_plan(plan),
            ))
            entry = projected.entries[0]
            tampered = replace(
                projected,
                entries=(replace(entry, target_path=entry.target_path.with_name(
                    "Tampered.mkv"
                )),),
            )
            with self.assertRaisesRegex(WorkCatalogError, "权威凭据"):
                WorkCatalog(root / "work_catalog").register_completed(
                    tampered, {}
                )
            with self.assertRaisesRegex(SeriesCatalogError, "权威凭据"):
                SeriesCatalog(root / "series_catalog").register_completed(tampered)


class PhaseCTaskTerminalStartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_persisted_boundary_recovers_task_before_queue_repair(self):
        module = _projection_module()
        for phase in (
            "prepared",
            "decision_written",
            "ownership_claimed",
            "plan_written",
            "task_terminal_written",
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                task_id = f"phase-c-startup-{phase}"
                task_root = root / "task"
                plan_root = root / "plan"
                decision_root = root / "decision_state"
                write_task_at(task_root, task_id, {
                    "uuid": task_id,
                    "workflow_status": "running",
                    "queue_position": 1,
                })
                plan = _simple_plan(root, task_id)

                def terminal(_decision, _plan, current):
                    current.update({
                        "workflow_status": "completed",
                        "queue_position": None,
                    })
                    return current

                def interrupt(current_phase: str) -> None:
                    if current_phase == phase:
                        raise KeyboardInterrupt(phase)

                service = module.ProjectionCommitService(
                    DecisionStateStore(decision_root),
                    PlanStore(plan_root),
                    task_root=task_root,
                    _phase_hook=interrupt,
                )
                with self.assertRaises(KeyboardInterrupt):
                    service.commit(
                        module.ProjectionAuthorityEnvelope(
                            previous_decision=None,
                            decision=DecisionState(task_id=task_id),
                            draft_plan=plan,
                            inventory_fingerprint=(
                                module.inventory_fingerprint_for_plan(plan)
                            ),
                        ),
                        task_terminal=module.TaskTerminalMutation.prepare(
                            task_root, task_id, terminal
                        ),
                    )
                pending = json.loads(
                    (root / "projection_journal" / f"{task_id}.json").read_text(
                        "utf-8"
                    )
                )
                self.assertEqual("pending", pending["status"])
                manager = TaskQueueManager(
                    task_root=task_root,
                    decision_state_root=decision_root,
                    plan_root=plan_root,
                    recovery_abandon_audit_root=root / "abandon",
                    test_mode=True,
                )
                await manager.start()
                await manager.stop()
                recovered = read_task_at(task_root, task_id)
                self.assertEqual("interrupted", recovered.get("workflow_status"))
                self.assertNotIn("projection_receipt_fingerprint", recovered)
                self.assertFalse(PlanStore(plan_root).path_for(task_id).exists())
                self.assertIsNone(
                    DecisionStateStore(decision_root).load_optional(task_id)
                )
                journal = json.loads(
                    (root / "projection_journal" / f"{task_id}.json").read_text(
                        "utf-8"
                    )
                )
                self.assertEqual("failed_closed", journal["status"])

    async def test_crash_after_task_cas_before_phase_advance_is_recovered(self):
        module = _projection_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "phase-c-task-cas-window"
            task_root = root / "task"
            plan_root = root / "plan"
            decision_root = root / "decision_state"
            write_task_at(task_root, task_id, {
                "uuid": task_id,
                "workflow_status": "running",
            })
            plan = _simple_plan(root, task_id)
            service = module.ProjectionCommitService(
                DecisionStateStore(decision_root),
                PlanStore(plan_root),
                task_root=task_root,
            )
            original_write_journal = service._write_journal

            def interrupt_phase_advance(task, phase, status, **kwargs):
                if phase == "task_terminal_written":
                    raise KeyboardInterrupt("after task CAS")
                return original_write_journal(task, phase, status, **kwargs)

            service._write_journal = interrupt_phase_advance
            with self.assertRaises(KeyboardInterrupt):
                service.commit(
                    module.ProjectionAuthorityEnvelope(
                        previous_decision=None,
                        decision=DecisionState(task_id=task_id),
                        draft_plan=plan,
                        inventory_fingerprint=(
                            module.inventory_fingerprint_for_plan(plan)
                        ),
                    ),
                    task_terminal=module.TaskTerminalMutation.prepare(
                        task_root,
                        task_id,
                        lambda _decision, _plan, current: {
                            **current,
                            "workflow_status": "completed",
                        },
                    ),
                )
            self.assertEqual(
                "completed", read_task_at(task_root, task_id)["workflow_status"]
            )
            manager = TaskQueueManager(
                task_root=task_root,
                decision_state_root=decision_root,
                plan_root=plan_root,
                recovery_abandon_audit_root=root / "abandon",
                test_mode=True,
            )
            await manager.start()
            await manager.stop()
            recovered = read_task_at(task_root, task_id)
            self.assertEqual("interrupted", recovered["workflow_status"])
            self.assertNotIn("projection_receipt_fingerprint", recovered)


if __name__ == "__main__":
    unittest.main()
