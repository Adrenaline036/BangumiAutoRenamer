"""R23 A/E contracts for GUI, config transactions, and path safety."""

from __future__ import annotations
from tests.source_collection_fixture import collected_files

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from nicegui import ui

from src.component.local_file_picker import local_file_picker
from src.config import config_manager as config_manager_module
from src.config.config_manager import ConfigManager
from src.pages import add_task_page, config_page
from src.pages.add_task_page import AddTaskDialog
from src.pages.config_page import ConfigPage
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from src.rename.rename_plan import ExecutionPolicy, RenamePlan
from src.task_queue import TaskQueueManager


# NiceGUI's implicit auto-index slot belongs to the importing task.  The RED
# tests use isolated event loops, so every real component is mounted beneath
# one explicit host rather than relying on that ambient task-local slot.
MOUNT_HOST = ui.column()


def _visible_values(element) -> tuple[str, ...]:
    values: list[str] = []
    for child in element.descendants():
        for name in ("text", "value", "options"):
            value = getattr(child, name, None)
            if value not in (None, "", (), [], {}):
                values.append(str(value))
    return tuple(values)


def _make_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0 or not link.exists():
        raise AssertionError(
            "Windows junction fixture could not be created: "
            + (result.stderr or result.stdout).strip()
        )


class MountedGuiAccumulatorIncidentRedContracts(
    unittest.IsolatedAsyncioTestCase
):
    async def test_a_mounted_add_task_accumulates_two_picker_visits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "Steins Gate"
            second = root / "Kaguya-sama"
            first.mkdir()
            second.mkdir()
            with MOUNT_HOST:
                dialog = AddTaskDialog()
            try:
                with patch.object(
                    add_task_page,
                    "local_file_picker",
                    new=AsyncMock(side_effect=[[str(first)], [str(second)]]),
                ):
                    await dialog._browse()
                    await dialog._browse()
                self.assertEqual(
                    (first.resolve(), second.resolve()),
                    tuple(path.resolve() for path in dialog._submission_paths()),
                )
            finally:
                dialog.close()

    async def test_a_mounted_add_task_shows_every_selected_path_and_remove_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "Yama no Susume S1"
            second = root / "Yama no Susume S2"
            first.mkdir()
            second.mkdir()
            with MOUNT_HOST:
                dialog = AddTaskDialog()
            try:
                with patch.object(
                    add_task_page,
                    "local_file_picker",
                    new=AsyncMock(return_value=[str(first), str(second)]),
                ):
                    await dialog._browse()
                visible = "\n".join(_visible_values(dialog))
                self.assertIn(str(first), visible)
                self.assertIn(str(second), visible)
                self.assertRegex(visible, r"移除|删除所选")
            finally:
                dialog.close()

    async def test_a_mounted_add_task_deduplicates_exact_picker_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            with MOUNT_HOST:
                dialog = AddTaskDialog()
            try:
                with patch.object(
                    add_task_page,
                    "local_file_picker",
                    new=AsyncMock(
                        return_value=[str(first), str(first), str(second)]
                    ),
                ):
                    await dialog._browse()
                self.assertEqual(
                    (first.resolve(), second.resolve()),
                    tuple(path.resolve() for path in dialog._submission_paths()),
                )
            finally:
                dialog.close()

    async def test_a_picker_exception_stays_in_mounted_dialog_with_visible_error(self):
        with MOUNT_HOST:
            dialog = AddTaskDialog()
        notices: list[str] = []
        escaped: Exception | None = None
        try:
            with (
                patch.object(
                    add_task_page,
                    "local_file_picker",
                    new=AsyncMock(side_effect=OSError("picker disconnected")),
                ),
                patch.object(
                    add_task_page,
                    "notify",
                    side_effect=lambda message, **_kwargs: notices.append(
                        str(message)
                    ),
                ),
            ):
                try:
                    await dialog._browse()
                except Exception as exc:  # frozen RED: must become UI state
                    escaped = exc
            self.assertIsNone(escaped)
            self.assertEqual((), dialog._submission_paths())
            self.assertTrue(any("选择" in row or "来源" in row for row in notices))
        finally:
            dialog.close()

    async def test_a_mounted_picker_exposes_available_windows_roots(self):
        with MOUNT_HOST:
            picker = local_file_picker(str(Path.home()), multiple=True)
        try:
            controls = []
            for child in picker.descendants():
                if type(child).__name__ in {"Button", "Select", "Toggle"}:
                    controls.extend(
                        str(getattr(child, name, "") or "")
                        for name in ("text", "value", "options")
                    )
            visible_controls = "\n".join(controls).casefold()
            available = [
                Path(f"{letter}:\\")
                for letter in ("C", "D")
                if Path(f"{letter}:\\").is_dir()
            ]
            self.assertTrue(available, "fixture has no Windows drive root")
            for drive in available:
                self.assertIn(str(drive).casefold(), visible_controls)
        finally:
            picker.close()

    async def test_control_single_picker_result_is_preserved_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "single"
            source.mkdir()
            with MOUNT_HOST:
                dialog = AddTaskDialog()
            try:
                with patch.object(
                    add_task_page,
                    "local_file_picker",
                    new=AsyncMock(return_value=[str(source)]),
                ):
                    await dialog._browse()
                self.assertEqual((source.resolve(),), dialog._submission_paths())
            finally:
                dialog.close()


