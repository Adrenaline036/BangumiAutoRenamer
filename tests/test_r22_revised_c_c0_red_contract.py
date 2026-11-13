"""C1 GREEN contracts distilled from the failed R22 full-library run.

All seven repaired production gaps are ordinary regression tests.  Provider
transport is always local; all filesystem writes stay inside
``TemporaryDirectory`` roots.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Mapping
import unittest
from unittest.mock import MagicMock, Mock, patch

import src.pages.plan_review_page as review_page
import src.rename.process as process_module
import src.utils.utils as utils_module
import tests.test_r1_real_media_production_red_contract as archive_fixture
import tests.test_complex_library_special_red_contract as special_fixture
import tests._media_flow_helpers as movie_fixture
import tests.test_provider_subject_plan_wiring_red_contract as phase_b_fixture
import tests.test_r1_real_media_production_red_contract as production_fixture
from src.pages.plan_review_page import (
    PlanReviewDialog,
    build_plan_review_snapshot,
)
from src.rename.decision_state import (
    DecisionCommandService,
    DecisionStateStore,
)
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_review import PlanReviewError, PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import PlanStatus
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
    persist_provider_selection_attempt,
)


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _authority_bytes(result) -> dict[Path, bytes]:
    paths = (
        result.tasks / f"{result.task_id}.json",
        result.plans / f"{result.task_id}.json",
        result.decisions / f"{result.task_id}.json",
    )
    return {path: path.read_bytes() for path in paths if path.is_file()}


def _load_task(result) -> dict:
    return json.loads(
        (result.tasks / f"{result.task_id}.json").read_text("utf-8")
    )


def _protected_boundary_files(result):
    """Fresh physical observations, never the producer's earlier tuple."""
    paths = [result.plan.source_root, *(result.root / name for name in
        ('records', 'series_catalog', 'work_catalog', 'provider_links', 'recovery', 'abandon'))]
    paths.extend(Path(getattr(result.renamer, name)) for name in
        ('BANGUMI_PATH', 'MOVIE_PATH', 'ANIME_PATH', 'ANIME_MOVIE_PATH'))
    source = paths[0]
    return {str(path): tuple(row for row in phase_b_fixture._source_manifest(path)
        if path == source or not row[0].endswith('.lock')) for path in paths}


def _operation_arguments(task: dict) -> tuple[int, str, dict[str, object]]:
    operation = current_provider_selection_context(task)
    edges = operation.get("provider_subject_evidence", {}).get(
        "provider_subject_boundary_edges", ()
    )
    if not isinstance(edges, (list, tuple)) or len(edges) != 1:
        raise AssertionError("incident fixture must freeze one boundary edge")
    revision = operation.get("expected_decision_revision")
    evidence_fingerprint = str(operation.get("evidence_fingerprint") or "")
    if not isinstance(revision, int) or len(evidence_fingerprint) != 64:
        raise AssertionError("incident fixture lost revision/evidence authority")
    return revision, evidence_fingerprint, dict(edges[0])


def _boundary_command(task, *, same_work=True):
    from src.pages.review_operation_view import build_review_operation_presentation
    kind = 'confirm_cohort' if same_work else 'separate_work'
    return next(dict(action.payload) for action in build_review_operation_presentation(task).actions
                if action.payload and action.payload.get('kind') == kind)


async def _dispatch_boundary(dialog, command=None):
    value = PlanReviewDialog._dispatch_review_command(dialog,
        command if command is not None else _boundary_command(dialog.task_data))
    if asyncio.iscoroutine(value):
        await value


