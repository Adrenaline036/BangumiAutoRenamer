"""无字体依赖的隔离媒体及真实队列入口夹具。"""
import asyncio
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from tests import test_r1_real_media_production_red_contract as r1_fixture
from tests._approval_stage_helpers import approve_dialog
import src.pages.plan_review_page as review_page
import src.utils.utils as utils_module
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.plan_execution import PlanExecutionService
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.decision_state import DecisionStateStore, DecisionCommandService
import hashlib
from src.rename.series_catalog import SeriesCatalogError
from src.rename.target_routing import TargetRootSnapshot
from src.task_queue import TaskQueueManager


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path

def _make_version(
    root: Path,
    resolution: str,
    *,
    font_bytes: bytes = b"violet-shared-font-package",
) -> tuple[Path, Path, Path]:
    source = root / (
        "[VCB-Studio] Gekijouban Violet Evergarden "
        f"[Ma10p_{resolution}]"
    )
    main = _write(
        source / (
            "[VCB-Studio] Gekijouban Violet Evergarden "
            f"[Ma10p_{resolution}][x265_flac].mkv"
        ),
        f"violet-main-{resolution}".encode("ascii"),
    )
    stem = (
        "[VCB-Studio] Gekijouban Violet Evergarden "
        f"[Ma10p_{resolution}][x265_flac]"
    )
    _write(source / f"{stem}.mka", f"violet-audio-{resolution}".encode("ascii"))
    _write(source / f"{stem}.sc.ass", f"violet-subtitle-{resolution}".encode("ascii"))
    for label in ("CM01", "CM02", "Menu"):
        _write(
            source / "SPs" / f"{stem} [{label}].mkv",
            f"violet-{resolution}-{label}".encode("ascii"),
        )
    font = _write(
        source / "[VCB-Studio] Gekijouban Violet Evergarden [Fonts].7z",
        font_bytes,
    )
    return source, main, font


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

