import unittest

from src.rename.provider_link_resolver import resolve_provider_link


def _provider(namespace: str, entity_type: str, provider_id: str) -> dict:
    return {
        "namespace": namespace,
        "entity_type": entity_type,
        "provider_id": provider_id,
    }


class ProviderLinkResolverG2Tests(unittest.TestCase):
    def test_reciprocal_prequel_and_sequel_records_are_one_edge_not_branch(self):
        subjects = (
            {
                **_provider("bangumi", "subject", "101"),
                "title": "Example",
                "original_title": "Example",
                "air_date": "2020-01-01",
                "episode_count": 12,
                "platform": "TV",
            },
            {
                **_provider("bangumi", "subject", "102"),
                "title": "Example Season 2",
                "original_title": "Example Season 2",
                "air_date": "2021-01-01",
                "episode_count": 12,
                "platform": "TV",
            },
        )
        result = resolve_provider_link(
            selected_provider=_provider("bangumi", "subject", "102"),
            bangumi_subjects=subjects,
            bangumi_relations=(
                {"from": "101", "to": "102", "relation": "续集"},
                {"from": "102", "to": "101", "relation": "前传"},
            ),
            tmdb_candidates=(
                {
                    **_provider("tmdb", "tv", "9001"),
                    "title": "Example",
                    "original_title": "Example",
                    "seasons": (
                        {"season_number": 1, "episode_count": 12},
                        {"season_number": 2, "episode_count": 12},
                    ),
                },
            ),
        )

        self.assertEqual("L2", result.automation_level)
        self.assertEqual({"101": 1, "102": 2}, dict(result.season_assignments))
        self.assertNotIn("branching_sequel_graph", result.reason_codes)

    def test_single_unrelated_tmdb_candidate_fails_closed(self):
        result = resolve_provider_link(
            selected_provider=_provider("bangumi", "subject", "101"),
            bangumi_subjects=(
                {
                    **_provider("bangumi", "subject", "101"),
                    "title": "Alpha",
                    "original_title": "Alpha",
                    "air_date": "2020-01-01",
                    "episode_count": 12,
                    "platform": "TV",
                },
            ),
            bangumi_relations=(),
            tmdb_candidates=(
                {
                    **_provider("tmdb", "tv", "9002"),
                    "title": "Completely Unrelated",
                    "original_title": "Completely Unrelated",
                    "seasons": ({"season_number": 1, "episode_count": 12},),
                },
            ),
        )

        self.assertEqual("L4", result.automation_level)
        self.assertEqual({}, dict(result.season_assignments))
        self.assertIn("tmdb_title_evidence_mismatch", result.reason_codes)

    def test_g2_consumes_frozen_evidence_without_scheduling_network(self):
        calls = []

        def must_not_fetch(*_args, **_kwargs):
            calls.append(True)
            raise AssertionError("G2 pure resolver must not schedule network")

        result = resolve_provider_link(
            selected_provider=_provider("bangumi", "subject", "101"),
            bangumi_subjects=(
                {
                    **_provider("bangumi", "subject", "101"),
                    "title": "Example",
                    "original_title": "Example",
                    "air_date": "2020-01-01",
                    "episode_count": 12,
                    "platform": "TV",
                },
            ),
            bangumi_relations=(),
            tmdb_candidates=(
                {
                    **_provider("tmdb", "tv", "9001"),
                    "title": "Example",
                    "original_title": "Example",
                    "seasons": ({"season_number": 1, "episode_count": 12},),
                },
            ),
            fetch_bangumi=must_not_fetch,
            fetch_tmdb=must_not_fetch,
        )

        self.assertEqual("L2", result.automation_level)
        self.assertEqual([], calls)

    def test_ordinary_tmdb_movie_without_materialized_link_does_not_enter_tv_crosswalk(self):
        result = resolve_provider_link(
            selected_provider=_provider("tmdb", "movie", "278"),
            tmdb_candidates=(
                {
                    **_provider("tmdb", "movie", "278"),
                    "title": "The Shawshank Redemption",
                },
            ),
        )

        self.assertEqual("L4", result.automation_level)
        self.assertIsNone(result.series_key)
        self.assertIsNone(result.season_number)
        self.assertIn(
            "crosswalk_inference_requires_bangumi_subject", result.reason_codes
        )


if __name__ == "__main__":
    unittest.main()
