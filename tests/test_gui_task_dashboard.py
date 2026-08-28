import json
import asyncio
import inspect
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.pages.add_task_page import media_flags
from src.main_page import BAR_CSS, is_local_gui_preview, runtime_mode_label
from src.pages.config_page import BASIC_CONFIG_KEYS, ConfigPage
from src.pages import data_table_page
from src.pages.data_table_page import (
    catalog_registration_action_needed,
    dashboard_signature,
    display_folder_name,
    execution_recovery_action_hint,
    load_task_rows,
    sort_active_tasks,
    task_counts,
    task_display_title,
    task_feedback_text,
    task_status_text,
)
from src.rename.decision_state import DecisionState


class GuiTaskDashboardTests(unittest.TestCase):
    def test_internal_inventory_errors_are_rendered_as_short_actions(self):
        self.assertEqual(
            "需要确认作品、保存位置和正片。",
            task_feedback_text({
                "error": "[电影资产清单] MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION",
                "movie_inventory_reason": "MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION",
            }),
        )
        self.assertEqual(
            "需要先确认视频用途，再检查字幕。",
            task_feedback_text({
                "error": "subtitle_video_not_content_version: internal detail",
            }),
        )
        self.assertNotIn(
            "selection_operation",
            task_feedback_text({
                "error": "internal",
                "selection_operation": {"state": "failed"},
            }),
        )

    def test_task_title_prefers_user_confirmed_provider_entry(self):
        task = {
            "workflow_status": "completed",
            "name": "First Search Result",
            "provider_selection": {"title": "Confirmed Work Title"},
        }

        self.assertEqual("Confirmed Work Title", task_display_title(task))
        self.assertEqual("First Search Result", task_display_title({
            "workflow_status": "completed",
            "name": "First Search Result",
        }))

    def test_missing_catalog_root_exposes_registration_action(self):
        task = {
            "workflow_status": "failed",
            "error": "[任务队列] 作品登记目录不存在；请点击处理作品登记",
        }
        decision = DecisionState(
            task_id="missing-catalog-root",
            recovery_state="catalog_root_missing",
            execution_failure={
                "code": "catalog_root_missing",
                "catalog_backend": "work",
                "catalog_key": "work-missing",
            },
        )

        self.assertTrue(catalog_registration_action_needed(task, decision))
        self.assertFalse(catalog_registration_action_needed({"error": "目标已存在"}))
        self.assertFalse(catalog_registration_action_needed(task))
        source = inspect.getsource(data_table_page._task_card)
        self.assertIn('"处理"', source)
        self.assertIn("选择原目录已移动或原目录已删除", source)
        self.assertIn("else:", source)

    def test_pending_and_history_actions_share_order_and_colors(self):
        source = inspect.getsource(data_table_page._task_card)
        history = source.split(
            'elif status not in ACTIVE_STATUSES:', 1
        )[1]

        self.assertLess(history.index('"处理"'), history.index('"重试"'))
        self.assertLess(history.index('"重试"'), history.index('"删除"'))
        self.assertIn("unelevated color=orange-8", history)
        self.assertIn("unelevated color=green-7", history)
        self.assertIn("unelevated color=red-7", history)

    def test_joined_file_filter_keeps_equal_width_segments(self):
        self.assertIn(".bar-equal-filter-toggle { width: 190px; }", BAR_CSS)
        self.assertIn(
            ".bar-equal-filter-toggle .q-btn { flex: 1 1 0 !important;",
            BAR_CSS,
        )

    def test_config_path_picker_uses_icon_only_button(self):
        source = inspect.getsource(ConfigPage._create_config_row)

        self.assertIn('icon="folder_open"', source)
        self.assertIn('flat round color=red-7 aria-label=', source)
        self.assertNotIn('RedButton(\n                            "选择"', source)

    def test_task_actions_use_reserved_column_and_feedback_uses_full_card_width(self):
        self.assertIn(
            "grid-template-columns: minmax(0,1fr) 394px", BAR_CSS
        )
        self.assertIn(".bar-task-actions { width: 394px", BAR_CSS)
        self.assertIn(".bar-task-feedback { display: block; width: 100%", BAR_CSS)
        self.assertIn("@media (max-width: 900px)", BAR_CSS)

        source = inspect.getsource(data_table_page._task_card)
        self.assertLess(
            source.index('"bar-task-actions gap-2 justify-end"'),
            source.index('"bar-task-feedback bar-error-text"'),
        )

    def test_task_card_defers_provider_selection_to_review_dialog(self):
        source = inspect.getsource(data_table_page._task_card)

        self.assertNotIn("provider_candidates", source)
        self.assertNotIn("可能的作品", source)

    def test_dialog_headers_reserve_right_side_status_area(self):
        self.assertIn(
            ".bar-dialog-header { display: grid !important; "
            "grid-template-columns: minmax(0,1fr) auto",
            BAR_CSS,
        )
        self.assertIn(
            ".bar-dialog-header-actions { min-width: max-content", BAR_CSS
        )

    def test_path_picker_preview_requires_separate_local_only_switch(self):
        with patch.dict(os.environ, {"BAR_UI_TEST_MODE": "true"}, clear=True):
            self.assertFalse(is_local_gui_preview())
        with patch.dict(
            os.environ,
            {"BAR_UI_TEST_MODE": "true", "BAR_LOCAL_GUI_PREVIEW": "true"},
            clear=True,
        ):
            self.assertTrue(is_local_gui_preview())

    def test_runtime_mode_label_distinguishes_preview_and_real_execution(self):
        self.assertEqual("界面测试 · 禁止执行", runtime_mode_label(True, False))
        self.assertEqual("只读预演 · 不创建文件", runtime_mode_label(False, True))
        self.assertEqual("真实执行已启用", runtime_mode_label(False, False))

    def test_acknowledged_recovery_has_persistent_action_hint(self):
        task = {
            "workflow_status": "failed",
            "queue_operation": "execute_approved",
            "execution_requested_fingerprint": "a" * 64,
            "execution_phase": None,
            "execution_requires_manual_check": False,
            "execution_may_have_written_targets": True,
            "execution_manual_check_acknowledged_at": "2026-08-13T01:00:00+00:00",
            "execution_recovery_disposition": "reapproval_required",
        }

        hint = execution_recovery_action_hint(task)

        self.assertIn("已核查", hint)
        self.assertIn("重新审核", hint)
        self.assertNotIn("重试", hint)
        self.assertNotIn("删除", hint)
        self.assertEqual(task_status_text(task), "失败")

    def test_unacknowledged_recovery_has_no_ready_hint(self):
        self.assertEqual(
            execution_recovery_action_hint(
                {
                    "workflow_status": "failed",
                    "execution_requires_manual_check": True,
                }
            ),
            "",
        )

    def test_dashboard_signature_is_order_independent_but_tracks_changes(self):
        first = [
            {"uuid": "one", "workflow_status": "running"},
            {"uuid": "two", "workflow_status": "queued", "queue_position": 1},
        ]
        reordered = list(reversed(first))
        changed = [dict(first[0]), dict(first[1], queue_position=2)]

        self.assertEqual(dashboard_signature(first), dashboard_signature(reordered))
        self.assertNotEqual(dashboard_signature(first), dashboard_signature(changed))

    def test_refresh_dashboard_skips_unchanged_dom_rebuild(self):
        rows = [{"uuid": "one", "workflow_status": "running"}]
        data_table_page._dashboard_signatures["__test__"] = dashboard_signature(rows)
        with (
            patch.object(data_table_page, "_dashboard_client_key", return_value="__test__"),
            patch.object(data_table_page, "load_task_rows", return_value=rows),
            patch.object(data_table_page._dashboard_summary, "refresh") as summary,
            patch.object(data_table_page._active_tasks, "refresh") as active,
            patch.object(data_table_page._review_tasks, "refresh") as review,
            patch.object(data_table_page._history_tasks, "refresh") as history,
        ):
            self.assertFalse(data_table_page.refresh_dashboard())
        summary.assert_not_called()
        active.assert_not_called()
        review.assert_not_called()
        history.assert_not_called()

    def test_refresh_dashboard_rebuilds_after_task_change(self):
        old_rows = [{"uuid": "one", "workflow_status": "running"}]
        new_rows = [{"uuid": "one", "workflow_status": "completed"}]
        data_table_page._dashboard_signatures["__test__"] = dashboard_signature(old_rows)
        with (
            patch.object(data_table_page, "_dashboard_client_key", return_value="__test__"),
            patch.object(data_table_page, "load_task_rows", return_value=new_rows),
            patch.object(data_table_page._dashboard_summary, "refresh") as summary,
            patch.object(data_table_page._active_tasks, "refresh") as active,
            patch.object(data_table_page._review_tasks, "refresh") as review,
            patch.object(data_table_page._history_tasks, "refresh") as history,
        ):
            self.assertTrue(data_table_page.refresh_dashboard())
        summary.assert_called_once_with()
        active.assert_called_once_with()
        review.assert_called_once_with()
        history.assert_called_once_with()

    def test_config_media_paths_match_library_folder_order(self):
        self.assertEqual(
            BASIC_CONFIG_KEYS[1:5],
            ["anime_path", "anime_movie_path", "movie_path", "bangumi_path"],
        )

    def test_config_persistence_note_is_single_sentence_at_bottom(self):
        source = inspect.getsource(ConfigPage.__init__)

        self.assertIn("配置保存在独立数据目录：{CONFIG_PATH}", source)
        self.assertNotIn("替换镜像时保持该目录挂载不变", source)
        self.assertNotIn("NAS 容器统一浏览 /volume1", source)
        self.assertGreater(
            source.index("配置保存在独立数据目录：{CONFIG_PATH}"),
            source.index("AI功能测试按钮"),
        )

    def test_media_type_options_map_to_existing_process_flags(self):
        self.assertEqual(media_flags("auto"), (None, None))
        self.assertEqual(media_flags("anime"), (True, False))
        self.assertEqual(media_flags("anime_movie"), (True, True))
        self.assertEqual(media_flags("movie"), (False, True))
        self.assertEqual(media_flags("tv"), (False, False))

    def test_dashboard_counts_queue_running_review_and_completed(self):
        rows = [
            {"workflow_status": "queued"},
            {"workflow_status": "running"},
            {"workflow_status": "need_confirm"},
            {"workflow_status": "approved"},
            {"workflow_status": "completed"},
            {"workflow_status": "failed"},
        ]
        self.assertEqual(
            task_counts(rows),
            {"queued": 1, "running": 1, "review": 2, "completed": 1},
        )

    def test_task_loader_supplies_safe_defaults_for_queued_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "queued.json").write_text(
                json.dumps(
                    {
                        "uuid": "queued",
                        "path": "synthetic/source",
                        "workflow_status": "queued",
                    }
                ),
                encoding="utf-8",
            )
            rows = load_task_rows(root)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "等待识别")
        self.assertIsNone(rows[0]["season_id"])
        self.assertEqual(task_status_text(rows[0]), "排队中")

    def test_task_loader_skips_record_whose_uuid_disagrees_with_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "trusted-name.json").write_text(
                json.dumps(
                    {
                        "uuid": "../escape",
                        "workflow_status": "failed",
                    }
                ),
                encoding="utf-8",
            )

            rows = load_task_rows(root)

        self.assertEqual(rows, [])

    def test_active_tasks_put_running_first_then_queue_top_to_bottom(self):
        rows = [
            {"uuid": "q3", "workflow_status": "queued", "queue_position": 3},
            {"uuid": "run", "workflow_status": "running"},
            {"uuid": "q1", "workflow_status": "queued", "queue_position": 1},
            {"uuid": "exec", "workflow_status": "executing"},
        ]
        self.assertEqual(
            [row["uuid"] for row in sort_active_tasks(rows)],
            ["exec", "run", "q1", "q3"],
        )

    def test_task_path_only_uses_final_folder_name(self):
        self.assertEqual(
            display_folder_name(r"D:\Incoming\Example Anime\Season 2"),
            "Season 2",
        )

    def test_manual_check_gate_blocks_retry_handle_and_delete(self):
        guarded = {
            "uuid": "guarded",
            "path": "synthetic/source",
            "workflow_status": "failed",
            "execution_requires_manual_check": True,
        }
        with (
            patch.object(data_table_page, "get_task", return_value=guarded),
            patch.object(data_table_page, "notify") as notify,
            patch.object(
                data_table_page.task_queue,
                "enqueue",
                new=AsyncMock(),
            ) as enqueue,
            patch.object(
                data_table_page, "plan_review_page", new=AsyncMock()
            ) as handle,
        ):
            asyncio.run(data_table_page._retry_task("guarded"))
            asyncio.run(data_table_page._open_review("guarded"))
            asyncio.run(data_table_page._delete_task("guarded"))

        enqueue.assert_not_awaited()
        handle.assert_not_awaited()
        self.assertEqual(notify.call_count, 3)
        self.assertTrue(
            all(
                "核查" in str(call.args[0])
                for call in notify.call_args_list
            )
        )

if __name__ == "__main__":
    unittest.main()
