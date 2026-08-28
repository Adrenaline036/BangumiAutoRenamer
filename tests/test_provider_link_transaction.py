import hashlib
import json
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from src.rename.decision_state import DecisionState, DecisionStateStore
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_review import PlanReviewService
from src.rename.plan_store import PlanStore
from src.rename.provider_link_catalog import ProviderLinkCatalog, ProviderLinkMapping
from src.rename.provider_link_decision import stage_provider_link_decision
from src.rename.provider_link_resolver import ProviderLinkResolution
from src.rename.provider_link_transaction import apply_provider_link_recovery
from src.rename.rename_plan import RenamePlan
from src.rename.rename_plan import (
    ExecutionPolicy,
    PlanEntry,
    ProviderIdentity,
    WorkIdentity,
)
from tests.presentation_fixtures import with_tv_presentation
from tests.projection_test_support import save_plan_fixture


def _fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _anchor(mapping_id, series_key):
    evidence = {"fixture": "anchor"}
    return ProviderLinkMapping.from_dict({
        "schema_version": 1,
        "mapping_id": mapping_id,
        "revision": 0,
        "local_series_key": series_key,
        "status": "materialized_confirmed",
        "provider_edges": (
            {"namespace": "tmdb", "entity_type": "tv", "provider_id": "4242", "role": "series"},
            {"namespace": "bangumi", "entity_type": "subject", "provider_id": "1", "role": "season"},
        ),
        "season_edges": (
            {"namespace": "bangumi", "entity_type": "subject", "provider_id": "1", "tmdb_tv_id": "4242", "season_number": 1},
        ),
        "evidence_snapshot": evidence,
        "evidence_fingerprint": _fingerprint(evidence),
    })


