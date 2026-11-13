"""R24 Phase-A RED contracts for business-authority and recovery findings.

Only sanitized synthetic files below ``TemporaryDirectory`` are used.  Phase B
promotes every incident assertion to an ordinary regression contract.
"""

from __future__ import annotations

from contextlib import nullcontext
import asyncio
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.pages import plan_review_page
from src.rename import (
    plan_verifier,
    process,
    projection_authority,
    tv_planning_authority,
)
from src.rename.execution_recovery import (
    ExecutionRecoveryError,
    abandon_catalog_update_recovery,
)
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.plan_review import (
    PlanReviewDecision,
    PlanReviewError,
    PlanReviewService,
    ReviewStatus,
    entry_id,
)
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import (
    ExecutionPolicy,
    PlanEntry,
    RenamePlan,
)
from src.rename.series_catalog import SeriesCatalogError
from src.rename.tv_planning_authority import TVPlanningAuthorityError
from src.task_queue import TaskQueueManager
from tests.presentation_fixtures import with_tv_presentation
from tests.projection_test_support import save_plan_fixture
import tests._media_flow_helpers as movie_fixture


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()




def _episode_ownership_case(
    root: Path,
    *,
    target_season: int,
    target_episode: int,
    source_episode: int | None,
):
    source = root / "source" / "Episode.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"r24-episode-ownership")
    target = (
        root / "library" / f"Season {target_season:02d}"
        / f"Example S{target_season:02d}E{target_episode:02d}.mkv"
    )
    projection = []
    if source_episode is not None:
        projection.append({
            "source_path": str(source),
            "source_episode": source_episode,
            "target_episode": target_episode,
        })
    plan = RenamePlan(
        task_id="episode-authority",
        source_root=source.parent,
        entries=(PlanEntry(
            source_path=source,
            target_path=target,
            season_id=target_season,
            episode_id=target_episode,
            media_kind="video",
            role="main_episode",
        ),),
        metadata={
            "source_episode_projection": {"episode_mapping": projection},
        },
    )
    authority = SimpleNamespace(
        episode_range_ownership=({
            "bangumi_subject_id": "subject-s1",
            "season_number": 1,
            "episode_start": 1,
            "episode_end": 12,
            "sources": [str(source.resolve(strict=False))],
            "evidence_fingerprint": "a" * 64,
        },),
        work_identity={"primary_provider": {"provider_id": "83121"}},
    )
    return plan, authority


def _multi_source_episode_ownership_case(
    root: Path,
    *,
    include_unlisted_source: bool,
):
    source_root = root / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    listed_a = source_root / "Episode A.mkv"
    listed_b = source_root / "Episode B.mkv"
    unlisted = source_root / "Episode Outside Frozen Sources.mkv"
    for source in (listed_a, listed_b, unlisted):
        source.write_bytes(f"r24-{source.stem}".encode("utf-8"))
    selected = (listed_a, unlisted) if include_unlisted_source else (listed_a, listed_b)
    source_episodes = (1, 2) if include_unlisted_source else (1, 1)
    entries = tuple(
        PlanEntry(
            source_path=source,
            target_path=(
                root / "library" / "Season 01"
                / f"Example S01E{target_episode:02d}.mkv"
            ),
            season_id=1,
            episode_id=target_episode,
            media_kind="video",
            role="main_episode",
        )
        for target_episode, source in enumerate(selected, start=1)
    )
    plan = RenamePlan(
        task_id=(
            "episode-authority-unlisted"
            if include_unlisted_source
            else "episode-authority-duplicate"
        ),
        source_root=source_root,
        entries=entries,
        metadata={
            "source_episode_projection": {
                "episode_mapping": [
                    {
                        "source_path": str(source),
                        "source_episode": source_episode,
                        "target_episode": target_episode,
                    }
                    for target_episode, (source, source_episode) in enumerate(
                        zip(selected, source_episodes), start=1
                    )
                ]
            },
        },
    )
    authority = SimpleNamespace(
        episode_range_ownership=({
            "bangumi_subject_id": "subject-s1",
            "season_number": 1,
            "episode_start": 1,
            "episode_end": 12,
            "sources": [
                str(listed_a.resolve(strict=False)),
                str(listed_b.resolve(strict=False)),
            ],
            "evidence_fingerprint": "c" * 64,
        },),
        work_identity={"primary_provider": {"provider_id": "83121"}},
    )
    return plan, authority


