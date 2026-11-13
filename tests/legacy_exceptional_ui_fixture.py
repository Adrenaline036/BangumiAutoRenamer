"""Frozen pre-dual-picker event fixture for v1 transaction regressions only.

This is NOT the current production GUI or an acceptance entry point. Keep the
old event shape here so existing v1 persistence, ownership, and worker-lifetime
assertions remain meaningful after removing its production renderer. Current
GUI reachability is covered by test_special_crosswalk_gui and gui_style_revision.
"""
from collections.abc import Callable, Mapping
from typing import Any
from src.pages.exceptional_episode_review import exceptional_entry_ids

def render_exceptional_episode_repair(
    dialog: Any,
    *,
    ui_api: Any,
    entry_identifier: Callable[[object], str],
    file_name: Callable[[object], str],
    render_partition_action: Callable[[Callable[[], object]], None],
    all_videos: bool = False,
) -> None:
    """Render one shared classification surface for all exceptional videos."""
    candidate_value = dialog.plan.metadata.get('provider_special_candidates')
    candidates = candidate_value if isinstance(candidate_value, Mapping) else {}
    identifiers = ({entry_identifier(e) for e in dialog.plan.entries
        if e.media_kind == 'video' and e.source_path not in dialog.plan.ignored_sources}
        if all_videos else exceptional_entry_ids(dialog.plan, dialog.snapshot.rows))
    affected = {entry_identifier(e): file_name(e.source_path) for e in dialog.plan.entries
                if entry_identifier(e) in identifiers}
    if not affected:
        return

    selected = [next(iter(affected))] if len(affected) == 1 else []
    current_content = dialog.plan.metadata.get('content_selection') is not None
    ui_api.label("异常分集归属").classes("bar-help font-bold")
    ui_api.label(
        ("不需要的内容请在“内容保留与排除”中调整；官方特别篇请逐个匹配。" if current_content else
         "这里确认视频用途与官方归属，不是保留或排除开关。附加内容可多选一次确认；官方特别篇请逐个匹配。") +
        "作为独立作品整理只带上所选视频及配套字幕、音轨；整包拆分请使用完整拆卡入口。"
    ).classes("bar-help")
    def options_for(values):
        from src.rename.provider_special_evidence import official_special_rows
        try:
            rows = official_special_rows(dialog.plan, values[0]) if len(values or ()) == 1 else ()
        except ValueError:
            rows = ()  # Verifier retains the invalid-observation issue.
        options = {}
        for item in rows if isinstance(rows, (list, tuple)) else ():
            if not isinstance(item, Mapping):
                continue
            episode_number = item.get("episode_number")
            episode_key = str(item.get("provider_episode_id") or "").strip()
            if not episode_key or type(episode_number) is not int or episode_number < 1:
                continue
            options[episode_key] = (f'Bangumi: {episode_number}' if item.get('provider_namespace') == 'bangumi'
                                    else f'TMDB: S00E{episode_number:02d}')
        return options

    episode_options = options_for(selected)
    candidate_label = ui_api.label("官方特别篇候选").classes("bar-help font-bold")
    with ui_api.row().classes("w-full items-end gap-2 flex-wrap"):
        file_select = ui_api.select(
            affected,
            label="异常视频（可多选）",
            value=selected,
            multiple=True,
        ).props("outlined dense options-dense multiple use-chips behavior=dialog").classes("grow")
        episode_select = ui_api.select(
            episode_options, label="选择特别篇", value=None,
        ).props("outlined dense options-dense behavior=dialog").classes("grow")
        official_button = ui_api.button(
            "归入特别篇", icon="playlist_add_check",
            on_click=lambda: dialog._apply_exceptional_special_inline(
                file_select.value, "confirmed_official_special", episode_select.value,
            ),
        ).props("outline color=blue-7")
        hint = ui_api.label(
            "请选择一个视频；若没有官方候选，请在“特别篇 / OVA”中获取资料。文件继续保留。"
            if current_content else "当前没有可验证的官方特别篇候选；仍可保留为附加内容或作为独立作品整理。"
        ).classes("bar-help text-orange-8 grow")
        extra_button = None
        if not current_content:
            extra_button = ui_api.button("保留为附加内容", icon="movie_filter",
                on_click=lambda: dialog._apply_exceptional_special_inline(
                    file_select.value, "unmatched_extra", None)).props("flat color=grey-8")
        render_partition_action(
            lambda: dialog._partition_exceptional_as_cross_work(file_select.value)
        )

    with ui_api.expansion('查看特别篇详细资料', value=False).classes('w-full'):
        details = ui_api.label().classes('whitespace-pre-wrap')

    def refresh_details():
        from src.rename.provider_special_evidence import official_special_rows
        values = file_select.value or []
        try:
            rows = official_special_rows(dialog.plan, values[0]) if len(values) == 1 else ()
        except ValueError:
            rows = ()
        row = next((r for r in rows if r['provider_episode_id'] == episode_select.value), None)
        details.set_text('请选择特别篇条目' if row is None else
            f'标题：{row["title"] or "未提供"}\n日期：{row["air_date"] or "未提供"}\n'
            f'时长：{row["runtime"] or "未提供"}\n官方分集 ID：{row["provider_episode_id"]}')

    def refresh(values):
        options = options_for(values)
        episode_select.set_options(options, value=next(iter(options)) if len(options) == 1 else None)
        episode_select.set_visibility(bool(options))
        candidate_label.set_visibility(bool(options))
        official_button.set_visibility(bool(options))
        official_button.set_enabled(bool(options))
        if extra_button is not None:
            extra_button.set_enabled(bool(values))
        hint.set_visibility(not bool(options))
        refresh_details()

    episode_select.on_value_change(lambda _: refresh_details())
    file_select.on_value_change(lambda event: refresh(event.value))
    refresh(file_select.value)
    return {'files': file_select, 'episode': episode_select, 'confirm': official_button}


def render_legacy_events(dialog, *, ui_api):
    from src.rename.plan_review import entry_id
    from src.pages.plan_review_page import _file_name
    from src.pages.exceptional_episode_review import render_cross_work_partition_action
    return render_exceptional_episode_repair(dialog, ui_api=ui_api,
        entry_identifier=entry_id, file_name=_file_name,
        render_partition_action=lambda cb: render_cross_work_partition_action(ui_api, cb))
