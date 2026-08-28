"""Large, mount-rooted file picker used by task and configuration dialogs."""

from __future__ import annotations

import platform
import string
from pathlib import Path
from typing import Optional

from nicegui import events, ui

from ..config.config_manager import cm


NAS_SYSTEM_NAMES = {"lost+found"}


def _is_system_entry(path: Path) -> bool:
    """Hide NAS application/system folders which are never media roots."""
    return path.name.startswith("@") or path.name.casefold() in NAS_SYSTEM_NAMES


def _is_visible_entry(path: Path, *, show_hidden_files: bool = False) -> bool:
    if _is_system_entry(path):
        return False
    return show_hidden_files or not path.name.startswith(".")


def _safe_picker_root(directory: str, configured_mount: Optional[str] = None) -> Path:
    """Prefer the configured Linux mount, otherwise retain the requested path."""
    requested = Path(directory).expanduser()
    if platform.system() == "Linux":
        mount = Path(configured_mount or cm.get_config("docker_mnt") or directory)
        mount = mount.expanduser()
        if mount.is_dir():
            return mount.resolve()
    if requested.is_dir():
        return requested.resolve()
    return Path.home().resolve()


def _within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _available_windows_roots() -> tuple[Path, ...]:
    if platform.system() != "Windows":
        return ()
    return tuple(
        root
        for letter in string.ascii_uppercase
        if (root := Path(f"{letter}:\\")).is_dir()
    )


class local_file_picker(ui.dialog):
    def __init__(
        self,
        directory: str,
        *,
        upper_limit: Optional[str] = None,
        multiple: bool = False,
        show_hidden_files: bool = False,
        title: str = "选择路径",
        display_root: Optional[str] = None,
    ) -> None:
        """Browse the server filesystem without exposing NAS system folders."""
        super().__init__()

        configured_mount = cm.get_config("docker_mnt")
        root = _safe_picker_root(directory, configured_mount)
        if upper_limit is not None:
            candidate = Path(directory if upper_limit == ... else upper_limit).expanduser()
            if candidate.is_dir():
                root = candidate.resolve()

        requested = Path(directory).expanduser()
        self.root = root
        self.path = requested.resolve() if requested.is_dir() and _within_root(requested, root) else root
        self.root_choices = (
            _available_windows_roots() if upper_limit is None else (self.root,)
        )
        self.show_hidden_files = show_hidden_files
        self.display_root = display_root

        self.classes("bar-file-picker-overlay")
        with self, ui.card().classes("bar-file-picker-dialog"):
            with ui.row().classes("w-full items-start justify-between no-wrap"):
                with ui.column().classes("gap-1 min-w-0"):
                    ui.label(title).classes("bar-dialog-title")
                    ui.label(
                        "双击文件夹进入；选中一项后确认，未选中则使用当前文件夹。"
                    ).classes("bar-muted")
                ui.button(icon="close", on_click=self.close).props(
                    'round flat color=grey-7 aria-label="关闭路径选择器"'
                )

            with ui.row().classes("bar-picker-location w-full items-center no-wrap"):
                ui.icon("account_tree", color="blue-7")
                self.location_label = ui.label().classes("bar-picker-location-text")

            if len(self.root_choices) > 1:
                with ui.row().classes("w-full items-center gap-1"):
                    ui.label("位置").classes("bar-muted")
                    for drive_root in self.root_choices:
                        ui.button(
                            str(drive_root),
                            on_click=lambda _event=None, drive=drive_root: self._switch_root(
                                drive
                            ),
                        ).props("flat dense color=blue-7")

            selection = "multiple" if multiple else "single"
            self.grid = (
                ui.aggrid(
                    {
                        "columnDefs": [
                            {
                                "field": "name",
                                "headerName": "文件夹或文件",
                                "flex": 1,
                                "minWidth": 280,
                            },
                            {
                                "field": "kind",
                                "headerName": "类型",
                                "width": 110,
                            },
                        ],
                        "rowSelection": selection,
                        "animateRows": False,
                        "suppressCellFocus": True,
                    }
                )
                .classes("bar-file-picker-grid w-full")
                .on("cellDoubleClicked", self.handle_double_click)
            )

            with ui.row().classes("bar-picker-actions w-full justify-end gap-2"):
                ui.button("取消", icon="close", on_click=self.close).props(
                    "outline color=red-6"
                )
                ui.button("确定", icon="check", on_click=self._handle_ok).props(
                    "unelevated color=red-6"
                )
        self.update_grid()

    def _location_text(self) -> str:
        if not self.display_root:
            return str(self.path)
        relative = self.path.relative_to(self.root)
        return str(Path(self.display_root) / relative) if relative.parts else self.display_root

    def update_grid(self) -> None:
        try:
            paths = [
                path
                for path in self.path.iterdir()
                if _is_visible_entry(path, show_hidden_files=self.show_hidden_files)
            ]
        except (OSError, PermissionError):
            paths = []
        paths.sort(key=lambda path: (not path.is_dir(), path.name.casefold()))

        rows = [
            {
                "name": f"📁  {path.name}" if path.is_dir() else path.name,
                "kind": "文件夹" if path.is_dir() else "文件",
                "path": str(path),
            }
            for path in paths
        ]
        if self.path != self.root:
            rows.insert(
                0,
                {"name": "📁  ..", "kind": "上一级", "path": str(self.path.parent)},
            )
        self.location_label.text = self._location_text()
        self.grid.options["rowData"] = rows
        self.grid.update()

    def _switch_root(self, root: Path) -> None:
        if root not in self.root_choices or not root.is_dir():
            return
        self.root = root.resolve()
        self.path = self.root
        self.update_grid()

    def handle_double_click(self, event: events.GenericEventArguments) -> None:
        selected = Path(event.args["data"]["path"])
        if not _within_root(selected, self.root):
            return
        if selected.is_dir():
            self.path = selected.resolve()
            self.update_grid()
        else:
            self.submit([str(selected)])

    async def _handle_ok(self) -> None:
        rows = await self.grid.get_selected_rows()
        selected = [row["path"] for row in rows if _within_root(Path(row["path"]), self.root)]
        self.submit(selected or [str(self.path)])
