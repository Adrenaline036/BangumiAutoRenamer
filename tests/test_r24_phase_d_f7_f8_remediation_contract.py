"""Incident contracts for the R24 Phase-D F7/F8 remediation.

All Provider-shaped data is sanitized and local.  Tests use temporary task
records only; they do not start NiceGUI, contact a Provider, or touch media.
"""

from __future__ import annotations
from tests.queue_fixture import TaskQueueManager as ControlledQueue

import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.pages.review_operation_view import (
    build_review_operation_presentation,
    dispatch_review_command_once,
)
from src.rename import provider_link_decision as decision_module
from src.rename.decision_state import DecisionStateStore
from src.rename.provider_link_decision import (
    freeze_provider_subject_boundary_edges,
    provider_subject_cohort_request_fingerprint,
    resolve_provider_subject_cohort_segment,
)
from src.rename.provider_link_resolver import collect_subject_observations
from src.rename.process import ProviderSearchError, Rename
from src.rename.review_operation import (
    ProviderSelectionAwaitingUserOutcome,
    TaskWorkflowState,
)
from src.rename.task_batch_scope import (
    TaskSourceScope,
    canonical_reservation_lineage_id,
    source_identity_set_fingerprint,
)
from src.rename.target_routing import TargetRootSnapshot
from src.task_queue import TaskJob, TaskQueueManager
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
    persist_provider_selection_attempt,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "r23_d_p1_incidents"
ACTIONABLE_SNAPSHOT = TargetRootSnapshot.capture({
    "bangumi_path": Path("D:/SANITIZED/TV"),
    "movie_path": Path("D:/SANITIZED/Movies"),
    "anime_path": Path("D:/SANITIZED/Anime"),
    "anime_movie_path": Path("D:/SANITIZED/AnimeMovies"),
})


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _provider_failure(*, live_conflict: bool = False) -> dict:
    if live_conflict:
        fixture = json.loads(
            (FIXTURE_ROOT / "p1_b_cohort_anchor.json").read_text("utf-8")
        )
        fixture = copy.deepcopy(fixture)
        row = next(
            item for item in fixture["subject_observations"]["subject_observations"]
            if item["bangumi_subject_id"] == "425211"
        )
        query = next(item for item in row["queries"] if item["locale"] == "original")
        candidate = next(
            item for item in query["candidates"]
            if str(item["tmdb_tv_id"]) == "332819"
        )
        candidate["match_tier"] = "exact"
        row.pop("response_fingerprint", None)
        row["response_fingerprint"] = _canonical_sha256(row)
        batch = fixture["subject_observations"]
        batch.pop("evidence_fingerprint", None)
        batch["evidence_fingerprint"] = _canonical_sha256(batch)
        selected = "293049"
    else:
        subjects = tuple({
            "provider_id": subject_id,
            "title": title,
            "original_title": title,
            "air_date": f"{year}-01-01",
            "episode_count": 2,
            "platform": "TV",
            "type": 2,
        } for subject_id, title, year in (
            ("valid-s1", "示例第一季", 2020),
            ("valid-s2", "示例第二季", 2021),
            ("valid-s3", "示例第三季", 2022),
        ))
        relations = (
            {"from": "valid-s1", "to": "valid-s2", "relation": "sequel"},
            {"from": "valid-s2", "to": "valid-s3", "relation": "sequel"},
        )

        def observe(subject):
            subject_id = str(subject["provider_id"])
            candidates = ()
            outcome = "no_match"
            if subject_id == "valid-s1":
                outcome = "ok"
                candidates = ({
                    "namespace": "tmdb",
                    "entity_type": "tv",
                    "provider_id": "8100",
                    "tmdb_tv_id": "8100",
                    "match_tier": "exact",
                    "title": "示例作品",
                    "original_title": "Example Work",
                    "first_air_date": "2020-01-01",
                    "seasons": tuple({
                        "season_number": number,
                        "air_date": f"{2019 + number}-01-01",
                        "episode_count": 2,
                    } for number in (1, 2, 3)),
                },)
            return {
                "bangumi_subject_id": subject_id,
                "observed_at": "2026-08-29T00:00:00+00:00",
                "policy_version": "subject-candidate-v1",
                "queries": ({
                    "locale": "original",
                    "query": str(subject["title"]),
                    "year": int(str(subject["air_date"])[:4]),
                    "outcome": outcome,
                    "truncated": False,
                    "candidates": candidates,
                },),
            }

        selected = "valid-s2"
        fixture = {
            "bangumi_subjects": subjects,
            "bangumi_relations": relations,
            "subject_observations": collect_subject_observations(
                selected_subject_id=selected,
                bangumi_subjects=subjects,
                bangumi_relations=relations,
                search_subject=observe,
                max_subjects=3,
            ),
        }
    edges = freeze_provider_subject_boundary_edges(
        selected_subject_id=selected,
        subject_observations=fixture["subject_observations"],
        bangumi_subjects=fixture["bangumi_subjects"],
        bangumi_relations=fixture["bangumi_relations"],
    )
    return {
        "schema_version": 1,
        "selected_subject_id": selected,
        "evidence_fingerprint": fixture["subject_observations"]["evidence_fingerprint"],
        "reason_codes": ["subject_candidate_evidence_missing"],
        "retry_requires_refresh": False,
        "subject_observations": fixture["subject_observations"],
        "bangumi_subjects": fixture["bangumi_subjects"],
        "bangumi_relations": fixture["bangumi_relations"],
        "provider_subject_boundary_edges": tuple(dict(item) for item in edges),
    }


