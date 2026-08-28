"""Behaviour-level regressions for the rc.36 production entrypoint."""

from __future__ import annotations

import hashlib
import inspect
import json
import tempfile
import unittest
import uuid
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.rename.decision_state import (
    DecisionCommandService,
    DecisionState,
    DecisionStateStore,
)
from src.rename.plan_store import PlanStore
from src.rename.process import Rename
from src.rename.provider_link_catalog import ProviderLinkCatalog, ProviderLinkMapping
from src.rename.provider_link_decision import stage_provider_link_decision
from src.rename.provider_link_resolver import resolve_provider_link
from src.rename.rename_plan import ProviderIdentity, RenamePlan, WorkIdentity
from src.rename.series_catalog import SeriesCatalog
from src.task_queue import TaskQueueManager
from src.pages.plan_review_page import PlanReviewDialog
from tests.projection_test_support import save_plan_fixture


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _materialized_yama_mapping() -> ProviderLinkMapping:
    evidence = {"fixture": "向山进发四季已确认映射"}
    return ProviderLinkMapping.from_dict(
        {
            "schema_version": 1,
            "mapping_id": f"provider-link-{uuid.uuid4()}",
            "revision": 3,
            "local_series_key": f"series-{uuid.uuid4()}",
            "status": "materialized_confirmed",
            "provider_edges": (
                {
                    "namespace": "tmdb",
                    "entity_type": "tv",
                    "provider_id": "53918",
                    "role": "series",
                },
                *(
                    {
                        "namespace": "bangumi",
                        "entity_type": "subject",
                        "provider_id": subject_id,
                        "role": "season",
                    }
                    for subject_id in ("41658", "86670", "216310", "290980")
                ),
            ),
            "season_edges": tuple(
                {
                    "namespace": "bangumi",
                    "entity_type": "subject",
                    "provider_id": subject_id,
                    "tmdb_tv_id": "53918",
                    "season_number": season,
                }
                for subject_id, season in (
                    ("41658", 1),
                    ("86670", 2),
                    ("216310", 3),
                    ("290980", 4),
                )
            ),
            "evidence_snapshot": evidence,
            "evidence_fingerprint": _fingerprint(evidence),
        }
    )


def _configured_renamer(root: Path) -> Rename:
    renamer = object.__new__(Rename)
    renamer.search = Mock(TMDB_KEY="offline-test-key")
    renamer.ai_processor = SimpleNamespace(
        ai_client=SimpleNamespace(is_available=lambda: False)
    )
    renamer.R = {}
    renamer.ANIME_PATH = root / "Anime"
    renamer.ANIME_MOVIE_PATH = root / "AnimeMovies"
    renamer.MOVIE_PATH = root / "Movies"
    renamer.BANGUMI_PATH = root / "TV"
    renamer.get_season_id = Mock(return_value=1)
    return renamer


def _yama_subjects() -> tuple[dict, ...]:
    return tuple(
        {
            "namespace": "bangumi",
            "entity_type": "subject",
            "provider_id": provider_id,
            "title": title,
            "original_title": original,
            "air_date": air_date,
            "episode_count": count,
            "platform": "TV",
        }
        for provider_id, title, original, air_date, count in (
            ("41658", "向山进发", "ヤマノススメ", "2013-01-02", 13),
            (
                "86670",
                "向山进发 第二季",
                "ヤマノススメ セカンドシーズン",
                "2014-07-09",
                24,
            ),
            (
                "216310",
                "向山进发 第三季",
                "ヤマノススメ サードシーズン",
                "2018-07-02",
                13,
            ),
            (
                "290980",
                "向山进发 Next Summit",
                "ヤマノススメ Next Summit",
                "2022-10-04",
                12,
            ),
        )
    )


def _yama_relations() -> tuple[dict, ...]:
    return (
        {"from": "41658", "to": "86670", "relation": "续集"},
        {"from": "86670", "to": "216310", "relation": "续集"},
        {"from": "216310", "to": "290980", "relation": "续集"},
    )


