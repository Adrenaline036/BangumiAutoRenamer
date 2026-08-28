"""Red contracts for the rc.34 WorkCatalog/SeriesCatalog split brain.

These tests intentionally describe the next accepted behaviour.  They must
fail by assertion against rc.34, rather than by fixture/import errors, until a
reviewed implementation introduces one catalog-binding projection contract.
"""

import json
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from src.pages.plan_review_page import catalog_backend_for_plan
from src.rename.decision_state import DecisionState, DecisionStateStore
from src.rename.inventory_plan import build_inventory_review_draft
from src.rename.movie_assets import inventory_movie_assets
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_review import PlanReviewService, ReviewStatus, entry_id
from src.rename.plan_store import PlanStore
from src.rename.recovery_service import RecoveryService
from src.rename.rename_plan import (
    ExecutionPolicy,
    PlanEntry,
    ProviderIdentity,
    RenamePlan,
    WorkIdentity,
)
from src.rename.series_catalog import SERIES_CATALOG_SCHEMA_VERSION
from src.rename.work_catalog import WORK_CATALOG_SCHEMA_VERSION
from tests.presentation_fixtures import with_movie_presentation, with_tv_presentation
from tests.projection_test_support import save_plan_fixture
from tests.test_isolated_sample_acceptance import routing_snapshot_for


WORK_KEY = "work-12345678-1234-1234-1234-123456789abc"
SERIES_KEY = "series-12345678-1234-1234-1234-123456789abc"


def _tv_work_identity() -> WorkIdentity:
    return WorkIdentity(
        "confirmed",
        "animation",
        "Example",
        2024,
        ProviderIdentity(
            "confirmed",
            "tmdb",
            "tv",
            "1234",
            "Example",
            2024,
            "explicit_user",
        ),
        "provider_selection",
    )


def _movie_work_identity() -> WorkIdentity:
    return WorkIdentity(
        "confirmed",
        "animation",
        "Movie",
        2024,
        ProviderIdentity(
            "confirmed",
            "tmdb",
            "movie",
            "5678",
            "Movie",
            2024,
            "explicit_user",
        ),
        "provider_selection",
    )


def _tv_plan(root: Path, task_id: str = "catalog-split-brain") -> RenamePlan:
    source_root = root / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    source = source_root / "Example 01.mkv"
    source.write_bytes(b"synthetic-media")
    target_root = root / "library"
    target = target_root / "Example (2024)" / "Season 01" / "Example S01E01.mkv"
    plan = RenamePlan(
        task_id=task_id,
        source_root=source_root,
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
            "catalog_work_key": WORK_KEY,
            "catalog_revision": 3,
            "canonical_work_root": str(target_root / "Example (2024)"),
            "season_number": 1,
            "season_source": "provider",
        },
        execution_policy=ExecutionPolicy(target_root=target_root, dry_run=False),
        work_identity=_tv_work_identity(),
    )
    return with_tv_presentation(plan)


def _work_state(task_id: str) -> DecisionState:
    identity = _tv_work_identity()
    return DecisionState(
        task_id=task_id,
        confirmed_work=identity.to_dict(),
        provider_evidence=(identity.primary_provider.to_dict(),),
        library_domain="animation",
        layout_mode="tv_episodes",
        season_binding={
            "season_binding_id": "season-binding-1",
            "season_number": 1,
            "decision_source": "provider",
        },
        catalog_link_intent={
            "work_key": WORK_KEY,
            "catalog_revision": 3,
        },
    )


def _series_state(previous: DecisionState) -> DecisionState:
    base = previous if previous.confirmed_work else _work_state(previous.task_id)
    return replace(
        base,
        catalog_link_intent=None,
        series_binding={
            "series_key": SERIES_KEY,
            "catalog_revision": 0,
            "decision_source": "new_series_intent",
        },
    )


def _movie_plan(root: Path, task_id: str) -> RenamePlan:
    source_root = root / "movie-source"
    source_root.mkdir(parents=True, exist_ok=True)
    source = source_root / "Movie.mkv"
    source.write_bytes(b"synthetic-movie")
    target_root = root / "movies"
    plan = RenamePlan(
        task_id=task_id,
        source_root=source_root,
        entries=(
            PlanEntry(
                source,
                target_root / "Movie (2024)" / "Movie (2024).mkv",
                media_kind="video",
                role="movie_main",
                requires_review=True,
            ),
        ),
        expected_sources=(source,),
        metadata={
            "target_root": str(target_root),
            "catalog_work_key": WORK_KEY,
            "catalog_revision": 3,
        },
        execution_policy=ExecutionPolicy(target_root=target_root, dry_run=False),
        work_identity=_movie_work_identity(),
    )
    return with_movie_presentation(plan)