class ConfigTransactionIncidentRedContracts(unittest.TestCase):
    def test_e_two_manager_reverse_updates_do_not_lose_unrelated_field(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            first = ConfigManager(path)
            second = ConfigManager(path)
            self.assertTrue(first.set_config("mode", "复制"))
            self.assertTrue(second.set_config("anime_path", r"D:\Anime\TV"))
            current = ConfigManager(path)
            self.assertEqual("复制", current.get_config("mode"))
            self.assertEqual(r"D:\Anime\TV", current.get_config("anime_path"))

    def test_e_loading_current_config_is_physically_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            ConfigManager(path)
            with patch.object(ConfigManager, "write_config") as write_config:
                loaded = ConfigManager(path)
            self.assertEqual("链接", loaded.get_config("mode"))
            write_config.assert_not_called()

    def test_e_mounted_config_page_rejects_relative_target_as_one_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            manager = ConfigManager(path)
            before = path.read_bytes()
            notices: list[str] = []
            with (patch.object(config_page, "cm", manager),
patch.object(
                    config_page.ui,
                    "notify",
                    side_effect=lambda message, **_kwargs: notices.append(
                        str(message)
                    ),
                ),
patch.object(config_page, "update_log_level_from_config")):
                with MOUNT_HOST:
                    dialog = ConfigPage()
                try:
                    dialog.config.anime_path = r"relative\Anime"
                    dialog._handle_ok()
                finally:
                    dialog.close()
            self.assertEqual(before, path.read_bytes())
            self.assertTrue(any("路径" in message for message in notices))

    def test_e_each_config_transaction_uses_a_unique_temp_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            first = ConfigManager(path)
            second = ConfigManager(path)
            temporary_paths: list[Path] = []

            def observe_replace(source, _destination) -> None:
                temporary_paths.append(Path(source))

            with patch.object(
                config_manager_module.os,
                "replace",
                side_effect=observe_replace,
            ):
                first.write_config()
                second.write_config()
            self.assertEqual(2, len(temporary_paths))
            self.assertEqual(2, len(set(temporary_paths)))

    def test_e_two_writers_contend_without_error_or_lost_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            first = ConfigManager(path)
            second = ConfigManager(path)
            rendezvous = threading.Barrier(2)
            real_replace = os.replace
            errors: list[Exception] = []

            def synchronized_replace(source, destination) -> None:
                try:
                    rendezvous.wait(timeout=0.5)
                except threading.BrokenBarrierError:
                    pass
                real_replace(source, destination)

            def update(manager: ConfigManager, key: str, value: str) -> None:
                try:
                    manager.set_config(key, value)
                except Exception as exc:  # frozen RED: no writer may escape
                    errors.append(exc)

            with patch.object(
                config_manager_module.os,
                "replace",
                side_effect=synchronized_replace,
            ):
                threads = (
                    threading.Thread(
                        target=update,
                        args=(first, "mode", "复制"),
                        daemon=True,
                    ),
                    threading.Thread(
                        target=update,
                        args=(second, "anime_path", r"D:\Anime\TV"),
                        daemon=True,
                    ),
                )
                for worker in threads:
                    worker.start()
                for worker in threads:
                    worker.join(timeout=3)
            self.assertFalse(any(worker.is_alive() for worker in threads))
            self.assertEqual([], errors)
            current = ConfigManager(path)
            self.assertEqual("复制", current.get_config("mode"))
            self.assertEqual(r"D:\Anime\TV", current.get_config("anime_path"))

    def test_e_two_processes_merge_reverse_updates_without_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            ConfigManager(path)
            start = root / "start.signal"
            script = """
import sys
import time
from pathlib import Path
from src.config.config_manager import ConfigManager

config_path, ready_path, start_path, key, value = sys.argv[1:]
manager = ConfigManager(Path(config_path))
Path(ready_path).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 10
while not Path(start_path).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("start signal timeout")
    time.sleep(0.01)
manager.set_config(key, value)
"""
            processes = []
            ready_paths = []
            for index, (key, value) in enumerate((
                ("mode", "复制"),
                ("anime_path", r"D:\Anime\TV"),
            )):
                ready = root / f"ready-{index}.signal"
                ready_paths.append(ready)
                environment = os.environ.copy()
                environment["BAR_DATA_DIR"] = str(root / f"child-data-{index}")
                processes.append(subprocess.Popen(
                    [
                        sys.executable,
                        "-X",
                        "utf8",
                        "-c",
                        script,
                        str(path),
                        str(ready),
                        str(start),
                        key,
                        value,
                    ],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                ))

            deadline = time.monotonic() + 8
            while (
                not all(item.exists() for item in ready_paths)
                and all(process.poll() is None for process in processes)
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            ready = all(item.exists() for item in ready_paths)
            outputs: list[tuple[str, str]] = []
            return_codes: list[int | None] = []
            try:
                if ready:
                    start.write_text("start", encoding="utf-8")
                for process in processes:
                    try:
                        outputs.append(process.communicate(timeout=10))
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        outputs.append(process.communicate(timeout=5))
                    return_codes.append(process.returncode)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)

            self.assertTrue(ready, outputs)
            self.assertEqual([0, 0], return_codes, outputs)
            current = ConfigManager(path)
            self.assertEqual("复制", current.get_config("mode"))
            self.assertEqual(r"D:\Anime\TV", current.get_config("anime_path"))

    def test_control_single_manager_round_trip_remains_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ConfigManager(Path(directory) / "config.json")
            self.assertTrue(manager.set_config("anime_path", r"D:\Anime\Test"))
            self.assertEqual(r"D:\Anime\Test", manager.get_config("anime_path"))


class ReparseAndScopeEscapeIncidentRedContracts(unittest.TestCase):
    def test_e_source_scan_does_not_descend_windows_junction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            outside = root / "outside"
            source.mkdir()
            outside.mkdir()
            escaped = outside / "escaped.mkv"
            escaped.write_bytes(b"outside")
            local = source / "local.mkv"
            local.write_bytes(b"inside")
            junction = source / "junction"
            _make_junction(junction, outside)
            try:
                scanned = collected_files(source)
            finally:
                if junction.exists():
                    junction.rmdir()
            self.assertEqual((local.resolve(),), scanned)

    def test_e_execution_rejects_target_root_that_is_a_junction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            outside = root / "outside-target"
            source_root.mkdir()
            outside.mkdir()
            source = source_root / "Episode 01.mkv"
            source.write_bytes(b"episode")
            target_root = root / "target-root"
            _make_junction(target_root, outside)
            plan = RenamePlan.from_mapping(
                task_id="junction-target",
                source_root=source_root,
                mapping={source: target_root / "Season 01" / "S01E01.mkv"},
                metadata={"target_root": str(target_root)},
                execution_policy=ExecutionPolicy(target_root=target_root),
            )
            try:
                with self.assertRaisesRegex(
                    PlanExecutionError, "junction|reparse|重解析|链接"
                ):
                    PlanExecutionService._validate_execution_paths(plan)
            finally:
                if target_root.exists():
                    target_root.rmdir()



    def test_control_plain_directory_scan_stays_within_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            expected = source / "Episode 01.mkv"
            expected.write_bytes(b"episode")
            self.assertEqual(
                (expected.resolve(),),
                collected_files(source),
            )


if __name__ == "__main__":
    unittest.main()
