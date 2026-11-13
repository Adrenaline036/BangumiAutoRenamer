import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.rename.plan_verifier import PlanVerifier
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import ExecutionPolicy, PlanEntry, PlanStatus, RenamePlan
from src.rename.process import Rename
from tests.presentation_fixtures import with_movie_presentation, with_tv_presentation


class RenamePlanTests(unittest.TestCase):
    def test_plan_collections_are_immutable_snapshots(self):
        entries = []
        metadata = {"name": "Example"}
        plan = RenamePlan(
            task_id="immutable",
            source_root=Path("source"),
            entries=entries,
            metadata=metadata,
        )

        entries.append(
            PlanEntry(Path("late.mkv"), Path("Season1/S01E01.mkv"))
        )
        metadata["name"] = "Changed"

        self.assertEqual(plan.entries, ())
        self.assertEqual(plan.metadata["name"], "Example")
        with self.assertRaises(TypeError):
            plan.metadata["name"] = "Rejected"

    def setUp(self):
        self.verifier = PlanVerifier()

    def test_plan_construction_does_not_create_source_or_target_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source" / "episode.mkv"
            target = root / "library" / "Show" / "Season1" / "S01E01.mkv"

            plan = with_tv_presentation(RenamePlan.from_mapping(
                task_id="pure-plan",
                source_root=source.parent,
                mapping={source: target},
                expected_sources=[source],
            ))

            self.assertFalse(source.parent.exists())
            self.assertFalse(target.parent.exists())
            self.assertEqual({source: target}, plan.as_mapping())
            # Pure structure has no admitted sources or Decision-owned font
            # observation; it must remain read-only, without creating either root.
            report = self.verifier.verify(plan)
            self.assertEqual(PlanStatus.READY, report.status)
            self.assertEqual(set(), {issue.code for issue in report.issues})
            self.assertFalse(source.parent.exists())
            self.assertFalse(target.parent.exists())

    def test_rename_constructor_does_not_create_configured_media_roots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured = {
                "bangumi_path": root / "tv",
                "movie_path": root / "movies",
                "anime_path": root / "anime",
                "anime_movie_path": root / "anime-movies",
            }

            with (
                patch(
                    "src.rename.process.cm.get_config",
                    side_effect=lambda key: str(configured[key]),
                ),
                patch("src.rename.process.Search"),
            ):
                Rename()

            self.assertTrue(all(not path.exists() for path in configured.values()))

    def test_verifier_rejects_duplicate_target_case_insensitively(self):
        root = Path("source")
        plan = with_movie_presentation(RenamePlan(
            task_id="duplicate-target",
            source_root=root,
            entries=(
                PlanEntry.from_paths(root / "01.mkv", Path("out/EP01.mkv")),
                PlanEntry.from_paths(root / "02.mkv", Path("out/ep01.mkv")),
            ),
            expected_sources=(root / "01.mkv", root / "02.mkv"),
        ))

        result = self.verifier.verify(plan)

        self.assertEqual(PlanStatus.INVALID, result.status)
        self.assertIn("duplicate_target", {issue.code for issue in result.issues})

    def test_verifier_rejects_duplicate_video_episode_with_different_names(self):
        root = Path("source")
        plan = RenamePlan.from_mapping(
            task_id="duplicate-episode",
            source_root=root,
            mapping={
                root / "a.mkv": Path("out/Season1/S01E01 - A.mkv"),
                root / "b.mkv": Path("out/Season1/S01E01 - B.mkv"),
            },
        )

        result = self.verifier.verify(plan)

        self.assertEqual(PlanStatus.INVALID, result.status)
        self.assertIn(
            "duplicate_video_episode",
            {issue.code for issue in result.issues},
        )

    def test_verifier_rejects_orphan_and_unexpected_sources(self):
        root = Path("source")
        expected = root / "expected.mkv"
        unexpected = root / "unexpected.mkv"
        plan = RenamePlan.from_mapping(
            task_id="coverage",
            source_root=root,
            mapping={unexpected: Path("out/Season1/S01E01.mkv")},
            expected_sources=[expected],
        )

        result = self.verifier.verify(plan)
        issue_codes = {issue.code for issue in result.issues}

        self.assertEqual(PlanStatus.INVALID, result.status)
        self.assertIn("orphan_source", issue_codes)
        self.assertIn("unexpected_source", issue_codes)

    def test_verifier_rejects_source_outside_declared_source_root(self):
        root = Path("source")
        outside = Path("other") / "episode.mkv"
        plan = RenamePlan.from_mapping(
            task_id="source-escape",
            source_root=root,
            mapping={outside: Path("library/Season1/S01E01.mkv")},
            metadata={"target_root": "library"},
        )

        result = self.verifier.verify(plan)

        self.assertEqual(PlanStatus.INVALID, result.status)
        self.assertIn(
            "source_outside_root", {issue.code for issue in result.issues}
        )

    def test_verifier_rejects_target_outside_declared_target_root(self):
        root = Path("source")
        source = root / "episode.mkv"
        plan = RenamePlan.from_mapping(
            task_id="target-escape",
            source_root=root,
            mapping={source: Path("outside/Season1/S01E01.mkv")},
            metadata={"target_root": "library"},
        )

        result = self.verifier.verify(plan)

        self.assertEqual(PlanStatus.INVALID, result.status)
        self.assertIn(
            "target_outside_root", {issue.code for issue in result.issues}
        )

    def test_verifier_rejects_invalid_declared_target_root(self):
        root = Path("source")
        source = root / "episode.mkv"
        plan = RenamePlan.from_mapping(
            task_id="invalid-target-root",
            source_root=root,
            mapping={source: Path("library/Season1/S01E01.mkv")},
            metadata={"target_root": 42},
        )

        result = self.verifier.verify(plan)

        self.assertEqual(PlanStatus.INVALID, result.status)
        self.assertIn(
            "invalid_target_root", {issue.code for issue in result.issues}
        )

    def test_verifier_rejects_negative_season_and_s00e00(self):
        root = Path("source")
        plan = RenamePlan(
            task_id="invalid-season",
            source_root=root,
            entries=(
                PlanEntry(
                    source_path=root / "negative.mkv",
                    target_path=Path("out/Season-1/negative.mkv"),
                    season_id=-1,
                    media_kind="video",
                ),
                PlanEntry.from_paths(
                    root / "unknown.mkv",
                    Path("out/Season0/S00E00 - unknown.mkv"),
                ),
            ),
            expected_sources=(root / "negative.mkv", root / "unknown.mkv"),
        )

        result = self.verifier.verify(plan)
        issue_codes = {issue.code for issue in result.issues}

        self.assertEqual(PlanStatus.INVALID, result.status)
        self.assertIn("negative_season", issue_codes)
        self.assertIn("s00e00_forbidden", issue_codes)
        self.assertIn("invalid_episode", issue_codes)

    def test_review_flag_does_not_bypass_missing_movie_authority(self):
        root = Path("source")
        entry = PlanEntry(
            source_path=root / "clip.mkv",
            target_path=Path("out/ManualReview/Season1/clip.mkv"),
            season_id=1,
            media_kind="video",
            requires_review=True,
        )
        plan = with_movie_presentation(RenamePlan(
            task_id="review",
            source_root=root,
            entries=(entry,),
            expected_sources=(entry.source_path,),
        ))

        result = self.verifier.verify(plan)
        self.assertTrue(plan.entries[0].requires_review)
        self.assertEqual(PlanStatus.READY, result.status)
        self.assertEqual(set(), {i.code for i in result.issues})

    def test_representative_large_plans_preserve_structure_but_require_font_authority(self):
        cases = {
            "lucky-star-main": (1, 24),
            "ranma-main": (1, 161),
            "next-summit-main": (4, 12),
        }

        for task_id, (season, count) in cases.items():
            with self.subTest(task_id=task_id):
                root = Path("source") / task_id
                mapping = {
                    root / f"{episode:03d}.mkv": (
                        Path("library")
                        / task_id
                        / f"Season{season}"
                        / f"S{season:02d}E{episode:03d}.mkv"
                    )
                    for episode in range(1, count + 1)
                }
                plan = with_tv_presentation(RenamePlan.from_mapping(
                    task_id=task_id,
                    source_root=root,
                    mapping=mapping,
                    expected_sources=mapping,
                ))

                result = self.verifier.verify(plan)

                self.assertEqual(PlanStatus.READY, result.status)
                self.assertEqual(set(), {issue.code for issue in result.issues})
                self.assertEqual(mapping, plan.as_mapping())
                self.assertEqual(count, len(plan.entries))

    def test_execution_policy_roundtrip_and_fingerprint_are_strict(self):
        root = Path.cwd() / "source"
        target_root = Path.cwd() / "library"
        plan = with_tv_presentation(RenamePlan.from_mapping(
            task_id="execution-policy",
            source_root=root,
            mapping={root / "01.mkv": target_root / "Show S01E01.mkv"},
            metadata={"target_root": str(target_root)},
            execution_policy=ExecutionPolicy(target_root=target_root),
        ))
        payload = plan.to_dict()
        restored = RenamePlan.from_dict(payload)

        self.assertEqual(plan, restored)
        report = self.verifier.verify(restored)
        self.assertEqual(PlanStatus.READY, report.status)
        self.assertEqual(set(), {issue.code for issue in report.issues})
        dry_run = replace(
            restored,
            execution_policy=ExecutionPolicy(
                target_root=target_root, dry_run=True
            ),
        )
        self.assertNotEqual(
            PlanStore.fingerprint(restored), PlanStore.fingerprint(dry_run)
        )

    def test_execution_policy_requires_absolute_matching_target_root(self):
        root = Path("source")
        plan = RenamePlan.from_mapping(
            task_id="relative-execution-policy",
            source_root=root,
            mapping={root / "01.mkv": Path("library/Show S01E01.mkv")},
            metadata={"target_root": "library"},
            execution_policy=ExecutionPolicy(target_root=Path("library")),
        )

        codes = {issue.code for issue in self.verifier.verify(plan).issues}
        self.assertIn("execution_target_root_not_absolute", codes)


if __name__ == "__main__":
    unittest.main()
