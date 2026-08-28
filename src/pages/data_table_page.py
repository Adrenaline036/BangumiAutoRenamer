"""Task dashboard with visible queued, running, review, and history states."""

from __future__ import annotations

from functools import partial
import json
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, Iterable, List, Mapping

from nicegui import context, ui
from nicegui.events import GenericEventArguments

from ..element.red import notify
from ..logger import logger
from ..rename.plan_review import PlanReviewService
from ..rename.plan_store import PlanStore, PlanStoreError
from ..rename.decision_state import (
    DecisionState,
    DecisionStateError,
    DecisionStateStore,
)
from ..rename.execution_recovery import (
    ExecutionRecoveryError,
    acknowledge_execution_recovery,
    abandon_catalog_update_recovery,
    load_execution_recovery,
    reconcile_catalog_update,
    requires_execution_reapproval,
)
from ..rename.work_catalog import WorkCatalog
from ..task_queue import ACTIVE_STATUSES, task_queue
from ..utils.path import (
    DECISION_STATE_PATH,
    PLAN_PATH,
    PLAN_REVIEW_PATH,
    RECORD_PATH,
    RECOVERY_ABANDON_AUDIT_PATH,
    TASK_PATH,
    WORK_CATALOG_PATH,
)
from ..utils.utils import get_task, safe_task_path
from .plan_review_page import plan_review_page
from .ui_terms import RENAME_PLAN_TERM, localize_plan_term


STATUS_TEXT = {
    "queued": "排队中",
    "running": "执行中",
    "analyzing": "分析中",
    "plan_ready": "整理计划就绪",
    "need_confirm": "待处理",
    "approved": "已批准 · 未执行",
    "invalid": "计划无效",
    "executing": "落盘中",
    "partial_failed": "部分失败",
    "completed": "已完成",
    "catalog_update_pending": "硬链接完成 · 作品登记待恢复",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}

STATUS_COLOR = {
    "queued": "amber-7",
    "running": "blue-7",
    "analyzing": "blue-7",
    "executing": "blue-7",
    "plan_ready": "green-7",
    "need_confirm": "orange-8",
    "approved": "green-7",
    "completed": "green-7",
    "invalid": "red-7",
    "failed": "red-7",
    "partial_failed": "red-7",
    "cancelled": "grey-7",
    "interrupted": "deep-orange-8",
}

ROLE_LABELS = {
    True: "动画",
    False: "非动画",
    None: "自动判断",
}

_dashboard_signatures: Dict[str, tuple[str, ...]] = {}


def _dashboard_client_key() -> str:
    """Keep timer snapshots isolated when multiple browser clients are open."""
    try:
        return str(context.client.id)
    except RuntimeError:
        return "__test__"

def task_status_text(task_data: Dict[str, Any]) -> str:
    status = task_data.get("workflow_status")
    if status in {"failed", "invalid", "partial_failed", "interrupted"}:
        return STATUS_TEXT.get(str(status), str(status))
    if task_data.get("error") and not status:
        return str(task_data["error"])
    return STATUS_TEXT.get(str(status), "状态未知")


def task_feedback_text(task_data: Mapping[str, Any]) -> str:
    """Turn internal failure details into one short operator instruction."""

    if requires_execution_reapproval(task_data):
        return "已核查，需重新审核。"
    if _is_reapproved_execution_ready(task_data):
        return "已批准，尚未执行。"
    if _is_safe_approved_retry(task_data):
        return "执行准备未完成，文件尚未处理。请稍后重新执行。"
    if _requires_execution_check(task_data):
        return "执行结果需要核查，请先确认目标文件。"
    operation = task_data.get("selection_operation")
    if isinstance(operation, Mapping) and operation.get("state") in {
        "failed", "interrupted",
    }:
        return "这次选择未生效，请打开任务重试或换一个作品。"
    reason = str(task_data.get("movie_inventory_reason") or "")
    error = str(task_data.get("error") or "")
    if reason == "MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION" or (
        "[电影资产清单]" in error
    ):
        return "需要确认作品、保存位置和正片。"
    if (
        "subtitle_video_not_content_version" in error
        or "字幕所跟随的视频还没有被确认为正片" in error
    ):
        return "需要先确认视频用途，再检查字幕。"
    return localize_plan_term(error)


