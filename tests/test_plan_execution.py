import hashlib
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import (
    AssetRole,
    CollisionResolution,
    ExecutionPolicy,
    PlanEntry,
    PresentationAssetPlanContract,
    PresentationGroup,
    ProviderIdentity,
    RenamePlan,
    SubtitlePlanContract,
    UnknownTokenAction,
    UnknownTokenDisposition,
    WorkIdentity,
    build_default_version_descriptor,
)
from src.rename.asset_collision import build_reuse_resolution
from src.rename.decision_state import DecisionStateStore
from src.rename.subtitle_labels import (
    DimensionResolution,
    SUBTITLE_ASSOCIATION_RULE_VERSION,
    SUBTITLE_LABEL_RULE_VERSION,
)
from tests.presentation_fixtures import with_tv_presentation
from tests.projection_test_support import save_plan_fixture


class PlanExecutionTests(unittest.TestCase):
    def test_execution_creates_new_hardlink_and_reuses_approved_identical_asset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            source_root.mkdir()
            video = source_root / "Episode.mkv"
            font = source_root / "Fonts.7z"
            video.write_bytes(b"video")
            font.write_bytes(b"fonts")
            target_root = root / "library"
            video_target = target_root / "Season 01" / "Example S01E01.mkv"
            font_target = target_root / "Associated" / "Fonts.7z"
            font_target.parent.mkdir(parents=True)
            font_target.write_bytes(b"fonts")
            collision = build_reuse_resolution(font, font_target)
            self.assertIsNotNone(collision)
            group = PresentationGroup("tv-episodes", "tv_episodes", "TV 分集版", None)
            plan = RenamePlan(
                task_id="mixed-create-reuse",
                source_root=source_root,
                entries=(
                    PlanEntry(
                        video, video_target, season_id=1, episode_id=1,
                        media_kind="video", role="main_episode", requires_review=True,
                        presentation_asset=PresentationAssetPlanContract(
                            group.group_id, AssetRole("content_version"), None,
                            build_default_version_descriptor(group.group_id, 1, 1),
                        ),
                    ),
                    PlanEntry(
                        font, font_target, media_kind="associated", role="font",
                        requires_review=True,
                        presentation_asset=PresentationAssetPlanContract(
                            group.group_id, AssetRole("associated"), None, None,
                        ),
                        collision_resolution=collision,
                    ),
                ),
                expected_sources=(video, font),
                metadata={
                    "target_root": str(target_root),
                    "season_number": 1,
                    "associated_asset_manifest": {
                        "schema_version": 1,
                        "assets": [{
                            "role": "fonts",
                            "scope": "series",
                            "season_number": 1,
                            "source_path": str(font),
                            "target_path": str(font_target),
                            "source_size": font.stat().st_size,
                            "source_sha256": hashlib.sha256(
                                font.read_bytes()
                            ).hexdigest(),
                        }],
                    },
                },
                execution_policy=ExecutionPolicy(target_root=target_root, dry_run=False),
                presentation_groups=(group,),
                work_identity=WorkIdentity(
                    "confirmed", "animation", "Example", 2026,
                    ProviderIdentity(
                        "confirmed", "bangumi", "subject", "1", "Example", 2026,
                        "explicit_user",
                    ),
                    "provider_selection",
                ),
            )
            task_root = root / "tasks"
            task_root.mkdir()
            (task_root / f"{plan.task_id}.json").write_text(json.dumps({
                "uuid": plan.task_id, "path": str(source_root),
                "workflow_status": "approved", "completed_at": None,
            }), encoding="utf-8")
            store = PlanStore(root / "plans")
            plan = save_plan_fixture(store, plan)
            review = PlanReviewService(store, root / "reviews")
            fingerprint = store.fingerprint(plan)
            review.approve_entries(
                plan.task_id, fingerprint, tuple(entry_id(entry) for entry in plan.entries)
            )
            service = PlanExecutionService(
                store, review, task_root=task_root, record_root=root / "records",
                catalog_root=root / "catalog",
            )

            result = service.execute(plan.task_id, fingerprint)

            self.assertEqual("completed", result.ledger_status)
            self.assertEqual(video.stat().st_ino, video_target.stat().st_ino)
            self.assertNotEqual(font.stat().st_ino, font_target.stat().st_ino)
            ledger = json.loads(result.ledger_path.read_text("utf-8"))
            self.assertEqual(
                ["created", "reused_identical_asset"],
                [item["status"] for item in ledger["entries"]],
            )
    def _approved_service(
        self,
        root: Path,
        *,
        ledger_status="completed",
        ledger_fingerprint=None,
        transfer_error=None,
        ledger_entries=None,
        ledger_mode=None,
    ):
        task_id = "approved-execution"
        source = root / "source" / "01.mkv"
        target = root / "library" / "Season1" / "Example S01E01.mkv"
        plan = with_tv_presentation(RenamePlan(
            task_id=task_id,
            source_root=source.parent,
            entries=(
                PlanEntry(
                    source,
                    target,
                    season_id=1,
                    episode_id=1,
                    media_kind="video",
                    requires_review=True,
                ),
            ),
            expected_sources=(source,),
            metadata={"target_root": str(root / "library")},
            execution_policy=ExecutionPolicy(
                target_root=root / "library",
                dry_run=ledger_status == "dry_run",
            ),
        ))
        task_root = root / "task"
        record_root = root / "record"
        task_root.mkdir()
        source.parent.mkdir(parents=True)
        source.write_bytes(b"synthetic-video")
        (task_root / f"{task_id}.json").write_text(
            json.dumps(
                {
                    "uuid": task_id,
                    "path": str(source.parent),
                    "workflow_status": "approved",
                    "completed_at": None,
                }
            ),
            encoding="utf-8",
        )
        store = PlanStore(root / "plan")
        plan = save_plan_fixture(store, plan)
        review = PlanReviewService(store, root / "review")
        fingerprint = store.fingerprint(plan)
        review.approve_entries(
            task_id, fingerprint, (entry_id(plan.entries[0]),)
        )
        calls = []

        class FakeTransfer:
            def __init__(
                self, mapping, identifier, plan_fingerprint, *,
                mode, dry_run, record_root: Path,
            ):
                calls.append((mapping, identifier, plan_fingerprint))
                self.plan_fingerprint = plan_fingerprint
                self.mode = mode
                self.dry_run = dry_run

            def trans_file(self):
                record_root.mkdir(parents=True, exist_ok=True)
                entries = (
                    ledger_entries
                    if ledger_entries is not None
                    else [
                        {
                            "source": str(source_path),
                            "target": str(target_path),
                            "status": (
                                "planned"
                                if ledger_status == "dry_run"
                                else "created"
                            ),
                            "transfer_type": (
                                None if ledger_status == "dry_run" else "hardlink"
                            ),
                            "source_device": 1,
                            "source_inode": 2,
                            "target_device": 1,
                            "target_inode": 2,
                            "target_link_count": 2,
                        }
                        for source_path, target_path in calls[-1][0].items()
                    ]
                )
                (record_root / f"{task_id}.ledger.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "task_id": task_id,
                            "plan_fingerprint": (
                                ledger_fingerprint
                                if ledger_fingerprint is not None
                                else self.plan_fingerprint
                            ),
                            "status": ledger_status,
                            "mode": ledger_mode or self.mode,
                            "dry_run": ledger_status == "dry_run",
                            "entries": entries,
                        }
                    ),
                    encoding="utf-8",
                )
                return transfer_error

        service = PlanExecutionService(
            store,
            review,
            task_root=task_root,
            record_root=record_root,
            transfer_factory=FakeTransfer,
        )
        return service, store, plan, fingerprint, calls, task_root

    def test_exact_approved_version_executes_and_writes_back_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, task_root = (
                self._approved_service(root)
            )

            result = service.execute(plan.task_id, fingerprint)

            self.assertEqual(result.ledger_status, "completed")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], plan.as_mapping())
            self.assertEqual(calls[0][2], fingerprint)
            task = json.loads(
                (task_root / f"{plan.task_id}.json").read_text("utf-8")
            )
            self.assertEqual(task["workflow_status"], "completed")
            self.assertEqual(task["execution_status"], "completed")
            self.assertEqual(task["execution_fingerprint"], fingerprint)
            self.assertTrue(task["execution_ledger"].endswith(".ledger.json"))

    def test_unsafe_task_id_is_rejected_before_task_path_lookup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, _plan, _fingerprint, calls, _task_root = (
                self._approved_service(root)
            )

            with self.assertRaisesRegex(PlanExecutionError, "不安全的任务 ID"):
                service.execute("../escape", "a" * 64)

            self.assertEqual(calls, [])
            self.assertFalse((root / "escape.json").exists())

    def test_stale_fingerprint_rejects_before_transfer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, _fingerprint, calls, _task_root = (
                self._approved_service(root)
            )

            with self.assertRaisesRegex(PlanExecutionError, "版本已变化"):
                service.execute(plan.task_id, "0" * 64)

            self.assertEqual(calls, [])

    def test_pending_catalog_detach_without_transaction_blocks_before_transfer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base, _store, plan, fingerprint, calls, _task_root = (
                self._approved_service(root)
            )
            decision_root = root / "decision"
            states = DecisionStateStore(decision_root)
            state = states.create(plan.task_id)
            states.save(
                replace(state, recovery_state="catalog_detach_pending"),
                expected_revision=state.revision,
            )
            service = PlanExecutionService(
                base.plan_store,
                base.review_service,
                task_root=base.task_root,
                record_root=base.record_root,
                transfer_factory=base.transfer_factory,
                decision_state_root=decision_root,
                recovery_transaction_root=root / "transactions",
            )

            with self.assertRaisesRegex(PlanExecutionError, "事务记录缺失"):
                service.execute(plan.task_id, fingerprint)

            self.assertEqual([], calls)

    def test_decision_plan_fingerprint_mismatch_blocks_before_transfer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base, store, plan, _fingerprint, calls, _task_root = (
                self._approved_service(root)
            )
            decision_root = root / "decision"
            states = DecisionStateStore(decision_root)
            state = states.create(plan.task_id)
            revised_plan = replace(
                plan,
                metadata={
                    **dict(plan.metadata),
                    "decision_state_fingerprint": "stale-decision-fingerprint",
                },
            )
            revised_plan = save_plan_fixture(store, revised_plan)
            revised_fingerprint = store.fingerprint(revised_plan)
            service = PlanExecutionService(
                store,
                base.review_service,
                task_root=base.task_root,
                record_root=base.record_root,
                transfer_factory=base.transfer_factory,
                decision_state_root=decision_root,
                recovery_transaction_root=root / "transactions",
            )

            with self.assertRaisesRegex(PlanExecutionError, "fingerprint"):
                service.execute(plan.task_id, revised_fingerprint)

            self.assertNotEqual(
                "stale-decision-fingerprint",
                DecisionStateStore.fingerprint(state),
            )
            self.assertEqual([], calls)

    def test_preview_task_rejects_before_transfer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, task_root = (
                self._approved_service(root)
            )
            task_path = task_root / f"{plan.task_id}.json"
            task = json.loads(task_path.read_text("utf-8"))
            task["preview_only"] = True
            task_path.write_text(json.dumps(task), encoding="utf-8")

            with self.assertRaisesRegex(PlanExecutionError, "预览任务"):
                service.execute(plan.task_id, fingerprint)

            self.assertEqual(calls, [])

    def test_unapproved_decision_rejects_before_transfer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, _task_root = (
                self._approved_service(root)
            )
            service.review_service.delete(plan.task_id)
            service.review_service.load_or_create(plan)

            with self.assertRaisesRegex(PlanExecutionError, "尚未全部批准"):
                service.execute(plan.task_id, fingerprint)

            self.assertEqual(calls, [])

    def test_missing_execution_policy_rejects_before_transfer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, store, plan, _fingerprint, calls, task_root = (
                self._approved_service(root)
            )
            unsafe = replace(plan, execution_policy=None)
            unsafe = save_plan_fixture(store, unsafe)
            fingerprint = store.fingerprint(unsafe)
            service.review_service.approve_entries(
                unsafe.task_id,
                fingerprint,
                tuple(entry_id(entry) for entry in unsafe.entries),
            )
            task_path = task_root / f"{unsafe.task_id}.json"
            task = json.loads(task_path.read_text("utf-8"))
            task["workflow_status"] = "approved"
            task_path.write_text(json.dumps(task), encoding="utf-8")

            with self.assertRaisesRegex(PlanExecutionError, "不可变执行策略"):
                service.execute(unsafe.task_id, fingerprint)

            self.assertEqual([], calls)

    def test_ledger_mode_must_match_approved_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, _task_root = (
                self._approved_service(root, ledger_mode="move")
            )

            with self.assertRaisesRegex(PlanExecutionError, "mode"):
                service.execute(plan.task_id, fingerprint)

            self.assertEqual(1, len(calls))

    def test_runtime_dry_run_kill_switch_blocks_real_plan_before_transfer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, _task_root = (
                self._approved_service(root)
            )

            with patch.dict("os.environ", {"BAR_DRY_RUN": "true"}):
                with self.assertRaisesRegex(PlanExecutionError, "BAR_DRY_RUN"):
                    service.execute(plan.task_id, fingerprint)

            self.assertEqual([], calls)

    def test_runtime_mode_change_blocks_approved_hardlink_before_transfer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, _task_root = (
                self._approved_service(root)
            )

            with patch(
                "src.rename.plan_execution.cm.get_config",
                return_value="剪切",
            ):
                with self.assertRaisesRegex(PlanExecutionError, "hardlink"):
                    service.execute(plan.task_id, fingerprint)

            self.assertEqual([], calls)

    def test_completed_ledger_rejects_null_hardlink_proof(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source" / "01.mkv"
            target = root / "library" / "Season1" / "Example S01E01.mkv"
            service, _store, plan, fingerprint, calls, _task_root = (
                self._approved_service(
                    root,
                    ledger_entries=[{
                        "source": str(source),
                        "target": str(target),
                        "status": "created",
                        "transfer_type": "hardlink",
                        "source_device": None,
                        "source_inode": None,
                        "target_device": None,
                        "target_inode": None,
                        "target_link_count": 2,
                    }],
                )
            )

            with self.assertRaisesRegex(PlanExecutionError, "inode"):
                service.execute(plan.task_id, fingerprint)

            self.assertEqual(1, len(calls))

    def test_atomic_execution_lock_allows_exactly_one_transfer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base, _store, plan, fingerprint, calls, _task_root = (
                self._approved_service(root)
            )
            entered = threading.Event()
            release = threading.Event()
            original_factory = base.transfer_factory

            class BlockingTransfer:
                def __init__(self, *args, **kwargs):
                    self.inner = original_factory(*args, **kwargs)

                def trans_file(self):
                    entered.set()
                    self_outer = release.wait(timeout=5)
                    if not self_outer:
                        raise RuntimeError("test release timeout")
                    return self.inner.trans_file()

            service = PlanExecutionService(
                base.plan_store,
                base.review_service,
                task_root=base.task_root,
                record_root=base.record_root,
                transfer_factory=BlockingTransfer,
            )
            first_errors = []

            def run_first():
                try:
                    service.execute(plan.task_id, fingerprint)
                except Exception as exc:  # pragma: no cover - failure aid
                    first_errors.append(exc)

            thread = threading.Thread(target=run_first)
            thread.start()
            self.assertTrue(entered.wait(timeout=5))
            with self.assertRaisesRegex(PlanExecutionError, "执行锁"):
                service.execute(plan.task_id, fingerprint)
            release.set()
            thread.join(timeout=5)

            self.assertFalse(thread.is_alive())
            self.assertEqual([], first_errors)
            self.assertEqual(1, len(calls))

    def test_second_validation_catches_change_immediately_before_transfer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base, store, plan, fingerprint, calls, task_root = (
                self._approved_service(root)
            )

            class RacingService(PlanExecutionService):
                validations = 0

                def validate_approved(self, task_id, expected_fingerprint):
                    result = super().validate_approved(
                        task_id, expected_fingerprint
                    )
                    self.validations += 1
                    if self.validations == 1:
                        save_plan_fixture(
                            store,
                            replace(
                                plan,
                                metadata={
                                    **dict(plan.metadata),
                                    "changed": True,
                                },
                            )
                        )
                    return result

            service = RacingService(
                store,
                base.review_service,
                task_root=base.task_root,
                record_root=base.record_root,
                transfer_factory=base.transfer_factory,
            )

            with self.assertRaisesRegex(PlanExecutionError, "版本已变化"):
                service.execute(plan.task_id, fingerprint)

            self.assertEqual(calls, [])
            task = json.loads(
                (task_root / f"{plan.task_id}.json").read_text("utf-8")
            )
            self.assertEqual(task["workflow_status"], "failed")
            self.assertEqual(task["execution_status"], "failed")
            self.assertFalse(task["execution_requires_manual_check"])
            self.assertFalse(task["execution_may_have_written_targets"])
            self.assertIsNone(task["execution_recovery_message"])

    def test_dry_run_ledger_keeps_plan_approved_and_not_completed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, task_root = (
                self._approved_service(root, ledger_status="dry_run")
            )

            result = service.execute(plan.task_id, fingerprint)

            self.assertEqual(result.ledger_status, "dry_run")
            self.assertEqual(len(calls), 1)
            task = json.loads(
                (task_root / f"{plan.task_id}.json").read_text("utf-8")
            )
            self.assertEqual(task["workflow_status"], "approved")
            self.assertEqual(task["execution_status"], "dry_run")
            self.assertIsNone(task["completed_at"])

    def test_mismatched_ledger_fingerprint_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, task_root = (
                self._approved_service(root, ledger_fingerprint="0" * 64)
            )

            with self.assertRaisesRegex(PlanExecutionError, "fingerprint"):
                service.execute(plan.task_id, fingerprint)

            self.assertEqual(len(calls), 1)
            task = json.loads(
                (task_root / f"{plan.task_id}.json").read_text("utf-8")
            )
            self.assertEqual(task["workflow_status"], "failed")
            self.assertEqual(task["execution_status"], "failed")
            self.assertTrue(task["execution_requires_manual_check"])
            self.assertTrue(task["execution_may_have_written_targets"])
            self.assertIn("可能已有", task["execution_recovery_message"])

    def test_completed_ledger_with_tampered_mapping_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, task_root = (
                self._approved_service(
                    root,
                    ledger_entries=[
                        {
                            "source": str(root / "source" / "other.mkv"),
                            "target": str(root / "library" / "other.mkv"),
                            "status": "created",
                        }
                    ],
                )
            )

            with self.assertRaisesRegex(PlanExecutionError, "映射"):
                service.execute(plan.task_id, fingerprint)

            self.assertEqual(len(calls), 1)
            task = json.loads(
                (task_root / f"{plan.task_id}.json").read_text("utf-8")
            )
            self.assertTrue(task["execution_requires_manual_check"])

    def test_completed_ledger_with_noncreated_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source" / "01.mkv"
            target = root / "library" / "Season1" / "Example S01E01.mkv"
            service, _store, plan, fingerprint, calls, _task_root = (
                self._approved_service(
                    root,
                    ledger_entries=[
                        {
                            "source": str(source),
                            "target": str(target),
                            "status": "planned",
                        }
                    ],
                )
            )

            with self.assertRaisesRegex(PlanExecutionError, "条目状态"):
                service.execute(plan.task_id, fingerprint)

            self.assertEqual(len(calls), 1)

    def test_directory_source_rejects_before_transfer_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, task_root = (
                self._approved_service(root)
            )
            source = plan.entries[0].source_path
            source.unlink()
            source.mkdir()

            with self.assertRaisesRegex(PlanExecutionError, "不是普通文件"):
                service.execute(plan.task_id, fingerprint)

            self.assertEqual(calls, [])
            task = json.loads(
                (task_root / f"{plan.task_id}.json").read_text("utf-8")
            )
            self.assertFalse(task["execution_requires_manual_check"])

    def test_symlinked_target_ancestor_escape_rejects_before_transfer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, task_root = (
                self._approved_service(root)
            )
            library = root / "library"
            outside = root / "outside"
            library.mkdir()
            outside.mkdir()
            try:
                (library / "Season1").symlink_to(
                    outside, target_is_directory=True
                )
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            with self.assertRaisesRegex(PlanExecutionError, "真实路径越出"):
                service.execute(plan.task_id, fingerprint)

            self.assertEqual(calls, [])
            task = json.loads(
                (task_root / f"{plan.task_id}.json").read_text("utf-8")
            )
            self.assertFalse(task["execution_requires_manual_check"])

    def test_completed_fingerprint_cannot_execute_twice(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, _task_root = (
                self._approved_service(root)
            )
            service.execute(plan.task_id, fingerprint)

            with self.assertRaisesRegex(PlanExecutionError, "已经执行完成"):
                service.execute(plan.task_id, fingerprint)

            self.assertEqual(len(calls), 1)

    def test_real_trans_boundary_creates_verified_temp_hardlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base, store, plan, fingerprint, _calls, task_root = (
                self._approved_service(root)
            )
            record_root = root / "record"
            service = PlanExecutionService(
                store,
                base.review_service,
                task_root=task_root,
                record_root=record_root,
            )

            with patch("src.rename.trans.RECORD_PATH", record_root), patch(
                "src.rename.trans.cm.get_config", return_value="链接"
            ), patch.dict("os.environ", {"BAR_DRY_RUN": "false"}):
                result = service.execute(plan.task_id, fingerprint)

            source_stat = plan.entries[0].source_path.stat()
            target_stat = plan.entries[0].target_path.stat()
            self.assertEqual(result.ledger_status, "completed")
            self.assertEqual(source_stat.st_dev, target_stat.st_dev)
            self.assertEqual(source_stat.st_ino, target_stat.st_ino)
            self.assertGreaterEqual(target_stat.st_nlink, 2)
            ledger = json.loads(result.ledger_path.read_text("utf-8"))
            self.assertEqual(ledger["plan_fingerprint"], fingerprint)

    def test_schema3_subtitle_rule_executes_exact_approved_hardlinks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "library"
            task_root = root / "task"
            record_root = root / "record"
            source_root.mkdir()
            task_root.mkdir()
            video_source = source_root / "Show - 01.mkv"
            subtitle_source = source_root / "Show - 01.Mystery.SC.ass"
            video_source.write_bytes(b"synthetic-video")
            subtitle_source.write_bytes(b"synthetic-subtitle")
            video_target = target_root / "Show" / "Season 01" / "Show S01E01.mkv"
            video = PlanEntry.from_paths(video_source, video_target)
            subtitle = PlanEntry(
                source_path=subtitle_source,
                target_path=(
                    target_root / "Show" / "Season 01" /
                    "Show S01E01.subtitle-review.ass"
                ),
                season_id=1,
                episode_id=1,
                media_kind="subtitle",
                role="subtitle",
                requires_review=True,
                subtitle=SubtitlePlanContract(
                    associated_video_source_path=video_source,
                    raw_label="Mystery.SC",
                    primary_language="zh",
                    script="Hans",
                    unknown_tokens=("Mystery",),
                    association_state=DimensionResolution.RESOLVED,
                    parse_state=DimensionResolution.UNRESOLVED,
                    association_rule_version=SUBTITLE_ASSOCIATION_RULE_VERSION,
                    label_rule_version=SUBTITLE_LABEL_RULE_VERSION,
                    reason_codes=("TAG_UNKNOWN_TOKEN",),
                ),
            )
            plan = with_tv_presentation(RenamePlan(
                task_id="subtitle-approved-execution",
                source_root=source_root,
                entries=(video, subtitle),
                expected_sources=(video_source, subtitle_source),
                metadata={"target_root": str(target_root)},
                execution_policy=ExecutionPolicy(target_root=target_root),
            ))
            store = PlanStore(root / "plan")
            plan = save_plan_fixture(store, plan)
            review = PlanReviewService(store, root / "review")
            revised = review.override_subtitle_decision(
                plan.task_id,
                store.fingerprint(plan),
                entry_id(subtitle),
                primary_language="zh",
                script="Hans",
                track_title_tokens=("Mystery",),
                unknown_dispositions=(UnknownTokenDisposition(
                    "Mystery", UnknownTokenAction.PRESERVE_AS_TITLE,
                ),),
                reason="合成测试确认 Mystery 为轨道标题",
            )
            fingerprint = store.fingerprint(revised)
            review.approve_entries(
                revised.task_id,
                fingerprint,
                tuple(entry_id(entry) for entry in revised.entries),
            )
            (task_root / f"{revised.task_id}.json").write_text(
                json.dumps({
                    "uuid": revised.task_id,
                    "path": str(source_root),
                    "workflow_status": "approved",
                    "completed_at": None,
                }),
                encoding="utf-8",
            )
            service = PlanExecutionService(
                store,
                review,
                task_root=task_root,
                record_root=record_root,
            )

            with patch("src.rename.trans.RECORD_PATH", record_root), patch(
                "src.rename.trans.cm.get_config", return_value="链接"
            ), patch.dict("os.environ", {"BAR_DRY_RUN": "false"}):
                result = service.execute(revised.task_id, fingerprint)

            revised_subtitle = next(
                entry for entry in revised.entries
                if entry.media_kind == "subtitle"
            )
            self.assertEqual(
                "Show S01E01.zh-Hans.Mystery.ass",
                revised_subtitle.target_path.name,
            )
            for entry in revised.entries:
                self.assertTrue(entry.source_path.exists())
                self.assertEqual(
                    entry.source_path.read_bytes(), entry.target_path.read_bytes()
                )
                self.assertEqual(
                    entry.source_path.stat().st_ino,
                    entry.target_path.stat().st_ino,
                )
            ledger = json.loads(result.ledger_path.read_text("utf-8"))
            self.assertEqual("completed", ledger["status"])
            self.assertEqual(fingerprint, ledger["plan_fingerprint"])
            self.assertEqual(
                ["hardlink", "hardlink"],
                [item["transfer_type"] for item in ledger["entries"]],
            )


if __name__ == "__main__":
    unittest.main()
