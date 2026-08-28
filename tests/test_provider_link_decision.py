import hashlib
import inspect
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from src.pages.plan_review_page import PlanReviewDialog, provider_link_review_summary
from src.rename.decision_state import DecisionState, DecisionStateStore
from src.rename.provider_link_catalog import ProviderLinkCatalog, ProviderLinkMapping
from src.rename.provider_link_decision import stage_provider_link_decision
from src.rename.provider_link_resolver import resolve_provider_link
from src.rename.rename_plan import RenamePlan


def _fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _subject(provider_id, title, count, date):
    return {
        "namespace": "bangumi",
        "entity_type": "subject",
        "provider_id": provider_id,
        "title": title,
        "original_title": title,
        "episode_count": count,
        "air_date": date,
        "platform": "TV",
    }


def _tmdb(title, seasons):
    return {
        "namespace": "tmdb",
        "entity_type": "tv",
        "provider_id": "4242",
        "title": title,
        "original_title": title,
        "seasons": tuple(seasons),
    }


class ProviderLinkG3DecisionTests(unittest.TestCase):
    def test_common_franchise_prefix_is_not_identity_evidence(self):
        result = resolve_provider_link(
            selected_provider={
                "namespace": "bangumi", "entity_type": "subject", "provider_id": "1"
            },
            bangumi_subjects=(_subject("1", "Fate Zero", 12, "2020-01-01"),),
            tmdb_candidates=(
                _tmdb(
                    "Fate",
                    ({"season_number": 1, "episode_count": 12, "air_date": "2020-01-01"},),
                ),
            ),
        )

        self.assertEqual("L4", result.automation_level)
        self.assertIn("tmdb_title_evidence_mismatch", result.reason_codes)

    def test_explicit_season_suffix_can_reduce_to_the_same_series_root(self):
        result = resolve_provider_link(
            selected_provider={
                "namespace": "bangumi", "entity_type": "subject", "provider_id": "1"
            },
            bangumi_subjects=(
                _subject("1", "Example Series Season 2", 12, "2020-01-01"),
            ),
            tmdb_candidates=(
                _tmdb(
                    "Example Series",
                    ({"season_number": 1, "episode_count": 12, "air_date": "2020-01-01"},),
                ),
            ),
        )

        self.assertEqual("L2", result.automation_level)

    def test_unique_materialized_anchor_reuses_series_as_l1(self):
        snapshot = {"fixture": "confirmed-first-season"}
        series_key = f"series-{uuid.uuid4()}"
        anchor = ProviderLinkMapping.from_dict(
            {
                "schema_version": 1,
                "mapping_id": f"provider-link-{uuid.uuid4()}",
                "revision": 3,
                "local_series_key": series_key,
                "status": "materialized_confirmed",
                "provider_edges": (
                    {"namespace": "tmdb", "entity_type": "tv", "provider_id": "4242", "role": "series"},
                    {"namespace": "bangumi", "entity_type": "subject", "provider_id": "1", "role": "season"},
                ),
                "season_edges": (
                    {"namespace": "bangumi", "entity_type": "subject", "provider_id": "1", "tmdb_tv_id": "4242", "season_number": 1},
                ),
                "evidence_snapshot": snapshot,
                "evidence_fingerprint": _fingerprint(snapshot),
            }
        )
        subjects = (
            _subject("1", "Example Series", 12, "2020-01-01"),
            _subject("2", "Example Series Season 2", 12, "2021-01-01"),
        )
        result = resolve_provider_link(
            selected_provider={
                "namespace": "bangumi", "entity_type": "subject", "provider_id": "2"
            },
            existing_links=(anchor,),
            bangumi_subjects=subjects,
            bangumi_relations=({"from": "1", "to": "2", "relation": "续集"},),
            tmdb_candidates=(
                _tmdb(
                    "Example Series",
                    (
                        {"season_number": 1, "episode_count": 12, "air_date": "2020-01-01"},
                        {"season_number": 2, "episode_count": 12, "air_date": "2021-01-01"},
                    ),
                ),
            ),
        )

        self.assertEqual("L1", result.automation_level)
        self.assertEqual(series_key, result.proposed_series_key)
        self.assertEqual(anchor.mapping_id, result.anchor_mapping_id)

    def test_catalog_only_call_path_can_discover_l1_anchor(self):
        snapshot = {"fixture": "catalog-anchor"}
        anchor = ProviderLinkMapping.from_dict(
            {
                "schema_version": 1,
                "mapping_id": f"provider-link-{uuid.uuid4()}",
                "revision": 0,
                "local_series_key": f"series-{uuid.uuid4()}",
                "status": "materialized_confirmed",
                "provider_edges": (
                    {"namespace": "tmdb", "entity_type": "tv", "provider_id": "4242", "role": "series"},
                    {"namespace": "bangumi", "entity_type": "subject", "provider_id": "1", "role": "season"},
                ),
                "season_edges": (
                    {"namespace": "bangumi", "entity_type": "subject", "provider_id": "1", "tmdb_tv_id": "4242", "season_number": 1},
                ),
                "evidence_snapshot": snapshot,
                "evidence_fingerprint": _fingerprint(snapshot),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            catalog = ProviderLinkCatalog(Path(directory) / "provider-links")
            catalog.save(anchor, expected_revision=-1)
            result = resolve_provider_link(
                selected_provider={"namespace": "bangumi", "entity_type": "subject", "provider_id": "2"},
                provider_link_catalog=catalog,
                bangumi_subjects=(
                    _subject("1", "Example Series", 12, "2020-01-01"),
                    _subject("2", "Example Series Season 2", 12, "2021-01-01"),
                ),
                bangumi_relations=({"from": "1", "to": "2", "relation": "续集"},),
                tmdb_candidates=(
                    _tmdb(
                        "Example Series",
                        (
                            {"season_number": 1, "episode_count": 12, "air_date": "2020-01-01"},
                            {"season_number": 2, "episode_count": 12, "air_date": "2021-01-01"},
                        ),
                    ),
                ),
            )

        self.assertEqual("L1", result.automation_level)
        self.assertEqual(anchor.local_series_key, result.series_key)

    def test_l2_decision_freezes_exact_snapshot_into_plan_for_review(self):
        resolution = resolve_provider_link(
            selected_provider={
                "namespace": "bangumi", "entity_type": "subject", "provider_id": "1"
            },
            bangumi_subjects=(_subject("1", "Example Series", 12, "2020-01-01"),),
            tmdb_candidates=(
                _tmdb(
                    "Example Series",
                    ({"season_number": 1, "episode_count": 12, "air_date": "2020-01-01"},),
                ),
            ),
        )
        staged = stage_provider_link_decision(DecisionState(task_id="g3-stage"), resolution)
        with tempfile.TemporaryDirectory() as directory:
            projected = DecisionStateStore(Path(directory)).project_plan(
                staged,
                RenamePlan(task_id=staged.task_id, source_root=Path(directory), entries=()),
            )

        binding = staged.to_dict()["provider_link_binding"]
        self.assertEqual("inferred", binding["status"])
        self.assertEqual(
            binding,
            projected.to_dict()["metadata"]["provider_link_snapshot"],
        )
        summary = provider_link_review_summary(projected.metadata["provider_link_snapshot"])
        self.assertEqual("L2", summary["level"])
        self.assertIn("尚未执行文件整理", summary["pending_notice"])
        self.assertIn("Bangumi #1 → 第 1 季", summary["assignments"])

    def test_plan_review_header_exposes_mapping_and_non_materialized_notice(self):
        source = inspect.getsource(PlanReviewDialog._render_header)

        self.assertIn("已匹配作品季度", source)
        self.assertNotIn("证据路径", source)
        self.assertNotIn("provider_link['level']", source)
        self.assertIn("pending_notice", source)

    def test_assisted_or_conflicted_result_cannot_be_staged_automatically(self):
        conflict = resolve_provider_link(
            selected_provider={
                "namespace": "tmdb", "entity_type": "movie", "provider_id": "278"
            }
        )
        with self.assertRaisesRegex(ValueError, "不能进入整理计划审核"):
            stage_provider_link_decision(DecisionState(task_id="ordinary-movie"), conflict)


if __name__ == "__main__":
    unittest.main()
