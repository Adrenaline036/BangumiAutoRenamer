"""G0 red contracts for Bangumi -> TMDB season crosswalk automation.

These tests intentionally specify the accepted behaviour before production
implementation.  Missing APIs must surface as assertion failures, never import
or fixture errors.  G0 may not modify media, catalogs, or production code.
"""

import hashlib
import importlib
import json
import tempfile
import unittest
from pathlib import Path

from src.rename.decision_state import DecisionState, DecisionStateStore
from src.rename.rename_plan import RenamePlan


SERIES_KEY = "series-12345678-1234-1234-1234-123456789abc"
MAPPING_ID = "provider-link-12345678-1234-1234-1234-123456789abc"
TMDB_TV_ID = "53918"

# Curated real-world corpus.  The URLs are evidence anchors for refreshing the
# fixture when either community database changes; tests never call the network.
REAL_CORPUS_SOURCES = {
    "yama_no_susume": (
        "https://bgm.tv/subject/41658",
        "https://bgm.tv/subject/86670",
        "https://bgm.tv/subject/216310",
        "https://bgm.tv/subject/290980",
        "https://www.themoviedb.org/tv/53918/seasons",
    ),
    "yuru_camp": (
        "https://bgm.tv/subject/148732/relations",
        "https://bgm.tv/subject/262897",
        "https://bgm.tv/subject/405785",
        "https://www.themoviedb.org/tv/76075/seasons",
    ),
    "attack_on_titan_season_3_split_cour": (
        "https://bgm.tv/subject/217300",
        "https://bgm.tv/subject/263750",
        "https://www.themoviedb.org/tv/1429/seasons",
    ),
}


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _provider(namespace: str, entity_type: str, provider_id: str) -> dict:
    return {
        "namespace": namespace,
        "entity_type": entity_type,
        "provider_id": str(provider_id),
    }


def _yama_subjects() -> tuple[dict, ...]:
    return (
        {
            **_provider("bangumi", "subject", "41658"),
            "title": "向山进发",
            "original_title": "ヤマノススメ",
            "air_date": "2013-01-02",
            "episode_count": 13,
            "platform": "TV",
        },
        {
            **_provider("bangumi", "subject", "86670"),
            "title": "向山进发 第二季",
            "original_title": "ヤマノススメ セカンドシーズン",
            "air_date": "2014-07-09",
            "episode_count": 24,
            "platform": "TV",
        },
        {
            **_provider("bangumi", "subject", "216310"),
            "title": "向山进发 第三季",
            "original_title": "ヤマノススメ サードシーズン",
            "air_date": "2018-07-02",
            "episode_count": 13,
            "platform": "TV",
        },
        {
            **_provider("bangumi", "subject", "290980"),
            "title": "向山进发 Next Summit",
            "original_title": "ヤマノススメ Next Summit",
            "air_date": "2022-10-04",
            "episode_count": 12,
            "platform": "TV",
        },
    )


def _yama_relations(*, branch: bool = False) -> tuple[dict, ...]:
    relations = [
        {"from": "41658", "to": "86670", "relation": "续集"},
        {"from": "86670", "to": "216310", "relation": "续集"},
        {"from": "216310", "to": "290980", "relation": "续集"},
        # This real Bangumi side story must never consume a season number.
        {"from": "86670", "to": "216311", "relation": "番外篇"},
    ]
    if branch:
        relations.append(
            {"from": "216310", "to": "999999", "relation": "续集"}
        )
    return tuple(relations)


def _tmdb_yama() -> dict:
    return {
        **_provider("tmdb", "tv", TMDB_TV_ID),
        "title": "Encouragement of Climb",
        "original_title": "ヤマノススメ",
        "first_air_date": "2013-01-03",
        "seasons": (
            {"season_number": 1, "air_date": "2013-01-03", "episode_count": 13},
            {"season_number": 2, "air_date": "2014-07-09", "episode_count": 24},
            {"season_number": 3, "air_date": "2018-07-02", "episode_count": 13},
            {"season_number": 4, "air_date": "2022-10-05", "episode_count": 12},
        ),
    }


