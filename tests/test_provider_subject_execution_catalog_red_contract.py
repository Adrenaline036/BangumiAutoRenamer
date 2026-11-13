"""Phase C regression contracts for provider-subject execution and recovery.

Every execution contract starts from the real Phase B AddTask -> non-test
queue/worker -> provider selection -> DecisionState/Plan persistence harness.
The tests then use the real approval and approved-execution services in
temporary roots.  They never construct the provider snapshot or final Plan as
the successful production result.
"""

from __future__ import annotations
from tests.queue_fixture import TaskQueueManager as ControlledQueue

from tests._approval_stage_helpers import approve_dialog
import asyncio
from collections.abc import Mapping
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import src.pages.plan_review_page as review_page
import src.rename.projection_authority as projection_module
import src.utils.utils as utils_module
import tests.test_provider_subject_plan_wiring_red_contract as phase_b
import tests._media_flow_helpers as execution_fixture
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.execution_recovery import reconcile_catalog_update
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.provider_link_catalog import (
    ProviderLinkCatalog,
    ProviderLinkCatalogError,
    ProviderLinkMapping,
    fingerprint_evidence,
    match_mapping_provider,
)
from src.rename.rename_plan import PlanStatus
from src.rename.series_catalog import SeriesCatalog
from src.rename.work_catalog import WorkCatalog
from src.task_queue import TaskQueueManager
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