def _abandon_case(root: Path, suffix: str):
    task_id = f"abandon-drift-{suffix}"
    source = root / "media" / "source.mkv"
    target = root / "library" / "target.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"r24-source")
    target.write_bytes(b"r24-target")
    store = PlanStore(root / "plans")
    plan = save_plan_fixture(
        store,
        RenamePlan.from_mapping(
            task_id=task_id,
            source_root=source,
            mapping={source: target},
            expected_sources=(source,),
            metadata={"target_root": str(target.parent)},
        ),
    )
    fingerprint = store.fingerprint(plan)
    review = PlanReviewService(store, root / "reviews")
    review.load_or_create(plan)
    task_path = root / "tasks" / f"{task_id}.json"
    ledger_path = root / "records" / f"{task_id}.ledger.json"
    record_path = root / "records" / f"{task_id}.json"
    _write_json(task_path, {
        "uuid": task_id,
        "workflow_status": "failed",
        "execution_status": "catalog_update_pending",
        "execution_requires_manual_check": True,
    })
    _write_json(ledger_path, {
        "schema_version": 1,
        "task_id": task_id,
        "plan_fingerprint": fingerprint,
        "status": "completed",
        "entries": [{
            "source": str(source),
            "target": str(target),
            "status": "created",
            "transfer_type": "hardlink",
        }],
    })
    _write_json(record_path, {"task_id": task_id, "kind": "history"})
    kwargs = {
        "task_root": root / "tasks",
        "record_root": root / "records",
        "plan_store": store,
        "review_root": root / "reviews",
        "decision_state_root": root / "decisions",
        "audit_root": root / "audit",
        "expected_plan_fingerprint": fingerprint,
    }
    return SimpleNamespace(
        task_id=task_id,
        store=store,
        fingerprint=fingerprint,
        task_path=task_path,
        ledger_path=ledger_path,
        record_path=record_path,
        review_path=root / "reviews" / f"{task_id}.json",
        kwargs=kwargs,
    )






class EpisodeAuthorityRedContracts(unittest.TestCase):
    def test_f1_in_range_local_projection_cannot_contradict_frozen_episode_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, authority = _episode_ownership_case(
                Path(directory), target_season=1, target_episode=2, source_episode=1
            )
            with self.assertRaises(TVPlanningAuthorityError):
                plan_review_page._with_task_episode_ownership(plan, authority)

    def test_f1_cross_season_local_projection_cannot_relabel_authority_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, authority = _episode_ownership_case(
                Path(directory), target_season=2, target_episode=1, source_episode=1
            )
            with self.assertRaises(TVPlanningAuthorityError):
                plan_review_page._with_task_episode_ownership(plan, authority)

    def test_f1_missing_source_projection_never_falls_back_to_target_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, authority = _episode_ownership_case(
                Path(directory), target_season=1, target_episode=1, source_episode=None
            )
            with self.assertRaises(TVPlanningAuthorityError):
                plan_review_page._with_task_episode_ownership(plan, authority)

    def test_f1_multi_source_row_requires_membership_and_unique_source_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unlisted_plan, authority = _multi_source_episode_ownership_case(
                root / "unlisted", include_unlisted_source=True
            )
            with self.assertRaises(TVPlanningAuthorityError):
                plan_review_page._with_task_episode_ownership(
                    unlisted_plan, authority
                )

            duplicate_plan, authority = _multi_source_episode_ownership_case(
                root / "duplicate", include_unlisted_source=False
            )
            with self.assertRaises(TVPlanningAuthorityError):
                plan_review_page._with_task_episode_ownership(
                    duplicate_plan, authority
                )






