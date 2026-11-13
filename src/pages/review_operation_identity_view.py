"""Small pure presentation helpers for provider identity adjudication."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Optional

from ..rename.review_operation import ReviewCommand


def command_action_spec(
    command: ReviewCommand,
) -> Optional[tuple[str, str, str]]:
    labels = {
        "separate_work": ("分开整理", "call_split"),
    }
    if command.kind == 'separate_work' and command.payload.get('boundary_edge', {}).get('boundary_kind') == 'selected_bangumi_subject':
        return 'command-separate_work', '仅使用 Bangumi 资料继续', 'check'
    if command.kind != "select_identity":
        label_icon = labels.get(command.kind)
        return (
            (f"command-{command.kind}", *label_icon)
            if label_icon is not None else None
        )
    edge = command.payload.get("boundary_edge")
    candidate = edge.get("candidate") if isinstance(edge, Mapping) else None
    if not isinstance(candidate, Mapping):
        return None
    tmdb_id = str(candidate.get("provider_id") or "")
    title = str(
        candidate.get("title")
        or candidate.get("original_title")
        or "未命名作品"
    )
    return (
        f"command-select_identity-{tmdb_id}",
        f"选择 TMDB {tmdb_id} · {title}",
        "fact_check",
    )


def operation_copy(
    lifecycle: str,
    commands: tuple[ReviewCommand, ...],
    code: str,
    failure_reasons: Mapping[str, str],
) -> tuple[str, str]:
    if lifecycle == "awaiting_season":
        return "作品已选择", "请确认本次整理的季号；特别篇使用第 0 季"
    if lifecycle == "awaiting_user" and any(
        command.kind == "select_identity" for command in commands
    ):
        return (
            "确认作品对应与整理关系",
            "TMDB 有多个候选，可选择对应条目，也可使用 Bangumi 资料继续整理",
        )
    if lifecycle == 'awaiting_user' and any(c.payload.get('boundary_edge', {}).get('boundary_kind') == 'selected_bangumi_subject' for c in commands):
        return '已取得 Bangumi 资料', 'TMDB 资料未确认，可以使用 Bangumi 资料继续整理'
    if lifecycle == "awaiting_user":
        return "还需确认作品关系", "所选作品已记录，请确认与相邻作品的整理关系"
    return "这次选择未生效", failure_reasons.get(
        code, failure_reasons["selection_failed"]
    )