class ProviderSubjectExecutionCatalogRedContracts(
    unittest.IsolatedAsyncioTestCase
):
    async def _real_86_plan(
        self,
        root: Path,
        *,
        episodes: tuple[int, ...] = tuple(range(1, 13)),
        subtitles: bool = True,
    ) -> SimpleNamespace:
        source = phase_b._make_episode_source(
            root / "source", "86 Part 2", episodes, subtitles=subtitles
        )
        harness = phase_b.ProviderSubjectPlanWiringRedContracts(
            methodName="runTest"
        )
        result = await harness._run_real_add_select(
            root=root / "flow",
            source=source,
            subjects=phase_b.EIGHTY_SIX_SUBJECTS,
            relations=phase_b.EIGHTY_SIX_RELATIONS,
            selected_subject_id="331887",
            library_root=root / "library",
        )
        review = PlanReviewService(PlanStore(result.plans), result.reviews, result.root / 'work_catalog',
            decision_state_root=result.decisions, task_root=result.tasks)
        result.state = review.decision_state_store.load(result.task_id)
        result.task = utils_module.read_task_at(result.tasks, result.task_id)
        verification = PlanVerifier().verify(result.plan)
        problems = []
        if verification.status not in {PlanStatus.ACCEPTED, PlanStatus.NEED_CONFIRM}:
            problems.append(
                f"86 provider Plan status is {verification.status.value}, not human-reviewable"
            )
        if verification.issues:
            problems.append(
                "86 provider Plan verifier issues: "
                + ", ".join(issue.code for issue in verification.issues)
            )
        projection = result.plan.metadata.get("source_episode_projection")
        if not isinstance(projection, Mapping) or projection.get("mode") != "relative":
            problems.append("86 provider Plan did not freeze relative episode projection")
        self.assertEqual(
            [],
            problems,
            phase_b.format_provider_flow_diagnostics(
                result,
                test_identity=f"{self.id()}::_real_86_plan",
                sequence=("86-relative-provider-plan",),
            ),
        )
        return result

    async def _approve(self, result: SimpleNamespace):
        store = PlanStore(result.plans)
        review = PlanReviewService(
            store, result.reviews, result.root / "work_catalog",
            decision_state_root=result.decisions, task_root=result.tasks
        )
        plan = store.load(result.task_id)
        result.plan = plan
        result.state = review.decision_state_store.load(result.task_id)
        result.task = utils_module.read_task_at(result.tasks, result.task_id)
        dialog = SimpleNamespace(
            review_service=review,
            plan=plan,
            selected_ids={entry_id(item) for item in plan.entries},
            task_data=dict(result.task),
            decision=None,
            review_badge=SimpleNamespace(text=""),
            _refresh_plan_header=MagicMock(),
            _render_entries=MagicMock(),
            _refresh_execute_button=MagicMock(),
        )
        with (
            patch.object(utils_module, "TASK_PATH", result.tasks),
            patch.object(review_page, "notify"),
        ):
            await approve_dialog(dialog, tuple(entry_id(item) for item in plan.entries))
        approved = store.load(result.task_id)
        return store, review, approved, store.fingerprint(approved)

    async def test_live_waiter_is_read_only_across_projection_cas_barrier(self):
        """A live waiter must not create an unjournaled task-record third state."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas_entered = threading.Event()
            cas_release = threading.Event()
            waiter_polled = threading.Event()
            cas_lock = threading.Lock()
            cas_count = 0
            cas_observations = []
            barrier_task_id: str | None = None
            task_sha_at_barrier: str | None = None
            task_sha_after_waiter: str | None = None
            journal_during_barrier: dict[str, object] = {}
            original_cas = projection_module.replace_task_record_at_if_sha256
            original_read = TaskQueueManager._read

            def blocked_selection_cas(
                task_root,
                task_id,
                *,
                expected_sha256,
                data,
            ):
                nonlocal cas_count, barrier_task_id, task_sha_at_barrier
                with cas_lock:
                    cas_count += 1
                    current_count = cas_count
                journal_path = Path(task_root).parent / 'projection_journal' / f'{task_id}.json'
                journal = _json(journal_path) if journal_path.exists() else {}
                selection = current_provider_selection_context(data)
                cas_observations.append((current_count, journal.get('phase'),
                    selection.get('state') if selection else None, data.get('queue_operation')))
                if (not cas_entered.is_set() and journal.get('status') == 'pending'
                        and journal.get('phase') == 'plan_written' and selection
                        and data.get('queue_operation') == 'reidentify'):
                    barrier_task_id = task_id
                    task_path = Path(task_root) / f"{task_id}.json"
                    task_sha_at_barrier = hashlib.sha256(
                        task_path.read_bytes()
                    ).hexdigest()
                    cas_entered.set()
                    if not cas_release.wait(timeout=10):
                        raise TimeoutError("FL-07 deterministic CAS barrier timed out")
                return original_cas(
                    task_root,
                    task_id,
                    expected_sha256=expected_sha256,
                    data=data,
                )

            def traced_read(manager, task_id):
                result = original_read(manager, task_id)
                caller = sys._getframe(1).f_code.co_name
                if (
                    caller == "wait_for_selection_result"
                    and cas_entered.is_set()
                    and not cas_release.is_set()
                ):
                    waiter_polled.set()
                return result

            flow = None
            flow_error: BaseException | None = None
            result = None
            restart_error: BaseException | None = None
            try:
                with (
                    patch.object(
                        projection_module,
                        "replace_task_record_at_if_sha256",
                        blocked_selection_cas,
                    ),
                    patch.object(TaskQueueManager, "_read", traced_read),
                ):
                    flow = asyncio.create_task(self._real_86_plan(root))
                    self.assertTrue(
                        await asyncio.wait_for(
                            asyncio.to_thread(cas_entered.wait, 10), timeout=11
                        ),
                        f"provider reidentify never reached its plan-written terminal CAS: {cas_observations}",
                    )
                    self.assertTrue(
                        await asyncio.wait_for(
                            asyncio.to_thread(waiter_polled.wait, 10), timeout=11
                        ),
                        "live waiter did not poll while the projection CAS was blocked",
                    )
                    self.assertIsNotNone(barrier_task_id)
                    task_path = (
                        root / "flow" / "tasks" / f"{barrier_task_id}.json"
                    )
                    task_sha_after_waiter = hashlib.sha256(
                        task_path.read_bytes()
                    ).hexdigest()
                    journal_path = (
                        root
                        / "flow"
                        / "projection_journal"
                        / f"{barrier_task_id}.json"
                    )
                    journal_during_barrier = _json(journal_path)
            finally:
                cas_release.set()
                if flow is not None:
                    try:
                        result = await asyncio.wait_for(asyncio.shield(flow), timeout=30)
                    except BaseException as exc:
                        flow_error = exc
                        if not flow.done():
                            await flow

            journal_path = (
                root
                / "flow"
                / "projection_journal"
                / f"{barrier_task_id}.json"
            )
            final_journal = _json(journal_path)
            final_task = _json(
                root / "flow" / "tasks" / f"{barrier_task_id}.json"
            )
            restarted = ControlledQueue(
                task_root=root / "flow" / "tasks",
                decision_state_root=root / "flow" / "decisions",
                plan_root=root / "flow" / "plans",
                recovery_abandon_audit_root=root / "flow" / "abandon",
                test_mode=True,
            )
            try:
                await restarted.start()
            except BaseException as exc:
                restart_error = exc
            finally:
                await restarted.stop()
            pending_after_restart = tuple(
                path.name
                for path in (root / "flow" / "projection_journal").glob("*.json")
                if _json(path).get("status") == "pending"
            )

        self.assertIsNone(flow_error)
        self.assertIsNotNone(result)
        self.assertEqual("plan_written", journal_during_barrier.get("phase"))
        self.assertEqual("pending", journal_during_barrier.get("status"))
        self.assertEqual(task_sha_at_barrier, task_sha_after_waiter)
        self.assertEqual("completed", final_journal.get("status"))
        self.assertEqual(
            "succeeded", current_provider_selection_context(final_task)["state"]
        )
        self.assertIsNone(restart_error)
        self.assertEqual((), pending_after_restart)

    async def test_real_86_relative_execution_materializes_exact_ranges(self):
        """Real approved execution must persist both split-cour ranges."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await self._real_86_plan(root)
            runner = execution_fixture.MediaFlow(
                methodName="runTest"
            )
            try:
                task, ledger, _service = await runner._approve_and_execute(
                    root=result.root, result=result
                )
            except PlanExecutionError as exc:
                self.fail(
                    "Phase B nested range Plan was rejected before approved "
                    f"execution could start: {exc}"
                )

            problems: list[str] = []
            if task.get("execution_status") != "completed":
                problems.append(f"execution did not complete: {task.get('error')}")
            if ledger.get("status") != "completed":
                problems.append("approved queue did not persist a completed ledger")
            targets = {Path(item["target"]) for item in ledger.get("entries", ())}
            if targets != {entry.target_path for entry in result.plan.entries}:
                problems.append("ledger targets differ from the immutable Plan")
            if not all(path.is_file() for path in targets):
                problems.append("approved execution did not create every Plan target")

            mapping_files = tuple((result.root / "provider_links").glob("*.json"))
            if len(mapping_files) != 1:
                problems.append(
                    f"execution persisted {len(mapping_files)} ProviderLink records"
                )
            else:
                mapping = ProviderLinkMapping.from_dict(_json(mapping_files[0]))
                actual = {
                    str(row.get("provider_id")): (
                        str(row.get("tmdb_tv_id")),
                        row.get("season_number"),
                        row.get("episode_start"),
                        row.get("episode_end"),
                    )
                    for row in mapping.season_edges
                }
                expected = {
                    "302189": ("100565", 1, 1, 11),
                    "331887": ("100565", 1, 12, 23),
                }
                if actual != expected:
                    problems.append(
                        f"ProviderLink materialization lost exact ranges: {actual!r}"
                    )
                match = match_mapping_provider(
                    mapping,
                    {
                        "namespace": "bangumi",
                        "entity_type": "subject",
                        "provider_id": "331887",
                    },
                )
                if (
                    match is None
                    or match.season_number != 1
                    or getattr(match, "episode_start", None) != 12
                    or getattr(match, "episode_end", None) != 23
                ):
                    problems.append("L0 match did not return the frozen E12-E23 range")
            self.assertEqual([], problems)

    async def test_source_inventory_drift_is_rejected_before_transfer(self):
        """Approved source bytes may not drift before the media boundary."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await self._real_86_plan(root, subtitles=False)
            store, review, plan, fingerprint = await self._approve(result)
            source = plan.entries[0].source_path
            source.write_bytes(source.read_bytes() + b"-changed-after-approval")
            transfer_calls: list[str] = []

            class TransferMustNotRun:
                def __init__(self, *_args, **_kwargs):
                    transfer_calls.append("constructed")

                def trans_file(self):
                    transfer_calls.append("called")
                    return None

            service = PlanExecutionService(
                store,
                review,
                task_root=result.tasks,
                record_root=result.root / "records",
                catalog_root=result.root / "work_catalog",
                series_catalog_root=result.root / "series_catalog",
                decision_state_root=result.decisions,
                recovery_transaction_root=result.root / "recovery_transactions",
                provider_link_catalog_root=result.root / "provider_links",
                transfer_factory=TransferMustNotRun,
            )
            try:
                with patch(
                    "src.rename.plan_execution.is_dry_run_enabled",
                    return_value=False,
                ):
                    service.execute(result.task_id, fingerprint)
            except PlanExecutionError:
                pass

            task = _json(result.tasks / f"{result.task_id}.json")
            target_files = tuple(
                path
                for path in (root / "library").rglob("*")
                if path.is_file()
            )
            problems = []
            if transfer_calls:
                problems.append("source drift reached the transfer boundary")
            if target_files:
                problems.append("source drift created target files")
            if (result.root / "records" / f"{result.task_id}.ledger.json").exists():
                problems.append("source drift created an execution ledger")
            if task.get("execution_phase") != "failed_before_media":
                problems.append(
                    f"source drift persisted unsafe phase {task.get('execution_phase')!r}"
                )
            if task.get("execution_requires_manual_check") is not False:
                problems.append("pre-media rejection incorrectly requires media reconciliation")
            self.assertEqual([], problems)

    async def test_existing_same_season_wrong_range_conflicts_without_rewrite(self):
        """An anchored row with the same subject/season but another range conflicts."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await self._real_86_plan(root, subtitles=False)
            snapshot = dict(result.plan.metadata["provider_link_snapshot"])
            mapping_id = str(snapshot["mapping_id"])
            series_key = str(snapshot["series_key"])
            evidence = {"fixture": "foreign-range"}
            anchor = ProviderLinkMapping.from_dict({
                "schema_version": 1,
                "mapping_id": mapping_id,
                "revision": 0,
                "local_series_key": series_key,
                "status": "materialized_confirmed",
                "provider_edges": (
                    {
                        "namespace": "tmdb", "entity_type": "tv",
                        "provider_id": "100565", "role": "series",
                    },
                    {
                        "namespace": "bangumi", "entity_type": "subject",
                        "provider_id": "302189", "role": "season",
                    },
                    {
                        "namespace": "bangumi", "entity_type": "subject",
                        "provider_id": "331887", "role": "season",
                    },
                ),
                "season_edges": (
                    {
                        "namespace": "bangumi", "entity_type": "subject",
                        "provider_id": "302189", "tmdb_tv_id": "100565",
                        "season_number": 1, "episode_start": 1,
                        "episode_end": 11,
                    },
                    {
                        "namespace": "bangumi", "entity_type": "subject",
                        "provider_id": "331887", "tmdb_tv_id": "100565",
                        "season_number": 1, "episode_start": 13,
                        "episode_end": 24,
                    },
                ),
                "evidence_snapshot": evidence,
                "evidence_fingerprint": fingerprint_evidence(evidence),
            })
            catalog = ProviderLinkCatalog(result.root / "provider_links")
            persisted = catalog.save(anchor, expected_revision=-1)
            mapping_path = result.root / "provider_links" / f"{mapping_id}.json"
            before = mapping_path.read_bytes()

            snapshot.update({
                "automation_level": "L1",
                "anchor": {
                    "mapping_id": mapping_id,
                    "revision": persisted.revision,
                    "evidence_fingerprint": persisted.evidence_fingerprint,
                },
            })
            plan = replace(
                result.plan,
                metadata={**dict(result.plan.metadata), "provider_link_snapshot": snapshot},
            )
            service = PlanExecutionService(
                PlanStore(result.plans),
                PlanReviewService(PlanStore(result.plans), result.reviews, result.root / 'work_catalog',
                    decision_state_root=result.decisions, task_root=result.tasks),
                task_root=result.tasks,
                record_root=result.root / "records",
                catalog_root=result.root / "work_catalog",
                series_catalog_root=result.root / "series_catalog",
                decision_state_root=result.decisions,
                recovery_transaction_root=result.root / "recovery_transactions",
                provider_link_catalog_root=result.root / "provider_links",
            )
            rejected = False
            try:
                service._materialize_provider_link(plan)
            except ProviderLinkCatalogError:
                rejected = True
            after = mapping_path.read_bytes()
            self.assertTrue(rejected, "foreign range was silently treated as an exact row")
            self.assertEqual(before, after, "range conflict rewrote ProviderLinkCatalog")

    async def test_series_recovery_replays_provider_range_offline_and_completes_task(self):
        """TV catalog recovery must restore both Series and Provider facts."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await self._real_86_plan(root, subtitles=False)
            runner = execution_fixture.MediaFlow(
                methodName="runTest"
            )
            with patch.object(
                PlanExecutionService,
                "_materialize_provider_link",
                side_effect=ProviderLinkCatalogError(
                    "RED provider range registration failure"
                ),
            ):
                try:
                    task, ledger, _service = await runner._approve_and_execute(
                        root=result.root, result=result
                    )
                except PlanExecutionError as exc:
                    self.fail(
                        "Phase B nested range Plan was rejected before the "
                        f"injected provider registration failure: {exc}"
                    )
            self.assertEqual("catalog_update_pending", task.get("execution_status"))
            self.assertEqual("completed", ledger.get("status"))
            self.assertEqual((), tuple((result.root / "provider_links").glob("*.json")))
            media_before = tuple(
                (
                    str(entry.target_path),
                    entry.target_path.stat().st_dev,
                    entry.target_path.stat().st_ino,
                    entry.target_path.stat().st_size,
                )
                for entry in result.plan.entries
            )
            problems: list[str] = []
            try:
                reconcile_catalog_update(
                    result.task_id,
                    task_root=result.tasks,
                    record_root=result.root / "records",
                    plan_store=PlanStore(result.plans),
                    work_catalog=WorkCatalog(result.root / "work_catalog"),
                    series_catalog=SeriesCatalog(result.root / "series_catalog"),
                    provider_link_catalog=ProviderLinkCatalog(
                        result.root / "provider_links"
                    ),
                )
            except Exception as exc:
                problems.append(f"TV recovery command rejected exact frozen evidence: {exc}")

            mapping_files = tuple((result.root / "provider_links").glob("*.json"))
            if len(mapping_files) != 1:
                problems.append("TV recovery did not restore one ProviderLink mapping")
            else:
                mapping = ProviderLinkMapping.from_dict(_json(mapping_files[0]))
                rows = {
                    str(row.get("provider_id")): (
                        row.get("episode_start"), row.get("episode_end")
                    )
                    for row in mapping.season_edges
                }
                if rows != {"302189": (1, 11), "331887": (12, 23)}:
                    problems.append(f"TV recovery restored incorrect ranges: {rows!r}")
            recovered_task = _json(result.tasks / f"{result.task_id}.json")
            if recovered_task.get("execution_status") != "completed":
                problems.append("TV recovery did not close the pending task")
            media_after = tuple(
                (
                    str(entry.target_path),
                    entry.target_path.stat().st_dev,
                    entry.target_path.stat().st_ino,
                    entry.target_path.stat().st_size,
                )
                for entry in result.plan.entries
            )
            if media_after != media_before:
                problems.append("TV recovery changed existing media identity")
            self.assertEqual([], problems)

    async def test_legacy_range_less_split_cour_remains_unapprovable_control(self):
        """Existing Phase B old-Plan gate remains a normal Phase C control."""

        with tempfile.TemporaryDirectory() as directory:
            result = await self._real_86_plan(Path(directory), subtitles=False)
            snapshot = dict(result.plan.metadata["provider_link_snapshot"])
            snapshot["schema_version"] = 1
            snapshot.pop("derived_episode_ranges", None)
            metadata = dict(result.plan.metadata)
            metadata["provider_link_snapshot"] = snapshot
            metadata.pop("source_episode_projection", None)
            legacy = replace(result.plan, metadata=metadata)
            verification = PlanVerifier().verify(legacy)
            codes = {issue.code for issue in verification.issues}
            self.assertEqual(PlanStatus.INVALID, verification.status)
            self.assertIn("provider_episode_ranges_missing_replan_required", codes)


if __name__ == "__main__":
    unittest.main()
