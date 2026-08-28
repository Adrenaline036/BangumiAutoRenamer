"""Phase A RED contracts for the R22 projection-authority replacement.

The contracts are ordinary regression tests after Phase B GREEN.  All
filesystem state is isolated below ``TemporaryDirectory``; provider transport
is frozen by the existing production-entry fixture.
"""

from __future__ import annotations

import ast
from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename.decision_state import (
    DecisionCommandService,
    DecisionState,
    DecisionStateStore,
)
from src.rename.inventory_plan import build_inventory_review_draft
from src.rename.movie_assets import inventory_movie_assets
from src.rename.plan_store import PlanStore, PlanStoreError
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import RenamePlan
import tests.test_provider_subject_plan_wiring_red_contract as phase_b_fixture
from tests.presentation_fixtures import with_tv_presentation


AUTHORITY_FINGERPRINT_FIELDS = {
    "receipt_schema_version",
    "plan_schema_version",
    "task_id",
    "decision_schema_version",
    "decision_revision",
    "decision_fingerprint",
    "inventory_schema_version",
    "inventory_revision",
    "inventory_fingerprint",
    "source_ownership_schema_version",
    "source_ownership_revision",
    "source_ownership_fingerprint",
    "episode_ownership_schema_version",
    "episode_ownership_fingerprint",
    "tv_planning_authority_schema_version",
    "tv_planning_authority_policy_version",
    "tv_planning_authority_fingerprint",
    "batch_scope_fingerprint",
    "reservation_lineage_id",
    "reservation_revision",
    "work_identity_fingerprint",
    "series_binding_fingerprint",
    "season_binding_fingerprint",
    "target_routing_fingerprint",
    "naming_policy_fingerprint",
    "font_policy_fingerprint",
    "subtitle_policy_fingerprint",
    "execution_policy_fingerprint",
    "policy_bundle_version",
    "projector_version",
    "entry_set_fingerprint",
}


CORRELATION_ONLY_FIELDS = {
    "created_at",
    "updated_at",
    "journal_nonce",
    "journal_attempt_id",
    "temporary_path",
    "request_token",
    "season_binding_id",
    "cohort_decision_id",
}


COMMIT_PHASES = (
    "prepared",
    "decision_written",
    "ownership_claimed",
    "plan_written",
    "task_terminal_written",
    "completed",
)


def _projection_api(testcase: unittest.TestCase):
    try:
        return importlib.import_module("src.rename.projection_authority")
    except ModuleNotFoundError:
        testcase.fail(
            "canonical projection-authority module is missing; current Plan "
            "writers still have no typed receipt/commit boundary"
        )


def _issue_codes(plan: RenamePlan) -> set[str]:
    return {issue.code for issue in PlanVerifier().verify(plan).issues}


def _episode_ownership_plan(
    root: Path,
    *,
    include_second_ownership: bool,
) -> RenamePlan:
    source = root / "source"
    work_root = root / "Anime" / "86 -不存在的战区- (2021)"
    first = source / "86 [11].mkv"
    second = source / "86 [12].mkv"
    mapping = {
        first: work_root / "Season 01" / "86 -不存在的战区- (2021) S01E11.mkv",
        second: work_root / "Season 01" / "86 -不存在的战区- (2021) S01E12.mkv",
    }
    ownership = [{
        "schema_version": 1,
        "source_path": str(first),
        "bangumi_subject_id": "302189",
        "tmdb_tv_id": "100565",
        "season_number": 1,
        "source_episode": 11,
        "target_episode": 11,
        "range_authority_id": "subject:302189:season:1:1-11",
        "evidence_fingerprint": "1" * 64,
        "decision_source": "provider_subject_segment",
    }]
    if include_second_ownership:
        ownership.append({
            "schema_version": 1,
            "source_path": str(second),
            "bangumi_subject_id": "331887",
            "tmdb_tv_id": "100565",
            "season_number": 1,
            "source_episode": 12,
            "target_episode": 12,
            "range_authority_id": "subject:331887:season:1:12-23",
            "evidence_fingerprint": "2" * 64,
            "decision_source": "provider_subject_segment",
        })
    plan = RenamePlan.from_mapping(
        task_id="eighty-six-episode-ownership",
        source_root=source,
        mapping=mapping,
        metadata={
            "season_id": 1,
            "season_number": 1,
            "season_source": "provider_crosswalk",
            "provider_link_snapshot": {
                "schema_version": 2,
                "selected_subject_id": "302189",
                "tmdb_tv_id": "100565",
                "derived_episode_ranges": [
                    {
                        "bangumi_subject_id": "302189",
                        "tmdb_tv_id": "100565",
                        "season_number": 1,
                        "episode_start": 1,
                        "episode_end": 11,
                    },
                    {
                        "bangumi_subject_id": "331887",
                        "tmdb_tv_id": "100565",
                        "season_number": 1,
                        "episode_start": 12,
                        "episode_end": 23,
                    },
                ],
                "resolution_fingerprint": "3" * 64,
                "evidence_fingerprint": "4" * 64,
            },
            "task_episode_ownership": ownership,
            "task_episode_ownership_fingerprint": "5" * 64,
        },
    )
    return with_tv_presentation(plan)


