import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.rename.plan_store import PlanStore, PlanStoreError
from src.rename.plan_verifier import PlanVerificationResult
from src.rename.process import Rename
from src.rename.rename_plan import PlanEntry, PlanStatus, RenamePlan
from src.pages.data_table_page import task_status_text
from tests.projection_test_support import save_plan_fixture


class PlanPersistenceTests(unittest.TestCase):
    def _sample_plan(self) -> RenamePlan:
        return RenamePlan(
            task_id="task-1",
            source_root=Path("source"),
            entries=(
                PlanEntry(
                    source_path=Path("source/episode.mkv"),
                    target_path=Path("library/Season1/S01E01.mkv"),
                    season_id=1,
                    episode_id=1,
                    media_kind="video",
                    role="main_episode",
                    requires_review=True,
                    evidence=("rule: episode token",),
                ),
            ),
            expected_sources=(Path("source/episode.mkv"),),
            ignored_sources=(Path("source/cover.jpg"),),
            metadata={"name": "Example", "season_id": 1},
        )

    def test_plan_json_round_trip_preserves_contract(self):
        original = self._sample_plan()

        restored = RenamePlan.from_dict(original.to_dict())

        self.assertEqual(restored, original)
        self.assertEqual(restored.metadata, original.metadata)
        self.assertIsInstance(restored.entries, tuple)
        with self.assertRaises(TypeError):
            restored.metadata["name"] = "Changed"

    def test_store_round_trip_and_unknown_schema_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = PlanStore(Path(temp_dir) / "plans")
            plan = self._sample_plan()
            plan = save_plan_fixture(store, plan)
            saved_path = store.path_for(plan.task_id)

            self.assertEqual(store.load(plan.task_id), plan)
            payload = json.loads(saved_path.read_text(encoding="utf-8"))
            payload["schema_version"] = 999
            saved_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaisesRegex(PlanStoreError, "schema_version"):
                store.load(plan.task_id)

    def test_corrupt_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = PlanStore(Path(temp_dir))
            store.root.mkdir(parents=True, exist_ok=True)
            store.path_for("broken").write_text("{broken", encoding="utf-8")

            with self.assertRaises(PlanStoreError):
                store.load("broken")

    def test_task_list_renders_need_confirm_as_pending_handling(self):
        self.assertEqual(
            task_status_text({"error": None, "workflow_status": "need_confirm"}),
            "待处理",
        )
        self.assertEqual(
            task_status_text({"error": None, "workflow_status": "approved"}),
            "已批准 · 未执行",
        )

    def test_need_confirm_is_persisted_and_does_not_execute(self):
        renamer = object.__new__(Rename)
        renamer.search = SimpleNamespace(TMDB_KEY="offline-test-key")
        renamer.ai_processor = SimpleNamespace(
            ai_client=SimpleNamespace(is_available=lambda: False)
        )
        renamer.R = {}
        renamer.last_plan = None
        renamer.check_task_type = Mock(
            return_value=(
                "示例电影",
                {
                    "id": 1234,
                    "title": "示例电影",
                    "original_title": "Example Movie",
                    "release_date": "2026-01-01",
                },
                False,
                True,
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "Example Movie.mkv"
            source.touch()
            renamer.ANIME_PATH = root / "anime"
            renamer.ANIME_MOVIE_PATH = root / "anime-movie"
            renamer.MOVIE_PATH = root / "movie"
            renamer.BANGUMI_PATH = root / "tv"
            tasks = root / "task"
            tasks.mkdir()

            with (
                patch("src.rename.process.TASK_PATH", tasks),
                patch("src.rename.process.PLAN_PATH", root / "plan"),
                patch("src.rename.process.PlanVerifier") as verifier_factory,
                patch(
                    "src.rename.process.Trans", create=True
                ) as trans_factory,
            ):
                verifier_factory.return_value.verify.return_value = (
                    PlanVerificationResult(PlanStatus.NEED_CONFIRM)
                )
                result = renamer._process(source, _is_movie=True)

            task = json.loads(
                (tasks / f"{renamer.last_plan.task_id}.json").read_text(
                    encoding="utf-8"
                )
            )
            persisted = PlanStore(root / "plan").load(
                renamer.last_plan.task_id
            )

        self.assertEqual(result, PlanStatus.NEED_CONFIRM.value)
        self.assertEqual(task["workflow_status"], "need_confirm")
        self.assertEqual(task["plan_status"], "need_confirm")
        self.assertEqual(persisted, renamer.last_plan)
        trans_factory.assert_not_called()

    def test_accepted_plan_still_requires_human_approval_before_execution(self):
        renamer = object.__new__(Rename)
        renamer.search = SimpleNamespace(TMDB_KEY="offline-test-key")
        renamer.ai_processor = SimpleNamespace(
            ai_client=SimpleNamespace(is_available=lambda: False)
        )
        renamer.R = {}
        renamer.last_plan = None
        renamer.check_task_type = Mock(
            return_value=(
                "示例电影",
                {
                    "id": 1234,
                    "title": "示例电影",
                    "original_title": "Example Movie",
                    "release_date": "2026-01-01",
                },
                False,
                True,
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "Example Movie.mkv"
            source.touch()
            renamer.ANIME_PATH = root / "anime"
            renamer.ANIME_MOVIE_PATH = root / "anime-movie"
            renamer.MOVIE_PATH = root / "movie"
            renamer.BANGUMI_PATH = root / "tv"
            tasks = root / "task"
            tasks.mkdir()

            with (
                patch("src.rename.process.TASK_PATH", tasks),
                patch("src.rename.process.PLAN_PATH", root / "plan"),
                patch("src.rename.process.PlanVerifier") as verifier_factory,
                patch(
                    "src.rename.process.Trans", create=True
                ) as trans_factory,
            ):
                verifier_factory.return_value.verify.return_value = (
                    PlanVerificationResult(PlanStatus.ACCEPTED)
                )
                result = renamer._process(source, _is_movie=True)

            task = json.loads(
                (tasks / f"{renamer.last_plan.task_id}.json").read_text(
                    encoding="utf-8"
                )
            )
            persisted = PlanStore(root / "plan").load(
                renamer.last_plan.task_id
            )

        self.assertEqual(result, PlanStatus.NEED_CONFIRM.value)
        self.assertEqual(task["workflow_status"], "need_confirm")
        self.assertEqual(task["plan_status"], "accepted")
        self.assertEqual(persisted, renamer.last_plan)
        trans_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