def _selected_locale_conflict_failure() -> dict:
    """Select the First Kiss row that owns two exact TMDB identities."""

    failure = copy.deepcopy(_provider_failure(live_conflict=True))
    batch = failure["subject_observations"]
    batch["selected_subject_id"] = "425211"
    batch.pop("evidence_fingerprint", None)
    batch["evidence_fingerprint"] = _canonical_sha256(batch)
    failure["selected_subject_id"] = "425211"
    failure["evidence_fingerprint"] = batch["evidence_fingerprint"]
    failure["provider_subject_boundary_edges"] = freeze_provider_subject_boundary_edges(
        selected_subject_id="425211",
        subject_observations=batch,
        bangumi_subjects=failure["bangumi_subjects"],
        bangumi_relations=failure["bangumi_relations"],
    )
    return failure


def _worker_for_failure(failure: dict, root: Path) -> Rename:
    """Build the production selection seam from one frozen observation batch."""

    task_id = "live-conflict"
    decisions = root / "decisions"
    DecisionStateStore(decisions).create(task_id)
    batch = failure["subject_observations"]
    rows = {
        str(item["bangumi_subject_id"]): copy.deepcopy(item)
        for item in batch["subject_observations"]
    }
    isolated_config = SimpleNamespace(
        config={},
        get_config=lambda key: str(root / "configured" / key),
    )
    with (
        patch("src.rename.process.cm", isolated_config),
        patch("src.rename.process.Search", return_value=SimpleNamespace()),
    ):
        worker = Rename(
            decision_state_root=decisions,
            provider_link_catalog_root=root / "provider-links",
        )
    worker.bangumi_search = SimpleNamespace(
        load_crosswalk_evidence=lambda _subject_id: (
            tuple(copy.deepcopy(failure["bangumi_subjects"])),
            tuple(copy.deepcopy(failure["bangumi_relations"])),
        )
    )
    worker._observe_tmdb_tv_subject = lambda subject: copy.deepcopy(
        rows[str(subject.get("provider_id") or subject.get("id") or "")]
    )
    worker.search = SimpleNamespace(
        get_tv_info_by_id=Mock(
            side_effect=AssertionError("awaiting-user path read executable TMDB data")
        )
    )
    source_fingerprint = source_identity_set_fingerprint(())
    batch_fingerprint = "a" * 64
    worker.active_task_source_scope = TaskSourceScope(
        schema_version=1,
        task_id=task_id,
        media_domain="tv",
        batch_scope_fingerprint=batch_fingerprint,
        reservation_lineage_id=canonical_reservation_lineage_id(
            owner_task_id=task_id,
            origin_batch_scope_fingerprint=batch_fingerprint,
            source_identity_set_fingerprint=source_fingerprint,
        ),
        reservation_revision=0,
        source_identity_set_fingerprint=source_fingerprint,
        source_identities=(),
    )
    worker.active_consumer_task_id = task_id
    worker.active_provider_naming_selection = None
    worker.active_provider_link_failure = None
    return worker


def build_canonical_actionable_task(*, refresh: bool = False) -> dict[str, object]:
    """Return a full schema-3 confirm-only task from sanitized observations."""

    evidence = _provider_failure()
    task: dict[str, object] = {
        "uuid": "sanitized-f7-f8-task",
        "workflow_status": "need_confirm",
        "draft_root_snapshot": ACTIONABLE_SNAPSHOT.to_dict(),
        "draft_root_config_fingerprint": ACTIONABLE_SNAPSHOT.fingerprint,
    }
    persist_provider_selection_attempt(task, {
        "schema_version": 1,
        "state": "awaiting_user",
        "failure_code": "relation_unresolved",
        "reason_codes": ["season_window_date_locked"],
        "expected_decision_revision": 7,
        "evidence_fingerprint": evidence["evidence_fingerprint"],
        "retry_requires_refresh": refresh,
        "candidate": (
            {"namespace": "bangumi", "provider_id": "valid-s2"}
            if refresh else None
        ),
        "boundary_resolution_failure": {
            "public_title": "季度边界需要确认",
            "affected_subject_titles": ["示例续季"],
            "technical_summary": "sanitized boundary summary",
        },
        "provider_subject_evidence": evidence,
    })
    return task


