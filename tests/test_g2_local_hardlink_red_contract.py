"""G2 RED contracts for the first bounded local hardlink execution.

These tests freeze the gap found by reading the real G1 S1 Plan: the
cross-provider snapshot owns a proposed local series key, but the production
Plan does not yet project the SeriesCatalog identity required by execution.
The contracts began as expected failures.  They remain ordinary regression
tests after the production wiring reaches green.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.decision_state import DecisionStateStore
from src.rename.plan_review import PlanReviewError, PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.provider_link_catalog import ProviderLinkCatalog
from src.rename.rename_plan import (
    ExecutionPolicy,
    PlanEntry,
    ProviderIdentity,
    RenamePlan,
    WorkIdentity,
)
from tests.presentation_fixtures import with_tv_presentation
from tests.projection_test_support import save_plan_fixture
from tests.test_r1_real_media_production_red_contract import _candidate, _make_season
from src.rename.series_catalog import SeriesCatalog


def red_contract(test):
    return test


class G2ProductionProjectionContracts(unittest.IsolatedAsyncioTestCase):
    @red_contract
    async def test_real_queue_s1_projects_one_series_identity_for_execution(self):
        """The real queue path must persist, not merely compute, execution identity."""

        from tests.test_r1_real_media_production_red_contract import (
            R1RealQueueWorkerContracts,
        )

        case = R1RealQueueWorkerContracts(
            methodName="test_four_real_shape_seasons_converge_to_one_series_and_s01_to_s04"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume",
            )
            result = await case._run_flow(
                root=root / "flow",
                source=source,
                task_id="g2-s1-series-projection",
                candidate=_candidate("41658"),
            )

        metadata = result.plan.metadata
        snapshot = metadata.get("provider_link_snapshot") or {}
        series_binding = result.state.series_binding or {}
        expected_key = str(snapshot.get("series_key") or "")
        problems = []
        if not expected_key:
            problems.append("cross-provider snapshot has no proposed series key")
        if metadata.get("series_key") != expected_key:
            problems.append("Plan series key does not match the cross-provider decision")
        if series_binding.get("series_key") != expected_key:
            problems.append("DecisionState has no matching series registration intent")
        if not metadata.get("season_binding_id"):
            problems.append("Plan has no durable season binding identity")
        if metadata.get("canonical_work_root") != str(
            result.plan.execution_policy.target_root
        ):
            problems.append("Plan canonical series root does not match its execution root")
        if metadata.get("series_catalog_revision") != 0:
            problems.append("new S1 series intent must start at catalog revision 0")
        self.assertEqual([], problems, "\n".join(problems))

    @red_contract
    async def test_real_queue_s1_executes_real_temp_hardlinks_and_closes_both_catalogs(self):
        """One production-generated S1 Plan must close media and both registrations."""

        from tests.test_r1_real_media_production_red_contract import (
            R1RealQueueWorkerContracts,
        )

        case = R1RealQueueWorkerContracts(
            methodName="test_four_real_shape_seasons_converge_to_one_series_and_s01_to_s04"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flow_root = root / "flow"
            source = _make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume",
            )
            font_archive = source / "[VCB-Studio] Yama no Susume [Fonts].7z"
            font_archive.write_bytes(b"g2-production-font-archive")
            original_bytes = {}
            for index, item in enumerate(sorted(source.rglob("*"))):
                if item.is_file():
                    payload = f"g2-real-hardlink-{index}-{item.name}".encode("utf-8")
                    item.write_bytes(payload)
                    original_bytes[item] = payload

            previous_dry_run = os.environ.get("BAR_DRY_RUN")
            os.environ["BAR_DRY_RUN"] = "false"
            try:
                generated = await case._run_flow(
                    root=flow_root,
                    source=source,
                    task_id="g2-s1-real-temp-hardlink",
                    candidate=_candidate("41658"),
                )
                plan = generated.plan_store.load("g2-s1-real-temp-hardlink")
                planned_font = next(
                    item for item in plan.entries
                    if item.source_path == font_archive
                )
                self.assertEqual("Associated", planned_font.target_path.parent.name)
                self.assertEqual(
                    "向山进发 (2013) [Fonts].7z",
                    planned_font.target_path.name,
                )
                self.assertIn(font_archive, plan.expected_sources)
                plan_fingerprint = generated.plan_store.fingerprint(plan)
                review = PlanReviewService(
                    generated.plan_store, flow_root / "reviews"
                )
                review.approve_entries(
                    plan.task_id,
                    plan_fingerprint,
                    tuple(entry_id(item) for item in plan.entries),
                )
                task_path = flow_root / "tasks" / f"{plan.task_id}.json"
                task = json.loads(task_path.read_text("utf-8"))
                task["workflow_status"] = "approved"
                task_path.write_text(
                    json.dumps(task, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                service = PlanExecutionService(
                    generated.plan_store,
                    review,
                    task_root=flow_root / "tasks",
                    record_root=flow_root / "records",
                    catalog_root=flow_root / "work_catalog",
                    series_catalog_root=flow_root / "series_catalog",
                    decision_state_root=flow_root / "decisions",
                    recovery_transaction_root=flow_root / "transactions",
                    provider_link_catalog_root=flow_root / "provider_links",
                )
                execution = service.execute(plan.task_id, plan_fingerprint)
            finally:
                if previous_dry_run is None:
                    os.environ.pop("BAR_DRY_RUN", None)
                else:
                    os.environ["BAR_DRY_RUN"] = previous_dry_run

            persisted_task = json.loads(task_path.read_text("utf-8"))
            persisted_state = DecisionStateStore(flow_root / "decisions").load(
                plan.task_id
            )
            persisted_plan = generated.plan_store.load(plan.task_id)
            snapshot = persisted_plan.metadata["provider_link_snapshot"]
            series_key = str(snapshot["series_key"])
            series = SeriesCatalog(flow_root / "series_catalog").load(series_key)
            provider_link = ProviderLinkCatalog(flow_root / "provider_links").load(
                str(snapshot["mapping_id"])
            )
            ledger = json.loads(execution.ledger_path.read_text("utf-8"))

            self.assertEqual("completed", execution.ledger_status)
            self.assertEqual("completed", persisted_task.get("workflow_status"))
            self.assertEqual(series_key, persisted_state.series_binding["series_key"])
            self.assertEqual(series_key, series.series_key)
            self.assertEqual(series_key, provider_link.local_series_key)
            self.assertEqual(
                str(persisted_plan.execution_policy.target_root),
                str(series.canonical_root),
            )
            self.assertEqual(
                [1],
                sorted(
                    int(item["season_number"])
                    for item in series.season_bindings
                ),
            )
            self.assertEqual("materialized_confirmed", provider_link.status)
            self.assertEqual("completed", ledger.get("status"))
            self.assertTrue(ledger.get("entries"))
            self.assertTrue(
                all(item.get("transfer_type") == "hardlink" for item in ledger["entries"])
            )
            for item in persisted_plan.entries:
                source_stat = item.source_path.stat()
                target_stat = item.target_path.stat()
                self.assertEqual(original_bytes[item.source_path], item.source_path.read_bytes())
                self.assertEqual(source_stat.st_dev, target_stat.st_dev)
                self.assertEqual(source_stat.st_ino, target_stat.st_ino)
                self.assertGreaterEqual(target_stat.st_nlink, 2)

    async def test_series_catalog_drift_is_rejected_before_temp_transfer(self):
        """A competing series registration after approval must not reach media."""

        from tests.test_r1_real_media_production_red_contract import (
            R1RealQueueWorkerContracts,
        )

        case = R1RealQueueWorkerContracts(
            methodName="test_four_real_shape_seasons_converge_to_one_series_and_s01_to_s04"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flow_root = root / "flow"
            source = _make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume",
            )
            generated = await case._run_flow(
                root=flow_root,
                source=source,
                task_id="g2-series-revision-drift",
                candidate=_candidate("41658"),
            )
            plan = generated.plan_store.load("g2-series-revision-drift")
            fingerprint = generated.plan_store.fingerprint(plan)
            review = PlanReviewService(generated.plan_store, flow_root / "reviews")
            review.approve_entries(
                plan.task_id,
                fingerprint,
                tuple(entry_id(item) for item in plan.entries),
            )
            task_path = flow_root / "tasks" / f"{plan.task_id}.json"
            task = json.loads(task_path.read_text("utf-8"))
            task["workflow_status"] = "approved"
            task_path.write_text(
                json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            SeriesCatalog(flow_root / "series_catalog").register_completed(
                plan, expected_revision=0
            )
            transfer_calls = []

            class TransferMustNotRun:
                def __init__(self, *_args, **_kwargs):
                    transfer_calls.append("constructed")

                def trans_file(self):
                    raise AssertionError("TRANSFER_REACHED_SERIES_DRIFT_SENTINEL")

            service = PlanExecutionService(
                generated.plan_store,
                review,
                task_root=flow_root / "tasks",
                record_root=flow_root / "records",
                transfer_factory=TransferMustNotRun,
                catalog_root=flow_root / "work_catalog",
                series_catalog_root=flow_root / "series_catalog",
                decision_state_root=flow_root / "decisions",
                recovery_transaction_root=flow_root / "transactions",
                provider_link_catalog_root=flow_root / "provider_links",
            )
            with self.assertRaisesRegex(PlanExecutionError, "作品季度登记"):
                service.execute(plan.task_id, fingerprint)
            self.assertEqual([], transfer_calls)
            self.assertTrue(all(not item.target_path.exists() for item in plan.entries))


class G2ExecutionSafetyContracts(unittest.TestCase):
    @red_contract
    def test_incomplete_crosswalk_tv_plan_is_rejected_before_transfer(self):
        """Missing series registration identity must fail before any media call."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            source_root.mkdir()
            source = source_root / "Episode 01.mkv"
            source.write_bytes(b"synthetic-g2-video")
            series_root = root / "library" / "向山进发 (2013)"
            target = series_root / "Season 01" / "向山进发 S01E01.mkv"
            proposed_series_key = f"series-{uuid.uuid4()}"
            plan = with_tv_presentation(RenamePlan(
                task_id="g2-incomplete-series-preflight",
                source_root=source_root,
                entries=(PlanEntry(
                    source,
                    target,
                    season_id=1,
                    episode_id=1,
                    media_kind="video",
                    role="main_episode",
                    requires_review=True,
                ),),
                expected_sources=(source,),
                metadata={
                    "target_root": str(series_root),
                    "season_id": 1,
                    "season_number": 1,
                    "season_source": "provider",
                    "provider_link_snapshot": {
                        "automation_level": "L2",
                        "mapping_id": f"provider-link-{uuid.uuid4()}",
                        "revision": 0,
                        "evidence_fingerprint": "a" * 64,
                        "status": "inferred",
                        "series_key": proposed_series_key,
                        "tmdb_tv_id": "53918",
                        "season_assignments": ({
                            "bangumi_subject_id": "41658",
                            "season_number": 1,
                        },),
                        "evidence_path": ("g2-red",),
                        "reason_codes": ("linear_one_to_one",),
                        "anchor": None,
                    },
                },
                execution_policy=ExecutionPolicy(
                    target_root=series_root,
                    dry_run=False,
                ),
                work_identity=WorkIdentity(
                    "confirmed",
                    "animation",
                    "向山进发",
                    2013,
                    ProviderIdentity(
                        "confirmed",
                        "tmdb",
                        "tv",
                        "53918",
                        "向山进发",
                        2013,
                        "explicit_user",
                    ),
                    "provider_selection",
                ),
            ))
            task_root = root / "task"
            task_root.mkdir()
            (task_root / f"{plan.task_id}.json").write_text(
                json.dumps({
                    "uuid": plan.task_id,
                    "path": str(source_root),
                    "workflow_status": "approved",
                    "completed_at": None,
                }),
                encoding="utf-8",
            )
            store = PlanStore(root / "plan")
            plan = save_plan_fixture(store, plan)
            review = PlanReviewService(store, root / "review")
            fingerprint = store.fingerprint(plan)
            transfer_calls = []

            class TransferMustNotRun:
                def __init__(self, *_args, **_kwargs):
                    transfer_calls.append("constructed")

                def trans_file(self):
                    raise AssertionError("TRANSFER_REACHED_G2_SENTINEL")

            with self.assertRaisesRegex(
                PlanReviewError,
                "tv_authority_incomplete",
            ):
                review.approve_entries(
                    plan.task_id,
                    fingerprint,
                    tuple(entry_id(item) for item in plan.entries),
                )
            service = PlanExecutionService(
                store,
                review,
                task_root=task_root,
                record_root=root / "record",
                transfer_factory=TransferMustNotRun,
                catalog_root=root / "work_catalog",
                series_catalog_root=root / "series_catalog",
                decision_state_root=root / "decision_state",
                recovery_transaction_root=root / "transactions",
                provider_link_catalog_root=root / "provider_link_catalog",
            )
            with self.assertRaisesRegex(PlanExecutionError, "作品季度登记"):
                service._validate_provider_link_preflight(plan)
            self.assertEqual([], transfer_calls)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
