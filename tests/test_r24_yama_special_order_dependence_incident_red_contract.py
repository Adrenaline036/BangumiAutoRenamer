"""Incident RED for ERR-20260903-025 official-special order dependence.

The two user-observed orderings are represented by sanitized temporary
packages. Provider transport is local and bounded. The former incident
expected-failure wrapper was removed after the canonical GREEN repair.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from typing import Mapping
from unittest.mock import patch

import src.pages.plan_review_page as review_page
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.decision_state import DecisionStateStore
from src.rename.get_info import TMDBRequestError
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import (
    AssetRole,
    PlanEntry,
    PresentationAssetPlanContract,
    ProviderIdentity,
    RenamePlan,
    WorkIdentity,
)
from src.pages.review_operation_view import ReviewCommandApplyResult
import tests.test_provider_subject_plan_wiring_red_contract as phase_b_fixture
import tests.test_r22_revised_c_c0_red_contract as c1_fixture
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
)


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "r24_yama_special_order_dependence_incident.json"
)
def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text("utf-8"))


def _manifest(root: Path) -> tuple[tuple[str, int, str], ...]:
    if not root.exists():
        return ()
    return tuple(
        (
            path.relative_to(root).as_posix(),
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    )


def _make_source(root: Path, producer: dict[str, object]) -> Path:
    source = root / str(producer["folder"])
    for episode in producer["regular_episodes"]:
        path = source / f"{producer['folder']} [{int(episode):02d}].mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"regular-{episode}".encode("ascii"))
    for index, relative in enumerate(producer["special_files"], start=1):
        path = source / str(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"special-{index}".encode("ascii"))
        path.with_suffix(".sc.ass").write_text(
            "[Script Info]\nTitle: sanitized OVA\n", encoding="utf-8"
        )
    return source


class _FrozenYamaSpecialSearch(phase_b_fixture._ProviderSearch):
    def __init__(self, selected_subject_id: str, outcome: str = "rows"):
        super().__init__(fail_subject=None, failure_mode="no_match")
        self.selected_subject_id = str(selected_subject_id)
        self.outcome = outcome
        self.season_zero_calls = 0

    def _lookup(self, query: object):
        value = " ".join(str(query or "").casefold().split())
        no_match_ids = (
            {"41658"}
            if self.selected_subject_id == "86670"
            else set(phase_b_fixture.YAMA_SUBJECTS) - {self.selected_subject_id}
        )
        for subject_id, subject in phase_b_fixture.YAMA_SUBJECTS.items():
            exact = {
                " ".join(str(subject["name"]).casefold().split()),
                " ".join(str(subject["name_cn"]).casefold().split()),
            }
            if value in exact:
                if str(subject_id) in no_match_ids:
                    return "", None
                return "向山进发", dict(phase_b_fixture.YAMA_TMDB)
        return super()._lookup(query)

    def get_season_info(self, provider_id, season_number):
        self.season_zero_calls += 1
        if str(provider_id) != "53918" or int(season_number) != 0:
            return None
        if self.outcome == "error":
            raise TMDBRequestError("sanitized Season 0 failure")
        rows = (
            tuple(dict(row) for row in _fixture()["season_zero_rows"])
            if self.outcome == "rows" else ()
        )
        return {"season_number": 0, "episodes": rows}


async def _run_producer(
    root: Path,
    order: dict[str, object],
    *,
    season_zero_outcome: str = "rows",
):
    producer = dict(order["producer"])
    source = _make_source(root / "sources", producer)
    source_before = _manifest(source)
    created = []
    original_factory = phase_b_fixture._configured_renamer

    def configured(actual_root, **kwargs):
        renamer = original_factory(actual_root, **kwargs)
        renamer.search = _FrozenYamaSpecialSearch(
            str(producer["bangumi_subject_id"]),
            outcome=season_zero_outcome,
        )
        if season_zero_outcome == "missing_resolver":
            renamer.search.get_season_info = None
        created.append(renamer)
        return renamer

    harness = phase_b_fixture.ProviderSubjectPlanWiringRedContracts(
        methodName="runTest"
    )
    with patch.object(
        phase_b_fixture, "_configured_renamer", side_effect=configured
    ):
        result = await harness._run_real_add_select(
            root=root / "flow",
            source=source,
            subjects=phase_b_fixture.YAMA_SUBJECTS,
            relations=phase_b_fixture.YAMA_RELATIONS,
            selected_subject_id=str(producer["bangumi_subject_id"]),
            fail_subject=None,
            failure_mode="no_match",
        )
    if len(created) != 1:
        raise AssertionError("producer fixture did not create one Rename worker")
    return result, created[0], source, source_before, producer


def _configured_result_roots(result):
    """Reopen the producer's actual config, not a retired Task UI field."""
    renamer = result.renamer
    return phase_b_fixture._root_config_patch({
        'bangumi_path': str(renamer.BANGUMI_PATH),
        'movie_path': str(renamer.MOVIE_PATH),
        'anime_path': str(renamer.ANIME_PATH),
        'anime_movie_path': str(renamer.ANIME_MOVIE_PATH),
    })