def _real_resolution_corpus() -> tuple[dict, ...]:
    """Verified shapes that are materially different, not title variants."""
    return (
        {
            "name": "yama_linear_four_seasons",
            "selected_id": "290980",
            "subjects": _yama_subjects(),
            "relations": _yama_relations(),
            "tmdb": _tmdb_yama(),
            "expected_level": "L2",
            "expected_assignments": {
                "41658": 1,
                "86670": 2,
                "216310": 3,
                "290980": 4,
            },
            "excluded_ids": {"216311"},
            "sources": REAL_CORPUS_SOURCES["yama_no_susume"],
        },
        {
            "name": "yuru_camp_linear_with_movie_and_spinoff",
            "selected_id": "405785",
            "subjects": (
                {
                    **_provider("bangumi", "subject", "148732"),
                    "title": "摇曳露营△",
                    "original_title": "ゆるキャン△",
                    "air_date": "2018-01-04",
                    "episode_count": 12,
                    "platform": "TV",
                },
                {
                    **_provider("bangumi", "subject", "262897"),
                    "title": "摇曳露营△ 第二季",
                    "original_title": "ゆるキャン△ SEASON 2",
                    "air_date": "2021-01-07",
                    "episode_count": 13,
                    "platform": "TV",
                },
                {
                    **_provider("bangumi", "subject", "405785"),
                    "title": "摇曳露营△ 第三季",
                    "original_title": "ゆるキャン△ SEASON３",
                    "air_date": "2024-04-04",
                    "episode_count": 12,
                    "platform": "TV",
                },
                {
                    **_provider("bangumi", "subject", "262898"),
                    "title": "电影 摇曳露营△",
                    "original_title": "映画 ゆるキャン△",
                    "air_date": "2022-07-01",
                    "episode_count": 1,
                    "platform": "剧场版",
                },
                {
                    **_provider("bangumi", "subject", "266070"),
                    "title": "房间露营△",
                    "original_title": "へやキャン△",
                    "air_date": "2020-01-06",
                    "episode_count": 12,
                    "platform": "WEB",
                },
            ),
            "relations": (
                {"from": "148732", "to": "262897", "relation": "续集"},
                {"from": "262897", "to": "405785", "relation": "续集"},
                {"from": "405785", "to": "262898", "relation": "番外篇"},
                {"from": "148732", "to": "266070", "relation": "番外篇"},
            ),
            "tmdb": {
                **_provider("tmdb", "tv", "76075"),
                "title": "Laid-Back Camp",
                "original_title": "ゆるキャン△",
                "first_air_date": "2018-01-04",
                "seasons": (
                    {"season_number": 0, "episode_count": 8},
                    {"season_number": 1, "episode_count": 12},
                    {"season_number": 2, "episode_count": 13},
                    {"season_number": 3, "episode_count": 12},
                ),
            },
            "expected_level": "L2",
            "expected_assignments": {"148732": 1, "262897": 2, "405785": 3},
            "excluded_ids": {"262898", "266070"},
            "sources": REAL_CORPUS_SOURCES["yuru_camp"],
        },
        {
            "name": "attack_on_titan_two_bgm_entries_share_tmdb_season_3",
            "selected_id": "263750",
            "subjects": (
                {
                    **_provider("bangumi", "subject", "217300"),
                    "title": "进击的巨人 第三季",
                    "original_title": "進撃の巨人 Season 3",
                    "air_date": "2018-07-22",
                    "episode_count": 12,
                    "platform": "TV",
                },
                {
                    **_provider("bangumi", "subject", "263750"),
                    "title": "进击的巨人 第三季 Part.2",
                    "original_title": "進撃の巨人 Season 3 Part.2",
                    "air_date": "2019-04-28",
                    "episode_count": 10,
                    "platform": "TV",
                },
            ),
            "relations": (
                {"from": "217300", "to": "263750", "relation": "续集"},
            ),
            "tmdb": {
                **_provider("tmdb", "tv", "1429"),
                "title": "Attack on Titan",
                "original_title": "進撃の巨人",
                "first_air_date": "2013-04-07",
                "seasons": (
                    {"season_number": 3, "air_date": "2018-07-23", "episode_count": 22},
                ),
            },
            "expected_level": "L3",
            "expected_assignments": {"217300": 3, "263750": 3},
            "excluded_ids": set(),
            "sources": REAL_CORPUS_SOURCES["attack_on_titan_season_3_split_cour"],
        },
    )


