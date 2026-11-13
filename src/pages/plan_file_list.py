"""The single file-card renderer for planned and source-only views."""
from typing import Dict
from nicegui import ui
from src.component.icons import IconToggle




class PlanFileListMixin:
    def _render_toolbar(self) -> None:
        from .content_review_view import open_content_workbench
        from .plan_review_page import SECONDARY_TOGGLE_PROPS, notify
        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            ui.input(
                "搜索文件名或路径",
                on_change=lambda event: self._set_query(event.value),
            ).props("clearable dense outlined").classes("grow")
            if self.plan is not None:
                IconToggle(
                    {"all": "全部文件", "issues": "问题文件"},
                    value="all",
                    on_change=lambda event: self._set_view_mode(event.value),
                ).props(f"dense {SECONDARY_TOGGLE_PROPS}").classes(
                    "bar-equal-filter-toggle"
                )
                self.selection_label = ui.label("已选择 0 项").classes("bar-help")
    def _render_plan_tools(self):
        from .content_review_view import open_content_workbench
        from .plan_review_page import notify
        from ..rename.default_content import source_only_pending
        source_pending = source_only_pending(self.plan, self.task_decision)
        with ui.row().classes('bar-plan-tools w-full items-center gap-2') as tools:
            regroup_button = ui.button('调整计划范围', icon='o_call_split',
                on_click=self._open_source_regrouping).props('outline color=blue-7')
            content_button = ui.button('内容保留与排除', icon='o_checklist',
                on_click=lambda: open_content_workbench(self, ui=ui, notify=notify)).props('outline color=blue-7')
            if source_pending:
                tools.classes('no-wrap')
                for button in (regroup_button, content_button):
                    button.style('flex: 1 1 0; min-width: 0')
                return
            from .subtitle_editor import open_subtitle_editor
            ui.button('调整字幕结构', icon='o_subtitles',
                on_click=lambda: open_subtitle_editor(self)).props('outline color=blue-7')
            from .exceptional_episode_review import open_official_special_picker
            self.official_special_button = ui.button('特别篇 / OVA', icon='o_playlist_add_check',
                on_click=lambda: open_official_special_picker(self, ui_api=ui, notify=notify)).props('outline color=blue-7')
            from .main_episode_editor import open_main_episode_editor
            self.main_episode_button = ui.button('作为正片整理', icon='o_movie',
                on_click=lambda: open_main_episode_editor(self)).props('outline color=blue-7')
            from .episode_range_editor import open_episode_range_editor
            self.episode_range_button = ui.button('设置连续集数', icon='o_format_list_numbered',
                on_click=lambda: open_episode_range_editor(self)).props('outline color=blue-7')
            from .episode_version_editor import open_episode_version_editor
            self.episode_version_button = ui.button('设置剧集版本', icon='o_video_library',
                on_click=lambda: open_episode_version_editor(self)).props('outline color=blue-7')
            self._refresh_official_special_action()
            self.movie_primary_button = ui.button('正片选择与标签编辑', icon='o_edit',
                on_click=lambda: self._edit_movie_version_groups(
                    self.plan.presentation_groups[0].group_id)).props('outline color=blue-7')
        self._refresh_movie_primary_action()

    def _refresh_movie_primary_action(self) -> None:
        from ..rename.provider_selection_authority import is_confirmed_movie_plan
        button = getattr(self, 'movie_primary_button', None)
        if button is not None:
            button.set_visibility(is_confirmed_movie_plan(self.plan)
                and bool(self.plan.presentation_groups))

    def _refresh_official_special_action(self) -> None:
        visible = (self.plan is not None and 'tv_completion_resolution' in self.plan.metadata
            and any(g.kind == 'tv_episodes' for g in self.plan.presentation_groups))
        for name in ('official_special_button', 'main_episode_button', 'episode_range_button', 'episode_version_button'):
            button = getattr(self, name, None)
            if button is not None:
                button.set_visibility(visible)

    def _render_entries(self) -> None:
        from .plan_review_page import filter_plan_rows, REVIEW_PAGE_SIZE
        self.entries_container.clear()
        approved = set(self.decision.approved_entry_ids) if self.decision is not None else set()
        rows = filter_plan_rows(
            self.snapshot.rows,
            self.query,
            self.review_only,
            subtitle_only=self.subtitle_only,
            unresolved_only=self.unresolved_only,
            conflict_only=self.conflict_only,
            unknown_only=self.unknown_only,
            language=self.language_filter,
            flag=self.flag_filter,
        )
        if self.view_mode == "issues":
            rows = [row for row in rows if row.get("execution_blocked")]
        page_count = max(1, (len(rows) + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
        self.page = min(max(1, self.page), page_count)
        start = (self.page - 1) * REVIEW_PAGE_SIZE
        visible_rows = rows[start:start + REVIEW_PAGE_SIZE]
        with self.entries_container:
            if not rows:
                with ui.column().classes("bar-empty-state w-full items-center"):
                    ui.icon('o_search_off', size="38px").classes("bar-muted")
                    ui.label("没有符合当前筛选条件的条目")
                return
            with ui.row().classes("w-full items-center justify-between gap-3"):
                ui.label(
                    f"共 {len(rows)} 个文件；当前显示 {start + 1}–"
                    f"{start + len(visible_rows)}"
                ).classes("bar-help")
                if page_count > 1:
                    with ui.row().classes("items-center gap-1"):
                        previous = ui.button(
                            icon='o_chevron_left',
                            on_click=lambda: self._change_page(-1),
                        ).props("flat dense round")
                        if self.page == 1:
                            previous.disable()
                        ui.label(f"{self.page} / {page_count}").classes("bar-help")
                        following = ui.button(
                            icon='o_chevron_right',
                            on_click=lambda: self._change_page(1),
                        ).props("flat dense round")
                        if self.page == page_count:
                            following.disable()
            for row in visible_rows:
                self._render_entry(row, str(row["entry_id"]) in approved)

    def _render_entry(self, row: Dict[str, object], approved: bool) -> None:
        from .plan_review_page import _file_name, MEDIA_LABELS, ROLE_LABELS
        identifier = str(row["entry_id"])
        from .plan_completion_status import entry_status
        done = (entry_status(self.plan, self.task_data)
                if self.plan is not None else "")
        is_inventory_draft = (
            self.plan is not None and str(self.plan.metadata.get("mapping_source") or "")
            == "inventory_review_draft"
        )
        review_class = (
            " bar-review-entry-review"
            if row.get("requires_review") and not is_inventory_draft and done != "已整理" else ""
        )
        if row.get("execution_blocked") and not is_inventory_draft:
            review_class += " bar-review-entry-blocked"
        with ui.card().classes(
            "bar-review-entry w-full" + review_class
        ):
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                if self.plan is not None:
                    checkbox = ui.checkbox(
                        value=identifier in self.selected_ids,
                        on_change=lambda event, eid=identifier: self._toggle_selected(
                            eid, event.value
                        ),
                    ).props("color=blue-7")
                    if is_inventory_draft:
                        checkbox.disable()
                        checkbox.tooltip("请先使用上方“整理计划”，无需逐文件处理占位草案")
                    elif row.get("execution_blocked"):
                        checkbox.tooltip("可选中后结构化修正；普通批准不能越过此阻断")
                with ui.column().classes("grow min-w-0 gap-2"):
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.label(f"#{row['id']}").classes("font-bold")
                        ui.label(_file_name(row["source"])).classes(
                            "bar-review-file-name"
                        )
                        if self.plan is not None:
                            ui.badge(
                                MEDIA_LABELS.get(
                                    str(row.get("media_kind")),
                                    str(row.get("media_kind")),
                                ),
                                color="blue-7",
                            ).props("outline")
                            ui.badge(
                                ROLE_LABELS.get(
                                    str(row.get("role")), str(row.get("role"))
                                ),
                                color="grey-7",
                            ).props("outline")
                            if row.get("season") is not None:
                                ui.label(f"第 {row['season']} 季").classes("bar-meta-chip")
                            if row.get("episode") is not None:
                                end = row.get("episode_end")
                                label = f"第 {row['episode']}–{end} 集" if end is not None else f"第 {row['episode']} 集"
                                ui.label(label).classes("bar-meta-chip")
                            if is_inventory_draft:
                                ui.badge("等待整包完成", color="blue-7")
                            elif row.get("requires_review") and done != "已整理":
                                ui.badge("需要确认", color="orange-8")
                            if row.get("execution_blocked") and not is_inventory_draft:
                                ui.badge("阻断执行", color="red-7")
                            if done or approved:
                                ui.badge(done or "已批准 · 未执行", color="green-7")
                            if row.get("presentation_group"):
                                ui.badge(
                                    str(
                                        row.get("presentation_group_label")
                                        or row["presentation_group"]
                                    ),
                                    color="purple-7",
                                ).props("outline")
                            if row.get("asset_role"):
                                ui.badge(
                                    ROLE_LABELS.get(
                                        str(row["asset_role"]), str(row["asset_role"])
                                    ),
                                    color="teal-7",
                                ).props("outline")
                    if row.get("issue_codes") and not is_inventory_draft:
                        ui.label(
                            "此文件：" + "、".join(row["issue_codes"])
                        ).classes("bar-review-file-issue")
                    with ui.expansion("查看来源路径" if self.plan is None else "查看路径与识别详情").classes(
                        "bar-entry-detail w-full"
                    ):
                        with ui.column().classes("w-full gap-2"):
                            ui.label("源文件").classes("bar-field-label")
                            ui.label(str(row["source"])).classes("bar-path-box w-full")
                            if self.plan is not None:
                                ui.label("建议目标").classes("bar-field-label")
                                ui.label(
                                    "完成整包后生成"
                                    if is_inventory_draft else str(row["target"])
                                ).classes("bar-path-box w-full")
                                if row.get("evidence"):
                                    ui.label(
                                        "判断依据：" + str(row["evidence"])
                                    ).classes("bar-evidence")
                                if row.get("version_ref"):
                                    ui.label(
                                        f"版本：{row.get('version_label') or '默认单版本'} · "
                                        f"{row.get('version_ref')}"
                                    ).classes("bar-evidence font-mono")
                                if row.get("media_kind") == "subtitle" and not is_inventory_draft:
                                    with ui.column().classes("bar-subtitle-contract w-full gap-1"):
                                        ui.label(
                                            f"原始标签：{row.get('raw_label') or '空'} · "
                                            f"关联：{row.get('association_state')} · "
                                            f"解析：{row.get('parse_state')}"
                                        ).classes("bar-evidence")
                                        ui.label(
                                            "语言："
                                            f"{row.get('primary_language') or '未定'}"
                                            f"-{row.get('script') or '?'} · "
                                            f"附加：{', '.join(row.get('additional_languages', ())) or '无'} · "
                                            f"flags：{', '.join(row.get('flags', ())) or '无'}"
                                        ).classes("bar-evidence")
                                        if row.get("unknown_tokens"):
                                            ui.label(
                                                "未知 token：" + ", ".join(row["unknown_tokens"])
                                            ).classes("bar-error-text")

    def _set_query(self, value: str) -> None:
        self.query = value or ""
        self.page = 1
        self._render_entries()

    def _set_view_mode(self, value: object) -> None:
        self.view_mode = "issues" if str(value) == "issues" else "all"
        self.page = 1
        self._render_entries()

    def _change_page(self, delta: int) -> None:
        self.page = max(1, self.page + int(delta))
        self._render_entries()
