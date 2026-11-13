"""Task dashboard with visible queued, running, review, and history states."""

from __future__ import annotations

from src.component.icons import outlined_icon
from ..queue_runtime import ui_disk_call
from ..queue_runtime import disk_call

from src.component.dialog_frame import dialog_card

from functools import partial
import asyncio
import json
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, Iterable, List, Mapping

from nicegui import context, ui, run
from nicegui.events import GenericEventArguments

from ..element.red import notify
from ..rename.replacement_completion import has_completed_media
from ..logger import logger
from ..rename.plan_review import PlanReviewService
from ..rename.plan_store import PlanStore, PlanStoreError
from ..rename.review_operation import (
    build_review_operation,
    provider_selection_awaits_user,
)
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
    requires_execution_replan,
    safe_approved_execution_retry,
)
from ..rename.work_catalog import WorkCatalog
from ..rename.series_catalog import SeriesCatalog
from ..rename.provider_link_catalog import ProviderLinkCatalog
from ..task_queue import ACTIVE_STATUSES, task_queue
from ..utils.path import (
    DECISION_STATE_PATH,
    PLAN_PATH,
    PLAN_REVIEW_PATH,
    RECOVERY_TRANSACTION_PATH,
    RECORD_PATH,
    RECOVERY_ABANDON_AUDIT_PATH,
    TASK_PATH,
    WORK_CATALOG_PATH,
    SERIES_CATALOG_PATH,
    PROVIDER_LINK_CATALOG_PATH,
)
from ..utils.utils import get_task, safe_task_path, read_task_file
from .plan_review_page import plan_review_page
from .ui_terms import RENAME_PLAN_TERM, localize_plan_term


STATUS_TEXT = {
    "queued": "排队中",
    "running": "处理中",
    "analyzing": "分析中",
    "plan_ready": "整理计划就绪",
    "need_confirm": "待处理",
    "approved": "已批准 · 未执行",
    "invalid": "计划无效",
    "executing": "文件整理中",
    "partial_failed": "部分失败",
    "completed": "已完成",
    "catalog_update_pending": "硬链接完成 · 作品登记待恢复",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
    "rolled_back": "已回滚",
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
    "rolled_back": "blue-grey-7",
}

_dashboard_signatures: Dict[str, tuple[str, ...]] = {}
_dashboard_reads: dict[str, asyncio.Task] = {}
_dashboard_views = {}
_dashboard_rows = {}
_deleting_tasks = set()
_UNREAD = object()


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


def can_offer_asset_rollback(task, decision):
    operation=getattr(decision,'asset_operation',None)
    if operation is None: return False
    value=operation.to_dict()
    from src.rename.replacement_completion import uses_completion_policy
    if uses_completion_policy(value) or task.get('asset_rollback_phase') not in {'rollback_prepared', 'incomplete'}:
        return False
    return (value.get('policy') in {'asset-operation-v2','asset-operation-v3'} and value.get('mode')=='replace'
            and not task.get('preview_only')
            and task.get('workflow_status') in {
                'completed','partial_failed','failed','catalog_update_pending','interrupted','rolled_back'}
            and (task.get('execution_status') in {'completed','failed','rolled_back','catalog_update_pending'}
                 or bool(task.get('asset_rollback_phase'))))


def task_media_label(task_data: Mapping[str, Any]) -> str:
    """Render the frozen library domain and presentation as one card label."""

    is_anime = task_data.get("is_anime")
    is_movie = task_data.get("is_movie")
    labels = {
        (True, False): "动画剧集",
        (True, True): "动画电影",
        (False, False): "非动画剧集",
        (False, True): "电影",
    }
    return labels.get((is_anime, is_movie), "自动判断")


