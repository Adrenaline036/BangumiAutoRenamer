import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from src.rename.plan_review import (
    PlanReviewError,
    PlanReviewService,
    entry_id,
)
from src.rename.plan_store import PlanStore, PlanStoreError
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import (
    PlanEntry,
    RenamePlan,
    SeriesScopeIdentity,
    SubtitleOverrideScope,
    SubtitlePlanContract,
    UnknownTokenAction,
    UnknownTokenDisposition,
)
from src.rename.subtitle_labels import (
    DimensionResolution,
    SUBTITLE_ASSOCIATION_RULE_VERSION,
    SUBTITLE_LABEL_RULE_VERSION,
)
from tests.presentation_fixtures import with_tv_presentation
from tests.projection_test_support import save_plan_fixture


def make_plan(*, unresolved=False, unknown=False):
    video = PlanEntry.from_paths(
        Path("Source/Ep01.mkv"),
        Path("Library/Show/Season 01/Show S01E01.mkv"),
    )
    contract = SubtitlePlanContract(
        associated_video_source_path=video.source_path,
        raw_label="Mystery.SC" if unknown else "SC",
        primary_language="zh",
        script="Hans",
        track_title_tokens=(),
        flags=(),
        unknown_tokens=("Mystery",) if unknown else (),
        association_state=DimensionResolution.RESOLVED,
        parse_state=(
            DimensionResolution.UNRESOLVED
            if unresolved else DimensionResolution.RESOLVED
        ),
        association_rule_version=SUBTITLE_ASSOCIATION_RULE_VERSION,
        label_rule_version=SUBTITLE_LABEL_RULE_VERSION,
        reason_codes=("fixture",),
    )
    subtitle = PlanEntry(
        source_path=Path("Source/Ep01.Mystery.SC.ass" if unknown else "Source/Ep01.SC.ass"),
        target_path=Path("Library/Show/Season 01/Show S01E01.zh-Hans.ass"),
        season_id=1,
        episode_id=1,
        media_kind="subtitle",
        requires_review=unresolved or unknown,
        subtitle=contract,
    )
    return with_tv_presentation(RenamePlan(
        task_id="subtitle-plan",
        source_root=Path("Source"),
        entries=(video, subtitle),
        expected_sources=(video.source_path, subtitle.source_path),
        metadata={"target_root": "Library", "nested": {"items": ["a"]}},
    ))


def make_series_plan():
    base = make_plan(unknown=True)
    video2 = PlanEntry.from_paths(
        Path("Source/Ep02.mkv"),
        Path("Library/Show/Season 01/Show S01E02.mkv"),
    )
    contract2 = replace(
        base.entries[1].subtitle,
        associated_video_source_path=video2.source_path,
        raw_label="MYSTERY.sc",
        unknown_tokens=("MYSTERY",),
    )
    subtitle2 = replace(
        base.entries[1],
        source_path=Path("Source/Ep02.MYSTERY.sc.ass"),
        target_path=Path("Library/Show/Season 01/Show S01E02.zh-Hans.ass"),
        episode_id=2,
        subtitle=contract2,
    )
    return with_tv_presentation(replace(
        base,
        entries=(base.entries[0], base.entries[1], video2, subtitle2),
        expected_sources=(
            base.entries[0].source_path,
            base.entries[1].source_path,
            video2.source_path,
            subtitle2.source_path,
        ),
        series_scope=SeriesScopeIdentity.from_provider("tmdb", "tv", 12345),
    ))


