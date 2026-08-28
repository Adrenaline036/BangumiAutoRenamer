"""R21 regression contracts for source slices and provider-backed specials.

The queue/worker/Provider/``Rename.process/_process`` path is production code;
only provider transport and temporary filesystem roots are frozen.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import src.pages.add_task_page as add_task_page
import src.pages.plan_review_page as review_page
import src.rename.get_info as get_info_module
import src.utils.utils as utils_module
import tests.test_r1_real_media_production_red_contract as production_fixture
from src.pages.add_task_page import AddTaskDialog
from src.pages.plan_review_page import PlanReviewDialog, build_plan_review_snapshot
from src.rename.decision_state import DecisionCommandService, DecisionStateStore
from src.rename.get_info import Search
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.provider_search import BangumiProviderClient
from src.rename.media_classifier import LocalRole, classify_media
from src.task_queue import TaskQueueManager


STEINS_SUBJECTS = {
    "10380": {
        "id": 10380, "type": 2, "name": "STEINS;GATE",
        "name_cn": "命运石之门", "date": "2011-04-06", "eps": 24,
        "platform": "TV", "images": {"large": production_fixture.POSTER_URL},
    },
    "129807": {
        "id": 129807, "type": 2, "name": "STEINS;GATE 0",
        "name_cn": "命运石之门 0", "date": "2018-04-12", "eps": 23,
        "platform": "TV", "images": {"large": production_fixture.POSTER_URL},
    },
}


STEINS_RELATIONS = {
    "10380": (),
    "129807": (),
}


SPECIAL_EVIDENCE_CALLS: list[tuple[int, int]] = []

_BASE_CONFIGURED_RENAMER = production_fixture._configured_renamer


def _candidate(subject_id: str) -> dict[str, object]:
    item = STEINS_SUBJECTS[subject_id]
    return {
        "namespace": "bangumi",
        "entity_type": "subject",
        "provider_id": subject_id,
        "title": item["name_cn"],
        "original_title": item["name"],
        "year": int(str(item["date"])[:4]),
        "poster_url": production_fixture.POSTER_URL,
        "source_url": f"https://bgm.tv/subject/{subject_id}",
        "snapshot_at": "2026-08-24T00:00:00+00:00",
    }


class _SteinsSearch(production_fixture._FrozenSearch):
    def get_tv_info(self, query, _year=None):
        value = str(query or "").casefold()
        if "0" in value or "zero" in value:
            return "命运石之门 0", {
                "id": 78102,
                "name": "命运石之门 0",
                "original_name": "Steins;Gate 0",
                "first_air_date": "2018-04-12",
                "genres": [{"name": "Animation"}],
                "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 23}],
            }
        if "steins" in value or "命运石" in value:
            return "命运石之门", {
                "id": 42509,
                "name": "命运石之门",
                "original_name": "Steins;Gate",
                "first_air_date": "2011-04-06",
                "genres": [{"name": "Animation"}],
                "seasons": [
                    {"season_number": 0, "name": "Specials", "episode_count": 10},
                    {"season_number": 1, "name": "Season 1", "episode_count": 24},
                ],
            }
        return "", None

    def get_season_info(self, provider_id, season_number):
        key = (int(provider_id), int(season_number))
        SPECIAL_EVIDENCE_CALLS.append(key)
        if key == (42509, 0):
            return {"season_number": 0, "episodes": (
                {
                    "id": 123005,
                    "episode_number": 5,
                    "name": "Decoy Special",
                    "air_date": "2014-10-15",
                    "runtime": 24,
                },
                {
                    "id": 123006,
                    "episode_number": 6,
                    "name": "Open the Missing Link - Divide By Zero",
                    "air_date": "2015-12-03",
                    "runtime": 24,
                },
            )}
        return None


class _KaguyaSearch(production_fixture._FrozenSearch):
    def get_tv_info(self, query, _year=None):
        if "辉夜" in str(query) or "kaguya" in str(query).casefold():
            return "辉夜大小姐想让我告白：初吻不会结束", {
                "id": 330299,
                "name": "辉夜大小姐想让我告白：初吻不会结束",
                "original_name": "かぐや様は告らせたい-ファーストキッスは終わらない-",
                "first_air_date": "2023-03-31",
                "genres": [{"name": "Animation"}],
                "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 4}],
            }
        return "", None

    def get_movie_info(self, query, _year=None):
        if "辉夜" in str(query) or "kaguya" in str(query).casefold():
            return "辉夜大小姐想让我告白：初吻不会结束", {
                "id": 997317,
                "title": "辉夜大小姐想让我告白：初吻不会结束",
                "original_title": "かぐや様は告らせたい",
                "release_date": "2022-12-17",
                "genres": [{"name": "Animation"}],
            }
        return "", None

    def get_tv_info_by_id(self, provider_id):
        return self.get_tv_info("辉夜") if int(provider_id) == 330299 else ("", None)

    def get_movie_info_by_id(self, provider_id):
        return self.get_movie_info("辉夜") if int(provider_id) == 997317 else ("", None)


class _KaguyaMainSearch(production_fixture._FrozenSearch):
    def get_tv_info(self, query, _year=None):
        if "辉夜" in str(query) or "kaguya" in str(query).casefold():
            return "辉夜大小姐想让我告白", {
                "id": 83121,
                "name": "辉夜大小姐想让我告白",
                "original_name": "かぐや様は告らせたい",
                "first_air_date": "2019-01-12",
                "genres": [{"name": "Animation"}],
                "seasons": [
                    {"season_number": 0, "name": "Specials", "episode_count": 5},
                    {"season_number": 1, "name": "Season 1", "episode_count": 12},
                    {"season_number": 2, "name": "Season 2", "episode_count": 12},
                    {"season_number": 3, "name": "Season 3", "episode_count": 13},
                ],
            }
        return "", None

    def get_tv_info_by_id(self, provider_id):
        return self.get_tv_info("辉夜") if int(provider_id) == 83121 else ("", None)

    def get_season_info(self, provider_id, season_number):
        key = (int(provider_id), int(season_number))
        SPECIAL_EVIDENCE_CALLS.append(key)
        if key == (83121, 0):
            return {"season_number": 0, "episodes": (
                {
                    "id": 4340225,
                    "episode_number": 4,
                    "name": "Kaguya Decoy Special",
                    "air_date": "2022-12-24",
                    "runtime": 24,
                },
                {
                    "id": 4340226,
                    "episode_number": 5,
                    "name": "Kaguya-sama: Love Is War - New Year Special - This Year Never Ends",
                    "air_date": "2023-01-01",
                    "runtime": 60,
                },
            )}
        return None


class _KaguyaSession:
    def post(self, _url, *, json=None, **_kwargs):
        return production_fixture._Response({"data": [{
            "id": 425211, "type": 2,
            "name": "かぐや様は告らせたい-ファーストキッスは終わらない-",
            "name_cn": "辉夜大小姐想让我告白：初吻不会结束",
            "date": "2023-03-31", "eps": 4, "platform": "TV",
            "images": {"large": production_fixture.POSTER_URL},
        }]})

    def get(self, url, **_kwargs):
        if str(url).endswith("/subjects"):
            return production_fixture._Response([])
        return production_fixture._Response({
            "id": 425211, "type": 2,
            "name": "かぐや様は告らせたい-ファーストキッスは終わらない-",
            "name_cn": "辉夜大小姐想让我告白：初吻不会结束",
            "date": "2023-03-31", "eps": 4, "platform": "TV",
            "images": {"large": production_fixture.POSTER_URL},
        })

    def get_tv_info_by_id(self, provider_id):
        if int(provider_id) == 42509:
            return self.get_tv_info("Steins;Gate")
        if int(provider_id) == 78102:
            return self.get_tv_info("Steins;Gate 0")
        return "", None


def _configured_renamer(root: Path):
    renamer = _BASE_CONFIGURED_RENAMER(root)
    renamer.search = _SteinsSearch()
    return renamer


def _configured_kaguya_renamer(root: Path):
    renamer = _BASE_CONFIGURED_RENAMER(root)
    renamer.search = _KaguyaSearch()
    renamer.bangumi_search = BangumiProviderClient(
        user_agent="BangumiAutoRenamer-R21-contract/0.1",
        session=_KaguyaSession(),
        sleeper=lambda _seconds: None,
    )
    return renamer


def _configured_kaguya_main_renamer(root: Path):
    renamer = _BASE_CONFIGURED_RENAMER(root)
    renamer.search = _KaguyaMainSearch()
    return renamer


def _write(path: Path, payload: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class _UiNode:
    def __init__(self, *, value=None, ui=None, button_label=None):
        self.value = value
        self._ui = ui
        self._button_label = button_label

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def classes(self, *_args):
        return self

    def props(self, value="", **_kwargs):
        if self._ui is not None:
            self._ui.props.append(str(value))
            if self._button_label is not None:
                self._ui.button_props[self._button_label] = str(value)
        return self

    def tooltip(self, value=""):
        if self._ui is not None:
            self._ui.visible.append(str(value))
        return self

    def clear(self):
        return None

    def update(self):
        return None


class _CaptureUi:
    def __init__(self, *, selected=None):
        self.selected = selected
        self.callbacks = {}
        self.visible = []
        self.props = []
        self.button_props = {}
        self.selects = []

    def _node(self, *, value=None, button_label=None):
        return _UiNode(value=value, ui=self, button_label=button_label)

    def expansion(self, *_args, **_kwargs):
        return self._node()

    def column(self, *_args, **_kwargs):
        return self._node()

    def row(self, *_args, **_kwargs):
        return self._node()

    def label(self, value, **_kwargs):
        self.visible.append(str(value))
        return self._node()

    def icon(self, *_args, **_kwargs):
        return self._node()

    def separator(self):
        return self._node()

    def input(self, *_args, **kwargs):
        return self._node(value=kwargs.get("value"))

    def number(self, *_args, **kwargs):
        return self._node(value=kwargs.get("value"))

    def checkbox(self, *_args, **kwargs):
        return self._node(value=kwargs.get("value", False))

    def select(self, _options, **kwargs):
        value = kwargs.get("value")
        if value is None and isinstance(_options, dict) and len(_options) == 1:
            value = next(iter(_options))
        if value is None:
            value = self.selected
        node = self._node(value=value)
        self.selects.append((_options, node))
        if isinstance(_options, dict):
            self.visible.extend(str(label) for label in _options.values())
        return node

    def button(self, label, *, on_click=None, **_kwargs):
        self.visible.append(str(label))
        if on_click is not None:
            self.callbacks[str(label)] = on_click
        return self._node(button_label=str(label))


class _NoopAnyDialog:
    def __init__(self, *_args, **_kwargs):
        pass

    def __await__(self):
        async def wait_once():
            return None

        return wait_once().__await__()

    def delete(self):
        return None


def _select_special_episode(ui: _CaptureUi, episode_number: int) -> bool:
    marker = f"S00E{episode_number:02d}"
    for options, node in ui.selects:
        if not isinstance(options, dict):
            continue
        matches = [
            key for key, label in options.items() if marker in str(label)
        ]
        if len(matches) == 1:
            node.value = matches[0]
            return True
    return False


class ComplexLibrarySpecialRedContracts(unittest.IsolatedAsyncioTestCase):
    def _render_issue_actions(self, result, *, selected_source: Path):
        plan = result.plan_store.load(result.task_id)
        state = result.decision_store.load(result.task_id)
        matching = [entry for entry in plan.entries if entry.source_path == selected_source]
        if len(matching) != 1:
            raise AssertionError(
                f"real persisted Plan did not retain exactly one selected source: {matching}"
            )
        selected_entry = matching[0]
        dialog = object.__new__(PlanReviewDialog)
        dialog.plan = plan
        dialog.task_decision = state
        dialog.task_data = dict(result.task)
        dialog.snapshot = build_plan_review_snapshot(plan)
        dialog.issues_container = _UiNode()
        dialog.review_service = PlanReviewService(
            result.plan_store,
            result.root / "reviews",
            result.root / "work_catalog",
        )
        dialog.decision_command_service = DecisionCommandService(
            result.decision_store, result.plan_store
        )
        dialog.selected_ids = {entry_id(selected_entry)}
        dialog._adopt_revision = MagicMock()
        ui = _CaptureUi(selected=entry_id(selected_entry))
        with (
            patch.object(review_page, "ui", ui),
            patch.object(review_page, "notify"),
        ):
            PlanReviewDialog._render_issues(dialog)
        return dialog, ui, selected_entry

    async def _run(
        self, root: Path, source: Path, task_id: str, subject_id: str,
        *, candidate=None, renamer_factory=_configured_renamer,
    ):
        harness = SimpleNamespace(
            _NoopReviewDialog=(
                production_fixture.R1RealQueueWorkerContracts._NoopReviewDialog
            )
        )
        subjects = dict(production_fixture.YAMA_SUBJECTS)
        subjects.update(STEINS_SUBJECTS)
        relations = dict(production_fixture.YAMA_RELATIONS)
        relations.update(STEINS_RELATIONS)
        with (
            patch.dict(production_fixture.YAMA_SUBJECTS, subjects, clear=True),
            patch.dict(production_fixture.YAMA_RELATIONS, relations, clear=True),
            patch.object(
                production_fixture,
                "_configured_renamer",
                side_effect=lambda actual_root: renamer_factory(actual_root),
            ),
        ):
            result = await production_fixture.R1RealQueueWorkerContracts._run_flow(
                harness,
                root=root,
                source=source,
                task_id=task_id,
                candidate=(candidate or _candidate(subject_id)),
                is_anime=True,
                is_movie=False,
            )
        result.task_id = task_id
        result.root = root
        return result

    async def _run_via_add_task(
        self, root: Path, source: Path, subject_id: str,
        *, candidate=None, renamer_factory=_configured_renamer,
    ):
        """Use the real Add Task submit method before the queue/worker/provider path."""

        tasks = root / "tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        plans = root / "plans"
        decisions = root / "decisions"
        renamer = renamer_factory(root)
        manager = TaskQueueManager(
            task_root=tasks,
            decision_state_root=decisions,
            recovery_abandon_audit_root=root / "abandon_audit",
            plan_root=plans,
            test_mode=False,
        )
        submit = SimpleNamespace(
            path=str(source),
            media_type="anime",
            use_ai=False,
            _submitting=False,
            submit_button=MagicMock(),
            close=MagicMock(),
        )
        subjects = dict(production_fixture.YAMA_SUBJECTS)
        subjects.update(STEINS_SUBJECTS)
        relations = dict(production_fixture.YAMA_RELATIONS)
        relations.update(STEINS_RELATIONS)
        with (
            patch.dict(production_fixture.YAMA_SUBJECTS, subjects, clear=True),
            patch.dict(production_fixture.YAMA_RELATIONS, relations, clear=True),
            patch("src.task_queue.Rename", return_value=renamer),
            patch("src.rename.process.TASK_PATH", tasks),
            patch("src.rename.process.PLAN_PATH", plans),
            patch("src.rename.process.DECISION_STATE_PATH", decisions),
            patch("src.rename.process.PROVIDER_LINK_CATALOG_PATH", root / "provider_links"),
            patch.object(add_task_page, "task_queue", manager),
            patch.object(add_task_page, "is_ui_test_mode", return_value=False),
            patch.object(add_task_page, "notify"),
        ):
            await manager.start()
            try:
                await AddTaskDialog._submit(submit)
                await asyncio.wait_for(manager._queue.join(), timeout=8)
                task_files = tuple(tasks.glob("*.json"))
                self.assertEqual(1, len(task_files), "Add Task must persist exactly one task")
                task_id = task_files[0].stem
                with (
                    patch.object(review_page, "TASK_PATH", tasks),
                    patch.object(review_page, "PLAN_PATH", plans),
                    patch.object(review_page, "DECISION_STATE_PATH", decisions),
                    patch.object(utils_module, "TASK_PATH", tasks),
                    patch.object(
                        review_page,
                        "PlanReviewDialog",
                        production_fixture.R1RealQueueWorkerContracts._NoopReviewDialog,
                    ),
                    patch.object(review_page, "RecognitionRecoveryDialog", _NoopAnyDialog),
                    patch.object(review_page, "_dialog_mount_context", return_value=nullcontext()),
                    patch.object(review_page, "notify"),
                ):
                    await review_page.plan_review_page(task_id)
                try:
                    initial_plan = PlanStore(plans).load(task_id)
                    initial_state = DecisionStateStore(decisions).load(task_id)
                except Exception as exc:
                    raise AssertionError(
                        "real Add Task→worker→review left no trusted Plan/DecisionState"
                    ) from exc
                dialog = SimpleNamespace(
                    task_decision=initial_state,
                    plan=initial_plan,
                    task_data={},
                    close=Mock(),
                )
                with (
                    patch.object(review_page, "task_queue", manager),
                    patch.object(review_page, "notify"),
                ):
                    await PlanReviewDialog._select_provider_candidate(
                        dialog, candidate or _candidate(subject_id)
                    )
                await asyncio.wait_for(manager._queue.join(), timeout=8)
            finally:
                await manager.stop()
        return SimpleNamespace(
            task_id=task_id,
            initial_plan=initial_plan,
            initial_state=initial_state,
            plan=PlanStore(plans).load(task_id),
            state=DecisionStateStore(decisions).load(task_id),
            task=json.loads((tasks / f"{task_id}.json").read_text("utf-8")),
            plan_store=PlanStore(plans),
            decision_store=DecisionStateStore(decisions),
            tasks=tasks,
            plans=plans,
            decisions=decisions,
            root=root,
            submit=submit,
        )

    async def test_single_file_add_task_reaches_real_provider_worker(self):
        """A plain single-file child is already a supported production source."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(
                root / "sources" / "[VCB-Studio] Yama no Susume [01][Ma10p_1080p].mkv"
            )
            try:
                result = await self._run_via_add_task(
                    root / "flow",
                    source,
                    "41658",
                    candidate=production_fixture._candidate("41658"),
                    renamer_factory=production_fixture._configured_renamer,
                )
            except Exception as exc:
                self.fail(f"single-file production flow did not reach review: {exc}")
            selection = result.task.get("selection_operation") or {}
            targets = tuple(result.plan.entries)
            problems = []
            if selection.get("state") != "succeeded":
                problems.append(f"selection state is {selection.get('state')!r}")
            if result.task.get("task_source") != "ui" or not result.submit.close.called:
                problems.append("single-file task did not pass through AddTaskDialog._submit")
            if result.state.revision <= result.initial_state.revision:
                problems.append("DecisionState revision did not advance")
            if not targets or any(item.source_path != source for item in targets):
                problems.append("single-file Plan lost or multiplied the exact source")
            if targets and any("Season 01" not in item.target_path.parts for item in targets):
                problems.append("ordinary single-file target is not in Season 01")
            self.assertEqual([], problems)

    async def test_real_ignore_button_excludes_only_exact_23beta_source(self):
        """The SG0 package keeps its work authority while exact 23β becomes a child."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "sources" / "[VCB-Studio] Steins;Gate 0 [Ma10p_1080p]"
            main = _write(package / "[VCB-Studio] Steins;Gate 0 [01].mkv", b"main")
            beta = _write(
                package / "SPs" / "[VCB-Studio] Steins;Gate [23β][Ma10p_1080p][x265_flac].mkv",
                b"23-beta",
            )
            sibling = _write(package / "SPs" / "[VCB-Studio] Steins;Gate 0 [Menu].mkv", b"menu")
            source_before = {
                path: (path.stat().st_size, path.read_bytes())
                for path in (main, beta, sibling)
            }
            result = await self._run(root / "flow", package, "sg0-exact-ignore", "129807")
            before = result.plan_store.load("sg0-exact-ignore")
            beta_entry = next(entry for entry in before.entries if entry.source_path == beta)
            before_sources = set(before.expected_sources)
            decision_path = root / "flow" / "decisions" / "sg0-exact-ignore.json"
            decision_before = decision_path.read_bytes()
            review = PlanReviewService(
                result.plan_store,
                root / "flow" / "reviews",
                root / "flow" / "work_catalog",
                decision_state_root=root / "flow" / "decisions",
                task_root=root / "flow" / "tasks",
            )
            review.approve_entries(
                before.task_id,
                result.plan_store.fingerprint(before),
                tuple(entry_id(entry) for entry in before.entries),
            )
            dialog = object.__new__(PlanReviewDialog)
            dialog.plan = before
            dialog.selected_ids = {entry_id(beta_entry)}
            dialog.review_service = review
            dialog._adopt_revision = MagicMock()
            ui = _CaptureUi()
            with (
                patch.object(review_page, "ui", ui),
                patch.object(review_page, "notify"),
            ):
                PlanReviewDialog._render_presentation_override(dialog)
                callback = ui.callbacks.get("本次不整理所选文件")
                if callback is not None:
                    dialog.presentation_reason.value = "23β属于原作特别篇，拆为独立child"
                    callback()
            revised = result.plan_store.load("sg0-exact-ignore")
            decision_after = decision_path.read_bytes()
            review_after = review.load("sg0-exact-ignore")
            source_after = {
                path: (path.stat().st_size, path.read_bytes())
                for path in (main, beta, sibling)
            }

        self.assertIsNotNone(callback, "审核页没有可达的“不整理所选文件”按钮")
        self.assertEqual(before_sources, set(revised.expected_sources))
        self.assertIn(beta, revised.ignored_sources)
        self.assertIn(main, revised.expected_sources)
        self.assertIn(sibling, revised.expected_sources)
        self.assertEqual(decision_before, decision_after)
        self.assertEqual(source_before, source_after)
        self.assertEqual(before_sources - {beta}, {entry.source_path for entry in revised.entries})
        self.assertEqual("draft", review_after.status.value)
        self.assertEqual((), review_after.approved_entry_ids)

    async def test_first_kiss_explicit_tv_provider_wins_over_movie_candidate(self):
        """The user's explicit TV 2023 choice must remain primary authority."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sources" / "Kaguya First Kiss (2023)"
            for episode in range(1, 5):
                _write(source / f"Kaguya First Kiss [{episode:02d}].mkv", bytes([episode]))
            candidate = {
                "namespace": "tmdb", "entity_type": "tv", "provider_id": "330299",
                "title": "辉夜大小姐想让我告白：初吻不会结束",
                "original_title": "かぐや様は告らせたい",
                "year": 2023,
                "poster_url": production_fixture.POSTER_URL,
                "source_url": "https://www.themoviedb.org/tv/330299",
                "snapshot_at": "2026-08-24T00:00:00+00:00",
            }
            result = await self._run(
                root / "flow", source, "first-kiss-tv", "425211",
                candidate=candidate,
                renamer_factory=_configured_kaguya_renamer,
            )
            plan = result.plan_store.load("first-kiss-tv")
            primary = plan.work_identity.primary_provider if plan.work_identity else None
            targets = tuple(entry.target_path for entry in plan.entries)
        self.assertEqual("succeeded", result.task.get("selection_operation", {}).get("state"))
        self.assertIsNotNone(primary)
        self.assertEqual(("tmdb", "tv", "330299"), (
            primary.namespace, primary.entity_type, primary.provider_id,
        ))
        self.assertTrue(targets)
        self.assertTrue(all("Season 01" in target.parts for target in targets))
        self.assertTrue(all("(2023)" in str(target) for target in targets))
        self.assertEqual({"tv_episodes"}, {group.kind for group in plan.presentation_groups})
        self.assertEqual({1, 2, 3, 4}, {entry.episode_id for entry in plan.entries})
        self.assertEqual({1}, {entry.season_id for entry in plan.entries})
        self.assertIn(
            ("bangumi", "subject", "425211"),
            {
                (item.namespace, item.entity_type, item.provider_id)
                for item in plan.linked_provider_identities
            },
        )
        self.assertEqual(
            DecisionStateStore.fingerprint(result.state),
            plan.metadata.get("decision_state_fingerprint"),
        )
        authority_text = json.dumps(
            {
                "task": result.task,
                "decision": result.state.to_dict(),
                "plan": plan.to_dict(),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        self.assertNotIn("997317", authority_text)

    async def test_23beta_single_file_binds_original_series_s00e06(self):
        """The isolated child needs explicit provider-backed official-special authority."""

        problems = []
        season_payload = {
            "season_number": 0,
            "episodes": [{
                "id": 123006,
                "episode_number": 6,
                "name": "Open the Missing Link - Divide By Zero",
                "air_date": "2015-12-03",
                "runtime": 24,
            }],
        }
        season_transport = MagicMock()
        season_transport.info.return_value = season_payload
        with patch.object(
            get_info_module.tmdb, "TV_Seasons", return_value=season_transport
        ):
            filtered = Search.get_season_info(object.__new__(Search), 42509, 0)
        filtered_rows = tuple((filtered or {}).get("episodes") or ())
        if len(filtered_rows) != 1 or filtered_rows[0].get("id") != 123006:
            problems.append("production Search.get_season_info dropped TMDB episode id")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(
                root / "sources" / "[VCB-Studio] Steins;Gate [23β][Ma10p_1080p][x265_flac].mkv",
                b"23-beta",
            )
            SPECIAL_EVIDENCE_CALLS.clear()
            try:
                result = await self._run_via_add_task(
                    root / "flow", source, "10380"
                )
            except Exception as exc:
                self.fail(f"23β single-file production flow stopped before review: {exc}")
            plan = result.plan_store.load(result.task_id)
            dialog, ui, selected_entry = self._render_issue_actions(
                result, selected_source=source
            )
            if not _select_special_episode(ui, 6):
                problems.append("多候选问题卡无法选择S00E06")
            callback = ui.callbacks.get("归入特别篇")
            if callback is not None:
                with patch.object(review_page, "notify"):
                    outcome = callback()
                    if inspect.isawaitable(outcome):
                        await outcome
            plan = result.plan_store.load(result.task_id)
            state = result.decision_store.load(result.task_id)
            primary = plan.work_identity.primary_provider if plan.work_identity else None
            targets = tuple(entry.target_path for entry in plan.entries)
            source_unchanged = source.read_bytes() == b"23-beta"
            decision = (
                state.to_dict().get("provider_special_decisions", {})
                .get(entry_id(selected_entry), {})
            )
            verification = PlanVerifier().verify(plan)
            issue_codes = {issue.code for issue in verification.issues}

        if result.task.get("task_source") != "ui" or not result.submit.close.called:
            problems.append("23β child did not enter through AddTaskDialog._submit")
        if result.task.get("selection_operation", {}).get("state") != "succeeded":
            problems.append("single-file Provider selection did not complete")
        if (42509, 0) not in SPECIAL_EVIDENCE_CALLS:
            problems.append("worker did not fetch provider Season 0 episode evidence")
        if callback is None:
            problems.append("审核问题卡没有可达的“归入特别篇”动作")
        visible = "\n".join(ui.visible)
        for evidence in (
            "S00E05", "Decoy Special", "2014-10-15", "24 分钟",
            "S00E06", "Open the Missing Link - Divide By Zero", "2015-12-03",
        ):
            if evidence not in visible:
                problems.append(f"特别篇证据未展示：{evidence}")
        if primary is None or (
            primary.namespace, primary.entity_type, primary.provider_id
        ) != ("bangumi", "subject", "10380"):
            problems.append("original Steins;Gate provider identity was not retained")
        expected_decision = {
            "action": "confirmed_official_special",
            "provider_namespace": "tmdb",
            "provider_entity_type": "tv",
            "provider_id": "42509",
            "season_number": 0,
            "episode_number": 6,
            "provider_episode_id": "123006",
        }
        if decision != expected_decision:
            problems.append(f"DecisionState special binding mismatch: {decision!r}")
        if "provider_special_decision_required" in issue_codes:
            problems.append("23β决定落盘后仍被Verifier阻断")
        if verification.status.value == "invalid":
            problems.append("23β决定落盘后Plan仍不可继续审核")
        if len(targets) != 1 or "Season 00" not in targets[0].parts:
            problems.append(f"23β target is not uniquely inside Season 00: {targets}")
        elif "S00E06" not in targets[0].name:
            problems.append(f"23β target is not official S00E06: {targets[0].name}")
        if any("Steins;Gate 0" in str(target) for target in targets):
            problems.append("23β leaked back into the SG0 work target")
        if not source_unchanged:
            problems.append("Plan-only special binding modified the source")
        self.assertEqual([], problems)

    async def test_special_review_contract_is_tri_state_and_keeps_world_premiere_extra(self):
        """Official match, Extra and needs-review must be explicit GUI decisions."""

        problems = []
        digest = classify_media(
            item_path=Path("SPs/World Premiere Digest.mkv"),
            candidate_name="World Premiere Digest",
            season_id=3,
            extra_context_name="World Premiere Digest",
        )
        if digest.local_role != LocalRole.DISC_EXTRA or digest.season_scope != 3:
            problems.append("World Premiere Digest was auto-promoted instead of staying Season03 Extras")

        candidate = {
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": "83121",
            "title": "辉夜大小姐想让我告白",
            "original_title": "かぐや様は告らせたい",
            "year": 2019,
            "poster_url": production_fixture.POSTER_URL,
            "source_url": "https://www.themoviedb.org/tv/83121",
            "snapshot_at": "2026-08-24T00:00:00+00:00",
        }
        for index, action in enumerate(("归入特别篇", "保留为附加内容")):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "sources" / "Kaguya S3"
                _write(source / "Kaguya S03E01.mkv", b"episode")
                world = _write(
                    source / "SPs" / "World Premiere Digest.mkv", b"digest"
                )
                source_before = {
                    path: (path.stat().st_size, path.read_bytes())
                    for path in source.rglob("*") if path.is_file()
                }
                SPECIAL_EVIDENCE_CALLS.clear()
                result = await self._run(
                    root / "flow",
                    source,
                    f"kaguya-special-{index}",
                    "10380",
                    candidate=candidate,
                    renamer_factory=_configured_kaguya_main_renamer,
                )
                before = result.plan_store.load(result.task_id)
                world_before = [
                    entry for entry in before.entries if entry.source_path == world
                ]
                if len(world_before) != 1:
                    problems.append("real Kaguya Plan did not retain the digest exactly once")
                    continue
                if "Season 03" not in world_before[0].target_path.parts or (
                    "Extras" not in world_before[0].target_path.parts
                ):
                    problems.append("World Premiere Digest was not conservative Season03/Extras")
                if "S00E05" in world_before[0].target_path.name:
                    problems.append("World Premiere Digest was auto-filled into S00E05")
                dialog, ui, selected_entry = self._render_issue_actions(
                    result, selected_source=world
                )
                if action == "归入特别篇" and not _select_special_episode(ui, 5):
                    problems.append("辉夜多候选问题卡无法选择S00E05")
                callback = ui.callbacks.get(action)
                if callback is not None:
                    with patch.object(review_page, "notify"):
                        outcome = callback()
                        if inspect.isawaitable(outcome):
                            await outcome
                if (83121, 0) not in SPECIAL_EVIDENCE_CALLS:
                    problems.append("worker did not freeze TMDB Season 0 episode evidence")
                if "官方特别篇候选" not in ui.visible:
                    problems.append("问题卡没有展示简洁的官方特别篇候选")
                visible = "\n".join(ui.visible)
                for evidence in (
                    "S00E04", "Kaguya Decoy Special", "2022-12-24", "24 分钟",
                    "S00E05", "This Year Never Ends", "2023-01-01", "60 分钟",
                ):
                    if evidence not in visible:
                        problems.append(f"辉夜特别篇证据未展示：{evidence}")
                if callback is None:
                    problems.append(f"问题卡没有可达的“{action}”动作")
                    continue
                persisted = result.plan_store.load(result.task_id)
                persisted_state = result.decision_store.load(result.task_id)
                verification = PlanVerifier().verify(persisted)
                issue_codes = {issue.code for issue in verification.issues}
                decision = (
                    persisted_state.to_dict().get("provider_special_decisions", {})
                    .get(entry_id(selected_entry), {})
                )
                target = next(
                    entry.target_path for entry in persisted.entries
                    if entry.source_path == world
                )
                if action == "归入特别篇":
                    expected = {
                        "action": "confirmed_official_special",
                        "provider_namespace": "tmdb",
                        "provider_entity_type": "tv",
                        "provider_id": "83121",
                        "season_number": 0,
                        "episode_number": 5,
                        "provider_episode_id": "4340226",
                    }
                    if decision != expected:
                        problems.append(f"official special decision mismatch: {decision!r}")
                    if "Season 00" not in target.parts or "S00E05" not in target.name:
                        problems.append(f"official candidate did not project to S00E05: {target}")
                else:
                    if decision != {"action": "unmatched_extra"}:
                        problems.append(f"keep-extra decision mismatch: {decision!r}")
                    if "Season 03" not in target.parts or "Extras" not in target.parts:
                        problems.append(f"keep-extra did not remain Season03/Extras: {target}")
                if "provider_special_decision_required" in issue_codes:
                    problems.append(f"{action}落盘后仍被Verifier阻断")
                if (
                    verification.status.value != "invalid"
                    or "tv_authority_incomplete" not in issue_codes
                ):
                    problems.append(
                        f"{action}落盘后未保持direct-TMDB动画TV的authority阻断"
                    )

                source_after = {
                    path: (path.stat().st_size, path.read_bytes())
                    for path in source.rglob("*") if path.is_file()
                }
                if source_after != source_before:
                    problems.append(f"{action} Plan-only callback modified source files")

                authority_root = root / "flow"
                frozen = {
                    path.relative_to(authority_root).as_posix(): path.read_bytes()
                    for path in authority_root.rglob("*") if path.is_file()
                }
                with patch.object(review_page, "notify"):
                    outcome = callback()
                    if inspect.isawaitable(outcome):
                        await outcome
                after_stale = {
                    path.relative_to(authority_root).as_posix(): path.read_bytes()
                    for path in authority_root.rglob("*") if path.is_file()
                }
                if after_stale != frozen:
                    problems.append(f"stale {action} callback rewrote persisted authority")
        self.assertEqual([], problems)


if __name__ == "__main__":
    unittest.main()
