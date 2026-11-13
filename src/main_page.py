"""Application shell for the BAR task and review workspace."""

from __future__ import annotations

from src.component.dialog_frame import dialog_card

from nicegui import ui

from .__version__ import __url__, __version__
from .pages.add_task_page import show_add_task_dialog
from .pages.library_assets_view import show_library_assets
from .pages.config_page import config_page
from .pages.data_table_page import create_table, refresh_dashboard
from .pages.download_log import download_log
from .pages.recovery_records_page import show_recovery_records
from .rename.trans import is_dry_run_enabled


ui.dialog.default_props('transition-show=scale transition-hide=scale transition-duration=240')
ui.button.default_props('no-caps unelevated')

BAR_CSS = r"""
.bar-review-section {border: 1px solid #e1e6ef; border-radius: 14px; padding: 20px; min-width: 0;}
.bar-review-feedback {overflow-wrap: anywhere;}
.bar-review-feedback:has(> .bar-help:only-child:empty) {display: none;}
.bar-review-season {border-top: 1px solid #e1e6ef; padding-top: 16px;}
.bar-season-fields {display: grid; grid-template-columns: max-content minmax(0, 1fr) minmax(120px, 0.7fr) max-content; gap: 12px;}
.bar-season-fields > * {min-width: 0;}
.bar-review-picker {gap: 8px;}
.bar-review-dialog .bar-path-text {overflow-wrap: anywhere;}
@media (max-width: 640px) {
  .bar-season-fields {grid-template-columns: minmax(0, 1fr);}
  .bar-review-section {padding: 12px;}
}

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
.bar-task-activity { flex: 0 0 auto; white-space: nowrap; }
.bar-path-trigger { align-self: flex-start; min-height: 28px !important; padding: 2px 6px !important;
  color: #485267 !important; font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; }
.bar-path-trigger .q-icon { color: #7a8496; }
.bar-task-actions { width: 394px; max-width: 394px; min-width: 394px; align-self: start;
  justify-self: end; flex: 0 0 394px; flex-wrap: wrap !important; }
.bar-task-card-active .bar-task-actions { align-self: center; }
.bar-task-actions .q-btn { flex: 0 0 auto; }
.bar-task-feedback { display: block; width: 100%; max-width: none; margin-top: 8px;
  box-sizing: border-box; }
.bar-task-footnote { display: block; width: 100%; margin-top: 6px; }
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
.bar-file-picker-grid { flex: 1 1 auto; min-height: 0; height: 100%;
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
  padding: 24px !important; max-height: 90vh; overflow: hidden; flex-wrap: nowrap !important; gap: 18px !important; }
.bar-task-wizard { width: min(92vw, 760px) !important; }
.bar-dialog-title { font-size: 24px; font-weight: 850; letter-spacing: -.035em; }
.bar-dialog-header { display: grid !important; grid-template-columns: minmax(0,1fr) auto;
  column-gap: 16px; align-items: start; }
.bar-dialog-header-content { min-width: 0; }
.bar-dialog-header-actions { min-width: max-content; justify-self: end; flex: 0 0 auto; }
.bar-dialog-actions { width: 100%; gap: 8px; }
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
.bar-review-dialog { width: min(96vw, 1480px) !important; max-height: min(92vh, 980px); overflow: hidden; }
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
.bar-provider-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); align-items: start; }
.bar-provider-column { min-width: 0; width: 100%; overflow-wrap: anywhere; }
.bar-candidate-card { display: flex !important; flex-direction: row !important; gap: 14px; min-width: 0; padding: 12px !important; border: 1px solid #cddbef; box-shadow: none !important; }
.bar-candidate-poster { width: 92px; min-width: 92px; height: 138px; border-radius: 9px; overflow: hidden; background: #eef1f5; }
.bar-candidate-link { color: #1769aa; font-size: 13px; font-weight: 650; }
.bar-group-summary { min-width: min(280px,100%); padding: 10px 12px !important; box-shadow: none !important; border: 1px solid var(--bar-border); }
.bar-completion-choice-row { align-items: stretch; }
.bar-completion-choice { flex: 1 1 320px; width: auto; }
.bar-movie-primary-menu { max-height: min(42vh, 280px) !important; overscroll-behavior: contain; }
.bar-movie-primary-confirm { margin-left: 2px; }
.bar-equal-filter-toggle { width: 224px; flex-shrink: 0; border: 1px solid #1e88e5; border-radius: 9px; overflow: hidden; box-shadow: none; }
.bar-equal-filter-toggle .q-btn:not(.q-btn--round) {
  flex: 1 1 0 !important; min-width: 0 !important; border-radius: 0;
  height: 38px; min-height: 38px; padding: 0 8px !important;
}
.bar-equal-filter-toggle .q-btn__content { flex-wrap: nowrap; white-space: nowrap; }
.bar-subtitle-contract { padding: 10px 12px; border: 1px solid #d9e3f0; border-radius: 10px; background: #f7fbff; }
.bar-path-box { padding: 10px 12px; background: #f6f7f9; border-radius: 10px; font-family: "Cascadia Code", Consolas, monospace;
  font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
.bar-evidence { color: #536078; font-size: 12px; line-height: 1.55; }
@media (max-width: 760px) {
  .bar-provider-grid { grid-template-columns: minmax(0,1fr); }
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
  .bar-progress { width: 100%; min-width: 100px; grid-column: 2;
    flex: none !important; }
  .bar-review-action .q-icon { margin-right: 3px; }
  .bar-form-grid { grid-template-columns: 1fr; }
  .bar-dialog { width: 96vw !important; padding: 18px !important; }

  .bar-summary-value { max-width: 58%; }
}
@media (max-width: 900px) {
  .bar-task-header { grid-template-columns: minmax(0,1fr); row-gap: 10px; }
  .bar-task-actions { width: 100%; max-width: 100%; min-width: 0; justify-self: stretch;
    justify-content: flex-end !important; flex-basis: auto; }
  .bar-dialog-header { grid-template-columns: minmax(0,1fr) auto; }
}
"""