def _is_reapproved_execution_ready(task_data: Mapping[str, Any]) -> bool:
    requested = str(task_data.get("execution_requested_fingerprint") or "")
    reapproved = str(task_data.get("execution_reapproval_fingerprint") or "")
    return bool(
        task_data.get("workflow_status") == "approved"
        and task_data.get("queue_operation") == "execute_approved"
        and requested == reapproved
        and len(requested) == 64
        and all(character in "0123456789abcdef" for character in requested)
    )


def _is_safe_approved_retry(task_data: Mapping[str, Any]) -> bool:
    return bool(
        task_data.get("workflow_status") == "failed"
        and task_data.get("queue_operation") == "execute_approved"
        and task_data.get("execution_phase") == "failed_before_media"
        and not task_data.get("execution_requires_manual_check")
        and str(task_data.get("execution_requested_fingerprint") or "")
    )


def _requires_execution_check(task_data: Mapping[str, Any]) -> bool:
    if requires_execution_reapproval(task_data):
        return False
    if task_data.get("execution_requires_manual_check"):
        return True
    return bool(
        task_data.get("workflow_status")
        in {"failed", "interrupted", "invalid", "partial_failed"}
        and task_data.get("queue_operation") == "execute_approved"
        and not _is_safe_approved_retry(task_data)
    )


def execution_recovery_action_hint(task_data: Dict[str, Any]) -> str:
    """Describe the post-check action state without erasing failure history."""
    if requires_execution_reapproval(task_data):
        return "已核查，需重新审核。"
    return ""


def catalog_registration_action_needed(
    task_data: Dict[str, Any], decision_state: DecisionState | None = None
) -> bool:
    """Expose the catalog repair UI for the precise missing-root failure."""
    failure = (
        decision_state.execution_failure if decision_state is not None else None
    )
    return bool(
        isinstance(failure, Mapping)
        and failure.get("code") == "catalog_root_missing"
        and failure.get("catalog_backend") in {"work", "series"}
        and failure.get("catalog_key")
    )


def task_display_title(
    task_data: Dict[str, Any], *, decision_state: DecisionState | None = None
) -> str:
    """Prefer the work explicitly confirmed by the user over search history."""

    if decision_state is not None:
        title = str(decision_state.confirmed_work.get("title") or "").strip()
        if title:
            return title
    selected = task_data.get("provider_selection")
    if isinstance(selected, dict):
        title = str(selected.get("title") or "").strip()
        if title:
            return title
    return str(
        task_data.get("name")
        or STATUS_TEXT.get(str(task_data.get("workflow_status")), "")
    )


