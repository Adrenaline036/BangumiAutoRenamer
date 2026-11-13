"""Phase-B GREEN contracts for the canonical cross-task cohort authority."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename.decision_state import DecisionStateStore
from src.rename.plan_store import PlanStore
from src.rename.process import Rename
from src.rename.projection_authority import (
    ProjectionAuthorityEnvelope,
    ProjectionCommitService,
    inventory_fingerprint_for_plan,
)
from src.rename.provider_search import ProviderSearchError
from src.rename.rename_plan import RenamePlan
from src.rename.successor_authority import (
    CohortAuthority,
    CohortAuthorityDraft,
    CohortAuthorityReader,
    CohortAuthorityStore,
    SuccessorAuthorityError,
    successor_authority_root,
)
from tests.test_r24_successor_authority_incident_red_contract import (
    _cohort_case,
    _confirmed_s1_authority,
    _resolver_seam_worker,
)


FORBIDDEN_ROUTING_FIELDS = {
    "canonical_root", "resolved_target_root", "target_root", "source_path",
}


class _FailingPlanStore(PlanStore):
    def save(self, plan: RenamePlan) -> Path:
        raise OSError("injected Plan failure")


def _material(root: Path):
    reference_root = root / "reference" / "decisions"
    source_decision = _confirmed_s1_authority(reference_root)
    authority = CohortAuthorityStore(
        successor_authority_root(reference_root)
    ).list_current()[0]
    boundary = deepcopy(next(iter(
        source_decision.provider_subject_boundary_decisions.values()
    )))
    draft = CohortAuthorityDraft.from_confirmation(
        boundary_decision=boundary,
        work_identity=deepcopy(source_decision.confirmed_work),
        resolution=authority.project("41658")[2],
        provider_snapshot=deepcopy(authority.provider_snapshot),
        title_selection=deepcopy(authority.title_selection),
    )
    return source_decision, authority, draft


def _transaction(
    root: Path,
    task_id: str,
    *,
    state_store: DecisionStateStore | None = None,
    plan_store: PlanStore | None = None,
    service: ProjectionCommitService | None = None,
    draft: CohortAuthorityDraft | None = None,
    confirmed_work=None,
    boundary_decisions=None,
):
    reference, _authority, base_draft = _material(root / f"material-{task_id}")
    cohort_draft = draft or base_draft
    decisions = state_store or DecisionStateStore(root / "decisions")
    plans = plan_store or PlanStore(root / "plans")
    previous = decisions.create(task_id)
    candidate = replace(
        previous,
        confirmed_work=deepcopy(
            confirmed_work
            if confirmed_work is not None else reference.confirmed_work
        ),
        provider_subject_boundary_decisions=deepcopy(
            boundary_decisions
            if boundary_decisions is not None
            else reference.provider_subject_boundary_decisions
        ),
    )
    plan = RenamePlan(task_id, root / "source", ())
    envelope = ProjectionAuthorityEnvelope(
        previous_decision=previous,
        decision=candidate,
        draft_plan=plan,
        inventory_fingerprint=inventory_fingerprint_for_plan(plan),
        cohort_authority=cohort_draft,
    )
    commit_service = service or ProjectionCommitService(
        decisions,
        plans,
        journal_root=root / "journal",
    )
    return commit_service, decisions, plans, previous, envelope, cohort_draft


def _routing_keys(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in FORBIDDEN_ROUTING_FIELDS:
                found.add(normalized)
            found.update(_routing_keys(item))
    elif isinstance(value, (tuple, list)):
        for item in value:
            found.update(_routing_keys(item))
    return found


class SuccessorAuthorityAtomicGreenContracts(unittest.TestCase):
    def test_commit_publishes_decision_plan_and_cohort_authority_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, states, plans, _previous, envelope, _draft = _transaction(
                root, "producer-s1"
            )
            written, projected = service.commit(envelope)

            records = service.cohort_authority_store.list_current()
            self.assertEqual(1, written.revision)
            self.assertEqual(projected, plans.load("producer-s1"))
            self.assertEqual(1, len(records))
            self.assertEqual("producer-s1", records[0].source_task_id)
            lookup = CohortAuthorityReader(
                service.cohort_authority_store, states
            ).lookup("86670", consumer_task_id="consumer-s2")
            self.assertEqual(("matched", "current_authority_matched"), (
                lookup.status, lookup.reason_code,
            ))
            self.assertEqual(2, lookup.authority.season_assignments["86670"])

    def test_plan_write_failure_rolls_back_decision_and_cohort_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plans = _FailingPlanStore(root / "plans")
            service, states, plans, previous, envelope, _draft = _transaction(
                root, "rollback-s1", plan_store=plans
            )
            with self.assertRaisesRegex(OSError, "injected Plan failure"):
                service.commit(envelope)

            self.assertEqual(previous, states.load("rollback-s1"))
            self.assertFalse(plans.path_for("rollback-s1").exists())
            self.assertEqual((), service.cohort_authority_store.list_current())

    def test_restart_recovers_authority_published_at_decision_blind_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            states = DecisionStateStore(root / "decisions")
            plans = PlanStore(root / "plans")

            def interrupt(phase: str) -> None:
                if phase == "decision_written":
                    raise KeyboardInterrupt("injected authority blind window")

            service = ProjectionCommitService(
                states,
                plans,
                journal_root=root / "journal",
                _phase_hook=interrupt,
            )
            service, states, plans, previous, envelope, _draft = _transaction(
                root,
                "restart-s1",
                state_store=states,
                plan_store=plans,
                service=service,
            )
            with self.assertRaisesRegex(
                KeyboardInterrupt, "authority blind window"
            ):
                service.commit(envelope)
            self.assertEqual(1, len(
                service.cohort_authority_store.list_current()
            ))

            restarted = ProjectionCommitService(
                states,
                plans,
                journal_root=root / "journal",
            )
            self.assertEqual("failed_closed", restarted.recover("restart-s1"))
            self.assertEqual(previous, states.load("restart-s1"))
            self.assertFalse(plans.path_for("restart-s1").exists())
            self.assertEqual((), restarted.cohort_authority_store.list_current())


class SuccessorAuthorityValidationGreenContracts(unittest.TestCase):
    def test_schema_and_all_publish_inputs_forbid_target_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, authority, draft = _material(root)
            self.assertEqual(set(), _routing_keys(authority.to_dict()))

            bad_boundary = deepcopy(draft.boundary_decision)
            bad_boundary["canonical_root"] = "X:/must-not-cross"
            with self.assertRaisesRegex(
                SuccessorAuthorityError, "authority_contains_target_routing"
            ):
                CohortAuthorityDraft.from_confirmation(
                    boundary_decision=bad_boundary,
                    work_identity=draft.work_identity,
                    resolution=draft.resolution,
                    provider_snapshot=draft.provider_snapshot,
                    title_selection=draft.title_selection,
                )

            values = dict(authority.__dict__)
            values.pop("authority_fingerprint")
            values["provider_snapshot"] = {
                **dict(authority.provider_snapshot),
                "target_root": "X:/must-not-cross",
            }
            with self.assertRaisesRegex(
                SuccessorAuthorityError, "authority_contains_target_routing"
            ):
                CohortAuthority.create(**values)

            tampered = authority.to_dict()
            tampered["canonical_root"] = "X:/must-not-cross"
            with self.assertRaises(SuccessorAuthorityError):
                CohortAuthority.from_dict(tampered)
            self.assertEqual(1, source.revision)

    def test_canonical_root_changes_producer_fingerprint_not_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions_a = root / "a" / "decisions"
            decisions_b = root / "b" / "decisions"
            producer_a = _confirmed_s1_authority(
                decisions_a, canonical_root=root / "route-a"
            )
            producer_b = _confirmed_s1_authority(
                decisions_b, canonical_root=root / "route-b"
            )
            authority_a = CohortAuthorityStore(
                successor_authority_root(decisions_a)
            ).list_current()[0]
            authority_b = CohortAuthorityStore(
                successor_authority_root(decisions_b)
            ).list_current()[0]

            self.assertNotEqual(
                DecisionStateStore.fingerprint(producer_a),
                DecisionStateStore.fingerprint(producer_b),
            )
            self.assertEqual(
                authority_a.project("86670"), authority_b.project("86670")
            )
            self.assertNotIn(str(root / "route-a"), str(authority_a.to_dict()))
            self.assertNotIn(str(root / "route-b"), str(authority_b.to_dict()))
            self.assertEqual(set(), _routing_keys(authority_a.to_dict()))
            self.assertEqual(set(), _routing_keys(authority_b.to_dict()))

    def test_producer_drift_and_same_task_reentry_preserve_v3_relationship(self):
        with tempfile.TemporaryDirectory() as directory:
            decisions = Path(directory) / "decisions"
            producer = _confirmed_s1_authority(decisions)
            reader = CohortAuthorityReader.from_decision_root(decisions)
            same_task = reader.lookup(
                "86670", consumer_task_id=producer.task_id
            )
            self.assertEqual(
                ("matched", "current_authority_matched"),
                (same_task.status, same_task.reason_code),
            )
            changed_season = deepcopy(producer.season_binding)
            changed_season["season_number"] = 2
            DecisionStateStore(decisions).save(
                replace(producer, season_binding=changed_season),
                expected_revision=producer.revision,
            )
            drifted = reader.lookup("86670", consumer_task_id="consumer-s2")
            self.assertEqual(
                ("matched", "current_authority_matched"),
                (drifted.status, drifted.reason_code),
            )
            self.assertEqual(same_task.authority, drifted.authority)

    def test_policy_fingerprint_subject_and_work_mismatches_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            source, authority, draft = _material(Path(directory))
            wrong_policy = authority.to_dict()
            wrong_policy["policy_version"] = "stale-policy"
            with self.assertRaisesRegex(
                SuccessorAuthorityError, "authority_header_invalid"
            ):
                CohortAuthority.from_dict(wrong_policy)

            wrong_fingerprint = authority.to_dict()
            wrong_fingerprint["authority_fingerprint"] = "0" * 64
            with self.assertRaisesRegex(
                SuccessorAuthorityError, "authority_fingerprint_mismatch"
            ):
                CohortAuthority.from_dict(wrong_fingerprint)

            with self.assertRaisesRegex(
                SuccessorAuthorityError, "authority_subject_mismatch"
            ):
                authority.project("subject-outside-cohort")

            wrong_work = deepcopy(draft.work_identity)
            wrong_work["primary_provider"]["provider_id"] = "99999"
            mismatched = CohortAuthorityDraft.from_confirmation(
                boundary_decision=draft.boundary_decision,
                work_identity=wrong_work,
                resolution=draft.resolution,
                provider_snapshot=draft.provider_snapshot,
                title_selection=draft.title_selection,
            )
            with self.assertRaisesRegex(
                SuccessorAuthorityError, "authority_work_identity_mismatch"
            ):
                mismatched.bind(source, authority_revision=1)

    def test_inconsistent_current_registration_proof_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, states, plans, _previous, envelope, draft = _transaction(
                root, "producer-a"
            )
            first.commit(envelope)

            alt_work = deepcopy(draft.work_identity)
            alt_work["primary_provider"]["provider_id"] = "99999"
            alt_boundary = deepcopy(draft.boundary_decision)
            alt_boundary["selected_tmdb_tv_id"] = "99999"
            alt_resolution = replace(
                draft.resolution,
                tmdb_tv_id="99999",
                proposed_series_key="tmdb-tv-99999",
            )
            alt_snapshot = deepcopy(draft.provider_snapshot)
            alt_snapshot["provider_id"] = "99999"
            alt_snapshot["tmdb_tv_id"] = "99999"
            alt_draft = CohortAuthorityDraft.from_confirmation(
                boundary_decision=alt_boundary,
                work_identity=alt_work,
                resolution=alt_resolution,
                provider_snapshot=alt_snapshot,
                title_selection=draft.title_selection,
            )
            reference, _unused, _unused_draft = _material(root / "alt")
            alt_rows = deepcopy(reference.provider_subject_boundary_decisions)
            for row in alt_rows.values():
                row["selected_tmdb_tv_id"] = "99999"
            second, _states, _plans, _previous, alt_envelope, _draft = (
                _transaction(
                    root,
                    "producer-b",
                    state_store=states,
                    plan_store=plans,
                    service=first,
                    draft=alt_draft,
                    confirmed_work=alt_work,
                    boundary_decisions=alt_rows,
                )
            )
            second.commit(alt_envelope)

            conflict = CohortAuthorityReader(
                first.cohort_authority_store, states
            ).lookup("86670", consumer_task_id="consumer-s2")
            self.assertEqual(
                ("invalid", "authority_registration_mismatch"),
                (conflict.status, conflict.reason_code),
            )


class SuccessorAuthorityProductionWiringGreenContracts(unittest.TestCase):
    def test_process_wires_reader_before_any_provider_fallback(self):
        process_source = inspect.getsource(Rename._process)
        resolver_source = inspect.getsource(
            Rename._resolve_bangumi_provider_selection
        )
        self.assertIn("_resolve_bangumi_provider_selection", process_source)
        self.assertLess(
            resolver_source.index("CohortAuthorityReader.from_decision_root"),
            resolver_source.index("ProviderLinkCatalog("),
        )
        self.assertLess(
            resolver_source.index(".lookup("),
            resolver_source.index("resolve_provider_link("),
        )
        self.assertLess(
            process_source.index("active_consumer_task_id"),
            process_source.index("_resolve_bangumi_provider_selection"),
        )

    def test_exact_process_task_identity_does_not_require_source_scope(self):
        case = _cohort_case()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = root / "decisions"
            _confirmed_s1_authority(decisions)
            worker = _resolver_seam_worker(
                case,
                task_id="unused-scope-id",
                decision_state_root=decisions,
                provider_link_catalog_root=root / "provider-links",
            )
            worker.active_task_source_scope = None
            worker.active_consumer_task_id = "consumer-s2"
            _name, _info, resolution = (
                worker._resolve_bangumi_provider_selection(
                    dict(worker.active_provider_selection)
                )
            )
            self.assertEqual(2, dict(resolution.season_assignments)["86670"])
            self.assertEqual([], worker.search.calls)
            self.assertEqual(0, worker.bangumi_search.calls)

    def test_corrupted_relationship_authority_never_falls_back_to_provider_io(self):
        case = _cohort_case()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = root / "decisions"
            _confirmed_s1_authority(decisions)
            store = CohortAuthorityStore(successor_authority_root(decisions))
            authority = store.list_current()[0]
            payload = authority.to_dict()
            payload['authority_fingerprint'] = '0' * 64
            store.path_for(authority.authority_id).write_text(
                json.dumps(payload), encoding='utf-8')
            worker = _resolver_seam_worker(
                case,
                task_id="consumer-s2",
                decision_state_root=decisions,
                provider_link_catalog_root=root / "provider-links",
            )
            with self.assertRaisesRegex(
                ProviderSearchError, "已经变化或存在冲突"
            ):
                worker._resolve_bangumi_provider_selection(
                    dict(worker.active_provider_selection)
                )
            self.assertEqual([], worker.search.calls)
            self.assertEqual(0, worker.bangumi_search.calls)


if __name__ == "__main__":
    unittest.main()
