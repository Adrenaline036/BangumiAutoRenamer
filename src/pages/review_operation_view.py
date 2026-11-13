"""Public, typed projection for the mounted ReviewOperation controls.

The persisted ReviewOperation is the authority for this deterministic NiceGUI
projection. Tests exercise the same public boundary without private dialogs.
"""

from __future__ import annotations

from src.component.icons import outlined_icon

from .progress_view import bar_progress
from ..rename.provider_recovery import can_refresh_selection, transport_failure_text

from collections.abc import Awaitable, Callable, Mapping, MutableSet
from dataclasses import dataclass
from enum import Enum
from functools import partial
from types import MappingProxyType
from typing import Any, Optional

from ..rename.review_operation import (
    ReviewCommand,
    TaskWorkflowState,
    build_review_operation,
)
from .review_operation_identity_view import command_action_spec, operation_copy
from .tv_content_actions import RevisionProgress


_FAILURE_REASONS = MappingProxyType({
    "provider_unavailable": "暂时无法读取作品资料",
    "provider_not_found": "所选资料条目不存在，请检查作品链接",
    "provider_unauthorized": "资料服务拒绝访问，请检查访问配置",
    "provider_invalid_response": "资料服务返回格式无效，暂时不能确认作品",
    "provider_request_failed": "资料请求未完成，请检查作品链接和访问配置",
    "relation_unresolved": "没有找到可靠的季度关系",
    "content_mapping_failed": "视频集号或内容映射未完成，请核对正片编号",
    "save_failed": "更新结果未能安全保存",
    "stale_request": "页面内容已经更新",
    "app_restarted": "处理期间应用重新启动",
    "selection_failed": "未能应用所选作品",
})

_RELATION_DETAILS = MappingProxyType({
    'subject_candidate_confidence_insufficient': '已找到候选，但资料尚不足以确认它与所选条目的季度对应关系',
    'subject_candidate_evidence_missing': '缺少部分条目的候选资料，尚不能确认季度关系',
    'subject_query_truncated': '检索结果被截断，尚不能确认唯一季度关系',
    'subject_evidence_truncated': '续作资料尚未取全，尚不能确认季度关系',
    'subject_candidate_budget_exhausted': '本次资料检索未完成，请重新获取资料后确认',
    'subject_candidate_tie': '找到多个同等候选，需要先确认作品身份',
    'locale_candidate_conflict': '不同语言检索结果指向不同作品，需要先确认作品身份',
    'season_window_ambiguous': '存在多个相容季度，尚不能确认唯一对应关系',
    'season_count_mismatch': '条目集数与季度资料不一致，尚不能确认对应关系',
    'season_episode_count_mismatch': '条目集数与季度资料不一致，尚不能确认对应关系',
    'branching_sequel_graph': '续作关系存在分支，需要先确认本次整理范围',
    'cyclic_sequel_graph': '续作资料存在循环，尚不能生成季度对应关系',
})


@dataclass(frozen=True)
class ReviewSeasonAssignmentView:
    title: str
    season_number: object


@dataclass(frozen=True)
class ReviewBoundaryFailureView:
    public_title: str
    affected_subject_titles: tuple[str, ...]
    technical_summary: str


@dataclass(frozen=True)
class ReviewActionView:
    key: str
    label: str
    icon: str
    props: str
    payload: Optional[Mapping[str, object]] = None


@dataclass(frozen=True)
class ReviewOperationPresentation:
    state: TaskWorkflowState
    lifecycle: str
    heading: str
    reason_code: str
    reason_text: str
    assignments: tuple[ReviewSeasonAssignmentView, ...]
    outside_subject_titles: tuple[str, ...]
    resolved_target_root: str
    boundary_failure: Optional[ReviewBoundaryFailureView]
    actions: tuple[ReviewActionView, ...]


@dataclass(frozen=True)
class ReviewOperationCallbacks:
    select_provider_candidate: Callable[[Mapping[str, object]], None]
    dispatch_review_command: Callable[[Mapping[str, object]], Awaitable[None] | None]
    show_provider_candidates: Callable[[], None]
    remove_provider_failure_task: Callable[[], None | Awaitable[None]]
    close_review: Callable[[], None]


class ReviewCommandDispatchError(ValueError):
    """A mounted command no longer matches the current persisted authority."""


class ReviewCommandApplyResult(str, Enum):
    """Typed outcome of one application-boundary command attempt."""

    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    REJECTED = "rejected"


def _frozen_payload(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(value))


