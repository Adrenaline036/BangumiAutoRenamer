"""Incident-shaped RED for the remaining 2026-09-04 full-set failures.

The fixture preserves provider media types, owned episode segments and the
review-state transitions that failed through the real AddTask/review flow.  It
contains no real media roots or Provider payloads.  The tests remain as
permanent incident regressions after the canonical GREEN.
"""

from __future__ import annotations

from dataclasses import replace
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from tests.legacy_exceptional_ui_fixture import render_exceptional_episode_repair
import src.pages.plan_review_page as review_page
from src.rename import provider_special_evidence as provider_special
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.decision_state import DecisionStateStore
from src.rename.projection_authority import (
    ProjectionAuthorityError,
    ProviderReselectionPendingInput,
    TaskTerminalMutation,
    commit_provider_reselection_pending,
)
from src.rename.rename_plan import (
    AssetRole,
    DimensionResolution,
    PlanEntry,
    PresentationAssetPlanContract,
    ProviderIdentity,
    RenamePlan,
    SubtitlePlanContract,
    WorkIdentity,
)
from src.task_queue import TaskQueueManager
import tests.test_complex_library_special_red_contract as ui_fixture
from tests.projection_test_support import save_plan_fixture


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "r24_fullset_provider_authority_incidents.json"
)


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _content_asset(group: str = "tv-group") -> PresentationAssetPlanContract:
    return PresentationAssetPlanContract(
        presentation_group=group,
        asset_role=AssetRole("content_version"),
        segment=None,
        version_descriptor=None,
    )


class FullsetProviderAuthorityFixtureContracts(unittest.TestCase):
    def test_fixture_is_sanitized_and_obligation_complete(self):
        payload = _fixture()
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.assertEqual(
            "r24-fullset-provider-authority-incidents/v1",
            payload["schema_version"],
        )
        self.assertEqual(7, len(payload["obligations"]))
        self.assertEqual(
            {f"OBL{index}" for index in range(1, 8)},
            {str(row).split()[0] for row in payload["obligations"]},
        )
        for forbidden in (
            "d:\\\\anime", "c:\\\\users", "api_key", "cookie",
            "password", ".env", "bangumi_token",
        ):
            self.assertNotIn(forbidden, serialized.casefold())


