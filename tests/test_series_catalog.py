import json
import tempfile
import unittest
import uuid
from pathlib import Path

from src.rename.series_catalog import SeriesCatalog, SeriesCatalogError
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import (
    PresentationGroup, ProviderIdentity, RenamePlan, WorkIdentity,
)
from tests.projection_test_support import save_plan_fixture


def tmdb_tv(provider_id="100565"):
    return {"namespace": "tmdb", "entity_type": "tv", "provider_id": provider_id}


def completed_tv_plan(
    root: Path, *, task_id: str, series_key: str, season: int,
    provider: dict, title: str = "86－不存在的战区－",
) -> RenamePlan:
    identity = ProviderIdentity(
        status="confirmed",
        namespace=str(provider["namespace"]),
        entity_type=str(provider["entity_type"]),
        provider_id=str(provider["provider_id"]),
        title=title,
        year=2021,
        decision_source="user",
    )
    plan = RenamePlan(
        task_id, root, (),
        metadata={
            "series_key": series_key,
            "season_binding_id": f"season-binding-{uuid.uuid4()}",
            "season_number": season,
            "season_source": "provider",
            "canonical_work_root": str(root / "Anime" / "86 (2021)"),
        },
        work_identity=WorkIdentity(
            status="confirmed", domain="animation", title=title, year=2021,
            primary_provider=identity, domain_source="provider",
        ),
        presentation_groups=(
            PresentationGroup("tv", "tv_episodes", "TV 分集版", None),
        ),
    )
    return save_plan_fixture(PlanStore(root / "plans"), plan)


class SeriesCatalogTests(unittest.TestCase):
    def test_live_lock_blocks_but_legacy_file_is_recoverable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog = SeriesCatalog(root / "series")
            series_key = f"series-{uuid.uuid4()}"
            lock_path = catalog.root / f".{series_key}.json.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text("legacy-token", encoding="ascii")
            with catalog.lock(series_key):
                with self.assertRaises(SeriesCatalogError):
                    with catalog.lock(series_key):
                        pass

    def test_same_tmdb_tv_provider_reuses_one_series_root_across_seasons(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog = SeriesCatalog(root / "series")
            series_key = f"series-{uuid.uuid4()}"
            first = catalog.register_completed(
                completed_tv_plan(
                    root, task_id="season-one", series_key=series_key,
                    season=1, provider=tmdb_tv(),
                ),
                expected_revision=0,
            )
            second = catalog.register_completed(
                completed_tv_plan(
                    root, task_id="season-two", series_key=series_key,
                    season=2, provider=tmdb_tv(),
                ),
                expected_revision=1,
            )
            matched = catalog.find_by_provider(tmdb_tv())
            self.assertEqual(series_key, matched.series_key)
            self.assertEqual(first.canonical_root, matched.canonical_root)
            self.assertEqual([1, 2], [x["season_number"] for x in second.season_bindings])

    def test_bangumi_subject_does_not_silently_merge_by_title(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog = SeriesCatalog(root / "series")
            catalog.register_completed(
                completed_tv_plan(
                    root, task_id="bangumi-one",
                    series_key=f"series-{uuid.uuid4()}", season=1,
                    provider={
                        "namespace": "bangumi", "entity_type": "subject",
                        "provider_id": "1",
                    },
                    title="Example",
                ),
                expected_revision=0,
            )
            self.assertIsNone(catalog.find_by_provider(
                {"namespace": "bangumi", "entity_type": "subject", "provider_id": "2", "title": "Example"}
            ))

    def test_shadow_migration_is_read_only_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog = SeriesCatalog(root / "series")
            legacy = {
                "group_kind": "tv_episodes",
                "canonical_root": str(root / "Anime" / "Example"),
                "providers": [tmdb_tv("42")],
            }
            first = catalog.shadow_legacy_record(legacy)
            second = catalog.shadow_legacy_record(legacy)
            self.assertEqual(first, second)
            self.assertEqual("exact", first["classification"])
            self.assertFalse((root / "series").exists())

    def test_relocate_series_verifies_all_ledger_hardlinks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog = SeriesCatalog(root / "series")
            series_key = f"series-{uuid.uuid4()}"
            plan = completed_tv_plan(
                root,
                task_id="season-one",
                series_key=series_key,
                season=1,
                provider=tmdb_tv(),
            )
            catalog.register_completed(plan, expected_revision=0)
            old_root = Path(str(plan.metadata["canonical_work_root"]))
            new_root = root / "Anime" / "Moved 86 (2021)"
            source = root / "source" / "episode.mkv"
            target = new_root / "Season 01" / "86 S01E01.mkv"
            source.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            source.write_bytes(b"episode")
            target.hardlink_to(source)
            record_root = root / "records"
            record_root.mkdir()
            (record_root / "season-one.ledger.json").write_text(
                json.dumps({
                    "task_id": "season-one",
                    "status": "completed",
                    "entries": [{
                        "source": str(source),
                        "target": str(old_root / "Season 01" / target.name),
                    }],
                }),
                encoding="utf-8",
            )
            task_root = root / "tasks"
            task_root.mkdir()
            (task_root / "season-one.json").write_text(
                json.dumps({"workflow_status": "failed"}), encoding="utf-8"
            )

            relocated = catalog.relocate(
                series_key,
                new_root,
                allowed_root=root / "Anime",
                record_root=record_root,
                task_root=task_root,
            )

            self.assertEqual(new_root, relocated.canonical_root)
            payload = json.loads((catalog.root / f"{series_key}.json").read_text("utf-8"))
            self.assertEqual(str(old_root), payload["relocated_from"])

    def test_archive_missing_series_keeps_recovery_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog = SeriesCatalog(root / "series")
            series_key = f"series-{uuid.uuid4()}"
            plan = completed_tv_plan(
                root,
                task_id="season-one",
                series_key=series_key,
                season=1,
                provider=tmdb_tv(),
            )
            catalog.register_completed(plan, expected_revision=0)
            task_root = root / "tasks"
            task_root.mkdir()
            (task_root / "season-one.json").write_text(
                json.dumps({"workflow_status": "failed"}), encoding="utf-8"
            )

            archived = catalog.archive_missing(series_key, task_root=task_root)

            self.assertTrue(archived.is_file())
            self.assertFalse((catalog.root / f"{series_key}.json").exists())


if __name__ == "__main__":
    unittest.main()