def task_feedback_text(task_data: Mapping[str, Any]) -> str:
    """Turn internal failure details into one short operator instruction."""

    if requires_execution_reapproval(task_data):
        return "已核查，需重新审核。"
    if _is_reapproved_execution_ready(task_data):
        return "已批准，尚未执行。"
    if requires_execution_replan(task_data):
        return "目标或作品登记已变化，原计划不能重新执行。请重新生成整理计划并重新批准。"
    if _is_safe_approved_retry(task_data):
        return "执行准备未完成，文件尚未处理。请稍后重新执行。"
    if _requires_execution_check(task_data):
        return "执行结果需要核查，请先确认目标文件。"
    if provider_selection_awaits_user(task_data):
        return "作品关系仍需确认，请打开任务继续或安全移除。"
    operation, _unused = build_review_operation(task_data)
    if operation.context.get("lifecycle") in {
        "failed", "interrupted", "legacy_stale",
    }:
        from ..rename.provider_recovery import transport_failure_text, can_refresh_selection
        reason = transport_failure_text(operation.context)
        if operation.context.get("failure_code") == "internal_busy" and reason:
            return reason
        if can_refresh_selection(operation.context):
            if operation.context.get('failure_code') == 'selection_failed':
                return '本次处理未完成，所选作品已保留，请打开原任务重新读取资料。'
            return (reason or '作品资料暂时无法读取。') + '所选作品已保留，请打开原任务重新读取资料。'
        if reason:
            return reason
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
    return safe_approved_execution_retry(task_data)


def _requires_execution_check(task_data: Mapping[str, Any]) -> bool:
    if requires_execution_replan(task_data):
        return False
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


def load_task_rows(task_root: Path = TASK_PATH, *, _paths=None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not task_root.exists():
        return rows
    timed_paths = []
    for path in (task_root.glob("*.json") if _paths is None else _paths):
        try:
            timed_paths.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            continue
    for _, path in sorted(timed_paths, key=lambda item: item[0], reverse=True):
        data = get_task(path.stem) if task_root == TASK_PATH else read_task_file(path)
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
            json.dumps(row if '_display' in row else _card_projection(row),
                       ensure_ascii=False, sort_keys=True, default=str)
            for row in rows
        )
    )


def task_counts(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"queued": 0, "running": 0, "review": 0, "completed": 0}
    for row in rows:
        status = row.get("workflow_status")
        if status in {"running", "analyzing", "executing"}:
            counts["running"] += 1
        elif row.get('_selection_wait') or status == "queued":
            counts["queued"] += 1
        elif row.get('_selection_review') or status in {"need_confirm", "approved", "invalid"}:
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


def create_table():
    from .dashboard_view import DashboardView
    client = context.client
    key = client.id
    _dashboard_views[key] = DashboardView(render_card=_task_card, refresh=refresh_dashboard)
    def cleanup():
        _dashboard_views.pop(key, None)
        _dashboard_rows.pop(key, None)
        _dashboard_signatures.pop(key, None)
    client.on_disconnect(cleanup)
    return refresh_dashboard(force=True)


def _display_task_rows(observed=None):
    return load_task_rows() if observed is None else observed


def _card_projection(task, decision=None):
    # 这些字段仅用于显示；按钮仅绑定任务 ID，操作入口重新读取并校验权威。
    operation, _ = build_review_operation(task)
    selection_finished = (operation.context.get('kind') == 'provider_selection'
        and operation.context.get('lifecycle') in {'failed', 'awaiting_user', 'awaiting_season', 'interrupted'})
    row = {key: task.get(key) for key in (
        'uuid', 'workflow_status', 'preview_only', 'queue_operation', 'path',
        'queue_position', 'created_at', 'is_anime', 'is_movie', 'plan_entry_count',
        'plan_rebase_message', 'plan_fingerprint')}
    row['_selection_finished'] = selection_finished
    row['_selection_request_id'] = operation.context.get('request_id')
    if selection_finished:
        row['workflow_status'] = 'need_confirm'
        row['queue_position'] = None
    row['_display'] = dict(
        title=task_display_title(task, decision_state=decision),
        reapproval=requires_execution_reapproval(task),
        retry=_is_safe_approved_retry(task), check=_requires_execution_check(task),
        replan=requires_execution_replan(task), rollback_restart=_needs_rollback_restart(task),
        rollback=can_offer_asset_rollback(task, decision),
        registration=catalog_registration_action_needed(task, decision),
        recovery_hint=execution_recovery_action_hint(task),
        reapproved=_is_reapproved_execution_ready(task),
        feedback=task_feedback_text(task) if task.get('error') else '')
    return row


_card_cache = {}
from threading import RLock
_card_cache_lock = RLock()


def _display_file_stamp(path):
    try:
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
    except FileNotFoundError:
        return None