def _mapping_payload(status: str = "inferred") -> dict:
    evidence = {
        "bangumi_subjects": [item["provider_id"] for item in _yama_subjects()],
        "bangumi_relations": list(_yama_relations()),
        "tmdb_tv_id": TMDB_TV_ID,
        "ranking_version": "crosswalk-v1",
    }
    return {
        "schema_version": 1,
        "mapping_id": MAPPING_ID,
        "revision": 0,
        "local_series_key": SERIES_KEY,
        "status": status,
        "provider_edges": (
            {
                **_provider("tmdb", "tv", TMDB_TV_ID),
                "role": "series",
            },
        ),
        "season_edges": tuple(
            {
                **_provider("bangumi", "subject", subject_id),
                "tmdb_tv_id": TMDB_TV_ID,
                "season_number": season_number,
            }
            for season_number, subject_id in enumerate(
                ("41658", "86670", "216310", "290980"), start=1
            )
        ),
        "evidence_snapshot": evidence,
        "evidence_fingerprint": _fingerprint(evidence),
        "confirmed_by": None,
        "confirmed_at": None,
        "materialized_at": None,
    }


class _ApiContractCase(unittest.TestCase):
    def _require_api(self, module_name: str):
        try:
            api = importlib.import_module(module_name)
        except ModuleNotFoundError:
            api = None
        self.assertIsNotNone(
            api,
            f"G1 must implement {module_name}; G0 intentionally remains red",
        )
        return api