def _approve(root: Path, plan: RenamePlan):
    plan_store = PlanStore(root / "plan")
    state_store = DecisionStateStore(root / "decision")
    plan = save_plan_fixture(plan_store, plan, state_store=state_store)
    review = PlanReviewService(
        plan_store,
        root / "review",
        decision_state_root=state_store.root,
        task_root=root / "task",
    )
    fingerprint = plan_store.fingerprint(plan)
    review.approve_entries(
        plan.task_id,
        fingerprint,
        tuple(entry_id(entry) for entry in plan.entries),
    )
    return plan_store, review, fingerprint


class CatalogBackendSplitBrainRedContracts(unittest.TestCase):
    def test_movie_work_key_drift_fails_closed_before_transfer(self):
        """Movie drift is deliberately manual until a replay contract exists."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "movie-key-drift"
            decisions = DecisionStateStore(root / "decision")
            identity = _movie_work_identity()
            old_state = decisions.save(
                DecisionState(
                    task_id=task_id,
                    confirmed_work=identity.to_dict(),
                    provider_evidence=(identity.primary_provider.to_dict(),),
                    library_domain="animation",
                    layout_mode="movie_versions",
                    catalog_link_intent={
                        "work_key": WORK_KEY,
                        "catalog_revision": 3,
                    },
                ),
                expected_revision=-1,
            )
            old_plan = decisions.project_plan(
                old_state, _movie_plan(root, task_id)
            )
            plan_store, review, fingerprint = _approve(root, old_plan)
            current_state = decisions.load(task_id)
            new_work_key = "work-87654321-4321-4321-4321-cba987654321"
            decisions.save(
                replace(
                    current_state,
                    catalog_link_intent={
                        "work_key": new_work_key,
                        "catalog_revision": 0,
                    },
                ),
                expected_revision=current_state.revision,
            )
            transfer_calls = []

            class TransferMustNotRun:
                def __init__(self, *args, **kwargs):
                    transfer_calls.append((args, kwargs))

            service = PlanExecutionService(
                plan_store,
                review,
                task_root=root / "task",
                record_root=root / "record",
                catalog_root=root / "work_catalog",
                series_catalog_root=root / "series_catalog",
                decision_state_root=root / "decision",
                recovery_transaction_root=root / "transaction",
                transfer_factory=TransferMustNotRun,
            )

            with self.assertRaisesRegex(
                PlanExecutionError,
                "登记身份已变化且无法安全重规划.*仅允许剧集",
            ):
                service.execute(task_id, fingerprint)

            self.assertEqual([], transfer_calls)
            self.assertEqual(fingerprint, plan_store.fingerprint(plan_store.load(task_id)))
            self.assertEqual(ReviewStatus.APPROVED, review.load(task_id).status)

    def test_plan_projection_freezes_work_catalog_execution_snapshot(self):
        """A Plan snapshot records the DecisionState-derived execution binding."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = DecisionStateStore(root / "decision")
            state = store.save(_work_state("projection-work"), expected_revision=-1)
            projected = store.project_plan(state, _tv_plan(root, state.task_id))

            binding = projected.metadata.get("catalog_binding_snapshot")
            self.assertIsInstance(binding, Mapping)
            self.assertEqual("work", binding.get("backend"))
            self.assertEqual(WORK_KEY, binding.get("key"))
            self.assertEqual(3, binding.get("revision"))

    def test_plan_projection_freezes_series_catalog_execution_snapshot(self):
        """Series intent projects the same explicit shape, not an implicit key hint."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = DecisionStateStore(root / "decision")
            state = store.save(
                _series_state(DecisionState(task_id="projection-series")),
                expected_revision=-1,
            )
            draft = _tv_plan(root, state.task_id)
            plan = replace(
                draft,
                metadata={
                    **draft.metadata,
                    "series_key": SERIES_KEY,
                    "series_catalog_revision": 0,
                },
            )
            projected = store.project_plan(state, plan)

            binding = projected.metadata.get("catalog_binding_snapshot")
            self.assertIsInstance(binding, Mapping)
            self.assertEqual("series", binding.get("backend"))
            self.assertEqual(SERIES_KEY, binding.get("key"))
            self.assertEqual(0, binding.get("revision"))

    def test_old_approved_work_plan_is_rejected_after_series_intent_drift(self):
        """The real Work -> Series sequence must stop before any transfer call."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = DecisionStateStore(root / "decision")
            old_state = decisions.save(
                _work_state("approved-then-drifted"), expected_revision=-1
            )
            old_plan = decisions.project_plan(
                old_state, _tv_plan(root, old_state.task_id)
            )
            plan_store, review, fingerprint = _approve(root, old_plan)
            current_state = decisions.load(old_state.task_id)
            decisions.save(
                _series_state(current_state),
                expected_revision=current_state.revision,
            )
            task_root = root / "task"
            task_root.mkdir()
            (task_root / f"{old_plan.task_id}.json").write_text(
                json.dumps({
                    "uuid": old_plan.task_id,
                    "path": str(old_plan.source_root),
                    "workflow_status": "approved",
                    "completed_at": None,
                }),
                encoding="utf-8",
            )
            catalog_root = root / "work_catalog"
            catalog_root.mkdir()
            (catalog_root / f"{WORK_KEY}.json").write_text(
                json.dumps({
                    "schema_version": WORK_CATALOG_SCHEMA_VERSION,
                    "work_key": WORK_KEY,
                    "revision": 3,
                    "title": "Example",
                    "year": 2024,
                    "domain": "animation",
                    "group_kind": "tv_episodes",
                    "canonical_root": str(
                        old_plan.metadata["canonical_work_root"]
                    ),
                    "aliases": [],
                    "providers": [old_plan.work_identity.primary_provider.to_dict()],
                    "versions": [],
                    "seasons": [],
                    "assets": [],
                }),
                encoding="utf-8",
            )
            transfer_calls = []
            new_series_root = root / "library" / "Series Example (2024)"

            def drift_planner(decision, stale_plan):
                self.assertEqual(SERIES_KEY, decision.series_binding["series_key"])
                old_root = Path(str(stale_plan.metadata["canonical_work_root"]))
                revised_entries = tuple(
                    replace(
                        entry,
                        target_path=new_series_root / entry.target_path.relative_to(old_root),
                    )
                    for entry in stale_plan.entries
                )
                metadata = dict(stale_plan.metadata)
                metadata.update({
                    "canonical_work_root": str(new_series_root),
                    "series_key": SERIES_KEY,
                    "series_catalog_revision": 0,
                    "catalog_work_key": None,
                    "catalog_revision": 0,
                })
                return replace(stale_plan, entries=revised_entries, metadata=metadata)

            class TransferMustNotRun:
                def __init__(self, *args, **kwargs):
                    transfer_calls.append((args, kwargs))

            service = PlanExecutionService(
                plan_store,
                review,
                task_root=task_root,
                record_root=root / "record",
                catalog_root=catalog_root,
                series_catalog_root=root / "series_catalog",
                decision_state_root=root / "decision",
                recovery_transaction_root=root / "transaction",
                transfer_factory=TransferMustNotRun,
                catalog_drift_planner=drift_planner,
            )

            try:
                result = service.execute(old_plan.task_id, fingerprint)
            except PlanExecutionError as exc:
                self.fail(
                    "catalog drift must create a new unapproved Plan before "
                    f"legacy WorkCatalog preflight, but execution raised: {exc}"
                )

            self.assertEqual("reapproval_required", result.ledger_status)
            revised = plan_store.load(old_plan.task_id)
            self.assertNotEqual(fingerprint, plan_store.fingerprint(revised))
            self.assertEqual(
                new_series_root / "Season 01" / "Example S01E01.mkv",
                revised.entries[0].target_path,
            )
            self.assertEqual(
                {
                    "backend": "series",
                    "key": SERIES_KEY,
                    "revision": 0,
                },
                {
                    key: revised.metadata["catalog_binding_snapshot"][key]
                    for key in ("backend", "key", "revision")
                },
            )
            self.assertNotEqual(ReviewStatus.APPROVED, review.load(old_plan.task_id).status)
            self.assertEqual([], transfer_calls)

    def test_catalog_root_failure_survives_transient_task_plan_and_ledger_cleanup(self):
        """Recovery keeps a structured code and frozen Work binding durably."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "durable-catalog-failure"
            decisions = DecisionStateStore(root / "decision")
            state = decisions.save(_work_state(task_id), expected_revision=-1)
            plan = decisions.project_plan(state, _tv_plan(root, task_id))
            plan_store, review, fingerprint = _approve(root, plan)
            task_root = root / "task"
            task_root.mkdir()
            (task_root / f"{task_id}.json").write_text(
                json.dumps({
                    "uuid": task_id,
                    "path": str(plan.source_root),
                    "workflow_status": "approved",
                    "completed_at": None,
                }),
                encoding="utf-8",
            )
            catalog_root = root / "work_catalog"
            catalog_root.mkdir()
            (catalog_root / f"{WORK_KEY}.json").write_text(
                json.dumps({
                    "schema_version": WORK_CATALOG_SCHEMA_VERSION,
                    "work_key": WORK_KEY,
                    "revision": 3,
                    "title": "Example",
                    "year": 2024,
                    "domain": "animation",
                    "group_kind": "tv_episodes",
                    "canonical_root": str(plan.metadata["canonical_work_root"]),
                    "aliases": [],
                    "providers": [plan.work_identity.primary_provider.to_dict()],
                    "versions": [],
                    "seasons": [],
                    "assets": [],
                }),
                encoding="utf-8",
            )
            service = PlanExecutionService(
                plan_store,
                review,
                task_root=task_root,
                record_root=root / "record",
                catalog_root=catalog_root,
                series_catalog_root=root / "series_catalog",
                decision_state_root=root / "decision",
                recovery_transaction_root=root / "transaction",
            )

            with self.assertRaisesRegex(PlanExecutionError, "作品登记目录不存在"):
                service.execute(task_id, fingerprint)

            # These are the transient artifacts missing in the NAS evidence.
            (task_root / f"{task_id}.json").unlink(missing_ok=True)
            plan_store.path_for(task_id).unlink(missing_ok=True)
            (root / "record" / f"{task_id}.ledger.json").unlink(missing_ok=True)

            persisted = decisions.load(task_id).to_dict()
            self.assertEqual("catalog_root_missing", persisted.get("recovery_state"))
            self.assertEqual(
                {
                    "code": "catalog_root_missing",
                    "catalog_backend": "work",
                    "catalog_key": WORK_KEY,
                },
                persisted.get("execution_failure"),
            )

    def test_recovery_gui_uses_failure_binding_not_current_series_intent(self):
        """A later identity choice cannot redirect an earlier Work failure."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _tv_plan(root, "failure-routing")
            decision = DecisionState.from_dict({
                **_series_state(DecisionState(task_id=plan.task_id)).to_dict(),
                "execution_failure": {
                    "code": "catalog_root_missing",
                    "catalog_backend": "work",
                    "catalog_key": WORK_KEY,
                },
            })

            self.assertEqual(
                ("work", WORK_KEY), catalog_backend_for_plan(plan, decision)
            )

    def test_recovery_service_uses_same_structured_failure_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _tv_plan(root, "service-failure-routing")
            decision = DecisionState.from_dict({
                **_series_state(DecisionState(task_id=plan.task_id)).to_dict(),
                "execution_failure": {
                    "code": "catalog_root_missing",
                    "catalog_backend": "work",
                    "catalog_key": WORK_KEY,
                },
            })
            catalog_root = root / "work_catalog"
            catalog_root.mkdir()
            (catalog_root / f"{WORK_KEY}.json").write_text(json.dumps({
                "schema_version": WORK_CATALOG_SCHEMA_VERSION,
                "work_key": WORK_KEY,
                "revision": 3,
                "title": "Example",
                "year": 2024,
                "domain": "animation",
                "group_kind": "tv_episodes",
                "canonical_root": str(root / "missing-work-root"),
                "aliases": [],
                "providers": [plan.work_identity.primary_provider.to_dict()],
                "versions": [],
                "seasons": [],
                "assets": [],
            }), encoding="utf-8")
            service = RecoveryService(
                catalog_root=catalog_root,
                record_root=root / "record",
                series_catalog_root=root / "series_catalog",
            )

            snapshot = service._diagnose_catalog(decision, plan)

            self.assertEqual("catalog_root_missing", snapshot.state)
            self.assertIn("missing-work-root", snapshot.detail)

    def test_successful_execution_clears_durable_catalog_failure(self):
        """A repaired and completed execution cannot leave stale recovery routing."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "clear-catalog-failure"
            decisions = DecisionStateStore(root / "decision")
            state = decisions.save(
                replace(
                    _work_state(task_id),
                    recovery_state="catalog_root_missing",
                    execution_failure={
                        "code": "catalog_root_missing",
                        "catalog_backend": "work",
                        "catalog_key": WORK_KEY,
                    },
                ),
                expected_revision=-1,
            )
            plan = decisions.project_plan(state, _tv_plan(root, task_id))
            canonical_root = Path(str(plan.metadata["canonical_work_root"]))
            canonical_root.mkdir(parents=True)
            plan_store, review, fingerprint = _approve(root, plan)
            task_root = root / "task"
            task_root.mkdir()
            (task_root / f"{task_id}.json").write_text(
                json.dumps({
                    "uuid": task_id,
                    "path": str(plan.source_root),
                    "workflow_status": "approved",
                    "completed_at": None,
                }),
                encoding="utf-8",
            )
            catalog_root = root / "work_catalog"
            catalog_root.mkdir()
            (catalog_root / f"{WORK_KEY}.json").write_text(
                json.dumps({
                    "schema_version": WORK_CATALOG_SCHEMA_VERSION,
                    "work_key": WORK_KEY,
                    "revision": 3,
                    "title": "Example",
                    "year": 2024,
                    "domain": "animation",
                    "group_kind": "tv_episodes",
                    "canonical_root": str(canonical_root),
                    "aliases": [],
                    "providers": [plan.work_identity.primary_provider.to_dict()],
                    "versions": [],
                    "seasons": [],
                    "assets": [],
                }),
                encoding="utf-8",
            )
            service = PlanExecutionService(
                plan_store,
                review,
                task_root=task_root,
                record_root=root / "record",
                catalog_root=catalog_root,
                series_catalog_root=root / "series_catalog",
                decision_state_root=root / "decision",
                recovery_transaction_root=root / "transaction",
            )

            result = service.execute(task_id, fingerprint)

            self.assertEqual("completed", result.ledger_status)
            persisted = decisions.load(task_id)
            self.assertEqual("healthy", persisted.recovery_state)
            self.assertIsNone(persisted.execution_failure)

    def test_production_drift_planner_replays_inventory_and_recalculates_targets(self):
        """The default runtime planner, not only an injected fake, moves targets."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Example 01.mkv").write_bytes(b"episode")
            task_id = "production-drift-planner"
            draft = build_inventory_review_draft({
                "uuid": task_id,
                "path": str(source),
                "name": "Example",
                "is_anime": True,
                "movie_inventory": inventory_movie_assets(source).to_dict(),
                "draft_target_root": str(root / "library"),
                "provider_identity": {
                    "namespace": "tmdb",
                    "entity_type": "tv",
                    "provider_id": "1234",
                    "title": "Example",
                    "year": 2024,
                },
            }, dry_run=False)
            plan_store = PlanStore(root / "plan")
            draft = save_plan_fixture(plan_store, draft)
            review = PlanReviewService(
                plan_store, root / "review", root / "work_catalog"
            )
            completed = review.complete_inventory_draft(
                task_id,
                plan_store.fingerprint(draft),
                group_id=draft.presentation_groups[0].group_id,
                group_kind="tv_episodes",
                target_root=root / "library",
                reason="initial legacy Work projection",
                root_snapshot=routing_snapshot_for(
                    root / "library",
                    domain="animation", group_kind="tv_episodes",
                ),
                work_domain="animation",
                season_number=1,
                season_source="provider",
            )
            new_series_root = root / "library" / "Series Example (2024)"
            series_catalog_root = root / "series_catalog"
            series_catalog_root.mkdir()
            (series_catalog_root / f"{SERIES_KEY}.json").write_text(
                json.dumps({
                    "schema_version": SERIES_CATALOG_SCHEMA_VERSION,
                    "series_key": SERIES_KEY,
                    "revision": 0,
                    "canonical_root": str(new_series_root),
                    "canonical_display_name": "Series Example",
                    "domain": "animation",
                    "provider_evidence": [
                        completed.work_identity.primary_provider.to_dict()
                    ],
                    "season_bindings": [],
                    "materialization_state": "materialized",
                    "migration_source": None,
                    "associated_assets": [],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            decision = DecisionState(
                task_id=task_id,
                library_domain="animation",
                layout_mode="tv_episodes",
                series_binding={
                    "series_key": SERIES_KEY,
                    "canonical_root": str(new_series_root),
                    "catalog_revision": 0,
                    "decision_source": "new_series_intent",
                },
                season_binding={
                    "season_binding_id": "season-binding-current",
                    "season_number": 1,
                    "decision_source": "provider",
                },
            )

            replanned = review.replan_for_catalog_drift(decision, completed)

            self.assertTrue(replanned.entries)
            self.assertTrue(all(
                entry.target_path.is_relative_to(new_series_root)
                for entry in replanned.entries
            ))
            self.assertEqual(SERIES_KEY, replanned.metadata["series_key"])
            self.assertIsNone(replanned.metadata["catalog_work_key"])


if __name__ == "__main__":
    unittest.main()