def _apply_boundary(result, *, same_work=True) -> ReviewCommandApplyResult:
    if result.state.pending_tv_selection is not None:
        from src.pages.review_operation_view import build_review_operation_presentation
        from src.rename.plan_review import PlanReviewService

        kind = 'confirm_cohort' if same_work else 'separate_work'
        actions = build_review_operation_presentation(result.task).actions
        command = next(dict(action.payload) for action in actions
                       if action.payload and action.payload.get('kind') == kind)
        # Preserve the actual request and all frozen content. This is public
        # service setup, not evidence that a registered GUI event was invoked.
        with c1_fixture._boundary_patches(result, []), _configured_result_roots(result):
            service = PlanReviewService(PlanStore(result.plans), result.reviews,
                result.root / 'work_catalog', decision_state_root=result.decisions,
                task_root=result.tasks,
                provider_link_catalog_root=result.manager.provider_link_catalog_root,
                root_snapshot_source=result.manager.root_snapshot_source)
            intent = service.prepare_tv_boundary_completion(result.task_id,
                PlanStore.fingerprint(result.plan), review_command=command)
            service.commit_tv_boundary_completion(intent)
        return ReviewCommandApplyResult.APPLIED

    raise ValueError("当前任务缺少季度待确认依据，旧回调已停用")


def _eligible_video_ids(plan: RenamePlan) -> set[str]:
    return {
        entry_id(entry)
        for entry in plan.entries
        if entry.media_kind == "video"
        and entry.role == "episode_like_bonus"
    }


class YamaSpecialOrderFixtureControls(unittest.TestCase):
    def test_fixture_is_sanitized_and_freezes_both_user_observed_orders(self):
        fixture = _fixture()
        serialized = json.dumps(fixture, ensure_ascii=False, sort_keys=True)
        self.assertEqual(1, fixture["schema_version"])
        self.assertEqual(
            [("s1", "s2"), ("s2", "s1")],
            [tuple(row["sequence"]) for row in fixture["orders"]],
        )
        self.assertEqual(
            {"completed_with_rows", "completed_empty", "not_run", "unavailable_or_error"},
            set(fixture["required_observation_states"]),
        )
        for forbidden in ("D:\\\\", "C:\\\\", "api_key", "cookie", ".env"):
            self.assertNotIn(forbidden.casefold(), serialized.casefold())


