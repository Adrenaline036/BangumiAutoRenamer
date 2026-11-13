"""R23-D incident-shaped RED contracts for the two fresh Phase-D P1s.

The fixture values are whitelist-sanitized derivatives of the fixed Phase-D
evidence.  Expected behavior is written here from the frozen contract; it is
never learned from the current projector or resolver implementation.
"""

from __future__ import annotations
from tests.queue_fixture import TaskQueueManager as ControlledQueue

import copy
import hashlib
import inspect
import json
import os
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.pages import add_task_page, plan_review_page
from src.pages.add_task_page import AddTaskDialog
from src.rename import provider_link_resolver
from src.rename.decision_state import DecisionStateError, DecisionStateStore
from src.rename.catalog_binding import binding_from_decision
from src.rename.plan_execution import PlanExecutionService
from src.rename.plan_store import PlanStore
from src.rename.projection_authority import (
    FinalPlanProjector,
    ProjectionAuthorityError,
    ProjectionAuthorityEnvelope,
    SourceIdentity as ProjectionSourceIdentity,
    SourceOwnershipRecord,
    TV_AUTHORITY_FIELD_RESPONSIBILITY,
)
from src.rename.provider_link_resolver import (
    ProviderCohortAnchorError,
    resolve_provider_link,
    trusted_relation_tmdb_anchor,
)
from src.rename.recovery_service import _series_catalog_materializes_plan
from src.rename.rename_plan import (
    ExecutionPolicy,
    PlanEntry,
    PresentationGroup,
    ProviderIdentity,
    RenamePlan,
    WorkIdentity,
)
from src.rename.task_batch_scope import (
    SourceIdentity as BatchSourceIdentity,
    TaskSourceScope,
    canonical_reservation_lineage_id,
    source_identity_set_fingerprint,
)
from src.rename.tv_planning_authority import (
    TV_PLANNING_AUTHORITY_POLICY_VERSION,
    TVPlanningAuthority,
)
from src.task_queue import TaskQueueManager
from tests.test_r23_a_e_gui_config_path_red_contract import MOUNT_HOST, _visible_values


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "r23_d_p1_incidents"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / name).read_text("utf-8"))


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _tv_authority_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            {"domain": "bar.tv-planning-authority", "value": value},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _refresh_subject_batch(batch: dict[str, object]) -> dict[str, object]:
    for observation in batch["subject_observations"]:
        payload = {
            key: value
            for key, value in observation.items()
            if key != "response_fingerprint"
        }
        observation["response_fingerprint"] = _canonical_sha256(payload)
    payload = {
        key: value for key, value in batch.items() if key != "evidence_fingerprint"
    }
    batch["evidence_fingerprint"] = _canonical_sha256(payload)
    return batch


def _resolve(case: dict[str, object], **overrides):
    values = {
        "selected_provider": case["selected_provider"],
        "bangumi_subjects": case["bangumi_subjects"],
        "bangumi_relations": case["bangumi_relations"],
        "subject_observations": case["subject_observations"],
    }
    values.update(overrides)
    return resolve_provider_link(**values)


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path












class FixtureShapeControls(unittest.TestCase):
    def test_sanitized_fixtures_have_one_common_fixed_lineage(self):
        fixtures = [
            _fixture("p1_b_cohort_anchor.json"),
            _fixture("entry_success_control.json"),
        ]
        self.assertEqual(
            1, len({item["source_evidence_fingerprint"] for item in fixtures})
        )
        self.assertEqual(1, len({item["sanitizer_sha256"] for item in fixtures}))
        self.assertEqual(5, fixtures[0]["source_shape"]["subject_observation_count"])
        self.assertEqual(6, fixtures[1]["picker_visit_count"])
        self.assertEqual(16, fixtures[1]["final_input_count"])

    def test_shape_manifest_describes_every_fixture_without_expected_learning(self):
        manifest = _fixture("fixture_shape_manifest.json")
        described = set(manifest["fixtures"])
        self.assertEqual(
            {
                "entry_success_control.json",
                "p1_b_cohort_anchor.json",
            },
            described,
        )
        self.assertNotIn("actual_output", json.dumps(manifest, ensure_ascii=False))




