"""Application shell for the BAR task and review workspace."""

from __future__ import annotations

import os

from nicegui import ui

from .__version__ import __url__, __version__
from .pages.add_task_page import show_add_task_dialog
from .pages.config_page import config_page
from .pages.data_table_page import create_table, refresh_dashboard
from .pages.download_log import download_log
from .rename.trans import is_dry_run_enabled
from .task_queue import is_ui_test_mode


BAR_CSS = r"""
:root {
  --bar-bg: #f6f7fb;
  --bar-panel: #ffffff;
  --bar-border: #e6e8ef;
  --bar-text: #172033;
  --bar-muted: #6b7486;
  --bar-red: #d64d4d;
  --bar-yellow: #e9aa22;
  --bar-blue: #3376d4;
  --bar-green: #269b63;
  --bar-radius: 18px;
}
html, body { background: var(--bar-bg); color: var(--bar-text); }
body { font-family: Inter, "Microsoft YaHei", system-ui, sans-serif; }
.nicegui-content { padding: 0 !important; gap: 0 !important; }
.bar-header {
  height: 60px; padding: 0 24px; background: #fff !important;
  color: var(--bar-text) !important; border-bottom: 1px solid var(--bar-border);
  box-shadow: none !important;
}
.bar-brand { transform: translateY(2px); }
.bar-brand-title { font-size: 17px; font-weight: 800; letter-spacing: -.02em; }
.bar-brand-subtitle { color: var(--bar-muted); font-size: 11px; }
.bar-live-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--bar-green);
  box-shadow: 0 0 0 5px rgba(38,155,99,.12); }
.bar-header-actions { margin-left: auto; transform: translateY(1px); }
.bar-header-button { border-radius: 10px !important; font-weight: 750; min-height: 38px; }
.bar-page { min-height: calc(100vh - 60px); padding: 26px clamp(18px, 4vw, 58px) 54px; }
.bar-workspace { max-width: 1440px; margin: 0 auto; gap: 22px; }
.bar-page-title { font-size: clamp(26px, 3vw, 38px); font-weight: 850; letter-spacing: -.045em; }
.bar-muted { color: var(--bar-muted); }
.bar-help { color: #7a8496; font-size: 12px; line-height: 1.55; }
.bar-stat-grid { display: grid !important; grid-template-columns: repeat(4,minmax(150px,1fr)); gap: 14px; }
.bar-stat-card { min-width: 0; border: 1px solid var(--bar-border); border-radius: var(--bar-radius) !important;
  padding: 17px 18px; box-shadow: 0 8px 24px rgba(28,39,61,.05) !important; }
.bar-stat-icon { font-size: 24px; }
.bar-stat-value { font-size: 28px; line-height: 1; font-weight: 850; }
.bar-stat-label { margin-top: 8px; color: var(--bar-muted); font-size: 13px; font-weight: 700; }
.bar-tone-amber { border-top: 4px solid var(--bar-yellow); } .bar-tone-amber .bar-stat-icon { color: var(--bar-yellow); }
.bar-tone-blue { border-top: 4px solid var(--bar-blue); } .bar-tone-blue .bar-stat-icon { color: var(--bar-blue); }
.bar-tone-red { border-top: 4px solid var(--bar-red); } .bar-tone-red .bar-stat-icon { color: var(--bar-red); }
.bar-tone-green { border-top: 4px solid var(--bar-green); } .bar-tone-green .bar-stat-icon { color: var(--bar-green); }
.bar-tabs { background: white; border: 1px solid var(--bar-border); border-radius: 14px; padding: 4px; }
.bar-tab-panels { background: transparent !important; }
.bar-empty-state { padding: 58px 18px; background: white; border: 1px dashed #cfd4df; border-radius: var(--bar-radius); }
.bar-task-card { border: 1px solid var(--bar-border); border-radius: 15px !important;
  position: relative; padding: 8px 14px; box-shadow: 0 3px 12px rgba(28,39,61,.035) !important; }
.bar-task-card:hover { border-color: #cbd1dc; box-shadow: 0 10px 28px rgba(28,39,61,.08) !important; }
.bar-task-header { display: grid !important; grid-template-columns: minmax(0,1fr) 394px;
  column-gap: 16px; align-items: start; }
.bar-task-content { min-width: 0; width: 100%; }
.bar-task-title { font-size: 17px; font-weight: 780; }
.bar-path-text { color: #485267; font-family: "Cascadia Code", Consolas, monospace; font-size: 12px;
  overflow-wrap: anywhere; line-height: 1.55; }
.bar-meta-chip { padding: 3px 8px; color: #4c576b; background: #f0f2f6; border-radius: 999px; font-size: 11px; font-weight: 700; }
.bar-task-topline { min-height: 25px; }
.bar-inline-progress { position: absolute; top: 19px; left: 50%; transform: translateX(-50%);
  width: min(40%, 480px); min-width: 300px; margin-left: 0; }
.bar-progress { width: 100%; min-width: 0; max-width: none; flex: 0 0 100% !important; border-radius: 999px; }
.bar-queue-text, .bar-running-text { width: 58px; flex: 0 0 58px; font-size: 12px; font-weight: 750; white-space: nowrap; }
.bar-inline-progress .bar-queue-text, .bar-inline-progress .bar-running-text {
  position: absolute; top: 50%; right: calc(100% + 18px); transform: translateY(-50%); }
.bar-queue-text { color: #986407; }
.bar-running-text { color: #245fae; }
.bar-path-trigger { align-self: flex-start; min-height: 28px !important; padding: 2px 6px !important;
  color: #485267 !important; font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; }
.bar-path-trigger .q-icon { color: #7a8496; }
.bar-task-actions { width: 394px; max-width: 394px; min-width: 394px; align-self: start;
  justify-self: end; flex: 0 0 394px; flex-wrap: wrap !important; }
.bar-task-actions .q-btn { flex: 0 0 auto; }
.bar-task-feedback { display: block; width: 100%; max-width: none; margin-top: 8px;
  box-sizing: border-box; }
.bar-task-footnote { display: block; width: 100%; margin-top: 6px; }
.bar-review-action.bar-queue-delete { width: 68px; min-width: 68px !important;
  height: 28px; min-height: 28px !important; margin-top: 0; padding: 0 6px !important;
  border-radius: 7px !important; font-size: 12px; }
.bar-queue-delete .q-btn__content { flex-wrap: nowrap !important; gap: 3px; white-space: nowrap; }
.bar-queue-delete .q-icon { font-size: 17px; }
.bar-queue-delete .q-icon.on-left { margin-right: 0; }
.bar-review-action { border-radius: 9px !important; min-width: 88px !important; min-height: 38px !important;
  padding: 0 14px !important; font-weight: 700; }
.bar-file-picker-overlay .q-dialog__inner { padding: 18px !important; }
.bar-file-picker-dialog { width: min(94vw, 1100px) !important; max-width: none !important;
  height: min(88vh, 780px); padding: 22px !important; border-radius: 20px !important;
  flex-wrap: nowrap !important; gap: 14px !important; overflow: hidden; }
.bar-picker-location { min-height: 44px; padding: 9px 13px; background: #f5f7fa;
  border: 1px solid var(--bar-border); border-radius: 11px; }
.bar-picker-location-text { min-width: 0; overflow-wrap: anywhere; color: #354158;
  font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; }
.bar-file-picker-grid { flex: 1 1 auto; min-height: 360px; height: 100%;
  border: 1px solid var(--bar-border); border-radius: 12px; overflow: hidden; }
.bar-file-picker-grid .ag-root-wrapper { border: 0 !important; border-radius: 11px; }
.bar-picker-actions { flex: 0 0 auto; padding-top: 2px; }
.bar-path-menu { min-width: min(90vw, 440px); max-width: min(94vw, 620px); padding: 7px 5px; border: 1px solid var(--bar-border);
  border-radius: 12px; box-shadow: 0 12px 34px rgba(24,34,52,.12); }
.bar-path-level { min-height: 27px; }
.bar-path-tree-label { font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }
.bar-error-text { color: #b93232; background: #fff2f2; border-left: 4px solid var(--bar-red); padding: 9px 12px; border-radius: 8px; }
.bar-recovery-ready { color: #1f7049; background: #edf9f2; border-left: 4px solid var(--bar-green);
  padding: 7px 10px; border-radius: 8px; font-size: 12px; line-height: 1.45; }
.bar-dialog { width: min(94vw, 980px) !important; max-width: none !important; border-radius: 22px !important;
  padding: 24px !important; max-height: 90vh; overflow-y: auto; flex-wrap: nowrap !important; gap: 18px !important; }
.bar-task-wizard { width: min(92vw, 760px) !important; }
.bar-dialog-title { font-size: 24px; font-weight: 850; letter-spacing: -.035em; }
.bar-dialog-header { display: grid !important; grid-template-columns: minmax(0,1fr) auto;
  column-gap: 16px; align-items: start; }
.bar-dialog-header-content { min-width: 0; }
.bar-dialog-header-actions { min-width: max-content; justify-self: end; flex: 0 0 auto; }
.bar-dialog-actions { position: sticky; bottom: -24px; margin: 0 -24px -24px; width: calc(100% + 48px) !important;
  padding: 14px 24px; background: rgba(255,255,255,.96); border-top: 1px solid var(--bar-border); backdrop-filter: blur(12px); z-index: 5; }
.bar-config-actions { margin-top: auto; flex-shrink: 0; }
.bar-stepper { align-items: center; gap: 10px; padding: 12px 14px; background: #f5f6f9; border-radius: 13px; }
.bar-step-dot { width: 24px; height: 24px; display: grid; place-items: center; border: 1px solid #bcc3cf; border-radius: 50%; color: var(--bar-muted); font-size: 12px; font-weight: 800; }
.bar-step-active { color: white; background: var(--bar-red); border-color: var(--bar-red); }
.bar-field-label { font-size: 13px; font-weight: 800; }
.bar-form-grid { display: grid !important; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 16px; }
.bar-callout { align-items: flex-start; gap: 9px; padding: 12px 14px; border-radius: 12px; font-size: 13px; line-height: 1.55; }
.bar-callout-warning { background: #fff8e8; color: #7c560c; border: 1px solid #f1d68e; }
.bar-callout-info { background: #eef6ff; color: #24588f; border: 1px solid #bfd9f5; }
.bar-confirm-card { padding: 16px; background: #f7f8fa; border: 1px solid var(--bar-border); border-radius: 14px; }
.bar-summary-value { max-width: 68%; text-align: right; overflow-wrap: anywhere; }
.bar-review-dialog { width: min(96vw, 1480px) !important; height: min(92vh, 980px); overflow-y: auto; }
.bar-review-entries { flex: none !important; min-height: 0; }
.bar-review-entry { border: 1px solid var(--bar-border); border-radius: 13px !important; padding: 9px 12px; box-shadow: none !important; }
.bar-review-entry-review { border-left: 5px solid var(--bar-yellow); background: #fffdf7; }
.bar-review-entry-blocked { border-left-color: #c62828; background: #fff7f7; }
.bar-review-file-name { min-width: min(420px, 55vw); max-width: 100%; font-weight: 750; overflow-wrap: anywhere; }
.bar-review-file-issue { color: #a52626; font-size: 12px; overflow-wrap: anywhere; }
.bar-entry-detail .q-item { min-height: 30px; padding: 2px 0; color: var(--bar-muted); font-size: 12px; }
.bar-advanced-review { border-top: 1px solid var(--bar-border); }
.bar-issue-summary { padding: 10px 12px; background: #fff5f5; border: 1px solid #efcaca; border-radius: 11px; align-items: stretch; }
.bar-issue-heading { min-width: 0; }
.bar-issue-icon { flex: 0 0 auto; }
.bar-error-title { color: #9e2525; font-weight: 700; line-height: 1.45; }
.bar-issue-detail { color: #344054; font-size: 13px; line-height: 1.55; }
.bar-candidate-grid { display: grid !important; grid-template-columns: repeat(auto-fit,minmax(300px,1fr)); }
.bar-candidate-card { display: flex !important; flex-direction: row !important; gap: 14px; min-width: 0; padding: 12px !important; border: 1px solid #cddbef; box-shadow: none !important; }
.bar-candidate-poster { width: 92px; min-width: 92px; height: 138px; border-radius: 9px; overflow: hidden; background: #eef1f5; }
.bar-candidate-link { color: #1769aa; font-size: 13px; font-weight: 650; }
.bar-group-summary { min-width: min(280px,100%); padding: 10px 12px !important; box-shadow: none !important; border: 1px solid var(--bar-border); }
.bar-completion-choice-row { align-items: stretch; }
.bar-completion-choice { flex: 1 1 320px; width: auto; }
.bar-movie-primary-menu { max-height: min(42vh, 280px) !important; overscroll-behavior: contain; }
.bar-movie-primary-confirm { margin-left: 2px; }
.bar-equal-filter-toggle { width: 190px; }
.bar-equal-filter-toggle .q-btn { flex: 1 1 0 !important; min-width: 0 !important; }
.bar-subtitle-contract { padding: 10px 12px; border: 1px solid #d9e3f0; border-radius: 10px; background: #f7fbff; }
.bar-path-box { padding: 10px 12px; background: #f6f7f9; border-radius: 10px; font-family: "Cascadia Code", Consolas, monospace;
  font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
.bar-evidence { color: #536078; font-size: 12px; line-height: 1.55; }
@media (max-width: 760px) {
  .bar-header { height: 58px; min-height: 58px; padding: 0 12px; }
  .bar-brand-title { font-size: 0; white-space: nowrap; }
  .bar-brand-title::after { content: "BAR"; font-size: 17px; }
  .bar-brand-subtitle { display: none; }
  .bar-header .q-badge { font-size: 0; padding-inline: 7px; }
  .bar-header .q-badge::after { content: "TEST"; font-size: 10px; }
  .bar-header-button { width: 42px; min-width: 42px !important; padding: 0 !important; }
  .bar-header-button .q-btn__content .block,
  .bar-header-button .q-btn__content > span:not(.q-icon) { display: none !important; }
  .bar-page { padding: 18px 12px 36px; }
  .bar-stat-grid { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .bar-stat-card { padding: 14px; }
  .bar-task-card { padding: 8px 11px; }
  .bar-task-card > .q-card__section, .bar-task-card .row.no-wrap { flex-wrap: wrap !important; }
  .bar-inline-progress { position: static; order: 10; width: 100%; min-width: 0;
    margin-left: 0; transform: none; display: grid !important;
    grid-template-columns: 58px minmax(100px, 1fr) 58px; gap: 12px; }
  .bar-inline-progress .bar-queue-text, .bar-inline-progress .bar-running-text {
    position: static; transform: none; grid-column: 1; }
  .bar-progress { width: 100%; min-width: 100px; grid-column: 2;
    flex: none !important; }
  .bar-review-action .q-icon { margin-right: 3px; }
  .bar-form-grid { grid-template-columns: 1fr; }
  .bar-dialog { width: 96vw !important; padding: 18px !important; }
  .bar-dialog-actions { bottom: -18px; margin: 0 -18px -18px; width: calc(100% + 36px) !important; padding: 12px 18px; }
  .bar-summary-value { max-width: 58%; }
}
@media (max-width: 900px) {
  .bar-task-header { grid-template-columns: minmax(0,1fr); row-gap: 10px; }
  .bar-task-actions { width: 100%; max-width: 100%; min-width: 0; justify-self: stretch;
    justify-content: flex-end !important; flex-basis: auto; }
  .bar-dialog-header { grid-template-columns: minmax(0,1fr) auto; }
}
"""

