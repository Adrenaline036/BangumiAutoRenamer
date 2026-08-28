"""Incident-derived regression contracts for domain-aware target routing.

The live Plan-only counterexample resolved an animation TV cohort correctly
but persisted every target under the general ``TV`` root.  These tests keep
provider transport local and all writes under ``TemporaryDirectory`` while
exercising the real AddTask -> queue/worker -> provider callback -> Plan path.

The contracts exercise ordinary assertions in both default and legacy STRICT
environments; no expected-failure wrapper remains after GREEN.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import src.pages.add_task_page as add_task_page
import src.pages.plan_review_page as review_page
import src.rename.process as process_module
import tests.test_isolated_sample_acceptance as isolated_fixture
import tests.test_r22_revised_c_c0_red_contract as c1_fixture
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.plan_review import PlanReviewError, PlanReviewService
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.provider_link_decision import (
    provider_subject_cohort_request_fingerprint,
)
from src.rename.target_routing import (
    TargetRootSnapshot,
    resolve_target_root,
)


def _snapshot(flow_root: Path) -> TargetRootSnapshot:
    return TargetRootSnapshot.capture({
        "bangumi_path": flow_root / "TV",
        "movie_path": flow_root / "Movies",
        "anime_path": flow_root / "Anime",
        "anime_movie_path": flow_root / "AnimeMovies",
    })


@contextmanager
def _configured_roots(snapshot: TargetRootSnapshot):
    values = {
        **snapshot.to_dict(),
        "mode": "链接",
        "ai_confidence_threshold": "Medium",
        "docker_mnt": "",
    }
    with patch.object(
        process_module.cm,
        "get_config",
        side_effect=lambda key: values.get(str(key), ""),
    ):
        yield


_ORIGINAL_ADD_TASK_SUBMIT = add_task_page.AddTaskDialog._submit


async def _submit_as_automatic(instance) -> None:
    """Drive the public AddTask entry with the live incident's auto mode."""

    instance.media_type = "auto"
    await _ORIGINAL_ADD_TASK_SUBMIT(instance)


async def _run_auto_boundary_incident(root: Path, work: str):
    snapshot = _snapshot(root / "flow")
    with (
        _configured_roots(snapshot),
        patch.object(
            add_task_page.AddTaskDialog,
            "_submit",
            _submit_as_automatic,
        ),
    ):
        result = await c1_fixture._run_boundary_incident(root, work)
    return result, snapshot


def _completed_movie_plan(root: Path):
    harness = isolated_fixture.IsolatedSampleAcceptanceTests(methodName="runTest")
    _source, _allowed, draft, store, service = (
        harness._inventory_plan_with_identity(
            root,
            ("Example Work [Ma10p_1080p].mkv",),
            task_id="routing-verifier",
        )
    )
    snapshot = _snapshot(root / "library-roots")
    completed = service.complete_inventory_draft(
        draft.task_id,
        store.fingerprint(draft),
        group_id=draft.presentation_groups[0].group_id,
        group_kind="movie_continuous",
        target_root=snapshot.anime_movie_path,
        reason="路由验证夹具确认动画电影正片",
        root_snapshot=snapshot.to_dict(),
        work_domain="animation",
    )
    return completed, snapshot


def _issue_codes(plan) -> set[str]:
    return {issue.code for issue in PlanVerifier().verify(plan).issues}


def _rebase_target_root(plan, target_root: Path):
    original_root = Path(str(plan.metadata["target_root"]))
    original_canonical = Path(str(plan.metadata["canonical_work_root"]))
    canonical = target_root / original_canonical.name
    metadata = dict(plan.metadata)
    metadata.update({
        "target_root": str(target_root),
        "allowed_target_root": str(target_root),
        "canonical_work_root": str(canonical),
    })
    entries = tuple(
        replace(
            entry,
            target_path=target_root / entry.target_path.relative_to(original_root),
        )
        for entry in plan.entries
    )
    return replace(
        plan,
        metadata=metadata,
        entries=entries,
        execution_policy=replace(plan.execution_policy, target_root=target_root),
    )


def _bind_preview_to_snapshot(task: dict, snapshot: TargetRootSnapshot) -> dict:
    revised = deepcopy(task)
    operation = revised["selection_operation"]
    preview = operation["provider_subject_cohort_preview"]
    request = dict(preview["request"])
    request.update({
        "schema_version": 3,
        "target_root_config_fingerprint": snapshot.fingerprint,
        "resolved_target_root": str(snapshot.anime_path),
    })
    fingerprint = provider_subject_cohort_request_fingerprint(request)
    preview.update({
        "schema_version": 3,
        "request": request,
        "request_fingerprint": fingerprint,
        "target_root_config_fingerprint": snapshot.fingerprint,
        "resolved_target_root": str(snapshot.anime_path),
    })
    return revised


