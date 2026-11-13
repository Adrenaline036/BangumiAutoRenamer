"""SG0 source conservation and refusal of the retired binary partition writer.

Provider transport is frozen, but AddTask submission, queue/worker
persistence, review rendering and all ownership stores are real production
code.
"""

from __future__ import annotations
from tests.queue_fixture import TaskQueueManager as ControlledQueue

import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from src.pages.plan_review_page import (
    PlanReviewDialog,
    render_cross_work_partition_action,
)
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_execution import PlanExecutionError
from src.rename.plan_review import PlanReviewError
from src.rename.plan_store import PlanStore
from src.rename.projection_authority import SourceOwnershipStore
from tests.legacy_partition_builders import new_partition_decision
from src.rename.task_batch_scope import (
    SourceReservationConflict,
    TaskSourceScope,
)
from src.task_queue import TaskQueueManager
import tests.test_complex_library_special_red_contract as _special_fixture


class SG0AutomaticPartitionIncidentRedContracts(unittest.IsolatedAsyncioTestCase):
    async def _parent_flow(self, root: Path):
        package = root / "sources" / "[VCB-Studio] Steins;Gate 0 [Ma10p_1080p]"
        main = _special_fixture._write(
            package / "[VCB-Studio] Steins;Gate 0 [01].mkv", b"main"
        )
        beta = _special_fixture._write(
            package
            / "SPs"
            / "[VCB-Studio] Steins;Gate [23β][Ma10p_1080p][x265_flac].mkv",
            b"23-beta",
        )
        sibling = _special_fixture._write(
            package / "SPs" / "[VCB-Studio] Steins;Gate 0 [Menu].mkv",
            b"menu",
        )
        result = await _special_fixture.ComplexLibrarySpecialRedContracts._run_via_add_task(
            self,
            root / "flow",
            package,
            "129807",
            renamer_factory=_special_fixture._configured_renamer,
        )
        return result, package, main, beta, sibling

    @staticmethod
    def _manager(root: Path, result) -> TaskQueueManager:
        return ControlledQueue(
            task_root=result.tasks,
            decision_state_root=result.decisions,
            recovery_abandon_audit_root=root / "flow" / "abandon_audit",
            plan_root=result.plans,
            batch_scope_root=root / "flow" / "task_batch_scope",
            test_mode=True,
        )

    async def test_parent_only_admission_does_not_guess_or_split_same_work_extras(self):
        """Admission keeps the complete package intact until a typed decision."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _package, main, beta, sibling = await self._parent_flow(root)
            task_files = tuple(result.tasks.glob("*.json"))
            task = json.loads(task_files[0].read_text("utf-8"))
            scope = TaskSourceScope.from_mapping(task["task_source_scope"])
            plan = result.plan_store.load(result.task_id)
            ownership = SourceOwnershipStore(
                result.plans.parent / "source_ownership"
            ).load_optional(result.task_id)

        self.assertEqual(1, len(task_files))
        self.assertEqual({main, beta, sibling}, {
            Path(item.source_path) for item in scope.source_identities
        })
        self.assertEqual({main, beta, sibling}, set(plan.expected_sources))
        self.assertIsNotNone(ownership)
        self.assertEqual({main, beta, sibling}, {
            item.source_path for item in ownership.identities
        })

    @staticmethod
    def _snapshot(root):
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
                if p.is_file() and p.suffix != '.lock'}

    async def _reject_binary(self, manager, result, beta, *, fingerprint=None):
        plan = result.plan_store.load(result.task_id)
        await manager.partition_task_source(parent_task_id=result.task_id,
            expected_plan_fingerprint=fingerprint or PlanStore.fingerprint(plan),
            selected_entry_id=entry_id(next(e for e in plan.entries if e.source_path == beta)),
            reason='用户确认另一作品')

    async def test_parent_review_action_opens_new_regrouping_without_binary_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _package, _main, _beta, _sibling = await self._parent_flow(root)
            before = self._snapshot(root)
            from unittest.mock import AsyncMock
            dialog = SimpleNamespace(_open_selected_source_regrouping=AsyncMock(), selected_ids={'selected-beta'})
            ui = _special_fixture._CaptureUi()
            with patch('src.pages.plan_review_page.ui', ui), patch.object(
                    TaskQueueManager, 'partition_task_source', side_effect=AssertionError('retired writer')) as old:
                render_cross_work_partition_action(lambda: PlanReviewDialog._partition_selected_as_cross_work(dialog))
                callback = ui.callbacks.get('作为独立作品整理')
                self.assertIsNotNone(callback)
                outcome = callback()
                if inspect.isawaitable(outcome):
                    await outcome
            dialog._open_selected_source_regrouping.assert_awaited_once_with(('selected-beta',))
            old.assert_not_called()
            self.assertEqual(before, self._snapshot(root))
            self.assertEqual({result.task_id}, {p.stem for p in result.tasks.glob('*.json')})

    async def test_retired_stale_duplicates_and_cross_batch_source_conflict_remain_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _package, _main, beta, _sibling = await self._parent_flow(root)
            manager = self._manager(root, result)
            await manager.start()
            try:
                before = self._snapshot(root)
                for fingerprint in (PlanStore.fingerprint(result.plan_store.load(result.task_id)), 'stale'):
                    with self.subTest(fingerprint=fingerprint), self.assertRaisesRegex(PlanExecutionError, '旧二分拆卡已停用'):
                        await self._reject_binary(manager, result, beta, fingerprint=fingerprint)
                    self.assertEqual(before, self._snapshot(root))
                duplicate = manager.prepare_input_batch((beta,), is_anime=True, is_movie=False,
                     source='ui', preview_only=True)
                with self.assertRaises(SourceReservationConflict):
                    await manager.enqueue_batch(**duplicate)
            finally:
                await manager.stop()
            self.assertEqual({result.task_id}, {p.stem for p in result.tasks.glob('*.json')})

    async def test_retired_binary_never_dispatches_a_child_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _package, _main, beta, _sibling = await self._parent_flow(root)
            manager = self._manager(root, result)
            before = self._snapshot(root)
            with patch('src.task_queue.Rename', side_effect=AssertionError('no child worker')) as worker:
                with self.assertRaisesRegex(PlanExecutionError, '旧二分拆卡已停用'):
                    await self._reject_binary(manager, result, beta)
            worker.assert_not_called()
            self.assertTrue(manager._queue.empty())
            self.assertFalse(manager._pending)
            self.assertEqual(before, self._snapshot(root))

    async def test_retired_binary_rejects_before_any_plan_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _package, _main, beta, _sibling = await self._parent_flow(root)
            manager = self._manager(root, result)
            before = self._snapshot(root)
            with patch('src.rename.plan_store.PlanStore.save', side_effect=OSError('seeded')) as writer:
                with self.assertRaisesRegex(PlanExecutionError, '旧二分拆卡已停用'):
                    await self._reject_binary(manager, result, beta)
            writer.assert_not_called()
            self.assertEqual(before, self._snapshot(root))

    async def test_retired_binary_never_enters_old_mutation_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _package, _main, beta, _sibling = await self._parent_flow(root)
            manager = self._manager(root, result)
            before = self._snapshot(root)
            with patch('src.rename.source_partition.SourcePartitionStore.checkpoint',
                    side_effect=KeyboardInterrupt('old mutation must be unreachable')) as checkpoint:
                with self.assertRaisesRegex(PlanExecutionError, '旧二分拆卡已停用'):
                    await self._reject_binary(manager, result, beta)
            checkpoint.assert_not_called()
            restarted = self._manager(root, result)
            await restarted.start()
            try:
                self.assertEqual(before, self._snapshot(root))
                self.assertFalse(restarted._pending)
            finally:
                await restarted.stop()

    async def test_historical_pending_partition_blocks_approval_retry_and_abandon(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _package, _main, beta, _sibling = await self._parent_flow(root)
            plan = result.plan_store.load(result.task_id)
            fingerprint = PlanStore.fingerprint(plan)
            parent_task = json.loads(
                (result.tasks / f"{result.task_id}.json").read_text("utf-8")
            )
            scope = TaskSourceScope.from_mapping(parent_task["task_source_scope"])
            selected = tuple(
                item for item in scope.source_identities
                if Path(item.source_path) == beta
            )
            manager = self._manager(root, result)
            await manager.start()
            decision = new_partition_decision(
                parent_task_id=result.task_id,
                child_task_id="seeded-child",
                expected_parent_plan_fingerprint=fingerprint,
                expected_parent_batch_id=str(parent_task["batch_id"]),
                expected_parent_scope_fingerprint=(
                    scope.source_identity_set_fingerprint
                ),
                expected_parent_reservation_revision=scope.reservation_revision,
                selected_source_identities=selected,
                reason="typed cross-work decision",
            )
            from tests.legacy_partition_fixture import seed_history
            seed_history(manager.source_partition_store, decision, ())
            before = self._snapshot(root)
            review = PlanReviewService(
                result.plan_store,
                root / "flow" / "reviews",
                root / "flow" / "work_catalog",
                decision_state_root=result.decisions,
                task_root=result.tasks,
            )
            with self.assertRaisesRegex(PlanReviewError, "partition"):
                review.approve_entries(
                    result.task_id,
                    fingerprint,
                    tuple(entry_id(entry) for entry in plan.entries),
                )
            try:
                with self.assertRaisesRegex(PlanExecutionError, "partition"):
                    await manager.retry_current(result.task_id)
                with self.assertRaisesRegex(PlanExecutionError, "partition"):
                    await manager.abandon_and_remove(
                        result.task_id,
                        record_root=root / "flow" / "record",
                        plan_root=result.plans,
                        review_root=root / "flow" / "reviews",
                    )
                self.assertEqual(before, self._snapshot(root))
            finally:
                await manager.stop()


if __name__ == "__main__":
    unittest.main()
