import hashlib
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.asset_execution_recovery import AssetExecutionRecovery
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
from src.rename.decision_state import DecisionState, DecisionStateStore
from src.rename.projection_authority import commit_projected_plan
from src.rename.series_catalog import SeriesCatalog
from src.rename.series_registration import manual_tv_binding
from src.rename.subtitle_labels import (
    DimensionResolution,
    SUBTITLE_ASSOCIATION_RULE_VERSION,
    SUBTITLE_LABEL_RULE_VERSION,
)
from tests.presentation_fixtures import with_tv_presentation


def _save_registered_tv_fixture(store, plan, *, task_root):
    """Explicit legal TV preconditions for these execution component tests.

    This is not a migration for arbitrary legacy fixtures. Only the successful
    ordinary TMDB fixture is initialized here; later revisions retain its exact
    Decision authority and use the production final projector.
    """
    states = DecisionStateStore(store.root.parent / 'decision_state')
    previous = states.load_optional(plan.task_id)
    if previous is None:
        provider = ProviderIdentity('confirmed', 'tmdb', 'tv', 'fixture-tv-1',
            'Example', 2026, 'explicit_user')
        work = (plan.work_identity if plan.work_identity is not None
            and plan.work_identity.status == 'confirmed' else
            WorkIdentity('confirmed', 'general', 'Example', 2026, provider, 'provider_selection'))
        if work.domain == 'unknown':
            # with_tv_presentation intentionally leaves domain undecided. This
            # ordinary-TV fixture explicitly selects the general TV domain.
            work = replace(work, domain='general', domain_source='provider_selection')
        provider = work.primary_provider
        canonical_root = Path(plan.metadata['target_root'])
        binding = manual_tv_binding(work, SeriesCatalog(store.root.parent / 'series_catalog'),
            canonical_root)
        candidate = DecisionState(task_id=plan.task_id, confirmed_work=work.to_dict(),
            provider_evidence=(provider.to_dict(),), library_domain='general', layout_mode='tv_episodes',
            series_binding=binding, season_binding={'season_binding_id': f'season-{plan.task_id}',
                'season_number': 1, 'decision_source': 'explicit_user'})
        plan = replace(plan, work_identity=work)
    else:
        candidate = previous
    from tests.projection_test_support import task_source_scope_fixture
    task_source_scope_fixture(store, plan, task_root=task_root)
    decision, projected = commit_projected_plan(states, store, previous=previous,
        candidate=candidate, draft_plan=plan)
    task_path = task_root / f'{plan.task_id}.json'
    task = json.loads(task_path.read_text('utf-8')) if task_path.exists() else {
        'uuid': plan.task_id, 'path': str(plan.source_root), 'workflow_status': 'pending',
        'completed_at': None}
    task.update(plan_fingerprint=store.fingerprint(projected),
        decision_state_revision=decision.revision,
        decision_state_fingerprint=states.fingerprint(decision))
    task_root.mkdir(parents=True, exist_ok=True)
    task_path.write_text(json.dumps(task), encoding='utf-8')
    return projected