class CohortAnchorResolverIncidentRedContracts(unittest.TestCase):
    def test_p1_b_selected_no_match_uses_unique_season_covering_cohort_anchor(self):
        case = _fixture("p1_b_cohort_anchor.json")
        expected = case["frozen_expected"]
        resolution = _resolve(case)
        self.assertEqual(expected["status"], resolution.status)
        self.assertEqual(expected["tmdb_tv_id"], resolution.tmdb_tv_id)
        self.assertEqual(
            expected["selected_season_number"],
            dict(resolution.season_assignments)[expected["selected_subject_id"]],
        )
        self.assertEqual(expected["review_disposition"], resolution.review_disposition)
        self.assertEqual(
            {"330287", "330299"},
            {
                row["outside_tmdb_tv_id"]
                for row in resolution.boundaries
            },
        )

    def test_p1_b_positive_is_order_independent_and_repeat_deterministic(self):
        case = _fixture("p1_b_cohort_anchor.json")
        batch = copy.deepcopy(case["subject_observations"])
        batch["subject_observations"].reverse()
        _refresh_subject_batch(batch)
        forward = _resolve(case)
        reverse = _resolve(
            case,
            bangumi_subjects=tuple(reversed(case["bangumi_subjects"])),
            bangumi_relations=tuple(reversed(case["bangumi_relations"])),
            subject_observations=batch,
        )
        repeated = _resolve(case)
        signature = lambda value: (
            value.status,
            value.tmdb_tv_id,
            value.season_assignments,
            value.boundaries,
            value.resolution_fingerprint,
        )
        self.assertEqual(signature(forward), signature(reverse))
        self.assertEqual(signature(forward), signature(repeated))
        self.assertEqual("inferred", forward.status)

    def test_p1_b_same_id_conflicting_season_snapshot_fails_with_anchor_reason(self):
        case = _fixture("p1_b_cohort_anchor.json")
        batch = copy.deepcopy(case["subject_observations"])
        anchor = None
        for observation in batch["subject_observations"]:
            for query in observation["queries"]:
                for candidate in query["candidates"]:
                    if (
                        candidate.get("tmdb_tv_id") == "83121"
                        and candidate.get("match_tier") == "exact"
                    ):
                        anchor = copy.deepcopy(candidate)
                        break
        self.assertIsNotNone(anchor)
        anchor["seasons"][1]["episode_count"] += 7
        selected = next(
            row
            for row in batch["subject_observations"]
            if row["bangumi_subject_id"] == case["selected_provider"]["provider_id"]
        )
        selected["queries"][0].update(outcome="ok", candidates=[anchor])
        _refresh_subject_batch(batch)
        resolution = _resolve(case, subject_observations=batch)
        self.assertNotEqual("inferred", resolution.status)
        self.assertTrue(
            set(resolution.reason_codes)
            & {
                "cohort_anchor_snapshot_conflict",
                "subject_anchor_snapshot_conflict",
                "season_snapshot_conflict",
            }
        )

    def test_p1_b_no_anchor_stays_manual(self):
        case = _fixture("p1_b_cohort_anchor.json")
        batch = copy.deepcopy(case["subject_observations"])
        for observation in batch["subject_observations"]:
            for query in observation["queries"]:
                query.update(outcome="no_match", candidates=[])
        _refresh_subject_batch(batch)
        resolution = _resolve(case, subject_observations=batch)
        self.assertNotEqual("inferred", resolution.status)
        self.assertIn("subject_candidate_evidence_missing", resolution.reason_codes)

    def test_p1_b_transport_invalid_budget_and_truncated_fail_closed(self):
        case = _fixture("p1_b_cohort_anchor.json")
        selected_id = case["selected_provider"]["provider_id"]
        for outcome, expected_reason in (
            ("transport_error", "subject_candidate_transport_error"),
            ("invalid_response", "subject_candidate_invalid_response"),
            ("budget_exhausted", "subject_candidate_budget_exhausted"),
        ):
            with self.subTest(outcome=outcome):
                batch = copy.deepcopy(case["subject_observations"])
                selected = next(
                    row
                    for row in batch["subject_observations"]
                    if row["bangumi_subject_id"] == selected_id
                )
                selected["queries"][0].update(outcome=outcome, candidates=[])
                _refresh_subject_batch(batch)
                resolution = _resolve(case, subject_observations=batch)
                self.assertNotEqual("inferred", resolution.status)
                self.assertIn(expected_reason, resolution.reason_codes)
        batch = copy.deepcopy(case["subject_observations"])
        selected = next(
            row
            for row in batch["subject_observations"]
            if row["bangumi_subject_id"] == selected_id
        )
        selected["queries"][0]["truncated"] = True
        _refresh_subject_batch(batch)
        resolution = _resolve(case, subject_observations=batch)
        self.assertNotEqual("inferred", resolution.status)
        self.assertIn("subject_query_truncated", resolution.reason_codes)

    def test_p1_b_branch_and_cycle_fail_closed(self):
        case = _fixture("p1_b_cohort_anchor.json")
        branch = [
            *case["bangumi_relations"],
            {"from": "293049", "to": "425211", "relation": "sequel"},
        ]
        branch_resolution = _resolve(case, bangumi_relations=branch)
        self.assertIn("branching_sequel_graph", branch_resolution.reason_codes)
        cycle = [
            *case["bangumi_relations"],
            {"from": "565802", "to": "248175", "relation": "sequel"},
        ]
        cycle_resolution = _resolve(case, bangumi_relations=cycle)
        self.assertIn("cyclic_sequel_graph", cycle_resolution.reason_codes)


