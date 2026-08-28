import tempfile
import unittest
from pathlib import Path

from src.rename.inventory_plan import InventoryDraftError, build_inventory_review_draft
from src.rename.movie_assets import inventory_movie_assets
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import PlanStatus


class InventoryPlanTests(unittest.TestCase):
    def _task(self, source: Path, target: Path):
        inventory = inventory_movie_assets(source)
        return {
            "uuid": "inventory-draft",
            "path": str(source),
            "name": "Example",
            "is_anime": True,
            "movie_inventory": inventory.to_dict(),
            "draft_target_root": str(target),
            "provider_identity": {
                "namespace": "tmdb",
                "entity_type": "movie",
                "provider_id": "100",
                "title": "Example",
                "year": 2026,
            },
        }

    def _mixed_task(self, source: Path, allowed_root: Path, initial_root: Path):
        task = self._task(source, allowed_root)
        task.pop("provider_identity")
        task["draft_initial_target_root"] = str(initial_root)
        task["provider_candidates"] = {
            "movie_continuous": {
                "namespace": "tmdb",
                "entity_type": "movie",
                "provider_id": "997317",
                "title": "Kaguya-sama: The First Kiss That Never Ends",
                "year": 2022,
            },
            "tv_episodes": {
                "namespace": "tmdb",
                "entity_type": "tv",
                "provider_id": "77721",
                "title": "Kaguya-sama: Love Is War",
                "year": 2019,
            },
        }
        return task

    def test_unchanged_inventory_becomes_blocked_review_draft(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            (source / "Feature.mkv").write_bytes(b"video-a")
            (source / "Feature-alt.mkv").write_bytes(b"video-b")
            (source / "Feature.SC.ass").write_text("synthetic", encoding="utf-8")

            plan = build_inventory_review_draft(
                self._task(source, root / "library" / "Example (2026)"),
                dry_run=True,
            )
            result = PlanVerifier().verify(plan)

            self.assertEqual(3, len(plan.entries))
            self.assertEqual(PlanStatus.INVALID, result.status)
            self.assertTrue(all(entry.requires_review for entry in plan.entries))
            self.assertEqual(
                {"unknown"},
                {
                    entry.presentation_asset.asset_role.kind
                    for entry in plan.entries if entry.media_kind != "subtitle"
                },
            )
            subtitle = next(entry for entry in plan.entries if entry.media_kind == "subtitle")
            self.assertEqual(2, len(subtitle.subtitle.association_candidates))

    def test_mixed_movie_tv_inventory_uses_stable_groups_and_marks_placeholder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            (source / "Movie Cut.mkv").write_bytes(b"movie")
            (source / "TV Part 01.mkv").write_bytes(b"episode")

            plan = build_inventory_review_draft(
                self._mixed_task(
                    source,
                    root / "library",
                    root / "library" / "Kaguya-sama (2022)",
                ),
                dry_run=True,
            )

            self.assertEqual(
                ["movie-continuous", "tv-episodes"],
                [group.group_id for group in plan.presentation_groups],
            )
            self.assertEqual("placeholder", plan.metadata["presentation_assignment_state"])
            self.assertTrue(all(
                entry.presentation_asset.presentation_group == "movie-continuous"
                for entry in plan.entries
            ))
            self.assertEqual(PlanStatus.INVALID, PlanVerifier().verify(plan).status)

    def test_initial_suggested_target_must_stay_inside_allowed_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            (source / "Feature.mkv").write_bytes(b"video")
            task = self._task(source, root / "allowed")
            task["draft_initial_target_root"] = str(root / "outside" / "Example")

            with self.assertRaisesRegex(InventoryDraftError, "越出允许目标根"):
                build_inventory_review_draft(task, dry_run=True)

    def test_target_root_fields_must_be_absolute(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            (source / "Feature.mkv").write_bytes(b"video")
            task = self._task(source, Path("relative-library"))

            with self.assertRaisesRegex(InventoryDraftError, "必须是绝对路径"):
                build_inventory_review_draft(task, dry_run=True)

    def test_inventory_drift_blocks_draft_creation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            video = source / "Feature.mkv"
            video.write_bytes(b"before")
            task = self._task(source, root / "library" / "Example (2026)")
            video.write_bytes(b"after-with-different-size")

            with self.assertRaisesRegex(InventoryDraftError, "漂移"):
                build_inventory_review_draft(task, dry_run=True)


if __name__ == "__main__":
    unittest.main()