def load_task_rows(task_root: Path = TASK_PATH) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not task_root.exists():
        return rows
    for path in sorted(
        task_root.glob("*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ):
        try:
            data = get_task(path.stem) if task_root == TASK_PATH else _read(path)
        except Exception as exc:
            logger.error(f"[任务工作台] 无法读取 {path.name}: {exc}")
            continue
        if not data:
            continue
        data = dict(data)
        stored_uuid = data.get("uuid", path.stem)
        if str(stored_uuid) != path.stem:
            logger.error(
                f"[任务工作台] 跳过 UUID 与文件名不匹配的记录：{path.name}"
            )
            continue
        data["uuid"] = path.stem
        data.setdefault("path", "未记录")
        data.setdefault("name", "等待识别")
        data.setdefault("season_id", None)
        data.setdefault("is_anime", None)
        data.setdefault("is_movie", None)
        data.setdefault("use_ai", False)
        data.setdefault("workflow_status", "failed" if data.get("error") else "completed")
        history = data.get("operation_history")
        if not isinstance(history, list):
            data["operation_history_state"] = "legacy_unknown"
            data["available_recovery_actions"] = [
                "replan_unapproved", "safe_abandon"
            ]
            data["current_authority_usable"] = False
        else:
            data.setdefault("operation_history_state", "current")
            data.setdefault("available_recovery_actions", [])
            data.setdefault(
                "current_authority_usable",
                bool(data.get("projection_receipt_fingerprint")),
            )
        rows.append(data)
    return rows


def dashboard_signature(rows: Iterable[Dict[str, Any]]) -> tuple[str, ...]:
    """Return a stable snapshot key so unchanged timers do not rebuild the UI."""
    return tuple(
        sorted(
            json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            for row in rows
        )
    )


def task_counts(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"queued": 0, "running": 0, "review": 0, "completed": 0}
    for row in rows:
        status = row.get("workflow_status")
        if status == "queued":
            counts["queued"] += 1
        elif status in {"running", "analyzing", "executing"}:
            counts["running"] += 1
        elif status in {"need_confirm", "approved", "invalid"}:
            counts["review"] += 1
        elif status == "completed":
            counts["completed"] += 1
    return counts


def sort_active_tasks(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Put the current worker first, then queued jobs in execution order."""
    running_order = {"executing": 0, "running": 1, "analyzing": 2, "queued": 3}
    return sorted(
        rows,
        key=lambda row: (
            running_order.get(str(row.get("workflow_status")), 9),
            int(row.get("queue_position") or 1_000_000),
            str(row.get("created_at") or ""),
        ),
    )


def display_folder_name(raw_path: str) -> str:
    """Return a compact final folder label for Windows or POSIX input."""
    value = str(raw_path or "").rstrip("\\/")
    if not value:
        return "未记录路径"
    return PureWindowsPath(value.replace("/", "\\")).name or value


def path_tree_parts(raw_path: str) -> List[str]:
    """Build human-readable levels for the click-open path tree."""
    value = str(raw_path or "").rstrip("\\/")
    if not value:
        return ["未记录路径"]
    path = PureWindowsPath(value.replace("/", "\\"))
    return [part for part in path.parts if part not in {"\\", "/"}]


def _read(path: Path) -> dict:
    import json

    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def create_table() -> None:
    _dashboard_signatures[_dashboard_client_key()] = dashboard_signature(
        load_task_rows()
    )
    with ui.column().classes("bar-workspace w-full"):
        _dashboard_summary()
        with ui.tabs(on_change=_refresh_inactive_sections).classes(
            "bar-tabs w-full"
        ) as tabs:
            active_tab = ui.tab("active", label="执行队列", icon="playlist_play")
            review_tab = ui.tab("review", label="待处理", icon="rule")
            history_tab = ui.tab("history", label="任务历史", icon="history")

        with ui.tab_panels(tabs, value=active_tab).classes(
            "bar-tab-panels w-full"
        ):
            with ui.tab_panel(active_tab).classes("p-0 pt-4"):
                _active_tasks()
            with ui.tab_panel(review_tab).classes("p-0 pt-4"):
                _review_tasks()
            with ui.tab_panel(history_tab).classes("p-0 pt-4"):
                _history_tasks()


@ui.refreshable
def _dashboard_summary() -> None:
    rows = load_task_rows()
    counts = task_counts(rows)
    with ui.row().classes("w-full items-end justify-between gap-4"):
        with ui.column().classes("gap-1"):
            ui.label("任务工作台").classes("bar-page-title")
            ui.label(
                "排队、执行、待处理和历史结果分区展示；每 4 秒检查，变化时更新。"
            ).classes("bar-muted")
        ui.badge(f"共 {len(rows)} 个任务", color="grey-7").props("outline")
    with ui.row().classes("bar-stat-grid w-full"):
        _stat_card("排队中", counts["queued"], "schedule", "amber")
        _stat_card("执行中", counts["running"], "autorenew", "blue")
        _stat_card("待处理", counts["review"], "fact_check", "red")
        _stat_card("已完成", counts["completed"], "task_alt", "green")


@ui.refreshable
def _active_tasks() -> None:
    rows = sort_active_tasks([
        row for row in load_task_rows()
        if row["workflow_status"] in ACTIVE_STATUSES
    ])
    _task_section(
        rows,
        empty_title="当前没有排队或执行中的任务",
        empty_hint="点击右上角“添加任务”后，任务会立刻出现在这里。",
    )


@ui.refreshable
def _review_tasks() -> None:
    rows = [
        row for row in load_task_rows()
        if row["workflow_status"] in {"need_confirm", "approved", "invalid"}
    ]
    _task_section(
        rows,
        empty_title="没有等待处理的整理任务",
        empty_hint="识别存在不确定项时会在这里暂停，不会直接落盘。",
    )


@ui.refreshable
def _history_tasks() -> None:
    rows = [
        row for row in load_task_rows()
        if row["workflow_status"] not in ACTIVE_STATUSES
        and row["workflow_status"] not in {"need_confirm", "approved", "invalid"}
    ]
    _task_section(
        rows,
        empty_title="还没有历史任务",
        empty_hint="已完成、失败或中断的任务会保留在这里。",
    )


def refresh_dashboard(*, force: bool = False) -> bool:
    """Refresh only after task data changes, preserving hover and animations."""
    client_key = _dashboard_client_key()
    signature = dashboard_signature(load_task_rows())
    if not force and signature == _dashboard_signatures.get(client_key):
        return False
    _dashboard_signatures[client_key] = signature
    _dashboard_summary.refresh()
    _active_tasks.refresh()
    _review_tasks.refresh()
    _history_tasks.refresh()
    return True


def _refresh_inactive_sections(_: GenericEventArguments) -> None:
    """Load review/history when the user changes workspace tabs."""
    _review_tasks.refresh()
    _history_tasks.refresh()


def _stat_card(label: str, value: int, icon: str, tone: str) -> None:
    with ui.card().classes(f"bar-stat-card bar-tone-{tone}"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.icon(icon).classes("bar-stat-icon")
            ui.label(str(value)).classes("bar-stat-value")
        ui.label(label).classes("bar-stat-label")


def _task_section(
    rows: List[Dict[str, Any]], *, empty_title: str, empty_hint: str
) -> None:
    if not rows:
        with ui.column().classes("bar-empty-state w-full items-center"):
            ui.icon("inbox", size="42px").classes("bar-muted")
            ui.label(empty_title).classes("text-lg font-medium")
            ui.label(empty_hint).classes("bar-muted text-center")
        return
    with ui.column().classes("w-full gap-3"):
        for row in rows:
            _task_card(row)


def _task_card(task: Dict[str, Any]) -> None:
    task_id = str(task["uuid"])
    status = str(task.get("workflow_status"))
    reapproval_required = requires_execution_reapproval(task)
    safe_approved_retry = _is_safe_approved_retry(task)
    execution_check_required = _requires_execution_check(task)
    try:
        title_authority = DecisionStateStore(DECISION_STATE_PATH).load_optional(
            task_id
        )
    except DecisionStateError as exc:
        logger.error(f"[任务工作台] 无法读取标题权威 {task_id}：{exc}")
        title_authority = None
    with ui.card().classes("bar-task-card w-full"):
        with ui.row().classes("bar-task-header w-full items-start"):
            with ui.column().classes("bar-task-content gap-1 min-w-0"):
                with ui.row().classes("bar-task-topline w-full items-center gap-2 flex-wrap"):
                    ui.badge(
                        STATUS_TEXT.get(status, status),
                        color=STATUS_COLOR.get(status, "grey-7"),
                    )
                    if task.get("preview_only"):
                        ui.badge("测试任务 · 不执行", color="orange-7").props(
                            "outline"
                        )
                    recovery_hint = execution_recovery_action_hint(task)
                    if recovery_hint:
                        ui.badge("已核查 · 需重新审核", color="orange-8").props(
                            "outline"
                        )
                    ui.label(
                        "电影"
                        if task.get("is_movie") is True
                        else ROLE_LABELS.get(task.get("is_anime"), "自动判断")
                    ).classes("bar-meta-chip")
                    if task.get("use_ai"):
                        ui.label("AI").classes("bar-meta-chip")
                    if task.get("queue_operation") == "execute_approved":
                        ui.label(f"执行已批准的{RENAME_PLAN_TERM}").classes(
                            "bar-meta-chip"
                        )
                    if status in ACTIVE_STATUSES:
                        _task_progress(task)
                if status not in ACTIVE_STATUSES:
                    ui.label(
                        task_display_title(task, decision_state=title_authority)
                    ).classes(
                        "bar-task-title"
                    )
                _path_tree_button(str(task.get("path") or ""))

            with ui.row().classes("bar-task-actions gap-2 justify-end"):
                if reapproval_required:
                    ui.button(
                        "重新审核",
                        icon="rate_review",
                        on_click=partial(_open_execution_reapproval, task_id),
                    ).classes("bar-review-action").props(
                        "unelevated color=orange-8"
                    )
                elif safe_approved_retry:
                    ui.button(
                        "重新执行",
                        icon="refresh",
                        on_click=partial(_retry_approved_execution, task_id),
                    ).classes("bar-review-action").props(
                        "unelevated color=green-7"
                    )
                elif execution_check_required:
                    ui.button(
                        "核查",
                        icon="fact_check",
                        on_click=partial(_open_execution_recovery, task_id),
                    ).classes("bar-review-action").props(
                        "unelevated color=deep-orange-8"
                    ).tooltip("查看历史执行记录并确认处理方式")
                elif status == "queued":
                    ui.button(
                        "删除",
                        icon="delete",
                        on_click=partial(_delete_task, task_id),
                    ).classes("bar-review-action bar-queue-delete").props(
                        'unelevated color=red-7 aria-label="删除排队任务"'
                    ).tooltip("删除排队任务")
                elif status in {"need_confirm", "approved", "invalid"}:
                    ui.button(
                        "处理", icon="construction", on_click=partial(_open_review, task_id)
                    ).classes("bar-review-action").props("unelevated color=orange-8")
                    ui.button(
                        "重试",
                        icon="refresh",
                        on_click=partial(_retry_task, task_id),
                    ).classes("bar-review-action").props("unelevated color=green-7")
                    ui.button(
                        "删除",
                        icon="delete",
                        on_click=partial(_delete_task, task_id),
                    ).classes("bar-review-action").props("unelevated color=red-7")
                elif status not in ACTIVE_STATUSES:
                    registration_action_needed = catalog_registration_action_needed(
                        task, title_authority
                    )
                    if registration_action_needed:
                        ui.button(
                            "处理",
                            icon="folder_off",
                            on_click=partial(_open_review, task_id),
                        ).classes("bar-review-action").props(
                            'unelevated color=orange-8 aria-label="处理失效的作品登记"'
                        ).tooltip("选择原目录已移动或原目录已删除")
                    else:
                        ui.button(
                            "处理", icon="construction", on_click=partial(_open_review, task_id)
                        ).classes("bar-review-action").props(
                            'unelevated color=orange-8 aria-label="处理整理任务"'
                        ).tooltip("查看问题、修改设置或重新识别")
                    ui.button(
                        "重试", icon="refresh", on_click=partial(_retry_task, task_id)
                    ).classes("bar-review-action").props(
                        'unelevated color=green-7 aria-label="重新入队"'
                    ).tooltip("重新入队")
                    ui.button(
                        "删除", icon="delete", on_click=partial(_delete_task, task_id)
                    ).classes("bar-review-action").props(
                        'unelevated color=red-7 aria-label="删除任务记录"'
                    ).tooltip("删除任务记录")

        if task.get("error"):
            feedback_classes = (
                "bar-task-feedback bar-recovery-ready"
                if _is_reapproved_execution_ready(task)
                else "bar-task-feedback bar-error-text"
            )
            ui.label(task_feedback_text(task)).classes(
                feedback_classes
            )
        if task.get("plan_rebase_message"):
            ui.label(localize_plan_term(task["plan_rebase_message"])).classes(
                "bar-task-feedback text-blue-8"
            )
        if execution_check_required:
            ui.label(
                "目标目录可能已有执行结果，请核查后再选择处理方式。"
            ).classes("bar-task-feedback bar-error-text")
        elif recovery_hint:
            ui.label(localize_plan_term(recovery_hint)).classes(
                "bar-task-feedback bar-recovery-ready"
            )
        if task.get("plan_entry_count") is not None:
            ui.label(
                f"{RENAME_PLAN_TERM} {task['plan_entry_count']} 项"
            ).classes("bar-task-footnote bar-help")


def _task_progress(task: Dict[str, Any]) -> None:
    status = task.get("workflow_status")
    if status == "queued":
        position = task.get("queue_position")
        text = f"队列 {position}" if position else "等待调度"
        with ui.row(wrap=False).classes("bar-inline-progress items-center gap-3"):
            ui.label(text).classes("bar-queue-text")
            ui.linear_progress(value=0, show_value=False).props(
                "color=amber-7 track-color=amber-2"
            ).classes("bar-progress grow")
    elif status in {"running", "analyzing", "executing"}:
        with ui.row(wrap=False).classes("bar-inline-progress items-center gap-3"):
            ui.label("处理中").classes("bar-running-text")
            ui.linear_progress(value=0, show_value=False).props(
                "indeterminate color=blue-7 track-color=blue-2"
            ).classes("bar-progress grow")


def _path_tree_button(raw_path: str) -> None:
    """Show only the leaf in the card and reveal a folder tree on click."""
    parts = path_tree_parts(raw_path)
    with ui.button(display_folder_name(raw_path), icon="folder_open").classes(
        "bar-path-trigger"
    ).props("flat no-caps dense"):
        with ui.menu().classes("bar-path-menu"):
            ui.label("来源路径").classes("bar-field-label px-3 pt-2")
            with ui.column().classes("gap-0 px-2 pb-2"):
                for depth, part in enumerate(parts):
                    with ui.row(wrap=False).classes("items-center gap-2 bar-path-level").style(
                        f"padding-left:{depth * 14}px"
                    ):
                        ui.icon("folder" if depth < len(parts) - 1 else "folder_open", size="17px")
                        ui.label(part).classes("bar-path-tree-label")


async def _open_review(task_id: str) -> None:
    data = get_task(task_id)
    if requires_execution_reapproval(data):
        notify("该任务需要重新审核，请使用“重新审核”", type="warning")
        return
    if data.get("execution_requires_manual_check"):
        notify("请先打开“核查”并确认历史记录与目标目录", type="warning")
        return
    await plan_review_page(task_id)
    _dashboard_summary.refresh()
    _review_tasks.refresh()
    _history_tasks.refresh()


async def _retry_task(task_id: str) -> None:
    data = get_task(task_id)
    if not data:
        notify("任务记录不存在", type="negative")
        return
    if requires_execution_reapproval(data):
        notify("该任务需要重新审核，不能直接重试", type="warning")
        return
    if data.get("execution_requires_manual_check"):
        notify("请先打开“核查”并确认历史记录与目标目录", type="warning")
        return
    await task_queue.retry_current(task_id, source="retry")
    notify(f"任务 {task_id[:8]} 已重新加入队列", type="positive")
    refresh_dashboard()
    _review_tasks.refresh()
    _history_tasks.refresh()


async def _retry_approved_execution(task_id: str) -> None:
    data = get_task(task_id)
    if not data:
        notify("任务记录不存在", type="negative")
        return
    if not _is_safe_approved_retry(data):
        notify("当前状态需要先核查，不能直接重新执行", type="warning")
        refresh_dashboard()
        return
    fingerprint = str(data.get("execution_requested_fingerprint") or "")
    await task_queue.enqueue_approved(task_id, fingerprint)
    notify("已重新加入执行队列", type="positive")
    refresh_dashboard()
    _review_tasks.refresh()
    _history_tasks.refresh()


async def _delete_task(task_id: str) -> None:
    task_data = get_task(task_id)
    if requires_execution_reapproval(task_data):
        notify("该任务需要重新审核，不能直接删除", type="warning")
        return
    if task_data.get("execution_requires_manual_check"):
        notify("核查完成前不能直接删除任务和历史记录", type="warning")
        return
    if task_data.get("workflow_status") == "queued":
        if not task_queue.cancel_queued(task_id):
            notify("任务已开始执行，不能再从排队列表删除", type="warning")
            refresh_dashboard()
            return
    try:
        task_queue.abandon_and_remove(
            task_id,
            record_root=RECORD_PATH,
            plan_root=PLAN_PATH,
            review_root=PLAN_REVIEW_PATH,
        )
    except (PlanExecutionError, ValueError) as exc:
        notify(localize_plan_term(exc), type="negative")
        return
    notify(f"已删除任务记录 {task_id[:8]}", type="positive")
    refresh_dashboard()
    _review_tasks.refresh()
    _history_tasks.refresh()


async def _open_execution_reapproval(task_id: str) -> None:
    task_data = get_task(task_id)
    if not requires_execution_reapproval(task_data):
        notify("任务状态已变化，请刷新后再处理", type="warning")
        refresh_dashboard()
        return
    await plan_review_page(task_id)
    _dashboard_summary.refresh()
    _review_tasks.refresh()
    _history_tasks.refresh()


async def _open_execution_recovery(task_id: str) -> None:
    task_data = get_task(task_id)
    if not task_data:
        notify("任务记录不存在", type="negative")
        return
    snapshot = load_execution_recovery(task_id, RECORD_PATH)
    with ui.dialog() as dialog, ui.card().classes("bar-dialog max-w-2xl"):
        ui.label("执行结果人工核查").classes("bar-dialog-title")
        if task_data.get("execution_phase") == "media_boundary_entered":
            ui.label(
                "本次整理可能已经处理部分文件，请先核对目标目录。"
            ).classes("bar-error-text")
        else:
            ui.label(
                "无法确认上次是否进入文件操作阶段，请先核对目标目录。"
            ).classes("bar-error-text")
        with ui.column().classes("w-full gap-2"):
            if snapshot.ledger_status != "missing":
                ui.label("已找到历史执行记录").classes("bar-help")
                status_text = {
                    "completed": "已完成",
                    "running": "未完成",
                    "failed": "未完成",
                }.get(snapshot.ledger_status, "需要核查")
                ui.label(f"记录状态：{status_text}").classes("bar-help")
                ui.label(f"记录条目：{snapshot.entry_count}").classes(
                    "bar-help"
                )
            else:
                ui.label("未找到可用的历史执行记录").classes("bar-help")
            if snapshot.error:
                ui.label("历史执行记录无法完整读取，请谨慎核查目标文件。").classes(
                    "bar-error-text"
                )
            ui.label(
                "请在文件系统中核对目标文件是否存在，并与历史记录逐项比对。"
                "只有完成核查后才点击下方确认。"
            ).classes("bar-muted")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("关闭", on_click=dialog.close).props("flat")
            if task_data.get("execution_status") == "catalog_update_pending":
                ui.button(
                    "恢复作品登记",
                    icon="sync",
                    on_click=partial(_reconcile_catalog_update, dialog, task_id),
                ).props("unelevated color=blue-8")
                ui.button(
                    "放弃恢复",
                    icon="remove_circle_outline",
                    on_click=partial(
                        _confirm_abandon_catalog_update,
                        dialog,
                        task_id,
                        snapshot.plan_fingerprint,
                    ),
                ).props("flat color=red-7")
            else:
                ui.button(
                    "我已完成核查",
                    icon="verified_user",
                    on_click=partial(_acknowledge_execution_recovery, dialog, task_id),
                ).props("unelevated color=deep-orange-8")
    dialog.open()
    await dialog


def _acknowledge_execution_recovery(dialog: ui.dialog, task_id: str) -> None:
    try:
        acknowledge_execution_recovery(task_id, TASK_PATH)
    except ExecutionRecoveryError as exc:
        notify(localize_plan_term(exc), type="negative")
        return
    dialog.submit(True)
    notify("已核查，需重新审核", type="positive")
    refresh_dashboard()
    _review_tasks.refresh()
    _history_tasks.refresh()


def _reconcile_catalog_update(dialog: ui.dialog, task_id: str) -> None:
    try:
        reconcile_catalog_update(
            task_id,
            task_root=TASK_PATH,
            record_root=RECORD_PATH,
            plan_store=PlanStore(PLAN_PATH),
            work_catalog=WorkCatalog(WORK_CATALOG_PATH),
        )
    except ExecutionRecoveryError as exc:
        notify(localize_plan_term(exc), type="negative")
        return
    dialog.submit(True)
    notify("作品登记已从历史记录恢复，任务现已完成", type="positive")
    refresh_dashboard()
    _review_tasks.refresh()
    _history_tasks.refresh()


def _confirm_abandon_catalog_update(
    recovery_dialog: ui.dialog,
    task_id: str,
    fingerprint: str,
) -> None:
    with ui.dialog() as confirm, ui.card().classes("bar-dialog max-w-md"):
        ui.label("放弃恢复？").classes("bar-dialog-title")
        ui.label(
            "只会从任务列表移除此任务。不会修改媒体文件或作品登记，"
            "操作记录仍会保留。"
        ).classes("bar-help")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("返回", on_click=confirm.close).props("flat")
            ui.button(
                "移除任务",
                icon="delete_outline",
                on_click=partial(
                    _abandon_catalog_update,
                    confirm,
                    recovery_dialog,
                    task_id,
                    fingerprint,
                ),
            ).props("unelevated color=red-7")
    confirm.open()


def _abandon_catalog_update(
    confirm: ui.dialog,
    recovery_dialog: ui.dialog,
    task_id: str,
    fingerprint: str,
) -> None:
    try:
        abandon_catalog_update_recovery(
            task_id,
            task_root=TASK_PATH,
            record_root=RECORD_PATH,
            plan_store=PlanStore(PLAN_PATH),
            review_root=PLAN_REVIEW_PATH,
            decision_state_root=DECISION_STATE_PATH,
            audit_root=RECOVERY_ABANDON_AUDIT_PATH,
            expected_plan_fingerprint=fingerprint,
        )
    except ExecutionRecoveryError as exc:
        notify(localize_plan_term(exc), type="negative")
        return
    confirm.submit(True)
    recovery_dialog.submit(True)
    notify(
        "任务已移除；媒体文件和作品登记未修改，操作记录已保留。",
        type="positive",
    )
    refresh_dashboard()
    _review_tasks.refresh()
    _history_tasks.refresh()


# Compatibility wrappers retained for older event-slot callers and tests.
async def handle_edit(ev: GenericEventArguments) -> None:
    await _open_review(ev.args["row"]["uuid"])


async def handle_review(ev: GenericEventArguments) -> None:
    await _open_review(ev.args["row"]["uuid"])


async def handle_retry(ev: GenericEventArguments) -> None:
    await _retry_task(ev.args["row"]["uuid"])


async def handle_delete(ev: GenericEventArguments, is_notify: bool = True) -> None:
    await _delete_task(ev.args["row"]["uuid"])
