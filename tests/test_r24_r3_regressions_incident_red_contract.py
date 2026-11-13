"""Incident-shaped RED contracts for the R3 Plan-only regressions.

The fixtures are sanitized and use temporary paths only.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from src.pages.plan_review_page import PlanReviewDialog
from src.rename.decision_state import DecisionStateStore
from src.rename.plan_review import PlanReviewError, PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import (
    AssetRole,
    PlanEntry,
    PresentationAssetPlanContract,
    PresentationGroup,
    RenamePlan,
    SubtitlePlanContract,
)
from src.rename.review_operation import (
    TaskWorkflowState,
    build_review_operation,
)
from src.rename.subtitle_labels import DimensionResolution
from src.rename.successor_authority import (
    CohortAuthority,
    CohortAuthorityReader,
    CohortAuthorityStore,
    SuccessorAuthorityError,
    successor_authority_root,
)
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
    persist_provider_selection_attempt,
)
from tests.test_r24_phase_d_f7_f8_remediation_contract import (
    build_canonical_actionable_task,
)
from tests.test_r24_successor_authority_incident_red_contract import (
    _confirmed_s1_authority,
)


def _special_bundle_plan(root: Path, *, with_candidate: bool) -> tuple[RenamePlan, str]:
    source_root = root / "source"
    work_root = root / "target" / "Steins Gate"
    stem = "Steins Gate 2011 SP"
    source_video = source_root / f"{stem}.mkv"
    source_audio = source_root / f"{stem}.mka"
    source_subtitle = source_root / f"{stem}.sc.ass"
    old_parent = work_root / "Season 01" / "Extras"
    group_id = "tv-special-bundle"
    video = PlanEntry(
        source_path=source_video,
        target_path=old_parent / f"{stem}.mkv",
        season_id=1,
        media_kind="video",
        role="episode_like_bonus",
        requires_review=True,
        presentation_asset=PresentationAssetPlanContract(
            group_id, AssetRole("extra", "episode_like_bonus"), None, None
        ),
    )
    audio = PlanEntry(
        source_path=source_audio,
        target_path=old_parent / f"{stem}.mka",
        season_id=1,
        media_kind="associated",
        role="associated",
        requires_review=True,
        presentation_asset=PresentationAssetPlanContract(
            group_id, AssetRole("associated"), None, None
        ),
    )
    subtitle = PlanEntry(
        source_path=source_subtitle,
        target_path=old_parent / f"{stem}.zh-Hans.ass",
        season_id=1,
        media_kind="subtitle",
        role="extra_subtitle",
        requires_review=True,
        subtitle=SubtitlePlanContract(
            associated_video_source_path=source_video,
            association_candidates=(source_video,),
            raw_label="sc",
            primary_language="zh",
            script="Hans",
            association_state=DimensionResolution.RESOLVED,
            parse_state=DimensionResolution.RESOLVED,
            decision_source="incident_fixture",
        ),
        presentation_asset=None,
    )
    selected_id = entry_id(video)
    candidate = {
        "provider_namespace": "tmdb",
        "provider_entity_type": "tv",
        "provider_id": "42509",
        "season_number": 0,
        "episode_number": 6,
        "provider_episode_id": "123006",
    }
    from src.rename.rename_plan import WorkIdentity, ProviderIdentity
    from src.rename.provider_special_evidence import freeze_provider_special_observation
    work = WorkIdentity('confirmed', 'animation', 'Steins Gate', 2011,
        ProviderIdentity('confirmed','tmdb','tv','42509','Steins Gate',2011,'explicit_user'))
    observation = freeze_provider_special_observation(provider_namespace='tmdb',provider_entity_type='tv',provider_id='42509',
        resolver=lambda *_: {'episodes': [candidate] if with_candidate else []},enabled=True)
    return RenamePlan(
        task_id="r3-extra-bundle",
        work_identity=work,
        source_root=source_root,
        entries=(video, audio, subtitle),
        metadata={
            "canonical_work_root": str(work_root),
            "target_root": str(work_root),
            "season_number": 1,
            "provider_special_observation": observation.to_dict(),
            "provider_special_candidates": (
                {selected_id: [candidate]} if with_candidate else {}
            ),
        },
        presentation_groups=(
            PresentationGroup(group_id, "tv_episodes", "Steins Gate", None),
        ),
    ), selected_id


def _review(root: Path) -> PlanReviewService:
    return PlanReviewService(
        PlanStore(root / "plans"), root / "reviews", root / "catalog"
    )


def _bundle_problems(plan: RenamePlan) -> list[str]:
    video = next(entry for entry in plan.entries if entry.media_kind == "video")
    audio = next(entry for entry in plan.entries if entry.media_kind == "associated")
    subtitle = next(entry for entry in plan.entries if entry.media_kind == "subtitle")
    problems: list[str] = []
    if len({video.target_path.parent, audio.target_path.parent, subtitle.target_path.parent}) != 1:
        problems.append("typed bundle没有进入同一目标目录")
    if audio.target_path.stem != video.target_path.stem:
        problems.append("associated audio没有跟随video stem")
    if not subtitle.target_path.name.startswith(video.target_path.stem + "."):
        problems.append("精确绑定字幕没有跟随video stem")
    if subtitle.presentation_asset is not None:
        problems.append("字幕错误携带presentation_asset")
    if (
        audio.presentation_asset is None
        or audio.presentation_asset.asset_role.kind != "associated"
    ):
        problems.append("associated audio丢失呈现资产身份")
    if (
        subtitle.subtitle is None
        or subtitle.subtitle.associated_video_source_path != video.source_path
    ):
        problems.append("字幕丢失精确关联视频权威")
    issue_codes = {
        issue.code for issue in PlanVerifier().verify(plan).issues
    }
    if "subtitle_presentation_asset_forbidden" in issue_codes:
        problems.append("完整PlanVerifier仍拒绝字幕呈现资产")
    return problems


class CandidateFreeExtraBundleIncidentRed(unittest.TestCase):
    def test_candidate_free_extra_accepts_and_projects_the_typed_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, selected_id = _special_bundle_plan(root, with_candidate=False)
            service = _review(root)
            problems: list[str] = []
            try:
                with patch(
                    "src.rename.plan_review.PlanReviewService._current_plan",
                    return_value=plan,
                ):
                    revised = service.apply_provider_special_decision(
                        plan.task_id,
                        "f" * 64,
                        selected_id,
                        decision={"action": "unmatched_extra"},
                        reason="用户明确保留为附加内容",
                        persist_revision=False,
                    )
            except PlanReviewError as exc:
                revised = None
                problems.append(str(exc))
            if revised is not None:
                problems.extend(_bundle_problems(revised))
                if "Extras" not in next(
                    entry.target_path.parts for entry in revised.entries
                    if entry.media_kind == "video"
                ):
                    problems.append("Extra视频没有进入Extras")
        self.assertEqual([], problems)


    def test_official_action_still_requires_a_frozen_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, selected_id = _special_bundle_plan(root, with_candidate=False)
            service = _review(root)
            with patch(
                "src.rename.plan_review.PlanReviewService._current_plan",
                return_value=plan,
            ):
                with self.assertRaisesRegex(
                    PlanReviewError, "没有冻结的官方特别篇候选"
                ):
                    service.apply_provider_special_decision(
                        plan.task_id,
                        "d" * 64,
                        selected_id,
                        decision={"action": "confirmed_official_special"},
                        reason="不能绕过官方候选",
                        persist_revision=False,
                    )

    def test_extra_rejects_nonvideo_and_excludes_font_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, selected_id = _special_bundle_plan(root, with_candidate=False)
            audio = next(entry for entry in plan.entries if entry.media_kind == "associated")
            service = _review(root)
            with patch(
                "src.rename.plan_review.PlanReviewService._current_plan",
                return_value=plan,
            ):
                with self.assertRaisesRegex(PlanReviewError, "只能应用于视频"):
                    service.apply_provider_special_decision(
                        plan.task_id,
                        "c" * 64,
                        entry_id(audio),
                        decision={"action": "unmatched_extra"},
                        reason="非视频负控",
                        persist_revision=False,
                    )

            font = replace(
                audio,
                source_path=audio.source_path.with_suffix(".zip"),
                target_path=audio.target_path.with_suffix(".zip"),
                role="font",
            )
            plan_with_font = replace(plan, entries=plan.entries + (font,))
            with patch(
                "src.rename.plan_review.PlanReviewService._current_plan",
                return_value=plan_with_font,
            ):
                revised = service.apply_provider_special_decision(
                    plan.task_id,
                    "b" * 64,
                    selected_id,
                    decision={"action": "unmatched_extra"},
                    reason="字体包不能进入特别篇闭包",
                    persist_revision=False,
                )
            revised_font = next(
                entry for entry in revised.entries if entry.source_path == font.source_path
            )
            self.assertEqual(font.target_path, revised_font.target_path)

    def test_exact_subtitle_binding_outweighs_separate_presentation_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, selected_id = _special_bundle_plan(root, with_candidate=False)
            crossed = replace(
                plan,
                presentation_groups=plan.presentation_groups + (
                    PresentationGroup(
                        "other-group", "tv_episodes", "Other", None
                    ),
                ),
            )
            service = _review(root)
            with patch(
                "src.rename.plan_review.PlanReviewService._current_plan",
                return_value=crossed,
            ):
                revised = service.apply_provider_special_decision(
                    plan.task_id,
                    "a" * 64,
                    selected_id,
                    decision={"action": "unmatched_extra"},
                    reason="精确字幕关联优先于展示分组",
                    persist_revision=False,
                )
            self.assertEqual([], _bundle_problems(revised))

    def test_official_special_projects_video_audio_and_subtitle_as_one_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, selected_id = _special_bundle_plan(root, with_candidate=True)
            service = _review(root)
            decision = {
                "action": "confirmed_official_special",
                "provider_namespace": "tmdb",
                "provider_entity_type": "tv",
                "provider_id": "42509",
                "season_number": 0,
                "episode_number": 6,
                "provider_episode_id": "123006",
            }
            with patch(
                "src.rename.plan_review.PlanReviewService._current_plan",
                return_value=plan,
            ):
                revised = service.apply_provider_special_decision(
                    plan.task_id,
                    "e" * 64,
                    selected_id,
                    decision=decision,
                    reason="用户确认官方特别篇",
                    persist_revision=False,
                )
            problems = _bundle_problems(revised)
            if "Season 00" not in next(
                entry.target_path.parts for entry in revised.entries
                if entry.media_kind == "video"
            ):
                problems.append("官方特别篇视频没有进入Season 00")
        self.assertEqual([], problems)


class ProviderSpecialProductionWiringContracts(unittest.TestCase):
    @staticmethod
    def _dialog(plan: RenamePlan) -> SimpleNamespace:
        dialog = SimpleNamespace(
            plan=plan,
            task_decision=SimpleNamespace(
                provider_special_decisions={}, revision=7, content_selection=None
            ),
        )
        dialog._adopt_revision = MagicMock()
        return dialog

    def test_public_extra_command_uses_canonical_projector_and_terminal_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, selected_id = _special_bundle_plan(
                Path(directory), with_candidate=False
            )
            dialog = self._dialog(plan)
            projector = MagicMock(return_value=plan)
            dialog.review_service = SimpleNamespace(
                apply_provider_special_decision=projector
            )
            committed_state = SimpleNamespace(
                provider_special_decisions={
                    selected_id: {"action": "unmatched_extra"}
                },
                revision=8,
            )
            commit = MagicMock(return_value=(committed_state, plan))
            dialog.decision_command_service = SimpleNamespace(update=commit)

            with patch("src.pages.plan_review_page.notify"):
                PlanReviewDialog._apply_provider_special_inline(
                    dialog,
                    selected_id, "unmatched_extra", None
                )

            projector.assert_called_once()
            self.assertEqual(
                {"action": "unmatched_extra"},
                projector.call_args.kwargs["decision"],
            )
            commit.assert_called_once()
            self.assertEqual(
                {selected_id: {"action": "unmatched_extra"}},
                commit.call_args.kwargs["changes"]["provider_special_decisions"],
            )
            self.assertIsNotNone(commit.call_args.kwargs["task_terminal"])
            dialog._adopt_revision.assert_called_once_with(plan)

    def test_public_extra_command_fails_closed_when_projector_is_disconnected(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, selected_id = _special_bundle_plan(
                Path(directory), with_candidate=False
            )
            dialog = self._dialog(plan)
            projector = MagicMock(
                side_effect=PlanReviewError("canonical projector disconnected")
            )
            dialog.review_service = SimpleNamespace(
                apply_provider_special_decision=projector
            )
            commit = MagicMock()
            dialog.decision_command_service = SimpleNamespace(update=commit)

            with patch("src.pages.plan_review_page.notify") as notify:
                PlanReviewDialog._apply_provider_special_inline(
                    dialog,
                    selected_id, "unmatched_extra", None
                )

            projector.assert_called_once()
            commit.assert_not_called()
            dialog._adopt_revision.assert_not_called()
            notify.assert_called_once_with(
                "canonical projector disconnected", type="negative"
            )


class CohortProjectionDriftIncidentRed(unittest.TestCase):
    def test_noncohort_special_decision_does_not_invalidate_cohort_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            decisions = Path(directory) / "decisions"
            producer = _confirmed_s1_authority(decisions)
            store = DecisionStateStore(decisions)
            changed = store.save(
                replace(
                    producer,
                    provider_special_decisions={
                        "sanitized-entry": {"action": "unmatched_extra"}
                    },
                ),
                expected_revision=producer.revision,
            )
            lookup = CohortAuthorityReader.from_decision_root(decisions).lookup(
                "86670", consumer_task_id="consumer-s2"
            )
            problems = []
            if (lookup.status, lookup.reason_code) != (
                "matched", "current_authority_matched"
            ):
                problems.append(
                    f"非cohort Decision撤销了权威:{lookup.status}/{lookup.reason_code}"
                )
            if DecisionStateStore.fingerprint(changed) == DecisionStateStore.fingerprint(producer):
                problems.append("控制无效：完整Decision fingerprint没有变化")
            changed_again = store.save(
                replace(
                    changed,
                    manual_role_decisions={"sanitized-other": "disc_extra"},
                    provider_evidence=changed.provider_evidence + ({
                        "status": "observation_only",
                    },),
                ),
                expected_revision=changed.revision,
            )
            restarted_lookup = CohortAuthorityReader.from_decision_root(
                decisions
            ).lookup("86670", consumer_task_id="consumer-after-restart")
            if restarted_lookup.status != "matched":
                problems.append("manual-role/raw-observation漂移在重启后撤销了权威")
            series_binding = deepcopy(changed_again.series_binding)
            series_binding["canonical_root"] = str(Path(directory) / "new-route")
            store.save(
                replace(changed_again, series_binding=series_binding),
                expected_revision=changed_again.revision,
            )
            route_lookup = CohortAuthorityReader.from_decision_root(decisions).lookup(
                "86670", consumer_task_id="consumer-route-change"
            )
            if route_lookup.status != "matched":
                problems.append("纯目标路由变化污染了cohort identity authority")
        self.assertEqual([], problems)

    def test_cohort_critical_season_drift_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            decisions = Path(directory) / "decisions"
            producer = _confirmed_s1_authority(decisions)
            producer_bytes = DecisionStateStore(decisions).path_for(producer.task_id).read_bytes()
            store = CohortAuthorityStore(successor_authority_root(decisions))
            record = store.list_current()[0]
            # V3 producer state is audit-only. Change the actual frozen fact,
            # including its content hash, while retaining contradictory proof.
            payload = deepcopy(record.__dict__)
            payload.pop('authority_fingerprint')
            payload['season_assignments']['86670'] = 99
            for row in payload['resolution']['season_assignments']:
                if row['bangumi_subject_id'] == '86670':
                    row['season_number'] = 99
            changed = CohortAuthority.create(**payload)
            store.path_for(record.authority_id).write_text(
                json.dumps(changed.to_dict()), encoding='utf-8')
            lookup = CohortAuthorityReader.from_decision_root(decisions).lookup(
                "86670", consumer_task_id="consumer-s2"
            )
            self.assertEqual(producer_bytes,
                DecisionStateStore(decisions).path_for(producer.task_id).read_bytes())
        self.assertEqual(
            ("invalid", "authority_registration_mismatch"),
            (lookup.status, lookup.reason_code),
        )

    def test_v1_authority_record_is_read_only_invalid_not_silently_upgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            decisions = Path(directory) / "decisions"
            _confirmed_s1_authority(decisions)
            authority = CohortAuthorityStore(
                successor_authority_root(decisions)
            ).list_current()[0]
            legacy = authority.to_dict()
            legacy["schema_version"] = 1
            legacy["policy_version"] = "provider-subject-cohort-v1"
            legacy.pop("source_cohort_projection_fingerprint")
            with self.assertRaises(SuccessorAuthorityError):
                CohortAuthority.from_dict(legacy)


class FailedLifecycleCapabilityIncidentRed(unittest.TestCase):
    def test_failed_and_awaiting_user_with_same_capability_expose_same_commands(self):
        awaiting_task = build_canonical_actionable_task()
        awaiting_operation, _ = build_review_operation(awaiting_task)
        failed_task = deepcopy(awaiting_task)
        attempt = current_provider_selection_context(failed_task)
        attempt["state"] = "failed"
        attempt["failure_code"] = "relation_unresolved"
        persist_provider_selection_attempt(failed_task, attempt)
        failed_operation, _ = build_review_operation(failed_task)

        awaiting_commands = tuple(command.kind for command in awaiting_operation.commands)
        failed_commands = tuple(command.kind for command in failed_operation.commands)
        problems = []
        if failed_operation.state != TaskWorkflowState.ACTIONABLE:
            problems.append(f"failed标签覆盖canonical capability:{failed_operation.state.value}")
        if failed_commands != awaiting_commands:
            problems.append("相同证据的failed/awaiting_user命令不一致")
        restarted_task = json.loads(json.dumps(failed_task, ensure_ascii=False))
        restarted_operation, _ = build_review_operation(restarted_task)
        if tuple(command.kind for command in restarted_operation.commands) != failed_commands:
            problems.append("重启后failed capability命令发生变化")
        self.assertEqual([], problems)

    def test_failed_attempt_without_boundary_evidence_remains_blocked(self):
        task = build_canonical_actionable_task()
        attempt = current_provider_selection_context(task)
        attempt["state"] = "failed"
        attempt["provider_subject_evidence"] = {}
        attempt.pop("provider_subject_cohort_preview", None)
        persist_provider_selection_attempt(task, attempt)
        operation, _ = build_review_operation(task)
        self.assertEqual(TaskWorkflowState.BLOCKED, operation.state)
        self.assertEqual((), operation.commands)

    def test_failed_attempt_with_tampered_edge_fingerprint_remains_blocked(self):
        task = build_canonical_actionable_task()
        attempt = current_provider_selection_context(task)
        attempt["state"] = "failed"
        attempt["provider_subject_evidence"]["provider_subject_boundary_edges"][0][
            "evidence_fingerprint"
        ] = "0" * 64
        persist_provider_selection_attempt(task, attempt)
        operation, _ = build_review_operation(task)
        self.assertEqual(TaskWorkflowState.BLOCKED, operation.state)
        self.assertEqual((), operation.commands)


if __name__ == "__main__":
    unittest.main()