def _action(
    *,
    key: str,
    label: str,
    icon: str,
    style: str,
    payload: Optional[Mapping[str, object]] = None,
) -> ReviewActionView:
    selector = f"review-action-{key}"
    return ReviewActionView(
        key=key,
        label=label,
        icon=icon,
        props=(
            f"{style} no-caps aria-label='{label}' "
            f"data-testid='{selector}'"
        ),
        payload=_frozen_payload(payload) if payload is not None else None,
    )


def build_review_operation_presentation(task: Mapping[str, object]) -> Optional[ReviewOperationPresentation]:
    """Build a deterministic mounted-UI model from canonical task authority."""

    operation, _augmented = build_review_operation(task)
    context = operation.context
    lifecycle = str(context.get("lifecycle") or "")
    if context.get("kind") == "none":
        return None
    if lifecycle == "pending" or operation.state == TaskWorkflowState.RESOLVED:
        return ReviewOperationPresentation(
            state=operation.state,
            lifecycle=lifecycle, heading="",
            reason_code="",
            reason_text="",
            assignments=(),
            outside_subject_titles=(),
            resolved_target_root="",
            boundary_failure=None,
            actions=(),
        )

    code = str(context.get("failure_code") or "selection_failed")
    preview = operation.preview.to_dict() if operation.preview is not None else {}
    assignments = tuple(
        ReviewSeasonAssignmentView(
            title=str(row.get("title") or "作品"),
            season_number=row.get("season_number"),
        )
        for row in preview.get("season_assignments", ()) or ()
        if isinstance(row, Mapping)
    )
    outside = tuple(
        str(item)
        for item in preview.get("outside_subject_titles", ()) or ()
        if str(item)
    )
    resolved_target_root = str(preview.get("resolved_target_root") or "").strip()

    boundary_value = context.get("boundary_resolution_failure")
    boundary_failure = None
    if isinstance(boundary_value, Mapping):
        boundary_failure = ReviewBoundaryFailureView(
            public_title=str(
                boundary_value.get("public_title") or "无法安全确定季度边界"
            ),
            affected_subject_titles=tuple(
                str(item)
                for item in boundary_value.get("affected_subject_titles", ()) or ()
                if str(item)
            ),
            technical_summary=str(
                boundary_value.get("technical_summary") or ""
            ).strip(),
        )

    actions: list[ReviewActionView] = []
    candidate = context.get("candidate")
    if can_refresh_selection(context):
        actions.append(_action(
            key="refresh",
            label="重新获取资料",
            icon='o_refresh',
            style="unelevated color=blue-7",
            payload=candidate,
        ))
    for command in operation.commands:
        action_spec = command_action_spec(command)
        if action_spec is None:
            continue
        action_key, label, icon = action_spec
        actions.append(_action(
            key=action_key,
            label=label,
            icon=outlined_icon(icon),
            style="outline color=blue-7",
            payload=command.to_dict(),
        ))
    actions.extend((
        _action(
            key="switch-provider",
            label="换一个作品",
            icon='o_swap_horiz',
            style="outline color=blue-7",
        ),
        _action(
            key="remove-task",
            label="移除任务",
            icon='o_delete_outline',
            style="flat color=red-7",
        ),
        _action(
            key="close",
            label="关闭",
            icon='o_close',
            style="flat color=grey-8",
        ),
    ))
    heading, reason_text = operation_copy(
        lifecycle, operation.commands, code, _FAILURE_REASONS
    )
    reason_text = transport_failure_text(context) or reason_text
    if lifecycle in {'failed', 'interrupted'} and code == 'relation_unresolved':
        reason_text = next((_RELATION_DETAILS[reason] for reason in context.get('reason_codes', ())
                            if reason in _RELATION_DETAILS), reason_text)
    return ReviewOperationPresentation(
        state=operation.state,
        lifecycle=lifecycle,
        heading=heading,
        reason_code=code,
        reason_text=reason_text,
        assignments=assignments,
        outside_subject_titles=outside,
        resolved_target_root=resolved_target_root,
        boundary_failure=boundary_failure,
        actions=tuple(actions),
    )