def _boundary_dialog(result, *, task: dict | None = None, decision=...):
    dialog = SimpleNamespace()
    dialog.plan = PlanStore(result.plans).load(result.task_id)
    dialog.task_data = dict(task if task is not None else _load_task(result))
    dialog.task_decision = (
        DecisionStateStore(result.decisions).load(result.task_id)
        if decision is ...
        else decision
    )
    dialog.snapshot = build_plan_review_snapshot(dialog.plan)
    dialog.review_service = PlanReviewService(
        PlanStore(result.plans), result.reviews, result.root / "work_catalog",
        decision_state_root=result.decisions, task_root=result.tasks,
        record_root=result.root / 'records',
        provider_link_catalog_root=result.manager.provider_link_catalog_root,
        root_snapshot_source=result.manager.root_snapshot_source,
    )
    dialog.decision_command_service = DecisionCommandService(
        DecisionStateStore(result.decisions), PlanStore(result.plans)
    )
    dialog.selected_ids = set()
    dialog._adopt_revision = Mock()
    dialog._render_groups = Mock()
    dialog._show_provider_candidates = Mock()
    dialog._close_review = Mock()
    dialog._select_provider_candidate = Mock()
    dialog._remove_provider_failure_task = Mock()
    dialog._dispatch_review_command = PlanReviewDialog._dispatch_review_command.__get__(dialog)
    return dialog


def _boundary_patches(
    result,
    notices: list[tuple[str, object]],
    *,
    root_snapshot: Mapping[str, object] | None = None,
) -> ExitStack:
    stack = ExitStack()
    snapshot = root_snapshot or {
        'bangumi_path': str(result.renamer.BANGUMI_PATH),
        'movie_path': str(result.renamer.MOVIE_PATH),
        'anime_path': str(result.renamer.ANIME_PATH),
        'anime_movie_path': str(result.renamer.ANIME_MOVIE_PATH),
    }
    if isinstance(snapshot, Mapping):
        stack.enter_context(phase_b_fixture._root_config_patch(snapshot))
    stack.enter_context(patch.object(review_page, "TASK_PATH", result.tasks))
    stack.enter_context(patch.object(review_page, "PLAN_PATH", result.plans))
    stack.enter_context(
        patch.object(review_page, "PLAN_REVIEW_PATH", result.reviews)
    )
    stack.enter_context(
        patch.object(review_page, "DECISION_STATE_PATH", result.decisions)
    )
    stack.enter_context(patch.object(utils_module, "TASK_PATH", result.tasks))
    stack.enter_context(
        patch.object(
            review_page,
            "notify",
            side_effect=lambda message, **kwargs: notices.append(
                (str(message), kwargs.get("type"))
            ),
        )
    )
    return stack


KAGUYA_SUBJECTS = {
    "248175": phase_b_fixture._subject(
        "248175",
        "辉夜大小姐想让我告白",
        "かぐや様は告らせたい～天才たちの恋愛頭脳戦～",
        "2019-01-12",
        12,
    ),
    "293049": phase_b_fixture._subject(
        "293049",
        "辉夜大小姐想让我告白？",
        "かぐや様は告らせたい？～天才たちの恋愛頭脳戦～",
        "2020-04-11",
        12,
    ),
}
KAGUYA_RELATIONS = {
    "248175": (("293049", "续集"),),
    "293049": (("248175", "前传"),),
}
KAGUYA_TMDB = phase_b_fixture._tmdb_tv(
    83121,
    "辉夜大小姐想让我告白",
    "Kaguya-sama: Love Is War",
    "2019-01-12",
    ((1, "2019-01-12", 12), (2, "2020-04-11", 12), (3, "2022-04-09", 13)),
)


class _KaguyaProviderSearch(phase_b_fixture._ProviderSearch):
    def _lookup(self, query: object):
        value = str(query or "").casefold()
        if "辉夜" in value or "かぐや" in value or "kaguya" in value:
            fail_terms = (
                self.fail_subject
                if isinstance(self.fail_subject, tuple)
                else ((self.fail_subject,) if self.fail_subject else ())
            )
            if any(str(term).casefold() in value for term in fail_terms):
                if self.failure_mode == "transport":
                    from src.rename.get_info import TMDBRequestError

                    raise TMDBRequestError("local C0 provider failure")
                return "", None
            return "辉夜大小姐想让我告白", dict(KAGUYA_TMDB)
        return super()._lookup(query)

    def get_tv_info_by_id(self, provider_id):
        if int(provider_id) == 83121:
            return "辉夜大小姐想让我告白", dict(KAGUYA_TMDB)
        return super().get_tv_info_by_id(provider_id)


