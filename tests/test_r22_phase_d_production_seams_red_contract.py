"""Incident RED contracts for the four Phase-D production seams.

The real Phase-D evidence remains private under ``local/development/evidence``.
This module recreates only sanitized semantic shapes and writes exclusively
below ``TemporaryDirectory``.  It never contacts a Provider, reads credentials,
or touches real media.

The production GREEN keeps every incident and disconnect shape as an ordinary
regression test; no expected-failure wrapper remains.
"""

from __future__ import annotations
from tests.queue_fixture import TaskQueueManager as ControlledQueue
from tests.source_collection_fixture import collected_identity

import ast
from copy import deepcopy
from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import src.pages.add_task_page as add_task_page
import src.pages.plan_review_page as review_page
from src.pages.add_task_page import AddTaskDialog
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.decision_state import DecisionStateStore
from src.rename.physical_identity import (
    canonical_location_key,
    canonical_physical_key,
)
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import PlanEntry, RenamePlan
from src.rename.task_batch_scope import (
    BatchTaskSourceRequest,
    SourceIdentity as BatchSourceIdentity,
    TaskBatchScopeManifestStore,
)
from src.rename.tv_planning_authority import (
    TVPlanningAuthorityBuilder,
    TVPlanningAuthorityError,
)
from src.task_queue import TaskQueueManager
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
)
import tests.test_provider_subject_plan_wiring_red_contract as phase_b_fixture
import tests.test_r22_fullset_p1_red_contract as fullset_fixture
import tests.test_r22_revised_c_c0_red_contract as c1_fixture
import tests.test_r22_tv_planning_authority_red_contract as authority_fixture
from tests.projection_test_support import reserve_active_batch_fixture


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _episode_plan(
    root: Path,
    *,
    episodes: range = range(1, 24),
    include_versions_and_subtitles: bool = False,
    font_suffix: str | None = None,
) -> tuple[RenamePlan, dict[int, set[str]], Path | None]:
    source_root = root / "source"
    target_root = root / "target" / "Season 01"
    entries: list[PlanEntry] = []
    expected: list[Path] = []
    by_episode: dict[int, set[str]] = {}
    for episode in episodes:
        video = _write(
            source_root / f"Example E{episode:02d}-1080p.mkv",
            f"episode-{episode}-1080p".encode("ascii"),
        )
        by_episode.setdefault(episode, set()).add(str(video.resolve()))
        entries.append(PlanEntry(
            source_path=video,
            target_path=target_root / f"Example S01E{episode:02d}-1080p.mkv",
            season_id=1,
            episode_id=episode,
            media_kind="video",
            role="main_episode",
        ))
        expected.append(video)
        if include_versions_and_subtitles and episode in {11, 12}:
            version = _write(
                source_root / f"Example E{episode:02d}-2160p.mkv",
                f"episode-{episode}-2160p".encode("ascii"),
            )
            subtitle = _write(
                source_root / f"Example E{episode:02d}-1080p.zh-Hans.ass",
                b"[Script Info]\nTitle: sanitized\n",
            )
            by_episode[episode].update({
                str(version.resolve()), str(subtitle.resolve())
            })
            entries.extend((
                PlanEntry(
                    source_path=version,
                    target_path=(
                        target_root / f"Example S01E{episode:02d}-2160p.mkv"
                    ),
                    season_id=1,
                    episode_id=episode,
                    media_kind="video",
                    role="main_episode",
                ),
                PlanEntry(
                    source_path=subtitle,
                    target_path=(
                        target_root / f"Example S01E{episode:02d}-1080p.zh-Hans.ass"
                    ),
                    season_id=1,
                    episode_id=episode,
                    media_kind="subtitle",
                    role="subtitle",
                ),
            ))
            expected.extend((version, subtitle))

    font_path = None
    if font_suffix is not None:
        font_path = _write(
            source_root / f"[Studio] Example [Fonts]{font_suffix}",
            b"sanitized-font-archive",
        )
        entries.append(PlanEntry(
            source_path=font_path,
            target_path=root / "inventory" / font_path.name,
            media_kind="associated",
            role="associated",
        ))
        expected.append(font_path)

    return RenamePlan(
        task_id="phase-d-production-seam",
        source_root=source_root,
        entries=tuple(entries),
        expected_sources=tuple(expected),
        metadata={},
    ), by_episode, font_path


