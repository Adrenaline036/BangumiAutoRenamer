"""Regression contracts for canonical movie subtitle labels and ``Extras``.

The movie regression deliberately keeps the real task queue, provider
selection, worker, review-page completion button, DecisionState projection and
persisted Plan in the call path.  TV routing uses both production projections
that currently decide the extras directory.  This module stops at persisted
Plan authority; real approval and transfer are covered by the movie-font E2E.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import src.pages.plan_review_page as review_page
import src.utils.utils as utils_module
import tests.test_isolated_sample_acceptance as isolated_fixture
import tests.test_movie_font_work_catalog_red_contract as movie_fixture
import tests.test_r1_real_media_production_red_contract as production_fixture
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.decision_state import DecisionCommandService, DecisionStateStore
from src.rename.plan_review import PlanReviewService
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.series_catalog import SeriesCatalog
from src.rename.subtitle_workflow import format_subtitle_target_fields
from src.rename.target_routing import TargetRootSnapshot


class SubtitleExtrasCanonicalRedContracts(unittest.IsolatedAsyncioTestCase):
    async def _complete_real_movie(self, root: Path, task_id: str):
        case = movie_fixture.MovieFontProductionRedContracts(methodName="runTest")
        source, primary, expected_sources = movie_fixture._make_omoide(
            root / "sources"
        )
        initial = await case._run_provider_flow(root / "flow", source, task_id)
        completed = case._complete_movie(
            root=root / "flow",
            result=initial,
            primary=primary,
        )
        return case, source, primary, expected_sources, initial, completed

    async def test_real_movie_ui_flow_persists_canonical_zh_hans_plan(self):
        """The real movie entry point must persist the canonical subtitle target."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (
                case,
                _source,
                primary,
                expected_sources,
                initial,
                completed,
            ) = await self._complete_real_movie(root, "movie-subtitle-canonical")
            plan = completed.store.load("movie-subtitle-canonical")
            by_source = {entry.source_path: entry for entry in plan.entries}
            main_entry = by_source[primary]
            subtitle_entry = next(
                entry
                for entry in plan.entries
                if entry.source_path.name.lower().endswith(".sc.ass")
            )
            subtitle = subtitle_entry.subtitle
            if subtitle is None:
                raise AssertionError("production movie subtitle lost its structured contract")
            expected_target = format_subtitle_target_fields(
                target_video=main_entry.target_path,
                source_subtitle=subtitle_entry.source_path,
                primary_language=subtitle.primary_language or "",
                script=subtitle.script,
                track_title_tokens=subtitle.track_title_tokens,
                flags=subtitle.flags,
            )
            legacy_target = main_entry.target_path.with_name(
                f"{main_entry.target_path.stem}.sc.ass"
            )
            extra_entries = tuple(
                entry for entry in plan.entries if "SPs" in entry.source_path.parts
            )
            verification = PlanVerifier().verify(plan)

            problems = []
            if initial.initial_plan.metadata.get("mapping_source") != "inventory_review_draft":
                problems.append("initial worker did not persist the inventory review draft")
            if initial.task.get("selection_operation", {}).get(
                "previous_workflow_status"
            ) != "invalid":
                problems.append("provider selection did not follow the safe-invalid draft")
            if len(plan.entries) != 7 or set(plan.expected_sources) != set(expected_sources):
                problems.append("production movie flow did not preserve the exact seven-file Plan")
            if (
                subtitle.raw_label,
                subtitle.primary_language,
                subtitle.script,
            ) != ("sc", "zh", "Hans"):
                problems.append("structured subtitle authority is not the resolved sc -> zh/Hans contract")
            if subtitle_entry.target_path != expected_target:
                problems.append(
                    f"movie subtitle target is not canonical: {subtitle_entry.target_path}"
                )
            if not subtitle_entry.target_path.name.endswith(".zh-Hans.ass"):
                problems.append("movie subtitle target did not use the canonical language tag")
            if "subtitle_target_not_canonical" in {
                issue.code for issue in verification.issues
            }:
                problems.append("the canonical production Plan was rejected by PlanVerifier")
            if len(extra_entries) != 3 or any(
                entry.target_path.parent.name != "Extras" for entry in extra_entries
            ):
                problems.append("movie extras did not retain the exact canonical Extras directory")
            persisted = completed.store.load(plan.task_id)
            if persisted != plan:
                problems.append("movie smart-completion Plan was not persisted exactly")
            if completed.state.revision <= initial.state.revision:
                problems.append("movie UI callback did not advance DecisionState")
            if plan.metadata.get("decision_state_revision") != completed.state.revision:
                problems.append("persisted Plan does not reference the current DecisionState revision")
            if plan.metadata.get("decision_state_fingerprint") != DecisionStateStore.fingerprint(
                completed.state
            ):
                problems.append("persisted Plan does not reference the current DecisionState")
            if expected_target.exists() or legacy_target.exists():
                problems.append("planning created a subtitle target before approval")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_smart_completion_clears_terminal_timestamp_from_review_task(self):
        """A new review Plan must not retain the earlier invalid-run timestamp."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (
                _case,
                _source,
                _primary,
                _expected_sources,
                initial,
                completed,
            ) = await self._complete_real_movie(root, "movie-clear-completed-at")

            self.assertEqual("invalid", initial.task.get("workflow_status"))
            self.assertIsNotNone(
                initial.task.get("completed_at"),
                "fixture must preserve the real invalid-worker counterexample",
            )
            self.assertEqual("need_confirm", completed.task.get("workflow_status"))
            self.assertIsNone(
                completed.task.get("completed_at"),
                "a non-terminal review task must not retain a terminal timestamp",
            )

    async def test_verifier_rejects_movie_raw_alias_target_against_zh_hans_contract(self):
        """A season-null movie must not bypass the shared subtitle formatter."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (
                _case,
                _source,
                primary,
                _expected_sources,
                _initial,
                completed,
            ) = await self._complete_real_movie(root, "movie-subtitle-verifier")
            plan = completed.store.load("movie-subtitle-verifier")
            main_entry = next(
                entry for entry in plan.entries if entry.source_path == primary
            )
            subtitle_entry = next(
                entry
                for entry in plan.entries
                if entry.source_path.name.lower().endswith(".sc.ass")
            )
            subtitle = subtitle_entry.subtitle
            if subtitle is None:
                raise AssertionError("production movie subtitle lost its structured contract")
            canonical_target = format_subtitle_target_fields(
                target_video=main_entry.target_path,
                source_subtitle=subtitle_entry.source_path,
                primary_language=subtitle.primary_language or "",
                script=subtitle.script,
                track_title_tokens=subtitle.track_title_tokens,
                flags=subtitle.flags,
            )
            canonical_entry = replace(subtitle_entry, target_path=canonical_target)
            canonical_plan = replace(
                plan,
                entries=tuple(
                    canonical_entry
                    if entry.source_path == subtitle_entry.source_path
                    else entry
                    for entry in plan.entries
                ),
            )
            canonical_codes = {
                issue.code for issue in PlanVerifier().verify(canonical_plan).issues
            }
            split_target = main_entry.target_path.with_name(
                f"{main_entry.target_path.stem}.sc.ass"
            )
            split_entry = replace(canonical_entry, target_path=split_target)
            split_plan = replace(
                canonical_plan,
                entries=tuple(
                    split_entry if entry.source_path == canonical_entry.source_path else entry
                    for entry in canonical_plan.entries
                ),
            )

            canonical_payload = canonical_entry.to_dict()
            split_payload = split_entry.to_dict()
            canonical_payload.pop("target_path")
            split_payload.pop("target_path")
            split_codes = {issue.code for issue in PlanVerifier().verify(split_plan).issues}
            self.assertNotEqual(canonical_entry.target_path, split_entry.target_path)
            self.assertEqual(canonical_payload, split_payload)
            self.assertNotIn("subtitle_target_not_canonical", canonical_codes)
            self.assertEqual(
                {
                    "subtitle_target_not_canonical",
                    "projection_receipt_mismatch",
                },
                split_codes - canonical_codes,
            )

    async def test_traditional_tv_worker_persists_exact_capitalized_extras(self):
        """The rules/traditional route must emit ``Season NN/Extras``."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = production_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Second Season",
            )
            extra_source = source / "SPs" / (
                "[VCB-Studio] Yama no Susume Second Season [NCOP]"
                "[Ma10p_1080p][x265_flac].mkv"
            )
            extra_source.parent.mkdir(parents=True)
            extra_source.write_bytes(b"traditional-extra")
            harness = SimpleNamespace(
                _NoopReviewDialog=(
                    production_fixture.R1RealQueueWorkerContracts._NoopReviewDialog
                )
            )
            result = await production_fixture.R1RealQueueWorkerContracts._run_flow(
                harness,
                root=root / "flow",
                source=source,
                task_id="traditional-tv-extras-canonical",
                candidate=production_fixture._candidate("86670"),
            )
            persisted = PlanStore(root / "flow" / "plans").load(result.plan.task_id)
            extra = next(
                entry for entry in persisted.entries if entry.source_path == extra_source
            )
            canonical_root = Path(str(persisted.metadata["canonical_work_root"]))

        self.assertEqual("rules", persisted.metadata.get("mapping_source"))
        self.assertEqual(canonical_root / "Season 02", extra.target_path.parent.parent)
        self.assertEqual("Extras", extra.target_path.parent.name)

    async def test_smart_tv_button_persists_exact_capitalized_extras(self):
        """The review smart-completion projection must use the same exact name."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            case = movie_fixture.MovieFontProductionRedContracts(methodName="runTest")
            source, _primary, _expected = movie_fixture._make_omoide(root / "sources")
            initial = await case._run_provider_flow(
                root / "flow", source, "smart-tv-extras-canonical"
            )
            flow = root / "flow"
            store = PlanStore(flow / "plans")
            review = PlanReviewService(store, flow / "reviews", flow / "work_catalog")
            state_store = DecisionStateStore(flow / "decisions")
            dialog = SimpleNamespace(
                plan=initial.plan,
                task_decision=initial.state,
                task_data=dict(initial.task),
                review_service=review,
                decision_command_service=DecisionCommandService(state_store, store),
                series_catalog=SeriesCatalog(flow / "series_catalog"),
                selected_ids=set(),
                selection_label=SimpleNamespace(text=""),
                review_badge=SimpleNamespace(text=""),
                presentation_group=SimpleNamespace(options={}, update=MagicMock()),
                _video_options=MagicMock(return_value={}),
                _selected_movie_primary_sources=MagicMock(return_value=[]),
                _render_movie_primary_picker=MagicMock(),
                _complete_movie_with_primary=MagicMock(),
                _toggle_movie_segments=MagicMock(),
                _render_groups=MagicMock(),
                _render_catalog_context=MagicMock(),
                _render_completion_workflow=MagicMock(),
                _render_issues=MagicMock(),
                _refresh_plan_header=MagicMock(),
                _render_entries=MagicMock(),
                _refresh_execute_button=MagicMock(),
            )
            dialog._completion_domain = PlanReviewDialog._completion_domain.__get__(dialog)
            dialog._inventory_draft_for_completion = (
                PlanReviewDialog._inventory_draft_for_completion.__get__(dialog)
            )
            dialog._series_binding_for_tv = (
                PlanReviewDialog._series_binding_for_tv.__get__(dialog)
            )
            dialog._complete_inventory_draft = (
                PlanReviewDialog._complete_inventory_draft.__get__(dialog)
            )
            dialog._adopt_revision = PlanReviewDialog._adopt_revision.__get__(dialog)

            class _Node:
                def __init__(self, *, value=None):
                    self.value = value

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def classes(self, *_args):
                    return self

                def props(self, *_args):
                    return self

                def tooltip(self, *_args):
                    return self

            class _Ui:
                def __init__(self):
                    self.callbacks = {}

                @staticmethod
                def column():
                    return _Node()

                @staticmethod
                def row():
                    return _Node()

                @staticmethod
                def label(_value):
                    return _Node()

                @staticmethod
                def number(*_args, **kwargs):
                    return _Node(value=kwargs.get("value"))

                @staticmethod
                def checkbox(*_args, **_kwargs):
                    return _Node()

                def button(self, label, *, on_click=None, **_kwargs):
                    if on_click is not None:
                        self.callbacks[label] = on_click
                    return _Node()

            ui = _Ui()
            snapshot = TargetRootSnapshot.capture({
                "bangumi_path": flow / "TV",
                "movie_path": flow / "Movies",
                "anime_path": flow / "Anime",
                "anime_movie_path": flow / "AnimeMovies",
            })
            before_revision = initial.state.revision
            before_fingerprint = store.fingerprint(initial.plan)
            with (
                patch.object(review_page, "ui", ui),
                patch.object(review_page, "notify") as notify_mock,
                patch.object(review_page, "TASK_PATH", flow / "tasks"),
                patch.object(utils_module, "TASK_PATH", flow / "tasks"),
                patch(
                    "src.rename.target_routing.snapshot_from_config_manager",
                    return_value=snapshot,
                ),
            ):
                PlanReviewDialog._render_completion_choice(
                    dialog,
                    dialog.plan.presentation_groups[0],
                    selected_kind="tv_episodes",
                )
                if dialog.tv_season_input is not None:
                    dialog.tv_season_input.value = 4
                callback = ui.callbacks.get("按季和集智能整理")
                if callback is None:
                    raise AssertionError("TV smart completion button is not wired")
                callback()

            persisted = store.load(initial.plan.task_id)
            persisted_state = state_store.load(initial.plan.task_id)
            notice_calls = tuple(
                (str(call.args[0]), str(call.kwargs.get("type") or ""))
                for call in notify_mock.call_args_list
                if call.args
            )
            notifications = tuple(text for text, _kind in notice_calls)
            extras = tuple(
                entry
                for entry in persisted.entries
                if entry.presentation_asset is not None
                and entry.presentation_asset.asset_role.kind == "extra"
            )

        self.assertEqual(
            "inventory_smart_completion",
            persisted.metadata.get("mapping_source"),
            notifications,
        )
        self.assertEqual(
            (("已完成可确定项目；仍有 4 个结构问题需要确认", "warning"),),
            notice_calls,
        )
        self.assertGreater(persisted_state.revision, before_revision)
        self.assertNotEqual(before_fingerprint, store.fingerprint(persisted))
        self.assertEqual(
            persisted_state.revision,
            persisted.metadata.get("decision_state_revision"),
        )
        self.assertEqual(
            DecisionStateStore.fingerprint(persisted_state),
            persisted.metadata.get("decision_state_fingerprint"),
        )
        self.assertEqual({"tv_episodes"}, {group.kind for group in persisted.presentation_groups})
        self.assertEqual(
            {4},
            {
                entry.season_id
                for entry in persisted.entries
                if entry.media_kind != "associated"
            },
        )
        self.assertEqual({4}, {entry.season_id for entry in extras})
        self.assertEqual(3, len(extras))
        expected_season_root = (
            Path(str(persisted.metadata["canonical_work_root"])) / "Season 04"
        )
        self.assertTrue(
            all(entry.target_path.parent.parent == expected_season_root for entry in extras)
        )
        self.assertTrue(all(entry.target_path.parent.name == "Extras" for entry in extras))

    def test_movie_extras_control_is_exact_capitalized_plural(self):
        """The already-correct movie projection is a non-RED control."""

        fixture = isolated_fixture.IsolatedSampleAcceptanceTests(
            methodName="runTest"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _source, allowed, plan, store, service = fixture._inventory_plan_with_identity(
                root,
                (
                    "Example Work [1080p].mkv",
                    "SPs/Example Work [CM01].mkv",
                ),
                task_id="movie-extras-control",
                is_movie=True,
            )
            revised = service.complete_inventory_draft(
                plan.task_id,
                store.fingerprint(plan),
                group_id=plan.presentation_groups[0].group_id,
                group_kind="movie_continuous",
                target_root=allowed / "AnimeMovies",
                reason="用户确认电影正片",
                root_snapshot=isolated_fixture.routing_snapshot_for(
                    allowed / "AnimeMovies",
                    domain="animation", group_kind="movie_continuous",
                ),
                primary_video_source=next(
                    entry.source_path
                    for entry in plan.entries
                    if entry.media_kind == "video"
                    and "SPs" not in entry.source_path.parts
                ),
            )
            extra = next(
                entry
                for entry in revised.entries
                if entry.presentation_asset is not None
                and entry.presentation_asset.asset_role.kind == "extra"
            )

        self.assertEqual("Extras", extra.target_path.parent.name)


if __name__ == "__main__":
    unittest.main()
