import unittest

from src.rename.cleaner import parse_release_title, prepare_search_title


class ReleaseTitleParsingTests(unittest.TestCase):
    def test_littlebakas_suffix_and_trailing_year_are_metadata(self):
        title, year = prepare_search_title(
            'Lucky Star 2007 [BD 1920x1080 HEVC FLAC] - LittleBakas!'
        )

        self.assertEqual('Lucky Star', title)
        self.assertEqual(2007, year)

    def test_hyphen_and_exclamation_in_title_are_preserved(self):
        title, year = prepare_search_title(
            'K-ON! 2009 [BD 1920x1080 HEVC FLAC] - SomeGroup'
        )

        self.assertEqual('K-ON!', title)
        self.assertEqual(2009, year)

    def test_legitimate_hyphenated_subtitle_is_not_treated_as_release_group(self):
        title, year = prepare_search_title(
            '86 - Eighty Six [BD 1920x1080 HEVC FLAC]'
        )

        self.assertEqual('86 - Eighty Six', title)
        self.assertEqual(0, year)

    def test_year_is_only_extracted_when_it_is_a_trailing_field(self):
        title, year = prepare_search_title(
            'Space 1999 Remastered [BD 1920x1080 HEVC FLAC]'
        )

        self.assertEqual('Space 1999 Remastered', title)
        self.assertEqual(0, year)

    def test_plain_one_word_subtitle_is_not_removed_without_suffix_structure(self):
        title, year = prepare_search_title('Steins;Gate - Zero [BD 1080p]')

        self.assertEqual('Steins;Gate - Zero', title)
        self.assertEqual(0, year)

    def test_parser_preserves_original_and_generates_year_fallback(self):
        parsed = parse_release_title(
            'Lucky Star 2007 [BD 1920x1080 HEVC FLAC] - LittleBakas!'
        )

        self.assertEqual(
            'Lucky Star 2007 [BD 1920x1080 HEVC FLAC] - LittleBakas!',
            parsed.original,
        )
        self.assertIn('BD 1920x1080 HEVC FLAC', parsed.technical_tags)
        self.assertEqual('LittleBakas!', parsed.release_group)
        self.assertEqual(('Lucky Star', 2007), parsed.candidates[0].key)
        self.assertIn(
            ('Lucky Star 2007', 0),
            [candidate.key for candidate in parsed.candidates],
        )

    def test_title_ending_in_year_keeps_literal_fallback(self):
        parsed = parse_release_title('Class of 1999 [BD 1080p HEVC]')

        self.assertEqual(('Class of', 1999), parsed.candidates[0].key)
        self.assertIn(
            ('Class of 1999', 0),
            [candidate.key for candidate in parsed.candidates],
        )

    def test_bracketed_official_title_is_not_discarded_as_release_group(self):
        parsed = parse_release_title('[Oshi no Ko] Season 2 [BD 1080p HEVC]')
        candidate_keys = [candidate.key for candidate in parsed.candidates]

        self.assertEqual(('Oshi no Ko', 0), candidate_keys[0])
        self.assertIn(('Oshi no Ko Season 2', 0), candidate_keys)

    def test_leading_release_group_produces_both_safe_candidates(self):
        parsed = parse_release_title(
            '[VCB-Studio] Yama no Susume Next Summit '
            '[Ma10p_1080p][x265_aac]'
        )
        candidate_keys = [candidate.key for candidate in parsed.candidates]

        self.assertEqual(
            ('Yama no Susume Next Summit', 0),
            candidate_keys[0],
        )
        self.assertIn(
            ('VCB-Studio Yama no Susume Next Summit', 0),
            candidate_keys,
        )

    def test_leading_group_without_technical_block_is_only_a_fallback(self):
        parsed = parse_release_title('[U2-Rip] キャンディ ボーイ')
        candidate_keys = [candidate.key for candidate in parsed.candidates]

        self.assertEqual(('U2-Rip キャンディ ボーイ', 0), candidate_keys[0])
        self.assertIn(('キャンディ ボーイ', 0), candidate_keys[1:])

        official = parse_release_title('[Oshi no Ko] Season 2')
        official_keys = [candidate.key for candidate in official.candidates]
        self.assertNotIn(('Season 2', 0), official_keys)

    def test_technical_numbers_and_checksum_do_not_pollute_title(self):
        parsed = parse_release_title(
            'Example Show [WEB-DL 2160p AV1 10bit EAC3][A1B2C3D4]'
        )

        self.assertEqual(('Example Show', 0), parsed.candidates[0].key)
        self.assertEqual(2, len(parsed.technical_tags))

    def test_adjacent_dual_audio_and_bracket_group_are_release_metadata(self):
        original = (
            'Ranma ½ (BDRip 1444x1080p x265 HEVC FLAC,AAC 2.0x2)'
            '(Dual Audio)[sxales]'
        )
        parsed = parse_release_title(original)
        candidate_keys = [candidate.key for candidate in parsed.candidates]

        self.assertEqual(('Ranma ½', 0), candidate_keys[0])
        self.assertEqual('sxales', parsed.release_group)
        self.assertIn('Dual Audio', parsed.technical_tags)
        self.assertTrue(
            any('Dual Audio' in title and 'sxales' in title for title, _ in candidate_keys)
        )


if __name__ == '__main__':
    unittest.main()
