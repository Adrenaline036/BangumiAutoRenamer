"""R24 R8 S1 -> S2 incident-shaped GREEN regression.

This module freezes the user-observed semantic
shape without the private harness, raw DOM, credentials, or local media paths.
The production resolver seam is exercised against a real temporary
``DecisionStateStore`` across a simulated restart boundary.  This component
evidence deliberately does not claim NiceGUI/AddTask/task-queue reachability or
target-root correctness; Phase-B GREEN owns those separate wiring obligations.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from src.rename.decision_state import DecisionStateStore
from src.rename.process import Rename
from src.rename.provider_link_decision import (
    freeze_provider_subject_boundary_edges,
    resolve_provider_subject_boundary_decision,
    stage_provider_link_decision,
)
from src.rename.provider_link_resolver import collect_subject_observations
from src.rename.provider_search import ProviderSearchError
from src.rename.rename_plan import ProviderIdentity, WorkIdentity
from src.rename.successor_authority import (
    CohortAuthorityDraft,
    CohortAuthorityStore,
    successor_authority_root,
)


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "r24_successor_authority_r8_s1_s2_incident.json"
)
def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _cohort_case() -> dict[str, object]:
    return dict(_fixture()["cohort"])


def _observation_batch(case: dict[str, object]) -> dict[str, object]:
    observations = {
        str(row["bangumi_subject_id"]): deepcopy(row)
        for row in case["observations"]
    }
    return dict(collect_subject_observations(
        selected_subject_id=str(case["selected_subject_id_s1"]),
        bangumi_subjects=deepcopy(case["subjects"]),
        bangumi_relations=deepcopy(case["relations"]),
        search_subject=lambda subject: deepcopy(
            observations[str(subject["provider_id"])]
        ),
        max_subjects=len(case["subjects"]),
    ))


def _confirmed_s1_authority(
    root: Path,
    *,
    task_id: str = "incident-s1",
    canonical_root: Path | None = None,
):
    case = _cohort_case()
    batch = _observation_batch(case)
    edges = freeze_provider_subject_boundary_edges(
        selected_subject_id=str(case["selected_subject_id_s1"]),
        subject_observations=batch,
        bangumi_subjects=deepcopy(case["subjects"]),
        bangumi_relations=deepcopy(case["relations"]),
    )
    if not edges:
        raise AssertionError("incident fixture did not freeze an actionable edge")
    failure = {
        "selected_subject_id": str(case["selected_subject_id_s1"]),
        "subject_observations": batch,
        "bangumi_subjects": deepcopy(case["subjects"]),
        "bangumi_relations": deepcopy(case["relations"]),
        "evidence_fingerprint": str(batch["evidence_fingerprint"]),
        "provider_subject_boundary_edges": tuple(deepcopy(edges)),
    }
    resolution, boundary = resolve_provider_subject_boundary_decision(
        failure,
        same_work=True,
        boundary_edge=edges[0],
    )
    if boundary is None:
        raise AssertionError("incident fixture did not produce cohort authority")
    if len(tuple(boundary.get("cohort_edges", ()))) != 3:
        raise AssertionError("incident authority did not freeze three cohort edges")

    store = DecisionStateStore(root)
    state = store.create(task_id)
    state = stage_provider_link_decision(state, resolution)
    tmdb = ProviderIdentity(
        status="confirmed",
        namespace="tmdb",
        entity_type="tv",
        provider_id=str(case["tmdb_tv_id"]),
        title="向山进发",
        year=2013,
        decision_source="incident_s1_explicit_confirmation",
        evidence=("sanitized R8 S1 cohort confirmation",),
        snapshot_at="2026-08-31T08:43:00+00:00",
    )
    bangumi = ProviderIdentity(
        status="confirmed",
        namespace="bangumi",
        entity_type="subject",
        provider_id=str(case["selected_subject_id_s1"]),
        title="向山进发",
        year=2013,
        decision_source="incident_s1_explicit_confirmation",
        evidence=("sanitized R8 S1 selected subject",),
        snapshot_at="2026-08-31T08:43:00+00:00",
    )
    work = WorkIdentity(
        status="confirmed",
        domain="animation",
        title="向山进发",
        year=2013,
        primary_provider=tmdb,
        domain_source="incident_s1_explicit_confirmation",
    )
    shared = {
        key: value
        for key, value in boundary.items()
        if key not in {"from_subject_id", "to_subject_id", "relation"}
    }
    shared.update({
        "expected_decision_revision": 0,
        "request_fingerprint": _canonical_sha256({
            "cohort_subject_ids": boundary["cohort_subject_ids"],
            "season_assignments": boundary["season_assignments"],
            "tmdb_tv_id": boundary["selected_tmdb_tv_id"],
        }),
    })
    boundary_decisions = {}
    for edge in boundary["cohort_edges"]:
        row = {
            **shared,
            "from_subject_id": str(edge["from_subject_id"]),
            "to_subject_id": str(edge["to_subject_id"]),
            "relation": str(edge["relation"]),
        }
        boundary_decisions[
            f"{row['from_subject_id']}->{row['to_subject_id']}"
        ] = row
    candidate = replace(
        state,
        confirmed_work=work.to_dict(),
        provider_evidence=(tmdb.to_dict(), bangumi.to_dict()),
        linked_provider_identities=(bangumi.to_dict(),),
        series_binding={
            "series_key": str(resolution.proposed_series_key),
            "canonical_root": str(
                canonical_root
                or root.parent / "non-authoritative-routing-sentinel" / "yama"
            ),
            "catalog_revision": 0,
            "decision_source": "provider_crosswalk",
        },
        season_binding={
            "season_binding_id": "season-binding-incident-s1",
            "season_number": 1,
            "decision_source": "provider_crosswalk",
        },
        library_domain="animation",
        layout_mode="tv_episodes",
        provider_subject_boundary_decisions=boundary_decisions,
    )
    written = store.save(candidate, expected_revision=0)
    draft = CohortAuthorityDraft.from_confirmation(
        boundary_decision=boundary,
        work_identity=work.to_dict(),
        resolution=resolution,
        provider_snapshot=case["observations"][0]["queries"][0]["candidates"][0],
        title_selection={
            "namespace": "bangumi",
            "entity_type": "subject",
            "provider_id": str(case["subjects"][0]["provider_id"]),
            "title": str(case["subjects"][0]["title"]),
            "original_title": str(case["subjects"][0]["original_title"]),
        },
    )
    authority_store = CohortAuthorityStore(successor_authority_root(root))
    authority, previous = authority_store.preview(draft, written)
    if previous is not None:
        raise AssertionError("incident fixture unexpectedly replaced authority")
    authority_store.publish_prepared(
        authority,
        expected_previous_fingerprint="",
    )
    return written


def _tmdb_info(candidate: dict[str, object]) -> dict[str, object]:
    return {
        "id": int(candidate["tmdb_tv_id"]),
        "name": str(candidate["title"]),
        "original_name": str(candidate["original_title"]),
        "first_air_date": str(candidate["first_air_date"]),
        "genres": [{"name": "Animation"}],
        "seasons": [
            {
                "season_number": int(row["season_number"]),
                "name": f"Season {row['season_number']}",
                "air_date": str(row["air_date"]),
                "episode_count": int(row["episode_count"]),
            }
            for row in candidate["seasons"]
        ],
    }


class _IncidentTmdb:
    def __init__(self, case: dict[str, object]):
        candidate = case["observations"][0]["queries"][0]["candidates"][0]
        self.info = _tmdb_info(candidate)
        self.calls: list[tuple[str, str]] = []

    def get_tv_info(self, query, _year=None):
        self.calls.append(("search", str(query)))
        value = str(query or "").casefold()
        if value in {"ヤマノススメ".casefold(), "向山进发".casefold()}:
            result = dict(self.info)
            result["_bar_search_alternatives"] = [{
                "id": result["id"],
                "name": str(query),
                "original_name": str(query),
                "first_air_date": result["first_air_date"],
            }]
            return "向山进发", result
        return "", None

    def get_tv_info_by_id(self, provider_id):
        self.calls.append(("by_id", str(provider_id)))
        if str(provider_id) == "53918":
            return "向山进发", dict(self.info)
        return "", None


class _IncidentBangumi:
    def __init__(self, case: dict[str, object]):
        self.case = case
        self.calls = 0

    def load_crosswalk_evidence(self, _provider_id):
        self.calls += 1
        return (
            tuple(deepcopy(self.case["subjects"])),
            tuple(deepcopy(self.case["relations"])),
        )


class _ResolverSeamWorker(Rename):
    """Explicit seam-only harness; it is not a production-entry substitute."""

    def __init__(
        self,
        case: dict[str, object],
        *,
        task_id: str,
        decision_state_root: Path,
        provider_link_catalog_root: Path,
    ):
        self.decision_state_root = Path(decision_state_root)
        self.provider_link_catalog_root = Path(provider_link_catalog_root)
        self.search = _IncidentTmdb(case)
        self.bangumi_search = _IncidentBangumi(case)
        self.active_provider_selection = {
            "namespace": "bangumi",
            "entity_type": "subject",
            "provider_id": str(case["selected_subject_id_s2"]),
            "title": "向山进发 第二季",
            "original_title": "ヤマノススメ セカンドシーズン",
            "snapshot_at": "2026-08-31T08:43:00+00:00",
        }
        self.active_task_source_scope = SimpleNamespace(task_id=task_id)
        self.active_provider_link_failure = None
        self.active_provider_naming_selection = None


def _resolver_seam_worker(
    case: dict[str, object],
    *,
    task_id: str,
    decision_state_root: Path,
    provider_link_catalog_root: Path,
):
    return _ResolverSeamWorker(
        case,
        task_id=task_id,
        decision_state_root=decision_state_root,
        provider_link_catalog_root=provider_link_catalog_root,
    )


class R24SuccessorAuthorityIncidentRedContracts(unittest.TestCase):
    def test_s2_consumes_persisted_s1_cohort_authority_without_reconfirmation(self):
        case = _cohort_case()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = root / "decisions"
            provider_links = root / "provider-links"
            s1_written = _confirmed_s1_authority(decisions)
            DecisionStateStore(decisions).create("incident-s2")

            # New store instance is the restart boundary from the R8 incident.
            restarted = DecisionStateStore(decisions)
            s1_before = restarted.load("incident-s1")
            self.assertEqual(1, s1_before.revision)
            self.assertEqual(3, len(s1_before.provider_subject_boundary_decisions))
            self.assertEqual(
                {"41658": 1, "86670": 2, "216310": 3, "290980": 4},
                {
                    str(key): int(value)
                    for key, value in next(iter(
                        s1_before.provider_subject_boundary_decisions.values()
                    ))["season_assignments"].items()
                },
            )

            worker = _resolver_seam_worker(
                case,
                task_id="incident-s2",
                decision_state_root=decisions,
                provider_link_catalog_root=provider_links,
            )
            selection = dict(worker.active_provider_selection)
            problems: list[str] = []
            result = None
            try:
                result = worker._resolve_bangumi_provider_selection(selection)
            except ProviderSearchError as exc:
                problems.append(
                    "S2 repeated provider/cohort resolution instead of consuming "
                    f"S1 authority: {exc}"
                )

            if result is not None:
                _name, info, resolution = result
                if str(info.get("id") or "") != "53918":
                    problems.append("S2 projected a different TMDB work")
                if dict(resolution.season_assignments).get("86670") != 2:
                    problems.append("S2 did not project the frozen season-2 assignment")
            if worker.active_provider_link_failure is not None:
                problems.append("S2 created a fresh cohort-confirmation failure")
            if worker.bangumi_search.calls:
                problems.append("S2 re-read Bangumi instead of consuming frozen authority")
            if worker.search.calls:
                problems.append("S2 re-read TMDB instead of consuming frozen authority")

            self.assertEqual(
                DecisionStateStore.fingerprint(s1_written),
                DecisionStateStore.fingerprint(restarted.load("incident-s1")),
                "cross-task consumption mutated the producer authority",
            )
            s2_after = restarted.load("incident-s2")
            self.assertEqual(0, s2_after.revision)
            self.assertEqual({}, dict(s2_after.confirmed_work))
            self.assertEqual([], problems, "\n".join(problems))

    def test_missing_or_unconfirmed_authority_remains_fail_closed(self):
        case = _cohort_case()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = root / "decisions"
            DecisionStateStore(decisions).create("incident-s2")
            worker = _resolver_seam_worker(
                case,
                task_id="incident-s2",
                decision_state_root=decisions,
                provider_link_catalog_root=root / "provider-links",
            )
            with self.assertRaises(ProviderSearchError):
                worker._resolve_bangumi_provider_selection(
                    dict(worker.active_provider_selection)
                )

    def test_fixture_freezes_r8_sequence_and_privacy_boundary(self):
        fixture = _fixture()
        self.assertEqual(1, fixture.get("schema_version"))
        evidence_contract = fixture.get("evidence_contract")
        self.assertIsInstance(evidence_contract, dict)
        self.assertEqual(
            "resolver_seam_only", evidence_contract.get("evidence_scope")
        )
        self.assertIn(
            "task queue production wiring",
            evidence_contract.get("does_not_claim", ()),
        )
        routing_contract = fixture.get("target_routing_contract")
        self.assertIsInstance(routing_contract, dict)
        self.assertEqual(
            "non_authoritative_test_sentinel",
            routing_contract.get("producer_series_canonical_root_role"),
        )
        self.assertEqual(
            {
                "canonical_root",
                "resolved_target_root",
                "target_root",
                "source_path",
            },
            set(routing_contract.get("canonical_authority_forbidden_fields", ())),
        )
        steps = [row["step"] for row in fixture["durable_sequence"]]
        self.assertEqual(
            [
                "s1_confirmed",
                "restart_boundary",
                "s2_before_resolution",
                "current_product_outcome",
            ],
            steps,
        )
        s1 = fixture["durable_sequence"][0]
        self.assertEqual(1, s1["decision_revision"])
        self.assertEqual(3, s1["boundary_count"])
        self.assertEqual(1, s1["cohort_decision_count"])
        self.assertEqual(
            {"41658": 1, "86670": 2, "216310": 3, "290980": 4},
            s1["season_assignments"],
        )
        text = FIXTURE_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "D:\\\\Anime",
            "C:\\\\Users",
            "task UUID=",
            "Authorization:",
            "api_key",
            "<div",
            "outerHTML",
        ):
            self.assertNotIn(forbidden, text)
        self.assertRegex(_canonical_sha256(fixture), r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