def _yama_tmdb_info() -> dict:
    return {
        "id": 53918,
        "name": "向山进发",
        "original_name": "ヤマノススメ",
        "first_air_date": "2013-01-03",
        "genres": [{"name": "Animation"}],
        "seasons": [
            {
                "season_number": number,
                "name": f"Season {number}",
                "air_date": air_date,
                "episode_count": count,
            }
            for number, air_date, count in (
                (1, "2013-01-03", 13),
                (2, "2014-07-09", 24),
                (3, "2018-07-02", 13),
                (4, "2022-10-05", 12),
            )
        ],
    }


class _FrozenYamaEvidence:
    def load_crosswalk_evidence(self, subject_id):
        if str(subject_id) != "290980":
            raise AssertionError("unexpected Bangumi subject")
        return _yama_subjects(), _yama_relations()


class _UnexpectedOnlineEvidence:
    def search_animation(self, *_args, **_kwargs):
        return []

    def load_crosswalk_evidence(self, _subject_id):
        raise AssertionError("L0 must not fetch online crosswalk evidence")


class Rc36ProductionWiringRedContracts(unittest.TestCase):
    def test_queue_has_version_bound_provider_reidentification_command(self):
        command = getattr(TaskQueueManager, "enqueue_reidentify", None)
        self.assertTrue(
            callable(command),
            "真实任务队列缺少按原 task_id 重识别的公开命令",
        )
        parameters = inspect.signature(command).parameters
        self.assertTrue(
            {"task_id", "provider_selection", "expected_revision"}.issubset(
                parameters
            ),
            "重识别命令必须绑定 task_id、所选资料来源和期望修订号",
        )

    def test_materialized_l0_projects_complete_four_season_snapshot(self):
        mapping = _materialized_yama_mapping()
        resolution = resolve_provider_link(
            selected_provider={
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": "290980",
            },
            existing_links=(mapping,),
        )

        self.assertEqual("L0", resolution.automation_level)
        self.assertEqual("53918", resolution.tmdb_tv_id)
        self.assertEqual(mapping.local_series_key, resolution.proposed_series_key)
        self.assertEqual(
            {
                "41658": 1,
                "86670": 2,
                "216310": 3,
                "290980": 4,
            },
            dict(resolution.season_assignments),
            "离线已确认映射必须投影完整四季，不能只返回当前季",
        )
        self.assertEqual(mapping.mapping_id, resolution.anchor_mapping_id)
        self.assertEqual(mapping.revision, resolution.anchor_revision)
        self.assertEqual(
            mapping.evidence_fingerprint,
            resolution.anchor_fingerprint,
        )
        try:
            staged = stage_provider_link_decision(
                DecisionState(task_id="yama-l0"), resolution
            )
        except ValueError as exc:
            self.fail(f"离线已确认映射无法进入计划投影：{exc}")
        self.assertEqual("L0", staged.provider_link_binding["automation_level"])
        self.assertEqual(
            4,
            next(
                item["season_number"]
                for item in staged.provider_link_binding["season_assignments"]
                if item["bangumi_subject_id"] == "290980"
            ),
        )

    def test_decision_and_plan_do_not_split_when_plan_save_fails(self):
        class FailingPlanStore(PlanStore):
            def save(self, plan):
                raise OSError("injected plan persistence failure")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_store = DecisionStateStore(root / "decisions")
            initial = state_store.save(
                DecisionState(task_id="atomic-reidentify"),
                expected_revision=-1,
            )
            service = DecisionCommandService(
                state_store,
                FailingPlanStore(root / "plans"),
            )
            with self.assertRaises(OSError):
                service.update(
                    initial.task_id,
                    expected_revision=initial.revision,
                    changes={"library_domain": "animation"},
                    planner=lambda _state: RenamePlan(
                        task_id=initial.task_id,
                        source_root=root / "source",
                        entries=(),
                    ),
                )

            reloaded = state_store.load(initial.task_id)
            self.assertEqual(
                initial.revision,
                reloaded.revision,
                "计划写入失败时不得先推进决定修订号",
            )
            self.assertIsNone(reloaded.library_domain)

    def test_ordinary_tmdb_tv_provider_still_persists_a_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Ordinary Show S01E01.mkv"
            source.touch()
            renamer = _configured_renamer(root)
            renamer.search.get_tv_info_by_id.return_value = (
                "普通剧集",
                {
                    "id": 4242,
                    "name": "普通剧集",
                    "original_name": "Ordinary Show",
                    "first_air_date": "2020-01-01",
                    "genres": [{"name": "Drama"}],
                    "seasons": [{"season_number": 1, "name": "Season 1"}],
                },
            )
            tasks = root / "tasks"
            tasks.mkdir()
            with (
                patch("src.rename.process.TASK_PATH", tasks),
                patch("src.rename.process.PLAN_PATH", root / "plans"),
                patch(
                    "src.rename.process.DECISION_STATE_PATH",
                    root / "decisions",
                ),
            ):
                result = renamer._process(
                    source,
                    _is_anime=False,
                    _tuuid="ordinary-tv-control",
                    provider_selection={
                        "namespace": "tmdb",
                        "entity_type": "tv",
                        "provider_id": "4242",
                        "title": "普通剧集",
                    },
                )
                persisted = PlanStore(root / "plans").load(
                    "ordinary-tv-control"
                )

        self.assertEqual("need_confirm", result)
        self.assertEqual("tv", persisted.work_identity.primary_provider.entity_type)
        self.assertNotIn("provider_link_snapshot", persisted.metadata)