def is_local_gui_preview() -> bool:
    """Expose developer-only controls only in the workstation preview."""
    return os.getenv("BAR_LOCAL_GUI_PREVIEW", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def runtime_mode_label(test_mode: bool, dry_run: bool) -> str:
    if test_mode:
        return "界面测试 · 禁止执行"
    if dry_run:
        return "只读预演 · 不创建文件"
    return "真实执行已启用"


def main_page() -> None:
    ui.add_css(BAR_CSS)
    about_dialog = _build_about_dialog()
    with ui.header(wrap=False).classes("bar-header items-center justify-between"):
        with ui.row(wrap=False).classes("bar-brand items-center gap-3"):
            ui.element("div").classes("bar-live-dot")
            with ui.column().classes("gap-0"):
                ui.label("Bangumi Auto Renamer").classes("bar-brand-title")
                ui.label(f"任务与整理工作台 · v{__version__}").classes(
                    "bar-brand-subtitle"
                )
            test_mode = is_ui_test_mode()
            dry_run = is_dry_run_enabled()
            mode_label = runtime_mode_label(test_mode, dry_run)
            ui.badge(
                mode_label,
                color=("orange-8" if test_mode or dry_run else "red-7"),
            ).props("outline")

        with ui.row(wrap=False).classes("bar-header-actions items-center gap-2"):
            if is_local_gui_preview():
                try:
                    from local.development.gui_preview_controls import (
                        add_path_picker_preview_button,
                    )
                except ModuleNotFoundError:
                    pass
                else:
                    add_path_picker_preview_button()
            ui.button(
                "添加任务", icon="add", on_click=show_add_task_dialog
            ).classes("bar-header-button").props(
                'color=red-6 unelevated aria-label="添加任务"'
            )
            ui.button(
                "配置", icon="tune", on_click=config_page
            ).classes("bar-header-button").props(
                'color=blue-7 unelevated aria-label="配置"'
            )
            with ui.button("菜单", icon="menu").classes(
                "bar-header-button"
            ).props(
                'color=amber-7 text-color=white unelevated aria-label="菜单"'
            ):
                with ui.menu():
                    ui.menu_item("下载运行日志", download_log)
                    ui.menu_item(
                        "项目地址",
                        lambda: ui.navigate.to(__url__, new_tab=True),
                    )
                    ui.separator()
                    ui.menu_item("关于当前界面", about_dialog.open)

    with ui.column().classes("bar-page w-full"):
        create_table()
    ui.timer(4.0, refresh_dashboard)


def _build_about_dialog():
    with ui.dialog() as dialog, ui.card().classes("bar-dialog").style(
        "width:min(90vw,560px)!important"
    ):
        ui.label("关于任务工作台").classes("bar-dialog-title")
        ui.label(
            "本地重构版保留规则优先、AI 可选和人工确认边界；"
            "参考项目只用于信息架构与交互方式。"
        ).classes("bar-muted")
        with ui.column().classes("bar-confirm-card w-full gap-2"):
            ui.label(f"版本：{__version__}")
            ui.label("队列并发：1 个后台任务")
            ui.label(
                "当前模式："
                + runtime_mode_label(is_ui_test_mode(), is_dry_run_enabled())
            )
        with ui.row().classes("w-full justify-end"):
            ui.button("关闭", on_click=dialog.close).props("color=red-6")
    return dialog
