"""R23-D incident-shaped RED contracts for the two fresh Phase-D P1s.

The fixture values are whitelist-sanitized derivatives of the fixed Phase-D
evidence.  Expected behavior is written here from the frozen contract; it is
never learned from the current projector or resolver implementation.
"""

from __future__ import annotations

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
from src.rename import projection_authority, provider_link_resolver
from src.rename.decision_state import DecisionStateError, DecisionStateStore
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
    PlanEntry,
    PresentationGroup,
    ProviderIdentity,
    RenamePlan,
    WorkIdentity,
)
from src.rename.series_catalog import _validated_associated_manifest_rows
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


def _authority_for_case(
    case: dict[str, object], *, source: Path, video: Path
) -> TVPlanningAuthority:
    values = case["fixture_values"]
    identities = (
        BatchSourceIdentity.from_mapping({
            "source_path": str(source),
            "location_key": "location-font",
            "physical_key": "physical-font",
            "size": source.stat().st_size,
            "mtime_ns": source.stat().st_mtime_ns,
        }),
        BatchSourceIdentity.from_mapping({
            "source_path": str(video),
            "location_key": "location-video",
            "physical_key": "physical-video",
            "size": video.stat().st_size,
            "mtime_ns": video.stat().st_mtime_ns,
        }),
    )
    set_fingerprint = source_identity_set_fingerprint(identities)
    batch_fingerprint = "a" * 64
    scope = TaskSourceScope(
        schema_version=1,
        task_id="incident-tv-font",
        media_domain="tv",
        batch_scope_fingerprint=batch_fingerprint,
        reservation_lineage_id=canonical_reservation_lineage_id(
            owner_task_id="incident-tv-font",
            origin_batch_scope_fingerprint=batch_fingerprint,
            source_identity_set_fingerprint=set_fingerprint,
        ),
        reservation_revision=0,
        source_identity_set_fingerprint=set_fingerprint,
        source_identities=identities,
    )
    body = {
        "schema_version": 1,
        "policy_version": TV_PLANNING_AUTHORITY_POLICY_VERSION,
        "task_source_scope": scope.to_dict(),
        "decision_revision": 1,
        "observation_refs": ["observation-fixture"],
        "work_identity": {"provider_id": "83121"},
        "naming_authority": {"canonical_title": "Series Fixture"},
        "season_assignments": {"subject-fixture": int(values["season_number"])},
        "selected_season_number": int(values["season_number"]),
        "episode_range_ownership": [],
        "file_decisions": {},
        "non_regular_file_decisions": {},
        "associated_asset_manifest": {
            "schema_version": 1,
            "scope": "series",
            "series_key": values["series_key"],
            "entries": [{
                "source": str(source),
                "target_name": values["target_name"],
                "size": int(values["source_size"]),
                "content_sha256": values["source_sha256"],
                "content_key": values["source_sha256"],
            }],
        },
        "derived_provenance": {"source": "incident-fixture"},
    }
    return TVPlanningAuthority.from_mapping({
        **body,
        "fingerprint": _tv_authority_sha256(body),
    })