def render_review_operation(
    ui_module: Any,
    task: Mapping[str, object],
    callbacks: ReviewOperationCallbacks,
    *, progress: Optional[RevisionProgress] = None, common_actions: bool = True,
) -> Optional[ReviewOperationPresentation]:
    """Render the public presentation model through the mounted NiceGUI API."""

    presentation = build_review_operation_presentation(task)
    if presentation is None:
        return None
    if presentation.lifecycle == "pending":
        with ui_module.row().classes("bar-callout w-full items-center gap-2"):
            bar_progress(ui_module)
            ui_module.label("正在更新作品信息，请稍候…").classes("font-bold")
        return presentation
    if presentation.state == TaskWorkflowState.RESOLVED:
        return presentation

    with ui_module.card().classes("bar-callout bar-callout-warning w-full"):
        if progress is not None:
            with ui_module.column().classes('w-full gap-2').bind_visibility_from(progress, 'status', backward=bool):
                bar_progress(ui_module).bind_visibility_from(progress, 'running')
                ui_module.label().bind_text_from(progress, 'status').props('data-testid=review-operation-progress')
        ui_module.label(presentation.heading).classes("font-bold text-orange-9")
        ui_module.label(presentation.reason_text).classes("bar-help")
        if presentation.assignments:
            ui_module.label("本次确认将整理以下季度：").classes(
                "font-bold text-blue-9"
            )
            for assignment in presentation.assignments:
                ui_module.label(
                    f"{assignment.title} → 第 {assignment.season_number} 季"
                ).classes("bar-help")
            if presentation.outside_subject_titles:
                ui_module.label(
                    "仍需单独处理：" + "、".join(
                        presentation.outside_subject_titles
                    )
                ).classes("bar-help text-orange-9")
            if presentation.resolved_target_root:
                ui_module.label(
                    "整理目标：" + presentation.resolved_target_root
                ).classes("bar-help text-blue-9")
        if presentation.boundary_failure is not None:
            ui_module.label(presentation.boundary_failure.public_title).classes(
                "font-bold text-red-8"
            )
            if presentation.boundary_failure.affected_subject_titles:
                ui_module.label(
                    "涉及：" + "、".join(
                        presentation.boundary_failure.affected_subject_titles
                    )
                ).classes("bar-help")
            if presentation.boundary_failure.technical_summary:
                ui_module.label(
                    presentation.boundary_failure.technical_summary
                ).classes("bar-help text-grey-8")
        with ui_module.row().classes("items-center gap-2 flex-wrap"):
            for action in presentation.actions:
                if not common_actions and not action.key.startswith("command-"):
                    continue
                if action.key == "refresh" and action.payload is not None:
                    on_click = partial(
                        callbacks.select_provider_candidate,
                        dict(action.payload),
                    )
                elif action.key.startswith("command-") and action.payload is not None:
                    on_click = partial(
                        callbacks.dispatch_review_command,
                        dict(action.payload),
                    )
                elif action.key == "switch-provider":
                    on_click = callbacks.show_provider_candidates
                elif action.key == "remove-task":
                    on_click = callbacks.remove_provider_failure_task
                else:
                    on_click = callbacks.close_review
                button = ui_module.button(
                    action.label,
                    on_click=on_click,
                    icon=action.icon or None,
                ).props(action.props)
                if progress is not None and action.key != 'close':
                    button.bind_enabled_from(progress, 'running', backward=lambda busy: not busy)
    return presentation


def dispatch_review_command_once(
    task: Mapping[str, object],
    review_command: Mapping[str, object],
    dispatched_idempotency_keys: MutableSet[str],
    apply_boundary: Callable[
        [bool, int, str, dict[str, object]],
        ReviewCommandApplyResult | str,
    ],
) -> bool:
    """Validate and dispatch one frozen command through one CAS/idempotency gate."""

    command = ReviewCommand.from_mapping(review_command)
    current, _augmented = build_review_operation(task)
    if (
        current.state != TaskWorkflowState.ACTIONABLE
        or command.operation_fingerprint != current.fingerprint
        or command.expected_revision != current.revision
        or command.idempotency_key not in {
            item.idempotency_key for item in current.commands
        }
    ):
        raise ReviewCommandDispatchError("review command authority is stale")
    if command.idempotency_key in dispatched_idempotency_keys:
        return False
    dispatched_idempotency_keys.add(command.idempotency_key)
    payload = command.payload
    try:
        result = ReviewCommandApplyResult(apply_boundary(
            bool(payload.get("same_work")),
            command.expected_revision,
            str(payload.get("evidence_fingerprint", "")),
            dict(payload.get("boundary_edge", {})),
        ))
    except Exception:
        dispatched_idempotency_keys.discard(command.idempotency_key)
        raise
    if result == ReviewCommandApplyResult.REJECTED:
        dispatched_idempotency_keys.discard(command.idempotency_key)
        return False
    return True
