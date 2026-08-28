import json
import asyncio
import inspect
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, patch

from src.pages.plan_review_page import (
    PlanReviewDialog,
    RecognitionRecoveryDialog,
    ROLE_LABELS,
    SECONDARY_TOGGLE_PROPS,
    build_plan_review_snapshot,
    catalog_backend_for_plan,
    catalog_lookup_uses_frozen_key,
    can_approve_plan,
    can_execute_approved_plan,
    export_plan_json,
    filter_plan_rows,
    is_fresh_unexecuted_task,
    candidate_type_warning,
    normalize_provider_candidates,
    plan_review_page,
    provider_candidate_poster,
    provider_candidate_url,
    provider_candidates_for_display,
    summarize_plan_issues,
    workflow_status_for_plan,
)
from src.pages.ui_terms import localize_plan_term
from src.rename.plan_review import PlanReviewService, ReviewStatus
from src.rename.plan_store import PlanStore, PlanStoreError
from src.rename.rename_plan import PlanEntry, RenamePlan
from src.rename.target_routing import TargetRootSnapshot
from tests.presentation_fixtures import with_tv_presentation


class PlanReviewPageTests(unittest.TestCase):
    def test_fresh_task_does_not_render_recovery_for_catalog_lookup_failure(self):
        self.assertTrue(is_fresh_unexecuted_task({}, None))
        self.assertTrue(
            is_fresh_unexecuted_task(
                {"execution_status": None},
                SimpleNamespace(recovery_state="healthy", execution_failure=None),
            )
        )
        self.assertFalse(
            is_fresh_unexecuted_task(
                {}, SimpleNamespace(recovery_state="materialization_missing")
            )
        )
        self.assertFalse(
            is_fresh_unexecuted_task({"execution_status": "completed"}, None)
        )

    def test_movie_link_and_asset_roles_use_user_facing_labels(self):
        groups = inspect.getsource(PlanReviewDialog._render_groups)
        entry = inspect.getsource(PlanReviewDialog._render_entry)

        self.assertIn('"关联资料"', groups)
        self.assertIn("ROLE_LABELS.get", entry)
        self.assertEqual("正片", ROLE_LABELS["movie_content"])

    def test_smart_completion_clears_obsolete_inventory_error(self):
        persistence = inspect.getsource(
            PlanReviewService._revision_task_terminal
        )

        self.assertIn('mapping_source") == "inventory_smart_completion"', persistence)
        self.assertIn('current["error"] = None', persistence)
        self.assertIn("TaskTerminalMutation.prepare", persistence)

    def test_unified_workspace_contains_rerecognition_and_inline_owner_repair(self):
        dialog_source = inspect.getsource(PlanReviewDialog)
        recovery_source = inspect.getsource(RecognitionRecoveryDialog)

        self.assertIn("重新识别设置", dialog_source)
        self.assertIn("检索名称", dialog_source)
        self.assertIn("允许 AI 辅助", dialog_source)
        self.assertIn("presentation_asset_owner_unresolved", dialog_source)
        self.assertIn("保存归属", dialog_source)
        self.assertIn("本次不整理", dialog_source)
        self.assertIn("整理任务", recovery_source)
        self.assertIn("保存并重新识别", recovery_source)

    def test_catalog_recovery_ui_has_move_delete_and_tree_picker(self):
        source = (
            inspect.getsource(PlanReviewDialog._render_catalog_context_content)
            + inspect.getsource(PlanReviewDialog._render_series_catalog_recovery_actions)
        )
        picker = inspect.getsource(
            PlanReviewDialog._pick_catalog_relocation_directory
        )
        deletion = inspect.getsource(
            PlanReviewDialog._archive_missing_catalog_work
        ) + inspect.getsource(PlanReviewDialog._detach_missing_catalog)

        self.assertIn("原目录已移动", source)
        self.assertIn("原目录已删除", source)
        self.assertIn("可直接粘贴", source)
        self.assertIn('icon="folder_open"', source)
        self.assertIn('aria-label="从文件夹树选择移动后的目录"', source)
        self.assertIn('props("outline color=blue-8")', source)
        self.assertIn("local_file_picker", picker)
        self.assertIn("catalog_detach_service.execute", deletion)
        self.assertIn("NAS 文件未被删除", deletion)
        self.assertIn("所有已登记季度", source)
        self.assertIn("_relocate_series_catalog", source)
        self.assertIn("_archive_missing_series_catalog", source)

    def test_provider_columns_are_independent_and_limited_to_four(self):
        candidates = [
            {"namespace": "bangumi", "provider_id": str(index)}
            for index in range(6)
        ] + [
            {"namespace": "tmdb", "provider_id": str(index)}
            for index in range(5)
        ]

        self.assertEqual(
            ["0", "1", "2", "3"],
            [item["provider_id"] for item in provider_candidates_for_display(candidates, "bangumi")],
        )
        self.assertEqual(
            ["0", "1", "2", "3"],
            [item["provider_id"] for item in provider_candidates_for_display(candidates, "tmdb")],
        )

    def test_each_provider_column_has_a_manual_detail_link_entry(self):
        source = inspect.getsource(PlanReviewDialog._render_provider_column)
        handler = inspect.getsource(PlanReviewDialog._select_manual_provider_url)

        self.assertIn("结果（最多 4 项）", source)
        self.assertIn("粘贴 {provider_label} 作品详情页链接", source)
        self.assertIn("读取链接并选择此作品", source)
        self.assertIn("resolve_provider_item_url", handler)

    def test_confirmed_work_collapses_candidates_and_can_reopen_them(self):
        groups = inspect.getsource(PlanReviewDialog._render_groups)
        selected = inspect.getsource(
            PlanReviewDialog._render_selected_provider_identity
        )
        reopen = inspect.getsource(PlanReviewDialog._show_provider_candidates)
        selection = inspect.getsource(PlanReviewDialog._select_provider_candidate)

        self.assertIn("provider_selection_expanded", groups)
        self.assertIn("当前已选作品", selected)
        self.assertIn("重新选择作品", selected)
        self.assertIn("self.provider_selection_expanded = True", reopen)
        self.assertIn("await task_queue.enqueue_reidentify", selection)
        self.assertIn('self.task_data["provider_selection"]', selection)

    def test_movie_primary_action_passes_selected_path_to_completion_service(self):
        dialog = Mock()
        dialog.movie_primary_video_input = Mock(
            value="/volume1/Vault/Anime/Movie/main-feature.mkv"
        )
        dialog.task_data = {
            "draft_suggested_roots": {
                "movie_continuous": "/volume1/Media/Jellyfin/ManualReview/BAR-Test"
            }
        }
        dialog.plan = Mock(
            task_id="movie-primary-action",
            metadata={"target_root": "/unused"},
            work_identity=Mock(domain="animation"),
        )
        dialog._completion_domain.return_value = "animation"
        dialog._inventory_draft_for_completion.return_value = dialog.plan
        revised = Mock(name="revised_plan")
        dialog.review_service.complete_inventory_draft.return_value = revised
        dialog.task_decision = Mock(revision=0)
        dialog.decision_command_service = Mock()
        dialog.decision_command_service.update.return_value = (
            Mock(revision=1), revised
        )
        snapshot = TargetRootSnapshot.capture({
            "bangumi_path": "/volume1/Media/Jellyfin/TV",
            "movie_path": "/volume1/Media/Jellyfin/Movies",
            "anime_path": "/volume1/Media/Jellyfin/Anime",
            "anime_movie_path": (
                "/volume1/Media/Jellyfin/ManualReview/BAR-Test"
            ),
        })

        with (
            patch.object(PlanStore, "fingerprint", return_value="fingerprint"),
            patch("src.pages.plan_review_page.notify") as notify,
            patch(
                "src.rename.target_routing.snapshot_from_config_manager",
                return_value=snapshot,
            ),
        ):
            PlanReviewDialog._complete_movie_with_primary(dialog, "movie-continuous")

        dialog.review_service.complete_inventory_draft.assert_called_once_with(
            "movie-primary-action",
            "fingerprint",
            group_id="movie-continuous",
            group_kind="movie_continuous",
            target_root=Path("/volume1/Media/Jellyfin/ManualReview/BAR-Test"),
            reason="用户明确选择 1 个电影正片版本；未选视频进入版本级特典判断",
            primary_video_sources=(Path(
                "/volume1/Vault/Anime/Movie/main-feature.mkv"
            ),),
            root_snapshot=snapshot.to_dict(),
            work_domain="animation",
            persist_revision=False,
            _source_plan=dialog.plan,
            _revision_parent_fingerprint="fingerprint",
        )
        dialog._adopt_revision.assert_called_once_with(revised)
        notify.assert_called_with(
            "电影正片版本、特典和字幕已生成新版整理计划；尚未执行文件操作。"
        )

    def test_failed_recompletion_keeps_current_plan_in_dialog(self):
        movie = inspect.getsource(PlanReviewDialog._complete_movie_with_primary)
        generic = inspect.getsource(PlanReviewDialog._complete_inventory_draft)

        self.assertIn("_source_plan=draft", movie)
        self.assertIn("_revision_parent_fingerprint=PlanStore.fingerprint(self.plan)", movie)
        self.assertNotIn("self._adopt_revision(draft)", movie)
        self.assertIn("_source_plan=draft", generic)
        self.assertNotIn("self._adopt_revision(draft)", generic)

    def test_unknown_domain_requires_readable_library_selection(self):
        dialog = Mock()
        dialog.plan = Mock(work_identity=Mock(domain="unknown"))
        dialog.task_decision = Mock(library_domain=None)

        with patch("src.pages.plan_review_page.notify") as notify:
            result = PlanReviewDialog._completion_domain(dialog)

        self.assertIsNone(result)
        notify.assert_called_once_with(
            "请先选择保存到“动画媒体库”还是“普通影视媒体库”",
            type="warning",
        )

        dialog.task_decision.library_domain = "animation"
        self.assertEqual(
            "animation", PlanReviewDialog._completion_domain(dialog)
        )

    def test_library_domain_switch_reprojects_and_adopts_current_plan(self):
        dialog = Mock()
        dialog.plan = Mock(task_id="domain-switch")
        dialog.task_decision = Mock(revision=4)
        projected = Mock(name="projected_plan")
        dialog.decision_command_service.update.return_value = (
            Mock(revision=5), projected
        )
        terminal = object()

        with (
            patch(
                "src.pages.plan_review_page._task_terminal_for_projected_plan",
                return_value=terminal,
            ),
            patch("src.pages.plan_review_page.notify") as notify,
        ):
            PlanReviewDialog._select_library_domain(dialog, "general")

        _args, kwargs = dialog.decision_command_service.update.call_args
        self.assertEqual({"library_domain": "general"}, kwargs["changes"])
        self.assertIs(dialog.plan, kwargs["planner"](Mock()))
        self.assertIs(terminal, kwargs["task_terminal"])
        dialog._adopt_revision.assert_called_once_with(projected)
        notify.assert_called_once_with(
            "保存位置已更新；请重新选择整理方式以生成对应目录，尚未执行文件操作。",
            type="warning",
        )

    def test_invalid_plan_stays_invalid_and_cannot_be_approved(self):
        self.assertFalse(can_approve_plan("invalid"))
        self.assertTrue(can_approve_plan("need_confirm"))
        self.assertEqual("invalid", workflow_status_for_plan("invalid"))
        self.assertEqual("need_confirm", workflow_status_for_plan("need_confirm"))

    def test_gui_plan_terminology_is_consistently_localized(self):
        self.assertEqual(
            "整理计划 整理计划 整理计划",
            localize_plan_term("Plan PLAN plan"),
        )
        self.assertEqual(
            "整理计划版本标识 与 结构校验",
            localize_plan_term("Plan fingerprint 与 Verifier"),
        )

    def test_execute_gate_requires_approved_task_and_non_test_mode(self):
        self.assertTrue(
            can_execute_approved_plan(
                ReviewStatus.APPROVED, "approved", test_mode=False
            )
        )
        self.assertFalse(
            can_execute_approved_plan(
                ReviewStatus.DRAFT, "approved", test_mode=False
            )
        )
        self.assertFalse(
            can_execute_approved_plan(
                ReviewStatus.APPROVED, "need_confirm", test_mode=False
            )
        )
        self.assertFalse(
            can_execute_approved_plan(
                ReviewStatus.APPROVED, "approved", test_mode=True
            )
        )
        self.assertFalse(
            can_execute_approved_plan(
                ReviewStatus.APPROVED,
                "approved",
                test_mode=False,
                preview_only=True,
            )
        )
    def _plan(self) -> RenamePlan:
        return with_tv_presentation(RenamePlan(
            task_id="review-task",
            source_root=Path("source"),
            entries=(
                PlanEntry(
                    source_path=Path("source/Episode 01.mkv"),
                    target_path=Path("library/Season1/S01E01.mkv"),
                    season_id=1,
                    episode_id=1,
                    media_kind="video",
                    role="main_episode",
                    evidence=("Rule: episode token",),
                ),
                PlanEntry(
                    source_path=Path("source/OVA.mkv"),
                    target_path=Path("library/Season1/S01E01.mkv"),
                    season_id=1,
                    episode_id=1,
                    media_kind="video",
                    role="episode_like_bonus",
                    requires_review=True,
                    evidence=("Needs human review",),
                ),
            ),
            expected_sources=(
                Path("source/Episode 01.mkv"),
                Path("source/OVA.mkv"),
            ),
            metadata={"name": "Example"},
        ))

    def test_legacy_tv_plan_uses_work_catalog_recovery_backend(self):
        plan = self._plan()
        plan = replace(
            plan,
            metadata={**plan.metadata, "catalog_work_key": "work-legacy-tv"},
        )

        self.assertEqual(
            ("work", "work-legacy-tv"),
            catalog_backend_for_plan(plan, None),
        )

    def test_legacy_plan_proposed_key_matches_execution_catalog_lookup(self):
        plan = self._plan()
        plan = replace(
            plan,
            metadata={**plan.metadata, "proposed_work_key": "work-proposed-tv"},
        )

        self.assertEqual(
            ("work", "work-proposed-tv"),
            catalog_backend_for_plan(plan, None),
        )

    def test_new_tv_plan_uses_series_catalog_recovery_backend(self):
        plan = self._plan()
        plan = replace(
            plan,
            metadata={
                **plan.metadata,
                "catalog_work_key": "work-legacy-tv",
                "series_key": "series-11111111-1111-1111-1111-111111111111",
            },
        )
        decision = SimpleNamespace(
            series_binding={
                "series_key": "series-22222222-2222-2222-2222-222222222222"
            }
        )

        self.assertEqual(
            ("series", "series-22222222-2222-2222-2222-222222222222"),
            catalog_backend_for_plan(plan, decision),
        )

    def test_failed_catalog_root_uses_frozen_key_instead_of_provider_lookup(self):
        failed = SimpleNamespace(execution_failure={
            "code": "catalog_root_missing",
            "catalog_backend": "work",
            "catalog_key": "work-key",
        })
        series_failed = SimpleNamespace(execution_failure={
            "code": "catalog_root_missing",
            "catalog_backend": "series",
            "catalog_key": "series-key",
        })
        self.assertTrue(
            catalog_lookup_uses_frozen_key(series_failed, "series", "series-key")
        )
        self.assertTrue(
            catalog_lookup_uses_frozen_key(failed, "work", "work-key")
        )
        self.assertFalse(
            catalog_lookup_uses_frozen_key(
                SimpleNamespace(execution_failure=None),
                "series",
                "series-key",
            )
        )
        self.assertFalse(
            catalog_lookup_uses_frozen_key(failed, "series", None)
        )

    def test_snapshot_contains_entries_evidence_and_verifier_issues(self):
        snapshot = build_plan_review_snapshot(self._plan())

        self.assertEqual(snapshot.task_id, "review-task")
        self.assertEqual(len(snapshot.rows), 2)
        self.assertEqual(snapshot.rows[1]["evidence"], "Needs human review")
        self.assertTrue(snapshot.rows[1]["requires_review"])
        self.assertEqual("Fixture Show", snapshot.rows[0]["presentation_group_label"])
        self.assertIn("duplicate_target", {issue["code"] for issue in snapshot.issues})
        self.assertIn(
            "duplicate_video_episode",
            {issue["code"] for issue in snapshot.issues},
        )

    def test_filter_matches_paths_roles_evidence_and_review_flag(self):
        rows = build_plan_review_snapshot(self._plan()).rows

        self.assertEqual(len(filter_plan_rows(rows, "episode 01")), 1)
        self.assertEqual(len(filter_plan_rows(rows, "HUMAN REVIEW")), 1)
        self.assertEqual(len(filter_plan_rows(rows, "episode_like_bonus")), 1)
        review_rows = filter_plan_rows(rows, "", review_only=True)
        self.assertEqual(
            [row["source"] for row in review_rows],
            [str(Path("source/OVA.mkv"))],
        )

    def test_export_is_the_versioned_plan_contract(self):
        plan = self._plan()

        payload = json.loads(export_plan_json(plan))
        restored = RenamePlan.from_dict(payload)

        self.assertEqual(restored, plan)
        self.assertEqual(payload["schema_version"], 6)

    def test_subtitle_filters_are_composable(self):
        rows = (
            {
                "source": "a.ass", "target": "a.zh-Hans.ass",
                "media_kind": "subtitle", "requires_review": True,
                "association_state": "resolved", "parse_state": "unresolved",
                "execution_blocked": True, "issue_codes": ("subtitle_parse_unresolved",),
                "primary_language": "zh", "flags": ("default",),
                "unknown_tokens": ("Group",),
            },
            {
                "source": "a.mkv", "target": "Show S01E01.mkv",
                "media_kind": "video", "requires_review": False,
                "association_state": "", "parse_state": "",
                "execution_blocked": False, "issue_codes": (),
                "primary_language": None, "flags": (), "unknown_tokens": (),
            },
        )
        filtered = filter_plan_rows(
            rows, "", subtitle_only=True, unresolved_only=True,
            unknown_only=True, language="zh", flag="default",
        )
        self.assertEqual(["a.ass"], [row["source"] for row in filtered])

    def test_issue_summary_collapses_repetition_and_names_files(self):
        rows = (
            {"source": r"source\A.mkv", "issue_codes": ("role_unknown",)},
            {"source": r"source\B.mkv", "issue_codes": ("role_unknown",)},
        )
        issues = (
            {"code": "role_unknown", "message": "角色未知", "source": r"source\A.mkv"},
            {"code": "role_unknown", "message": "角色未知", "source": r"source\B.mkv"},
        )

        summary = summarize_plan_issues(issues, rows)

        self.assertEqual(1, len(summary))
        self.assertEqual(["A.mkv", "B.mkv"], summary[0]["files"])
        self.assertEqual(2, summary[0]["file_count"])

    def test_issue_summary_translates_internal_content_version_language(self):
        summary = summarize_plan_issues(({
            "code": "presentation_group_content_missing",
            "message": "呈现分组 movie-continuous 没有可执行的内容版本",
        },), ())

        self.assertEqual("这个观看形式还没有确认任何正片", summary[0]["title"])
        self.assertIn("与只读测试无关", summary[0]["detail"])
        self.assertNotIn("可执行的内容版本", summary[0]["title"])

    def test_issue_card_keeps_icon_and_title_in_one_header_row(self):
        source = inspect.getsource(PlanReviewDialog._render_issues)
        self.assertIn("bar-issue-heading", source)
        self.assertIn("no-wrap", source)
        self.assertIn("bar-issue-detail", source)

    def test_inventory_draft_hides_derived_technical_errors_behind_one_action(self):
        source = inspect.getsource(PlanReviewDialog._render_issues)
        entry_source = inspect.getsource(PlanReviewDialog._render_entry)

        self.assertIn("还差一步：选择电影正片", source)
        self.assertIn("不需要逐条处理", source)
        self.assertIn('== "inventory_review_draft"', source)
        self.assertIn("等待整包完成", entry_source)
        self.assertIn("完成整包后生成", entry_source)

    def test_inventory_completion_is_a_primary_workflow_before_file_list(self):
        constructor = inspect.getsource(PlanReviewDialog.__init__)
        completion = inspect.getsource(PlanReviewDialog._render_completion_workflow)
        library_choices = inspect.getsource(
            PlanReviewDialog._render_library_choices
        )

        self.assertLess(
            constructor.index("_render_completion_workflow"),
            constructor.index("_render_toolbar"),
        )
        self.assertLess(
            constructor.index("_render_completion_workflow"),
            constructor.index("_render_entries"),
        )
        self.assertIn('ui.label("整理计划")', completion)
        self.assertNotIn("完成这个包", completion)
        self.assertNotIn("先确认上方作品，再选择整理方式", completion)
        self.assertNotIn("已确认：", completion)
        self.assertIn("self._render_library_choices", completion)
        self.assertIn('ui.label("保存位置")', library_choices)
        self.assertNotIn("还需确认保存到哪个媒体库", completion)
        choice = inspect.getsource(PlanReviewDialog._render_completion_choice)
        picker = inspect.getsource(PlanReviewDialog._render_movie_primary_picker)
        self.assertIn("这些视频是同一电影的连续分段", choice)
        self.assertIn("ui.checkbox(", choice)
        self.assertNotIn('icon="view_carousel"', choice)
        self.assertIn("self._toggle_movie_segments", choice)
        self.assertIn("选择电影正片版本", picker)
        self.assertIn("按所选视频生成电影正片版本", choice)
        self.assertIn("按季和集智能整理", choice)
        self.assertLess(
            choice.index("按季和集智能整理"),
            choice.index("按所选视频生成电影正片版本"),
        )
        self.assertNotIn("智能整理固定在左", choice)

    def test_movie_segments_checkbox_can_select_and_cancel(self):
        dialog = Mock()
        dialog.plan = Mock(task_id="movie-segments")
        dialog.review_service = Mock()
        reopened = Mock()
        dialog.review_service.reopen_inventory_draft.return_value = reopened
        dialog.task_decision = Mock(revision=0)
        dialog.decision_command_service = Mock()
        dialog.decision_command_service.update.return_value = (
            Mock(revision=1), reopened
        )

        PlanReviewDialog._toggle_movie_segments(dialog, "group-1", True)
        dialog._complete_inventory_draft.assert_called_once_with(
            "group-1", "movie_continuous"
        )

        with patch.object(PlanStore, "fingerprint", return_value="fingerprint"):
            PlanReviewDialog._toggle_movie_segments(dialog, "group-1", False)

        dialog.review_service.reopen_inventory_draft.assert_called_once_with(
            "movie-segments",
            "fingerprint",
            reason="用户取消连续分段整理方式",
            persist_revision=False,
        )
        dialog._adopt_revision.assert_called_once_with(reopened)

    def test_movie_primary_picker_is_anchored_and_has_confirm_action(self):
        picker = inspect.getsource(
            PlanReviewDialog._render_movie_primary_picker
        )

        self.assertIn("behavior=menu", picker)
        self.assertNotIn("behavior=dialog", picker)
        self.assertIn('picker.add_slot("append")', picker)
        self.assertIn('ui.button("确定"', picker)
        self.assertIn('picker.run_method("hidePopup")', picker)
        self.assertIn('picker.on("popup-show"', picker)
        self.assertIn('picker.on("popup-hide"', picker)

    def test_completion_choice_remains_visible_after_smart_completion(self):
        completion = inspect.getsource(
            PlanReviewDialog._render_completion_workflow
        )
        choice = inspect.getsource(PlanReviewDialog._render_completion_choice)

        smart_branch = completion.split(
            'if mapping_source == "inventory_smart_completion":', 1
        )[1].split('if mapping_source != "inventory_review_draft":', 1)[0]
        self.assertIn("self._render_completion_choice", smart_branch)

    def test_completion_choice_keeps_inferred_season_and_selected_state(self):
        """A reliable season must not be silently rewritten as user input.

        This protects the NAS regression where every TV task was forced
        through a visible manual field and subsequently created a different
        series/season decision on each retry.
        """
        choice = inspect.getsource(PlanReviewDialog._render_completion_choice)
        completion = inspect.getsource(PlanReviewDialog._complete_inventory_draft)
        self.assertIn('source in {"", "unset", "legacy_placeholder"}', choice)
        self.assertIn('"自动识别：第 {season_value} 季"', choice)
        self.assertIn('if selected_season is not None:', completion)
        self.assertNotIn('else 1\n                )', completion)
        self.assertNotIn("点击另一项会生成新版整理计划", choice)

    def test_file_filter_keeps_joined_shape_and_equal_width_class(self):
        toolbar = inspect.getsource(PlanReviewDialog._render_toolbar)

        self.assertIn('ui.toggle(', toolbar)
        self.assertIn('"bar-equal-filter-toggle"', toolbar)
        self.assertIn("SECONDARY_TOGGLE_PROPS", toolbar)
        self.assertIn("color=white", SECONDARY_TOGGLE_PROPS)
        self.assertIn("text-color=blue-7", SECONDARY_TOGGLE_PROPS)
        self.assertIn("toggle-color=blue-7", SECONDARY_TOGGLE_PROPS)
        self.assertIn("toggle-text-color=white", SECONDARY_TOGGLE_PROPS)

    def test_unknown_library_choice_uses_outline_buttons_and_accessible_state(self):
        completion = inspect.getsource(PlanReviewDialog._render_library_choices)

        self.assertIn('"outline color=blue-7 no-caps "', completion)
        self.assertIn("aria-pressed", completion)
        self.assertIn("已选择；选择整理方式后应用", completion)
        self.assertIn('ui.label("保存位置")', completion)

    def test_completed_plan_explains_real_hardlink_execution_steps(self):
        completion = inspect.getsource(
            PlanReviewDialog._render_completion_workflow
        )
        confirmation = inspect.getsource(PlanReviewDialog._confirm_execute)

        self.assertIn("整理计划已生成，尚未创建硬链接", completion)
        self.assertIn("批准全部 · 不执行", completion)
        self.assertIn("执行已批准的整理计划", completion)
        self.assertIn("当前整理计划是只读预演版本", completion)
        self.assertIn("实际创建硬链接", confirmation)
        self.assertIn("不会移动或复制源文件", confirmation)
        self.assertNotIn("实际创建、复制或移动文件", confirmation)

    def test_file_card_displays_group_name_instead_of_internal_group_id(self):
        source = inspect.getsource(PlanReviewDialog._render_entry)

        self.assertIn('row.get("presentation_group_label")', source)

    def test_provider_candidates_accept_lists_and_build_links_and_posters(self):
        candidates = normalize_provider_candidates({"provider_candidates": {
            "tv_episodes": [
                {
                    "namespace": "tmdb", "entity_type": "tv",
                    "provider_id": "123", "title": "同名作品",
                    "poster_path": "/poster.jpg",
                },
                {
                    "namespace": "bangumi", "entity_type": "subject",
                    "provider_id": "456", "title": "同名作品",
                },
            ],
        }})

        self.assertEqual(2, len(candidates))
        self.assertEqual("tv_episodes", candidates[0]["group_kind"])
        self.assertEqual(
            "https://www.themoviedb.org/tv/123",
            provider_candidate_url(candidates[0]),
        )
        self.assertEqual(
            "https://image.tmdb.org/t/p/w342/poster.jpg",
            provider_candidate_poster(candidates[0]),
        )
        self.assertEqual(
            "https://bgm.tv/subject/456",
            provider_candidate_url(candidates[1]),
        )

        frozen = MappingProxyType({
            "provider_candidates": MappingProxyType({
                "tv_episodes": tuple(
                    MappingProxyType(item) for item in candidates
                ),
            }),
        })
        self.assertEqual(2, len(normalize_provider_candidates(dict(frozen))))

    def test_explicit_type_mismatch_has_plain_language_warning(self):
        warning = candidate_type_warning(False, [{"entity_type": "movie"}])
        self.assertIn("没有找到正确的电视剧匹配", warning)
        self.assertIn("电影结果", warning)
        self.assertEqual("", candidate_type_warning(False, [{"entity_type": "tv"}]))

    def test_movie_content_version_target_rule_is_visible_in_gui(self):
        source = inspect.getsource(PlanReviewDialog)

        self.assertIn("电影连续版的正片目标文件名由作品目录和版本信息自动生成", source)
        self.assertIn("保存版本决定会覆盖这里填写的目标路径", source)

    def test_v4_migration_banner_explains_old_approval_is_invalid(self):
        source = inspect.getsource(PlanReviewDialog._render_header)

        self.assertIn("此整理计划已从 schema v4 转换", source)
        self.assertIn("旧批准已经失效，必须重新处理并批准", source)
        self.assertIn("原始 v4 历史不会被覆盖", source)

    def test_low_confidence_provider_card_cannot_look_auto_confirmed(self):
        source = inspect.getsource(PlanReviewDialog._render_provider_candidate)

        self.assertIn("低置信参考", source)
        self.assertIn("不会自动确认或改变观看形式", source)

    def test_missing_or_corrupt_plan_opens_recovery_workspace(self):
        class AwaitableRecovery:
            def __init__(self):
                self.delete = Mock()

            def __await__(self):
                async def done():
                    return None
                return done().__await__()

        recovery = AwaitableRecovery()
        with (
            patch("src.pages.plan_review_page.PlanStore") as store_factory,
            patch("src.pages.plan_review_page.PlanReviewDialog") as dialog,
            patch("src.pages.plan_review_page.RecognitionRecoveryDialog", return_value=recovery) as recovery_dialog,
            patch("src.pages.plan_review_page._dialog_mount_context", return_value=nullcontext()),
        ):
            store_factory.return_value.load.side_effect = PlanStoreError(
                "broken plan"
            )
            asyncio.run(plan_review_page("missing"))

        dialog.assert_not_called()
        recovery_dialog.assert_called_once()
        recovery.delete.assert_called_once()

    def test_review_dialog_mounts_outside_refreshable_dashboard_and_cleans_up(self):
        plan = self._plan()

        class AwaitableDialog:
            def __init__(self):
                self.delete = Mock()

            def __await__(self):
                async def wait_until_closed():
                    return None

                return wait_until_closed().__await__()

        opened_dialog = AwaitableDialog()
        with (
            patch("src.pages.plan_review_page.PlanStore") as store_factory,
            patch("src.pages.plan_review_page.get_task", return_value={"workflow_status": "need_confirm"}),
            patch("src.pages.plan_review_page._dialog_mount_context", return_value=nullcontext()) as mount,
            patch("src.pages.plan_review_page.PlanReviewDialog", return_value=opened_dialog) as dialog_factory,
        ):
            store_factory.return_value.load.return_value = plan
            asyncio.run(plan_review_page(plan.task_id))

        mount.assert_called_once_with()
        dialog_factory.assert_called_once_with(
            plan, {"workflow_status": "need_confirm"}
        )
        opened_dialog.delete.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
