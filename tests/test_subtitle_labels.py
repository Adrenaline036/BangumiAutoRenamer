import unittest
from pathlib import Path

from src.rename.subtitle_labels import (
    AssociationStatus,
    DimensionResolution,
    LabelParseStatus,
    NormalizationKind,
    SUBTITLE_LABEL_RULE_VERSION,
    TokenRole,
    SUBTITLE_ASSOCIATION_RULE_VERSION,
    associate_subtitle_to_video,
    parse_subtitle_label,
)


class SubtitleAssociationTests(unittest.TestCase):
    def test_exact_dot_suffix_associates_and_preserves_raw_label(self):
        result = associate_subtitle_to_video(
            Path("EP01.SC.default.ass"),
            [Path("EP01.mkv")],
        )

        self.assertEqual(AssociationStatus.CONFIRMED, result.status)
        self.assertEqual(DimensionResolution.RESOLVED, result.resolution_state)
        self.assertEqual(Path("EP01.mkv"), result.associated_video)
        self.assertEqual("SC.default", result.raw_label)
        self.assertFalse(result.blocks_execution)

    def test_allowed_boundaries_are_recognized_without_changing_raw_text(self):
        cases = {
            "EP01_SC.ass": "SC",
            "EP01-SC.ass": "SC",
            "EP01 SC.ass": "SC",
            "EP01.[SC].ass": "[SC]",
            "EP01（SC）.ass": "（SC）",
            "EP01．ＳＣ．ass": "ＳＣ",
        }

        for subtitle, raw_label in cases.items():
            with self.subTest(subtitle=subtitle):
                result = associate_subtitle_to_video(
                    Path(subtitle),
                    [Path("EP01.mkv")],
                )
                self.assertEqual(AssociationStatus.CONFIRMED, result.status)
                self.assertEqual(raw_label, result.raw_label)

    def test_casefold_matching_is_explicit_and_cross_platform(self):
        result = associate_subtitle_to_video(
            Path("ep01.SC.ass"),
            [Path("EP01.mkv")],
        )

        self.assertEqual(AssociationStatus.CONFIRMED, result.status)
        self.assertIn("ASSOC_NFKC_CASEFOLD_MATCH", result.reason_codes)

    def test_substring_without_boundary_is_rejected(self):
        result = associate_subtitle_to_video(
            Path("EP0101.SC.ass"),
            [Path("EP01.mkv")],
        )

        self.assertEqual(AssociationStatus.NO_VIDEO, result.status)
        self.assertTrue(result.blocks_execution)

    def test_longest_unique_video_stem_wins(self):
        result = associate_subtitle_to_video(
            Path("EP01 NCOP.SC.ass"),
            [Path("EP01.mkv"), Path("EP01 NCOP.mkv")],
        )

        self.assertEqual(Path("EP01 NCOP.mkv"), result.associated_video)
        self.assertEqual("SC", result.raw_label)
        self.assertIn("ASSOC_LONGEST_VIDEO_STEM", result.reason_codes)

    def test_same_stem_multiple_video_formats_is_a_conflict(self):
        result = associate_subtitle_to_video(
            Path("EP01.SC.ass"),
            [Path("EP01.mkv"), Path("EP01.mp4")],
        )

        self.assertEqual(AssociationStatus.MULTIPLE_VIDEOS, result.status)
        self.assertEqual(DimensionResolution.CONFLICT, result.resolution_state)
        self.assertEqual(
            (Path("EP01.mkv"), Path("EP01.mp4")),
            result.candidate_videos,
        )
        self.assertIsNone(result.associated_video)
        self.assertTrue(result.blocks_execution)

    def test_normalization_equivalent_video_stems_remain_ambiguous(self):
        result = associate_subtitle_to_video(
            Path("ep01.SC.ass"),
            [Path("EP01.mkv"), Path("ｅｐ０１.mp4")],
        )

        self.assertEqual(AssociationStatus.MULTIPLE_VIDEOS, result.status)

    def test_common_episode_name_does_not_fuzzily_choose_a_video_version(self):
        result = associate_subtitle_to_video(
            Path("EP01.SC.ass"),
            [Path("EP01.1080p.mkv"), Path("EP01.2160p.mkv")],
        )

        self.assertEqual(AssociationStatus.NO_VIDEO, result.status)

    def test_multiple_subtitles_may_independently_share_one_video(self):
        video = Path("EP01.mkv")
        simplified = associate_subtitle_to_video(Path("EP01.SC.ass"), [video])
        traditional = associate_subtitle_to_video(Path("EP01.TC.ass"), [video])

        self.assertEqual(AssociationStatus.CONFIRMED, simplified.status)
        self.assertEqual(AssociationStatus.CONFIRMED, traditional.status)
        self.assertEqual(video, simplified.associated_video)
        self.assertEqual(video, traditional.associated_video)

    def test_duplicate_candidate_path_does_not_create_false_ambiguity(self):
        video = Path("EP01.mkv")
        result = associate_subtitle_to_video(
            Path("EP01.SC.ass"),
            [video, video],
        )

        self.assertEqual(AssociationStatus.CONFIRMED, result.status)

    def test_empty_label_is_reportable_but_association_is_confirmed(self):
        result = associate_subtitle_to_video(
            Path("EP01.ass"),
            [Path("EP01.mkv")],
        )

        self.assertEqual(AssociationStatus.CONFIRMED, result.status)
        self.assertEqual("", result.raw_label)
        self.assertIn("TAG_EMPTY", result.reason_codes)

    def test_unsupported_subtitle_extension_fails_closed(self):
        result = associate_subtitle_to_video(
            Path("EP01.SC.txt"),
            [Path("EP01.mkv")],
        )

        self.assertEqual(AssociationStatus.INVALID_SUBTITLE, result.status)
        self.assertEqual(DimensionResolution.UNRESOLVED, result.resolution_state)
        self.assertTrue(result.blocks_execution)

    def test_non_video_candidates_are_ignored(self):
        result = associate_subtitle_to_video(
            Path("EP01.SC.ass"),
            [Path("EP01.flac"), Path("EP01.jpg")],
        )

        self.assertEqual(AssociationStatus.NO_VIDEO, result.status)

    def test_result_contract_is_versioned_and_json_friendly(self):
        result = associate_subtitle_to_video(
            Path("EP01.SC.ass"),
            [Path("EP01.mkv")],
        )

        data = result.to_dict()
        self.assertEqual(1, data["schema_version"])
        self.assertEqual(SUBTITLE_ASSOCIATION_RULE_VERSION, data["rule_version"])
        self.assertEqual("confirmed", data["status"])
        self.assertEqual("resolved", data["resolution_state"])
        self.assertEqual("EP01.mkv", data["associated_video"])
        self.assertEqual(["EP01.mkv"], data["candidate_videos"])
        self.assertFalse(data["blocks_execution"])


