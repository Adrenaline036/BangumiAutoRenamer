"""Incident-derived RED contracts for canonical media naming.

The live Plan-only counterexample persisted ``Kaguya-sama: Love Is War`` as
one Windows directory component.  These contracts keep provider transport
local and all writes under ``TemporaryDirectory`` while separating:

* P1 portable leaf/root safety; and
* P2 localized title authority.

The incident contracts are ordinary regressions after the authorized GREEN
implementation; no expected-failure wrapper remains.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
from typing import Mapping
import unittest
from unittest.mock import Mock, patch

import src.rename.get_info as get_info
import tests.test_r22_revised_c_c0_red_contract as c1_fixture
import tests.test_target_routing_production_red_contract as routing_fixture
import tests.test_isolated_sample_acceptance as isolated_fixture
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.get_info import Search
from src.rename.inventory_plan import build_inventory_review_draft
from src.rename.movie_assets import inventory_movie_assets
from src.rename.plan_review import PlanReviewService, _safe_media_name
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.work_catalog import WorkCatalog
from tests.test_isolated_sample_acceptance import routing_snapshot_for
from tests.projection_test_support import save_plan_fixture
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
)

def _naming_module():
    try:
        return importlib.import_module("src.rename.canonical_naming")
    except (ImportError, ModuleNotFoundError) as exc:
        raise AssertionError(
            "shared canonical naming production module is missing"
        ) from exc


def _api(name: str):
    value = getattr(_naming_module(), name, None)
    if not callable(value):
        raise AssertionError(f"canonical naming API {name!r} is missing")
    return value


def _mapping(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, dict):
            return dict(payload)
    raise AssertionError("canonical naming decision is not serializable")


def _issue_codes(plan) -> set[str]:
    with patch(
        "requests.sessions.Session.request",
        side_effect=AssertionError("Verifier must not read Provider network"),
    ):
        return {issue.code for issue in PlanVerifier().verify(plan).issues}


def _decision(plan) -> dict[str, object]:
    value = plan.to_dict()['metadata'].get("canonical_naming_decision")
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical_identity_payload(identity: dict[str, str]) -> bytes:
    return json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _windows_invalid_components(path: Path, root: Path) -> tuple[str, ...]:
    invalid = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return (str(path),)
    return tuple(part for part in parts if invalid.search(part))


async def _complete_kaguya_cohort(
    root: Path,
    *,
    bangumi_title: str,
    tmdb_title: str,
    disconnect: bool = False,
):
    subjects = deepcopy(c1_fixture.KAGUYA_SUBJECTS)
    subjects["248175"]["name_cn"] = bangumi_title
    tmdb_payload = deepcopy(c1_fixture.KAGUYA_TMDB)
    tmdb_payload["name"] = tmdb_title
    with (
        patch.object(c1_fixture, "KAGUYA_SUBJECTS", subjects),
        patch.object(c1_fixture, "KAGUYA_TMDB", tmdb_payload),
    ):
        result, snapshot = await routing_fixture._run_auto_boundary_incident(
            root, "kaguya"
        )

    task = c1_fixture._load_task(result)
    result.pending_boundary_fingerprint = result.state.pending_tv_selection.fingerprint
    notices: list[tuple[str, object]] = []

    def boundary_io_snapshot():
        def files(roots):
            return {
                str(path): (path.stat().st_ino, path.stat().st_size,
                            path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
                for scan_root in roots if scan_root.exists()
                for path in scan_root.rglob("*") if path.is_file() and path.suffix != '.lock'
            }
        return {
            "source": files((root / "source",)),
            "business": files(tuple(result.root / name for name in (
                "records", "provider_links", "series_catalog", "work_catalog",
                "recovery", "abandon", "ledger", "font_supply",
            ))),
            "targets": files(tuple(result.root / name for name in (
                "Anime", "AnimeMovies", "Movies", "TV",
            ))),
            "authorities": files(tuple(result.root / name for name in ('plans', 'decisions', 'tasks'))),
        }

    result.boundary_io_before = boundary_io_snapshot()
    with (
        routing_fixture._configured_roots(snapshot),
        c1_fixture._boundary_patches(result, notices),
    ):
        from src.pages.review_operation_view import build_review_operation_presentation
        from src.rename.initial_movie_completion import RootSnapshotSource
        command = next(action.payload for action in build_review_operation_presentation(task).actions
                       if action.payload and action.payload.get('kind') == 'confirm_cohort')
        service = PlanReviewService(PlanStore(result.plans), result.reviews, result.root / 'work_catalog',
            decision_state_root=result.decisions, task_root=result.tasks,
            root_snapshot_source=RootSnapshotSource(lambda: snapshot))
        prepare = lambda: service.prepare_tv_boundary_completion(result.task_id,
            PlanStore.fingerprint(service.plan_store.load(result.task_id)), review_command=command)
        if disconnect:
            error = _naming_module().CanonicalNamingError
            with patch.object(_naming_module(), 'resolve_title_authority',
                    side_effect=error('shared title resolver disconnected')) as resolver, \
                    patch.object(service, 'commit_tv_boundary_completion') as commit:
                with unittest.TestCase().assertRaisesRegex(error, 'disconnected'):
                    prepare()
                resolver.assert_called()
                commit.assert_not_called()
        else:
            service.commit_tv_boundary_completion(prepare())
    result.boundary_io_after = boundary_io_snapshot()
    return result, snapshot, PlanStore(result.plans).load(result.task_id)


class CanonicalNamingAlreadyGreenControls(unittest.TestCase):
    def test_existing_safe_name_behavior_is_frozen(self):
        self.assertEqual(
            "A＜B＞C：D＂E／F＼G｜H？I＊J",
            _safe_media_name('A<B>C:D"E/F\\G|H?I*J'),
        )
        self.assertEqual("A B", _safe_media_name("  A   B . "))

    def test_collision_fixture_identity_is_order_independent(self):
        left = {"namespace": "tmdb", "entity_type": "tv", "provider_id": "83121"}
        right = {"provider_id": "83121", "entity_type": "tv", "namespace": "tmdb"}
        self.assertEqual(
            _canonical_identity_payload(left),
            _canonical_identity_payload(right),
        )
        self.assertNotEqual(
            _canonical_identity_payload(left),
            _canonical_identity_payload({**left, "provider_id": "53918"}),
        )



class CanonicalLeafP1RedContracts(unittest.TestCase):
    def test_shared_leaf_policy_is_portable_idempotent_and_nfc_first(self):
        canonicalize = _api("canonicalize_media_leaf")
        collision_key = _api("windows_collision_key")
        error = getattr(_naming_module(), "CanonicalNamingError", None)
        if not isinstance(error, type) or not issubclass(error, Exception):
            self.fail("CanonicalNamingError is missing")

        source = "  Cafe\u0301: A/B\\C\x00D  . "
        expected = "Café： A／B＼C_D"
        actual = canonicalize(source)
        self.assertEqual(expected, actual)
        self.assertEqual(actual, canonicalize(actual), "leaf helper is not idempotent")
        self.assertEqual("Ａ：Ｂ／Ｃ＼Ｄ", canonicalize("Ａ：Ｂ／Ｃ＼Ｄ"))
        self.assertEqual(
            collision_key("Example Work"),
            collision_key("example work. "),
        )

        long_leaf = canonicalize("长" * 300)
        self.assertLessEqual(
            len(long_leaf.encode("utf-16-le")) // 2,
            240,
            "canonical leaf exceeds the frozen UTF-16 budget",
        )
        self.assertEqual(long_leaf, canonicalize("长" * 300))
        for unsafe in ("", " . ", "..", "CON", "con.txt", "LPT9"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(error):
                    canonicalize(unsafe)

    def test_collision_suffix_uses_frozen_work_identity_not_arrival_order(self):
        suffix_for = _api("deterministic_collision_suffix")
        identity = {
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": "83121",
        }
        digest = hashlib.sha256(_canonical_identity_payload(identity)).hexdigest()
        expected = f" [同名-{digest[:12]}]"
        self.assertEqual(expected, suffix_for(identity))
        self.assertEqual(
            expected,
            suffix_for({
                "provider_id": "83121",
                "namespace": "tmdb",
                "entity_type": "tv",
            }),
        )
        self.assertNotEqual(
            expected,
            suffix_for({**identity, "provider_id": "53918"}),
        )

        # The suffix is not a test-only helper: the production completion path
        # must consume it when a different registered identity owns the same
        # canonical title/year leaf.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            harness = isolated_fixture.IsolatedSampleAcceptanceTests(
                methodName="runTest"
            )
            _source, allowed, draft, store, service = (
                harness._inventory_plan_with_identity(
                    root,
                    ("Example Work [1080p].mkv",),
                    task_id="collision-first",
                )
            )
            snapshot = routing_snapshot_for(
                allowed / "AnimeMovies",
                domain="animation",
                group_kind="movie_continuous",
            )
            first = service.complete_inventory_draft(
                draft.task_id,
                store.fingerprint(draft),
                group_id=draft.presentation_groups[0].group_id,
                group_kind="movie_continuous",
                target_root=allowed / "AnimeMovies",
                reason="freeze first registered collision owner",
                root_snapshot=snapshot,
            )
            from tests.test_work_catalog import complete_ledger
            WorkCatalog(root / "catalog").register_completed(first, complete_ledger(first, root / "records"))

            source2 = root / "source-second"
            source2.mkdir()
            (source2 / "Example Work [2160p].mkv").write_bytes(b"second")
            task2 = {
                "uuid": "collision-second",
                "path": str(source2),
                "name": "Example Work",
                "is_anime": True,
                "is_movie": True,
                "movie_inventory": inventory_movie_assets(source2).to_dict(),
                "draft_target_root": str(allowed),
                "draft_initial_target_root": str(
                    allowed / "AnimeMovies" / "Example Work (2026)"
                ),
                "provider_identity": {
                    **identity,
                    "entity_type": "movie",
                    "title": "Example Work",
                    "year": 2026,
                },
                "proposed_work_key": (
                    "work-12345678-1234-1234-1234-333333333333"
                ),
            }
            second_draft = build_inventory_review_draft(task2, dry_run=False)
            second_draft = save_plan_fixture(store, second_draft)
            second = service.complete_inventory_draft(
                second_draft.task_id,
                store.fingerprint(second_draft),
                group_id=second_draft.presentation_groups[0].group_id,
                group_kind="movie_continuous",
                target_root=allowed / "AnimeMovies",
                reason="exercise deterministic registered collision",
                root_snapshot=snapshot,
            )
            second_identity = {
                "namespace": "tmdb",
                "entity_type": "movie",
                "provider_id": "83121",
            }
            self.assertEqual(
                f"Example Work (2026){suffix_for(second_identity)}",
                Path(second.metadata["canonical_work_root"]).name,
            )

    def test_completion_api_removes_the_arbitrary_canonical_root_bypass(self):
        parameters = inspect.signature(
            PlanReviewService.complete_inventory_draft
        ).parameters
        self.assertNotIn(
            "canonical_series_root",
            parameters,
            "callers can still override the canonical root after leaf cleanup",
        )
        builder = _api("build_canonical_work_root")
        names = inspect.signature(builder).parameters
        self.assertTrue(
            {"resolved_target_root", "decision", "year"} <= set(names),
            "root builder does not consume frozen target + naming decision + year",
        )

    def test_verifier_blocks_legacy_policy_and_every_invalid_target_component(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _snapshot = routing_fixture._completed_movie_plan(Path(directory))
            legacy_metadata = dict(plan.metadata)
            legacy_metadata.pop("canonical_naming_decision", None)
            legacy = replace(plan, metadata=legacy_metadata)
            self.assertIn("canonical_naming_policy_missing", _issue_codes(legacy))

            old_root = Path(str(plan.metadata["canonical_work_root"]))
            target_root = Path(str(plan.metadata["target_root"]))
            bad_root = target_root / "Bad:Work (2026)"
            bad_entries = tuple(
                replace(
                    entry,
                    target_path=bad_root / entry.target_path.relative_to(old_root),
                )
                for entry in plan.entries
            )
            bad_metadata = dict(plan.metadata)
            bad_metadata["canonical_work_root"] = str(bad_root)
            bad = replace(plan, metadata=bad_metadata, entries=bad_entries)
            self.assertIn("canonical_path_leaf_invalid", _issue_codes(bad))

            entry = plan.entries[0]
            bad_file = replace(
                entry,
                target_path=entry.target_path.with_name("Episode?01.mkv"),
            )
            self.assertIn(
                "canonical_path_leaf_invalid",
                _issue_codes(replace(plan, entries=(bad_file,))),
            )


class CanonicalLeafP1ProductionRedContracts(unittest.IsolatedAsyncioTestCase):
    async def test_live_shaped_english_cohort_uses_one_safe_root_builder(self):
        with tempfile.TemporaryDirectory() as directory:
            result, snapshot, plan = await _complete_kaguya_cohort(
                Path(directory),
                bangumi_title="Kaguya-sama: Love Is War",
                tmdb_title="Kaguya-sama: Love Is War",
            )
            canonical = Path(str(plan.metadata.get("canonical_work_root") or ""))
            problems: list[str] = []
            if canonical.name != "Kaguya-sama： Love Is War (2019)":
                problems.append(f"unsafe canonical leaf: {canonical.name!r}")
            for entry in plan.entries:
                invalid = _windows_invalid_components(
                    entry.target_path, snapshot.anime_path
                )
                if invalid:
                    problems.append(f"invalid target components: {invalid!r}")
            decision = _decision(plan)
            if decision.get("leaf_policy_version") != "bar-windows-portable-leaf-v1":
                problems.append("Plan omitted the frozen leaf policy version")
            self.assertEqual(_mapping(_api('decision_from_work_identity')(plan.work_identity)), decision)
            self.assert_boundary_receipt(result, plan)
            if result.business_json or result.target_files or not result.source_unchanged:
                problems.append("Plan-only production fixture crossed its safety boundary")
        self.assertEqual([], problems, "\n".join(problems))

    def assert_boundary_receipt(self, result, plan):
        receipt = plan.metadata['tv_boundary_completion_receipt']
        context = current_provider_selection_context(c1_fixture._load_task(result))
        self.assertEqual(plan.metadata['decision_state_revision'], receipt['result_decision_revision'])
        self.assertEqual(PlanStore.fingerprint(plan), context['result_plan_fingerprint'])
        self.assertEqual(result.pending_boundary_fingerprint, receipt['pending_fingerprint'])


class TitleAuthorityP2RedContracts(unittest.TestCase):
    def test_bangumi_identity_uses_exact_tmdb_zh_cn_for_short_title_v2(self):
        resolve = _api("resolve_title_authority")
        conflict = getattr(_naming_module(), "TitleAuthorityConflict", None)
        if not isinstance(conflict, type) or not issubclass(conflict, Exception):
            self.fail("TitleAuthorityConflict is missing")

        selected = {
            "namespace": "bangumi",
            "entity_type": "subject",
            "provider_id": "248175",
            "localized_title": "辉夜大小姐想让我告白～天才们的恋爱头脑战～",
            "original_title": "かぐや様は告らせたい～天才たちの恋愛頭脳戦～",
        }
        mapped = ({
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": "83121",
            "localized_title": "辉夜大小姐想让我告白",
            "original_title": "Kaguya-sama: Love Is War",
        },)
        decision = _mapping(resolve(
            selection=selected,
            mapped_candidates=mapped,
            primary_provider_identity={
                "namespace": "tmdb",
                "entity_type": "tv",
                "provider_id": "83121",
            },
        ))
        self.assertEqual("辉夜大小姐想让我告白", decision.get("display_title"))
        self.assertEqual("tmdb:zh-CN", decision.get("title_source"))
        self.assertEqual(
            "bar-title-authority-v2",
            decision.get("title_policy_version"),
        )
        self.assertIn(
            "辉夜大小姐想让我告白～天才们的恋爱头脑战～",
            tuple(decision.get("aliases") or ()),
            "the selected Bangumi full title was not retained as an alias",
        )
        self.assertIn(
            "かぐや様は告らせたい～天才たちの恋愛頭脳戦～",
            tuple(decision.get("aliases") or ()),
        )

        with self.assertRaises(conflict):
            resolve(
                selection=selected,
                mapped_candidates=(
                    mapped[0],
                    {**mapped[0], "provider_id": "330299"},
                ),
                primary_provider_identity=None,
            )

    def test_unlocalized_tmdb_keeps_bangumi_fallback_and_direct_original(self):
        resolve = _api("resolve_title_authority")
        conflict = getattr(_naming_module(), "TitleAuthorityConflict", None)
        if not isinstance(conflict, type) or not issubclass(conflict, Exception):
            self.fail("TitleAuthorityConflict is missing")

        bangumi = {
            "namespace": "bangumi",
            "entity_type": "subject",
            "provider_id": "9001",
            "localized_title": "可用的官方中文名",
            "original_title": "Original Work",
        }
        unlocalized_tmdb = {
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": "7001",
            "localized_title": "Original Work",
            "original_title": "Original Work",
        }
        fallback = _mapping(resolve(
            selection=bangumi,
            mapped_candidates=(unlocalized_tmdb,),
            primary_provider_identity=unlocalized_tmdb,
        ))
        self.assertEqual("可用的官方中文名", fallback.get("display_title"))
        self.assertEqual("bangumi:name_cn", fallback.get("title_source"))
        self.assertEqual(
            "bar-title-authority-v2",
            fallback.get("title_policy_version"),
        )
        self.assertIn(
            "Original Work",
            tuple(fallback.get("aliases") or ()),
        )

        direct = resolve(
            selection=unlocalized_tmdb,
            mapped_candidates=(),
            primary_provider_identity=unlocalized_tmdb,
        )
        self.assertEqual("Original Work", direct.display_title)
        self.assertEqual("tmdb:original", direct.title_source)

    def test_direct_tmdb_without_original_title_uses_localized_title(self):
        resolve = _api("resolve_title_authority")
        conflict = getattr(_naming_module(), "TitleAuthorityConflict", None)
        if not isinstance(conflict, type) or not issubclass(conflict, Exception):
            self.fail("TitleAuthorityConflict is missing")
        incomplete = {
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": "83121",
            "localized_title": "辉夜大小姐想让我告白",
        }
        decision = resolve(selection=incomplete, primary_provider_identity=incomplete)
        self.assertEqual("辉夜大小姐想让我告白", decision.display_title)
        self.assertEqual("tmdb:zh-CN", decision.title_source)

    def test_legacy_work_projection_has_non_provider_title_provenance(self):
        project = _api("decision_from_work_identity")
        provider = SimpleNamespace(to_dict=lambda: {
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": "83121",
            "title": "已经冻结的旧标题",
        })
        work = SimpleNamespace(
            primary_provider=provider,
            title="已经冻结的旧标题",
        )
        with patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Work projection reread Provider network"),
        ):
            decision = _mapping(project(work))
        self.assertEqual("已经冻结的旧标题", decision.get("display_title"))
        self.assertEqual(
            "work:frozen_projection",
            decision.get("title_source"),
        )

    def test_multi_work_series_uses_owner_subjects_exact_tmdb_zh_cn(self):
        choose_owner = _api("select_bangumi_work_title_owner")
        resolve = _api("resolve_title_authority")
        subjects = (
            {
                "provider_id": "season-2",
                "title": "完整系列名 第二季",
                "original_title": "Series Second Season",
                "air_date": "2020-01-01",
            },
            {
                "provider_id": "season-1",
                "title": "完整系列名～第一季度宣传副标题～",
                "original_title": "Series First Season",
                "air_date": "2019-01-01",
            },
        )
        assignments = {"season-1": 1, "season-2": 2}
        owner = choose_owner(
            subjects=subjects,
            season_assignments=assignments,
        )
        self.assertEqual("season-1", owner.get("provider_id"))

        tmdb_owner = {
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": "work-1",
            "localized_title": "系列短标题",
            "original_title": "Series First Work",
        }
        tmdb_sequel = {
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": "work-2",
            "localized_title": "系列续作标题",
            "original_title": "Series Second Work",
        }
        selection = {
            "namespace": "bangumi",
            "entity_type": "subject",
            "provider_id": str(owner.get("provider_id") or ""),
            "localized_title": str(owner.get("title") or ""),
            "original_title": str(owner.get("original_title") or ""),
        }
        decision = _mapping(resolve(
            selection=selection,
            mapped_candidates=(tmdb_sequel, tmdb_owner),
            primary_provider_identity=tmdb_owner,
        ))
        self.assertEqual("系列短标题", decision.get("display_title"))
        self.assertEqual("tmdb:zh-CN", decision.get("title_source"))
        self.assertEqual(
            "bar-title-authority-v2",
            decision.get("title_policy_version"),
        )

    def test_v1_plan_is_stale_at_the_shared_verifier_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _snapshot = routing_fixture._completed_movie_plan(Path(directory))
            metadata = dict(plan.metadata)
            serialized_metadata = dict(plan.to_dict().get("metadata") or {})
            decision = dict(
                serialized_metadata.get("canonical_naming_decision") or {}
            )
            decision["title_policy_version"] = "bar-title-authority-v1"
            fingerprint_payload = {
                key: decision.get(key)
                for key in (
                    "display_title",
                    "title_source",
                    "aliases",
                    "original_title",
                    "primary_provider_identity",
                    "evidence",
                    "title_policy_version",
                    "leaf_policy_version",
                )
            }
            decision["decision_fingerprint"] = hashlib.sha256(
                json.dumps(
                    fingerprint_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            metadata["canonical_naming_decision"] = decision
            legacy = replace(plan, metadata=metadata)
            self.assertIn(
                "canonical_naming_policy_missing",
                _issue_codes(legacy),
            )

    def test_title_resolver_uses_only_supplied_frozen_evidence(self):
        resolve = _api("resolve_title_authority")
        selected = {
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": "83121",
            "localized_title": "辉夜大小姐想让我告白",
            "original_title": "Kaguya-sama: Love Is War",
        }
        with patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("title resolver reread Provider network"),
        ):
            decision = _mapping(resolve(
                selection=selected,
                primary_provider_identity=selected,
            ))
        self.assertEqual("辉夜大小姐想让我告白", decision.get("display_title"))
        self.assertEqual(
            "83121",
            dict(decision.get("primary_provider_identity") or {}).get(
                "provider_id"
            ),
        )

    @patch("src.rename.get_info.tmdb.TV")
    @patch("src.rename.get_info.tmdb.Search")
    def test_tv_search_and_selected_detail_use_one_zh_cn_locale(
        self,
        search_factory,
        tv_factory,
    ):
        localized = {
            "id": 83121,
            "name": "辉夜大小姐想让我告白",
            "original_name": "Kaguya-sama: Love Is War",
            "first_air_date": "2019-01-12",
            "genre_ids": [16],
        }
        fake_search = SimpleNamespace(results=[dict(localized)])
        fake_search.tv = Mock()
        search_factory.return_value = fake_search
        fake_tv = SimpleNamespace(id=83121)
        detail_calls: list[dict[str, object]] = []

        def info(**kwargs):
            detail_calls.append(dict(kwargs))
            is_zh = kwargs.get("language") == "zh-CN"
            fake_tv.__dict__.update({
                **localized,
                "name": (
                    "辉夜大小姐想让我告白"
                    if is_zh else "Kaguya-sama: Love Is War"
                ),
                "genres": [{"id": 16, "name": "动画"}],
                "seasons": [],
            })
            return dict(fake_tv.__dict__)

        fake_tv.info = info
        tv_factory.return_value = fake_tv
        name, payload = object.__new__(Search).get_tv_info("辉夜大小姐想让我告白", 2019)
        self.assertEqual([{"language": "zh-CN"}], detail_calls)
        self.assertEqual("zh-CN", fake_search.tv.call_args.kwargs.get("language"))
        self.assertEqual("辉夜大小姐想让我告白", name)
        self.assertEqual("辉夜大小姐想让我告白", payload.get("name"))
        self.assertEqual(
            "辉夜大小姐想让我告白",
            payload.get("_bar_search_name"),
        )


class TitleAuthorityP2ProductionRedContracts(unittest.IsolatedAsyncioTestCase):
    assert_boundary_receipt = CanonicalLeafP1ProductionRedContracts.assert_boundary_receipt
    async def test_real_kaguya_payload_persists_short_tmdb_title_v2(self):
        with tempfile.TemporaryDirectory() as directory:
            result, snapshot, plan = await _complete_kaguya_cohort(
                Path(directory),
                bangumi_title="辉夜大小姐想让我告白～天才们的恋爱头脑战～",
                tmdb_title="辉夜大小姐想让我告白",
            )
            decision = _decision(plan)
            canonical = Path(str(plan.metadata.get("canonical_work_root") or ""))
            problems: list[str] = []
            if plan.work_identity is None or plan.work_identity.title != "辉夜大小姐想让我告白":
                problems.append("TMDB zh-CN short title is not the Work title authority")
            if canonical != snapshot.anime_path / "辉夜大小姐想让我告白 (2019)":
                problems.append(f"fresh short canonical root is {canonical!s}")
            self.assertEqual(_mapping(_api('decision_from_work_identity')(plan.work_identity)), decision)
            if decision.get("title_policy_version") != "bar-title-authority-v2":
                problems.append("Plan omitted the v2 title policy version")
            if decision.get("leaf_policy_version") != "bar-windows-portable-leaf-v1":
                problems.append("Plan omitted the independent leaf policy version")
            from src.rename.successor_authority import CohortAuthorityStore, successor_authority_root
            store = CohortAuthorityStore(successor_authority_root(result.decisions))
            authorities = store.list_for_subject('248175')
            self.assertEqual(1, len(authorities))
            self.assertIn('辉夜大小姐想让我告白～天才们的恋爱头脑战～',
                          json.dumps(authorities[0].title_selection, ensure_ascii=False))
            fingerprint = str(decision.get("decision_fingerprint") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                problems.append("Plan naming decision has no canonical fingerprint")
            self.assert_boundary_receipt(result, plan)
            if result.business_json or result.target_files or not result.source_unchanged:
                problems.append("Bangumi Plan-only fixture crossed its safety boundary")
        self.assertEqual([], problems, "\n".join(problems))

    async def test_manual_tmdb_tv_freezes_current_naming_and_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            result = await c1_fixture._run_direct_tv(
                Path(directory),
                task_id="canonical-naming-direct-tmdb",
                provider_id=83121,
                folder="Kaguya-sama Season 1",
                stem="Kaguya-sama",
                count=12,
            )
            plan = PlanStore(result.plans).load(result.task_id)
            problems: list[str] = []
            self.assertIsNotNone(plan.work_identity, result.task.get('error'))
            self.assertEqual('tmdb', plan.work_identity.primary_provider.namespace)
            self.assertEqual('83121', plan.work_identity.primary_provider.provider_id)
            self.assertTrue(plan.metadata.get('canonical_naming_decision'))
            from src.rename.series_registration import validate_plan_series_registration
            from src.rename.plan_verifier import tv_authority_issue
            self.assertIsNotNone(validate_plan_series_registration(plan))
            self.assertIsNone(tv_authority_issue(plan))
            for field, code in (('series_registration_authority', 'tv_authority_incomplete'),
                                ('canonical_naming_decision', 'canonical_naming_policy_missing')):
                damaged = replace(plan, metadata={**plan.metadata, field: None})
                self.assertIn(code, {issue.code for issue in PlanVerifier().verify(damaged).issues})
            self.assertEqual('need_confirm', result.task.get('workflow_status'))
            self.assertEqual('83121', str(result.task['provider_selection']['provider_id']))
            business_json = tuple(
                path
                for name in ("records", "series_catalog", "work_catalog", "ledger")
                for path in (result.root / name).rglob("*.json")
                if (result.root / name).exists()
            )
            target_files = tuple(
                path
                for name in ("Anime", "AnimeMovies", "Movies", "TV")
                for path in (result.root / name).rglob("*")
                if (result.root / name).exists() and path.is_file()
            )
            if business_json or target_files:
                problems.append(
                    "direct TMDB production fixture crossed its business/media boundary"
                )
        self.assertEqual([], problems, "\n".join(problems))

    async def test_cohort_production_entry_cannot_bypass_shared_title_resolver(self):
        error = getattr(_naming_module(), "CanonicalNamingError", None)
        if not isinstance(error, type) or not issubclass(error, Exception):
            self.fail("CanonicalNamingError is missing")
        with tempfile.TemporaryDirectory() as directory:
            result, _snapshot, plan = await _complete_kaguya_cohort(
                    Path(directory),
                    bangumi_title=(
                        "辉夜大小姐想让我告白～天才们的恋爱头脑战～"
                    ),
                    tmdb_title="辉夜大小姐想让我告白",
                    disconnect=True,
                )
            task = c1_fixture._load_task(result)
            self.assertNotIn("canonical_naming_decision", plan.metadata)
            # Inventory freezes its validated root before provider naming. That
            # root alone must never make a disconnected naming flow executable.
            self.assertIsNone(plan.work_identity)
            self.assertIsNone(plan.metadata.get("font_contribution"))
            verification = PlanVerifier().verify(plan)
            self.assertEqual("invalid", verification.status.value)
            self.assertIn("work_identity_unconfirmed", {issue.code for issue in verification.issues})
            self.assertTrue(result.boundary_io_before["source"])
            self.assertEqual(result.boundary_io_before, result.boundary_io_after)
            self.assertFalse(result.boundary_io_after["business"])
            self.assertFalse(result.boundary_io_after["targets"])
            self.assertEqual(
                "awaiting_user",
                current_provider_selection_context(task).get("state"),
                "disconnecting the shared resolver still produced a completed Plan",
            )


if __name__ == "__main__":
    unittest.main()
