import tempfile
import unittest
import os
import json
from dataclasses import replace
from pathlib import Path

from src.rename.rename_plan import (
    AssetRole, CollisionResolution, EditionValue, EvidenceValue, PlanEntry,
    PresentationAssetPlanContract, PresentationGroup, ProviderIdentity,
    RenamePlan, VersionDescriptor, WorkIdentity,
)
from src.rename.work_catalog import WorkCatalog, WorkCatalogError
from src.rename.plan_store import PlanStore
from tests.projection_test_support import save_plan_fixture


def trusted(plan: RenamePlan) -> RenamePlan:
    return save_plan_fixture(PlanStore(plan.source_root / "plans"), plan)


def provider(provider_id="997317"):
    return ProviderIdentity(
        "confirmed", "tmdb", "movie", provider_id, "Example", 2022,
        "manual", aliases=("Example Movie",),
    )


def movie_plan(root: Path, work_key: str, target_name="Example (2022) - 1080p.mkv"):
    source = root / "source.mkv"
    target_root = root / "AnimeMovies"
    target = target_root / "Example (2022)" / target_name
    descriptor = VersionDescriptor(
        "v-1", EvidenceValue("resolved", EditionValue("default")), "1080p",
        EvidenceValue("resolved", "1080p"), EvidenceValue.unknown(),
        EvidenceValue.unknown(), "1080p",
    )
    return RenamePlan(
        task_id="task-one", source_root=root,
        entries=(PlanEntry(
            source, target, media_kind="video", role="movie_content",
            presentation_asset=PresentationAssetPlanContract(
                "movie-continuous", AssetRole("content_version"), None, descriptor
            ),
        ),),
        metadata={
            "target_root": str(target_root), "proposed_work_key": work_key,
            "canonical_work_root": str(target.parent),
        },
        presentation_groups=(PresentationGroup(
            "movie-continuous", "movie_continuous", "电影连续版", None
        ),),
        work_identity=WorkIdentity(
            "confirmed", "animation", "Example", 2022, provider(), "manual"
        ),
    )


def tv_plan(root: Path, work_key: str, season: int, provider_id="42"):
    source = root / f"source-s{season}.mkv"
    target_root = root / "Anime"
    target = target_root / "Example" / f"Season {season:02d}" / f"Example S{season:02d}E01.mkv"
    return RenamePlan(
        task_id=f"task-season-{season}", source_root=root,
        entries=(PlanEntry(
            source, target, media_kind="video", role="main_episode",
            season_id=season, episode_id=1,
        ),),
        metadata={
            "target_root": str(target_root), "proposed_work_key": work_key,
            "canonical_work_root": str(target_root / "Example"),
            "season_number": season, "season_source": "explicit_user",
        },
        presentation_groups=(PresentationGroup(
            "tv-episodes", "tv_episodes", "TV 分集版", None
        ),),
        work_identity=WorkIdentity(
            "confirmed", "animation", "Example", 2020,
            ProviderIdentity("confirmed", "tmdb", "tv", provider_id, "Example", 2020, "manual"),
            "manual",
        ),
    )
