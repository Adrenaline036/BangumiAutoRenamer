import json
import tempfile
import unittest
from pathlib import Path

from src.rename.subtitle_report import (
    InventoryEntry,
    InventoryKind,
    SubtitleReportError,
    build_subtitle_sample_report,
    canonical_report_bytes,
    inventory_series_names,
)


class SubtitleSampleReportTests(unittest.TestCase):
    def setUp(self):
        self.entries = (
            InventoryEntry("Disc1/Show 01.mkv"),
            InventoryEntry("Disc1/Show 01.SC.ass"),
            InventoryEntry("Disc1/Show 01.Mystery.ass"),
            InventoryEntry("Disc2/Show 01.mkv"),
            InventoryEntry("Disc2/Show 01.TC.ass"),
            InventoryEntry("poster.jpg"),
        )

    def test_report_is_deterministic_and_versioned(self):
        first = build_subtitle_sample_report(self.entries, scope_id="series-001")
        second = build_subtitle_sample_report(reversed(self.entries), scope_id="series-001")
        self.assertEqual(first, second)
        self.assertEqual(canonical_report_bytes(first, include_report_id=True), canonical_report_bytes(second, include_report_id=True))
        self.assertEqual(3, first["aggregates"]["subtitle_count"])
        self.assertEqual(2, first["aggregates"]["video_count"])

    def test_same_video_names_are_isolated_by_parent_directory(self):
        report = build_subtitle_sample_report(self.entries, scope_id="series-001")
        statuses = [record["association"]["status"] for record in report["records"]]
        self.assertEqual(["confirmed", "confirmed", "confirmed"], statuses)

    def test_summary_mode_omits_private_names_and_raw_labels(self):
        report = build_subtitle_sample_report(self.entries, scope_id="series-001", privacy_mode="summary")
        encoded = canonical_report_bytes(report, include_report_id=True).decode("utf-8")
        self.assertNotIn("Disc1", encoded)
        self.assertNotIn("Mystery", encoded)
        self.assertNotIn("relative_path", encoded)
        self.assertNotIn("raw_label", encoded)

    def test_unknown_candidates_are_pending_human_only(self):
        report = build_subtitle_sample_report(self.entries, scope_id="series-001")
        candidate = report["rule_candidates"][0]
        self.assertEqual("mystery", candidate["normalized_token"])
        self.assertEqual("pending_human", candidate["decision"])
        self.assertNotIn("proposed_mapping", candidate)

    def test_invalid_and_duplicate_paths_fail_closed(self):
        for value in ("../escape.ass", "C:/media/file.ass", "/root/file.ass", "bad\x00name.ass"):
            with self.subTest(value=value), self.assertRaises(SubtitleReportError):
                InventoryEntry(value)
        with self.assertRaises(SubtitleReportError):
            build_subtitle_sample_report(
                [InventoryEntry("A/SC.ass"), InventoryEntry("a/sc.ass")],
                scope_id="series-001",
            )

    def test_inventory_adapter_reads_names_only_and_refuses_links(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "Disc").mkdir()
            (root / "Disc" / "Show 01.mkv").write_bytes(b"video-not-read")
            (root / "Disc" / "Show 01.SC.ass").write_bytes(b"subtitle-not-read")
            entries = inventory_series_names(root)
            self.assertIn(InventoryEntry("Disc/Show 01.SC.ass"), entries)
            link = root / "linked.ass"
            try:
                link.symlink_to(root / "Disc" / "Show 01.SC.ass")
            except OSError:
                self.skipTest("symlink creation unavailable")
            with self.assertRaises(SubtitleReportError):
                inventory_series_names(root)

    def test_json_roundtrip_and_empty_inventory(self):
        report = build_subtitle_sample_report([], scope_id="series-001")
        restored = json.loads(canonical_report_bytes(report, include_report_id=True))
        self.assertEqual(report, restored)
        self.assertEqual(0, report["aggregates"]["entry_count"])


if __name__ == "__main__":
    unittest.main()
