import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.rename.plan_review import PlanReviewService, ReviewStatus, entry_id
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import (
    AssetRole,
    PlanEntry,
    PresentationAssetPlanContract,
    RenamePlan,
)
from tests.presentation_fixtures import with_movie_presentation
from tests.projection_test_support import save_plan_fixture


class PresentationReviewTests(unittest.TestCase):
    def _service_and_plan(self, root: Path):
        source = root / "source" / "Feature.mkv"
        target = root / "library" / "Example (2026)" / "old-name.mkv"
        plan = with_movie_presentation(RenamePlan(
            task_id="presentation-review",
            source_root=source.parent,
            entries=(PlanEntry(
                source,
                target,
                media_kind="video",
                requires_review=True,
            ),),
            expected_sources=(source,),
            metadata={"target_root": str(root / "library")},
        ))
        store = PlanStore(root / "plans")
        plan = save_plan_fixture(store, plan)
        return PlanReviewService(store, root / "reviews"), store, plan

    def test_structured_version_revision_changes_fingerprint_and_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service, store, plan = self._service_and_plan(Path(temp_dir))
            old_fingerprint = store.fingerprint(plan)

            revised = service.revise_presentation_asset(
                plan.task_id,
                old_fingerprint,
                entry_id(plan.entries[0]),
                presentation_group="fixture-movie",
                role_kind="content_version",
                edition_key="theatrical",
                edition_label="剧场版",
                resolution="1080p",
                dynamic_range="SDR",
                key_audio="FLAC 2.0",
                preferred=True,
                reason="人工核对文件名与探测摘要",
            )

            descriptor = revised.entries[0].presentation_asset.version_descriptor
            self.assertNotEqual(old_fingerprint, store.fingerprint(revised))
            self.assertEqual("剧场版 1080p SDR FLAC 2.0", descriptor.display_label)
            self.assertEqual(
                "Example (2026) - 剧场版 1080p SDR FLAC 2.0.mkv",
                revised.entries[0].target_path.name,
            )
            self.assertEqual(
                descriptor.version_ref,
                revised.presentation_groups[0].preferred_version_ref,
            )
            self.assertEqual(
                ReviewStatus.DRAFT,
                service.load(plan.task_id).status,
            )

    def test_group_upsert_records_manual_provider_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service, store, plan = self._service_and_plan(Path(temp_dir))

            revised = service.upsert_presentation_group(
                plan.task_id,
                store.fingerprint(plan),
                group_id="tv-episodes",
                kind="tv_episodes",
                display_name="TV 分集版",
                provider_namespace="bangumi",
                provider_entity_type="subject",
                provider_id="425211",
                provider_title="Example TV",
                provider_year=2026,
                reason="人工确认分集版作品身份",
            )

            group = next(
                item for item in revised.presentation_groups
                if item.group_id == "tv-episodes"
            )
            self.assertIsNone(group.provider_identity)
            self.assertEqual(
                "manual_review",
                revised.work_identity.primary_provider.decision_source,
            )
            self.assertEqual(
                "425211", revised.work_identity.primary_provider.provider_id
            )
            self.assertEqual("upsert_presentation_group:tv-episodes", revised.revision_action)

    def test_group_kind_changes_without_changing_work_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service, store, plan = self._service_and_plan(Path(temp_dir))
            plan = replace(
                plan,
                entries=(replace(
                    plan.entries[0],
                    presentation_asset=PresentationAssetPlanContract(
                        "fixture-movie", AssetRole("unknown"), None, None
                    ),
                ),),
            )
            plan = save_plan_fixture(store, plan)
            original_identity = plan.work_identity

            revised = service.revise_presentation_group_kind(
                plan.task_id,
                store.fingerprint(plan),
                group_id="fixture-movie",
                kind="tv_episodes",
                reason="本地文件实际按集播放",
            )

            group = revised.presentation_groups[0]
            self.assertEqual("tv_episodes", group.kind)
            self.assertEqual("TV 分集版", group.display_name)
            self.assertIsNone(group.provider_identity)
            self.assertEqual(original_identity, revised.work_identity)
            self.assertEqual(
                "revise_presentation_group_kind:fixture-movie:tv_episodes",
                revised.revision_action,
            )


if __name__ == "__main__":
    unittest.main()