class MediaFlow(unittest.IsolatedAsyncioTestCase):
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
            patch("src.rename.target_routing.snapshot_from_config_manager",
                return_value=TargetRootSnapshot.capture({"bangumi_path": root / "TV",
                    "movie_path": root / "Movies", "anime_path": root / "Anime",
                    "anime_movie_path": root / "AnimeMovies"})),
        ):
            return await r1_fixture.R1RealQueueWorkerContracts._run_flow(
                harness,
                root=root,
                source=source,
                task_id=task_id,
                candidate=_movie_candidate(),
                is_anime=True,
                is_movie=True,
            )

    async def _approve_and_execute(
        self,
        *,
        root: Path,
        result: SimpleNamespace,
        fail_series_registration: bool = False,
    ) -> tuple[dict, dict, PlanExecutionService]:
        store = PlanStore(root / "plans")
        review = PlanReviewService(
            store,
            root / "reviews",
            root / "work_catalog",
            decision_state_root=root / "decisions",
            task_root=root / "tasks",
            provider_link_catalog_root=root / "provider_links",
            record_root=root / "records",
        )
        plan = store.load(result.plan.task_id)
        result.plan = plan
        result.state = review.decision_state_store.load(plan.task_id)
        result.task = json.loads((root/'tasks'/f'{plan.task_id}.json').read_text('utf-8'))
        fingerprint = store.fingerprint(plan)
        dialog = SimpleNamespace(
            review_service=review,
            plan=plan,
            selected_ids={entry_id(item) for item in plan.entries},
            task_data=dict(result.task),
            decision=None,
            review_badge=SimpleNamespace(text=""),
            _refresh_plan_header=MagicMock(),
            _render_entries=MagicMock(),
            _refresh_execute_button=MagicMock(),
        )
        with (
            patch.object(utils_module, "TASK_PATH", root / "tasks"),
            patch.object(review_page, "notify") as approval_notices,
        ):
            await approve_dialog(dialog, tuple(entry_id(item) for item in plan.entries))
            self.assertEqual('approved', json.loads((root/'tasks'/f'{plan.task_id}.json').read_text('utf-8')).get('workflow_status'),
                [str(call.args) for call in approval_notices.call_args_list])

        service = PlanExecutionService(
            store,
            review,
            task_root=root / "tasks",
            record_root=root / "records",
            catalog_root=root / "work_catalog",
            series_catalog_root=root / "series_catalog",
            decision_state_root=root / "decisions",
            recovery_transaction_root=root / "recovery_transactions",
            provider_link_catalog_root=root / "provider_links",
        )
        manager = TaskQueueManager(
            task_root=root / "tasks",
            execution_service=service,
            decision_state_root=root / "decisions",
            recovery_abandon_audit_root=root / "abandon_audit",
            plan_root=root / "plans",

        )
        await manager.start()
        try:
            registration_patch = (
                patch.object(
                    service.series_catalog,
                    "register_completed",
                    side_effect=SeriesCatalogError(
                        "RED synthetic SeriesCatalog failure"
                    ),
                )
                if fail_series_registration
                else nullcontext()
            )
            expected_failure_log = (
                patch("src.task_queue.logger.exception")
                if fail_series_registration
                else nullcontext()
            )
            with (
                registration_patch,
                expected_failure_log as failure_log,
                patch(
                    "src.rename.plan_execution.is_dry_run_enabled",
                    return_value=False,
                ),
            ):
                await manager.enqueue_approved(plan.task_id, fingerprint)
                await asyncio.wait_for(manager._queue.join(), timeout=60)
            if fail_series_registration:
                self.assertEqual(1, failure_log.call_count)
        finally:
            await manager.stop()

        task = json.loads(
            (root / "tasks" / f"{plan.task_id}.json").read_text("utf-8")
        )
        ledger_path = root / "records" / f"{plan.task_id}.ledger.json"
        ledger = (
            json.loads(ledger_path.read_text("utf-8"))
            if ledger_path.is_file()
            else {}
        )
        return task, ledger, service

    async def _complete_movie(
        self,
        *,
        root: Path,
        result: SimpleNamespace,
        primary: Path | tuple[Path, ...],
        version_groups=None,
        restore_sources=(),
    ) -> SimpleNamespace:
        primary_sources = primary if isinstance(primary, tuple) else (primary,)
        store = PlanStore(root / "plans")
        review = PlanReviewService(
            store,
            root / "reviews",
            root / "work_catalog",
            decision_state_root=root / "decisions",
            task_root=root / "tasks",
            record_root=root / "records",
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

        snapshot = TargetRootSnapshot.capture({
            "bangumi_path": root / "TV",
            "movie_path": root / "Movies",
            "anime_path": root / "Anime",
            "anime_movie_path": root / "AnimeMovies",
        })
        from src.rename.initial_movie_completion import RootSnapshotSource
        review.root_snapshot_source = RootSnapshotSource(lambda: snapshot)
        if restore_sources:
            if dialog.task_decision.content_selection is None:
                self.assertTrue(set(restore_sources) <= {e.source_path for e in dialog.plan.entries},
                    'explicit historical fixture must already retain requested companions')
            else:
                from tests.test_content_choice_contract import change
                dialog.plan = change(review, dialog.plan, restore_sources, action='restore')
                dialog.task_decision = review.decision_state_store.load(dialog.plan.task_id)
        from nicegui import ui as real_ui
        from nicegui.client import Client
        from nicegui.page import page as gui_page
        from tests._gui_helpers import GuiDriver
        client = Client(gui_page('/isolated-movie-completion'), request=None)
        self.addCleanup(client.delete)
        dialog.client = client
        driver = GuiDriver('runTest')
        driver.client = client
        with (
            client,
            patch.object(utils_module, "TASK_PATH", root / "tasks"),
            patch.object(review_page, "TASK_PATH", root / "tasks"),
            patch.object(review_page, "notify") as completion_notice,
            patch.object(review_page, "ui", real_ui),
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
            # 打开当前工具行使用的同一版本表单；本夹具验证保存与登记。
            PlanReviewDialog._edit_movie_version_groups(dialog, dialog.plan.presentation_groups[0].group_id)
            # Set the actual form controls, then dispatch its registered save.
            # This fixture owns these exact filenames; ambiguous rows are errors.
            selected_names = {item.name for item in primary_sources}
            picker = next(e for e in client.elements.values()
                if e._props.get('label') == '电影正片文件（可多选）')
            picker.value = [key for key in picker.options if Path(key).name in selected_names]
            matched = {Path(key).name for key in picker.value}
            if matched != selected_names:
                raise AssertionError('requested primary is not represented by actual movie form')
            if version_groups is not None:
                elements = list(client.elements.values())
                for index, element in enumerate(elements):
                    if element._props.get('label') != '版本标签':
                        continue
                    label = next(item for item in reversed(elements[:index]) if type(item).__name__ == 'Label')
                    names = set(label.text.split(' / '))
                    groups = [row for row in version_groups if {Path(p).name for p in row['sources']} == names]
                    if len(groups) != 1:
                        raise AssertionError('fixture version group is not one actual form row')
                    element.value = groups[0]['label']
            await driver.click('保存正片版本')
        return SimpleNamespace(
            plan=store.load(result.plan.task_id),
            state=DecisionStateStore(root / "decisions").load(result.plan.task_id),
            task=json.loads(
                (root / "tasks" / f"{result.plan.task_id}.json").read_text("utf-8")
            ),
            review=review,
            store=store,
            notices=tuple((str(call.args[0]),call.kwargs.get('type'))
                          for call in completion_notice.call_args_list if call.args),
            form_messages=tuple(element.text for element in client.elements.values()
                if type(element).__name__ == 'Label' and element.text),
            replacement_controls=tuple((element.visible, element.value)
                for element in client.elements.values() if type(element).__name__ == 'Select'
                and element.props.get('label') == '已有版本换源'),
        )

    async def _run_flow(
        self,
        *,
        root: Path,
        source: Path,
        task_id: str,
        subject_id: str,
        confirm_fixture_ova: bool = True,
        confirm_fixture_extras: tuple[Path, ...] = (),
    ) -> SimpleNamespace:
        harness = SimpleNamespace(
            _NoopReviewDialog=(
                r1_fixture.R1RealQueueWorkerContracts._NoopReviewDialog
            )
        )
        with (
            patch(
                "src.rename.process.SERIES_CATALOG_PATH",
                root / "series_catalog",
            ),
            patch("src.rename.process.RECORD_PATH", root / "records"),
            patch("src.rename.process.is_dry_run_enabled", return_value=False),
        ):
            result = await r1_fixture.R1RealQueueWorkerContracts._run_flow(
                harness,
                root=root,
                source=source,
                task_id=task_id,
                candidate=r1_fixture._candidate(subject_id),
                is_anime=True,
                is_movie=False,
            )
        # 夹具明确确认已知 OVA，产品不能替用户推断该决定。
        if confirm_fixture_ova or confirm_fixture_extras:
            from src.rename.initial_movie_completion import RootSnapshotSource
            from src.rename.target_routing import TargetRootSnapshot
            roots = TargetRootSnapshot.capture({'bangumi_path': root/'TV', 'movie_path': root/'Movies',
                'anime_path': root/'Anime', 'anime_movie_path': root/'AnimeMovies'})
            review = PlanReviewService(PlanStore(root/'plans'), root/'reviews', root/'work_catalog',
                decision_state_root=root/'decisions', task_root=root/'tasks', record_root=root/'records',
                provider_link_catalog_root=root/'provider_links')
            review.root_snapshot_source = RootSnapshotSource(lambda: roots)
            for item in result.plan.entries:
                if item.media_kind != 'video':
                    continue
                explicit_extra = item.source_path in confirm_fixture_extras
                if not explicit_extra and not (confirm_fixture_ova and '[06.5(OVA)]' in item.source_path.name):
                    continue
                state = review.decision_state_store.load(task_id)
                result.plan = review.revise_tv_content(task_id, PlanStore.fingerprint(result.plan),
                    expected_revision=state.revision, mode='role' if explicit_extra else 'special',
                    edit=({'entry_id': entry_id(item), 'role': 'extra'} if explicit_extra else
                          {'entry_id': entry_id(item), 'action': 'unmatched_extra', 'provider_episode_id': None}))
            result.task = json.loads((root/'tasks'/f'{task_id}.json').read_text('utf-8'))
            result.state = review.decision_state_store.load(task_id)
        return result

    async def _materialize_s1(self, root):
        source, archive = make_season(root)
        result = await self._run_flow(root=root, source=source, task_id='materialized-season-one', subject_id='41658')
        task, ledger, service = await self._approve_and_execute(root=root, result=result)
        self.assertEqual('completed', task.get('execution_status'), task.get('error'))
        self.assertEqual('completed', ledger['status'])
        result.task = task
        result.state = service.decision_state_store.load(result.plan.task_id)
        return result, archive

    async def _completed_pair(
        self,
        root: Path,
        *,
        order: tuple[str, str] = ("1080p", "2160p"),
    ):
        harness = MediaFlow(
            methodName="runTest"
        )
        packages = {
            resolution: _make_version(
                root / "sources",
                resolution,
            )
            for resolution in ("1080p", "2160p")
        }
        before = {
            resolution: _source_manifest(packages[resolution][0])
            for resolution in packages
        }
        completed = {}
        for resolution in order:
            source, primary, _font = packages[resolution]
            task_id = f"violet-{resolution}-{order[0]}-first"
            initial = await harness._run_provider_flow(root, source, task_id)
            completed[resolution] = await harness._complete_movie(
                root=root,
                result=initial,
                primary=primary,
            )
        return harness, packages, before, completed

    async def _approve_without_execute(self, root: Path, completed) -> str:
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
            await approve_dialog(dialog)
        return fingerprint

    @staticmethod
    def _execution_service(root: Path) -> PlanExecutionService:
        completed_store = PlanStore(root / "plans")
        return PlanExecutionService(
            completed_store,
            PlanReviewService(
                completed_store,
                root / "reviews",
                root / "work_catalog",
                decision_state_root=root / 'decisions', task_root=root / 'tasks', record_root=root / 'records',
            ),
            task_root=root / "tasks",
            record_root=root / "records",
            catalog_root=root / "work_catalog",
            series_catalog_root=root / "series_catalog",
            decision_state_root=root / "decisions",
            recovery_transaction_root=root / "recovery_transactions",
            provider_link_catalog_root=root / "provider_links",
        )



async def approved_tv(owner):
    from tests.test_initial_tv_completion_contract import InitialTVCompletionContracts
    from src.rename.plan_execution import PlanExecutionService
    from src.rename.plan_store import PlanStore
    from src.rename.plan_review import entry_id
    harness = InitialTVCompletionContracts('runTest')
    owner.addCleanup(harness.doCleanups)
    data, review, _, result, *_ = await harness.fixture(anime=False)
    plan = result.plan
    fingerprint = PlanStore.fingerprint(plan)
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from tests._approval_stage_helpers import approve_dialog
    from src.pages import plan_review_page
    from src.utils import utils
    dialog = SimpleNamespace(review_service=review, plan=plan,
        task_data=result.task, decision=None, review_badge=SimpleNamespace(text=''),
        _refresh_plan_header=MagicMock(), _render_entries=MagicMock(),
        _refresh_execute_button=MagicMock())
    with patch.object(utils, 'TASK_PATH', data/'tasks'), patch.object(plan_review_page, 'notify'):
        await approve_dialog(dialog, tuple(map(entry_id, plan.entries)))
    task = json.loads((data/'tasks'/f'{plan.task_id}.json').read_text('utf-8'))
    owner.assertEqual('approved', task['workflow_status'])
    service = PlanExecutionService(review.plan_store, review,
        task_root=data/'tasks', record_root=data/'records',
        catalog_root=data/'work_catalog', series_catalog_root=data/'series_catalog',
        decision_state_root=data/'decisions', recovery_transaction_root=data/'recovery',
        provider_link_catalog_root=data/'provider_links')
    return service,plan,fingerprint


def _source_manifest(root: Path) -> dict[str, tuple[int, str]]:
    return {
        str(path.relative_to(root)): (path.stat().st_size, _sha256(path))
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }

def _work_key(plan) -> str:
    return str(
        plan.metadata.get("catalog_work_key")
        or plan.metadata.get("proposed_work_key")
        or ""
    )

def file_identity(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_nlink, _sha256(path)

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

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


def make_season(root: Path, *, second=False, archive_bytes=b'excluded archive', archive_name=None):
    stem = '[VCB-Studio] Yama no Susume' + (' Second Season' if second else '')
    source = r1_fixture._make_season(root/'sources', stem+' [Ma10p_1080p]', stem, ova=second)
    archive = source/(archive_name or stem+' [Fonts].7z')
    archive.write_bytes(archive_bytes)
    return source, archive

def revise(service, plan, mode, edit=None):
    state = service.decision_state_store.load(plan.task_id)
    return service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
        expected_revision=state.revision, mode=mode, edit=edit or {})