class ProviderLinkResolutionG0Contracts(_ApiContractCase):
    def test_verified_real_corpus_resolves_without_assuming_one_bgm_item_per_season(self):
        api = self._require_api("src.rename.provider_link_resolver")
        for fixture in _real_resolution_corpus():
            with self.subTest(corpus=fixture["name"]):
                result = api.resolve_provider_link(
                    selected_provider=_provider(
                        "bangumi", "subject", fixture["selected_id"]
                    ),
                    bangumi_subjects=fixture["subjects"],
                    bangumi_relations=fixture["relations"],
                    tmdb_candidates=(fixture["tmdb"],),
                    existing_links=(),
                )

                self.assertEqual(fixture["expected_level"], result.automation_level)
                self.assertEqual("inferred", result.status)
                self.assertEqual(
                    fixture["tmdb"]["provider_id"], result.tmdb_tv_id
                )
                self.assertRegex(
                    result.proposed_series_key, r"^series-[0-9a-f-]{36}$"
                )
                self.assertNotIn(
                    fixture["tmdb"]["provider_id"], result.proposed_series_key
                )
                self.assertEqual(
                    fixture["expected_assignments"],
                    dict(result.season_assignments),
                )
                for excluded_id in fixture["excluded_ids"]:
                    self.assertNotIn(excluded_id, dict(result.season_assignments))
                self.assertTrue(result.requires_plan_review)
                self.assertGreaterEqual(len(fixture["sources"]), 3)

    def test_real_side_story_is_excluded_from_season_ordinal(self):
        api = self._require_api("src.rename.provider_link_resolver")

        result = api.resolve_provider_link(
            selected_provider=_provider("bangumi", "subject", "216311"),
            bangumi_subjects=_yama_subjects()
            + (
                {
                    **_provider("bangumi", "subject", "216311"),
                    "title": "向山进发 回忆的礼物",
                    "air_date": "2017-10-28",
                    "episode_count": 2,
                    "platform": "OVA",
                },
            ),
            bangumi_relations=_yama_relations(),
            tmdb_candidates=(_tmdb_yama(),),
            existing_links=(),
        )

        self.assertIn(result.automation_level, {"L3", "L4"})
        self.assertNotIn("216311", dict(result.season_assignments))
        self.assertIn("non_mainline_relation", result.reason_codes)

    def test_linear_series_can_use_dates_when_provider_episode_totals_differ(self):
        api = self._require_api("src.rename.provider_link_resolver")
        tmdb = {
            **_tmdb_yama(),
            "seasons": (
                {"season_number": 1, "air_date": "2013-01-03", "episode_count": 15},
                {"season_number": 2, "air_date": "2014-07-09", "episode_count": 25},
                {"season_number": 3, "air_date": "2018-07-02", "episode_count": 13},
                {"season_number": 4, "air_date": "2022-10-05", "episode_count": 12},
            ),
        }

        result = api.resolve_provider_link(
            selected_provider=_provider("bangumi", "subject", "290980"),
            bangumi_subjects=_yama_subjects(),
            bangumi_relations=_yama_relations(),
            tmdb_candidates=(tmdb,),
            existing_links=(),
        )

        self.assertEqual(
            {"41658": 1, "86670": 2, "216310": 3, "290980": 4},
            dict(result.season_assignments),
        )
        self.assertIn("linear_one_to_one_air_date_locked", result.reason_codes)

    def test_branching_sequel_graph_fails_closed_without_assignments(self):
        api = self._require_api("src.rename.provider_link_resolver")

        result = api.resolve_provider_link(
            selected_provider=_provider("bangumi", "subject", "290980"),
            bangumi_subjects=_yama_subjects()
            + (
                {
                    **_provider("bangumi", "subject", "999999"),
                    "title": "Synthetic Branch",
                    "air_date": "2022-10-04",
                    "episode_count": 12,
                    "platform": "TV",
                },
            ),
            bangumi_relations=_yama_relations(branch=True),
            tmdb_candidates=(_tmdb_yama(),),
            existing_links=(),
        )

        self.assertEqual("L4", result.automation_level)
        self.assertEqual("conflicted", result.status)
        self.assertEqual({}, dict(result.season_assignments))
        self.assertIn("branching_sequel_graph", result.reason_codes)

    def test_cyclic_or_bidirectional_sequel_graph_fails_closed(self):
        api = self._require_api("src.rename.provider_link_resolver")
        cyclic = _yama_relations() + (
            {"from": "290980", "to": "41658", "relation": "续集"},
        )

        result = api.resolve_provider_link(
            selected_provider=_provider("bangumi", "subject", "290980"),
            bangumi_subjects=_yama_subjects(),
            bangumi_relations=cyclic,
            tmdb_candidates=(_tmdb_yama(),),
            existing_links=(),
        )

        self.assertEqual("L4", result.automation_level)
        self.assertEqual("conflicted", result.status)
        self.assertEqual({}, dict(result.season_assignments))
        self.assertIn("cyclic_sequel_graph", result.reason_codes)

    def test_missing_tmdb_season_does_not_shift_later_season_ordinals(self):
        api = self._require_api("src.rename.provider_link_resolver")
        missing_season_three = {
            **_tmdb_yama(),
            "seasons": (
                {"season_number": 1, "air_date": "2013-01-03", "episode_count": 13},
                {"season_number": 2, "air_date": "2014-07-09", "episode_count": 24},
                {"season_number": 4, "air_date": "2022-10-05", "episode_count": 12},
            ),
        }

        result = api.resolve_provider_link(
            selected_provider=_provider("bangumi", "subject", "290980"),
            bangumi_subjects=_yama_subjects(),
            bangumi_relations=_yama_relations(),
            tmdb_candidates=(missing_season_three,),
            existing_links=(),
        )

        self.assertIn(result.automation_level, {"L3", "L4"})
        self.assertNotEqual(3, dict(result.season_assignments).get("290980"))
        self.assertIn("tmdb_season_gap", result.reason_codes)
        self.assertTrue(result.requires_plan_review)

    def test_same_title_reboot_collision_requires_manual_provider_identity(self):
        api = self._require_api("src.rename.provider_link_resolver")
        original = {
            **_provider("bangumi", "subject", "700001"),
            "title": "同名作品",
            "original_title": "Same Title",
            "air_date": "1999-01-01",
            "episode_count": 12,
            "platform": "TV",
        }
        reboot = {
            **_provider("bangumi", "subject", "700002"),
            "title": "同名作品",
            "original_title": "Same Title",
            "air_date": "2024-01-01",
            "episode_count": 12,
            "platform": "TV",
        }
        candidates = (
            {
                **_provider("tmdb", "tv", "800001"),
                "title": "Same Title",
                "first_air_date": "1999-01-01",
                "seasons": ({"season_number": 1, "episode_count": 12},),
            },
            {
                **_provider("tmdb", "tv", "800002"),
                "title": "Same Title",
                "first_air_date": "2024-01-01",
                "seasons": ({"season_number": 1, "episode_count": 12},),
            },
        )

        result = api.resolve_provider_link(
            selected_provider=_provider("bangumi", "subject", "700002"),
            bangumi_subjects=(original, reboot),
            bangumi_relations=(),
            tmdb_candidates=candidates,
            existing_links=(),
        )

        self.assertEqual("L4", result.automation_level)
        self.assertEqual("conflicted", result.status)
        self.assertIsNone(result.tmdb_tv_id)
        self.assertIn("same_title_reboot_collision", result.reason_codes)

    def test_materialized_exact_link_is_l0_offline_and_never_calls_providers(self):
        api = self._require_api("src.rename.provider_link_resolver")
        catalog_api = self._require_api("src.rename.provider_link_catalog")
        mapping = catalog_api.ProviderLinkMapping.from_dict(
            _mapping_payload("materialized_confirmed")
        )
        provider_calls = []

        def provider_must_not_run(*_args, **_kwargs):
            provider_calls.append(True)
            raise AssertionError("L0 exact resolution must be offline")

        result = api.resolve_provider_link(
            selected_provider=_provider("bangumi", "subject", "290980"),
            existing_links=(mapping,),
            fetch_bangumi=provider_must_not_run,
            fetch_tmdb=provider_must_not_run,
        )

        self.assertEqual("L0", result.automation_level)
        self.assertEqual(SERIES_KEY, result.series_key)
        self.assertEqual(4, result.season_number)
        self.assertEqual([], provider_calls)


