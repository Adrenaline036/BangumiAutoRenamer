"""Production contracts for movie font archives and WorkCatalog authority.

Provider transport and storage roots are isolated, while the real
task queue, worker, provider-selection action, ``Rename.process/_process``,
movie smart-completion action, approval, execution queue and ``Trans`` remain
in the call path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import src.pages.plan_review_page as review_page
import src.utils.utils as utils_module
import tests.test_r1_real_media_production_red_contract as r1_fixture
import tests.test_tv_font_series_association_red_contract as tv_fixture
from tests.test_isolated_sample_acceptance import routing_snapshot_for
from tests.projection_test_support import save_plan_fixture
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.decision_state import DecisionCommandService, DecisionStateStore
from src.rename.execution_recovery import (
    ExecutionRecoveryError,
    reconcile_catalog_update,
)
from src.rename.inventory_plan import build_inventory_review_draft
from src.rename.movie_assets import inventory_movie_assets
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.recovery_service import RecoveryService
from src.rename.rename_plan import PlanStatus
from src.rename.target_routing import TargetRootSnapshot
from src.rename.utils import is_font_archive
from src.rename.work_catalog import WorkCatalog
from src.task_queue import TaskQueueManager


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class _LockTrackingWorkCatalog(WorkCatalog):
    """Observe that recovery re-diagnosis and registration share one work lock."""

    def __init__(self, root: Path):
        super().__init__(root)
        self.enforce_recovery_lock = False
        self.lock_depth = 0
        self.lock_violations: list[str] = []

    @contextmanager
    def lock(self, work_key: str):
        with super().lock(work_key):
            self.lock_depth += 1
            try:
                yield
            finally:
                self.lock_depth -= 1

    def load(self, work_key: str):
        if self.enforce_recovery_lock and self.lock_depth == 0:
            self.lock_violations.append("load")
        return super().load(work_key)

    def register_completed(self, *args, **kwargs):
        if self.enforce_recovery_lock and self.lock_depth == 0:
            self.lock_violations.append("register_completed")
        return super().register_completed(*args, **kwargs)


def _make_omoide(root: Path, *, task_suffix: str = "") -> tuple[Path, Path, tuple[Path, ...]]:
    source = root / (
        "[VCB-Studio] Yama no Susume Omoide Present "
        f"[Ma10p_1080p]{task_suffix}"
    )
    stem = "[VCB-Studio] Yama no Susume Omoide Present [Ma10p_1080p][x265_flac]"
    main = _write(source / f"{stem}.mkv", b"omoide-main-video")
    planned = (
        main,
        _write(source / f"{stem}.mka", b"omoide-external-audio"),
        _write(source / f"{stem}.sc.ass", b"omoide-subtitle"),
        _write(source / "SPs" / f"{stem} [CM01].mkv", b"omoide-cm01"),
        _write(source / "SPs" / f"{stem} [CM02].mkv", b"omoide-cm02"),
        _write(source / "SPs" / f"{stem} [Menu].mkv", b"omoide-menu"),
        _write(
            source / "[VCB-Studio] Yama no Susume Omoide Present [Fonts].7z",
            b"omoide-font-package",
        ),
    )
    _write(source / "CDs" / "soundtrack.flac", b"ignored-cd")
    _write(source / "Scans" / "booklet.webp", b"ignored-scan")
    return source, main, planned


def _movie_candidate() -> dict:
    return {
        "namespace": "tmdb",
        "entity_type": "movie",
        "provider_id": "763096",
        "title": "向山进发 回忆的礼物",
        "original_title": "ヤマノススメ おもいでプレゼント",
        "year": 2017,
        "poster_url": r1_fixture.POSTER_URL,
        "source_url": "https://www.themoviedb.org/movie/763096",
    }


class MovieFontProductionRedContracts(unittest.IsolatedAsyncioTestCase):
    async def _run_provider_flow(
        self, root: Path, source: Path, task_id: str,
    ) -> SimpleNamespace:
        harness = SimpleNamespace(
            _NoopReviewDialog=(
                r1_fixture.R1RealQueueWorkerContracts._NoopReviewDialog
            )
        )
        with (
            patch("src.rename.process.SERIES_CATALOG_PATH", root / "series_catalog"),
            patch("src.rename.process.RECORD_PATH", root / "records"),
            patch("src.rename.process.is_dry_run_enabled", return_value=False),
        ):
            return await r1_fixture.R1RealQueueWorkerContracts._run_flow(
                harness,
                root=root,
                source=source,
                task_id=task_id,
                candidate=_movie_candidate(),
            )

    def _complete_movie(
        self,
        *,
        root: Path,
        result: SimpleNamespace,
        primary: Path | tuple[Path, ...],
    ) -> SimpleNamespace:
        primary_sources = primary if isinstance(primary, tuple) else (primary,)
        store = PlanStore(root / "plans")
        review = PlanReviewService(
            store,
            root / "reviews",
            root / "work_catalog",
            decision_state_root=root / "decisions",
            task_root=root / "tasks",
        )
        task = json.loads(
            (root / "tasks" / f"{result.plan.task_id}.json").read_text("utf-8")
        )
        dialog = SimpleNamespace(
            plan=store.load(result.plan.task_id),
            task_decision=DecisionStateStore(root / "decisions").load(
                result.plan.task_id
            ),
            task_data=task,
            review_service=review,
            decision_command_service=DecisionCommandService(
                DecisionStateStore(root / "decisions"), store
            ),
            movie_primary_video_input=SimpleNamespace(
                value=[str(item) for item in primary_sources]
            ),
            selected_ids=set(),
            selection_label=SimpleNamespace(text=""),
            review_badge=SimpleNamespace(text=""),
            presentation_group=SimpleNamespace(options={}, update=MagicMock()),
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
        dialog._adopt_revision = PlanReviewDialog._adopt_revision.__get__(dialog)
        dialog._video_options = PlanReviewDialog._video_options.__get__(dialog)
        dialog._selected_movie_primary_sources = (
            PlanReviewDialog._selected_movie_primary_sources.__get__(dialog)
        )
        dialog._render_movie_primary_picker = (
            PlanReviewDialog._render_movie_primary_picker.__get__(dialog)
        )
        dialog._complete_movie_with_primary = (
            PlanReviewDialog._complete_movie_with_primary.__get__(dialog)
        )
        dialog._select_library_domain = MagicMock()
        dialog._complete_inventory_draft = MagicMock()
        dialog._toggle_movie_segments = MagicMock()

        class _UiNode:
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

            def add_slot(self, *_args):
                return self

            def set_visibility(self, *_args):
                return None

            def update(self):
                return None

            def on(self, *_args, **_kwargs):
                return self

            async def run_method(self, *_args):
                return None

        class _CompletionUi:
            def __init__(self):
                self.callbacks = {}

            @staticmethod
            def column():
                return _UiNode()

            @staticmethod
            def row():
                return _UiNode()

            @staticmethod
            def label(_value):
                return _UiNode()

            @staticmethod
            def checkbox(*_args, **_kwargs):
                return _UiNode()

            @staticmethod
            def number(*_args, **kwargs):
                return _UiNode(value=kwargs.get("value"))

            @staticmethod
            def select(_options, *, value=None, **_kwargs):
                return _UiNode(value=value)

            def button(self, label, *, on_click=None, **_kwargs):
                if on_click is not None:
                    self.callbacks[label] = on_click
                return _UiNode()

        snapshot = TargetRootSnapshot.capture({
            "bangumi_path": root / "TV",
            "movie_path": root / "Movies",
            "anime_path": root / "Anime",
            "anime_movie_path": root / "AnimeMovies",
        })
        with (
            patch.object(utils_module, "TASK_PATH", root / "tasks"),
            patch.object(review_page, "TASK_PATH", root / "tasks"),
            patch.object(review_page, "notify"),
            patch.object(review_page, "ui", completion_ui := _CompletionUi()),
            patch(
                "src.rename.target_routing.snapshot_from_config_manager",
                return_value=snapshot,
            ),
        ):
            PlanReviewDialog._render_completion_choice(
                dialog,
                dialog.plan.presentation_groups[0],
                selected_kind=None,
            )
            dialog.movie_primary_video_input.value = [
                str(item) for item in primary_sources
            ]
            completion_callback = completion_ui.callbacks.get(
                "按所选视频生成电影正片版本"
            )
            if completion_callback is None:
                raise AssertionError("movie completion action was not wired to its UI button")
            completion_callback()
        return SimpleNamespace(
            plan=store.load(result.plan.task_id),
            state=DecisionStateStore(root / "decisions").load(result.plan.task_id),
            task=json.loads(
                (root / "tasks" / f"{result.plan.task_id}.json").read_text("utf-8")
            ),
            review=review,
            store=store,
        )

    async def _approve_and_execute(
        self,
        *,
        root: Path,
        completed: SimpleNamespace,
        before_queue=None,
        before_register=None,
    ) -> tuple[dict, dict, PlanExecutionService]:
        plan = completed.store.load(completed.plan.task_id)
        fingerprint = completed.store.fingerprint(plan)
        dialog = SimpleNamespace(
            review_service=completed.review,
            plan=plan,
            selected_ids={entry_id(item) for item in plan.entries},
            task_data=dict(completed.task),
            decision=None,
            review_badge=SimpleNamespace(text=""),
            _refresh_plan_header=MagicMock(),
            _render_entries=MagicMock(),
            _refresh_execute_button=MagicMock(),
            snapshot=review_page.build_plan_review_snapshot(plan),
        )
        dialog._approve = PlanReviewDialog._approve.__get__(dialog)
        with (
            patch.object(utils_module, "TASK_PATH", root / "tasks"),
            patch.object(review_page, "notify"),
        ):
            PlanReviewDialog._approve_all(dialog)

        service = PlanExecutionService(
            completed.store,
            completed.review,
            task_root=root / "tasks",
            record_root=root / "records",
            catalog_root=root / "work_catalog",
            series_catalog_root=root / "series_catalog",
            decision_state_root=root / "decisions",
            recovery_transaction_root=root / "recovery_transactions",
            provider_link_catalog_root=root / "provider_links",
        )
        original_register = service.work_catalog.register_completed

        def registering(*args, **kwargs):
            if before_register is not None:
                before_register(args[0], args[1])
            return original_register(*args, **kwargs)

        manager = TaskQueueManager(
            task_root=root / "tasks",
            execution_service=service,
            decision_state_root=root / "decisions",
            recovery_abandon_audit_root=root / "abandon_audit",
            plan_root=root / "plans",
            test_mode=False,
        )

        class _UiNode:
            def __init__(self):
                self.opened = False
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def classes(self, *_args):
                return self

            def props(self, *_args):
                return self

            def open(self):
                self.opened = True

            def close(self):
                self.closed = True

        class _FakeUi:
            def __init__(self):
                self.confirm = _UiNode()
                self.confirm_callback = None

            def dialog(self):
                return self.confirm

            @staticmethod
            def card():
                return _UiNode()

            @staticmethod
            def label(_value):
                return _UiNode()

            @staticmethod
            def row():
                return _UiNode()

            def button(self, label, *, on_click=None, **_kwargs):
                if label == "确认入队执行":
                    self.confirm_callback = on_click
                return _UiNode()

        fake_ui = _FakeUi()
        dialog._queue_execution = PlanReviewDialog._queue_execution.__get__(dialog)
        dialog._close_review = MagicMock()
        await manager.start()
        try:
            with (
                patch.object(
                    service.work_catalog,
                    "register_completed",
                    side_effect=registering,
                ),
                patch("src.rename.plan_execution.is_dry_run_enabled", return_value=False),
                patch("src.task_queue.logger.exception"),
                patch.object(review_page, "ui", fake_ui),
                patch.object(review_page, "task_queue", manager),
                patch.object(review_page, "is_ui_test_mode", return_value=False),
                patch.object(utils_module, "TASK_PATH", root / "tasks"),
                patch.object(review_page, "notify"),
            ):
                PlanReviewDialog._confirm_execute(dialog)
                self.assertTrue(fake_ui.confirm.opened)
                self.assertIsNotNone(fake_ui.confirm_callback)
                if before_queue is not None:
                    before_queue(plan)
                await fake_ui.confirm_callback()
                await asyncio.wait_for(manager._queue.join(), timeout=8)
        finally:
            await manager.stop()
        task = json.loads(
            (root / "tasks" / f"{plan.task_id}.json").read_text("utf-8")
        )
        ledger_path = root / "records" / f"{plan.task_id}.ledger.json"
        ledger = json.loads(ledger_path.read_text("utf-8")) if ledger_path.is_file() else {}
        return task, ledger, service

    async def test_real_omoide_queue_dialog_execution_creates_exact_seven_with_movie_font_authority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, primary, expected_sources = _make_omoide(root / "sources")
            tv_case = tv_fixture.TVFontSeriesAssociationRedContracts(
                methodName="runTest"
            )
            _tv_result, _tv_source, tv_font = await tv_case._materialize_s1(
                root / "flow",
                font_bytes=(
                    source / "[VCB-Studio] Yama no Susume Omoide Present [Fonts].7z"
                ).read_bytes(),
            )
            series_before = {
                path: path.read_bytes()
                for path in (root / "flow" / "series_catalog").glob("*.json")
            }
            provider_before = {
                path: path.read_bytes()
                for path in (root / "flow" / "provider_links").glob("*.json")
            }
            initial = await self._run_provider_flow(root / "flow", source, "omoide-e2e")
            completed = self._complete_movie(
                root=root / "flow", result=initial, primary=primary
            )
            task, ledger, _service = await self._approve_and_execute(
                root=root / "flow", completed=completed
            )

            plan = completed.store.load("omoide-e2e")
            font_entry = next(
                item for item in plan.entries if is_font_archive(item.source_path)
            )
            manifest = plan.metadata.get("associated_asset_manifest")
            work_key = str(plan.metadata.get("catalog_work_key") or plan.metadata.get("proposed_work_key"))
            catalog = WorkCatalog(root / "flow" / "work_catalog").load(work_key)
            font_assets = [
                item for item in catalog.get("assets", [])
                if any(
                    association.get("role") == "font"
                    for association in item.get("associations", [])
                    if isinstance(association, dict)
                )
            ]
            expected_font_target = (
                root / "flow" / "AnimeMovies" / "向山进发 回忆的礼物 (2017)"
                / "Associated" / "向山进发 回忆的礼物 (2017) [Fonts].7z"
            )
            problems = []
            if initial.initial_plan.metadata.get("mapping_source") != "inventory_review_draft":
                problems.append("initial production worker did not persist the inventory draft")
            if initial.task.get("selection_operation", {}).get("previous_workflow_status") != "invalid":
                problems.append("provider selection did not follow the safe-invalid draft")
            if len(plan.entries) != 7 or set(plan.expected_sources) != set(expected_sources):
                problems.append("Omoide inventory8 did not become the exact seven-file Plan")
            if len(inventory_movie_assets(source).entries) != 8:
                problems.append("Omoide fixture did not preserve the inventory8 boundary")
            primary_provider = (
                plan.work_identity.primary_provider if plan.work_identity is not None else None
            )
            if (
                primary_provider is None
                or (
                    primary_provider.namespace,
                    primary_provider.entity_type,
                    primary_provider.provider_id,
                ) != ("tmdb", "movie", "763096")
            ):
                problems.append("TMDB movie 763096 is not the primary work authority")
            linked = {
                (item.namespace, item.entity_type, item.provider_id)
                for item in plan.linked_provider_identities
            }
            if ("bangumi", "subject", "216311") not in linked:
                problems.append("Bangumi 216311 is missing as auxiliary movie evidence")
            if {group.kind for group in plan.presentation_groups} != {
                "movie_continuous"
            }:
                problems.append("Omoide smart completion did not persist one movie group")
            canonical_root = Path(str(plan.metadata.get("canonical_work_root") or ""))
            if not canonical_root or any(
                not entry.target_path.is_relative_to(canonical_root)
                for entry in plan.entries
            ):
                problems.append("one or more Omoide targets escaped the canonical movie root")
            by_source = {entry.source_path: entry for entry in plan.entries}
            main_entry = by_source[primary]
            audio_entry = next(
                entry for entry in plan.entries if entry.source_path.suffix.lower() == ".mka"
            )
            subtitle_entry = next(
                entry for entry in plan.entries if entry.source_path.name.lower().endswith(".sc.ass")
            )
            extra_entries = [
                entry for entry in plan.entries if "SPs" in entry.source_path.parts
            ]
            if (
                audio_entry.target_path.parent != main_entry.target_path.parent
                or audio_entry.target_path.stem != main_entry.target_path.stem
                or subtitle_entry.target_path.parent != main_entry.target_path.parent
                or subtitle_entry.subtitle is None
                or subtitle_entry.subtitle.associated_video_source_path != primary
                or not subtitle_entry.target_path.name.startswith(
                    f"{main_entry.target_path.stem}."
                )
                or not subtitle_entry.target_path.name.endswith(".zh-Hans.ass")
            ):
                problems.append("movie audio/subtitle did not follow the selected primary video")
            if len(extra_entries) != 3 or any(
                "Extras" not in entry.target_path.relative_to(canonical_root).parts
                for entry in extra_entries
            ):
                problems.append("the three SP videos were not routed to the movie Extras area")
            if font_entry.target_path != expected_font_target:
                problems.append(f"movie font target is not canonical: {font_entry.target_path}")
            if (
                font_entry.media_kind != "associated"
                or font_entry.role != "font"
                or font_entry.presentation_asset is None
                or font_entry.presentation_asset.asset_role.kind != "associated"
                or font_entry.presentation_asset.owner_version_ref != "shared"
            ):
                problems.append("movie font entry is outside the strict associated/font/shared set")
            if (
                not isinstance(manifest, Mapping)
                or manifest.get("schema_version") != 1
                or manifest.get("catalog_binding") != {
                    "backend": "work",
                    "key": work_key,
                }
                or [dict(item) for item in manifest.get("assets", ())] != [{
                "role": "fonts",
                "scope": "work",
                "season_number": None,
                "source_path": str(font_entry.source_path),
                "target_path": str(font_entry.target_path),
                "source_size": font_entry.source_path.stat().st_size,
                "source_sha256": _sha256(font_entry.source_path),
                }]
            ):
                problems.append("movie font manifest is absent or not the exact work/null-season row")
            for key in (
                "season_number", "season_id", "series_key", "season_binding_id",
                "provider_link_snapshot",
            ):
                if plan.metadata.get(key) is not None:
                    problems.append(f"movie Plan retained TV authority field {key}")
            rows = ledger.get("entries", [])
            if len(rows) != 7 or any(
                row.get("status") != "created"
                or row.get("action") != "create"
                or row.get("transfer_type") != "hardlink"
                for row in rows
            ):
                problems.append("movie execution ledger is not seven create/hardlink rows")
            if task.get("execution_status") != "completed":
                problems.append(f"movie task did not complete: {task.get('execution_status')}")
            if not os.path.samefile(font_entry.source_path, font_entry.target_path):
                problems.append("fresh movie font is not an independent movie-library hardlink")
            if font_entry.target_path == tv_font or os.path.samefile(font_entry.target_path, tv_font):
                problems.append("movie font incorrectly reused the TV-series location")
            if len(font_assets) != 1:
                problems.append("WorkCatalog did not persist exactly one strict font asset")
            else:
                font_size = font_entry.source_path.stat().st_size
                font_digest = _sha256(font_entry.source_path)
                asset = font_assets[0]
                exact_association = {
                    "source": str(font_entry.source_path),
                    "task_id": plan.task_id,
                    "owner_version_ref": "shared",
                    "media_kind": "associated",
                    "role": "font",
                }
                if (
                    asset.get("target") != str(font_entry.target_path)
                    or asset.get("content_key")
                    != f"sha256:{font_size}:{font_digest}"
                    or asset.get("size") != font_size
                    or asset.get("sha256") != font_digest
                    or asset.get("scope") != "shared"
                    or asset.get("associations") != [exact_association]
                ):
                    problems.append(
                        "fresh WorkCatalog font identity/target/association is not exact"
                    )
            series_after = {
                path: path.read_bytes()
                for path in (root / "flow" / "series_catalog").glob("*.json")
            }
            provider_after = {
                path: path.read_bytes()
                for path in (root / "flow" / "provider_links").glob("*.json")
            }
            if series_after != series_before or provider_after != provider_before:
                problems.append("movie execution changed the materialized TV authority")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_post_transfer_font_tamper_blocks_work_catalog_and_recovery_health(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "flow"
            source, primary, _expected = _make_omoide(Path(temp_dir) / "sources", task_suffix="-tamper")
            initial = await self._run_provider_flow(root, source, "omoide-tamper")
            completed = self._complete_movie(root=root, result=initial, primary=primary)
            catalog_before = {
                path.name: path.read_bytes()
                for path in (root / "work_catalog").glob("*.json")
            } if (root / "work_catalog").exists() else {}

            def tamper(plan, _ledger):
                font = next(item for item in plan.entries if is_font_archive(item.source_path))
                original_size = font.source_path.stat().st_size
                font.source_path.write_bytes(b"x" * original_size)

            task, ledger, service = await self._approve_and_execute(
                root=root, completed=completed, before_register=tamper
            )
            plan = completed.store.load("omoide-tamper")
            state = DecisionStateStore(root / "decisions").load("omoide-tamper")
            font = next(item for item in plan.entries if is_font_archive(item.source_path))
            recovery = RecoveryService(
                catalog_root=root / "work_catalog",
                record_root=root / "records",
                series_catalog_root=root / "series_catalog",
            )
            snapshot = recovery.diagnose(state, plan)
            catalog_after = {
                path.name: path.read_bytes()
                for path in (root / "work_catalog").glob("*.json")
            } if (root / "work_catalog").exists() else {}
            problems = []
            if task.get("execution_status") != "catalog_update_pending":
                problems.append("tampered font did not stop at catalog_update_pending")
            if catalog_before != catalog_after:
                problems.append("tampered font changed WorkCatalog bytes/files")
            if not font.target_path.exists() or not os.path.samefile(font.source_path, font.target_path):
                problems.append("tamper recovery did not preserve the hardlink evidence")
            if len(ledger.get("entries", [])) != 7:
                problems.append("tamper case lost the completed seven-row ledger")
            if snapshot.state == "healthy":
                problems.append("recovery misreported tampered font as healthy")
            task_path = root / "tasks" / f"{plan.task_id}.json"
            task_before_recovery = task_path.read_bytes()
            catalog_before_recovery = dict(catalog_after)

            def media_state():
                rows = {}
                for path in (font.source_path, font.target_path):
                    stat = path.stat()
                    rows[str(path)] = (
                        stat.st_dev, stat.st_ino, stat.st_nlink, stat.st_size,
                        _sha256(path),
                    )
                return rows

            media_before_recovery = media_state()
            try:
                recovery.rebuild_catalog(state, plan, service.work_catalog)
            except ValueError:
                pass
            else:
                problems.append("RecoveryService rebuilt a Catalog from tampered font evidence")
            try:
                reconcile_catalog_update(
                    plan.task_id,
                    task_root=root / "tasks",
                    record_root=root / "records",
                    plan_store=completed.store,
                    work_catalog=service.work_catalog,
                )
            except ExecutionRecoveryError:
                pass
            else:
                problems.append("public catalog recovery accepted tampered font evidence")
            catalog_after_recovery = {
                path.name: path.read_bytes()
                for path in (root / "work_catalog").glob("*.json")
            } if (root / "work_catalog").exists() else {}
            if task_path.read_bytes() != task_before_recovery:
                problems.append("tamper recovery changed the pending task authority")
            if catalog_after_recovery != catalog_before_recovery:
                problems.append("tamper recovery changed the WorkCatalog")
            if media_state() != media_before_recovery:
                problems.append("tamper recovery changed the preserved media evidence")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_post_transfer_same_bytes_non_hardlink_replacement_blocks_catalog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "flow"
            source, primary, _expected = _make_omoide(
                Path(temp_dir) / "sources", task_suffix="-identity"
            )
            initial = await self._run_provider_flow(root, source, "omoide-identity")
            completed = self._complete_movie(root=root, result=initial, primary=primary)

            def replace_target(plan, _ledger):
                font = next(item for item in plan.entries if is_font_archive(item.source_path))
                font.target_path.unlink()
                shutil.copyfile(font.source_path, font.target_path)

            task, ledger, _service = await self._approve_and_execute(
                root=root,
                completed=completed,
                before_register=replace_target,
            )
            plan = completed.store.load("omoide-identity")
            state = DecisionStateStore(root / "decisions").load("omoide-identity")
            font = next(item for item in plan.entries if is_font_archive(item.source_path))
            catalog_files = tuple((root / "work_catalog").glob("*.json")) \
                if (root / "work_catalog").exists() else ()
            problems = []
            if not font.target_path.is_file():
                problems.append("identity replacement did not leave the intended fault fixture")
            else:
                if _sha256(font.source_path) != _sha256(font.target_path):
                    problems.append("identity replacement changed bytes instead of only hardlink identity")
                if os.path.samefile(font.source_path, font.target_path):
                    problems.append("identity replacement unexpectedly remained a hardlink")
            if task.get("execution_status") != "catalog_update_pending":
                problems.append("same-byte non-hardlink target did not stop at catalog_update_pending")
            if catalog_files:
                problems.append("same-byte non-hardlink target was registered in WorkCatalog")
            if len(ledger.get("entries", [])) != 7:
                problems.append("identity fault did not preserve the completed transfer ledger")
            recovery = RecoveryService(
                catalog_root=root / "work_catalog",
                record_root=root / "records",
                series_catalog_root=root / "series_catalog",
            )
            snapshot = recovery.diagnose(state, plan)
            if snapshot.state == "healthy":
                problems.append("recovery misreported a same-byte non-hardlink target as healthy")
            task_path = root / "tasks" / f"{plan.task_id}.json"
            task_before_recovery = task_path.read_bytes()
            catalog_before_recovery = {
                path.name: path.read_bytes()
                for path in (root / "work_catalog").glob("*.json")
            } if (root / "work_catalog").exists() else {}

            def media_state():
                rows = {}
                for path in (font.source_path, font.target_path):
                    stat = path.stat()
                    rows[str(path)] = (
                        stat.st_dev, stat.st_ino, stat.st_nlink, stat.st_size,
                        _sha256(path),
                    )
                return rows

            media_before_recovery = media_state()
            try:
                recovery.rebuild_catalog(state, plan, _service.work_catalog)
            except ValueError:
                pass
            else:
                problems.append("RecoveryService accepted a same-byte non-hardlink target")
            try:
                reconcile_catalog_update(
                    plan.task_id,
                    task_root=root / "tasks",
                    record_root=root / "records",
                    plan_store=completed.store,
                    work_catalog=_service.work_catalog,
                )
            except ExecutionRecoveryError:
                pass
            else:
                problems.append("public recovery accepted a same-byte non-hardlink target")
            catalog_after_recovery = {
                path.name: path.read_bytes()
                for path in (root / "work_catalog").glob("*.json")
            } if (root / "work_catalog").exists() else {}
            if task_path.read_bytes() != task_before_recovery:
                problems.append("identity recovery changed the pending task authority")
            if catalog_after_recovery != catalog_before_recovery:
                problems.append("identity recovery changed the WorkCatalog")
            if media_state() != media_before_recovery:
                problems.append("identity recovery changed the preserved media evidence")
            self.assertEqual([], problems, "\n".join(problems))

    async def test_pre_execution_font_drift_is_rejected_before_any_media_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "flow"
            source, primary, _expected = _make_omoide(
                Path(temp_dir) / "sources", task_suffix="-prewrite"
            )
            initial = await self._run_provider_flow(root, source, "omoide-prewrite")
            completed = self._complete_movie(root=root, result=initial, primary=primary)
            plan = completed.store.load("omoide-prewrite")
            targets = tuple(entry.target_path for entry in plan.entries)

            def drift(current_plan):
                font = next(
                    item for item in current_plan.entries
                    if is_font_archive(item.source_path)
                )
                size = font.source_path.stat().st_size
                font.source_path.write_bytes(b"d" * size)

            task, ledger, _service = await self._approve_and_execute(
                root=root,
                completed=completed,
                before_queue=drift,
            )
            catalog_files = tuple((root / "work_catalog").glob("*.json")) \
                if (root / "work_catalog").exists() else ()
            problems = []
            if any(os.path.lexists(target) for target in targets):
                problems.append("font drift reached media transfer and created target files")
            if ledger:
                problems.append("font drift produced an execution ledger before rejection")
            if catalog_files:
                problems.append("font drift wrote WorkCatalog before rejection")
            if task.get("execution_status") == "completed":
                problems.append("font drift incorrectly completed the approved task")
            if task.get("execution_phase") == "media_boundary_entered":
                problems.append("font drift crossed the durable media boundary")
            self.assertEqual([], problems, "\n".join(problems))


class MovieFontPlanningAndCatalogRedContracts(unittest.TestCase):
    def _draft_movie(
        self,
        *,
        root: Path,
        task_id: str,
        font_name: str | None,
        font_bytes: bytes = b"font-package",
        provider_id: str = "9001",
        video_name: str | None = None,
    ):
        source = root / f"source-{task_id}"
        source.mkdir(parents=True)
        main = _write(
            source / (video_name or f"Example Work [{task_id}].mkv"),
            task_id.encode(),
        )
        if font_name is not None:
            _write(source / font_name, font_bytes)
        task = {
            "uuid": task_id,
            "path": str(source),
            "name": "Example Work",
            "is_anime": True,
            "is_movie": True,
            "movie_inventory": inventory_movie_assets(source).to_dict(),
            "draft_target_root": str(root / "library"),
            "draft_initial_target_root": str(root / "library" / "AnimeMovies" / "Example Work (2026)"),
            "provider_identity": {
                "namespace": "tmdb",
                "entity_type": "movie",
                "provider_id": provider_id,
                "title": "Example Work",
                "year": 2026,
            },
            "proposed_work_key": f"work-12345678-1234-1234-1234-{task_id[-1] * 12}",
        }
        store = PlanStore(root / "plans")
        draft = build_inventory_review_draft(task, dry_run=False)
        state_store = DecisionStateStore(root / "decisions")
        draft = save_plan_fixture(store, draft, state_store=state_store)
        service = PlanReviewService(
            store,
            root / "reviews",
            root / "catalog",
            decision_state_root=state_store.root,
            task_root=root / "tasks",
        )
        completed = service.complete_inventory_draft(
            task_id,
            store.fingerprint(draft),
            group_id=draft.presentation_groups[0].group_id,
            group_kind="movie_continuous",
            target_root=root / "library" / "AnimeMovies",
            reason="RED movie font routing",
            root_snapshot=routing_snapshot_for(
                root / "library" / "AnimeMovies",
                domain="animation", group_kind="movie_continuous",
            ),
            primary_video_source=main,
        )
        return source, completed, store, service

    def test_movie_font_routing_is_content_deterministic_and_unregistered_targets_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source1, first, _store, _service = self._draft_movie(
                root=root,
                task_id="route1",
                font_name="[Release-A] Example Work [Fonts].7z",
            )
            first_font = next(item for item in first.entries if is_font_archive(item.source_path))
            expected_primary = (
                root / "library" / "AnimeMovies" / "Example Work (2026)"
                / "Associated" / "Example Work (2026) [Fonts].7z"
            )
            for entry in first.entries:
                entry.target_path.parent.mkdir(parents=True, exist_ok=True)
                os.link(entry.source_path, entry.target_path)
            ledger_entries = []
            for entry in first.entries:
                stat = entry.target_path.stat()
                ledger_entries.append({
                    "source": str(entry.source_path),
                    "target": str(entry.target_path),
                    "status": "created",
                    "action": "create",
                    "transfer_type": "hardlink",
                    "source_device": stat.st_dev,
                    "source_inode": stat.st_ino,
                    "target_device": stat.st_dev,
                    "target_inode": stat.st_ino,
                    "target_link_count": stat.st_nlink,
                })
            WorkCatalog(root / "catalog").register_completed(
                first,
                {
                    "task_id": first.task_id,
                    "plan_fingerprint": PlanStore.fingerprint(first),
                    "status": "completed",
                    "entries": ledger_entries,
                },
            )

            _source2, second, _store2, _service2 = self._draft_movie(
                root=root,
                task_id="route2",
                font_name="[Release-B] Totally Different Name [Fonts].7z",
            )
            second_font = next(item for item in second.entries if is_font_archive(item.source_path))
            different = b"different-font-content"
            _source3, third, _store3, _service3 = self._draft_movie(
                root=root,
                task_id="route3",
                font_name="[Release-C] Example Work [Fonts].7z",
                font_bytes=different,
            )
            third_font = next(item for item in third.entries if is_font_archive(item.source_path))
            digest = hashlib.sha256(different).hexdigest()
            expected_secondary = expected_primary.with_name(
                f"Example Work (2026) [{digest}] [Fonts].7z"
            )

            conflict_root = root / "unregistered"
            _conflict_source, conflict, _store4, _service4 = self._draft_movie(
                root=conflict_root,
                task_id="route4",
                font_name="[Release-D] Example Work [Fonts].7z",
                provider_id="9002",
            )
            unregistered = (
                conflict_root / "library" / "AnimeMovies" / "Example Work (2026)"
                / "Associated" / "Example Work (2026) [Fonts].7z"
            )
            _write(unregistered, b"foreign-unregistered-content")
            # Replanning after the conflict exists must not silently collect it.
            _source4b, conflict_after, _store5, _service5 = self._draft_movie(
                root=conflict_root,
                task_id="route5",
                font_name="[Release-E] Example Work [Fonts].7z",
                provider_id="9003",
            )

            problems = []
            if first_font.target_path != expected_primary:
                problems.append("fresh movie font did not use the canonical primary name")
            if (
                second_font.target_path != expected_primary
                or second_font.collision_resolution is None
                or second_font.collision_resolution.action != "reuse_existing"
            ):
                problems.append("same content under a different source name did not reuse trusted WorkCatalog location")
            if third_font.target_path != expected_secondary:
                problems.append("different content did not use the deterministic full-SHA256 target")
            if "Packages" in third_font.target_path.parts:
                problems.append("different content escaped through the legacy Packages route")
            if PlanVerifier().verify(conflict_after).status not in {
                PlanStatus.INVALID, PlanStatus.FAIL_CLOSED,
            }:
                problems.append("unregistered canonical target was not rejected before approval")
            self.assertEqual([], problems, "\n".join(problems))

    def test_explicit_cross_provider_link_appends_provider_and_recovers_exactly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _source1, first, _store1, _service1 = self._draft_movie(
                root=root,
                task_id="link1",
                font_name="[Release-A] Example Work [Fonts].7z",
                provider_id="9001",
            )
            catalog = WorkCatalog(root / "catalog")

            def materialize(plan):
                rows = []
                for entry in plan.entries:
                    if entry.collision_resolution is None:
                        entry.target_path.parent.mkdir(parents=True, exist_ok=True)
                        os.link(entry.source_path, entry.target_path)
                        status, action, transfer = "created", "create", "hardlink"
                    else:
                        status = "reused_identical_asset"
                        action = transfer = "reuse_existing"
                    source_stat = entry.source_path.stat()
                    target_stat = entry.target_path.stat()
                    rows.append({
                        "source": str(entry.source_path),
                        "target": str(entry.target_path),
                        "status": status,
                        "action": action,
                        "transfer_type": transfer,
                        "source_device": source_stat.st_dev,
                        "source_inode": source_stat.st_ino,
                        "target_device": target_stat.st_dev,
                        "target_inode": target_stat.st_ino,
                        "target_link_count": target_stat.st_nlink,
                    })
                return {
                    "task_id": plan.task_id,
                    "plan_fingerprint": PlanStore.fingerprint(plan),
                    "status": "completed",
                    "entries": rows,
                }

            first_ledger = materialize(first)
            base = catalog.register_completed(first, first_ledger, expected_revision=0)

            _source2, second, second_store, second_service = self._draft_movie(
                root=root,
                task_id="link2",
                font_name="[Release-B] Alternate Provider [Fonts].7z",
                provider_id="9002",
                video_name="Example Work [2160p_SDR].mkv",
            )
            linked = second_service.link_existing_catalog_work(
                second.task_id,
                second_store.fingerprint(second),
                work_key=str(base["work_key"]),
                reason="operator explicitly confirmed the cross-provider work",
            )
            linked_ledger = materialize(linked)
            record_root = root / "records"
            record_root.mkdir()
            (record_root / f"{linked.task_id}.ledger.json").write_text(
                json.dumps(linked_ledger), encoding="utf-8"
            )
            updated = catalog.register_completed(
                linked,
                linked_ledger,
                expected_revision=int(linked.metadata.get("catalog_revision") or 0),
            )
            state = DecisionStateStore(root / "decisions").load(linked.task_id)
            snapshot = RecoveryService(
                catalog_root=root / "catalog", record_root=record_root
            ).diagnose(state, linked)

            provider_ids = {
                str(item.get("provider_id")) for item in updated.get("providers", [])
            }
            self.assertEqual({"9001", "9002"}, provider_ids)
            self.assertEqual("explicit_user", linked.metadata.get("catalog_link_decision"))
            self.assertEqual(base["work_key"], linked.metadata.get("catalog_work_key"))
            self.assertEqual("healthy", snapshot.state)
            self.assertTrue(
                catalog.materializes_completed_plan(updated, linked, linked_ledger)
            )

    def test_movie_manifest_is_closed_null_season_and_rejects_tv_authority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _source, plan, _store, _service = self._draft_movie(
                root=root,
                task_id="manifest1",
                font_name="Example Work [Fonts].7z",
            )
            problems = []
            manifest = plan.metadata.get("associated_asset_manifest")
            if not isinstance(manifest, Mapping):
                problems.append("production movie Plan did not freeze an associated font manifest")
            else:
                if manifest.get("schema_version") != 1:
                    problems.append("movie font manifest schema_version is not frozen to 1")
                try:
                    PlanExecutionService._validate_frozen_associated_assets(plan)
                except PlanExecutionError as exc:
                    problems.append(f"valid movie manifest was rejected: {exc}")
                assets = [dict(item) for item in manifest.get("assets", [])]
                work_key = str(
                    plan.metadata.get("catalog_work_key")
                    or plan.metadata.get("proposed_work_key")
                )
                if manifest.get("catalog_binding") != {
                    "backend": "work",
                    "key": work_key,
                }:
                    problems.append("movie manifest is not bound to the exact WorkCatalog record")
                if len(assets) != 1:
                    problems.append("movie manifest is not the exact one-font closed set")
                else:
                    invalid_row = dict(assets[0])
                    invalid_row["season_number"] = 1
                    invalid_metadata = dict(plan.metadata)
                    invalid_metadata["associated_asset_manifest"] = {
                        **manifest, "assets": [invalid_row],
                    }
                    invalid_metadata["season_number"] = 1
                    invalid_plan = replace(plan, metadata=invalid_metadata)
                    with self.assertRaises(PlanExecutionError):
                        PlanExecutionService._validate_frozen_associated_assets(invalid_plan)
                    missing_metadata = dict(plan.metadata)
                    missing_metadata["associated_asset_manifest"] = {
                        **manifest, "assets": [],
                    }
                    with self.assertRaises(PlanExecutionError):
                        PlanExecutionService._validate_frozen_associated_assets(
                            replace(plan, metadata=missing_metadata)
                        )
                    non_font = next(
                        item for item in plan.entries
                        if not is_font_archive(item.source_path)
                    )
                    extra_row = {
                        **assets[0],
                        "source_path": str(non_font.source_path),
                        "target_path": str(non_font.target_path),
                        "source_size": non_font.source_path.stat().st_size,
                        "source_sha256": _sha256(non_font.source_path),
                    }
                    extra_metadata = dict(plan.metadata)
                    extra_metadata["associated_asset_manifest"] = {
                        **manifest, "assets": [assets[0], extra_row],
                    }
                    with self.assertRaises(PlanExecutionError):
                        PlanExecutionService._validate_frozen_associated_assets(
                            replace(plan, metadata=extra_metadata)
                        )
                    for key, value in (
                        ("season_id", 1),
                        ("series_key", "series-foreign"),
                        ("season_binding_id", "binding-foreign"),
                        ("provider_link_snapshot", {"foreign": True}),
                    ):
                        contaminated = dict(plan.metadata)
                        contaminated[key] = value
                        with self.subTest(tv_authority=key):
                            with self.assertRaises(PlanExecutionError):
                                PlanExecutionService._validate_frozen_associated_assets(
                                    replace(plan, metadata=contaminated)
                                )
                if manifest.get("scope") != "work":
                    problems.append("movie manifest top-level scope is not work")
                if {group.kind for group in plan.presentation_groups} != {
                    "movie_continuous"
                }:
                    problems.append("movie manifest Plan has a non-movie presentation group")
            self.assertEqual([], problems, "\n".join(problems))

    def test_work_catalog_r_plus_one_foreign_materialization_is_not_healthy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _source, first, store, _service = self._draft_movie(
                root=root,
                task_id="recover1",
                font_name="Example Work [Fonts].7z",
            )
            record_root = root / "records"
            record_root.mkdir()
            catalog = WorkCatalog(root / "catalog")

            def materialize(plan):
                rows = []
                for entry in plan.entries:
                    collision = entry.collision_resolution
                    if collision is None:
                        entry.target_path.parent.mkdir(parents=True, exist_ok=True)
                        os.link(entry.source_path, entry.target_path)
                        status = "created"
                        action = "create"
                        transfer_type = "hardlink"
                    else:
                        status = "reused_identical_asset"
                        action = "reuse_existing"
                        transfer_type = "reuse_existing"
                    stat = entry.target_path.stat()
                    rows.append({
                        "source": str(entry.source_path),
                        "target": str(entry.target_path),
                        "status": status,
                        "action": action,
                        "transfer_type": transfer_type,
                        "source_device": entry.source_path.stat().st_dev,
                        "source_inode": entry.source_path.stat().st_ino,
                        "target_device": stat.st_dev,
                        "target_inode": stat.st_ino,
                        "target_link_count": stat.st_nlink,
                    })
                ledger = {
                    "task_id": plan.task_id,
                    "plan_fingerprint": PlanStore.fingerprint(plan),
                    "status": "completed",
                    "entries": rows,
                }
                (record_root / f"{plan.task_id}.ledger.json").write_text(
                    json.dumps(ledger), encoding="utf-8"
                )
                return ledger

            first_ledger = materialize(first)
            base = catalog.register_completed(
                first, first_ledger, expected_revision=0
            )
            _source2, pending, pending_store, _pending_service = self._draft_movie(
                root=root,
                task_id="recover2",
                font_name="Different Release Name [Fonts].7z",
                video_name="Example Work [2160p_SDR].mkv",
            )
            pending_ledger = materialize(pending)
            pending_state = DecisionStateStore(root / "decisions").load(
                pending.task_id
            )
            recovery = RecoveryService(
                catalog_root=root / "catalog",
                record_root=record_root,
            )
            problems = []
            pending_snapshot = recovery.diagnose(pending_state, pending)
            if pending_snapshot.state != "catalog_update_pending":
                problems.append("revision r was not diagnosed as catalog_update_pending")

            exact = catalog.register_completed(
                pending,
                pending_ledger,
                expected_revision=int(pending.metadata.get("catalog_revision") or 0),
            )
            exact_snapshot = recovery.diagnose(pending_state, pending)
            if exact_snapshot.state != "healthy":
                problems.append("exact revision r+1 materialization was not healthy")

            provider_only = json.loads(json.dumps(exact))
            provider_only["providers"] = [{
                **provider_only["providers"][0],
                "provider_id": "foreign-provider-only",
            }]
            catalog_path = root / "catalog" / f"{exact['work_key']}.json"
            catalog_path.write_text(json.dumps(provider_only), encoding="utf-8")
            if recovery.diagnose(pending_state, pending).state != "catalog_corrupt":
                problems.append("provider-only foreign r+1 materialization was not rejected")

            version_only = json.loads(json.dumps(exact))
            version_only["versions"] = [
                item for item in version_only.get("versions", [])
                if item.get("task_id") != pending.task_id
            ]
            catalog_path.write_text(json.dumps(version_only), encoding="utf-8")
            if recovery.diagnose(pending_state, pending).state != "catalog_corrupt":
                problems.append("version-only foreign r+1 materialization was not rejected")

            foreign_r = json.loads(json.dumps(base))
            foreign_r["providers"] = [{
                **foreign_r["providers"][0],
                "provider_id": "foreign-provider-at-r",
            }]
            catalog_path.write_text(json.dumps(foreign_r), encoding="utf-8")
            if recovery.diagnose(pending_state, pending).state != "catalog_corrupt":
                problems.append("foreign revision r base authority was offered as recoverable")

            foreign = dict(exact)
            foreign["versions"] = [
                item for item in exact.get("versions", [])
                if item.get("task_id") != pending.task_id
            ]
            foreign["assets"] = [
                {
                    **item,
                    "associations": [
                        association
                        for association in item.get("associations", [])
                        if association.get("task_id") != pending.task_id
                    ],
                }
                for item in exact.get("assets", [])
            ]
            catalog_path.write_text(json.dumps(foreign), encoding="utf-8")
            foreign_snapshot = recovery.diagnose(pending_state, pending)
            if foreign_snapshot.state != "catalog_corrupt":
                problems.append("foreign revision r+1 materialization was not rejected")
            too_new = dict(exact)
            too_new["revision"] = int(exact.get("revision") or 0) + 1
            catalog_path.write_text(json.dumps(too_new), encoding="utf-8")
            too_new_snapshot = recovery.diagnose(pending_state, pending)
            if too_new_snapshot.state != "catalog_corrupt":
                problems.append("revision r+2 materialization was not rejected")
            self.assertEqual([], problems, "\n".join(problems))

    def test_same_plan_multi_font_assignment_is_order_independent_and_duplicates_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)

            def complete(root: Path, task_id: str, alpha: bytes, beta: bytes):
                source = root / "source"
                source.mkdir(parents=True)
                main = _write(source / "Example Work.mkv", b"movie")
                _write(source / "Alpha [Fonts].7z", alpha)
                _write(source / "Beta [Fonts].7z", beta)
                task = {
                    "uuid": task_id,
                    "path": str(source),
                    "name": "Example Work",
                    "is_anime": True,
                    "is_movie": True,
                    "movie_inventory": inventory_movie_assets(source).to_dict(),
                    "draft_target_root": str(root / "library"),
                    "draft_initial_target_root": str(
                        root / "library" / "AnimeMovies" / "Example Work (2026)"
                    ),
                    "provider_identity": {
                        "namespace": "tmdb", "entity_type": "movie",
                        "provider_id": task_id, "title": "Example Work", "year": 2026,
                    },
                    "proposed_work_key": (
                        "work-12345678-1234-1234-1234-" + task_id[-1] * 12
                    ),
                }
                draft = build_inventory_review_draft(task, dry_run=False)
                store = PlanStore(root / "plans")
                draft = save_plan_fixture(store, draft)
                plan = PlanReviewService(
                    store, root / "reviews", root / "catalog"
                ).complete_inventory_draft(
                    task_id,
                    store.fingerprint(draft),
                    group_id=draft.presentation_groups[0].group_id,
                    group_kind="movie_continuous",
                    target_root=root / "library" / "AnimeMovies",
                    reason="RED deterministic multi-font assignment",
                    root_snapshot=routing_snapshot_for(
                        root / "library" / "AnimeMovies",
                        domain="animation", group_kind="movie_continuous",
                    ),
                    primary_video_source=main,
                )
                return plan

            first = complete(base / "first", "multi1", b"font-z", b"font-a")
            reversed_payload = complete(
                base / "second", "multi2", b"font-a", b"font-z"
            )
            duplicate = complete(
                base / "duplicate", "multi3", b"same-font", b"same-font"
            )

            def digest_targets(plan):
                return {
                    (
                        _sha256(entry.source_path),
                        entry.source_path.stat().st_size,
                    ): entry.target_path.name
                    for entry in plan.entries if is_font_archive(entry.source_path)
                }

            first_map = digest_targets(first)
            second_map = digest_targets(reversed_payload)
            smallest = min(first_map)
            largest = max(first_map)
            expected_primary = "Example Work (2026) [Fonts].7z"
            expected_secondary = (
                f"Example Work (2026) [{largest[0]}] [Fonts].7z"
            )
            problems = []
            if first_map != second_map:
                problems.append("digest-to-target mapping changed when source names/payload order changed")
            if first_map.get(smallest) != expected_primary:
                problems.append("stable smallest content identity did not receive the primary name")
            if first_map.get(largest) != expected_secondary:
                problems.append("secondary content did not receive the full-digest suffix")
            if PlanVerifier().verify(duplicate).status not in {
                PlanStatus.INVALID, PlanStatus.FAIL_CLOSED,
            }:
                problems.append("two source archives with one content identity were not rejected")
            self.assertEqual([], problems, "\n".join(problems))

    def test_legacy_pending_recovery_backfills_font_identity_and_replays_byte_stably(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _source, plan, store, _service = self._draft_movie(
                root=root,
                task_id="legacy1",
                font_name="Legacy Release [Fonts].7z",
            )
            for entry in plan.entries:
                entry.target_path.parent.mkdir(parents=True, exist_ok=True)
                os.link(entry.source_path, entry.target_path)
            fingerprint = store.fingerprint(plan)
            ledger = {
                "task_id": plan.task_id,
                "plan_fingerprint": fingerprint,
                "status": "completed",
                "entries": [
                    {
                        "source": str(entry.source_path),
                        "target": str(entry.target_path),
                        "status": "created",
                        "action": "create",
                        "transfer_type": "hardlink",
                    }
                    for entry in plan.entries
                ],
            }
            task_root = root / "tasks"
            record_root = root / "records"
            catalog_root = root / "catalog"
            task_root.mkdir(exist_ok=True)
            record_root.mkdir()
            catalog_root.mkdir(exist_ok=True)
            (task_root / f"{plan.task_id}.json").write_text(
                json.dumps({
                    "uuid": plan.task_id,
                    "workflow_status": "catalog_update_pending",
                    "execution_status": "catalog_update_pending",
                }),
                encoding="utf-8",
            )
            (record_root / f"{plan.task_id}.ledger.json").write_text(
                json.dumps(ledger), encoding="utf-8"
            )
            font = next(item for item in plan.entries if is_font_archive(item.source_path))
            work_key = str(plan.metadata.get("proposed_work_key"))
            legacy = {
                "schema_version": 1,
                "work_key": work_key,
                "revision": 0,
                "title": plan.work_identity.title,
                "year": plan.work_identity.year,
                "domain": plan.work_identity.domain,
                "group_kind": "movie_continuous",
                "canonical_root": str(plan.metadata.get("canonical_work_root")),
                "aliases": [],
                "providers": [plan.work_identity.primary_provider.to_dict()],
                "versions": [],
                "assets": [{
                    "target": str(font.target_path),
                    "source": str(font.source_path),
                    "task_id": plan.task_id,
                    "media_kind": "associated",
                    "role": "font",
                }],
            }
            catalog_path = catalog_root / f"{work_key}.json"
            catalog_path.write_text(json.dumps(legacy), encoding="utf-8")
            state = DecisionStateStore(root / "decisions").load(plan.task_id)
            recovery = RecoveryService(
                catalog_root=catalog_root,
                record_root=record_root,
            )
            tracking_catalog = _LockTrackingWorkCatalog(catalog_root)
            problems = []
            initial_snapshot = recovery.diagnose(state, plan)
            if initial_snapshot.state != "catalog_update_pending":
                problems.append(
                    "rev=r legacy materialization was not diagnosed as catalog_update_pending"
                )

            task_path = task_root / f"{plan.task_id}.json"
            pending_task_bytes = task_path.read_bytes()
            legacy_catalog_bytes = catalog_path.read_bytes()
            drifted_metadata = dict(plan.metadata)
            drifted_metadata.update({
                "catalog_work_key": "work-ffffffff-ffff-ffff-ffff-ffffffffffff",
                "proposed_work_key": "work-ffffffff-ffff-ffff-ffff-ffffffffffff",
            })
            drifted_plan = replace(plan, metadata=drifted_metadata)
            try:
                with patch.object(store, "load", side_effect=[plan, drifted_plan]):
                    reconcile_catalog_update(
                        plan.task_id,
                        task_root=task_root,
                        record_root=record_root,
                        plan_store=store,
                        work_catalog=tracking_catalog,
                    )
            except ExecutionRecoveryError:
                pass
            else:
                problems.append("Plan work-key drift while waiting for the lock was not rejected")
            if task_path.read_bytes() != pending_task_bytes:
                problems.append("Plan work-key drift changed the pending task")
            if catalog_path.read_bytes() != legacy_catalog_bytes:
                problems.append("Plan work-key drift changed WorkCatalog bytes")

            original_lock = tracking_catalog.lock

            @contextmanager
            def task_state_drift_lock(key: str):
                with original_lock(key):
                    changed = json.loads(pending_task_bytes.decode("utf-8"))
                    changed.update({
                        "workflow_status": "abandoned",
                        "execution_status": "abandoned",
                        "operator_note": "concurrent task-state sentinel",
                    })
                    task_path.write_text(json.dumps(changed), encoding="utf-8")
                    yield

            try:
                with patch.object(tracking_catalog, "lock", new=task_state_drift_lock):
                    reconcile_catalog_update(
                        plan.task_id,
                        task_root=task_root,
                        record_root=record_root,
                        plan_store=store,
                        work_catalog=tracking_catalog,
                    )
            except ExecutionRecoveryError:
                pass
            else:
                problems.append("concurrent task-state drift was overwritten by recovery")
            drifted_task = json.loads(task_path.read_text("utf-8"))
            if drifted_task.get("operator_note") != "concurrent task-state sentinel":
                problems.append("recovery overwrote the concurrent task authority")
            if catalog_path.read_bytes() != legacy_catalog_bytes:
                problems.append("concurrent task-state drift changed WorkCatalog bytes")
            task_path.write_bytes(pending_task_bytes)

            original_diagnose = recovery.diagnose

            def diagnose_under_shared_lock(*args, **kwargs):
                if tracking_catalog.enforce_recovery_lock and tracking_catalog.lock_depth == 0:
                    tracking_catalog.lock_violations.append("diagnose")
                return original_diagnose(*args, **kwargs)

            recovery.diagnose = diagnose_under_shared_lock
            tracking_catalog.enforce_recovery_lock = True
            try:
                recovery.rebuild_catalog(
                    state,
                    plan,
                    tracking_catalog,
                )
            except ValueError as exc:
                problems.append(f"RecoveryService rebuild entry rejected trusted legacy evidence: {exc}")

            rebuilt_bytes = catalog_path.read_bytes()
            rebuilt_payload = json.loads(rebuilt_bytes.decode("utf-8"))
            if int(rebuilt_payload.get("revision", -1)) != 1:
                problems.append("RecoveryService did not materialize the exact r+1 Catalog")

            first = reconcile_catalog_update(
                plan.task_id,
                task_root=task_root,
                record_root=record_root,
                plan_store=store,
                work_catalog=tracking_catalog,
            )
            first_bytes = catalog_path.read_bytes()
            if first_bytes != rebuilt_bytes:
                problems.append("r+1 exact pending reconciliation rewrote the WorkCatalog")
            font_assets = [
                item for item in first.get("assets", [])
                if str(item.get("target")) == str(font.target_path)
            ]
            if len(font_assets) != 1 or not font_assets[0].get("content_key"):
                problems.append("trusted legacy recovery did not backfill the font content identity")
            completed_task = json.loads(
                (task_root / f"{plan.task_id}.json").read_text("utf-8")
            )
            if completed_task.get("execution_status") != "completed":
                problems.append("r+1 exact pending reconciliation did not complete the task")
            try:
                reconcile_catalog_update(
                    plan.task_id,
                    task_root=task_root,
                    record_root=record_root,
                    plan_store=store,
                    work_catalog=tracking_catalog,
                )
            except ExecutionRecoveryError:
                pass
            else:
                problems.append("terminal completed recovery unexpectedly remained actionable")
            if catalog_path.read_bytes() != first_bytes:
                problems.append("terminal recovery rejection changed the materialized WorkCatalog")
            if tracking_catalog.lock_violations:
                problems.append(
                    "recovery diagnosed or registered outside the shared work lock: "
                    + ",".join(tracking_catalog.lock_violations)
                )
            self.assertEqual([], problems, "\n".join(problems))

    def test_non_font_movie_associated_assets_do_not_require_font_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _source, plan, _store, _service = self._draft_movie(
                root=root,
                task_id="control1",
                font_name=None,
            )
            self.assertFalse(any(is_font_archive(item.source_path) for item in plan.entries))
            PlanExecutionService._validate_frozen_associated_assets(plan)

    def test_font_archive_scope_is_exact_and_does_not_capture_other_associated_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exact = _write(root / "Example [Fonts].7z", b"fonts")
            controls = (
                _write(root / "Example Fonts.7z", b"not-exact"),
                _write(root / "Example [Fonts].zip", b"wrong-suffix"),
                _write(root / "audio.mka", b"audio"),
                _write(root / "subtitle.ass", b"subtitle"),
                _write(root / "Associated.7z", b"ordinary-associated"),
            )
            self.assertTrue(is_font_archive(exact))
            self.assertFalse(any(is_font_archive(item) for item in controls))


if __name__ == "__main__":
    unittest.main()