def _persist_failed_attempt(
    evidence: dict,
    root: Path,
    *,
    awaiting_user: bool = False,
) -> dict:
    task_id = "live-conflict" if evidence["selected_subject_id"] == "293049" else "valid"
    request_id = f"request-{task_id}"
    task_path = root / "tasks" / f"{task_id}.json"
    task = {
        "uuid": task_id,
        "workflow_status": "queued",
        "draft_root_snapshot": ACTIONABLE_SNAPSHOT.to_dict(),
        "draft_root_config_fingerprint": ACTIONABLE_SNAPSHOT.fingerprint,
        "provider_selection_failure": evidence,
    }
    persist_provider_selection_attempt(task, {
        "schema_version": 1,
        "request_id": request_id,
        "state": "pending",
        "expected_decision_revision": 3,
        "previous_workflow_status": "need_confirm",
        "candidate": {"namespace": "bangumi", "provider_id": evidence["selected_subject_id"]},
    })
    if awaiting_user:
        persist_provider_selection_attempt(task, {
            "schema_version": 1,
            "request_id": request_id,
            "state": "awaiting_user",
            "expected_decision_revision": 3,
            "previous_workflow_status": "need_confirm",
            "failure_code": "relation_unresolved",
            "reason_codes": list(evidence.get("reason_codes", ()) or ()),
            "evidence_fingerprint": evidence["evidence_fingerprint"],
            "provider_subject_evidence": evidence,
        })
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    if awaiting_user:
        return ControlledQueue(
            task_root=root / "tasks",
            plan_root=root / "plans",
            decision_state_root=root / "decisions",
            recovery_abandon_audit_root=root / "audit-restart",
            test_mode=True,
        )._read(task_id)
    manager = ControlledQueue(
        task_root=root / "tasks",
        plan_root=root / "plans",
        decision_state_root=root / "decisions",
        recovery_abandon_audit_root=root / "audit",
        test_mode=True,
    )
    manager._fail_reidentify(
        TaskJob(
            task_id=task_id,
            path=root / "source",
            request_id=request_id,
            operation="reidentify",
        ),
        RuntimeError("季度匹配需要人工确认"),
    )
    return ControlledQueue(
        task_root=root / "tasks",
        plan_root=root / "plans",
        decision_state_root=root / "decisions",
        recovery_abandon_audit_root=root / "audit-restart",
        test_mode=True,
    )._read(task_id)