class ProviderLinkStateG0Contracts(_ApiContractCase):
    def test_mapping_roundtrip_keeps_many_to_many_ready_edges_and_status(self):
        api = self._require_api("src.rename.provider_link_catalog")
        mapping = api.ProviderLinkMapping.from_dict(_mapping_payload())
        restored = api.ProviderLinkMapping.from_dict(mapping.to_dict())

        self.assertEqual("inferred", restored.status)
        self.assertEqual(4, len(restored.season_edges))
        self.assertFalse(restored.reusable_offline)
        self.assertEqual(mapping.evidence_fingerprint, restored.evidence_fingerprint)

    def test_plan_approval_promotes_only_to_user_confirmed(self):
        api = self._require_api("src.rename.provider_link_transaction")
        catalog_api = self._require_api("src.rename.provider_link_catalog")
        mapping = catalog_api.ProviderLinkMapping.from_dict(_mapping_payload())

        transition = api.plan_materialization_transition(
            mapping,
            plan_approved=True,
            transfer_started=False,
            ledger_status=None,
            series_catalog_registered=False,
            provider_link_catalog_registered=False,
            recovery_pending=False,
        )

        self.assertEqual("user_confirmed", transition.mapping_status)
        self.assertFalse(transition.reusable_offline)
        self.assertFalse(transition.invoke_transfer)

    def test_only_closed_execution_and_both_catalogs_become_materialized(self):
        api = self._require_api("src.rename.provider_link_transaction")
        catalog_api = self._require_api("src.rename.provider_link_catalog")
        mapping = catalog_api.ProviderLinkMapping.from_dict(
            _mapping_payload("user_confirmed")
        )

        transition = api.plan_materialization_transition(
            mapping,
            plan_approved=True,
            transfer_started=True,
            ledger_status="completed",
            series_catalog_registered=True,
            provider_link_catalog_registered=True,
            recovery_pending=False,
        )

        self.assertEqual("materialized_confirmed", transition.mapping_status)
        self.assertTrue(transition.reusable_offline)
        self.assertEqual("completed", transition.task_status)

    def test_four_incomplete_windows_never_become_materialized_or_retransfer(self):
        api = self._require_api("src.rename.provider_link_transaction")
        catalog_api = self._require_api("src.rename.provider_link_catalog")
        mapping = catalog_api.ProviderLinkMapping.from_dict(
            _mapping_payload("user_confirmed")
        )
        windows = (
            {
                "name": "approved_not_executed",
                "transfer_started": False,
                "ledger_status": None,
                "series_catalog_registered": False,
                "provider_link_catalog_registered": False,
                "recovery_pending": False,
            },
            {
                "name": "execution_failed",
                "transfer_started": True,
                "ledger_status": "failed",
                "series_catalog_registered": False,
                "provider_link_catalog_registered": False,
                "recovery_pending": False,
            },
            {
                "name": "series_catalog_failed",
                "transfer_started": True,
                "ledger_status": "completed",
                "series_catalog_registered": False,
                "provider_link_catalog_registered": False,
                "recovery_pending": True,
            },
            {
                "name": "provider_link_catalog_failed",
                "transfer_started": True,
                "ledger_status": "completed",
                "series_catalog_registered": True,
                "provider_link_catalog_registered": False,
                "recovery_pending": True,
            },
        )
        for window in windows:
            with self.subTest(window=window["name"]):
                transition = api.plan_materialization_transition(
                    mapping,
                    plan_approved=True,
                    **{key: value for key, value in window.items() if key != "name"},
                )
                self.assertNotEqual(
                    "materialized_confirmed", transition.mapping_status
                )
                self.assertFalse(transition.reusable_offline)
                if window["ledger_status"] == "completed":
                    self.assertEqual("catalog_update_pending", transition.task_status)
                    self.assertFalse(transition.invoke_transfer)

    def test_decision_and_plan_freeze_the_same_crosswalk_fingerprint(self):
        binding = {
            "mapping_id": MAPPING_ID,
            "revision": 0,
            "evidence_fingerprint": _mapping_payload()["evidence_fingerprint"],
            "status": "user_confirmed",
        }
        raw = DecisionState(task_id="crosswalk-projection").to_dict()
        raw["provider_link_binding"] = binding
        restored = DecisionState.from_dict(raw)

        self.assertEqual(
            binding, restored.to_dict().get("provider_link_binding")
        )
        with tempfile.TemporaryDirectory() as directory:
            store = DecisionStateStore(Path(directory) / "decision")
            projected = store.project_plan(
                restored,
                RenamePlan(
                    task_id=restored.task_id,
                    source_root=Path(directory) / "source",
                    entries=(),
                ),
            )
        self.assertEqual(
            binding,
            dict(projected.metadata.get("provider_link_snapshot") or {}),
        )

    def test_crosswalk_fingerprint_drift_blocks_before_transfer(self):
        api = self._require_api("src.rename.provider_link_transaction")

        self.assertTrue(
            api.provider_link_blocks_execution(
                current_mapping_id=MAPPING_ID,
                current_revision=1,
                current_fingerprint="new-fingerprint",
                plan_mapping_id=MAPPING_ID,
                plan_revision=0,
                plan_fingerprint="old-fingerprint",
            )
        )