class Rc36RealEntrypointE2E(unittest.IsolatedAsyncioTestCase):
    async def _run_selected_provider_flow(
        self,
        *,
        root: Path,
        source: Path,
        task_id: str,
        renamer: Rename,
        candidate: dict,
        is_anime: bool,
        is_movie: bool,
    ):
        tasks = root / "tasks"
        tasks.mkdir()
        plan_root = root / "plans"
        decision_root = root / "decisions"
        manager = TaskQueueManager(
            task_root=tasks,
            decision_state_root=decision_root,
            plan_root=plan_root,
            recovery_abandon_audit_root=root / "abandon_audit",
            test_mode=False,
        )
        fake_dialog = None
        with (
            patch("src.task_queue.Rename", return_value=renamer),
            patch("src.rename.process.TASK_PATH", tasks),
            patch("src.rename.process.PLAN_PATH", plan_root),
            patch("src.rename.process.DECISION_STATE_PATH", decision_root),
            patch(
                "src.rename.process.PROVIDER_LINK_CATALOG_PATH",
                root / "provider_links",
            ),
            patch(
                "src.rename.process.SERIES_CATALOG_PATH",
                root / "series_catalog",
            ),
        ):
            await manager.start()
            try:
                await manager.enqueue(
                    source,
                    task_id=task_id,
                    is_anime=is_anime,
                    is_movie=is_movie,
                    use_ai=False,
                )
                await asyncio.wait_for(manager._queue.join(), timeout=5)
                initial_plan = PlanStore(plan_root).load(task_id)
                initial_state = DecisionStateStore(decision_root).load(task_id)
                fake_dialog = SimpleNamespace(
                    task_decision=initial_state,
                    plan=initial_plan,
                    task_data={},
                    close=Mock(),
                )
                with (
                    patch("src.pages.plan_review_page.task_queue", manager),
                    patch("src.pages.plan_review_page.notify"),
                ):
                    await PlanReviewDialog._select_provider_candidate(
                        fake_dialog, candidate
                    )
                await asyncio.wait_for(manager._queue.join(), timeout=5)
            finally:
                await manager.stop()
        return (
            PlanStore(plan_root).load(task_id),
            DecisionStateStore(decision_root).load(task_id),
            json.loads((tasks / f"{task_id}.json").read_text("utf-8")),
            fake_dialog,
        )

    async def test_add_select_worker_process_persists_yama_crosswalk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Yama no Susume Next Summit S04E01.mkv"
            source.touch()
            tasks = root / "tasks"
            tasks.mkdir()
            plan_root = root / "plans"
            decision_root = root / "decisions"
            provider_links = root / "provider_links"
            renamer = _configured_renamer(root)
            renamer.get_season_id = Rename.get_season_id.__get__(renamer, Rename)
            renamer.bangumi_search = None
            tmdb_info = _yama_tmdb_info()
            def get_tv_info(query, *_args, **_kwargs):
                def with_query_evidence(info):
                    result = dict(info)
                    result["_bar_search_alternatives"] = [{
                        "id": result["id"],
                        "name": str(query),
                        "original_name": str(query),
                        "first_air_date": result.get("first_air_date"),
                    }]
                    return result
                if str(query).startswith("Yama no Susume"):
                    initial = dict(tmdb_info)
                    initial["name"] = "Yama no Susume Next Summit"
                    return "Yama no Susume Next Summit", with_query_evidence(initial)
                return "向山进发", with_query_evidence(tmdb_info)

            renamer.search.get_tv_info.side_effect = get_tv_info
            renamer.search.get_movie_info.return_value = ("", None)
            renamer.search.get_tv_info_by_id.return_value = (
                "向山进发",
                tmdb_info,
            )
            manager = TaskQueueManager(
                task_root=tasks,
                decision_state_root=decision_root,
                plan_root=plan_root,
                recovery_abandon_audit_root=root / "abandon_audit",
                test_mode=False,
            )
            task_id = "real-yama-provider-selection"
            with (
                patch("src.task_queue.Rename", return_value=renamer),
                patch("src.rename.process.TASK_PATH", tasks),
                patch("src.rename.process.PLAN_PATH", plan_root),
                patch("src.rename.process.DECISION_STATE_PATH", decision_root),
                patch(
                    "src.rename.process.PROVIDER_LINK_CATALOG_PATH",
                    provider_links,
                ),
            ):
                await manager.start()
                try:
                    await manager.enqueue(
                        source,
                        task_id=task_id,
                        is_anime=True,
                        is_movie=False,
                        use_ai=False,
                    )
                    await asyncio.wait_for(manager._queue.join(), timeout=5)
                    initial_plan = PlanStore(plan_root).load(task_id)
                    initial_state = DecisionStateStore(decision_root).load(task_id)
                    renamer.bangumi_search = _FrozenYamaEvidence()
                    fake_dialog = SimpleNamespace(
                        task_decision=initial_state,
                        plan=initial_plan,
                        task_data={},
                        close=Mock(),
                    )
                    with (
                        patch("src.pages.plan_review_page.task_queue", manager),
                        patch("src.pages.plan_review_page.notify"),
                    ):
                        await PlanReviewDialog._select_provider_candidate(
                            fake_dialog,
                            {
                                "namespace": "bangumi",
                                "entity_type": "subject",
                                "provider_id": "290980",
                                "title": "向山进发 Next Summit",
                                "original_title": "ヤマノススメ Next Summit",
                                "year": 2022,
                            },
                        )
                    await asyncio.wait_for(manager._queue.join(), timeout=5)
                finally:
                    await manager.stop()

            persisted_plan = PlanStore(plan_root).load(task_id)
            persisted_state = DecisionStateStore(decision_root).load(task_id)
            task = json.loads((tasks / f"{task_id}.json").read_text("utf-8"))
            snapshot = persisted_plan.to_dict()["metadata"][
                "provider_link_snapshot"
            ]
            self.assertEqual(
                snapshot,
                persisted_state.to_dict()["provider_link_binding"],
            )
            self.assertEqual("53918", snapshot["tmdb_tv_id"])
            self.assertEqual(
                {
                    "41658": 1,
                    "86670": 2,
                    "216310": 3,
                    "290980": 4,
                },
                {
                    item["bangumi_subject_id"]: item["season_number"]
                    for item in snapshot["season_assignments"]
                },
            )
            self.assertEqual(4, persisted_plan.metadata["season_number"])
            self.assertEqual("provider_crosswalk", persisted_plan.metadata["season_source"])
            self.assertTrue(
                all("Season 04" in str(target) for target in persisted_plan.as_mapping().values())
            )
            self.assertEqual("need_confirm", task["workflow_status"])
            self.assertEqual("290980", task["provider_selection"]["provider_id"])
            fake_dialog.close.assert_not_called()

    async def test_ordinary_tv_uses_same_add_select_worker_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Ordinary Show S01E01.mkv"
            source.touch()
            info = {
                "id": 4242,
                "name": "普通剧集",
                "original_name": "Ordinary Show",
                "first_air_date": "2020-01-01",
                "genres": [{"name": "Drama"}],
                "seasons": [{"season_number": 1, "name": "Season 1"}],
            }
            renamer = _configured_renamer(root)
            renamer.search.get_tv_info.return_value = ("普通剧集", info)
            renamer.search.get_movie_info.return_value = ("", None)
            renamer.search.get_tv_info_by_id.return_value = (
                "普通剧集", info
            )
            plan, state, task, dialog = await self._run_selected_provider_flow(
                root=root,
                source=source,
                task_id="real-ordinary-tv",
                renamer=renamer,
                candidate={
                    "namespace": "tmdb", "entity_type": "tv",
                    "provider_id": "4242", "title": "普通剧集",
                },
                is_anime=False,
                is_movie=False,
            )

        self.assertEqual("tv", plan.work_identity.primary_provider.entity_type)
        self.assertNotIn("provider_link_snapshot", plan.metadata)
        self.assertIsNone(state.provider_link_binding)
        self.assertEqual("4242", task["provider_selection"]["provider_id"])
        self.assertEqual("need_confirm", task["workflow_status"])
        dialog.close.assert_not_called()

    async def test_materialized_l0_uses_same_entrypoint_without_online_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Yama no Susume Next Summit S04E01.mkv"
            source.touch()
            mapping = replace(_materialized_yama_mapping(), revision=0)
            ProviderLinkCatalog(root / "provider_links").save(
                mapping, expected_revision=-1
            )
            tmdb_info = _yama_tmdb_info()
            renamer = _configured_renamer(root)
            canonical_root = renamer.ANIME_PATH / "向山进发 (2013)"
            existing = save_plan_fixture(
                PlanStore(root / "series-fixture-plans"),
                RenamePlan(
                    task_id="completed-yama-s1-fixture",
                    source_root=root / "completed-s1-source",
                    entries=(),
                    metadata={
                        "series_key": mapping.local_series_key,
                        "season_binding_id": f"season-binding-{uuid.uuid4()}",
                        "season_number": 1,
                        "season_source": "provider_crosswalk",
                        "canonical_work_root": str(canonical_root),
                    },
                    work_identity=WorkIdentity(
                        "confirmed",
                        "animation",
                        "向山进发",
                        2013,
                        ProviderIdentity(
                            "confirmed", "tmdb", "tv", "53918", "向山进发",
                            2013, "explicit_user",
                        ),
                        "provider_selection",
                    ),
                ),
            )
            SeriesCatalog(root / "series_catalog").register_completed(
                existing,
                expected_revision=0,
            )
            renamer.search.get_tv_info.return_value = (
                "Yama no Susume Next Summit",
                {**tmdb_info, "name": "Yama no Susume Next Summit"},
            )
            renamer.search.get_movie_info.return_value = ("", None)
            renamer.search.get_tv_info_by_id.return_value = (
                "向山进发", tmdb_info
            )
            renamer.bangumi_search = _UnexpectedOnlineEvidence()
            plan, state, task, dialog = await self._run_selected_provider_flow(
                root=root,
                source=source,
                task_id="real-yama-l0",
                renamer=renamer,
                candidate={
                    "namespace": "bangumi", "entity_type": "subject",
                    "provider_id": "290980", "title": "向山进发 Next Summit",
                },
                is_anime=True,
                is_movie=False,
            )

        snapshot = plan.metadata["provider_link_snapshot"]
        self.assertEqual("L0", snapshot["automation_level"])
        self.assertEqual(mapping.local_series_key, snapshot["series_key"])
        self.assertEqual(4, plan.metadata["season_number"])
        self.assertEqual(str(canonical_root), plan.metadata["canonical_work_root"])
        self.assertEqual(1, plan.metadata["series_catalog_revision"])
        self.assertEqual(
            plan.to_dict()["metadata"]["provider_link_snapshot"],
            state.to_dict()["provider_link_binding"],
        )
        self.assertEqual("need_confirm", task["workflow_status"])
        dialog.close.assert_not_called()

    async def test_ordinary_movie_uses_same_add_select_worker_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Ordinary Movie.mkv"
            source.touch()
            info = {
                "id": 8181,
                "title": "普通电影",
                "original_title": "Ordinary Movie",
                "release_date": "2024-01-01",
                "genres": [{"name": "Drama"}],
            }
            renamer = _configured_renamer(root)
            renamer.search.get_tv_info.return_value = ("", None)
            renamer.search.get_movie_info.return_value = (
                "普通电影", info
            )
            renamer.search.get_movie_info_by_id.return_value = (
                "普通电影", info
            )
            plan, state, task, dialog = await self._run_selected_provider_flow(
                root=root,
                source=source,
                task_id="real-ordinary-movie",
                renamer=renamer,
                candidate={
                    "namespace": "tmdb", "entity_type": "movie",
                    "provider_id": "8181", "title": "普通电影",
                },
                is_anime=False,
                is_movie=True,
            )

        self.assertEqual("movie", plan.work_identity.primary_provider.entity_type)
        self.assertNotIn("provider_link_snapshot", plan.metadata)
        self.assertIsNone(state.provider_link_binding)
        self.assertEqual("8181", task["provider_selection"]["provider_id"])
        self.assertEqual("need_confirm", task["workflow_status"])
        dialog.close.assert_not_called()

    def test_ordinary_tmdb_movie_provider_still_persists_a_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Ordinary Movie.mkv"
            source.touch()
            renamer = _configured_renamer(root)
            renamer.search.get_movie_info_by_id.return_value = (
                "普通电影",
                {
                    "id": 8181,
                    "title": "普通电影",
                    "original_title": "Ordinary Movie",
                    "release_date": "2024-01-01",
                    "genres": [{"name": "Drama"}],
                },
            )
            tasks = root / "tasks"
            tasks.mkdir()
            with (
                patch("src.rename.process.TASK_PATH", tasks),
                patch("src.rename.process.PLAN_PATH", root / "plans"),
                patch(
                    "src.rename.process.DECISION_STATE_PATH",
                    root / "decisions",
                ),
            ):
                result = renamer._process(
                    source,
                    _is_anime=False,
                    _tuuid="ordinary-movie-control",
                    provider_selection={
                        "namespace": "tmdb",
                        "entity_type": "movie",
                        "provider_id": "8181",
                        "title": "普通电影",
                    },
                )
                persisted = PlanStore(root / "plans").load(
                    "ordinary-movie-control"
                )

        self.assertEqual("need_confirm", result)
        self.assertEqual(
            "movie", persisted.work_identity.primary_provider.entity_type
        )
        self.assertNotIn("provider_link_snapshot", persisted.metadata)


if __name__ == "__main__":
    unittest.main()
