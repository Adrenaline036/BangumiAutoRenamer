from contextlib import nullcontext
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
from src.rename.plan_execution import PlanExecutionError
from src.rename.plan_store import PlanStore, PlanStoreError
from src.rename.rename_plan import ExecutionPolicy, PlanEntry, RenamePlan
from src.rename.target_routing import TargetRootSnapshot
from tests.presentation_fixtures import with_tv_presentation


class PlanReviewPageTests(unittest.IsolatedAsyncioTestCase):
    def _current_page(self):
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        helper = ContentWorkbenchGui('runTest')
        helper.create_page(cleanup_owner=self)
        return helper

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
        self.assertNotIn("允许 AI 辅助", dialog_source)
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

    async def test_provider_columns_share_one_manual_detail_link_entry(self):
        from unittest.mock import AsyncMock
        from src.pages import provider_selection_work
        helper = self._current_page()
        with helper.client:
            helper.view._show_provider_candidates()
        fields = [e for e in helper.view.descendants() if e._props.get('label') == 'Bangumi / TMDB 作品详情页链接']
        self.assertEqual(1, len(fields))
        fields[0].value = 'https://www.themoviedb.org/movie/100'
        with helper.client, patch.object(provider_selection_work, 'select', new=AsyncMock()) as select:
            await helper.driver.click('读取链接并选择此作品')
        select.assert_awaited_once_with(helper.view)
        self.assertEqual('https://www.themoviedb.org/movie/100', helper.view.provider_url_input.value)

    async def test_confirmed_work_collapses_candidates_and_can_reopen_them(self):
        helper = self._current_page()
        view = helper.view
        self.assertFalse(view.provider_selection_expanded)
        self.assertIn('当前已选作品', [getattr(e, 'text', '') for e in view.descendants()])
        self.assertFalse(any(e._props.get('label') == 'Bangumi / TMDB 作品详情页链接' for e in view.descendants()))
        with helper.client:
            await helper.driver.click('重新选择作品')
        self.assertTrue(view.provider_selection_expanded)
        self.assertFalse(view.provider_url_input.is_deleted)
        self.assertNotIn('当前已选作品', [getattr(e, 'text', '') for e in view.groups_container.descendants()])

    async def test_movie_primary_action_passes_selected_path_to_completion_service(self):
        helper = self._current_page()
        view, service = helper.view, helper.service
        old = PlanStore.fingerprint(view.plan)
        selected = [str(helper.videos[1]), str(helper.videos[0])]
        view.movie_primary_video_input = SimpleNamespace(value=selected)
        group_id = view.plan.presentation_groups[0].group_id
        with helper.client, patch.object(service, 'revise_movie_versions', wraps=service.revise_movie_versions) as revise:
            await view._complete_movie_with_primary(group_id)
        self.assertEqual(1, revise.call_count, helper.notices)
        self.assertEqual([[path] for path in selected], [group['sources'] for group in revise.call_args.kwargs['groups']])
        self.assertNotEqual(old, PlanStore.fingerprint(view.plan), helper.notices)
        self.assertEqual(PlanStore.fingerprint(view.plan), PlanStore.fingerprint(service.plan_store.load(view.plan.task_id)))
        self.assertFalse(service.load(view.plan.task_id).approved_entry_ids)

    async def test_failed_recompletion_keeps_current_plan_in_dialog(self):
        from tests.test_content_choice_contract import records
        helper = self._current_page()
        view = helper.view
        old = view.plan
        before = records(helper.root)
        selected = [str(helper.videos[0])]
        view.movie_primary_video_input = SimpleNamespace(value=selected)
        with helper.client, patch.object(helper.service, 'revise_movie_versions', side_effect=ValueError('校准故障：内容无法补全')) as revise:
            await view._complete_movie_with_primary(view.plan.presentation_groups[0].group_id)
        revise.assert_called_once()
        self.assertIs(old, view.plan)
        self.assertEqual(selected, view.movie_primary_video_input.value)
        self.assertEqual(before, records(helper.root))
        self.assertTrue(any('内容无法补全' in text for text in helper.notices), helper.notices)

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

    async def test_library_domain_switch_reprojects_and_adopts_current_plan(self):
        from tests.test_general_season_correction_contract import GeneralSeasonCorrectionContracts
        from tests._approval_stage_helpers import approve_dialog
        from src.rename.decision_state import DecisionStateError
        from tests.test_content_choice_contract import records
        from src.pages import plan_review_page as page
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        helper = GeneralSeasonCorrectionContracts('runTest')
        async with helper.current() as (flow, plan, state):
            gui = ContentWorkbenchGui('runTest')
            gui.create_page(prepared=(flow.root, flow.service, plan, []), cleanup_owner=self)
            owner = gui.view
            fingerprint = PlanStore.fingerprint(plan)
            with gui.client, patch.object(page, 'get_task', side_effect=flow.manager._read), patch.object(page, 'notify'):
                await approve_dialog(owner)
                flow.manager.execution_service.validate_approved_authority(flow.task_id, fingerprint)
                domain = 'animation' if state.library_domain == 'general' else 'general'
                before = records(flow.root)
                with patch.object(owner.decision_command_service, 'update', side_effect=DecisionStateError('校准故障：改域未提交')):
                    await owner._select_library_domain(domain)
                self.assertEqual(before, records(flow.root))
                self.assertEqual(fingerprint, PlanStore.fingerprint(owner.plan))
                flow.manager.execution_service.validate_approved_authority(flow.task_id, fingerprint)
                await PlanReviewDialog._select_library_domain(owner, domain)
            current = flow.store.load(flow.task_id)
            self.assertEqual(domain, flow.service.decision_state_store.load(flow.task_id).library_domain)
            self.assertNotEqual(fingerprint, PlanStore.fingerprint(current))
            self.assertEqual(PlanStore.fingerprint(current), PlanStore.fingerprint(owner.plan))
            self.assertFalse(flow.service.load(flow.task_id).approved_entry_ids)
            for identity in (fingerprint, PlanStore.fingerprint(current)):
                with self.assertRaises(PlanExecutionError):
                    flow.manager.execution_service.validate_approved_authority(flow.task_id, identity)

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

    def test_execute_gate_requires_approved_task_and_non_preview_record(self):
        live_policy = ExecutionPolicy(target_root=Path("library"), dry_run=False)
        self.assertTrue(
            can_execute_approved_plan(
                ReviewStatus.APPROVED,
                "approved",

                execution_policy=live_policy,
            )
        )
        self.assertFalse(
            can_execute_approved_plan(
                ReviewStatus.DRAFT,
                "approved",

                execution_policy=live_policy,
            )
        )
        self.assertFalse(
            can_execute_approved_plan(
                ReviewStatus.APPROVED,
                "need_confirm",

                execution_policy=live_policy,
            )
        )
        self.assertFalse(
            can_execute_approved_plan(
                ReviewStatus.APPROVED,
                "approved",
                preview_only=True,
                execution_policy=live_policy,
            )
        )
        self.assertFalse(
            can_execute_approved_plan(
                ReviewStatus.APPROVED,
                "approved",

                preview_only=True,
                execution_policy=live_policy,
            )
        )
        self.assertFalse(
            can_execute_approved_plan(
                ReviewStatus.APPROVED,
                "approved",

                execution_policy=ExecutionPolicy(
                    target_root=Path("library"), dry_run=True
                ),
            )
        )

    def test_production_execute_controls_reject_the_current_dry_run_plan(self):
        class UiElement:
            def __init__(self, label=""):
                self.label = label
                self.disabled = False
                self.tooltip_text = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def classes(self, *_args):
                return self

            def props(self, *_args):
                return self

            def disable(self):
                self.disabled = True
                return self

            def enable(self):
                self.disabled = False
                return self

            def tooltip(self, value):
                self.tooltip_text = value
                return self

        class UiCapture:
            def __init__(self):
                self.buttons = []

            def expansion(self, *_args, **_kwargs):
                return UiElement()

            def row(self):
                return UiElement()

            def button(self, label, **_kwargs):
                button = UiElement(label)
                self.buttons.append(button)
                return button

        dialog = SimpleNamespace(
            snapshot=SimpleNamespace(plan_status="ready"),
            decision=SimpleNamespace(status=ReviewStatus.APPROVED),
            task_data={"workflow_status": "approved", "preview_only": False},
            plan=SimpleNamespace(
                execution_policy=ExecutionPolicy(
                    target_root=Path("library"), dry_run=True
                )
            ),
            _download=Mock(),
            _approve_selected=Mock(),
            _safe_end=Mock(),
            _approve_all=Mock(),
            _confirm_execute=Mock(),
            _close_review=Mock(),
        )
        ui_capture = UiCapture()
        with (patch("src.pages.plan_review_page.ui", ui_capture)):
            PlanReviewDialog.render_actions(dialog)
            execute_button = next(
                button for button in ui_capture.buttons
                if button.label == "执行整理计划"
            )
            self.assertTrue(execute_button.disabled)
            self.assertEqual(
                "旧预演计划不能执行；请先点击“重试”生成真实整理计划",
                execute_button.tooltip_text,
            )
            execute_button.enable()
            PlanReviewDialog._refresh_execute_button(dialog)
            self.assertTrue(execute_button.disabled)
            with patch("src.pages.plan_review_page.notify") as notify:
                PlanReviewDialog._confirm_execute(dialog)
        notify.assert_called_once_with(
            "旧预演计划不能执行；请先点击“重试”生成真实整理计划",
            type="warning",
        )

    def test_execute_gate_fails_closed_when_canonical_policy_gate_is_disconnected(self):
        live_policy = ExecutionPolicy(target_root=Path("library"), dry_run=False)
        disconnected = PlanExecutionError(
            "执行策略权威门已断开", code="execution_policy_gate_disconnected"
        )
        with patch(
            "src.rename.plan_execution.require_approved_execution_policy",
            create=True,
            side_effect=disconnected,
        ) as gate:
            with self.assertRaises(PlanExecutionError) as raised:
                can_execute_approved_plan(
                    ReviewStatus.APPROVED,
                    "approved",

                    execution_policy=live_policy,
                )
        self.assertIs(raised.exception, disconnected)
        gate.assert_called_once_with(live_policy)

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

    def test_inventory_draft_hides_derived_technical_errors_behind_one_action(self):
        source = inspect.getsource(PlanReviewDialog._render_issues)
        entry_source = inspect.getsource(PlanReviewDialog._render_entry)

        self.assertIn("还差一步：选择电影正片", source)
        self.assertIn("不需要逐条处理", source)
        self.assertIn('== "inventory_review_draft"', source)
        self.assertIn("等待整包完成", entry_source)
        self.assertIn("完成整包后生成", entry_source)

    def test_inventory_completion_is_a_primary_workflow_before_file_list(self):
        helper = self._current_page()
        children = list(helper.view.descendants())
        self.assertIn(helper.view.completion_container, children)
        self.assertIn(helper.view.entries_container, children)
        self.assertLess(children.index(helper.view.completion_container), children.index(helper.view.entries_container))
        self.assertFalse(helper.view.completion_container.is_deleted)
        # Choice visibility/order and real buttons are covered behaviorally in
        # test_err060_role_entry, rather than requiring inline source strings.

    def test_retired_movie_layout_toggle_is_not_a_hidden_callback(self):
        self.assertFalse(hasattr(PlanReviewDialog, '_toggle_movie_segments'))

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

    def test_completed_plan_keeps_version_editor_not_duplicate_generation(self):
        completion = inspect.getsource(
            PlanReviewDialog._render_completion_workflow
        )
        choice = inspect.getsource(PlanReviewDialog._render_completion_choice)

        smart_branch = completion.split(
            'if mapping_source == "inventory_smart_completion":', 1
        )[1].split('if mapping_source != "inventory_review_draft":', 1)[0]
        self.assertIn('not is_confirmed_movie_plan(self.plan)', smart_branch)
        self.assertIn('self._render_completion_choice(group, selected_kind="tv_episodes")', smart_branch)
        self.assertNotIn('layout_mode =', smart_branch)
        self.assertIn('正片选择与标签编辑', inspect.getsource(PlanReviewDialog._render_plan_tools))
        self.assertIn('self._render_library_choices()', smart_branch)

    async def test_completion_choice_keeps_inferred_season_and_selected_state(self):
        from tests.test_general_season_correction_contract import GeneralSeasonCorrectionContracts
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        from src.pages import plan_review_page as page
        helper = GeneralSeasonCorrectionContracts('runTest')
        async with helper.current() as (flow, plan, state):
            gui = ContentWorkbenchGui('runTest')
            gui.create_page(prepared=(flow.root, flow.service, plan, []), cleanup_owner=self)
            self.assertIsNone(gui.view.tv_season_input)
            saved = state.season_binding
            command = Mock()
            command.update.return_value = (state, plan)
            with patch.object(flow.service, 'complete_inventory_draft', return_value=plan) as complete,                 patch.object(page, '_inventory_draft_for_completion', return_value=plan):
                result = page._complete_inventory_plan(plan, state, flow.service, command, flow.service.series_catalog,
                    plan.presentation_groups[0].group_id, 'tv_episodes', flow.service.root_snapshot_source.observe(),
                    state.library_domain, None)
            self.assertIs(plan, result)
            self.assertEqual(saved['season_number'], complete.call_args.kwargs['season_number'])
            self.assertEqual(saved['decision_source'], complete.call_args.kwargs['season_source'])
            self.assertNotIn('season_binding', command.update.call_args.kwargs['changes'])
            self.assertEqual(saved, flow.service.decision_state_store.load(flow.task_id).season_binding)

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
        self.assertIn("已选择；生成新版整理计划后应用", completion)
        self.assertIn('ui.label("保存位置")', completion)

    def test_completed_plan_explains_real_hardlink_execution_steps(self):
        completion = inspect.getsource(
            PlanReviewDialog._render_completion_workflow
        )
        confirmation = inspect.getsource(PlanReviewDialog._confirm_execute)

        self.assertIn("整理计划已生成，尚未创建硬链接", completion)
        self.assertIn("批准整理计划", completion)
        self.assertIn("执行整理计划", completion)
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
        warning = candidate_type_warning(False, [{"namespace": "tmdb", "entity_type": "movie"}])
        self.assertIn("没有找到正确的电视剧匹配", warning)
        self.assertIn("电影结果", warning)
        self.assertEqual("", candidate_type_warning(False, [{"namespace": "tmdb", "entity_type": "tv"}]))

    def test_movie_content_version_target_rule_is_visible_in_gui(self):
        source = inspect.getsource(PlanReviewDialog)

        self.assertIn("电影连续版的正片目标文件名由作品目录和版本信息自动生成", source)
        self.assertIn("保存版本决定会覆盖这里填写的目标路径", source)

    def test_old_schema_cannot_reuse_current_approval(self):
        helper = self._current_page()
        store, plan = helper.service.plan_store, helper.plan
        payload = plan.to_dict()
        payload['schema_version'] = 4
        payload['approved_entry_ids'] = ['旧批准']
        path = store.path_for(plan.task_id)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        before = path.read_bytes()
        with self.assertRaises(PlanStoreError):
            store.load(plan.task_id)
        self.assertEqual(before, path.read_bytes())

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

    async def test_review_dialog_mounts_outside_refreshable_dashboard_and_cleans_up(self):
        from src.pages import plan_review_page as page
        helper = self._current_page()
        created = []
        class ClosingDialog(PlanReviewDialog):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.mounted_on = self.parent_slot.parent
                self.hydrated = False
                created.append(self)
            def _load_plan_view(self, plan, *, snapshot=None):
                super()._load_plan_view(plan, snapshot=snapshot)
                self.hydrated = True
            def __await__(self):
                async def close():
                    self._close_review()
                return close().__await__()
        with helper.client, page.ui.column() as dashboard:
            with patch.object(page, 'PlanReviewDialog', ClosingDialog), patch.object(page.task_queue, '_provider_reads', {}, create=True):
                await page.plan_review_page(helper.plan.task_id)
        self.assertEqual(1, len(created))
        self.assertIs(helper.client.content, created[0].mounted_on)
        self.assertIsNot(dashboard, created[0].mounted_on)
        self.assertTrue(created[0].hydrated)
        self.assertTrue(created[0].is_deleted)

    async def test_failed_tv_recompletion_keeps_current_plan_and_records(self):
        from tests.test_general_season_correction_contract import GeneralSeasonCorrectionContracts
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        from tests.test_content_choice_contract import records
        from src.pages import plan_review_page as page
        fixture = GeneralSeasonCorrectionContracts('runTest')
        async with fixture.current() as (flow, plan, state):
            gui = ContentWorkbenchGui('runTest')
            gui.create_page(prepared=(flow.root, flow.service, plan, []), cleanup_owner=self)
            worker = getattr(gui.view, '_season_reference_task', None)
            if worker is not None:
                await worker
            before = records(flow.root)
            original = gui.view.plan
            with gui.client, patch.object(page, '_complete_inventory_plan', side_effect=ValueError('校准故障：剧集补全失败')) as complete:
                await gui.view._complete_inventory_draft(plan.presentation_groups[0].group_id, 'tv_episodes')
            complete.assert_called_once()
            self.assertIs(original, gui.view.plan)
            self.assertEqual(before, records(flow.root))
            self.assertTrue(any('剧集补全失败' in text for text in gui.notices), gui.notices)


if __name__ == "__main__":
    unittest.main()
