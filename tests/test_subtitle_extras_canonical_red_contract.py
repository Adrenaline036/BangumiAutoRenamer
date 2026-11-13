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
import tests._media_flow_helpers as movie_fixture
import tests.test_r1_real_media_production_red_contract as production_fixture
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.decision_state import DecisionCommandService, DecisionStateStore
from src.rename.plan_review import PlanReviewService
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.series_catalog import SeriesCatalog
from src.rename.subtitle_workflow import format_subtitle_target_fields
from src.rename.target_routing import TargetRootSnapshot
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
)


class SubtitleExtrasCanonicalRedContracts(unittest.IsolatedAsyncioTestCase):
    async def _complete_real_movie(self, root: Path, task_id: str):
        case = movie_fixture.MediaFlow(methodName="runTest")
        source, primary, expected_sources = movie_fixture._make_omoide(
            root / "sources"
        )
        initial = await case._run_provider_flow(root / "flow", source, task_id)
        completed = await case._complete_movie(
            root=root / "flow",
            result=initial,
            primary=primary,
        )
        self.addCleanup(case.doCleanups)
        self.assertEqual('initial_movie_completion', completed.plan.revision_action,
                         (completed.notices, completed.form_messages))
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
            if current_provider_selection_context(initial.task).get(
                "previous_workflow_status"
            ) != "need_confirm":
                problems.append("provider selection did not follow the unapproved inventory draft")
            actual_inputs=set(_source.rglob('*'))
            actual_inputs={p for p in actual_inputs if p.is_file()}
            media_inputs = actual_inputs - set(plan.ignored_sources)
            if {e.source_path for e in plan.entries} != media_inputs or set(plan.expected_sources) != actual_inputs:
                problems.append("production movie flow lost retained media or inventory")
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
            non_watchable = {p for p in expected_sources if 'SPs' in p.parts}
            if extra_entries or not non_watchable <= set(plan.ignored_sources):
                problems.append("CM/Menu must stay excluded while the canonical subtitle remains")
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
            self.assertEqual("need_confirm", completed.task.get("workflow_status"),
                [(i.code, i.message, str(i.source_path)) for i in PlanVerifier().verify(completed.plan).issues])
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
                    "content_policy_pending",
                    "projection_receipt_mismatch",
                    "movie_version_selection_invalid",
                    "movie_asset_authority_invalid",
                },
                split_codes - canonical_codes,
            )

    async def test_current_tv_explicit_extra_uses_exact_capitalized_extras(self):
        """New default keeps an ambiguous NCOP pending until explicit choice."""

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
                is_anime=True,
                is_movie=False,
            )
            persisted = PlanStore(root / "flow" / "plans").load(result.plan.task_id)
            flow = root/'flow'
            review = PlanReviewService(PlanStore(flow/'plans'), flow/'reviews', flow/'work_catalog',
                task_root=flow/'tasks', decision_state_root=flow/'decisions', record_root=flow/'records',
                provider_link_catalog_root=flow/'provider_links')
            from src.rename.initial_movie_completion import RootSnapshotSource
            roots = TargetRootSnapshot.capture({'bangumi_path': flow/'TV', 'movie_path': flow/'Movies',
                'anime_path': flow/'Anime', 'anime_movie_path': flow/'AnimeMovies'})
            review.root_snapshot_source = RootSnapshotSource(lambda: roots)
            from tests.test_content_choice_contract import change
            persisted=change(review,persisted,[extra_source],action='restore')
            extra=next(e for e in persisted.entries if e.source_path==extra_source)
            from src.rename.plan_review import entry_id
            persisted = review.revise_tv_content(persisted.task_id, PlanStore.fingerprint(persisted),
                expected_revision=review.decision_state_store.load(persisted.task_id).revision, mode='role',
                edit={'entry_id': entry_id(extra), 'role': 'extra'})
            extra = next(e for e in persisted.entries if e.source_path == extra_source)
            canonical_root = Path(str(persisted.metadata["canonical_work_root"]))

        self.assertEqual('tv_content_role', persisted.revision_action)
        self.assertEqual(canonical_root / "Season 02" / "Extras", extra.target_path.parent)

    async def test_movie_has_no_legacy_tv_conversion_or_plan_mutation(self):
        """Retired cross-type layout action must not return through old state."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            case = movie_fixture.MediaFlow(methodName="runTest")
            source, _primary, _expected = movie_fixture._make_omoide(root / "sources")
            initial = await case._run_provider_flow(root / "flow", source, "smart-tv-extras-canonical")
            self.assertIsNotNone(initial.state.content_selection)
            flow = root / "flow"
            store = PlanStore(flow / "plans")
            review = PlanReviewService(
                store, flow / "reviews", flow / "work_catalog",
                task_root=flow / "tasks",
                decision_state_root=flow / "decisions",
            )
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
                def expansion(*_args, **_kwargs):
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
                self.assertNotIn("生成剧集整理计划", ui.callbacks)
                self.assertNotIn("重新生成剧集整理计划", ui.callbacks)
                self.assertNotIn("按季和集智能整理", ui.callbacks)

            persisted = store.load(initial.plan.task_id)
            persisted_state = state_store.load(initial.plan.task_id)
            notify_mock.assert_not_called()
        self.assertEqual(before_revision, persisted_state.revision)
        self.assertEqual(before_fingerprint, store.fingerprint(persisted))
        self.assertEqual('movie', persisted.work_identity.primary_provider.entity_type)



if __name__ == "__main__":
    unittest.main()