class YamaSpecialOrderIncidentRedContracts(unittest.IsolatedAsyncioTestCase):
    async def test_both_first_task_orders_freeze_and_project_official_special_evidence(self):
        problems: list[str] = []
        for order in _fixture()["orders"]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result, worker, source, source_before, producer = (
                    await _run_producer(root, dict(order))
                )
                context = current_provider_selection_context(result.task)
                observation = context.get("provider_subject_evidence", {}).get(
                    "provider_special_observation"
                )
                alias = str(producer["alias"])
                if not isinstance(observation, dict):
                    problems.append(f"{alias}: awaiting-user outcome omitted frozen Season 0 observation")
                elif observation.get("state") != "completed_with_rows":
                    problems.append(f"{alias}: frozen observation state is not completed_with_rows")
                if worker.search.season_zero_calls != 1:
                    problems.append(f"{alias}: Season 0 observation call count was {worker.search.season_zero_calls}")

                applied = _apply_boundary(result)
                plan = PlanStore(result.plans).load(result.task_id)
                reloaded_plan = PlanStore(result.plans).load(result.task_id)
                reloaded_state = DecisionStateStore(result.decisions).load(
                    result.task_id
                )
                eligible_ids = _eligible_video_ids(plan)
                candidates = dict(
                    plan.metadata.get("provider_special_candidates") or {}
                )
                projected_observation = plan.metadata.get(
                    "provider_special_observation"
                )
                issues = tuple(
                    issue
                    for issue in PlanVerifier().verify(plan).issues
                    if issue.code == "provider_special_decision_required"
                )
                expected_count = int(producer["expected_special_videos"])
                if applied is not ReviewCommandApplyResult.APPLIED:
                    problems.append(f"{alias}: boundary confirmation was not applied")
                if len(eligible_ids) != expected_count:
                    problems.append(
                        f"{alias}: typed eligible videos={len(eligible_ids)} expected={expected_count}"
                    )
                if set(candidates) != eligible_ids:
                    problems.append(
                        f"{alias}: projected candidates={len(candidates)} eligible={len(eligible_ids)}"
                    )
                if len(issues) != expected_count:
                    problems.append(
                        f"{alias}: manual official-special issues={len(issues)} expected={expected_count}"
                    )
                if not isinstance(projected_observation, Mapping) or (
                    projected_observation.get("state") != "completed_with_rows"
                    or len(str(projected_observation.get("observation_fingerprint") or "")) != 64
                ):
                    problems.append(f"{alias}: Plan lost typed/fingerprinted Season 0 evidence")
                if PlanStore.fingerprint(plan) != PlanStore.fingerprint(reloaded_plan):
                    problems.append(f"{alias}: Plan restart changed projected evidence")
                if reloaded_state.revision < 1:
                    problems.append(f"{alias}: Decision restart lost the committed revision")
                if _manifest(source) != source_before or not result.source_unchanged:
                    problems.append(f"{alias}: Plan-only flow changed source files")
                if result.target_files or result.target_roots_exist:
                    problems.append(f"{alias}: Plan-only flow materialized a target")
                if result.business_json:
                    problems.append(f"{alias}: Plan-only flow wrote formal business state")
        self.assertEqual([], problems, "\n".join(problems))

    async def test_empty_and_unavailable_observations_stay_distinct_after_ui_projection(self):
        cases = (
            ("empty", "completed_empty", False),
            ("error", "unavailable_or_error", True),
            ("missing_resolver", "unavailable_or_error", True),
        )
        for outcome, expected_state, expected_unavailable in cases:
            with tempfile.TemporaryDirectory() as directory:
                result, worker, source, source_before, _producer = (
                    await _run_producer(
                        Path(directory),
                        dict(_fixture()["orders"][0]),
                        season_zero_outcome=outcome,
                    )
                )
                self.assertEqual(1 if outcome != "missing_resolver" else 0,
                                 worker.search.season_zero_calls)
                self.assertIs(ReviewCommandApplyResult.APPLIED, _apply_boundary(result))
                plan = PlanStore(result.plans).load(result.task_id)
                observation = plan.metadata.get("provider_special_observation")
                self.assertIsInstance(observation, Mapping)
                self.assertEqual(expected_state, observation.get("state"))
                self.assertFalse(plan.metadata.get("provider_special_candidates"))
                codes = {issue.code for issue in PlanVerifier().verify(plan).issues}
                self.assertEqual(
                    expected_unavailable,
                    "provider_special_evidence_unavailable" in codes,
                )
                self.assertEqual(source_before, _manifest(source))
                self.assertFalse(result.target_roots_exist)
                self.assertFalse(result.business_json)

    async def test_disconnect_at_the_single_projector_fails_closed_before_commit(self):
        module = importlib.import_module("src.rename.provider_special_evidence")
        order = dict(_fixture()["orders"][0])
        with tempfile.TemporaryDirectory() as directory:
            result, _worker, source, source_before, _producer = await _run_producer(
                Path(directory), order
            )
            plan_path = result.plans / f"{result.task_id}.json"
            decision_path = result.decisions / f"{result.task_id}.json"
            before = (plan_path.read_bytes(), decision_path.read_bytes())
            with patch.object(
                module,
                "project_provider_special_evidence",
                side_effect=module.ProviderSpecialEvidenceError(
                    "projector disconnected"
                ),
            ):
                if result.state.pending_tv_selection is not None:
                    with self.assertRaisesRegex(module.ProviderSpecialEvidenceError, 'projector disconnected'):
                        _apply_boundary(result)
                else:
                    applied = _apply_boundary(result)
                    self.assertIs(ReviewCommandApplyResult.REJECTED, applied)
            self.assertEqual(before, (plan_path.read_bytes(), decision_path.read_bytes()))
            self.assertEqual(source_before, _manifest(source))
            self.assertFalse(result.target_roots_exist)