def _inventory_task(
    root: Path,
    source: Path,
    *,
    claimed_child: Path | None,
) -> dict[str, object]:
    claims = []
    if claimed_child is not None:
        stat_result = claimed_child.stat()
        claims.append({
            "schema_version": 1,
            "task_id": "child-23beta",
            "state": "active",
            "source_path": str(claimed_child.resolve()),
            "location_key": str(claimed_child.resolve()).casefold(),
            "physical_key": f"{stat_result.st_dev}:{stat_result.st_ino}",
            "size": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
            "inventory_fingerprint": "6" * 64,
        })
    return {
        "uuid": "parent-sg0",
        "path": str(source),
        "movie_inventory": inventory_movie_assets(source).to_dict(),
        "draft_target_root": str(root / "target"),
        "draft_initial_target_root": str(root / "target" / "命运石之门 0 (2018)"),
        "provider_identity": {
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": "78102",
            "title": "命运石之门 0",
            "year": 2018,
        },
        "source_ownership_snapshot": {
            "schema_version": 1,
            "revision": 0,
            "claims": claims,
            "fingerprint": "7" * 64,
        },
    }


def _simple_plan(root: Path, task_id: str = "projection-contract") -> RenamePlan:
    source = root / "source" / "Episode 01.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"projection contract fixture\n")
    return RenamePlan.from_mapping(
        task_id=task_id,
        source_root=root / "source",
        mapping={
            source:
            root / "target" / "Season 01" / "Fixture S01E01.mkv"
        },
        metadata={"name": "Fixture", "season_number": 1},
    )


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return f"{_call_name(node.func)}()"
    return ""


