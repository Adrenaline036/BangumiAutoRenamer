"""Incident-shaped regression contracts for ERR-20260903-024.

The real Kaguya S2 Plan-only failure kept a legal full-width question mark in
the source leaf.  ``normalize_target_stem`` applied NFKC, emitted ASCII ``?``,
and the TV-extra producer persisted that unsafe value until PlanVerifier
correctly blocked the whole package.  These contracts now require the shared
portable stem boundary on every ordinary run.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename.canonical_naming import (
    CanonicalNamingError,
    canonicalize_media_leaf,
    is_portable_media_leaf,
)
from src.rename.episode_inference import normalize_target_stem
from src.rename.plan_review import PlanReviewService
from src.rename.plan_verifier import PlanVerifier
from tests.test_isolated_sample_acceptance import routing_snapshot_for


ASCII_INVALID = '\\/:*?"<>|'
FULLWIDTH_INVALID = "＼／：＊？＂＜＞｜"
FULLWIDTH_MAP = {
    "\\": "＼",
    "/": "／",
    ":": "：",
    "*": "＊",
    "?": "？",
    '"': "＂",
    "<": "＜",
    ">": "＞",
    "|": "｜",
}


def _complete_tv_extra(root: Path):
    from tests.test_isolated_sample_acceptance import (
        IsolatedSampleAcceptanceTests,
    )

    harness = IsolatedSampleAcceptanceTests(methodName="runTest")
    source, allowed, draft, store, service = harness._inventory_plan_with_identity(
        root,
        (
            "Example Work [01] [1080p].mkv",
            f"SPs/Example Work [Menu01] {FULLWIDTH_INVALID} [1080p].mkv",
        ),
        task_id="kaguya-path-leaf-red",
        season_id=2,
        is_movie=False,
    )
    completed = service.complete_inventory_draft(
        draft.task_id,
        store.fingerprint(draft),
        group_id=draft.presentation_groups[0].group_id,
        group_kind="tv_episodes",
        target_root=allowed / "Anime",
        reason="sanitized Kaguya S2 target-leaf incident",
        root_snapshot=routing_snapshot_for(
            allowed / "Anime",
            domain="animation",
            group_kind="tv_episodes",
        ),
        season_number=2,
        season_source="explicit_user",
    )
    extra = next(
        entry
        for entry in completed.entries
        if entry.source_path.name.startswith("Example Work [Menu01] ")
    )
    return source, completed, extra, store


class KaguyaPathLeafRedControls(unittest.TestCase):
    def test_fixture_is_windows_legal_and_shared_builder_maps_all_nine(self):
        self.assertEqual(9, len(ASCII_INVALID))
        self.assertEqual(9, len(FULLWIDTH_INVALID))
        self.assertEqual(FULLWIDTH_INVALID, "".join(FULLWIDTH_MAP[c] for c in ASCII_INVALID))
        self.assertEqual(FULLWIDTH_INVALID, canonicalize_media_leaf(ASCII_INVALID))
        self.assertEqual(FULLWIDTH_INVALID, canonicalize_media_leaf(FULLWIDTH_INVALID))
        self.assertTrue(is_portable_media_leaf(FULLWIDTH_INVALID))

    def test_existing_builder_keeps_empty_device_tail_and_length_guards(self):
        self.assertEqual("A B", canonicalize_media_leaf("  A   B . "))
        self.assertEqual("长" * 240, canonicalize_media_leaf("长" * 300))
        for unsafe in ("", " . ", "..", "CON", "con.txt", "LPT9"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(CanonicalNamingError):
                    canonicalize_media_leaf(unsafe)

    def test_all_four_production_target_producers_use_the_shared_stem_helper(self):
        roots = (
            Path("src/rename/tv_video_projection.py"),
            Path("src/rename/process.py"),
            Path("src/rename/provider_special_evidence.py"),
            Path("src/rename/special_decision_projection.py"),
        )
        calls = []
        for path in roots:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            calls.extend(
                (path.as_posix(), node.lineno)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "normalize_target_stem"
            )
        self.assertEqual(4, len(calls), calls)
        self.assertEqual(1, sum(path.endswith("tv_video_projection.py") for path, _ in calls))
        self.assertEqual(1, sum(path.endswith("special_decision_projection.py") for path, _ in calls))
        self.assertEqual(1, sum(path.endswith("process.py") for path, _ in calls))
        self.assertEqual(
            1,
            sum(path.endswith("provider_special_evidence.py") for path, _ in calls),
        )

    def test_historical_invalid_revision_is_rejected_without_mutating_current(self):
        with tempfile.TemporaryDirectory() as directory:
            _source, completed, extra, store = _complete_tv_extra(Path(directory))
            before = store.fingerprint(completed)
            unsafe_extra = replace(
                extra,
                target_path=extra.target_path.with_name("Menu?invalid.mkv"),
            )
            unsafe = replace(
                completed,
                entries=tuple(
                    unsafe_extra if entry is extra else entry
                    for entry in completed.entries
                ),
            )
            self.assertFalse(is_portable_media_leaf(unsafe_extra.target_path.name))
            self.assertIn(
                "canonical_path_leaf_invalid",
                {issue.code for issue in PlanVerifier().verify(unsafe).issues},
            )
            reloaded = store.load(completed.task_id)
            self.assertEqual(before, store.fingerprint(reloaded))
            self.assertEqual(extra.target_path, next(
                entry.target_path
                for entry in reloaded.entries
                if entry.source_path.name.startswith("Example Work [Menu01] ")
            ))


class KaguyaPathLeafIncidentRedContracts(unittest.TestCase):
    def test_shared_target_stem_explicitly_maps_all_nine_and_is_idempotent(self):
        actual = normalize_target_stem(
            f"Example Work [Menu01] {ASCII_INVALID} [Ma10p_1080p]",
            "Example Work",
        )
        expected = f"Example Work [Menu01] {FULLWIDTH_INVALID}"
        self.assertEqual(expected, actual)
        self.assertTrue(is_portable_media_leaf(actual))
        self.assertEqual(actual, normalize_target_stem(actual, "Example Work"))
        self.assertEqual(
            "ordinary label",
            normalize_target_stem("ordinary label", "Example Work"),
        )

        video = normalize_target_stem(
            f"Example Work [Menu01] {ASCII_INVALID} [Ma10p_1080p]",
            "Example Work",
        )
        subtitle = normalize_target_stem(
            f"Example Work [Menu01] {ASCII_INVALID} [Ma10p_1080p].sc",
            "Example Work",
        )
        self.assertEqual(f"{video}.sc", subtitle)
        self.assertTrue(is_portable_media_leaf(subtitle))

    def test_real_inventory_completion_emits_one_portable_extra_target(self):
        with tempfile.TemporaryDirectory() as directory:
            source, completed, extra, _store = _complete_tv_extra(Path(directory))
            self.assertEqual(
                f"Example Work [Menu01] {FULLWIDTH_INVALID}.mkv",
                extra.target_path.name,
            )
            self.assertTrue(is_portable_media_leaf(extra.target_path.name))
            self.assertNotIn(
                "canonical_path_leaf_invalid",
                {issue.code for issue in PlanVerifier().verify(completed).issues},
            )
            self.assertTrue(all(path.exists() for path in source.iterdir()))
            self.assertFalse(extra.target_path.exists(), "Plan-only RED wrote target media")

    def test_shared_canonicalizer_disconnect_fails_closed(self):
        with patch(
            "src.rename.episode_inference.canonicalize_media_leaf",
            side_effect=CanonicalNamingError("canonicalizer disconnected"),
            create=True,
        ):
            with self.assertRaisesRegex(
                CanonicalNamingError,
                "canonicalizer disconnected",
            ):
                normalize_target_stem("Menu？", "Example Work")


if __name__ == "__main__":
    unittest.main()
