"""Gate 1 red contracts for catalog detach recovery and title authority.

These tests intentionally describe the approved behavior before production
implementation exists.  They must fail by assertion/API mismatch, never by
collection or fixture errors, until Gate 2 implements the contract.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import tempfile
import unittest
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.pages import data_table_page
from src.pages.data_table_page import task_display_title
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.decision_state import (
    DecisionCommandService,
    DecisionState,
    DecisionStateStore,
)
from src.rename.plan_review import ReviewStatus
from src.rename.plan_store import PlanStore
from src.rename.recovery_service import RecoveryService
from src.rename.recovery_transaction import (
    CatalogDetachService,
    RecoveryTransactionStore,
    transaction_blocks_execution,
)
from tests.projection_test_support import save_plan_fixture
from src.rename.series_catalog import SeriesCatalog
from src.rename.rename_plan import PlanEntry, RenamePlan
from src.rename.work_catalog import (
    WORK_CATALOG_SCHEMA_VERSION,
    WorkCatalog,
    WorkCatalogError,
)


WORK_KEY = "work-12345678-1234-1234-1234-123456789abc"


class RecoveryDiagnosisGate1Contracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.mkv"
        self.target = self.root / "library" / "Example.mkv"
        self.source.write_bytes(b"media")
        self.target.parent.mkdir()
        os.link(self.source, self.target)
        self.plan = RenamePlan(
            "gate1-recovery",
            self.root,
            (
                PlanEntry(
                    self.source,
                    self.target,
                    media_kind="video",
                    role="main_episode",
                ),
            ),
            expected_sources=(self.source,),
        )
        self.catalog_root = self.root / "catalog"
        self.record_root = self.root / "record"
        self.catalog_root.mkdir()
        self.record_root.mkdir()
        self.decision = DecisionState(
            task_id=self.plan.task_id,
            revision=4,
            catalog_link_intent={"work_key": WORK_KEY},
        )
        (self.record_root / f"{self.plan.task_id}.ledger.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "task_id": self.plan.task_id,
                    "plan_fingerprint": PlanStore.fingerprint(self.plan),
                    "status": "completed",
                    "entries": [],
                }
            ),
            encoding="utf-8",
        )
        self.service = RecoveryService(
            catalog_root=self.catalog_root,
            record_root=self.record_root,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_catalog_record_has_distinct_diagnosis_and_two_safe_routes(self):
        snapshot = self.service.diagnose(self.decision, self.plan)

        self.assertEqual("catalog_record_missing", snapshot.state)
        self.assertEqual(
            ("rebuild_catalog", "confirm_catalog_detach"),
            snapshot.allowed_actions,
        )

    def test_missing_catalog_root_routes_to_move_or_delete_without_guessing(self):
        missing_root = self.root / "deleted-work-root"
        (self.catalog_root / f"{WORK_KEY}.json").write_text(
            json.dumps(
                {
                    "schema_version": WORK_CATALOG_SCHEMA_VERSION,
                    "work_key": WORK_KEY,
                    "revision": 3,
                    "title": "Example",
                    "year": 2024,
                    "domain": "animation",
                    "group_kind": "movie_continuous",
                    "canonical_root": str(missing_root),
                    "aliases": [],
                    "providers": [],
                    "versions": [],
                    "assets": [{"target": str(self.target)}],
                }
            ),
            encoding="utf-8",
        )
        # The fixture itself must pass the production Catalog validator; a
        # damaged fixture must never force diagnose() into permissive parsing.
        self.assertEqual(
            missing_root,
            WorkCatalog(self.catalog_root).get_match(WORK_KEY).canonical_root,
        )

        snapshot = self.service.diagnose(self.decision, self.plan)

        self.assertEqual("catalog_root_missing", snapshot.state)
        self.assertEqual(
            ("relocate_catalog", "confirm_catalog_detach"),
            snapshot.allowed_actions,
        )

    def test_corrupt_catalog_is_a_distinct_fail_closed_state(self):
        (self.catalog_root / f"{WORK_KEY}.json").write_text(
            "{not-json", encoding="utf-8"
        )

        snapshot = self.service.diagnose(self.decision, self.plan)

        self.assertEqual("catalog_corrupt", snapshot.state)
        self.assertEqual((), snapshot.allowed_actions)

    def test_source_unavailable_only_routes_to_reselect_source(self):
        self.target.unlink()
        self.source.unlink()

        snapshot = self.service.diagnose(self.decision, self.plan)

        self.assertEqual("source_unavailable", snapshot.state)
        self.assertTrue(
            hasattr(snapshot, "allowed_actions"),
            "RecoverySnapshot must expose explicit allowed_actions routing",
        )
        self.assertEqual(("reselect_source",), snapshot.allowed_actions)
        self.assertNotIn("confirm_catalog_detach", snapshot.allowed_actions)

    def test_evidence_conflict_never_falls_back_to_fake_recovery(self):
        self.target.unlink()
        self.target.write_bytes(b"foreign target")

        snapshot = self.service.diagnose(self.decision, self.plan)

        self.assertEqual("evidence_conflict", snapshot.state)
        self.assertTrue(
            hasattr(snapshot, "allowed_actions"),
            "Evidence conflicts must expose an empty allowed_actions contract",
        )
        self.assertEqual((), snapshot.allowed_actions)

    def test_missing_catalog_and_targets_can_generate_unapproved_rebuild_plan(self):
        self.target.unlink()

        snapshot = self.service.diagnose(self.decision, self.plan)

        self.assertEqual("materialization_missing", snapshot.state)
        self.assertEqual(("rebuild_plan",), snapshot.allowed_actions)
        self.assertTrue(snapshot.can_rebuild_plan)
        rebuilt = self.service.rebuild_missing_materialization_plan(
            self.decision,
            self.plan,
            PlanStore(self.root / "rebuild-plan"),
        )
        self.assertEqual(
            "catalog_record_and_materialization_missing",
            rebuilt.metadata["recovery_reason"],
        )

    def test_decision_state_can_persist_distinct_catalog_detach_pending_state(self):
        try:
            pending = DecisionState(
                task_id="gate1-pending",
                recovery_state="catalog_detach_pending",
            )
        except Exception as exc:  # Gate 1 converts missing contract into red assertion.
            self.fail(
                "DecisionState must accept the distinct catalog_detach_pending "
                f"state: {exc}"
            )

        self.assertEqual("catalog_detach_pending", pending.recovery_state)

    def test_work_and_series_diagnosis_is_domain_orthogonal(self):
        missing_root = self.root / "missing-library-work"
        work_payload = {
            "schema_version": WORK_CATALOG_SCHEMA_VERSION,
            "work_key": WORK_KEY,
            "revision": 1,
            "title": "Example",
            "year": 2024,
            "domain": "animation",
            "group_kind": "movie_continuous",
            "canonical_root": str(missing_root),
            "aliases": [],
            "providers": [],
            "versions": [],
            "assets": [{"target": str(self.target)}],
        }
        (self.catalog_root / f"{WORK_KEY}.json").write_text(
            json.dumps(work_payload), encoding="utf-8"
        )
        series_root = self.root / "series"
        series_root.mkdir()
        series_key = f"series-{uuid.uuid4()}"
        (series_root / f"{series_key}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "series_key": series_key,
                    "revision": 1,
                    "canonical_root": str(missing_root),
                    "canonical_display_name": "Example",
                    "provider_evidence": [],
                    "season_bindings": [],
                }
            ),
            encoding="utf-8",
        )
        service = RecoveryService(
            catalog_root=self.catalog_root,
            record_root=self.record_root,
            series_catalog_root=series_root,
        )

        for backend, domain in (
            ("work", "animation"),
            ("work", "general"),
            ("series", "animation"),
            ("series", "general"),
        ):
            with self.subTest(backend=backend, domain=domain):
                decision = DecisionState(
                    task_id=self.plan.task_id,
                    revision=4,
                    library_domain=domain,
                    catalog_link_intent=(
                        {"work_key": WORK_KEY} if backend == "work" else None
                    ),
                    series_binding=(
                        {"series_key": series_key} if backend == "series" else None
                    ),
                )
                snapshot = service.diagnose(decision, self.plan)
                self.assertEqual("catalog_root_missing", snapshot.state)
                self.assertEqual(
                    ("relocate_catalog", "confirm_catalog_detach"),
                    snapshot.allowed_actions,
                )

    def test_four_routes_keep_foreign_target_inode_conflicts_fail_closed(self):
        """Routing dimensions must never bypass the hardlink evidence gate."""

        self.target.unlink()
        self.target.write_bytes(b"foreign target with a different inode")
        series_root = self.root / "series-inode"
        series_root.mkdir()
        series_key = f"series-{uuid.uuid4()}"
        service = RecoveryService(
            catalog_root=self.catalog_root,
            record_root=self.record_root,
            series_catalog_root=series_root,
        )

        for backend, domain in (
            ("work", "animation"),
            ("work", "general"),
            ("series", "animation"),
            ("series", "general"),
        ):
            with self.subTest(backend=backend, domain=domain):
                decision = DecisionState(
                    task_id=self.plan.task_id,
                    revision=4,
                    library_domain=domain,
                    catalog_link_intent=(
                        {"work_key": WORK_KEY} if backend == "work" else None
                    ),
                    series_binding=(
                        {"series_key": series_key} if backend == "series" else None
                    ),
                )
                snapshot = service.diagnose(decision, self.plan)
                self.assertEqual("evidence_conflict", snapshot.state)
                self.assertTrue(
                    hasattr(snapshot, "allowed_actions"),
                    "All routing variants must preserve explicit fail-closed actions",
                )
                self.assertEqual((), snapshot.allowed_actions)

    def test_relocation_refuses_while_old_root_exists_without_catalog_write(self):
        old_root = self.root / "old-root"
        new_root = self.root / "allowed" / "new-root"
        old_root.mkdir()
        new_root.mkdir(parents=True)
        catalog = WorkCatalog(self.catalog_root)
        payload = {
            "schema_version": WORK_CATALOG_SCHEMA_VERSION,
            "work_key": WORK_KEY,
            "revision": 1,
            "title": "Example",
            "year": 2024,
            "domain": "animation",
            "group_kind": "movie_continuous",
            "canonical_root": str(old_root),
            "aliases": [],
            "providers": [],
            "versions": [],
            "assets": [],
        }
        path = self.catalog_root / f"{WORK_KEY}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        before = path.read_bytes()
        task_root = self.root / "tasks"
        task_root.mkdir()

        with self.assertRaisesRegex(WorkCatalogError, "旧登记目录仍存在"):
            catalog.relocate(
                WORK_KEY,
                new_root,
                allowed_root=new_root.parent,
                task_root=task_root,
            )

        self.assertEqual(before, path.read_bytes())


class CatalogDetachTransactionGate1Contracts(unittest.TestCase):
    @staticmethod
    def _transaction_api():
        try:
            return importlib.import_module("src.rename.recovery_transaction")
        except ModuleNotFoundError:
            return None

    def _require_transaction_api(self):
        api = self._transaction_api()
        self.assertIsNotNone(
            api,
            "Gate 2 must add the catalog-detach transaction API; "
            "Gate 1 intentionally remains red until then",
        )
        return api

    def test_decision_detached_crash_window_advances_without_second_state_write(self):
        api = self._require_transaction_api()
        transaction = api.RecoveryTransaction.from_dict(
            {
                "task_id": "gate1-idempotent-state",
                "operation": "catalog_detach_pending",
                "phase": "catalog_archived",
                "expected_decision_revision": 7,
                "prepared_plan_fingerprint": "prepared-fingerprint",
            }
        )
        already_detached = DecisionState(
            task_id="gate1-idempotent-state",
            revision=8,
            catalog_link_intent=None,
            series_binding=None,
            season_binding=None,
        )

        step = api.reconcile_catalog_detach_progress(
            transaction,
            decision=already_detached,
            persisted_plan_fingerprint="old-plan",
        )

        self.assertEqual("decision_detached", step.next_phase)
        self.assertFalse(step.write_decision)
        self.assertTrue(step.advance_transaction)

    def test_plan_projected_crash_window_advances_without_new_nonce_or_rewrite(self):
        api = self._require_transaction_api()
        transaction = api.RecoveryTransaction.from_dict(
            {
                "task_id": "gate1-idempotent-plan",
                "operation": "catalog_detach_pending",
                "phase": "decision_detached",
                "expected_decision_revision": 8,
                "prepared_plan_fingerprint": "prepared-fingerprint",
            }
        )

        step = api.reconcile_catalog_detach_progress(
            transaction,
            decision=DecisionState(
                task_id="gate1-idempotent-plan",
                revision=8,
            ),
            persisted_plan_fingerprint="prepared-fingerprint",
        )

        self.assertEqual("plan_projected", step.next_phase)
        self.assertFalse(step.write_plan)
        self.assertFalse(step.generate_recovery_nonce)
        self.assertTrue(step.advance_transaction)

    def test_unfinished_transaction_is_an_execution_blocker(self):
        api = self._require_transaction_api()
        transaction = api.RecoveryTransaction.from_dict(
            {
                "task_id": "gate1-block",
                "operation": "catalog_detach_pending",
                "phase": "catalog_archived",
                "expected_decision_revision": 2,
                "prepared_plan_fingerprint": "prepared-fingerprint",
            }
        )

        self.assertTrue(api.transaction_blocks_execution(transaction))

    def test_prepared_crash_after_archive_advances_without_second_archive(self):
        api = self._require_transaction_api()
        transaction = api.RecoveryTransaction.from_dict(
            {
                "task_id": "gate1-prepared-window",
                "operation": "catalog_detach_pending",
                "phase": "prepared",
                "catalog_backend": "work",
                "catalog_key": WORK_KEY,
                "expected_decision_revision": 7,
                "prepared_plan_fingerprint": "prepared-fingerprint",
            }
        )

        step = api.reconcile_catalog_detach_progress(
            transaction,
            decision=DecisionState(
                task_id="gate1-prepared-window",
                revision=7,
                catalog_link_intent={"work_key": WORK_KEY},
            ),
            persisted_plan_fingerprint="old-plan",
            catalog_status="archived_with_matching_tombstone",
        )

        self.assertEqual("catalog_archived", step.next_phase)
        self.assertFalse(step.archive_catalog)
        self.assertTrue(step.advance_transaction)

    def test_catalog_archived_phase_requests_one_detach_when_state_not_detached(self):
        api = self._require_transaction_api()
        transaction = api.RecoveryTransaction.from_dict(
            {
                "task_id": "gate1-catalog-archived",
                "operation": "catalog_detach_pending",
                "phase": "catalog_archived",
                "catalog_backend": "work",
                "catalog_key": WORK_KEY,
                "expected_decision_revision": 7,
                "prepared_plan_fingerprint": "prepared-fingerprint",
            }
        )

        step = api.reconcile_catalog_detach_progress(
            transaction,
            decision=DecisionState(
                task_id="gate1-catalog-archived",
                revision=7,
                catalog_link_intent={"work_key": WORK_KEY},
            ),
            persisted_plan_fingerprint="old-plan",
            catalog_status="archived_with_matching_tombstone",
        )

        self.assertTrue(step.write_decision)
        self.assertFalse(step.advance_transaction)

    def test_pending_recovery_state_cannot_be_treated_as_decision_detached(self):
        api = self._require_transaction_api()
        transaction = api.RecoveryTransaction.from_dict(
            {
                "task_id": "gate3-pending-clear",
                "operation": "catalog_detach_pending",
                "phase": "catalog_archived",
            }
        )
        pending = DecisionState(
            task_id="gate3-pending-clear",
            catalog_link_intent=None,
            series_binding=None,
            season_binding=None,
            recovery_state="catalog_detach_pending",
        )

        step = api.reconcile_catalog_detach_progress(
            transaction,
            decision=pending,
            persisted_plan_fingerprint="old-plan",
        )

        self.assertTrue(step.write_decision)
        self.assertFalse(step.advance_transaction)

    def test_plan_decision_fingerprint_mismatch_blocks_execution_without_transaction(self):
        api = self._require_transaction_api()

        self.assertTrue(
            api.transaction_blocks_execution(
                None,
                current_decision_fingerprint="decision-new",
                plan_decision_fingerprint="decision-old",
            )
        )

    def test_missing_or_corrupt_transaction_has_one_actionable_reconcile_command(self):
        api = self._require_transaction_api()
        pending = SimpleNamespace(
            task_id="gate1-missing-transaction",
            recovery_state="catalog_detach_pending",
        )
        for payload, expected_state in (
            (None, "transaction_missing"),
            ("{broken-json", "transaction_corrupt"),
        ):
            with self.subTest(expected_state=expected_state):
                snapshot = api.diagnose_detach_transaction(
                    pending,
                    transaction_payload=payload,
                )
                self.assertEqual(expected_state, snapshot.state)
                self.assertEqual(
                    ("reconcile_detach_transaction",),
                    snapshot.allowed_actions,
                )

    def test_reconcile_missing_transaction_uses_business_evidence_not_blind_reset(self):
        api = self._require_transaction_api()

        intact = api.plan_detach_transaction_reconciliation(
            catalog_status="intact",
            tombstone_matches=False,
            decision_is_detached=False,
        )
        self.assertEqual("cancel_detach", intact.action)
        self.assertFalse(intact.archive_catalog)

        archived = api.plan_detach_transaction_reconciliation(
            catalog_status="missing",
            tombstone_matches=True,
            decision_is_detached=False,
        )
        self.assertEqual("reconstruct_transaction", archived.action)
        self.assertEqual("catalog_archived", archived.resume_phase)

        ambiguous = api.plan_detach_transaction_reconciliation(
            catalog_status="missing",
            tombstone_matches=False,
            decision_is_detached=False,
        )
        self.assertEqual("manual_repair_required", ambiguous.action)
        self.assertFalse(ambiguous.mutate_business_state)

    def test_stale_transaction_recovery_depends_on_whether_archive_started(self):
        api = self._require_transaction_api()
        prepared = api.RecoveryTransaction.from_dict(
            {
                "task_id": "gate1-stale-prepared",
                "operation": "catalog_detach_pending",
                "phase": "prepared",
                "catalog_backend": "work",
                "catalog_key": WORK_KEY,
                "expected_decision_revision": 2,
                "prepared_plan_fingerprint": "prepared-fingerprint",
            }
        )
        later = api.RecoveryTransaction.from_dict(
            {**prepared.to_dict(), "phase": "catalog_archived"}
        )

        prepared_conflict = api.diagnose_detach_transaction_conflict(
            prepared,
            current_catalog_backend="series",
            current_catalog_key="series-other",
            current_decision_fingerprint="changed",
            current_plan_fingerprint="changed-plan",
        )
        self.assertEqual("transaction_conflict", prepared_conflict.state)
        self.assertEqual(
            ("abandon_stale_transaction",),
            prepared_conflict.allowed_actions,
        )
        self.assertFalse(prepared_conflict.mutate_business_state)

        later_conflict = api.diagnose_detach_transaction_conflict(
            later,
            current_catalog_backend="series",
            current_catalog_key="series-other",
            current_decision_fingerprint="changed",
            current_plan_fingerprint="changed-plan",
        )
        self.assertEqual(
            ("reconcile_detach_transaction",),
            later_conflict.allowed_actions,
        )
        self.assertFalse(later_conflict.advance_phase)


class CatalogDetachGate3ExecutionContracts(unittest.TestCase):
    def test_work_catalog_detach_finishes_with_cleared_state_and_projected_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
            source.write_bytes(b"media")
            missing_target = root / "missing-library" / "Example.mkv"
            plan = RenamePlan(
                "gate3-work-detach",
                root,
                (PlanEntry(source, missing_target, media_kind="video", role="main_episode"),),
                expected_sources=(source,),
                metadata={"catalog_work_key": WORK_KEY, "catalog_revision": 3},
            )
            plan_store = PlanStore(root / "plans")
            plan = save_plan_fixture(plan_store, plan)
            decision_store = DecisionStateStore(root / "decisions")
            original = decision_store.save(
                DecisionState(
                    task_id=plan.task_id,
                    recovery_state="catalog_root_missing",
                    execution_failure={
                        "code": "catalog_root_missing",
                        "catalog_backend": "work",
                        "catalog_key": WORK_KEY,
                    },
                    catalog_link_intent={"work_key": WORK_KEY},
                    season_binding={
                        "season_binding_id": "season-binding-old",
                        "season_number": 1,
                        "decision_source": "provider",
                    },
                ),
                expected_revision=-1,
            )
            catalog_root = root / "catalog"
            catalog_root.mkdir()
            (catalog_root / f"{WORK_KEY}.json").write_text(
                json.dumps(
                    {
                        "schema_version": WORK_CATALOG_SCHEMA_VERSION,
                        "work_key": WORK_KEY,
                        "revision": 3,
                        "title": "Example",
                        "year": 2024,
                        "domain": "animation",
                        "group_kind": "movie_continuous",
                        "canonical_root": str(root / "missing-library"),
                        "aliases": [],
                        "providers": [],
                        "versions": [],
                        "assets": [],
                    }
                ),
                encoding="utf-8",
            )
            series_root = root / "series"
            series_root.mkdir()
            task_root = root / "tasks"
            task_root.mkdir()
            transaction_store = RecoveryTransactionStore(root / "transactions")
            work_catalog = WorkCatalog(catalog_root)
            service = CatalogDetachService(
                transaction_store=transaction_store,
                decision_store=decision_store,
                plan_store=plan_store,
                work_catalog=work_catalog,
                series_catalog=SeriesCatalog(series_root),
                task_root=task_root,
            )

            # Simulate the critical crash window: Catalog was archived, but the
            # prepared transaction phase was not advanced yet.
            with transaction_store.lock(plan.task_id):
                service._prepare(
                    plan.task_id, catalog_backend="work", catalog_key=WORK_KEY
                )
            work_catalog.archive_missing(WORK_KEY, task_root=task_root)

            decision, projected, transaction = service.execute(
                plan.task_id, catalog_backend="work", catalog_key=WORK_KEY
            )

            self.assertEqual("completed", transaction.phase)
            self.assertIsNone(decision.recovery_state)
            self.assertIsNone(decision.execution_failure)
            self.assertIsNone(decision.catalog_link_intent)
            self.assertIsNone(decision.series_binding)
            self.assertIsNone(decision.season_binding)
            self.assertGreater(decision.revision, original.revision)
            self.assertNotIn("catalog_work_key", projected.metadata)
            self.assertNotIn("catalog_revision", projected.metadata)
            self.assertEqual("catalog_detached", projected.metadata["recovery_reason"])
            self.assertEqual(
                DecisionStateStore.fingerprint(decision),
                projected.metadata["decision_state_fingerprint"],
            )
            self.assertFalse((catalog_root / f"{WORK_KEY}.json").exists())
            self.assertEqual(
                1,
                len(list((catalog_root / ".deleted").glob(f"{WORK_KEY}.*.json"))),
            )
            self.assertFalse(
                transaction_blocks_execution(
                    transaction,
                    current_decision_fingerprint=DecisionStateStore.fingerprint(decision),
                    plan_decision_fingerprint=str(
                        projected.metadata["decision_state_fingerprint"]
                    ),
                )
            )

    def test_gui_delete_routes_both_backends_through_transaction_service(self):
        source = inspect.getsource(PlanReviewDialog._detach_missing_catalog)
        work = inspect.getsource(PlanReviewDialog._archive_missing_catalog_work)
        series = inspect.getsource(PlanReviewDialog._archive_missing_series_catalog)

        self.assertIn("catalog_detach_service.execute", source)
        self.assertIn('_detach_missing_catalog("work"', work)
        self.assertIn('_detach_missing_catalog("series"', series)
        self.assertNotIn(".archive_missing(", work)
        self.assertNotIn(".archive_missing(", series)
        recovery = inspect.getsource(
            PlanReviewDialog._render_detach_transaction_recovery
        )
        self.assertIn("继续删除登记事务", recovery)
        self.assertIn("对账并继续删除登记", recovery)


class _UiStub:
    def classes(self, *_args, **_kwargs):
        return self

    def props(self, *_args, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TitleAuthorityGate1Contracts(unittest.TestCase):
    def test_dashboard_title_uses_decision_state_over_stale_task_json(self):
        stale_task = {
            "uuid": "gate1-title",
            "workflow_status": "failed",
            "name": "错误的首次搜索标题",
            "provider_selection": {"title": "错误的旧兼容标题"},
        }
        authority = DecisionState(
            task_id="gate1-title",
            confirmed_work={"title": "用户选择的正确标题"},
        )

        try:
            actual = task_display_title(stale_task, decision_state=authority)
        except TypeError as exc:
            self.fail(
                "task_display_title must accept authoritative DecisionState "
                f"input: {exc}"
            )
        self.assertEqual(
            "用户选择的正确标题",
            actual,
        )

    def test_review_header_renders_authoritative_title_not_legacy_metadata(self):
        dialog = SimpleNamespace(
            plan=SimpleNamespace(
                metadata={
                    "name": "错误的旧 Plan 标题",
                    "season_id": 1,
                    "mapping_source": "fixture",
                },
                task_id="gate1-title",
                migrated_from_schema=None,
            ),
            snapshot=SimpleNamespace(
                plan_status="need_confirm",
                rows=(),
                source_root=Path("source"),
            ),
            decision=SimpleNamespace(status=ReviewStatus.DRAFT),
            task_decision=DecisionState(
                task_id="gate1-title",
                confirmed_work={"title": "用户选择的正确标题"},
            ),
            _render_groups=Mock(),
            _render_catalog_context=Mock(),
        )
        stub = _UiStub()
        with (
            patch("src.pages.plan_review_page.ui.row", return_value=stub),
            patch("src.pages.plan_review_page.ui.column", return_value=stub),
            patch("src.pages.plan_review_page.ui.label", return_value=stub),
            patch("src.pages.plan_review_page.ui.badge", return_value=stub),
            patch("src.pages.plan_review_page._meta_item") as meta_item,
        ):
            PlanReviewDialog._render_header(dialog)

        self.assertEqual(
            ("作品", "用户选择的正确标题"),
            meta_item.call_args_list[0].args,
        )

    def test_closing_review_observably_refreshes_history(self):
        with (
            patch.object(
                data_table_page,
                "get_task",
                return_value={"execution_requires_manual_check": False},
            ),
            patch.object(
                data_table_page,
                "plan_review_page",
                new=AsyncMock(),
            ),
            patch.object(data_table_page._history_tasks, "refresh") as history,
        ):
            asyncio.run(data_table_page._open_review("gate1-title"))

        history.assert_called_once_with()

    def test_failed_candidate_command_keeps_previous_authoritative_title(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            states = DecisionStateStore(root / "decision")
            created = states.create("gate1-title-failure")
            previous = states.save(
                created.__class__(
                    **{
                        **created.__dict__,
                        "confirmed_work": {"title": "原先正确标题"},
                    }
                ),
                expected_revision=created.revision,
            )
            service = DecisionCommandService(states, PlanStore(root / "plans"))

            def failing_planner(_state):
                raise RuntimeError("synthetic candidate planning failure")

            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                service.update(
                    previous.task_id,
                    expected_revision=previous.revision,
                    changes={"confirmed_work": {"title": "不应生效的新标题"}},
                    planner=failing_planner,
                )

            persisted = states.load(previous.task_id)
            self.assertEqual(previous, persisted)
            try:
                displayed = task_display_title(
                    {
                        "uuid": previous.task_id,
                        "name": "错误兼容标题",
                        "provider_selection": {"title": "错误兼容标题"},
                    },
                    decision_state=persisted,
                )
            except TypeError as exc:
                self.fail(
                    "failed commands must still render the prior DecisionState "
                    f"title: {exc}"
                )
            self.assertEqual("原先正确标题", displayed)


if __name__ == "__main__":
    unittest.main()
