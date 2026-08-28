import unittest
from pathlib import Path

from src.rename.subtitle_labels import DimensionResolution
from src.rename.subtitle_workflow import (
    VideoPathMapping,
    build_subtitle_decisions,
    format_subtitle_target_fields,
)


class SubtitleWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.mapping = VideoPathMapping(
            Path("Source/[AIC] Show - 01.mkv"),
            Path("Library/Show/Season 01/Show S01E01.mkv"),
        )

    def test_associates_by_source_stem_but_formats_with_target_stem(self):
        decision = build_subtitle_decisions(
            [Path("Source/[AIC] Show - 01.SC.ass")],
            [self.mapping],
        )[0]

        self.assertEqual(self.mapping.source_path, decision.association.associated_video)
        self.assertEqual(self.mapping.target_path, decision.associated_video_target)
        self.assertEqual(
            Path("Library/Show/Season 01/Show S01E01.zh-Hans.ass"),
            decision.target_preview,
        )
        self.assertFalse(decision.blocks_execution)

    def test_passing_only_target_video_cannot_falsely_associate_source_name(self):
        decision = build_subtitle_decisions(
            [Path("Source/[AIC] Show - 01.SC.ass")],
            [VideoPathMapping(self.mapping.target_path, self.mapping.target_path)],
        )[0]
        self.assertTrue(decision.blocks_execution)
        self.assertIn("TARGET_ASSOCIATION_UNRESOLVED", decision.reason_codes)

    def test_unresolved_label_has_no_executable_target(self):
        decision = build_subtitle_decisions(
            [Path("Source/[AIC] Show - 01.[Group].SC.ass")],
            [self.mapping],
        )[0]
        self.assertIsNone(decision.target_preview)
        self.assertEqual(DimensionResolution.UNRESOLVED, decision.target_resolution)
        self.assertTrue(decision.blocks_execution)

    def test_conflicting_source_to_target_mapping_fails_closed(self):
        other = VideoPathMapping(
            self.mapping.source_path,
            Path("Library/Other/Show S01E01.mkv"),
        )
        decision = build_subtitle_decisions(
            [Path("Source/[AIC] Show - 01.SC.ass")],
            [self.mapping, other],
        )[0]
        self.assertEqual(DimensionResolution.CONFLICT, decision.target_resolution)
        self.assertIn("TARGET_VIDEO_MAPPING_CONFLICT", decision.reason_codes)

    def test_existing_target_snapshot_blocks_without_touching_filesystem(self):
        expected = Path("Library/Show/Season 01/Show S01E01.zh-Hans.ass")
        decision = build_subtitle_decisions(
            [Path("Source/[AIC] Show - 01.SC.ass")],
            [self.mapping],
            existing_target_paths=[expected],
        )[0]
        self.assertEqual(expected, decision.target_preview)
        self.assertEqual(DimensionResolution.CONFLICT, decision.target_resolution)
        self.assertIn("TARGET_ALREADY_EXISTS", decision.reason_codes)

    def test_casefold_equivalent_targets_collide_in_batch(self):
        decisions = build_subtitle_decisions(
            [
                Path("Source/[AIC] Show - 01.SC.ass"),
                Path("source/[aic] show - 01.chs.ass"),
            ],
            [self.mapping],
        )
        self.assertEqual(2, len(decisions))
        for decision in decisions:
            self.assertIn("TARGET_BATCH_COLLISION", decision.reason_codes)
            self.assertTrue(decision.blocks_execution)

    def test_two_default_tracks_for_one_video_are_both_blocked(self):
        decisions = build_subtitle_decisions(
            [
                Path("Source/[AIC] Show - 01.SC.default.ass"),
                Path("Source/[AIC] Show - 01.TC.default.ass"),
            ],
            [self.mapping],
        )
        for decision in decisions:
            self.assertIn("TARGET_MULTIPLE_DEFAULT_TRACKS", decision.reason_codes)
            self.assertEqual(DimensionResolution.CONFLICT, decision.target_resolution)

    def test_bilingual_title_and_flags_have_deterministic_order(self):
        decision = build_subtitle_decisions(
            [Path("Source/[AIC] Show - 01.JPSC.sdh.forced.default.ass")],
            [self.mapping],
        )[0]
        self.assertEqual(
            Path("Library/Show/Season 01/Show S01E01.zh-Hans.日简双语.default.forced.sdh.ass"),
            decision.target_preview,
        )

    def test_output_order_and_serialization_are_deterministic(self):
        inputs = [
            Path("Source/[AIC] Show - 01.TC.ass"),
            Path("Source/[AIC] Show - 01.SC.ass"),
        ]
        first = build_subtitle_decisions(inputs, [self.mapping])
        second = build_subtitle_decisions(reversed(inputs), [self.mapping])
        self.assertEqual(
            [item.to_dict() for item in first],
            [item.to_dict() for item in second],
        )

    def test_formatter_rejects_unsupported_script_combinations(self):
        with self.assertRaises(ValueError):
            format_subtitle_target_fields(
                target_video=self.mapping.target_path,
                source_subtitle=Path("Source/episode.ass"),
                primary_language="ja",
                script="Hans",
            )
        with self.assertRaises(ValueError):
            format_subtitle_target_fields(
                target_video=self.mapping.target_path,
                source_subtitle=Path("Source/episode.ass"),
                primary_language="zh",
                script="Latn",
            )


if __name__ == "__main__":
    unittest.main()