class PlanExecutionTests(unittest.TestCase):
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
        plan = _save_registered_tv_fixture(store, plan, task_root=task_root)
        review = PlanReviewService(store, root / "review",
            decision_state_root=root / 'decision_state', task_root=task_root)
        fingerprint = store.fingerprint(plan)
        review.approve_entries(
            task_id, fingerprint, (entry_id(plan.entries[0]),)
        )
        calls = []

        class FakeTransfer:
            def __init__(
                self, mapping, identifier, plan_fingerprint, *,
                mode, dry_run, record_root: Path, source_identities=None,
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
            series_catalog_root=root / 'series_catalog',
            decision_state_root=root / 'decision_state',
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
            self.assertEqual(task["workflow_status"], "partial_failed")
            self.assertTrue(task["font_error"])
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
            service, _store, plan, _fingerprint, calls, task_root = (
                self._approved_service(root)
            )
            task_path = task_root / f'{plan.task_id}.json'
            before = task_path.read_bytes()

            with self.assertRaisesRegex(PlanExecutionError, "版本已变化"):
                service.execute(plan.task_id, "0" * 64)

            self.assertEqual(calls, [])
            self.assertEqual(before, task_path.read_bytes())

    def test_preparing_failure_cannot_overwrite_another_task_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service, _store, plan, fingerprint, calls, task_root = self._approved_service(Path(temp_dir))
            original = service.validate_approved
            task_path = task_root / f'{plan.task_id}.json'
            newer = []

            def changed(*args, **kwargs):
                if service._read_task(plan.task_id).get('execution_phase') == 'preparing':
                    task = json.loads(task_path.read_text('utf-8'))
                    task.update(workflow_status='needs_review', execution_fingerprint='newer-request')
                    task_path.write_text(json.dumps(task), encoding='utf-8')
                    newer.append(task_path.read_bytes())
                    raise PlanExecutionError('controlled pre-write rejection after state change')
                return original(*args, **kwargs)

            with patch.object(service, 'validate_approved', side_effect=changed):
                with self.assertRaises(PlanExecutionError):
                    service.execute(plan.task_id, fingerprint)
            self.assertEqual(1, len(newer))
            self.assertEqual(newer[0], task_path.read_bytes(), 'stale execution failure overwrote newer task')
            self.assertEqual([], calls)

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
                series_catalog_root=root / 'series_catalog',
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
            revised_plan = _save_registered_tv_fixture(store, revised_plan, task_root=base.task_root)
            revised_fingerprint = store.fingerprint(revised_plan)
            service = PlanExecutionService(
                store,
                base.review_service,
                task_root=base.task_root,
                record_root=base.record_root,
                transfer_factory=base.transfer_factory,
                decision_state_root=decision_root,
                series_catalog_root=root / 'series_catalog',
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
            # A current TV projection cannot sign a missing execution policy.
            with self.assertRaisesRegex(ValueError, "冻结执行策略"):
                _save_registered_tv_fixture(store, unsafe, task_root=task_root)
            self.assertEqual(plan,store.load(plan.task_id))
            # Legacy-shaped consumer still rejects independently of projection.
            with self.assertRaises(PlanExecutionError):
                service._validate_frozen_associated_assets(unsafe)
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
                decision_state_root=root / 'decision_state',
                series_catalog_root=root / 'series_catalog',
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

                # Both reads still verify approval authority; only the second
                # performs physical checks. Inject between those same reads.
                def validate_approved_authority(self, task_id, expected_fingerprint):
                    result = super().validate_approved_authority(
                        task_id, expected_fingerprint
                    )
                    self.validations += 1
                    if self.validations == 1:
                        _save_registered_tv_fixture(
                            store,
                            replace(
                                plan,
                                metadata={
                                    **dict(plan.metadata),
                                    "changed": True,
                                },
                            ), task_root=task_root,
                        )
                    return result

            service = RacingService(
                store,
                base.review_service,
                task_root=base.task_root,
                record_root=base.record_root,
                transfer_factory=base.transfer_factory,
                decision_state_root=root / 'decision_state',
                series_catalog_root=root / 'series_catalog',
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

    def test_approved_dry_run_plan_is_rejected_before_transfer_or_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, task_root = (
                self._approved_service(root, ledger_status="dry_run")
            )

            protected_roots = (
                root / "plan",
                root / "review",
                root / "decision_state",
                root / "catalog",
                root / "series_catalog",
                root / "record",
                root / "library",
                task_root,
            )

            def snapshot():
                return {
                    str(path.relative_to(root)): path.read_bytes()
                    for protected in protected_roots
                    if protected.exists()
                    for path in protected.rglob("*")
                    if path.is_file()
                }

            before = snapshot()
            service._replan_if_catalog_binding_drift = lambda *_args: self.fail(
                "dry-run Plan reached catalog drift handling"
            )

            with self.assertRaises(PlanExecutionError) as raised:
                service.execute(plan.task_id, fingerprint)

            self.assertEqual(raised.exception.code, "dry_run_plan_requires_retry")
            self.assertRegex(str(raised.exception), "旧预演计划|重试生成真实计划")
            self.assertEqual(calls, [])
            self.assertFalse(
                (root / "records" / f"{plan.task_id}.ledger.json").exists()
            )
            task = json.loads(
                (task_root / f"{plan.task_id}.json").read_text("utf-8")
            )
            self.assertEqual(task["workflow_status"], "approved")
            self.assertIsNone(task["completed_at"])
            self.assertEqual(before, snapshot())

    def test_dry_run_plan_is_rejected_by_authority_used_for_asset_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, task_root = (
                self._approved_service(root, ledger_status="dry_run")
            )
            task_before = (task_root / f"{plan.task_id}.json").read_bytes()

            with self.assertRaises(PlanExecutionError) as raised:
                service.validate_approved_authority(plan.task_id, fingerprint)
            self.assertEqual(raised.exception.code, "dry_run_plan_requires_retry")
            with self.assertRaisesRegex(ValueError, "旧预演计划|重试生成真实计划"):
                AssetExecutionRecovery(service).inspect(plan.task_id, fingerprint)

            self.assertEqual(calls, [])
            self.assertEqual(
                task_before,
                (task_root / f"{plan.task_id}.json").read_bytes(),
            )
            self.assertFalse((root / "record").exists())

    def test_executor_uses_the_canonical_approved_execution_policy_gate(self):
        import inspect

        authority = inspect.getsource(PlanExecutionService.validate_approved_authority)
        execute = inspect.getsource(PlanExecutionService.execute)
        locked = inspect.getsource(PlanExecutionService._execute_locked)
        self.assertIn("require_approved_execution_policy", authority)
        self.assertIn("require_approved_execution_policy", execute)
        self.assertIn("require_approved_execution_policy", locked)
        self.assertNotIn("ledger_status == \"dry_run\"", locked)

    def test_executor_fails_closed_when_canonical_policy_gate_is_disconnected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, task_root = (
                self._approved_service(root)
            )
            before = (task_root / f"{plan.task_id}.json").read_bytes()
            service._replan_if_catalog_binding_drift = lambda *_args: self.fail(
                "disconnected gate reached catalog drift handling"
            )
            disconnected = PlanExecutionError(
                "执行策略权威门已断开",
                code="execution_policy_gate_disconnected",
            )
            with patch(
                "src.rename.plan_execution.require_approved_execution_policy",
                create=True,
                side_effect=disconnected,
            ) as gate:
                with self.assertRaises(PlanExecutionError) as raised:
                    service.execute(plan.task_id, fingerprint)
            self.assertIs(raised.exception, disconnected)
            gate.assert_called_once()
            self.assertEqual(calls, [])
            self.assertEqual(
                before, (task_root / f"{plan.task_id}.json").read_bytes()
            )
            self.assertFalse((root / "record").exists())
            self.assertFalse((root / "library").exists())

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

    def test_directory_source_cannot_complete_or_leave_targets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, plan, fingerprint, calls, task_root = (
                self._approved_service(root)
            )
            source = plan.entries[0].source_path
            source.rename(source.with_suffix(".held"))
            source.mkdir()

            from src.rename.trans import Trans
            service.transfer_factory = Trans
            with patch("src.rename.trans.cm.get_config",return_value="链接"), self.assertRaises(PlanExecutionError):
                service.execute(plan.task_id, fingerprint)

            self.assertFalse(plan.entries[0].target_path.exists())
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
                decision_state_root=root / 'decision_state',
                series_catalog_root=root / 'series_catalog',
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
            plan = _save_registered_tv_fixture(store, plan, task_root=task_root)
            review = PlanReviewService(store, root / "review",
                decision_state_root=root / 'decision_state', task_root=task_root)
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
                    "plan_fingerprint": fingerprint,
                }),
                encoding="utf-8",
            )
            service = PlanExecutionService(
                store,
                review,
                task_root=task_root,
                record_root=record_root,
                decision_state_root=root / 'decision_state',
                series_catalog_root=root / 'series_catalog',
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
