"""Two-step task creation wizard with immediate queue feedback."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence, Tuple

from nicegui import ui

from ..component.local_file_picker import local_file_picker
from ..element.red import notify
from ..task_queue import is_ui_test_mode, task_queue
from .ui_terms import RENAME_PLAN_TERM


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
        self.path = (
            r"D:\GUI-PREVIEW\Incoming\Example Anime [1080p]"
            if is_ui_test_mode()
            else ""
        )
        self.input_paths: Tuple[str, ...] = ()
        self.media_type = "auto"
        self.use_ai = False
        self._submitting = False

        with self, ui.card().classes("bar-dialog bar-task-wizard"):
            with ui.row().classes("bar-dialog-header w-full items-start"):
                with ui.column().classes("bar-dialog-header-content gap-1"):
                    ui.label("添加整理任务").classes("bar-dialog-title")
                    ui.label(
                        "先选择来源，再确认入队参数；提交后立即出现在任务队列。"
                    ).classes("bar-muted")
                if is_ui_test_mode():
                    ui.badge("测试模式 · 不执行", color="orange-7").classes(
                        "bar-dialog-header-actions"
                    )

            self.stepper = ui.row().classes("bar-stepper w-full")
            self.content = ui.column().classes("w-full gap-4")
            self.actions = ui.row().classes(
                "bar-dialog-actions w-full items-center justify-between"
            )
            self._render()

    def _render(self) -> None:
        self.stepper.clear()
        with self.stepper:
            self._step_marker(1, "选择来源")
            ui.icon("chevron_right").classes("bar-muted")
            self._step_marker(2, "确认入队")

        self.content.clear()
        with self.content:
            if self.step == 1:
                self._render_source_step()
            else:
                self._render_confirm_step()

        self.actions.clear()
        with self.actions:
            ui.button("取消", on_click=self.close, icon="close").props(
                "flat color=grey-7"
            )
            with ui.row().classes("gap-2"):
                if self.step == 2:
                    ui.button(
                        "上一步", on_click=self._previous, icon="arrow_back"
                    ).props("outline color=blue-7")
                    self.submit_button = ui.button(
                        "加入测试队列（不执行）"
                        if is_ui_test_mode()
                        else "确认加入队列",
                        on_click=self._submit,
                        icon="playlist_add_check",
                    ).props("color=green-7")
                else:
                    ui.button(
                        "下一步", on_click=self._next, icon="arrow_forward"
                    ).props("color=red-6")

    def _step_marker(self, number: int, label: str) -> None:
        active = number <= self.step
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.label("✓" if number < self.step else str(number)).classes(
                "bar-step-dot " + ("bar-step-active" if active else "")
            )
            ui.label(label).classes(
                "font-medium" if active else "bar-muted"
            )

    def _render_source_step(self) -> None:
        with ui.column().classes("w-full gap-2"):
            ui.label("来源路径").classes("bar-field-label")
            with ui.row().classes("w-full gap-2 no-wrap items-center"):
                self.path_input = ui.input(
                    placeholder="选择下载完成的文件或目录",
                    value=self.path,
                    on_change=self._path_changed,
                ).props("outlined dense clearable").classes("grow")
                ui.button("浏览", on_click=self._browse, icon="folder_open").props(
                    "outline color=blue-7"
                )
            ui.label(
                f"任务提交只登记来源路径；目标路径将在识别后以{RENAME_PLAN_TERM}形式供处理和批准。"
            ).classes("bar-help")
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
                                icon="remove_circle_outline",
                                on_click=lambda _event=None, path=selected_path: self._remove_source(
                                    path
                                ),
                            ).props("flat dense color=red-6")

        with ui.row().classes("bar-form-grid w-full"):
            with ui.column().classes("gap-2 grow"):
                ui.label("媒体类型").classes("bar-field-label")
                ui.select(
                    MEDIA_OPTIONS,
                    value=self.media_type,
                    on_change=lambda event: setattr(
                        self, "media_type", event.value
                    ),
                ).props("outlined dense options-dense").classes("w-full")
                ui.label("不确定时保持自动识别。").classes("bar-help")
            with ui.column().classes("gap-2 grow"):
                ui.label("AI 策略").classes("bar-field-label")
                ui.switch(
                    "允许本任务使用已配置的 AI",
                    value=self.use_ai,
                    on_change=lambda event: setattr(
                        self, "use_ai", bool(event.value)
                    ),
                ).props("color=blue-7")
                ui.label("关闭时只运行确定性规则。").classes("bar-help")

        if is_ui_test_mode():
            with ui.row().classes("bar-callout bar-callout-warning w-full"):
                ui.icon("science", color="orange-8")
                ui.label(
                    "测试模式允许使用不存在的合成路径；入队后不会调用 Rename 或 Trans。"
                )

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
            self._summary_row("AI", "允许" if self.use_ai else "禁用")
            self._summary_row(
                "任务标记", f"is_anime={is_anime} / is_movie={is_movie}"
            )
        with ui.row().classes("bar-callout bar-callout-info w-full"):
            ui.icon("schedule", color="blue-7")
            ui.label(
                "点击确认后弹窗立即关闭，任务先显示为“排队中”；开始执行后转为“执行中”。"
            )

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

    def _next(self) -> None:
        paths = self._submission_paths()
        if not paths:
            notify("请先选择或填写来源路径", type="warning")
            return
        if not is_ui_test_mode() and any(not path.exists() for path in paths):
            notify("来源路径不存在，请重新选择", type="negative")
            return
        self.step = 2
        self._render()

    def _previous(self) -> None:
        self.step = 1
        self._render()

    async def _submit(self) -> None:
        if self._submitting:
            return
        self._submitting = True
        self.submit_button.props("loading disable")
        is_anime, is_movie = media_flags(self.media_type)
        try:
            batch = task_queue.prepare_input_batch(
                AddTaskDialog._submission_paths(self),
                is_anime=is_anime,
                is_movie=is_movie,
                use_ai=self.use_ai,
                source="ui",
                preview_only=is_ui_test_mode(),
            )
            task_ids = await task_queue.enqueue_batch(**batch)
        except Exception as exc:
            notify(f"加入队列失败：{exc}", type="negative")
            self.submit_button.props(remove="loading disable")
            self._submitting = False
            return
        self.close()
        task_id = task_ids[0]
        notify(
            f"任务 {task_id[:8]}"
            + (f" 等 {len(task_ids)} 项" if len(task_ids) > 1 else "")
            + " 已加入"
            + ("测试队列；不会执行" if is_ui_test_mode() else "执行队列"),
            type="positive",
        )


def show_add_task_dialog() -> AddTaskDialog:
    dialog = AddTaskDialog()
    dialog.open()
    return dialog


async def pick_file() -> None:
    """Backward-compatible entrypoint used by the top navigation."""

    show_add_task_dialog()