class ProviderSpecialEvidenceStateRedContracts(unittest.TestCase):
    def test_observation_states_and_fingerprint_tamper_are_explicit(self):
        module = importlib.import_module("src.rename.provider_special_evidence")
        rows = tuple(dict(row) for row in _fixture()["season_zero_rows"])

        def resolver(_provider_id, _season_number):
            return {"season_number": 0, "episodes": rows}

        def empty(_provider_id, _season_number):
            return {"season_number": 0, "episodes": ()}

        def failure(_provider_id, _season_number):
            raise TMDBRequestError("sanitized failure")

        cases = (
            (True, resolver, "completed_with_rows"),
            (True, empty, "completed_empty"),
            (False, None, "not_run"),
            (True, None, "unavailable_or_error"),
            (True, failure, "unavailable_or_error"),
        )
        for enabled, candidate_resolver, expected in cases:
            observation = module.freeze_provider_special_observation(
                provider_namespace="tmdb",
                provider_entity_type="tv",
                provider_id="53918",
                resolver=candidate_resolver,
                enabled=enabled,
            )
            self.assertEqual(expected, observation.state)
            self.assertEqual(64, len(observation.observation_fingerprint))
            self.assertEqual(
                observation,
                module.ProviderSpecialObservation.from_mapping(
                    observation.to_dict()
                ),
            )
            tampered = observation.to_dict()
            tampered["state"] = (
                "completed_empty"
                if expected != "completed_empty" else "not_run"
            )
            with self.assertRaises(module.ProviderSpecialEvidenceError):
                module.ProviderSpecialObservation.from_mapping(tampered)

    def test_verifier_rejects_missing_or_unavailable_evidence_for_typed_ova(self):
        source = Path("sanitized") / "SPs" / "Yama OVA 01.mkv"
        entry = PlanEntry(
            source_path=source,
            target_path=Path("sanitized-target") / "Season 01" / "Extras" / source.name,
            season_id=1,
            media_kind="video",
            role="episode_like_bonus",
            requires_review=False,
            presentation_asset=PresentationAssetPlanContract(
                presentation_group="tv-episodes",
                asset_role=AssetRole("extra", "episode_like_bonus"),
                segment=None,
                version_descriptor=None,
            ),
        )
        for metadata in (
            {},
            {
                "provider_special_observation": {
                    "schema_version": 1,
                    "state": "unavailable_or_error",
                    "provider_identity": {
                        "namespace": "tmdb",
                        "entity_type": "tv",
                        "provider_id": "53918"
                    },
                    "season_number": 0,
                    "episodes": [],
                    "observation_fingerprint": "0" * 64
                }
            },
        ):
            plan = RenamePlan(
                task_id="err025-verifier-disconnect",
                source_root=Path("sanitized"),
                entries=(entry,),
                expected_sources=(source,),
                metadata=metadata,
                work_identity=WorkIdentity(
                    status="confirmed",
                    domain="animation",
                    title="Yama",
                    year=2013,
                    primary_provider=ProviderIdentity(
                        status="confirmed",
                        namespace="tmdb",
                        entity_type="tv",
                        provider_id="53918",
                        title="Yama",
                        year=2013,
                        decision_source="provider_selection",
                    ),
                    domain_source="provider_selection",
                ),
            )
            codes = {issue.code for issue in PlanVerifier().verify(plan).issues}
            self.assertIn("provider_special_evidence_unavailable", codes)

    def test_normal_and_ui_producers_use_one_canonical_projector(self):
        module = importlib.import_module("src.rename.provider_special_evidence")
        projector = module.project_provider_special_evidence
        process_module = importlib.import_module("src.rename.process")
        process_source = inspect.getsource(process_module)
        from src.rename import initial_tv_completion
        self.assertIn("project_provider_special_evidence", process_source)
        self.assertIn("project_provider_special_evidence", inspect.getsource(initial_tv_completion))
        self.assertIsNotNone(projector)
        canonical_source = inspect.getsource(module)
        self.assertEqual(1, canonical_source.count("def _provider_special_eligibility"))
        self.assertEqual(0, process_source.count("def _provider_special_eligibility"))
        self.assertFalse(
            hasattr(process_module.Rename, "_freeze_provider_special_candidates")
        )
        self.assertNotIn("._freeze_provider_special_candidates(", process_source)


if __name__ == "__main__":
    unittest.main()
