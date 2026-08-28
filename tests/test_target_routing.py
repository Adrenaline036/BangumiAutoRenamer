import unittest
from pathlib import Path

from src.rename.target_routing import (
    TargetRootSnapshot,
    TargetRoutingError,
    resolve_target_root,
)


class TargetRoutingTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = TargetRootSnapshot.capture({
            "bangumi_path": "/volume1/Media/Jellyfin/TV",
            "movie_path": "/volume1/Media/Jellyfin/Movies",
            "anime_path": "/volume1/Media/Jellyfin/Anime",
            "anime_movie_path": "/volume1/Media/Jellyfin/AnimeMovies",
        })

    def test_routes_all_four_domain_layout_combinations(self):
        self.assertEqual(
            Path("/volume1/Media/Jellyfin/AnimeMovies"),
            resolve_target_root("animation", "movie_continuous", self.snapshot),
        )
        self.assertEqual(
            Path("/volume1/Media/Jellyfin/Anime"),
            resolve_target_root("animation", "tv_episodes", self.snapshot),
        )
        self.assertEqual(
            Path("/volume1/Media/Jellyfin/Movies"),
            resolve_target_root("general", "movie_continuous", self.snapshot),
        )
        self.assertEqual(
            Path("/volume1/Media/Jellyfin/TV"),
            resolve_target_root("general", "tv_episodes", self.snapshot),
        )

    def test_snapshot_fingerprint_is_stable_and_changes_with_config(self):
        same = TargetRootSnapshot.from_dict(self.snapshot.to_dict())
        self.assertEqual(self.snapshot.fingerprint, same.fingerprint)
        changed = TargetRootSnapshot.capture({
            **self.snapshot.to_dict(),
            "anime_movie_path": "/volume1/Elsewhere/AnimeMovies",
        })
        self.assertNotEqual(self.snapshot.fingerprint, changed.fingerprint)

    def test_rejects_unknown_domain_instead_of_falling_back_to_movies(self):
        with self.assertRaises(TargetRoutingError):
            resolve_target_root("unknown", "movie_continuous", self.snapshot)


if __name__ == "__main__":
    unittest.main()
