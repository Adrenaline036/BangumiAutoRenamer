import tempfile
import unittest
from pathlib import Path
from difflib import SequenceMatcher

from src.rename.process import Rename, _is_generic_season_name


class SeasonRecognitionTests(unittest.TestCase):
    def setUp(self):
        # get_season_id does not depend on initialized Rename state.  Bypassing
        # __init__ keeps these tests offline and avoids creating configured
        # media directories.
        self.renamer = object.__new__(Rename)

    def recognize(self, path_name, seasons, titles=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            work_path = Path(temp_dir) / "organized-show"
            season_id = self.renamer.get_season_id(
                {"seasons": seasons},
                work_path,
                Path(path_name),
                titles,
            )
            created_season_dirs = sorted(
                path.name for path in work_path.glob("Season*")
            )
        return season_id, created_season_dirs

    @staticmethod
    def seasons_with_next_summit():
        return [
            {"season_number": 1, "name": "Season 1"},
            {"season_number": 4, "name": "Next Summit"},
        ]

    def test_custom_season_name_inside_release_name_selects_season_four(self):
        season_id, _ = self.recognize(
            "[Group] Yama no Susume - Next Summit [1080p HEVC]",
            self.seasons_with_next_summit(),
        )

        self.assertEqual(4, season_id)

    def test_custom_season_name_matching_is_case_insensitive(self):
        season_id, _ = self.recognize(
            "[Group] Yama no Susume - next summit [1080p]",
            self.seasons_with_next_summit(),
        )

        self.assertEqual(4, season_id)

    def test_partial_word_does_not_match_custom_season_name(self):
        season_id, _ = self.recognize(
            "[Group] Yama no Susume - Next Summitville [1080p]",
            self.seasons_with_next_summit(),
        )

        self.assertEqual(1, season_id)

    def test_explicit_season_number_has_priority(self):
        season_id, _ = self.recognize(
            "[Group] Yama no Susume S04 [1080p]",
            self.seasons_with_next_summit(),
        )

        self.assertEqual(4, season_id)

    def test_similarity_fallback_returns_candidate_season_number(self):
        # Keep the typo deliberately: it bypasses exact-name matching while
        # remaining above to_sim_max's established 0.6 similarity threshold.
        self.assertGreater(SequenceMatcher(None, "summit", "summt").ratio(), 0.6)

        season_id, _ = self.recognize(
            "Main Title Summt",
            [{"season_number": 4, "name": "Summit"}],
            [{"type": "Default", "title": "Main Title"}],
        )

        self.assertEqual(4, season_id)

    def test_similarity_result_is_independent_of_title_order(self):
        seasons = [{"season_number": 4, "name": "Alpha Arc"}]
        titles = [
            {"type": "Default", "title": "Alpha"},
            {"type": "English", "title": "Noise"},
        ]

        first_order, _ = self.recognize(
            "Alpha Arx Noise", seasons, titles
        )
        reversed_order, _ = self.recognize(
            "Alpha Arx Noise", seasons, list(reversed(titles))
        )

        self.assertEqual(4, first_order)
        self.assertEqual(first_order, reversed_order)

    def test_low_similarity_keeps_default_season_one(self):
        season_id, _ = self.recognize(
            "Main Title Completely Unrelated",
            [{"season_number": 4, "name": "Summit"}],
            [{"type": "Default", "title": "Main Title"}],
        )

        self.assertEqual(1, season_id)

    def test_first_custom_season_in_tmdb_order_wins_when_both_match(self):
        season_id, _ = self.recognize(
            "[Group] Next Summit [1080p]",
            [
                {"season_number": 4, "name": "Next Summit"},
                {"season_number": 5, "name": "Summit"},
            ],
        )

        self.assertEqual(4, season_id)

    def test_recognition_does_not_create_candidate_directories(self):
        _, created_season_dirs = self.recognize(
            "[Group] Yama no Susume - Next Summit [1080p HEVC]",
            self.seasons_with_next_summit(),
        )

        self.assertEqual([], created_season_dirs)

    def test_generic_season_names_are_detected_consistently(self):
        for season_name in ("Season 1", "season2", " SEASON 10 "):
            with self.subTest(season_name=season_name):
                self.assertTrue(_is_generic_season_name(season_name))

        for season_name in ("Next Summit", "Season Finale"):
            with self.subTest(season_name=season_name):
                self.assertFalse(_is_generic_season_name(season_name))


if __name__ == "__main__":
    unittest.main()
