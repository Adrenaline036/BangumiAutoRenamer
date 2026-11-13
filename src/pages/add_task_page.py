"""Source admission; accepted tasks continue in the background queue."""

from __future__ import annotations
from ..queue_runtime import ui_disk_call

from src.component.dialog_frame import dialog_card
from .progress_view import bar_progress

import asyncio
from pathlib import Path
from typing import Optional, Sequence, Tuple

from nicegui import ui, run

from ..component.local_file_picker import local_file_picker
from ..element.red import notify
from ..task_queue import task_queue


MEDIA_OPTIONS = {
    "auto": "自动识别",
    "anime": "动画剧集",
    "anime_movie": "动画电影",
    "movie": "电影",
    "tv": "真人剧集",
}


def media_flags(value: str) -> Tuple[Optional[bool], Optional[bool]]:
    return {
        "auto": (None, None),
        "anime": (True, False),
        "anime_movie": (True, True),
        "movie": (False, True),
        "tv": (False, False),
    }[value]


class AddTaskDialog(ui.dialog):
    def __init__(self) -> None:
        super().__init__()
        self.step = 1
        self.path = ""
        self.input_paths: Tuple[str, ...] = ()
        self.media_type = "auto"
        self._submitting = False
        self._submission_needs_check = False
        self._discovering = False
        self._generation = 0
        self._closed = False
        self.discovery = None
        self.client.on_disconnect(self._invalidate)
        self.on_value_change(lambda event: self._invalidate() if not event.value else None)

        with self, dialog_card(header=2).classes("scroll bar-dialog bar-task-wizard bar-modal-fixed"):
            with ui.row().classes("bar-dialog-header w-full items-start"):
                with ui.column().classes("bar-dialog-header-content gap-1"):
                    ui.label("添加整理任务").classes("bar-dialog-title")
                    self.progress = bar_progress(ui).bind_visibility_from(self, '_submitting')

            self.stepper = ui.row().classes("bar-stepper w-full")
            self.content = ui.column().classes("w-full gap-4 bar-modal-body scroll")
            self.actions = ui.row().classes(
                "bar-dialog-actions w-full items-center justify-between"
            ).classes('bar-modal-footer')
            self._render()

    def _render(self) -> None:
        self.stepper.clear()
        self.stepper.set_visibility(self.step == 2)
        if self.step == 2:
            with self.stepper:
                ui.label('核对来源分组').classes('bar-field-label')

        self.content.clear()
        with self.content:
            if self.step == 1:
                self._render_source_step()
            else:
                self._render_confirm_step()

        self.actions.clear()
        with self.actions:
            ui.button("关闭", on_click=self.close, icon='o_close').props(
                "flat color=grey-7"
            )
            with ui.row().classes("gap-2 order-first"):
                if self.step == 2:
                    ui.button(
                        "上一步", on_click=self._previous, icon='o_arrow_back'
                    ).props("outline color=blue-7")
                    self.submit_button = ui.button(
                        "确认加入队列",
                        on_click=self._submit,
                        icon='o_playlist_add_check',
                    ).props("color=green-7")
                else:
                    self.submit_button = ui.button(
                        "添加任务", on_click=self._next, icon='o_add'
                    ).props("color=red-6")
            if self._submitting or self._submission_needs_check:
                self.submit_button.props('disable')

    def _render_source_step(self) -> None:
        with ui.column().classes("w-full gap-2"):
            ui.label("来源路径").classes("bar-field-label")
            with ui.row().classes("w-full gap-2 no-wrap items-center"):
                self.path_input = ui.input(
                    placeholder="选择源文件或目录",
                    value=self.path,
                    on_change=self._path_changed,
                ).props("outlined dense clearable").classes("grow")
                ui.button("浏览", on_click=self._browse, icon='o_folder_open').props(
                    "outline color=blue-7"
                )
            selected_paths = self._submission_paths()
            if selected_paths:
                ui.label(f"已选择 {len(selected_paths)} 项来源").classes(
                    "bar-field-label"
                )
                with ui.column().classes("w-full gap-1"):
                    for selected_path in selected_paths:
                        with ui.row().classes(
                            "w-full items-center justify-between gap-2 no-wrap"
                        ):
                            ui.label(str(selected_path)).classes(
                                "font-mono text-sm grow break-all"
                            )
                            ui.button(
                                "移除",
                                icon='o_remove_circle_outline',
                                on_click=lambda _event=None, path=selected_path: self._remove_source(
                                    path
                                ),
                            ).props("flat dense color=red-6")


    def _render_confirm_step(self) -> None:
        is_anime, is_movie = media_flags(self.media_type)
        selected = self._submission_paths()
        source_summary = (
            str(selected[0]) if len(selected) == 1
            else f"{selected[0]} 等 {len(selected)} 项"
            if selected else "尚未填写"
        )
        with ui.column().classes("bar-confirm-card w-full gap-3"):
            self._summary_row("来源", source_summary, monospace=True)
            self._summary_row("媒体类型", MEDIA_OPTIONS[self.media_type])
        if self.discovery is not None:
            from ..rename.input_directory_discovery import discovery_task_groups
            task_groups = discovery_task_groups(self.discovery)
            ui.label(f'发现 {len(self.discovery.groups)} 个完整来源组；{len(task_groups)} 组生成任务卡，'
                     f'{len(self.discovery.groups) - len(task_groups)} 组明确杂项无需整理').classes('bar-field-label')
            with ui.column().classes('bar-source-groups scroll w-full'):
                for group in self.discovery.groups:
                    with ui.column().classes('bar-confirm-card w-full gap-1'):
                        ui.label(str(group.path)).classes('font-mono break-all')
                        ui.label(f'{len(group.identities)} 个文件 · ' + ('明确杂项：不创建任务，源文件保留' if group not in task_groups
                            else '待核对来源' if group.reason else '自动识别作品'))
                        if group.reason and group.reason != '花絮、特典或音频附加内容；请核对完整来源组':
                            ui.label(group.reason).classes('bar-help')
                        with ui.expansion('完整文件清单').classes('w-full'):
                            for row in group.identities:
                                ui.label(row.source_path).classes('text-sm break-all')

    def _summary_row(self, label: str, value: str, monospace: bool = False) -> None:
        with ui.row().classes("w-full justify-between gap-4 no-wrap"):
            ui.label(label).classes("bar-muted")
            ui.label(value).classes(
                "bar-summary-value " + ("font-mono" if monospace else "")
            )

    async def _browse(self) -> None:
        initial = self.path if self.path and Path(self.path).exists() else "~"
        try:
            result: Optional[Sequence[str]] = await local_file_picker(
                initial, multiple=True
            )
        except Exception as exc:
            notify(f"选择来源路径失败：{exc}", type="negative")
            return
        if result:
            if self._closed or self.is_deleted:
                return
            self._generation += 1
            self.discovery = None
            combined = (*self._submission_paths(), *(Path(str(path)) for path in result))
            unique: list[str] = []
            seen: set[str] = set()
            for candidate in combined:
                resolved = candidate.expanduser().resolve(strict=False)
                key = str(resolved).casefold()
                if key in seen:
                    continue
                seen.add(key)
                unique.append(str(candidate))
            self.input_paths = tuple(unique)
            self.path = self.input_paths[0]
            self._render()

    def _remove_source(self, path: Path) -> None:
        self._generation += 1
        self.discovery = None
        remove_key = str(path.expanduser().resolve(strict=False)).casefold()
        retained = tuple(
            str(candidate)
            for candidate in self._submission_paths()
            if str(candidate.expanduser().resolve(strict=False)).casefold()
            != remove_key
        )
        self.input_paths = retained
        self.path = retained[0] if retained else ""
        self._render()

    def _path_changed(self, event) -> None:
        self._generation += 1
        self.discovery = None
        self.path = event.value or ""
        self.input_paths = ()

    def _submission_paths(self) -> Tuple[Path, ...]:
        for name in ("input_paths", "paths", "selected_paths"):
            values = getattr(self, name, ())
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                normalized = tuple(Path(str(value)) for value in values if str(value))
                if normalized:
                    return normalized
        value = str(getattr(self, "path", "") or "").strip()
        return (Path(value),) if value else ()

    async def _next(self) -> None:
        if self._discovering or self._submitting or self._closed or self._submission_needs_check:
            return
        paths = self._submission_paths()
        if not paths:
            notify("请先选择或填写来源路径", type="warning")
            return
        self._discovering = True
        direct = False
        generation = self._generation
        try:
            from ..rename.input_directory_discovery import discover_sources
            observed = self.discovery
            if observed is None:
                observed = await run.io_bound(discover_sources, paths)
            if self._closed or self.is_deleted or generation != self._generation or paths != self._submission_paths():
                return
            with self.client:
                self.discovery = observed
                direct = bool(observed is not None and len(paths) == 1 and len(observed.groups) == 1
                              and not observed.groups[0].reason)
                if direct:
                    from ..rename.input_directory_discovery import discovery_task_groups
                    direct = len(discovery_task_groups(observed)) == 1
                if not direct:
                    self.step = 2
                    self._render()
        except (ValueError, RuntimeError, OSError) as exc:
            if not self._closed and not self.is_deleted and generation == self._generation:
                with self.client:
                    notify(f'来源扫描未完成：{exc}', type='negative')
        finally:
            self._discovering = False
        if direct:
            await self._submit()

    def _invalidate(self):
        self._closed = True
        self._generation += 1

    def close(self):
        self._invalidate()
        super().close()

    def _previous(self) -> None:
        if self._submission_needs_check:
            return
        self._generation += 1
        self.step = 1
        self._render()

    async def _submit(self) -> None:
        if (self._submitting or getattr(self, '_closed', False) or getattr(self, '_discovering', False)
                or getattr(self, '_submission_needs_check', False)):
            return
        self._submitting = True
        admission_started = False
        accepted = False

        def mark_admission_started():
            nonlocal admission_started
            admission_started = True

        try:
            self.submit_button.props("disable")
            observed = getattr(self, 'discovery', None)
            generation = getattr(self, '_generation', 0)
            paths = AddTaskDialog._submission_paths(self)
            media_type = self.media_type

            def inputs_unchanged():
                return (not getattr(self, '_closed', False) and not getattr(self, 'is_deleted', False)
                        and generation == getattr(self, '_generation', 0)
                        and paths == AddTaskDialog._submission_paths(self)
                        and media_type == self.media_type
                        and observed is getattr(self, 'discovery', None))

            is_anime, is_movie = media_flags(media_type)
            if observed is not None:
                if tuple(path.expanduser().absolute() for path in paths) != observed.roots:
                    raise ValueError('所选来源已变化，请返回重新扫描')
                batch = await ui_disk_call(task_queue.prepare_discovered_batch, observed,
                    is_anime=is_anime, is_movie=is_movie, source='ui')
                if batch.get('discovery') != observed:
                    raise ValueError('入队准备缺少原完整预览，未添加任务')
                if not inputs_unchanged():
                    return
            else:
                raise ValueError('请先完成来源扫描并核对完整分组')
            task_ids = await task_queue.enqueue_batch(**batch, admission_guard=inputs_unchanged,
                                                     on_admission_started=mark_admission_started)
            accepted = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not admission_started and not getattr(self, '_closed', False) and not getattr(self, 'is_deleted', False):
                notify(f"加入队列失败：{exc}", type="negative")
            return
        finally:
            # 停止进度显示；接纳结果不确定时，单独锁住本次提交供人工核对。
            self._submitting = False
            self._submission_needs_check = admission_started and not accepted
            if not getattr(self, '_closed', False) and not getattr(self, 'is_deleted', False):
                if self._submission_needs_check:
                    self.submit_button.props('disable')
                    notify('接纳结果需要核对，请关闭此窗口并查看任务卡；本次提交不可直接重试。', type='warning')
                elif not accepted:
                    self.submit_button.props(remove='disable')
        self.close()
        if not task_ids:
            notify('所选来源均为已确认杂项，无需创建任务；源文件保持原状。', type='info')
            return
        task_id = task_ids[0]
        notify(
            f"任务 {task_id[:8]}"
            + (f" 等 {len(task_ids)} 项" if len(task_ids) > 1 else "")
            + (" 已添加或核查；请在任务卡确认整理计划"),
            type="positive",
        )

def show_add_task_dialog() -> AddTaskDialog:
    dialog = AddTaskDialog()
    dialog.open()
    return dialog
