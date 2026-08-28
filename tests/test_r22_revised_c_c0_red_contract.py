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
import tests.test_complex_library_archive_red_contract as archive_fixture
import tests.test_complex_library_special_red_contract as special_fixture
import tests.test_movie_font_work_catalog_red_contract as movie_fixture
import tests.test_provider_subject_plan_wiring_red_contract as phase_b_fixture
import tests.test_r1_real_media_production_red_contract as production_fixture
import tests.test_tv_font_series_association_red_contract as font_series_fixture
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
from src.rename.utils import is_font_archive


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


def _operation_arguments(task: dict) -> tuple[int, str, dict[str, object]]:
    operation = task.get("selection_operation")
    if not isinstance(operation, dict):
        raise AssertionError("incident fixture lost selection_operation")
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


def _boundary_dialog(result, *, task: dict | None = None, decision=...):
    dialog = object.__new__(PlanReviewDialog)
    dialog.plan = PlanStore(result.plans).load(result.task_id)
    dialog.task_data = dict(task if task is not None else _load_task(result))
    dialog.task_decision = (
        DecisionStateStore(result.decisions).load(result.task_id)
        if decision is ...
        else decision
    )
    dialog.snapshot = build_plan_review_snapshot(dialog.plan)
    dialog.review_service = PlanReviewService(
        PlanStore(result.plans), result.reviews, result.root / "work_catalog"
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
    return dialog


def _boundary_patches(
    result,
    notices: list[tuple[str, object]],
    *,
    root_snapshot: Mapping[str, object] | None = None,
) -> ExitStack:
    stack = ExitStack()
    snapshot = root_snapshot or _load_task(result).get("draft_root_snapshot")
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
    renamer = archive_fixture._BASE_CONFIGURED_RENAMER(root)
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


def _direct_tv_renamer(root: Path):
    renamer = archive_fixture._BASE_CONFIGURED_RENAMER(root)
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
        problems: list[str] = []
        for work in ("yama", "kaguya"):
            with tempfile.TemporaryDirectory() as directory:
                result = await _run_boundary_incident(Path(directory), work)
                revision, evidence, edge = _operation_arguments(result.task)
                before_state = DecisionStateStore(result.decisions).load(
                    result.task_id
                )
                dialog = _boundary_dialog(result, decision=None)
                notices: list[tuple[str, object]] = []
                with _boundary_patches(result, notices):
                    PlanReviewDialog._apply_provider_subject_boundary_inline(
                        dialog, True, revision, evidence, edge
                    )
                state = DecisionStateStore(result.decisions).load(result.task_id)
                plan = PlanStore(result.plans).load(result.task_id)
                task = _load_task(result)
                if state.revision <= before_state.revision:
                    problems.append(f"{work}: fresh disk Decision was rejected by stale dialog cache")
                if task.get("workflow_status") != "need_confirm" or (
                    task.get("selection_operation") or {}
                ).get("state") != "succeeded":
                    problems.append(f"{work}: boundary action did not close the failed task")
                verification = PlanVerifier().verify(plan)
                if verification.status != PlanStatus.ACCEPTED or verification.issues:
                    problems.append(f"{work}: boundary action did not persist an approvable Plan")
                if result.business_json or result.target_files:
                    problems.append(f"{work}: Plan-only action crossed a business/media boundary")
        self.assertEqual([], problems, "\n".join(problems))

    async def test_p1_1_real_drift_refreshes_once_and_invalidates_old_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            result = await _run_boundary_incident(Path(directory), "yama")
            stale_task = dict(result.task)
            old_revision, evidence, edge = _operation_arguments(stale_task)
            state_store = DecisionStateStore(result.decisions)
            current = state_store.save(
                state_store.load(result.task_id),
                expected_revision=old_revision,
            )
            current_task = _load_task(result)
            current_task["selection_operation"][
                "expected_decision_revision"
            ] = current.revision
            task_path = result.tasks / f"{result.task_id}.json"
            task_path.write_text(
                json.dumps(current_task, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            before_old_click = _authority_bytes(result)
            dialog = _boundary_dialog(
                result,
                task=stale_task,
                decision=result.state,
            )
            notices: list[tuple[str, object]] = []
            capture = special_fixture._CaptureUi()
            capture.card = capture.column
            with _boundary_patches(result, notices), patch.object(
                review_page, "ui", capture
            ):
                PlanReviewDialog._apply_provider_subject_boundary_inline(
                    dialog, True, old_revision, evidence, edge
                )
            problems = []
            if _authority_bytes(result) != before_old_click:
                problems.append("old callback rebased itself and changed disk authority")
            if (
                dialog.task_decision is None
                or dialog.task_decision.revision != current.revision
                or (
                    dialog.task_data.get("selection_operation") or {}
                ).get("expected_decision_revision")
                != current.revision
            ):
                problems.append("stale click did not reload current disk authority")

            refreshed_ui = special_fixture._CaptureUi()
            refreshed_ui.card = refreshed_ui.column
            with _boundary_patches(result, notices), patch.object(
                review_page, "ui", refreshed_ui
            ):
                PlanReviewDialog._render_selection_operation(dialog)
                fresh_callback = refreshed_ui.callbacks.get(
                    "确认与相邻作品一起整理"
                )
                if callable(fresh_callback):
                    value = fresh_callback()
                    if asyncio.iscoroutine(value):
                        await value
            if not callable(fresh_callback):
                problems.append("refresh did not expose a newly frozen boundary action")
            task = _load_task(result)
            if (
                task.get("workflow_status") != "need_confirm"
                or (task.get("selection_operation") or {}).get("state")
                != "succeeded"
            ):
                problems.append("newly frozen boundary action did not succeed once")
            frozen = _authority_bytes(result)
            with _boundary_patches(result, notices):
                PlanReviewDialog._apply_provider_subject_boundary_inline(
                    dialog, True, old_revision, evidence, edge
                )
            if _authority_bytes(result) != frozen:
                problems.append("invalidated old callback changed authority after success")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_p1_1_restart_closes_decision_written_before_task_close(self):
        with tempfile.TemporaryDirectory() as directory:
            result = await _run_boundary_incident(Path(directory), "yama")
            revision, evidence, edge = _operation_arguments(result.task)
            dialog = _boundary_dialog(result)
            notices: list[tuple[str, object]] = []
            with _boundary_patches(result, notices), patch.object(
                review_page, "mutate_task", return_value=None
            ):
                PlanReviewDialog._apply_provider_subject_boundary_inline(
                    dialog, True, revision, evidence, edge
                )

            written_state = DecisionStateStore(result.decisions).load(result.task_id)
            written_plan = PlanStore(result.plans).load(result.task_id)
            failed_task = _load_task(result)
            plan_before_restart = (
                result.plans / f"{result.task_id}.json"
            ).read_bytes()
            state_before_restart = (
                result.decisions / f"{result.task_id}.json"
            ).read_bytes()
            restarted = _boundary_dialog(
                result,
                task=failed_task,
                decision=written_state,
            )
            restarted.plan = written_plan
            restart_failures: list[str] = []
            with _boundary_patches(result, notices):
                for attempt in (1, 2):
                    try:
                        PlanReviewDialog._apply_provider_subject_boundary_inline(
                            restarted, True, revision, evidence, edge
                        )
                    except PlanReviewError as exc:
                        restart_failures.append(
                            f"restart attempt {attempt} re-entered draft completion: {exc}"
                        )
            task = _load_task(result)
            problems = list(restart_failures)
            if written_state.revision <= revision:
                problems.append("fault did not reach the Decision durable boundary")
            if (
                result.plans / f"{result.task_id}.json"
            ).read_bytes() != plan_before_restart:
                problems.append("restart rewrote the already committed Plan")
            if (
                result.decisions / f"{result.task_id}.json"
            ).read_bytes() != state_before_restart:
                problems.append("restart advanced Decision twice")
            selection = task.get("selection_operation") or {}
            if (
                task.get("workflow_status") != "need_confirm"
                or selection.get("state") != "succeeded"
                or selection.get("result_decision_revision")
                != written_state.revision
            ):
                problems.append("restart did not idempotently close the exact staged decision")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_p1_2_explicit_zip_font_reaches_execution_and_series_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flow = root / "flow"
            source = production_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume",
            )
            archive = _write(
                source
                / "[UHA-WINGS&VCB-Studio] Kaguya-sama wa Kokurasetai "
                "[Fonts].zip",
                b"incident-shaped-explicit-font-zip",
            )
            special_case = special_fixture.ComplexLibrarySpecialRedContracts(
                methodName="runTest"
            )
            with (
                patch.object(process_module, "SERIES_CATALOG_PATH", flow / "series_catalog"),
                patch.object(process_module, "RECORD_PATH", flow / "records"),
            ):
                result = await special_case._run(
                    flow,
                    source,
                    "c0-explicit-tv-font-zip",
                    "41658",
                    candidate=production_fixture._candidate("41658"),
                    renamer_factory=production_fixture._configured_renamer,
                )
            dialog, ui, _entry = special_case._render_issue_actions(
                result, selected_source=archive
            )
            callback = ui.callbacks.get("设为字体包")
            if callable(callback):
                with patch.object(review_page, "notify"):
                    callback()
            plan = result.plan_store.load(result.task_id)
            verification = PlanVerifier().verify(plan)
            problems = []
            if not callable(callback):
                problems.append("real issue card did not expose explicit font-role action")
            font_entry = next(
                (entry for entry in plan.entries if entry.source_path == archive),
                None,
            )
            manifest = plan.metadata.get("associated_asset_manifest")
            assets = (
                tuple(manifest.get("assets") or ())
                if isinstance(manifest, Mapping)
                else ()
            )
            if font_entry is None or font_entry.role != "font" or len(assets) != 1:
                problems.append("button did not freeze one explicit zip font and manifest")
            if verification.status != PlanStatus.ACCEPTED or verification.issues:
                problems.append("explicit zip font Plan was not approvable before execution")

            executor = font_series_fixture.TVFontSeriesAssociationRedContracts(
                methodName="runTest"
            )
            task = {}
            ledger = {}
            try:
                with patch("src.task_queue.logger.exception"):
                    task, ledger, _service = await executor._approve_and_execute(
                        root=flow,
                        result=result,
                    )
            except Exception as exc:  # RED must end as one business assertion.
                problems.append(f"execution path raised {type(exc).__name__}: {exc}")
            if task.get("execution_status") != "completed":
                problems.append(
                    "explicit zip font did not pass execution: "
                    f"{task.get('error')}"
                )
            font_ledger = next(
                (
                    row
                    for row in ledger.get("entries", ())
                    if row.get("source") == str(archive)
                ),
                None,
            )
            if not isinstance(font_ledger, dict) or font_ledger.get("status") not in {
                "created",
                "reused_identical_asset",
            }:
                problems.append("execution ledger lost the approved zip font")
            catalog_files = tuple((flow / "series_catalog").glob("*.json"))
            if len(catalog_files) != 1:
                problems.append("completed zip font did not materialize one SeriesCatalog")
            else:
                catalog = json.loads(catalog_files[0].read_text("utf-8"))
                if not any(
                    row.get("role") == "fonts"
                    for row in catalog.get("associated_assets", ())
                    if isinstance(row, dict)
                ):
                    problems.append("SeriesCatalog lost the approved zip font association")
            if archive.read_bytes() != b"incident-shaped-explicit-font-zip":
                problems.append("execution changed the font source bytes")
            self.assertEqual([], problems, "\n".join(problems))

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
                movie_case = movie_fixture.MovieFontProductionRedContracts(
                    methodName="runTest"
                )
                try:
                    completed = movie_case._complete_movie(
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
                entries = [
                    entry
                    for entry in plan.entries
                    if entry.source_path in {source / name for name in menu_names}
                ]
                if len(entries) != 3:
                    problems.append(f"{order_index}: Menu01/02/03 source set was not exact")
                    continue
                targets = [entry.target_path.name for entry in entries]
                if len(set(targets)) != 3:
                    problems.append(f"{order_index}: Menu01/02/03 target slots collided")
                owner_refs = {
                    entry.presentation_asset.owner_version_ref
                    for entry in entries
                    if entry.presentation_asset is not None
                }
                if len(owner_refs) != 1 or None in owner_refs:
                    problems.append(f"{order_index}: DoVi extras lost one stable owner version")
                for index in range(1, 4):
                    marker = f"Menu{index:02d}"
                    if not any(marker in target for target in targets):
                        problems.append(f"{order_index}: target lost semantic label {marker}")
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

    async def test_p1_4_direct_tv_is_invalid_before_approval_and_cohort_measurement(self):
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
                if verification.status != PlanStatus.INVALID:
                    problems.append(f"{name}: authority-free direct TV Plan remained approvable")
                if "tv_authority_incomplete" not in issue_codes:
                    problems.append(f"{name}: no stable authority-blocked issue cohort")
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
                ),
                task_root=result.root / "tasks",
                record_root=result.root / "records",
                catalog_root=result.root / "work_catalog",
                series_catalog_root=result.root / "series_catalog",
                decision_state_root=result.root / "decisions",
                provider_link_catalog_root=result.root / "provider_links",
            )
            try:
                service._validate_provider_link_preflight(plan)
            except PlanExecutionError:
                rejected = True
            else:
                rejected = False
            problems = []
            if not rejected:
                problems.append("execution preflight accepted authority-free TV Plan")
            if plan.metadata.get("series_key") or plan.metadata.get(
                "provider_link_snapshot"
            ):
                problems.append("fixture unexpectedly gained complete TV authority")
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

    def test_unconfirmed_archive_controls_remain_fail_closed(self):
        self.assertTrue(is_font_archive(Path("Example [Fonts].7z")))
        self.assertFalse(is_font_archive(Path("Example [Fonts].zip")))
        self.assertFalse(is_font_archive(Path("Example [Fonts].rar")))


if __name__ == "__main__":
    unittest.main()
