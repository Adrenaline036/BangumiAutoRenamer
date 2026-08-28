import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.rename.movie_assets import (
    DISC_STRUCTURE_REQUIRES_MANUAL_WORKFLOW,
    MovieAssetInventoryError,
    compare_movie_asset_inventory,
    inventory_movie_assets,
)
from src.rename.utils import is_font_archive, is_ignored_asset_directory


class MovieAssetInventoryTests(unittest.TestCase):
    def test_font_archive_gate_requires_a_final_bracketed_fonts_token(self):
        accepted = (
            Path("[Fonts].7z"),
            Path("Example Work [Fonts].7Z"),
        )
        rejected = (
            Path("Fonts.7z"),
              Path("Example Work Fonts Archive.7z"),
              Path("Example Work [FontsBackup].7z"),
              Path("Example Work [Fonts]notes.7z"),
              Path("ExampleWork[Fonts].7z"),
              Path("Example Work [Fonts].zip"),
          )

        self.assertTrue(all(is_font_archive(path) for path in accepted))
        self.assertFalse(any(is_font_archive(path) for path in rejected))

    def test_auxiliary_directory_gate_is_exact_and_unicode_normalized(self):
        self.assertTrue(all(
            is_ignored_asset_directory(name)
            for name in ("CD", "CDs", "Scan", "Scans", "ＣＤｓ")
        ))
        self.assertFalse(any(
            is_ignored_asset_directory(name)
            for name in ("CD Art", "Scans Backup", "MyScans", "Disc")
        ))

    def test_inventory_skips_cd_and_scan_subtrees_but_keeps_sp_and_font_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "movie"
            root.mkdir()
            (root / "Feature.mkv").touch()
            (root / "Example [Fonts].7z").touch()
            (root / "CDs").mkdir()
            (root / "CDs" / "track.flac").touch()
            (root / "Scans").mkdir()
            (root / "Scans" / "[Fonts].7z").touch()
            (root / "SPs").mkdir()
            (root / "SPs" / "Trailer.mkv").touch()

            inventory = inventory_movie_assets(root)

        paths = {entry.relative_path for entry in inventory.entries}
        self.assertIn("Example [Fonts].7z", paths)
        self.assertIn("SPs/Trailer.mkv", paths)
        self.assertFalse(any(path.startswith("CDs") for path in paths))
        self.assertFalse(any(path.startswith("Scans") for path in paths))

    def test_inventory_is_relative_deterministic_and_metadata_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "movie"
            extras = root / "Extras"
            extras.mkdir(parents=True)
            (root / "Feature.mkv").write_bytes(b"feature")
            (root / "Feature.zh-Hans.ass").write_bytes(b"subtitle")
            (extras / "Interview.MP4").write_bytes(b"extra")
            (root / "poster.jpg").write_bytes(b"image")

            with patch("builtins.open", side_effect=AssertionError("content read")):
                first = inventory_movie_assets(root)
                second = inventory_movie_assets(root)

        self.assertEqual(first, second)
        self.assertEqual(2, first.video_count)
        self.assertIn(
            "MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION",
            first.reason_codes,
        )
        self.assertFalse(hasattr(first, "mapping"))
        self.assertEqual(64, len(first.fingerprint))
        by_path = {entry.relative_path: entry for entry in first.entries}
        self.assertEqual("video", by_path["Feature.mkv"].entry_type)
        self.assertEqual("subtitle", by_path["Feature.zh-Hans.ass"].entry_type)
        self.assertEqual("video", by_path["Extras/Interview.MP4"].entry_type)
        self.assertEqual("associated", by_path["poster.jpg"].entry_type)
        for entry in first.entries:
            self.assertFalse(Path(entry.relative_path).is_absolute())
            self.assertNotIn(str(root), entry.relative_path)

    def test_compare_reports_type_size_and_mtime_drift(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "movie"
            root.mkdir()
            feature = root / "Feature.mkv"
            feature.write_bytes(b"a")
            expected = inventory_movie_assets(root)

            feature.write_bytes(b"changed")
            stat_result = feature.stat()
            os.utime(
                feature,
                ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000),
            )
            comparison = compare_movie_asset_inventory(root, expected)

        self.assertFalse(comparison.unchanged)
        self.assertEqual(1, len(comparison.changed))
        self.assertEqual("Feature.mkv", comparison.changed[0].relative_path)
        self.assertEqual(("size_bytes", "mtime_ns"), comparison.changed[0].fields)

    def test_added_and_removed_paths_are_reported_without_absolute_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "movie"
            root.mkdir()
            old = root / "old.mkv"
            old.touch()
            expected = inventory_movie_assets(root)
            old.unlink()
            (root / "new.mkv").touch()

            comparison = compare_movie_asset_inventory(root, expected)

        self.assertEqual(("new.mkv",), comparison.added_paths)
        self.assertEqual(("old.mkv",), comparison.removed_paths)

    def test_file_replaced_by_directory_reports_type_drift(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "movie"
            root.mkdir()
            asset = root / "asset"
            asset.touch()
            expected = inventory_movie_assets(root)
            asset.unlink()
            asset.mkdir()

            comparison = compare_movie_asset_inventory(root, expected)

        self.assertFalse(comparison.unchanged)
        self.assertEqual("asset", comparison.changed[0].relative_path)
        self.assertIn("entry_type", comparison.changed[0].fields)

    def test_entry_limit_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "movie"
            root.mkdir()
            (root / "one.mkv").touch()
            (root / "two.ass").touch()

            with self.assertRaises(MovieAssetInventoryError) as caught:
                inventory_movie_assets(root, max_entries=1)

        self.assertEqual("INVENTORY_MAX_ENTRIES_EXCEEDED", caught.exception.code)

    def test_symlink_is_rejected_instead_of_followed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "movie"
            outside = Path(temp_dir) / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "secret.mkv").touch()
            link = root / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            with self.assertRaises(MovieAssetInventoryError) as caught:
                inventory_movie_assets(root)

        self.assertEqual("UNTRUSTED_FILESYSTEM_LINK", caught.exception.code)
        self.assertEqual("linked", caught.exception.relative_path)

    def test_disc_structure_is_summarized_and_requires_manual_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "movie"
            stream = root / "BDMV" / "STREAM"
            stream.mkdir(parents=True)
            (stream / "00001.m2ts").touch()

            inventory = inventory_movie_assets(root)

        self.assertIn(DISC_STRUCTURE_REQUIRES_MANUAL_WORKFLOW, inventory.reason_codes)
        self.assertEqual(
            ["BDMV"],
            [
                entry.relative_path
                for entry in inventory.entries
                if entry.entry_type == "disc_structure"
            ],
        )
        self.assertNotIn("BDMV/STREAM/00001.m2ts", {
            entry.relative_path for entry in inventory.entries
        })


if __name__ == "__main__":
    unittest.main()