class MinimalGreenAntiBypassControls(unittest.TestCase):
    def test_tv_authority_responsibility_table_is_total_and_typed(self):
        self.assertEqual(
            {item.name for item in fields(TVPlanningAuthority)},
            set(TV_AUTHORITY_FIELD_RESPONSIBILITY),
        )
        self.assertEqual(
            "typed_plan_transform",
            TV_AUTHORITY_FIELD_RESPONSIBILITY["selected_season_number"],
        )


    def test_resolver_and_ui_share_anchor_and_disconnect_fail_closed(self):
        case = _fixture("p1_b_cohort_anchor.json")
        batch = case["subject_observations"]
        direct = trusted_relation_tmdb_anchor(batch, "83121")
        via_ui = plan_review_page._trusted_relation_tmdb_anchor(
            {"subject_observations": batch}, "83121"
        )
        self.assertEqual(direct, via_ui)
        self.assertIn(
            "trusted_relation_tmdb_anchor(",
            inspect.getsource(provider_link_resolver._resolve_subject_observations),
        )
        self.assertIn(
            "trusted_relation_tmdb_anchor(",
            inspect.getsource(plan_review_page._trusted_relation_tmdb_anchor),
        )

        disconnected = ProviderCohortAnchorError("cohort_anchor_disconnected")
        with patch.object(
            provider_link_resolver,
            "trusted_relation_tmdb_anchor",
            side_effect=disconnected,
        ):
            resolution = _resolve(case)
        self.assertNotEqual("inferred", resolution.status)
        self.assertIn("cohort_anchor_disconnected", resolution.reason_codes)

        with patch.object(
            plan_review_page,
            "trusted_relation_tmdb_anchor",
            side_effect=disconnected,
        ):
            with self.assertRaises(DecisionStateError):
                plan_review_page._trusted_relation_tmdb_anchor(
                    {"subject_observations": batch}, "83121"
                )