def _plan_case(root: Path, case: dict[str, object]):
    values = case["fixture_values"]
    source_root = root / "source"
    target_root = root / "library"
    archive_suffix = str(values["archive_suffix"])
    source_name = (
        "Source Archive.zip"
        if archive_suffix.casefold() == ".zip"
        else f"Source [Fonts]{archive_suffix}"
    )
    font = _write(
        source_root / source_name,
        str(values["font_payload_utf8"]).encode("utf-8"),
    )
    video = _write(source_root / "Episode 01.mkv", b"video-fixture")
    canonical_root = target_root / "Series Fixture (2001) [tmdbid-83121]"
    font_target = canonical_root / "Associated" / str(values["target_name"])
    video_target = canonical_root / "Season 01" / "S01E01.mkv"
    provider = ProviderIdentity(
        status="confirmed",
        namespace="tmdb",
        entity_type="tv",
        provider_id="83121",
        title="Series Fixture",
        year=2001,
        decision_source="incident_fixture",
    )
    work = WorkIdentity(
        status="confirmed",
        domain="animation",
        title="Series Fixture",
        year=2001,
        primary_provider=provider,
        domain_source="incident_fixture",
    )
    draft = RenamePlan(
        task_id="incident-tv-font",
        source_root=source_root,
        entries=(
            PlanEntry(video, video_target, 1, 1, "video", "main"),
            PlanEntry(font, font_target, None, None, "associated", "font"),
        ),
        expected_sources=(video, font),
        metadata={
            "canonical_title": "Series Fixture",
            "canonical_work_root": str(canonical_root),
            "target_root": str(target_root),
            "season_number": int(values["season_number"]),
            "season_binding_id": "season-binding-fixture",
            "season_source": "provider_crosswalk",
            "series_key": values["series_key"],
            "tv_authority_required": True,
        },
        presentation_groups=(
            PresentationGroup(
                group_id="tv-fixture",
                kind="tv_episodes",
                display_name="Season Fixture",
                preferred_version_ref=None,
            ),
        ),
        work_identity=work,
    )
    authority = _authority_for_case(case, source=font, video=video)
    state = SimpleNamespace(
        tv_planning_authority=authority,
        schema_version=4,
        revision=1,
        series_binding={},
        season_binding={},
    )
    inventory = "b" * 64
    ownership = SourceOwnershipRecord(
        task_id=draft.task_id,
        revision=1,
        decision_revision=1,
        inventory_fingerprint=inventory,
        identities=tuple(
            ProjectionSourceIdentity.from_path(path, inventory_fingerprint=inventory)
            for path in draft.expected_sources
        ),
    )
    envelope = ProjectionAuthorityEnvelope(
        previous_decision=None,
        decision=state,
        draft_plan=draft,
        inventory_fingerprint=inventory,
    )
    return draft, envelope, ownership, font, font_target


def _project(envelope, ownership):
    with (
        patch.object(
            DecisionStateStore,
            "project_plan",
            return_value=envelope.draft_plan,
        ),
        patch.object(DecisionStateStore, "fingerprint", return_value="c" * 64),
    ):
        return FinalPlanProjector.project(envelope, ownership)


def _expected_manifest(case, source: Path, target: Path) -> dict[str, object]:
    values = case["fixture_values"]
    return {
        "schema_version": 1,
        "assets": [{
            "role": "fonts",
            "scope": "series",
            "season_number": int(values["season_number"]),
            "source_path": str(source),
            "target_path": str(target),
            "source_size": int(values["source_size"]),
            "source_sha256": values["source_sha256"],
        }],
    }


def _persisted_manifest(plan: RenamePlan):
    return plan.to_dict()["metadata"].get("associated_asset_manifest")


class FixtureShapeControls(unittest.TestCase):
    def test_sanitized_fixtures_have_one_common_fixed_lineage(self):
        fixtures = [
            _fixture("p1_a_tv_font_projection.json"),
            _fixture("p1_b_cohort_anchor.json"),
            _fixture("entry_success_control.json"),
        ]
        self.assertEqual(
            1, len({item["source_evidence_fingerprint"] for item in fixtures})
        )
        self.assertEqual(1, len({item["sanitizer_sha256"] for item in fixtures}))
        self.assertEqual(7, fixtures[0]["case_count"])
        self.assertEqual(5, fixtures[1]["source_shape"]["subject_observation_count"])
        self.assertEqual(6, fixtures[2]["picker_visit_count"])
        self.assertEqual(16, fixtures[2]["final_input_count"])

    def test_shape_manifest_describes_every_fixture_without_expected_learning(self):
        manifest = _fixture("fixture_shape_manifest.json")
        described = set(manifest["fixtures"])
        self.assertEqual(
            {
                "entry_success_control.json",
                "p1_a_tv_font_projection.json",
                "p1_b_cohort_anchor.json",
            },
            described,
        )
        self.assertNotIn("actual_output", json.dumps(manifest, ensure_ascii=False))


