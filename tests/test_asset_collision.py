import tempfile
import unittest
from pathlib import Path

from src.rename.plan_review import _resolve_asset_collisions
from src.rename.rename_plan import (
    AssetRole,
    PlanEntry,
    PresentationAssetPlanContract,
    build_default_version_descriptor,
)


class AssetCollisionPlanningTests(unittest.TestCase):
    def test_different_content_with_known_owner_uses_version_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work_root = root / "Movie (2026)"
            target = work_root / "Associated" / "Fonts.7z"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old-fonts")
            source = root / "new" / "Fonts.7z"
            source.parent.mkdir()
            source.write_bytes(b"different-fonts")
            descriptor = build_default_version_descriptor("movie-continuous", None, None)
            descriptor = descriptor.__class__(
                descriptor.version_ref,
                descriptor.edition,
                "2160p-sdr",
                descriptor.resolution,
                descriptor.dynamic_range,
                descriptor.key_audio,
                "2160p SDR",
            )
            content = PlanEntry(
                root / "movie.mkv", work_root / "Movie (2026) - 2160p SDR.mkv",
                media_kind="video",
                presentation_asset=PresentationAssetPlanContract(
                    "movie-continuous", AssetRole("content_version"), None, descriptor
                ),
            )
            asset = PlanEntry(
                source, target, media_kind="associated", role="font",
                presentation_asset=PresentationAssetPlanContract(
                    "movie-continuous", AssetRole("associated"), None, None,
                    descriptor.version_ref,
                ),
            )

            revised, unresolved = _resolve_asset_collisions((content, asset), work_root)

            self.assertEqual([], unresolved)
            self.assertEqual(
                work_root / "Associated" / "2160p SDR" / "Fonts.7z",
                revised[1].target_path,
            )
            self.assertIsNone(revised[1].collision_resolution)

    def test_different_content_without_owner_stays_blocked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work_root = root / "Movie (2026)"
            target = work_root / "Associated" / "Fonts.7z"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old-fonts")
            source = root / "new" / "Fonts.7z"
            source.parent.mkdir()
            source.write_bytes(b"different-fonts")
            asset = PlanEntry(
                source, target, media_kind="associated", role="font",
                presentation_asset=PresentationAssetPlanContract(
                    "movie-continuous", AssetRole("associated"), None, None, None
                ),
            )

            revised, unresolved = _resolve_asset_collisions((asset,), work_root)

            self.assertEqual(["Fonts.7z"], unresolved)
            self.assertEqual(target, revised[0].target_path)
            self.assertIsNone(revised[0].collision_resolution)

    def test_same_named_generic_asset_uses_package_scope_after_version_collision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work_root = root / "Movie (2026)"
            common_target = work_root / "Associated" / "Booklet.bin"
            version_target = (
                work_root / "Associated" / "2160p SDR" / "Booklet.bin"
            )
            common_target.parent.mkdir(parents=True)
            version_target.parent.mkdir(parents=True)
            common_target.write_bytes(b"first-package")
            version_target.write_bytes(b"second-package-same-version")
            source = root / "Release Group B [2160p SDR]" / "Booklet.bin"
            source.parent.mkdir()
            source.write_bytes(b"third-package-same-name")
            descriptor = build_default_version_descriptor(
                "movie-continuous", None, None
            )
            descriptor = descriptor.__class__(
                descriptor.version_ref,
                descriptor.edition,
                "2160p-sdr",
                descriptor.resolution,
                descriptor.dynamic_range,
                descriptor.key_audio,
                "2160p SDR",
            )
            content = PlanEntry(
                root / "movie.mkv", work_root / "Movie (2026) - 2160p SDR.mkv",
                media_kind="video",
                presentation_asset=PresentationAssetPlanContract(
                    "movie-continuous", AssetRole("content_version"), None, descriptor
                ),
            )
            asset = PlanEntry(
                source, common_target, media_kind="associated", role="booklet",
                presentation_asset=PresentationAssetPlanContract(
                    "movie-continuous", AssetRole("associated"), None, None,
                    descriptor.version_ref,
                ),
            )

            revised, unresolved = _resolve_asset_collisions(
                (content, asset),
                work_root,
                package_label=source.parent.name,
            )

            self.assertEqual([], unresolved)
            self.assertEqual(
                work_root / "Associated" / "2160p SDR"
                / "Release Group B [2160p SDR]" / "Booklet.bin",
                revised[1].target_path,
            )
            self.assertIsNone(revised[1].collision_resolution)


if __name__ == "__main__":
    unittest.main()