def runtime_mode_label(dry_run: bool) -> str:
    if dry_run:
        return "只读预演 · 不创建文件"
    return "文件整理须批准后执行"


BAR_CSS += r"""
/* One visual language across all dialogs and equivalent actions. */
.q-btn:not(.q-btn--round) { border-radius: 9px; min-height: 38px; font-weight: 700; }
.q-btn:not(.q-btn--round):not(.q-btn--dense) { min-width: 88px; padding: 0 14px; font-size: 14px; }
.q-icon.material-icons { font-family: "Material Icons Outlined"; }
.bar-config-row { display: grid; grid-template-columns: 170px minmax(0, 1fr); gap: 18px; align-items: center; }
.bar-config-label { gap: 8px; white-space: nowrap; }
.bar-config-input, .bar-config-toggle { min-width: 0; }
.bar-config-toggle { display: flex; overflow: hidden; border-radius: 9px; }
.bar-config-toggle .q-btn:not(.q-btn--round) { flex: 1 1 0 !important; min-width: 0 !important; border-radius: 0 !important; padding: 0 6px !important; }
.bar-config-toggle .q-btn__content { flex-wrap: nowrap; }
.bar-config-toggle .q-icon { font-size: 18px; margin-right: 6px; }
@media (max-width: 640px) {
  .bar-config-row { grid-template-columns: minmax(0, 1fr); gap: 8px; }
  .bar-config-input .q-field__native { font-size: 16px; }
}
@media (max-width: 420px) {
  .bar-config-toggle .q-btn { font-size: 12px !important; }
  .bar-config-toggle .q-btn__content { flex-direction: column; gap: 2px; padding: 4px 0; }
  .bar-config-toggle .q-icon { margin-right: 0; }
}
.q-btn .q-icon { font-size: 20px; }
.q-dialog__inner > .q-card { border-radius: 22px !important; padding: 24px; max-height: 90vh; }
/* Scroll tracks belong to the body, clear of the rounded outer frame. */
.scroll { scrollbar-width: thin; scrollbar-color: #cbd1dc transparent; }
.scroll::-webkit-scrollbar { width: 8px; height: 8px; }
.scroll::-webkit-scrollbar-thumb { background: #cbd1dc; border: 2px solid white; border-radius: 8px; }
.q-dialog__inner > .bar-modal-fixed {
 display: flex; flex-direction: column; flex-wrap: nowrap; align-items: stretch;
 overflow: hidden !important; padding: 0 !important; gap: 0 !important;
 max-height: min(92dvh, 980px) !important; max-width: calc(100vw - 32px) !important;
}
.bar-modal-header { flex: 0 0 auto; padding: 20px 24px 12px; gap: 8px; min-width: 0; }
.bar-modal-body { flex: 1 1 auto; min-height: 0; min-width: 0; width: 100%;
 overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable;
 padding: 12px 24px; box-sizing: border-box; gap: 18px; overflow-wrap: anywhere; }
.bar-modal-body > * { flex-shrink: 0; max-width: 100%; }
.bar-modal-footer { flex: 0 0 auto; padding: 12px 24px; border-top: 1px solid var(--bar-border);
 background: white; min-width: 0; }
.bar-modal-footer > .bar-dialog-actions, .bar-modal-footer > .bar-plan-actions {
 position: static; margin: 0; padding: 0; width: 100% !important; border: 0; }
.bar-source-groups { max-height: none; overflow: visible; padding-right: 0; }
.bar-plan-actions { width: 100%; }
.bar-plan-tools { align-items: stretch; }
.bar-plan-tools > .q-btn { flex: 1 1 180px; }
.bar-plan-tools > .nicegui-column { flex: 1 1 180px; }
.bar-plan-tools > .nicegui-column:empty { display: none; }
.bar-plan-tools > .nicegui-column .q-btn { flex: 1 1 auto; }
.bar-content-dialog { width: min(96vw, 1480px) !important; }
.bar-content-columns { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr); gap: 24px; min-height: 0; }
.bar-content-pane { min-width: 0; min-height: 0; }
.bar-content-files { height: 600px; flex-shrink: 0; gap: 0; overflow-y: auto; padding-right: 8px; scrollbar-gutter: stable; }
.bar-content-files > .nicegui-column { height: 60px; min-height: 60px; flex-shrink: 0; justify-content: center; }
.bar-content-files .q-checkbox__label { min-width: 0; line-height: 20px; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }
.bar-regroup-dialog { width: min(95vw, 1280px) !important; }
.bar-regroup-dialog .q-table__container { box-shadow: none; }
@media(max-width: 760px) {
 .bar-content-columns { grid-template-columns: minmax(0,1fr); }
 .bar-content-files { height: auto; overflow: visible; padding-right: 0; }
 .bar-modal-header { padding: 16px 16px 10px; }
 .bar-modal-body { padding: 10px 16px; }
 .bar-modal-footer { padding: 10px 16px; }
 .bar-modal-header .bar-dialog-header { display: flex !important; flex-wrap: wrap; gap: 8px; }
 .bar-dialog-header-content { flex: 1 1 auto; }
 .bar-dialog-title { white-space: normal; overflow-wrap: anywhere; }
 .bar-dialog-header-actions { min-width: 0; max-width: 100%; }
}
"""

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
            dry_run = is_dry_run_enabled()
            mode_label = runtime_mode_label(dry_run)
            ui.badge(
                mode_label,
                color=("orange-8" if dry_run else "red-7"),
            ).props("outline")

        with ui.row(wrap=False).classes("bar-header-actions items-center gap-2"):
            ui.button(
                "添加任务", icon='o_add', on_click=show_add_task_dialog
            ).classes("bar-header-button").props(
                'color=red-6 unelevated aria-label="添加任务"'
            )
            ui.button(
                "配置", icon='o_tune', on_click=config_page
            ).classes("bar-header-button").props(
                'color=blue-7 unelevated aria-label="配置"'
            )
            with ui.button("菜单", icon='o_menu').classes(
                "bar-header-button"
            ).props(
                'color=amber-7 text-color=white unelevated aria-label="菜单"'
            ):
                with ui.menu().classes("min-w-48 whitespace-nowrap"):
                    with ui.menu_item("下载运行日志", download_log) as item:
                        ui.icon('o_download', size='20px').classes('mr-3').move(item, target_index=0)
                    with ui.menu_item("媒体库", show_library_assets) as item:
                        ui.icon('o_video_library', size='20px').classes('mr-3').move(item, target_index=0)
                    with ui.menu_item("恢复记录", show_recovery_records) as item:
                        ui.icon('o_restore', size='20px').classes('mr-3').move(item, target_index=0)
                    with ui.menu_item(
                        "项目地址",
                        lambda: ui.navigate.to(__url__, new_tab=True),
                    ) as item:
                        ui.icon('o_open_in_new', size='20px').classes('mr-3').move(item, target_index=0)
                    ui.separator()
                    with ui.menu_item("关于当前界面", about_dialog.open) as item:
                        ui.icon('o_info', size='20px').classes('mr-3').move(item, target_index=0)

    with ui.column().classes("bar-page w-full"):
        create_table()
    from .utils.task_changes import DashboardRefresh
    # NiceGUI owns this timer with its client; no global subscriber survives it.
    ui.timer(0.2, DashboardRefresh(refresh_dashboard).poll)


def _build_about_dialog():
    with ui.dialog() as dialog, dialog_card().classes("bar-dialog").style(
        "width:min(90vw,560px)!important"
    ):
        ui.label("关于任务工作台").classes("bar-dialog-title")
        ui.label(
            "本地重构版保留规则优先和人工确认边界；"
            "参考项目只用于信息架构与交互方式。"
        ).classes("bar-muted")
        with ui.column().classes("bar-confirm-card w-full gap-2"):
            ui.label(f"版本：{__version__}")
            ui.label("队列并发：1 个后台任务")
            ui.label(
                "当前模式："
                + runtime_mode_label(is_dry_run_enabled())
            )
        with ui.row().classes("w-full justify-end").classes('bar-modal-footer'):
            ui.button("关闭", on_click=dialog.close, icon='o_close').props("color=red-6")
    return dialog