class RoutingCompletionApiRedContracts(unittest.TestCase):
    def test_complete_inventory_snapshot_is_a_required_keyword(self):
        parameter = inspect.signature(
            PlanReviewService.complete_inventory_draft
        ).parameters["root_snapshot"]
        self.assertIs(
            inspect.Parameter.empty,
            parameter.default,
            "root_snapshot still has a permissive default",
        )

    def test_complete_inventory_rejects_explicit_none_as_a_business_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            harness = isolated_fixture.IsolatedSampleAcceptanceTests(
                methodName="runTest"
            )
            _source, allowed, draft, store, service = (
                harness._inventory_plan_with_identity(
                    root,
                    ("Example Work [Ma10p_1080p].mkv",),
                    task_id="explicit-none-routing",
                )
            )
            with self.assertRaisesRegex(PlanReviewError, "冻结目标配置"):
                service.complete_inventory_draft(
                    draft.task_id,
                    store.fingerprint(draft),
                    group_id=draft.presentation_groups[0].group_id,
                    group_kind="movie_continuous",
                    target_root=allowed / "AnimeMovies",
                    reason="显式None必须稳定拒绝",
                    root_snapshot=None,
                    work_domain="animation",
                )


class RoutingVerifierRedContracts(unittest.TestCase):
    def test_verifier_reports_missing_and_invalid_frozen_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _snapshot_value = _completed_movie_plan(Path(directory))
            missing_metadata = dict(plan.metadata)
            missing_metadata.pop("target_root_snapshot", None)
            missing_metadata.pop("target_root_config_fingerprint", None)
            missing = replace(plan, metadata=missing_metadata)
            self.assertIn("target_root_snapshot_missing", _issue_codes(missing))
            self.assertIn(
                "target_root_snapshot_missing",
                _issue_codes(replace(missing, execution_policy=None)),
                "review-only completion bypassed the frozen routing gate",
            )

            invalid_metadata = dict(plan.metadata)
            invalid_metadata["target_root_snapshot"] = {
                "anime_path": "relative/Anime"
            }
            invalid_metadata["target_root_config_fingerprint"] = "0" * 64
            invalid = replace(plan, metadata=invalid_metadata)
            self.assertIn("target_root_snapshot_invalid", _issue_codes(invalid))

    def test_verifier_rejects_an_internally_consistent_wrong_domain_root(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, snapshot = _completed_movie_plan(Path(directory))
            wrong = _rebase_target_root(plan, snapshot.movie_path)
            self.assertIn("target_root_route_mismatch", _issue_codes(wrong))

    def test_verifier_rejects_entry_outside_the_canonical_work_root(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, snapshot = _completed_movie_plan(Path(directory))
            entry = plan.entries[0]
            canonical = Path(str(plan.metadata["canonical_work_root"]))
            foreign = snapshot.anime_movie_path / "Different Work (2026)"
            foreign_name = entry.target_path.name.replace(
                canonical.name, foreign.name, 1
            )
            escaped = replace(
                plan,
                entries=(replace(
                    entry,
                    target_path=foreign / foreign_name,
                ),),
            )
            self.assertIn(
                "target_path_outside_canonical_root",
                _issue_codes(escaped),
            )


class RoutingProductionCallbackRedContracts(unittest.IsolatedAsyncioTestCase):
    async def test_incident_fixture_freezes_four_distinct_roots_in_auto_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            result, snapshot = await _run_auto_boundary_incident(
                Path(directory), "yama"
            )
            task = c1_fixture._load_task(result)
            frozen = TargetRootSnapshot.from_dict(task["draft_root_snapshot"])
            self.assertIsNone(task.get("is_anime"))
            self.assertEqual(snapshot.to_dict(), frozen.to_dict())
            self.assertEqual(
                snapshot.fingerprint,
                task.get("draft_root_config_fingerprint"),
            )
            self.assertEqual(4, len(set(frozen.to_dict().values())))
            self.assertEqual(
                str(snapshot.bangumi_path),
                task["draft_suggested_roots"]["tv_episodes"],
            )
            self.assertFalse(result.business_json)
            self.assertTrue(result.source_unchanged)

    async def _assert_cohort_routes_to_animation_library(
        self, root: Path, work: str
    ) -> list[str]:
        result, snapshot = await _run_auto_boundary_incident(root, work)
        task = c1_fixture._load_task(result)
        operation = task.get("selection_operation") or {}
        preview = operation.get("provider_subject_cohort_preview") or {}
        request = preview.get("request") or {}
        expected_root = resolve_target_root(
            "animation", "tv_episodes", snapshot
        )
        problems: list[str] = []
        if preview.get("schema_version") != 3:
            problems.append(f"{work}: preview schema is not routing-aware v3")
        if request.get("target_root_config_fingerprint") != snapshot.fingerprint:
            problems.append(f"{work}: preview request omitted frozen root fingerprint")
        if request.get("resolved_target_root") != str(expected_root):
            problems.append(f"{work}: preview request did not bind the Anime root")
        if preview.get("request_fingerprint") != (
            provider_subject_cohort_request_fingerprint(request)
            if request else None
        ):
            problems.append(f"{work}: preview fingerprint does not cover routing")

        dialog = c1_fixture._boundary_dialog(result, task=task)
        capture = c1_fixture.special_fixture._CaptureUi()
        capture.card = capture.column
        notices: list[tuple[str, object]] = []
        with (
            _configured_roots(snapshot),
            c1_fixture._boundary_patches(result, notices),
            patch.object(review_page, "ui", capture),
        ):
            PlanReviewDialog._render_selection_operation(dialog)
        if str(expected_root) not in "\n".join(capture.visible):
            problems.append(f"{work}: operator preview did not display the Anime root")

        revision, evidence, edge = c1_fixture._operation_arguments(task)
        with (
            _configured_roots(snapshot),
            c1_fixture._boundary_patches(result, notices),
        ):
            PlanReviewDialog._apply_provider_subject_boundary_inline(
                dialog, True, revision, evidence, edge
            )
        completed = PlanStore(result.plans).load(result.task_id)
        metadata = completed.metadata
        canonical = Path(str(metadata.get("canonical_work_root") or ""))
        exact_roots = {
            Path(str(metadata.get("allowed_target_root") or "")),
            Path(str(metadata.get("target_root") or "")),
            completed.execution_policy.target_root,
        }
        if exact_roots != {expected_root}:
            problems.append(f"{work}: Plan roots are not exactly the Anime root")
        try:
            canonical.relative_to(expected_root)
        except ValueError:
            problems.append(f"{work}: canonical work root escaped Anime")
        for entry in completed.entries:
            try:
                entry.target_path.relative_to(canonical)
            except ValueError:
                problems.append(
                    f"{work}: {entry.target_path.name} escaped canonical work root"
                )
        final_snapshot = metadata.get("target_root_snapshot")
        if final_snapshot != snapshot.to_dict():
            problems.append(f"{work}: Plan did not persist the preview snapshot")
        if metadata.get("target_root_config_fingerprint") != snapshot.fingerprint:
            problems.append(f"{work}: Plan root fingerprint forked from preview")
        receipt = metadata.get(
            "provider_subject_boundary_selection_receipt"
        ) or {}
        if receipt.get("schema_version") != 3:
            problems.append(f"{work}: receipt schema is not routing-aware v3")
        if receipt.get("target_root_config_fingerprint") != snapshot.fingerprint:
            problems.append(f"{work}: receipt omitted frozen root fingerprint")
        if receipt.get("resolved_target_root") != str(expected_root):
            problems.append(f"{work}: receipt did not bind the Anime root")
        if receipt.get("request_fingerprint") != preview.get(
            "request_fingerprint"
        ):
            problems.append(f"{work}: preview and receipt routing fingerprints forked")
        return problems

    async def test_yama_cohort_preview_and_plan_route_to_anime(self):
        with tempfile.TemporaryDirectory() as directory:
            problems = await self._assert_cohort_routes_to_animation_library(
                Path(directory), "yama"
            )
        self.assertEqual([], problems, "\n".join(problems))

    async def test_kaguya_cohort_preview_and_plan_route_to_anime(self):
        with tempfile.TemporaryDirectory() as directory:
            problems = await self._assert_cohort_routes_to_animation_library(
                Path(directory), "kaguya"
            )
        self.assertEqual([], problems, "\n".join(problems))

    async def test_old_preview_is_invalidated_when_live_root_snapshot_drifts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, snapshot = await _run_auto_boundary_incident(root, "yama")
            task = _bind_preview_to_snapshot(
                c1_fixture._load_task(result), snapshot
            )
            task_path = result.tasks / f"{result.task_id}.json"
            task_path.write_text(
                json.dumps(task, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            dialog = c1_fixture._boundary_dialog(result, task=task)
            revision, evidence, edge = c1_fixture._operation_arguments(task)
            before = c1_fixture._authority_bytes(result)
            changed = TargetRootSnapshot.capture({
                **snapshot.to_dict(),
                "anime_path": root / "changed-after-preview" / "Anime",
            })
            notices: list[tuple[str, object]] = []
            with (
                _configured_roots(changed),
                c1_fixture._boundary_patches(
                    result,
                    notices,
                    root_snapshot=changed.to_dict(),
                ),
            ):
                PlanReviewDialog._apply_provider_subject_boundary_inline(
                    dialog, True, revision, evidence, edge
                )
            self.assertEqual(
                before,
                c1_fixture._authority_bytes(result),
                "a callback frozen under an old root snapshot changed authority",
            )
            self.assertTrue(
                any("路径配置" in message or "页面内容已经更新" in message
                    for message, _kind in notices),
                "root drift did not produce a stable operator-visible refresh action",
            )


if __name__ == "__main__":
    unittest.main()