class TVAuthorityFontProjectionIncidentRedContracts(unittest.TestCase):
    def test_p1_a_all_seven_real_shapes_project_typed_plan_manifest(self):
        cases = _fixture("p1_a_tv_font_projection.json")["cases"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected_manifests = []
            actual_manifests = []
            for case in cases:
                draft, envelope, ownership, source, target = _plan_case(
                    root / case["case_id"], case
                )
                projected = _project(envelope, ownership)
                self.assertNotIn("associated_asset_manifest", draft.metadata)
                expected_manifests.append(_expected_manifest(case, source, target))
                actual_manifests.append(_persisted_manifest(projected))
            self.assertEqual(expected_manifests, actual_manifests)

    def test_p1_a_manifest_survives_persistence_and_plan_revision(self):
        case = _fixture("p1_a_tv_font_projection.json")["cases"][0]
        with tempfile.TemporaryDirectory() as directory:
            draft, envelope, ownership, source, target = _plan_case(
                Path(directory), case
            )
            projected = _project(envelope, ownership)
            restarted = RenamePlan.from_dict(projected.to_dict())
            expected = _expected_manifest(case, source, target)
            self.assertEqual(expected, _persisted_manifest(restarted))
            revised_metadata = dict(restarted.metadata)
            revised_metadata.pop("associated_asset_manifest", None)
            revised_draft = replace(restarted, metadata=revised_metadata)
            revised = FinalPlanProjector.revise(restarted, revised_draft)
            self.assertEqual(expected, _persisted_manifest(revised))

    def test_p1_a_execution_catalog_and_recovery_consume_identical_rows(self):
        case = _fixture("p1_a_tv_font_projection.json")["cases"][0]
        with tempfile.TemporaryDirectory() as directory:
            _draft, envelope, ownership, source, target = _plan_case(
                Path(directory), case
            )
            target.parent.mkdir(parents=True)
            os.link(source, target)
            projected = _project(envelope, ownership)
            expected = _expected_manifest(case, source, target)
            self.assertEqual(expected, _persisted_manifest(projected))
            PlanExecutionService._validate_frozen_associated_assets(projected)
            catalog_rows = _validated_associated_manifest_rows(
                projected,
                season_number=int(case["fixture_values"]["season_number"]),
            )
            self.assertEqual(1, len(catalog_rows))
            fingerprint = PlanStore.fingerprint(projected)
            row = expected["assets"][0]
            match = SimpleNamespace(
                season_bindings=({
                    "season_binding_id": "season-binding-fixture",
                    "season_number": int(row["season_number"]),
                    "task_id": projected.task_id,
                    "decision_source": "provider_crosswalk",
                    "provider_identity": projected.work_identity.primary_provider.to_dict(),
                },),
                associated_assets=({
                    "target_path": str(target),
                    "size": row["source_size"],
                    "sha256": row["source_sha256"],
                    "associations": [{
                        "task_id": projected.task_id,
                        "season_number": row["season_number"],
                        "plan_fingerprint": fingerprint,
                        "source_path": str(source),
                        "source_size": row["source_size"],
                        "source_sha256": row["source_sha256"],
                    }],
                },),
            )
            self.assertTrue(_series_catalog_materializes_plan(match, projected))

    def test_p1_a_current_v4_missing_manifest_cannot_use_legacy_recovery(self):
        case = _fixture("p1_a_tv_font_projection.json")["cases"][0]
        with tempfile.TemporaryDirectory() as directory:
            draft, _envelope, _ownership, source, target = _plan_case(
                Path(directory), case
            )
            target.parent.mkdir(parents=True)
            os.link(source, target)
            fingerprint = PlanStore.fingerprint(draft)
            ledger = {
                "status": "completed",
                "task_id": draft.task_id,
                "plan_fingerprint": fingerprint,
                "entries": [
                    {
                        "source": str(entry.source_path),
                        "target": str(entry.target_path),
                        "status": "created",
                        "transfer_type": "hardlink",
                    }
                    for entry in draft.entries
                ],
            }
            values = case["fixture_values"]
            match = SimpleNamespace(
                season_bindings=({
                    "season_binding_id": "season-binding-fixture",
                    "season_number": int(values["season_number"]),
                    "task_id": draft.task_id,
                    "decision_source": "provider_crosswalk",
                    "provider_identity": draft.work_identity.primary_provider.to_dict(),
                },),
                associated_assets=({
                    "target_path": str(target),
                    "size": int(values["source_size"]),
                    "sha256": values["source_sha256"],
                    "associations": [{
                        "task_id": draft.task_id,
                        "season_number": int(values["season_number"]),
                        "plan_fingerprint": fingerprint,
                        "source_path": str(source),
                        "source_size": int(values["source_size"]),
                        "source_sha256": values["source_sha256"],
                    }],
                },),
            )
            self.assertFalse(
                _series_catalog_materializes_plan(
                    match, draft, completed_ledger=ledger
                )
            )

    def test_p1_a_projector_canonicalizes_legacy_target_and_blank_zip_role(self):
        case = next(
            row
            for row in _fixture("p1_a_tv_font_projection.json")["cases"]
            if row["fixture_values"]["archive_suffix"] == ".zip"
        )
        with tempfile.TemporaryDirectory() as directory:
            draft, envelope, ownership, source, target = _plan_case(
                Path(directory), case
            )
            legacy_entries = tuple(
                replace(
                    entry,
                    target_path=entry.target_path.with_name("Legacy Package.zip"),
                    role="",
                )
                if entry.source_path == source else entry
                for entry in draft.entries
            )
            legacy_draft = replace(draft, entries=legacy_entries)
            projected = _project(
                replace(envelope, draft_plan=legacy_draft), ownership
            )
            projected_font = next(
                entry for entry in projected.entries if entry.source_path == source
            )
            self.assertEqual("associated", projected_font.media_kind)
            self.assertEqual("font", projected_font.role)
            self.assertEqual(target, projected_font.target_path)
            self.assertEqual(
                _expected_manifest(case, source, target),
                _persisted_manifest(projected),
            )


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
            TV_AUTHORITY_FIELD_RESPONSIBILITY["associated_asset_manifest"],
        )
        self.assertEqual(
            "typed_plan_transform",
            TV_AUTHORITY_FIELD_RESPONSIBILITY["selected_season_number"],
        )

    def test_tv_projector_disconnect_fails_closed(self):
        case = _fixture("p1_a_tv_font_projection.json")["cases"][0]
        with tempfile.TemporaryDirectory() as directory:
            _draft, envelope, ownership, _source, _target = _plan_case(
                Path(directory), case
            )
            with patch.object(
                projection_authority,
                "_project_tv_associated_asset_manifest",
                side_effect=ProjectionAuthorityError("projector disconnected"),
            ):
                with self.assertRaisesRegex(
                    ProjectionAuthorityError, "projector disconnected"
                ):
                    _project(envelope, ownership)

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
            manager = TaskQueueManager(
                task_root=root / "state" / "tasks",
                decision_state_root=root / "state" / "decisions",
                recovery_abandon_audit_root=root / "state" / "abandon",
                plan_root=root / "state" / "plans",
                batch_scope_root=root / "state" / "batch_scope",
                test_mode=True,
            )
            with patch.object(add_task_page, "is_ui_test_mode", return_value=False):
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
                dialog._next()
                with (
                    patch.object(add_task_page, "task_queue", manager),
                    patch.object(add_task_page, "is_ui_test_mode", return_value=True),
                    patch.object(
                        add_task_page,
                        "notify",
                        side_effect=lambda message, **_kwargs: notices.append(
                            str(message)
                        ),
                    ),
                ):
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
