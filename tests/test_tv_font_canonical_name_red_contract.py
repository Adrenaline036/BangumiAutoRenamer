"""RED contracts for human-readable TV series font archive targets.

Provider transport is frozen/offline, while task creation, provider selection,
``Rename.process/_process``, Plan persistence, approval, the execution queue,
``Trans`` and SeriesCatalog use production code in temporary directories.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import tests.test_r1_real_media_production_red_contract as r1_fixture
import tests.test_tv_font_series_association_red_contract as green_fixture
from src.rename.plan_verifier import PlanVerifier
from src.rename.process import _canonical_tv_font_target, _utf16_code_units


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _font_entry(plan, source: Path):
    return next(item for item in plan.entries if item.source_path == source)


def _asset_target(asset: Mapping) -> str | None:
    location = asset.get("location")
    if isinstance(location, Mapping):
        return location.get("target_path")
    return asset.get("target_path")


class TVFontCanonicalNameRedContracts(unittest.IsolatedAsyncioTestCase):
    def test_safe_canonical_component_is_stable_and_fail_closed(self):
        digest = "a" * 64
        sanitized = _canonical_tv_font_target(
            Path("C:/Anime/A:B. "),
            digest,
            disambiguate=False,
        )
        self.assertEqual("A_B [Fonts].7z", sanitized.name)

        long_root = Path("C:/Anime") / ("向山进发" * 80)
        first = _canonical_tv_font_target(
            long_root,
            digest,
            disambiguate=True,
        )
        second = _canonical_tv_font_target(
            long_root,
            digest,
            disambiguate=True,
        )
        self.assertEqual(first, second)
        self.assertLessEqual(_utf16_code_units(first.name), 240)
        self.assertTrue(first.name.endswith(f"[{digest}] [Fonts].7z"))

        with self.assertRaisesRegex(ValueError, "安全的字体文件名"):
            _canonical_tv_font_target(
                Path("C:/Anime/CON"),
                digest,
                disambiguate=False,
            )

    async def _run_flow(
        self,
        *,
        root: Path,
        source: Path,
        task_id: str,
        subject_id: str,
    ):
        return await green_fixture.TVFontSeriesAssociationRedContracts._run_flow(
            self,
            root=root,
            source=source,
            task_id=task_id,
            subject_id=subject_id,
        )

    async def _approve_and_execute(self, *, root: Path, result):
        return await green_fixture.TVFontSeriesAssociationRedContracts._approve_and_execute(
            self,
            root=root,
            result=result,
        )

    async def _materialize_s1(
        self,
        root: Path,
        *,
        font_bytes: bytes = b"one-unified-series-font-package",
    ):
        return await green_fixture.TVFontSeriesAssociationRedContracts._materialize_s1(
            self,
            root,
            font_bytes=font_bytes,
        )

    async def test_fresh_s1_uses_series_name_and_planning_writes_no_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = r1_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume",
            )
            font = source / "[VCB-Studio] Yama no Susume [Fonts].7z"
            font.write_bytes(b"canonical-name-primary-content")

            result = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-canonical-s1",
                subject_id="41658",
            )
            canonical_root = Path(str(result.plan.metadata["canonical_work_root"]))
            expected = canonical_root / "Associated" / "向山进发 (2013) [Fonts].7z"
            entry = _font_entry(result.plan, font)
            manifest = result.plan.metadata.get("associated_asset_manifest")
            rows = manifest.get("assets") if isinstance(manifest, Mapping) else ()
            row = next(
                (
                    item
                    for item in rows or ()
                    if isinstance(item, Mapping)
                    and item.get("source_path") == str(font)
                ),
                None,
            )

            self.assertEqual("accepted", PlanVerifier().verify(result.plan).status.value)
            self.assertEqual(expected, entry.target_path)
            self.assertEqual(str(expected), row.get("target_path") if row else None)
            self.assertFalse(expected.exists(), "planning created the font target")
            self.assertEqual(
                [],
                list((root / "series_catalog").glob("*.json")),
                "fresh planning materialized SeriesCatalog before execution",
            )

            task, ledger, _service = await self._approve_and_execute(
                root=root,
                result=result,
            )
            self.assertEqual("completed", task.get("execution_status"), task.get("error"))
            self.assertTrue(os.path.samefile(font, expected))
            font_ledger = next(
                item
                for item in ledger.get("entries", ())
                if item.get("source") == str(font)
            )
            self.assertEqual("hardlink", font_ledger.get("transfer_type"))
            catalog_path = next((root / "series_catalog").glob("series-*.json"))
            payload = json.loads(catalog_path.read_text("utf-8"))
            assets = payload.get("associated_assets") or ()
            self.assertEqual([str(expected)], [_asset_target(item) for item in assets])

    async def test_s2_different_basename_same_content_reuses_canonical_s1_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s1, _s1_font, s1_target = await self._materialize_s1(root)
            canonical_root = Path(str(s1.plan.metadata["canonical_work_root"]))
            expected = canonical_root / "Associated" / "向山进发 (2013) [Fonts].7z"
            self.assertEqual(expected, s1_target)

            source = r1_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Second Season",
                ova=True,
            )
            s2_font = source / "[VCB-Studio] Yama no Susume Second Season [Fonts].7z"
            s2_font.write_bytes(b"one-unified-series-font-package")
            result = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-canonical-s2-reuse",
                subject_id="86670",
            )
            entry = _font_entry(result.plan, s2_font)
            self.assertEqual(expected, entry.target_path)
            self.assertIsNotNone(entry.collision_resolution)
            self.assertEqual("reuse_existing", entry.collision_resolution.action)

            task, ledger, _service = await self._approve_and_execute(
                root=root,
                result=result,
            )
            self.assertEqual("completed", task.get("execution_status"), task.get("error"))
            self.assertFalse(os.path.samefile(s2_font, expected))
            self.assertEqual(_sha256(s2_font), _sha256(expected))
            font_ledger = next(
                item
                for item in ledger.get("entries", ())
                if item.get("source") == str(s2_font)
            )
            self.assertEqual("reuse_existing", font_ledger.get("transfer_type"))
            self.assertEqual(
                [expected],
                list((canonical_root / "Associated").glob("*.7z")),
            )

    async def test_second_different_content_uses_full_digest_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s1, _s1_font, s1_target = await self._materialize_s1(
                root,
                font_bytes=b"canonical-primary-font-content",
            )
            canonical_root = Path(str(s1.plan.metadata["canonical_work_root"]))
            primary = canonical_root / "Associated" / "向山进发 (2013) [Fonts].7z"
            self.assertEqual(primary, s1_target)
            primary_bytes = primary.read_bytes()

            source = r1_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Second Season",
                ova=True,
            )
            second_font = source / "[VCB-Studio] Yama no Susume Second Season [Fonts].7z"
            second_font.write_bytes(b"canonical-secondary-font-content")
            digest = _sha256(second_font)
            expected = (
                canonical_root
                / "Associated"
                / f"向山进发 (2013) [{digest}] [Fonts].7z"
            )
            result = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-canonical-s2-different-content",
                subject_id="86670",
            )
            entry = _font_entry(result.plan, second_font)
            self.assertEqual(expected, entry.target_path)
            self.assertIsNone(entry.collision_resolution)
            self.assertEqual(primary_bytes, primary.read_bytes())
            self.assertFalse(expected.exists())

            task, _ledger, _service = await self._approve_and_execute(
                root=root,
                result=result,
            )
            self.assertEqual("completed", task.get("execution_status"), task.get("error"))
            self.assertTrue(os.path.samefile(second_font, expected))
            self.assertEqual(primary_bytes, primary.read_bytes())
            catalog_path = next((root / "series_catalog").glob("series-*.json"))
            payload = json.loads(catalog_path.read_text("utf-8"))
            locations = {
                _asset_target(item)
                for item in payload.get("associated_assets") or ()
                if isinstance(item, Mapping)
            }
            self.assertEqual({str(primary), str(expected)}, locations)

    async def test_same_plan_primary_assignment_is_digest_stable_across_source_order(self):
        contents = {
            "Alpha [Fonts].7z": b"same-plan-font-content-alpha",
            "Beta [Fonts].7z": b"same-plan-font-content-beta",
        }
        digests = {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in contents.items()
        }
        primary_digest = min(digests.values())
        mappings = []
        digest_orders = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, reverse_content in enumerate((False, True)):
                isolated = root / f"order-{index}"
                source = r1_fixture._make_season(
                    isolated / "sources",
                    "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
                    "[VCB-Studio] Yama no Susume",
                )
                payloads = list(contents.values())
                if reverse_content:
                    payloads.reverse()
                sources = []
                for name, payload in zip(contents, payloads):
                    archive = source / name
                    archive.write_bytes(payload)
                    sources.append(archive)
                result = await self._run_flow(
                    root=isolated,
                    source=source,
                    task_id=f"tv-font-canonical-multi-{index}",
                    subject_id="41658",
                )
                verification = PlanVerifier().verify(result.plan)
                self.assertEqual("accepted", verification.status.value)
                font_entries = [
                    item
                    for item in result.plan.entries
                    if item.source_path in sources
                ]
                digest_order = [_sha256(item.source_path) for item in font_entries]
                digest_orders.append(digest_order)
                mapping = {
                    _sha256(item.source_path): item.target_path.name
                    for item in font_entries
                }
                mappings.append(mapping)
                expected = {
                    digest: (
                        "向山进发 (2013) [Fonts].7z"
                        if digest == primary_digest
                        else f"向山进发 (2013) [{digest}] [Fonts].7z"
                    )
                    for digest in digests.values()
                }
                self.assertEqual(expected, mapping)
                task, ledger, _service = await self._approve_and_execute(
                    root=isolated,
                    result=result,
                )
                self.assertEqual(
                    "completed",
                    task.get("execution_status"),
                    task.get("error"),
                )
                font_ledger = [
                    item
                    for item in ledger.get("entries", ())
                    if item.get("source") in {str(path) for path in sources}
                ]
                self.assertEqual(2, len(font_ledger))
                self.assertEqual(
                    {"hardlink"},
                    {item.get("transfer_type") for item in font_ledger},
                )
                catalog_path = next(
                    (isolated / "series_catalog").glob("series-*.json")
                )
                catalog = json.loads(catalog_path.read_text("utf-8"))
                self.assertEqual(2, len(catalog.get("associated_assets") or ()))
            self.assertEqual(mappings[0], mappings[1])
            self.assertEqual(digest_orders[0], list(reversed(digest_orders[1])))

    async def test_same_plan_duplicate_content_is_not_approvable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = r1_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume",
            )
            archives = (
                source / "Alpha [Fonts].7z",
                source / "Beta [Fonts].7z",
            )
            for archive in archives:
                archive.write_bytes(b"same-plan-duplicate-font-content")
            result = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-canonical-duplicate-content",
                subject_id="41658",
            )
            verification = PlanVerifier().verify(result.plan)
            self.assertNotEqual("accepted", verification.status.value)
            targets = {
                _font_entry(result.plan, archive).target_path
                for archive in archives
            }
            self.assertEqual(1, len(targets))
            self.assertFalse(next(iter(targets)).exists())
            self.assertEqual([], list((root / "series_catalog").glob("*.json")))

    async def test_unregistered_canonical_primary_never_gets_adopted_or_overwritten(self):
        for label, existing_bytes in (
            ("same", b"canonical-name-primary-content"),
            ("different", b"foreign-unregistered-font-content"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = r1_fixture._make_season(
                    root / "sources",
                    "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
                    "[VCB-Studio] Yama no Susume",
                )
                font = source / "[VCB-Studio] Yama no Susume [Fonts].7z"
                font.write_bytes(b"canonical-name-primary-content")
                target = (
                    root
                    / "Anime"
                    / "向山进发 (2013)"
                    / "Associated"
                    / "向山进发 (2013) [Fonts].7z"
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(existing_bytes)
                before = target.read_bytes()
                result = await self._run_flow(
                    root=root,
                    source=source,
                    task_id=f"tv-font-canonical-unregistered-{label}",
                    subject_id="41658",
                )
                verification = PlanVerifier().verify(result.plan)
                self.assertNotEqual("accepted", verification.status.value)
                unresolved = result.plan.metadata.get("asset_collision_unresolved") or ()
                self.assertIn(
                    "font_unregistered_target_conflict",
                    {item.get("code") for item in unresolved},
                )
                self.assertEqual(before, target.read_bytes())
                self.assertEqual([], list((root / "series_catalog").glob("*.json")))

    async def test_damaged_registered_primary_blocks_new_different_content(self):
        for label in ("missing", "replaced"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                s1, _s1_font, primary = await self._materialize_s1(
                    root,
                    font_bytes=b"registered-primary-font-content",
                )
                catalog_path = next((root / "series_catalog").glob("series-*.json"))
                catalog_before = catalog_path.read_bytes()
                primary.unlink()
                if label == "replaced":
                    primary.write_bytes(b"externally-replaced-font-content")

                source = r1_fixture._make_season(
                    root / "sources",
                    "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                    "[VCB-Studio] Yama no Susume Second Season",
                    ova=True,
                )
                second_font = (
                    source
                    / "[VCB-Studio] Yama no Susume Second Season [Fonts].7z"
                )
                second_font.write_bytes(b"new-different-font-content")
                digest = _sha256(second_font)
                new_target = (
                    Path(str(s1.plan.metadata["canonical_work_root"]))
                    / "Associated"
                    / f"向山进发 (2013) [{digest}] [Fonts].7z"
                )
                result = await self._run_flow(
                    root=root,
                    source=source,
                    task_id=f"tv-font-canonical-damaged-primary-{label}",
                    subject_id="86670",
                )
                verification = PlanVerifier().verify(result.plan)
                self.assertNotEqual("accepted", verification.status.value)
                unresolved = result.plan.metadata.get("asset_collision_unresolved") or ()
                self.assertIn(
                    "font_catalog_evidence_conflict",
                    {item.get("code") for item in unresolved},
                )
                self.assertEqual(catalog_before, catalog_path.read_bytes())
                self.assertFalse(new_target.exists())
                self.assertFalse(
                    (root / "records" / f"{result.plan.task_id}.ledger.json").exists()
                )

    async def test_existing_legacy_location_is_reused_without_automatic_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s1, _s1_font, current_target = await self._materialize_s1(root)
            canonical_root = Path(str(s1.plan.metadata["canonical_work_root"]))
            legacy_target = (
                canonical_root
                / "Associated"
                / "[VCB-Studio] Yama no Susume [Fonts].7z"
            )
            legacy_target.parent.mkdir(parents=True, exist_ok=True)
            current_target.replace(legacy_target)

            catalog_path = next((root / "series_catalog").glob("series-*.json"))
            payload = json.loads(catalog_path.read_text("utf-8"))
            assets = payload.get("associated_assets") or ()
            self.assertEqual(1, len(assets))
            asset = assets[0]
            location = asset.get("location")
            if isinstance(location, dict):
                location["target_path"] = str(legacy_target)
            else:
                asset["target_path"] = str(legacy_target)
            catalog_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            catalog_before = catalog_path.read_bytes()

            source = r1_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume Second Season [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Second Season",
                ova=True,
            )
            s2_font = source / "[VCB-Studio] Yama no Susume Second Season [Fonts].7z"
            s2_font.write_bytes(b"one-unified-series-font-package")
            result = await self._run_flow(
                root=root,
                source=source,
                task_id="tv-font-canonical-legacy-control",
                subject_id="86670",
            )
            entry = _font_entry(result.plan, s2_font)
            self.assertEqual(legacy_target, entry.target_path)
            self.assertEqual("reuse_existing", entry.collision_resolution.action)
            self.assertEqual(catalog_before, catalog_path.read_bytes())
            self.assertFalse(
                (canonical_root / "Associated" / "向山进发 (2013) [Fonts].7z").exists()
            )


if __name__ == "__main__":
    unittest.main()
