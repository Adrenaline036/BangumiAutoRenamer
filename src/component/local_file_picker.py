"""Large, mount-rooted file picker used by task and configuration dialogs."""

from __future__ import annotations

import asyncio
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

        self._navigation = 0
        self._closed = False
        self.root = self.path = Path(directory).expanduser()
        self.root_choices = ()
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
                ui.button(icon='o_close', on_click=self.close).props(
                    'round flat color=grey-7 aria-label="关闭路径选择器"'
                )

            with ui.row().classes("bar-picker-location w-full items-center no-wrap"):
                ui.icon('o_account_tree', color="blue-7")
                self.location_label = ui.label().classes("bar-picker-location-text")

            self.root_row = ui.row().classes('w-full items-center gap-1')
            self.root_row.set_visibility(False)

            selection = "multiple" if multiple else "single"
            self.grid = (
                ui.aggrid(
                    {
                        "columnDefs": [
                            {
                                "field": "name",
                                "headerName": "文件夹或文件",
                                ':cellRenderer': """params => {
                                    const row = document.createElement('span');
                                    row.style.display = 'inline-flex'; row.style.alignItems = 'center';
                                    const icon = document.createElement('i');
                                    icon.className = 'material-icons-outlined';
                                    icon.style.fontSize = '20px'; icon.style.marginRight = '8px';
                                    icon.setAttribute('aria-hidden', 'true');
                                    icon.textContent = params.data.kind === '文件' ? 'description' : 'folder';
                                    const name = document.createElement('span');
                                    name.textContent = params.value == null ? '' : String(params.value);
                                    row.append(icon, name); return row;
                                }""",
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
                ui.button("取消", icon='o_close', on_click=self.close).props(
                    "outline color=red-6"
                )
                self.confirm_button = ui.button("确定", icon='o_check', on_click=self._handle_ok).props(
                    "unelevated color=red-6"
                )
        self.confirm_button.disable()
        self.on('hide', self._invalidate)
        configured_mount = cm.get_config('docker_mnt')
        self._initial_load = asyncio.create_task(self._initialize(directory, upper_limit, configured_mount))

    def _location_text(self) -> str:
        if not self.display_root:
            return str(self.path)
        relative = self.path.relative_to(self.root)
        return str(Path(self.display_root) / relative) if relative.parts else self.display_root

    def _invalidate(self):
        self._closed = True
        self._navigation += 1

    def close(self):
        self._invalidate()
        super().close()

    def _current(self, serial):
        return not self._closed and not self.is_deleted and serial == self._navigation

    async def _initialize(self, directory, upper_limit, configured_mount):
        def prepare():
            root = _safe_picker_root(directory, configured_mount)
            if upper_limit is not None:
                candidate = Path(directory if upper_limit == ... else upper_limit).expanduser()
                if candidate.is_dir():
                    root = candidate.resolve()
            requested = Path(directory).expanduser()
            path = requested.resolve() if requested.is_dir() and _within_root(requested, root) else root
            choices = _available_windows_roots() if upper_limit is None else (root,)
            return root, path, choices
        serial = self._navigation
        root, path, choices = await asyncio.to_thread(prepare)
        if not self._current(serial):
            return
        self.root, self.path, self.root_choices = root, path, choices
        with self.client, self.root_row:
            if len(choices) > 1:
                self.root_row.set_visibility(True)
                ui.label('位置').classes('bar-muted')
                for drive in choices:
                    ui.button(str(drive), on_click=lambda _event=None, drive=drive: self._switch_root(drive), icon='o_storage').props('flat dense color=blue-7')
        await self.update_grid()

    async def update_grid(self):
        self._navigation += 1
        serial = self._navigation
        path, root = self.path, self.root
        self.confirm_button.disable()
        rows = await asyncio.to_thread(_directory_rows, path, root, self.show_hidden_files)
        if not self._current(serial):
            return
        with self.client:
            self.location_label.text = self._location_text()
            self.grid.options['rowData'] = rows
            self.grid.update()
            self.confirm_button.enable()

    async def _switch_root(self, root):
        if root not in self.root_choices:
            return
        self._navigation += 1
        serial = self._navigation
        resolved = await asyncio.to_thread(lambda: root.resolve() if root.is_dir() else None)
        if resolved is None or not self._current(serial):
            return
        self.root = self.path = resolved
        await self.update_grid()

    async def handle_double_click(self, event: events.GenericEventArguments):
        selected, root = Path(event.args['data']['path']), self.root
        self._navigation += 1
        serial = self._navigation
        def inspect():
            if not _within_root(selected, root):
                return None
            return selected.resolve(), selected.is_dir()
        found = await asyncio.to_thread(inspect)
        if found is None or not self._current(serial):
            return
        path, directory = found
        if directory:
            self.path = path
            await self.update_grid()
        else:
            self.submit([str(selected)])

    async def _handle_ok(self):
        serial, root, path = self._navigation, self.root, self.path
        rows = await self.grid.get_selected_rows()
        selected = await asyncio.to_thread(lambda: [row['path'] for row in rows
            if _within_root(Path(row['path']), root)])
        if self._current(serial):
            self.submit(selected or [str(path)])


def _directory_rows(directory, root, show_hidden):
    try:
        items = [(path, path.is_dir()) for path in directory.iterdir()
                 if _is_visible_entry(path, show_hidden_files=show_hidden)]
    except OSError:
        items = []
    items.sort(key=lambda item: (not item[1], item[0].name.casefold()))
    rows = [{'name': path.name,
             'kind': '文件夹' if is_dir else '文件', 'path': str(path)} for path, is_dir in items]
    if directory != root:
        rows.insert(0, {'name': '..', 'kind': '上一级', 'path': str(directory.parent)})
    return rows
