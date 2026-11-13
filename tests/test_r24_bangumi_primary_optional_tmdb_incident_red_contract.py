"""Incident RED: Bangumi TV identity must not require a TMDB TV work.

The fixture preserves the First Kiss provider/entity mismatch without real
paths, credentials, browser state or raw Provider payloads.  These contracts
must fail before GREEN and then remain ordinary unwrapped regression tests.
"""

from __future__ import annotations

from tests._approval_stage_helpers import approve_dialog
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from src.rename.decision_state import (
    DECISION_STATE_SCHEMA_VERSION,
    DecisionState,
    DecisionStateError,
    DecisionStateStore,
)
from src.rename.plan_store import PlanStore, PlanStoreError
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_review import PlanReviewError, PlanReviewService
from src.rename.projection_authority import TaskEpisodeOwnership
from src.rename.provider_link_resolver import (
    ProviderLinkResolution,
    collect_subject_observations,
    resolve_provider_link,
    subject_is_explicit_no_match,
)
from src.rename.provider_link_decision import (
    resolve_provider_subject_boundary_decision,
)
from src.rename.rename_plan import (
    PLAN_SCHEMA_VERSION,
    ProviderIdentity,
    RenamePlan,
)
from src.rename.task_batch_scope import (
    TaskSourceScope,
    canonical_reservation_lineage_id,
    source_identity_set_fingerprint,
)
from src.rename.tv_planning_authority import (
    TV_PLANNING_AUTHORITY_POLICY_VERSION,
    TV_PLANNING_AUTHORITY_SCHEMA_VERSION,
    TVPlanningAuthorityBuilder,
)
from src.rename.execution_recovery import (
    ExecutionRecoveryError,
    reconcile_catalog_update,
)
from src.rename.successor_authority import CohortAuthority, CohortAuthorityDraft
from src.task_queue import TaskQueueManager
from tests.test_r22_tv_planning_authority_red_contract import (
    _authority_dict,
    _typed_input,
)
import tests.test_provider_subject_plan_wiring_red_contract as _wiring


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "r24_bangumi_primary_optional_tmdb_incident.json"
)


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _incident_case() -> dict[str, object]:
    incident = deepcopy(_fixture()["incident"])
    selected = dict(incident["selected_subject"])
    optional_links = [
        dict(item) for item in incident["optional_metadata_links"]
    ]
    source = "First-Kiss/First Kiss [01].mkv"
    return {
        "task_id": "first-kiss-bangumi-primary",
        "media_domain": "tv",
        "source_scope": [source],
        "observations": [],
        "selected_subject_id": selected["provider_id"],
        "tmdb_tv_id": None,
        "source_owned_segments": [{
            "bangumi_subject_id": selected["provider_id"],
            "season_number": 1,
            "episode_start": 1,
            "episode_end": 4,
            "sources": [source],
            "evidence_fingerprint": "a" * 64,
        }],
        "user_decisions": {
            "decision_revision": 1,
            "confirmed_identity": (
                selected["namespace"],
                selected["entity_type"],
                selected["provider_id"],
            ),
            "confirmed_display_title": selected["title"],
            "title_source": (
                selected["namespace"],
                selected["entity_type"],
                selected["provider_id"],
            ),
            "metadata_provider_identities": optional_links,
            "season_assignments": {selected["provider_id"]: 1},
            "cohort_action": "standalone_work",
        },
    }


