"""Phase B regressions for production provider-subject Plan wiring.

Every new-behaviour RED enters through AddTaskDialog and the non-test queue,
then uses the real provider-selection callback and worker.  The three legacy
controls call the already-established real queue/provider-worker E2E without
claiming a second AddTask proof.  Provider transport and filesystem roots are
isolated; resolver, DecisionState, PlanStore and PlanVerifier are never
replaced with synthetic downstream fixtures.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, nullcontext
from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Mapping
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import src.pages.add_task_page as add_task_page
import src.pages.data_table_page as dashboard_page
import src.pages.plan_review_page as review_page
import src.rename.process as process_module
import src.task_queue as task_queue_module
import src.utils.utils as utils_module
import tests.test_complex_library_special_red_contract as ui_fixture
import tests.test_rc36_production_wiring_red_contract as rc36_fixture
import tests.test_r1_real_media_production_red_contract as production_fixture
from src.pages.add_task_page import AddTaskDialog
from src.pages.plan_review_page import PlanReviewDialog, build_plan_review_snapshot
from src.rename.decision_state import DecisionCommandService, DecisionStateStore
from src.rename.get_info import TMDBRequestError
from src.rename.plan_review import (
    PlanReviewDecision,
    PlanReviewService,
    ReviewStatus,
    entry_id,
)
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import PlanStatus
from src.rename.provider_search import BangumiProviderClient
from src.rename.provider_link_resolver import (
    collect_subject_observations,
    resolve_provider_link,
)
from src.rename.provider_link_decision import (
    freeze_provider_subject_boundary_edges,
    resolve_provider_subject_boundary_decision,
)
from src.rename.process import Rename
from src.task_queue import TaskQueueManager
from tests.projection_test_support import save_plan_fixture


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def provider_flow_diagnostics(
    result: SimpleNamespace,
    *,
    test_identity: str,
    sequence: tuple[str, ...] = (),
) -> dict[str, object]:
    """Return a deterministic, non-mutating snapshot for flaky flow failures."""

    task = result.task if isinstance(result.task, Mapping) else {}
    selection = task.get("selection_operation")
    selection = selection if isinstance(selection, Mapping) else {}
    candidate = selection.get("candidate")
    candidate = candidate if isinstance(candidate, Mapping) else {}
    provider_failure = task.get("provider_selection_failure")
    provider_failure = (
        provider_failure if isinstance(provider_failure, Mapping) else {}
    )
    plan_snapshot = result.plan.metadata.get("provider_link_snapshot")
    plan_snapshot = plan_snapshot if isinstance(plan_snapshot, Mapping) else {}
    source_projection = result.plan.metadata.get("source_episode_projection")
    source_projection = (
        source_projection if isinstance(source_projection, Mapping) else {}
    )
    state_payload = result.state.to_dict()
    verification = PlanVerifier().verify(result.plan)

    process_provider_ids = []
    for call in result.process_calls:
        provider_selection = call.kwargs.get("provider_selection")
        provider_selection = (
            provider_selection if isinstance(provider_selection, Mapping) else {}
        )
        process_provider_ids.append(
            str(provider_selection.get("provider_id") or "") or None
        )

    return {
        "schema_version": 1,
        "test_identity": str(test_identity),
        "sequence": list(sequence),
        "task": {
            "task_id": result.task_id,
            "workflow_status": task.get("workflow_status"),
            "queue_operation": task.get("queue_operation"),
            "error_present": bool(task.get("error")),
        },
        "selection": {
            "state": selection.get("state"),
            "failure_code": selection.get("failure_code"),
            "reason_codes": list(selection.get("reason_codes") or ()),
            "expected_decision_revision": selection.get(
                "expected_decision_revision"
            ),
            "result_decision_revision": selection.get(
                "result_decision_revision"
            ),
            "candidate": {
                key: candidate.get(key)
                for key in ("namespace", "entity_type", "provider_id")
            },
            "provider_failure_reason_codes": list(
                provider_failure.get("reason_codes") or ()
            ),
        },
        "queue": dict(result.queue_lifecycle),
        "calls": {
            "process_count": result.process_call_count,
            "process_provider_ids": process_provider_ids,
            "reidentify_count": len(result.reidentify_calls),
            "mounted_before_selection_count": len(
                result.mounted_before_selection
            ),
        },
        "decision": {
            "revision": result.state.revision,
            "payload_fingerprint": _sha256_json(state_payload),
            "provider_evidence_count": len(result.state.provider_evidence),
            "provider_binding_schema_version": (
                state_payload.get("provider_link_binding") or {}
            ).get("schema_version")
            if isinstance(state_payload.get("provider_link_binding"), Mapping)
            else None,
        },
        "plan": {
            "status": verification.status.value,
            "issue_codes": [issue.code for issue in verification.issues],
            "fingerprint": PlanStore(result.plans).fingerprint(result.plan),
            "revision_action": result.plan.revision_action,
            "decision_revision": result.plan.metadata.get(
                "decision_state_revision"
            ),
            "decision_fingerprint": result.plan.metadata.get(
                "decision_state_fingerprint"
            ),
            "selected_subject_id": plan_snapshot.get("selected_subject_id"),
            "tmdb_tv_id": plan_snapshot.get("tmdb_tv_id"),
            "source_episode_projection": {
                "present": bool(source_projection),
                "mode": source_projection.get("mode"),
                "offset": source_projection.get("offset"),
                "mapping_count": len(
                    source_projection.get("episode_mapping") or ()
                ),
            },
        },
    }


def format_provider_flow_diagnostics(
    *results: SimpleNamespace,
    test_identity: str,
    sequence: tuple[str, ...] = (),
) -> str:
    """Format only the structured snapshot; do not retry or repair the flow."""

    return json.dumps(
        {
            "schema_version": 1,
            "test_identity": str(test_identity),
            "sequence": list(sequence),
            "flows": [
                provider_flow_diagnostics(
                    result,
                    test_identity=test_identity,
                    sequence=sequence,
                )
                for result in results
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )


def _subject(
    subject_id: str,
    title: str,
    original_title: str,
    air_date: str,
    episode_count: int,
) -> dict[str, object]:
    return {
        "id": int(subject_id),
        "type": 2,
        "name": original_title,
        "name_cn": title,
        "date": air_date,
        "eps": episode_count,
        "platform": "TV",
        "images": {"large": production_fixture.POSTER_URL},
    }


STEINS_SUBJECTS = {
    "10380": _subject(
        "10380", "命运石之门", "STEINS;GATE", "2011-04-06", 24
    ),
    "129807": _subject(
        "129807", "命运石之门 0", "STEINS;GATE 0", "2018-04-12", 23
    ),
}
STEINS_RELATIONS = {
    "10380": (("129807", "续集"),),
    "129807": (("10380", "前传"),),
}

YAMA_SUBJECTS = {
    key: dict(production_fixture.YAMA_SUBJECTS[key])
    for key in ("41658", "86670", "216310", "290980")
}
YAMA_RELATIONS = {
    key: tuple(production_fixture.YAMA_RELATIONS[key])
    for key in YAMA_SUBJECTS
}

EIGHTY_SIX_SUBJECTS = {
    "302189": _subject(
        "302189", "86—不存在的战区—", "86―エイティシックス―", "2021-04-11", 11
    ),
    "331887": _subject(
        "331887",
        "86—不存在的战区— 第2部分",
        "86―エイティシックス― 第2クール",
        "2021-10-03",
        12,
    ),
}
EIGHTY_SIX_RELATIONS = {
    "302189": (("331887", "续集"),),
    "331887": (("302189", "前传"),),
}


def _candidate(subjects: dict[str, dict[str, object]], subject_id: str) -> dict:
    item = subjects[subject_id]
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


def _tmdb_tv(
    provider_id: int,
    title: str,
    original_title: str,
    first_air_date: str,
    seasons: tuple[tuple[int, str, int], ...],
) -> dict[str, object]:
    return {
        "id": provider_id,
        "name": title,
        "original_name": original_title,
        "first_air_date": first_air_date,
        "genres": [{"name": "Animation"}],
        "seasons": [
            {
                "season_number": number,
                "name": f"Season {number}",
                "air_date": air_date,
                "episode_count": count,
            }
            for number, air_date, count in seasons
        ],
    }


STEINS_TMDB = _tmdb_tv(
    42509, "命运石之门", "Steins;Gate", "2011-04-06",
    ((1, "2011-04-06", 24),),
)
STEINS_ZERO_TMDB = _tmdb_tv(
    78102, "命运石之门 0", "Steins;Gate 0", "2018-04-12",
    ((1, "2018-04-12", 23),),
)
YAMA_TMDB = _tmdb_tv(
    53918, "向山进发", "ヤマノススメ", "2013-01-03",
    (
        (1, "2013-01-03", 13),
        (2, "2014-07-09", 24),
        (3, "2018-07-02", 13),
        (4, "2022-10-05", 12),
    ),
)
EIGHTY_SIX_TMDB = _tmdb_tv(
    100565, "86—不存在的战区—", "86 EIGHTY-SIX", "2021-04-11",
    ((1, "2021-04-11", 23),),
)


class _ProviderSearch:
    def __init__(
        self,
        *,
        fail_subject: str | tuple[str, ...] | None = None,
        failure_mode: str = "transport",
    ):
        self.fail_subject = fail_subject
        self.failure_mode = failure_mode

    def _lookup(self, query: object):
        value = str(query or "").casefold()
        fail_terms = (
            self.fail_subject if isinstance(self.fail_subject, tuple)
            else ((self.fail_subject,) if self.fail_subject else ())
        )
        if any(term.casefold() in value for term in fail_terms):
            if self.failure_mode == "transport":
                raise TMDBRequestError("phase-b transport failure")
            return "", None
        if "steins" in value or "命运石" in value:
            if " 0" in value or value.endswith("0"):
                return "命运石之门 0", dict(STEINS_ZERO_TMDB)
            return "命运石之门", dict(STEINS_TMDB)
        if "yama" in value or "ヤマ" in value or "向山" in value:
            return "向山进发", dict(YAMA_TMDB)
        if "86" in value or "エイティ" in value:
            return "86—不存在的战区—", dict(EIGHTY_SIX_TMDB)
        if value == "ordinary show":
            return "Ordinary Show", _tmdb_tv(
                4242, "Ordinary Show", "Ordinary Show", "2020-01-01",
                ((1, "2020-01-01", 2),),
            )
        return "", None

    def get_tv_info(self, query, _year=None):
        name, info = self._lookup(query)
        if not name or not isinstance(info, dict):
            return name, info
        result = dict(info)
        result["_bar_search_alternatives"] = [{
            "id": result["id"],
            "name": str(query or ""),
            "original_name": str(query or ""),
            "first_air_date": result.get("first_air_date"),
        }]
        return name, result

    def get_tv_info_by_id(self, provider_id):
        table = {
            42509: ("命运石之门", STEINS_TMDB),
            78102: ("命运石之门 0", STEINS_ZERO_TMDB),
            53918: ("向山进发", YAMA_TMDB),
            100565: ("86—不存在的战区—", EIGHTY_SIX_TMDB),
        }
        value = table.get(int(provider_id))
        return (value[0], dict(value[1])) if value else ("", None)

    def get_movie_info(self, _query, _year=None):
        return "", None

    def get_movie_info_by_id(self, _provider_id):
        return "", None


def _configured_renamer(
    root: Path,
    *,
    fail_subject: str | tuple[str, ...] | None = None,
    failure_mode: str = "transport",
    library_root: Path | None = None,
) -> Rename:
    renamer = object.__new__(Rename)
    renamer.search = _ProviderSearch(
        fail_subject=fail_subject,
        failure_mode=failure_mode,
    )
    renamer.bangumi_search = BangumiProviderClient(
        user_agent="BangumiAutoRenamer-Phase-B-RED/0.1",
        session=production_fixture._BangumiFlatSession(),
        sleeper=lambda _seconds: None,
    )
    renamer.ai_processor = SimpleNamespace(
        ai_client=SimpleNamespace(is_available=lambda: False)
    )
    renamer.R = {}
    media_root = library_root or root
    renamer.ANIME_PATH = media_root / "Anime"
    renamer.ANIME_MOVIE_PATH = media_root / "AnimeMovies"
    renamer.MOVIE_PATH = media_root / "Movies"
    renamer.BANGUMI_PATH = media_root / "TV"
    renamer.get_season_id = Rename.get_season_id.__get__(renamer, Rename)
    return renamer


def _root_config_patch(snapshot: Mapping[str, object]):
    """Patch the shared config singleton with one explicit four-root fixture."""

    values = {
        **dict(snapshot),
        "mode": "链接",
        "ai_confidence_threshold": "Medium",
        "docker_mnt": "",
    }
    return patch.object(
        process_module.cm,
        "get_config",
        side_effect=lambda key: values.get(str(key), ""),
    )


def _make_episode_source(
    root: Path, stem: str, episodes: tuple[int, ...], *, subtitles: bool = False
) -> Path:
    source = root / stem
    for episode in episodes:
        path = source / f"{stem} [{episode:02d}].mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"episode-{episode}".encode("ascii"))
        if subtitles:
            path.with_suffix(".sc.ass").write_text(
                "[Script Info]\nTitle: test\n", encoding="utf-8"
            )
    return source


def _make_ambiguous_episode_versions(root: Path) -> Path:
    source = root / "86 Part 2 E12"
    for quality in ("1080p", "2160p"):
        path = source / f"86 Part 2 [12][{quality}].mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(quality.encode("ascii"))
        path.with_suffix(".sc.ass").write_text(
            "[Script Info]\nTitle: version subtitle\n", encoding="utf-8"
        )
    return source


def _source_manifest(root: Path) -> tuple[tuple[str, int, int, str], ...]:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        stat = path.stat()
        rows.append((
            path.relative_to(root).as_posix(),
            stat.st_size,
            stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        ))
    return tuple(rows)


def _byte_manifest(root: Path) -> tuple[tuple[str, bytes], ...]:
    if not root.exists():
        return ()
    return tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    )


class _ReviewCapture:
    mounted: list[tuple[object, dict[str, object]]] = []

    def __init__(self, plan, task_data):
        self.plan = plan
        self.task_data = dict(task_data)
        type(self).mounted.append((plan, dict(task_data)))

    def __await__(self):
        async def wait_once():
            return None

        return wait_once().__await__()

    def delete(self):
        return None


class ProviderSubjectPlanWiringRedContracts(unittest.IsolatedAsyncioTestCase):
    async def _run_real_add_select(
        self,
        *,
        root: Path,
        source: Path,
        subjects: dict[str, dict[str, object]],
        relations: dict[str, tuple[tuple[str, str], ...]],
        selected_subject_id: str,
        fail_subject: str | tuple[str, ...] | None = None,
        failure_mode: str = "transport",
        library_root: Path | None = None,
    ) -> SimpleNamespace:
        tasks = root / "tasks"
        plans = root / "plans"
        reviews = root / "reviews"
        decisions = root / "decisions"
        records = root / "records"
        provider_links = root / "provider_links"
        series_catalog = root / "series_catalog"
        work_catalog = root / "work_catalog"
        recovery = root / "recovery"
        abandon = root / "abandon"
        tasks.mkdir(parents=True, exist_ok=True)
        _ReviewCapture.mounted.clear()
        candidate = _candidate(subjects, selected_subject_id)

        combined_subjects = dict(production_fixture.YAMA_SUBJECTS)
        combined_subjects.update(subjects)
        combined_relations = dict(production_fixture.YAMA_RELATIONS)
        combined_relations.update(relations)
        source_before = _source_manifest(source)

        with ExitStack() as stack:
            stack.enter_context(
                patch.dict(
                    production_fixture.YAMA_SUBJECTS,
                    combined_subjects,
                    clear=True,
                )
            )
            stack.enter_context(
                patch.dict(
                    production_fixture.YAMA_RELATIONS,
                    combined_relations,
                    clear=True,
                )
            )
            stack.enter_context(patch.object(task_queue_module, "TASK_PATH", tasks))
            stack.enter_context(patch.object(task_queue_module, "PLAN_PATH", plans))
            stack.enter_context(patch.object(task_queue_module, "PLAN_REVIEW_PATH", reviews))
            stack.enter_context(patch.object(task_queue_module, "RECORD_PATH", records))
            stack.enter_context(patch.object(process_module, "TASK_PATH", tasks))
            stack.enter_context(patch.object(process_module, "PLAN_PATH", plans))
            stack.enter_context(patch.object(process_module, "DECISION_STATE_PATH", decisions))
            stack.enter_context(
                patch.object(process_module, "PROVIDER_LINK_CATALOG_PATH", provider_links)
            )
            stack.enter_context(
                patch.object(process_module, "SERIES_CATALOG_PATH", series_catalog)
            )
            stack.enter_context(patch.object(process_module, "RECORD_PATH", records))
            stack.enter_context(patch.object(review_page, "TASK_PATH", tasks))
            stack.enter_context(patch.object(review_page, "PLAN_PATH", plans))
            stack.enter_context(patch.object(review_page, "PLAN_REVIEW_PATH", reviews))
            stack.enter_context(patch.object(review_page, "DECISION_STATE_PATH", decisions))
            stack.enter_context(patch.object(review_page, "RECORD_PATH", records))
            stack.enter_context(patch.object(review_page, "SERIES_CATALOG_PATH", series_catalog))
            stack.enter_context(patch.object(review_page, "WORK_CATALOG_PATH", work_catalog))
            stack.enter_context(patch.object(review_page, "RECOVERY_TRANSACTION_PATH", recovery))
            stack.enter_context(patch.object(utils_module, "TASK_PATH", tasks))
            stack.enter_context(
                patch.object(review_page, "_dialog_mount_context", return_value=nullcontext())
            )
            stack.enter_context(patch.object(review_page, "RecognitionRecoveryDialog", ui_fixture._NoopAnyDialog))
            stack.enter_context(patch.object(review_page, "notify"))
            stack.enter_context(patch.object(add_task_page, "is_ui_test_mode", return_value=False))
            stack.enter_context(patch.object(add_task_page, "notify"))

            renamer = _configured_renamer(
                root,
                fail_subject=fail_subject,
                failure_mode=failure_mode,
                library_root=library_root,
            )
            stack.enter_context(_root_config_patch({
                "bangumi_path": str(renamer.BANGUMI_PATH),
                "movie_path": str(renamer.MOVIE_PATH),
                "anime_path": str(renamer.ANIME_PATH),
                "anime_movie_path": str(renamer.ANIME_MOVIE_PATH),
            }))
            process_spy = MagicMock(wraps=renamer._process)
            renamer._process = process_spy
            manager = TaskQueueManager(
                task_root=tasks,
                decision_state_root=decisions,
                recovery_abandon_audit_root=abandon,
                plan_root=plans,
                test_mode=False,
            )
            stack.enter_context(patch("src.task_queue.Rename", return_value=renamer))
            stack.enter_context(patch.object(add_task_page, "task_queue", manager))
            reidentify_spy = AsyncMock(wraps=manager.enqueue_reidentify)
            stack.enter_context(
                patch.object(manager, "enqueue_reidentify", reidentify_spy)
            )
            queue_join_count = 0

            submit = SimpleNamespace(
                path=str(source),
                media_type="anime",
                use_ai=False,
                _submitting=False,
                submit_button=MagicMock(),
                close=MagicMock(),
            )
            await manager.start()
            try:
                await AddTaskDialog._submit(submit)
                await asyncio.wait_for(manager._queue.join(), timeout=12)
                queue_join_count += 1
                task_files = tuple(tasks.glob("*.json"))
                self.assertEqual(1, len(task_files), "Add Task must persist one UI task")
                task_id = task_files[0].stem
                stack.enter_context(patch.object(review_page, "PlanReviewDialog", _ReviewCapture))
                await review_page.plan_review_page(task_id)
                initial_plan = PlanStore(plans).load(task_id)
                initial_state = DecisionStateStore(decisions).load(task_id)
                initial_plan_bytes = (plans / f"{task_id}.json").read_bytes()
                initial_state_bytes = (decisions / f"{task_id}.json").read_bytes()
                mounted_before_selection = tuple(_ReviewCapture.mounted)
                dialog = SimpleNamespace(
                    task_decision=initial_state,
                    plan=initial_plan,
                    task_data=json.loads(task_files[0].read_text("utf-8")),
                    close=Mock(),
                )
                stack.enter_context(patch.object(review_page, "task_queue", manager))
                await PlanReviewDialog._select_provider_candidate(dialog, candidate)
                await asyncio.wait_for(manager._queue.join(), timeout=12)
                queue_join_count += 1
            finally:
                await manager.stop()

            queue_lifecycle = {
                "join_count": queue_join_count,
                "started_after_stop": manager._started,
                "worker_present_after_stop": manager._worker_task is not None,
                "active_task_after_stop": manager.active_task_id is not None,
                "pending_count_after_stop": len(manager._pending),
                "queue_size_after_stop": manager._queue.qsize(),
                "unfinished_tasks_after_stop": getattr(
                    manager._queue, "_unfinished_tasks", None
                ),
            }

            task = json.loads((tasks / f"{task_id}.json").read_text("utf-8"))
            plan = PlanStore(plans).load(task_id)
            state = DecisionStateStore(decisions).load(task_id)
            history = tuple((plans / "history" / task_id).glob("*.json"))
            business_json = tuple(
                path
                for business_root in (
                    records, provider_links, series_catalog, work_catalog,
                    recovery, abandon,
                )
                if business_root.exists()
                for path in business_root.rglob("*.json")
            )
            target_files = tuple(
                path
                for target_root in (renamer.ANIME_PATH, renamer.ANIME_MOVIE_PATH)
                if target_root.exists()
                for path in target_root.rglob("*")
                if path.is_file()
            )
            return SimpleNamespace(
                root=root,
                task_id=task_id,
                task=task,
                tasks=tasks,
                plans=plans,
                reviews=reviews,
                decisions=decisions,
                plan=plan,
                state=state,
                initial_plan=initial_plan,
                initial_state=initial_state,
                initial_plan_bytes=initial_plan_bytes,
                initial_state_bytes=initial_state_bytes,
                history=history,
                business_json=business_json,
                source_unchanged=_source_manifest(source) == source_before,
                source_files=tuple(
                    sorted(path for path in source.rglob("*") if path.is_file())
                ),
                target_files=target_files,
                target_roots_exist=any(
                    path.exists() for path in (renamer.ANIME_PATH, renamer.ANIME_MOVIE_PATH)
                ),
                anime_root=renamer.ANIME_PATH,
                anime_movie_root=renamer.ANIME_MOVIE_PATH,
                manager=manager,
                submit=submit,
                process_call_count=process_spy.call_count,
                process_calls=tuple(process_spy.call_args_list),
                reidentify_calls=tuple(reidentify_spy.await_args_list),
                mounted_before_selection=mounted_before_selection,
                selected_subject_id=selected_subject_id,
                queue_lifecycle=queue_lifecycle,
            )

    def _real_path_problems(self, result) -> list[str]:
        problems = []
        if result.submit.close.call_count != 1:
            problems.append("Add Task submit did not close exactly once")
        if result.process_call_count != 2:
            problems.append(
                f"initial recognition and provider reidentify reached Rename._process {result.process_call_count} times"
            )
        elif str(
            result.process_calls[1].kwargs.get("provider_selection", {}).get(
                "provider_id", ""
            )
        ) != result.selected_subject_id:
            problems.append("second Rename._process call did not carry the selected provider")
        if len(result.reidentify_calls) != 1:
            problems.append("real provider callback did not call enqueue_reidentify exactly once")
        elif result.reidentify_calls[0].args[:1] != (result.task_id,):
            problems.append("provider callback reidentified a different task")
        if len(result.mounted_before_selection) != 1:
            problems.append("real review page did not mount the current task exactly once")
        if result.task.get("task_source") != "ui":
            problems.append("task did not originate at Add Task UI")
        if result.business_json:
            problems.append(f"Plan-only flow wrote business JSON: {result.business_json!r}")
        if not result.source_unchanged:
            problems.append("Plan-only flow changed source bytes or metadata")
        if result.target_files:
            problems.append(f"Plan-only flow created target files: {result.target_files!r}")
        if result.target_roots_exist:
            problems.append("Plan-only flow created a target library root")
        return problems

    def _entry_projection_problems(
        self,
        result,
        *,
        season_number: int,
        expected_targets: dict[Path, int],
        expected_tmdb_id: str,
        expected_work_root: Path,
    ) -> list[str]:
        problems = []
        entries = tuple(result.plan.entries)
        work_identity = result.plan.work_identity
        if (
            work_identity is None
            or work_identity.primary_provider.namespace != "tmdb"
            or work_identity.primary_provider.entity_type != "tv"
            or work_identity.primary_provider.provider_id != expected_tmdb_id
        ):
            problems.append("Plan work identity does not match the resolved TMDB TV work")
        if Path(str(result.plan.metadata.get("canonical_work_root") or "")) != expected_work_root:
            problems.append("Plan canonical work root does not match the resolved work")
        if not str(result.plan.metadata.get("series_key") or ""):
            problems.append("Plan has no stable series key")
        actual_sources = tuple(str(entry.source_path) for entry in entries)
        expected_sources = tuple(str(path) for path in result.source_files)
        if len(entries) != len(result.source_files):
            problems.append("Plan entry count does not equal the source-file closed set")
        if len(actual_sources) != len(set(actual_sources)):
            problems.append("Plan contains duplicate entries for one source")
        if set(actual_sources) != set(expected_sources):
            problems.append("Plan source set is not exact")
        target_paths = tuple(str(entry.target_path) for entry in entries)
        if len(target_paths) != len(set(target_paths)):
            problems.append("Plan contains duplicate target paths")
        for entry in entries:
            target_episode = expected_targets.get(Path(entry.source_path))
            if target_episode is None:
                problems.append(f"Plan contains unexpected source {entry.source_path}")
                continue
            if entry.season_id != season_number or entry.episode_id != target_episode:
                problems.append(f"PlanEntry ignored projected season/episode: {entry.source_path}")
            marker = f"S{season_number:02d}E{target_episode:02d}"
            if marker not in entry.target_path.name:
                problems.append(f"target name ignored projected episode: {entry.target_path}")
            if f"Season {season_number:02d}" not in entry.target_path.parts:
                problems.append(f"target directory ignored projected season: {entry.target_path}")
            try:
                Path(entry.target_path).resolve(strict=False).relative_to(
                    expected_work_root.resolve(strict=False)
                )
            except ValueError:
                problems.append(f"target escaped the resolved work root: {entry.target_path}")
        return problems

    def _nested_snapshot_problems(
        self,
        result,
        *,
        selected_subject_id: str,
        expected_tmdb_id: str,
        expected_ranges: tuple[tuple[str, str, int, int, int], ...],
        expected_segment: tuple[str, ...] | None = None,
        expected_boundaries: tuple[tuple[str, str, str, str, str], ...] = (),
        expected_observation_ids: dict[str, str] | None = None,
        expected_observation_subjects: tuple[str, ...] | None = None,
        require_accepted: bool = True,
    ) -> list[str]:
        problems: list[str] = self._real_path_problems(result)
        snapshot = result.plan.to_dict()["metadata"].get("provider_link_snapshot")
        binding = result.state.to_dict().get("provider_link_binding")
        if not isinstance(snapshot, dict):
            problems.append("final Plan did not persist a provider-link snapshot")
            snapshot = {}
        if snapshot != binding:
            problems.append("DecisionState and Plan provider snapshots differ")
        if snapshot.get("schema_version") != 2:
            problems.append("nested provider snapshot schema_version is not 2")
        if str(snapshot.get("selected_subject_id") or "") != selected_subject_id:
            problems.append("selected Bangumi subject is not frozen")
        if str(snapshot.get("tmdb_tv_id") or "") != expected_tmdb_id:
            problems.append("selected work TMDB TV identity is not frozen")
        observations = snapshot.get("subject_observations")
        if not isinstance(observations, dict):
            problems.append("subject observation batch is absent")
        else:
            if observations.get("schema_version") != 1:
                problems.append("subject observation batch schema_version is not 1")
            if str(observations.get("selected_subject_id") or "") != selected_subject_id:
                problems.append("subject observation batch selected a different subject")
            if type(observations.get("truncated")) is not bool:
                problems.append("subject observation truncation is not a strict boolean")
            if type(observations.get("boundary_complete")) is not bool:
                problems.append("subject observation boundary state is not a strict boolean")
            payload = {key: value for key, value in observations.items() if key != "evidence_fingerprint"}
            if observations.get("evidence_fingerprint") != _sha256_json(payload):
                problems.append("subject observation fingerprint is not canonical")
            observed_ids = {}
            observed_subjects = set()
            for observation in observations.get("subject_observations", ()):
                if not isinstance(observation, dict):
                    problems.append("subject observation row is malformed")
                    continue
                response_payload = {
                    key: value for key, value in observation.items()
                    if key != "response_fingerprint"
                }
                if observation.get("response_fingerprint") != _sha256_json(response_payload):
                    problems.append("subject response fingerprint is not canonical")
                subject_id = str(observation.get("bangumi_subject_id") or "")
                observed_subjects.add(subject_id)
                for query in observation.get("queries", ()):
                    if not isinstance(query, dict):
                        problems.append("subject query row is malformed")
                        continue
                    if query.get("outcome") not in {
                        "ok", "no_match", "transport_error", "invalid_response",
                        "budget_exhausted",
                    }:
                        problems.append("subject query persisted an unsupported outcome")
                    if type(query.get("truncated")) is not bool:
                        problems.append("subject query truncation is not a strict boolean")
                candidate_ids = set()
                for query in observation.get("queries", ()):
                    if not isinstance(query, dict) or query.get("outcome") != "ok":
                        continue
                    candidates = query.get("candidates", ())
                    if not isinstance(candidates, (list, tuple)):
                        problems.append("successful subject query candidates are malformed")
                        continue
                    if len(candidates) != 1:
                        problems.append(
                            "successful subject query did not freeze one exact candidate"
                        )
                    for candidate in candidates:
                        if not isinstance(candidate, dict):
                            problems.append("successful subject candidate is malformed")
                            continue
                        tmdb_tv_id = str(candidate.get("tmdb_tv_id") or "")
                        provider_id = str(candidate.get("provider_id") or "")
                        if (
                            candidate.get("namespace") != "tmdb"
                            or candidate.get("entity_type") != "tv"
                            or candidate.get("match_tier") != "exact"
                            or not tmdb_tv_id
                            or tmdb_tv_id != provider_id
                        ):
                            problems.append(
                                "successful subject candidate lost canonical TMDB TV identity"
                            )
                            continue
                        candidate_ids.add(tmdb_tv_id)
                if len(candidate_ids) == 1:
                    observed_ids[subject_id] = next(iter(candidate_ids))
            if expected_observation_ids is not None and observed_ids != expected_observation_ids:
                problems.append(f"subject observation identities differ: {observed_ids!r}")
            expected_subjects = (
                set(expected_observation_subjects)
                if expected_observation_subjects is not None
                else (
                    set(expected_observation_ids)
                    if expected_observation_ids is not None else None
                )
            )
            if expected_subjects is not None and observed_subjects != expected_subjects:
                problems.append(
                    f"subject observation row set differs: {sorted(observed_subjects)!r}"
                )
        rows = snapshot.get("derived_episode_ranges")
        actual_ranges = ()
        if isinstance(rows, (list, tuple)):
            actual_ranges = tuple(
                (
                    str(row.get("bangumi_subject_id") or ""),
                    str(row.get("tmdb_tv_id") or ""),
                    row.get("season_number"),
                    row.get("episode_start"),
                    row.get("episode_end"),
                )
                for row in rows if isinstance(row, dict)
            )
        if actual_ranges != expected_ranges:
            problems.append(f"derived ranges differ: {actual_ranges!r}")
        segment = snapshot.get("work_segment")
        if not isinstance(segment, dict):
            problems.append("selected work segment is absent")
        elif expected_segment is not None and tuple(segment.get("subject_ids") or ()) != expected_segment:
            problems.append(f"selected work segment differs: {segment!r}")
        boundaries = snapshot.get("boundaries")
        if not isinstance(boundaries, (list, tuple)):
            problems.append("work boundaries are absent")
        else:
            actual_boundaries = tuple(
                (
                    str(row.get("from_subject_id") or ""),
                    str(row.get("to_subject_id") or ""),
                    str(row.get("relation") or ""),
                    str(row.get("boundary_kind") or ""),
                    str(row.get("outside_tmdb_tv_id") or ""),
                )
                for row in boundaries if isinstance(row, dict)
            )
            if actual_boundaries != expected_boundaries:
                problems.append(f"work boundaries differ: {actual_boundaries!r}")
        resolution_fingerprint = str(snapshot.get("resolution_fingerprint") or "")
        if len(resolution_fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in resolution_fingerprint
        ):
            problems.append("derived resolution fingerprint is absent")
        resolution_payload = {
            key: snapshot.get(key)
            for key in (
                "tmdb_tv_id", "work_segment", "season_assignments",
                "derived_episode_ranges", "boundaries", "mapping_shape",
                "season_count_policy_version",
            )
        }
        if resolution_fingerprint != _sha256_json(resolution_payload):
            problems.append("derived resolution fingerprint is not canonical")
        if result.state.revision <= result.initial_state.revision:
            problems.append("DecisionState revision did not advance")
        if PlanStore(result.plans).fingerprint(result.plan) == PlanStore(result.plans).fingerprint(result.initial_plan):
            problems.append("current Plan fingerprint did not replace the initial draft")
        current_history = tuple(
            (result.plans / "history" / result.task_id).glob("*.json")
        )
        if len(current_history) < 2:
            problems.append("Plan history does not contain initial and provider revisions")
        selection = result.task.get("selection_operation") or {}
        if selection.get("state") != "succeeded":
            problems.append("provider selection did not durably succeed")
        if require_accepted:
            verification = PlanVerifier().verify(result.plan)
            if verification.status != PlanStatus.ACCEPTED or verification.issues:
                problems.append(
                    "unique provider resolution did not persist an accepted issue-free Plan"
                )
            if result.task.get("workflow_status") != "need_confirm":
                problems.append("unique provider resolution did not return to review")
        return problems

    async def test_f3b_diagnostic_snapshot_is_structured_and_non_mutating(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _make_episode_source(
                root / "source", "Steins Gate 0", tuple(range(1, 24))
            )
            result = await self._run_real_add_select(
                root=root / "flow",
                source=source,
                subjects=STEINS_SUBJECTS,
                relations=STEINS_RELATIONS,
                selected_subject_id="129807",
            )
            task_before = json.dumps(
                result.task,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            plan_before = json.dumps(
                result.plan.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            state_before = json.dumps(
                result.state.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            diagnostic = provider_flow_diagnostics(
                result,
                test_identity=self.id(),
                sequence=("single-provider-selection",),
            )
            self.assertEqual(
                {"calls", "decision", "plan", "queue", "schema_version",
                 "selection", "sequence", "task", "test_identity"},
                set(diagnostic),
            )
            self.assertEqual(2, diagnostic["calls"]["process_count"])
            self.assertEqual(1, diagnostic["calls"]["reidentify_count"])
            self.assertEqual(2, diagnostic["queue"]["join_count"])
            self.assertEqual(0, diagnostic["queue"]["unfinished_tasks_after_stop"])
            self.assertEqual("succeeded", diagnostic["selection"]["state"])
            self.assertEqual("accepted", diagnostic["plan"]["status"])
            self.assertEqual([], diagnostic["plan"]["issue_codes"])
            self.assertEqual(
                task_before,
                json.dumps(
                    result.task,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            self.assertEqual(
                plan_before,
                json.dumps(
                    result.plan.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            self.assertEqual(
                state_before,
                json.dumps(
                    result.state.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )

    async def test_real_steins_selected_subject_freezes_separate_work_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _make_episode_source(root / "source", "Steins Gate", tuple(range(1, 25)))
            result = await self._run_real_add_select(
                root=root / "flow",
                source=source,
                subjects=STEINS_SUBJECTS,
                relations=STEINS_RELATIONS,
                selected_subject_id="10380",
                library_root=root / "library",
            )
            problems = self._nested_snapshot_problems(
                result,
                selected_subject_id="10380",
                expected_tmdb_id="42509",
                expected_ranges=(("10380", "42509", 1, 1, 24),),
                expected_segment=("10380",),
                expected_boundaries=((
                    "10380", "129807", "sequel",
                    "different_unique_tmdb_id", "78102",
                ),),
                expected_observation_ids={"10380": "42509", "129807": "78102"},
            )
            problems.extend(self._entry_projection_problems(
                result,
                season_number=1,
                expected_targets={
                    source / f"Steins Gate [{episode:02d}].mkv": episode
                    for episode in range(1, 25)
                },
                expected_tmdb_id="42509",
                expected_work_root=root / "library" / "Anime" / "命运石之门 (2011)",
            ))
            snapshot = result.plan.to_dict()["metadata"].get("provider_link_snapshot") or {}
            boundaries = snapshot.get("boundaries") or ()
            if not any(
                str(row.get("outside_tmdb_tv_id") or "") == "78102"
                for row in boundaries if isinstance(row, dict)
            ):
                problems.append("Steins;Gate 0 was not frozen as a different-work boundary")
            zero_source = _make_episode_source(
                root / "zero-source", "Steins Gate 0", tuple(range(1, 24))
            )
            zero_result = await self._run_real_add_select(
                root=root / "zero-flow",
                source=zero_source,
                subjects=STEINS_SUBJECTS,
                relations=STEINS_RELATIONS,
                selected_subject_id="129807",
                library_root=root / "library",
            )
            problems.extend(self._nested_snapshot_problems(
                zero_result,
                selected_subject_id="129807",
                expected_tmdb_id="78102",
                expected_ranges=(("129807", "78102", 1, 1, 23),),
                expected_segment=("129807",),
                expected_boundaries=((
                    "10380", "129807", "sequel",
                    "different_unique_tmdb_id", "42509",
                ),),
                expected_observation_ids={"10380": "42509", "129807": "78102"},
            ))
            problems.extend(self._entry_projection_problems(
                zero_result,
                season_number=1,
                expected_targets={
                    zero_source / f"Steins Gate 0 [{episode:02d}].mkv": episode
                    for episode in range(1, 24)
                },
                expected_tmdb_id="78102",
                expected_work_root=root / "library" / "Anime" / "命运石之门 0 (2018)",
            ))
            main_key = str(result.plan.metadata.get("series_key") or "")
            zero_key = str(zero_result.plan.metadata.get("series_key") or "")
            if not main_key or not zero_key or main_key == zero_key:
                problems.append("Steins;Gate and Steins;Gate 0 did not persist separate series keys")
            if result.plan.metadata.get("canonical_work_root") == zero_result.plan.metadata.get(
                "canonical_work_root"
            ):
                problems.append("Steins;Gate and Steins;Gate 0 share one canonical root")
            self.assertEqual(
                [],
                problems,
                format_provider_flow_diagnostics(
                    result,
                    zero_result,
                    test_identity=self.id(),
                    sequence=("steins-main", "steins-zero"),
                ),
            )

    async def test_real_steins_zero_selected_subject_does_not_inherit_main_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _make_episode_source(root / "source", "Steins Gate 0", tuple(range(1, 24)))
            result = await self._run_real_add_select(
                root=root / "flow",
                source=source,
                subjects=STEINS_SUBJECTS,
                relations=STEINS_RELATIONS,
                selected_subject_id="129807",
            )
            problems = self._nested_snapshot_problems(
                result,
                selected_subject_id="129807",
                expected_tmdb_id="78102",
                expected_ranges=(("129807", "78102", 1, 1, 23),),
                expected_segment=("129807",),
                expected_boundaries=((
                    "10380", "129807", "sequel",
                    "different_unique_tmdb_id", "42509",
                ),),
                expected_observation_ids={"10380": "42509", "129807": "78102"},
            )
            problems.extend(self._entry_projection_problems(
                result,
                season_number=1,
                expected_targets={
                    source / f"Steins Gate 0 [{episode:02d}].mkv": episode
                    for episode in range(1, 24)
                },
                expected_tmdb_id="78102",
                expected_work_root=result.root / "Anime" / "命运石之门 0 (2018)",
            ))
            if result.plan.metadata.get("season_number") != 1:
                problems.append("Steins;Gate 0 did not project Season 01")
            self.assertEqual(
                [],
                problems,
                format_provider_flow_diagnostics(
                    result,
                    test_identity=self.id(),
                    sequence=("steins-zero",),
                ),
            )

    async def test_real_yama_selection_persists_all_four_exact_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected_ranges = (
                ("41658", "53918", 1, 1, 13),
                ("86670", "53918", 2, 1, 24),
                ("216310", "53918", 3, 1, 13),
                ("290980", "53918", 4, 1, 12),
            )
            problems = []
            diagnostic_results = []
            yama_series_keys = set()
            yama_work_roots = set()
            for subject_id, season_number, count in (
                ("41658", 1, 13),
                ("86670", 2, 24),
                ("216310", 3, 13),
                ("290980", 4, 12),
            ):
                source = _make_episode_source(
                    root / f"source-{season_number}",
                    f"Yama Season {season_number}",
                    tuple(range(1, count + 1)),
                )
                result = await self._run_real_add_select(
                    root=root / f"flow-{season_number}",
                    source=source,
                    subjects=YAMA_SUBJECTS,
                    relations=YAMA_RELATIONS,
                    selected_subject_id=subject_id,
                    library_root=root / "library",
                )
                diagnostic_results.append(result)
                problems.extend(self._nested_snapshot_problems(
                    result,
                    selected_subject_id=subject_id,
                    expected_tmdb_id="53918",
                    expected_ranges=expected_ranges,
                    expected_segment=("41658", "86670", "216310", "290980"),
                    expected_observation_ids={
                        "41658": "53918", "86670": "53918",
                        "216310": "53918", "290980": "53918",
                    },
                ))
                if result.plan.metadata.get("season_number") != season_number:
                    problems.append(
                        f"selected Yama subject {subject_id} did not project Season {season_number:02d}"
                    )
                yama_series_keys.add(str(result.plan.metadata.get("series_key") or ""))
                yama_work_roots.add(str(result.plan.metadata.get("canonical_work_root") or ""))
                problems.extend(self._entry_projection_problems(
                    result,
                    season_number=season_number,
                    expected_targets={
                        source / f"Yama Season {season_number} [{episode:02d}].mkv": episode
                        for episode in range(1, count + 1)
                    },
                    expected_tmdb_id="53918",
                    expected_work_root=root / "library" / "Anime" / "向山进发 (2013)",
                ))
            if len(yama_series_keys) != 1 or "" in yama_series_keys:
                problems.append(f"Yama seasons did not share one series key: {yama_series_keys!r}")
            if yama_work_roots != {
                str(root / "library" / "Anime" / "向山进发 (2013)"),
            }:
                problems.append(f"Yama canonical roots differ: {yama_work_roots!r}")
            self.assertEqual(
                [],
                problems,
                format_provider_flow_diagnostics(
                    *diagnostic_results,
                    test_identity=self.id(),
                    sequence=("yama-s1", "yama-s2", "yama-s3", "yama-s4"),
                ),
            )

    async def test_real_eighty_six_split_cour_persists_source_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            problems = []
            diagnostic_results = []
            for label, source_numbers, mode, offset, expected_mapping in (
                (
                    "absolute", tuple(range(12, 24)), "absolute", 0,
                    tuple((episode, episode) for episode in range(12, 24)),
                ),
                (
                    "relative", tuple(range(1, 13)), "relative", 11,
                    tuple((episode, episode + 11) for episode in range(1, 13)),
                ),
            ):
                source = _make_episode_source(
                    root / f"source-{label}", f"86 Part 2 {label}", source_numbers,
                    subtitles=True,
                )
                result = await self._run_real_add_select(
                    root=root / f"flow-{label}",
                    source=source,
                    subjects=EIGHTY_SIX_SUBJECTS,
                    relations=EIGHTY_SIX_RELATIONS,
                    selected_subject_id="331887",
                )
                diagnostic_results.append(result)
                problems.extend(self._nested_snapshot_problems(
                    result,
                    selected_subject_id="331887",
                    expected_tmdb_id="100565",
                    expected_ranges=(
                        ("302189", "100565", 1, 1, 11),
                        ("331887", "100565", 1, 12, 23),
                    ),
                    expected_segment=("302189", "331887"),
                    expected_observation_ids={
                        "302189": "100565", "331887": "100565",
                    },
                ))
                projection = result.plan.to_dict()["metadata"].get(
                    "source_episode_projection"
                )
                if not isinstance(projection, dict):
                    problems.append(f"{label} source episode projection is absent from Plan")
                    continue
                mapping = tuple(
                    (row.get("source_episode"), row.get("target_episode"))
                    for row in projection.get("episode_mapping", ())
                    if isinstance(row, dict)
                )
                if mapping != expected_mapping:
                    problems.append(f"86 {label} source projection differs: {mapping!r}")
                if projection.get("mode") != mode or projection.get("offset") != offset:
                    problems.append(f"86 {label} projection mode/offset differs")
                source_rows = tuple(
                    sorted(
                        (
                            str(row.get("source_path") or ""),
                            row.get("source_episode"),
                            row.get("target_episode"),
                        )
                        for row in projection.get("episode_mapping", ())
                        if isinstance(row, dict)
                    )
                )
                expected_source_rows = tuple(
                    sorted(
                        (
                            str(source / f"86 Part 2 {label} [{episode:02d}].mkv"),
                            episode,
                            target,
                        )
                        for episode, target in expected_mapping
                    )
                )
                if source_rows != expected_source_rows:
                    problems.append(f"86 {label} projection did not bind exact source paths")
                inventory_payload = {
                    "inventory_scope": projection.get("inventory_scope"),
                    "source_episodes": tuple(
                        {
                            "source_path": path,
                            "source_episode": source_episode,
                        }
                        for path, source_episode, _target_episode in expected_source_rows
                    ),
                }
                if projection.get("inventory_fingerprint") != _sha256_json(inventory_payload):
                    problems.append(f"86 {label} inventory fingerprint is not canonical")
                if (
                    projection.get("target_episode_start"),
                    projection.get("target_episode_end"),
                ) != (12, 23):
                    problems.append(f"86 {label} target range differs")
                payload = {
                    key: value for key, value in projection.items()
                    if key != "projection_fingerprint"
                }
                if projection.get("projection_fingerprint") != _sha256_json(payload):
                    problems.append(f"{label} source projection fingerprint is not canonical")
                decision_projection = result.state.to_dict().get(
                    "source_episode_projection"
                )
                if decision_projection != projection:
                    problems.append(f"86 {label} DecisionState and Plan projections differ")

                expected_entry_targets = {}
                for source_path, _source_episode, target_episode in expected_source_rows:
                    video_path = Path(source_path)
                    expected_entry_targets[video_path] = target_episode
                    expected_entry_targets[video_path.with_suffix(".sc.ass")] = target_episode
                problems.extend(self._entry_projection_problems(
                    result,
                    season_number=1,
                    expected_targets=expected_entry_targets,
                    expected_tmdb_id="100565",
                    expected_work_root=(
                        result.root / "Anime" / "86—不存在的战区— (2021)"
                    ),
                ))
            self.assertEqual(
                [],
                problems,
                format_provider_flow_diagnostics(
                    *diagnostic_results,
                    test_identity=self.id(),
                    sequence=("86-absolute", "86-relative"),
                ),
            )

    async def test_ambiguous_projection_has_two_real_actions_and_rejects_stale_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _make_ambiguous_episode_versions(root / "source")
            result = await self._run_real_add_select(
                root=root / "flow",
                source=source,
                subjects=EIGHTY_SIX_SUBJECTS,
                relations=EIGHTY_SIX_RELATIONS,
                selected_subject_id="331887",
            )
            plan = result.plan
            state = result.state
            problems = self._nested_snapshot_problems(
                result,
                selected_subject_id="331887",
                expected_tmdb_id="100565",
                expected_ranges=(
                    ("302189", "100565", 1, 1, 11),
                    ("331887", "100565", 1, 12, 23),
                ),
                expected_segment=("302189", "331887"),
                expected_observation_ids={
                    "302189": "100565", "331887": "100565",
                },
                require_accepted=False,
            )
            frozen_provider_snapshot = plan.metadata.get("provider_link_snapshot")
            if result.task.get("workflow_status") != "need_confirm":
                problems.append("ambiguous projection was not kept in the review workflow")
            dialog = object.__new__(PlanReviewDialog)
            dialog.plan = plan
            dialog.task_decision = state
            dialog.task_data = dict(result.task)
            dialog.snapshot = build_plan_review_snapshot(plan)
            dialog.issues_container = ui_fixture._UiNode()
            dialog.review_service = PlanReviewService(
                PlanStore(result.plans), result.reviews, result.root / "work_catalog"
            )
            dialog.decision_command_service = DecisionCommandService(
                DecisionStateStore(result.decisions), PlanStore(result.plans)
            )
            dialog.selected_ids = set()
            dialog._adopt_revision = Mock()
            ui = ui_fixture._CaptureUi()
            notices: list[tuple[str, object]] = []
            with (
                patch.object(review_page, "ui", ui),
                patch.object(review_page, "TASK_PATH", result.tasks),
                patch.object(review_page, "PLAN_PATH", result.plans),
                patch.object(review_page, "PLAN_REVIEW_PATH", result.reviews),
                patch.object(review_page, "DECISION_STATE_PATH", result.decisions),
                patch.object(utils_module, "TASK_PATH", result.tasks),
                patch.object(
                    review_page,
                    "notify",
                    side_effect=lambda message, **kwargs: notices.append(
                        (str(message), kwargs.get("type"))
                    ),
                ),
            ):
                PlanReviewDialog._render_issues(dialog)
                issue_codes = {
                    issue.code for issue in PlanVerifier().verify(plan).issues
                }
                if PlanVerifier().verify(plan).status != PlanStatus.INVALID:
                    problems.append("ambiguous source projection was not held for review")
                if "source_episode_projection_decision_required" not in issue_codes:
                    problems.append("ambiguous source projection has no blocking issue")
                first = ui.callbacks.get("沿用文件中的集号")
                second = ui.callbacks.get("从本段第1集重新编号")
                if not callable(first) or not callable(second):
                    problems.append("ambiguous projection has no two explicit review actions")
                else:
                    value = first()
                    if inspect.isawaitable(value):
                        await value
                    current_state = DecisionStateStore(result.decisions).load(result.task_id)
                    current_plan = PlanStore(result.plans).load(result.task_id)
                    if current_state.revision <= state.revision:
                        problems.append("projection action did not advance DecisionState")
                    projection = (
                        current_plan.to_dict().get("metadata", {}).get(
                            "source_episode_projection"
                        ) or {}
                    )
                    if projection.get("mode") != "absolute":
                        problems.append("absolute action did not persist the chosen projection")
                    if current_state.to_dict().get("source_episode_projection") != projection:
                        problems.append("projection action split DecisionState from Plan")
                    current_task = json.loads(
                        (result.tasks / f"{result.task_id}.json").read_text("utf-8")
                    )
                    verification = PlanVerifier().verify(current_plan)
                    if verification.status != PlanStatus.ACCEPTED or verification.issues:
                        problems.append("chosen projection did not close the blocking review issue")
                    if current_task.get("workflow_status") != "need_confirm":
                        problems.append("chosen projection did not return the task to review")
                    if current_plan.metadata.get("provider_link_snapshot") != frozen_provider_snapshot:
                        problems.append("projection action changed frozen provider evidence")
                    entries_by_source = {
                        Path(entry.source_path): entry for entry in current_plan.entries
                    }
                    absolute_subtitles = tuple(
                        entry for entry in current_plan.entries
                        if entry.media_kind == "subtitle"
                    )
                    if len(absolute_subtitles) != 2:
                        problems.append(
                            "absolute two-version projection did not preserve two subtitles"
                        )
                    for subtitle_entry in absolute_subtitles:
                        video_source = (
                            Path(subtitle_entry.source_path)
                            .with_suffix("").with_suffix(".mkv")
                        )
                        video_entry = entries_by_source.get(video_source)
                        if (
                            video_entry is None
                            or subtitle_entry.subtitle is None
                            or subtitle_entry.subtitle.associated_video_source_path
                            != video_source
                            or subtitle_entry.target_path.parent
                            != video_entry.target_path.parent
                            or subtitle_entry.target_path.name
                            != f"{video_entry.target_path.stem}.zh-Hans.ass"
                        ):
                            problems.append(
                                "absolute two-version subtitle lost its exact video owner: "
                                f"{subtitle_entry.source_path}"
                            )
                    _ReviewCapture.mounted.clear()
                    with (
                        patch.object(review_page, "TASK_PATH", result.tasks),
                        patch.object(review_page, "PLAN_PATH", result.plans),
                        patch.object(review_page, "PLAN_REVIEW_PATH", result.reviews),
                        patch.object(review_page, "DECISION_STATE_PATH", result.decisions),
                        patch.object(utils_module, "TASK_PATH", result.tasks),
                        patch.object(review_page, "PlanReviewDialog", _ReviewCapture),
                        patch.object(
                            review_page, "_dialog_mount_context", return_value=nullcontext()
                        ),
                    ):
                        await review_page.plan_review_page(result.task_id)
                    if len(_ReviewCapture.mounted) != 1:
                        problems.append("projection action did not reopen through the real page route")
                    elif (
                        PlanStore(result.plans).fingerprint(_ReviewCapture.mounted[0][0])
                        != PlanStore(result.plans).fingerprint(current_plan)
                    ):
                        problems.append("real page reopen mounted a stale projection Plan")
                    reload_dialog = object.__new__(PlanReviewDialog)
                    reload_dialog.plan = (
                        _ReviewCapture.mounted[0][0]
                        if len(_ReviewCapture.mounted) == 1 else current_plan
                    )
                    reload_dialog.task_decision = current_state
                    reload_dialog.task_data = (
                        _ReviewCapture.mounted[0][1]
                        if len(_ReviewCapture.mounted) == 1 else current_task
                    )
                    reload_dialog.snapshot = build_plan_review_snapshot(reload_dialog.plan)
                    reload_dialog.issues_container = ui_fixture._UiNode()
                    reload_dialog.review_service = dialog.review_service
                    reload_dialog.decision_command_service = dialog.decision_command_service
                    reload_dialog.selected_ids = set()
                    reload_dialog._adopt_revision = Mock()
                    reload_ui = ui_fixture._CaptureUi()
                    with patch.object(review_page, "ui", reload_ui):
                        PlanReviewDialog._render_issues(reload_dialog)
                    if not any(
                        "沿用文件中的集号" in value for value in reload_ui.visible
                    ):
                        problems.append("reopened review did not show the persisted projection choice")
                    frozen = {
                        "task": (result.tasks / f"{result.task_id}.json").read_bytes(),
                        "plan": (result.plans / f"{result.task_id}.json").read_bytes(),
                        "decision": (result.decisions / f"{result.task_id}.json").read_bytes(),
                    }
                    value = second()
                    if inspect.isawaitable(value):
                        await value
                    current = {
                        "task": (result.tasks / f"{result.task_id}.json").read_bytes(),
                        "plan": (result.plans / f"{result.task_id}.json").read_bytes(),
                        "decision": (result.decisions / f"{result.task_id}.json").read_bytes(),
                    }
                    if current != frozen:
                        problems.append("stale projection callback changed persisted authority")
                    if not any(
                        kind == "warning" and ("刷新" in message or "更新" in message)
                        for message, kind in notices
                    ):
                        problems.append("stale projection callback gave no explicit refresh notice")

            relative_source = _make_ambiguous_episode_versions(
                root / "relative-source"
            )
            relative_result = await self._run_real_add_select(
                root=root / "relative-flow",
                source=relative_source,
                subjects=EIGHTY_SIX_SUBJECTS,
                relations=EIGHTY_SIX_RELATIONS,
                selected_subject_id="331887",
            )
            problems.extend(self._nested_snapshot_problems(
                relative_result,
                selected_subject_id="331887",
                expected_tmdb_id="100565",
                expected_ranges=(
                    ("302189", "100565", 1, 1, 11),
                    ("331887", "100565", 1, 12, 23),
                ),
                expected_segment=("302189", "331887"),
                expected_observation_ids={
                    "302189": "100565", "331887": "100565",
                },
                require_accepted=False,
            ))
            relative_dialog = object.__new__(PlanReviewDialog)
            relative_dialog.plan = relative_result.plan
            relative_dialog.task_decision = relative_result.state
            relative_dialog.task_data = dict(relative_result.task)
            relative_dialog.snapshot = build_plan_review_snapshot(relative_result.plan)
            relative_dialog.issues_container = ui_fixture._UiNode()
            relative_dialog.review_service = PlanReviewService(
                PlanStore(relative_result.plans),
                relative_result.reviews,
                relative_result.root / "work_catalog",
            )
            relative_dialog.decision_command_service = DecisionCommandService(
                DecisionStateStore(relative_result.decisions),
                PlanStore(relative_result.plans),
            )
            relative_dialog.selected_ids = set()
            relative_dialog._adopt_revision = Mock()
            relative_ui = ui_fixture._CaptureUi()
            with (
                patch.object(review_page, "ui", relative_ui),
                patch.object(review_page, "TASK_PATH", relative_result.tasks),
                patch.object(review_page, "PLAN_PATH", relative_result.plans),
                patch.object(review_page, "PLAN_REVIEW_PATH", relative_result.reviews),
                patch.object(
                    review_page, "DECISION_STATE_PATH", relative_result.decisions
                ),
                patch.object(utils_module, "TASK_PATH", relative_result.tasks),
                patch.object(review_page, "notify"),
            ):
                PlanReviewDialog._render_issues(relative_dialog)
                relative_action = relative_ui.callbacks.get("从本段第1集重新编号")
                if not callable(relative_action):
                    problems.append("fresh relative projection action is not executable")
                else:
                    value = relative_action()
                    if inspect.isawaitable(value):
                        await value
            relative_state = DecisionStateStore(relative_result.decisions).load(
                relative_result.task_id
            )
            relative_plan = PlanStore(relative_result.plans).load(
                relative_result.task_id
            )
            relative_task = json.loads(
                (relative_result.tasks / f"{relative_result.task_id}.json").read_text(
                    "utf-8"
                )
            )
            relative_projection = (
                relative_plan.to_dict().get("metadata", {}).get(
                    "source_episode_projection"
                ) or {}
            )
            if relative_state.revision <= relative_result.state.revision:
                problems.append("fresh relative action did not advance DecisionState")
            if (
                relative_projection.get("mode") != "relative"
                or relative_projection.get("offset") != 11
            ):
                problems.append("fresh relative action did not persist the relative choice")
            if relative_state.to_dict().get(
                "source_episode_projection"
            ) != relative_projection:
                problems.append("fresh relative action split DecisionState from Plan")
            relative_verification = PlanVerifier().verify(relative_plan)
            if (
                relative_verification.status != PlanStatus.ACCEPTED
                or relative_verification.issues
            ):
                problems.append("fresh relative action did not produce an approvable Plan")
            if relative_task.get("workflow_status") != "need_confirm":
                problems.append("fresh relative action did not return the task to review")
            relative_selection = relative_task.get("selection_operation")
            if isinstance(relative_selection, dict) and relative_selection.get(
                "state"
            ) == "failed":
                problems.append("fresh relative action left a failed selection operation")
            relative_result.plan = relative_plan
            relative_result.state = relative_state
            relative_result.task = relative_task
            problems.extend(self._entry_projection_problems(
                relative_result,
                season_number=1,
                expected_targets={path: 23 for path in relative_result.source_files},
                expected_tmdb_id="100565",
                expected_work_root=(
                    relative_result.root / "Anime" / "86—不存在的战区— (2021)"
                ),
            ))
            entries_by_source = {
                Path(entry.source_path): entry for entry in relative_plan.entries
            }
            subtitle_entries = tuple(
                entry for entry in relative_plan.entries if entry.media_kind == "subtitle"
            )
            if len(subtitle_entries) != 2:
                problems.append("two-version projection did not preserve two subtitles")
            for subtitle_entry in subtitle_entries:
                video_source = Path(subtitle_entry.source_path).with_suffix("").with_suffix(
                    ".mkv"
                )
                video_entry = entries_by_source.get(video_source)
                if (
                    video_entry is None
                    or subtitle_entry.subtitle is None
                    or subtitle_entry.subtitle.associated_video_source_path != video_source
                    or subtitle_entry.target_path.parent != video_entry.target_path.parent
                    or subtitle_entry.target_path.name
                    != f"{video_entry.target_path.stem}.zh-Hans.ass"
                ):
                    problems.append(
                        f"two-version subtitle lost its exact video owner: {subtitle_entry.source_path}"
                    )
            visible = "\n".join(ui.visible)
            for internal in (
                "schema", "fingerprint", "series_key", "binding",
                "subject_observations", "work_segment", "resolution", "offset",
            ):
                if internal in visible.casefold():
                    problems.append(f"visible projection UI exposed internal term {internal}")
            self.assertEqual([], problems)

    async def test_provider_evidence_failure_persists_reason_and_restart_does_not_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _make_episode_source(root / "source", "Steins Gate", tuple(range(1, 25)))
            result = await self._run_real_add_select(
                root=root / "flow",
                source=source,
                subjects=STEINS_SUBJECTS,
                relations=STEINS_RELATIONS,
                selected_subject_id="10380",
                fail_subject=("steins;gate 0", "命运石之门 0"),
            )
            problems = self._real_path_problems(result)
            failure = result.task.get("selection_operation")
            if not isinstance(failure, dict):
                problems.append("provider evidence failure is not durably structured")
            else:
                if failure.get("state") != "failed":
                    problems.append("provider selection operation is not durably failed")
                if failure.get("selected_subject_id") != "10380":
                    problems.append("failure lost the selected Bangumi subject")
                if not failure.get("reason_codes"):
                    problems.append("failure reason codes are absent")
                elif "subject_candidate_transport_error" not in failure.get("reason_codes", ()):
                    problems.append("transport failure lost its structured reason code")
                if len(str(failure.get("evidence_fingerprint") or "")) != 64:
                    problems.append("failure evidence fingerprint is absent")
                if failure.get("retry_requires_refresh") is not True:
                    problems.append("failure does not require refreshed evidence")
            if (result.plans / f"{result.task_id}.json").read_bytes() != result.initial_plan_bytes:
                problems.append("failed provider evidence replaced the current Plan")
            if (result.decisions / f"{result.task_id}.json").read_bytes() != result.initial_state_bytes:
                problems.append("failed provider evidence replaced DecisionState")

            task_path = result.tasks / f"{result.task_id}.json"
            task_before_restart = task_path.read_bytes()
            _ReviewCapture.mounted.clear()
            restart_executor = AsyncMock()
            restarted = TaskQueueManager(
                task_root=result.tasks,
                executor=restart_executor,
                decision_state_root=result.decisions,
                recovery_abandon_audit_root=result.root / "abandon",
                plan_root=result.plans,
                test_mode=False,
            )
            with (
                patch.object(review_page, "TASK_PATH", result.tasks),
                patch.object(review_page, "PLAN_PATH", result.plans),
                patch.object(review_page, "DECISION_STATE_PATH", result.decisions),
                patch.object(utils_module, "TASK_PATH", result.tasks),
                patch.object(review_page, "PlanReviewDialog", _ReviewCapture),
                patch.object(review_page, "RecognitionRecoveryDialog", ui_fixture._NoopAnyDialog),
                patch.object(review_page, "_dialog_mount_context", return_value=nullcontext()),
                patch.object(review_page, "notify"),
            ):
                await restarted.start()
                try:
                    await review_page.plan_review_page(result.task_id)
                    await asyncio.sleep(0)
                finally:
                    await restarted.stop()
            if restarted._queue.qsize() or restarted._pending:
                problems.append("restart automatically requeued failed provider evidence")
            if restart_executor.await_count:
                problems.append("restart executed the failed provider selection again")
            if task_path.read_bytes() != task_before_restart:
                problems.append("restart rewrote the persisted provider failure")
            if len(_ReviewCapture.mounted) != 1:
                problems.append("restart did not reconstruct the persisted review state")
            elif _ReviewCapture.mounted[0][1].get("selection_operation") != failure:
                problems.append("refreshed review lost the persisted failure reason")

            failure_dialog = object.__new__(PlanReviewDialog)
            reloaded_failure_task = json.loads(task_path.read_text("utf-8"))
            failure_dialog.task_data = reloaded_failure_task
            failure_dialog._show_provider_candidates = Mock()
            failure_dialog._close_review = Mock()
            failure_dialog._select_provider_candidate = Mock()
            failure_ui = ui_fixture._CaptureUi()
            failure_ui.card = failure_ui.column
            with patch.object(review_page, "ui", failure_ui):
                PlanReviewDialog._render_selection_operation(failure_dialog)
            visible = "\n".join(failure_ui.visible)
            for expected in (
                "这次选择未生效", "重新获取资料", "换一个作品", "移除任务", "关闭"
            ):
                if expected not in visible:
                    problems.append(f"refreshed failure UI lost action/text: {expected}")
            for action in ("重新获取资料", "换一个作品", "移除任务"):
                if not callable(failure_ui.callbacks.get(action)):
                    problems.append(f"transport failure action is not executable: {action}")
            if "重试" in failure_ui.callbacks:
                problems.append("refresh-required failure still exposes the old blind retry action")
            for internal in (
                "multiple_tmdb_candidates", "subject_observations",
                "fingerprint", "series_key", "relation_unresolved",
            ):
                if internal in visible.casefold():
                    problems.append(f"failure UI exposed internal term {internal}")

            no_match_source = _make_episode_source(
                root / "no-match-source", "Steins Gate", tuple(range(1, 25))
            )
            no_match_result = await self._run_real_add_select(
                root=root / "no-match-flow",
                source=no_match_source,
                subjects=STEINS_SUBJECTS,
                relations=STEINS_RELATIONS,
                selected_subject_id="10380",
                fail_subject=("steins;gate 0", "命运石之门 0"),
                failure_mode="no_match",
            )
            problems.extend(self._real_path_problems(no_match_result))
            no_match_failure = no_match_result.task.get("selection_operation") or {}
            if "subject_candidate_evidence_missing" not in no_match_failure.get(
                "reason_codes", ()
            ):
                problems.append("no-match neighbour lost its manual-boundary reason")
            no_match_evidence = str(
                no_match_failure.get("evidence_fingerprint") or ""
            )
            if (
                no_match_failure.get("state") != "failed"
                or str(no_match_failure.get("selected_subject_id") or "") != "10380"
                or str(
                    (no_match_failure.get("candidate") or {}).get("provider_id") or ""
                ) != "10380"
                or no_match_failure.get("expected_decision_revision")
                != no_match_result.state.revision
                or len(no_match_evidence) != 64
                or any(char not in "0123456789abcdef" for char in no_match_evidence)
            ):
                problems.append("no-match failure is not bound to exact selection authority")
            if no_match_failure.get("retry_requires_refresh") is True:
                problems.append("no-match neighbour was mislabeled as a transport retry")
            no_match_dialog = object.__new__(PlanReviewDialog)
            no_match_dialog.plan = no_match_result.plan
            no_match_dialog.task_decision = no_match_result.state
            no_match_dialog.task_data = dict(no_match_result.task)
            no_match_dialog.snapshot = build_plan_review_snapshot(no_match_result.plan)
            no_match_dialog.review_service = PlanReviewService(
                PlanStore(no_match_result.plans),
                no_match_result.reviews,
                no_match_result.root / "work_catalog",
            )
            no_match_dialog.decision_command_service = DecisionCommandService(
                DecisionStateStore(no_match_result.decisions),
                PlanStore(no_match_result.plans),
            )
            no_match_dialog.selected_ids = set()
            no_match_dialog._adopt_revision = Mock()
            no_match_dialog._show_provider_candidates = Mock()
            no_match_dialog._close_review = Mock()
            no_match_dialog._select_provider_candidate = Mock()
            no_match_ui = ui_fixture._CaptureUi()
            no_match_ui.card = no_match_ui.column
            no_match_notices: list[tuple[str, object]] = []
            with (
                patch.object(review_page, "ui", no_match_ui),
                patch.object(review_page, "TASK_PATH", no_match_result.tasks),
                patch.object(review_page, "PLAN_PATH", no_match_result.plans),
                patch.object(review_page, "PLAN_REVIEW_PATH", no_match_result.reviews),
                patch.object(review_page, "DECISION_STATE_PATH", no_match_result.decisions),
                patch.object(utils_module, "TASK_PATH", no_match_result.tasks),
                _root_config_patch(no_match_result.task["draft_root_snapshot"]),
                patch.object(
                    review_page,
                    "notify",
                    side_effect=lambda message, **kwargs: no_match_notices.append(
                        (str(message), kwargs.get("type"))
                    ),
                ),
            ):
                PlanReviewDialog._render_selection_operation(no_match_dialog)
                for action in (
                    "确认与相邻作品一起整理", "分开整理",
                    "换一个作品", "移除任务",
                ):
                    if not callable(no_match_ui.callbacks.get(action)):
                        problems.append(f"no-match neighbour action is not executable: {action}")
                separate = no_match_ui.callbacks.get("分开整理")
                together = no_match_ui.callbacks.get("确认与相邻作品一起整理")
                if callable(separate):
                    value = separate()
                    if inspect.isawaitable(value):
                        await value
                    separated_state = DecisionStateStore(no_match_result.decisions).load(
                        no_match_result.task_id
                    )
                    separated_plan = PlanStore(no_match_result.plans).load(
                        no_match_result.task_id
                    )
                    separated_task = json.loads(
                        (no_match_result.tasks / f"{no_match_result.task_id}.json").read_text(
                            "utf-8"
                        )
                    )
                    if separated_state.revision <= no_match_result.state.revision:
                        problems.append("separate-work action did not advance DecisionState")
                    separated_snapshot = separated_plan.to_dict()["metadata"].get(
                        "provider_link_snapshot"
                    )
                    if separated_snapshot != separated_state.to_dict().get(
                        "provider_link_binding"
                    ):
                        problems.append("separate-work action split DecisionState from Plan")
                    boundary_rows = (
                        separated_snapshot.get("boundaries", ())
                        if isinstance(separated_snapshot, dict) else ()
                    )
                    expected_evidence = no_match_evidence
                    if not any(
                        str(row.get("from_subject_id") or "") == "10380"
                        and str(row.get("to_subject_id") or "") == "129807"
                        and row.get("boundary_kind") == "explicit_user_separate"
                        and str(row.get("evidence_fingerprint") or "") == expected_evidence
                        for row in boundary_rows if isinstance(row, dict)
                    ):
                        problems.append("separate-work decision is not bound to the exact edge/evidence")
                    verification = PlanVerifier().verify(separated_plan)
                    if verification.status != PlanStatus.ACCEPTED or verification.issues:
                        problems.append("separate-work action did not produce an approvable Plan")
                    if separated_task.get("workflow_status") != "need_confirm":
                        problems.append("separate-work action did not return to review")
                    separated_selection = separated_task.get("selection_operation")
                    if not isinstance(separated_selection, dict) or (
                        separated_selection.get("state") != "succeeded"
                        or separated_selection.get("result_decision_revision")
                        != separated_state.revision
                        or str(
                            (separated_selection.get("candidate") or {}).get(
                                "provider_id"
                            ) or ""
                        ) != "10380"
                        or str(separated_selection.get("evidence_fingerprint") or "")
                        != expected_evidence
                    ):
                        problems.append(
                            "separate-work action did not close the exact failed selection"
                        )
                    frozen_paths = tuple(
                        path
                        for path in (
                            no_match_result.tasks / f"{no_match_result.task_id}.json",
                            no_match_result.plans / f"{no_match_result.task_id}.json",
                            no_match_result.decisions / f"{no_match_result.task_id}.json",
                            no_match_result.reviews / f"{no_match_result.task_id}.json",
                        )
                        if path.exists()
                    )
                    frozen = {path: path.read_bytes() for path in frozen_paths}
                    if callable(together):
                        value = together()
                        if inspect.isawaitable(value):
                            await value
                    if {path: path.read_bytes() for path in frozen_paths} != frozen:
                        problems.append("stale work-boundary callback changed persisted authority")
                    if not any(
                        kind == "warning" and ("刷新" in message or "更新" in message)
                        for message, kind in no_match_notices
                    ):
                        problems.append("stale work-boundary callback gave no refresh notice")
            if "重新获取资料" in no_match_ui.callbacks:
                problems.append("no-match neighbour incorrectly exposes transport refresh")

            same_source = _make_episode_source(
                root / "same-work-source", "Yama Season 1", tuple(range(1, 14))
            )
            same_result = await self._run_real_add_select(
                root=root / "same-work-flow",
                source=same_source,
                subjects=YAMA_SUBJECTS,
                relations=YAMA_RELATIONS,
                selected_subject_id="41658",
                fail_subject=("第二季", "セカンドシーズン"),
                failure_mode="no_match",
            )
            same_failed_revision = same_result.state.revision
            problems.extend(self._real_path_problems(same_result))
            same_failure = same_result.task.get("selection_operation") or {}
            if "subject_candidate_evidence_missing" not in same_failure.get(
                "reason_codes", ()
            ):
                problems.append("fresh same-work flow did not persist a missing-neighbour decision")
            same_evidence = str(same_failure.get("evidence_fingerprint") or "")
            if (
                same_failure.get("state") != "failed"
                or str(same_failure.get("selected_subject_id") or "") != "41658"
                or str(
                    (same_failure.get("candidate") or {}).get("provider_id") or ""
                ) != "41658"
                or same_failure.get("expected_decision_revision") != same_failed_revision
                or len(same_evidence) != 64
                or any(char not in "0123456789abcdef" for char in same_evidence)
            ):
                problems.append("fresh same-work failure is not bound to exact authority")
            same_dialog = object.__new__(PlanReviewDialog)
            same_dialog.plan = same_result.plan
            same_dialog.task_decision = same_result.state
            same_dialog.task_data = dict(same_result.task)
            same_dialog.snapshot = build_plan_review_snapshot(same_result.plan)
            same_dialog.review_service = PlanReviewService(
                PlanStore(same_result.plans),
                same_result.reviews,
                same_result.root / "work_catalog",
            )
            same_dialog.decision_command_service = DecisionCommandService(
                DecisionStateStore(same_result.decisions), PlanStore(same_result.plans)
            )
            same_dialog.selected_ids = set()
            same_dialog._adopt_revision = Mock()
            same_dialog._show_provider_candidates = Mock()
            same_dialog._close_review = Mock()
            same_dialog._select_provider_candidate = Mock()
            same_ui = ui_fixture._CaptureUi()
            same_ui.card = same_ui.column
            with (
                patch.object(review_page, "ui", same_ui),
                patch.object(review_page, "TASK_PATH", same_result.tasks),
                patch.object(review_page, "PLAN_PATH", same_result.plans),
                patch.object(review_page, "PLAN_REVIEW_PATH", same_result.reviews),
                patch.object(review_page, "DECISION_STATE_PATH", same_result.decisions),
                patch.object(utils_module, "TASK_PATH", same_result.tasks),
                _root_config_patch(same_result.task["draft_root_snapshot"]),
                patch.object(review_page, "notify"),
            ):
                PlanReviewDialog._render_selection_operation(same_dialog)
                same_action = same_ui.callbacks.get("确认与相邻作品一起整理")
                if not callable(same_action):
                    problems.append("fresh same-work action is not executable")
                else:
                    value = same_action()
                    if inspect.isawaitable(value):
                        await value
            same_state = DecisionStateStore(same_result.decisions).load(
                same_result.task_id
            )
            same_plan = PlanStore(same_result.plans).load(same_result.task_id)
            same_task = json.loads(
                (same_result.tasks / f"{same_result.task_id}.json").read_text("utf-8")
            )
            same_result.state = same_state
            same_result.plan = same_plan
            same_result.task = same_task
            problems.extend(self._nested_snapshot_problems(
                same_result,
                selected_subject_id="41658",
                expected_tmdb_id="53918",
                expected_ranges=(
                    ("41658", "53918", 1, 1, 13),
                    ("86670", "53918", 2, 1, 24),
                    ("216310", "53918", 3, 1, 13),
                    ("290980", "53918", 4, 1, 12),
                ),
                expected_segment=("41658", "86670", "216310", "290980"),
                expected_observation_ids={
                    "41658": "53918", "216310": "53918", "290980": "53918",
                },
                expected_observation_subjects=(
                    "41658", "86670", "216310", "290980",
                ),
            ))
            problems.extend(self._entry_projection_problems(
                same_result,
                season_number=1,
                expected_targets={
                    same_source / f"Yama Season 1 [{episode:02d}].mkv": episode
                    for episode in range(1, 14)
                },
                expected_tmdb_id="53918",
                expected_work_root=(
                    same_result.root / "Anime" / "向山进发 (2013)"
                ),
            ))
            if same_state.revision <= same_failed_revision:
                problems.append("fresh same-work action did not advance DecisionState")
            same_selection = same_task.get("selection_operation")
            if not isinstance(same_selection, dict) or (
                same_selection.get("state") != "succeeded"
                or same_selection.get("result_decision_revision") != same_state.revision
                or str(
                    (same_selection.get("candidate") or {}).get("provider_id") or ""
                ) != "41658"
                or str(same_selection.get("evidence_fingerprint") or "")
                != same_evidence
            ):
                problems.append("fresh same-work action did not close the exact failure")
            boundary_decisions = same_state.to_dict().get(
                "provider_subject_boundary_decisions"
            )
            if isinstance(boundary_decisions, dict):
                decision_rows = tuple(
                    row for row in boundary_decisions.values()
                    if isinstance(row, dict)
                )
            elif isinstance(boundary_decisions, (list, tuple)):
                decision_rows = tuple(
                    row for row in boundary_decisions if isinstance(row, dict)
                )
            else:
                decision_rows = ()
            expected_cohort_edges = {
                ("41658", "86670", "sequel"),
                ("86670", "216310", "sequel"),
                ("216310", "290980", "sequel"),
            }
            matching_same_decisions = tuple(
                row for row in decision_rows
                if str(row.get("decision") or "") == "same_work"
                and str(row.get("evidence_fingerprint") or "") == same_evidence
                and row.get("expected_decision_revision") == same_failed_revision
                and tuple(row.get("cohort_subject_ids") or ()) == (
                    "41658", "86670", "216310", "290980",
                )
            )
            actual_cohort_edges = {
                (
                    str(row.get("from_subject_id") or ""),
                    str(row.get("to_subject_id") or ""),
                    str(row.get("relation") or ""),
                )
                for row in matching_same_decisions
            }
            cohort_decision_ids = {
                str(row.get("cohort_decision_id") or "")
                for row in matching_same_decisions
            }
            if (
                len(decision_rows) != 3
                or len(matching_same_decisions) != 3
                or actual_cohort_edges != expected_cohort_edges
                or len(cohort_decision_ids) != 1
                or "" in cohort_decision_ids
            ):
                problems.append(
                    "fresh same-work decisions are not one exact cohort/evidence/revision closed set"
                )

            removal_source = _make_episode_source(
                root / "removal-source", "Steins Gate", tuple(range(1, 25))
            )
            removal_result = await self._run_real_add_select(
                root=root / "removal-flow",
                source=removal_source,
                subjects=STEINS_SUBJECTS,
                relations=STEINS_RELATIONS,
                selected_subject_id="10380",
                fail_subject=("steins;gate 0", "命运石之门 0"),
                failure_mode="no_match",
            )
            problems.extend(self._real_path_problems(removal_result))
            removal_dialog = object.__new__(PlanReviewDialog)
            removal_dialog.plan = removal_result.plan
            removal_dialog.task_decision = removal_result.state
            removal_dialog.task_data = dict(removal_result.task)
            removal_dialog.snapshot = build_plan_review_snapshot(removal_result.plan)
            removal_dialog.review_service = PlanReviewService(
                PlanStore(removal_result.plans), removal_result.reviews,
                removal_result.root / "work_catalog",
            )
            removal_dialog.decision_command_service = DecisionCommandService(
                DecisionStateStore(removal_result.decisions), PlanStore(removal_result.plans)
            )
            removal_dialog.selected_ids = set()
            removal_dialog._adopt_revision = Mock()
            removal_dialog._show_provider_candidates = Mock()
            removal_dialog._close_review = Mock()
            removal_dialog._select_provider_candidate = Mock()
            removal_ui = ui_fixture._CaptureUi()
            removal_ui.card = removal_ui.column
            source_before_removal = _source_manifest(removal_source)
            protected_roots = (
                removal_result.root / "records",
                removal_result.root / "provider_links",
                removal_result.root / "series_catalog",
                removal_result.root / "work_catalog",
                removal_result.root / "recovery",
            )
            protected_before = {
                item: _byte_manifest(item) for item in protected_roots
            }
            removal_manager = TaskQueueManager(
                task_root=removal_result.tasks,
                decision_state_root=removal_result.decisions,
                recovery_abandon_audit_root=removal_result.root / "abandon",
                plan_root=removal_result.plans,
                test_mode=False,
            )
            with (
                patch.object(review_page, "ui", removal_ui),
                patch.object(review_page, "task_queue", removal_manager),
                patch.object(review_page, "TASK_PATH", removal_result.tasks),
                patch.object(review_page, "PLAN_PATH", removal_result.plans),
                patch.object(review_page, "PLAN_REVIEW_PATH", removal_result.reviews),
                patch.object(review_page, "DECISION_STATE_PATH", removal_result.decisions),
                patch.object(utils_module, "TASK_PATH", removal_result.tasks),
                patch.object(dashboard_page, "TASK_PATH", removal_result.tasks),
                patch.object(
                    dashboard_page, "RECORD_PATH", removal_result.root / "records"
                ),
                patch.object(dashboard_page, "PLAN_PATH", removal_result.plans),
                patch.object(
                    dashboard_page, "PLAN_REVIEW_PATH", removal_result.reviews
                ),
                patch.object(
                    dashboard_page,
                    "DECISION_STATE_PATH",
                    removal_result.decisions,
                ),
                patch.object(
                    dashboard_page,
                    "RECOVERY_ABANDON_AUDIT_PATH",
                    removal_result.root / "abandon",
                ),
                patch.object(
                    dashboard_page,
                    "WORK_CATALOG_PATH",
                    removal_result.root / "work_catalog",
                ),
                patch.object(
                    dashboard_page, "refresh_dashboard", return_value=True
                ) as dashboard_refresh,
                patch.object(
                    dashboard_page._review_tasks, "refresh"
                ) as review_rows_refresh,
                patch.object(review_page, "notify"),
            ):
                PlanReviewDialog._render_selection_operation(removal_dialog)
                remove_action = removal_ui.callbacks.get("移除任务")
                if not callable(remove_action):
                    problems.append("provider failure has no executable remove-task action")
                else:
                    value = remove_action()
                    if inspect.isawaitable(value):
                        await value
            if (removal_result.tasks / f"{removal_result.task_id}.json").exists():
                problems.append("remove-task action did not remove the active task card")
            if removal_dialog._close_review.call_count != 1:
                problems.append("remove-task action did not close the review dialog once")
            if not dashboard_refresh.call_count and not review_rows_refresh.call_count:
                problems.append("remove-task action did not refresh the mounted dashboard")
            if any(
                str(row.get("uuid") or "") == removal_result.task_id
                for row in dashboard_page.load_task_rows(removal_result.tasks)
            ):
                problems.append("removed provider task remained in dashboard reload rows")
            if _source_manifest(removal_source) != source_before_removal:
                problems.append("remove-task action changed source media")
            if {item: _byte_manifest(item) for item in protected_roots} != protected_before:
                problems.append("remove-task action changed Catalog, record, or recovery state")
            if removal_result.anime_root.exists() or removal_result.anime_movie_root.exists():
                problems.append("remove-task action created or changed a target library")
            self.assertEqual([], problems)

    async def test_legacy_range_less_split_cour_requires_regeneration_without_migration(self):
        """A consistent old approved authority may be synthetic; new output may not."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _make_episode_source(
                root / "source", "86 Part 2", tuple(range(12, 24))
            )
            result = await self._run_real_add_select(
                root=root / "flow",
                source=source,
                subjects=EIGHTY_SIX_SUBJECTS,
                relations=EIGHTY_SIX_RELATIONS,
                selected_subject_id="331887",
            )
            current_snapshot = result.plan.metadata.get("provider_link_snapshot")
            self.assertIsInstance(
                current_snapshot,
                Mapping,
                "the real 86 Plan must exist before the compatibility fixture is downgraded",
            )
            self.assertEqual("100565", str(current_snapshot.get("tmdb_tv_id") or ""))
            self.assertEqual(1, result.plan.metadata.get("season_number"))
            legacy_snapshot = dict(result.state.provider_link_binding or {})
            for key in (
                "subject_observations", "work_segment", "boundaries",
                "derived_episode_ranges", "mapping_shape",
                "season_count_policy_version", "resolution_fingerprint",
            ):
                legacy_snapshot.pop(key, None)
            legacy_snapshot.update({
                "schema_version": 1,
                "automation_level": "L2",
                "evidence_path": ["legacy-range-less-split-cour"],
            })
            metadata = dict(result.plan.to_dict()["metadata"])
            metadata["provider_link_snapshot"] = legacy_snapshot
            metadata.pop("source_episode_projection", None)
            legacy_plan_root = root / "legacy-plan"
            legacy_review_root = root / "legacy-review"
            legacy_decision_root = root / "legacy-decision"
            legacy_task_root = root / "legacy-task"
            legacy_task_root.mkdir(parents=True)
            decision_store = DecisionStateStore(legacy_decision_root)
            legacy_state_changes = {
                "revision": 0,
                "provider_link_binding": legacy_snapshot,
            }
            for field, empty_value in (
                ("source_episode_projection", None),
                ("source_episode_projection_decision", None),
                ("provider_subject_boundary_decisions", {}),
            ):
                if hasattr(result.state, field):
                    legacy_state_changes[field] = empty_value
            legacy_state = decision_store.save(
                replace(result.state, **legacy_state_changes),
                expected_revision=-1,
            )
            legacy_store = PlanStore(legacy_plan_root)
            legacy = decision_store.project_plan(
                legacy_state,
                replace(result.plan, metadata=metadata),
            )
            self.assertNotIn("source_episode_projection", legacy.metadata)
            self.assertFalse(
                legacy_state.to_dict().get("source_episode_projection"),
                "legacy DecisionState must not silently restore a new projection",
            )
            legacy = save_plan_fixture(
                legacy_store, legacy, state_store=decision_store
            )
            fingerprint = legacy_store.fingerprint(legacy)
            review_service = PlanReviewService(
                legacy_store, legacy_review_root, root / "legacy-work-catalog"
            )
            review_service._save(PlanReviewDecision(
                task_id=legacy.task_id,
                plan_fingerprint=fingerprint,
                approved_entry_ids=tuple(sorted(entry_id(item) for item in legacy.entries)),
                status=ReviewStatus.APPROVED,
            ))
            legacy_task = dict(result.task)
            for field in (
                "selection_operation", "completed_at", "execution_phase",
                "execution_failure", "execution_recovery_state", "queue_position",
            ):
                legacy_task.pop(field, None)
            legacy_task.update({
                "workflow_status": "approved",
                "plan_status": "accepted",
                "plan_entry_count": len(legacy.entries),
                "error": None,
                "execution_requested_fingerprint": None,
                "execution_reapproval_fingerprint": None,
            })
            task_path = legacy_task_root / f"{legacy.task_id}.json"
            task_path.write_text(
                json.dumps(legacy_task, ensure_ascii=False, indent=2), "utf-8"
            )
            authority_paths = (
                legacy_store.path_for(legacy.task_id),
                review_service._path_helper.path_for(legacy.task_id),
                decision_store.path_for(legacy.task_id),
                task_path,
            )
            before = {path: path.read_bytes() for path in authority_paths}
            _ReviewCapture.mounted.clear()
            with (
                patch.object(review_page, "TASK_PATH", legacy_task_root),
                patch.object(review_page, "PLAN_PATH", legacy_plan_root),
                patch.object(review_page, "PLAN_REVIEW_PATH", legacy_review_root),
                patch.object(review_page, "DECISION_STATE_PATH", legacy_decision_root),
                patch.object(utils_module, "TASK_PATH", legacy_task_root),
                patch.object(review_page, "PlanReviewDialog", _ReviewCapture),
                patch.object(review_page, "_dialog_mount_context", return_value=nullcontext()),
            ):
                await review_page.plan_review_page(legacy.task_id)
            self.assertEqual(1, len(_ReviewCapture.mounted))
            self.assertEqual(fingerprint, legacy_store.fingerprint(_ReviewCapture.mounted[0][0]))
            self.assertEqual("approved", _ReviewCapture.mounted[0][1].get("workflow_status"))
            self.assertEqual(
                ReviewStatus.APPROVED,
                review_service.load(legacy.task_id).status,
            )
            reloaded = legacy_store.load(legacy.task_id)
            verification = PlanVerifier().verify(reloaded)
            after = {path: path.read_bytes() for path in authority_paths}
            self.assertEqual(
                before,
                after,
                "reopening/verifying an old approved authority must not migrate any authority",
            )
            self.assertIn(
                "provider_episode_ranges_missing_replan_required",
                {issue.code for issue in verification.issues},
                "same-season multi-subject v1 evidence must not guess an offset",
            )


