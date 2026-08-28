"""R22 C1 incident-derived cohort boundary regression contracts.

The private runtime replay remains under ``local/``.  This module uses only a
sanitized semantic fixture: no local path, task UUID, credential, runtime log,
or complete media manifest is copied into the test tree.

Provider transport is local and every write stays inside
``TemporaryDirectory``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import src.pages.plan_review_page as review_page
import src.rename.provider_link_decision as decision_module
import tests.test_complex_library_special_red_contract as ui_fixture
import tests.test_provider_subject_plan_wiring_red_contract as phase_b_fixture
import tests.test_r22_revised_c_c0_red_contract as c1_fixture
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.decision_state import DecisionStateStore
from src.rename.plan_review import PlanReviewService, ReviewStatus
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.provider_link_decision import (
    freeze_provider_subject_boundary_edges,
    resolve_provider_subject_boundary_decision,
)
from src.rename.provider_link_resolver import collect_subject_observations
from src.rename.rename_plan import PlanStatus, RenamePlan
from tests.projection_test_support import save_plan_fixture


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "r22_c1_cohort_incident_v1.json"
)
def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _observation_batch(case: dict[str, object]) -> dict[str, object]:
    observations = {
        str(row["bangumi_subject_id"]): deepcopy(row)
        for row in case["observations"]
    }
    return dict(collect_subject_observations(
        selected_subject_id=str(case["selected_subject_id"]),
        bangumi_subjects=deepcopy(case["subjects"]),
        bangumi_relations=deepcopy(case["relations"]),
        search_subject=lambda subject: deepcopy(
            observations[str(subject["provider_id"])]
        ),
        max_subjects=len(case["subjects"]),
    ))


def _failure(case: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    batch = _observation_batch(case)
    edges = freeze_provider_subject_boundary_edges(
        selected_subject_id=str(case["selected_subject_id"]),
        subject_observations=batch,
        bangumi_subjects=deepcopy(case["subjects"]),
        bangumi_relations=deepcopy(case["relations"]),
    )
    if not edges:
        raise AssertionError("fixture did not freeze an actionable selected edge")
    return ({
        "selected_subject_id": str(case["selected_subject_id"]),
        "subject_observations": batch,
        "bangumi_subjects": deepcopy(case["subjects"]),
        "bangumi_relations": deepcopy(case["relations"]),
        "evidence_fingerprint": batch["evidence_fingerprint"],
        "provider_subject_boundary_edges": tuple(deepcopy(edges)),
    }, dict(edges[0]))


def _process_yama_tmdb(case: dict[str, object]) -> dict[str, object]:
    candidate = case["observations"][0]["queries"][0]["candidates"][0]
    return {
        "id": int(candidate["provider_id"]),
        "name": candidate["title"],
        "original_name": candidate["original_title"],
        "first_air_date": candidate["first_air_date"],
        "genres": [{"name": "Animation"}],
        "seasons": [
            {
                "season_number": row["season_number"],
                "name": f"Season {row['season_number']}",
                "air_date": row["air_date"],
                "episode_count": row["episode_count"],
            }
            for row in candidate["seasons"]
        ],
    }


def _make_yama_source(root: Path, case: dict[str, object]) -> Path:
    source = root / "Yama Season 1"
    shape = case["source_shape"]
    for episode in shape["regular_episode_numbers"]:
        path = source / f"Yama Season 1 [{episode:02d}].mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"episode-{episode}".encode("ascii"))
    ova = source / f"Yama Season 1 [{shape['bonus_marker']}].mkv"
    ova.write_bytes(b"ova-13")
    if shape["bonus_subtitle"]:
        ova.with_suffix(".sc.ass").write_text(
            "[Script Info]\nTitle: OVA\n", encoding="utf-8"
        )
    return source


async def _run_yama_incident(root: Path):
    case = _fixture()["yama"]
    source = _make_yama_source(root / "source", case)
    harness = phase_b_fixture.ProviderSubjectPlanWiringRedContracts(
        methodName="runTest"
    )
    fail_terms = (
        "第二季",
        "セカンドシーズン",
        "第三季",
        "サードシーズン",
        "Next Summit",
    )
    with patch.dict(
        phase_b_fixture.YAMA_TMDB,
        _process_yama_tmdb(case),
        clear=True,
    ):
        result = await harness._run_real_add_select(
            root=root / "flow",
            source=source,
            subjects=phase_b_fixture.YAMA_SUBJECTS,
            relations=phase_b_fixture.YAMA_RELATIONS,
            selected_subject_id="41658",
            fail_subject=fail_terms,
            failure_mode="no_match",
        )
    return result, source, case


def _operation_edge(task: dict[str, object]) -> tuple[int, str, dict[str, object]]:
    operation = task.get("selection_operation")
    if not isinstance(operation, dict):
        raise AssertionError("selection operation is missing")
    evidence = operation.get("provider_subject_evidence")
    edges = (
        evidence.get("provider_subject_boundary_edges")
        if isinstance(evidence, dict) else None
    )
    if not isinstance(edges, (list, tuple)) or not edges:
        raise AssertionError("selection operation has no frozen boundary edge")
    revision = operation.get("expected_decision_revision")
    fingerprint = str(operation.get("evidence_fingerprint") or "")
    if not isinstance(revision, int) or len(fingerprint) != 64:
        raise AssertionError("selection operation authority is incomplete")
    return revision, fingerprint, dict(edges[0])


class CohortResolverIncidentRedContracts(unittest.TestCase):
    def test_yama_all_successors_no_match_closes_with_the_existing_date_locked_gate(self):
        case = _fixture()["yama"]
        failure, edge = _failure(case)
        try:
            resolution, decision = resolve_provider_subject_boundary_decision(
                failure,
                same_work=True,
                boundary_edge=edge,
            )
        except ValueError as exc:
            self.fail(f"all-successor-no-match cohort did not close: {exc}")

        self.assertEqual("reviewable", resolution.review_disposition)
        self.assertEqual("53918", resolution.tmdb_tv_id)
        self.assertEqual(
            {"41658": 1, "86670": 2, "216310": 3, "290980": 4},
            dict(resolution.season_assignments),
        )
        self.assertIn("linear_one_to_one_air_date_locked", resolution.reason_codes)
        self.assertEqual(
            "date-count-delta-v1", resolution.season_count_policy_version
        )
        self.assertIsInstance(decision, dict)
        self.assertEqual(
            ("41658", "86670", "216310", "290980"),
            tuple(decision.get("cohort_subject_ids") or ()),
        )
        self.assertEqual(3, len(decision.get("cohort_edges") or ()))

    def test_steins_incompatible_successor_is_a_manual_boundary_not_a_false_merge(self):
        case = _fixture()["steins_boundary"]
        failure, edge = _failure(case)
        try:
            resolution, decision = resolve_provider_subject_boundary_decision(
                failure,
                same_work=True,
                boundary_edge=edge,
            )
        except ValueError as exc:
            self.fail(f"unique compatible selected segment did not close: {exc}")

        self.assertEqual("reviewable", resolution.review_disposition)
        self.assertEqual("42509", resolution.tmdb_tv_id)
        self.assertEqual({"10380": 1}, dict(resolution.season_assignments))
        self.assertNotIn("129807", dict(resolution.season_assignments))
        self.assertEqual(1, len(resolution.boundaries))
        boundary = resolution.boundaries[0]
        self.assertEqual(
            ("10380", "129807"),
            (boundary.get("from_subject_id"), boundary.get("to_subject_id")),
        )
        self.assertNotEqual(
            "different_unique_tmdb_id", boundary.get("boundary_kind")
        )
        self.assertEqual("", str(boundary.get("outside_tmdb_tv_id") or ""))
        self.assertIsInstance(decision, dict)

    def test_only_complete_no_match_rows_can_be_offered_for_overlay(self):
        base = _fixture()["yama"]
        two = {
            **deepcopy(base),
            "subjects": deepcopy(base["subjects"][:2]),
            "relations": deepcopy(base["relations"][:1]),
            "observations": deepcopy(base["observations"][:2]),
        }
        original = deepcopy(two)
        problems: list[str] = []
        for mode in ("transport_error", "truncated", "tie"):
            case = deepcopy(two)
            query = case["observations"][1]["queries"][0]
            if mode == "transport_error":
                query["outcome"] = "transport_error"
            elif mode == "truncated":
                query["truncated"] = True
            else:
                query["outcome"] = "ok"
                selected = deepcopy(
                    case["observations"][0]["queries"][0]["candidates"][0]
                )
                foreign = deepcopy(selected)
                foreign["provider_id"] = foreign["tmdb_tv_id"] = "99999"
                query["candidates"] = [selected, foreign]
            batch = _observation_batch(case)
            edges = freeze_provider_subject_boundary_edges(
                selected_subject_id=str(case["selected_subject_id"]),
                subject_observations=batch,
                bangumi_subjects=case["subjects"],
                bangumi_relations=case["relations"],
            )
            if edges:
                problems.append(f"{mode} was exposed as an overlay action")
        if two != original:
            problems.append("overlay safety checks mutated the raw fixture")
        self.assertEqual([], problems, "\n".join(problems))

    def test_preview_and_receipt_share_one_order_stable_canonical_function(self):
        function = getattr(
            decision_module,
            "provider_subject_cohort_request_fingerprint",
            None,
        )
        if not callable(function):
            self.fail("one canonical cohort request fingerprint API is missing")
        self.assertIs(
            function,
            getattr(
                review_page,
                "provider_subject_cohort_request_fingerprint",
                None,
            ),
            "UI and decision/receipt must import the same canonical function",
        )
        request = {
            "schema_version": 2,
            "same_work": True,
            "selected_subject_id": "41658",
            "cohort_subject_ids": ["41658", "86670", "216310", "290980"],
            "cohort_edges": deepcopy(_fixture()["yama"]["relations"]),
            "subject_evidence_fingerprints": {
                "41658": "a" * 64,
                "86670": "b" * 64,
                "216310": "c" * 64,
                "290980": "d" * 64,
            },
            "selected_tmdb_tv_id": "53918",
            "season_assignments": {
                "41658": 1,
                "86670": 2,
                "216310": 3,
                "290980": 4,
            },
            "expected_decision_revision": 0,
        }
        reversed_request = deepcopy(request)
        reversed_request["cohort_subject_ids"].reverse()
        reversed_request["cohort_edges"].reverse()
        reversed_request["subject_evidence_fingerprints"] = dict(reversed(
            tuple(reversed_request["subject_evidence_fingerprints"].items())
        ))
        reversed_request["season_assignments"] = dict(reversed(
            tuple(reversed_request["season_assignments"].items())
        ))
        first = function(request)
        second = function(reversed_request)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")


class CohortRealEntrypointRedContracts(unittest.IsolatedAsyncioTestCase):
    async def test_real_entrypoint_closes_four_seasons_and_keeps_ova_in_s1_extras(self):
        with tempfile.TemporaryDirectory() as directory:
            result, source, case = await _run_yama_incident(Path(directory))
            dialog = c1_fixture._boundary_dialog(result)
            notices: list[tuple[str, object]] = []
            capture = ui_fixture._CaptureUi()
            capture.card = capture.column
            with c1_fixture._boundary_patches(result, notices), patch.object(
                review_page, "ui", capture
            ):
                PlanReviewDialog._render_selection_operation(dialog)
                callbacks = [
                    callback
                    for label, callback in capture.callbacks.items()
                    if "确认" in label and "整理" in label
                ]
                if len(callbacks) == 1:
                    value = callbacks[0]()
                    if asyncio.iscoroutine(value):
                        await value

            task = json.loads(
                (result.tasks / f"{result.task_id}.json").read_text("utf-8")
            )
            plan = PlanStore(result.plans).load(result.task_id)
            state = DecisionStateStore(result.decisions).load(result.task_id)
            visible = "\n".join(capture.visible)
            problems: list[str] = []
            for title in (
                "向山进发",
                "向山进发 第二季",
                "向山进发 第三季",
                "向山进发 Next Summit",
            ):
                if title not in visible:
                    problems.append(f"cohort preview omitted {title}")
            if len(callbacks) != 1:
                problems.append("cohort preview did not expose one bounded confirm action")
            operation = task.get("selection_operation") or {}
            if (
                task.get("workflow_status") != "need_confirm"
                or operation.get("state") != "succeeded"
            ):
                problems.append("all-successor-no-match decision did not close the task")
            verification = PlanVerifier().verify(plan)
            if verification.status != PlanStatus.ACCEPTED or verification.issues:
                problems.append("cohort decision did not persist an approvable Plan")

            targets = tuple(entry.target_path for entry in plan.entries)
            forbidden = str(case["source_shape"]["forbidden_target_marker"])
            if any(forbidden in target.name for target in targets):
                problems.append("13(OVA) was fabricated as S01E13")
            ova_entries = tuple(
                entry for entry in plan.entries
                if str(case["source_shape"]["bonus_marker"])
                in entry.source_path.name
            )
            if len(ova_entries) != 2:
                problems.append("OVA video/subtitle pair was dropped")
            for entry in ova_entries:
                parts = entry.target_path.parts
                if "Season 01" not in parts or "Extras" not in parts:
                    problems.append(
                        f"OVA asset escaped Season 01/Extras: {entry.target_path}"
                    )

            preview = (
                result.task.get("selection_operation", {}).get(
                    "provider_subject_cohort_preview"
                )
            )
            receipt = plan.metadata.get(
                "provider_subject_boundary_selection_receipt"
            )
            if not isinstance(preview, Mapping) or not isinstance(
                receipt, Mapping
            ):
                problems.append("cohort preview/receipt pair is incomplete")
            else:
                if receipt.get("schema_version") != 3:
                    problems.append("cohort receipt schema is not routing-aware v3")
                if receipt.get("target_root_config_fingerprint") != (
                    plan.metadata.get("target_root_config_fingerprint")
                ):
                    problems.append("receipt lost the frozen target-root fingerprint")
                if receipt.get("resolved_target_root") != plan.metadata.get(
                    "target_root"
                ):
                    problems.append("receipt target root forked from the Plan")
                if preview.get("request_fingerprint") != receipt.get(
                    "request_fingerprint"
                ):
                    problems.append("preview and receipt fingerprints forked")
                if tuple(receipt.get("cohort_subject_ids") or ()) != (
                    "41658", "86670", "216310", "290980"
                ):
                    problems.append("receipt omitted cohort subjects")
            boundary_rows = tuple(
                state.provider_subject_boundary_decisions.values()
            )
            cohort_ids = {
                str(row.get("cohort_decision_id") or "")
                for row in boundary_rows
            }
            if len(boundary_rows) != 3 or len(cohort_ids) != 1 or "" in cohort_ids:
                problems.append("Decision edges are not linked by one cohort decision id")
            if not result.source_unchanged or not source.exists():
                problems.append("Plan-only cohort flow changed the source")
            if result.business_json or result.target_files:
                problems.append("Plan-only cohort flow crossed a business/media boundary")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_failed_cohort_action_persists_reason_and_renders_safe_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _source, _case = await _run_yama_incident(Path(directory))
            revision, evidence, edge = _operation_edge(result.task)
            dialog = c1_fixture._boundary_dialog(result)
            notices: list[tuple[str, object]] = []
            with c1_fixture._boundary_patches(result, notices), patch.object(
                review_page,
                "resolve_provider_subject_boundary_decision",
                side_effect=ValueError("季度相容分段存在多解"),
            ):
                PlanReviewDialog._apply_provider_subject_boundary_inline(
                    dialog, True, revision, evidence, edge
                )

            task = json.loads(
                (result.tasks / f"{result.task_id}.json").read_text("utf-8")
            )
            operation = task.get("selection_operation") or {}
            failure = operation.get("boundary_resolution_failure")
            if not isinstance(failure, dict):
                self.fail("boundary failure was notify-only and did not persist")
            self.assertRegex(str(failure.get("public_code") or ""), r"^[a-z0-9_]+$")
            self.assertTrue(str(failure.get("public_title") or "").strip())
            self.assertTrue(tuple(failure.get("affected_subject_titles") or ()))
            self.assertTrue(
                {"change_work", "separate", "remove"}.issubset(
                    set(failure.get("safe_actions") or ())
                )
            )
            technical = json.dumps(failure, ensure_ascii=False)
            self.assertNotIn("324820", technical)

            reopened = c1_fixture._boundary_dialog(result, task=task)
            capture = ui_fixture._CaptureUi()
            capture.card = capture.column
            with patch.object(review_page, "ui", capture):
                PlanReviewDialog._render_selection_operation(reopened)
            visible = "\n".join(capture.visible)
            self.assertIn(str(failure["public_title"]), visible)
            self.assertTrue(
                any(title in visible for title in failure["affected_subject_titles"])
            )
            for label in ("换一个作品", "分开整理", "移除任务"):
                self.assertIn(label, capture.callbacks)


class CohortAlreadyGreenSafetyControls(unittest.TestCase):
    def test_zero_approval_draft_is_byte_stable_and_plan_change_resets_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PlanStore(root / "plans")
            review_root = root / "reviews"
            service = PlanReviewService(store, review_root, root / "work_catalog")
            plan = RenamePlan(
                task_id="cohort-draft-idempotence",
                source_root=root / "source",
                entries=(),
                metadata={"mapping_source": "inventory_review_draft"},
            )
            plan = save_plan_fixture(store, plan)
            first = service.load_or_create(plan)
            review_path = review_root / f"{plan.task_id}.json"
            first_bytes = review_path.read_bytes()
            second = service.load_or_create(plan)
            self.assertEqual(first, second)
            self.assertEqual(first_bytes, review_path.read_bytes())
            self.assertEqual(ReviewStatus.DRAFT, first.status)
            self.assertEqual((), first.approved_entry_ids)

            fingerprint = store.fingerprint(plan)
            revised = RenamePlan(
                task_id=plan.task_id,
                source_root=plan.source_root,
                entries=(),
                metadata={
                    "mapping_source": "inventory_review_draft",
                    "cohort_preview_fingerprint": "f" * 64,
                },
                revision_parent_fingerprint=fingerprint,
                revision_action="refresh_cohort_preview",
                revision_reason="冻结资料发生变化",
            )
            revised = save_plan_fixture(store, revised)
            refreshed = service.load_or_create(revised)
            refreshed_bytes = review_path.read_bytes()
            self.assertNotEqual(first_bytes, refreshed_bytes)
            self.assertEqual(store.fingerprint(revised), refreshed.plan_fingerprint)
            self.assertEqual(ReviewStatus.DRAFT, refreshed.status)
            self.assertEqual((), refreshed.approved_entry_ids)
            self.assertEqual(refreshed, service.load_or_create(revised))
            self.assertEqual(refreshed_bytes, review_path.read_bytes())

    def test_fixture_schema_and_privacy_boundary_are_stable(self):
        fixture = _fixture()
        self.assertEqual(1, fixture.get("schema_version"))
        self.assertEqual(
            "R22 C1 local Plan-only incident, sanitized semantic replay",
            fixture.get("provenance", {}).get("source"),
        )
        text = FIXTURE_PATH.read_text(encoding="utf-8")
        for forbidden in ("D:\\\\Anime", "C:\\\\Users", "task UUID=", "TMDB Key"):
            self.assertNotIn(forbidden, text)
        self.assertRegex(_canonical_sha256(fixture), r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