class FullsetProviderAuthorityIncidentRedContracts(unittest.TestCase):
    def test_provider_actionability_is_shared_and_presentation_scoped(self):
        """86/movie/mixed candidates must not be actionable by namespace alone."""

        try:
            from src.rename.provider_selection_authority import (
                provider_selection_capability,
            )
        except ImportError as exc:
            self.fail(f"shared provider capability authority is missing: {exc}")
        cases = _fixture()["incidents"]["provider_capability"]["cases"]
        for row in cases:
            with self.subTest(row["name"]):
                result = provider_selection_capability(
                    row["candidate"],
                    group_kind=row["group_kind"],
                    task_media_kind=row["task_media_kind"],
                )
                self.assertEqual(row["actionable"], result.actionable)

        dialog_source = inspect.getsource(review_page.PlanReviewDialog)
        callsites = {
            "ui": dialog_source,
            "queue": inspect.getsource(TaskQueueManager._prepare_reidentify_job),
        }
        from src.rename.process import Rename

        from src.rename import recognition_queue_admission
        self.assertIn('from .rename.recognition_queue_admission import admit', inspect.getsource(TaskQueueManager.enqueue_reidentify))
        self.assertIn('await admit(', inspect.getsource(TaskQueueManager.enqueue_reidentify))
        self.assertIn('_prepare_reidentify_job(', inspect.getsource(recognition_queue_admission))

        callsites["process"] = inspect.getsource(Rename.process)
        for label, source in callsites.items():
            self.assertIn(
                "provider_selection_capability(", source,
                f"{label} does not consume the shared capability authority",
            )

    def test_mixed_movie_and_episode_candidates_keep_distinct_presentations(self):
        """First Kiss-style mixed evidence must not be flattened into movies."""

        incident = _fixture()["incidents"]["provider_capability"]
        try:
            from src.rename.provider_selection_authority import (
                partition_provider_candidates_by_presentation,
            )
        except ImportError as exc:
            self.fail(f"provider presentation partition authority is missing: {exc}")
        candidates = tuple(
            {**dict(row["candidate"]), "case_name": row["name"]}
            for row in incident["cases"]
        )
        grouped = partition_provider_candidates_by_presentation(candidates)
        actual = {
            str(candidate["case_name"]): group_kind
            for group_kind, rows in grouped.items()
            for candidate in rows
        }
        self.assertEqual(incident["expected_groups"], actual)

        from src.rename.process import Rename

        self.assertIn(
            "partition_provider_candidates_by_presentation(",
            inspect.getsource(Rename.process),
        )

    def test_reselection_awaiting_user_atomically_supersedes_old_authority(self):
        """An awaiting-user selection must not retain the previous Plan revision."""

        incident = _fixture()["incidents"]["reselection"]
        source = inspect.getsource(
            TaskQueueManager._persist_reidentify_awaiting_user
        )
        self.assertIn(incident["required_transaction"], source)
        self.assertNotIn(
            f'{incident["forbidden_task_only_writer"]}(', source,
            "provider reselection still writes only the task record",
        )
        self.assertIn("result_decision_revision", source)
        self.assertIn("result_plan_fingerprint", source)

    def test_reselection_pending_commit_replaces_decision_plan_and_task_together(self):
        """The canonical transaction leaves no persisted old-work/new-task split."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "provider-reselection-atomic"
            source = root / "source" / "Episode 01.mkv"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"provider-reselection-fixture")
            mapping = {
                source: root / "target" / "Old Work" / "Season 01" / "S01E01.mkv"
            }
            provider = ProviderIdentity(
                status="confirmed",
                namespace="bangumi",
                entity_type="subject",
                provider_id="old-subject",
                title="Old Work",
                year=2020,
                decision_source="explicit_user",
            )
            old_draft = RenamePlan.from_mapping(
                task_id=task_id,
                source_root=source.parent,
                mapping=mapping,
            )
            old_draft = replace(
                old_draft,
                work_identity=WorkIdentity(
                    status="confirmed",
                    domain="animation",
                    title="Old Work",
                    year=2020,
                    primary_provider=provider,
                    domain_source="explicit_user",
                ),
            )
            plan_store = PlanStore(root / "plans")
            state_store = DecisionStateStore(root / "decisions")
            old_plan = save_plan_fixture(
                plan_store,
                old_draft,
                state_store=state_store,
            )
            old_state = state_store.load(task_id)
            task_root = root / "tasks"
            task_root.mkdir()
            (task_root / f"{task_id}.json").write_text(
                json.dumps({
                    "uuid": task_id,
                    "workflow_status": "invalid",
                    "provider_selection": provider.to_dict(),
                    "season_id": 1,
                }),
                encoding="utf-8",
            )
            fresh_draft = RenamePlan.from_mapping(
                task_id=task_id,
                source_root=source.parent,
                mapping={
                    source: root / "target" / "ManualReview" / source.name
                },
                metadata={"mapping_source": "inventory_review_draft"},
            )
            terminal = TaskTerminalMutation.prepare(
                task_root,
                task_id,
                lambda decision, _plan, current: {
                    **current,
                    "workflow_status": "need_confirm",
                    "provider_selection": None,
                    "season_id": None,
                    "result_decision_revision": decision.revision,
                },
            )
            new_state, new_plan = commit_provider_reselection_pending(
                state_store,
                plan_store,
                ProviderReselectionPendingInput(
                    task_id=task_id,
                    expected_revision=old_state.revision,
                ),
                fresh_draft,
                task_terminal=terminal,
            )
            disk_task = json.loads(
                (task_root / f"{task_id}.json").read_text("utf-8")
            )
            self.assertGreater(new_state.revision, old_state.revision)
            self.assertEqual({}, new_state.confirmed_work)
            self.assertEqual((), new_state.provider_evidence)
            self.assertIsNone(new_plan.work_identity)
            self.assertNotEqual(
                PlanStore.fingerprint(old_plan), PlanStore.fingerprint(new_plan)
            )
            self.assertEqual(new_state.revision, disk_task["decision_state_revision"])
            self.assertEqual(PlanStore.fingerprint(new_plan), disk_task["plan_fingerprint"])
            self.assertIsNone(disk_task["provider_selection"])
            self.assertIsNone(disk_task["season_id"])
            with self.assertRaises(ProjectionAuthorityError):
                commit_provider_reselection_pending(
                    state_store,
                    plan_store,
                    ProviderReselectionPendingInput(
                        task_id=task_id,
                        expected_revision=old_state.revision,
                    ),
                    fresh_draft,
                    task_terminal=TaskTerminalMutation.prepare(
                        task_root, task_id, lambda *_args: disk_task
                    ),
                )

    def test_confirmed_same_work_subject_ranges_form_one_owned_union(self):
        """A second confirmed subject in the same season is not an overflow."""

        case = _fixture()["incidents"]["same_work_ranges"]
        source_root = Path("sanitized-source")
        target_root = Path("sanitized-target") / "Fixture Work"
        entries = tuple(
            PlanEntry(
                source_path=source_root / f"Fixture E{episode:02d}.mkv",
                target_path=(
                    target_root / "Season 01" / f"Fixture S01E{episode:02d}.mkv"
                ),
                season_id=1,
                episode_id=episode,
                media_kind="video",
                role="main_episode",
                presentation_asset=_content_asset(),
            )
            for episode in case["episode_numbers"]
        )
        plan = RenamePlan(
            task_id="same-work-range-union",
            source_root=source_root,
            entries=entries,
            expected_sources=tuple(entry.source_path for entry in entries),
            metadata={"canonical_work_root": str(target_root)},
        )
        observation = provider_special.ProviderSpecialObservation.build(
            state="completed_with_rows",
            provider_namespace="tmdb",
            provider_entity_type="tv",
            provider_id=case["tmdb_tv_id"],
            episodes=({
                "provider_namespace": "tmdb",
                "provider_entity_type": "tv",
                "provider_id": case["tmdb_tv_id"],
                "season_number": 0,
                "episode_number": 1,
                "provider_episode_id": "special-1",
                "title": "Fixture Special",
                "air_date": "2024-01-01",
                "runtime": 24,
            },),
        )
        resolution = SimpleNamespace(
            tmdb_tv_id=case["tmdb_tv_id"],
            season_assignments=case["season_assignments"],
            derived_episode_ranges=case["derived_episode_ranges"],
        )
        projected = provider_special.project_provider_special_evidence(
            plan,
            observation,
            selected_subject_id=case["selected_subject_id"],
            provider_link_resolution=resolution,
        )
        actual = sorted(
            int(row["projected_episode"])
            for row in projected.metadata.get(
                "provider_regular_episode_overflows", ()
            )
        )
        self.assertEqual(case["expected_overflows"], actual)

    def test_exceptional_video_without_official_candidates_keeps_safe_exits(self):
        """Eligibility-unavailable videos still allow Extra or independent work."""

        case = _fixture()["incidents"][
            "exceptional_without_provider_candidates"
        ]
        entry = PlanEntry(
            source_path=Path("sanitized") / "Unknown Special.mkv",
            target_path=Path("sanitized-target") / "Extras" / "Unknown Special.mkv",
            season_id=1,
            media_kind="video",
            role="unknown",
            requires_review=True,
            presentation_asset=PresentationAssetPlanContract(
                presentation_group="tv-group",
                asset_role=AssetRole("extra", "other"),
                segment=None,
                version_descriptor=None,
            ),
        )
        identifier = entry_id(entry)
        plan = RenamePlan(
            task_id="exceptional-no-candidate",
            source_root=Path("sanitized"),
            entries=(entry,),
            expected_sources=(entry.source_path,),
            metadata={
                "provider_special_eligibility_unavailable": (identifier,),
            },
        )
        for issue_code in (
            case["issue_code"],
            "provider_special_evidence_unavailable",
        ):
            with self.subTest(issue_code=issue_code):
                dialog = SimpleNamespace(
                    plan=plan,
                    snapshot=SimpleNamespace(rows=({
                        "entry_id": identifier,
                        "issue_codes": (issue_code,),
                    },)),
                    _apply_exceptional_special_inline=lambda *_args: None,
                    _partition_exceptional_as_cross_work=lambda *_args: None,
                )
                ui = ui_fixture._CaptureUi()
                render_exceptional_episode_repair(
                    dialog,
                    ui_api=ui,
                    entry_identifier=entry_id,
                    file_name=lambda path: Path(path).name,
                    render_partition_action=lambda callback: ui.button(
                        "作为独立作品整理", on_click=callback
                    ),
                )
                for action in case["required_actions"]:
                    self.assertIn(action, ui.callbacks)

    def test_unassociated_subtitle_reaches_structured_review_instead_of_abort(self):
        """Only unresolved videos block range freezing; subtitles stay reviewable."""

        case = _fixture()["incidents"]["subtitle"]
        source_root = Path("sanitized")
        video = PlanEntry(
            source_path=source_root / "Fixture E01.mkv",
            target_path=Path("sanitized-target") / "Season 01" / "Fixture S01E01.mkv",
            season_id=1,
            episode_id=1,
            media_kind="video",
            role="main_episode",
            presentation_asset=_content_asset(),
        )
        subtitle = PlanEntry(
            source_path=source_root / "Feature Commentary.ass",
            target_path=Path("sanitized-target") / "Season 01" / "Feature Commentary.ass",
            season_id=1,
            media_kind="subtitle",
            role="subtitle",
            requires_review=True,
            subtitle=SubtitlePlanContract(
                associated_video_source_path=None,
                raw_label="Feature Commentary",
                primary_language=None,
                script=None,
                association_state=DimensionResolution.UNRESOLVED,
                parse_state=DimensionResolution.UNRESOLVED,
                reason_codes=(case["issue_code"],),
            ),
        )
        plan = RenamePlan(
            task_id="subtitle-reviewable",
            source_root=source_root,
            entries=(video, subtitle),
            expected_sources=(video.source_path, subtitle.source_path),
        )
        try:
            segments = review_page._bind_regular_sources_to_ranges(
                plan,
                ({
                    "bangumi_subject_id": "subject-a",
                    "tmdb_tv_id": "900001",
                    "season_number": 1,
                    "episode_start": 1,
                    "episode_end": 12,
                },),
                selected_subject_id="subject-a",
                evidence_fingerprint="a" * 64,
            )
        except review_page.TVPlanningAuthorityError as exc:
            self.fail(f"unassociated subtitle aborted Plan generation: {exc}")
        self.assertEqual(case["expected_regular_sources"], len(segments[0]["sources"]))
        self.assertEqual(str(video.source_path.resolve(strict=False)), segments[0]["sources"][0])



if __name__ == "__main__":
    unittest.main()