class CommandCapabilityContracts(unittest.TestCase):
    def test_distal_live_locale_conflict_persists_actionable_after_queue_restart(self):
        evidence = _provider_failure(live_conflict=True)
        segment = resolve_provider_subject_cohort_segment(
            selected_subject_id="293049",
            subject_observations=evidence["subject_observations"],
            bangumi_subjects=evidence["bangumi_subjects"],
            bangumi_relations=evidence["bangumi_relations"],
        )
        self.assertEqual(
            ("248175", "293049", "317613"),
            segment.segment_subject_ids,
        )
        self.assertEqual(("248175",), segment.anchor_subject_ids)
        self.assertEqual(({
            "from_subject_id": "317613",
            "to_subject_id": "425211",
            "relation": "sequel",
            "boundary_kind": "season_incompatible_manual",
            "outside_tmdb_tv_id": "",
        },), segment.resolution.boundaries)
        self.assertEqual(1, len(evidence["provider_subject_boundary_edges"]))
        self.assertEqual(
            "83121",
            evidence["provider_subject_boundary_edges"][0]["anchor_tmdb_tv_id"],
        )
        with tempfile.TemporaryDirectory() as directory:
            restarted = _persist_failed_attempt(
                evidence,
                Path(directory),
                awaiting_user=True,
            )
        operation = restarted["review_operation"]
        presentation = build_review_operation_presentation(restarted)
        self.assertEqual("actionable", operation["state"])
        self.assertEqual(
            ["confirm_cohort", "separate_work"],
            [item["kind"] for item in operation["commands"]],
        )
        self.assertTrue(any(
            action.key == "command-confirm_cohort" for action in presentation.actions
        ))
        request = operation["context"]["provider_subject_cohort_preview"]["request"]
        self.assertEqual("83121", request["selected_tmdb_tv_id"])
        self.assertEqual(
            {"248175": 1, "293049": 2, "317613": 3},
            request["season_assignments"],
        )
        self.assertNotIn("425211", request["cohort_subject_ids"])

    def test_selected_locale_conflict_remains_blocked_after_restart(self):
        evidence = _selected_locale_conflict_failure()
        self.assertEqual((), evidence["provider_subject_boundary_edges"])
        with tempfile.TemporaryDirectory() as directory:
            restarted = _persist_failed_attempt(evidence, Path(directory))
        operation = restarted["review_operation"]
        presentation = build_review_operation_presentation(restarted)
        self.assertEqual("blocked", operation["state"])
        self.assertEqual([], operation["commands"])
        self.assertFalse(any(
            action.key.startswith("command-") for action in presentation.actions
        ))

    def test_valid_zero_edge_persists_only_satisfiable_confirm_command(self):
        evidence = _provider_failure()
        self.assertEqual(1, len(evidence["provider_subject_boundary_edges"]))
        with tempfile.TemporaryDirectory() as directory:
            restarted = _persist_failed_attempt(
                evidence,
                Path(directory),
                awaiting_user=True,
            )
        operation = restarted["review_operation"]
        preview = operation["context"]["provider_subject_cohort_preview"]
        self.assertEqual("actionable", operation["state"])
        self.assertEqual(
            ["confirm_cohort", "separate_work"], [item["kind"] for item in operation["commands"]]
        )
        self.assertEqual(3, preview["schema_version"])
        self.assertEqual(
            preview["request_fingerprint"],
            provider_subject_cohort_request_fingerprint(preview["request"]),
        )

    def test_rebuilds_repairable_preview_but_blocks_persisted_tamper_or_target_drift(self):
        repairable = build_canonical_actionable_task()
        attempt = current_provider_selection_context(repairable)
        attempt["provider_subject_cohort_preview"].pop("request", None)
        attempt["provider_subject_cohort_preview"].pop("request_fingerprint", None)
        persist_provider_selection_attempt(repairable, attempt)
        repaired = current_provider_selection_context(repairable)[
            "provider_subject_cohort_preview"
        ]
        self.assertEqual(3, repaired["schema_version"])
        self.assertIsInstance(repaired["request"], dict)

        tampered = copy.deepcopy(repairable)
        tampered["review_operation"]["preview"].pop("request", None)
        tampered["review_operation"]["context"][
            "provider_subject_cohort_preview"
        ].pop("request", None)

        drifted = build_canonical_actionable_task()
        attempt = current_provider_selection_context(drifted)
        preview = attempt["provider_subject_cohort_preview"]
        request = dict(preview["request"])
        request["target_root_config_fingerprint"] = "f" * 64
        preview["request"] = request
        preview["request_fingerprint"] = provider_subject_cohort_request_fingerprint(request)
        persist_provider_selection_attempt(drifted, attempt)
        self.assertNotEqual('f' * 64, current_provider_selection_context(drifted)[
            'provider_subject_cohort_preview']['request']['target_root_config_fingerprint'],
            'untrusted input preview is rebuilt from the actual frozen roots')
        # Now corrupt the persisted object, not the regenerable builder input.
        for preview in (drifted['review_operation']['preview'],
                        drifted['review_operation']['context']['provider_subject_cohort_preview']):
            preview['request']['target_root_config_fingerprint'] = 'f' * 64
            preview['request_fingerprint'] = provider_subject_cohort_request_fingerprint(preview['request'])

        for task in (tampered, drifted):
            presentation = build_review_operation_presentation(task)
            self.assertEqual(TaskWorkflowState.BLOCKED, presentation.state)
            self.assertFalse(any(
                action.key.startswith("command-") for action in presentation.actions
            ))