class MountedSixVisitEntrySuccessControl(unittest.IsolatedAsyncioTestCase):
    async def test_live_sequence_six_picker_visits_one_active_batch(self):
        contract = _fixture("entry_success_control.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual: dict[str, Path] = {}
            exact_child = contract["exact_child"]
            for visit in contract["picker_visits"]:
                for relative in visit:
                    path = root / relative
                    actual[relative] = path
                    if relative == exact_child:
                        _write(path, b"official-special")
                    else:
                        _write(path / "Episode.mkv", relative.encode("utf-8"))
            picker_results = [
                [str(actual[relative]) for relative in visit]
                for visit in contract["picker_visits"]
            ]
            manager = ControlledQueue(
                task_root=root / "state" / "tasks",
                decision_state_root=root / "state" / "decisions",
                recovery_abandon_audit_root=root / "state" / "abandon",
                plan_root=root / "state" / "plans",
                batch_scope_root=root / "state" / "batch_scope",
                test_mode=True,
            )
            with MOUNT_HOST:
                dialog = AddTaskDialog()
            notices: list[str] = []
            try:
                with patch.object(
                    add_task_page,
                    "local_file_picker",
                    new=AsyncMock(side_effect=picker_results),
                ):
                    for expected_count in contract["cumulative_selected_counts"]:
                        await dialog._browse()
                        self.assertEqual(
                            expected_count, len(dialog._submission_paths())
                        )
                visible = "\n".join(_visible_values(dialog))
                self.assertTrue(
                    all(str(actual[relative]) in visible for relative in actual)
                )
                with (patch.object(add_task_page, "task_queue", manager),
patch.object(
                        add_task_page,
                        "notify",
                        side_effect=lambda message, **_kwargs: notices.append(
                            str(message)
                        ),
                    )):
                    await dialog._next()
                    await dialog._submit()
            finally:
                dialog.close()
                await manager.stop()

            task_files = sorted((root / "state" / "tasks").glob("*.json"))
            records = [json.loads(path.read_text("utf-8")) for path in task_files]
            self.assertEqual(contract["final_input_count"], len(records))
            self.assertEqual(1, len({row["batch_id"] for row in records}))
            manifests = list((root / "state" / "batch_scope").rglob("*.json"))
            self.assertEqual(contract["expected_batch_count"], len(manifests))
            manifest = json.loads(manifests[0].read_text("utf-8"))
            self.assertEqual("active", manifest["state"])
            self.assertEqual(16, len(manifest["task_source_scopes"]))
            by_path = {row["path"]: row for row in records}
            parent = actual[contract["exact_child_parent"]]
            child = actual[exact_child]
            parent_keys = {
                (row["location_key"], row["physical_key"])
                for row in by_path[str(parent)]["task_source_scope"]["source_identities"]
            }
            child_keys = {
                (row["location_key"], row["physical_key"])
                for row in by_path[str(child)]["task_source_scope"]["source_identities"]
            }
            self.assertEqual(1, len(child_keys))
            self.assertTrue(parent_keys.isdisjoint(child_keys))
            self.assertEqual(contract["submit_count"], 1)
            self.assertTrue(notices)


class RedWrapperIntegrityControls(unittest.TestCase):
    def test_red_wrapper_is_removed(self):
        module = inspect.getmodule(self)
        self.assertFalse(hasattr(module, "STRICT"))
        self.assertFalse(hasattr(module, "incident" + "_red"))
        source = inspect.getsource(inspect.getmodule(self))
        decorators = [
            line.strip()
            for line in source.splitlines()
            if line.lstrip().startswith("@")
        ]
        self.assertFalse(any(line.startswith("@unittest.skip") for line in decorators))
        self.assertFalse(
            any(line.startswith("@unittest.expectedFailure") for line in decorators)
        )


if __name__ == "__main__":
    unittest.main()