class WorkCatalogTests(unittest.TestCase):
    def test_tv_seasons_share_one_canonical_series_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = "work-12345678-1234-1234-1234-123456789abc"
            catalog = WorkCatalog(root / "catalog")
            first = trusted(tv_plan(root, key, 1))
            catalog.register_completed(first, {"plan_fingerprint": "one"})
            second = trusted(tv_plan(root, key, 2))
            record = catalog.register_completed(
                second, {"plan_fingerprint": "two"}, expected_revision=1
            )
            match = catalog.find_exact(second.work_identity.primary_provider, group_kind="tv_episodes")
            self.assertEqual("tv_episodes", record["group_kind"])
            self.assertEqual([1, 2], [item["season_number"] for item in record["seasons"]])
            self.assertEqual(root / "Anime" / "Example", match.canonical_root)

    def test_v1_record_remains_readable_without_startup_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog_root = root / "catalog"
            catalog_root.mkdir()
            key = "work-12345678-1234-1234-1234-123456789abc"
            path = catalog_root / f"{key}.json"
            payload = {
                "schema_version": 1, "work_key": key, "revision": 1,
                "title": "Example", "year": 2022, "domain": "animation",
                "group_kind": "movie_continuous",
                "canonical_root": str(root / "Example (2022)"),
                "aliases": [], "providers": [provider().to_dict()],
                "versions": [], "assets": [{
                    "target": str(root / "Example (2022)" / "Associated" / "Fonts.7z"),
                    "source": str(root / "source" / "Fonts.7z"),
                    "media_kind": "associated", "role": "font", "task_id": "old",
                }],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = WorkCatalog(catalog_root).load(key)

            self.assertEqual(1, loaded["schema_version"])
            self.assertEqual(payload, json.loads(path.read_text("utf-8")))

    def test_reused_content_adds_association_without_promoting_shared_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = "work-12345678-1234-1234-1234-123456789abc"
            base = movie_plan(root, key)
            target = base.entries[0].target_path.parent / "Associated" / "Fonts.7z"
            first_source = root / "first-fonts.7z"
            second_source = root / "second-fonts.7z"
            asset_contract = PresentationAssetPlanContract(
                "movie-continuous", AssetRole("associated"), None, None, "v-1"
            )
            first = replace(base, entries=base.entries + (PlanEntry(
                first_source, target, media_kind="associated", role="font",
                presentation_asset=asset_contract,
            ),))
            catalog = WorkCatalog(root / "catalog")
            first = trusted(first)
            catalog.register_completed(first, {"plan_fingerprint": "one"})
            collision = CollisionResolution(
                "reuse_existing", 5, "a" * 64, 5, "a" * 64, 1, 2
            )
            second = replace(
                base,
                task_id="task-two",
                entries=(PlanEntry(
                    second_source, target, media_kind="associated", role="font",
                    presentation_asset=asset_contract,
                    collision_resolution=collision,
                ),),
            )

            second = trusted(second)
            record = catalog.register_completed(
                second, {"plan_fingerprint": "two"}, expected_revision=1
            )

            asset = next(item for item in record["assets"] if item["target"] == str(target))
            self.assertEqual(2, len(asset["associations"]))
            self.assertEqual("version", asset["scope"])
            self.assertEqual(f"sha256:5:{'a' * 64}", asset["content_key"])
    def test_register_then_exact_provider_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = WorkCatalog(root / "catalog")
            key = "work-12345678-1234-1234-1234-123456789abc"
            plan = trusted(movie_plan(root, key))
            record = catalog.register_completed(plan, {"plan_fingerprint": "fp"})
            match = catalog.find_exact(provider())
            self.assertEqual(1, record["revision"])
            self.assertEqual(key, match.work_key)
            self.assertEqual("1080p", match.versions[0]["display_label"])

    def test_revision_check_blocks_stale_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = WorkCatalog(root / "catalog")
            key = "work-12345678-1234-1234-1234-123456789abc"
            plan = trusted(movie_plan(root, key))
            catalog.register_completed(plan, {"plan_fingerprint": "one"})
            with self.assertRaises(WorkCatalogError):
                catalog.register_completed(
                    trusted(movie_plan(root, key, "Example (2022) - 2160p SDR.mkv")),
                    {"plan_fingerprint": "two"}, expected_revision=0,
                )

    def test_work_lock_blocks_concurrent_execution_for_same_movie(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = WorkCatalog(Path(tmp) / "catalog")
            key = "work-12345678-1234-1234-1234-123456789abc"
            with catalog.lock(key):
                with self.assertRaises(WorkCatalogError):
                    with catalog.lock(key):
                        pass

    def test_work_lock_recovers_legacy_residue(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = WorkCatalog(Path(tmp) / "catalog")
            key = "work-12345678-1234-1234-1234-123456789abc"
            lock_path = catalog.lock_root / f"{key}.lock"
            lock_path.write_text("legacy-token", encoding="ascii")
            with catalog.lock(key):
                self.assertTrue(lock_path.is_file())

    def test_relocation_updates_catalog_only_after_whole_directory_inode_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = WorkCatalog(root / "catalog")
            key = "work-12345678-1234-1234-1234-123456789abc"
            plan = trusted(movie_plan(root, key))
            source = plan.entries[0].source_path
            target = plan.entries[0].target_path
            source.write_bytes(b"movie")
            target.parent.mkdir(parents=True)
            os.link(source, target)
            catalog.register_completed(plan, {"plan_fingerprint": "fp"})
            new_parent = root / "ConfiguredAnimeMovies"
            new_parent.mkdir()
            new_root = new_parent / target.parent.name
            target.parent.rename(new_root)
            task_root = root / "tasks"
            task_root.mkdir()

            updated = catalog.relocate(
                key, new_root, allowed_root=new_parent, task_root=task_root
            )

            self.assertEqual(str(new_root), updated["canonical_root"])
            self.assertEqual(
                str(new_root / target.name), updated["versions"][0]["target"]
            )

    def test_archive_missing_removes_only_registration_and_keeps_recovery_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = WorkCatalog(root / "catalog")
            key = "work-12345678-1234-1234-1234-123456789abc"
            plan = trusted(movie_plan(root, key))
            catalog.register_completed(plan, {"plan_fingerprint": "fp"})
            task_root = root / "tasks"
            task_root.mkdir()

            archive_path = catalog.archive_missing(key, task_root=task_root)

            self.assertFalse((catalog.root / f"{key}.json").exists())
            self.assertTrue(archive_path.is_file())
            archived = json.loads(archive_path.read_text(encoding="utf-8"))
            self.assertEqual(key, archived["work_key"])
            self.assertEqual(str(plan.entries[0].target_path.parent), archived["canonical_root"])

    def test_archive_missing_refuses_while_registered_directory_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = WorkCatalog(root / "catalog")
            key = "work-12345678-1234-1234-1234-123456789abc"
            plan = trusted(movie_plan(root, key))
            plan.entries[0].target_path.parent.mkdir(parents=True)
            catalog.register_completed(plan, {"plan_fingerprint": "fp"})
            task_root = root / "tasks"
            task_root.mkdir()

            with self.assertRaisesRegex(WorkCatalogError, "原登记目录仍存在"):
                catalog.archive_missing(key, task_root=task_root)

            self.assertTrue((catalog.root / f"{key}.json").is_file())

    def test_archive_missing_refuses_active_task_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = WorkCatalog(root / "catalog")
            key = "work-12345678-1234-1234-1234-123456789abc"
            catalog.register_completed(
                trusted(movie_plan(root, key)), {"plan_fingerprint": "fp"}
            )
            task_root = root / "tasks"
            task_root.mkdir()
            (task_root / "active.json").write_text(json.dumps({
                "uuid": "active",
                "workflow_status": "approved",
                "catalog_work_key": key,
            }), encoding="utf-8")

            with self.assertRaisesRegex(WorkCatalogError, "活动任务"):
                catalog.archive_missing(key, task_root=task_root)

            self.assertTrue((catalog.root / f"{key}.json").is_file())


if __name__ == "__main__":
    unittest.main()