class ProviderLinkG4TransactionTests(unittest.TestCase):
    def _service(self, root):
        store = PlanStore(root / "plans")
        review = PlanReviewService(store, root / "reviews")
        return PlanExecutionService(
            store,
            review,
            task_root=root / "tasks",
            record_root=root / "records",
            catalog_root=root / "work-catalog",
            series_catalog_root=root / "series-catalog",
            decision_state_root=root / "decision",
            recovery_transaction_root=root / "recovery",
            provider_link_catalog_root=root / "provider-links",
        )

    def test_preflight_uses_nested_anchor_even_when_top_snapshot_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            mapping_id = f"provider-link-{uuid.uuid4()}"
            anchor = service.provider_link_catalog.save(
                _anchor(mapping_id, f"series-{uuid.uuid4()}"), expected_revision=-1
            )
            binding = {
                "mapping_id": mapping_id,
                "revision": 0,
                "evidence_fingerprint": _fingerprint(
                    {"fixture": "same-inference-snapshot"}
                ),
                "status": "inferred",
                "automation_level": "L1",
                "series_key": anchor.local_series_key,
                "tmdb_tv_id": "4242",
                "season_assignments": ({"bangumi_subject_id": "2", "season_number": 2},),
                "evidence_path": ("proof",),
                "anchor": {
                    "mapping_id": mapping_id,
                    "revision": anchor.revision,
                    "evidence_fingerprint": anchor.evidence_fingerprint,
                },
            }
            canonical_root = root / "library" / "Anchored Show (2024)"
            source_root = root / "source"
            source_root.mkdir()
            source = source_root / "Episode 02.mkv"
            source.write_bytes(b"anchor-preflight")
            work_identity = WorkIdentity(
                "confirmed",
                "animation",
                "Anchored Show",
                2024,
                ProviderIdentity(
                    "confirmed", "tmdb", "tv", "4242", "Anchored Show",
                    2024, "explicit_user",
                ),
                "provider_selection",
            )
            existing = save_plan_fixture(
                PlanStore(root / "series-fixture-plans"),
                RenamePlan(
                    task_id="anchor-preflight-existing-series",
                    source_root=source_root,
                    entries=(),
                    metadata={
                        "series_key": anchor.local_series_key,
                        "season_binding_id": "season-binding-anchor-existing",
                        "season_number": 1,
                        "season_source": "provider_crosswalk",
                        "canonical_work_root": str(canonical_root),
                    },
                    work_identity=work_identity,
                ),
            )
            service.series_catalog.register_completed(existing, expected_revision=0)
            state_store = DecisionStateStore(root / "decision")
            state = state_store.save(
                DecisionState(
                    task_id="anchor-preflight",
                    confirmed_work=work_identity.to_dict(),
                    provider_evidence=(
                        work_identity.primary_provider.to_dict(),
                    ),
                    series_binding={
                        "series_key": anchor.local_series_key,
                        "canonical_root": str(canonical_root),
                        "catalog_revision": 1,
                        "decision_source": "provider_crosswalk",
                    },
                    season_binding={
                        "season_binding_id": "season-binding-anchor-preflight",
                        "season_number": 2,
                        "decision_source": "provider_crosswalk",
                    },
                    provider_link_binding=binding,
                    library_domain="animation",
                    layout_mode="tv_episodes",
                ),
                expected_revision=-1,
            )
            plan = with_tv_presentation(RenamePlan(
                task_id="anchor-preflight",
                source_root=source_root,
                entries=(PlanEntry(
                    source,
                    canonical_root / "Season 02" / "Anchored Show S02E01.mkv",
                    season_id=2,
                    episode_id=1,
                    media_kind="video",
                    role="main_episode",
                ),),
                metadata={
                    "target_root": str(canonical_root),
                    "season_number": 2,
                    "season_source": "provider_crosswalk",
                },
                execution_policy=ExecutionPolicy(
                    target_root=canonical_root,
                    dry_run=False,
                ),
                work_identity=work_identity,
            ))
            plan = state_store.project_plan(state, plan)
            service._validate_provider_link_preflight(plan)
            service.provider_link_catalog.save(
                replace(anchor, status="user_confirmed"),
                expected_revision=anchor.revision,
            )

            with self.assertRaisesRegex(PlanExecutionError, "作品季度关系已变化"):
                service._validate_provider_link_preflight(plan)

    def test_l2_materialization_creates_reusable_catalog_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            resolution = ProviderLinkResolution(
                automation_level="L2",
                status="inferred",
                tmdb_tv_id="4242",
                proposed_series_key=f"series-{uuid.uuid4()}",
                season_assignments=(("1", 1), ("2", 2)),
                requires_plan_review=True,
                evidence_path=("one", "two"),
                reason_codes=("linear_one_to_one",),
            )
            state = stage_provider_link_decision(
                DecisionState(task_id="l2-materialize"), resolution
            )
            plan = DecisionStateStore(root / "decision").project_plan(
                state,
                RenamePlan(task_id=state.task_id, source_root=root / "source", entries=()),
            )
            service._materialize_provider_link(plan)
            binding = plan.metadata["provider_link_snapshot"]
            persisted = service.provider_link_catalog.load(str(binding["mapping_id"]))

            self.assertEqual("materialized_confirmed", persisted.status)
            self.assertTrue(persisted.reusable_offline)
            self.assertEqual({1, 2}, {int(edge["season_number"]) for edge in persisted.season_edges})

    def test_l1_materialization_merges_new_season_without_replacing_series_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            mapping_id = f"provider-link-{uuid.uuid4()}"
            series_key = f"series-{uuid.uuid4()}"
            anchor = service.provider_link_catalog.save(
                _anchor(mapping_id, series_key), expected_revision=-1
            )
            resolution = ProviderLinkResolution(
                automation_level="L1",
                status="inferred",
                tmdb_tv_id="4242",
                proposed_series_key=series_key,
                series_key=series_key,
                season_assignments=(("1", 1), ("2", 2)),
                requires_plan_review=True,
                evidence_path=("one", "two"),
                reason_codes=("anchored_confirmed_series",),
                anchor_mapping_id=anchor.mapping_id,
                anchor_revision=anchor.revision,
                anchor_fingerprint=anchor.evidence_fingerprint,
            )
            state = stage_provider_link_decision(
                DecisionState(task_id="l1-materialize"), resolution
            )
            plan = DecisionStateStore(root / "decision").project_plan(
                state,
                RenamePlan(task_id=state.task_id, source_root=root / "source", entries=()),
            )
            service._materialize_provider_link(plan)
            persisted = service.provider_link_catalog.load(mapping_id)

            self.assertEqual(series_key, persisted.local_series_key)
            self.assertEqual(1, persisted.revision)
            self.assertEqual({"1", "2"}, {str(edge["provider_id"]) for edge in persisted.season_edges})
            evidence = persisted.to_dict()["evidence_snapshot"]
            self.assertEqual([{"fixture": "anchor"}], evidence["evidence_history"])
            self.assertEqual("L1", evidence["latest_evidence"]["automation_level"])
            self.assertNotEqual(
                plan.metadata["provider_link_snapshot"]["evidence_fingerprint"],
                persisted.evidence_fingerprint,
            )

            second_resolution = replace(
                resolution,
                season_assignments=(("1", 1), ("2", 2), ("3", 3)),
                evidence_path=("one", "two", "three"),
                anchor_revision=persisted.revision,
                anchor_fingerprint=persisted.evidence_fingerprint,
            )
            second_state = stage_provider_link_decision(
                DecisionState(task_id="l1-materialize-second"), second_resolution
            )
            second_plan = DecisionStateStore(root / "decision").project_plan(
                second_state,
                RenamePlan(
                    task_id=second_state.task_id,
                    source_root=root / "source-two",
                    entries=(),
                ),
            )
            service._materialize_provider_link(second_plan)
            twice_persisted = service.provider_link_catalog.load(mapping_id)
            twice_evidence = twice_persisted.to_dict()["evidence_snapshot"]

            self.assertEqual(2, twice_persisted.revision)
            self.assertEqual(2, len(twice_evidence["evidence_history"]))
            self.assertEqual(
                {"fixture": "anchor"}, twice_evidence["evidence_history"][0]
            )
            self.assertEqual(
                ("one", "two"),
                tuple(twice_evidence["evidence_history"][1]["evidence_path"]),
            )
            self.assertEqual(
                ("one", "two", "three"),
                tuple(twice_evidence["latest_evidence"]["evidence_path"]),
            )

    def test_completed_ledger_recovery_only_calls_missing_fact_writers(self):
        mapping = _anchor(
            f"provider-link-{uuid.uuid4()}", f"series-{uuid.uuid4()}"
        )
        calls = []

        recovery = apply_provider_link_recovery(
            ledger_status="completed",
            series_catalog_status="registered",
            provider_link_catalog_status="missing",
            decision_status="catalog_update_pending",
            current_mapping=replace(mapping, status="user_confirmed"),
            write_series_catalog=lambda: calls.append("series"),
            write_provider_link_catalog=lambda: calls.append("provider-link"),
            write_decision=lambda status: calls.append(f"decision:{status}"),
        )

        self.assertFalse(recovery.invoke_transfer)
        self.assertEqual(
            ["provider-link", "decision:materialized_confirmed"], calls
        )

    def test_completed_recovery_is_idempotent_and_writes_nothing(self):
        mapping = _anchor(
            f"provider-link-{uuid.uuid4()}", f"series-{uuid.uuid4()}"
        )
        calls = []

        recovery = apply_provider_link_recovery(
            ledger_status="completed",
            series_catalog_status="registered",
            provider_link_catalog_status="registered",
            decision_status="healthy",
            current_mapping=mapping,
            write_series_catalog=lambda: calls.append("series"),
            write_provider_link_catalog=lambda: calls.append("provider-link"),
            write_decision=lambda status: calls.append(f"decision:{status}"),
        )

        self.assertEqual("completed", recovery.final_task_status)
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
