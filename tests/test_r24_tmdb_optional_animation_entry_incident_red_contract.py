from tests.source_collection_fixture import collected_files, collected_identity
import asyncio
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nicegui.client import Client

from src.pages.data_table_page import _open_review
from src.pages.plan_review_page import (
    PlanReviewDialog,
    normalize_provider_candidates,
)
from src.rename.decision_state import DecisionStateStore
from src.rename.inventory_plan import (
    build_inventory_review_draft,
)
from src.rename.movie_assets import inventory_movie_assets
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import PlanStatus
from src.utils.utils import read_task_at, write_task_at


class _AwaitableDialog:
    def __init__(self) -> None:
        self.delete = Mock()
        self.awaited = False
        self.is_deleted = False
        self.exit_requested = False
        self.value = False
        self.submitted = asyncio.Event()

    def open(self):
        self.value = True

    def _load_plan_view(self, plan, *, snapshot=None):
        self.plan = plan
        self.snapshot = snapshot

    def __await__(self):
        self.awaited = True
        self.exit_requested = True
        async def done():
            return None

        return done().__await__()


class TMDBOptionalAnimationEntryIncidentContract(unittest.TestCase):
    @staticmethod
    def _inventory_task(
        source: Path,
        target: Path,
        *,
        task_id: str = "tmdb-optional-animation-entry",
        is_anime: object = True,
        is_movie: object = False,
    ) -> dict[str, object]:
        return {
            "uuid": task_id,
            "path": str(source),
            "name": "Steins;Gate 0",
            "is_anime": is_anime,
            "is_movie": is_movie,
            "workflow_status": "invalid",
            "plan_status": "inventory_only",
            "execution_blocked": True,
            "movie_inventory": inventory_movie_assets(source).to_dict(),
            "draft_target_root": str(target),
            "draft_initial_target_root": str(target / "Steins;Gate 0"),
            "provider_identity": None,
            "provider_candidates": {},
        }

    @contextmanager
    def _public_entry_environment(
        self,
        task_root: Path,
        plan_root: Path,
        decision_root: Path,
    ):
        with (
            patch("src.pages.plan_review_page.TASK_PATH", task_root),
            patch("src.pages.plan_review_page.PLAN_PATH", plan_root),
            patch("src.pages.plan_review_page.DECISION_STATE_PATH", decision_root),
            patch("src.pages.plan_review_page.task_queue", self.entry_manager),
            patch("src.rename.target_routing.snapshot_from_config_manager", return_value=self.entry_roots),
            patch("src.utils.utils.TASK_PATH", task_root),
            patch("src.pages.plan_review_page.is_dry_run_enabled", return_value=True),
            patch("src.pages.data_table_page._dashboard_summary.refresh"),
            patch("src.pages.data_table_page._review_tasks.refresh"),
            patch("src.pages.data_table_page._history_tasks.refresh"),
            patch("src.pages.plan_review_page.notify"),
        ):
            yield

    @staticmethod
    async def _open_from_ui_slot(task_id: str) -> None:
        with Client.auto_index_client.content:
            async with asyncio.timeout(8):
                await _open_review(task_id)

    @contextmanager
    def _persisted_workspace(
        self,
        task_id: str,
        *,
        is_anime: object = True,
        is_movie: object = False,
        video_names: tuple[str, ...] = ('01.mkv', '02.mkv'),
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            for number, name in enumerate(video_names, 1):
                (source / name).write_bytes(f'video-{number}'.encode('ascii'))
            task_root, plan_root, decision_root = (
                root / "tasks", root / "plans", root / "decisions"
            )
            for path in (task_root, plan_root, decision_root):
                path.mkdir()
            from src.task_queue import TaskQueueManager, TaskJob
            from src.rename.task_batch_scope import BatchTaskSourceRequest
            from src.rename.target_routing import TargetRootSnapshot, ROOT_KEYS

            async def admitted_only(job):
                manager._update(job.task_id, workflow_status='need_confirm')

            manager = TaskQueueManager(task_root=task_root, plan_root=plan_root,
                decision_state_root=decision_root, executor=admitted_only,
                batch_scope_root=root/'task_batch_scope', recovery_abandon_audit_root=root/'abandon')
            self.entry_manager = manager
            self.entry_roots = TargetRootSnapshot.capture({key: root/'roots'/key for key in ROOT_KEYS})

            async def admit():
                request = BatchTaskSourceRequest(task_id, (str(source),), 'parent_directory',
                    tuple(collected_identity(p) for p in collected_files(source)),
                    'movie' if is_movie is True else 'tv')
                try:
                    await manager.enqueue_batch(batch_id='entry-'+task_id, task_requests=(request,),
                        jobs=(TaskJob(task_id, source, is_anime=is_anime, is_movie=is_movie,
                            source='ui', preview_only=False),))
                    await asyncio.wait_for(manager._queue.join(), 5)
                finally:
                    await manager.stop()

            asyncio.run(admit())
            task = manager._read(task_id)
            task.update(self._inventory_task(
                source,
                root / "library",
                task_id=task_id,
                is_anime=is_anime,
                is_movie=is_movie,
            ))
            write_task_at(task_root, task_id, task)
            yield task_root, plan_root, decision_root, task

    def test_empty_animation_tv_builds_unresolved_tv_placeholder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            (source / "01.mkv").write_bytes(b"episode-1")
            (source / "02.mkv").write_bytes(b"episode-2")
            task = self._inventory_task(source, root / "anime")

            plan = build_inventory_review_draft(task, dry_run=True)

        self.assertEqual(
            ["tv-episodes"],
            [group.group_id for group in plan.presentation_groups],
        )
        self.assertIsNotNone(plan.work_identity)
        self.assertEqual("unresolved", plan.work_identity.status)
        self.assertEqual("unresolved", plan.work_identity.primary_provider.status)
        self.assertIsNone(plan.work_identity.primary_provider.namespace)
        self.assertIsNone(plan.work_identity.primary_provider.provider_id)
        self.assertIsNone(task["provider_identity"])
        self.assertEqual({}, task["provider_candidates"])
        self.assertEqual({}, dict(plan.metadata["provider_candidates"]))
        self.assertEqual(PlanStatus.INVALID, PlanVerifier().verify(plan).status)
        self.assertFalse(PlanVerifier().verify(plan).accepted)

    def test_empty_animation_movie_builds_unresolved_movie_placeholder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            (source / "movie.mkv").write_bytes(b"movie")
            task = self._inventory_task(
                source,
                root / "anime-movies",
                is_anime=True,
                is_movie=True,
            )

            plan = build_inventory_review_draft(task, dry_run=True)

        self.assertEqual(
            ["movie-continuous"],
            [group.group_id for group in plan.presentation_groups],
        )
        self.assertIsNotNone(plan.work_identity)
        self.assertEqual("unresolved", plan.work_identity.status)
        self.assertEqual("unresolved", plan.work_identity.primary_provider.status)
        self.assertIsNone(plan.work_identity.primary_provider.namespace)
        self.assertIsNone(plan.work_identity.primary_provider.provider_id)
        self.assertEqual(PlanStatus.INVALID, PlanVerifier().verify(plan).status)
        self.assertFalse(PlanVerifier().verify(plan).accepted)

    def test_task_card_process_persists_empty_decision_and_blocked_plan(self):
        # Historical incident ID retained; opening is now read-only, and the
        # first-link preparation is the explicit publication boundary.
        task_id = "public-empty-provider-animation"
        with self._persisted_workspace(task_id) as (
            task_root, plan_root, decision_root, _task
        ):
            opened = _AwaitableDialog()
            task_before = (task_root / f"{task_id}.json").read_bytes()
            with (
                self._public_entry_environment(task_root, plan_root, decision_root),
                patch(
                    "src.pages.plan_review_page.PlanReviewDialog",
                    return_value=opened,
                ) as dialog_factory,
                patch(
                    "src.pages.plan_review_page.RecognitionRecoveryDialog"
                ) as recovery_factory,
            ):
                asyncio.run(self._open_from_ui_slot(task_id))
                self.assertFalse(PlanStore(plan_root).path_for(task_id).exists())
                self.assertFalse(DecisionStateStore(decision_root).path_for(task_id).exists())
                self.assertEqual(task_before, (task_root / f"{task_id}.json").read_bytes())
                from src.pages.initial_workspace import create_initial_workspace
                asyncio.run(create_initial_workspace(task_id, read_task_at(task_root, task_id)))

            self.assertTrue(PlanStore(plan_root).path_for(task_id).exists())
            decision = DecisionStateStore(decision_root).load(task_id)
            plan = PlanStore(plan_root).load(task_id)
            persisted_task = read_task_at(task_root, task_id)

            recovery_factory.assert_not_called()
            dialog_factory.assert_called_once()
            self.assertEqual(2, len(dialog_factory.call_args.args))
            opened_plan = dialog_factory.call_args.args[0]
            self.assertIsNone(opened_plan)
            self.assertTrue(opened.awaited)
            opened.delete.assert_called_once()
            self.assertEqual(
                [],
                normalize_provider_candidates(dict(plan.metadata)),
            )
            self.assertEqual({}, dict(decision.confirmed_work))
            self.assertIsNone(persisted_task["provider_identity"])
            self.assertEqual({}, persisted_task["provider_candidates"])
        self.assertEqual(0, decision.revision, 'single initial publication has revision zero')
        self.assertEqual('content-selection-v2', decision.content_selection.to_dict()['policy'])
        self.assertEqual('safe-watchable-content-v1', decision.to_dict()['content_policy'])
        self.assertIsNone(plan.work_identity)
        self.assertEqual(PlanStatus.INVALID, PlanVerifier().verify(plan).status)
        self.assertFalse(PlanVerifier().verify(plan).accepted)
        self.assertTrue(persisted_task["execution_blocked"])
        self.assertTrue(persisted_task["inventory_draft_created"])

    def test_reopen_reuses_one_unresolved_revision(self):
        task_id = "restart-empty-provider-animation"
        with self._persisted_workspace(task_id) as (
            task_root, plan_root, decision_root, _task
        ):
            def open_once() -> None:
                with (
                    self._public_entry_environment(
                        task_root, plan_root, decision_root
                    ),
                    patch(
                        "src.pages.plan_review_page.PlanReviewDialog",
                        return_value=_AwaitableDialog(),
                    ),
                ):
                    asyncio.run(self._open_from_ui_slot(task_id))

            open_once()
            self.assertFalse(PlanStore(plan_root).path_for(task_id).exists())
            task_before = (task_root / f"{task_id}.json").read_bytes()
            open_once()
            self.assertEqual(task_before, (task_root / f"{task_id}.json").read_bytes())
            self.assertFalse(DecisionStateStore(decision_root).path_for(task_id).exists())
            from src.pages.initial_workspace import create_initial_workspace
            with self._public_entry_environment(task_root, plan_root, decision_root):
                asyncio.run(create_initial_workspace(task_id, read_task_at(task_root, task_id)))
            first = DecisionStateStore(decision_root).load(task_id)
            first_plan = PlanStore(plan_root).load(task_id)
            first_task_bytes = (task_root / f"{task_id}.json").read_bytes()
            open_once()
            second = DecisionStateStore(decision_root).load(task_id)
            second_plan = PlanStore(plan_root).load(task_id)
            second_task_bytes = (task_root / f"{task_id}.json").read_bytes()

            decision_files = list(decision_root.glob("*.json"))
            plan_files = list(plan_root.glob("*.json"))

        self.assertEqual(0, first.revision)
        self.assertEqual(DecisionStateStore.fingerprint(first), DecisionStateStore.fingerprint(second))
        self.assertEqual(first_task_bytes, second_task_bytes)
        self.assertEqual(
            PlanStore.fingerprint(first_plan),
            PlanStore.fingerprint(second_plan),
        )
        self.assertEqual([f"{task_id}.json"], [item.name for item in decision_files])
        self.assertEqual([f"{task_id}.json"], [item.name for item in plan_files])

    def test_unresolved_groups_expose_manual_bangumi_input(self):
        class _Container:
            def clear(self) -> None:
                return None

            def classes(self, _value: str):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        renderer = SimpleNamespace(
            groups_container=_Container(),
            plan=SimpleNamespace(
                metadata={"provider_candidates": {}, "presentation_assignment_state": "placeholder"}
            ),
            snapshot=SimpleNamespace(
                work_identity={"status": "unresolved"},
                presentation_groups=(),
                linked_provider_identities=(),
            ),
            provider_selection_expanded=False,
            task_data={"is_movie": False},
            _render_selection_operation=Mock(),
            _render_selected_provider_identity=Mock(),
            render_provider_column=Mock(),
            _render_provider_link_input=Mock(),
        )
        element = Mock()
        element.classes.return_value = element
        with (
            patch("src.pages.plan_review_page.ui.label", return_value=element),
            patch("src.pages.plan_review_page.ui.row", return_value=_Container()),
        ):
            PlanReviewDialog.render_groups(renderer)

        self.assertEqual(
            [("bangumi", []), ("tmdb", [])],
            [call.args for call in renderer.render_provider_column.call_args_list],
        )

        provider_renderer = SimpleNamespace(
            _render_provider_candidate=Mock(),
            _select_manual_provider_url=Mock(),
        )
        manual_input = Mock()
        manual_input.props.return_value = manual_input
        manual_input.classes.return_value = manual_input
        label = Mock()
        label.classes.return_value = label
        button = Mock()
        button.props.return_value = button
        button.classes.return_value = button
        with (
            patch("src.pages.plan_review_page.ui.column", return_value=_Container()),
            patch("src.pages.plan_review_page.ui.label", return_value=label) as label_factory,
            patch("src.pages.plan_review_page.ui.input", return_value=manual_input) as input_factory,
            patch("src.pages.plan_review_page.ui.button", return_value=button),
        ):
            PlanReviewDialog._render_provider_link_input(provider_renderer)
            PlanReviewDialog.render_provider_column(provider_renderer, "bangumi", [])

        input_factory.assert_called_once_with(
            label="Bangumi / TMDB 作品详情页链接",
            placeholder="粘贴 bgm.tv/subject 或 themoviedb.org/tv、movie 详情页链接",
        )
        self.assertIs(manual_input, provider_renderer.provider_url_input)
        self.assertIn(
            "自动搜索没有可显示结果；仍可粘贴人工找到的详情页链接。",
            [call.args[0] for call in label_factory.call_args_list],
        )

    def test_mixed_empty_opens_recovery_without_writes(self):
        task_id = "mixed-empty-provider"
        with self._persisted_workspace(
            task_id, is_anime=None, is_movie=None
        ) as (task_root, plan_root, decision_root, _task):
            task_bytes = (task_root / f"{task_id}.json").read_bytes()
            recoveries = [_AwaitableDialog(), _AwaitableDialog()]
            with (
                self._public_entry_environment(task_root, plan_root, decision_root),
                patch(
                    "src.pages.plan_review_page.RecognitionRecoveryDialog",
                ) as recovery_factory,
                patch(
                    "src.pages.plan_review_page.PlanReviewDialog", side_effect=recoveries,
                ) as dialog_factory,
            ):
                asyncio.run(self._open_from_ui_slot(task_id))
                asyncio.run(self._open_from_ui_slot(task_id))

            recovery_factory.assert_not_called()
            self.assertEqual(2, dialog_factory.call_count)
            self.assertTrue(all(call.args[0] is None for call in dialog_factory.call_args_list))
            for recovery in recoveries:
                self.assertTrue(recovery.awaited)
                recovery.delete.assert_called_once()
            self.assertEqual(task_bytes, (task_root / f"{task_id}.json").read_bytes())
            self.assertEqual([], list(plan_root.iterdir()))
            self.assertEqual([], list(decision_root.iterdir()))
