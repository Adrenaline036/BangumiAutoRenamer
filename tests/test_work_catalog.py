import tempfile
import unittest
import json
from dataclasses import replace
from pathlib import Path

from src.rename.rename_plan import (
    AssetRole, EditionValue, EvidenceValue, PlanEntry,
    PresentationAssetPlanContract, PresentationGroup, ProviderIdentity,
    RenamePlan, VersionDescriptor, WorkIdentity,
)
from src.rename.work_catalog import WorkCatalog, WorkCatalogError
from src.rename.plan_store import PlanStore
from src.rename.trans import Trans
from src.rename.asset_collision import build_reuse_resolution
from tests.projection_test_support import save_plan_fixture


def trusted(plan: RenamePlan) -> RenamePlan:
    return save_plan_fixture(PlanStore(plan.source_root / "plans"), plan)


def complete_ledger(plan: RenamePlan, record_root: Path | None = None):
    """Component fixture: real Trans output, never a manufactured completion."""
    record_root = record_root or plan.source_root / "records"
    operations = [dict(source=str(entry.source_path), target=str(entry.target_path),
        action="reuse_existing" if entry.collision_resolution else "create",
        collision_resolution=entry.collision_resolution.to_dict() if entry.collision_resolution else None)
        for entry in plan.entries]
    error = Trans(plan.as_mapping(), plan.task_id, PlanStore.fingerprint(plan), mode="hardlink",
        dry_run=False, record_root=record_root, operations=operations).trans_file()
    if error:
        raise AssertionError(error)
    return json.loads((record_root / f"{plan.task_id}.ledger.json").read_text("utf-8"))


def remove_fixture_materialization(plan):
    for entry in plan.entries:
        entry.target_path.unlink()
    Path(plan.metadata["canonical_work_root"]).rmdir()


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
            catalog.register_completed(first, complete_ledger(first))
            second = trusted(tv_plan(root, key, 2))
            record = catalog.register_completed(
                second, complete_ledger(second), expected_revision=1
            )
            match = catalog.find_exact(second.work_identity.primary_provider, group_kind="tv_episodes")
            self.assertEqual("tv_episodes", record["group_kind"])
            self.assertEqual([1, 2], [item["season_number"] for item in record["seasons"]])
            self.assertEqual(root / "Anime" / "Example", match.canonical_root)


    def test_register_then_exact_provider_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = WorkCatalog(root / "catalog")
            key = "work-12345678-1234-1234-1234-123456789abc"
            plan = trusted(movie_plan(root, key))
            record = catalog.register_completed(plan, complete_ledger(plan))
            match = catalog.find_exact(provider())
            self.assertEqual(1, record["revision"])
            self.assertEqual(key, match.work_key)
            self.assertEqual("1080p", match.versions[0]["display_label"])

    def test_selected_slot_change_blocks_stale_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = WorkCatalog(root / "catalog")
            key = "work-12345678-1234-1234-1234-123456789abc"
            plan = trusted(movie_plan(root, key))
            catalog.register_completed(plan, complete_ledger(plan))
            raw_second = movie_plan(root, key, "Example (2022) - 1080p revised.mkv")
            second = trusted(replace(raw_second, task_id="stale-second", entries=(
                replace(raw_second.entries[0], source_path=root / "source-second.mkv"),)))
            ledger = complete_ledger(second)
            before = (catalog.root / f"{key}.json").read_bytes()
            with self.assertRaisesRegex(WorkCatalogError, "追加依赖"):
                catalog.register_completed(
                    second, ledger, expected_revision=0,
                )
            self.assertEqual(before, (catalog.root / f"{key}.json").read_bytes())

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
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text("legacy-token", encoding="ascii")
            with catalog.lock(key):
                self.assertTrue(lock_path.is_file())

    def test_relocation_updates_catalog_only_after_whole_directory_inode_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = WorkCatalog(root / "catalog")
            key = "work-12345678-1234-1234-1234-123456789abc"
            plan = trusted(movie_plan(root, key))
            target = plan.entries[0].target_path
            catalog.register_completed(plan, complete_ledger(plan))
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
            catalog.register_completed(plan, complete_ledger(plan))
            remove_fixture_materialization(plan)
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
            catalog.register_completed(plan, complete_ledger(plan))
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
            plan = trusted(movie_plan(root, key))
            catalog.register_completed(plan, complete_ledger(plan))
            remove_fixture_materialization(plan)
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