class AbandonReentryRedContracts(unittest.TestCase):
    def test_f6_existing_audit_rehashes_every_live_evidence_before_cleanup(self):
        problems: list[str] = []
        for drift in ("ledger", "record", "plan", "task"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                case = _abandon_case(root, drift)
                with patch.object(Path, "unlink", side_effect=OSError("crash-after-audit")):
                    with self.assertRaises(ExecutionRecoveryError):
                        abandon_catalog_update_recovery(case.task_id, **case.kwargs)
                self.assertEqual(1, len(list((root / "audit").glob("*.json"))))
                if drift == "ledger":
                    payload = json.loads(case.ledger_path.read_text("utf-8"))
                    payload["entries"][0]["status"] = "tampered"
                    _write_json(case.ledger_path, payload)
                elif drift == "record":
                    _write_json(case.record_path, {"task_id": case.task_id, "kind": "changed"})
                elif drift == "plan":
                    plan_path = case.store.path_for(case.task_id)
                    payload = json.loads(plan_path.read_text("utf-8"))
                    payload.setdefault("metadata", {})["post_audit_drift"] = True
                    _write_json(plan_path, payload)
                else:
                    payload = json.loads(case.task_path.read_text("utf-8"))
                    payload["execution_status"] = "completed"
                    _write_json(case.task_path, payload)
                try:
                    abandon_catalog_update_recovery(case.task_id, **case.kwargs)
                except ExecutionRecoveryError:
                    continue
                problems.append(f"{drift}: stale audit allowed active-surface cleanup")
        self.assertEqual([], problems, "\n".join(problems))

    def test_f6_unchanged_existing_audit_retry_is_idempotent_control(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = _abandon_case(root, "unchanged")
            with patch.object(Path, "unlink", side_effect=OSError("crash-after-audit")):
                with self.assertRaises(ExecutionRecoveryError):
                    abandon_catalog_update_recovery(case.task_id, **case.kwargs)
            audit = abandon_catalog_update_recovery(case.task_id, **case.kwargs)
            self.assertEqual(case.task_id, audit["task_id"])
            self.assertFalse(case.task_path.exists())
            self.assertFalse(case.store.path_for(case.task_id).exists())
            self.assertFalse(case.review_path.exists())
            self.assertTrue(case.ledger_path.exists())
            self.assertTrue(case.record_path.exists())

    def test_f6_existing_audit_rejects_multi_surface_and_wrong_task_ledger(self):
        problems: list[str] = []
        for variant in ("ledger-and-task", "wrong-task-ledger"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                case = _abandon_case(root, variant)
                with patch.object(Path, "unlink", side_effect=OSError("crash-after-audit")):
                    with self.assertRaises(ExecutionRecoveryError):
                        abandon_catalog_update_recovery(case.task_id, **case.kwargs)
                if variant == "ledger-and-task":
                    ledger = json.loads(case.ledger_path.read_text("utf-8"))
                    ledger["entries"][0]["status"] = "tampered-after-audit"
                    _write_json(case.ledger_path, ledger)
                    task = json.loads(case.task_path.read_text("utf-8"))
                    task["execution_status"] = "completed"
                    _write_json(case.task_path, task)
                else:
                    ledger = json.loads(case.ledger_path.read_text("utf-8"))
                    ledger["task_id"] = "different-synthetic-task"
                    _write_json(case.ledger_path, ledger)
                blocked = False
                try:
                    abandon_catalog_update_recovery(case.task_id, **case.kwargs)
                except ExecutionRecoveryError:
                    blocked = True
                active_surfaces = (
                    case.review_path,
                    case.store.path_for(case.task_id),
                    case.task_path,
                )
                if not blocked or not all(path.exists() for path in active_surfaces):
                    problems.append(
                        f"{variant}: stale audit accepted drift or unlinked active surfaces"
                    )
        self.assertEqual([], problems, "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