def _cached_dashboard_rows():
    # 只缓存卡片投影；批准、执行和冲突校验不读取此缓存。
    from copy import deepcopy
    with _card_cache_lock:
        rows, live = [], set()
        for path in TASK_PATH.glob('*.json'):
            decision_path = DECISION_STATE_PATH / path.name
            key = (str(path.absolute()), str(decision_path.absolute()))
            live.add(key)
            stamp = (_display_file_stamp(path), _display_file_stamp(decision_path))
            cached = _card_cache.get(key)
            if cached is not None and cached[0] == stamp:
                row = deepcopy(cached[1])
            else:
                tasks = load_task_rows(TASK_PATH, _paths=[path])
                if not tasks:
                    _card_cache.pop(key, None)
                    continue
                row = _card_projection(tasks[0], _read_card_decision(path.stem))
                after = (_display_file_stamp(path), _display_file_stamp(decision_path))
                if after == stamp:
                    _card_cache[key] = (stamp, deepcopy(row))
                else:
                    _card_cache.pop(key, None)
            if stamp[0] is not None:
                rows.append((stamp[0][0], row))
        for key in set(_card_cache) - live:
            _card_cache.pop(key, None)
        return [row for _, row in sorted(rows, key=lambda pair: pair[0], reverse=True)]


def _prepare_dashboard(previous, *, force=False):
    rows = _cached_dashboard_rows()
    for index, row in enumerate(rows):
        row['_order'] = index
    signature = dashboard_signature(rows)
    if not force and signature == previous:
        return None
    return rows, signature, {}


def _render_dashboard(client_key, snapshot):
    if snapshot is None:
        return False
    rows, signature, _ = snapshot
    _dashboard_rows[client_key] = rows
    from .provider_selection_work import overlay_rows
    rows = overlay_rows(task_queue, rows)
    from .execution_wait import overlay_rows as execution_rows
    rows = execution_rows(task_queue, rows)
    rows.sort(key=lambda row: not row.get('_execution_review', False))
    signature = dashboard_signature(rows)
    view = _dashboard_views.get(client_key)
    if view is None:
        return False
    review_statuses = {'need_confirm', 'approved', 'invalid'}
    view.publish({
        'active': sort_active_tasks([r for r in rows if r.get('_selection_wait') or r['workflow_status'] in ACTIVE_STATUSES]),
        'review': [r for r in rows if not r.get('_selection_wait') and (r.get('_selection_review') or r['workflow_status'] in review_statuses)],
        'history': [r for r in rows if not r.get('_selection_wait') and not r.get('_selection_review') and r['workflow_status'] not in ACTIVE_STATUSES | review_statuses],
    }, task_counts(rows))
    _dashboard_signatures[client_key] = signature
    return True


def show_task_queue(client):
    view = _dashboard_views.get(getattr(client, 'id', None))
    if view is not None:
        view.show_queue()


def project_selection_change(manager, task=None, *, removed=None):
    """复用已经显示的行，单任务变化不等待整页磁盘刷新。"""
    if manager is not task_queue:
        return
    from nicegui import Client
    for key, rows in tuple(_dashboard_rows.items()):
        client = Client.instances.get(key)
        if client is None:
            continue
        current = [r for r in rows if r['uuid'] != removed]
        if task:
            row = _card_projection(task)
            old = next((r for r in current if r['uuid'] == task['uuid']), None)
            if old is not None and old.get('plan_fingerprint') == row.get('plan_fingerprint'):
                row['_display']['title'] = old['_display']['title']
            current = [row if r['uuid'] == task['uuid'] else r for r in current]
        with client:
            _render_dashboard(key, (current, (), {}))


def refresh_dashboard(*, force: bool = False):
    """显式刷新与定时刷新共用后台读取；返回任务供需要完成结果的回调等待。"""
    client = context.client
    async def update():
        with client:
            return await refresh_dashboard_background(force=force)
    return asyncio.create_task(update())


async def refresh_dashboard_background(*, force: bool = False) -> bool:
    """并发请求共享一次后台观察；观察期间再变化时补读。"""
    from ..utils.task_changes import revision
    client = context.client
    key = client.id
    pending = _dashboard_reads.get(key)
    if pending is not None:
        if not force:
            return None
        result = await asyncio.shield(pending)
        return await refresh_dashboard_background(force=True) if result is False else result
    async def read():
        previous = _dashboard_signatures.get(key)
        view = _dashboard_views.get(key)
        failed, published = False, False
        if view is not None:
            view.reading()
        try:
            for _ in range(2):
                observed = revision()
                snapshot = await ui_disk_call(_prepare_dashboard, previous, force=force or bool(task_queue._provider_reads))
                if client.id not in client.instances or _dashboard_signatures.get(key) != previous:
                    return None
                published = _render_dashboard(key, snapshot) or published
                previous = _dashboard_signatures.get(key)
                if revision() == observed:
                    return published
                await asyncio.sleep(0)
            return published
        except (OSError, ValueError) as exc:
            failed = True
            logger.error(f"[任务工作台] 刷新失败：{type(exc).__name__}")
            if client.id in client.instances:
                notify('任务列表暂时无法读取，已保留上次显示，请稍后重试。', type='warning')
            return True if published else None
        finally:
            if view is not None and client.id in client.instances:
                view.finished(failed=failed)
            _dashboard_reads.pop(key, None)
    _dashboard_reads[key] = asyncio.create_task(read())
    return await asyncio.shield(_dashboard_reads[key])