class ProviderSubjectTransportAndBoundaryControls(unittest.TestCase):
    @staticmethod
    def _single_subject_resolution(search_result):
        subject = {
            "provider_id": "subject-a",
            "title": "Exact Anime",
            "original_title": "Exact Anime",
            "air_date": "2020-01-01",
            "episode_count": 2,
            "platform": "TV",
            "type": 2,
        }
        renamer = object.__new__(Rename)
        renamer.active_provider_selection = {"snapshot_at": "2026-08-24T00:00:00+00:00"}
        renamer.search = SimpleNamespace(get_tv_info=Mock(return_value=search_result))
        batch = collect_subject_observations(
            selected_subject_id="subject-a",
            bangumi_subjects=(subject,),
            bangumi_relations=(),
            search_subject=Rename._observe_tmdb_tv_subject.__get__(renamer, Rename),
            max_subjects=1,
        )
        resolution = resolve_provider_link(
            selected_provider={
                "namespace": "bangumi", "entity_type": "subject",
                "provider_id": "subject-a",
            },
            bangumi_subjects=(subject,),
            bangumi_relations=(),
            subject_observations=batch,
        )
        return batch, resolution

    def test_real_search_adapter_does_not_upgrade_approximate_tie_or_live_action(self):
        exact = _tmdb_tv(
            7001, "Exact Anime", "Exact Anime", "2020-01-01",
            ((1, "2020-01-01", 2),),
        )
        batch, resolution = self._single_subject_resolution(("Exact Anime", exact))
        self.assertEqual("reviewable", resolution.review_disposition)
        candidate = batch["subject_observations"][0]["queries"][0]["candidates"][0]
        self.assertEqual("exact", candidate["match_tier"])

        missing_year = {**exact, "first_air_date": "", "seasons": ()}
        _batch, resolution = self._single_subject_resolution(
            ("Exact Anime", missing_year)
        )
        self.assertEqual("manual_required", resolution.review_disposition)
        self.assertIn("subject_candidate_confidence_insufficient", resolution.reason_codes)

        season_year = {
            **missing_year,
            "seasons": ({
                "season_number": 1, "air_date": "2020-01-01",
                "episode_count": 2,
            },),
        }
        _batch, resolution = self._single_subject_resolution(
            ("Exact Anime", season_year)
        )
        self.assertEqual("reviewable", resolution.review_disposition)

        truncated = {**exact, "_bar_search_truncated": True}
        _batch, resolution = self._single_subject_resolution(
            ("Exact Anime", truncated)
        )
        self.assertEqual("manual_required", resolution.review_disposition)
        self.assertIn("subject_query_truncated", resolution.reason_codes)

        approximate = {**exact, "name": "Exact Anime Live", "original_name": "Other"}
        _batch, resolution = self._single_subject_resolution(
            ("Exact Anime Live", approximate)
        )
        self.assertEqual("manual_required", resolution.review_disposition)
        self.assertIn("subject_candidate_confidence_insufficient", resolution.reason_codes)

        tied = dict(exact)
        tied["_bar_search_alternatives"] = [
            {"id": 7001, "name": "Exact Anime", "first_air_date": "2020-01-01"},
            {
                "id": 7002, "name": "Exact Anime",
                "first_air_date": "2020-01-01", "genre_ids": [16],
            },
        ]
        _batch, resolution = self._single_subject_resolution(("Exact Anime", tied))
        self.assertEqual("blocked", resolution.review_disposition)
        self.assertIn("subject_candidate_tie", resolution.reason_codes)

        live_action = {
            **exact,
            "genres": [{"name": "Drama"}],
        }
        _batch, resolution = self._single_subject_resolution(
            ("Exact Anime", live_action)
        )
        self.assertEqual("manual_required", resolution.review_disposition)
        self.assertIn("subject_candidate_confidence_insufficient", resolution.reason_codes)

    def test_boundary_actions_bind_one_canonical_edge_and_hide_ambiguous_middle(self):
        subjects = tuple({
            "provider_id": subject_id,
            "title": title,
            "original_title": title,
            "air_date": "2020-01-01",
            "episode_count": 2,
            "platform": "TV",
            "type": 2,
        } for subject_id, title in (
            ("left", "左侧作品"), ("middle", "中间作品"), ("right", "右侧作品"),
        ))
        relations = (
            {"from": "left", "to": "middle", "relation": "sequel"},
            {"from": "middle", "to": "right", "relation": "sequel"},
        )

        def observe(subject):
            subject_id = str(subject["provider_id"])
            candidates = ()
            outcome = "no_match"
            if subject_id == "middle":
                outcome = "ok"
                candidates = ({
                    "namespace": "tmdb", "entity_type": "tv",
                    "provider_id": "8001", "tmdb_tv_id": "8001",
                    "match_tier": "exact", "title": "中间作品",
                    "first_air_date": "2020-01-01",
                    "seasons": ({
                        "season_number": 1, "air_date": "2020-01-01",
                        "episode_count": 2,
                    },),
                },)
            return {
                "bangumi_subject_id": subject_id,
                "observed_at": "2026-08-24T00:00:00+00:00",
                "policy_version": "subject-candidate-v1",
                "queries": ({
                    "locale": "original", "query": str(subject["title"]),
                    "year": 2020, "outcome": outcome, "truncated": False,
                    "candidates": candidates,
                },),
            }

        batch = collect_subject_observations(
            selected_subject_id="middle",
            bangumi_subjects=subjects,
            bangumi_relations=relations,
            search_subject=observe,
            max_subjects=3,
        )
        forward = freeze_provider_subject_boundary_edges(
            selected_subject_id="middle", subject_observations=batch,
            bangumi_subjects=subjects, bangumi_relations=relations,
        )
        reverse = freeze_provider_subject_boundary_edges(
            selected_subject_id="middle", subject_observations=batch,
            bangumi_subjects=tuple(reversed(subjects)),
            bangumi_relations=tuple(reversed(relations)),
        )
        self.assertEqual(forward, reverse)
        self.assertEqual(2, len(forward))

        dialog = object.__new__(PlanReviewDialog)
        operation = {
            "state": "failed",
            "failure_code": "relation_unresolved",
            "reason_codes": ["subject_candidate_evidence_missing"],
            "expected_decision_revision": 3,
            "evidence_fingerprint": batch["evidence_fingerprint"],
            "provider_subject_evidence": {
                "subject_observations": batch,
                "provider_subject_boundary_edges": [dict(item) for item in forward],
            },
        }
        dialog.task_data = {"selection_operation": operation}
        dialog._show_provider_candidates = Mock()
        dialog._close_review = Mock()
        dialog._remove_provider_failure_task = Mock()
        dialog._apply_provider_subject_boundary_inline = Mock()
        captured = ui_fixture._CaptureUi()
        captured.card = captured.column
        with patch.object(review_page, "ui", captured):
            PlanReviewDialog._render_selection_operation(dialog)
        self.assertNotIn("确认与相邻作品一起整理", captured.callbacks)
        self.assertNotIn("分开整理", captured.callbacks)
        self.assertIn("换一个作品", captured.callbacks)
        self.assertIn("移除任务", captured.callbacks)

        single = (forward[0],)
        operation["provider_subject_evidence"]["provider_subject_boundary_edges"] = [
            dict(single[0])
        ]
        captured = ui_fixture._CaptureUi()
        captured.card = captured.column
        with patch.object(review_page, "ui", captured):
            PlanReviewDialog._render_selection_operation(dialog)
        action = captured.callbacks["确认与相邻作品一起整理"]
        action()
        dialog._apply_provider_subject_boundary_inline.assert_called_once_with(
            True, 3, batch["evidence_fingerprint"], dict(single[0])
        )

        malformed_relations = (
            {"from": "middle", "to": "right", "relation": ""},
            {"from": "left", "to": "middle", "relation": "side_story"},
        )
        self.assertEqual((), freeze_provider_subject_boundary_edges(
            selected_subject_id="middle", subject_observations=batch,
            bangumi_subjects=subjects, bangumi_relations=malformed_relations,
        ))
        truncated_batch = json.loads(json.dumps(batch))
        truncated_selected = next(
            row for row in truncated_batch["subject_observations"]
            if row["bangumi_subject_id"] == "middle"
        )
        truncated_selected["queries"][0]["truncated"] = True
        self.assertEqual((), freeze_provider_subject_boundary_edges(
            selected_subject_id="middle", subject_observations=truncated_batch,
            bangumi_subjects=subjects, bangumi_relations=relations,
        ))
        mismatched_batch = json.loads(json.dumps(batch))
        mismatched_selected = next(
            row for row in mismatched_batch["subject_observations"]
            if row["bangumi_subject_id"] == "middle"
        )
        mismatched_selected["queries"][0]["candidates"][0][
            "provider_id"
        ] = "foreign-id"
        self.assertEqual((), freeze_provider_subject_boundary_edges(
            selected_subject_id="middle", subject_observations=mismatched_batch,
            bangumi_subjects=subjects, bangumi_relations=relations,
        ))

        two_subjects = tuple(
            subject for subject in subjects
            if str(subject["provider_id"]) in {"middle", "right"}
        )

        def observe_two_seasons(subject):
            row = observe(subject)
            if str(subject["provider_id"]) == "middle":
                candidate = dict(row["queries"][0]["candidates"][0])
                candidate["seasons"] = (
                    {"season_number": 1, "air_date": "2020-01-01", "episode_count": 2},
                    {"season_number": 2, "air_date": "2021-01-01", "episode_count": 2},
                )
                row = dict(row)
                query = dict(row["queries"][0])
                query["candidates"] = (candidate,)
                row["queries"] = (query,)
            return row

        prequel_relations = ({
            "from": "right", "to": "middle", "relation": "prequel",
        },)
        prequel_batch = collect_subject_observations(
            selected_subject_id="middle",
            bangumi_subjects=two_subjects,
            bangumi_relations=prequel_relations,
            search_subject=observe_two_seasons,
            max_subjects=2,
        )
        prequel_edges = freeze_provider_subject_boundary_edges(
            selected_subject_id="middle", subject_observations=prequel_batch,
            bangumi_subjects=two_subjects, bangumi_relations=prequel_relations,
        )
        self.assertEqual(("middle", "right", "sequel"), (
            prequel_edges[0]["from_subject_id"],
            prequel_edges[0]["to_subject_id"],
            prequel_edges[0]["relation"],
        ))
        prequel_failure = {
            "selected_subject_id": "middle",
            "subject_observations": prequel_batch,
            "bangumi_subjects": two_subjects,
            "bangumi_relations": prequel_relations,
            "evidence_fingerprint": prequel_batch["evidence_fingerprint"],
            "provider_subject_boundary_edges": prequel_edges,
        }
        prequel_resolution, prequel_decision = (
            resolve_provider_subject_boundary_decision(
                prequel_failure, same_work=True,
                boundary_edge=prequel_edges[0],
            )
        )
        self.assertEqual("reviewable", prequel_resolution.review_disposition)
        self.assertIsInstance(prequel_decision, Mapping)
        self.assertEqual(2, prequel_decision["schema_version"])
        self.assertEqual(("middle", "right"), tuple(
            prequel_decision["cohort_subject_ids"]
        ))
        self.assertEqual(1, len(prequel_decision["cohort_edges"]))
        self.assertTrue(str(prequel_decision["cohort_decision_id"]).startswith(
            "cohort-"
        ))
        self.assertEqual({
            "from_subject_id": "middle",
            "to_subject_id": "right",
            "relation": "sequel",
            "decision": "same_work",
            "evidence_fingerprint": prequel_batch["evidence_fingerprint"],
        }, {
            key: prequel_decision[key]
            for key in (
                "from_subject_id", "to_subject_id", "relation", "decision",
                "evidence_fingerprint",
            )
        })
        separate_batch = collect_subject_observations(
            selected_subject_id="middle",
            bangumi_subjects=two_subjects,
            bangumi_relations=prequel_relations,
            search_subject=observe,
            max_subjects=2,
        )
        separate_edges = freeze_provider_subject_boundary_edges(
            selected_subject_id="middle", subject_observations=separate_batch,
            bangumi_subjects=two_subjects, bangumi_relations=prequel_relations,
        )
        separate_failure = {
            "selected_subject_id": "middle",
            "subject_observations": separate_batch,
            "bangumi_subjects": two_subjects,
            "bangumi_relations": prequel_relations,
            "evidence_fingerprint": separate_batch["evidence_fingerprint"],
            "provider_subject_boundary_edges": separate_edges,
        }
        separate_resolution, separate_decision = (
            resolve_provider_subject_boundary_decision(
                separate_failure, same_work=False,
                boundary_edge=separate_edges[0],
            )
        )
        self.assertIsNone(separate_decision)
        self.assertEqual("reviewable", separate_resolution.review_disposition)
        self.assertEqual(({
            "from_subject_id": "middle",
            "to_subject_id": "right",
            "relation": "sequel",
            "boundary_kind": "explicit_user_separate",
            "outside_tmdb_tv_id": "",
            "evidence_fingerprint": separate_batch["evidence_fingerprint"],
        },), separate_resolution.boundaries)