class TypedDispatchResultContracts(unittest.TestCase):
    def _command(self):
        task = build_canonical_actionable_task()
        presentation = build_review_operation_presentation(task)
        command = next(
            action.payload for action in presentation.actions
            if action.key == "command-confirm_cohort"
        )
        return task, command

    def test_rejected_callback_releases_key_and_allows_retry(self):
        task, command = self._command()
        rejected = Mock(return_value="rejected")
        dispatched: set[str] = set()
        self.assertFalse(dispatch_review_command_once(task, command, dispatched, rejected))
        self.assertFalse(dispatch_review_command_once(task, command, dispatched, rejected))
        self.assertEqual(2, rejected.call_count)
        self.assertEqual(set(), dispatched)

    def test_exception_releases_key_while_applied_and_already_applied_retain_it(self):
        task, command = self._command()
        dispatched: set[str] = set()
        with self.assertRaisesRegex(RuntimeError, "sanitized callback failure"):
            dispatch_review_command_once(
                task,
                command,
                dispatched,
                Mock(side_effect=RuntimeError("sanitized callback failure")),
            )
        self.assertEqual(set(), dispatched)
        for result in ("applied", "already_applied"):
            with self.subTest(result=result):
                keys: set[str] = set()
                self.assertTrue(dispatch_review_command_once(
                    task, command, keys, Mock(return_value=result)
                ))
                self.assertFalse(dispatch_review_command_once(
                    task, command, keys, Mock(return_value=result)
                ))
                self.assertEqual({command["idempotency_key"]}, keys)


class ProductionSelectionReachabilityContracts(unittest.TestCase):
    def test_distal_conflict_reaches_real_worker_as_typed_awaiting_user(self):
        failure = _provider_failure(live_conflict=True)
        with tempfile.TemporaryDirectory() as directory:
            worker = _worker_for_failure(failure, Path(directory))
            outcome = worker._resolve_bangumi_provider_selection({
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": "293049",
                "title": "辉夜大小姐想让我告白 第二季",
                "original_title": "Kaguya-sama Season 2",
            })
        self.assertIsInstance(outcome, ProviderSelectionAwaitingUserOutcome)
        evidence = dict(outcome.provider_subject_evidence)
        self.assertEqual(1, len(evidence["provider_subject_boundary_edges"]))
        self.assertEqual(
            "83121",
            evidence["provider_subject_boundary_edges"][0]["anchor_tmdb_tv_id"],
        )

    def test_selected_conflict_or_canonical_disconnect_never_reaches_awaiting_user(self):
        failure = _provider_failure(live_conflict=True)
        for label, disconnected in (("canonical-disconnect", True),):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                worker = _worker_for_failure(failure, Path(directory))
                context = (
                    patch.object(
                        decision_module,
                        "resolve_provider_subject_cohort_segment",
                        side_effect=ValueError("canonical segment disconnected"),
                    )
                    if disconnected else patch.object(
                        decision_module,
                        "resolve_provider_subject_cohort_segment",
                        wraps=decision_module.resolve_provider_subject_cohort_segment,
                    )
                )
                with context, self.assertRaises(ProviderSearchError):
                    worker._resolve_bangumi_provider_selection({
                        "namespace": "bangumi",
                        "entity_type": "subject",
                        "provider_id": "293049",
                    })

    def test_selected_conflict_keeps_tmdb_optional_for_bangumi_primary(self):
        failure = _selected_locale_conflict_failure()
        with tempfile.TemporaryDirectory() as directory:
            worker = _worker_for_failure(failure, Path(directory))
            outcome = worker._resolve_bangumi_provider_selection({
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": "425211",
            })
        self.assertNotIsInstance(outcome, ProviderSelectionAwaitingUserOutcome)
        _name, info, resolution = outcome
        self.assertEqual(
            ("bangumi", "subject", "425211"),
            tuple(
                info["_bar_primary_provider"][field]
                for field in ("namespace", "entity_type", "provider_id")
            ),
        )
        self.assertIsNone(resolution.tmdb_tv_id)
        self.assertEqual({"425211": 1}, dict(resolution.season_assignments))
        self.assertIn(
            "bangumi_primary_tmdb_metadata_ambiguous",
            resolution.reason_codes,
        )

    def test_blocked_raw_conflict_requires_complete_canonical_anchor_edge(self):
        failure = _provider_failure(live_conflict=True)
        edge = dict(failure["provider_subject_boundary_edges"][0])
        for label, mutation in (
            ("missing-anchor-id", lambda item: item.pop("anchor_tmdb_tv_id")),
            ("missing-anchor-fingerprints", lambda item: item.pop(
                "anchor_evidence_fingerprints"
            )),
            ("wrong-evidence", lambda item: item.update(
                evidence_fingerprint="f" * 64
            )),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                malformed = copy.deepcopy(edge)
                mutation(malformed)
                worker = _worker_for_failure(failure, Path(directory))
                with (
                    patch(
                        "src.rename.process.freeze_provider_subject_boundary_edges",
                        return_value=(malformed,),
                    ),
                    self.assertRaises(ProviderSearchError),
                ):
                    worker._resolve_bangumi_provider_selection({
                        "namespace": "bangumi",
                        "entity_type": "subject",
                        "provider_id": "293049",
                    })


if __name__ == "__main__":
    unittest.main()
