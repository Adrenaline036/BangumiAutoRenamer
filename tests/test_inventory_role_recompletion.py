"""Component completion/role tests with real temporary source admission.

The supplied work is a movie identity; choosing TV presentation must not turn
it into a series identity. These drafts are not production-entry evidence.
"""
import json
import tempfile
import unittest
from pathlib import Path

from src.rename.inventory_plan import build_inventory_review_draft
from src.rename.movie_assets import inventory_movie_assets
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from tests.test_isolated_sample_acceptance import routing_snapshot_for
from tests.projection_test_support import save_plan_fixture, task_source_scope_fixture


class InventoryRoleRecompletionTests(unittest.TestCase):
    def _draft(self, root: Path):
        source = root / "source"
        source.mkdir()
        (source / "Episode 01.mkv").write_bytes(b"episode")
        (source / "Collaboration Movie.mkv").write_bytes(b"unknown-a")
        (source / "Hands Up to the Sky MV.mkv").write_bytes(b"unknown-b")
        task = {
            "uuid": "role-recompletion",
            "path": str(source),
            "name": "Example",
            "is_anime": True,
            "movie_inventory": inventory_movie_assets(source).to_dict(),
            "draft_target_root": str(root / "library"),
            "provider_identity": {
                "namespace": "tmdb", "entity_type": "movie",
                "provider_id": "100", "title": "Example", "year": 2026,
            },
        }
        return build_inventory_review_draft(task, dry_run=False)

    def test_component_observation_uses_real_admission_and_preserves_task_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlanStore(root / "plans")
            draft = self._draft(root)
            observation = task_source_scope_fixture(store, draft)
            self.assertEqual(3, len(observation.to_dict()["source_identities"]))
            self.assertNotIn("font_contribution", draft.metadata)
            task_path = root / "task" / f"{draft.task_id}.json"
            task = json.loads(task_path.read_text("utf-8"))
            original_scope = task.pop("task_source_scope")
            task["fixture_sentinel"] = "caller-owned-field"
            task_path.write_text(json.dumps(task), encoding="utf-8")
            repeated = task_source_scope_fixture(store, draft)
            restored = json.loads(task_path.read_text("utf-8"))
            self.assertEqual(observation.to_dict(), repeated.to_dict())
            self.assertEqual(original_scope, restored["task_source_scope"])
            self.assertEqual("caller-owned-field", restored["fixture_sentinel"])

    def test_recompletion_uses_origin_group_and_accumulates_roles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlanStore(root / "plans")
            service = PlanReviewService(store, root / "reviews", root / "catalog")
            draft = self._draft(root)
            draft = save_plan_fixture(store, draft)
            task = json.loads((root / "task" / f"{draft.task_id}.json").read_text("utf-8"))
            self.assertEqual(draft.task_id, task["task_source_scope"]["task_id"])
            self.assertEqual(3, len(task["task_source_scope"]["source_identities"]))
            # Deliberately complete a TV plan from the movie placeholder group:
            # this is the lineage transition that previously raised an error.
            completed = service.complete_inventory_draft(
                draft.task_id, store.fingerprint(draft),
                group_id="movie-continuous", group_kind="tv_episodes",
                target_root=root / "library" / "Anime", reason="选择 TV",
                root_snapshot=routing_snapshot_for(
                    root / "library" / "Anime",
                    domain="animation", group_kind="tv_episodes",
                ),
                work_domain="animation",
            )
            self.assertEqual("tv-episodes", completed.presentation_groups[0].group_id)
            self.assertEqual("movie", completed.work_identity.primary_provider.entity_type)
            self.assertEqual("work", completed.metadata["catalog_binding_snapshot"]["backend"])
            first = next(entry for entry in completed.entries
                         if entry.source_path.name == "Collaboration Movie.mkv")
            second = next(entry for entry in completed.entries
                          if entry.source_path.name == "Hands Up to the Sky MV.mkv")
            revised = service.recomplete_inventory_with_role_decisions(
                completed.task_id, store.fingerprint(completed),
                decisions={entry_id(first): "extra"}, reason="确认第一个特典",
            )
            revised2 = service.recomplete_inventory_with_role_decisions(
                revised.task_id, store.fingerprint(revised),
                decisions={entry_id(second): "extra"}, reason="确认第二个特典",
            )
            context = revised2.metadata["inventory_completion_context"]
            self.assertEqual("movie-continuous", context["origin_group_id"])
            self.assertEqual({entry_id(first), entry_id(second)}, set(context["manual_role_decisions"]))
            self.assertEqual(store.fingerprint(revised), revised2.revision_parent_fingerprint)

    def test_failure_does_not_persist_an_intermediate_draft(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlanStore(root / "plans")
            service = PlanReviewService(store, root / "reviews", root / "catalog")
            draft = self._draft(root)
            draft = save_plan_fixture(store, draft)
            completed = service.complete_inventory_draft(
                draft.task_id, store.fingerprint(draft),
                group_id="movie-continuous", group_kind="tv_episodes",
                target_root=root / "library" / "Anime", reason="选择 TV",
                root_snapshot=routing_snapshot_for(
                    root / "library" / "Anime",
                    domain="animation", group_kind="tv_episodes",
                ),
                work_domain="animation",
            )
            fingerprint = store.fingerprint(completed)
            with self.assertRaisesRegex(Exception, "待确认文件"):
                service.recomplete_inventory_with_role_decisions(
                    completed.task_id, fingerprint,
                    decisions={"not-an-entry": "extra"}, reason="错误输入",
                )
            self.assertEqual(fingerprint, store.fingerprint(store.load(completed.task_id)))


if __name__ == "__main__":
    unittest.main()