def _read_card_decision(task_id):
    try:
        return DecisionStateStore(DECISION_STATE_PATH).load_optional(task_id)
    except DecisionStateError as exc:
        logger.error(f"[任务工作台] 无法读取标题权威 {task_id}：{exc}")
        return None


def _task_card(task: Dict[str, Any], *, title_authority=_UNREAD) -> None:
    task_id = str(task["uuid"])
    status = str(task.get("workflow_status"))
    if '_display' not in task:
        if title_authority is _UNREAD:
            title_authority = _read_card_decision(task_id)
        task = _card_projection(task, title_authority)
    display = task['_display']
    status = str(task.get('workflow_status'))
    active = bool(task.get('_selection_wait') or status in ACTIVE_STATUSES)
    reapproval_required = display['reapproval']
    safe_approved_retry = display['retry']
    execution_check_required = display['check']
    with ui.card().classes("bar-task-card w-full" + (" bar-task-card-active" if active else "")):
        with ui.row().classes("bar-task-header w-full items-start"):
            with ui.column().classes("bar-task-content gap-1 min-w-0"):
                with ui.row().classes("bar-task-topline w-full items-center gap-2 flex-wrap"):
                    if task.get('_approval_wait'):
                        ui.badge('正在批准', color='blue-7')
                    elif task.get('_selection_wait'):
                        ui.badge(task['_selection_stage'], color='blue-7' if status in {'running', 'analyzing', 'executing'} else 'amber-7')
                    elif status in ACTIVE_STATUSES:
                        _task_activity(task)
                    else:
                        ui.badge(
                            STATUS_TEXT.get(status, status),
                            color=STATUS_COLOR.get(status, "grey-7"),
                        )
                    if task.get("preview_only"):
                        ui.badge("预览记录 · 不执行", color="orange-7").props(
                            "outline"
                        )
                    recovery_hint = display['recovery_hint']
                    if recovery_hint:
                        ui.badge("已核查 · 需重新审核", color="orange-8").props(
                            "outline"
                        )
                    ui.label(task_media_label(task)).classes("bar-meta-chip")
                    if status not in ACTIVE_STATUSES and task.get("queue_operation") == "execute_approved":
                        ui.label(f"执行已批准的{RENAME_PLAN_TERM}").classes(
                            "bar-meta-chip"
                        )
                if not active:
                    ui.label(
                        display['title']
                    ).classes(
                        "bar-task-title"
                    )
                _path_tree_button(str(task.get("path") or ""))

            with ui.row().classes("bar-task-actions gap-2 justify-end"):
                if task.get('_selection_wait'):
                    cancel = _cancel_execution_wait if task.get('_execution_wait') else _cancel_selection_wait
                    button = ui.button('取消等待', icon='o_stop_circle', on_click=partial(cancel, task_id)).classes('bar-review-action').props('outline color=grey-8')
                    button.set_enabled(bool(task.get('_selection_cancel')))
                elif display['rollback_restart']:
                    ui.button('重新生成计划', icon='o_refresh', on_click=partial(_restart_after_rollback, task_id)).classes(
                        'bar-review-action').props('unelevated color=blue-7')
                    ui.button('安全结束', icon='o_archive', on_click=partial(_confirm_safe_end, task_id)).classes(
                        'bar-review-action').props('outline color=grey-8')
                elif reapproval_required:
                    ui.button(
                        "重新审核",
                        icon='o_rate_review',
                        on_click=partial(_open_execution_reapproval, task_id),
                    ).classes("bar-review-action").props(
                        "unelevated color=orange-8"
                    )
                elif display['replan']:
                    ui.button("重新生成整理计划", icon='o_refresh',
                        on_click=partial(_replan_failed_execution, task_id)
                    ).classes("bar-review-action").props("unelevated color=orange-8")
                elif safe_approved_retry:
                    ui.button(
                        "重新执行",
                        icon='o_refresh',
                        on_click=partial(_retry_approved_execution, task_id),
                    ).classes("bar-review-action").props(
                        "unelevated color=green-7"
                    )
                elif execution_check_required:
                    ui.button(
                        "核查",
                        icon='o_fact_check',
                        on_click=partial(_open_execution_recovery, task_id),
                    ).classes("bar-review-action").props(
                        "unelevated color=deep-orange-8"
                    ).tooltip("查看历史执行记录并确认处理方式")
                elif status == "queued":
                    ui.button(
                        "删除",
                        icon='o_delete',
                        on_click=lambda event, task_id=task_id: _delete_task(task_id, event.sender),
                    ).classes("bar-review-action").props(
                        'unelevated color=red-7 aria-label="删除排队任务"'
                    ).tooltip("删除排队任务")
                elif status in {"need_confirm", "approved", "invalid"}:
                    ui.button(
                        "处理", icon='o_construction', on_click=partial(_open_review, task_id)
                    ).classes("bar-review-action").props("unelevated color=orange-8")
                    ui.button(
                        "重建",
                        icon='o_refresh',
                        on_click=partial(_retry_task, task_id),
                    ).classes("bar-review-action").props("unelevated color=blue-7").tooltip(
                        "按当前状态重新识别或核查资产；不是直接重新执行整理")
                    ui.button(
                        "删除",
                        icon='o_delete',
                        on_click=lambda event, task_id=task_id: _delete_task(task_id, event.sender),
                    ).classes("bar-review-action").props("unelevated color=red-7")
                elif status not in ACTIVE_STATUSES:
                    registration_action_needed = display['registration']
                    if registration_action_needed:
                        ui.button(
                            "处理",
                            icon='o_folder_off',
                            on_click=partial(_open_review, task_id),
                        ).classes("bar-review-action").props(
                            'unelevated color=orange-8 aria-label="处理失效的作品登记"'
                        ).tooltip("选择原目录已移动或原目录已删除")
                    else:
                        ui.button(
                            "处理", icon='o_construction', on_click=partial(_open_review, task_id)
                        ).classes("bar-review-action").props(
                            'unelevated color=orange-8 aria-label="处理整理任务"'
                        ).tooltip("查看问题、修改设置或重新识别")
                    if status == 'completed':
                        ui.button('核查并补齐', icon='o_restore', on_click=partial(_rebuild_task, task_id)).classes(
                            'bar-review-action').props('unelevated color=blue-7 aria-label="核查并补齐"').tooltip('核对缺失目标，生成新的待批准计划')
                    else:
                        ui.button(
                            "重建", icon='o_refresh', on_click=partial(_retry_task, task_id)
                        ).classes("bar-review-action").props(
                            'unelevated color=blue-7 aria-label="重建"'
                        ).tooltip("按当前状态重新识别或核查资产；不是直接重新执行整理")
                    ui.button(
                        "删除", icon='o_delete', on_click=lambda event, task_id=task_id: _delete_task(task_id, event.sender)
                    ).classes("bar-review-action").props(
                        'unelevated color=red-7 aria-label="删除任务记录"'
                    ).tooltip("删除任务记录")

                if display['rollback']:
                    ui.button('回滚',icon='o_undo',on_click=partial(_rollback_asset_task,task_id)).classes(
                        'bar-review-action').props('unelevated color=blue-7 aria-label="回滚换源"').tooltip(
                            '核查本次换源的媒体和登记，确认后回滚')

        if not active and display['feedback']:
            feedback_classes = (
                "bar-task-feedback bar-recovery-ready"
                if display['reapproved']
                else "bar-task-feedback bar-error-text"
            )
            ui.label(display['feedback']).classes(
                feedback_classes
            )
        if not active and task.get("plan_rebase_message"):
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
        if not active and task.get("plan_entry_count") is not None:
            ui.label(
                f"{RENAME_PLAN_TERM} {task['plan_entry_count']} 项"
            ).classes("bar-task-footnote bar-help")


