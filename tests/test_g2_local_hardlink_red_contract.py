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
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.provider_link_catalog import ProviderLinkCatalog
from src.rename.rename_plan import (
    ExecutionPolicy,
    PlanEntry,
    ProviderIdentity,
    RenamePlan,
    WorkIdentity,
)
from tests.presentation_fixtures import with_tv_presentation
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
                is_anime=True,
                is_movie=False,
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
        canonical_root = Path(metadata['canonical_work_root'])
        if not canonical_root.is_relative_to(result.plan.execution_policy.target_root):
            problems.append("Plan canonical series root escapes its execution boundary")
        if any(not entry.target_path.is_relative_to(canonical_root) for entry in result.plan.entries):
            problems.append("Plan entry escapes its canonical series root")
        if metadata.get("series_catalog_revision") != 0:
            problems.append("new S1 series intent must start at catalog revision 0")
        self.assertEqual([], problems, "\n".join(problems))

    @red_contract
    async def test_real_queue_s1_executes_real_temp_hardlinks_and_closes_both_catalogs(self):
        """One production-generated S1 Plan must close media and both registrations."""

        from tests import _media_flow_helpers as font_fixture

        case = font_fixture.MediaFlow()
        with tempfile.TemporaryDirectory() as directory:
            flow_root = Path(directory) / "flow"
            generated, font_archive = await case._materialize_s1(flow_root)
            plan = PlanStore(flow_root / "plans").load(generated.plan.task_id)
            task_path = flow_root / "tasks" / f"{plan.task_id}.json"
            persisted_task = json.loads(task_path.read_text("utf-8"))
            persisted_state = DecisionStateStore(flow_root / "decisions").load(plan.task_id)
            snapshot = plan.metadata["provider_link_snapshot"]
            series_key = str(snapshot["series_key"])
            series = SeriesCatalog(flow_root / "series_catalog").load(series_key)
            provider_link = ProviderLinkCatalog(flow_root / "provider_links").load(str(snapshot["mapping_id"]))
            ledger = json.loads((flow_root / "records" / f"{plan.task_id}.ledger.json").read_text("utf-8"))

            self.assertEqual("completed", persisted_task.get("workflow_status"))
            self.assertEqual("completed", persisted_task.get("execution_status"))
            self.assertEqual(series_key, persisted_state.series_binding["series_key"])
            self.assertEqual(series_key, series.series_key)
            self.assertEqual(series_key, provider_link.local_series_key)
            self.assertEqual(plan.metadata['canonical_work_root'], str(series.canonical_root))
            self.assertTrue(series.canonical_root.is_relative_to(plan.execution_policy.target_root))
            self.assertEqual([1], sorted(int(row["season_number"]) for row in series.season_bindings))
            self.assertEqual("materialized_confirmed", provider_link.status)
            self.assertEqual("completed", ledger["status"])
            self.assertTrue(ledger["entries"])
            self.assertTrue(all(row["transfer_type"] == "hardlink" for row in ledger["entries"]))

            # Raw archives belong to the separately completed contribution, never
            # the media hardlink ledger or Catalog associated-asset writer.
            self.assertIn(font_archive, plan.expected_sources)
            self.assertNotIn(font_archive, {entry.source_path for entry in plan.entries})
            self.assertFalse(any(row["source"] == str(font_archive) for row in ledger["entries"]))
            self.assertEqual((), series.associated_assets)
            for entry in plan.entries:
                expected = entry.source_path.read_bytes()
                self.assertEqual(expected, entry.source_path.read_bytes())
                self.assertEqual(expected, entry.target_path.read_bytes())
                source_stat, target_stat = entry.source_path.stat(), entry.target_path.stat()
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
                is_anime=True,
                is_movie=False,
            )
            plan = generated.plan_store.load("g2-series-revision-drift")
            fingerprint = generated.plan_store.fingerprint(plan)
            review = PlanReviewService(generated.plan_store, flow_root / "reviews", flow_root / 'work_catalog',
                decision_state_root=flow_root / 'decisions', task_root=flow_root / 'tasks')
            fingerprint = generated.plan_store.fingerprint(plan)
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
            # Registering the same slot is no longer a conflict by itself.
            # A changed authoritative root must still stop before transfer.
            catalog_path = SeriesCatalog(flow_root / "series_catalog")._path(plan.metadata['series_key'])
            damaged = json.loads(catalog_path.read_text('utf-8'))
            damaged['canonical_root'] += '-foreign'
            catalog_path.write_text(json.dumps(damaged), encoding='utf-8')
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
            with self.assertRaisesRegex(PlanExecutionError, "作品根|作品季度登记"):
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
            # This deliberately incomplete structural object cannot be saved as
            # a current approved Plan. Keep its missing registration
            # authority explicit instead of inventing either in a fixture.
            codes = {issue.code for issue in PlanVerifier().verify(plan).issues}
            self.assertIn("tv_authority_incomplete", codes)
            store = PlanStore(root / "plan")
            review = PlanReviewService(store, root / "review")
            transfer_calls = []

            class TransferMustNotRun:
                def __init__(self, *_args, **_kwargs):
                    transfer_calls.append("constructed")

                def trans_file(self):
                    raise AssertionError("TRANSFER_REACHED_G2_SENTINEL")

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
            self.assertFalse((task_root / f"{plan.task_id}.json").exists())
            self.assertFalse((root / "plan" / f"{plan.task_id}.json").exists())


if __name__ == "__main__":
    unittest.main()
