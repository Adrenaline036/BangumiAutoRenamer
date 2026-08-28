"""Incident-derived RED contracts for the R22 full-set Plan-only P1s.

The live evidence remains under ``local/``.  This module keeps only sanitized
provider/state shapes and writes exclusively below ``TemporaryDirectory``.
Provider transport is frozen; production queue/worker/reidentify/Plan paths
remain in the real call chain where a test is labelled as production-entry.

All incident contracts are ordinary regression tests after GREEN.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

import tests.test_complex_library_special_red_contract as special_fixture
import tests.test_provider_subject_plan_wiring_red_contract as phase_b_fixture
import tests.test_r1_real_media_production_red_contract as r1_fixture
import tests.test_r22_c1_cohort_incident_red_contract as cohort_fixture
from src.rename.decision_state import (
    DecisionState,
    DecisionStateError,
    DecisionStateStore,
)
from src.rename.plan_verifier import PlanVerifier
from src.rename.provider_link_decision import (
    freeze_provider_subject_boundary_edges,
)
from src.rename.rename_plan import ProviderIdentity, RenamePlan, WorkIdentity
from tests.presentation_fixtures import with_tv_presentation


def _case(selected_subject_id: str) -> dict[str, object]:
    case = deepcopy(cohort_fixture._fixture()["yama"])
    case["selected_subject_id"] = selected_subject_id
    return case


def _batch(case: dict[str, object]) -> dict[str, object]:
    return cohort_fixture._observation_batch(case)


def _segment_api():
    import src.rename.provider_link_decision as module

    value = getattr(module, "resolve_provider_subject_cohort_segment", None)
    if not callable(value):
        raise AssertionError(
            "shared pure cohort segment resolver is missing; preview/apply still "
            "cannot consume one invariant"
        )
    return value


def _edge_anchor_payload(edges: object) -> tuple[dict[str, object], ...]:
    if not isinstance(edges, (list, tuple)):
        return ()
    return tuple(dict(item) for item in edges if isinstance(item, dict))


def _tv_range_plan(
    root: Path,
    *,
    episodes: tuple[int, ...],
    episode_start: int,
    episode_end: int,
    duplicate_target: bool = False,
) -> RenamePlan:
    source = root / "source"
    target = root / "Anime" / "命运石之门 0 (2018)" / "Season 01"
    mapping = {}
    for index, episode in enumerate(episodes):
        src = source / f"Steins Gate 0 [{episode:02d}]-{index}.mkv"
        dst_episode = episodes[0] if duplicate_target and index else episode
        mapping[src] = target / f"命运石之门 0 (2018) S01E{dst_episode:02d}.mkv"
    plan = RenamePlan.from_mapping(
        task_id="sg0-range-contract",
        source_root=source,
        mapping=mapping,
        metadata={
            "provider_link_snapshot": {
                "schema_version": 2,
                "selected_subject_id": "129807",
                "tmdb_tv_id": "78102",
                "work_segment": {"subject_ids": ["129807"]},
                "derived_episode_ranges": [{
                    "bangumi_subject_id": "129807",
                    "tmdb_tv_id": "78102",
                    "season_number": 1,
                    "episode_start": episode_start,
                    "episode_end": episode_end,
                }],
                "resolution_fingerprint": "a" * 64,
                "evidence_fingerprint": "b" * 64,
            },
            "season_id": 1,
            "season_number": 1,
            "season_source": "provider_crosswalk",
        },
    )
    return with_tv_presentation(plan)


def _issue_codes(plan: RenamePlan) -> set[str]:
    return {issue.code for issue in PlanVerifier().verify(plan).issues}


class CohortAnchorRedContracts(unittest.TestCase):
    def test_selected_no_match_s3_freezes_full_segment_anchor_authority(self):
        case = _case("216310")
        batch = _batch(case)
        edges = _edge_anchor_payload(freeze_provider_subject_boundary_edges(
            selected_subject_id="216310",
            subject_observations=batch,
            bangumi_subjects=case["subjects"],
            bangumi_relations=case["relations"],
        ))
        problems = []
        if not edges:
            problems.append("selected no-match S3 has no reviewable cohort entry")
        anchor_ids = {
            str(anchor)
            for edge in edges
            for anchor in edge.get("anchor_subject_ids", ())
        }
        if "41658" not in anchor_ids:
            problems.append("frozen action did not retain the exact S1 anchor")
        if any(str(edge.get("anchor_tmdb_tv_id") or "") != "53918" for edge in edges):
            problems.append("frozen action lost the unique TMDB work identity")
        if any(not isinstance(edge.get("anchor_evidence_fingerprints"), dict) for edge in edges):
            problems.append("anchor evidence fingerprints are not frozen by subject")
        selected_row = next(
            item for item in batch["subject_observations"]
            if str(item.get("bangumi_subject_id") or "") == "216310"
        )
        if selected_row["queries"][0]["outcome"] != "no_match":
            problems.append("preview rewrote selected Provider observation instead of overlaying")
        self.assertEqual([], problems, "\n".join(problems))

    def test_one_shared_segment_resolver_accepts_concordant_anchors_and_rejects_drift(self):
        resolver = _segment_api()
        case = _case("216310")
        # A second exact row for the same TMDB work is legal when its frozen
        # provider snapshot is concordant with the first anchor.
        exact = deepcopy(case["observations"][0])
        exact["bangumi_subject_id"] = "290980"
        case["observations"][3] = exact
        batch = _batch(case)
        try:
            resolution = resolver(
                selected_subject_id="216310",
                subject_observations=batch,
                bangumi_subjects=case["subjects"],
                bangumi_relations=case["relations"],
            )
        except Exception as exc:  # RED must be a failure, never an error.
            self.fail(f"concordant multi-anchor segment was not resolved: {exc}")
        payload = (
            resolution.to_dict() if callable(getattr(resolution, "to_dict", None))
            else dict(resolution) if isinstance(resolution, dict) else {}
        )
        self.assertEqual(("290980", "41658"), tuple(sorted(payload.get("anchor_subject_ids", ()))))
        self.assertEqual("53918", str(payload.get("anchor_tmdb_tv_id") or ""))

        conflict = deepcopy(case)
        candidate = conflict["observations"][3]["queries"][0]["candidates"][0]
        candidate["seasons"][1]["episode_count"] = 99
        try:
            resolver(
                selected_subject_id="216310",
                subject_observations=_batch(conflict),
                bangumi_subjects=conflict["subjects"],
                bangumi_relations=conflict["relations"],
            )
        except (ValueError, RuntimeError):
            pass
        else:
            self.fail("same-ID contradictory anchor snapshots did not fail closed")

    def test_truncated_or_incomplete_batch_still_has_no_action(self):
        case = _case("216310")
        batch = _batch(case)
        batch["truncated"] = True
        self.assertEqual((), freeze_provider_subject_boundary_edges(
            selected_subject_id="216310",
            subject_observations=batch,
            bangumi_subjects=case["subjects"],
            bangumi_relations=case["relations"],
        ))


class CohortRealEntrypointRedContracts(unittest.IsolatedAsyncioTestCase):
    async def test_real_add_task_s3_no_match_exposes_disk_backed_cohort_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = phase_b_fixture._make_episode_source(
                root / "source",
                "Yama no Susume Third Season",
                (1, 2),
                subtitles=True,
            )
            harness = phase_b_fixture.ProviderSubjectPlanWiringRedContracts(
                methodName="runTest"
            )
            try:
                result = await harness._run_real_add_select(
                    root=root / "flow",
                    source=source,
                    subjects=phase_b_fixture.YAMA_SUBJECTS,
                    relations=phase_b_fixture.YAMA_RELATIONS,
                    selected_subject_id="216310",
                    fail_subject=(
                        "第二季", "セカンドシーズン", "第三季",
                        "サードシーズン", "Next Summit",
                    ),
                    failure_mode="no_match",
                )
            except Exception as exc:
                self.fail(f"real AddTask→worker→BGM S3 path crashed: {exc}")
        operation = result.task.get("selection_operation") or {}
        evidence = operation.get("provider_subject_evidence") or {}
        edges = _edge_anchor_payload(evidence.get("provider_subject_boundary_edges"))
        problems = harness._real_path_problems(result)
        if not edges:
            problems.append("disk task has no cohort action for selected no-match S3")
        if operation.get("state") not in {"awaiting_user", "succeeded"}:
            problems.append(f"selection operation ended as {operation.get('state')!r}")
        if result.source_unchanged is not True or result.target_files:
            problems.append("Plan-only cohort preview changed source or target media")
        self.assertEqual([], problems, "\n".join(problems))


class RegularEpisodeRangeRedContracts(unittest.TestCase):
    def test_verifier_rejects_regular_episode_outside_frozen_subject_range(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = _tv_range_plan(
                Path(directory), episodes=(24,), episode_start=1, episode_end=23
            )
            codes = _issue_codes(plan)
        self.assertIn("provider_regular_episode_overflow_unresolved", codes)

    def test_range_membership_rejects_lower_gap_and_duplicate_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lower = _tv_range_plan(root / "lower", episodes=(1,), episode_start=2, episode_end=23)
            duplicate = _tv_range_plan(
                root / "duplicate",
                episodes=(12, 13),
                episode_start=1,
                episode_end=23,
                duplicate_target=True,
            )
            lower_codes = _issue_codes(lower)
            duplicate_codes = _issue_codes(duplicate)
        problems = []
        if "provider_regular_episode_overflow_unresolved" not in lower_codes:
            problems.append("episode below frozen lower bound was not blocked")
        if not ({
            "provider_regular_episode_projection_duplicate",
            "duplicate_target",
        } & duplicate_codes):
            problems.append("two sources projected to one provider episode without a blocker")
        self.assertEqual([], problems, "\n".join(problems))

    def test_true_regular_episode_inside_frozen_range_is_not_an_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = _tv_range_plan(
                Path(directory), episodes=(24,), episode_start=1, episode_end=24
            )
            codes = _issue_codes(plan)
        self.assertNotIn("provider_regular_episode_overflow_unresolved", codes)


class RegularEpisodeOverflowProductionRedContracts(unittest.IsolatedAsyncioTestCase):
    async def test_real_sg0_24_and_subtitle_become_one_explicit_s00_candidate(self):
        class _OverflowSearch(special_fixture._SteinsSearch):
            def get_season_info(self, provider_id, season_number):
                if (int(provider_id), int(season_number)) == (78102, 0):
                    return {"season_number": 0, "episodes": ({
                        "id": 1649075,
                        "episode_number": 1,
                        "name": "OVA 结晶多形的情人节",
                        "air_date": "2018-12-21",
                        "runtime": 24,
                    },)}
                return super().get_season_info(provider_id, season_number)

        def renamer_factory(root: Path):
            renamer = special_fixture._configured_renamer(root)
            renamer.search = _OverflowSearch()
            return renamer

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = phase_b_fixture._make_episode_source(
                root / "source", "Steins Gate 0", tuple(range(1, 25)), subtitles=True
            )
            harness = special_fixture.ComplexLibrarySpecialRedContracts(
                methodName="runTest"
            )
            try:
                result = await harness._run(
                    root / "flow",
                    source,
                    "sg0-24-overflow",
                    "129807",
                    renamer_factory=renamer_factory,
                )
            except Exception as exc:
                self.fail(f"real SG0 1..24 provider path crashed: {exc}")
            plan = result.plan_store.load("sg0-24-overflow")
            video = next(
                entry for entry in plan.entries
                if entry.media_kind == "video" and "[24]" in entry.source_path.name
            )
            subtitle = next(
                entry for entry in plan.entries
                if entry.media_kind == "subtitle" and "[24]" in entry.source_path.name
            )
            codes = _issue_codes(plan)
            payload = json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True)
            selected_entry_id = special_fixture.entry_id(video)
            candidate = plan.metadata["provider_special_candidates"][selected_entry_id][0]
            decision = {
                "action": "confirmed_official_special",
                **{
                    key: candidate[key]
                    for key in (
                        "provider_namespace",
                        "provider_entity_type",
                        "provider_id",
                        "season_number",
                        "episode_number",
                        "provider_episode_id",
                    )
                },
            }
            review = special_fixture.PlanReviewService(
                result.plan_store,
                root / "flow" / "reviews",
                root / "flow" / "work_catalog",
            )
            confirmed = review.apply_provider_special_decision(
                plan.task_id,
                result.plan_store.fingerprint(plan),
                selected_entry_id,
                decision=decision,
                reason="确认冻结的官方 S00E01",
                persist_revision=False,
            )
            confirmed_video = next(
                entry for entry in confirmed.entries if entry.source_path == video.source_path
            )
            confirmed_subtitle = next(
                entry
                for entry in confirmed.entries
                if entry.source_path == subtitle.source_path
            )
        problems = []
        if "provider_regular_episode_overflow_unresolved" not in codes:
            problems.append("real S01E24 shape has no structured overflow issue")
        if "S01E24" in str(video.target_path) or "S01E24" in str(subtitle.target_path):
            problems.append("overflow video/subtitle still silently target S01E24")
        if "1649075" not in payload or "confirmed_official_special" in payload:
            problems.append("S00E01 is not offered as unconfirmed evidence, or was auto-authorized")
        if subtitle.subtitle is None or subtitle.subtitle.associated_video_source_path != video.source_path:
            problems.append("overflow subtitle did not remain paired to the exact video")
        if "Season 00" not in confirmed_video.target_path.parts or "S00E01" not in confirmed_video.target_path.name:
            problems.append("confirmed official special video did not move to S00E01")
        if (
            confirmed_subtitle.target_path.parent != confirmed_video.target_path.parent
            or "S00E01" not in confirmed_subtitle.target_path.name
        ):
            problems.append("confirmed official special subtitle did not follow its exact video")
        self.assertEqual([], problems, "\n".join(problems))


class LinkedProviderDecisionSchemaRedContracts(unittest.TestCase):
    def test_v2_explicit_empty_is_valid_but_v1_missing_linked_is_stale(self):
        base = DecisionState(
            task_id="linked-schema",
            confirmed_work={
                "status": "confirmed",
                "primary_provider": {
                    "status": "confirmed",
                    "namespace": "tmdb",
                    "entity_type": "tv",
                    "provider_id": "330299",
                    "title": "辉夜大小姐想让我告白：初吻不会结束",
                },
            },
        ).to_dict()
        v2 = deepcopy(base)
        v2["schema_version"] = 2
        v2["linked_provider_identities"] = []
        try:
            direct = DecisionState.from_dict(v2)
        except Exception as exc:
            self.fail(f"new-schema explicit empty linked set was not accepted: {exc}")
        self.assertEqual((), tuple(direct.linked_provider_identities))
        self.assertFalse(
            any(
                "font" in key or "work_intent" in key
                for key in direct.to_dict()
            ),
            "Decision v2 must not reserve future movie-font intent placeholders",
        )
        legacy = deepcopy(base)
        legacy["schema_version"] = 1
        legacy.pop("linked_provider_identities", None)
        try:
            DecisionState.from_dict(legacy)
        except DecisionStateError:
            pass
        else:
            self.fail("legacy Decision without linked field was silently accepted")

    def test_plan_roundtrip_preserves_legitimate_empty_linked_set(self):
        plan = RenamePlan.from_mapping(
            task_id="direct-provider-empty-linked",
            source_root=Path("source"),
            mapping={Path("source/01.mkv"): Path("target/Season 01/E01.mkv")},
        )
        restored = RenamePlan.from_dict(plan.to_dict())
        self.assertEqual((), restored.linked_provider_identities)

    def test_verifier_rejects_plan_that_drops_selected_provider_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _tv_range_plan(
                root,
                episodes=(1,),
                episode_start=1,
                episode_end=4,
            )
            metadata = dict(plan.metadata)
            snapshot = dict(metadata["provider_link_snapshot"])
            snapshot.update({
                "selected_subject_id": "425211",
                "tmdb_tv_id": "330299",
                "work_segment": {"subject_ids": ["425211"]},
            })
            metadata["provider_link_snapshot"] = snapshot
            plan = replace(
                plan,
                metadata=metadata,
                work_identity=WorkIdentity(
                    "confirmed",
                    "animation",
                    "辉夜大小姐想让我告白：初吻不会结束",
                    2023,
                    ProviderIdentity(
                        "confirmed",
                        "tmdb",
                        "tv",
                        "330299",
                        "辉夜大小姐想让我告白：初吻不会结束",
                        2023,
                        "provider_crosswalk",
                    ),
                    "provider_selection",
                ),
                linked_provider_identities=(),
            )
            codes = _issue_codes(plan)
        self.assertIn("linked_provider_identity_authority_missing", codes)


class LinkedProviderRealEntrypointRedContracts(unittest.IsolatedAsyncioTestCase):
    async def test_real_bgm_to_tmdb_reidentify_persists_linked_identity_from_decision(self):
        candidate = {
            "namespace": "bangumi",
            "entity_type": "subject",
            "provider_id": "425211",
            "title": "辉夜大小姐想让我告白：初吻不会结束",
            "original_title": "かぐや様は告らせたい-ファーストキッスは終わらない-",
            "year": 2023,
            "poster_url": r1_fixture.POSTER_URL,
            "source_url": "https://bgm.tv/subject/425211",
            "snapshot_at": "2026-08-26T00:00:00+00:00",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "Kaguya First Kiss (2023)"
            for episode in range(1, 5):
                special_fixture._write(
                    source / f"Kaguya First Kiss [{episode:02d}].mkv",
                    bytes([episode]),
                )
            harness = special_fixture.ComplexLibrarySpecialRedContracts(
                methodName="runTest"
            )
            try:
                result = await harness._run(
                    root / "flow",
                    source,
                    "first-kiss-bgm-reidentify",
                    "425211",
                    candidate=candidate,
                    renamer_factory=special_fixture._configured_kaguya_renamer,
                )
            except Exception as exc:
                self.fail(f"real BGM→TMDB reidentify path crashed: {exc}")
            plan = result.plan_store.load("first-kiss-bgm-reidentify")
            state = DecisionStateStore(root / "flow" / "decisions").load(
                "first-kiss-bgm-reidentify"
            )
            persisted_plan = RenamePlan.from_dict(plan.to_dict())
            persisted_state = DecisionState.from_dict(state.to_dict())
        expected = ("bangumi", "subject", "425211")
        plan_linked = {
            (item.namespace, item.entity_type, item.provider_id)
            for item in persisted_plan.linked_provider_identities
        }
        state_payload = persisted_state.to_dict()
        state_linked = {
            (
                str(item.get("namespace") or ""),
                str(item.get("entity_type") or ""),
                str(item.get("provider_id") or ""),
            )
            for item in state_payload.get("linked_provider_identities", ())
            if isinstance(item, dict)
        }
        problems = []
        if expected not in state_linked:
            problems.append("disk Decision lost selected BGM425211 linked authority")
        if expected not in plan_linked:
            problems.append("disk Plan did not project BGM425211 from Decision")
        if plan.metadata.get("decision_state_fingerprint") != DecisionStateStore.fingerprint(state):
            problems.append("Plan fingerprint does not bind the linked-aware Decision")
        primary = plan.work_identity.primary_provider if plan.work_identity else None
        if primary is None or primary.provider_id != "330299":
            problems.append("real reidentify did not retain TMDB330299 as primary")
        self.assertEqual([], problems, "\n".join(problems))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
