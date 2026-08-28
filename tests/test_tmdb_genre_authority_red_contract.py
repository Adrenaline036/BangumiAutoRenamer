"""Incident-derived regressions for TMDB animation genre authority.

The production incident used TMDB ``zh-CN`` detail data whose stable genre
identity was ``id=16`` while the display name was localized to ``动画``.  The
current selected-candidate path only recognizes the English display name and
therefore downgrades an exact Bangumi -> TMDB match to fuzzy.

Remote provider transport and every persistence root are isolated by the
reused production-entry harness.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Mapping
import unittest
from unittest.mock import Mock, patch

import src.rename.process as process_module
import tests.test_provider_subject_plan_wiring_red_contract as phase_b_fixture
import tests.test_rc36_production_wiring_red_contract as rc36_fixture
from src.rename.plan_store import PlanStore
from src.rename.process import Rename
from src.rename.provider_search import (
    ProviderSearchBatch,
    ProviderSearchResult,
    ProviderSearchStatus,
    SearchPolicy,
    SOURCE_TMDB_TV,
)
from src.rename.rename_plan import ProviderIdentity


def _load_classifier(case: unittest.TestCase):
    rename_root = Path(process_module.__file__).parent
    definitions: list[Path] = []
    for path in sorted(rename_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "classify_tmdb_animation_evidence"
            for node in tree.body
        ):
            definitions.append(path)
    case.assertEqual(
        1,
        len(definitions),
        "TMDB genre classifier must have one unique src.rename definition; "
        f"found {[path.name for path in definitions]!r}",
    )
    module = importlib.import_module(
        f"src.rename.{definitions[0].stem}"
    )
    classifier = getattr(module, "classify_tmdb_animation_evidence", None)
    case.assertTrue(
        callable(classifier),
        "the unique classify_tmdb_animation_evidence() is not public callable",
    )
    return module, classifier


def _field(result: object, name: str):
    if isinstance(result, Mapping):
        return result.get(name)
    return getattr(result, name, None)


def _status(result: object) -> str:
    value = _field(result, "status")
    return str(getattr(value, "value", value) or "")


def _assert_provenance(case: unittest.TestCase, result: object) -> None:
    policy = str(_field(result, "policy_version") or "")
    source = str(_field(result, "evidence_source") or "")
    case.assertTrue(policy, "classification did not freeze a policy version")
    case.assertTrue(source, "classification did not freeze an evidence source")


def _candidate_by_id(candidates, provider_id: str) -> dict[str, object]:
    return next(
        dict(item)
        for item in candidates
        if str(item.get("provider_id") or "") == provider_id
    )


def _conflicting_info(provider_id: int = 4242) -> dict[str, object]:
    return {
        "id": provider_id,
        "name": "本地化作品",
        "original_name": "Localized Work",
        "first_air_date": "2020-01-01",
        "genres": [{"id": 18, "name": "动画"}],
        "seasons": [{"season_number": 1, "air_date": "2020-01-01"}],
    }


def _provider_batch(info: Mapping[str, object]) -> ProviderSearchBatch:
    candidate = {
        "namespace": "tmdb",
        "entity_type": "tv",
        "provider_id": str(info["id"]),
        "match_eligible": True,
    }
    policy = SearchPolicy(
        domain="unknown",
        media_hint="tv",
        expected_sources=(SOURCE_TMDB_TV,),
        scheduled_sources=(SOURCE_TMDB_TV,),
    )
    result = ProviderSearchResult(
        source=SOURCE_TMDB_TV,
        status=ProviderSearchStatus.SUCCESS,
        candidates=(candidate,),
        runtime={"name": str(info["name"]), "info": dict(info)},
    )
    return ProviderSearchBatch(policy, (result,), (candidate,))


class TMDBGenrePureClassifierRedContracts(unittest.TestCase):
    def test_strict_numeric_id16_from_detail_or_search_confirms_animation(self):
        _module, classify = _load_classifier(self)
        cases = (
            ({"genres": ({"id": 16, "name": "动画"},)}, "detail"),
            ({"genre_ids": (16,)}, "search"),
            ({
                "genres": ({"id": 16, "name": "Animation"},),
                "genre_ids": (16,),
            }, "combined"),
        )
        for kwargs, label in cases:
            with self.subTest(label=label):
                result = classify(**kwargs)
                self.assertEqual("confirmed_animation", _status(result))
                _assert_provenance(self, result)

    def test_numeric_conflict_and_invalid_id_types_remain_unknown(self):
        _module, classify = _load_classifier(self)
        cases = (
            (
                {"genres": ({"id": 18, "name": "动画"},)},
                "numeric evidence contradicts the localized animation name",
            ),
            (
                {
                    "genres": ({"id": 18, "name": "剧情"},),
                    "genre_ids": (16,),
                },
                "detail and search numeric sources disagree",
            ),
            (
                {"genres": ({"id": True, "name": ""},), "genre_ids": ("16",)},
                "bool and string ids are not strict integers",
            ),
        )
        for kwargs, label in cases:
            with self.subTest(label=label):
                result = classify(**kwargs)
                self.assertEqual("unknown_or_conflict", _status(result))
                _assert_provenance(self, result)
                self.assertTrue(
                    str(_field(result, "reason_code") or ""),
                    "unknown/conflict classification has no structured reason",
                )

    def test_unambiguous_numeric_non_animation_is_distinct_from_unknown(self):
        _module, classify = _load_classifier(self)
        result = classify(
            genres=({"id": 18, "name": "剧情"},),
            genre_ids=(18,),
        )
        self.assertEqual("confirmed_non_animation", _status(result))
        _assert_provenance(self, result)

    def test_names_are_legacy_fallback_only_when_no_usable_numeric_id_exists(self):
        _module, classify = _load_classifier(self)
        for name in ("Animation", "anime", "动画", "动漫"):
            with self.subTest(name=name):
                result = classify(genres=({"name": name},))
                self.assertEqual("confirmed_animation", _status(result))
                _assert_provenance(self, result)
                self.assertIn(
                    "legacy",
                    str(_field(result, "evidence_source") or "").casefold(),
                )


class TMDBGenreObservationAdapterRedContracts(unittest.TestCase):
    def test_selected_localized_detail_id16_is_exact_and_freezes_policy(self):
        info = {
            "id": 53918,
            "name": "向山进发",
            "original_name": "ヤマノススメ",
            "first_air_date": "2013-01-03",
            "genres": [{"id": 16, "name": "动画"}],
            "seasons": [{
                "season_number": 1,
                "air_date": "2013-01-03",
                "episode_count": 13,
            }],
            "_bar_search_alternatives": [{
                "id": 53918,
                "name": "向山进发",
                "original_name": "ヤマノススメ",
                "first_air_date": "2013-01-03",
                "genre_ids": [16],
            }],
        }
        candidates = process_module._tmdb_subject_observation_candidates(
            "向山进发", "向山进发", info, expected_year=2013
        )
        candidate = _candidate_by_id(candidates, "53918")
        components = dict(candidate.get("score_components") or {})
        self.assertEqual("exact", candidate.get("match_tier"))
        self.assertIs(True, components.get("animation_confirmed"))
        self.assertTrue(components.get("animation_policy_version"))
        self.assertTrue(components.get("animation_evidence_source"))

    def test_alternative_search_genre_ids_uses_the_same_frozen_classifier(self):
        info = {
            "id": 999,
            "name": "不相关作品",
            "original_name": "Unrelated",
            "first_air_date": "2013-01-03",
            "genres": [{"id": 18, "name": "剧情"}],
            "seasons": [],
            "_bar_search_alternatives": [
                {
                    "id": 999,
                    "name": "不相关作品",
                    "first_air_date": "2013-01-03",
                    "genre_ids": [18],
                },
                {
                    "id": 53918,
                    "name": "向山进发",
                    "original_name": "ヤマノススメ",
                    "first_air_date": "2013-01-03",
                    "genre_ids": [16],
                    "seasons": [{
                        "season_number": 1,
                        "air_date": "2013-01-03",
                        "episode_count": 13,
                    }],
                },
            ],
        }
        candidates = process_module._tmdb_subject_observation_candidates(
            "向山进发", "不相关作品", info, expected_year=2013
        )
        candidate = _candidate_by_id(candidates, "53918")
        components = dict(candidate.get("score_components") or {})
        self.assertEqual("exact", candidate.get("match_tier"))
        self.assertIs(True, components.get("animation_confirmed"))
        self.assertTrue(components.get("animation_policy_version"))
        self.assertTrue(components.get("animation_evidence_source"))

    def test_numeric_name_conflict_is_fuzzy_unknown_with_structured_reason(self):
        info = {
            **_conflicting_info(53918),
            "_bar_search_alternatives": [{
                "id": 53918,
                "name": "本地化作品",
                "original_name": "Localized Work",
                "first_air_date": "2020-01-01",
                "genre_ids": [18],
            }],
        }
        candidates = process_module._tmdb_subject_observation_candidates(
            "本地化作品", "本地化作品", info, expected_year=2020
        )
        candidate = _candidate_by_id(candidates, "53918")
        components = dict(candidate.get("score_components") or {})
        self.assertEqual("fuzzy", candidate.get("match_tier"))
        self.assertIsNone(components.get("animation_confirmed"))
        self.assertTrue(components.get("animation_policy_version"))
        self.assertTrue(components.get("animation_evidence_source"))
        self.assertTrue(components.get("animation_reason_code"))

    def test_existing_title_and_year_gates_still_reject_an_exact_title_wrong_year(self):
        info = {
            "id": 53918,
            "name": "向山进发",
            "original_name": "ヤマノススメ",
            "first_air_date": "2013-01-03",
            "genres": [{"name": "Animation"}],
            "seasons": [],
            "_bar_search_alternatives": [],
        }
        candidate = process_module._tmdb_subject_observation_candidates(
            "向山进发", "向山进发", info, expected_year=2020
        )[0]
        self.assertEqual("fuzzy", candidate.get("match_tier"))
        self.assertIs(False, candidate["score_components"]["year_compatible"])


class TMDBGenreTriStateCallSiteRedContracts(unittest.TestCase):
    def test_auto_batch_and_legacy_tv_movie_never_downgrade_unknown_to_false(self):
        info = _conflicting_info()

        batch_renamer = object.__new__(Rename)
        batch_result = batch_renamer._task_type_from_provider_batch(
            _provider_batch(info), is_anime=None
        )
        self.assertIsInstance(batch_result, tuple)
        self.assertIsNone(batch_result[2], "provider batch guessed general-domain False")

        tv_renamer = object.__new__(Rename)
        tv_renamer.search = Mock()
        tv_renamer._search_title_candidates = Mock(
            return_value=("本地化作品", dict(info), None)
        )
        tv_result = tv_renamer.check_task_type(
            "legacy-tv", "本地化作品 S01", 2020, Path("episode.mkv"),
            is_anime=None, is_movie=False,
        )
        self.assertIsInstance(tv_result, tuple)
        self.assertIsNone(tv_result[2], "legacy TV guessed general-domain False")

        movie_info = {
            **dict(info),
            "title": info["name"],
            "release_date": info["first_air_date"],
        }
        movie_renamer = object.__new__(Rename)
        movie_renamer.search = Mock()
        movie_renamer._search_title_candidates = Mock(
            return_value=("本地化作品", movie_info, None)
        )
        movie_result = movie_renamer.check_task_type(
            "legacy-movie", "本地化作品", 2020, Path("movie.mkv"),
            is_anime=None, is_movie=True,
        )
        self.assertIsInstance(movie_result, tuple)
        self.assertIsNone(movie_result[2], "legacy Movie guessed general-domain False")

    def test_explicit_animation_and_general_authority_remain_higher_than_genres(self):
        info = _conflicting_info()
        renamer = object.__new__(Rename)
        self.assertIs(
            True,
            renamer._task_type_from_provider_batch(
                _provider_batch(info), is_anime=True
            )[2],
        )
        self.assertIs(
            False,
            renamer._task_type_from_provider_batch(
                _provider_batch(info), is_anime=False
            )[2],
        )

    def test_direct_tmdb_keeps_explicit_domain_and_verified_link_authority(self):
        def run_case(
            label: str,
            *,
            info: Mapping[str, object],
            selection_domain: str | None = None,
            explicit_is_anime: bool | None = None,
            linked_identity: ProviderIdentity | None = None,
        ):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / f"{label} S01E01.mkv"
                source.touch()
                renamer = rc36_fixture._configured_renamer(root)
                renamer.search.get_tv_info_by_id.return_value = (
                    str(info["name"]), dict(info)
                )
                link_lookup = Mock(return_value=linked_identity)
                renamer._animation_bangumi_link = link_lookup
                tasks = root / "tasks"
                tasks.mkdir()
                plan_root = root / "plans"
                task_id = f"direct-{label}"
                selection = {
                    "namespace": "tmdb",
                    "entity_type": "tv",
                    "provider_id": str(info["id"]),
                    "title": str(info["name"]),
                    "year": 2020,
                }
                if selection_domain is not None:
                    selection["library_domain"] = selection_domain
                with (
                    patch.object(process_module, "TASK_PATH", tasks),
                    patch.object(process_module, "PLAN_PATH", plan_root),
                    patch.object(
                        process_module,
                        "DECISION_STATE_PATH",
                        root / "decisions",
                    ),
                    patch.object(
                        process_module,
                        "PROVIDER_LINK_CATALOG_PATH",
                        root / "provider_links",
                    ),
                    patch.object(
                        process_module,
                        "SERIES_CATALOG_PATH",
                        root / "series_catalog",
                    ),
                ):
                    renamer._process(
                        source,
                        _is_anime=explicit_is_anime,
                        _is_movie=False,
                        _tuuid=task_id,
                        provider_selection=selection,
                    )
                    plan = PlanStore(plan_root).load(task_id)
            return plan, link_lookup

        numeric_animation = {
            **_conflicting_info(4242),
            "genres": [{"id": 16, "name": "动画"}],
        }
        numeric_non_animation = {
            **_conflicting_info(4243),
            "genres": [{"id": 18, "name": "剧情"}],
        }
        conflict = _conflicting_info(4244)
        verified_link = ProviderIdentity(
            "confirmed",
            "bangumi",
            "subject",
            "777",
            "本地化作品",
            2020,
            "verified_provider_link",
        )

        explicit_plan, _ = run_case(
            "explicit-animation",
            info=numeric_non_animation,
            explicit_is_anime=True,
        )
        animation_domain_plan, _ = run_case(
            "domain-animation",
            info=numeric_non_animation,
            selection_domain="animation",
        )
        general_plan, general_lookup = run_case(
            "domain-general",
            info=numeric_animation,
            selection_domain="general",
        )
        linked_plan, _ = run_case(
            "verified-link",
            info=numeric_non_animation,
            linked_identity=verified_link,
        )
        unknown_plan, _ = run_case("unknown-conflict", info=conflict)

        self.assertEqual("animation", explicit_plan.work_identity.domain)
        self.assertEqual("animation", animation_domain_plan.work_identity.domain)
        self.assertEqual("general", general_plan.work_identity.domain)
        general_lookup.assert_not_called()
        self.assertEqual("animation", linked_plan.work_identity.domain)
        self.assertIn(verified_link, linked_plan.linked_provider_identities)
        self.assertEqual("unknown", unknown_plan.work_identity.domain)

    def test_six_logical_genre_shapes_share_five_classifier_calls(self):
        source = Path(process_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        classifier_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "classify_tmdb_animation_evidence"
                )
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "classify_tmdb_animation_evidence"
                )
            )
        ]
        self.assertEqual(
            5,
            len(classifier_calls),
            "six logical TMDB genre shapes must use five shared classifier calls",
        )
        observation_function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_tmdb_subject_observation_candidates"
        )
        observation_calls = [
            node
            for node in ast.walk(observation_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "classify_tmdb_animation_evidence"
        ]
        self.assertEqual(
            1,
            len(observation_calls),
            "selected and alternative candidates must share one merged-payload call",
        )
        legacy_lines = [
            line.strip()
            for line in source.splitlines()
            if (
                "g['name'].lower() == 'animation'" in line
                or 'in {\'animation\', \'anime\'}' in line
                or '== "animation"' in line
            )
        ]
        self.assertEqual(
            [], legacy_lines,
            "English display-name-only TMDB genre branches remain in process.py",
        )


class TMDBGenreProductionEntryRedContracts(unittest.IsolatedAsyncioTestCase):
    async def test_live_shaped_yama_s1_reaches_exact_four_season_plan(self):
        original_get_tv_info = phase_b_fixture._ProviderSearch.get_tv_info

        def localized_get_tv_info(search, query, year=None):
            name, payload = original_get_tv_info(search, query, year)
            if isinstance(payload, dict) and payload.get("id") == 53918:
                payload["genres"] = [{"id": 16, "name": "动画"}]
                payload["_bar_search_alternatives"] = [
                    {
                        **dict(item),
                        "genre_ids": [16],
                    }
                    for item in payload.get("_bar_search_alternatives", ())
                ]
            return name, payload

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = phase_b_fixture._make_episode_source(
                root / "source", "Yama Season 1", tuple(range(1, 14))
            )
            harness = phase_b_fixture.ProviderSubjectPlanWiringRedContracts(
                methodName="runTest"
            )
            localized_detail = {
                **dict(phase_b_fixture.YAMA_TMDB),
                "genres": [{"id": 16, "name": "动画"}],
            }
            with (
                patch.dict(
                    phase_b_fixture.YAMA_TMDB,
                    localized_detail,
                    clear=True,
                ),
                patch.object(
                    phase_b_fixture._ProviderSearch,
                    "get_tv_info",
                    new=localized_get_tv_info,
                ),
            ):
                result = await harness._run_real_add_select(
                    root=root / "flow",
                    source=source,
                    subjects=phase_b_fixture.YAMA_SUBJECTS,
                    relations=phase_b_fixture.YAMA_RELATIONS,
                    selected_subject_id="41658",
                    library_root=root / "library",
                )

            problems = harness._real_path_problems(result)
            selection = result.task.get("selection_operation") or {}
            if selection.get("state") != "succeeded":
                problems.append(
                    f"provider selection state is {selection.get('state')!r}"
                )
            work_identity = result.plan.work_identity
            primary = getattr(work_identity, "primary_provider", None)
            if primary is None or (
                primary.namespace,
                primary.entity_type,
                primary.provider_id,
            ) != ("tmdb", "tv", "53918"):
                problems.append("live-shaped entry did not freeze TMDB TV #53918")
            expected_root = root / "library" / "Anime" / "向山进发 (2013)"
            if Path(str(result.plan.metadata.get("canonical_work_root") or "")) != expected_root:
                problems.append("live-shaped entry did not reach the Chinese canonical root")
            if result.plan.metadata.get("season_number") != 1:
                problems.append("Bangumi #41658 did not project Season 01")
            if len(result.plan.entries) != 13 or any(
                "S01E" not in entry.target_path.name
                or "Season 01" not in entry.target_path.parts
                for entry in result.plan.entries
            ):
                problems.append("live-shaped entry did not persist the 13-entry S1 Plan")

            snapshot = result.plan.to_dict()["metadata"].get(
                "provider_link_snapshot"
            )
            observations = (
                snapshot.get("subject_observations")
                if isinstance(snapshot, dict) else None
            )
            exact_subjects: set[str] = set()
            policy_versions: set[str] = set()
            evidence_sources: set[str] = set()
            if isinstance(observations, dict):
                for row in observations.get("subject_observations", ()):
                    if not isinstance(row, dict):
                        continue
                    subject_id = str(row.get("bangumi_subject_id") or "")
                    for query in row.get("queries", ()):
                        if not isinstance(query, dict) or query.get("outcome") != "ok":
                            continue
                        for candidate in query.get("candidates", ()):
                            if not isinstance(candidate, dict):
                                continue
                            components = candidate.get("score_components") or {}
                            if (
                                candidate.get("match_tier") == "exact"
                                and str(candidate.get("tmdb_tv_id") or "") == "53918"
                                and components.get("animation_confirmed") is True
                            ):
                                exact_subjects.add(subject_id)
                            policy = str(components.get("animation_policy_version") or "")
                            source_name = str(components.get("animation_evidence_source") or "")
                            if policy:
                                policy_versions.add(policy)
                            if source_name:
                                evidence_sources.add(source_name)
            else:
                problems.append("live-shaped entry has no frozen subject observations")
            if exact_subjects != {"41658", "86670", "216310", "290980"}:
                problems.append(f"exact observation subjects are {sorted(exact_subjects)!r}")
            if len(policy_versions) != 1 or not evidence_sources:
                problems.append(
                    "observation fingerprint payload lacks stable animation policy/source"
                )
            self.assertEqual([], problems, "\n".join(problems))

    async def test_classifier_disconnect_recreates_the_exact_incident_reason(self):
        """The new classifier must carry the production-entry success path."""

        original_get_tv_info = phase_b_fixture._ProviderSearch.get_tv_info

        def localized_get_tv_info(search, query, year=None):
            name, payload = original_get_tv_info(search, query, year)
            if isinstance(payload, dict) and payload.get("id") == 53918:
                payload["genres"] = [{"id": 16, "name": "动画"}]
                payload["_bar_search_alternatives"] = [
                    {**dict(item), "genre_ids": [16]}
                    for item in payload.get("_bar_search_alternatives", ())
                ]
            return name, payload

        forced_non_animation = SimpleNamespace(
            is_anime=False,
            policy_version="disconnect-control",
            evidence_source="forced_non_animation",
            reason_code="disconnect_control",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = phase_b_fixture._make_episode_source(
                root / "source", "Yama Season 1", tuple(range(1, 14))
            )
            harness = phase_b_fixture.ProviderSubjectPlanWiringRedContracts(
                methodName="runTest"
            )
            localized_detail = {
                **dict(phase_b_fixture.YAMA_TMDB),
                "genres": [{"id": 16, "name": "动画"}],
            }
            with (
                patch.dict(
                    phase_b_fixture.YAMA_TMDB,
                    localized_detail,
                    clear=True,
                ),
                patch.object(
                    phase_b_fixture._ProviderSearch,
                    "get_tv_info",
                    new=localized_get_tv_info,
                ),
                patch.object(
                    process_module,
                    "classify_tmdb_animation_evidence",
                    return_value=forced_non_animation,
                ),
            ):
                result = await harness._run_real_add_select(
                    root=root / "flow",
                    source=source,
                    subjects=phase_b_fixture.YAMA_SUBJECTS,
                    relations=phase_b_fixture.YAMA_RELATIONS,
                    selected_subject_id="41658",
                    library_root=root / "library",
                )

        selection = result.task.get("selection_operation") or {}
        problems = harness._real_path_problems(result)
        if selection.get("state") != "failed":
            problems.append(
                f"disconnect selection state is {selection.get('state')!r}"
            )
        if selection.get("failure_code") != "relation_unresolved":
            problems.append(
                f"disconnect failure code is {selection.get('failure_code')!r}"
            )
        reason_codes = tuple(selection.get("reason_codes") or ())
        if "subject_candidate_confidence_insufficient" not in reason_codes:
            problems.append(f"disconnect reason codes are {reason_codes!r}")
        if "subject_candidate_confidence_insufficient" not in str(
            result.task.get("error") or ""
        ):
            problems.append("persisted task error lost the exact incident reason")
        self.assertEqual([], problems, "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