class ProviderLinkTransactionG0Contracts(_ApiContractCase):
    def test_stale_revision_cannot_overwrite_a_concurrent_mapping(self):
        api = self._require_api("src.rename.provider_link_catalog")
        with tempfile.TemporaryDirectory() as directory:
            catalog = api.ProviderLinkCatalog(Path(directory) / "provider_links")
            created = catalog.save(
                api.ProviderLinkMapping.from_dict(_mapping_payload()),
                expected_revision=-1,
            )
            first = catalog.save(
                api.ProviderLinkMapping.from_dict(
                    {**created.to_dict(), "status": "user_confirmed"}
                ),
                expected_revision=created.revision,
            )

            with self.assertRaises(api.ProviderLinkCatalogError):
                catalog.save(
                    api.ProviderLinkMapping.from_dict(
                        {**created.to_dict(), "status": "conflicted"}
                    ),
                    expected_revision=created.revision,
                )

            persisted = catalog.load(MAPPING_ID)
            self.assertEqual(first.revision, persisted.revision)
            self.assertEqual("user_confirmed", persisted.status)

    def test_completed_ledger_recovery_fills_catalogs_without_second_transfer(self):
        api = self._require_api("src.rename.provider_link_transaction")

        recovery = api.plan_provider_link_recovery(
            ledger_status="completed",
            series_catalog_status="registered",
            provider_link_catalog_status="missing",
            decision_status="catalog_update_pending",
            current_mapping=_mapping_payload("user_confirmed"),
        )

        self.assertFalse(recovery.invoke_transfer)
        self.assertFalse(recovery.write_series_catalog)
        self.assertTrue(recovery.write_provider_link_catalog)
        self.assertEqual("materialized_confirmed", recovery.final_mapping_status)
        self.assertEqual("completed", recovery.final_task_status)

    def test_recovery_is_idempotent_after_both_catalogs_are_registered(self):
        api = self._require_api("src.rename.provider_link_transaction")

        recovery = api.plan_provider_link_recovery(
            ledger_status="completed",
            series_catalog_status="registered",
            provider_link_catalog_status="registered",
            decision_status="healthy",
            current_mapping=_mapping_payload("materialized_confirmed"),
        )

        self.assertFalse(recovery.invoke_transfer)
        self.assertFalse(recovery.write_series_catalog)
        self.assertFalse(recovery.write_provider_link_catalog)
        self.assertFalse(recovery.write_decision)
        self.assertEqual("completed", recovery.final_task_status)


