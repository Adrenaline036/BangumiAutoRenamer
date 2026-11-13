import json
import asyncio
import inspect
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.pages.add_task_page import media_flags
from src.main_page import BAR_CSS, runtime_mode_label
from development.preview import is_local_gui_preview
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
    task_media_label,
    task_status_text,
)
from src.rename.decision_state import DecisionState


class GuiTaskDashboardTests(unittest.IsolatedAsyncioTestCase):
    def test_task_card_distinguishes_animation_series_and_movies(self):
        self.assertEqual(
            "动画剧集", task_media_label({"is_anime": True, "is_movie": False})
        )
        self.assertEqual(
            "动画电影", task_media_label({"is_anime": True, "is_movie": True})
        )
        self.assertEqual(
            "非动画剧集", task_media_label({"is_anime": False, "is_movie": False})
        )
        self.assertEqual(
            "电影", task_media_label({"is_anime": False, "is_movie": True})
        )
        self.assertEqual("自动判断", task_media_label({}))

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
        from nicegui import Client
        from nicegui.page import page as nice_page
        for status in ('need_confirm', 'failed'):
            with self.subTest(status=status):
                client = Client(nice_page('/card-actions'), request=None)
                self.addCleanup(client.delete)
                with client:
                    data_table_page._task_card({'uuid': 'actions', 'workflow_status': status}, title_authority=None)
                actions = [e for e in client.elements.values() if 'bar-review-action' in e.classes]
                self.assertEqual(['处理', '重建', '删除'], [e._props.get('label') for e in actions])
                self.assertEqual(['orange-8', 'blue-7', 'red-7'], [e._props.get('color') for e in actions])

    def test_joined_file_filter_keeps_equal_width_segments(self):
        # Check sizing without forbidding the requested outer-corner styling.
        self.assertRegex(BAR_CSS, r"\.bar-equal-filter-toggle\s*\{[^}]*width:\s*190px;")
        self.assertRegex(
            BAR_CSS,
            r"\.bar-equal-filter-toggle \.q-btn:not\(\.q-btn--round\)\s*\{[^}]*flex:\s*1 1 0 !important;",
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
        self.assertEqual("只读预演 · 不创建文件", runtime_mode_label(True))
        self.assertEqual("文件整理须批准后执行", runtime_mode_label(False))

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

    async def test_refresh_dashboard_skips_unchanged_dom_rebuild(self):
        from nicegui import Client
        from nicegui.page import page as nice_page
        client = Client(nice_page('/dashboard-calibration'), request=None)
        self.addCleanup(client.delete)
        rows = [{'uuid': 'one', 'workflow_status': 'running'}]
        with client, patch.object(data_table_page, 'load_task_rows', return_value=rows), \
                patch.object(data_table_page, '_read_card_decision', return_value=None):
            await data_table_page.create_table()
            view = data_table_page._dashboard_views[client.id]
            before = dict(view.sections['active'].cards)
            self.assertIs(False, await data_table_page.refresh_dashboard())
            self.assertEqual(before, view.sections['active'].cards)

    async def test_refresh_dashboard_rebuilds_after_task_change(self):
        from nicegui import Client
        from nicegui.page import page as nice_page
        client = Client(nice_page('/dashboard-calibration'), request=None)
        self.addCleanup(client.delete)
        rows = [{'uuid': 'one', 'workflow_status': 'running'}]
        with client, patch.object(data_table_page, 'load_task_rows', side_effect=lambda: rows), \
                patch.object(data_table_page, '_read_card_decision', return_value=None):
            await data_table_page.create_table()
            view = data_table_page._dashboard_views[client.id]
            rows[0] = dict(rows[0], workflow_status='completed')
            self.assertIs(True, await data_table_page.refresh_dashboard())
            self.assertNotIn('one', view.sections['active'].cards)
            self.assertIn('one', view.sections['history'].cards)
            self.assertEqual('1', view.counts['completed'].text)

    def test_config_media_paths_match_library_folder_order(self):
        self.assertEqual(
            BASIC_CONFIG_KEYS[1:5],
            ["anime_path", "anime_movie_path", "movie_path", "bangumi_path"],
        )

    def test_config_has_no_persistence_note_or_bottom_separator(self):
        source = inspect.getsource(ConfigPage.__init__)

        self.assertNotIn("配置保存在独立数据目录", source)
        self.assertNotIn('ui.separator().style("margin: 20px 0;")', source)
        self.assertIn('RedButton("取消", on_click=self.close)', source)
        self.assertIn('"保存"', source)

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
                "enqueue_batch",
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