class SubtitlePlanContractTests(unittest.TestCase):
    def test_schema_v4_roundtrip_is_strict_and_fingerprint_covers_contract(self):
        plan = make_plan()
        payload = plan.to_dict()
        self.assertEqual(6, payload["schema_version"])
        restored = RenamePlan.from_dict(payload)
        self.assertEqual(payload, restored.to_dict())
        self.assertEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(restored))

        subtitle = restored.entries[1]
        revised = replace(
            restored,
            entries=(restored.entries[0], replace(
                subtitle,
                subtitle=replace(subtitle.subtitle, flags=("forced",)),
            )),
        )
        self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(revised))

    def test_metadata_and_subtitle_contract_are_deeply_immutable(self):
        plan = make_plan()
        with self.assertRaises(TypeError):
            plan.metadata["nested"] = {}
        with self.assertRaises(TypeError):
            plan.metadata["nested"]["items"] = ()
        with self.assertRaises(FrozenInstanceError):
            plan.entries[1].subtitle.raw_label = "changed"

    def test_legacy_schema_v1_fails_closed(self):
        payload = make_plan().to_dict()
        payload["schema_version"] = 1
        with self.assertRaisesRegex(ValueError, "schema_version"):
            RenamePlan.from_dict(payload)

    def test_subtitle_contract_presence_is_enforced_by_verifier(self):
        plan = make_plan()
        missing = replace(plan.entries[1], subtitle=None)
        result = PlanVerifier().verify(replace(plan, entries=(plan.entries[0], missing)))
        self.assertIn("subtitle_contract_missing", {issue.code for issue in result.issues})

        wrong = replace(plan.entries[0], subtitle=plan.entries[1].subtitle)
        result = PlanVerifier().verify(replace(plan, entries=(wrong, plan.entries[1])))
        self.assertIn("subtitle_contract_on_non_subtitle", {issue.code for issue in result.issues})

    def test_unresolved_subtitle_is_a_hard_issue_not_approvable_review_flag(self):
        plan = make_plan(unresolved=True)
        result = PlanVerifier().verify(plan)
        self.assertIn("subtitle_parse_unresolved", {issue.code for issue in result.issues})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = PlanStore(root / "plans")
            plan = save_plan_fixture(store, plan)
            service = PlanReviewService(store, root / "reviews")
            fingerprint = store.fingerprint(plan)
            with self.assertRaisesRegex(PlanReviewError, "subtitle_parse_unresolved"):
                service.approve_entries(
                    plan.task_id,
                    fingerprint,
                    [entry_id(entry) for entry in plan.entries],
                )

    def test_stale_rule_version_is_a_hard_issue(self):
        plan = make_plan()
        subtitle = plan.entries[1]
        stale = replace(subtitle, subtitle=replace(subtitle.subtitle, label_rule_version="old"))
        result = PlanVerifier().verify(replace(plan, entries=(plan.entries[0], stale)))
        self.assertIn("subtitle_label_rule_stale", {issue.code for issue in result.issues})

    def test_unknown_token_requires_explicit_disposition_and_discard_reason(self):
        plan = make_plan(unknown=True)
        result = PlanVerifier().verify(plan)
        self.assertIn("subtitle_unknown_disposition_missing", {issue.code for issue in result.issues})
        contract = plan.entries[1].subtitle
        discard = replace(contract, unknown_dispositions=(UnknownTokenDisposition(
            "Mystery", UnknownTokenAction.DISCARD_MANUAL,
        ),))
        result = PlanVerifier().verify(replace(
            plan,
            entries=(plan.entries[0], replace(plan.entries[1], subtitle=discard)),
        ))
        self.assertIn("subtitle_unknown_discard_reason_missing", {issue.code for issue in result.issues})

    def test_generic_target_override_is_disabled_for_subtitles(self):
        plan = make_plan()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = PlanStore(root / "plans")
            plan = save_plan_fixture(store, plan)
            service = PlanReviewService(store, root / "reviews")
            with self.assertRaisesRegex(PlanReviewError, "结构化字幕决定"):
                service.override_target(
                    plan.task_id,
                    store.fingerprint(plan),
                    entry_id(plan.entries[1]),
                    Path("Library/Show/Season 01/custom.ass"),
                    "try raw path",
                )

    def test_structured_override_regenerates_target_and_invalidates_approval(self):
        plan = make_plan(unresolved=True, unknown=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = PlanStore(root / "plans")
            plan = save_plan_fixture(store, plan)
            service = PlanReviewService(store, root / "reviews")
            before = store.fingerprint(plan)
            service.load_or_create(plan)
            revised = service.override_subtitle_decision(
                plan.task_id,
                before,
                entry_id(plan.entries[1]),
                primary_language="zh",
                script="Hans",
                track_title_tokens=("Mystery",),
                flags=("default",),
                unknown_dispositions=(UnknownTokenDisposition(
                    "Mystery", UnknownTokenAction.PRESERVE_AS_TITLE,
                ),),
                reason="确认 Mystery 是字幕轨道标题",
            )
            after = store.fingerprint(revised)
            self.assertNotEqual(before, after)
            self.assertEqual(
                Path("Library/Show/Season 01/Show S01E01.zh-Hans.Mystery.default.ass"),
                revised.entries[1].target_path,
            )
            review = service.load(plan.task_id)
            self.assertEqual(after, review.plan_fingerprint)
            self.assertEqual((), review.approved_entry_ids)
            self.assertFalse(PlanVerifier().verify(revised).issues)

    def test_series_rule_only_matches_current_plan_and_keeps_video_associations(self):
        plan = make_series_plan()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = PlanStore(root / "plans")
            plan = save_plan_fixture(store, plan)
            service = PlanReviewService(store, root / "reviews")
            revised = service.apply_subtitle_override_rule(
                plan.task_id,
                store.fingerprint(plan),
                entry_id(plan.entries[1]),
                scope=SubtitleOverrideScope.SERIES,
                primary_language="zh",
                script="Hans",
                track_title_tokens=("Mystery",),
                unknown_dispositions=(UnknownTokenDisposition(
                    "Mystery", UnknownTokenAction.PRESERVE_AS_TITLE,
                ),),
                reason="同一 Plan 中精确标签签名一致",
            )
            subtitles = [entry for entry in revised.entries if entry.media_kind == "subtitle"]
            self.assertEqual(2, len(subtitles))
            self.assertEqual(
                (Path("Source/Ep01.mkv"), Path("Source/Ep02.mkv")),
                tuple(entry.subtitle.associated_video_source_path for entry in subtitles),
            )
            self.assertEqual(
                ("Mystery", "MYSTERY"),
                tuple(entry.subtitle.track_title_tokens[-1] for entry in subtitles),
            )
            self.assertTrue(all(
                entry.subtitle.decision_source == "manual_series"
                for entry in subtitles
            ))
            self.assertEqual(1, len(revised.subtitle_override_rules))
            self.assertFalse(PlanVerifier().verify(revised).issues)

    def test_series_scope_requires_stable_provider_identity(self):
        plan = make_plan(unknown=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = PlanStore(root / "plans")
            plan = save_plan_fixture(store, plan)
            service = PlanReviewService(store, root / "reviews")
            with self.assertRaisesRegex(PlanReviewError, "TMDB TV"):
                service.apply_subtitle_override_rule(
                    plan.task_id,
                    store.fingerprint(plan),
                    entry_id(plan.entries[1]),
                    scope=SubtitleOverrideScope.SERIES,
                    primary_language="zh",
                    script="Hans",
                    unknown_dispositions=(UnknownTokenDisposition(
                        "Mystery", UnknownTokenAction.DISCARD_MANUAL, "发布组标记",
                    ),),
                    reason="人工确认",
                )

    def test_series_preview_has_no_store_or_review_side_effect(self):
        plan = make_series_plan()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = PlanStore(root / "plans")
            plan = save_plan_fixture(store, plan)
            service = PlanReviewService(store, root / "reviews")
            before = store.fingerprint(plan)
            preview = service.apply_subtitle_override_rule(
                plan.task_id,
                before,
                entry_id(plan.entries[1]),
                scope=SubtitleOverrideScope.SERIES,
                primary_language="zh",
                script="Hans",
                track_title_tokens=("Mystery",),
                unknown_dispositions=(UnknownTokenDisposition(
                    "Mystery", UnknownTokenAction.PRESERVE_AS_TITLE,
                ),),
                reason="只预览当前 Plan 精确命中范围",
                _persist=False,
            )

            self.assertNotEqual(before, store.fingerprint(preview))
            self.assertEqual(before, store.fingerprint(store.load(plan.task_id)))
            with self.assertRaises(PlanReviewError):
                service.load(plan.task_id)

    def test_latest_rule_revert_creates_new_fingerprint_and_blank_review(self):
        plan = make_plan(unknown=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = PlanStore(root / "plans")
            plan = save_plan_fixture(store, plan)
            service = PlanReviewService(store, root / "reviews")
            applied = service.override_subtitle_decision(
                plan.task_id,
                store.fingerprint(plan),
                entry_id(plan.entries[1]),
                primary_language="zh",
                script="Hans",
                unknown_dispositions=(UnknownTokenDisposition(
                    "Mystery", UnknownTokenAction.DISCARD_MANUAL, "发布组标记",
                ),),
                reason="人工确认发布组标记",
            )
            applied_fingerprint = store.fingerprint(applied)
            reverted = service.revert_subtitle_rule(
                plan.task_id,
                applied_fingerprint,
                applied.subtitle_override_rules[-1].rule_id,
                "撤销以重新判断",
            )
            reverted_fingerprint = store.fingerprint(reverted)
            self.assertNotIn(reverted_fingerprint, {
                store.fingerprint(plan), applied_fingerprint,
            })
            self.assertEqual((), service.load(plan.task_id).approved_entry_ids)
            self.assertEqual("revert_subtitle_rule", reverted.revision_action.split(":")[0])


if __name__ == "__main__":
    unittest.main()
