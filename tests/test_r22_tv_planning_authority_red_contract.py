"""Ordinary GREEN contracts for the frozen R22 Option B authority.

The fixture contains only sanitized provider/state shapes.  These tests began
as incident-shaped RED and now run without skips or expected-failure wrappers.

No test reads real media, credentials, Catalog data or a live Provider.  All
durable writes are isolated below ``TemporaryDirectory``.
"""

from __future__ import annotations

import asyncio
import ast
from copy import deepcopy
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import src.pages.plan_review_page as review_page
from src.pages.data_table_page import load_task_rows
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.decision_state import DecisionState, DecisionStateStore
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.projection_authority import TaskTerminalMutation
from src.rename.rename_plan import RenamePlan
from src.task_queue import TaskQueueManager
import tests.test_complex_library_special_red_contract as ui_fixture
import tests.test_r22_c1_cohort_incident_red_contract as cohort_fixture
import tests.test_r22_revised_c_c0_red_contract as c1_fixture
from tests.projection_test_support import (
    reserve_active_batch_fixture,
    save_plan_fixture,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "r22_tv_planning_incidents_v2.json"
)
MANIFEST_PHASES = (
    "preparing",
    "reservations_written",
    "tasks_enqueued",
    "active",
    "completed",
    "abandoned",
)


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _sha256(value: object, *, domain: str) -> str:
    encoded = json.dumps(
        {"domain": domain, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _authority_api(testcase: unittest.TestCase):
    try:
        module = importlib.import_module("src.rename.tv_planning_authority")
    except ModuleNotFoundError:
        testcase.fail(
            "the frozen unique TVPlanningAuthorityBuilder module is missing"
        )
    required = {
        "FrozenTVPlanningObservationBatch",
        "TVPlanningUserDecision",
        "TVPlanningAuthorityInput",
        "TVPlanningAuthority",
        "TVPlanningAuthorityBuilder",
        "TVPlanningAuthorityError",
        "ObservationBlobStore",
    }
    missing = sorted(name for name in required if not hasattr(module, name))
    testcase.assertEqual([], missing, f"TV authority API missing: {missing}")
    return module


def _batch_api(testcase: unittest.TestCase):
    try:
        module = importlib.import_module("src.rename.task_batch_scope")
    except ModuleNotFoundError:
        testcase.fail(
            "TaskBatchScopeManifest/reservation authority module is missing"
        )
    required = {
        "TaskSourceScope",
        "BatchTaskSourceRequest",
        "TaskBatchScopeManifestStore",
        "TaskBatchScopeError",
        "SourceReservationConflict",
        "canonical_reservation_lineage_id",
        "source_identity_set_fingerprint",
        "TASK_BATCH_MANIFEST_PHASES",
    }
    missing = sorted(name for name in required if not hasattr(module, name))
    testcase.assertEqual([], missing, f"batch scope API missing: {missing}")
    return module


def _source_paths(case: dict[str, object]) -> tuple[str, ...]:
    direct = case.get("source_scope")
    if isinstance(direct, list):
        return tuple(str(item) for item in direct)
    segments = case.get("source_owned_segments")
    values: list[str] = []
    if isinstance(segments, list):
        for segment in segments:
            if isinstance(segment, dict):
                values.extend(str(item) for item in segment.get("sources", ()))
    non_regular = case.get("non_regular_files")
    if isinstance(non_regular, list):
        values.extend(
            str(item.get("source"))
            for item in non_regular if isinstance(item, dict)
        )
    return tuple(dict.fromkeys(values))


def _typed_input(
    testcase: unittest.TestCase,
    case: dict[str, object],
    *,
    batch_fingerprint: str | None = None,
):
    authority = _authority_api(testcase)
    batch = _batch_api(testcase)
    task_id = str(case["task_id"])
    sources = _source_paths(case)
    source_rows = tuple({
        "source_path": source,
        "location_key": source.casefold(),
        "physical_key": f"fixture:{index}:{source.casefold()}",
        "size": index + 1,
        "mtime_ns": 1000 + index,
    } for index, source in enumerate(sources))
    source_set_fingerprint = batch.source_identity_set_fingerprint(source_rows)
    batch_scope_fingerprint = batch_fingerprint or _sha256(
        {"task_id": task_id, "sources": sorted(sources)},
        domain="fixture.batch-scope",
    )
    lineage_id = batch.canonical_reservation_lineage_id(
        owner_task_id=task_id,
        origin_batch_scope_fingerprint=batch_scope_fingerprint,
        source_identity_set_fingerprint=source_set_fingerprint,
    )
    observation_facts = {
        "schema_version": 1,
        "provider_observations": deepcopy(case.get("observations", ())),
        "selected_subject_id": case.get("selected_subject_id"),
        "tmdb_tv_id": case.get("tmdb_tv_id"),
        "source_owned_segments": deepcopy(
            case.get("source_owned_segments", ())
        ),
        "non_regular_files": deepcopy(case.get("non_regular_files", ())),
        "font_archive": deepcopy(case.get("font_archive")),
    }
    observation_facts["evidence_fingerprint"] = _sha256(
        observation_facts, domain="fixture.frozen-tv-observations"
    )
    payload = {
        "observations": observation_facts,
        "user_decisions": deepcopy(case.get("user_decisions", {})),
        "task_source_scope": {
            "schema_version": 1,
            "task_id": task_id,
            "media_domain": case.get("media_domain", "tv"),
            "batch_scope_fingerprint": batch_scope_fingerprint,
            "reservation_lineage_id": lineage_id,
            "reservation_revision": 0,
            "source_identity_set_fingerprint": source_set_fingerprint,
            "source_identities": source_rows,
        },
    }
    typed = authority.TVPlanningAuthorityInput.from_mapping(payload)
    testcase.assertIsInstance(
        typed.observations, authority.FrozenTVPlanningObservationBatch
    )
    testcase.assertIsInstance(
        typed.user_decisions, authority.TVPlanningUserDecision
    )
    testcase.assertIsInstance(typed.task_source_scope, batch.TaskSourceScope)
    return authority, batch, typed


def _authority_dict(value: object) -> dict[str, object]:
    method = getattr(value, "to_dict", None)
    if not callable(method):
        raise AssertionError("TVPlanningAuthority has no to_dict boundary")
    result = method()
    if not isinstance(result, dict):
        raise AssertionError("TVPlanningAuthority.to_dict did not return an object")
    return result


def _batch_request(api, value: dict[str, object]):
    return api.BatchTaskSourceRequest.from_mapping(value)


class SanitizedFixtureControls(unittest.TestCase):
    def test_fixture_has_all_incident_shapes_and_no_private_boundary(self):
        fixture = _fixture()
        self.assertEqual(2, fixture["schema_version"])
        self.assertTrue({
            "yama_successor",
            "first_kiss",
            "eighty_six",
            "steins_source_overlap",
            "tv_font",
            "movie_controls",
            "stale_task_error",
        }.issubset(fixture))
        serialized = json.dumps(fixture, ensure_ascii=False)
        self.assertNotRegex(serialized, r"[A-Za-z]:\\")
        self.assertNotIn("/volume", serialized.casefold())
        self.assertNotRegex(
            serialized,
            r"(?i)(api[_ -]?key|token|secret)\s*[:=]\s*[A-Za-z0-9]{16,}",
        )

    def test_eighty_six_fixture_is_bounded_per_file_not_cartesian(self):
        case = _fixture()["eighty_six"]
        candidates = [
            tuple(row["bounded_candidates"])
            for row in case["non_regular_files"]
        ]
        self.assertEqual([("S00E05",), (), ()], candidates)
        self.assertLessEqual(max(map(len, candidates)), 1)

    def test_lineage_contract_uses_three_positive_identity_inputs(self):
        review = Path("local/development/REVIEW.md").read_text("utf-8")
        marker = "### DECISION-20260827-R22-OPTION-B-CONTRACT-FROZEN"
        self.assertIn(marker, review)
        frozen = review.split(marker, 1)[1].split("\n### ", 1)[0]
        for field in (
            "owner_task_id",
            "origin_batch_scope_fingerprint",
            "source_identity_set_fingerprint",
            "reservation_lineage_id",
        ):
            self.assertIn(field, frozen)
        self.assertIn("全部与已持久化reservation一致", frozen)
        self.assertIn("CAS predecessor链", frozen)
        for insufficient_identity in (
            "仅`task_id`相同",
            "仅`batch_scope_fingerprint`相同",
            "仅physical source相同",
        ):
            self.assertIn(insufficient_identity, frozen)
        self.assertIn("均不构成归属证明", frozen)


class FrozenArchitectureRedContracts(unittest.TestCase):
    def test_unique_builder_consumes_only_three_typed_frozen_inputs_and_is_io_free(self):
        module = _authority_api(self)
        batch = _batch_api(self)
        signature = inspect.signature(module.TVPlanningAuthorityInput)
        self.assertEqual(
            {"observations", "user_decisions", "task_source_scope"},
            set(signature.parameters),
        )
        annotations = inspect.get_annotations(module.TVPlanningAuthorityInput)
        self.assertIs(
            annotations.get("observations"),
            module.FrozenTVPlanningObservationBatch,
        )
        self.assertIs(
            annotations.get("user_decisions"),
            module.TVPlanningUserDecision,
        )
        self.assertIs(annotations.get("task_source_scope"), batch.TaskSourceScope)
        source = inspect.getsource(module.TVPlanningAuthorityBuilder)
        for forbidden in (
            "open(", ".stat(", ".exists(", "requests.", "ProviderClient",
            "Catalog(", "PlanStore(",
        ):
            self.assertNotIn(forbidden, source)

    def test_decision_v4_and_projection_receipt_bind_tv_batch_and_lineage_authority(self):
        _authority_api(self)
        decision = importlib.import_module("src.rename.decision_state")
        projection = importlib.import_module("src.rename.projection_authority")
        self.assertEqual(4, decision.DECISION_STATE_SCHEMA_VERSION)
        fields = set(inspect.signature(decision.DecisionState).parameters)
        self.assertIn("tv_planning_authority", fields)
        required = {
            "tv_planning_authority_schema_version",
            "tv_planning_authority_policy_version",
            "tv_planning_authority_fingerprint",
            "batch_scope_fingerprint",
            "reservation_lineage_id",
            "reservation_revision",
        }
        self.assertTrue(required.issubset(projection.AUTHORITY_FINGERPRINT_FIELDS))

    def test_builder_is_deterministic_but_semantic_revision_and_scope_drift_invalidate(self):
        case = deepcopy(_fixture()["yama_successor"])
        module, _batch, first_input = _typed_input(self, case)
        first = module.TVPlanningAuthorityBuilder().build(first_input)
        again = module.TVPlanningAuthorityBuilder().build(first_input)
        self.assertEqual(first.fingerprint, again.fingerprint)
        self.assertEqual(_authority_dict(first), _authority_dict(again))

        correlation = deepcopy(case)
        correlation["user_decisions"]["request_token"] = "correlation-only"
        correlation["user_decisions"]["occurred_at"] = "2099-01-01T00:00:00Z"
        _m, _b, correlation_input = _typed_input(self, correlation)
        self.assertEqual(
            first.fingerprint,
            module.TVPlanningAuthorityBuilder().build(correlation_input).fingerprint,
        )

        changed = deepcopy(case)
        changed["user_decisions"]["decision_revision"] += 1
        _m, _b, changed_input = _typed_input(self, changed)
        self.assertNotEqual(
            first.fingerprint,
            module.TVPlanningAuthorityBuilder().build(changed_input).fingerprint,
        )
        _m, _b, drifted_scope = _typed_input(
            self, case, batch_fingerprint="f" * 64
        )
        self.assertNotEqual(
            first.fingerprint,
            module.TVPlanningAuthorityBuilder().build(drifted_scope).fingerprint,
        )

    def test_observation_blob_is_create_if_absent_and_authority_survives_display_loss(self):
        module = _authority_api(self)
        with tempfile.TemporaryDirectory() as directory:
            store = module.ObservationBlobStore(Path(directory))
            payload = {"schema_version": 1, "facts": [{"outcome": "no_match"}]}
            first = store.put(payload)
            second = store.put(deepcopy(payload))
            self.assertEqual(first, second)
            self.assertEqual(payload, store.load(first))
            path = store.path_for(first)
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                store.put_at(first, {"schema_version": 1, "facts": []})
            self.assertEqual(before, path.read_bytes())

            case = deepcopy(_fixture()["yama_successor"])
            _m, _b, typed = _typed_input(self, case)
            authority = module.TVPlanningAuthorityBuilder().build(
                typed, observation_refs=(first,)
            )
            serialized = _authority_dict(authority)
            path.unlink()
            restored = module.TVPlanningAuthority.from_mapping(serialized)
            self.assertEqual(authority.fingerprint, restored.fingerprint)
            self.assertEqual("evidence_display_unavailable", store.display_state(first))


class IncidentAuthorityRedContracts(unittest.TestCase):
    def test_yama_successor_uses_frozen_observation_provenance_for_all_four_seasons(self):
        case = deepcopy(_fixture()["yama_successor"])
        module, _batch, typed = _typed_input(self, case)
        authority = module.TVPlanningAuthorityBuilder().build(typed)
        value = _authority_dict(authority)
        self.assertEqual("53918", value["work_identity"]["primary_provider"]["provider_id"])
        self.assertEqual(
            case["expected"]["season_assignments"], value["season_assignments"]
        )
        self.assertEqual(3, value["selected_season_number"])
        self.assertEqual("extra", value["file_decisions"]["Yama-S3/Yama [13(OVA)].mkv"]["role"])
        self.assertTrue(value["derived_provenance"]["observation_fingerprints"])

    def test_first_kiss_naming_authority_cannot_rewrite_tmdb_work_identity(self):
        case = deepcopy(_fixture()["first_kiss"])
        module, _batch, typed = _typed_input(self, case)
        value = _authority_dict(module.TVPlanningAuthorityBuilder().build(typed))
        primary = value["work_identity"]["primary_provider"]
        naming = value["naming_authority"]
        self.assertEqual(("tmdb", "tv", "330299"), (
            primary["namespace"], primary["entity_type"], primary["provider_id"]
        ))
        self.assertEqual(case["expected"]["canonical_title"], naming["title"])
        self.assertEqual("user_confirmed_bangumi_localized_title", naming["source"])
        self.assertNotEqual(naming["provider_id"], primary["provider_id"])

    def test_eighty_six_owns_two_ranges_and_matches_each_s00_candidate_per_file(self):
        case = deepcopy(_fixture()["eighty_six"])
        module, _batch, typed = _typed_input(self, case)
        value = _authority_dict(module.TVPlanningAuthorityBuilder().build(typed))
        ranges = value["episode_range_ownership"]
        self.assertEqual(
            [("302189", 1, 11), ("331887", 12, 23)],
            [
                (row["bangumi_subject_id"], row["episode_start"], row["episode_end"])
                for row in ranges
            ],
        )
        decisions = value["non_regular_file_decisions"]
        self.assertEqual(("S00E05",), tuple(
            decisions["86/86 [13(OVA)].mkv"]["bounded_candidates"]
        ))
        self.assertEqual((), tuple(
            decisions["86/86 [NCOP].mkv"]["bounded_candidates"]
        ))
        self.assertEqual("manual_extra", decisions["86/86 [NCOP].mkv"]["state"])
        self.assertEqual(1, sum(
            row["state"] == "official_special_candidate"
            for row in decisions.values()
        ))

    def test_tv_special_scope_has_tv_exit_before_multi_video_movie_classification(self):
        fixture = _fixture()["steins_source_overlap"]
        module = _authority_api(self)
        result = module.classify_planning_domain({
            "task_id": fixture["child_task_id"],
            "declared_media_domain": fixture["child_media_domain"],
            "declared_layout": fixture["child_layout"],
            "video_count": 2,
        })
        self.assertEqual("tv", result.media_domain)
        self.assertEqual("official_special", result.layout)
        self.assertNotEqual("movie_inventory", result.route)

    def test_tv_font_manifest_is_series_scoped_canonical_and_movie_is_rejected(self):
        fixture = _fixture()
        case = deepcopy(fixture["yama_successor"])
        case["font_archive"] = deepcopy(fixture["tv_font"])
        module, _batch, typed = _typed_input(self, case)
        value = _authority_dict(module.TVPlanningAuthorityBuilder().build(typed))
        manifest = value["associated_asset_manifest"]
        self.assertEqual("series", manifest["scope"])
        self.assertEqual(
            fixture["tv_font"]["expected_target"], manifest["entries"][0]["target_name"]
        )
        self.assertEqual(
            fixture["tv_font"]["content_sha256"], manifest["entries"][0]["content_sha256"]
        )
        movie_case = {
            "task_id": "movie-control",
            "media_domain": "movie",
            "source_scope": ["Movie/Movie.mkv"],
            "observations": [],
            "user_decisions": {},
        }
        _m, _b, movie_input = _typed_input(self, movie_case)
        with self.assertRaisesRegex(
            module.TVPlanningAuthorityError, "TV.*movie|movie.*TV"
        ):
            module.TVPlanningAuthorityBuilder().build(movie_input)


class BatchScopeLineageRedContracts(unittest.TestCase):
    def test_lineage_requires_task_batch_and_source_set_together(self):
        api = _batch_api(self)
        base = {
            "owner_task_id": "task-a",
            "origin_batch_scope_fingerprint": "a" * 64,
            "source_identity_set_fingerprint": "b" * 64,
        }
        lineage = api.canonical_reservation_lineage_id(**base)
        self.assertRegex(lineage, r"^[0-9a-f]{64}$")
        self.assertEqual(lineage, api.canonical_reservation_lineage_id(**base))
        for field, replacement in (
            ("owner_task_id", "task-b"),
            ("origin_batch_scope_fingerprint", "c" * 64),
            ("source_identity_set_fingerprint", "d" * 64),
        ):
            changed = dict(base)
            changed[field] = replacement
            self.assertNotEqual(
                lineage, api.canonical_reservation_lineage_id(**changed), field
            )

    def test_batch_specificity_assigns_shared_23beta_physical_source_only_to_child(self):
        api = _batch_api(self)
        case = _fixture()["steins_source_overlap"]
        shared = {
            "source_path": case["child_scope"][0],
            "location_key": case["shared_location_key"],
            "physical_key": case["shared_physical_key"],
            "size": 23,
            "mtime_ns": 2300,
        }
        parent = _batch_request(api, {
            "task_id": case["parent_task_id"],
            "input_scope": case["parent_scope"],
            "scope_kind": "parent_directory",
            "source_identities": [shared],
        })
        child = _batch_request(api, {
            "task_id": case["child_task_id"],
            "input_scope": case["child_scope"],
            "scope_kind": "exact_file",
            "source_identities": [shared],
        })
        with tempfile.TemporaryDirectory() as directory:
            store = api.TaskBatchScopeManifestStore(Path(directory))
            manifest = store.reserve_batch(
                batch_id=case["batch_id"], task_requests=(parent, child)
            )
            value = manifest.to_dict()
        owners = {
            row["physical_key"]: row["owner_task_id"]
            for row in value["reservations"]
        }
        self.assertEqual(case["child_task_id"], owners[case["shared_physical_key"]])
        parent_rows = [
            row for row in value["reservations"]
            if row["owner_task_id"] == case["parent_task_id"]
        ]
        self.assertFalse(any(
            row["physical_key"] == case["shared_physical_key"]
            for row in parent_rows
        ))

    def test_live_store_writer_lock_fails_closed_before_conflict_scan_or_manifest_write(self):
        api = _batch_api(self)
        file_lock = importlib.import_module("src.rename.file_lock")
        request = _batch_request(api, {
            "task_id": "concurrent-owner",
            "input_scope": ["Series/Episode 01.mkv"],
            "scope_kind": "exact_file",
            "source_identities": [{
                "source_path": "Series/Episode 01.mkv",
                "location_key": "series/episode 01.mkv",
                "physical_key": "fixture:concurrent:1",
                "size": 10,
                "mtime_ns": 10,
            }],
        })
        with tempfile.TemporaryDirectory() as directory:
            store = api.TaskBatchScopeManifestStore(Path(directory))
            with file_lock.advisory_file_lock(
                store.lock_path,
                owner={"component": "concurrent-test-owner"},
            ):
                with self.assertRaisesRegex(
                    api.TaskBatchScopeError, "另一进程|并发"
                ):
                    store.reserve_batch(
                        batch_id="concurrent-batch", task_requests=(request,)
                    )
            self.assertFalse(store.path_for("concurrent-batch").exists())

    def test_completed_reservation_blocks_new_lineage_until_explicit_cas_transfer(self):
        api = _batch_api(self)
        identity = {
            "source_path": "Series/Episode 01.mkv",
            "location_key": "series/episode 01.mkv",
            "physical_key": "fixture:completed:1",
            "size": 10,
            "mtime_ns": 10,
        }
        first = _batch_request(api, {
            "task_id": "owner-a",
            "input_scope": ["Series/"],
            "scope_kind": "exact_file",
            "source_identities": [identity],
        })
        second = _batch_request(api, {
            "task_id": "owner-b",
            "input_scope": ["Alias/"],
            "scope_kind": "exact_file",
            "source_identities": [{**identity, "source_path": "Alias/Episode 01.mkv"}],
        })
        with tempfile.TemporaryDirectory() as directory:
            store = api.TaskBatchScopeManifestStore(Path(directory))
            completed = reserve_active_batch_fixture(
                store, batch_id="batch-a", task_requests=(first,)
            )
            completed = store.transition(
                "batch-a", expected_revision=completed.revision,
                target_state="completed",
            )
            with self.assertRaises(api.SourceReservationConflict):
                store.reserve_batch(batch_id="batch-b", task_requests=(second,))
            transferred = store.retire_and_transfer(
                from_lineage_id=completed.reservations[0].reservation_lineage_id,
                expected_reservation_revision=(
                    completed.reservations[0].reservation_revision
                ),
                to_batch_id="batch-b",
                to_request=second,
                reason="user-confirmed-replan",
            )
            self.assertEqual("owner-b", transferred.owner_task_id)
            self.assertNotEqual(
                completed.reservations[0].reservation_lineage_id,
                transferred.reservation_lineage_id,
            )
            self.assertTrue(store.operation_history(transferred.reservation_lineage_id))

    def test_every_batch_manifest_crash_window_recovers_without_pending_or_orphans(self):
        api = _batch_api(self)
        self.assertEqual(MANIFEST_PHASES, tuple(api.TASK_BATCH_MANIFEST_PHASES))
        request = _batch_request(api, {
            "task_id": "crash-task",
            "input_scope": ["Series/Episode 01.mkv"],
            "scope_kind": "exact_file",
            "source_identities": [{
                "source_path": "Series/Episode 01.mkv",
                "location_key": "series/episode 01.mkv",
                "physical_key": "fixture:crash:1",
                "size": 10,
                "mtime_ns": 10,
            }],
        })
        for phase in MANIFEST_PHASES:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)

                def crash(current_phase: str, *, expected=phase):
                    if current_phase == expected:
                        raise KeyboardInterrupt(f"crash after {expected}")

                store = api.TaskBatchScopeManifestStore(root, _phase_hook=crash)
                if phase in {"completed", "abandoned"}:
                    active_store = api.TaskBatchScopeManifestStore(root)
                    active = reserve_active_batch_fixture(
                        active_store,
                        batch_id="crash-batch",
                        task_requests=(request,),
                    )
                    with self.assertRaises(KeyboardInterrupt):
                        store.transition(
                            "crash-batch",
                            expected_revision=active.revision,
                            target_state=phase,
                        )
                elif phase in {"preparing", "reservations_written"}:
                    with self.assertRaises(KeyboardInterrupt):
                        store.reserve_batch(
                            batch_id="crash-batch", task_requests=(request,)
                        )
                elif phase == "tasks_enqueued":
                    manifest = store.reserve_batch(
                        batch_id="crash-batch", task_requests=(request,)
                    )
                    with self.assertRaises(KeyboardInterrupt):
                        store.mark_tasks_enqueued(
                            manifest.batch_id,
                            expected_revision=manifest.revision,
                            task_ids=(request.task_id,),
                        )
                else:
                    manifest = store.reserve_batch(
                        batch_id="crash-batch", task_requests=(request,)
                    )
                    enqueued = store.mark_tasks_enqueued(
                        manifest.batch_id,
                        expected_revision=manifest.revision,
                        task_ids=(request.task_id,),
                    )
                    with self.assertRaises(KeyboardInterrupt):
                        store.activate_batch(
                            enqueued.batch_id,
                            expected_revision=enqueued.revision,
                        )
                recovered = api.TaskBatchScopeManifestStore(root).recover_pending(
                    "crash-batch"
                )
                self.assertIn(recovered.state, {"active", "completed", "abandoned", "failed_closed"})
                self.assertNotIn(recovered.state, MANIFEST_PHASES[:3])
                self.assertEqual(0, recovered.orphan_reservation_count)

    def test_real_queue_freezes_batch_before_any_worker_and_legacy_replan_gets_new_batch(self):
        api = _batch_api(self)
        enqueue_batch = getattr(TaskQueueManager, "enqueue_batch", None)
        self.assertTrue(callable(enqueue_batch), "real TaskQueue has no batch entry")
        source = inspect.getsource(enqueue_batch)
        self.assertLess(source.index("reserve_batch"), source.index("_queue.put"))
        self.assertIn("active", source)
        self.assertIn("batch_scope_fingerprint", source)
        self.assertIn("reservation_lineage_id", source)
        replan = getattr(TaskQueueManager, "enqueue_legacy_replan", None)
        self.assertTrue(callable(replan), "legacy replan has no current-schema exit")
        replan_source = inspect.getsource(replan)
        self.assertIn("single_task_minimal_batch", replan_source)
        self.assertNotIn("reuse_completed_manifest", replan_source)


class PersistenceAndAntiBypassRedContracts(unittest.TestCase):
    def test_task_terminal_clears_current_error_and_appends_immutable_operation_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_root = root / "tasks"
            task_root.mkdir()
            task_id = "terminal-history"
            failure = {
                "operation_id": "old-failure",
                "code": "provider_selection_failed",
                "phase": "provider_selection",
                "decision_revision": 0,
                "authority_fingerprint": "0" * 64,
                "outcome": "failed",
                "occurred_at": "2026-01-01T00:00:00Z",
            }
            previous = {
                "uuid": task_id,
                "workflow_status": "invalid",
                "error": "older provider selection failed",
                "operation_history": [failure],
            }
            (task_root / f"{task_id}.json").write_text(
                json.dumps(previous), encoding="utf-8"
            )
            plan = RenamePlan.from_mapping(
                task_id=task_id,
                source_root=root / "source",
                mapping={
                    root / "source" / "Episode 01.mkv":
                    root / "target" / "Episode 01.mkv"
                },
            )
            projected = save_plan_fixture(PlanStore(root / "plans"), plan)
            state = DecisionStateStore(root / "decision_state").load(task_id)
            mutation = TaskTerminalMutation.prepare(
                task_root,
                task_id,
                lambda _decision, _plan, payload: {
                    **payload,
                    "workflow_status": "need_confirm",
                },
            )
            rendered = mutation.render(state, projected)
            self.assertIsNone(rendered["error"])
            history = rendered["operation_history"]
            self.assertEqual(failure, history[0])
            self.assertEqual(2, len(history))
            self.assertEqual("succeeded", history[-1]["outcome"])
            self.assertEqual(
                projected.projection_receipt.fingerprint,
                history[-1]["authority_fingerprint"],
            )
            current_error = "这部电视剧尚未确认所属系列和季度"
            current_failure = {
                "code": "tv_authority_incomplete",
                "safe_actions": ["replan"],
            }
            current = mutation.render(
                state,
                projected,
            )
            self.assertIsNone(current["error"])
            mutation_with_current_error = TaskTerminalMutation.prepare(
                task_root,
                task_id,
                lambda _decision, _plan, payload: {
                    **payload,
                    "workflow_status": "invalid",
                    "error": current_error,
                    "current_operation_failure": current_failure,
                },
            )
            rendered_invalid = mutation_with_current_error.render(
                state, projected
            )
            self.assertEqual(current_error, rendered_invalid["error"])
            self.assertEqual(
                current_failure,
                rendered_invalid["current_operation_failure"],
            )

    def test_legacy_task_read_view_preserves_error_and_offers_replan_or_safe_abandon(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {
                "uuid": "legacy-task-view",
                "path": "Legacy/Series",
                "workflow_status": "invalid",
                "error": "legacy failure reason",
            }
            (root / "legacy-task-view.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            rows = load_task_rows(root)
            self.assertEqual(1, len(rows))
            row = rows[0]
            self.assertEqual("legacy failure reason", row["error"])
            required_legacy_fields = {
                "operation_history_state",
                "available_recovery_actions",
                "current_authority_usable",
            }
            self.assertEqual(
                set(),
                required_legacy_fields.difference(row),
                "legacy task read view is missing current recovery fields",
            )
            self.assertEqual("legacy_unknown", row["operation_history_state"])
            self.assertEqual(
                {"replan_unapproved", "safe_abandon"},
                set(row["available_recovery_actions"]),
            )
            self.assertFalse(row["current_authority_usable"])

    def test_builder_callsite_allowlist_and_consumer_matrix_have_zero_unknowns(self):
        module = _authority_api(self)
        expected_consumers = {
            "decision_store",
            "final_plan_projector",
            "plan_verifier",
            "approval",
            "execution",
            "series_catalog",
            "work_catalog",
            "recovery",
            "replay",
            "task_terminal",
        }
        self.assertEqual(0, module.authority_inventory_unknown_count())
        self.assertEqual(
            expected_consumers, set(module.TV_PLANNING_AUTHORITY_CONSUMERS)
        )
        callsites = tuple(module.TV_PLANNING_BUILDER_CALLSITE_ALLOWLIST)
        self.assertTrue(callsites)
        self.assertEqual(len(callsites), len(set(callsites)))
        self.assertTrue(all(str(item).startswith("src/") for item in callsites))


class RealEntrypointDisconnectRedContracts(unittest.IsolatedAsyncioTestCase):
    async def test_real_ui_cohort_click_persists_decision_v4_authority_and_receipt(self):
        module = _authority_api(self)
        with tempfile.TemporaryDirectory() as directory:
            result, _source, _case = await cohort_fixture._run_yama_incident(
                Path(directory)
            )
            dialog = c1_fixture._boundary_dialog(result)
            notices: list[tuple[str, object]] = []
            capture = ui_fixture._CaptureUi()
            capture.card = capture.column
            with c1_fixture._boundary_patches(result, notices), patch.object(
                review_page, "ui", capture
            ):
                PlanReviewDialog._render_selection_operation(dialog)
                callback = capture.callbacks.get("确认与相邻作品一起整理")
                self.assertTrue(callable(callback))
                value = callback()
                if asyncio.iscoroutine(value):
                    await value
            state = DecisionStateStore(result.decisions).load(result.task_id)
            plan = PlanStore(result.plans).load(result.task_id)
            self.assertIsInstance(
                state.tv_planning_authority, module.TVPlanningAuthority
            )
            receipt = plan.projection_receipt.authority
            self.assertEqual(
                state.tv_planning_authority.fingerprint,
                receipt["tv_planning_authority_fingerprint"],
            )
            self.assertEqual(
                state.tv_planning_authority.task_source_scope.reservation_lineage_id,
                receipt["reservation_lineage_id"],
            )

    async def test_disconnecting_builder_makes_real_ui_cohort_entry_fail_closed(self):
        module = _authority_api(self)
        error = module.TVPlanningAuthorityError("builder disconnected")
        with tempfile.TemporaryDirectory() as directory, patch.object(
            module.TVPlanningAuthorityBuilder,
            "build",
            side_effect=error,
        ):
            result, _source, _case = await cohort_fixture._run_yama_incident(
                Path(directory)
            )
            dialog = c1_fixture._boundary_dialog(result)
            notices: list[tuple[str, object]] = []
            capture = ui_fixture._CaptureUi()
            capture.card = capture.column
            with c1_fixture._boundary_patches(result, notices), patch.object(
                review_page, "ui", capture
            ):
                PlanReviewDialog._render_selection_operation(dialog)
                callback = capture.callbacks.get("确认与相邻作品一起整理")
                self.assertTrue(callable(callback))
                value = callback()
                if asyncio.iscoroutine(value):
                    await value
            task = json.loads(
                (result.tasks / f"{result.task_id}.json").read_text("utf-8")
            )
            self.assertNotEqual("need_confirm", task["workflow_status"])
            self.assertIn(
                task["workflow_status"], {"invalid", "failed", "awaiting_user"}
            )
            store = PlanStore(result.plans)
            current = (
                store.load(result.task_id)
                if store.path_for(result.task_id).is_file() else None
            )
            if current is not None:
                self.assertNotEqual(
                    "accepted", PlanVerifier().verify(current).status.value
                )
            failure = task.get("current_operation_failure") or {}
            self.assertEqual("tv_planning_authority_unavailable", failure.get("code"))
            self.assertTrue(
                {"retry", "replan", "safe_abandon"}.intersection(
                    failure.get("safe_actions") or ()
                )
            )


class MovieRegressionControls(unittest.TestCase):
    def test_existing_movie_fixture_contracts_remain_outside_tv_builder(self):
        fixture = _fixture()
        for row in fixture["movie_controls"]:
            self.assertEqual("movie", row["media_domain"])
            self.assertEqual("work_font_intent", row["font_contract"])
            self.assertEqual("Extras", row["extras_directory"])

    def test_temporary_red_wrapper_is_removed_after_green(self):
        source = Path(__file__).read_text("utf-8")
        strict_marker = "BAR_" + "STRICT_R22_TV_PLANNING_RED"
        wrapper_marker = "def " + "red_contract"
        self.assertNotIn(strict_marker, source)
        self.assertNotIn(wrapper_marker, source)
        tree = ast.parse(source, filename=__file__)
        forbidden = []
        for node in ast.walk(tree):
            decorators = getattr(node, "decorator_list", ())
            for decorator in decorators:
                name = ast.unparse(decorator.func if isinstance(
                    decorator, ast.Call
                ) else decorator)
                if name in {
                    "unittest.skip", "unittest.skipIf", "unittest.skipUnless",
                    "unittest.expectedFailure",
                }:
                    forbidden.append((name, getattr(node, "lineno", 0)))
        self.assertEqual([], forbidden)


if __name__ == "__main__":
    unittest.main()