async def _run_boundary_incident(root: Path, work: str):
    case = phase_b_fixture.ProviderSubjectPlanWiringRedContracts(
        methodName="runTest"
    )
    if work == "yama":
        source = phase_b_fixture._make_episode_source(
            root / "source", "Yama Season 1", tuple(range(1, 14))
        )
        return await case._run_real_add_select(
            root=root / "flow",
            source=source,
            subjects=phase_b_fixture.YAMA_SUBJECTS,
            relations=phase_b_fixture.YAMA_RELATIONS,
            selected_subject_id="41658",
            fail_subject=("第二季", "セカンドシーズン"),
            failure_mode="no_match",
        )

    source = phase_b_fixture._make_episode_source(
        root / "source",
        "Kaguya-sama wa Kokurasetai",
        tuple(range(1, 13)),
    )
    base_factory = phase_b_fixture._configured_renamer

    def configured(actual_root, **kwargs):
        renamer = base_factory(actual_root, **kwargs)
        renamer.search = _KaguyaProviderSearch(
            fail_subject=kwargs.get("fail_subject"),
            failure_mode=kwargs.get("failure_mode", "transport"),
        )
        return renamer

    with patch.object(
        phase_b_fixture, "_configured_renamer", side_effect=configured
    ):
        return await case._run_real_add_select(
            root=root / "flow",
            source=source,
            subjects=KAGUYA_SUBJECTS,
            relations=KAGUYA_RELATIONS,
            selected_subject_id="248175",
            fail_subject=("辉夜大小姐想让我告白？", "かぐや様は告らせたい？"),
            failure_mode="no_match",
        )


class _VioletSearch(production_fixture._FrozenSearch):
    def get_movie_info(self, query, _year=None):
        if "violet" in str(query).casefold() or "紫罗兰" in str(query):
            return "紫罗兰永恒花园 剧场版", {
                "id": 533514,
                "title": "紫罗兰永恒花园 剧场版",
                "original_title": "劇場版 ヴァイオレット・エヴァーガーデン",
                "release_date": "2020-09-18",
                "genres": [{"name": "Animation"}],
            }
        return "", None

    def get_movie_info_by_id(self, provider_id):
        if int(provider_id) == 533514:
            return self.get_movie_info("Violet")
        return "", None


def _violet_renamer(root: Path):
    renamer = archive_fixture._configured_renamer(root)
    renamer.search = _VioletSearch()
    return renamer


VIOLET_CANDIDATE = {
    "namespace": "tmdb",
    "entity_type": "movie",
    "provider_id": "533514",
    "title": "紫罗兰永恒花园 剧场版",
    "original_title": "劇場版 ヴァイオレット・エヴァーガーデン",
    "year": 2020,
    "poster_url": production_fixture.POSTER_URL,
    "source_url": "https://www.themoviedb.org/movie/533514",
    "snapshot_at": "2026-08-25T00:00:00+00:00",
}


class _DirectTvSearch(production_fixture._FrozenSearch):
    _TABLE = {
        83121: (
            "辉夜大小姐想让我告白",
            KAGUYA_TMDB,
        ),
        78102: (
            "命运石之门 0",
            phase_b_fixture._tmdb_tv(
                78102,
                "命运石之门 0",
                "Steins;Gate 0",
                "2018-04-12",
                ((0, "2018-04-12", 24), (1, "2018-04-12", 23)),
            ),
        ),
        330299: (
            "辉夜大小姐想让我告白：初吻不会结束",
            phase_b_fixture._tmdb_tv(
                330299,
                "辉夜大小姐想让我告白：初吻不会结束",
                "Kaguya-sama: Love Is War -The First Kiss That Never Ends-",
                "2022-12-17",
                ((1, "2022-12-17", 4),),
            ),
        ),
    }

    def get_tv_info_by_id(self, provider_id):
        value = self._TABLE.get(int(provider_id))
        return (value[0], dict(value[1])) if value else ("", None)

    def get_tv_info(self, query, _year=None):
        value = str(query or "").casefold()
        if "steins" in value or "命运石" in value:
            return self.get_tv_info_by_id(78102)
        if "first kiss" in value or "初吻" in value:
            return self.get_tv_info_by_id(330299)
        if "kaguya" in value or "辉夜" in value:
            return self.get_tv_info_by_id(83121)
        return super().get_tv_info(query, _year)


