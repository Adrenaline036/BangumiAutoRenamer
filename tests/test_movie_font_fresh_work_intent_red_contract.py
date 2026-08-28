"""RED contracts for fresh sibling movie Plans sharing work-scoped fonts.

The incident shape is two independently generated movie Plans for the same
confirmed TMDB work.  Both Plans exist before either one is executed.  All
provider transport is frozen and every filesystem write stays below a
``TemporaryDirectory``.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import src.pages.plan_review_page as review_page
import src.utils.utils as utils_module
import tests.test_movie_font_work_catalog_red_contract as movie_fixture
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.plan_execution import PlanExecutionService
from src.rename.plan_review import entry_id
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import ProviderIdentity
from src.rename.utils import is_font_archive
from src.rename.work_catalog import WorkCatalog


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_manifest(root: Path) -> dict[str, tuple[int, str]]:
    return {
        str(path.relative_to(root)): (path.stat().st_size, _sha256(path))
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _make_version(
    root: Path,
    resolution: str,
    *,
    font_bytes: bytes = b"violet-shared-font-package",
) -> tuple[Path, Path, Path]:
    source = root / (
        "[VCB-Studio] Gekijouban Violet Evergarden "
        f"[Ma10p_{resolution}]"
    )
    main = _write(
        source / (
            "[VCB-Studio] Gekijouban Violet Evergarden "
            f"[Ma10p_{resolution}][x265_flac].mkv"
        ),
        f"violet-main-{resolution}".encode("ascii"),
    )
    stem = (
        "[VCB-Studio] Gekijouban Violet Evergarden "
        f"[Ma10p_{resolution}][x265_flac]"
    )
    _write(source / f"{stem}.mka", f"violet-audio-{resolution}".encode("ascii"))
    _write(source / f"{stem}.sc.ass", f"violet-subtitle-{resolution}".encode("ascii"))
    for label in ("CM01", "CM02", "Menu"):
        _write(
            source / "SPs" / f"{stem} [{label}].mkv",
            f"violet-{resolution}-{label}".encode("ascii"),
        )
    font = _write(
        source / "[VCB-Studio] Gekijouban Violet Evergarden [Fonts].7z",
        font_bytes,
    )
    return source, main, font


def _work_key(plan) -> str:
    return str(
        plan.metadata.get("catalog_work_key")
        or plan.metadata.get("proposed_work_key")
        or ""
    )


def _font_entry(plan):
    return next(item for item in plan.entries if is_font_archive(item.source_path))


def _issue_codes(plan) -> set[str]:
    return {item.code for item in PlanVerifier().verify(plan).issues}


def _load_work_intent_builder():
    """Locate the one public pure builder without freezing its module path."""

    root = Path(__file__).resolve().parents[1] / "src" / "rename"
    matches: list[tuple[str, str]] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "derive_work_intent_key"
            ):
                matches.append((f"src.rename.{path.stem}", node.name))
    if len(matches) != 1:
        raise AssertionError(
            "expected exactly one public derive_work_intent_key pure builder, "
            f"found {matches}"
        )
    module_name, attribute = matches[0]
    value = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(value):
        raise AssertionError("derive_work_intent_key is not callable")
    return value


class FreshWorkIntentPlanningRedContracts(unittest.IsolatedAsyncioTestCase):
    async def _completed_pair(
        self,
        root: Path,
        *,
        order: tuple[str, str] = ("1080p", "2160p"),
        font_payloads: dict[str, bytes] | None = None,
    ):
        harness = movie_fixture.MovieFontProductionRedContracts(
            methodName="runTest"
        )
        payloads = font_payloads or {
            "1080p": b"violet-shared-font-package",
            "2160p": b"violet-shared-font-package",
        }
        packages = {
            resolution: _make_version(
                root / "sources",
                resolution,
                font_bytes=payloads[resolution],
            )
            for resolution in ("1080p", "2160p")
        }
        before = {
            resolution: _source_manifest(packages[resolution][0])
            for resolution in packages
        }
        completed = {}
        for resolution in order:
            source, primary, _font = packages[resolution]
            task_id = f"violet-{resolution}-{order[0]}-first"
            initial = await harness._run_provider_flow(root, source, task_id)
            completed[resolution] = harness._complete_movie(
                root=root,
                result=initial,
                primary=primary,
            )
        return harness, packages, before, completed

    def _approve_without_execute(self, root: Path, completed) -> str:
        plan = completed.store.load(completed.plan.task_id)
        fingerprint = completed.store.fingerprint(plan)
        dialog = SimpleNamespace(
            review_service=completed.review,
            plan=plan,
            selected_ids={entry_id(item) for item in plan.entries},
            task_data=dict(completed.task),
            decision=None,
            review_badge=SimpleNamespace(text=""),
            _refresh_plan_header=MagicMock(),
            _render_entries=MagicMock(),
            _refresh_execute_button=MagicMock(),
            snapshot=review_page.build_plan_review_snapshot(plan),
        )
        dialog._approve = PlanReviewDialog._approve.__get__(dialog)
        with (
            patch.object(utils_module, "TASK_PATH", root / "tasks"),
            patch.object(review_page, "notify"),
        ):
            PlanReviewDialog._approve_all(dialog)
        return fingerprint

    @staticmethod
    def _execution_service(root: Path) -> PlanExecutionService:
        completed_store = movie_fixture.PlanStore(root / "plans")
        return PlanExecutionService(
            completed_store,
            movie_fixture.PlanReviewService(
                completed_store,
                root / "reviews",
                root / "work_catalog",
            ),
            task_root=root / "tasks",
            record_root=root / "records",
            catalog_root=root / "work_catalog",
            series_catalog_root=root / "series_catalog",
            decision_state_root=root / "decisions",
            recovery_transaction_root=root / "recovery_transactions",
            provider_link_catalog_root=root / "provider_links",
        )

    async def test_one_pure_work_intent_builder_is_identity_based_and_fail_closed(self):
        builder = _load_work_intent_builder()
        primary = ProviderIdentity(
            "confirmed", "tmdb", "movie", "533514",
            "紫罗兰永恒花园外传", 2020, "provider_selection",
        )
        same_identity_other_title = ProviderIdentity(
            "confirmed", "tmdb", "movie", "533514",
            "Violet Evergarden: The Movie", 2020, "provider_selection",
        )
        other_provider = ProviderIdentity(
            "confirmed", "tmdb", "movie", "999999",
            "紫罗兰永恒花园外传", 2020, "provider_selection",
        )
        first = builder(
            primary_provider=primary,
            group_kind="movie_continuous",
        )
        self.assertEqual(
            first,
            builder(
                primary_provider=same_identity_other_title,
                group_kind="movie_continuous",
            ),
            "work intent drifted with display title instead of provider identity",
        )
        self.assertNotEqual(
            first,
            builder(
                primary_provider=other_provider,
                group_kind="movie_continuous",
            ),
            "same title under a different TMDB ID was merged",
        )
        self.assertNotEqual(
            first,
            builder(primary_provider=primary, group_kind="tv_episodes"),
            "movie and TV group kinds shared one work intent",
        )
        unconfirmed = ProviderIdentity(
            "candidate", "tmdb", "movie", "533514",
            "紫罗兰永恒花园外传", 2020, "search_guess",
        )
        with self.assertRaises((TypeError, ValueError)):
            builder(
                primary_provider=unconfirmed,
                group_kind="movie_continuous",
            )

    async def test_two_fresh_same_work_plans_share_intent_and_conditional_font_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "flow"
            try:
                _harness, packages, before, completed = await self._completed_pair(root)
            except Exception as exc:
                self.fail(f"fresh same-work Plan-only pair crashed: {exc}")
            plans = {
                resolution: item.store.load(item.plan.task_id)
                for resolution, item in completed.items()
            }
            problems = []
            keys = {_work_key(plan) for plan in plans.values()}
            if len(keys) != 1 or "" in keys:
                problems.append(f"same confirmed work produced divergent intents: {sorted(keys)}")
            font_targets = {_font_entry(plan).target_path for plan in plans.values()}
            if len(font_targets) != 1:
                problems.append("same work/same font content did not freeze one canonical target")
            main_targets = {
                next(
                    item.target_path for item in plans[resolution].entries
                    if item.source_path == packages[resolution][1]
                )
                for resolution in plans
            }
            if len(main_targets) != 2:
                problems.append("1080p and 2160p primaries did not retain distinct version targets")
            for resolution, plan in plans.items():
                payload = json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True)
                if "work-intent-v1" not in payload:
                    problems.append(f"{resolution}: Plan has no versioned work intent")
                if "create_or_reuse_registered_identical" not in payload:
                    problems.append(f"{resolution}: approved conditional font action is absent")
                font = _font_entry(plan)
                if _sha256(font.source_path) not in payload:
                    problems.append(f"{resolution}: font content key is not frozen in Plan")
            for resolution, (source, _main, _font) in packages.items():
                if _source_manifest(source) != before[resolution]:
                    problems.append(f"{resolution}: Plan-only changed the source manifest")
        self.assertEqual([], problems, "\n".join(problems))

    async def test_different_content_pending_siblings_require_shared_primary_decision(self):
        observations = []
        all_problems = []
        for order in (("1080p", "2160p"), ("2160p", "1080p")):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "flow"
                payloads = {
                    "1080p": b"font-content-A",
                    "2160p": b"font-content-B",
                }
                try:
                    _harness, packages, before, completed = await self._completed_pair(
                        root,
                        order=order,
                        font_payloads=payloads,
                    )
                except Exception as exc:
                    self.fail(f"different-content pending pair crashed: {exc}")
                plans = {
                    resolution: item.store.load(item.plan.task_id)
                    for resolution, item in completed.items()
                }
                digests = {_sha256(packages[key][2]) for key in packages}
                combined = "\n".join(
                    json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True)
                    for plan in plans.values()
                )
                keys = {_work_key(plan) for plan in plans.values()}
                codes = {
                    resolution: _issue_codes(plan)
                    for resolution, plan in plans.items()
                }
                if len(keys) != 1 or "" in keys:
                    all_problems.append(f"{order}: different bytes split one work intent")
                if not all(digest in combined for digest in digests):
                    all_problems.append(f"{order}: pending intent lost one content key")
                blocking = {
                    "work_font_primary_content_unresolved",
                    "work_intent_revision_stale",
                }
                if any(not (value & blocking) for value in codes.values()):
                    all_problems.append(
                        f"{order}: Plan can be approved before primary content is chosen"
                    )
                for resolution, (source, _main, _font) in packages.items():
                    if _source_manifest(source) != before[resolution]:
                        all_problems.append(f"{order}/{resolution}: source bytes changed")
                observations.append((order, sorted(digests), sorted(keys)))
        if len(observations) == 2 and observations[0][1:] != observations[1][1:]:
            all_problems.append("different-content result depends on Plan creation order")
        self.assertEqual([], all_problems, "\n".join(all_problems))

    async def test_same_hash_siblings_execute_created_then_truthful_reuse_in_both_orders(self):
        all_problems = []
        for order in (("1080p", "2160p"), ("2160p", "1080p")):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "flow"
                try:
                    harness, packages, before, completed = await self._completed_pair(
                        root, order=order
                    )
                except Exception as exc:
                    self.fail(f"same-hash execution pair crashed during planning: {exc}")
                outcomes = {}
                for resolution in order:
                    try:
                        task, ledger, _service = await harness._approve_and_execute(
                            root=root,
                            completed=completed[resolution],
                        )
                        outcomes[resolution] = (task, ledger, None)
                    except Exception as exc:
                        outcomes[resolution] = ({}, {}, str(exc))

                first, second = order
                first_task, first_ledger, first_error = outcomes[first]
                second_task, second_ledger, second_error = outcomes[second]
                if first_error:
                    all_problems.append(f"{order}: first execution crashed: {first_error}")
                if second_error:
                    all_problems.append(f"{order}: sibling execution crashed: {second_error}")
                if first_task.get("execution_status") != "completed":
                    all_problems.append(f"{order}: first task did not complete")
                if second_task.get("execution_status") != "completed":
                    all_problems.append(f"{order}: compatible sibling did not complete")

                first_plan = completed[first].store.load(completed[first].plan.task_id)
                second_plan = completed[second].store.load(completed[second].plan.task_id)
                first_font = _font_entry(first_plan)
                second_font = _font_entry(second_plan)
                first_row = next(
                    (
                        row for row in first_ledger.get("entries", ())
                        if row.get("source") == str(first_font.source_path)
                    ),
                    {},
                )
                second_row = next(
                    (
                        row for row in second_ledger.get("entries", ())
                        if row.get("source") == str(second_font.source_path)
                    ),
                    {},
                )
                if (
                    first_row.get("status"),
                    first_row.get("action"),
                    first_row.get("transfer_type"),
                ) != ("created", "create", "hardlink"):
                    all_problems.append(f"{order}: first font ledger is not a created hardlink")
                if (
                    second_row.get("status"),
                    second_row.get("action"),
                    second_row.get("transfer_type"),
                ) != (
                    "reused_identical_asset",
                    "reuse_existing",
                    "reuse_existing",
                ):
                    all_problems.append(f"{order}: second font ledger falsely reports creation")
                target = first_font.target_path
                if not target.is_file():
                    all_problems.append(f"{order}: canonical font target is absent")
                else:
                    if not os.path.samefile(first_font.source_path, target):
                        all_problems.append(f"{order}: created source is not the target hardlink")
                    if target.stat().st_nlink < 2:
                        all_problems.append(f"{order}: created font target has nlink < 2")
                    if os.path.samefile(second_font.source_path, target):
                        all_problems.append(f"{order}: reused independent source was misreported as a hardlink")
                    if _sha256(second_font.source_path) != _sha256(target):
                        all_problems.append(f"{order}: reused source content differs from registered target")

                records = sorted((root / "work_catalog").glob("work-*.json"))
                if len(records) != 1:
                    all_problems.append(f"{order}: expected one WorkCatalog record, got {len(records)}")
                else:
                    catalog = WorkCatalog(root / "work_catalog").load(records[0].stem)
                    font_assets = [
                        asset for asset in catalog.get("assets", ())
                        if any(
                            isinstance(item, dict) and item.get("role") == "font"
                            for item in asset.get("associations", ())
                        )
                    ]
                    if len(font_assets) != 1:
                        all_problems.append(f"{order}: catalog did not deduplicate the font asset")
                    else:
                        task_ids = {
                            item.get("task_id")
                            for item in font_assets[0].get("associations", ())
                            if isinstance(item, dict)
                        }
                        expected_ids = {
                            completed[first].plan.task_id,
                            completed[second].plan.task_id,
                        }
                        if task_ids != expected_ids:
                            all_problems.append(f"{order}: catalog lost one sibling association")
                for resolution, (source, _main, _font) in packages.items():
                    if _source_manifest(source) != before[resolution]:
                        all_problems.append(f"{order}/{resolution}: execution changed source bytes")
        self.assertEqual([], all_problems, "\n".join(all_problems))

    async def test_concurrent_same_hash_execution_serializes_on_one_work_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "flow"
            try:
                _harness, packages, before, completed = await self._completed_pair(root)
                fingerprints = {
                    resolution: self._approve_without_execute(root, item)
                    for resolution, item in completed.items()
                }
            except Exception as exc:
                self.fail(f"concurrent execution setup crashed: {exc}")
            services = {
                resolution: self._execution_service(root)
                for resolution in completed
            }
            with patch(
                "src.rename.plan_execution.is_dry_run_enabled",
                return_value=False,
            ):
                outcomes = await asyncio.gather(*(
                    asyncio.to_thread(
                        services[resolution].execute,
                        completed[resolution].plan.task_id,
                        fingerprints[resolution],
                    )
                    for resolution in ("1080p", "2160p")
                ), return_exceptions=True)
            problems = []
            for resolution, outcome in zip(("1080p", "2160p"), outcomes):
                if isinstance(outcome, BaseException):
                    problems.append(f"{resolution}: concurrent execute failed: {outcome}")
                task = json.loads(
                    (root / "tasks" / f"{completed[resolution].plan.task_id}.json")
                    .read_text("utf-8")
                )
                if task.get("execution_status") != "completed":
                    problems.append(f"{resolution}: concurrent task did not complete")
            records = tuple((root / "work_catalog").glob("work-*.json"))
            if len(records) != 1:
                problems.append(f"concurrent siblings produced {len(records)} catalog records")
            rows = []
            for resolution in ("1080p", "2160p"):
                ledger_path = root / "records" / (
                    f"{completed[resolution].plan.task_id}.ledger.json"
                )
                ledger = json.loads(ledger_path.read_text("utf-8")) if ledger_path.is_file() else {}
                font_source = str(_font_entry(completed[resolution].plan).source_path)
                rows.extend(
                    row for row in ledger.get("entries", ())
                    if row.get("source") == font_source
                )
            if sorted(row.get("status") for row in rows) != [
                "created", "reused_identical_asset",
            ]:
                problems.append("concurrent ledgers are not exactly one create plus one reuse")
            for resolution, (source, _main, _font) in packages.items():
                if _source_manifest(source) != before[resolution]:
                    problems.append(f"{resolution}: concurrent execution changed source bytes")
        self.assertEqual([], problems, "\n".join(problems))

    async def test_target_before_catalog_is_recovery_pending_not_sibling_takeover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "flow"
            try:
                _harness, packages, before, completed = await self._completed_pair(root)
            except Exception as exc:
                self.fail(f"target-before-Catalog fixture crashed: {exc}")
            first = completed["1080p"].store.load(completed["1080p"].plan.task_id)
            second = completed["2160p"].store.load(completed["2160p"].plan.task_id)
            first_font = _font_entry(first)
            second_font = _font_entry(second)
            first_font.target_path.parent.mkdir(parents=True, exist_ok=True)
            os.link(first_font.source_path, first_font.target_path)
            codes = _issue_codes(second)
            catalog_records = tuple((root / "work_catalog").glob("work-*.json"))
            problems = []
            if "work_font_target_catalog_pending" not in codes:
                problems.append("orphan target is not diagnosed as recovery-pending work intent")
            if second_font.collision_resolution is not None:
                problems.append("sibling silently adopted an unregistered same-byte target")
            if catalog_records:
                problems.append("target-only crash fixture unexpectedly has a WorkCatalog record")
            for resolution, (source, _main, _font) in packages.items():
                if _source_manifest(source) != before[resolution]:
                    problems.append(f"{resolution}: recovery diagnosis changed source bytes")
        self.assertEqual([], problems, "\n".join(problems))


class FreshWorkIntentControls(unittest.TestCase):
    def test_independent_equal_sources_cannot_both_be_the_same_hardlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _write(root / "first.7z", b"same-font-bytes")
            second = _write(root / "second.7z", b"same-font-bytes")
            target = root / "target.7z"
            os.link(first, target)
            self.assertTrue(os.path.samefile(first, target))
            self.assertGreaterEqual(target.stat().st_nlink, 2)
            self.assertFalse(os.path.samefile(second, target))
            self.assertEqual(_sha256(second), _sha256(target))

    def test_unregistered_existing_font_target_remains_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            harness = movie_fixture.MovieFontPlanningAndCatalogRedContracts(
                methodName="runTest"
            )
            _source, plan, _store, _service = harness._draft_movie(
                root=root,
                task_id="orphan1",
                font_name="Example Work [Fonts].7z",
            )
            font = _font_entry(plan)
            _write(font.target_path, font.source_path.read_bytes())
            _source2, later, _store2, _service2 = harness._draft_movie(
                root=root,
                task_id="orphan2",
                font_name="Other Release [Fonts].7z",
            )
            status = PlanVerifier().verify(later).status.value
        self.assertIn(status, {"invalid", "fail_closed"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