def _task_activity(task: Dict[str, Any]) -> None:
    status = task.get("workflow_status")
    if status == "queued":
        position = task.get("queue_position")
        text = f"排队中 · 第{position}位" if position else "排队中"
        ui.badge(text, color="amber-7")
    elif status in {"running", "analyzing", "executing"}:
        with ui.row(wrap=False).classes("bar-task-activity items-center gap-2").props("role=status"):
            ui.badge("处理中", color="blue-7")
    if status in ACTIVE_STATUSES:
        with ui.row(wrap=False).classes("bar-inline-progress items-center gap-3"):
            ui.linear_progress(value=0, show_value=False).props(
                "color=amber-7 track-color=amber-2" if status == 'queued'
                else "indeterminate color=blue-7 track-color=blue-2"
            ).classes("bar-progress grow")


def _path_tree_button(raw_path: str) -> None:
    """Show only the leaf in the card and reveal a folder tree on click."""
    parts = path_tree_parts(raw_path)
    with ui.button(display_folder_name(raw_path), icon='o_folder_open').classes(
        "bar-path-trigger"
    ).props("flat no-caps dense"):
        with ui.menu().classes("bar-path-menu"):
            ui.label("来源路径").classes("bar-field-label px-3 pt-2")
            with ui.column().classes("gap-0 px-2 pb-2"):
                for depth, part in enumerate(parts):
                    with ui.row(wrap=False).classes("items-center gap-2 bar-path-level").style(
                        f"padding-left:{depth * 14}px"
                    ):
                        ui.icon(outlined_icon("folder" if depth < len(parts) - 1 else "folder_open"), size="17px")
                        ui.label(part).classes("bar-path-tree-label")


