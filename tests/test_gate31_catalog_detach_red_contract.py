"""Gate 3.1 red contracts for safe Catalog detach recovery.

These contracts intentionally precede the production implementation.  A red
result must be an assertion failure caused by missing behavior, never a fixture
or import error.  Gate 3.1 does not authorize changes under ``src/``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.pages.plan_review_page import (
    PlanReviewDialog,
    can_approve_plan,
)
from src.rename.decision_state import DecisionState, DecisionStateStore
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_review import (
    PlanReviewDecision,
    PlanReviewService,
    ReviewStatus,
    entry_id,
)
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.recovery_service import RecoveryService
from src.rename.recovery_transaction import (
    CatalogDetachService,
    RecoveryTransactionError,
    RecoveryTransactionStore,
)
from src.rename.rename_plan import (
    ExecutionPolicy,
    PlanEntry,
    PlanStatus,
    ProviderIdentity,
    RenamePlan,
    WorkIdentity,
)
from src.rename.series_catalog import SERIES_CATALOG_SCHEMA_VERSION, SeriesCatalog
from src.rename.work_catalog import WORK_CATALOG_SCHEMA_VERSION, WorkCatalog
from tests.presentation_fixtures import with_tv_presentation
from tests.projection_test_support import save_plan_fixture
from tests.test_plan_execution import _save_registered_tv_fixture


WORK_KEY = "work-12345678-1234-1234-1234-123456789abc"
SERIES_KEY = "series-12345678-1234-1234-1234-123456789abc"


class _UiStub:
    def classes(self, *_args, **_kwargs):
        return self

    def props(self, *_args, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _detached_tv_plan(root: Path, task_id: str = "gate31-detached") -> RenamePlan:
    source = root / "source" / "01.mkv"
    target_root = root / "old-library"
    target = target_root / "Old Show" / "Season 01" / "Old Show S01E01.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"synthetic media")
    plan = with_tv_presentation(
        RenamePlan(
            task_id=task_id,
            source_root=source.parent,
            entries=(
                PlanEntry(
                    source,
                    target,
                    season_id=1,
                    episode_id=1,
                    media_kind="video",
                    role="main_episode",
                    requires_review=True,
                ),
            ),
            expected_sources=(source,),
            metadata={
                "target_root": str(target_root),
                "recovery_reason": "catalog_detached",
                "recovery_nonce": "gate31-nonce",
            },
            execution_policy=ExecutionPolicy(target_root=target_root, dry_run=False),
            work_identity=WorkIdentity(
                "confirmed",
                "general",
                "Current Show",
                2024,
                ProviderIdentity(
                    "confirmed",
                    "tmdb",
                    "tv",
                    "1234",
                    "Current Show",
                    2024,
                    "explicit_user",
                ),
                "provider_selection",
            ),
        )
    )
    baseline = replace(
        plan,
        metadata={
            key: value
            for key, value in plan.metadata.items()
            if key not in {"recovery_reason", "recovery_nonce"}
        },
    )
    # Explicit component preconditions: real temporary sources, identity,
    # manual Catalog binding, policy and Decision-owned font observation are
    # projected before detach. This is not proof of a live detach operation.
    baseline = _save_registered_tv_fixture(
        PlanStore(root / "plans"), baseline, task_root=root / "tasks")
    verification = PlanVerifier().verify(baseline)
    if verification.status not in {PlanStatus.ACCEPTED, PlanStatus.NEED_CONFIRM} or verification.issues:
        raise AssertionError(
            "Gate 3.1 fixture must be current and approvable before the detach marker: "
            f"{verification.status.value} / {[item.code for item in verification.issues]}"
        )
    return replace(baseline, metadata={**dict(baseline.metadata),
        "recovery_reason": "catalog_detached", "recovery_nonce": "gate31-nonce"})


class CatalogDetachedPlanRedContracts(unittest.TestCase):
    def test_detached_intermediate_plan_is_invalid_and_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = _detached_tv_plan(Path(directory))

            verification = PlanVerifier().verify(plan)

            self.assertEqual(PlanStatus.INVALID, verification.status)
            self.assertIn(
                "catalog_detached_replan_required",
                {issue.code for issue in verification.issues},
            )
            self.assertFalse(can_approve_plan(verification.status.value))

    def test_detached_intermediate_plan_short_circuits_other_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = _detached_tv_plan(Path(directory))
            malformed = replace(
                plan,
                subtitle_override_rules=(object(),),
            )

            verification = PlanVerifier().verify(malformed)

            self.assertEqual(PlanStatus.INVALID, verification.status)
            self.assertEqual(
                ["catalog_detached_replan_required"],
                [issue.code for issue in verification.issues],
            )

    def test_forged_approval_is_blocked_before_transfer_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _detached_tv_plan(root, "gate31-forged-approval")
            task_root = root / "tasks"
            task_root.mkdir(exist_ok=True)
            (task_root / f"{plan.task_id}.json").write_text(
                json.dumps(
                    {
                        "uuid": plan.task_id,
                        "path": str(plan.source_root),
                        "workflow_status": "approved",
                        "completed_at": None,
                    }
                ),
                encoding="utf-8",
            )
            plan_store = PlanStore(root / "plans")
            # Negative-only corruption of an actually receipted component
            # fixture. Never ask a fixture writer to accept a detached Plan.
            source_before = {p: p.read_bytes() for p in plan.expected_sources}
            plan_store.path_for(plan.task_id).write_text(json.dumps(plan.to_dict()), encoding="utf-8")
            review = PlanReviewService(plan_store, root / "reviews", root / "work_catalog",
                decision_state_root=root / "decision_state", task_root=task_root)
            fingerprint = plan_store.fingerprint(plan)
            # Deliberately bypass the public approval gate: execution must have
            # its own fail-closed defense against a forged/stale review file.
            review._save(
                PlanReviewDecision(
                    plan.task_id,
                    fingerprint,
                    tuple(entry_id(entry) for entry in plan.entries),
                    ReviewStatus.APPROVED,
                )
            )
            transfer_calls = []

            class FakeTransfer:
                def __init__(self, *_args, **_kwargs):
                    transfer_calls.append("constructed")

                def trans_file(self):
                    return "transfer must not be reached"

            service = PlanExecutionService(
                plan_store,
                review,
                task_root=task_root,
                record_root=root / "records",
                transfer_factory=FakeTransfer,
                catalog_root=root / "work_catalog",
                series_catalog_root=root / "series_catalog",
                decision_state_root=root / "decision_state",
                recovery_transaction_root=root / "transactions",
            )

            with self.assertRaisesRegex(PlanExecutionError, "catalog_detached_replan_required"):
                service.execute(plan.task_id, fingerprint)

            self.assertEqual([], transfer_calls)
            self.assertEqual(source_before, {p: p.read_bytes() for p in plan.expected_sources})

    def test_review_page_offers_one_explicit_replan_action_for_detached_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = _detached_tv_plan(Path(directory), "gate31-gui-replan")
            rendered_buttons = []
            stub = _UiStub()

            def record_button(label, *args, **kwargs):
                rendered_buttons.append((label, kwargs.get("on_click")))
                return stub

            dialog = SimpleNamespace(
                plan=plan,
                task_decision=DecisionState(task_id=plan.task_id),
                task_data={},
                series_catalog=SimpleNamespace(find_by_provider=Mock(return_value=None)),
                review_service=SimpleNamespace(
                    work_catalog=SimpleNamespace(
                        find_exact=Mock(return_value=None),
                        suggestions=Mock(return_value=()),
                    )
                ),
                recovery_service=Mock(),
                _restart_recognition=Mock(),
                _render_detach_transaction_recovery=Mock(),
                catalog_detach_service=CatalogDetachService(
                    transaction_store=RecoveryTransactionStore(Path(directory)/'transactions'),
                    decision_store=DecisionStateStore(Path(directory)/'decision_state'),
                    plan_store=PlanStore(Path(directory)/'plans'),
                    work_catalog=WorkCatalog(Path(directory)/'work_catalog'),
                    series_catalog=SeriesCatalog(Path(directory)/'series_catalog'),
                    task_root=Path(directory)/'tasks'),
            )
            with (
                patch("src.pages.plan_review_page.ui.row", return_value=stub),
                patch("src.pages.plan_review_page.ui.column", return_value=stub),
                patch("src.pages.plan_review_page.ui.card", return_value=stub),
                patch("src.pages.plan_review_page.ui.label", return_value=stub),
                patch("src.pages.plan_review_page.ui.icon", return_value=stub),
                patch("src.pages.plan_review_page.ui.button", side_effect=record_button),
            ):
                dialog.snapshot = SimpleNamespace(opening_context=None)
                PlanReviewDialog._render_catalog_context_content(dialog)

            replan_actions = [
                item
                for item in rendered_buttons
                if item[0] == "重新生成整理计划"
            ]
            self.assertEqual(1, len(replan_actions))
            self.assertIs(replan_actions[0][1], dialog._restart_recognition)

    def test_replan_uses_current_resolver_and_clears_detach_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            detached = _detached_tv_plan(root, "gate31-fresh-resolver")
            current_root = root / "current-library"
            resolved_target = (
                current_root
                / "Current Show"
                / "Season 01"
                / "Current Show S01E01.mkv"
            )
            resolver_calls = []

            def current_resolver(decision, inventory_plan):
                resolver_calls.append((decision, inventory_plan))
                return replace(
                    inventory_plan,
                    entries=(
                        replace(inventory_plan.entries[0], target_path=resolved_target),
                    ),
                    metadata={
                        "target_root": str(current_root),
                        "resolver_generation": "current-config",
                    },
                    execution_policy=replace(
                        inventory_plan.execution_policy,
                        target_root=current_root,
                    ),
                )

            recovery = RecoveryService(
                catalog_root=root / "catalog",
                record_root=root / "records",
                series_catalog_root=root / "series",
            )
            rebuild = getattr(
                recovery,
                "rebuild_catalog_detached_plan",
                None,
            )
            self.assertTrue(
                callable(rebuild),
                "RecoveryService must expose a detached-plan rebuild boundary",
            )
            decision = DecisionState(task_id=detached.task_id)
            revised = rebuild(decision, detached, planner=current_resolver)

            self.assertEqual([(decision, detached)], resolver_calls)
            self.assertEqual(resolved_target, revised.entries[0].target_path)
            self.assertNotIn("recovery_reason", revised.metadata)
            self.assertNotIn("recovery_nonce", revised.metadata)
            self.assertEqual("current-config", revised.metadata["resolver_generation"])


def _valid_work_payload(root: Path) -> dict:
    return {
        "schema_version": WORK_CATALOG_SCHEMA_VERSION,
        "work_key": WORK_KEY,
        "revision": 3,
        "title": "Example Movie",
        "year": 2024,
        "domain": "animation",
        "group_kind": "movie_continuous",
        "canonical_root": str(root / "missing-library"),
        "aliases": [],
        "providers": [],
        "versions": [],
        "assets": [],
    }


def _valid_series_payload(root: Path, task_id: str) -> dict:
    return {
        "schema_version": SERIES_CATALOG_SCHEMA_VERSION,
        "series_key": SERIES_KEY,
        "revision": 2,
        "canonical_root": str(root / "missing-library"),
        "canonical_display_name": "Example Series",
        "domain": "animation",
        "provider_evidence": [],
        "season_bindings": [
            {
                "season_binding_id": "season-binding-example",
                "season_number": 1,
                "provider_identity": {},
                "decision_source": "explicit_user",
                "task_id": task_id,
            }
        ],
        "materialization_state": "materialized",
        "migration_source": None,
    }


class TombstoneSchemaValidationRedContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plan_store = PlanStore(self.root / "plans")
        self.decision_store = DecisionStateStore(self.root / "decisions")
        self.work_catalog = WorkCatalog(self.root / "work-catalog")
        self.series_catalog = SeriesCatalog(self.root / "series-catalog")
        self.transaction_store = RecoveryTransactionStore(self.root / "transactions")
        self.task_root = self.root / "tasks"
        self.task_root.mkdir()
        self.service = CatalogDetachService(
            transaction_store=self.transaction_store,
            decision_store=self.decision_store,
            plan_store=self.plan_store,
            work_catalog=self.work_catalog,
            series_catalog=self.series_catalog,
            task_root=self.task_root,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _write_tombstone(self, backend: str, key: str, payload: dict) -> Path:
        catalog = self.series_catalog if backend == "series" else self.work_catalog
        archive = catalog.root / ".deleted"
        archive.mkdir(parents=True, exist_ok=True)
        path = archive / f"{key}.20260819T000000000000Z.{uuid.uuid4().hex}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _prepare_pending_reconciliation(self, backend: str):
        task_id = f"gate31-{backend}-reconcile"
        (self.task_root / f'{task_id}.json').write_text(
            json.dumps({'uuid': task_id, 'workflow_status': 'need_confirm'}), encoding='utf-8')
        key = SERIES_KEY if backend == "series" else WORK_KEY
        source = self.root / f"{backend}-source.mkv"
        source.write_bytes(b"media")
        plan = RenamePlan(
            task_id,
            self.root,
            (
                PlanEntry(
                    source,
                    self.root / "missing-library" / f"{backend}.mkv",
                    media_kind="video",
                    role="main_episode",
                ),
            ),
            expected_sources=(source,),
            metadata=(
                {"series_key": key}
                if backend == "series"
                else {"catalog_work_key": key}
            ),
        )
        plan = save_plan_fixture(self.plan_store, plan)
        state = DecisionState(
            task_id=task_id,
            recovery_state="catalog_detach_pending",
            series_binding=(
                {"series_key": key} if backend == "series" else None
            ),
            catalog_link_intent=(
                {"work_key": key} if backend == "work" else None
            ),
        )
        saved = self.decision_store.save(state, expected_revision=-1)
        return task_id, key, saved

    def test_work_catalog_exposes_read_only_complete_archived_payload_validator(self):
        work_validator = getattr(self.work_catalog, "validate_archived_payload", None)

        self.assertTrue(
            callable(work_validator),
            "WorkCatalog must expose validate_archived_payload(payload, expected_key)",
        )

    def test_series_catalog_exposes_read_only_complete_archived_payload_validator(self):
        series_validator = getattr(self.series_catalog, "validate_archived_payload", None)

        self.assertTrue(
            callable(series_validator),
            "SeriesCatalog must expose validate_archived_payload(payload, expected_key)",
        )

    def test_same_key_with_unsupported_schema_is_not_a_valid_tombstone(self):
        for backend, key, payload in (
            ("work", WORK_KEY, _valid_work_payload(self.root)),
            ("series", SERIES_KEY, _valid_series_payload(self.root, "task")),
        ):
            with self.subTest(backend=backend):
                damaged = {**payload, "schema_version": 999}
                self._write_tombstone(backend, key, damaged)
                self.assertEqual([], self.service._valid_tombstones(backend, key))

    def test_same_key_missing_required_fields_is_not_a_valid_tombstone(self):
        for backend, key, payload, missing in (
            ("work", WORK_KEY, _valid_work_payload(self.root), "canonical_root"),
            (
                "series",
                SERIES_KEY,
                _valid_series_payload(self.root, "task"),
                "season_bindings",
            ),
        ):
            with self.subTest(backend=backend):
                damaged = dict(payload)
                damaged.pop(missing)
                self._write_tombstone(backend, key, damaged)
                self.assertEqual([], self.service._valid_tombstones(backend, key))

    def test_one_complete_and_one_damaged_archive_recovers_using_only_complete_one(self):
        for backend in ("work", "series"):
            with self.subTest(backend=backend):
                task_id, key, _before = self._prepare_pending_reconciliation(backend)
                valid = (
                    _valid_series_payload(self.root, task_id)
                    if backend == "series"
                    else _valid_work_payload(self.root)
                )
                valid_path = self._write_tombstone(backend, key, valid)
                self._write_tombstone(
                    backend,
                    key,
                    {key.split("-")[0] + "_key": key, "schema_version": 999},
                )

                candidates = self.service._valid_tombstones(backend, key)

                self.assertEqual([valid_path], candidates)
                _decision, _plan, transaction = self.service.reconcile_and_execute(
                    task_id,
                    catalog_backend=backend,
                    catalog_key=key,
                )
                self.assertEqual("completed", transaction.phase)

    def test_zero_valid_archives_fail_closed_without_decision_mutation(self):
        for backend in ("work", "series"):
            with self.subTest(backend=backend):
                task_id, key, before = self._prepare_pending_reconciliation(backend)
                key_name = "series_key" if backend == "series" else "work_key"
                self._write_tombstone(
                    backend,
                    key,
                    {"schema_version": 999, key_name: key},
                )

                with self.assertRaises(RecoveryTransactionError):
                    self.service.reconcile_and_execute(
                        task_id,
                        catalog_backend=backend,
                        catalog_key=key,
                    )

                self.assertEqual(before, self.decision_store.load(task_id))

    def test_two_complete_archives_fail_closed_without_decision_mutation(self):
        for backend in ("work", "series"):
            with self.subTest(backend=backend):
                task_id, key, before = self._prepare_pending_reconciliation(backend)
                valid = (
                    _valid_series_payload(self.root, task_id)
                    if backend == "series"
                    else _valid_work_payload(self.root)
                )
                self._write_tombstone(backend, key, valid)
                self._write_tombstone(backend, key, valid)

                with self.assertRaises(RecoveryTransactionError):
                    self.service.reconcile_and_execute(
                        task_id,
                        catalog_backend=backend,
                        catalog_key=key,
                    )

                self.assertEqual(before, self.decision_store.load(task_id))


if __name__ == "__main__":
    unittest.main()