class BangumiPrimaryAuthorityIncidentRedContracts(unittest.IsolatedAsyncioTestCase):
    def test_fixture_is_sanitized_and_freezes_cross_entity_metadata(self):
        payload = _fixture()
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.assertEqual(
            "r24-bangumi-primary-optional-tmdb/v2",
            payload["schema_version"],
        )
        expected = payload["incident"]["expected"]
        self.assertEqual(
            ["bangumi", "subject", "425211"],
            expected["primary_identity"],
        )
        self.assertEqual(
            ["tmdb", "movie", "997317"],
            expected["optional_movie_identity"],
        )
        for forbidden in (
            "d:\\\\anime",
            "c:\\\\users",
            "api_key",
            "authorization:",
            "cookie",
            "outerhtml",
        ):
            self.assertNotIn(forbidden, serialized.casefold())

    def test_complete_no_match_is_frozen_as_standalone_bangumi_authority(self):
        selected = dict(_fixture()["incident"]["selected_subject"])
        subject = {
            **selected,
            "name_cn": selected["title"],
            "name": selected["original_title"],
            "date": selected["air_date"],
            "eps": 4,
            "episode_count": 4,
        }
        detail_read = Mock(
            side_effect=AssertionError(
                "complete no-match attempted an executable TMDB detail read"
            )
        )
        batch = collect_subject_observations(
            selected_subject_id="425211",
            bangumi_subjects=(subject,),
            bangumi_relations=(),
            search_subject=lambda _subject: {
                "bangumi_subject_id": "425211",
                "observed_at": "2026-09-04T00:00:00+00:00",
                "policy_version": "subject-candidate-v1",
                "queries": ({
                    "locale": "original",
                    "query": subject["original_title"],
                    "year": 2022,
                    "outcome": "no_match",
                    "truncated": False,
                    "candidates": (),
                },),
            },
            max_subjects=1,
        )
        resolution = resolve_provider_link(
            selected_provider={
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": "425211",
            },
            bangumi_subjects=(subject,),
            bangumi_relations=(),
            subject_observations=batch,
        )
        self.assertTrue(subject_is_explicit_no_match(batch, "425211"))
        self.assertEqual("reviewable", resolution.review_disposition)
        self.assertIsNone(resolution.tmdb_tv_id)
        self.assertEqual({"425211": 1}, dict(resolution.season_assignments))
        self.assertEqual(batch, resolution.subject_observations)
        self.assertIn(str(batch["evidence_fingerprint"]), resolution.evidence_path)
        detail_read.assert_not_called()

    def test_deleted_or_merged_tmdb_candidate_creates_new_bangumi_resolution(self):
        drift = _fixture()["provider_drift_incident"]
        selected = {
            "namespace": "bangumi",
            "entity_type": "subject",
            "provider_id": str(drift["selected_subject_id"]),
        }
        subject = {
            **selected,
            "title": "脱敏季度作品",
            "original_title": "Sanitized Seasonal Work",
            "air_date": "2022-12-17",
            "episode_count": 4,
            "platform": "TV",
        }
        initial = collect_subject_observations(
            selected_subject_id="425211",
            bangumi_subjects=(subject,),
            bangumi_relations=(),
            search_subject=lambda _subject: {
                "bangumi_subject_id": "425211",
                "observed_at": drift["initial_observation"]["observed_at"],
                "policy_version": "subject-candidate-v1",
                "queries": ({
                    "locale": "original",
                    "query": subject["original_title"],
                    "year": 2022,
                    "outcome": "ok",
                    "truncated": False,
                    "candidates": ({
                        "namespace": "tmdb",
                        "entity_type": "tv",
                        "provider_id": "330299",
                        "tmdb_tv_id": "330299",
                        "match_tier": "exact",
                        "seasons": ({
                            "season_number": 1,
                            "episode_count": 4,
                            "air_date": "2022-12-17",
                        },),
                    },),
                },),
            },
            max_subjects=1,
        )
        replacement = collect_subject_observations(
            selected_subject_id="425211",
            bangumi_subjects=(subject,),
            bangumi_relations=(),
            search_subject=lambda _subject: {
                "bangumi_subject_id": "425211",
                "observed_at": drift["replacement_observation"]["observed_at"],
                "policy_version": "subject-candidate-v1",
                "queries": ({
                    "locale": "original",
                    "query": subject["original_title"],
                    "year": 2022,
                    "outcome": "no_match",
                    "truncated": False,
                    "candidates": (),
                    "candidate_state": "deleted_or_merged",
                },),
            },
            max_subjects=1,
        )
        old_resolution = resolve_provider_link(
            selected_provider=selected,
            bangumi_subjects=(subject,),
            subject_observations=initial,
        )
        new_resolution = resolve_provider_link(
            selected_provider=selected,
            bangumi_subjects=(subject,),
            subject_observations=replacement,
        )
        self.assertEqual("330299", old_resolution.tmdb_tv_id)
        self.assertIsNone(new_resolution.tmdb_tv_id)
        self.assertEqual({"425211": 1}, dict(new_resolution.season_assignments))
        self.assertNotEqual(
            initial["evidence_fingerprint"], replacement["evidence_fingerprint"]
        )
        self.assertEqual(replacement, new_resolution.subject_observations)

    def test_tv_builder_accepts_bangumi_primary_with_zero_tmdb_tv_links(self):
        case = _incident_case()
        module, _batch, typed = _typed_input(self, case)
        authority = module.TVPlanningAuthorityBuilder().build(typed)
        value = _authority_dict(authority)
        primary = value["work_identity"]["primary_provider"]
        self.assertEqual(
            ("bangumi", "subject", "425211"),
            (
                primary["namespace"],
                primary["entity_type"],
                primary["provider_id"],
            ),
        )
        self.assertEqual(
            [("tmdb", "movie", "997317")],
            [
                (
                    item["namespace"],
                    item["entity_type"],
                    item["provider_id"],
                )
                for item in value["metadata_provider_identities"]
            ],
        )
        self.assertEqual(1, value["selected_season_number"])

    def test_episode_ownership_uses_generic_work_key_not_tmdb_completeness(self):
        identity_fingerprint = hashlib.sha256(
            b"bangumi:subject:425211"
        ).hexdigest()
        row = TaskEpisodeOwnership(
            source_path=Path("sanitized/First Kiss [01].mkv"),
            bangumi_subject_id="425211",
            work_identity_key="bangumi:subject:425211",
            work_identity_fingerprint=identity_fingerprint,
            season_number=1,
            source_episode=1,
            target_episode=1,
            range_authority_id="subject:425211:season:1:1-4",
            evidence_fingerprint="b" * 64,
            decision_source="explicit_bangumi_standalone",
        )
        self.assertEqual(2, row.schema_version)
        self.assertIsNone(row.to_dict().get("tmdb_tv_id"))
        self.assertEqual(
            "bangumi:subject:425211",
            row.to_dict()["work_identity_key"],
        )

    def test_process_resolver_returns_standalone_bangumi_without_tmdb_detail(self):
        selected = dict(_fixture()["incident"]["selected_subject"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = root / "decisions"
            DecisionStateStore(decisions).create("first-kiss-task")
            source_fingerprint = source_identity_set_fingerprint(())
            batch_fingerprint = "c" * 64
            scope = TaskSourceScope(
                schema_version=1,
                task_id="first-kiss-task",
                media_domain="tv",
                batch_scope_fingerprint=batch_fingerprint,
                reservation_lineage_id=canonical_reservation_lineage_id(
                    owner_task_id="first-kiss-task",
                    origin_batch_scope_fingerprint=batch_fingerprint,
                    source_identity_set_fingerprint=source_fingerprint,
                ),
                reservation_revision=0,
                source_identity_set_fingerprint=source_fingerprint,
                source_identities=(),
            )
            from src.rename.process import Rename

            worker = Rename.__new__(Rename)
            worker.decision_state_root = decisions
            worker.provider_link_catalog_root = root / "links"
            worker.active_task_source_scope = scope
            worker.active_consumer_task_id = scope.task_id
            worker.active_provider_selection = selected
            worker.active_provider_link_failure = None
            worker.active_provider_naming_selection = None
            worker.bangumi_search = SimpleNamespace(
                load_crosswalk_evidence=lambda _provider_id: ((selected,), ())
            )
            by_id = Mock(
                side_effect=AssertionError(
                    "zero-link Bangumi planning read executable TMDB details"
                )
            )
            worker.search = SimpleNamespace(
                get_tv_info=lambda _query, _year=None: ("", None),
                get_tv_info_by_id=by_id,
            )
            worker._observe_tmdb_tv_subject = (
                Rename._observe_tmdb_tv_subject.__get__(worker, Rename)
            )
            name, info, resolution = worker._resolve_bangumi_provider_selection(
                selected
            )
        self.assertEqual(selected["title"], name)
        self.assertIsInstance(resolution, ProviderLinkResolution)
        self.assertIsNone(resolution.tmdb_tv_id)
        self.assertEqual({"425211": 1}, dict(resolution.season_assignments))
        self.assertEqual("bangumi", info["_bar_primary_provider"]["namespace"])
        by_id.assert_not_called()

    def test_explicit_bangumi_relation_confirmation_needs_no_tmdb_anchor(self):
        subjects = (
            {
                "provider_id": "first",
                "title": "作品 第一季",
                "original_title": "Work First",
                "air_date": "2020-01-01",
                "name_cn": "作品 第一季",
                "name": "Work First",
                "date": "2020-01-01",
                "eps": 12,
                "platform": "TV",
            },
            {
                "provider_id": "second",
                "title": "作品 第二季",
                "original_title": "Work Second",
                "air_date": "2021-01-01",
                "name_cn": "作品 第二季",
                "name": "Work Second",
                "date": "2021-01-01",
                "eps": 12,
                "platform": "TV",
            },
        )
        relations = ({
            "from": "first",
            "to": "second",
            "relation": "sequel",
        },)
        batch = collect_subject_observations(
            selected_subject_id="second",
            bangumi_subjects=subjects,
            bangumi_relations=relations,
            search_subject=lambda subject: {
                "bangumi_subject_id": subject["provider_id"],
                "observed_at": "2026-09-04T00:00:00+00:00",
                "policy_version": "subject-candidate-v1",
                "queries": ({
                    "locale": "original",
                    "query": subject["original_title"],
                    "year": int(str(subject["air_date"])[:4]),
                    "outcome": "no_match",
                    "truncated": False,
                    "candidates": (),
                },),
            },
            max_subjects=2,
        )
        evidence_fingerprint = str(batch["evidence_fingerprint"])
        edge = {
            "from_subject_id": "first",
            "to_subject_id": "second",
            "relation": "sequel",
            "evidence_fingerprint": evidence_fingerprint,
        }
        failure = {
            "schema_version": 1,
            "selected_subject_id": "second",
            "subject_observations": batch,
            "bangumi_subjects": subjects,
            "bangumi_relations": relations,
            "evidence_fingerprint": evidence_fingerprint,
            "provider_subject_boundary_edges": (edge,),
        }
        together, decision = resolve_provider_subject_boundary_decision(
            failure,
            same_work=True,
            boundary_edge=edge,
        )
        self.assertIsNone(together.tmdb_tv_id)
        self.assertEqual(
            {"first": 1, "second": 2},
            dict(together.season_assignments),
        )
        self.assertEqual("same_work", decision["decision"])
        separate, separate_decision = resolve_provider_subject_boundary_decision(
            failure,
            same_work=False,
            boundary_edge=edge,
        )
        self.assertIsNone(separate.tmdb_tv_id)
        self.assertEqual({"second": 1}, dict(separate.season_assignments))
        self.assertIsNone(separate_decision)

    def test_no_tmdb_cohort_authority_survives_restart_and_projects_successor(self):
        subjects = (
            {
                "provider_id": "first",
                "title": "作品 第一季",
                "original_title": "Work First",
                "air_date": "2020-01-01",
                "episode_count": 12,
                "platform": "TV",
            },
            {
                "provider_id": "second",
                "title": "作品 第二季",
                "original_title": "Work Second",
                "air_date": "2021-01-01",
                "episode_count": 12,
                "platform": "TV",
            },
        )
        relations = ({
            "from": "first",
            "to": "second",
            "relation": "sequel",
        },)
        batch = collect_subject_observations(
            selected_subject_id="first",
            bangumi_subjects=subjects,
            bangumi_relations=relations,
            search_subject=lambda subject: {
                "bangumi_subject_id": subject["provider_id"],
                "observed_at": "2026-09-04T00:00:00+00:00",
                "policy_version": "subject-candidate-v1",
                "queries": ({
                    "locale": "original",
                    "query": subject["original_title"],
                    "year": int(str(subject["air_date"])[:4]),
                    "outcome": "no_match",
                    "truncated": False,
                    "candidates": (),
                },),
            },
            max_subjects=2,
        )
        edge = {
            "from_subject_id": "first",
            "to_subject_id": "second",
            "relation": "sequel",
            "evidence_fingerprint": batch["evidence_fingerprint"],
        }
        failure = {
            "selected_subject_id": "first",
            "subject_observations": batch,
            "bangumi_subjects": subjects,
            "bangumi_relations": relations,
            "evidence_fingerprint": batch["evidence_fingerprint"],
            "provider_subject_boundary_edges": (edge,),
        }
        resolution, boundary = resolve_provider_subject_boundary_decision(
            failure,
            same_work=True,
            boundary_edge=edge,
        )
        self.assertIsNotNone(boundary)
        work = {
            "status": "confirmed",
            "domain": "animation",
            "title": "作品 第一季",
            "year": 2020,
            "primary_provider": {
                "status": "confirmed",
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": "first",
                "title": "作品 第一季",
                "year": 2020,
                "decision_source": "explicit_user",
                "evidence": [],
                "original_title": "Work First",
                "poster_url": None,
                "source_url": None,
                "snapshot_at": None,
            },
            "domain_source": "provider_selection",
        }
        rows = {}
        for item in boundary["cohort_edges"]:
            row = {**boundary, **item}
            rows[f"{item['from_subject_id']}->{item['to_subject_id']}"] = row
        state = DecisionState(
            task_id="no-tmdb-cohort",
            revision=1,
            confirmed_work=work,
            provider_evidence=(work["primary_provider"],),
            provider_subject_boundary_decisions=rows,
        )
        draft = CohortAuthorityDraft.from_confirmation(
            boundary_decision=boundary,
            work_identity=work,
            resolution=resolution,
            provider_snapshot={},
            title_selection={
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": "first",
                "title": "作品 第一季",
                "original_title": "Work First",
            },
        )
        authority = draft.bind(state, authority_revision=1)
        restarted = CohortAuthority.from_dict(authority.to_dict())
        name, info, projected, title_owner = restarted.project("second")
        self.assertEqual("作品 第一季", name)
        self.assertIsNone(projected.tmdb_tv_id)
        self.assertEqual("bangumi", info["_bar_primary_provider"]["namespace"])
        self.assertEqual((), info["_bar_linked_provider_identities"])
        self.assertEqual("first", title_owner["provider_id"])

    def test_current_schema_versions_make_old_tmdb_mandatory_objects_legacy(self):
        self.assertEqual(5, DECISION_STATE_SCHEMA_VERSION)
        self.assertEqual(7, PLAN_SCHEMA_VERSION)
        self.assertEqual(2, TV_PLANNING_AUTHORITY_SCHEMA_VERSION)
        self.assertEqual(
            "tv-planning-authority-v2",
            TV_PLANNING_AUTHORITY_POLICY_VERSION,
        )

        legacy_state = DecisionState(task_id="legacy-v4").to_dict()
        legacy_state["schema_version"] = 4
        migrated_state = DecisionState.from_dict(legacy_state)
        self.assertEqual(4, migrated_state.migrated_from_schema)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decision_store = DecisionStateStore(root)
            path = decision_store.path_for("legacy-v4")
            path.write_text(json.dumps(legacy_state), encoding="utf-8")
            with self.assertRaisesRegex(DecisionStateError, "只读"):
                decision_store.load("legacy-v4")
            readonly_state = decision_store.load_readonly("legacy-v4")
            with self.assertRaisesRegex(DecisionStateError, "只读"):
                decision_store.save(readonly_state, expected_revision=0)

        legacy_plan = RenamePlan(
            task_id="legacy-v6",
            source_root=Path("sanitized"),
            entries=(),
        ).to_dict()
        legacy_plan["schema_version"] = 6
        migrated_plan = RenamePlan.from_dict(legacy_plan)
        self.assertEqual(6, migrated_plan.migrated_from_schema)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_store = PlanStore(root / "plans")
            with self.assertRaisesRegex(PlanStoreError, "Legacy Plan"):
                plan_store.save(migrated_plan)

            # Persist only a raw read-only display object so approval/execution
            # consumers must prove that they reject it before any side effect.
            plan_store.root.mkdir(parents=True, exist_ok=True)
            plan_store.path_for(migrated_plan.task_id).write_text(
                json.dumps(legacy_plan), encoding="utf-8"
            )
            fingerprint = plan_store.fingerprint(migrated_plan)
            review = PlanReviewService(
                plan_store,
                root / "reviews",
                catalog_root=root / "work_catalog",
                decision_state_root=root / "decisions",
                task_root=root / "tasks",
            )
            with self.assertRaisesRegex(PlanReviewError, "只读"):
                review.approve_entries(
                    migrated_plan.task_id,
                    fingerprint,
                    (),
                )
            execution = PlanExecutionService(
                plan_store,
                review,
                task_root=root / "tasks",
                record_root=root / "records",
                catalog_root=root / "work_catalog",
                series_catalog_root=root / "series_catalog",
                decision_state_root=root / "decisions",
                recovery_transaction_root=root / "recovery",
                provider_link_catalog_root=root / "provider_links",
            )
            with self.assertRaisesRegex(PlanExecutionError, "只读"):
                execution.validate_approved(migrated_plan.task_id, fingerprint)

    async def test_legacy_plan_is_blocked_by_ui_execute_and_recovery_entrypoints(self):
        legacy_payload = RenamePlan(
            task_id="legacy-entrypoints-v6",
            source_root=Path("sanitized"),
            entries=(),
            metadata={"catalog_work_key": "legacy-work"},
        ).to_dict()
        legacy_payload["schema_version"] = 6
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_store = PlanStore(root / "plans")
            plan_store.root.mkdir(parents=True)
            plan_store.path_for("legacy-entrypoints-v6").write_text(
                json.dumps(legacy_payload), encoding="utf-8"
            )
            migrated = plan_store.load("legacy-entrypoints-v6")
            self.assertEqual(6, migrated.migrated_from_schema)
            fingerprint = plan_store.fingerprint(migrated)
            task_root = root / "tasks"
            task_root.mkdir()
            task_path = task_root / "legacy-entrypoints-v6.json"
            task = {
                "uuid": "legacy-entrypoints-v6",
                "workflow_status": "approved",
                "preview_only": False,
                "execution_requires_manual_check": False,
            }
            task_path.write_text(json.dumps(task), encoding="utf-8")
            frozen_task = task_path.read_bytes()
            review = PlanReviewService(
                plan_store,
                root / "reviews",
                catalog_root=root / "work_catalog",
                decision_state_root=root / "decisions",
                task_root=task_root,
            )
            transfer = Mock(
                side_effect=AssertionError("legacy Plan crossed the media boundary")
            )
            execution = PlanExecutionService(
                plan_store,
                review,
                task_root=task_root,
                record_root=root / "records",
                transfer_factory=transfer,
                catalog_root=root / "work_catalog",
                series_catalog_root=root / "series_catalog",
                decision_state_root=root / "decisions",
                recovery_transaction_root=root / "recovery",
                provider_link_catalog_root=root / "provider_links",
            )

            dialog = SimpleNamespace(
                review_service=review,
                plan=migrated,
                task_data=dict(task),
                decision=None,
                review_badge=SimpleNamespace(text=""),
                _refresh_plan_header=MagicMock(),
                _render_entries=MagicMock(),
                _refresh_execute_button=MagicMock(),
            )
            with (
                patch(
                    "src.pages.plan_review_page.get_task",
                    return_value=dict(task),
                ),
                patch("src.pages.plan_review_page.notify") as notify,
            ):
                await approve_dialog(dialog, ())
            self.assertTrue(
                any("只读" in str(call.args[0]) for call in notify.call_args_list)
            )
            self.assertFalse((root / "reviews").exists())

            manager = TaskQueueManager(
                task_root=task_root,
                execution_service=execution,
                decision_state_root=root / "decisions",
                recovery_abandon_audit_root=root / "abandon",
                plan_root=plan_store.root,

            )

            async def queue_attempt() -> None:
                await manager.start()
                try:
                    with self.assertRaisesRegex(PlanExecutionError, "只读"):
                        await manager.enqueue_approved(
                            "legacy-entrypoints-v6", fingerprint
                        )
                finally:
                    await manager.stop()

            await queue_attempt()
            self.assertEqual(0, manager._queue.qsize())
            with self.assertRaisesRegex(PlanExecutionError, "只读"):
                execution.execute("legacy-entrypoints-v6", fingerprint)
            transfer.assert_not_called()

            work_catalog = MagicMock()
            with self.assertRaisesRegex(ExecutionRecoveryError, "只读"):
                reconcile_catalog_update(
                    "legacy-entrypoints-v6",
                    task_root=task_root,
                    record_root=root / "records",
                    plan_store=plan_store,
                    work_catalog=work_catalog,
                )
            work_catalog.lock.assert_not_called()
            self.assertEqual(frozen_task, task_path.read_bytes())


class BangumiPrimaryPublicPathIncidentRedContracts(
    unittest.IsolatedAsyncioTestCase
):
    async def test_real_add_select_multi_exact_tmdb_reaches_bangumi_plan_without_identity_gate(self):
        selected = dict(_fixture()["incident"]["selected_subject"])
        subject_id = str(selected["provider_id"])
        subjects = {
            subject_id: {
                **selected,
                "id": int(subject_id),
                "type": 2,
                "name_cn": selected["title"],
                "name": selected["original_title"],
                "date": selected["air_date"],
                "eps": 4,
                "images": {"large": _wiring.production_fixture.POSTER_URL},
            }
        }

        def observation_info(_search, query, _year=None):
            localized = str(query) == str(selected["title"])
            provider_id = 330299 if localized else 332819
            air_date = str(selected["air_date"])
            info = {
                "id": provider_id,
                "name": str(query),
                "original_name": str(query),
                "first_air_date": air_date,
                "genres": ({"id": 16, "name": "Animation"},),
                "seasons": ({
                    "season_number": 1,
                    "air_date": air_date,
                    "episode_count": 4,
                },),
                "_bar_search_alternatives": ({
                    "id": provider_id,
                    "name": str(query),
                    "original_name": str(query),
                    "first_air_date": air_date,
                },),
            }
            return str(query), info

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _wiring._make_episode_source(
                root / "source", "Ordinary Show", (1, 2, 3, 4)
            )
            detail_read = MagicMock(
                side_effect=AssertionError(
                    "ambiguous optional TMDB identity became an executable detail read"
                )
            )
            season_read = MagicMock(
                side_effect=AssertionError(
                    "ambiguous optional TMDB identity became a special-episode authority"
                )
            )
            with patch.multiple(
                _wiring._ProviderSearch,
                get_tv_subject_observation_info=observation_info,
                get_tv_info_by_id=detail_read,
                get_season_info=season_read,
                create=True,
            ):
                result = await _wiring.ProviderSubjectPlanWiringRedContracts._run_real_add_select(
                    self,
                    root=root / "flow",
                    source=source,
                    subjects=subjects,
                    relations={subject_id: ()},
                    selected_subject_id=subject_id,
                )
            detail_read.assert_not_called()
            season_read.assert_not_called()

        problems = _wiring.ProviderSubjectPlanWiringRedContracts._real_path_problems(
            self, result
        )
        primary = (
            result.plan.work_identity.primary_provider
            if result.plan.work_identity is not None else None
        )
        if primary is None or (
            primary.namespace,
            primary.entity_type,
            primary.provider_id,
        ) != ("bangumi", "subject", subject_id):
            failure = result.task.get("provider_selection_failure")
            reasons = (
                failure.get("reason_codes")
                if isinstance(failure, dict) else None
            )
            problems.append(
                "multi-exact public path did not persist Bangumi primary; "
                f"workflow={result.task.get('workflow_status')!r}; "
                f"error={result.task.get('error')!r}; "
                f"reason_codes={reasons!r}"
            )
        if result.state.season_binding is None or (
            result.state.season_binding.get("season_number") != 1
        ):
            problems.append("multi-exact public path did not persist Season1")
        operation = result.task.get("review_operation")
        if isinstance(operation, dict) and operation.get("commands"):
            problems.append("multi-exact public path persisted an identity command")
        from src.rename.plan_verifier import PlanVerifier
        from src.rename.rename_plan import PlanStatus

        verification = PlanVerifier().verify(result.plan)
        if verification.status != PlanStatus.READY or {i.code for i in verification.issues} != set():
            problems.append("multi-exact public path must await only the explicit font check")
        self.assertEqual([], problems, "\n".join(problems))

    async def test_real_add_select_reaches_current_bangumi_primary_plan(self):
        selected = dict(_fixture()["incident"]["selected_subject"])
        subjects = {str(selected["provider_id"]): selected}
        subjects[str(selected["provider_id"])] = {
            **selected,
            "id": int(str(selected["provider_id"])),
            "type": 2,
            "name_cn": selected["title"],
            "name": selected["original_title"],
            "date": selected["air_date"],
            "eps": 4,
            "images": {"large": _wiring.production_fixture.POSTER_URL},
        }
        relations = {str(selected["provider_id"]): ()}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _wiring._make_episode_source(
                root / "source",
                "Ordinary Show",
                (1, 2, 3, 4),
            )
            with patch(
                "tests.test_provider_subject_plan_wiring_red_contract."
                "_ProviderSearch.get_tv_info_by_id",
                side_effect=AssertionError(
                    "zero-link Bangumi selection read TMDB TV details"
                ),
            ) as tv_detail_read, patch(
                "tests.test_provider_subject_plan_wiring_red_contract."
                "_ProviderSearch.get_season_info",
                side_effect=AssertionError(
                    "zero-link Bangumi selection read TMDB season details"
                ),
            ) as season_detail_read:
                result = await _wiring.ProviderSubjectPlanWiringRedContracts._run_real_add_select(
                    self,
                    root=root / "flow",
                    source=source,
                    subjects=subjects,
                    relations=relations,
                    selected_subject_id=str(selected["provider_id"]),
                )
            tv_detail_read.assert_not_called()
            season_detail_read.assert_not_called()
        problems = _wiring.ProviderSubjectPlanWiringRedContracts._real_path_problems(
            self, result
        )
        primary = (
            result.plan.work_identity.primary_provider
            if result.plan.work_identity is not None else None
        )
        if primary is None or (
            primary.namespace,
            primary.entity_type,
            primary.provider_id,
        ) != ("bangumi", "subject", "425211"):
            problems.append("real AddTask path did not persist Bangumi primary")
        if result.state.layout_mode != "tv_episodes":
            problems.append("real AddTask path changed episodic presentation")
        if result.state.season_binding is None or (
            result.state.season_binding.get("season_number") != 1
        ):
            problems.append("standalone Bangumi work did not freeze Season1")
        if result.state.tv_planning_authority is None:
            problems.append("real AddTask path did not persist TV authority")
        if result.plan.metadata.get("provider_link_snapshot") is not None:
            problems.append("zero-link work fabricated a provider-link snapshot")
        from src.rename.plan_verifier import PlanVerifier
        from src.rename.rename_plan import PlanStatus

        verification = PlanVerifier().verify(result.plan)
        if verification.status != PlanStatus.READY or {i.code for i in verification.issues} != set():
            problems.append("current Bangumi-primary Plan is not approvable")
        self.assertEqual([], problems, "\n".join(problems))

    async def test_confirmed_tv_reidentify_rejects_without_replacing_reviewed_work(self):
        selected = dict(_fixture()["incident"]["selected_subject"])
        subjects = {
            "425211": {
                **selected,
                "id": 425211,
                "type": 2,
                "name_cn": selected["title"],
                "name": selected["original_title"],
                "date": selected["air_date"],
                "eps": 4,
                "images": {"large": _wiring.production_fixture.POSTER_URL},
            }
        }
        relations = {"425211": ()}
        tmdb_info = _wiring._tmdb_tv(
            330299,
            selected["title"],
            selected["original_title"],
            selected["air_date"],
            ((1, selected["air_date"], 4),),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _wiring._make_episode_source(
                root / "source", "Sanitized Seasonal Work", (1, 2, 3, 4)
            )
            with (
                patch(
                    "tests.test_provider_subject_plan_wiring_red_contract."
                    "_ProviderSearch._lookup",
                    return_value=(selected["title"], dict(tmdb_info)),
                ),
                patch(
                    "tests.test_provider_subject_plan_wiring_red_contract."
                    "_ProviderSearch.get_tv_info_by_id",
                    return_value=(selected["title"], dict(tmdb_info)),
                ),
            ):
                first = await _wiring.ProviderSubjectPlanWiringRedContracts._run_real_add_select(
                    self,
                    root=root / "flow",
                    source=source,
                    subjects=subjects,
                    relations=relations,
                    selected_subject_id="425211",
                )
            self.assertIsNotNone(
                first.plan.work_identity,
                "control failed: the pre-drift exact TMDB Plan was not created; "
                f"task_error={first.task.get('error')!r}; "
                f"provider_failure={first.task.get('provider_selection_failure')!r}; "
                f"process_calls={first.process_call_count}",
            )
            pre_drift_identities = {
                (
                    first.plan.work_identity.primary_provider.namespace,
                    first.plan.work_identity.primary_provider.entity_type,
                    first.plan.work_identity.primary_provider.provider_id,
                ),
                *{
                    (item.namespace, item.entity_type, item.provider_id)
                    for item in first.plan.linked_provider_identities
                },
            }
            self.assertIn(
                ("tmdb", "tv", "330299"),
                pre_drift_identities,
                "control failed: the pre-drift Plan did not freeze the exact TMDB TV observation",
            )
            old_fingerprint = PlanStore(first.plans).fingerprint(first.plan)
            old_revision = first.state.revision
            candidate = _wiring._candidate(subjects, "425211")
            renamer = _wiring._configured_renamer(
                first.root,
                fail_subject=(selected["title"], selected["original_title"]),
                failure_mode="no_match",
            )
            with ExitStack() as stack:
                stack.enter_context(
                    patch("src.task_queue.Rename", return_value=renamer)
                )
                stack.enter_context(
                    patch("src.rename.process.TASK_PATH", first.tasks)
                )
                stack.enter_context(
                    patch("src.rename.process.PLAN_PATH", first.plans)
                )
                stack.enter_context(
                    patch(
                        "src.rename.process.SERIES_CATALOG_PATH",
                        first.root / "series_catalog",
                    )
                )
                stack.enter_context(
                    patch("src.rename.process.RECORD_PATH", first.root / "records")
                )
                stack.enter_context(
                    _wiring._root_config_patch({
                        "bangumi_path": str(renamer.BANGUMI_PATH),
                        "movie_path": str(renamer.MOVIE_PATH),
                        "anime_path": str(renamer.ANIME_PATH),
                        "anime_movie_path": str(renamer.ANIME_MOVIE_PATH),
                    })
                )
                detail_read = stack.enter_context(
                    patch(
                        "tests.test_provider_subject_plan_wiring_red_contract."
                        "_ProviderSearch.get_tv_info_by_id",
                        side_effect=AssertionError(
                            "complete no-match reidentify read TMDB details"
                        ),
                    )
                )
                frozen_task = (first.tasks / f'{first.task_id}.json').read_bytes()
                with self.assertRaisesRegex(PlanExecutionError, '已确认.*不支持重新识别'):
                    await first.manager.enqueue_reidentify(
                        first.task_id, candidate, old_revision
                    )
                await first.manager.stop()
            detail_read.assert_not_called()
            current_plan = PlanStore(first.plans).load(first.task_id)
            current_state = DecisionStateStore(first.decisions).load(first.task_id)
            current_task = json.loads(
                (first.tasks / f"{first.task_id}.json").read_text("utf-8")
            )
            current_fingerprint = PlanStore(first.plans).fingerprint(current_plan)
            primary = current_plan.work_identity.primary_provider
            self.assertEqual(
                ("bangumi", "subject", "425211"),
                (primary.namespace, primary.entity_type, primary.provider_id),
            )
            self.assertEqual(old_revision, current_state.revision)
            self.assertEqual(old_fingerprint, current_fingerprint)
            self.assertEqual(frozen_task, (first.tasks / f'{first.task_id}.json').read_bytes())
            self.assertNotEqual("approved", current_task.get("workflow_status"))
            self.assertEqual(first.plan.metadata.get('provider_link_snapshot'),
                             current_plan.metadata.get('provider_link_snapshot'))


if __name__ == "__main__":
    unittest.main()
