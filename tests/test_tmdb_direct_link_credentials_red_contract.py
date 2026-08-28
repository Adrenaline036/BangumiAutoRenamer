"""Regression contracts for TMDB direct-link credential parity.

The primary contract starts at the real review-page URL handler, keeps the
single production task queue, worker, ``Rename.process/_process`` and stores in
the path, and fakes only remote provider transports.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, nullcontext
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import requests

import src.config.config_manager as config_module
import src.pages.plan_review_page as review_page
import src.rename.get_info as get_info
import src.rename.process as process_module
import src.rename.provider_search as provider_search
import src.utils.utils as utils_module
import tests.test_r1_real_media_production_red_contract as r1_fixture
from src.config.config_manager import ConfigManager
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.decision_state import DecisionStateStore
from src.rename.plan_store import PlanStore
from src.rename.process import Rename as ProductionRename
from src.rename.provider_search import BangumiProviderClient
from src.task_queue import TaskQueueManager


RUNTIME_KEY = "runtime-tmdb-sentinel-r19"
STORED_KEY = "stored-tmdb-sentinel-r19"


class _Response:
    def __init__(self, payload: object, *, error: Exception | None = None):
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _ManualProviderSession:
    """Frozen HTTP boundary for manual TMDB/Bangumi detail links."""

    def __init__(self, *, mode: str = "ok"):
        self.mode = mode
        self.calls: list[dict[str, object]] = []

    def get(self, url, *, params=None, headers=None, timeout=None):
        call = {
            "url": str(url),
            "params": dict(params or {}),
            "headers": dict(headers or {}),
            "timeout": timeout,
        }
        self.calls.append(call)
        if self.mode == "timeout":
            raise requests.Timeout("TMDB 请求超时")
        if self.mode == "http_error":
            return _Response({}, error=requests.HTTPError("TMDB 请求被拒绝"))
        if self.mode == "bad_json":
            return _Response(ValueError("invalid json"))
        if "api.themoviedb.org/3/movie/763096" in str(url):
            return _Response({
                "id": 763096,
                "title": "向山进发 回忆的礼物",
                "original_title": "ヤマノススメ おもいでプレゼント",
                "release_date": "2017-07-07",
                "poster_path": "/omoide.jpg",
            })
        if "api.themoviedb.org/3/tv/53918" in str(url):
            return _Response({
                "id": 53918,
                "name": "向山进发",
                "original_name": "ヤマノススメ",
                "first_air_date": "2013-01-03",
                "poster_path": "/yama.jpg",
            })
        if "api.bgm.tv/v0/subjects/216311" in str(url):
            return _Response(dict(r1_fixture.YAMA_SUBJECTS["216311"]))
        raise AssertionError(f"unexpected provider GET: {url}")

    def post(self, url, **_kwargs):
        raise AssertionError(f"unexpected provider POST: {url}")


def _candidate() -> dict[str, object]:
    return {
        "namespace": "tmdb",
        "entity_type": "movie",
        "provider_id": "763096",
        "title": "向山进发 回忆的礼物",
        "original_title": "ヤマノススメ おもいでプレゼント",
        "year": 2017,
        "poster_url": "https://image.tmdb.org/t/p/w342/omoide.jpg",
        "source_url": "https://www.themoviedb.org/movie/763096",
        "snapshot_at": "2026-08-23T00:00:00+00:00",
    }


def _config(root: Path, *, api_key: str) -> ConfigManager:
    config = ConfigManager(root / "config.json")
    config.config.update({
        "api_key": api_key,
        "bangumi_enabled": True,
        "bangumi_path": str(root / "TV"),
        "movie_path": str(root / "Movies"),
        "anime_path": str(root / "Anime"),
        "anime_movie_path": str(root / "AnimeMovies"),
        "ai_enabled": False,
    })
    config.write_config()
    return config


def _secret_hits(root: Path, secret: str) -> list[str]:
    needle = secret.encode("utf-8")
    hits: list[str] = []
    for path in root.rglob("*"):
        if path.is_file() and needle in path.read_bytes():
            hits.append(str(path.relative_to(root)))
    return hits


class TMDBDirectLinkCredentialRedContracts(unittest.IsolatedAsyncioTestCase):
    def _renamer_factory(self, root: Path, observed_keys: list[str]):
        def factory() -> ProductionRename:
            # Real construction is essential: it invokes production Search(),
            # which must consume the same effective credential as the page.
            renamer = ProductionRename()
            observed_keys.append(str(get_info.tmdb.API_KEY or ""))
            frozen = r1_fixture._FrozenSearch()
            frozen.TMDB_KEY = str(renamer.search.TMDB_KEY or "")
            renamer.search = frozen
            renamer.bangumi_search = BangumiProviderClient(
                user_agent="BangumiAutoRenamer-R19-contract/0.1",
                session=r1_fixture._BangumiFlatSession(),
                sleeper=lambda _seconds: None,
            )
            renamer.ai_processor = SimpleNamespace(
                ai_client=SimpleNamespace(is_available=lambda: False)
            )
            renamer.R = {}
            renamer.ANIME_PATH = root / "Anime"
            renamer.ANIME_MOVIE_PATH = root / "AnimeMovies"
            renamer.MOVIE_PATH = root / "Movies"
            renamer.BANGUMI_PATH = root / "TV"
            return renamer

        return factory

    async def _full_direct_flow(
        self,
        root: Path,
        *,
        env_key: str,
        stored_key: str,
        stale_revision: bool = False,
    ) -> SimpleNamespace:
        flow = root / "flow"
        tasks = flow / "tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        plans = flow / "plans"
        decisions = flow / "decisions"
        config = _config(flow, api_key=stored_key)
        config_before = config.config_path.read_bytes()
        source = r1_fixture._make_movie(root / "sources")
        task_id = "r19-tmdb-direct-link"
        session = _ManualProviderSession()
        observed_keys: list[str] = []
        notifications = Mock()
        config_constructor = MagicMock(return_value=config)
        manager = TaskQueueManager(
            task_root=tasks,
            decision_state_root=decisions,
            recovery_abandon_audit_root=flow / "abandon_audit",
            plan_root=plans,
            test_mode=False,
        )
        factory = self._renamer_factory(flow, observed_keys)
        env_patch = {"TMDB_API_KEY": env_key, "BAR_FORCE_DISABLE_AI": "true"}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env_patch, clear=False))
            stack.enter_context(patch.object(process_module, "cm", config))
            stack.enter_context(patch.object(get_info, "cm", config))
            stack.enter_context(patch.object(review_page, "cm", config))
            stack.enter_context(patch.object(
                config_module,
                "ConfigManager",
                side_effect=AssertionError(
                    "credential resolution constructed ConfigManager"
                ),
            ))
            stack.enter_context(patch.object(
                review_page, "ConfigManager", config_constructor, create=True
            ))
            stack.enter_context(patch.object(
                provider_search.requests, "Session", return_value=session
            ))
            stack.enter_context(patch.object(
                requests.sessions.Session,
                "request",
                side_effect=AssertionError("unexpected real network access"),
            ))
            stack.enter_context(patch.object(
                process_module,
                "AIProcessor",
                return_value=SimpleNamespace(
                    ai_client=SimpleNamespace(is_available=lambda: False)
                ),
            ))
            stack.enter_context(patch("src.task_queue.Rename", side_effect=factory))
            stack.enter_context(patch.object(process_module, "TASK_PATH", tasks))
            stack.enter_context(patch.object(process_module, "PLAN_PATH", plans))
            stack.enter_context(patch.object(
                process_module, "DECISION_STATE_PATH", decisions
            ))
            stack.enter_context(patch.object(
                process_module,
                "PROVIDER_LINK_CATALOG_PATH",
                flow / "provider_links",
            ))
            await manager.start()
            try:
                await manager.enqueue(
                    source,
                    task_id=task_id,
                    is_anime=True,
                    is_movie=True,
                    use_ai=False,
                )
                await asyncio.wait_for(manager._queue.join(), timeout=8)
                with ExitStack() as review_stack:
                    review_stack.enter_context(patch.object(
                        review_page, "TASK_PATH", tasks
                    ))
                    review_stack.enter_context(patch.object(
                        review_page, "PLAN_PATH", plans
                    ))
                    review_stack.enter_context(patch.object(
                        review_page, "DECISION_STATE_PATH", decisions
                    ))
                    review_stack.enter_context(patch.object(
                        utils_module, "TASK_PATH", tasks
                    ))
                    review_stack.enter_context(patch.object(
                        review_page,
                        "PlanReviewDialog",
                        r1_fixture.R1RealQueueWorkerContracts._NoopReviewDialog,
                    ))
                    review_stack.enter_context(patch.object(
                        review_page,
                        "_dialog_mount_context",
                        return_value=nullcontext(),
                    ))
                    review_stack.enter_context(patch.object(review_page, "notify"))
                    await review_page.plan_review_page(task_id)
                initial_plan = PlanStore(plans).load(task_id)
                initial_state = DecisionStateStore(decisions).load(task_id)
                authority_paths = (
                    tasks / f"{task_id}.json",
                    plans / f"{task_id}.json",
                    decisions / f"{task_id}.json",
                )
                before = {path: path.read_bytes() for path in authority_paths}
                dialog = SimpleNamespace(
                    tmdb_manual_url_input=SimpleNamespace(
                        value="https://www.themoviedb.org/movie/763096"
                    ),
                    task_decision=(
                        SimpleNamespace(revision=initial_state.revision + 99)
                        if stale_revision
                        else initial_state
                    ),
                    plan=initial_plan,
                    task_data={},
                    close=Mock(),
                )
                dialog._select_provider_candidate = (
                    PlanReviewDialog._select_provider_candidate.__get__(dialog)
                )
                with (
                    patch.object(review_page, "task_queue", manager),
                    patch.object(review_page, "notify", notifications),
                ):
                    await PlanReviewDialog._select_manual_provider_url(dialog, "tmdb")
                await asyncio.wait_for(manager._queue.join(), timeout=8)
                after = {path: path.read_bytes() for path in authority_paths}
            finally:
                await manager.stop()
        return SimpleNamespace(
            flow=flow,
            task_id=task_id,
            tasks=tasks,
            plans=plans,
            decisions=decisions,
            initial_plan=initial_plan,
            initial_state=initial_state,
            final_plan=PlanStore(plans).load(task_id),
            final_state=DecisionStateStore(decisions).load(task_id),
            task=json.loads((tasks / f"{task_id}.json").read_text("utf-8")),
            config=config,
            config_before=config_before,
            config_after=config.config_path.read_bytes(),
            config_constructor=config_constructor,
            session=session,
            observed_keys=observed_keys,
            notifications=notifications,
            dialog=dialog,
            authority_before=before,
            authority_after=after,
        )

    async def test_env_only_manual_link_runs_real_queue_worker_and_persists_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await self._full_direct_flow(
                root,
                env_key=RUNTIME_KEY,
                stored_key="",
            )
            selection = result.task.get("selection_operation") or {}
            primary = getattr(result.final_plan.work_identity, "primary_provider", None)
            problems: list[str] = []
            if selection.get("state") != "succeeded":
                problems.append(f"selection state is {selection.get('state')!r}")
            if primary is None:
                problems.append("persisted Plan has no primary provider")
            else:
                actual_provider = (
                    primary.namespace,
                    primary.entity_type,
                    primary.provider_id,
                )
                if actual_provider != ("tmdb", "movie", "763096"):
                    problems.append(f"primary provider is {actual_provider!r}")
            if result.final_state.revision <= result.initial_state.revision:
                problems.append("DecisionState revision did not advance")
            expected_state_fp = DecisionStateStore(result.decisions).fingerprint(
                result.final_state
            )
            if result.final_plan.metadata.get("decision_state_fingerprint") != expected_state_fp:
                problems.append("Plan/DecisionState fingerprint diverged")
            if len(result.observed_keys) < 2 or any(
                key != RUNTIME_KEY for key in result.observed_keys
            ):
                problems.append(f"worker Search keys are {result.observed_keys!r}")
            manual_keys = [
                str(call["params"].get("api_key") or "")
                for call in result.session.calls
                if "api.themoviedb.org" in str(call["url"])
            ]
            if manual_keys != [RUNTIME_KEY]:
                problems.append(f"manual-link HTTP keys are {manual_keys!r}")
            if result.config_constructor.call_count != 0:
                problems.append(
                    f"page constructed ConfigManager {result.config_constructor.call_count} time(s)"
                )
            if result.config_before != result.config_after:
                problems.append("config bytes changed during manual-link selection")
            if list(result.flow.glob(".config.json.tmp")):
                problems.append("config temp file remains")
            secret_hits = _secret_hits(result.flow, RUNTIME_KEY)
            if secret_hits:
                problems.append(f"runtime key persisted in {secret_hits!r}")
            result.dialog.close.assert_not_called()
            self.assertEqual([], problems, "\n".join(problems))

    async def test_runtime_key_wins_and_click_does_not_construct_or_write_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, api_key=STORED_KEY)
            before = config.config_path.read_bytes()
            captured_keys: list[str] = []
            selector = AsyncMock()
            dialog = SimpleNamespace(
                tmdb_manual_url_input=SimpleNamespace(
                    value="https://www.themoviedb.org/movie/763096"
                ),
                _select_provider_candidate=selector,
            )
            constructor = MagicMock(return_value=config)

            def capture(_url, **kwargs):
                captured_keys.append(str(kwargs.get("tmdb_api_key") or ""))
                return _candidate()

            with (
                patch.dict(os.environ, {"TMDB_API_KEY": RUNTIME_KEY}, clear=False),
                patch.object(review_page, "cm", config),
                patch.object(
                    config_module,
                    "ConfigManager",
                    side_effect=AssertionError(
                        "credential resolution constructed ConfigManager"
                    ),
                ),
                patch.object(
                    review_page, "ConfigManager", constructor, create=True
                ),
                patch.object(review_page, "resolve_provider_item_url", capture),
                patch.object(review_page, "notify"),
            ):
                await PlanReviewDialog._select_manual_provider_url(dialog, "tmdb")
            problems = []
            if captured_keys != [RUNTIME_KEY]:
                problems.append(f"resolver keys are {captured_keys!r}")
            if constructor.call_count != 0:
                problems.append(f"ConfigManager constructor count is {constructor.call_count}")
            if config.config_path.read_bytes() != before:
                problems.append("config bytes changed")
            if config.config_path.with_name(f".{config.config_path.name}.tmp").exists():
                problems.append("config temp file remains")
            if selector.await_count != 1:
                problems.append(f"candidate selector await count is {selector.await_count}")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_config_only_and_blank_env_support_movie_and_tv_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, api_key=STORED_KEY)
            before = config.config_path.read_bytes()
            session = _ManualProviderSession()
            selector = AsyncMock()
            dialog = SimpleNamespace(
                tmdb_manual_url_input=SimpleNamespace(value=""),
                _select_provider_candidate=selector,
            )
            constructor = MagicMock(return_value=config)
            with (
                patch.dict(os.environ, {"TMDB_API_KEY": "   "}, clear=False),
                patch.object(review_page, "cm", config),
                patch.object(
                    review_page, "ConfigManager", constructor, create=True
                ),
                patch.object(provider_search.requests, "Session", return_value=session),
                patch.object(review_page, "notify"),
            ):
                for value in (
                    "https://www.themoviedb.org/movie/763096",
                    "https://www.themoviedb.org/tv/53918",
                ):
                    dialog.tmdb_manual_url_input.value = value
                    await PlanReviewDialog._select_manual_provider_url(dialog, "tmdb")
            self.assertEqual(2, selector.await_count)
            candidates = [call.args[0] for call in selector.await_args_list]
            self.assertEqual(
                [("movie", "763096"), ("tv", "53918")],
                [(item["entity_type"], item["provider_id"]) for item in candidates],
            )
            self.assertEqual(
                [STORED_KEY, STORED_KEY],
                [str(call["params"].get("api_key")) for call in session.calls],
            )
            self.assertEqual(before, config.config_path.read_bytes())

    async def test_no_key_fails_before_http_enqueue_or_authority_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, api_key="")
            before_config = config.config_path.read_bytes()
            authority = {}
            for name in ("task.json", "plan.json", "decision.json"):
                path = root / name
                path.write_text(f"sentinel:{name}", encoding="utf-8")
                authority[path] = path.read_bytes()
            session = _ManualProviderSession()
            selector = AsyncMock()
            notifications = Mock()
            dialog = SimpleNamespace(
                tmdb_manual_url_input=SimpleNamespace(
                    value="https://www.themoviedb.org/movie/763096"
                ),
                _select_provider_candidate=selector,
            )
            with (
                patch.dict(os.environ, {}, clear=False),
                patch.object(review_page, "cm", config),
                patch.object(
                    review_page,
                    "ConfigManager",
                    MagicMock(return_value=config),
                    create=True,
                ),
                patch.object(provider_search.requests, "Session", return_value=session),
                patch.object(review_page, "notify", notifications),
            ):
                os.environ.pop("TMDB_API_KEY", None)
                await PlanReviewDialog._select_manual_provider_url(dialog, "tmdb")
            selector.assert_not_awaited()
            self.assertEqual([], session.calls)
            self.assertIn("尚未配置 TMDB API 密钥", notifications.call_args.args[0])
            self.assertEqual(before_config, config.config_path.read_bytes())
            self.assertEqual(authority, {path: path.read_bytes() for path in authority})

    async def test_provider_failures_do_not_leak_key_or_select_candidate(self):
        manager = SimpleNamespace(
            get_config=lambda key: STORED_KEY if key == "api_key" else (
                "BAR/R19" if key == "bangumi_user_agent" else ""
            )
        )
        for mode in ("http_error", "timeout", "bad_json"):
            with self.subTest(mode=mode):
                session = _ManualProviderSession(mode=mode)
                selector = AsyncMock()
                notifications = Mock()
                dialog = SimpleNamespace(
                    tmdb_manual_url_input=SimpleNamespace(
                        value="https://www.themoviedb.org/movie/763096"
                    ),
                    _select_provider_candidate=selector,
                )
                with (
                    patch.dict(os.environ, {}, clear=False),
                    patch.object(review_page, "cm", manager),
                    patch.object(
                        review_page,
                        "ConfigManager",
                        MagicMock(return_value=manager),
                        create=True,
                    ),
                    patch.object(
                        provider_search.requests, "Session", return_value=session
                    ),
                    patch.object(review_page, "notify", notifications),
                ):
                    os.environ.pop("TMDB_API_KEY", None)
                    await PlanReviewDialog._select_manual_provider_url(dialog, "tmdb")
                selector.assert_not_awaited()
                visible = str(notifications.call_args.args[0])
                self.assertNotIn(STORED_KEY, visible)
                self.assertTrue(visible.strip())

    async def test_bangumi_manual_link_does_not_require_tmdb_key(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory), api_key="")
            session = _ManualProviderSession()
            selector = AsyncMock()
            dialog = SimpleNamespace(
                bangumi_manual_url_input=SimpleNamespace(
                    value="https://bgm.tv/subject/216311"
                ),
                _select_provider_candidate=selector,
            )
            with (
                patch.dict(os.environ, {}, clear=False),
                patch.object(review_page, "cm", config),
                patch.object(
                    review_page,
                    "ConfigManager",
                    MagicMock(return_value=config),
                    create=True,
                ),
                patch.object(provider_search.requests, "Session", return_value=session),
                patch.object(review_page, "notify"),
            ):
                os.environ.pop("TMDB_API_KEY", None)
                await PlanReviewDialog._select_manual_provider_url(dialog, "bangumi")
            selector.assert_awaited_once()
            candidate = selector.await_args.args[0]
            self.assertEqual(("bangumi", "216311"), (
                candidate["namespace"], candidate["provider_id"]
            ))

    async def test_stale_revision_rejects_after_url_read_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            result = await self._full_direct_flow(
                Path(directory),
                env_key="",
                stored_key=STORED_KEY,
                stale_revision=True,
            )
            self.assertEqual(result.authority_before, result.authority_after)
            self.assertEqual(1, len([
                call for call in result.session.calls
                if "api.themoviedb.org" in str(call["url"])
            ]))
            visible = "\n".join(
                str(call.args[0]) for call in result.notifications.call_args_list
            )
            self.assertIn("内容已更新", visible)
            self.assertNotIn(STORED_KEY, visible)
            result.dialog.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
