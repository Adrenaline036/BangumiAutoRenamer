import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.rename.plan_review import (
    PlanReviewError,
    PlanReviewService,
    ReviewStatus,
    entry_id,
)
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import (
    PlanEntry,
    ProviderIdentity,
    RenamePlan,
    WorkIdentity,
)
from tests.presentation_fixtures import with_tv_presentation
from tests.projection_test_support import save_plan_fixture


class PlanReviewWorkflowTests(unittest.TestCase):
    def _plan(self) -> RenamePlan:
        return with_tv_presentation(RenamePlan(
            task_id="review-workflow",
            source_root=Path("source"),
            entries=(
                PlanEntry(
                    Path("source/01.mkv"),
                    Path("library/Season1/S01E01.mkv"),
                    season_id=1,
                    episode_id=1,
                    media_kind="video",
                    role="main_episode",
                    evidence=("rule mapping",),
                ),
                PlanEntry(
                    Path("source/OVA.mkv"),
                    Path("library/Season1/OVA.mkv"),
                    season_id=1,
                    media_kind="video",
                    role="episode_like_bonus",
                    requires_review=True,
                    evidence=("ambiguous bonus",),
                ),
            ),
            expected_sources=(
                Path("source/01.mkv"),
                Path("source/OVA.mkv"),
            ),
            metadata={
                "name": "Example",
                "target_root": str(Path("library")),
            },
        ))

    def test_override_creates_new_version_and_preserves_old_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlanStore(root / "plan")
            service = PlanReviewService(store, root / "review")
            plan = self._plan()
            plan = save_plan_fixture(store, plan)
            old_fingerprint = store.fingerprint(plan)

            revised = service.override_target(
                task_id=plan.task_id,
                expected_fingerprint=old_fingerprint,
                selected_entry_id=entry_id(plan.entries[1]),
                target_path=Path("library/Season1/OVA - reviewed.mkv"),
                reason="用户确认 OVA 名称",
            )
            new_fingerprint = store.fingerprint(revised)

            self.assertNotEqual(new_fingerprint, old_fingerprint)
            self.assertEqual(store.load(plan.task_id), revised)
            self.assertEqual(
                store.load_version(plan.task_id, old_fingerprint), plan
            )
            self.assertIn("用户确认 OVA 名称", revised.entries[1].evidence)
            self.assertTrue(revised.entries[1].requires_review)

    def test_completed_inventory_can_reopen_confirmed_draft_from_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlanStore(root / "plan")
            service = PlanReviewService(store, root / "review")
            provider = ProviderIdentity(
                status="confirmed",
                namespace="tmdb",
                entity_type="movie",
                provider_id="123",
                title="Example",
                year=2024,
                decision_source="manual",
            )
            draft = replace(
                self._plan(),
                metadata={
                    "name": "Example",
                    "target_root": "library",
                    "mapping_source": "inventory_review_draft",
                },
                work_identity=WorkIdentity(
                    status="confirmed",
                    domain="animation",
                    title="Example",
                    year=2024,
                    primary_provider=provider,
                    domain_source="task_media_type",
                ),
            )
            draft = save_plan_fixture(store, draft)
            draft_fingerprint = store.fingerprint(draft)
            completed = replace(
                draft,
                metadata={
                    **draft.metadata,
                    "mapping_source": "inventory_smart_completion",
                    "completion_mode": "movie_versions",
                },
                revision_parent_fingerprint=draft_fingerprint,
                revision_action="complete_inventory_draft:movie_continuous",
                revision_reason="用户选择电影正片版本",
            )
            completed = save_plan_fixture(store, completed)
            completed_fingerprint = store.fingerprint(completed)

            reopened = service.reopen_inventory_draft(
                completed.task_id,
                completed_fingerprint,
                reason="用户重新选择整理方式",
            )

            self.assertEqual(
                "inventory_review_draft",
                reopened.metadata["mapping_source"],
            )
            self.assertEqual(completed_fingerprint, reopened.revision_parent_fingerprint)
            self.assertEqual("reopen_inventory_draft", reopened.revision_action)
            self.assertEqual(store.load(completed.task_id), reopened)
            self.assertEqual(
                store.load_version(completed.task_id, completed_fingerprint),
                completed,
            )
            self.assertEqual(
                ReviewStatus.DRAFT,
                service.load_or_create(reopened).status,
            )

    def test_override_rederives_season_and_episode_from_new_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlanStore(root / "plan")
            service = PlanReviewService(store, root / "review")
            plan = self._plan()
            plan = save_plan_fixture(store, plan)

            revised = service.override_target(
                task_id=plan.task_id,
                expected_fingerprint=store.fingerprint(plan),
                selected_entry_id=entry_id(plan.entries[1]),
                target_path=Path("library/Season2/Example S02E03.mkv"),
                reason="用户确认应归入第二季第三集",
            )

            self.assertEqual(revised.entries[1].season_id, 2)
            self.assertEqual(revised.entries[1].episode_id, 3)

    def test_stale_fingerprint_rejects_approval_and_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlanStore(root / "plan")
            service = PlanReviewService(store, root / "review")
            plan = self._plan()
            plan = save_plan_fixture(store, plan)

            with self.assertRaisesRegex(PlanReviewError, "版本"):
                service.approve_entries(plan.task_id, "0" * 64, ())
            with self.assertRaisesRegex(PlanReviewError, "版本"):
                service.override_target(
                    plan.task_id,
                    "0" * 64,
                    entry_id(plan.entries[0]),
                    Path("library/Season1/changed.mkv"),
                    "stale",
                )

    def test_override_cannot_escape_original_target_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlanStore(root / "plan")
            service = PlanReviewService(store, root / "review")
            plan = self._plan()
            plan = save_plan_fixture(store, plan)

            with self.assertRaisesRegex(PlanReviewError, "越出"):
                service.override_target(
                    plan.task_id,
                    store.fingerprint(plan),
                    entry_id(plan.entries[1]),
                    Path("outside/escaped.mkv"),
                    "bad path",
                )

            self.assertEqual(store.load(plan.task_id), plan)

    def test_override_that_creates_target_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlanStore(root / "plan")
            service = PlanReviewService(store, root / "review")
            plan = self._plan()
            plan = save_plan_fixture(store, plan)
            fingerprint = store.fingerprint(plan)

            with self.assertRaisesRegex(PlanReviewError, "duplicate_target"):
                service.override_target(
                    plan.task_id,
                    fingerprint,
                    entry_id(plan.entries[1]),
                    plan.entries[0].target_path,
                    "bad collision",
                )

            self.assertEqual(store.load(plan.task_id), plan)

    def test_all_entries_must_be_approved_for_approved_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlanStore(root / "plan")
            service = PlanReviewService(store, root / "review")
            plan = self._plan()
            plan = save_plan_fixture(store, plan)
            fingerprint = store.fingerprint(plan)
            first_id = entry_id(plan.entries[0])
            second_id = entry_id(plan.entries[1])

            partial = service.approve_entries(
                plan.task_id, fingerprint, (first_id,)
            )
            approved = service.approve_entries(
                plan.task_id, fingerprint, (second_id,)
            )
            restored = service.load(plan.task_id)

            self.assertEqual(partial.status, ReviewStatus.DRAFT)
            self.assertEqual(approved.status, ReviewStatus.APPROVED)
            self.assertEqual(restored, approved)
            self.assertEqual(
                set(approved.approved_entry_ids), {first_id, second_id}
            )

    def test_corrupt_review_record_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlanStore(root / "plan")
            review_root = root / "review"
            service = PlanReviewService(store, review_root)
            plan = self._plan()
            plan = save_plan_fixture(store, plan)
            review_root.mkdir()
            (review_root / f"{plan.task_id}.json").write_text(
                "{broken", encoding="utf-8"
            )

            with self.assertRaises(PlanReviewError):
                service.load_or_create(plan)

            self.assertEqual(
                (review_root / f"{plan.task_id}.json").read_text(
                    encoding="utf-8"
                ),
                "{broken",
            )

    def test_delete_removes_current_history_and_review_for_exact_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlanStore(root / "plan")
            service = PlanReviewService(store, root / "review")
            plan = self._plan()
            plan = save_plan_fixture(store, plan)
            fingerprint = store.fingerprint(plan)
            service.load_or_create(plan)

            service.delete(plan.task_id)
            store.delete(plan.task_id)

            self.assertFalse(store.path_for(plan.task_id).exists())
            self.assertFalse(
                (root / "plan" / "history" / plan.task_id).exists()
            )
            self.assertFalse(
                (root / "review" / f"{plan.task_id}.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