class JellyfinMetadataProjectionG0Contracts(_ApiContractCase):
    def test_new_tmdb_anchored_series_projects_provider_id_in_folder_name(self):
        api = self._require_api("src.rename.provider_link_resolver")

        projection = api.project_jellyfin_series_identity(
            display_name="向山进发",
            year=2013,
            tmdb_tv_id=TMDB_TV_ID,
            materialized_canonical_root=None,
        )

        self.assertEqual(
            "向山进发 (2013) [tmdbid-53918]", projection.folder_name
        )
        self.assertEqual("tmdbid-53918", projection.provider_identifier)
        self.assertFalse(projection.write_nfo)

    def test_materialized_root_is_never_silently_renamed_for_tmdb_hint(self):
        api = self._require_api("src.rename.provider_link_resolver")
        existing = Path("/library/Anime/向山进发 (2013)")

        projection = api.project_jellyfin_series_identity(
            display_name="向山进发",
            year=2013,
            tmdb_tv_id=TMDB_TV_ID,
            materialized_canonical_root=existing,
        )

        self.assertEqual(existing, projection.canonical_root)
        self.assertFalse(projection.rename_existing_root)
        self.assertFalse(projection.write_nfo)
        self.assertTrue(projection.requires_live_jellyfin_verification)


if __name__ == "__main__":
    unittest.main()