_base_configured_renamer = archive_fixture._configured_renamer


def _direct_tv_renamer(root: Path):
    renamer = _base_configured_renamer(root)
    renamer.search = _DirectTvSearch()
    return renamer


def _make_tv_source(root: Path, folder: str, stem: str, count: int) -> Path:
    source = root / folder
    for episode in range(1, count + 1):
        _write(
            source
            / f"{stem} [{episode:02d}][Ma10p_1080p][x265_flac].mkv",
            f"episode-{episode}".encode("ascii"),
        )
    return source


async def _run_direct_tv(
    root: Path,
    *,
    task_id: str,
    provider_id: int,
    folder: str,
    stem: str,
    count: int,
):
    source = _make_tv_source(root / "sources", folder, stem, count)
    harness = SimpleNamespace(
        _NoopReviewDialog=(
            production_fixture.R1RealQueueWorkerContracts._NoopReviewDialog
        )
    )
    title, info = _DirectTvSearch().get_tv_info_by_id(provider_id)
    candidate = {
        "namespace": "tmdb",
        "entity_type": "tv",
        "provider_id": str(provider_id),
        "title": title,
        "original_title": str(info.get("original_name") or ""),
        "year": int(str(info.get("first_air_date") or "0000")[:4]),
        "poster_url": production_fixture.POSTER_URL,
        "source_url": f"https://www.themoviedb.org/tv/{provider_id}",
        "snapshot_at": "2026-08-25T00:00:00+00:00",
    }
    flow = root / "flow"
    with (
        patch.object(
            production_fixture,
            "_configured_renamer",
            side_effect=_direct_tv_renamer,
        ),
        patch.object(process_module, "SERIES_CATALOG_PATH", flow / "series_catalog"),
        patch.object(process_module, "RECORD_PATH", flow / "records"),
        patch.object(process_module, "is_dry_run_enabled", return_value=False),
    ):
        result = await production_fixture.R1RealQueueWorkerContracts._run_flow(
            harness,
            root=flow,
            source=source,
            task_id=task_id,
            candidate=candidate,
            is_anime=True,
            is_movie=False,
        )
    result.task_id = task_id
    result.root = flow
    result.source = source
    result.tasks = flow / "tasks"
    result.plans = flow / "plans"
    result.decisions = flow / "decisions"
    return result