def _needs_rollback_restart(task) -> bool:
    # Display hint only. The command independently checks the complete journal.
    return task.get('asset_rollback_phase') == 'rolled_back' and task.get('execution_status') == 'rolled_back'


async def _open_review(task_id: str) -> None:
    data = await disk_call(get_task, task_id)
    if _needs_rollback_restart(data):
        await _restart_after_rollback(task_id)
        return
    if requires_execution_reapproval(data):
        notify("该任务需要重新审核，请使用“重新审核”", type="warning")
        return
    if data.get("execution_requires_manual_check"):
        notify("请先打开“核查”并确认历史记录与目标目录", type="warning")
        return
    from ..rename.recognition_queue_admission import restore_title_confirmation
    try:
        await ui_disk_call(restore_title_confirmation, task_queue, task_id, expected_task=data)
    except (ValueError, RuntimeError, OSError) as exc:
        notify(str(exc), type='warning')
        return
    await plan_review_page(task_id)
    refresh_dashboard(force=True)


async def _rebuild_task(task_id: str) -> None:
    from .completed_rebuild_view import open_rebuild_dialog
    await open_rebuild_dialog(task_id, manager=task_queue, after_change=refresh_dashboard)


async def _retry_task(task_id: str) -> None:
    data = await disk_call(get_task, task_id)
    if not data:
        notify("任务记录不存在", type="negative")
        return
    if _needs_rollback_restart(data):
        await _restart_after_rollback(task_id)
        return
    if requires_execution_reapproval(data):
        notify("该任务需要重新审核，不能直接重试", type="warning")
        return
    if data.get("execution_requires_manual_check"):
        notify("请先打开“核查”并确认历史记录与目标目录", type="warning")
        return
    if provider_selection_awaits_user(data):
        notify("作品关系仍需确认，请打开任务继续或安全移除", type="warning")
        return
    try:
        await task_queue.retry_current(task_id, source="retry")
    except Exception as exc:
        notify(localize_plan_term(exc), type="negative")
        return
    refreshed = (await disk_call(get_task, task_id)) or {}
    notify('已核查并保留现有文件' if refreshed.get('workflow_status') == 'completed'
           else '已生成待批准修复计划' if refreshed.get('workflow_status') == 'need_confirm'
           else f"任务 {task_id[:8]} 已重新加入队列", type="positive")
    refresh_dashboard(force=True)


async def _cancel_execution_wait(task_id):
    from .execution_wait import cancel
    try:
        result = await cancel(task_queue, task_id)
        notify('已取消等待，原批准计划保留。' if result else '任务已入队或开始执行，请查看当前状态。',
               type='positive' if result else 'warning')
    except (OSError, ValueError, RuntimeError) as exc:
        notify(str(exc), type='negative')