class ProviderSubjectPlanCompatibilityControls(unittest.IsolatedAsyncioTestCase):
    async def test_existing_materialized_l0_control_still_uses_real_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Yama no Susume Next Summit S04E01.mkv"
            source.touch()
            source_stat = source.stat()
            source_before = (
                source_stat.st_size,
                source_stat.st_mtime_ns,
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            mapping = replace(rc36_fixture._materialized_yama_mapping(), revision=0)
            provider_root = root / "provider_links"
            series_root = root / "series_catalog"
            rc36_fixture.ProviderLinkCatalog(provider_root).save(
                mapping, expected_revision=-1
            )
            renamer = rc36_fixture._configured_renamer(root)
            canonical_root = renamer.ANIME_PATH / "向山进发 (2013)"
            existing = rc36_fixture.save_plan_fixture(
                rc36_fixture.PlanStore(root / "series-fixture-plans"),
                rc36_fixture.RenamePlan(
                    task_id="completed-yama-s1-fixture",
                    source_root=root / "completed-s1-source",
                    entries=(),
                    metadata={
                        "series_key": mapping.local_series_key,
                        "season_binding_id": "season-binding-l0-control",
                        "season_number": 1,
                        "season_source": "provider_crosswalk",
                        "canonical_work_root": str(canonical_root),
                    },
                    work_identity=rc36_fixture.WorkIdentity(
                        "confirmed", "animation", "向山进发", 2013,
                        rc36_fixture.ProviderIdentity(
                            "confirmed", "tmdb", "tv", "53918", "向山进发",
                            2013, "explicit_user",
                        ),
                        "provider_selection",
                    ),
                ),
            )
            rc36_fixture.SeriesCatalog(series_root).register_completed(
                existing,
                expected_revision=0,
            )
            before = {
                "provider": _byte_manifest(provider_root),
                "series": _byte_manifest(series_root),
            }
            tmdb_info = rc36_fixture._yama_tmdb_info()
            renamer.search.get_tv_info.return_value = (
                "Yama no Susume Next Summit",
                {**tmdb_info, "name": "Yama no Susume Next Summit"},
            )
            renamer.search.get_movie_info.return_value = ("", None)
            renamer.search.get_tv_info_by_id.return_value = ("向山进发", tmdb_info)
            renamer.bangumi_search = rc36_fixture._UnexpectedOnlineEvidence()
            case = rc36_fixture.Rc36RealEntrypointE2E(
                methodName="test_materialized_l0_uses_same_entrypoint_without_online_evidence"
            )
            with patch("src.rename.process.RECORD_PATH", root / "records"):
                plan, state, task, dialog = await case._run_selected_provider_flow(
                    root=root,
                    source=source,
                    task_id="real-yama-l0-byte-stable",
                    renamer=renamer,
                    candidate={
                        "namespace": "bangumi", "entity_type": "subject",
                        "provider_id": "290980", "title": "向山进发 Next Summit",
                    },
                    is_anime=True,
                    is_movie=False,
                )
            after = {
                "provider": _byte_manifest(provider_root),
                "series": _byte_manifest(series_root),
            }
            self.assertEqual(before, after, "Plan-only L0 reused Catalog must be byte-stable")
            source_stat = source.stat()
            self.assertEqual(
                source_before,
                (
                    source_stat.st_size,
                    source_stat.st_mtime_ns,
                    hashlib.sha256(source.read_bytes()).hexdigest(),
                ),
            )
            self.assertFalse(
                renamer.ANIME_PATH.exists() or renamer.ANIME_MOVIE_PATH.exists(),
                "Plan-only L0 control created a target root",
            )
            self.assertEqual("L0", plan.metadata["provider_link_snapshot"]["automation_level"])
            self.assertEqual(4, plan.metadata["season_number"])
            self.assertEqual("need_confirm", task["workflow_status"])
            self.assertEqual(
                (),
                tuple(
                    path
                    for business_root in (
                        root / "records", root / "ledger", root / "recovery",
                        root / "abandon_audit",
                    )
                    if business_root.exists()
                    for path in business_root.rglob("*.json")
                ),
                "Plan-only L0 control wrote record, execution, or recovery state",
            )
            self.assertEqual(
                plan.to_dict()["metadata"]["provider_link_snapshot"],
                state.to_dict()["provider_link_binding"],
            )
            dialog.close.assert_not_called()

    async def test_ordinary_tv_control_persists_no_segmented_provider_state(self):
        case = __import__(
            "tests.test_rc36_production_wiring_red_contract",
            fromlist=["ProductionWiringRedContracts"],
        ).Rc36RealEntrypointE2E(methodName="test_ordinary_tv_uses_same_add_select_worker_entrypoint")
        await case.test_ordinary_tv_uses_same_add_select_worker_entrypoint()

    async def test_ordinary_movie_control_persists_no_segmented_provider_state(self):
        case = __import__(
            "tests.test_rc36_production_wiring_red_contract",
            fromlist=["Rc36RealEntrypointE2E"],
        ).Rc36RealEntrypointE2E(methodName="test_ordinary_movie_uses_same_add_select_worker_entrypoint")
        await case.test_ordinary_movie_uses_same_add_select_worker_entrypoint()


if __name__ == "__main__":
    unittest.main()