class R22RevisedCC0RedContracts(unittest.IsolatedAsyncioTestCase):
    async def test_p1_1_cached_dialog_may_be_stale_while_disk_authority_is_fresh(self):
        # A missing cached Decision must ask for refresh, not auto-rebase consent.
        for work in ("yama", "kaguya"):
            with self.subTest(work=work), tempfile.TemporaryDirectory() as directory:
                result = await _run_boundary_incident(Path(directory), work)
                protected = _protected_boundary_files(result)
                before = _authority_bytes(result)
                stale = _boundary_dialog(result, decision=None)
                notices = []
                with _boundary_patches(result, notices):
                    await _dispatch_boundary(stale)
                self.assertEqual(before, _authority_bytes(result))
                self.assertTrue(any("刷新" in message for message, _ in notices))
                fresh = _boundary_dialog(result)
                with _boundary_patches(result, notices):
                    await _dispatch_boundary(fresh)
                state = DecisionStateStore(result.decisions).load(result.task_id)
                plan = PlanStore(result.plans).load(result.task_id)
                self.assertIsNone(state.pending_tv_selection)
                self.assertTrue(state.confirmed_work)
                self.assertGreater(state.revision, result.state.revision)
                self.assertFalse(fresh.review_service.load_or_create(plan).approved_entry_ids)
                self.assertEqual(protected, _protected_boundary_files(result))

    async def test_p1_1_real_drift_refreshes_once_and_invalidates_old_callback(self):
        # The later change is a real current command, not a raw revision edit.
        with tempfile.TemporaryDirectory() as directory:
            result = await _run_boundary_incident(Path(directory), "yama")
            protected = _protected_boundary_files(result)
            stale = _boundary_dialog(result)
            command = _boundary_command(stale.task_data)
            fresh = _boundary_dialog(result)
            notices = []
            with _boundary_patches(result, notices):
                await _dispatch_boundary(fresh, command)
            current = DecisionStateStore(result.decisions).load(result.task_id)
            self.assertIsNone(current.pending_tv_selection)
            self.assertGreater(current.revision, result.state.revision)
            before = _authority_bytes(result)
            with _boundary_patches(result, notices):
                await _dispatch_boundary(stale, command)
                await _dispatch_boundary(_boundary_dialog(result), command)
            self.assertEqual(before, _authority_bytes(result))
            self.assertTrue(any(kind == "negative" for _, kind in notices))
            self.assertEqual(protected, _protected_boundary_files(result))

    async def test_p1_1_restart_closes_decision_written_before_task_close(self):
        # Current transactions restore their original pending parent; they do
        # not silently roll forward an uncompleted confirmation after restart.
        from src.rename.projection_authority import ProjectionCommitService
        with tempfile.TemporaryDirectory() as directory:
            result = await _run_boundary_incident(Path(directory), "yama")
            dialog = _boundary_dialog(result)
            protected = _protected_boundary_files(result)
            before = _authority_bytes(result)
            reached = []
            def crash(_transaction, phase):
                if phase == "decision_written":
                    reached.append(phase)
                    raise SystemExit("isolated boundary interruption")
            with _boundary_patches(result, []):
                intent = dialog.review_service.prepare_tv_boundary_completion(result.task_id,
                    PlanStore.fingerprint(dialog.plan), review_command=_boundary_command(dialog.task_data))
                with patch.object(ProjectionCommitService, "_checkpoint", crash):
                    with self.assertRaises(SystemExit):
                        dialog.review_service.commit_tv_boundary_completion(intent)
                self.assertEqual(["decision_written"], reached)
                fresh = ProjectionCommitService(dialog.review_service.decision_state_store,
                    dialog.review_service.plan_store, task_root=result.tasks)
                self.assertEqual({result.task_id: "failed_closed"}, fresh.recover_pending())
                self.assertEqual(before, _authority_bytes(result))
                again = ProjectionCommitService(dialog.review_service.decision_state_store,
                    dialog.review_service.plan_store, task_root=result.tasks)
                again.recover_pending()
                self.assertEqual(before, _authority_bytes(result))
                reopened = _boundary_dialog(result)
                self.assertIsNotNone(reopened.task_decision.pending_tv_selection)
                await _dispatch_boundary(reopened)
            state = DecisionStateStore(result.decisions).load(result.task_id)
            self.assertIsNone(state.pending_tv_selection)
            self.assertFalse(reopened.review_service.load_or_create(
                PlanStore(result.plans).load(result.task_id)).approved_entry_ids)
            self.assertEqual(protected, _protected_boundary_files(result))


    async def test_p1_3_real_order_violet_menu_slots_are_unique_and_stable(self):
        primary_names = (
            "[VCB-Studio] Gekijouban Violet Evergarden "
            "[Ma10p_2160p_DoVi_P8.1][x265_flac].mkv",
            "[VCB-Studio] Gekijouban Violet Evergarden "
            "[Ma10p_2160p_SDR][x265_flac].mkv",
        )
        menu_names = tuple(
            "SPs/[VCB-Studio] Gekijouban Violet Evergarden "
            f"[Menu{index:02d}][Ma10p_2160p_DoVi_P8.1][x265_flac].mkv"
            for index in range(1, 4)
        )
        files = primary_names + menu_names
        mappings = []
        problems = []
        for order_index, order in enumerate((files, tuple(reversed(files)))):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "sources" / (
                    "[VCB-Studio] Gekijouban Violet Evergarden [Ma10p_2160p]"
                )
                for name in order:
                    _write(source / name, name.encode("utf-8"))
                primaries = tuple(source / name for name in primary_names)
                harness = SimpleNamespace(
                    _NoopReviewDialog=(
                        production_fixture.R1RealQueueWorkerContracts._NoopReviewDialog
                    )
                )
                flow = root / "flow"
                with (
                    patch.object(
                        production_fixture,
                        "_configured_renamer",
                        side_effect=_violet_renamer,
                    ),
                    patch.object(process_module, "SERIES_CATALOG_PATH", flow / "series_catalog"),
                    patch.object(process_module, "RECORD_PATH", flow / "records"),
                    patch.object(process_module, "is_dry_run_enabled", return_value=False),
                ):
                    initial = await production_fixture.R1RealQueueWorkerContracts._run_flow(
                        harness,
                        root=flow,
                        source=source,
                        task_id=f"c0-violet-real-order-{order_index}",
                        candidate=VIOLET_CANDIDATE,
                        is_anime=True,
                        is_movie=True,
                    )
                movie_case = movie_fixture.MediaFlow(
                    methodName="runTest"
                )
                try:
                    completed = await movie_case._complete_movie(
                        root=flow,
                        result=initial,
                        primary=primaries,

                    )
                    plan = completed.store.load(
                        f"c0-violet-real-order-{order_index}"
                    )
                except Exception as exc:
                    problems.append(
                        f"{order_index}: production completion raised "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                menu_sources = {source / name for name in menu_names}
                self.assertTrue(menu_sources <= set(plan.expected_sources))
                self.assertTrue(menu_sources <= set(plan.ignored_sources))
                self.assertFalse(menu_sources & {entry.source_path for entry in plan.entries})
                for name in menu_names:
                    self.assertEqual(name.encode("utf-8"), (source / name).read_bytes())
                entries = [entry for entry in plan.entries if entry.source_path in primaries]
                self.assertEqual(2, len(entries))
                self.assertEqual(2, len({entry.target_path for entry in entries}))
                mappings.append(
                    tuple(
                        sorted(
                            (entry.source_path.name, entry.target_path.name)
                            for entry in entries
                        )
                    )
                )
        if len(mappings) != 2 or mappings[0] != mappings[1]:
            problems.append("reversing source enumeration changed Violet asset slots")
        self.assertEqual([], problems, "\n".join(problems))

    async def test_direct_tv_requires_unambiguous_season_and_current_authority(self):
        cases = (
            (
                "kaguya-s2",
                83121,
                "[VCB-Studio] Kaguya-sama wa Kokurasetai？Tensai-tachi "
                "no Ren'ai Zunousen [Ma10p_1080p]",
                "[VCB-Studio] Kaguya-sama wa Kokurasetai？Tensai-tachi "
                "no Ren'ai Zunousen",
                12,
                "Season 01",
            ),
            (
                "steins-zero",
                78102,
                "[VCB-Studio] Steins;Gate 0 [Ma10p_1080p]",
                "[VCB-Studio] Steins;Gate 0",
                23,
                "Season 00",
            ),
        )
        problems = []
        for name, provider_id, folder, stem, count, forbidden_season in cases:
            with tempfile.TemporaryDirectory() as directory:
                result = await _run_direct_tv(
                    Path(directory),
                    task_id=f"c0-direct-{name}",
                    provider_id=provider_id,
                    folder=folder,
                    stem=stem,
                    count=count,
                )
                plan = result.plan_store.load(result.task_id)
                verification = PlanVerifier().verify(plan)
                issue_codes = {issue.code for issue in verification.issues}
                if name == 'kaguya-s2':
                    self.assertEqual(PlanStatus.INVALID, verification.status)
                    self.assertIn('tv_authority_incomplete', issue_codes)
                    self.assertIsNone(plan.work_identity)
                    self.assertIn('无法唯一确定当前季度', result.task.get('error', ''))
                else:
                    self.assertEqual('steins-zero', name)
                    self.assertIsNotNone(plan.work_identity, result.task.get('error'))
                    self.assertNotIn('tv_authority_incomplete', issue_codes)
                    self.assertEqual(set(), issue_codes)
                    self.assertEqual(1, plan.metadata.get('season_number'))
                    self.assertEqual({1}, {e.season_id for e in plan.entries if e.media_kind == 'video'})
                if any(
                    forbidden_season in str(entry.target_path)
                    and not entry.requires_review
                    for entry in plan.entries
                ):
                    problems.append(
                        f"{name}: wrong {forbidden_season} mapping remained executable"
                    )
                authority_issue = next(
                    (
                        issue
                        for issue in verification.issues
                        if issue.code == "tv_authority_incomplete"
                    ),
                    None,
                )
                if authority_issue is not None and any(
                    token in authority_issue.message
                    for token in ("series_key", "snapshot", "DecisionState")
                ):
                    problems.append(f"{name}: authority issue exposed an internal field")
                # P1-5 C0 meta-contract: bulk issue actions remain ineligible
                # until the authority-blocked cohort has been removed by C1.
                bulk_issue_actions_allowed = (
                    verification.status != PlanStatus.INVALID
                    and "tv_authority_incomplete" not in issue_codes
                )
                if bulk_issue_actions_allowed:
                    problems.append(f"{name}: unauthoritative Plan entered bulk issue handling")
        self.assertEqual([], problems, "\n".join(problems))

    async def test_p1_4_execution_preflight_rejects_authority_free_tv(self):
        with tempfile.TemporaryDirectory() as directory:
            result = await _run_direct_tv(
                Path(directory),
                task_id="c0-direct-first-kiss",
                provider_id=330299,
                folder=(
                    "[VCB-Studio] Kaguya-sama wa Kokurasetai - First Kiss "
                    "wa Owaranai [Ma10p_1080p]"
                ),
                stem=(
                    "[VCB-Studio] Kaguya-sama wa Kokurasetai - First Kiss "
                    "wa Owaranai"
                ),
                count=4,
            )
            plan = result.plan_store.load(result.task_id)
            service = PlanExecutionService(
                result.plan_store,
                PlanReviewService(
                    result.plan_store,
                    result.root / "reviews",
                    result.root / "work_catalog",
                    decision_state_root=result.root / 'decisions',
                    task_root=result.root / 'tasks',
                ),
                task_root=result.root / "tasks",
                record_root=result.root / "records",
                catalog_root=result.root / "work_catalog",
                series_catalog_root=result.root / "series_catalog",
                decision_state_root=result.root / "decisions",
                provider_link_catalog_root=result.root / "provider_links",
            )
            service._validate_provider_link_preflight(plan)
            self.assertTrue(plan.metadata.get('series_registration_authority'))
            self.assertNotIn('provider_link_snapshot', plan.metadata)
            for field, value in (('series_registration_authority', None), ('series_key', 'wrong-series')):
                invalid = replace(plan, metadata={**plan.metadata, field: value})
                with self.assertRaises(PlanExecutionError):
                    service._validate_provider_link_preflight(invalid)
            incompatible = replace(plan, metadata={**plan.metadata, 'provider_link_snapshot': {
                'series_key': plan.metadata['series_key'], 'season_assignments': []}})
            with self.assertRaises(PlanExecutionError):
                service._validate_provider_link_preflight(incompatible)
            problems = []
            if tuple((result.root / "records").glob("*.ledger.json")):
                problems.append("preflight-only rejection wrote an execution ledger")
            if any(
                path.is_file()
                for root in (result.root / "Anime", result.root / "AnimeMovies")
                if root.exists()
                for path in root.rglob("*")
            ):
                problems.append("preflight-only rejection wrote target media")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_crosswalk_positive_controls_do_not_depend_on_real_network(self):
        with tempfile.TemporaryDirectory() as directory:
            yama = await _run_boundary_incident(Path(directory), "yama")
            self.assertEqual(2, yama.process_call_count)
            self.assertEqual((), yama.business_json)
            self.assertTrue(yama.source_unchanged)
            self.assertFalse(yama.target_files)



if __name__ == "__main__":
    unittest.main()