def _production_writer_and_reverse_sync_sites() -> tuple[tuple[str, int], ...] | tuple:
    writers = []
    reverse_sync = []
    repository = Path(__file__).resolve().parents[1]
    for path in sorted((repository / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            receiver = name.rsplit(".", 1)[0].casefold() if "." in name else ""
            if name.endswith(".save") and (
                "plan_store" in receiver or "planstore" in receiver
            ):
                writers.append((path.relative_to(repository).as_posix(), node.lineno))
            if name.endswith(".synchronize_plan"):
                reverse_sync.append((path.relative_to(repository).as_posix(), node.lineno))
    return tuple(writers), tuple(reverse_sync)


class SuccessorStateClosureRedContracts(unittest.IsolatedAsyncioTestCase):
    async def test_real_add_select_successor_closes_task_decision_plan_and_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = phase_b_fixture._make_episode_source(
                root / "source",
                "Yama no Susume Third Season",
                (1, 2),
                subtitles=True,
            )
            harness = phase_b_fixture.ProviderSubjectPlanWiringRedContracts(
                methodName="runTest"
            )
            result = await harness._run_real_add_select(
                root=root / "flow",
                source=source,
                subjects=phase_b_fixture.YAMA_SUBJECTS,
                relations=phase_b_fixture.YAMA_RELATIONS,
                selected_subject_id="216310",
                fail_subject=(
                    "第二季", "セカンドシーズン", "第三季",
                    "サードシーズン", "Next Summit",
                ),
                failure_mode="no_match",
            )
        problems = harness._real_path_problems(result)
        operation = result.task.get("selection_operation") or {}
        if operation.get("state") != "awaiting_user":
            problems.append(
                "successor selection did not stop at the explicit cohort boundary: "
                f"{operation.get('state')!r}"
            )
        if "subject_candidate_evidence_missing" not in (
            operation.get("reason_codes") or ()
        ):
            problems.append("successor selection lost the cohort-boundary reason")
        if result.plan.to_dict() != result.initial_plan.to_dict():
            problems.append("awaiting-user selection partially replaced the Plan")
        if result.state.to_dict() != result.initial_state.to_dict():
            problems.append("awaiting-user selection partially replaced DecisionState")
        plan_payload = result.plan.to_dict()
        state_payload = result.state.to_dict()
        receipt = plan_payload.get("projection_receipt")
        if plan_payload.get("schema_version") != 6:
            problems.append("real entry persisted a pre-v6 Plan")
        if state_payload.get("schema_version") != 4:
            problems.append("real entry persisted a pre-v4 Decision")
        if not isinstance(receipt, dict):
            problems.append("real entry Plan has no typed ProjectionReceipt")
        elif (
            receipt.get("decision_revision") != result.state.revision
            or receipt.get("decision_fingerprint")
            != DecisionStateStore(result.decisions).fingerprint(result.state)
        ):
            problems.append("receipt is not bound to the persisted Decision")
        self.assertEqual([], problems, "\n".join(problems))

    async def test_real_entry_fails_closed_when_projection_commit_is_disconnected(self):
        module = _projection_api(self)
        service = getattr(module, "ProjectionCommitService", None)
        error_type = getattr(module, "ProjectionAuthorityError", RuntimeError)
        self.assertTrue(callable(service), "ProjectionCommitService is missing")
        self.assertTrue(hasattr(service, "commit"), "commit seam is missing")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = phase_b_fixture._make_episode_source(
                root / "source", "Steins Gate", (1, 2)
            )
            harness = phase_b_fixture.ProviderSubjectPlanWiringRedContracts(
                methodName="runTest"
            )
            with patch.object(
                service,
                "commit",
                side_effect=error_type("projection commit disconnected"),
            ):
                try:
                    await harness._run_real_add_select(
                        root=root / "flow",
                        source=source,
                        subjects=phase_b_fixture.STEINS_SUBJECTS,
                        relations=phase_b_fixture.STEINS_RELATIONS,
                        selected_subject_id="10380",
                    )
                except Exception:
                    pass
            task_files = tuple((root / "flow" / "tasks").glob("*.json"))
            plans = tuple((root / "flow" / "plans").glob("*.json"))
            task = (
                json.loads(task_files[0].read_text("utf-8"))
                if len(task_files) == 1
                else {}
            )
            plan_count = len(plans)
        self.assertEqual(1, len(task_files), "real AddTask did not persist fail-closed state")
        self.assertIn(task.get("workflow_status"), {"failed", "invalid", "interrupted"})
        self.assertNotEqual("succeeded", (task.get("selection_operation") or {}).get("state"))
        self.assertEqual(0, plan_count, "disconnected commit still persisted a current Plan")


class TaskEpisodeOwnershipRedContracts(unittest.TestCase):
    def test_owned_same_season_subject_ranges_are_checked_per_source(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = _episode_ownership_plan(
                Path(directory), include_second_ownership=True
            )
            codes = _issue_codes(plan)
        self.assertNotIn("provider_regular_episode_overflow_unresolved", codes)

    def test_unowned_same_season_range_does_not_expand_task_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = _episode_ownership_plan(
                Path(directory), include_second_ownership=False
            )
            codes = _issue_codes(plan)
        self.assertIn("provider_regular_episode_overflow_unresolved", codes)


class SourceOwnershipRedContracts(unittest.TestCase):
    def test_parent_inventory_excludes_source_claimed_by_child_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            child = source / "SPs" / "Steins Gate 23beta.mkv"
            child.parent.mkdir(parents=True)
            (source / "Steins Gate 0 [01].mkv").write_bytes(b"parent")
            child.write_bytes(b"child")
            plan = build_inventory_review_draft(
                _inventory_task(root, source, claimed_child=child),
                dry_run=True,
            )
        self.assertNotIn(child, plan.expected_sources)
        self.assertNotIn(child, tuple(entry.source_path for entry in plan.entries))

    def test_unclaimed_nested_source_remains_in_parent_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            nested = source / "SPs" / "ordinary-extra.mkv"
            nested.parent.mkdir(parents=True)
            (source / "Steins Gate 0 [01].mkv").write_bytes(b"parent")
            nested.write_bytes(b"nested")
            plan = build_inventory_review_draft(
                _inventory_task(root, source, claimed_child=None),
                dry_run=True,
            )
        self.assertIn(nested, plan.expected_sources)

    def test_hardlink_alias_conflicts_and_explicit_retire_allows_transfer(self):
        module = _projection_api(self)
        identity_type = getattr(module, "SourceIdentity", None)
        store_type = getattr(module, "SourceOwnershipStore", None)
        conflict_type = getattr(module, "SourceOwnershipConflict", RuntimeError)
        self.assertTrue(callable(identity_type), "SourceIdentity is missing")
        self.assertTrue(callable(store_type), "SourceOwnershipStore is missing")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
            alias = root / "alias.mkv"
            source.write_bytes(b"same physical source")
            os.link(source, alias)
            first = identity_type.from_path(
                source, inventory_fingerprint="8" * 64
            )
            second = identity_type.from_path(
                alias, inventory_fingerprint="8" * 64
            )
            self.assertEqual(first.physical_key, second.physical_key)
            store = store_type(root / "ownership")
            claim = store.claim(
                task_id="parent",
                decision_revision=0,
                inventory_fingerprint="8" * 64,
                identities=(first,),
            )
            with self.assertRaises(conflict_type):
                store.claim(
                    task_id="child",
                    decision_revision=0,
                    inventory_fingerprint="8" * 64,
                    identities=(second,),
                )
            store.retire(
                task_id="parent",
                expected_revision=claim.revision,
                reason="explicit_transfer",
            )
            transferred = store.claim(
                task_id="child",
                decision_revision=0,
                inventory_fingerprint="8" * 64,
                identities=(second,),
            )
        self.assertEqual("child", transferred.task_id)


class TvFontAuthorityRedContracts(unittest.TestCase):
    def test_tv_font_manifest_gate_is_shape_based_not_marker_based(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            work_root = root / "Anime" / "向山进发 (2013)"
            plan = RenamePlan.from_mapping(
                task_id="tv-font-inline-boundary",
                source_root=source,
                mapping={
                    source / "Yama [01].mkv":
                    work_root / "Season 01" / "向山进发 (2013) S01E01.mkv",
                    source / "[VCB-Studio] Yama no Susume [Fonts].7z":
                    work_root / "Associated" / "向山进发 (2013) [Fonts].7z",
                },
                metadata={
                    "canonical_work_root": str(work_root),
                    "season_id": 1,
                    "season_number": 1,
                    "season_source": "provider_crosswalk",
                },
            )
            plan = with_tv_presentation(plan)
            metadata = dict(plan.metadata)
            metadata.pop("tv_authority_required", None)
            metadata.pop("associated_asset_manifest", None)
            plan = replace(plan, metadata=metadata)
            codes = _issue_codes(plan)
        self.assertIn("tv_font_manifest_required", codes)


class ProjectionArchitectureRedContracts(unittest.TestCase):
    def test_production_has_one_plan_writer_and_no_plan_to_decision_sync(self):
        writers, reverse_sync = _production_writer_and_reverse_sync_sites()
        self.assertEqual(
            1,
            len(writers),
            f"production PlanStore writers are not canonical: {writers!r}",
        )
        self.assertEqual(
            (),
            reverse_sync,
            f"Plan-to-Decision synchronize_plan remains authoritative: {reverse_sync!r}",
        )

    def test_unreceipted_legacy_plan_cannot_be_saved_as_current(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PlanStore(root / "plans")
            plan = _simple_plan(root, "legacy-v5")
            with self.assertRaises((PlanStoreError, TypeError, ValueError)):
                store.save(plan)
        self.assertFalse(store.path_for("legacy-v5").exists())

    def test_projection_api_freezes_typed_contract_and_field_classification(self):
        module = _projection_api(self)
        required = {
            "ProjectionAuthorityError",
            "ProjectionAuthorityEnvelope",
            "TaskEpisodeOwnership",
            "SourceIdentity",
            "SourceOwnershipRecord",
            "ProjectionReceipt",
            "FinalPlanProjector",
            "ProjectionCommitService",
            "build_projection_receipt",
            "canonical_authority_fingerprint",
            "AUTHORITY_FINGERPRINT_FIELDS",
            "CORRELATION_ONLY_FIELDS",
            "PROJECTION_COMMIT_PHASES",
        }
        self.assertEqual(
            set(),
            {name for name in required if not hasattr(module, name)},
            "projection authority public contract is incomplete",
        )
        self.assertEqual(
            AUTHORITY_FINGERPRINT_FIELDS,
            set(module.AUTHORITY_FINGERPRINT_FIELDS),
        )
        self.assertEqual(
            CORRELATION_ONLY_FIELDS,
            set(module.CORRELATION_ONLY_FIELDS),
        )
        self.assertTrue(
            AUTHORITY_FINGERPRINT_FIELDS.isdisjoint(CORRELATION_ONLY_FIELDS)
        )
        self.assertEqual(COMMIT_PHASES, tuple(module.PROJECTION_COMMIT_PHASES))

    def test_receipt_is_deterministic_layered_and_has_no_self_hash_cycle(self):
        module = _projection_api(self)
        builder = getattr(module, "build_projection_receipt", None)
        fingerprint = getattr(module, "canonical_authority_fingerprint", None)
        self.assertTrue(callable(builder), "build_projection_receipt is missing")
        self.assertTrue(callable(fingerprint), "authority fingerprint builder is missing")
        hashes = iter(f"{index:064x}" for index in range(1, 20))
        authority = {
            "receipt_schema_version": 1,
            "plan_schema_version": 6,
            "task_id": "deterministic-task",
            "decision_schema_version": 4,
            "decision_revision": 4,
            "decision_fingerprint": next(hashes),
            "inventory_schema_version": 1,
            "inventory_revision": 2,
            "inventory_fingerprint": next(hashes),
            "source_ownership_schema_version": 1,
            "source_ownership_revision": 3,
            "source_ownership_fingerprint": next(hashes),
            "episode_ownership_schema_version": 1,
            "episode_ownership_fingerprint": next(hashes),
            "tv_planning_authority_schema_version": 0,
            "tv_planning_authority_policy_version": "not-applicable",
            "tv_planning_authority_fingerprint": next(hashes),
            "batch_scope_fingerprint": next(hashes),
            "reservation_lineage_id": next(hashes),
            "reservation_revision": 0,
            "work_identity_fingerprint": next(hashes),
            "series_binding_fingerprint": next(hashes),
            "season_binding_fingerprint": next(hashes),
            "target_routing_fingerprint": next(hashes),
            "naming_policy_fingerprint": next(hashes),
            "font_policy_fingerprint": next(hashes),
            "subtitle_policy_fingerprint": next(hashes),
            "execution_policy_fingerprint": next(hashes),
            "policy_bundle_version": "projection-policy-v1",
            "projector_version": "final-plan-projector-v1",
            "entry_set_fingerprint": next(hashes),
        }
        first = builder(authority)
        second = builder(dict(reversed(tuple(authority.items()))))
        first_payload = first.to_dict()
        second_payload = second.to_dict()
        self.assertEqual(first_payload, second_payload)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.fingerprint, fingerprint(authority))
        self.assertNotIn("plan_fingerprint", first_payload)
        self.assertNotIn("receipt_fingerprint", first_payload.get("authority", {}))
        for field in CORRELATION_ONLY_FIELDS:
            self.assertNotIn(field, first_payload.get("authority", {}))

    def test_after_plan_write_failure_cannot_leave_orphan_current_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_store = DecisionStateStore(root / "decisions")
            plan_store = PlanStore(root / "plans")
            plan = _simple_plan(root, "after-plan-write")
            original_save = plan_store.save
            module = _projection_api(self)
            envelope = module.ProjectionAuthorityEnvelope(
                previous_decision=None,
                decision=DecisionState(task_id=plan.task_id),
                draft_plan=plan,
                inventory_fingerprint=module.inventory_fingerprint_for_plan(plan),
            )
            service = module.ProjectionCommitService(state_store, plan_store)

            def save_then_fail(value):
                original_save(value)
                raise OSError("injected after Plan write")

            with patch.object(plan_store, "save", side_effect=save_then_fail):
                with self.assertRaises(OSError):
                    service.commit(envelope)
            self.assertIsNone(state_store.load_optional(plan.task_id))
            self.assertFalse(
                plan_store.path_for(plan.task_id).exists(),
                "rollback removed Decision but left an orphan current Plan",
            )

    def test_failure_before_plan_write_rolls_back_decision_without_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_store = DecisionStateStore(root / "decisions")
            plan_store = PlanStore(root / "plans")
            plan = _simple_plan(root, "before-plan-write")
            module = _projection_api(self)
            envelope = module.ProjectionAuthorityEnvelope(
                previous_decision=None,
                decision=DecisionState(task_id=plan.task_id),
                draft_plan=plan,
                inventory_fingerprint=module.inventory_fingerprint_for_plan(plan),
            )
            service = module.ProjectionCommitService(state_store, plan_store)
            with patch.object(
                plan_store, "save", side_effect=OSError("before Plan write")
            ):
                with self.assertRaises(OSError):
                    service.commit(envelope)
            self.assertIsNone(state_store.load_optional(plan.task_id))
            self.assertFalse(plan_store.path_for(plan.task_id).exists())

    def test_all_projection_journal_crash_windows_restart_fail_closed(self):
        module = _projection_api(self)
        envelope_type = module.ProjectionAuthorityEnvelope
        service_type = module.ProjectionCommitService
        inventory_fingerprint = module.inventory_fingerprint_for_plan

        for phase in COMMIT_PHASES[:-1]:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source" / "Episode 01.mkv"
                source.parent.mkdir(parents=True)
                source.write_bytes(b"projection crash-window source")
                plan = RenamePlan.from_mapping(
                    task_id=f"crash-{phase.replace('_', '-')}",
                    source_root=source.parent,
                    mapping={
                        source: root / "target" / "Season 01" / "Fixture S01E01.mkv"
                    },
                    metadata={"season_number": 1},
                )
                state_store = DecisionStateStore(root / "decisions")
                plan_store = PlanStore(root / "plans")
                decision = DecisionState(task_id=plan.task_id)
                envelope = envelope_type(
                    previous_decision=None,
                    decision=decision,
                    draft_plan=plan,
                    inventory_fingerprint=inventory_fingerprint(plan),
                )

                def fail_at(current_phase, *, expected=phase):
                    if current_phase == expected:
                        raise KeyboardInterrupt(f"injected crash after {expected}")

                service = service_type(
                    state_store,
                    plan_store,
                    journal_root=root / "journal",
                    _phase_hook=fail_at,
                )
                with self.assertRaisesRegex(KeyboardInterrupt, "injected crash"):
                    service.commit(envelope)

                pending = json.loads(
                    (root / "journal" / f"{plan.task_id}.json").read_text("utf-8")
                )
                self.assertEqual("pending", pending.get("status"))
                self.assertEqual(phase, pending.get("phase"))
                self.assertEqual(["task", "source"], pending.get("lock_order"))
                self.assertEqual(64, len(str(
                    pending.get("authority_input_fingerprint") or ""
                )))
                self.assertEqual(64, len(str(
                    pending.get("rollback_sha256") or ""
                )))
                self.assertTrue(
                    (root / "journal" / str(pending.get("rollback_ref"))).is_file()
                )
                if COMMIT_PHASES.index(phase) >= COMMIT_PHASES.index(
                    "decision_written"
                ):
                    self.assertEqual(64, len(str(
                        pending.get("current_decision_fingerprint") or ""
                    )))
                if COMMIT_PHASES.index(phase) >= COMMIT_PHASES.index(
                    "ownership_claimed"
                ):
                    self.assertEqual(64, len(str(
                        pending.get("current_ownership_fingerprint") or ""
                    )))
                if COMMIT_PHASES.index(phase) >= COMMIT_PHASES.index(
                    "plan_written"
                ):
                    self.assertEqual(64, len(str(
                        pending.get("current_plan_fingerprint") or ""
                    )))
                    self.assertEqual(64, len(str(
                        pending.get("authority_fingerprint") or ""
                    )))

                restarted = service_type(
                    state_store,
                    plan_store,
                    journal_root=root / "journal",
                )
                self.assertEqual("failed_closed", restarted.recover(plan.task_id))
                self.assertIsNone(state_store.load_optional(plan.task_id))
                self.assertFalse(plan_store.path_for(plan.task_id).exists())
                ownership = restarted.ownership_store.load_optional(plan.task_id)
                self.assertTrue(
                    ownership is None or ownership.state != "active",
                    f"{phase} restart left active ownership without a Plan",
                )


if __name__ == "__main__":
    unittest.main()