async def _retry_approved_execution(task_id: str) -> None:
    data = await disk_call(get_task, task_id)
    if not data:
        notify("任务记录不存在", type="negative")
        return
    if not _is_safe_approved_retry(data):
        notify("当前状态需要先核查，不能直接重新执行", type="warning")
        refresh_dashboard()
        return
    fingerprint = str(data.get("execution_requested_fingerprint") or "")
    from .queue_confirmation import open_queue_confirmation
    def completed():
        notify("已重新加入执行队列", type="positive")
        refresh_dashboard()
        refresh_dashboard(force=True)
        refresh_dashboard(force=True)
    open_queue_confirmation(lambda: task_queue.enqueue_approved(task_id, fingerprint),
        completed, title='重新执行原批准计划', background=(task_queue, task_id, fingerprint))


async def _replan_failed_execution(task_id: str) -> None:
    data = await disk_call(get_task, task_id)
    if not data or not requires_execution_replan(data):
        notify("当前状态不能安全重规划，请先核查", type="warning")
        return
    try:
        await task_queue.replan_failed_execution(
            task_id, str(data.get("execution_requested_fingerprint") or ""))
    except Exception as exc:
        notify(localize_plan_term(exc), type="negative")
        return
    notify("已撤销旧批准，正在重新生成整理计划", type="positive")
    refresh_dashboard(force=True)


async def _restart_after_rollback(task_id: str) -> None:
    from .asset_recovery_view import open_rolled_back_restart
    await open_rolled_back_restart(task_queue, task_id, refresh_dashboard)


async def _confirm_safe_end(task_id: str, *, on_success=None) -> None:
    with ui.dialog() as dialog, dialog_card().classes('bar-dialog max-w-xl'):
        ui.label('安全结束此任务').classes('bar-dialog-title')
        ui.label('结束后移除任务卡并撤销当前批准。源文件、现有媒体、作品登记及计划/执行/回滚审计均保留；继续整理需要新建任务。').classes('bar-help')
        confirmed = ui.checkbox('我确认结束任务，不删除媒体与历史证据')
        busy = False

        async def finish():
            nonlocal busy
            if not confirmed.value or busy:
                return
            busy = True
            button.disable()
            button.props('loading')
            try:
                if await _submit_safe_end(task_id):
                    dialog.close()
                    if on_success is not None:
                        on_success()
            finally:
                busy = False
                button.enable()
                button.props(remove='loading')

        with ui.row().classes('bar-modal-footer'):
            ui.button('取消', on_click=dialog.close, icon='o_close').props('flat')
            button = ui.button('确认安全结束', on_click=finish, icon='o_delete').props('unelevated color=blue-7')
    dialog.open()


async def _delete_task(task_id: str, button=None) -> bool:
    if task_id in _deleting_tasks:
        return False
    _deleting_tasks.add(task_id)
    if button is not None:
        button.disable()
        button.set_text('正在删除')
        button.props('loading')
    try:
        if _needs_rollback_restart(await disk_call(get_task, task_id)):
            await _confirm_safe_end(task_id)
            return False
        return await _submit_safe_end(task_id)
    finally:
        _deleting_tasks.discard(task_id)
        if button is not None and not button.is_deleted:
            button.enable()
            button.set_text('删除')
            button.props(remove='loading')


async def _submit_safe_end(task_id: str) -> bool:
    from ..rename.plan_execution import PlanExecutionError
    task_data = await disk_call(get_task, task_id)
    if requires_execution_reapproval(task_data):
        notify("该任务需要重新审核，不能直接删除", type="warning")
        return False
    if task_data.get("execution_requires_manual_check"):
        notify("核查完成前不能直接删除任务和历史记录", type="warning")
        return False
    try:
        await task_queue.abandon_and_remove(
            task_id,
            record_root=RECORD_PATH,
            plan_root=PLAN_PATH,
            review_root=PLAN_REVIEW_PATH,
        )
    except (PlanExecutionError, ValueError, OSError, RuntimeError) as exc:
        notify(localize_plan_term(exc), type="negative")
        return False
    project_selection_change(task_queue, removed=task_id)
    notify(f"已安全结束任务 {task_id[:8]}；媒体与历史证据保留", type="positive")
    refresh_dashboard(force=True)
    return True


async def _open_execution_reapproval(task_id: str) -> None:
    task_data = await disk_call(get_task, task_id)
    if not requires_execution_reapproval(task_data):
        notify("任务状态已变化，请刷新后再处理", type="warning")
        refresh_dashboard()
        return
    await plan_review_page(task_id)
    refresh_dashboard(force=True)