def _provider_evidence(subject_ids: tuple[str, ...]) -> dict[str, object]:
    return {
        "subject_observations": {
            "subject_observations": [
                {
                    "bangumi_subject_id": subject_id,
                    "response_fingerprint": str(index + 1) * 64,
                    "queries": [],
                }
                for index, subject_id in enumerate(subject_ids)
            ]
        }
    }


def _build_production_authority(
    root: Path,
    plan: RenamePlan,
    *,
    ranges: tuple[dict[str, object], ...],
    selected_subject_id: str,
):
    subject_ids = tuple(str(row["bangumi_subject_id"]) for row in ranges)
    # The collector now consumes an explicit current routing draft, not a raw
    # inventory with no work root or series identity.
    from src.rename.rename_plan import PresentationGroup
    work_root = root / "target" / "示例作品 (2021)"
    plan = replace(plan, metadata={**dict(plan.metadata),
        "canonical_work_root": str(work_root), "series_key": "series-production-seam",
        "series_catalog_revision": 0, "season_number": 1},
        presentation_groups=(PresentationGroup(group_id="tv", kind="tv_episodes", display_name="示例", preferred_version_ref=None),))
    identities = []
    for source in sorted(plan.expected_sources, key=lambda item: str(item).casefold()):
        resolved = Path(source).resolve(strict=True)
        stat_result = resolved.stat()
        identities.append(BatchSourceIdentity(
            source_path=str(resolved),
            location_key=canonical_location_key(resolved),
            physical_key=canonical_physical_key(stat_result),
            size=int(stat_result.st_size),
            mtime_ns=int(stat_result.st_mtime_ns),
        ))
    reservation_store = TaskBatchScopeManifestStore(root / "task_batch_scope")
    manifest = reserve_active_batch_fixture(
        reservation_store,
        batch_id="phase-d-production-seam",
        task_requests=(BatchTaskSourceRequest(
            task_id=plan.task_id,
            input_scope=(str(plan.source_root),),
            scope_kind="parent_directory",
            source_identities=tuple(identities),
            media_domain="tv",
        ),),
    )
    scope = manifest.task_source_scopes[0]
    with patch.object(review_page, "DECISION_STATE_PATH", root / "decisions"):
        return review_page._build_cohort_tv_planning_authority(
            plan=plan,
            task_data={
                "batch_id": manifest.batch_id,
                "task_source_scope": scope.to_dict(),
            },
            evidence=_provider_evidence(subject_ids),
            preview_request={
                "derived_episode_ranges": [dict(row) for row in ranges],
                "resolution_fingerprint": "a" * 64,
            },
            request_fingerprint="b" * 64,
            expected_revision=1,
            selected_subject_id=selected_subject_id,
            tmdb_tv_id="100565",
            season_assignments={subject_id: 1 for subject_id in subject_ids},
            display_title="示例作品",
            title_source={
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": subject_ids[0],
            },
            series_year=2021,
        )


