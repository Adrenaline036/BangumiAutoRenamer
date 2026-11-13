from development.preview import preview_session
"""User-facing GUI revisions, using real NiceGUI elements and disposable plans."""
import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nicegui import Client, ui
from nicegui.page import page as nice_page


class GuiStyleContracts(unittest.IsolatedAsyncioTestCase):
    async def test_special_errors_are_reports_and_dual_picker_stays_separate(self):
        from tests.test_special_crosswalk_gui import SpecialCrosswalkGui
        from tests.test_content_choice_contract import records
        helper = SpecialCrosswalkGui('runTest')
        from tests.test_initial_tv_completion_contract import InitialTVCompletionContracts
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        helper.fixture = lambda **kw: InitialTVCompletionContracts.fixture(self, **kw)
        helper.create_page = lambda **kw: ContentWorkbenchGui.create_page(helper, cleanup_owner=self, **kw)
        root, service, plan, extras, key = await helper.prepared()
        # Supply the incident's display observation; the real independent
        # picker below continues to consume the fixture's frozen dual catalog.
        from dataclasses import replace
        code = 'provider_special_decision_required'
        helper.view.snapshot = replace(helper.view.snapshot,
            issues=({'code':code, 'source':str(extras['exceptional']), 'message':'需要人工对应'},),
            rows=tuple({**row, 'issue_codes':(code,)} if row['entry_id'] == key else row
                       for row in helper.view.snapshot.rows))
        helper.view.plan = replace(helper.view.plan, metadata={**helper.view.plan.metadata,
            'provider_special_eligibility_unavailable':(key,)})
        with helper.client:
            helper.view._render_issues()
        panels = [e for e in helper.view.descendants() if 'bar-issue-summary' in e.classes
                  and any('provider_special_decision_required' in str(getattr(c, 'text', ''))
                          for c in e.descendants())]
        self.assertEqual(1, len(panels))
        text = ' '.join(str(getattr(e, 'text', '')) for e in panels[0].descendants())
        self.assertIn(extras['exceptional'].name, text)
        self.assertNotIn('异常分集归属', text)
        self.assertFalse(any(type(e).__name__ in {'Button', 'Select', 'Expansion'}
                             for e in panels[0].descendants()))
        before = records(root)
        await helper.driver.click('特别篇 / OVA')
        controls = helper.view._official_special_controls
        self.assertEqual('Bangumi 编号', controls['bangumi'].label)
        self.assertEqual('TMDB 编号', controls['episode'].label)
        self.assertTrue(controls['confirm'].visible)
        self.assertFalse(controls['confirm'].enabled)
        self.assertEqual(before, records(root))

    async def test_completion_choice_omits_auto_season_text_but_keeps_manual_fallback(self):
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        from tests.test_initial_tv_completion_contract import InitialTVCompletionContracts
        from dataclasses import replace
        helper = ContentWorkbenchGui('runTest')
        root, service, _, result, *_ = await InitialTVCompletionContracts.fixture(self, anime=False, selected_season=2)
        helper.create_page(prepared=(root, service, result.plan, []), cleanup_owner=self)
        original = helper.view.task_decision
        group = SimpleNamespace(kind='tv_episodes', group_id='shown')
        with helper.client:
            for source in ('explicit_user', 'unset'):
                helper.view.task_decision = replace(original,
                    season_binding={**original.season_binding, 'season_number':2, 'decision_source':source})
                with ui.column() as choices:
                    helper.view._render_completion_choice(group, selected_kind='tv_episodes', video_options={})
                self.assertFalse(any('自动识别：第' in str(getattr(e, 'text', '')) for e in choices.descendants()))
                self.assertEqual(source == 'unset', helper.view.tv_season_input is not None)
                self.assertTrue(any('生成剧集整理计划' in str(getattr(e, 'text', '')) for e in choices.descendants()))
        helper.view.task_decision = original
        self.assertEqual(2, service.decision_state_store.load(result.plan.task_id).season_binding['season_number'])

    def client(self):
        client = Client(nice_page('/isolated-gui-style'), request=None)
        self.addCleanup(client.delete)
        return client

    async def test_unselected_work_has_only_one_safe_end_in_fixed_footer(self):
        from tests.test_initial_auto_link_contract import InitialAutoLinkContracts
        from src.pages.plan_review_page import PlanReviewDialog
        from tests.test_dialog_scroll_layout import ancestors
        helper = InitialAutoLinkContracts('runTest')
        async with helper.flow(admission_only=True, admit=helper.admit_auto) as flow:
            with self.client():
                dialog = PlanReviewDialog(None, flow.manager._read(flow.task_id))
                try:
                    ends = [e for e in dialog.descendants()
                            if e._props.get('label') in {'安全结束任务', '安全结束此任务'}]
                    self.assertEqual(1, len(ends))
                    self.assertTrue(any('bar-modal-footer' in e.classes for e in ancestors(ends[0])))
                    self.assertFalse(dialog.approve_all_button.enabled)
                    self.assertFalse(dialog.execute_button.enabled)
                finally:
                    dialog.delete()

    async def test_plan_removes_user_rejected_font_destination_notice(self):
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        helper = ContentWorkbenchGui('runTest')
        helper.create_page(cleanup_owner=self)
        texts = [str(getattr(e, 'text', '')) for e in helper.view.descendants()]
        self.assertFalse(any('批准执行时自动增补字体' in text for text in texts))
        self.assertIn('内容保留与排除', texts)
        # Existing font checking/approval rules are covered in their own tests;
        # the user explicitly requested deleting this destination notice.

    async def test_movie_replacement_lives_inside_its_registered_work_card(self):
        from dataclasses import replace
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        from tests.test_dialog_scroll_layout import ancestors
        from src.pages import plan_review_page as page
        helper = ContentWorkbenchGui('runTest')
        helper.create_page(cleanup_owner=self)
        view = helper.view
        root = helper.root
        match = SimpleNamespace(title='Movie', year=2026, canonical_root=root,
                                versions=({'display_label': '1080p'},), work_key='registered-movie')
        view.plan = replace(view.plan, metadata={**view.plan.metadata,
            'asset_operation': {'requires_choice': True}})
        view._catalog_display.update(match=match, present=True, error=None)
        with helper.client, patch.object(page, 'catalog_backend_for_plan', return_value=('work', 'registered-movie')), \
                patch.object(view.review_service.work_catalog, 'get_match', return_value=match):
            view._render_catalog_context()
            view._render_issues()
        buttons = [e for e in view.descendants() if e._props.get('label') == '换源']
        self.assertEqual(1, len(buttons))
        self.assertTrue(buttons[0]._props.get('outline'))
        self.assertIn('bg-white', buttons[0].classes)
        self.assertEqual('blue-7', buttons[0]._props.get('color'))
        self.assertTrue(any(any(getattr(item, 'text', '') == '已登记的同一作品'
                               for item in parent.descendants())
                            for parent in ancestors(buttons[0])
                            if 'bar-confirm-card' in parent.classes))

    async def test_footer_colors_distinguish_approve_execute_and_end(self):
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        helper = ContentWorkbenchGui('runTest')
        helper.create_page(cleanup_owner=self)
        self.assertEqual('amber-7', helper.view.approve_all_button._props['color'])
        self.assertEqual('white', helper.view.approve_all_button._props['text-color'])
        self.assertEqual('green-7', helper.view.execute_button._props['color'])
        ends = [e for e in helper.view.descendants() if e._props.get('label') == '安全结束任务']
        self.assertEqual('red-7', ends[0]._props['color'])


    async def test_add_dialog_clean_settings_and_real_scroll_target(self):
        from src.pages.add_task_page import AddTaskDialog
        with self.client():
            dialog = AddTaskDialog()
            labels = [getattr(e, 'text', e._props.get('label', '')) for e in dialog.descendants()]
            self.assertNotIn('AI 策略', labels)
            self.assertNotIn('添加后自动关闭，可继续添加下一项；复杂目录会先显示分组供核对。', labels)
            self.assertFalse(any(e._props.get('label') == '可选识别设置' for e in dialog.descendants()))
            self.assertEqual('选择源文件或目录', dialog.path_input._props['placeholder'])
            # This project uses a native overflow body marked for Quasar's
            # getScrollTarget, not QScrollArea. Browser acceptance separately
            # verifies actual overflow and the anchored menu while scrolling.
            self.assertIn('scroll', dialog.content.classes)
            self.assertIn('bar-modal-body', dialog.content.classes)
            self.assertIn(dialog.path_input, tuple(dialog.content.descendants()))

    async def test_reference_failure_keeps_status_without_retry_button_or_repeat_request(self):
        from src.pages.season_reference_view import attach
        from src.rename.season_reference import SeasonReferences
        client = self.client()
        with client:
            owner = ui.dialog()
            owner.task_decision = SimpleNamespace(library_domain='general')
            with owner:
                container = ui.column()
            choice = SimpleNamespace(reference_container=container, annotate=lambda *a, **k: None)
            future = asyncio.get_running_loop().create_future()
            future.set_result(SeasonReferences(status='incomplete'))
            with patch('src.pages.season_reference_view.prepare', return_value=future) as prepare:
                attach(owner, ui, choice, provider=dict(namespace='tmdb', provider_id='1'),
                       title='示例', is_current=lambda: True, on_subject=lambda row: None)
                await choice.reference_task
            self.assertEqual(1, prepare.call_count)
            self.assertFalse(any(e._props.get('label') == '重试季度参考' for e in container.descendants()))
            self.assertTrue(any('资料不完整' in getattr(e, 'text', '') for e in container.descendants()))

    async def test_simulated_history_never_changes_real_task_or_enters_queue(self):
        from development.gui_simulation import remember, history_rows, forget
        from tests.test_gui_interactive_preview import files
        from src.rename.rename_plan import RenamePlan, PlanEntry
        from pathlib import Path
        import tempfile, copy
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, BAR_LOCAL_GUI_PREVIEW='true'):
            root = Path(temp)
            plan = RenamePlan('gui-memory', root, (PlanEntry(root/'one.mkv', root/'target/one.mkv'),))
            task = dict(uuid=plan.task_id, workflow_status='approved', name='示例', path=str(plan.source_root))
            original = copy.deepcopy(task)
            before = files(root)
            with patch('src.rename.plan_execution.PlanExecutionService.execute', side_effect=AssertionError('real execution')):
                remember(task, plan, 'completed')
                rows = history_rows([task])
                self.assertEqual('completed', rows[0]['workflow_status'])
                self.assertTrue(rows[0]['gui_simulation'])
                remember(task, plan, 'failed')
                self.assertEqual('failed', history_rows([task])[0]['workflow_status'])
            self.assertEqual(original, task)
            self.assertEqual(before, files(root))
            with patch.dict(os.environ, BAR_LOCAL_GUI_PREVIEW='false'):
                self.assertEqual([task], history_rows([task]))
                with self.assertRaises(ValueError):
                    remember(task, plan, 'completed')
            self.assertEqual('failed', history_rows([task])[0]['workflow_status'])
            forget(plan.task_id)
            self.assertEqual([task], history_rows([task]))

    async def test_normal_preview_guard_still_refuses_media(self):
        from development.preview import require_media_execution, GuiPreviewBlocked
        with patch.dict(os.environ, BAR_LOCAL_GUI_PREVIEW='true'):
            with self.assertRaises(GuiPreviewBlocked):
                require_media_execution()
        with patch.dict(os.environ, BAR_LOCAL_GUI_PREVIEW='false'):
            require_media_execution()

    async def test_simulated_failure_card_keeps_simulation_message_and_readonly_actions(self):
        from src.pages import data_table_page as dashboard
        task = dict(uuid='sim-card', name='示例', workflow_status='failed',
                    gui_simulation=True, error='假执行预览：模拟失败，没有操作任何文件。')
        client = self.client()
        with client, preview_session():
            dashboard._task_card(task, title_authority=None)
        texts = [getattr(e, 'text', '') for e in client.elements.values()]
        self.assertIn(task['error'], texts)
        for forbidden in ('重新执行', '重建', '核查', '删除', '回滚'):
            self.assertNotIn(forbidden, texts)
        self.assertIn('更改模拟结果', texts)
        self.assertIn('查看整理计划', texts)

    async def test_simulation_dialog_duplicate_close_disconnect_and_writers(self):
        from contextlib import ExitStack
        from pathlib import Path
        import tempfile
        from development import gui_simulation as simulation
        from tests.test_gui_interactive_preview import files
        from src.rename.rename_plan import RenamePlan, PlanEntry
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, BAR_LOCAL_GUI_PREVIEW='true'):
            root = Path(temp)
            plan = RenamePlan('gui-memory', root, (PlanEntry(root/'one.mkv', root/'target/one.mkv'),))
            task = dict(uuid=plan.task_id, name='样本', workflow_status='approved')
            before = files(root)
            client = self.client()
            with client, preview_session(), ExitStack() as stack:
                for name in ('src.task_queue.TaskQueueManager.enqueue_approved',
                             'src.rename.plan_execution.PlanExecutionService.execute',
                             'src.rename.trans.Trans.trans_file'):
                    stack.enter_context(patch(name, side_effect=AssertionError('simulation called writer')))
                stack.enter_context(patch.object(simulation, 'refresh_history'))
                stack.enter_context(patch.object(ui, 'notify'))
                original_remember = simulation.remember
                record = stack.enter_context(patch.object(simulation, 'remember', wraps=original_remember))
                for mode in ('close', 'disconnect', 'complete', 'running'):
                    simulation.forget(plan.task_id)
                    dialog = simulation.SimulationDialog(task, plan)
                    if mode != 'running':
                        dialog.outcome.value = 'completed'
                    dialog.open()
                    worker = asyncio.create_task(dialog.run())
                    await asyncio.sleep(0)
                    self.assertTrue(dialog.progress.visible)
                    await dialog.run()  # duplicate cannot publish twice
                    if mode == 'close':
                        dialog.close()
                    elif mode == 'disconnect':
                        dialog.cancel()  # registered disconnect callback
                    await worker
                    if mode in ('close', 'disconnect'):
                        self.assertEqual([task], simulation.history_rows([task]))
                    elif mode == 'complete':
                        self.assertEqual('completed', simulation.history_rows([task])[0]['workflow_status'])
                    else:
                        from src.pages import data_table_page as dashboard
                        from nicegui.elements.progress import LinearProgress
                        # The ordinary dashboard must show the same in-memory
                        # row in its real queue, not only in a separate dialog.
                        with patch.object(dashboard, 'load_task_rows', return_value=[task]), patch.object(dashboard, '_read_card_decision', return_value=None):
                            rows, _, decisions = dashboard._prepare_dashboard(None, force=True)
                        self.assertEqual('running', rows[0]['workflow_status'])
                        self.assertNotIn('error', rows[0])
                        self.assertEqual(1, dashboard.task_counts(rows)['running'])
                        with ui.column() as queue:
                            dashboard._active_tasks(rows, decisions)
                        labels = [getattr(e, 'text', '') for e in queue.descendants()]
                        self.assertEqual(1, labels.count('处理中'))
                        self.assertEqual(1, sum(isinstance(e, LinearProgress) for e in queue.descendants()))
                        self.assertIn('移除模拟展示', labels)
                        self.assertNotIn('删除', labels)
                        # No timeout hides the state before the user can inspect it.
                        await asyncio.sleep(0)
                        self.assertEqual('running', simulation.history_rows([task])[0]['workflow_status'])
                self.assertEqual(2, record.call_count)
            self.assertEqual(before, files(root))
            simulation.forget(plan.task_id)
