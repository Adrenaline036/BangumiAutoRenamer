"""R21 regression contracts for source slices and provider-backed specials.

The queue/worker/Provider/``Rename.process/_process`` path is production code;
only provider transport and temporary filesystem roots are frozen.
"""

from __future__ import annotations
from contextlib import nullcontext

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
from src.rename.cleaner import parse_release_title
from src.rename.decision_state import DecisionCommandService, DecisionStateStore
from src.rename.episode_inference import build_semantic_context
from src.rename.get_info import Search
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.provider_search import BangumiProviderClient
from src.rename.media_classifier import LocalRole, classify_media
from src.task_queue import TaskQueueManager
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
)


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


def _classify_through_production_title_seam(
    *,
    item_path: Path,
    release_title: str,
    season_id: int,
):
    """Build direct classifier controls from the production title parser."""

    parsed_title = parse_release_title(release_title)
    return classify_media(
        item_path=item_path,
        candidate_name=item_path.stem,
        season_id=season_id,
        extra_context_name=build_semantic_context(
            item_path.name,
            parsed_title.primary.key[0],
        ),
    )


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
        "platform": str(item.get("platform") or ""),
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
                "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 23, "air_date": "2018-04-12"}],
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
                    {"season_number": 1, "name": "Season 1", "episode_count": 24, "air_date": "2011-04-06"},
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
        if key == (78102, 0):
            return {"season_number": 0, "episodes": ()}
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
                "alternative_titles": [
                    "辉夜大小姐想让我告白？",
                    "かぐや様は告らせたい？",
                    "辉夜大小姐想让我告白 第1季",
                    "辉夜大小姐想让我告白 第3季",
                    "かぐや様は告らせたい Season 1",
                    "かぐや様は告らせたい Season 3",
                ],
                "seasons": [
                    {"season_number": 0, "name": "Specials", "episode_count": 5},
                    {"season_number": 1, "name": "Season 1", "episode_count": 12, "air_date": "2019-01-12"},
                    {"season_number": 2, "name": "Season 2", "episode_count": 12, "air_date": "2020-04-11"},
                    {"season_number": 3, "name": "Season 3", "episode_count": 13, "air_date": "2022-04-09"},
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


class _KaguyaMainSession:
    def post(self, _url, *, json=None, **_kwargs):
        return production_fixture._Response({"data": [self._subject()]})

    def get(self, url, **_kwargs):
        # The selected S2 belongs to a three-season component. The fixture
        # supplies the official response shapes for both relations and episodes.
        if '/episodes?' in str(url):
            return production_fixture._Response(dict(total=0, limit=100, offset=0, data=[]))
        subject_id = int(str(url).split('/subjects/')[1].split('/')[0])
        if str(url).endswith("/subjects"):
            adjacent = {293048: [(293049, '续集')],
                        293049: [(293048, '前传'), (293050, '续集')],
                        293050: [(293049, '前传')]}
            return production_fixture._Response([
                {**self._subject(key), 'relation': relation}
                for key, relation in adjacent[subject_id]])
        return production_fixture._Response(self._subject(subject_id))

    @staticmethod
    def _subject(subject_id=293049):
        if subject_id != 293049:
            season = 1 if subject_id == 293048 else 3
            return dict(id=subject_id, type=2, name=f'かぐや様は告らせたい Season {season}',
                name_cn=f'辉夜大小姐想让我告白 第{season}季',
                date='2019-01-12' if season == 1 else '2022-04-09',
                eps=12 if season == 1 else 13, platform='TV',
                images={'large': production_fixture.POSTER_URL})
        return {
            "id": 293049, "type": 2,
            "name": "かぐや様は告らせたい？",
            "name_cn": "辉夜大小姐想让我告白？",
            "date": "2020-04-11", "eps": 12, "platform": "TV",
            "images": {"large": production_fixture.POSTER_URL},
        }

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
    renamer.bangumi_search = BangumiProviderClient(
        user_agent="BangumiAutoRenamer-R21-contract/0.1",
        session=_KaguyaMainSession(),
        sleeper=lambda _seconds: None,
    )
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
        self._text_index = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def classes(self, *_args):
        return self

    def style(self, *_args):
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

    def bind_visibility_from(self, *_args, **_kwargs):
        return self

    def bind_enabled_from(self, *_args, **_kwargs):
        return self

    def bind_text_from(self, *_args, **_kwargs):
        return self

    def update(self):
        return None

    def on_value_change(self, callback):
        self.handler = callback
        return self

    def set_options(self, options, *, value=None):
        self.options = options
        self.value = value
        return self

    def set_visibility(self, visible):
        self.visible = visible
        return self

    def set_enabled(self, enabled):
        self.enabled = enabled
        return self

    def set_text(self, value):
        self.text = str(value)
        if self._ui is not None and self._text_index is not None:
            self._ui.visible[self._text_index] = self.text
        return self


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

    def label(self, value='', **_kwargs):
        self.visible.append(str(value))
        node = self._node()
        node._text_index = len(self.visible) - 1
        node.text = str(value)
        return node

    def icon(self, *_args, **_kwargs):
        return self._node()

    def spinner(self, *_args, **_kwargs):
        return self._node()

    def linear_progress(self, *_args, **_kwargs):
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
            node.set_value(matches[0])
            return True
    return False


class ComplexLibrarySpecialRedContracts(unittest.IsolatedAsyncioTestCase):
    def _assert_special_details(self, ui, expected):
        """Select and expand each candidate; stale or missing details must fail."""
        client = ui.driver.client
        expansions = [e for e in client.elements.values()
                      if type(e).__name__ == 'Expansion'
                      and getattr(e, 'text', '') == '查看特别篇详细资料']
        self.assertEqual(1, len(expansions))
        details = expansions[0]
        self.assertFalse(details.value)
        with client:
            details.set_value(True)
            for number, identifier, title, date, runtime in expected:
                self.assertTrue(_select_special_episode(ui, number))
                options = [options for options, _ in ui.selects
                           if isinstance(options, dict) and identifier in options]
                self.assertEqual([f'TMDB: S00E{number:02d}'],
                                 [option[identifier] for option in options])
                labels = [str(e.text) for e in client.elements.values()
                          if type(e).__name__ == 'Label'
                          and str(getattr(e, 'text', '')).startswith('标题：')]
                self.assertEqual([
                    f'标题：{title}\n日期：{date}\n时长：{runtime}\n官方分集 ID：{identifier}'
                ], labels)
                self.assertTrue(details.value)
            details.set_value(False)

    def _render_issue_actions(self, result, *, selected_source: Path):
        plan = result.plan_store.load(result.task_id)
        state = result.decision_store.load(result.task_id)
        matching = [entry for entry in plan.entries if entry.source_path == selected_source]
        if len(matching) != 1:
            raise AssertionError(
                f"real persisted Plan did not retain exactly one selected source: {matching}"
            )
        selected_entry = matching[0]
        dialog = SimpleNamespace()
        dialog.plan = plan
        dialog.task_decision = state
        dialog.task_data = dict(result.task)
        dialog.snapshot = build_plan_review_snapshot(plan)
        dialog.issues_container = _UiNode()
        dialog.review_service = PlanReviewService(
            result.plan_store,
            result.root / "reviews",
            result.root / "work_catalog",
            decision_state_root=result.root/'decisions', task_root=result.root/'tasks',
            record_root=result.root/'records', provider_link_catalog_root=result.root/'provider_links',
        )
        from src.rename.initial_movie_completion import RootSnapshotSource
        from src.rename.target_routing import TargetRootSnapshot
        roots = TargetRootSnapshot.capture({'bangumi_path': result.root/'TV', 'movie_path': result.root/'Movies',
            'anime_path': result.root/'Anime', 'anime_movie_path': result.root/'AnimeMovies'})
        dialog.review_service.root_snapshot_source = RootSnapshotSource(lambda: roots)
        dialog.decision_command_service = DecisionCommandService(
            result.decision_store, result.plan_store
        )
        dialog.selected_ids = {entry_id(selected_entry)}
        dialog.exit_requested = dialog.is_deleted = False
        dialog._adopt_revision = MagicMock()
        for name in ('_apply_exceptional_special_inline', '_apply_provider_special_inline',
                     '_partition_exceptional_as_cross_work', '_partition_selected_as_cross_work',
                     '_open_source_regrouping',
                     '_render_asset_role_repair', '_rebuild_inventory_role', '_render_asset_owner_repair',
                     '_open_content_exclusion'):
            setattr(dialog, name, getattr(PlanReviewDialog, name).__get__(dialog))
        dialog._exceptional_entry_ids = PlanReviewDialog._exceptional_entry_ids
        from nicegui import ui as actual_ui
        from nicegui.client import Client
        from nicegui.page import page as gui_page
        from tests._gui_helpers import GuiDriver
        client = Client(gui_page('/isolated-real-special'), request=None)
        self.addCleanup(client.delete)
        dialog.client = client
        driver = GuiDriver('runTest')
        driver.client = client
        with client, patch.object(review_page, 'ui', actual_ui), patch.object(review_page, 'notify'):
            dialog.issues_container = actual_ui.column()
            PlanReviewDialog._render_issues(dialog)
            # Frozen v1 transaction-event adapter, not the current issue panel.
            # Current dual-picker reachability is tested in special_crosswalk_gui.
            from tests.legacy_exceptional_ui_fixture import render_legacy_events
            render_legacy_events(dialog, ui_api=actual_ui)
            dialog._set_query = lambda _value: None
            dialog._set_view_mode = lambda _value: None
            from src.pages.plan_file_list import PlanFileListMixin
            dialog._refresh_official_special_action = PlanFileListMixin._refresh_official_special_action.__get__(dialog)
            dialog._refresh_movie_primary_action = PlanFileListMixin._refresh_movie_primary_action.__get__(dialog)
            PlanFileListMixin._render_plan_tools(dialog)
            PlanFileListMixin._render_toolbar(dialog)
        elements = tuple(client.elements.values())
        selects = [(e.options, e) for e in elements if type(e).__name__ == 'Select']
        visible = [str(e.text) for e in elements if hasattr(e, 'text')]
        visible.extend(str(label) for options, _e in selects if isinstance(options, dict) for label in options.values())
        async def click(label):
            with patch.object(review_page, 'notify'):
                await driver.click(label)
        callbacks = {e.text: (lambda label=e.text: click(label)) for e in elements if type(e).__name__ == 'Button'}
        return dialog, SimpleNamespace(visible=visible, selects=selects, callbacks=callbacks, driver=driver), selected_entry

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

        )
        subjects = dict(production_fixture.YAMA_SUBJECTS)
        subjects.update(STEINS_SUBJECTS)
        relations = dict(production_fixture.YAMA_RELATIONS)
        relations.update(STEINS_RELATIONS)
        with (patch.dict(production_fixture.YAMA_SUBJECTS, subjects, clear=True),
patch.dict(production_fixture.YAMA_RELATIONS, relations, clear=True),
patch("src.task_queue.Rename", return_value=renamer),
patch.multiple("src.rename.process", TASK_PATH=tasks, PLAN_PATH=plans,
                SERIES_CATALOG_PATH=root / "series_catalog", RECORD_PATH=root / "records",
                DECISION_STATE_PATH=decisions, PROVIDER_LINK_CATALOG_PATH=root / "provider_links"),
patch.object(add_task_page, "task_queue", manager),
patch.object(add_task_page, "notify")):
            await manager.start()
            try:
                from tests.workflow_gui_fixture import add_source
                submit = await add_source(source)
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
                    task_data=manager._read(task_id),
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
            selection = current_provider_selection_context(result.task)
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

    async def test_real_content_workbench_excludes_only_exact_23beta_source(self):
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
            from src.rename.plan_review import PlanReviewError
            from src.rename.initial_movie_completion import RootSnapshotSource
            from src.rename.target_routing import TargetRootSnapshot
            review.root_snapshot_source = RootSnapshotSource(
                lambda: TargetRootSnapshot.from_dict(before.metadata['target_root_snapshot']))
            with self.assertRaises(PlanReviewError):
                review.approve_entries(before.task_id, result.plan_store.fingerprint(before),
                    tuple(entry_id(entry) for entry in before.entries))
            dialog = SimpleNamespace()
            dialog.plan = before
            dialog.selected_ids = {entry_id(beta_entry)}
            dialog.review_service = review
            dialog._adopt_revision = MagicMock()
            from nicegui import ui as actual_ui
            from nicegui.client import Client
            from nicegui.page import page as gui_page
            from tests._gui_helpers import GuiDriver
            client = Client(gui_page('/isolated-exclude-beta'), request=None)
            self.addCleanup(client.delete)
            dialog.client = client
            driver = GuiDriver('runTest')
            driver.client = client
            with client, patch.object(review_page, 'ui', actual_ui), patch.object(review_page, 'notify'):
                await PlanReviewDialog._ignore_selected_entry(dialog)
                callback = driver.button('排除整理')
                await driver.click('排除整理')
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
        self.assertNotEqual(decision_before, decision_after)
        self.assertEqual(json.loads(decision_before)['confirmed_work'], json.loads(decision_after)['confirmed_work'])
        self.assertEqual(source_before, source_after)
        self.assertEqual({entry.source_path for entry in before.entries} - {beta},
                         {entry.source_path for entry in revised.entries})
        self.assertEqual("draft", review_after.status.value)
        self.assertEqual((), review_after.approved_entry_ids)

    async def test_first_kiss_bangumi_tv_subject_supplies_season_authority(self):
        """The actionable Bangumi TV subject selects the episodic projector."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sources" / "Kaguya First Kiss (2023)"
            for episode in range(1, 5):
                _write(source / f"Kaguya First Kiss [{episode:02d}].mkv", bytes([episode]))
            candidate = {
                "namespace": "bangumi", "entity_type": "subject", "provider_id": "425211",
                "title": "辉夜大小姐想让我告白：初吻不会结束",
                "original_title": "かぐや様は告らせたい",
                "year": 2023,
                "poster_url": production_fixture.POSTER_URL,
                "source_url": "https://bgm.tv/subject/425211",
                "snapshot_at": "2026-08-24T00:00:00+00:00",
                "platform": "TV",
            }
            result = await self._run(
                root / "flow", source, "first-kiss-tv", "425211",
                candidate=candidate,
                renamer_factory=_configured_kaguya_renamer,
            )
            plan = result.plan_store.load("first-kiss-tv")
            primary = plan.work_identity.primary_provider if plan.work_identity else None
            targets = tuple(entry.target_path for entry in plan.entries)
        self.assertEqual(
            "succeeded", current_provider_selection_context(result.task).get("state")
        )
        self.assertIsNotNone(primary)
        self.assertEqual(("bangumi", "subject", "425211"), (
            primary.namespace, primary.entity_type, primary.provider_id,
        ))
        self.assertTrue(targets)
        self.assertTrue(all("Season 01" in target.parts for target in targets))
        self.assertTrue(all("(2023)" in str(target) for target in targets))
        self.assertEqual({"tv_episodes"}, {group.kind for group in plan.presentation_groups})
        self.assertEqual({1, 2, 3, 4}, {entry.episode_id for entry in plan.entries})
        self.assertEqual({1}, {entry.season_id for entry in plan.entries})
        self.assertIn(
            ("tmdb", "tv", "330299"),
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
                "confirmed_work": result.state.confirmed_work,
                "linked_provider_identities": result.state.linked_provider_identities,
                "season_binding": result.state.season_binding,
                "series_binding": result.state.series_binding,
                "targets": [str(entry.target_path) for entry in plan.entries],
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        self.assertNotIn("997317", authority_text)

    async def test_23beta_single_file_binds_original_series_s00e06(self):
        """The isolated child needs explicit provider-backed official-special authority."""

        problems = []
        package_title = "[VCB-Studio] Steins;Gate 0 [Ma10p_1080p]"
        for marker in ("23β", "23b", "23B"):
            marker_source_name = (
                f"[VCB-Studio] Steins;Gate [{marker}]"
                "[Ma10p_1080p][x265_flac].mkv"
            )
            for context_path, release_title in (
                (
                    Path("source") / marker_source_name,
                    marker_source_name,
                ),
                (
                    Path("source") / "SPs" / marker_source_name,
                    package_title,
                ),
            ):
                classification = _classify_through_production_title_seam(
                    item_path=context_path,
                    release_title=release_title,
                    season_id=1,
                )
                if (
                    classification.local_role != LocalRole.EPISODE_LIKE_BONUS
                    or classification.episode_number != 23
                ):
                    problems.append(
                        f"{marker} did not retain typed episode-like eligibility "
                        f"through the production title seam in "
                        f"{context_path.parent.name}: {classification!r}"
                    )
        for marker in ("CM23", "PV01", "Menu04_2", "NCOP01_EP01-03", "NCED"):
            marker_name = f"[Group] Series [{marker}].mkv"
            classification = _classify_through_production_title_seam(
                item_path=Path("source") / "SPs" / marker_name,
                release_title="[Group] Series [Ma10p_1080p]",
                season_id=1,
            )
            if classification.local_role != LocalRole.DISC_EXTRA:
                problems.append(
                    f"clear extra {marker} was promoted by the 23β rule"
                )
        for marker in ("23β", "23b", "23B"):
            embedded_name = f"Steins {marker} Commentary.mkv"
            embedded = _classify_through_production_title_seam(
                item_path=Path("source") / embedded_name,
                release_title=embedded_name,
                season_id=1,
            )
            if embedded.local_role == LocalRole.EPISODE_LIKE_BONUS:
                problems.append(
                    f"non-bracketed {marker} text was treated as an episode token"
                )

        source_name = (
            "[VCB-Studio] Steins;Gate [23β]"
            "[Ma10p_1080p][x265_flac].mkv"
        )

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
                root / "sources" / source_name,
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
            self._assert_special_details(ui, (
                (5, '123005', 'Decoy Special', '2014-10-15', 24),
                (6, '123006', 'Open the Missing Link - Divide By Zero', '2015-12-03', 24),
            ))
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
        if current_provider_selection_context(result.task).get("state") != "succeeded":
            problems.append("single-file Provider selection did not complete")
        if (42509, 0) not in SPECIAL_EVIDENCE_CALLS:
            problems.append("worker did not fetch provider Season 0 episode evidence")
        if callback is None:
            problems.append("审核问题卡没有可达的“归入特别篇”动作")
        visible = "\n".join(ui.visible)
        for evidence in ("S00E05", "S00E06"):
            if evidence not in visible:
                problems.append(f"特别篇证据未展示：{evidence}")
        if primary is None or (
            primary.namespace, primary.entity_type, primary.provider_id
        ) != ("bangumi", "subject", "10380"):
            problems.append("original Steins;Gate provider identity was not retained")
        expected_decision = {
            "provider_namespace": "tmdb",
            "provider_entity_type": "tv",
            "provider_id": "42509",
            "season_number": 0,
            "episode_number": 6,
            "provider_episode_id": "123006",
        }
        if (decision.get('action') != 'confirmed_special_correspondence'
                or decision.get('policy') != 'special-selection.v2'
                or {key: decision.get('tmdb_row', {}).get(key) for key in expected_decision} != expected_decision):
            problems.append(f"DecisionState special binding mismatch: {decision!r}")
        if "provider_special_decision_required" in issue_codes:
            problems.append("23β决定落盘后仍被Verifier阻断")
        if issue_codes != set():
            problems.append(f"23β决定后应只等待核字体：{issue_codes}")
        if len(targets) != 1 or "Season 00" not in targets[0].parts:
            problems.append(f"23β target is not uniquely inside Season 00: {targets}")
        elif "S00E06" not in targets[0].name:
            problems.append(f"23β target is not official S00E06: {targets[0].name}")
        if any("Steins;Gate 0" in str(target) for target in targets):
            problems.append("23β leaked back into the SG0 work target")
        if not source_unchanged:
            problems.append("Plan-only special binding modified the source")
        self.assertEqual([], problems)

    async def test_episode_like_bonus_review_contract_is_tri_state(self):
        """Only a typed narrative bonus reaches the official-special decisions."""

        problems = []
        bonus_classification = _classify_through_production_title_seam(
            item_path=Path("SPs/Kaguya OVA 01.mkv"),
            release_title="Kaguya S2 [Ma10p_1080p]",
            season_id=2,
        )
        if (
            bonus_classification.local_role != LocalRole.EPISODE_LIKE_BONUS
            or bonus_classification.season_scope != 2
        ):
            problems.append("narrative OVA did not retain typed bonus eligibility")

        candidate = {
            "namespace": "bangumi",
            "entity_type": "subject",
            "provider_id": "293049",
            "title": "辉夜大小姐想让我告白？",
            "original_title": "かぐや様は告らせたい？",
            "year": 2020,
            "poster_url": production_fixture.POSTER_URL,
            "source_url": "https://bgm.tv/subject/293049",
            "snapshot_at": "2026-08-24T00:00:00+00:00",
            "platform": "TV",
        }
        for index, action in enumerate(("归入特别篇", "内容保留与排除")):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "sources" / "辉夜大小姐想让我告白？"
                _write(source / "Kaguya S02E01.mkv", b"episode")
                bonus = _write(
                    source
                    / "SPs"
                    / "辉夜大小姐想让我告白？ OVA 01.mkv",
                    b"bonus",
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
                bonus_before = [
                    entry for entry in before.entries if entry.source_path == bonus
                ]
                if len(bonus_before) != 1:
                    problems.append("real Kaguya Plan did not retain the OVA exactly once")
                    continue
                role = bonus_before[0].presentation_asset
                if role is None or role.asset_role.subtype is not None:
                    problems.append("待人工确认的用途不得提前签发特别篇子类型")
                if role is None or role.asset_role.kind != 'unknown' or 'ManualReview' not in bonus_before[0].target_path.parts:
                    problems.append('narrative OVA must remain unconfirmed before an explicit choice')
                if "S00E05" in bonus_before[0].target_path.name:
                    problems.append("narrative OVA was auto-filled into S00E05")
                dialog, ui, selected_entry = self._render_issue_actions(
                    result, selected_source=bonus
                )
                self._assert_special_details(ui, (
                    (4, '4340225', 'Kaguya Decoy Special', '2022-12-24', 24),
                    (5, '4340226', 'Kaguya-sama: Love Is War - New Year Special - This Year Never Ends',
                     '2023-01-01', 60),
                ))
                if action == "归入特别篇" and not _select_special_episode(ui, 5):
                    problems.append("辉夜多候选问题卡无法选择S00E05")
                callback = ui.callbacks.get(action)
                if callback is not None:
                    with patch.object(review_page, "notify"):
                        outcome = callback()
                        if inspect.isawaitable(outcome):
                            await outcome
                        if action == '内容保留与排除':
                            await ui.driver.click('排除整理')
                if (83121, 0) not in SPECIAL_EVIDENCE_CALLS:
                    problems.append("worker did not freeze TMDB Season 0 episode evidence")
                if "官方特别篇候选" not in ui.visible:
                    problems.append("问题卡没有展示简洁的官方特别篇候选")
                visible = "\n".join(ui.visible)
                for evidence in ("S00E04", "S00E05"):
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
                    if entry.source_path == bonus
                ) if action == '归入特别篇' else None
                if action == "归入特别篇":
                    expected = {
                        "provider_namespace": "tmdb",
                        "provider_entity_type": "tv",
                        "provider_id": "83121",
                        "season_number": 0,
                        "episode_number": 5,
                        "provider_episode_id": "4340226",
                    }
                    if (decision.get('action') != 'confirmed_special_correspondence'
                            or decision.get('policy') != 'special-selection.v2'
                            or {key: decision.get('tmdb_row', {}).get(key) for key in expected} != expected):
                        problems.append(f"official special decision mismatch: {decision!r}")
                    if "Season 00" not in target.parts or "S00E05" not in target.name:
                        problems.append(f"official candidate did not project to S00E05: {target}")
                else:
                    if decision or bonus not in persisted.ignored_sources or any(
                            e.source_path == bonus for e in persisted.entries):
                        problems.append('本次不整理应排除OVA，不签发特别篇决定')
                if "provider_special_decision_required" in issue_codes:
                    problems.append(f"{action}落盘后仍被Verifier阻断")
                if issue_codes != set():
                    problems.append(f"{action}落盘后应只等待核字体：{issue_codes}")

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
