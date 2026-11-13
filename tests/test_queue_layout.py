"""待处理的即时排序与刷新稳定性，使用实际工作台组件。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from nicegui import Client, ui
from src.pages import data_table_page as dashboard
from src.pages.dashboard_view import DashboardView


class QueueLayout(unittest.TestCase):
    def test_return_is_first_before_scan_and_keeps_order_after_refresh(self):
        manager = SimpleNamespace(_pending=[], _provider_reads={})
        client = Client.auto_index_client
        tasks = [dict(uuid=name, workflow_status=status, path=name,review_requested_at='2026-11-06T00:00:00Z')
                 for name, status in [('old-a','need_confirm'), ('old-b','invalid'),
                                      ('returned','queued'), ('second','running')]]
        rows = [dashboard._card_projection(task) for task in tasks]
        with client:
            with ui.column() as box:
                view = DashboardView(render_card=lambda row: ui.label(row['uuid']), refresh=lambda **kw: None)
        def order():
            section = view.sections['review']
            self.assertEqual([r['uuid'] for r in section.rows],
                [next(w.descendants()).text for w in section.container.default_slot.children])
            return [r['uuid'] for r in section.rows]
        try:
            with patch.object(dashboard,'task_queue',manager), patch.dict(dashboard._dashboard_views,{client.id:view}), patch.dict(dashboard._dashboard_rows,{},clear=True):
                with client:
                    dashboard._render_dashboard(client.id,(rows,(),{}))
                    view.tabs.set_value('review')
                    self.assertEqual(['old-a','old-b'],order())
                    with patch.object(dashboard,'_prepare_dashboard',side_effect=AssertionError('不可等待整表读取')):
                        dashboard.project_selection_change(manager,{**tasks[2],'workflow_status':'need_confirm','review_requested_at':'2026-11-06T00:10:00Z'})
                    self.assertEqual(['returned','old-a','old-b'],order())
                    # 后台旧顺序或修改时间顺序都不能让已返回的卡片跳动。
                    refreshed=[dashboard._card_projection({**t,'workflow_status':'need_confirm'} if t['uuid']=='returned' else t) for t in tasks]
                    dashboard._render_dashboard(client.id,(refreshed,(),{}))
                    self.assertEqual(['returned','old-a','old-b'],order())
                    dashboard.project_selection_change(manager,{**tasks[3],'workflow_status':'need_confirm','review_requested_at':'2026-11-06T00:20:00Z'})
                    self.assertEqual(['second','returned','old-a','old-b'],order())
                    dashboard._render_dashboard(client.id,(list(reversed(dashboard._dashboard_rows[client.id])),(),{}))
                    self.assertEqual(['second','returned','old-a','old-b'],order())
                    self.assertEqual('review',view.tabs.value)
                    # 删除不留下幽灵行，再入队后再次返回仍应到最前。
                    dashboard.project_selection_change(manager,removed='old-a')
                    self.assertEqual(['second','returned','old-b'],order())
                    dashboard.project_selection_change(manager,{**tasks[2],'workflow_status':'queued'})
                    self.assertEqual(['second','old-b'],order())
                    dashboard.project_selection_change(manager,{**tasks[2],'workflow_status':'need_confirm'})
                    self.assertEqual(['returned','second','old-b'],order())
        finally:
            dashboard._dashboard_signatures.pop(client.id,None)
            box.delete()


class LinkFailureOrder(unittest.IsolatedAsyncioTestCase):
    async def test_link_failure_returns_first_without_changing_persisted_review_time(self):
        import asyncio
        from datetime import datetime, timedelta
        from unittest.mock import AsyncMock
        from tests import test_planning_lifecycle_controls as fixture
        from tests.test_content_choice_contract import records
        from src.pages import plan_review_page as page
        helper = fixture.PlanningLifecycleControls('runTest')
        async with helper.flow(movie=True,automatic=False,admission_only=True,admit=helper.admit_auto) as f:
            dialog = helper.dialog(f)
            with dialog.client:
                with ui.column() as box:
                    view = DashboardView(render_card=lambda row: None,refresh=lambda **kw: None)
            initial = f.manager._read(f.task_id)
            newer_time=(datetime.fromisoformat(initial['review_requested_at'])+timedelta(seconds=1)).isoformat()
            rows = [dashboard._card_projection(dict(uuid='older',workflow_status='need_confirm',review_requested_at=newer_time)),
                    dashboard._card_projection(initial)]
            before = records(f.root)
            try:
                with patch.object(dashboard,'task_queue',f.manager), patch.dict(dashboard._dashboard_views,{dialog.client.id:view}), patch.dict(dashboard._dashboard_rows,{},clear=True):
                    with dialog.client:
                        dashboard._render_dashboard(dialog.client.id,(rows,(),{}))
                        view.tabs.set_value('review')
                        self.assertEqual(['older',f.task_id],[r['uuid'] for r in view.sections['review'].rows])
                        with patch.object(page,'_read_manual_provider_url',new=AsyncMock(side_effect=ValueError('请求超时'))):
                            await dialog._select_manual_provider_url()
                            request=f.manager._provider_reads[f.task_id]
                            await request.worker
                            await asyncio.sleep(.05)
                        self.assertEqual([f.task_id,'older'],[r['uuid'] for r in view.sections['review'].rows])
                        self.assertEqual(before,records(f.root))
                        self.assertEqual(initial.get('review_requested_at'),f.manager._read(f.task_id).get('review_requested_at'))
                        dashboard._render_dashboard(dialog.client.id,(rows,(),{}))
                        self.assertEqual([f.task_id,'older'],[r['uuid'] for r in view.sections['review'].rows])
            finally:
                dialog.delete();box.delete()
