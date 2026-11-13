"""Phase-C contracts for successor-authority all-entry root binding."""

from __future__ import annotations
from tests.queue_fixture import TaskQueueManager as ControlledQueue

import ast
import asyncio
import inspect
from pathlib import Path
import tempfile
import unittest

import src.task_queue as task_queue_module
from src.rename.process import Rename
from src.task_queue import TaskJob, TaskQueueManager
from tests.test_r24_successor_authority_incident_red_contract import (
    _cohort_case,
    _confirmed_s1_authority,
    _resolver_seam_worker,
)


ROOT = Path(__file__).parents[1]
SRC = ROOT / "src"
ENTRY_METHODS = {
    "prepare_input_batch",
    "prepare_discovered_batch",
    "enqueue_batch",
    "enqueue",
    "retry_current",
    "reenqueue_workspace",
    "enqueue_reidentify",
    "enqueue_legacy_replan",
}


def _production_calls(names: set[str]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for func in (node.func, *node.args):
                if isinstance(func, ast.Attribute) and func.attr in names:
                    found.add((path.relative_to(ROOT).as_posix(), func.attr))
    return found


class _RecordingRename:
    constructor_calls: list[dict[str, Path]] = []
    process_calls: list[tuple[object, ...]] = []

    def __init__(
        self,
        *,
        decision_state_root: Path,
        provider_link_catalog_root: Path,
    ) -> None:
        self.constructor_calls.append({
            "decision_state_root": Path(decision_state_root),
            "provider_link_catalog_root": Path(provider_link_catalog_root),
        })

    def process(self, *args: object) -> str:
        self.process_calls.append(args)
        return "recorded"


class SuccessorAuthorityPhaseCMigrationContracts(unittest.TestCase):
    def test_all_production_entry_adapters_converge_on_one_queue_gateway(self):
        self.assertEqual(
            {
                ("src/pages/add_task_page.py", "prepare_input_batch"),
                ("src/pages/add_task_page.py", "prepare_discovered_batch"),
                ("src/pages/add_task_page.py", "enqueue_batch"),
                ("src/web.py", "prepare_input_batch"),
                ("src/web.py", "enqueue_batch"),
                ("src/pages/data_table_page.py", "retry_current"),
                ("src/pages/plan_review_page.py", "reenqueue_workspace"),
                ("src/pages/provider_selection_work.py", "enqueue_reidentify"),
                ("src/task_queue.py", "enqueue_batch"),
                ("src/task_queue.py", "retry_current"),
            },
            _production_calls(ENTRY_METHODS),
        )
        constructors = _production_calls({"TaskQueueManager"})
        # Name-call constructors are checked separately because the helper
        # above intentionally inventories attribute-call entry adapters.
        self.assertEqual(set(), constructors)
        queue_source = (SRC / "task_queue.py").read_text(encoding="utf-8")
        from src.rename import recognition_queue_admission
        import inspect
        self.assertIn('from .rename.recognition_queue_admission import admit', inspect.getsource(TaskQueueManager.retry_current))
        self.assertIn('await admit(', inspect.getsource(TaskQueueManager.retry_current))
        self.assertIn('_prepare_reidentify_job(', inspect.getsource(recognition_queue_admission))
        self.assertEqual(1, queue_source.count("task_queue = TaskQueueManager()"))
        self.assertEqual(1, queue_source.count("async def _worker("))

    def test_queue_injects_its_effective_roots_into_every_ordinary_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = ControlledQueue(
                task_root=root / "task",
                decision_state_root=root / "decision_state",
                plan_root=root / "plan",
                test_mode=True,
            )
            _RecordingRename.constructor_calls.clear()
            _RecordingRename.process_calls.clear()
            original = task_queue_module.Rename
            task_queue_module.Rename = _RecordingRename
            try:
                result = asyncio.run(manager._execute_rename(TaskJob(
                    task_id="phase-c-consumer",
                    path=root / "source",
                    is_anime=True,
                    is_movie=False,
                )))
            finally:
                task_queue_module.Rename = original

            self.assertEqual("recorded", result)
            self.assertEqual([{
                "decision_state_root": root / "decision_state",
                "provider_link_catalog_root": root / "provider_link_catalog",
            }], _RecordingRename.constructor_calls)
            self.assertEqual("phase-c-consumer", _RecordingRename.process_calls[0][3])

    def test_isolated_successor_authority_wins_over_unrelated_global_state(self):
        case = _cohort_case()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = root / "isolated" / "decision_state"
            _confirmed_s1_authority(decisions)
            worker = _resolver_seam_worker(
                case,
                task_id="phase-c-consumer-s2",
                decision_state_root=decisions,
                provider_link_catalog_root=root / "isolated" / "provider_link_catalog",
            )
            _name, info, resolution = worker._resolve_bangumi_provider_selection(
                dict(worker.active_provider_selection)
            )
            self.assertEqual("53918", str(info["id"]))
            self.assertEqual(2, dict(resolution.season_assignments)["86670"])
            self.assertEqual([], worker.search.calls)
            self.assertEqual(0, worker.bangumi_search.calls)

    def test_legacy_global_resolver_and_decision_store_paths_are_zero(self):
        init_source = inspect.getsource(Rename.__init__)
        resolver_source = inspect.getsource(
            Rename._resolve_bangumi_provider_selection
        )
        class_source = inspect.getsource(Rename)
        queue_source = inspect.getsource(TaskQueueManager._execute_rename)

        self.assertIn("decision_state_root", init_source)
        self.assertIn("provider_link_catalog_root", init_source)
        self.assertNotIn("DECISION_STATE_PATH", resolver_source)
        self.assertNotIn("PROVIDER_LINK_CATALOG_PATH", resolver_source)
        self.assertEqual(0, class_source.count(
            "DecisionStateStore(DECISION_STATE_PATH)"
        ))
        self.assertNotIn("Rename().process", queue_source)
        self.assertIn("decision_state_root=self.decision_state_root", queue_source)
        self.assertIn(
            "provider_link_catalog_root=self.provider_link_catalog_root",
            queue_source,
        )


if __name__ == "__main__":
    unittest.main()
