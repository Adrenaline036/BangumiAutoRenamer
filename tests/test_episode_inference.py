import tempfile
import unittest
from pathlib import Path

from src.rename.episode_inference import (
    EpisodeInferenceError,
    build_episode_hints,
    build_secondary_sequence_hints,
    build_semantic_context,
    normalize_target_stem,
    validate_episode_mapping,
)


class DirectoryEpisodeInferenceTests(unittest.TestCase):
    def _paths(self, root: Path, names):
        paths = []
        for name in names:
            path = root / name
            path.touch()
            paths.append(path)
        return paths

    def test_lucky_star_full_season_uses_varying_space_delimited_field(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self._paths(
                root,
                [
                    f'Lucky Star 2007 - {episode:02d} '
                    '[BD 1920x1080 HEVC-yuv420p10 FLAC Chap] '
                    '- LittleBakas!.mkv'
                    for episode in range(1, 25)
                ],
            )
            hints = build_episode_hints(paths, 'Lucky Star')

        self.assertEqual(list(range(1, 25)), [hints[path] for path in paths])

    def test_long_season_ignores_fractional_title_and_audio_numbers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self._paths(
                root,
                [
                    f'Ranma 1⁄2 {episode:03d} '
                    '(BDRip 1444x1080p x265 HEVC FLAC,AAC 2.0x2)'
                    '(Dual Audio)[sxales].mkv'
                    for episode in range(1, 175)
                ],
            )
            hints = build_episode_hints(paths, 'Ranma ½')

        self.assertEqual(174, len(set(hints.values())))
        self.assertEqual(list(range(1, 175)), [hints[path] for path in paths])

    def test_small_named_reset_sequence_is_excluded_from_main_episode_hints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main = self._paths(
                root,
                [
                    f'Ranma ½ - {episode:03d} '
                    '(BDRip 1444x1080p x265 HEVC FLAC,AAC 2.0x2)'
                    '(Dual Audio)[sxales].mkv'
                    for episode in range(1, 162)
                ],
            )
            super_files = self._paths(
                root,
                [
                    f'Ranma ½ Super - {episode:02d} '
                    '(BDRip 1436x1080p x265 HEVC FLAC,AAC 2.0x2)'
                    '(Dual Audio)[sxales].mkv'
                    for episode in range(1, 4)
                ],
            )
            secondary = build_secondary_sequence_hints(
                main + super_files,
                'Ranma ½',
            )
            main_hints = build_episode_hints(main + super_files, 'Ranma ½')

        self.assertEqual([1, 2, 3], [secondary[path] for path in super_files])
        self.assertTrue(all(path not in main_hints for path in super_files))
        self.assertEqual(list(range(1, 162)), [main_hints[path] for path in main])

    def test_bracketed_named_reset_sequence_is_separate_from_main_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main = self._paths(
                root,
                [
                    f'[I.G&CASO][Lucky_Star][{episode:02d}]'
                    '[BDRIP][1920x1080][x264_FLAC_2][8ECF808D].mkv'
                    for episode in range(1, 25)
                ],
            )
            budoukan = self._paths(
                root,
                [
                    f'[I.G&CASO][Lucky_Star_in_Budoukan][{episode:02d}]'
                    '[BDRIP][1920x1080][x264_FLAC][0496A842].mkv'
                    for episode in range(1, 4)
                ],
            )

            secondary = build_secondary_sequence_hints(
                main + budoukan,
                'Lucky Star',
            )
            main_hints = build_episode_hints(main + budoukan, 'Lucky Star')

        self.assertEqual([1, 2, 3], [secondary[path] for path in budoukan])
        self.assertTrue(all(path not in main_hints for path in budoukan))
        self.assertEqual(list(range(1, 25)), [main_hints[path] for path in main])

    def test_explicit_season_family_is_not_assumed_to_be_bonus(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main = self._paths(
                root,
                [f'Example - {episode:02d}.mkv' for episode in range(1, 13)],
            )
            season_two = self._paths(
                root,
                [f'Example Season 2 - {episode:02d}.mkv' for episode in range(1, 4)],
            )

            secondary = build_secondary_sequence_hints(
                main + season_two,
                'Example',
            )

        self.assertEqual({}, secondary)

    def test_multiple_release_grammars_share_the_same_directory_invariants(self):
        cases = (
            (
                'bracket',
                'Example Show',
                lambda episode: f'[Group] Example Show [{episode:02d}][1080p].mkv',
            ),
            (
                'explicit-e',
                'Example Show',
                lambda episode: f'Example Show E{episode:03d} WEB-DL.mkv',
            ),
            (
                'versioned',
                'Example Show',
                lambda episode: f'Example Show {episode:02d}v2 10bit.mkv',
            ),
            (
                'numeric-title',
                '86',
                lambda episode: f'86 - {episode:02d} 1920x1080 FLAC 2.0.mkv',
            ),
            (
                'year-in-title',
                'Class of 1999',
                lambda episode: f'Class of 1999 - {episode:02d} [BDRip].mkv',
            ),
        )

        for label, series_title, formatter in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                paths = self._paths(
                    root,
                    [formatter(episode) for episode in range(1, 13)],
                )
                hints = build_episode_hints(paths, series_title)

                self.assertEqual(
                    list(range(1, 13)),
                    [hints[path] for path in paths],
                )

    def test_nova_and_title_word_ova_are_not_bonus_context(self):
        self.assertNotIn('OVA', build_semantic_context('NOVA - 01.mkv', 'NOVA'))
        self.assertNotIn(
            'OVA',
            build_semantic_context('OVA Project - 01.mkv', 'OVA Project'),
        )
        self.assertIn(
            'OVA 01',
            build_semantic_context('Example Show - OVA 01.mkv', 'Example Show'),
        )
        self.assertEqual(
            'AMBIGUOUS_ROLE_MARKER',
            build_semantic_context('OVA Project - 01.mkv', '不同语言标题'),
        )

    def test_duplicate_episode_mapping_is_rejected(self):
        first = Path('source/Show - 01.mkv')
        second = Path('source/Show - 01 alternate.mkv')

        with self.assertRaises(EpisodeInferenceError):
            validate_episode_mapping({first: 1, second: 1})

    def test_target_stem_removes_release_year_and_technical_metadata(self):
        value = normalize_target_stem(
            'Lucky Star 2007 - OVA '
            '[BD 1920x1080 HEVC-yuv420p10 FLAC Chap] - LittleBakas!'
        )

        self.assertEqual('Lucky Star - OVA', value)
        self.assertEqual(
            'Lucky Star - EP01',
            normalize_target_stem(
                'Lucky Star 2007 - EP01 [BDRip]',
                '幸运☆星',
            ),
        )
        self.assertEqual(
            'Lucky Star - 特典映像1',
            normalize_target_stem(
                'Lucky Star 2007 - 特典映像1 [BDRip]',
                '幸运☆星',
            ),
        )

    def test_target_stem_preserves_title_year_and_trailing_subtitle(self):
        self.assertEqual(
            'Class of 1999 - 01',
            normalize_target_stem(
                'Class of 1999 - 01 [BDRip]',
                'Class of 1999',
            ),
        )
        self.assertEqual(
            'Steins;Gate - Zero',
            normalize_target_stem(
                'Steins;Gate - Zero [BD 1080p]',
                'Steins;Gate - Zero',
            ),
        )
        self.assertEqual(
            'Blade Runner 2049 - Trailer',
            normalize_target_stem(
                'Blade Runner 2049 - Trailer [BDRip]',
                'Blade Runner 2049',
            ),
        )

    def test_target_stem_attaches_compound_subtitle_language_suffix(self):
        self.assertEqual(
            'Yama no Susume Next Summit [05].tc',
            normalize_target_stem(
                '[VCB-Studio] Yama no Susume Next Summit [05]'
                '[Ma10p_1080p][x265_flac].tc',
                'Yama no Susume Next Summit',
            ),
        )

    def test_video_and_group_suffixed_subtitle_share_normalized_base_stem(self):
        video = normalize_target_stem(
            'Ranma 1⁄2 - 001 '
            '(BDRip 1444x1080p x265 HEVC FLAC,AAC 2.0x2)'
            '(Dual Audio)[sxales]',
            'Ranma ½',
        )
        subtitle = normalize_target_stem(
            'Ranma 1⁄2 - 001 '
            '(BDRip 1444x1080p x265 HEVC FLAC,AAC 2.0x2)'
            '(Dual Audio)[sxales].sc',
            'Ranma ½',
        )

        self.assertEqual('Ranma 1⁄2 - 001', video)
        self.assertEqual(f'{video}.sc', subtitle)

    def test_target_stem_removes_leading_and_trailing_release_groups(self):
        self.assertEqual(
            'Series - 01',
            normalize_target_stem(
                '[VCB-Studio] Series - 01 [1080p][sxales]',
                'Series',
            ),
        )
        self.assertEqual(
            '[86] - 01',
            normalize_target_stem('[86] - 01 [1080p]', '[86]'),
        )

    def test_target_stem_preserves_role_marker_inside_mixed_sp_block(self):
        self.assertEqual(
            'Yama no Susume Next Summit [Blu-ray&DVD CM]',
            normalize_target_stem(
                '[VCB-Studio] Yama no Susume Next Summit '
                '[Blu-ray&DVD CM][Ma10p_1080p][x265_flac]',
                'Yama no Susume Next Summit',
            ),
        )


if __name__ == '__main__':
    unittest.main()