async def _open_execution_recovery(task_id: str) -> None:
    from .asset_recovery_view import open_asset_recovery
    if await open_asset_recovery(task_queue, task_id, refresh_dashboard):
        return
    task_data = await disk_call(get_task, task_id)
    if not task_data:
        notify("任务记录不存在", type="negative")
        return
    snapshot = await disk_call(load_execution_recovery, task_id, RECORD_PATH)
    with ui.dialog() as dialog, dialog_card().classes("bar-dialog max-w-2xl"):
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
        with ui.row().classes("w-full justify-end gap-2").classes('bar-modal-footer'):
            ui.button("关闭", on_click=dialog.close, icon='o_close').props("flat")
            if task_data.get("execution_status") == "catalog_update_pending":
                ui.button(
                    "恢复作品登记",
                    icon='o_sync',
                    on_click=partial(_reconcile_catalog_update, dialog, task_id),
                ).props("unelevated color=blue-8")
                ui.button(
                    "放弃恢复",
                    icon='o_remove_circle_outline',
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
                    icon='o_verified_user',
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
    refresh_dashboard(force=True)




async def _rollback_asset_task(task_id: str) -> None:
    from .asset_recovery_view import open_asset_rollback
    await open_asset_rollback(task_queue,task_id,refresh_dashboard)


async def _reconcile_catalog_update(dialog: ui.dialog, task_id: str) -> None:
    try:
        await task_queue.reconcile_catalog_update(task_id)
    except (ExecutionRecoveryError, RuntimeError, ValueError) as exc:
        notify(localize_plan_term(exc), type="negative")
        return
    if getattr(dialog, 'is_deleted', False):
        return
    dialog.submit(True)
    notify("作品登记已恢复；任务完成情况请查看卡片", type="positive")
    refresh_dashboard(force=True)


def _confirm_abandon_catalog_update(
    recovery_dialog: ui.dialog,
    task_id: str,
    fingerprint: str,
) -> None:
    with ui.dialog() as confirm, dialog_card().classes("bar-dialog max-w-md"):
        ui.label("放弃恢复？").classes("bar-dialog-title")
        ui.label(
            "只会从任务列表移除此任务。不会修改媒体文件或作品登记，"
            "操作记录仍会保留。"
        ).classes("bar-help")
        with ui.row().classes("w-full justify-end gap-2").classes('bar-modal-footer'):
            ui.button("返回", on_click=confirm.close, icon='o_arrow_back').props("flat")
            ui.button(
                "移除任务",
                icon='o_delete_outline',
                on_click=partial(
                    _abandon_catalog_update,
                    confirm,
                    recovery_dialog,
                    task_id,
                    fingerprint,
                ),
            ).props("unelevated color=red-7")
    confirm.open()


async def _abandon_catalog_update(
    confirm: ui.dialog,
    recovery_dialog: ui.dialog,
    task_id: str,
    fingerprint: str,
) -> None:
    try:
        await task_queue.abandon_catalog_update(task_id, fingerprint)
    except (ExecutionRecoveryError, RuntimeError, ValueError) as exc:
        notify(localize_plan_term(exc), type="negative")
        return
    if not getattr(confirm, 'is_deleted', False):
        confirm.submit(True)
    if not getattr(recovery_dialog, 'is_deleted', False):
        recovery_dialog.submit(True)
    notify(
        "任务已移除；媒体文件和作品登记未修改，操作记录已保留。",
        type="positive",
    )
    refresh_dashboard(force=True)


# Compatibility wrappers retained for older event-slot callers and tests.
async def handle_edit(ev: GenericEventArguments) -> None:
    await _open_review(ev.args["row"]["uuid"])


async def handle_review(ev: GenericEventArguments) -> None:
    await _open_review(ev.args["row"]["uuid"])


async def handle_retry(ev: GenericEventArguments) -> None:
    await _retry_task(ev.args["row"]["uuid"])


async def handle_delete(ev: GenericEventArguments, is_notify: bool = True) -> None:
    await _delete_task(ev.args["row"]["uuid"])


async def _cancel_selection_wait(task_id):
    from .provider_selection_work import cancel
    try:
        result = await cancel(task_queue, task_id)
        notify('已取消等待，原任务已保留。' if result else '任务已开始或已经结束，不能取消等待。',
               type='positive' if result else 'warning')
    except (ValueError, RuntimeError, OSError) as exc:
        notify(str(exc), type='negative')
    await refresh_dashboard_background(force=True)
