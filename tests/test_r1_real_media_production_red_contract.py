"""R1 contracts distilled from the five-directory local R0 reproduction.

The contracts run as ordinary assertions without expected-failure wrappers.
These tests isolate provider transport and UI/config dependencies; the queue, worker,
``Rename.process/_process`` and persistence stores remain production code.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import src.pages.plan_review_page as review_page
import src.utils.utils as utils_module
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.decision_state import DecisionStateStore
from src.rename.plan_store import PlanStore
from src.rename.process import Rename
from src.rename.provider_search import BangumiProviderClient
from src.rename.rename_plan import PresentationGroup, RenamePlan
from src.task_queue import TaskQueueManager
from tests.source_collection_fixture import admit_task


REAL_YAMA_ROOT = Path(os.getenv("BAR_R1_MEDIA_ROOT", "__unconfigured_media_fixture__"))
POSTER_URL = "https://lain.bgm.tv/pic/cover/l/r1-yama-contract.jpg"


def red_contract(test):
    """Historical RED marker retained after the G1 contracts turned green."""

    return test


YAMA_SUBJECTS = {
    "41658": {
        "id": 41658,
        "type": 2,
        "name": "ヤマノススメ",
        "name_cn": "向山进发",
        "date": "2013-01-02",
        "eps": 13,
        "platform": "TV",
        "images": {"large": POSTER_URL},
    },
    "86670": {
        "id": 86670,
        "type": 2,
        "name": "ヤマノススメ セカンドシーズン",
        "name_cn": "向山进发 第二季",
        "date": "2014-07-09",
        "eps": 24,
        "platform": "TV",
        "images": {"large": POSTER_URL},
    },
    "216310": {
        "id": 216310,
        "type": 2,
        "name": "ヤマノススメ サードシーズン",
        "name_cn": "向山进发 第三季",
        "date": "2018-07-02",
        "eps": 13,
        "platform": "TV",
        "images": {"large": POSTER_URL},
    },
    "290980": {
        "id": 290980,
        "type": 2,
        "name": "ヤマノススメ Next Summit",
        "name_cn": "向山进发 Next Summit",
        "date": "2022-10-04",
        "eps": 12,
        "platform": "TV",
        "images": {"large": POSTER_URL},
    },
    "216311": {
        "id": 216311,
        "type": 2,
        "name": "ヤマノススメ おもいでプレゼント",
        "name_cn": "向山进发 回忆的礼物",
        "date": "2017-07-07",
        "eps": 2,
        "platform": "OVA",
        "images": {"large": POSTER_URL},
    },
}


YAMA_RELATIONS = {
    "41658": (("86670", "续集"),),
    "86670": (("41658", "前传"), ("216310", "续集")),
    "216310": (("86670", "前传"), ("290980", "续集")),
    "290980": (("216310", "前传"),),
    "216311": (),
}


def _flat_relation(subject_id: str, relation: str) -> dict:
    """Match the current official Bangumi flat related-subject response."""

    subject = YAMA_SUBJECTS[subject_id]
    return {
        "id": subject["id"],
        "type": subject["type"],
        "name": subject["name"],
        "name_cn": subject["name_cn"],
        "relation": relation,
    }


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: object):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _BangumiFlatSession:
    """Frozen provider transport using the official flat relation shape."""

    def post(self, _url, *, json=None, **_kwargs):
        keyword = str((json or {}).get("keyword") or "").casefold()
        if "omoide" in keyword or "おもいで" in keyword or "回忆" in keyword:
            subjects = (YAMA_SUBJECTS["216311"],)
        elif "second" in keyword or "セカンド" in keyword or "第二" in keyword:
            subjects = (YAMA_SUBJECTS["86670"],)
        elif "third" in keyword or "サード" in keyword or "第三" in keyword:
            subjects = (YAMA_SUBJECTS["216310"],)
        elif "summit" in keyword:
            subjects = (YAMA_SUBJECTS["290980"],)
        else:
            subjects = (YAMA_SUBJECTS["41658"],)
        return _Response({"data": [dict(item) for item in subjects]})

    def get(self, url, **_kwargs):
        endpoint = str(url).split("https://api.bgm.tv", 1)[-1]
        if endpoint.startswith('/v0/episodes?'):
            return _Response(dict(total=0, offset=0, data=[]))
        parts = tuple(part for part in endpoint.split("/") if part)
        subject_id = parts[2]
        if len(parts) == 4 and parts[3] == "subjects":
            return _Response([
                _flat_relation(related_id, relation)
                for related_id, relation in YAMA_RELATIONS[subject_id]
            ])
        return _Response(dict(YAMA_SUBJECTS[subject_id]))


def _tmdb_yama_tv() -> dict:
    return {
        "id": 53918,
        "name": "向山进发",
        "original_name": "ヤマノススメ",
        "first_air_date": "2013-01-03",
        "genres": [{"name": "Animation"}],
        "seasons": [
            {
                "season_number": season,
                "name": f"Season {season}",
                "air_date": air_date,
                "episode_count": count,
            }
            for season, air_date, count in (
                # This production-wiring control freezes the count-safe
                # provider rows used by the Phase A authority model.  Provider
                # count disagreements are covered separately by its explicit
                # date/count boundary tests and must not be hidden here.
                (1, "2013-01-03", 13),
                (2, "2014-07-09", 24),
                (3, "2018-07-02", 13),
                (4, "2022-10-05", 12),
            )
        ],
    }


def _tmdb_yama_movie() -> dict:
    return {
        "id": 763096,
        "title": "向山进发 回忆的礼物",
        "original_title": "ヤマノススメ おもいでプレゼント",
        "release_date": "2017-07-07",
        # This intentionally mirrors the live mismatch: the animation domain
        # must also be allowed to come from the linked Bangumi OVA evidence.
        "genres": [],
    }


class _FrozenSearch:
    TMDB_KEY = "offline-r1-provider-transport"

    def __init__(self):
        self.tv = _tmdb_yama_tv()
        self.movie = _tmdb_yama_movie()

    def get_tv_info(self, query, _year=None):
        value = str(query or "").strip().casefold()
        if value == "ordinary show":
            return "普通剧集", {
                "id": 4242,
                "name": "普通剧集",
                "original_name": "Ordinary Show",
                "first_air_date": "2020-01-01",
                "genres": [{"name": "Drama"}],
                "seasons": [{"season_number": 1, "name": "Season 1"}],
            }
        yama_tv_titles = {
            str(subject.get(field) or "").strip().casefold()
            for subject_id, subject in YAMA_SUBJECTS.items()
            if subject_id != "216311"
            for field in ("name", "name_cn")
            if str(subject.get(field) or "").strip()
        }
        yama_tv_titles.add("yama no susume")
        if value in yama_tv_titles:
            info = dict(self.tv)
            info["_bar_search_alternatives"] = [{
                "id": info["id"],
                "name": str(query or ""),
                "original_name": str(query or ""),
                "first_air_date": info.get("first_air_date"),
            }]
            return "向山进发", info
        return "", None

    def get_tv_subject_observation_info(self, query, year=None):
        return self.get_tv_info(query, year)

    def get_movie_info(self, query, _year=None):
        value = str(query or "").strip().casefold()
        if value == "ordinary movie":
            return "普通电影", {
                "id": 8181,
                "title": "普通电影",
                "original_title": "Ordinary Movie",
                "release_date": "2024-01-01",
                "genres": [{"name": "Drama"}],
            }
        if "yama" in value or "ヤマ" in value or "向山" in value:
            return "向山进发 回忆的礼物", dict(self.movie)
        return "", None

    def get_tv_info_by_id(self, provider_id):
        if int(provider_id) == 4242:
            return self.get_tv_info("Ordinary Show")
        if int(provider_id) == 53918:
            return "向山进发", dict(self.tv)
        return "", None

    def get_movie_info_by_id(self, provider_id):
        if int(provider_id) == 8181:
            return self.get_movie_info("Ordinary Movie")
        if int(provider_id) == 763096:
            return "向山进发 回忆的礼物", dict(self.movie)
        return "", None

    def get_season_info(self, provider_id, season_number):
        if int(season_number) == 0 and int(provider_id) in {4242, 53918}:
            return {"season_number": 0, "episodes": ()}
        return None


def _configured_renamer(root: Path) -> Rename:
    renamer = object.__new__(Rename)
    renamer.search = _FrozenSearch()
    renamer.bangumi_search = BangumiProviderClient(
        user_agent="BangumiAutoRenamer-R1-contract/0.1",
        session=_BangumiFlatSession(),
        sleeper=lambda _seconds: None,
    )
    renamer.R = {}
    renamer.ANIME_PATH = root / "Anime"
    renamer.ANIME_MOVIE_PATH = root / "AnimeMovies"
    renamer.MOVIE_PATH = root / "Movies"
    renamer.BANGUMI_PATH = root / "TV"
    renamer.get_season_id = Rename.get_season_id.__get__(renamer, Rename)
    return renamer


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _make_season(root: Path, folder: str, stem: str, *, ova: bool = False) -> Path:
    source = root / folder
    for episode in (1, 2):
        base = f"{stem} [{episode:02d}][Ma10p_1080p][x265_flac]"
        _touch(source / f"{base}.mkv")
        _touch(source / f"{base}.sc.ass")
    if ova:
        _touch(source / f"{stem} [06.5(OVA)][Ma10p_1080p][x265_flac].mkv")
        _touch(source / f"{stem} [06.5(OVA)][Ma10p_1080p][x265_flac].sc.ass")
    return source


def _make_movie(root: Path) -> Path:
    folder = root / "[VCB-Studio] Yama no Susume Omoide Present [Ma10p_1080p]"
    stem = "[VCB-Studio] Yama no Susume Omoide Present"
    _touch(folder / f"{stem} [Ma10p_1080p][x265_flac].mkv")
    _touch(folder / f"{stem} [Ma10p_1080p][x265_flac].sc.ass")
    for suffix in ("CM01", "CM02", "Menu"):
        _touch(folder / f"{stem} [{suffix}][Ma10p_1080p][x265_flac].mkv")
    return folder


def _candidate(subject_id: str) -> dict:
    subject = YAMA_SUBJECTS[subject_id]
    return {
        "namespace": "bangumi",
        "entity_type": "subject",
        "provider_id": subject_id,
        "title": subject["name_cn"],
        "original_title": subject["name"],
        "year": int(str(subject["date"])[:4]),
        "poster_url": POSTER_URL,
        "source_url": f"https://bgm.tv/subject/{subject_id}",
        "snapshot_at": "2026-08-20T00:00:00+00:00",
        "platform": str(subject.get("platform") or ""),
    }


class R1ManifestAndProviderBoundaryContracts(unittest.TestCase):
    def test_local_source_copy_still_matches_r0_media_shapes(self):
        if not os.getenv("BAR_R1_MEDIA_ROOT") or not REAL_YAMA_ROOT.exists():
            self.skipTest("设置 BAR_R1_MEDIA_ROOT 后才执行本地样本检查")
        video_suffixes = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv", ".webm"}
        subtitle_suffixes = {".ass", ".ssa", ".srt", ".vtt", ".sup"}
        actual = {}
        for directory in REAL_YAMA_ROOT.iterdir():
            if not directory.is_dir():
                continue
            files = tuple(item for item in directory.rglob("*") if item.is_file())
            actual[directory.name] = (
                len(files),
                sum(item.suffix.casefold() in video_suffixes for item in files),
                sum(item.suffix.casefold() in subtitle_suffixes for item in files),
            )
        self.assertEqual(
            {
                "[VCB-Studio] Yama no Susume [Ma10p_1080p]": (74, 24, 13),
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]": (278, 49, 26),
                "[VCB-Studio] Yama no Susume Third Season [Ma10p_1080p]": (245, 26, 26),
                "[VCB-Studio] Yama no Susume Next Summit [Ma10p_1080p]": (157, 33, 24),
                "[VCB-Studio] Yama no Susume Omoide Present [Ma10p_1080p]": (272, 4, 1),
            },
            actual,
        )

    @red_contract
    def test_official_flat_bangumi_relations_reconstruct_all_four_tv_seasons(self):
        client = BangumiProviderClient(
            user_agent="BangumiAutoRenamer-R1-contract/0.1",
            session=_BangumiFlatSession(),
            sleeper=lambda _seconds: None,
        )
        subjects, relations = client.load_crosswalk_evidence("290980")
        self.assertEqual(
            {"41658", "86670", "216310", "290980"},
            {str(item["provider_id"]) for item in subjects},
            "官方关系数组是平铺条目；客户端必须遍历前传链而不是只保留所选季度",
        )
        self.assertTrue(relations)


class R1RealQueueWorkerContracts(unittest.IsolatedAsyncioTestCase):
    class _NoopReviewDialog:
        """Let the production page build/load state without mounting NiceGUI."""

        def __init__(self, plan, task_data, *, snapshot=None, loading=False):
            self.plan = plan
            self.task_data = task_data
            self.submitted = asyncio.Event()
            self.value = False
            self.exit_requested = False
            self.is_deleted = False

        def open(self):
            self.value = True

        def _load_plan_view(self, plan, *, snapshot=None):
            self.plan = plan
            self.snapshot = snapshot

        def __await__(self):
            async def wait_once():
                self.exit_requested = True
                return None

            return wait_once().__await__()

        def delete(self) -> None:
            return None

    async def _run_flow(
        self,
        *,
        root: Path,
        source: Path,
        task_id: str,
        candidate: dict,
        is_anime=None,
        is_movie=None,
    ) -> SimpleNamespace:
        tasks = root / "tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        plans = root / "plans"
        decisions = root / "decisions"
        renamer = _configured_renamer(root)
        from tests.test_provider_subject_plan_wiring_red_contract import _root_config_patch
        manager = TaskQueueManager(
            task_root=tasks,
            decision_state_root=decisions,
            provider_link_catalog_root=root / "provider_links",
            recovery_abandon_audit_root=root / "abandon_audit",
            plan_root=plans,

        )
        with (
            _root_config_patch({'bangumi_path': str(renamer.BANGUMI_PATH),
                'movie_path': str(renamer.MOVIE_PATH), 'anime_path': str(renamer.ANIME_PATH),
                'anime_movie_path': str(renamer.ANIME_MOVIE_PATH)}),
            patch("src.task_queue.Rename", return_value=renamer),
            patch("src.rename.process.TASK_PATH", tasks),
            patch("src.rename.process.PLAN_PATH", plans),
            patch("src.rename.process.SERIES_CATALOG_PATH", root / "series_catalog"),
            patch("src.rename.process.RECORD_PATH", root / "records"),
        ):
            await manager.start()
            try:
                await admit_task(manager,
                    source,
                    task_id=task_id,
                    is_anime=is_anime,
                    is_movie=is_movie,
                )
                await asyncio.wait_for(manager._queue.join(), timeout=8)
                # Opening the review is read-only. Historical admissions need
                # the same workspace preparation now used by first-link submit.
                with (
                    patch.object(review_page, "TASK_PATH", tasks),
                    patch.object(review_page, "PLAN_PATH", plans),
                    patch.object(review_page, "DECISION_STATE_PATH", decisions),
                    patch.object(review_page, "PLAN_REVIEW_PATH", root / 'reviews'),
                    patch.object(review_page, "RECORD_PATH", root / 'records'),
                    patch.object(review_page, "SERIES_CATALOG_PATH", root / 'series_catalog'),
                    patch.object(review_page, "WORK_CATALOG_PATH", root / 'work_catalog'),
                    patch.object(review_page, "RECOVERY_TRANSACTION_PATH", root / 'recovery'),
                    patch.object(review_page, "task_queue", manager),
                    patch.object(utils_module, "TASK_PATH", tasks),
                    patch.object(review_page, "PlanReviewDialog", self._NoopReviewDialog),
                    patch.object(review_page, "_dialog_mount_context", return_value=nullcontext()),
                    patch.object(review_page, "notify"),
                ):
                    await review_page.plan_review_page(task_id)
                    if not PlanStore(plans).path_for(task_id).exists():
                        from src.pages.initial_workspace import create_initial_workspace
                        await create_initial_workspace(task_id, review_page.get_task(task_id))
                initial_plan = PlanStore(plans).load(task_id)
                initial_state = DecisionStateStore(decisions).load(task_id)
                dialog = SimpleNamespace(
                    task_decision=initial_state,
                    plan=initial_plan,
                    task_data=json.loads(
                        (tasks / f"{task_id}.json").read_text("utf-8")
                    ),
                    close=Mock(),
                    _render_groups=Mock(),
                )
                with (
                    patch("src.pages.plan_review_page.task_queue", manager),
                    patch("src.pages.plan_review_page.notify"),
                ):
                    await PlanReviewDialog._select_provider_candidate(dialog, candidate)
                await asyncio.wait_for(manager._queue.join(), timeout=8)
            finally:
                await manager.stop()
        return SimpleNamespace(
            initial_plan=initial_plan,
            initial_state=initial_state,
            plan=PlanStore(plans).load(task_id),
            state=DecisionStateStore(decisions).load(task_id),
            task=json.loads((tasks / f"{task_id}.json").read_text("utf-8")),
            dialog=dialog,
            plan_store=PlanStore(plans),
            decision_store=DecisionStateStore(decisions),
        )

    @red_contract
    async def test_four_real_shape_seasons_converge_to_one_series_and_s01_to_s04(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = (
                ("41658", 1, _make_season(
                    root / "sources",
                    "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
                    "[VCB-Studio] Yama no Susume",
                )),
                ("86670", 2, _make_season(
                    root / "sources",
                    "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                    "[VCB-Studio] Yama no Susume Second Season",
                    ova=True,
                )),
                ("216310", 3, _make_season(
                    root / "sources",
                    "[VCB-Studio] Yama no Susume Third Season [Ma10p_1080p]",
                    "[VCB-Studio] Yama no Susume Third Season",
                )),
                ("290980", 4, _make_season(
                    root / "sources",
                    "[VCB-Studio] Yama no Susume Next Summit [Ma10p_1080p]",
                    "[VCB-Studio] Yama no Susume Next Summit",
                )),
            )
            results = []
            for subject_id, season, source in inputs:
                task_root = root / f"flow-{season}"
                result = await self._run_flow(
                    root=task_root,
                    source=source,
                    task_id=f"r1-yama-s{season}",
                    candidate=_candidate(subject_id),
                    is_anime=True,
                    is_movie=False,
                )
                results.append((subject_id, season, result))

        problems = []
        scope_ids = set()
        for subject_id, expected_season, result in results:
            binding = result.state.provider_link_binding or {}
            season_rows = {
                str(item.get("bangumi_subject_id")): item.get("season_number")
                for item in binding.get("season_assignments", ())
            }
            actual_season = season_rows.get(subject_id)
            if actual_season != expected_season:
                problems.append(
                    f"Bangumi {subject_id}: expected Season {expected_season:02d}, got {actual_season}"
                )
            scope_id = str(result.plan.metadata['series_key'])
            self.assertTrue(scope_id)
            if scope_id:
                scope_ids.add(scope_id)
            if result.plan.metadata.get("mapping_source") == "inventory_review_draft":
                problems.append(f"Bangumi {subject_id}: still exposed the movie inventory draft")
            if result.task.get("workflow_status") != "need_confirm":
                problems.append(
                    f"Bangumi {subject_id}: task ended as {result.task.get('workflow_status')}"
                )
            if result.state.revision <= result.initial_state.revision:
                problems.append(f"Bangumi {subject_id}: DecisionState revision did not advance")
            if result.plan.metadata.get("decision_state_revision") != result.state.revision:
                problems.append(f"Bangumi {subject_id}: Plan and DecisionState revisions diverged")
            expected_fingerprint = result.decision_store.fingerprint(result.state)
            if result.plan.metadata.get("decision_state_fingerprint") != expected_fingerprint:
                problems.append(f"Bangumi {subject_id}: Plan has a stale DecisionState fingerprint")
            targets = tuple(str(item.target_path) for item in result.plan.entries
                            if '06.5(OVA)' not in item.source_path.name)
            if not targets or any(f"Season {expected_season:02d}" not in item for item in targets):
                problems.append(f"Bangumi {subject_id}: targets are not all in Season {expected_season:02d}")
            if subject_id == "86670":
                extra_subtitles = tuple(
                    item for item in result.plan.entries
                    if "06.5(OVA)" in item.source_path.name
                    and item.source_path.suffix.casefold() == ".ass"
                )
                self.assertTrue(extra_subtitles)
                for item in extra_subtitles:
                    self.assertIn(item.source_path, result.plan.expected_sources)
                    self.assertTrue(item.requires_review,
                        'ambiguous OVA belongs in manual confirmation, not an invented episode')
        if len(scope_ids) != 1:
            problems.append(f"expected one shared scope id, got {sorted(scope_ids)}")
        self.assertEqual([], problems, "\n".join(problems))


    @red_contract
    async def test_selected_poster_survives_queue_worker_and_disk_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "Ordinary Show S01E01.mkv"
            _touch(source)
            candidate = _candidate("41658")
            candidate["poster_url"] = (
                "https://image.tmdb.org/t/p/w342/ordinary-show.jpg"
            )
            result = await self._run_flow(
                root=root / "flow",
                source=source,
                task_id="r1-poster-roundtrip",
                candidate=candidate,
                is_anime=True,
                is_movie=False,
            )

        expected = candidate["poster_url"]
        evidence = result.state.provider_evidence[0] if result.state.provider_evidence else {}
        self.assertEqual(
            (expected, expected, expected),
            (
                result.task.get("provider_selection", {}).get("poster_url"),
                result.plan.work_identity.primary_provider.poster_url,
                evidence.get("poster_url"),
            ),
            "封面必须跨 UI→queue→worker→Plan/DecisionState 磁盘回读保持不变",
        )

    @red_contract
    async def test_animation_movie_keeps_bangumi_ova_and_tmdb_movie_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _make_movie(root / "sources")
            result = await self._run_flow(
                root=root / "flow",
                source=source,
                task_id="r1-yama-movie",
                candidate={
                    "namespace": "tmdb",
                    "entity_type": "movie",
                    "provider_id": "763096",
                    "title": "向山进发 回忆的礼物",
                    "original_title": "ヤマノススメ おもいでプレゼント",
                    "year": 2017,
                    "poster_url": POSTER_URL,
                    "source_url": "https://www.themoviedb.org/movie/763096",
                },
                is_anime=True,
                is_movie=True,
            )

        providers = {
            (
                str(item.get("namespace") or ""),
                str(item.get("entity_type") or ""),
                str(item.get("provider_id") or ""),
            )
            for item in result.state.provider_evidence
        }
        problems = []
        if ("tmdb", "movie", "763096") not in providers:
            problems.append("missing TMDB movie 763096 authority")
        if ("bangumi", "subject", "216311") not in providers:
            problems.append("missing linked Bangumi OVA 216311 evidence")
        if result.state.library_domain != "animation":
            problems.append(f"library domain is {result.state.library_domain!r}, not animation")
        if result.task.get("is_anime") is not True:
            problems.append(f"task is_anime is {result.task.get('is_anime')!r}")
        if result.plan.metadata.get("provider_link_snapshot") is not None:
            problems.append("movie was incorrectly admitted to the TV season crosswalk")
        suggested = result.task.get("draft_suggested_roots", {}).get("movie_continuous", "")
        if "AnimeMovies" not in str(suggested):
            problems.append(f"movie route is not AnimeMovies: {suggested!r}")
        self.assertEqual([], problems, "\n".join(problems))

    def test_animation_movie_link_allows_exact_title_across_new_year_only(self):
        rename = Rename()
        rename.bangumi_search = BangumiProviderClient(
            user_agent="BangumiAutoRenamer-R1-contract/0.1",
            session=_BangumiFlatSession(),
            sleeper=lambda _seconds: None,
        )
        with patch.dict(YAMA_SUBJECTS["216311"], {"date": "2018-01-26"}):
            linked = rename._animation_movie_link(
                {
                    "title": "向山进发 回忆的礼物",
                    "original_title": "ヤマノススメ おもいでプレゼント",
                },
                {
                    "title": "向山进发 回忆的礼物",
                    "original_title": "ヤマノススメ おもいでプレゼント",
                    "release_date": "2017-07-07",
                },
            )

        self.assertIsNotNone(linked)
        self.assertEqual("216311", linked.provider_id)


class R1OperatorClosureContracts(unittest.IsolatedAsyncioTestCase):
    @red_contract
    async def test_provider_selection_keeps_review_open_until_result_is_visible(self):
        queue = SimpleNamespace(enqueue_reidentify=AsyncMock(return_value="task"))
        dialog = SimpleNamespace(
            task_decision=SimpleNamespace(revision=0, confirmed_work={}, content_selection=None,
                completed_rebuild_observation=None,layout_mode="tv_episodes",library_domain="animation"),
            plan=RenamePlan(task_id="r1-operator-closure", source_root=Path("source"), entries=()),
            task_data={"is_movie": False},
            close=Mock(),
        )
        with (
            patch("src.pages.plan_review_page.task_queue", queue),
            patch("src.pages.plan_review_page.get_task", return_value={}),
            patch("src.pages.plan_review_page.notify"),
        ):
            await PlanReviewDialog._select_provider_candidate(
                dialog,
                _candidate("86670"),
            )
        dialog.close.assert_not_called()


class R1PrimaryPickerContracts(unittest.TestCase):
    @red_contract
    def test_movie_rules_plan_with_unconfirmed_primary_renders_primary_picker(self):
        plan = RenamePlan(
            task_id="r1-rules-primary-picker",
            source_root=Path("source"),
            entries=(),
            metadata={"mapping_source": "rules"},
            presentation_groups=(PresentationGroup(
                "movie-continuous", "movie_continuous", "电影", None,
            ),),
        )
        dialog = SimpleNamespace(
            plan=plan,
            snapshot=SimpleNamespace(
                issues=({"code": "subtitle_video_not_content_version"},)
            ),
            completion_container=MagicMock(),
            _render_movie_primary_picker=Mock(),
            _video_options=Mock(return_value={"source/main.mkv": "main.mkv"}),
            _selected_movie_primary_sources=Mock(return_value=[]),
        )
        PlanReviewDialog._render_completion_workflow(dialog)
        dialog._render_movie_primary_picker.assert_called_once()

    @red_contract
    def test_tv_extra_subtitle_issue_does_not_offer_movie_primary_picker(self):
        plan = RenamePlan(
            task_id="r1-tv-extra-subtitle",
            source_root=Path("source"),
            entries=(),
            metadata={"mapping_source": "rules"},
            presentation_groups=(PresentationGroup(
                "tv-episodes", "tv_episodes", "剧集", None,
            ),),
        )
        dialog = SimpleNamespace(
            plan=plan,
            snapshot=SimpleNamespace(
                issues=({"code": "subtitle_video_not_content_version"},)
            ),
            completion_container=MagicMock(),
            _render_movie_primary_picker=Mock(),
            _video_options=Mock(return_value={"source/ova.mkv": "ova.mkv"}),
            _selected_movie_primary_sources=Mock(return_value=[]),
        )
        PlanReviewDialog._render_completion_workflow(dialog)
        dialog._render_movie_primary_picker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
