import tempfile
import unittest
import json
import os
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from src.rename.inventory_plan import build_inventory_review_draft
from src.rename.movie_assets import inventory_movie_assets
from src.rename.plan_review import PlanReviewError, PlanReviewService, entry_id
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.execution_recovery import reconcile_catalog_update
from src.rename.plan_store import PlanStore
from src.rename.decision_state import DecisionStateStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import PlanStatus
from src.rename.target_routing import TargetRootSnapshot
from src.rename.subtitle_workflow import format_subtitle_target_fields
from src.rename.work_catalog import WorkCatalog, WorkCatalogError
from tests.projection_test_support import save_plan_fixture


def routing_snapshot_for(
    target_root: Path,
    *,
    domain: str,
    group_kind: str,
) -> dict[str, str]:
    """Return four distinct roots with exactly one route bound to target."""

    target_root = Path(target_root)
    fixture_root = target_root.parent / ".bar-routing-fixture"
    values = {
        "bangumi_path": fixture_root / "TV",
        "movie_path": fixture_root / "Movies",
        "anime_path": fixture_root / "Anime",
        "anime_movie_path": fixture_root / "AnimeMovies",
    }
    route_key = {
        ("general", "tv_episodes"): "bangumi_path",
        ("general", "movie_continuous"): "movie_path",
        ("animation", "tv_episodes"): "anime_path",
        ("animation", "movie_continuous"): "anime_movie_path",
    }[(domain, group_kind)]
    values[route_key] = target_root
    snapshot = TargetRootSnapshot.capture(values)
    if len(set(snapshot.to_dict().values())) != 4:
        raise AssertionError("routing fixture roots must remain distinct")
    return snapshot.to_dict()