class RelationPreBuilderIncidentRedContracts(unittest.IsolatedAsyncioTestCase):
    async def test_a_real_s3_no_match_click_reaches_builder_via_unique_anchor(self):
        """The selected successor need not duplicate its anchor's exact hit."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = phase_b_fixture._make_episode_source(
                root / "source",
                "Yama no Susume Third Season",
                (1, 2),
                subtitles=True,
            )
            harness = phase_b_fixture.ProviderSubjectPlanWiringRedContracts(
                methodName="runTest"
            )
            result = await harness._run_real_add_select(
                root=root / "flow",
                source=source,
                subjects=phase_b_fixture.YAMA_SUBJECTS,
                relations=phase_b_fixture.YAMA_RELATIONS,
                selected_subject_id="216310",
                fail_subject=(
                    "第二季", "セカンドシーズン", "第三季",
                    "サードシーズン", "Next Summit",
                ),
                failure_mode="no_match",
            )
            revision, fingerprint, edge = c1_fixture._operation_arguments(
                result.task
            )
            dialog = c1_fixture._boundary_dialog(result)
            notices: list[tuple[str, object]] = []
            with c1_fixture._boundary_patches(result, notices):
                await c1_fixture._dispatch_boundary(dialog)

            task = c1_fixture._load_task(result)
            state = DecisionStateStore(result.decisions).load(result.task_id)
            plan = PlanStore(result.plans).load(result.task_id)
            operation = current_provider_selection_context(task)
            self.assertEqual("succeeded", operation.get("state"))
            self.assertEqual("need_confirm", task.get("workflow_status"))
            self.assertIsNotNone(state.tv_planning_authority)
            self.assertEqual(
                3, state.tv_planning_authority.selected_season_number
            )
            self.assertEqual(
                "216310",
                state.tv_planning_authority.work_identity[
                    "primary_provider"
                ]["provider_id"],
            )
            self.assertIn(
                ("tmdb", "tv", "53918"),
                {
                    (
                        str(item.get("namespace") or ""),
                        str(item.get("entity_type") or ""),
                        str(item.get("provider_id") or ""),
                    )
                    for item in (
                        state.tv_planning_authority.metadata_provider_identities
                    )
                },
            )
            self.assertEqual(result.task_id, plan.task_id)

    def test_a_public_boundary_no_longer_requires_selected_exact_candidate(self):
        from src.rename import tv_boundary_completion
        self.assertFalse(hasattr(PlanReviewDialog, '_apply_provider_subject_boundary_inline'))
        source = inspect.getsource(tv_boundary_completion.resolve)
        self.assertNotIn("匹配到的电视剧资料不完整", source)
        self.assertNotRegex(
            source,
            r"selected_observation\s*=.*?tmdb_candidate\s*=.*?if not isinstance\(tmdb_candidate",
        )

    def test_a_incomplete_or_truncated_component_has_no_action(self):
        case = fullset_fixture._case("216310")
        batch = fullset_fixture._batch(case)
        batch["truncated"] = True
        from src.rename.provider_link_decision import (
            freeze_provider_subject_boundary_edges,
        )

        self.assertEqual((), freeze_provider_subject_boundary_edges(
            selected_subject_id="216310",
            subject_observations=batch,
            bangumi_subjects=case["subjects"],
            bangumi_relations=case["relations"],
        ))

    def test_a_pure_builder_disconnect_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, _episodes, _font = _episode_plan(root, episodes=range(1, 3))
            ranges = ({
                "bangumi_subject_id": "41658",
                "season_number": 1,
                "episode_start": 1,
                "episode_end": 2,
            },)
            with patch.object(
                TVPlanningAuthorityBuilder,
                "build",
                side_effect=TVPlanningAuthorityError("builder disconnected"),
            ):
                with self.assertRaisesRegex(
                    TVPlanningAuthorityError, "builder disconnected"
                ):
                    _build_production_authority(
                        root, plan, ranges=ranges,
                        selected_subject_id="41658",
                    )


class SourceSegmentBindingIncidentRedContracts(unittest.TestCase):
    def test_b_real_assembler_binds_each_source_to_its_exact_86_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, by_episode, _font = _episode_plan(
                root,
                include_versions_and_subtitles=True,
            )
            ranges = (
                {
                    "bangumi_subject_id": "302189",
                    "season_number": 1,
                    "episode_start": 1,
                    "episode_end": 11,
                },
                {
                    "bangumi_subject_id": "331887",
                    "season_number": 1,
                    "episode_start": 12,
                    "episode_end": 23,
                },
            )
            value = _build_production_authority(
                root,
                plan,
                ranges=ranges,
                selected_subject_id="302189",
            ).to_dict()
            actual = {
                row["bangumi_subject_id"]: set(row["sources"])
                for row in value["episode_range_ownership"]
            }
            expected_first = set().union(
                *(by_episode[number] for number in range(1, 12))
            )
            expected_second = set().union(
                *(by_episode[number] for number in range(12, 24))
            )
            self.assertEqual(expected_first, actual["302189"])
            self.assertEqual(expected_second, actual["331887"])
            self.assertTrue(any("2160p" in path for path in expected_first))
            self.assertTrue(any(path.endswith(".ass") for path in expected_second))

    def test_b_production_assembler_has_no_selected_only_cartesian_shortcut(self):
        source = inspect.getsource(review_page._build_cohort_tv_planning_authority)
        self.assertNotIn(
            "regular_sources if subject_id == selected_subject_id else []",
            source,
        )
        self.assertNotRegex(
            source,
            r"sources['\"]:\s*regular_sources\s+if\s+subject_id",
        )

    def test_b_builder_preserves_already_frozen_per_file_segments(self):
        case = deepcopy(authority_fixture._fixture()["eighty_six"])
        module, _batch, typed = authority_fixture._typed_input(self, case)
        value = authority_fixture._authority_dict(
            module.TVPlanningAuthorityBuilder().build(typed)
        )
        self.assertEqual(
            case["source_owned_segments"],
            [dict(row) for row in value["episode_range_ownership"]],
        )


class RealAddTaskBatchAdmissionIncidentRedContracts(
    unittest.IsolatedAsyncioTestCase
):
    async def test_c_public_submit_reserves_all_inputs_and_excludes_exact_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "Steins Gate 0"
            child = _write(parent / "23beta.mkv", b"official-special")
            _write(parent / "Episode 01.mkv", b"parent-episode")
            paths: list[Path] = [parent, child]
            for index in range(14):
                paths.append(_write(
                    root / f"input-{index:02d}" / f"Episode {index + 1:02d}.mkv",
                    f"input-{index}".encode("ascii"),
                ))

            manager = ControlledQueue(
                task_root=root / "tasks",
                decision_state_root=root / "decisions",
                recovery_abandon_audit_root=root / "abandon",
                plan_root=root / "plans",
                batch_scope_root=root / "batch_scope",
                test_mode=True,
            )
            submit = SimpleNamespace(
                path=str(paths[0]),
                input_paths=tuple(str(path) for path in paths),
                paths=tuple(str(path) for path in paths),
                selected_paths=tuple(str(path) for path in paths),
                media_type="anime",
                _submitting=False,
                submit_button=MagicMock(),
                close=MagicMock(),
            )
            notices: list[str] = []
            try:
                with (patch.object(add_task_page, "task_queue", manager),
patch.object(
                        add_task_page,
                        "notify",
                        side_effect=lambda message, **_kwargs: notices.append(
                            str(message)
                        ),
                    )):
                    await AddTaskDialog._submit(submit)
            finally:
                await manager.stop()

            records = [
                json.loads(path.read_text("utf-8"))
                for path in sorted((root / "tasks").glob("*.json"))
            ]
            self.assertEqual(16, len(records))
            batch_ids = {str(row.get("batch_id") or "") for row in records}
            self.assertEqual(1, len(batch_ids))
            self.assertNotIn("", batch_ids)
            self.assertEqual(
                {"active"},
                {
                    json.loads(path.read_text("utf-8"))["state"]
                    for path in (root / "batch_scope").rglob("*.json")
                },
            )

            by_path = {str(row.get("path") or ""): row for row in records}
            parent_scope = by_path[str(parent)]["task_source_scope"]
            child_scope = by_path[str(child)]["task_source_scope"]
            parent_keys = {
                (row["location_key"], row["physical_key"])
                for row in parent_scope["source_identities"]
            }
            child_keys = {
                (row["location_key"], row["physical_key"])
                for row in child_scope["source_identities"]
            }
            self.assertEqual(1, len(child_keys))
            self.assertTrue(parent_keys.isdisjoint(child_keys))
            self.assertEqual(1, submit.close.call_count)
            self.assertTrue(notices)

    def test_c_add_task_is_a_real_enqueue_batch_caller_not_legacy_enqueue(self):
        tree = ast.parse(inspect.getsource(add_task_page))
        submit = next(
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_submit"
        )
        called = {
            node.func.attr
            for node in ast.walk(submit)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("enqueue_batch", called)
        self.assertNotIn("enqueue", called)

    def test_c_worker_contract_carries_frozen_task_scope_into_scanning(self):
        from src.task_queue import TaskJob
        import src.rename.process as process_module

        job_fields = set(inspect.signature(TaskJob).parameters)
        process_source = inspect.getsource(process_module.Rename._process)
        worker_source = inspect.getsource(TaskQueueManager._worker)
        self.assertIn("task_source_scope", job_fields)
        self.assertIn("task_source_scope", worker_source)
        self.assertIn("task_source_scope", process_source)

    def test_c_batch_store_specificity_excludes_parent_duplicate(self):
        api = authority_fixture._batch_api(self)
        case = authority_fixture._fixture()["steins_source_overlap"]
        shared = {
            "source_path": case["child_scope"][0],
            "location_key": case["shared_location_key"],
            "physical_key": case["shared_physical_key"],
            "size": 23,
            "mtime_ns": 2300,
        }
        parent = api.BatchTaskSourceRequest.from_mapping({
            "task_id": case["parent_task_id"],
            "input_scope": case["parent_scope"],
            "scope_kind": "parent_directory",
            "source_identities": [shared],
        })
        child = api.BatchTaskSourceRequest.from_mapping({
            "task_id": case["child_task_id"],
            "input_scope": case["child_scope"],
            "scope_kind": "exact_file",
            "source_identities": [shared],
        })
        with tempfile.TemporaryDirectory() as directory:
            manifest = api.TaskBatchScopeManifestStore(Path(directory)).reserve_batch(
                batch_id=case["batch_id"], task_requests=(parent, child)
            )
        owners = {
            row.source_identity.physical_key: row.owner_task_id
            for row in manifest.reservations
        }
        self.assertEqual(
            case["child_task_id"], owners[case["shared_physical_key"]]
        )

    def test_c_queue_admission_barrier_precedes_first_worker_visibility(self):
        source = inspect.getsource(TaskQueueManager.enqueue_batch)
        self.assertLess(source.index("reserve_batch"), source.index("_queue.put"))
        self.assertIn('manifest.state != "active"', source)

    def test_c_parent_scan_consumes_scope_after_exact_child_wins(self):
        from src.rename.process import _plan_expected_sources

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "Steins Gate 0"
            child = _write(parent / "23beta.mkv", b"official-special")
            episode = _write(parent / "Episode 01.mkv", b"parent-episode")
            manager = ControlledQueue(
                task_root=root / "tasks",
                decision_state_root=root / "decisions",
                recovery_abandon_audit_root=root / "abandon",
                plan_root=root / "plans",
                batch_scope_root=root / "batch_scope",
                test_mode=True,
            )
            shared = collected_identity(child)
            parent_request = review_page.BatchTaskSourceRequest(
                task_id="parent-task",
                input_scope=(str(parent),),
                scope_kind="parent_directory",
                source_identities=(
                    collected_identity(episode), shared
                ),
                media_domain="tv",
            )
            child_request = review_page.BatchTaskSourceRequest(
                task_id="child-task",
                input_scope=(str(child),),
                scope_kind="exact_file",
                source_identities=(shared,),
                media_domain="tv",
            )
            manifest = manager.batch_scope_store.reserve_batch(
                batch_id="scope-consumption",
                task_requests=(parent_request, child_request),
            )
            parent_scope = next(
                item for item in manifest.task_source_scopes
                if item.task_id == "parent-task"
            )
            expected = _plan_expected_sources(
                parent,
                {
                    episode: root / "target" / "S01E01.mkv",
                    child: root / "target" / "S00E05.mkv",
                },
                parent_scope,
            )
            self.assertEqual((episode,), expected)




class RedWrapperShapeControls(unittest.TestCase):
    def test_green_has_no_strict_switch_or_skip_wrapper(self):
        source = Path(__file__).read_text("utf-8")
        tree = ast.parse(source)
        env_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and node.func.attr == "getenv"
        ]
        self.assertEqual(0, len(env_calls))
        skipped_decorators = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "unittest"
            and node.attr in {"skip", "skipIf", "skipUnless"}
        ]
        skip_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name) and node.func.id == "skipTest"
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "skipTest"
            )
        ]
        self.assertEqual([], skipped_decorators)
        self.assertEqual([], skip_calls)


if __name__ == "__main__":
    unittest.main()
