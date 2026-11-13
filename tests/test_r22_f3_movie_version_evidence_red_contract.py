"""F3-A incident RED for movie version evidence boundaries.

The same movie input is completed through the real PlanReviewService path
under a neutral and an unrelated ``2160p-suite`` ancestor.  Production must
derive the same frozen 1080p descriptor in both cases.  Every contract is an
ordinary GREEN test; no temporary expected-failure wrapper remains.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import src.rename.plan_review as plan_review_module
from src.rename.plan_review import PlanReviewError, ReviewStatus, entry_id
from src.rename.plan_verifier import PlanVerifier
from src.rename.projection_authority import (
    ProjectionAuthorityError,
    inventory_fingerprint_for_plan,
    validate_plan_projection_receipt,
)
from src.rename.rename_plan import PlanStatus
import tests.test_isolated_sample_acceptance as movie_fixture


class MovieVersionEvidenceBoundaryRedContracts(unittest.TestCase):
    def _complete_files(
        self,
        base: Path,
        ancestor_name: str,
        files: tuple[str, ...],
        *,
        selected_sources: tuple[str, ...] = (),
    ):
        case_root = base / ancestor_name
        case_root.mkdir()
        harness = movie_fixture.IsolatedSampleAcceptanceTests(methodName="runTest")
        source, allowed, plan, store, service = harness._inventory_plan_with_identity(
            case_root,
            files,
            task_id=f"f3-{ancestor_name}",
        )
        revised = service.complete_inventory_draft(
            plan.task_id,
            store.fingerprint(plan),
            group_id=plan.presentation_groups[0].group_id,
            group_kind="movie_continuous",
            target_root=allowed / "AnimeMovies",
            reason="确认唯一电影正片",
            root_snapshot=movie_fixture.routing_snapshot_for(
                allowed / "AnimeMovies",
                domain="animation",
                group_kind="movie_continuous",
            ),
            primary_video_sources=tuple(
                source / relative for relative in selected_sources
            ) or None,
        )
        content = tuple(
            entry
            for entry in revised.entries
            if entry.presentation_asset is not None
            and entry.presentation_asset.asset_role.kind == "content_version"
        )
        self.assertEqual(len(selected_sources) or 1, len(content))
        self.assertTrue(
            all(
                entry.presentation_asset.version_descriptor is not None
                for entry in content
            )
        )
        return source, revised, content, store, service

    def _complete_case(self, base: Path, ancestor_name: str):
        source, revised, content, store, service = self._complete_files(
            base,
            ancestor_name,
            ("Example Work [Ma10p_1080p].mkv",),
        )
        return source, revised, content[0], store, service

    def test_neutral_ancestor_preserves_explicit_1080p_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            _source, _plan, _content, _store, _service = self._complete_case(
                Path(directory), "neutral-suite-boundary"
            )
            descriptor = _content.presentation_asset.version_descriptor
            self.assertEqual("1080p", descriptor.display_label)
            self.assertIn(" - 1080p", _content.target_path.stem)

    def test_unrelated_2160p_ancestor_cannot_override_explicit_1080p_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            neutral = self._complete_case(base, "neutral-suite-boundary")
            polluted = self._complete_case(base, "2160p-suite-boundary")
            neutral_descriptor = neutral[2].presentation_asset.version_descriptor
            polluted_descriptor = polluted[2].presentation_asset.version_descriptor
            self.assertEqual("1080p", neutral_descriptor.display_label)
            neutral_evidence = neutral_descriptor.to_dict()
            polluted_evidence = polluted_descriptor.to_dict()
            neutral_evidence.pop("version_ref")
            polluted_evidence.pop("version_ref")
            self.assertEqual(
                neutral_evidence,
                polluted_evidence,
                "an unrelated ancestor directory changed the frozen movie descriptor",
            )

    def test_controlled_directory_supplies_missing_resolution_token(self):
        with tempfile.TemporaryDirectory() as directory:
            _source, _plan, content, _store, _service = self._complete_files(
                Path(directory),
                "controlled-directory-fallback",
                ("2160p/Example Work.mkv",),
            )
            descriptor = content[0].presentation_asset.version_descriptor
            self.assertEqual("2160p", descriptor.display_label)
            self.assertEqual("2160p", descriptor.resolution.value)
            self.assertEqual("filename_hint", descriptor.resolution.selected_source)

    def test_filename_and_controlled_directory_resolution_conflict_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PlanReviewError, "规格证据冲突"):
                self._complete_files(
                    Path(directory),
                    "controlled-directory-conflict",
                    ("2160p/Example Work [1080p].mkv",),
                )

    def test_resolution_and_dynamic_range_combine_across_trusted_layers(self):
        files = (
            "2160p/Example Work [DoVi P8.1].mkv",
            "1080p/Example Work [SDR].mkv",
        )
        with tempfile.TemporaryDirectory() as directory:
            _source, _plan, content, _store, _service = self._complete_files(
                Path(directory),
                "controlled-directory-combinations",
                files,
                selected_sources=files,
            )
            labels = {
                entry.presentation_asset.version_descriptor.display_label
                for entry in content
            }
            self.assertEqual(
                {"2160p Dolby Vision P8.1", "1080p SDR"}, labels
            )

    def test_movie_revision_receipt_binds_inventory_and_descriptor_entry_set(self):
        with tempfile.TemporaryDirectory() as directory:
            source, plan, content, store, service = self._complete_case(
                Path(directory), "neutral-suite-receipt"
            )
            receipt = plan.projection_receipt
            self.assertIsNotNone(receipt)
            self.assertEqual(source, plan.source_root)
            self.assertEqual(
                inventory_fingerprint_for_plan(plan),
                receipt.authority["inventory_fingerprint"],
            )
            validate_plan_projection_receipt(plan)

            descriptor = content.presentation_asset.version_descriptor
            tampered_asset = replace(
                content.presentation_asset,
                version_descriptor=replace(descriptor, display_label="2160p"),
            )
            tampered_entry = replace(content, presentation_asset=tampered_asset)
            tampered = replace(
                plan,
                entries=tuple(
                    tampered_entry if entry.source_path == content.source_path else entry
                    for entry in plan.entries
                ),
            )
            with self.assertRaises(ProjectionAuthorityError):
                validate_plan_projection_receipt(tampered)

            with patch.object(
                plan_review_module,
                "_infer_movie_version_descriptor",
                side_effect=AssertionError("approval rescanned live path evidence"),
            ):
                verification = PlanVerifier().verify(plan)
                self.assertEqual(PlanStatus.NEED_CONFIRM, verification.status)
                self.assertEqual((), verification.issues)
                decision = service.approve_entries(
                    plan.task_id,
                    store.fingerprint(plan),
                    tuple(entry_id(entry) for entry in plan.entries),
                )
            self.assertEqual(ReviewStatus.APPROVED, decision.status)

    def test_descriptor_inference_has_one_completion_writer_and_no_consumer_rescan(self):
        source = Path(plan_review_module.__file__).read_text("utf-8")
        tree = ast.parse(source, filename=str(plan_review_module.__file__))
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_infer_movie_version_descriptor"
        ]
        self.assertEqual([], calls, 'retired completion no longer observes movie descriptors')
        expected = {
            'pages/asset_choice_forms.py': 'self.plan.source_root',
            'pages/movie_content_actions.py': 'plan.source_root',
            'rename/default_activation.py': 'plan.source_root',
            'rename/movie_video_projection.py': 'value.source_root',
        }
        observed = set()
        for path in Path(plan_review_module.__file__).parents[1].rglob('*.py'):
            nodes = [node for node in ast.walk(ast.parse(path.read_text('utf-8')))
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                     and node.func.id == '_infer_movie_version_descriptor']
            if nodes:
                relative = path.relative_to(Path(plan_review_module.__file__).parents[1]).as_posix()
                observed.add(relative)
                self.assertEqual(1, len(nodes))
                self.assertEqual(3, len(nodes[0].args))
                self.assertIn(relative, expected)
                self.assertEqual(expected[relative], ast.unparse(nodes[0].args[2]), relative)
        self.assertEqual(set(expected), observed)

        rename_root = Path(plan_review_module.__file__).parent
        for consumer_name in (
            "plan_execution.py",
            "execution_recovery.py",
            "recovery_service.py",
        ):
            consumer_source = (rename_root / consumer_name).read_text("utf-8")
            self.assertNotIn("_infer_movie_version_descriptor", consumer_source)

    def test_f3_green_has_no_temporary_wrapper_or_skip_bypass(self):
        source = Path(__file__).read_text("utf-8")
        legacy_env = "BAR_STRICT_" + "R22_F3_RED"
        self.assertNotIn(legacy_env, source)
        tree = ast.parse(source, filename=__file__)
        forbidden = []
        red_wrapper_nodes = []
        expected_failure_calls = []
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "red_contract"
            ) or (isinstance(node, ast.Name) and node.id == "red_contract"):
                red_wrapper_nodes.append(getattr(node, "lineno", 0))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "expectedFailure"
            ):
                expected_failure_calls.append(getattr(node, "lineno", 0))
            decorators = getattr(node, "decorator_list", ())
            for decorator in decorators:
                name = ast.unparse(
                    decorator.func if isinstance(decorator, ast.Call) else decorator
                )
                if name in {
                    "unittest.skip",
                    "unittest.skipIf",
                    "unittest.skipUnless",
                }:
                    forbidden.append((name, getattr(node, "lineno", 0)))
        self.assertEqual([], red_wrapper_nodes)
        self.assertEqual([], expected_failure_calls)
        self.assertEqual([], forbidden)


if __name__ == "__main__":
    unittest.main()
