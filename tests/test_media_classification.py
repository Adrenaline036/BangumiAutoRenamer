import tempfile
import unittest
from pathlib import Path

from src.rename.process import Rename
from src.rename.media_classifier import LocalRole, classify_media


class TraditionalMediaClassificationTests(unittest.TestCase):
    def setUp(self):
        self.renamer = object.__new__(Rename)
        self.renamer.R = {}

    def route(
        self,
        relative_source: str,
        season_id: int = 4,
        common_parts=None,
    ) -> Path:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source' / relative_source
            work_path = root / 'organized' / 'Yama no Susume (2013)'
            self.renamer.R = {}
            self.renamer.process_sub(
                'Yama no Susume Next Summit',
                common_parts,
                source,
                work_path,
                season_id,
            )
            return self.renamer.R[source].relative_to(work_path)

    def test_regular_episode_stays_in_season_root(self):
        filename = (
            '[VCB-Studio] Yama no Susume Next Summit [01]'
            '[Ma10p_1080p][x265_aac].mkv'
        )

        target = self.route(
            filename,
            common_parts=['[VCB-Studio] Yama no Susume Next Summit '],
        )

        self.assertEqual(
            Path('Season 04') / (
                'S04E01 - Yama no Susume Next Summit [01].mkv'
            ),
            target,
        )

    def test_single_digit_episode_is_still_supported(self):
        filename = '[Group] Series - 1.mkv'

        target = self.route(
            filename,
            common_parts=['[Group] Series - '],
        )

        self.assertEqual(
            Path('Season 04') / 'S04E01 - Series - 1.mkv',
            target,
        )

    def test_exploration_party_in_specials_folder_goes_to_season_extras(self):
        filename = (
            '[VCB-Studio] Yama no Susume Next Summit '
            '[Exploration Party][Ma10p_1080p][x265_aac].mkv'
        )

        target = self.route(f'SPs/{filename}')

        self.assertEqual(
            Path('Season 04') / 'Extras' / (
                'Yama no Susume Next Summit '
                '[Exploration Party].mkv'
            ),
            target,
        )
        self.assertNotIn('Season 00', target.parts)

    def test_ova_inside_specials_folder_is_kept_separate_from_main_episodes(self):
        filename = '[VCB-Studio] Yama no Susume Next Summit OVA01.mkv'

        target = self.route(f'SPs/{filename}')

        self.assertEqual(
            Path('Season 04') / 'Extras' / 'Yama no Susume Next Summit OVA01.mkv',
            target,
        )

    def test_ova_subtitle_inherits_the_confirmed_video_target(self):
        release_stem = '[Group] Series OVA01 [1080p][sxales]'
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source' / 'SPs'
            source.mkdir(parents=True)
            video = source / f'{release_stem}.mkv'
            subtitle = source / f'{release_stem}.sc.ass'
            video.touch()
            subtitle.touch()
            work_path = root / 'organized' / 'Series (2026)'

            error = self.renamer._process_traditional(
                source.parent,
                'Series',
                work_path,
                1,
                target_series_title='Series',
            )
            video_target = self.renamer.R[video].relative_to(work_path)
            subtitle_target = self.renamer.R[subtitle].relative_to(work_path)

        self.assertIsNone(error)
        self.assertEqual(Path('Season 01') / 'Extras', video_target.parent)
        self.assertEqual(video_target.parent, subtitle_target.parent)
        self.assertEqual(
            video_target.stem,
            subtitle_target.name[:-len('.zh-Hans.ass')],
        )

    def test_spaced_ova_number_is_preserved_without_becoming_main_episode(self):
        filename = '[Group] Series OVA 01.mkv'
        source = Path('source') / 'SPs' / filename

        classification = classify_media(
            item_path=source,
            candidate_name='OVA 01',
            season_id=4,
            extra_context_name='OVA 01.mkv',
        )
        target = self.route(f'SPs/{filename}')

        self.assertEqual(LocalRole.EPISODE_LIKE_BONUS, classification.local_role)
        self.assertEqual(1, classification.episode_number)
        self.assertEqual(
            Path('Season 04') / 'Extras' / 'Series OVA 01.mkv',
            target,
        )

    def test_numbered_special_is_kept_separate_from_main_episodes(self):
        filename = (
            'Ranma ½ Special - 01 '
            '(BDRip 1436x1080p x265 HEVC FLAC)(Dual Audio)[sxales].mkv'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source' / filename
            work_path = root / 'organized' / 'Ranma (1989)'
            self.renamer.R = {}
            classification = self.renamer.process_sub(
                'Ranma ½',
                None,
                source,
                work_path,
                1,
                semantic_context='SPECIAL 01',
                target_series_title='Ranma ½',
            )
            target = self.renamer.R[source].relative_to(work_path)

        self.assertEqual(LocalRole.EPISODE_LIKE_BONUS, classification.local_role)
        self.assertEqual(Path('Season 01') / 'Extras', target.parent)
        self.assertFalse(target.name.startswith('S01E01'))

    def test_special_inside_series_title_does_not_override_main_episode(self):
        filename = 'Special A - 01.mkv'
        classification = classify_media(
            item_path=Path('source') / filename,
            candidate_name=filename,
            season_id=1,
            extra_context_name='01',
            episode_hint=1,
        )

        self.assertEqual(LocalRole.MAIN_EPISODE, classification.local_role)
        self.assertEqual(1, classification.episode_number)

    def test_numbered_ova_and_oad_keep_bonus_role_and_episode_number(self):
        cases = (
            ('OAD 02.mkv', 'OAD 02', 2),
            ('OVA03.mkv', 'OVA03', 3),
        )

        for filename, context, expected_episode in cases:
            with self.subTest(filename=filename):
                classification = classify_media(
                    item_path=Path('source') / filename,
                    candidate_name=context,
                    season_id=4,
                    extra_context_name=filename,
                )

                self.assertEqual(
                    LocalRole.EPISODE_LIKE_BONUS,
                    classification.local_role,
                )
                self.assertEqual(
                    expected_episode,
                    classification.episode_number,
                )

    def test_vcb_integrated_oad_uses_the_main_sequence_number(self):
        cases = (
            ('[13(OAD)].mkv', '[13(OAD)]', 13),
            ('[14(OVA02)].mkv', '[14(OVA02)]', 14),
        )

        for filename, context, expected_episode in cases:
            with self.subTest(filename=filename):
                classification = classify_media(
                    item_path=Path('source') / filename,
                    candidate_name=context,
                    season_id=4,
                    extra_context_name=context,
                )

                self.assertEqual(
                    LocalRole.EPISODE_LIKE_BONUS,
                    classification.local_role,
                )
                self.assertEqual(expected_episode, classification.episode_number)

    def test_vcb_numbered_sp_is_disc_extra_not_episode_like_bonus(self):
        classification = classify_media(
            item_path=Path('source') / 'SPs' / '[Group] Series [SP06].mkv',
            candidate_name='[SP06]',
            season_id=4,
            extra_context_name='[SP06].mkv',
        )

        self.assertEqual(LocalRole.DISC_EXTRA, classification.local_role)

    def test_ambiguous_extra_words_do_not_silently_override_episode_routing(self):
        for marker in ('Event', 'IV', 'NC'):
            with self.subTest(marker=marker):
                filename = f'[Group] Series {marker} - 01.mkv'
                classification = classify_media(
                    item_path=Path('source') / filename,
                    candidate_name=f'{marker} - 01',
                    season_id=4,
                    extra_context_name=f'{marker} - 01.mkv',
                )

                self.assertEqual(LocalRole.UNKNOWN, classification.local_role)
                self.assertEqual('Low', classification.confidence)

    def test_ambiguous_extra_word_is_accepted_when_structurally_marked(self):
        classification = classify_media(
            item_path=Path('source') / '[Group] Series [Event].mkv',
            candidate_name='[Event]',
            season_id=4,
            extra_context_name='[Event].mkv',
        )

        self.assertEqual(LocalRole.DISC_EXTRA, classification.local_role)
        self.assertEqual('High', classification.confidence)

    def test_structured_marker_survives_title_excluded_semantic_context(self):
        filename = '[I.G&CASO][Lucky_Star][SP][Lucky_Racer][E1DEA6ED].mkv'
        classification = classify_media(
            item_path=Path('source') / filename,
            candidate_name=Path(filename).stem,
            season_id=1,
            extra_context_name='I G CASO SP LUCKY RACER E1DEA6ED',
        )

        self.assertEqual(LocalRole.DISC_EXTRA, classification.local_role)
        self.assertEqual('High', classification.confidence)

    def test_crc_substring_ed_does_not_override_episode_routing(self):
        filename = (
            '[I.G&CASO][Lucky_Star][15][BDRIP][1920x1080]'
            '[x264_FLAC_2][B63ED209].mkv'
        )
        classification = classify_media(
            item_path=Path('source') / filename,
            candidate_name=Path(filename).stem,
            season_id=1,
            extra_context_name=(
                'I G CASO 15 BDRIP 1920X1080 X264 FLAC 2 B63ED209'
            ),
        )

        self.assertEqual(LocalRole.MAIN_EPISODE, classification.local_role)
        self.assertEqual(15, classification.episode_number)

    def test_vcb_structured_extra_markers_are_recognized(self):
        markers = (
            'IV02',
            'CM23',
            'Menu04_2',
            'NCOP01_EP01-03',
            'PV01',
            'Trailer01',
            'Preview08',
            'Web Preview 01',
            'Advice08',
        )

        for marker in markers:
            with self.subTest(marker=marker):
                classification = classify_media(
                    item_path=Path('source') / f'[Group] Series [{marker}].mkv',
                    candidate_name=f'[{marker}]',
                    season_id=4,
                    extra_context_name=f'[{marker}].mkv',
                )

                self.assertEqual(LocalRole.DISC_EXTRA, classification.local_role)

    def test_named_disc_extra_goes_to_current_season_extras(self):
        filename = '[VCB-Studio] Yama no Susume Next Summit [NCOP].mkv'

        target = self.route(filename)

        self.assertEqual(
            Path('Season 04') / 'Extras' / 'Yama no Susume Next Summit [NCOP].mkv',
            target,
        )

    def test_extra_word_in_series_title_does_not_reclassify_main_episode(self):
        filename = 'Event Series - 01.mkv'

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source' / filename
            work_path = root / 'organized' / 'Event Series (2026)'
            self.renamer.R = {}
            self.renamer.process_sub(
                'Event Series',
                ['Event Series - '],
                source,
                work_path,
                1,
            )
            target = self.renamer.R[source].relative_to(work_path)

        self.assertEqual(
            Path('Season 01') / f'S01E01 - {filename}',
            target,
        )

    def test_ova_word_in_series_title_does_not_reclassify_main_episode(self):
        filename = 'OVA Project - 01.mkv'

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source' / filename
            work_path = root / 'organized' / 'OVA Project (2026)'
            self.renamer.R = {}
            self.renamer.process_sub(
                'OVA Project',
                ['OVA Project - '],
                source,
                work_path,
                1,
            )
            target = self.renamer.R[source].relative_to(work_path)

        self.assertEqual(
            Path('Season 01') / f'S01E01 - {filename}',
            target,
        )

    def test_episode_subtitle_follows_the_same_numbering_route(self):
        filename = '[Group] Series - 01.zh-Hans.ass'

        target = self.route(
            filename,
            common_parts=['[Group] Series - '],
        )

        self.assertEqual(
            Path('Season 04') / 'S04E01 - Series - 01.zh-Hans.ass',
            target,
        )

    def test_compound_subtitle_suffix_matches_normalized_video_stem(self):
        filename = (
            '[VCB-Studio] Yama no Susume Next Summit [05]'
            '[Ma10p_1080p][x265_flac].tc.ass'
        )

        target = self.route(
            filename,
            common_parts=['[VCB-Studio] Yama no Susume Next Summit '],
        )

        self.assertEqual(
            Path('Season 04') / (
                'S04E05 - Yama no Susume Next Summit [05].tc.ass'
            ),
            target,
        )

    def test_ranma_video_and_group_suffixed_subtitle_share_target_stem(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source'
            source.mkdir()
            pairs = []
            for episode in (1, 2):
                release_stem = (
                    f'Ranma 1⁄2 - {episode:03d} '
                    '(BDRip 1444x1080p x265 HEVC FLAC,AAC 2.0x2)'
                    '(Dual Audio)[sxales]'
                )
                video = source / f'{release_stem}.mkv'
                subtitle = source / f'{release_stem}.sc.ass'
                video.touch()
                subtitle.touch()
                pairs.append((episode, video, subtitle))
            work_path = root / 'organized' / 'Ranma 1／2 (1989)'

            error = self.renamer._process_traditional(
                source,
                'Ranma ½',
                work_path,
                1,
                target_series_title='Ranma ½',
            )
            targets = {
                source_path: target.relative_to(work_path)
                for source_path, target in self.renamer.R.items()
            }

        self.assertIsNone(error)
        for episode, video, subtitle in pairs:
            video_target = targets[video]
            subtitle_target = targets[subtitle]
            expected_stem = f'S01E{episode:02d} - Ranma 1⁄2 - {episode:03d}'
            self.assertEqual(
                Path('Season 01') / f'{expected_stem}.mkv',
                video_target,
            )
            self.assertEqual(
                Path('Season 01') / f'{expected_stem}.zh-Hans.ass',
                subtitle_target,
            )
            self.assertEqual(
                video_target.stem,
                subtitle_target.name[:-len('.zh-Hans.ass')],
            )

    def test_orphan_subtitle_is_held_for_manual_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source'
            source.mkdir()
            subtitle = source / 'Series - 01.sc.ass'
            subtitle.touch()
            work_path = root / 'organized' / 'Series (2026)'

            error = self.renamer._process_traditional(
                source,
                'Series',
                work_path,
                1,
                target_series_title='Series',
            )
            target = self.renamer.R[subtitle].relative_to(work_path)

        self.assertIsNone(error)
        self.assertEqual(Path('ManualReview') / 'subtitles', target.parent)
        self.assertTrue(target.name.endswith('-Series - 01.sc.ass'))
        self.assertIsNone(
            self.renamer.subtitle_contracts[subtitle].associated_video_source_path
        )
        self.assertTrue(
            self.renamer.subtitle_contracts[subtitle].association_state.value
            == 'unresolved'
        )

    def test_mixed_technical_sp_label_keeps_its_role_name(self):
        filename = (
            '[VCB-Studio] Yama no Susume Next Summit '
            '[Blu-ray&DVD CM][Ma10p_1080p][x265_flac].mkv'
        )

        target = self.route(f'SPs/{filename}')

        self.assertEqual(
            Path('Season 04') / 'Extras' / (
                'Yama no Susume Next Summit '
                '[Blu-ray&DVD CM].mkv'
            ),
            target,
        )

    def test_explicit_provider_special_stays_beside_its_related_season(self):
        filename = '[VCB-Studio] Yama no Susume Next Summit S00E01.mkv'

        target = self.route(f'Specials/{filename}')

        self.assertEqual(
            Path('Season 04') / 'Yama no Susume Next Summit S00E01.mkv',
            target,
        )

    def test_unknown_video_goes_to_manual_review_without_s00e00(self):
        filename = (
            '[VCB-Studio] Yama no Susume Next Summit '
            '[Unlabelled Clip][Ma10p_1080p][x265_aac].mkv'
        )

        target = self.route(filename)

        self.assertEqual(
            Path('ManualReview') / 'Season 04' / (
                'Yama no Susume Next Summit [Unlabelled Clip].mkv'
            ),
            target,
        )
        self.assertNotIn('S00E00', target.name)

    def test_codec_numbers_do_not_become_episode_numbers(self):
        filename = '[Group] Mystery [Ma10p_1080p][x265_aac].mkv'

        target = self.route(filename)

        self.assertEqual(
            Path('ManualReview') / 'Season 04' / 'Mystery.mkv',
            target,
        )

    def test_release_year_does_not_conflict_with_regular_episode_number(self):
        filename = (
            'Lucky Star 2007 - 01 '
            '[BD 1920x1080 HEVC-yuv420p10 FLAC Chap] - LittleBakas!.mkv'
        )
        classification = classify_media(
            item_path=Path('source') / filename,
            candidate_name=filename,
            season_id=1,
            extra_context_name=filename,
        )

        self.assertEqual(LocalRole.MAIN_EPISODE, classification.local_role)
        self.assertEqual(1, classification.episode_number)

    def test_lucky_star_release_folder_routes_main_and_ova_without_manual_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source'
            source.mkdir()
            main_one = source / (
                'Lucky Star 2007 - 01 '
                '[BD 1920x1080 HEVC-yuv420p10 FLAC Chap] - LittleBakas!.mkv'
            )
            main_two = source / (
                'Lucky Star 2007 - 02 '
                '[BD 1920x1080 HEVC-yuv420p10 FLAC Chap] - LittleBakas!.mkv'
            )
            ova = source / (
                'Lucky Star 2007 - OVA '
                '[BD 1920x1080 HEVC-yuv420p10 FLAC Chap] - LittleBakas!.mkv'
            )
            for media_file in (main_one, main_two, ova):
                media_file.touch()

            work_path = root / 'ManualReview' / 'BAR-Test' / 'Anime' / (
                '幸运☆星 (2007)'
            )
            self.renamer.R = {}
            self.renamer._process_traditional(
                source,
                'Lucky Star',
                work_path,
                1,
            )
            targets = {
                source_path: target.relative_to(work_path)
                for source_path, target in self.renamer.R.items()
            }

        self.assertEqual(
            Path('Season 01') / 'S01E01 - Lucky Star - 01.mkv',
            targets[main_one],
        )
        self.assertEqual(
            Path('Season 01') / 'S01E02 - Lucky Star - 02.mkv',
            targets[main_two],
        )
        self.assertEqual(
            Path('Season 01') / 'Extras' / 'Lucky Star - OVA.mkv',
            targets[ova],
        )
        self.assertFalse(
            any('ManualReview' in target.parts for target in targets.values())
        )

    def test_lucky_star_complete_24_episode_directory_is_unique_and_clean(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source'
            source.mkdir()
            main_files = []
            for episode in range(1, 25):
                path = source / (
                    f'Lucky Star 2007 - {episode:02d} '
                    '[BD 1920x1080 HEVC-yuv420p10 FLAC Chap] '
                    '- LittleBakas!.mkv'
                )
                path.touch()
                main_files.append(path)
            ova = source / (
                'Lucky Star 2007 - OVA '
                '[BD 1920x1080 HEVC-yuv420p10 FLAC Chap] '
                '- LittleBakas!.mkv'
            )
            ova.touch()

            work_path = root / 'organized' / '幸运☆星 (2007)'
            error = self.renamer._process_traditional(
                source,
                'Lucky Star',
                work_path,
                1,
            )
            targets = {
                source_path: target.relative_to(work_path)
                for source_path, target in self.renamer.R.items()
            }

        self.assertIsNone(error)
        self.assertEqual(25, len(set(targets.values())))
        for episode, source_path in enumerate(main_files, start=1):
            self.assertEqual(
                Path('Season 01') / (
                    f'S01E{episode:02d} - Lucky Star - {episode:02d}.mkv'
                ),
                targets[source_path],
            )
        self.assertEqual(
            Path('Season 01') / 'Extras' / 'Lucky Star - OVA.mkv',
            targets[ova],
        )
        self.assertFalse(
            any('ManualReview' in target.parts for target in targets.values())
        )

    def test_ranma_long_season_has_174_unique_episode_targets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source'
            source.mkdir()
            main_files = []
            for episode in range(1, 175):
                path = source / (
                    f'Ranma 1⁄2 {episode:03d} '
                    '(BDRip 1444x1080p x265 HEVC FLAC,AAC 2.0x2)'
                    '(Dual Audio)[sxales].mkv'
                )
                path.touch()
                main_files.append(path)

            work_path = root / 'organized' / '乱马1／2 (1989)'
            error = self.renamer._process_traditional(
                source,
                'Ranma ½',
                work_path,
                1,
            )
            targets = {
                source_path: target.relative_to(work_path)
                for source_path, target in self.renamer.R.items()
            }

        self.assertIsNone(error)
        self.assertEqual(174, len(targets))
        self.assertEqual(174, len(set(targets.values())))
        for episode, source_path in enumerate(main_files, start=1):
            self.assertEqual(
                f'S01E{episode:02d}',
                targets[source_path].name.split(' - ', 1)[0],
            )
        self.assertFalse(
            any('ManualReview' in target.parts for target in targets.values())
        )

    def test_title_word_ova_is_protected_for_a_complete_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source'
            source.mkdir()
            paths = []
            for episode in range(1, 13):
                path = source / f'OVA Project - {episode:02d} [1080p].mkv'
                path.touch()
                paths.append(path)

            work_path = root / 'organized' / 'OVA Project (2026)'
            error = self.renamer._process_traditional(
                source,
                'OVA Project',
                work_path,
                1,
            )
            targets = [
                self.renamer.R[path].relative_to(work_path)
                for path in paths
            ]

        self.assertIsNone(error)
        self.assertTrue(all(target.parts[0] == 'Season 01' for target in targets))
        self.assertTrue(all('ManualReview' not in target.parts for target in targets))

    def test_ranma_super_reset_sequence_does_not_conflict_with_main_episode_one(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source'
            source.mkdir()
            main_files = []
            for episode in range(1, 162):
                path = source / (
                    f'Ranma ½ - {episode:03d} '
                    '(BDRip 1444x1080p x265 HEVC FLAC,AAC 2.0x2)'
                    '(Dual Audio)[sxales].mkv'
                )
                path.touch()
                main_files.append(path)
            super_files = []
            for episode in range(1, 4):
                path = source / (
                    f'Ranma ½ Super - {episode:02d} '
                    '(BDRip 1436x1080p x265 HEVC FLAC,AAC 2.0x2)'
                    '(Dual Audio)[sxales].mkv'
                )
                path.touch()
                super_files.append(path)

            work_path = root / 'organized' / '乱马1／2 (1989)'
            error = self.renamer._process_traditional(
                source,
                'Ranma ½',
                work_path,
                1,
            )
            targets = {
                source_path: target.relative_to(work_path)
                for source_path, target in self.renamer.R.items()
            }

        self.assertIsNone(error)
        self.assertEqual(164, len(set(targets.values())))
        self.assertEqual('S01E01', targets[main_files[0]].name.split(' - ', 1)[0])
        self.assertEqual(
            Path('Season 01') / 'Extras',
            targets[super_files[0]].parent,
        )
        self.assertFalse(targets[super_files[0]].name.startswith('S01E01'))
        self.assertIn('Super - 01', targets[super_files[0]].name)

    def test_bracketed_lucky_star_families_do_not_collide(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source'
            source.mkdir()
            main_files = []
            for episode in range(1, 25):
                path = source / (
                    f'[I.G&CASO][Lucky_Star][{episode:02d}]'
                    '[BDRIP][1920x1080][x264_FLAC_2][8ECF808D].mkv'
                )
                path.touch()
                main_files.append(path)
            scene_cut_files = []
            for episode in range(1, 13):
                path = source / (
                    f'[I.G&CASO][Lucky_Star][Scene_Cut_{episode:02d}]'
                    '[BDRIP][1920x1080][x264_FLAC][D4DF9BDB].mkv'
                )
                path.touch()
                scene_cut_files.append(path)
            budoukan_files = []
            for episode in range(1, 4):
                path = source / (
                    f'[I.G&CASO][Lucky_Star_in_Budoukan][{episode:02d}]'
                    '[BDRIP][1920x1080][x264_FLAC][0496A842].mkv'
                )
                path.touch()
                budoukan_files.append(path)

            work_path = root / 'organized' / 'Lucky Star (2007)'
            error = self.renamer._process_traditional(
                source,
                'Lucky Star',
                work_path,
                1,
                target_series_title='Lucky Star',
            )
            targets = {
                source_path: target.relative_to(work_path)
                for source_path, target in self.renamer.R.items()
            }

        self.assertIsNone(error)
        self.assertEqual(39, len(set(targets.values())))
        self.assertTrue(
            all(targets[path].name.startswith('S01E') for path in main_files)
        )
        self.assertTrue(
            all(
                targets[path].parent == Path('Season 01') / 'Extras'
                for path in budoukan_files
            )
        )
        self.assertTrue(
            all(not targets[path].name.startswith('S01E') for path in budoukan_files)
        )
        self.assertTrue(
            all(
                targets[path].parent == Path('Season 01') / 'Extras'
                for path in scene_cut_files
            )
        )

    def test_duplicate_main_episode_is_rejected_before_trans(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source'
            source.mkdir()
            (source / 'Show [01].mkv').touch()
            (source / 'Show [01] alternate.mkv').touch()

            error = self.renamer._process_traditional(
                source,
                'Show',
                root / 'organized' / 'Show (2026)',
                1,
            )

        self.assertIsInstance(error, str)
        self.assertIn('剧集编号冲突', error)

    def test_sps_are_scoped_to_the_detected_season(self):
        filename = '[Group] Series [NCED].mkv'

        season_two = self.route(filename, season_id=2)
        season_four = self.route(filename, season_id=4)

        normalized = 'Series [NCED].mkv'
        self.assertEqual(Path('Season 02') / 'Extras' / normalized, season_two)
        self.assertEqual(Path('Season 04') / 'Extras' / normalized, season_four)

    def test_folder_processing_keeps_main_ova_and_disc_extras_separate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source'
            specials = source / 'SPs'
            specials.mkdir(parents=True)
            main_one = source / '[Group] Next Summit [01][1080p].mkv'
            main_two = source / '[Group] Next Summit [02][1080p].mkv'
            exploration = specials / '[Group] Next Summit [Exploration Party].mkv'
            ova = specials / '[Group] Next Summit OVA01.mkv'
            for media_file in (main_one, main_two, exploration, ova):
                media_file.touch()

            work_path = root / 'organized' / 'Yama no Susume (2013)'
            self.renamer.R = {}
            self.renamer._process_traditional(
                source,
                'Next Summit',
                work_path,
                4,
            )

            targets = {
                source_path: target.relative_to(work_path)
                for source_path, target in self.renamer.R.items()
            }

        self.assertEqual(
            Path('Season 04') / 'S04E01 - Next Summit [01].mkv',
            targets[main_one],
        )
        self.assertEqual(
            Path('Season 04') / 'S04E02 - Next Summit [02].mkv',
            targets[main_two],
        )
        self.assertEqual(
            Path('Season 04') / 'Extras' / 'Next Summit [Exploration Party].mkv',
            targets[exploration],
        )
        self.assertEqual(
            Path('Season 04') / 'Extras' / 'Next Summit OVA01.mkv',
            targets[ova],
        )


if __name__ == '__main__':
    unittest.main()
