import tempfile
import unittest
from pathlib import Path

from src.rename.decision_state import (
    DecisionCommandService,
    DecisionStateError,
    DecisionStateStore,
)
from src.rename.plan_store import PlanStore
from src.rename.projection_authority import RecognitionDecisionInput
from src.rename.rename_plan import ProviderIdentity, RenamePlan, WorkIdentity


class DecisionStateTests(unittest.TestCase):
    def test_live_lock_blocks_concurrent_update_but_legacy_file_does_not(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DecisionStateStore(root / "decision")
            lock_path = store.root / ".task-lock.json.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text("legacy-token", encoding="ascii")
            store.create("task-lock")
            with store._lock("task-lock"):
                with self.assertRaises(DecisionStateError):
                    store.save(store.load("task-lock"), expected_revision=0)

    def test_restart_reads_authoritative_choices_without_plan_or_task_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DecisionStateStore(root / "decision")
            created = store.create("task-one")
            saved = store.save(
                created.__class__(
                    **{
                        **created.__dict__,
                        "library_domain": "animation",
                        "layout_mode": "tv_episodes",
                        "season_binding": {
                            "season_binding_id": "season-binding-fixed",
                            "season_number": 2,
                            "decision_source": "provider",
                        },
                    }
                ),
                expected_revision=0,
            )
            restarted = DecisionStateStore(root / "decision").load("task-one")
            self.assertEqual(saved, restarted)
            self.assertEqual("animation", restarted.library_domain)
            self.assertEqual("tv_episodes", restarted.layout_mode)
            self.assertEqual(2, restarted.season_binding["season_number"])

    def test_stale_revision_cannot_overwrite_newer_browser_decision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            states = DecisionStateStore(root / "decision")
            states.create("task-two")
            service = DecisionCommandService(states, PlanStore(root / "plan"))
            first, _ = service.update(
                "task-two", expected_revision=0,
                changes={"library_domain": "animation"},
            )
            with self.assertRaisesRegex(DecisionStateError, "版本已变化"):
                service.update(
                    "task-two", expected_revision=0,
                    changes={"library_domain": "general"},
                )
            self.assertEqual("animation", states.load("task-two").library_domain)
            self.assertEqual(1, first.revision)

    def test_plan_is_one_way_snapshot_of_decision_revision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            states = DecisionStateStore(root / "decision")
            states.create("task-three")
            plans = PlanStore(root / "plan")
            service = DecisionCommandService(states, plans)

            def planner(state):
                return RenamePlan("task-three", root, (), metadata={"name": "Example"})

            state, plan = service.update(
                "task-three", expected_revision=0,
                changes={"layout_mode": "tv_episodes"}, planner=planner,
            )
            self.assertEqual(state.revision, plan.metadata["decision_state_revision"])
            self.assertEqual(
                states.fingerprint(state), plan.metadata["decision_state_fingerprint"]
            )
            self.assertEqual(plan, plans.load("task-three"))
            self.assertNotIn("completion_mode", state.to_dict())

    def test_explicit_rerecognition_clears_old_work_dependent_choices(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            states = DecisionStateStore(root / "decision")
            first = RenamePlan(
                "task-four", root, (),
                work_identity=WorkIdentity(
                    title="Old", year=2020, domain="animation",
                    domain_source="provider", status="confirmed",
                    primary_provider=ProviderIdentity(
                        status="confirmed", namespace="tmdb", entity_type="tv",
                        provider_id="1", title="Old", year=2020,
                        decision_source="user",
                    ),
                ),
            )
            initial = states.save(
                RecognitionDecisionInput(
                    task_id=first.task_id,
                    work_identity=first.work_identity,
                ).candidate(None),
                expected_revision=-1,
            )
            chosen = states.save(
                initial.__class__(**{
                    **initial.__dict__,
                    "layout_mode": "tv_episodes",
                    "series_binding": {"series_key": "series-old"},
                    "manual_role_decisions": {"entry": "extra"},
                }),
                expected_revision=initial.revision,
            )
            second = RenamePlan(
                "task-four", root, (),
                work_identity=WorkIdentity(
                    title="New", year=2024, domain="animation",
                    domain_source="provider", status="confirmed",
                    primary_provider=ProviderIdentity(
                        status="confirmed", namespace="tmdb", entity_type="tv",
                        provider_id="2", title="New", year=2024,
                        decision_source="user",
                    ),
                ),
            )
            synchronized = states.save(
                RecognitionDecisionInput(
                    task_id=second.task_id,
                    work_identity=second.work_identity,
                ).candidate(chosen),
                expected_revision=chosen.revision,
            )
            self.assertGreater(synchronized.revision, chosen.revision)
            self.assertEqual("2", synchronized.provider_evidence[0]["provider_id"])
            self.assertIsNone(synchronized.layout_mode)
            self.assertIsNone(synchronized.series_binding)
            self.assertEqual({}, synchronized.manual_role_decisions)


if __name__ == "__main__":
    unittest.main()