class SubtitleLabelParserTests(unittest.TestCase):
    def test_simplified_aliases_are_exact_tokens_and_case_insensitive(self):
        for raw in ("sc", "SC", "chS", "GB", "gb", "gbk", "zh-Hans"):
            with self.subTest(raw=raw):
                result = parse_subtitle_label(raw)
                self.assertEqual(LabelParseStatus.RESOLVED, result.status)
                self.assertEqual("zh", result.primary_language)
                self.assertEqual("Hans", result.script)

    def test_traditional_and_unspecified_chinese_remain_distinct(self):
        traditional = parse_subtitle_label("cHt.default")
        unspecified = parse_subtitle_label("中文")

        self.assertEqual(LabelParseStatus.RESOLVED, traditional.status)
        self.assertEqual("Hant", traditional.script)
        self.assertEqual(("default",), traditional.flags)
        self.assertEqual(LabelParseStatus.NEEDS_REVIEW, unspecified.status)
        self.assertEqual("zh", unspecified.primary_language)
        self.assertIsNone(unspecified.script)

    def test_registered_jpsc_and_jptc_are_structured_bilingual_labels(self):
        cases = {"jpsc": "Hans", "JPSC": "Hans", "ｊｐｓｃ": "Hans", "jptc": "Hant"}
        for raw, script in cases.items():
            with self.subTest(raw=raw):
                result = parse_subtitle_label(f"{raw}.default.sdh")
                self.assertEqual(LabelParseStatus.RESOLVED, result.status)
                self.assertEqual("zh", result.primary_language)
                self.assertEqual(script, result.script)
                self.assertEqual(("ja",), result.additional_languages)
                self.assertEqual(("default", "sdh"), result.flags)

    def test_composite_aliases_require_whole_token_boundaries(self):
        for raw in ("xjpsc", "jpsc2", "jp-sc", "GBFans", "Oscar", "Big5Fix"):
            with self.subTest(raw=raw):
                result = parse_subtitle_label(raw)
                self.assertNotEqual(LabelParseStatus.RESOLVED, result.status)
                self.assertEqual((raw,), result.unknown_tokens)

    def test_flag_aliases_require_whole_token_boundaries(self):
        result = parse_subtitle_label("SC.defaulted.forced2.SDHGroup")
        self.assertEqual((), result.flags)
        self.assertEqual(
            ("defaulted", "forced2", "SDHGroup"),
            result.unknown_tokens,
        )

    def test_non_width_nfkc_compatibility_never_becomes_auto_executable(self):
        cases = {"㎇": "gb", "㏄": "cc", "ⓢⓒ": "sc"}
        for raw, normalized in cases.items():
            with self.subTest(raw=raw):
                result = parse_subtitle_label(raw)
                self.assertEqual(normalized, result.tokens[0].normalized)
                self.assertEqual(
                    NormalizationKind.COMPATIBILITY,
                    result.tokens[0].normalization_kind,
                )
                self.assertTrue(result.blocks_execution)
                self.assertIn(
                    "TAG_UNSAFE_COMPATIBILITY_NORMALIZATION",
                    result.reason_codes,
                )

    def test_fullwidth_ascii_is_a_safe_width_variant(self):
        result = parse_subtitle_label("ＳＣ")
        self.assertEqual(LabelParseStatus.RESOLVED, result.status)
        self.assertEqual(NormalizationKind.WIDTH_FOLD, result.tokens[0].normalization_kind)

    def test_compound_bcp47_and_bracket_atoms_are_not_split(self):
        result = parse_subtitle_label("[AIC-Team].zh_Hans.v2.default")
        self.assertEqual(
            ("[AIC-Team]", "zh_Hans", "v2", "default"),
            result.raw_tokens,
        )
        self.assertEqual(TokenRole.TITLE, result.tokens[0].role)
        self.assertEqual("Hans", result.script)
        self.assertEqual(("[AIC-Team]", "v2"), result.track_title_tokens)

    def test_adjacent_brackets_are_separate_atoms_and_known_values_parse(self):
        result = parse_subtitle_label("【AIC】【SC】【default】")
        self.assertEqual(("【AIC】", "【SC】", "【default】"), result.raw_tokens)
        self.assertEqual("Hans", result.script)
        self.assertEqual(("default",), result.flags)

    def test_unregistered_multiple_languages_never_invent_primary(self):
        for raw in ("ja.sc", "en.sc", "sc.en"):
            with self.subTest(raw=raw):
                result = parse_subtitle_label(raw)
                self.assertEqual(LabelParseStatus.NEEDS_REVIEW, result.status)
                self.assertIsNone(result.primary_language)
                self.assertIn("TAG_MULTIPLE_LANGUAGES_NEED_REVIEW", result.reason_codes)

    def test_simplified_traditional_and_region_script_mismatches_conflict(self):
        for raw in ("sc.tc", "chs.big5", "zh-Hans.zh-Hant", "zh-TW.zh-Hans", "zh-CN.zh-Hant"):
            with self.subTest(raw=raw):
                result = parse_subtitle_label(raw)
                self.assertEqual(LabelParseStatus.CONFLICT, result.status)
                self.assertEqual(DimensionResolution.CONFLICT, result.resolution_state)

    def test_compatible_duplicate_aliases_deduplicate_semantics(self):
        for raw in ("sc.chs", "sc.zh-Hans", "zh-CN.zh-Hans"):
            with self.subTest(raw=raw):
                result = parse_subtitle_label(raw)
                self.assertEqual(LabelParseStatus.RESOLVED, result.status)
                self.assertEqual(("zh",), result.detected_languages)
                self.assertEqual("Hans", result.script)

    def test_hi_uses_language_or_accessibility_context(self):
        hindi = parse_subtitle_label("hi")
        english_hi = parse_subtitle_label("en.hi")
        hindi_sdh = parse_subtitle_label("hi.sdh")

        self.assertEqual("hi", hindi.primary_language)
        self.assertEqual((), hindi.flags)
        self.assertEqual("en", english_hi.primary_language)
        self.assertEqual(("sdh",), english_hi.flags)
        self.assertEqual("hi", hindi_sdh.primary_language)
        self.assertEqual(("sdh",), hindi_sdh.flags)

    def test_flags_are_deduplicated_and_have_deterministic_order(self):
        result = parse_subtitle_label("SC.sdh.forced.default.cc")
        self.assertEqual(("default", "forced", "sdh"), result.flags)
        self.assertEqual(LabelParseStatus.RESOLVED, result.status)

    def test_unknown_tokens_are_preserved_and_block_execution(self):
        result = parse_subtitle_label("[AIC].SC.v2.Mystery.default")
        self.assertEqual(("Mystery",), result.unknown_tokens)
        self.assertEqual(("[AIC]", "v2"), result.track_title_tokens)
        self.assertEqual(LabelParseStatus.NEEDS_REVIEW, result.status)

    def test_empty_flag_only_unclosed_bracket_and_zero_width_fail_closed(self):
        cases = ("", "default", "[AIC.SC", "SC.\u200b")
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertTrue(parse_subtitle_label(raw).blocks_execution)

    def test_unclosed_bracket_recovers_visible_tail_tokens_but_still_blocks(self):
        result = parse_subtitle_label("[Group.SC.default")

        self.assertEqual(("[Group", "SC", "default"), result.raw_tokens)
        self.assertEqual(TokenRole.UNKNOWN, result.tokens[0].role)
        self.assertIn("TAG_UNCLOSED_BRACKET", result.tokens[0].reason_codes)
        self.assertEqual("zh", result.primary_language)
        self.assertEqual("Hans", result.script)
        self.assertEqual(("default",), result.flags)
        self.assertTrue(result.blocks_execution)

    def test_unclosed_bracket_recovery_supports_unicode_separators(self):
        cases = {
            "（Group。TC。forced": ("Hant", ("forced",)),
            "【组．JPSC．sdh": ("Hans", ("sdh",)),
            "(Group·en·default": (None, ("default",)),
            "[Group SC default": ("Hans", ("default",)),
        }
        for raw, (script, flags) in cases.items():
            with self.subTest(raw=raw):
                result = parse_subtitle_label(raw)
                self.assertIn("TAG_UNCLOSED_BRACKET", result.tokens[0].reason_codes)
                self.assertEqual(script, result.script)
                self.assertEqual(flags, result.flags)
                self.assertTrue(result.blocks_execution)

        self.assertEqual(("[Group-SC",), parse_subtitle_label("[Group-SC").raw_tokens)

    def test_detected_and_additional_language_contract_is_explicit(self):
        composite = parse_subtitle_label("jpsc")
        mixed = parse_subtitle_label("jpsc.en")
        unresolved = parse_subtitle_label("ja.sc")

        self.assertEqual(("zh", "ja"), composite.detected_languages)
        self.assertEqual(("ja",), composite.additional_languages)
        self.assertEqual(("zh", "ja", "en"), mixed.detected_languages)
        self.assertEqual(("ja",), mixed.additional_languages)
        self.assertEqual(LabelParseStatus.NEEDS_REVIEW, mixed.status)
        self.assertEqual(("ja", "zh"), unresolved.detected_languages)
        self.assertEqual((), unresolved.additional_languages)
        self.assertIsNone(unresolved.primary_language)

    def test_ambiguous_region_shorthands_are_never_silently_consumed(self):
        for raw in ("cn", "tw", "jp", "sc.cn"):
            with self.subTest(raw=raw):
                result = parse_subtitle_label(raw)
                self.assertEqual(LabelParseStatus.NEEDS_REVIEW, result.status)
                self.assertIn("TAG_AMBIGUOUS_LANGUAGE_ALIAS", result.reason_codes)

    def test_token_spans_do_not_overlap_and_cover_every_non_separator_atom(self):
        raw = "[A.I.C].JPSC.default.sdh"
        result = parse_subtitle_label(raw)
        previous_end = -1
        for token in result.tokens:
            self.assertGreaterEqual(token.span_start, previous_end)
            self.assertEqual(token.raw, raw[token.span_start:token.span_end])
            previous_end = token.span_end

    def test_result_is_frozen_versioned_and_deterministic(self):
        first = parse_subtitle_label("JPSC.default.sdh")
        second = parse_subtitle_label("JPSC.default.sdh")
        self.assertEqual(first, second)
        self.assertEqual(SUBTITLE_LABEL_RULE_VERSION, first.to_dict()["rule_version"])
        self.assertEqual(first.to_dict(), second.to_dict())
        with self.assertRaises(Exception):
            first.raw_label = "changed"


if __name__ == "__main__":
    unittest.main()
