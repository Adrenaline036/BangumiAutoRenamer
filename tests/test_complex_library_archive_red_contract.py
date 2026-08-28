"""R21 regression contracts for complex-library archives and assets.

The cases exercise production queue, Provider and review projections.  All
filesystem roots are temporary; no real media, Catalog or runtime is used.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import src.pages.plan_review_page as review_page
import tests.test_complex_library_special_red_contract as special_fixture
import tests.test_movie_font_work_catalog_red_contract as movie_fixture
import tests.test_r1_real_media_production_red_contract as production_fixture
from src.pages.plan_review_page import PlanReviewDialog, build_plan_review_snapshot
from src.rename.decision_state import DecisionCommandService, DecisionStateStore
from src.rename.font_archive_contract import canonical_font_archive_target
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.utils import is_font_archive, is_ignored_asset_directory


_BASE_CONFIGURED_RENAMER = production_fixture._configured_renamer


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class _Node:
    def __init__(self, *, value=None, ui=None, button_label=None):
        self.value = value
        self._ui = ui
        self._button_label = button_label

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def classes(self, value=""):
        if self._ui is not None:
            self._ui.classes.append(str(value))
        return self

    def props(self, value=""):
        if self._ui is not None:
            self._ui.props.append(str(value))
            if self._button_label is not None:
                self._ui.button_props[self._button_label] = str(value)
        return self

    def tooltip(self, value=""):
        if self._ui is not None:
            self._ui.visible.append(str(value))
        return self

    def clear(self):
        return None

    def update(self):
        return None

    def add_slot(self, *_args, **_kwargs):
        return self

    def set_visibility(self, *_args, **_kwargs):
        return None

    def on(self, *_args, **_kwargs):
        return self


class _RoleUi:
    def __init__(self, selected):
        self.selected = selected
        self.callbacks = {}
        self.visible = []
        self.props = []
        self.classes = []
        self.button_props = {}

    def label(self, value):
        self.visible.append(str(value))
        return _Node(ui=self)

    def row(self):
        return _Node(ui=self)

    def column(self):
        return _Node(ui=self)

    def expansion(self, *_args, **_kwargs):
        return _Node(ui=self)

    def icon(self, *_args, **_kwargs):
        return _Node(ui=self)

    def separator(self):
        return _Node(ui=self)

    def input(self, *_args, **kwargs):
        return _Node(value=kwargs.get("value"), ui=self)

    def number(self, *_args, **kwargs):
        return _Node(value=kwargs.get("value"), ui=self)

    def checkbox(self, *_args, **kwargs):
        return _Node(value=kwargs.get("value", False), ui=self)

    def select(self, _options, **_kwargs):
        return _Node(value=self.selected, ui=self)

    def button(self, label, *, on_click=None, **_kwargs):
        self.visible.append(str(label))
        if on_click is not None:
            self.callbacks[str(label)] = on_click
        return _Node(ui=self, button_label=str(label))


class ComplexLibraryArchiveRedContracts(unittest.IsolatedAsyncioTestCase):
    def test_role_aware_zip_and_suffix_style_7z_keep_bytes_and_extension(self):
        """Only an explicit font role may promote non-strict .7z/.zip archives."""

        digest = "a" * 64
        problems = []
        for suffix in (".zip", ".7z"):
            try:
                target = canonical_font_archive_target(
                    Path("C:/Library/Example (2026)"),
                    digest,
                    disambiguate=False,
                    archive_suffix=suffix,
                )
            except Exception as exc:
                problems.append(f"canonical {suffix} target was rejected: {exc}")
            else:
                if target.name != f"Example (2026) [Fonts]{suffix}":
                    problems.append(f"canonical {suffix} target drifted: {target.name}")
        try:
            canonical_font_archive_target(
                Path("C:/Library/Example (2026)"),
                digest,
                disambiguate=False,
                archive_suffix=".rar",
            )
        except Exception:
            pass
        else:
            problems.append("explicit font role incorrectly opened unsupported .rar")
        self.assertEqual([], problems)

    async def test_real_font_role_button_updates_decision_and_persisted_plan(self):
        """The visible action must call the real Decision command and rebuild Plan."""

        problems = []
        cases = (
            ("Example [Fonts] - Mysilu.zip", True),
            ("Example [Fonts] - Mysilu.7z", True),
            ("Example [Fonts] - Mysilu.rar", False),
        )
        for index, (archive_name, should_allow) in enumerate(cases):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, primary, planned_sources = movie_fixture._make_omoide(
                    root / "sources"
                )
                strict_archive = next(path for path in planned_sources if path.suffix == ".7z")
                archive = strict_archive.with_name(archive_name)
                strict_archive.rename(archive)
                task_id = f"font-role-real-callback-{index}"
                case = movie_fixture.MovieFontProductionRedContracts(methodName="runTest")
                initial = await case._run_provider_flow(root / "flow", source, task_id)
                completed = case._complete_movie(
                    root=root / "flow", result=initial, primary=primary
                )
                store = completed.store
                states = DecisionStateStore(root / "flow" / "decisions")
                state = states.load(task_id)
                projected = store.load(task_id)
                archive_entry = next(
                    (entry for entry in projected.entries if entry.source_path == archive), None
                )
                before_source = {primary: primary.read_bytes(), archive: archive.read_bytes()}
                plan_before = (root / "flow" / "plans" / f"{task_id}.json").read_bytes()
                state_before = (root / "flow" / "decisions" / f"{task_id}.json").read_bytes()
                callback = None
                ui = _RoleUi(entry_id(archive_entry) if archive_entry else None)
                if archive_entry is not None:
                    identifier = entry_id(archive_entry)
                    snapshot = build_plan_review_snapshot(projected)
                    row = next(
                        (item for item in snapshot.rows if item["entry_id"] == identifier), None
                    )
                    if should_allow and (
                        row is None
                        or "presentation_asset_role_unknown" not in row.get("issue_codes", ())
                    ):
                        problems.append(f"{archive.suffix}: real Verifier did not expose a role issue")
                    dialog = object.__new__(PlanReviewDialog)
                    dialog.plan = projected
                    dialog.task_decision = state
                    dialog.review_service = completed.review
                    dialog.decision_command_service = DecisionCommandService(states, store)
                    dialog.snapshot = snapshot
                    dialog.issues_container = _Node(ui=ui)
                    dialog._adopt_revision = MagicMock()
                    dialog._rebuild_inventory_role = (
                        PlanReviewDialog._rebuild_inventory_role.__get__(dialog)
                    )
                    with (
                        patch.object(review_page, "ui", ui),
                        patch.object(review_page, "notify"),
                    ):
                        PlanReviewDialog._render_issues(dialog)
                        callback = ui.callbacks.get("设为字体包")
                        if callback is not None:
                            callback()
                persisted = store.load(task_id)
                persisted_state = states.load(task_id)
                source_unchanged = all(
                    path.read_bytes() == payload for path, payload in before_source.items()
                )
                if should_allow:
                    if callback is None:
                        problems.append(f"{archive.suffix}: 问题卡没有可达的“设为字体包”动作")
                    if "保留原文件，不会解包" not in ui.visible:
                        problems.append(f"{archive.suffix}: 字体动作没有说明不会解包")
                    if ui.button_props.get("设为字体包") != "outline color=blue-7":
                        problems.append(f"{archive.suffix}: 字体动作未复用蓝色outline样式")
                    if callback is not None and persisted_state.revision <= state.revision:
                        problems.append(f"{archive.suffix}: DecisionState revision没有推进")
                    if archive_entry is not None:
                        identifier = entry_id(archive_entry)
                        if persisted_state.manual_role_decisions.get(identifier) != "font":
                            problems.append(f"{archive.suffix}: 未保存exact font role")
                    persisted_archive = next(
                        (entry for entry in persisted.entries if entry.source_path == archive), None
                    )
                    if persisted_archive is None or persisted_archive.role != "font":
                        problems.append(f"{archive.suffix}: 磁盘Plan未保留字体条目")
                    elif persisted_archive.target_path.suffix.casefold() != archive.suffix.casefold():
                        problems.append(f"{archive.suffix}: 磁盘Plan丢失真实扩展名")
                    manifest = persisted.metadata.get("associated_asset_manifest")
                    if not isinstance(manifest, Mapping) or len(manifest.get("assets", ())) != 1:
                        problems.append(f"{archive.suffix}: 磁盘Plan未冻结字体清单")
                else:
                    if callback is not None:
                        problems.append(".rar错误暴露了字体确认动作")
                    if (root / "flow" / "plans" / f"{task_id}.json").read_bytes() != plan_before:
                        problems.append(".rar控制意外改写Plan")
                    if (root / "flow" / "decisions" / f"{task_id}.json").read_bytes() != state_before:
                        problems.append(".rar控制意外改写DecisionState")
                if not source_unchanged:
                    problems.append(f"{archive.suffix}: Plan-only确认修改了源字节")
        self.assertEqual([], problems)

    async def test_tv_font_role_button_builds_series_scope_manifest(self):
        """TV .zip/.7z use the real review exit; .rar remains unavailable."""

        problems = []
        cases = (
            ("Yama no Susume [Fonts] - Mysilu.zip", True),
            ("Yama no Susume [Fonts] - Mysilu.7z", True),
            ("Yama no Susume [Fonts] - Mysilu.rar", False),
        )
        for index, (archive_name, should_allow) in enumerate(cases):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = production_fixture._make_season(
                    root / "sources",
                    "[VCB-Studio] Yama no Susume [Ma10p_1080p]",
                    "[VCB-Studio] Yama no Susume",
                )
                archive = _write(source / archive_name, f"font-{index}".encode())
                source_before = archive.read_bytes()
                task_id = f"tv-font-role-r1-{index}"
                case = special_fixture.ComplexLibrarySpecialRedContracts(
                    methodName="runTest"
                )
                stack = ExitStack()
                self.addCleanup(stack.close)
                stack.enter_context(patch(
                    "src.rename.process.SERIES_CATALOG_PATH",
                    root / "flow" / "series_catalog",
                ))
                stack.enter_context(patch(
                    "src.rename.process.RECORD_PATH",
                    root / "flow" / "records",
                ))
                result = await case._run(
                    root / "flow",
                    source,
                    task_id,
                    "41658",
                    candidate=production_fixture._candidate("41658"),
                    renamer_factory=production_fixture._configured_renamer,
                )
                before = result.plan_store.load(task_id)
                plan_path = root / "flow" / "plans" / f"{task_id}.json"
                decision_path = root / "flow" / "decisions" / f"{task_id}.json"
                plan_bytes_before = plan_path.read_bytes()
                decision_bytes_before = decision_path.read_bytes()
                matching = [
                    entry for entry in before.entries if entry.source_path == archive
                ]
                callback = None
                ui = None
                selected_entry = None
                if len(matching) == 1:
                    selected_entry = matching[0]
                    snapshot = build_plan_review_snapshot(before)
                    row = next(
                        item for item in snapshot.rows
                        if item["entry_id"] == entry_id(selected_entry)
                    )
                    if should_allow and "presentation_asset_role_unknown" not in row.get(
                        "issue_codes", ()
                    ):
                        problems.append(
                            f"{archive.suffix}: TV Verifier没有产生字体角色问题"
                        )
                    dialog, ui, _selected = case._render_issue_actions(
                        result, selected_source=archive
                    )
                    callback = ui.callbacks.get("设为字体包")
                    if callback is not None:
                        with patch.object(review_page, "notify"):
                            callback()
                elif should_allow:
                    problems.append(
                        f"{archive.suffix}: production TV Plan未保留待确认archive"
                    )
                persisted = result.plan_store.load(task_id)
                state = result.decision_store.load(task_id)
                verification = PlanVerifier().verify(persisted)
                issue_codes = {issue.code for issue in verification.issues}
                persisted_entry = next(
                    (entry for entry in persisted.entries if entry.source_path == archive),
                    None,
                )
                if should_allow and callback is None:
                    problems.append(f"{archive.suffix}: TV问题卡没有“设为字体包”")
                if should_allow and ui is not None and (
                    ui.button_props.get("设为字体包") != "outline color=blue-7"
                ):
                    problems.append(f"{archive.suffix}: TV字体按钮样式未复用")
                if should_allow and selected_entry is not None and state.manual_role_decisions.get(
                    entry_id(selected_entry)
                ) != "font":
                    problems.append(f"{archive.suffix}: TV Decision未保存font角色")
                if should_allow and (persisted_entry is None or persisted_entry.role != "font"):
                    problems.append(f"{archive.suffix}: TV磁盘Plan未投影字体条目")
                elif should_allow and persisted_entry.target_path.suffix.casefold() != archive.suffix:
                    problems.append(f"{archive.suffix}: TV字体目标丢失真实扩展")
                manifest = persisted.metadata.get("associated_asset_manifest")
                assets = (
                    tuple(manifest.get("assets") or ())
                    if isinstance(manifest, Mapping) else ()
                )
                if should_allow and (
                    len(assets) != 1 or not isinstance(assets[0], Mapping)
                ):
                    problems.append(f"{archive.suffix}: TV Plan无唯一字体manifest")
                elif should_allow:
                    asset = assets[0]
                    if (
                        asset.get("scope") != "series"
                        or asset.get("role") != "fonts"
                        or asset.get("season_number") != 1
                        or Path(str(asset.get("source_path"))) != archive
                    ):
                        problems.append(
                            f"{archive.suffix}: TV字体manifest不是series/S01 authority"
                        )
                if should_allow and "presentation_asset_role_unknown" in issue_codes:
                    problems.append(f"{archive.suffix}: 字体决定落盘后仍被Verifier阻断")
                if should_allow and verification.status.value == "invalid":
                    problems.append(f"{archive.suffix}: 字体决定落盘后Plan仍不可继续审核")
                if not should_allow:
                    if callback is not None:
                        problems.append(".rar错误暴露了TV字体确认动作")
                    if plan_path.read_bytes() != plan_bytes_before:
                        problems.append(".rar控制意外改写TV Plan")
                    if decision_path.read_bytes() != decision_bytes_before:
                        problems.append(".rar控制意外改写TV DecisionState")
                if archive.read_bytes() != source_before:
                    problems.append(f"{archive.suffix}: Plan-only TV字体确认改写源字节")
                business_json = tuple(
                    path for folder in ("series_catalog", "records")
                    for path in (root / "flow" / folder).glob("*.json")
                    if path.is_file()
                )
                if business_json:
                    problems.append(f"{archive.suffix}: Plan-only确认意外写入Catalog/record")
                stack.close()
        self.assertEqual([], problems)

    async def test_real_movie_flow_ignores_exact_auxiliary_aliases_but_not_bonus(self):
        """Bonus CDs/BD Scans are exact ignores; generic Bonus remains reviewable."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, primary, _planned = movie_fixture._make_omoide(root / "sources")
            ignored_cd = _write(source / "Bonus CDs" / "disc.flac", b"bonus-cd")
            ignored_scan = _write(source / "BD Scans" / "scan-audio.flac", b"bd-scan")
            retained = _write(source / "Bonus" / "Interview.mkv", b"bonus-video")
            source_before = {
                path: path.read_bytes() for path in (ignored_cd, ignored_scan, retained)
            }
            case = movie_fixture.MovieFontProductionRedContracts(methodName="runTest")
            initial = await case._run_provider_flow(
                root / "flow", source, "auxiliary-alias-flow"
            )
            completed = case._complete_movie(
                root=root / "flow", result=initial, primary=primary
            )
            plan = completed.store.load("auxiliary-alias-flow")
            planned_sources = set(plan.expected_sources)
            source_unchanged = all(
                path.read_bytes() == payload
                for path, payload in source_before.items()
            )

        problems = []
        if not is_ignored_asset_directory(Path("Bonus CDs")):
            problems.append("Bonus CDs alias is not recognized")
        if not is_ignored_asset_directory(Path("BD Scans")):
            problems.append("BD Scans alias is not recognized")
        if is_ignored_asset_directory(Path("Bonus")):
            problems.append("generic Bonus directory was over-broadly ignored")
        if ignored_cd in planned_sources or ignored_scan in planned_sources:
            problems.append("real inventory/Plan retained an exact ignored auxiliary file")
        if retained not in planned_sources:
            problems.append("real inventory/Plan lost the generic Bonus video")
        if not source_unchanged:
            problems.append("Plan-only alias classification modified source bytes")
        self.assertEqual([], problems)

    async def test_violet_extras_use_stable_version_and_role_names_in_any_input_order(self):
        """Violet 1080p/DoVi extras must not inherit release basenames or order."""

        primary_files = (
            "Gekijouban Violet Evergarden [Ma10p_1080p][x265_flac].mkv",
            "Gekijouban Violet Evergarden [Ma10p_2160p_DoVi_P8.1][x265_flac].mkv",
            "Gekijouban Violet Evergarden [Ma10p_2160p_SDR][x265_flac].mkv",
        )
        roles_1080 = (
            "IV", "Menu01", "Menu02", "Menu03", "Menu04",
            "PV01", "PV02", "PV03", "SP", "Trailer01", "Trailer02",
        )
        roles_dovi = ("Menu01", "Menu02", "Menu03")
        extra_files = tuple(
            f"SPs/Gekijouban Violet Evergarden [Ma10p_1080p][{role}].mkv"
            for role in roles_1080
        ) + tuple(
            f"SPs/Gekijouban Violet Evergarden [Ma10p_2160p_DoVi_P8.1][{role}].mkv"
            for role in roles_dovi
        )
        files = primary_files + extra_files
        mappings = []
        shape_problems = []
        for index, order in enumerate((files, tuple(reversed(files)))):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "sources" / "Violet release"
                for offset, name in enumerate(order):
                    _write(source / name, f"violet-{offset}".encode())
                primaries = tuple(source / name for name in primary_files)

                class _VioletSearch(production_fixture._FrozenSearch):
                    def get_movie_info(self, query, _year=None):
                        if "violet" in str(query).casefold() or "紫罗兰" in str(query):
                            return "紫罗兰永恒花园 剧场版", {
                                "id": 533514,
                                "title": "紫罗兰永恒花园 剧场版",
                                "original_title": "劇場版 ヴァイオレット・エヴァーガーデン",
                                "release_date": "2020-09-18",
                                "genres": [{"name": "Animation"}],
                            }
                        return "", None

                    def get_movie_info_by_id(self, provider_id):
                        if int(provider_id) == 533514:
                            return self.get_movie_info("Violet")
                        return "", None

                def configured(actual_root):
                    renamer = _BASE_CONFIGURED_RENAMER(actual_root)
                    renamer.search = _VioletSearch()
                    return renamer

                candidate = {
                    "namespace": "tmdb",
                    "entity_type": "movie",
                    "provider_id": "533514",
                    "title": "紫罗兰永恒花园 剧场版",
                    "original_title": "劇場版 ヴァイオレット・エヴァーガーデン",
                    "year": 2020,
                    "poster_url": production_fixture.POSTER_URL,
                    "source_url": "https://www.themoviedb.org/movie/533514",
                    "snapshot_at": "2026-08-24T00:00:00+00:00",
                }
                harness = SimpleNamespace(
                    _NoopReviewDialog=(
                        production_fixture.R1RealQueueWorkerContracts._NoopReviewDialog
                    )
                )
                with (
                    patch.object(
                        production_fixture,
                        "_configured_renamer",
                        side_effect=configured,
                    ),
                    patch("src.rename.process.SERIES_CATALOG_PATH", root / "flow" / "series_catalog"),
                    patch("src.rename.process.RECORD_PATH", root / "flow" / "records"),
                    patch("src.rename.process.is_dry_run_enabled", return_value=False),
                ):
                    initial = await production_fixture.R1RealQueueWorkerContracts._run_flow(
                        harness,
                        root=root / "flow",
                        source=source,
                        task_id=f"violet-order-{index}",
                        candidate=candidate,
                        is_anime=True,
                        is_movie=True,
                    )
                case = movie_fixture.MovieFontProductionRedContracts(methodName="runTest")
                completed = case._complete_movie(
                    root=root / "flow", result=initial, primary=primaries
                )
                plan = completed.store.load(f"violet-order-{index}")
                extra_sources = {source / name for name in extra_files}
                primary_sources = {source / name for name in primary_files}
                primary_entries = [
                    entry for entry in plan.entries
                    if entry.source_path in primary_sources
                ]
                extra_entries = [
                    entry for entry in plan.entries
                    if entry.presentation_asset is not None
                    and entry.presentation_asset.asset_role.kind == "extra"
                ]
                for primary_source in primary_sources:
                    matching = [
                        entry for entry in primary_entries
                        if entry.source_path == primary_source
                    ]
                    if len(matching) != 1 or (
                        matching[0].presentation_asset is None
                        or matching[0].presentation_asset.asset_role.kind != "content_version"
                    ):
                        shape_problems.append(
                            f"{index}: primary is not one content_version: {primary_source.name}"
                        )
                if len(extra_entries) != len(extra_sources):
                    shape_problems.append(
                        f"{index}: expected {len(extra_sources)} extra entries, got {len(extra_entries)}"
                    )
                if {entry.source_path for entry in extra_entries} != extra_sources:
                    shape_problems.append(f"{index}: extra source set is not exact")
                if len({entry.source_path for entry in extra_entries}) != len(extra_entries):
                    shape_problems.append(f"{index}: duplicate extra source entry")
                if len({entry.target_path for entry in extra_entries}) != len(extra_entries):
                    shape_problems.append(f"{index}: duplicate extra target")
                if any(entry.source_path in primary_sources for entry in extra_entries):
                    shape_problems.append(f"{index}: primary leaked into extra role")
                if any(
                    "sdr" in " ".join((
                        str(entry.source_path),
                        str(entry.target_path),
                        str(entry.presentation_asset.version_descriptor)
                        if entry.presentation_asset is not None else "",
                    )).casefold()
                    for entry in extra_entries
                ):
                    shape_problems.append(f"{index}: SDR was assigned an extra")
                mappings.append(tuple(sorted(
                    (entry.source_path.name, entry.target_path.name)
                    for entry in extra_entries
                )))
        expected = {
            f"Gekijouban Violet Evergarden [Ma10p_1080p][{role}].mkv":
            f"紫罗兰永恒花园 剧场版 (2020) - 1080p - {role}.mkv"
            for role in roles_1080
        }
        expected.update({
            f"Gekijouban Violet Evergarden [Ma10p_2160p_DoVi_P8.1][{role}].mkv":
            f"紫罗兰永恒花园 剧场版 (2020) - 2160p DoVi P8.1 - {role}.mkv"
            for role in roles_dovi
        })
        expected_pairs = tuple(sorted(expected.items()))
        self.assertEqual([], shape_problems)
        self.assertEqual(mappings[0], mappings[1])
        self.assertEqual(expected_pairs, mappings[0])

    def test_unconfirmed_archives_and_rar_remain_closed_controls(self):
        self.assertTrue(is_font_archive(Path("Example [Fonts].7z")))
        self.assertFalse(is_font_archive(Path("Example [Fonts].zip")))
        self.assertFalse(is_font_archive(Path("Example [Fonts] - Release.7z")))
        self.assertFalse(is_font_archive(Path("random.zip")))
        self.assertFalse(is_font_archive(Path("random.rar")))
        self.assertFalse(is_ignored_asset_directory(Path("Bonus")))


if __name__ == "__main__":
    unittest.main()
