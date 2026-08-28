import json
import os
import tempfile
import unittest
from pathlib import Path

from src.rename.decision_state import DecisionState
from src.rename.plan_store import PlanStore
from src.rename.recovery_service import RecoveryService
from src.rename.rename_plan import PlanEntry, RenamePlan
from src.rename.work_catalog import WORK_CATALOG_SCHEMA_VERSION


class RecoveryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.mkv"
        self.target = self.root / "library" / "Example.mkv"
        self.source.write_bytes(b"media")
        self.target.parent.mkdir()
        os.link(self.source, self.target)
        self.plan = RenamePlan(
            "recover-task", self.root,
            (PlanEntry(self.source, self.target, media_kind="video", role="main_episode"),),
            expected_sources=(self.source,),
        )
        self.catalog_root = self.root / "catalog"
        self.record_root = self.root / "record"
        self.catalog_root.mkdir()
        self.record_root.mkdir()
        self.decision = DecisionState(
            task_id="recover-task", revision=3,
            catalog_link_intent={"work_key": "work-missing"},
        )
        self._write_ledger()
        self.service = RecoveryService(
            catalog_root=self.catalog_root, record_root=self.record_root
        )

    def tearDown(self):
        self.temp.cleanup()

    def _write_ledger(self):
        (self.record_root / "recover-task.ledger.json").write_text(
            json.dumps({
                "schema_version": 2,
                "task_id": "recover-task",
                "plan_fingerprint": PlanStore.fingerprint(self.plan),
                "status": "completed",
                "entries": [],
            }), encoding="utf-8",
        )

    def test_catalog_record_missing_is_not_materialization_missing(self):
        snapshot = self.service.diagnose(self.decision, self.plan)
        self.assertEqual("catalog_record_missing", snapshot.state)
        self.assertTrue(snapshot.can_rebuild_catalog)
        self.assertFalse(snapshot.can_rebuild_plan)
        self.assertEqual(
            ("rebuild_catalog", "confirm_catalog_detach"),
            snapshot.allowed_actions,
        )

    def test_missing_registered_root_precedes_missing_materialization(self):
        work_key = "work-12345678-1234-1234-1234-123456789abc"
        missing_root = self.root / "deleted-registered-root"
        (self.catalog_root / f"{work_key}.json").write_text(
            json.dumps(
                {
                    "schema_version": WORK_CATALOG_SCHEMA_VERSION,
                    "work_key": work_key,
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
            ),
            encoding="utf-8",
        )
        self.target.unlink()
        decision = DecisionState(
            task_id=self.plan.task_id,
            revision=3,
            catalog_link_intent={"work_key": work_key},
        )

        snapshot = self.service.diagnose(decision, self.plan)

        self.assertEqual("catalog_root_missing", snapshot.state)
        self.assertEqual(
            ("relocate_catalog", "confirm_catalog_detach"),
            snapshot.allowed_actions,
        )

    def test_deleted_hardlink_generates_new_unapproved_plan_candidate(self):
        self.target.unlink()
        decision = DecisionState(task_id="recover-task", revision=3)
        snapshot = self.service.diagnose(decision, self.plan)
        self.assertEqual("materialization_missing", snapshot.state)
        self.assertTrue(snapshot.can_rebuild_plan)
        store = PlanStore(self.root / "plans")
        revised = self.service.rebuild_missing_materialization_plan(
            decision, self.plan, store
        )
        self.assertEqual("materialization_missing", revised.metadata["recovery_reason"])
        self.assertNotEqual(PlanStore.fingerprint(self.plan), PlanStore.fingerprint(revised))

    def test_partial_materialization_marks_intact_hardlink_for_exact_reuse(self):
        second_source = self.root / "source-2.mkv"
        second_target = self.root / "library" / "Example-2.mkv"
        second_source.write_bytes(b"media-2")
        os.link(second_source, second_target)
        plan = RenamePlan(
            "recover-task", self.root,
            (
                self.plan.entries[0],
                PlanEntry(
                    second_source, second_target,
                    media_kind="video", role="main_episode",
                ),
            ),
            expected_sources=(self.source, second_source),
        )
        self.plan = plan
        self._write_ledger()
        self.target.unlink()
        revised = self.service.rebuild_missing_materialization_plan(
            DecisionState(task_id="recover-task", revision=3),
            plan,
            PlanStore(self.root / "plans-partial"),
        )
        self.assertIsNone(revised.entries[0].collision_resolution)
        self.assertEqual(
            "reuse_existing", revised.entries[1].collision_resolution.action
        )

    def test_foreign_target_is_evidence_conflict(self):
        self.target.unlink()
        self.target.write_bytes(b"foreign")
        snapshot = self.service.diagnose(self.decision, self.plan)
        self.assertEqual("evidence_conflict", snapshot.state)
        self.assertTrue(snapshot.requires_external_action)

    def test_missing_source_is_external_prerequisite(self):
        self.target.unlink()
        self.source.unlink()
        snapshot = self.service.diagnose(self.decision, self.plan)
        self.assertEqual("source_unavailable", snapshot.state)
        self.assertTrue(snapshot.requires_external_action)


if __name__ == "__main__":
    unittest.main()