class IsolatedSampleAcceptanceTests(unittest.TestCase):
    def _inventory_plan_with_identity(
        self, root: Path, files: tuple[str, ...], *, task_id: str = "smart-pack",
        season_id: int | None = None, is_movie: bool | None = None,
        file_payloads: dict[str, bytes] | None = None,
    ):
        source = root / "source"
        source.mkdir()
        for index, name in enumerate(files, start=1):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                (file_payloads or {}).get(
                    name,
                    f"file-{index}".encode("ascii"),
                )
            )
        allowed = root / "library"
        task = {
            "uuid": task_id,
            "path": str(source),
            "name": "Example Work",
            "is_anime": True,
            "is_movie": is_movie,
            "season_id": season_id,
            "movie_inventory": inventory_movie_assets(source).to_dict(),
            "draft_target_root": str(allowed),
            "draft_initial_target_root": str(allowed / "Movies" / "Example Work"),
            "provider_identity": {
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": "12345",
                "title": "Example Work",
                "year": 2026,
            },
            "proposed_work_key": (
                "work-12345678-1234-1234-1234-"
                + ("1" if task_id == "smart-pack" else "2") * 12
            ),
        }
        plan = build_inventory_review_draft(task, dry_run=False)
        store = PlanStore(root / "plans")
        plan = save_plan_fixture(store, plan)
        return source, allowed, plan, store, PlanReviewService(
            store, root / "reviews", root / "catalog"
        )

    def test_sequential_movie_task_reuses_catalog_directory_and_adds_variant(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, allowed, plan, store, service = self._inventory_plan_with_identity(
                root,
                ("Example Work [Ma10p_1080p].mkv",),
            )
            first = service.complete_inventory_draft(
                plan.task_id,
                store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="movie_continuous",
                target_root=allowed / "AnimeMovies",
                reason="用户确认电影正片",
                root_snapshot=routing_snapshot_for(
                    allowed / "AnimeMovies",
                    domain="animation", group_kind="movie_continuous",
                ),
            )
            catalog = WorkCatalog(root / "catalog")
            from tests.test_work_catalog import complete_ledger
            catalog.register_completed(first, complete_ledger(first, root / "records"))

            source2 = root / "source-4k"
            source2.mkdir()
            (source2 / "Example Work [Ma10p_2160p_SDR].mkv").write_bytes(b"4k")
            task2 = {
                "uuid": "second-pack",
                "path": str(source2),
                "name": "Example Work",
                "is_anime": True,
                "movie_inventory": inventory_movie_assets(source2).to_dict(),
                "draft_target_root": str(allowed),
                "draft_initial_target_root": str(allowed / "Example Work"),
                "provider_identity": {
                    "namespace": "bangumi", "entity_type": "subject",
                    "provider_id": "12345", "title": "Example Work", "year": 2026,
                },
                "proposed_work_key": "work-12345678-1234-1234-1234-222222222222",
            }
            draft2 = build_inventory_review_draft(task2, dry_run=False)
            draft2 = save_plan_fixture(store, draft2)
            second = service.complete_inventory_draft(
                draft2.task_id,
                store.fingerprint(draft2),
                group_id=draft2.presentation_groups[0].group_id,
                group_kind="movie_continuous",
                target_root=allowed / "AnimeMovies",
                reason="用户确认追加 4K SDR 版本",
                root_snapshot=routing_snapshot_for(
                    allowed / "AnimeMovies",
                    domain="animation", group_kind="movie_continuous",
                ),
            )

            self.assertEqual(
                first.metadata["canonical_work_root"],
                second.metadata["canonical_work_root"],
            )
            self.assertEqual(first.metadata["proposed_work_key"], second.metadata["catalog_work_key"])
            target = next(entry.target_path for entry in second.entries if entry.media_kind == "video")
            self.assertEqual("Example Work (2026) - 2160p SDR.mkv", target.name)
            self.assertEqual([], list(PlanVerifier().verify(second).issues))

    def test_unknown_tmdb_domain_can_explicitly_choose_anime_movie_library(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            primary = source / "Example Movie [Ma10p_1080p].mkv"
            primary.write_bytes(b"movie")
            initial = root / "placeholder"
            task = {
                "uuid": "unknown-domain-movie",
                "path": str(source),
                "name": "Example Movie",
                "is_anime": None,
                "movie_inventory": inventory_movie_assets(source).to_dict(),
                "draft_target_root": str(initial),
                "draft_initial_target_root": str(initial / "Example Movie"),
                "provider_identity": {
                    "namespace": "tmdb", "entity_type": "movie",
                    "provider_id": "997317", "title": "Example Movie",
                    "year": 2026,
                },
                "proposed_work_key": "work-12345678-1234-1234-1234-333333333333",
            }
            plan = build_inventory_review_draft(task, dry_run=False)
            store = PlanStore(root / "plans")
            plan = save_plan_fixture(store, plan)
            service = PlanReviewService(store, root / "reviews", root / "catalog")
            snapshot = TargetRootSnapshot.capture({
                "anime_path": root / "Anime",
                "anime_movie_path": root / "AnimeMovies",
                "bangumi_path": root / "TV",
                "movie_path": root / "Movies",
            })

            revised = service.complete_inventory_draft(
                plan.task_id,
                store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="movie_continuous",
                target_root=root / "AnimeMovies",
                reason="用户选择动画媒体库并确认电影正片",
                primary_video_source=primary,
                root_snapshot=snapshot.to_dict(),
                work_domain="animation",
            )

            self.assertEqual("animation", revised.work_identity.domain)
            self.assertEqual(
                "manual_library_selection", revised.work_identity.domain_source
            )
            self.assertEqual(root / "AnimeMovies", revised.execution_policy.target_root)
            self.assertEqual([], list(PlanVerifier().verify(revised).issues))

    def test_real_movie_execution_materializes_catalog_after_hardlink_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, allowed, plan, store, review = self._inventory_plan_with_identity(
                root, ("Example Work [Ma10p_1080p].mkv",)
            )
            revised = review.complete_inventory_draft(
                plan.task_id,
                store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="movie_continuous",
                target_root=allowed / "AnimeMovies",
                reason="用户确认真实硬链接电影正片",
                root_snapshot=routing_snapshot_for(
                    allowed / "AnimeMovies",
                    domain="animation", group_kind="movie_continuous",
                ),
            )
            fingerprint = store.fingerprint(revised)
            review.approve_entries(
                revised.task_id,
                fingerprint,
                tuple(entry_id(entry) for entry in revised.entries),
            )
            task_root = root / "tasks"
            task_root.mkdir()
            (task_root / f"{revised.task_id}.json").write_text(
                json.dumps({
                    "uuid": revised.task_id,
                    "workflow_status": "approved",
                    "path": str(source),
                }),
                encoding="utf-8",
            )
            result = PlanExecutionService(
                store,
                review,
                task_root=task_root,
                record_root=root / "records",
                catalog_root=root / "catalog",
                decision_state_root=review.decision_state_store.root,
                recovery_transaction_root=root / "recovery_transactions",
            ).execute(revised.task_id, fingerprint)

            target = revised.entries[0].target_path
            self.assertEqual("completed", result.ledger_status)
            self.assertTrue(target.is_file())
            self.assertEqual(source.joinpath("Example Work [Ma10p_1080p].mkv").stat().st_ino, target.stat().st_ino)
            match = WorkCatalog(root / "catalog").find_exact(
                revised.work_identity.primary_provider
            )
            self.assertEqual(target, Path(str(match.versions[0]["target"])))

    def test_catalog_write_failure_preserves_links_and_recovers_from_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, allowed, plan, store, review = self._inventory_plan_with_identity(
                root, ("Example Work [Ma10p_1080p].mkv",)
            )
            revised = review.complete_inventory_draft(
                plan.task_id,
                store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="movie_continuous",
                target_root=allowed / "AnimeMovies",
                reason="用户确认恢复测试电影正片",
                root_snapshot=routing_snapshot_for(
                    allowed / "AnimeMovies",
                    domain="animation", group_kind="movie_continuous",
                ),
            )
            fingerprint = store.fingerprint(revised)
            review.approve_entries(
                revised.task_id, fingerprint,
                tuple(entry_id(entry) for entry in revised.entries),
            )
            task_root = root / "tasks"
            task_root.mkdir()
            task_path = task_root / f"{revised.task_id}.json"
            task_path.write_text(json.dumps({
                "uuid": revised.task_id, "workflow_status": "approved",
                "path": str(source),
            }), encoding="utf-8")
            catalog_root = root / "catalog"
            service = PlanExecutionService(
                store, review, task_root=task_root,
                record_root=root / "records", catalog_root=catalog_root,
                decision_state_root=review.decision_state_store.root,
                recovery_transaction_root=root / "recovery_transactions",
            )
            with patch.object(
                service.work_catalog,
                "register_completed",
                side_effect=WorkCatalogError("synthetic catalog failure"),
            ):
                with self.assertRaises(PlanExecutionError):
                    service.execute(revised.task_id, fingerprint)

            failed = json.loads(task_path.read_text(encoding="utf-8"))
            self.assertEqual("catalog_update_pending", failed["execution_status"])
            self.assertTrue(revised.entries[0].target_path.is_file())
            record = reconcile_catalog_update(
                revised.task_id,
                task_root=task_root,
                record_root=root / "records",
                plan_store=store,
                work_catalog=WorkCatalog(catalog_root),
            )
            completed = json.loads(task_path.read_text(encoding="utf-8"))
            self.assertEqual("completed", completed["execution_status"])
            self.assertEqual(revised.metadata["proposed_work_key"], record["work_key"])

    def test_smart_completion_turns_numbered_tv_pack_into_approvable_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            names = tuple(
                item
                for episode in range(1, 5)
                for item in (
                    f"Example Work [{episode:02d}].mkv",
                    f"Example Work [{episode:02d}].mka",
                    f"Example Work [{episode:02d}].sc.ass",
                    f"Example Work [{episode:02d}].tc.ass",
                )
            )
            source, allowed, plan, store, service = self._inventory_plan_with_identity(
                root, names
            )

            revised = service.complete_inventory_draft(
                plan.task_id,
                store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="tv_episodes",
                target_root=allowed / "Anime",
                reason="用户确认按季和集播放",
                root_snapshot=routing_snapshot_for(
                    allowed / "Anime",
                    domain="animation", group_kind="tv_episodes",
                ),
            )

            result = PlanVerifier().verify(revised)
            self.assertEqual(PlanStatus.NEED_CONFIRM, result.status)
            self.assertEqual([], list(result.issues))
            self.assertEqual("tv-episodes", revised.presentation_groups[0].group_id)
            self.assertTrue(all(
                entry.presentation_asset is None
                or entry.presentation_asset.presentation_group == "tv-episodes"
                for entry in revised.entries
            ))
            videos = [entry for entry in revised.entries if entry.media_kind == "video"]
            self.assertEqual([1, 2, 3, 4], [entry.episode_id for entry in videos])
            self.assertTrue(all(
                entry.presentation_asset.asset_role.kind == "content_version"
                for entry in videos
            ))
            subtitles = [
                entry for entry in revised.entries if entry.media_kind == "subtitle"
            ]
            self.assertEqual(8, len(subtitles))
            self.assertTrue(all(entry.subtitle.association_state.value == "resolved" for entry in subtitles))
            self.assertTrue(all(entry.subtitle.parse_state.value == "resolved" for entry in subtitles))
            audio = [entry for entry in revised.entries if entry.source_path.suffix == ".mka"]
            self.assertEqual(4, len(audio))
            self.assertTrue(all(
                entry.presentation_asset.asset_role.kind == "associated"
                for entry in audio
            ))

    def test_tv_completion_classifies_extras_before_assigning_episode_numbers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, allowed, plan, store, service = self._inventory_plan_with_identity(
                root,
                (
                    "Example Work EP01.mkv",
                    "Example Work EP01.sc.ass",
                    "Example Work Promotion Clip 1.mkv",
                    "SPs/Example Work [NCOP].mkv",
                    "SPs/Example Work [Menu01].mkv",
                ),
                task_id="tv-main-and-extras",
            )

            revised = service.complete_inventory_draft(
                plan.task_id,
                store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="tv_episodes",
                target_root=allowed / "Anime",
                reason="用户确认按季和集播放",
                root_snapshot=routing_snapshot_for(
                    allowed / "Anime",
                    domain="animation", group_kind="tv_episodes",
                ),
            )

            videos = [entry for entry in revised.entries if entry.media_kind == "video"]
            content = [
                entry for entry in videos
                if entry.presentation_asset.asset_role.kind == "content_version"
            ]
            extras = [
                entry for entry in videos
                if entry.presentation_asset.asset_role.kind == "extra"
            ]
            self.assertEqual([1], [entry.episode_id for entry in content])
            self.assertEqual(3, len(extras))
            self.assertTrue(all(entry.target_path.parent.name == "Extras" for entry in extras))
            self.assertNotIn(
                "duplicate_target",
                {issue.code for issue in PlanVerifier().verify(revised).issues},
            )

    def test_full_tv_run_excludes_numbered_promotion_before_episode_inference(self):
        """Regression for a real season where EP01 and Promotion Clip 1 coexist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            files = tuple(
                f"Example Work EP{number:02d}.mkv" for number in range(1, 25)
            ) + (
                "Example Work Promotion Clip 1.mkv",
                "SPs/Example Work [NCOP].mkv",
                "SPs/Example Work [Menu01].mkv",
            )
            source, allowed, plan, store, service = self._inventory_plan_with_identity(
                root, files, task_id="tv-full-run-and-promotion"
            )
            revised = service.complete_inventory_draft(
                plan.task_id, store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="tv_episodes", target_root=allowed / "Anime",
                reason="完整季度按季和集播放",
                root_snapshot=routing_snapshot_for(
                    allowed / "Anime",
                    domain="animation", group_kind="tv_episodes",
                ),
            )
            videos = [entry for entry in revised.entries if entry.media_kind == "video"]
            content = [
                entry for entry in videos
                if entry.presentation_asset.asset_role.kind == "content_version"
            ]
            extras = [
                entry for entry in videos
                if entry.presentation_asset.asset_role.kind == "extra"
            ]
            self.assertEqual(list(range(1, 25)), sorted(entry.episode_id for entry in content))
            self.assertEqual(3, len(extras))
            self.assertTrue(all(entry.target_path.parent.name == "Extras" for entry in extras))
            self.assertNotIn(
                "duplicate_target",
                {issue.code for issue in PlanVerifier().verify(revised).issues},
            )

    def test_true_tv_episode_collision_is_persisted_for_inline_repair(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            files = tuple(
                f"Example Work EP{number:02d}.mkv" for number in range(1, 9)
            ) + ("Example Work EP01 alternate.mkv",)
            source, allowed, plan, store, service = self._inventory_plan_with_identity(
                root, files, task_id="tv-true-episode-collision"
            )
            revised = service.complete_inventory_draft(
                plan.task_id, store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="tv_episodes", target_root=allowed / "Anime",
                reason="保留冲突供人工处理",
                root_snapshot=routing_snapshot_for(
                    allowed / "Anime",
                    domain="animation", group_kind="tv_episodes",
                ),
            )
            issues = PlanVerifier().verify(revised).issues
            self.assertIn("episode_assignment_conflict", {issue.code for issue in issues})
            self.assertTrue(all(
                "ManualReview" in entry.target_path.parts
                for entry in revised.entries if entry.media_kind == "video"
            ))

    def test_tv_completion_preserves_selected_season_number(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, allowed, plan, store, service = self._inventory_plan_with_identity(
                root,
                ("Example Work [01].mkv", "Example Work [01].sc.ass"),
                task_id="tv-selected-season",
                season_id=4,
                is_movie=False,
            )

            revised = service.complete_inventory_draft(
                plan.task_id,
                store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="tv_episodes",
                target_root=allowed / "Anime",
                reason="用户确认第四季按季和集播放",
                root_snapshot=routing_snapshot_for(
                    allowed / "Anime",
                    domain="animation", group_kind="tv_episodes",
                ),
            )

            video = next(entry for entry in revised.entries if entry.media_kind == "video")
            self.assertEqual((4, 1), (video.season_id, video.episode_id))
            self.assertIn("Season 04", video.target_path.parts)
            self.assertIn("S04E01", video.target_path.name)

    def test_tv_completion_rehomes_unresolved_entries_and_refreshes_subtitle_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, allowed, plan, store, service = self._inventory_plan_with_identity(
                root,
                (
                    "Example Work EP01.mkv",
                    "Example Work EP01.sc.ass",
                    "Unclassified Feature.mkv",
                    "Unclassified Feature.unknown.ass",
                ),
                task_id="tv-current-root-and-rules",
            )

            revised = service.complete_inventory_draft(
                plan.task_id,
                store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="tv_episodes",
                target_root=allowed / "Anime",
                reason="用户确认按季和集播放",
                root_snapshot=routing_snapshot_for(
                    allowed / "Anime",
                    domain="animation", group_kind="tv_episodes",
                ),
            )
            result = PlanVerifier().verify(revised)
            issue_codes = {issue.code for issue in result.issues}

            self.assertTrue(all(
                entry.target_path.is_relative_to(allowed / "Anime")
                for entry in revised.entries
            ))
            self.assertNotIn("target_outside_root", issue_codes)
            self.assertNotIn("subtitle_association_rule_stale", issue_codes)
            self.assertNotIn("subtitle_label_rule_stale", issue_codes)
            unresolved = next(
                entry for entry in revised.entries
                if entry.source_path.name == "Unclassified Feature.unknown.ass"
            )
            self.assertTrue(unresolved.subtitle.association_rule_version)
            self.assertTrue(unresolved.subtitle.label_rule_version)

    def test_smart_completion_builds_jellyfin_cd_stack_for_numbered_movie_parts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, allowed, plan, store, service = self._inventory_plan_with_identity(
                root,
                tuple(f"Example Work [{part:02d}].mkv" for part in range(1, 4)),
                task_id="smart-movie-parts",
            )

            revised = service.complete_inventory_draft(
                plan.task_id,
                store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="movie_continuous",
                target_root=allowed / "AnimeMovie",
                reason="用户确认连续电影分段",
                root_snapshot=routing_snapshot_for(
                    allowed / "AnimeMovie",
                    domain="animation", group_kind="movie_continuous",
                ),
            )

            result = PlanVerifier().verify(revised)
            self.assertEqual(PlanStatus.NEED_CONFIRM, result.status)
            self.assertEqual([], list(result.issues))
            self.assertEqual(
                ["Example Work (2026) - 默认版本-cd1.mkv", "Example Work (2026) - 默认版本-cd2.mkv", "Example Work (2026) - 默认版本-cd3.mkv"],
                [entry.target_path.name for entry in revised.entries],
            )
            self.assertEqual(
                [1, 2, 3],
                [entry.presentation_asset.segment.index for entry in revised.entries],
            )

    def test_explicit_movie_primary_excludes_extras_and_resolves_main_subtitles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, allowed, plan, store, service = self._inventory_plan_with_identity(
                root,
                (
                    "Example Work.mkv",
                    "Example Work.sc.ass",
                    "Example Work.tc.ass",
                    "SPs/Example Work [NCOP].mkv",
                    "SPs/Example Work [NCOP].sc.ass",
                    "SPs/Example Work [Menu].mkv",
                ),
                task_id="movie-primary-with-extras",
            )

            revised = service.complete_inventory_draft(
                plan.task_id,
                store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="movie_continuous",
                target_root=allowed / "AnimeMovie",
                reason="用户明确选择电影正片；其余视频作为特典",
                root_snapshot=routing_snapshot_for(
                    allowed / "AnimeMovie",
                    domain="animation", group_kind="movie_continuous",
                ),
                primary_video_source=source / "Example Work.mkv",
            )

            result = PlanVerifier().verify(revised)
            self.assertEqual(PlanStatus.NEED_CONFIRM, result.status)
            self.assertEqual([], list(result.issues))
            videos = [entry for entry in revised.entries if entry.media_kind == "video"]
            content = [
                entry for entry in videos
                if entry.presentation_asset.asset_role.kind == "content_version"
            ]
            extras = [
                entry for entry in videos
                if entry.presentation_asset.asset_role.kind == "extra"
            ]
            self.assertEqual([source / "Example Work.mkv"], [entry.source_path for entry in content])
            self.assertEqual(2, len(extras))
            self.assertTrue(all(entry.target_path.parent.name == "Extras" for entry in extras))
            subtitles = [
                entry for entry in revised.entries
                if entry.media_kind == "subtitle" and entry.role != "extra_subtitle"
            ]
            self.assertTrue(all(
                entry.subtitle.associated_video_source_path == source / "Example Work.mkv"
                for entry in subtitles
            ))
            extra_subtitle = next(
                entry for entry in revised.entries
                if entry.source_path.name == "Example Work [NCOP].sc.ass"
            )
            self.assertEqual("subtitle", extra_subtitle.media_kind)
            self.assertEqual("extra_subtitle", extra_subtitle.role)
            self.assertEqual("Extras", extra_subtitle.target_path.parent.name)
            self.assertIsNotNone(extra_subtitle.subtitle)
            self.assertEqual(
                source / "SPs" / "Example Work [NCOP].mkv",
                extra_subtitle.subtitle.associated_video_source_path,
            )
            self.assertNotIn("target_outside_root", {issue.code for issue in result.issues})

    def test_one_package_can_confirm_two_movie_primary_versions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, allowed, plan, store, service = self._inventory_plan_with_identity(
                root,
                (
                    "Example Work [Ma10p_2160p_DoVi_P8.1].mkv",
                    "Example Work [Ma10p_2160p_DoVi_P8.1].sc.ass",
                    "Example Work [Ma10p_2160p_SDR].mkv",
                    "Example Work [Ma10p_2160p_SDR].sc.ass",
                ),
                task_id="movie-two-primary-versions",
            )
            primaries = (
                source / "Example Work [Ma10p_2160p_DoVi_P8.1].mkv",
                source / "Example Work [Ma10p_2160p_SDR].mkv",
            )

            revised = service.complete_inventory_draft(
                plan.task_id,
                store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="movie_continuous",
                target_root=allowed / "AnimeMovie",
                reason="用户一次确认两个电影技术版本",
                root_snapshot=routing_snapshot_for(
                    allowed / "AnimeMovie",
                    domain="animation", group_kind="movie_continuous",
                ),
                primary_video_sources=primaries,
            )

            content = [
                entry for entry in revised.entries
                if entry.presentation_asset is not None
                and entry.presentation_asset.asset_role.kind == "content_version"
            ]
            self.assertEqual(2, len(content))
            self.assertEqual(
                {"2160p Dolby Vision P8.1", "2160p SDR"},
                {entry.presentation_asset.version_descriptor.display_label for entry in content},
            )
            self.assertEqual(2, len({entry.target_path for entry in content}))
            subtitles = [entry for entry in revised.entries if entry.media_kind == "subtitle"]
            self.assertEqual(set(primaries), {
                entry.subtitle.associated_video_source_path for entry in subtitles
            })
            by_source = {entry.source_path: entry for entry in revised.entries}
            for subtitle in subtitles:
                contract = subtitle.subtitle
                self.assertIsNotNone(contract)
                video = by_source[contract.associated_video_source_path]
                self.assertEqual(
                    format_subtitle_target_fields(
                        target_video=video.target_path,
                        source_subtitle=subtitle.source_path,
                        primary_language=contract.primary_language or "",
                        script=contract.script,
                        track_title_tokens=contract.track_title_tokens,
                        flags=contract.flags,
                    ),
                    subtitle.target_path,
                )
            self.assertEqual(2, len({entry.target_path for entry in subtitles}))
            self.assertEqual([], list(PlanVerifier().verify(revised).issues))




    def test_smart_movie_completion_refuses_to_guess_unlabeled_multiple_videos(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, allowed, plan, store, service = self._inventory_plan_with_identity(
                root,
                ("Feature Alpha.mkv", "Feature Beta.mkv"),
                task_id="ambiguous-movie-parts",
            )

            with self.assertRaisesRegex(PlanReviewError, "不能唯一证明完整分段"):
                service.complete_inventory_draft(
                    plan.task_id,
                    store.fingerprint(plan),
                    group_id=plan.presentation_groups[0].group_id,
                    group_kind="movie_continuous",
                    target_root=allowed / "AnimeMovie",
                    reason="用户确认连续电影",
                    root_snapshot=routing_snapshot_for(
                        allowed / "AnimeMovie",
                        domain="animation", group_kind="movie_continuous",
                    ),
                )
            self.assertEqual(plan, store.load(plan.task_id))

    def test_explicit_segment_confirmation_uses_the_confirmed_video_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, allowed, plan, store, service = self._inventory_plan_with_identity(
                root,
                ("Feature Alpha.mkv", "Feature Beta.mkv"),
                task_id="confirmed-movie-parts",
            )
            videos = tuple(
                entry.source_path
                for entry in plan.entries
                if entry.media_kind == "video"
            )

            revised = service.complete_inventory_draft(
                plan.task_id,
                store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="movie_continuous",
                target_root=allowed / "AnimeMovie",
                reason="用户勾选连续分段并确认当前顺序",
                root_snapshot=routing_snapshot_for(
                    allowed / "AnimeMovie",
                    domain="animation", group_kind="movie_continuous",
                ),
                confirmed_segment_sources=tuple(reversed(videos)),
            )

            targets = {
                entry.source_path.name: entry.target_path.name
                for entry in revised.entries
                if entry.media_kind == "video"
            }
            self.assertIn("-cd1", targets[videos[1].name])
            self.assertIn("-cd2", targets[videos[0].name])

    def _mixed_task(
        self,
        source: Path,
        allowed_root: Path,
        initial_root: Path,
        *,
        task_id: str,
        title: str,
        movie_id: str,
        tv_id: str,
    ) -> dict:
        return {
            "uuid": task_id,
            "path": str(source),
            "name": title,
            "is_anime": True,
            "season_number": 1,  # Current explicit user selection, not a legacy season_id/path guess.
            "movie_inventory": inventory_movie_assets(source).to_dict(),
            "draft_target_root": str(allowed_root),
            "draft_initial_target_root": str(initial_root),
            "provider_candidates": {
                "movie_continuous": {
                    "namespace": "tmdb",
                    "entity_type": "movie",
                    "provider_id": movie_id,
                    "title": f"{title} 电影剪辑版",
                    "year": 2023,
                },
                "tv_episodes": {
                    "namespace": "bangumi",
                    "entity_type": "subject",
                    "provider_id": tv_id,
                    "title": f"{title} TV 分集版",
                    "year": 2023,
                },
            },
        }

    def _confirm_mixed_fixture(
        self,
        root: Path,
        *,
        task_id: str,
        title: str,
        movie_id: str,
        tv_id: str,
        dry_run: bool = True,
    ):
        source = root / "source"
        source.mkdir()
        movie_source = source / f"{title} Movie Cut.mkv"
        movie_source.write_bytes(b"movie-cut")
        episode_sources = []
        for episode in range(1, 5):
            item = source / f"{title} TV {episode:02d}.mkv"
            item.write_bytes(f"episode-{episode}".encode("ascii"))
            episode_sources.append(item)

        allowed_root = root / "library"
        initial_root = allowed_root / title
        plan = build_inventory_review_draft(
            self._mixed_task(
                source,
                allowed_root,
                initial_root,
                task_id=task_id,
                title=title,
                movie_id=movie_id,
                tv_id=tv_id,
            ),
            dry_run=dry_run,
        )
        store = PlanStore(root / "plans")
        plan = save_plan_fixture(store, plan)
        service = PlanReviewService(store, root / "reviews", root / "catalog",
            decision_state_root=root / "decision_state", task_root=root / "task",
            provider_link_catalog_root=root / "provider_links")

        for group_id, kind, namespace, entity_type, provider_id, suffix in (
            ("movie-continuous", "movie_continuous", "tmdb", "movie", movie_id, "电影剪辑版"),
            ("tv-episodes", "tv_episodes", "bangumi", "subject", tv_id, "TV 分集版"),
        ):
            plan = service.upsert_presentation_group(
                task_id,
                store.fingerprint(plan),
                group_id=group_id,
                kind=kind,
                display_name=suffix,
                provider_namespace=namespace,
                provider_entity_type=entity_type,
                provider_id=provider_id,
                provider_title=f"{title} {suffix}",
                provider_year=2023,
                reason="合成验收确认 provider 身份",
            )

        movie_entry = next(entry for entry in plan.entries if entry.source_path == movie_source)
        movie_target = initial_root / f"{title}.mkv"
        plan = service.override_target(
            task_id,
            store.fingerprint(plan),
            entry_id(movie_entry),
            movie_target,
            "合成验收确认电影目标目录",
        )
        plan = service.revise_presentation_asset(
            task_id,
            store.fingerprint(plan),
            entry_id(movie_entry),
            presentation_group="movie-continuous",
            role_kind="content_version",
            edition_key="movie-cut",
            edition_label="电影剪辑版",
            resolution="1080p",
            dynamic_range="SDR",
            key_audio="FLAC 2.0",
            preferred=True,
            reason="合成验收确认电影剪辑版",
        )

        for episode, source_path in enumerate(episode_sources, start=1):
            current = next(entry for entry in plan.entries if entry.source_path == source_path)
            target = initial_root / "TV" / "Season 01" / f"S01E{episode:02d}.mkv"
            plan = service.override_target(
                task_id,
                store.fingerprint(plan),
                entry_id(current),
                target,
                "合成验收确认 TV 集号",
            )
            plan = service.revise_presentation_asset(
                task_id,
                store.fingerprint(plan),
                entry_id(current),
                presentation_group="tv-episodes",
                role_kind="content_version",
                reason="合成验收确认 TV 分集版",
            )
        return plan

    def test_kaguya_and_uma_mixed_presentations_reach_reviewable_v4_plans(self):
        fixtures = (
            ("kaguya", "辉夜大小姐 First Kiss", "997317", "425211"),
            ("uma", "赛马娘 ROAD TO THE TOP", "112157", "467146"),
        )
        for task_id, title, movie_id, tv_id in fixtures:
            with self.subTest(title=title), tempfile.TemporaryDirectory() as temp_dir:
                plan = self._confirm_mixed_fixture(
                    Path(temp_dir),
                    task_id=task_id,
                    title=title,
                    movie_id=movie_id,
                    tv_id=tv_id,
                )
                result = PlanVerifier().verify(plan)
                self.assertEqual(PlanStatus.INVALID, result.status, result.issues)
                self.assertEqual(['mixed_presentation_requires_partition'], [i.code for i in result.issues])
                self.assertEqual(
                    {"movie-continuous", "tv-episodes"},
                    {entry.presentation_asset.presentation_group for entry in plan.entries},
                )
                movie_versions = {
                    entry.presentation_asset.version_descriptor.version_ref
                    for entry in plan.entries
                    if entry.presentation_asset.presentation_group == "movie-continuous"
                }
                self.assertEqual(1, len(movie_versions))

    def test_two_editions_of_one_movie_keep_distinct_refs_and_one_preference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            # Some release packs store alternate cuts in folders which look
            # like seasons.  Physical folder names are evidence, not TV season
            # identity; both files still belong to one movie presentation.
            season_one = source / "Season 01"
            season_two = source / "Season 02"
            season_one.mkdir()
            season_two.mkdir()
            theatrical = season_one / "Feature Theatrical.mkv"
            directors = season_two / "Feature Directors Cut.mkv"
            theatrical.write_bytes(b"theatrical")
            directors.write_bytes(b"directors")
            allowed_root = root / "library"
            initial_root = allowed_root / "Example (2026)"
            task = {
                "uuid": "multi-edition-movie",
                "path": str(source),
                "name": "Example",
                "is_anime": True,
                "movie_inventory": inventory_movie_assets(source).to_dict(),
                "draft_target_root": str(allowed_root),
                "draft_initial_target_root": str(initial_root),
                "provider_identity": {
                    "namespace": "tmdb",
                    "entity_type": "movie",
                    "provider_id": "100",
                    "title": "Example",
                    "year": 2026,
                },
            }
            plan = build_inventory_review_draft(task, dry_run=True)
            store = PlanStore(root / "plans")
            plan = save_plan_fixture(store, plan)
            service = PlanReviewService(store, root / "reviews")

            for source_path, edition_key, label, preferred in (
                (theatrical, "theatrical", "剧场版", True),
                (directors, "directors-cut", "导演剪辑版", False),
            ):
                current = next(entry for entry in plan.entries if entry.source_path == source_path)
                plan = service.override_target(
                    plan.task_id,
                    store.fingerprint(plan),
                    entry_id(current),
                    initial_root / f"Example (2026) - {label}.mkv",
                    "合成验收确认电影版本目标",
                )
                plan = service.revise_presentation_asset(
                    plan.task_id,
                    store.fingerprint(plan),
                    entry_id(current),
                    presentation_group="movie-continuous",
                    role_kind="content_version",
                    edition_key=edition_key,
                    edition_label=label,
                    resolution="1080p",
                    dynamic_range="SDR",
                    key_audio="FLAC 2.0",
                    preferred=preferred,
                    reason="合成验收确认同电影不同剪辑版本",
                )

            result = PlanVerifier().verify(plan)
            descriptors = [
                entry.presentation_asset.version_descriptor for entry in plan.entries
            ]
            self.assertEqual(PlanStatus.NEED_CONFIRM, result.status)
            self.assertEqual([], list(result.issues))
            self.assertEqual(2, len({item.version_ref for item in descriptors}))
            self.assertEqual(
                {"Season 01/Feature Theatrical.mkv", "Season 02/Feature Directors Cut.mkv"},
                {
                    item.relative_path
                    for item in inventory_movie_assets(source).entries
                    if item.entry_type == "video"
                },
            )
            self.assertIn(
                plan.presentation_groups[0].preferred_version_ref,
                {item.version_ref for item in descriptors},
            )


if __name__ == "__main__":
    unittest.main()
