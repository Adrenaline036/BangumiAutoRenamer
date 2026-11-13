import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.rename.plan_review import PlanReviewService, ReviewStatus, entry_id
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import (
    AssetRole,
    ExecutionPolicy,
    PlanEntry,
    PresentationAssetPlanContract,
    RenamePlan,
)
from tests.presentation_fixtures import with_movie_presentation
from tests.projection_test_support import save_plan_fixture, task_source_scope_fixture


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
            metadata={"target_root": str(root / "library"),
                "initial_suggested_target_root":str(target.parent),
                "canonical_work_root":str(target.parent)},
            execution_policy=ExecutionPolicy(target_root=root/'library',dry_run=True),
        ))
        store = PlanStore(root / "plans")
        plan = save_plan_fixture(store, plan)
        task_source_scope_fixture(store,plan)
        return PlanReviewService(store, root / "reviews",root/'work_catalog',
            task_root=root/'task',decision_state_root=root/'decision_state'), store, plan

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
            self.assertEqual('theatrical', descriptor.edition.value.key)
            self.assertEqual('1080p', descriptor.resolution.value)
            self.assertEqual('SDR', descriptor.dynamic_range.value)
            self.assertEqual('FLAC 2.0', descriptor.key_audio.value)
            from src.rename.movie_version_selection import validate_selection
            from src.rename.decision_state import DecisionStateStore
            self.assertEqual(validate_selection(revised),
                DecisionStateStore(service.decision_state_store.root).load(revised.task_id).movie_version_selection)
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
            changed = service.revise_presentation_asset(revised.task_id, store.fingerprint(revised),
                entry_id(revised.entries[0]), presentation_group='fixture-movie', role_kind='content_version',
                edition_key='extended', edition_label='加长版', resolution='2160p', dynamic_range='HDR',
                key_audio='AAC 2.0', preferred=True, reason='确认新显示标签与技术规格')
            self.assertEqual(descriptor.version_ref, changed.entries[0].presentation_asset.version_descriptor.version_ref)
            self.assertEqual(descriptor.version_ref, changed.presentation_groups[0].preferred_version_ref)
            self.assertNotEqual(store.fingerprint(revised), store.fingerprint(changed))

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

    def test_unsealed_suggested_root_cannot_redirect_media(self):
        from src.rename.projection_authority import validate_plan_projection_receipt
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); service,store,plan=self._service_and_plan(root)
            expected=plan.entries[0].target_path.parent
            changed=replace(plan,metadata={**plan.metadata,
                'initial_suggested_target_root':str(root/'library'/'Foreign Work')})
            validate_plan_projection_receipt(changed)
            store.save(changed)
            revised=service.upsert_presentation_group(plan.task_id,store.fingerprint(changed),
                group_id='tv',kind='tv_episodes',display_name='分集版',provider_namespace='bangumi',
                provider_entity_type='subject',provider_id='425211',provider_title='Example TV',
                provider_year=2026,reason='确认分集版')
            self.assertEqual(str(expected),revised.metadata['canonical_work_root'])
            with self.assertRaises(Exception):
                validate_plan_projection_receipt(replace(revised,metadata={**revised.metadata,
                    'canonical_work_root':str(root/'library'/'Foreign Work')}))

    def test_legacy_unsigned_work_root_requires_a_new_inventory_plan(self):
        from src.rename.plan_review import PlanReviewError
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); service,store,plan=self._service_and_plan(root)
            # Explicit legacy-shaped component projection: no current root claim.
            metadata=dict(plan.metadata); metadata.pop('canonical_work_root')
            legacy=save_plan_fixture(store,replace(plan,metadata=metadata))
            before=store.path_for(plan.task_id).read_bytes()
            with self.assertRaisesRegex(PlanReviewError,'绝对作品目录'):
                service.upsert_presentation_group(plan.task_id,store.fingerprint(legacy),
                    group_id='tv',kind='tv_episodes',display_name='分集版',provider_namespace='bangumi',
                    provider_entity_type='subject',provider_id='425211',provider_title='Example TV',
                    provider_year=2026,reason='确认分集版')
            self.assertEqual(before,store.path_for(plan.task_id).read_bytes())

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
            from src.rename.decision_state import DecisionStateStore
            states=DecisionStateStore(Path(temp_dir)/'decision_state')
            tv_key=revised.metadata['catalog_binding_snapshot']['key']
            movie=service.revise_presentation_group_kind(plan.task_id,store.fingerprint(revised),
                group_id='fixture-movie',kind='movie_continuous',reason='确认连续电影播放')
            self.assertEqual('work',movie.metadata['catalog_binding_snapshot']['backend'])
            self.assertNotEqual(tv_key,movie.metadata['catalog_binding_snapshot']['key'])


if __name__ == "__main__":
    unittest.main()
