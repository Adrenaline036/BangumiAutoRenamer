"""作品链接提交后的队列转场与紧凑卡片。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch
import unittest
from nicegui import ui, Client
from src.pages import plan_review_page as page, provider_selection_work as work, data_table_page as dashboard
from src.utils.task_changes import revision
from src.pages.dashboard_view import DashboardView
from tests import test_planning_lifecycle_controls as lifecycle
from tests.test_content_choice_contract import records


class QueueDisplay(unittest.IsolatedAsyncioTestCase):
    flow=lifecycle.PlanningLifecycleControls.flow
    admit_auto=lifecycle.PlanningLifecycleControls.admit_auto
    dialog=lifecycle.PlanningLifecycleControls.dialog
    candidate=staticmethod(lifecycle.PlanningLifecycleControls.candidate)

    async def test_actual_click_closes_and_projects_before_busy_lock_or_disk_read(self):
        async with self.flow(movie=True,automatic=False,admission_only=True,admit=self.admit_auto) as f:
            view=self.dialog(f); published=[]
            with view.client:
                with ui.column() as workspace:
                    front=DashboardView(render_card=lambda row: None,refresh=lambda **kw: None)
            tabs=next(e for e in workspace.descendants() if e.tag=='q-tabs')
            tabs.set_value('review')
            publish=front.publish
            front.publish=lambda *a: (published.append(a),publish(*a))
            key=view.client.id
            row=dashboard._card_projection(f.manager._read(f.task_id))
            await f.manager._admission_lock.acquire()
            before=records(f.root); seen=revision()
            with patch.object(dashboard,'task_queue',f.manager),patch.dict(dashboard._dashboard_views,{key:front}),patch.object(dashboard,'_prepare_dashboard',side_effect=AssertionError('不得等待全列表磁盘读取')):
                dashboard._render_dashboard(key,([row],(),{}))
                click=asyncio.create_task(view._select_manual_provider_url())
                try:
                    done,_=await asyncio.wait({click},timeout=.15)
                    self.assertTrue(done,'后台接收后必须立即返回，不等读取或计划生成')
                    self.assertFalse(view.value)
                    self.assertTrue(view.exit_requested)
                    self.assertGreater(revision(),seen)
                    self.assertEqual([f.task_id],[r['uuid'] for r in published[-1][0]['active']])
                    self.assertEqual([],published[-1][0]['review'])
                    self.assertEqual('review',tabs.value)
                    self.assertEqual(before,records(f.root))
                    request=f.manager._provider_reads[f.task_id]
                    self.assertFalse(request.worker.done())
                    self.assertTrue(await work.cancel(f.manager,f.task_id))
                finally:
                    request=f.manager._provider_reads.get(f.task_id)
                    if request and not request.worker.done():await work.cancel(f.manager,f.task_id)
                    if not click.done():click.cancel()
                    await asyncio.gather(click,return_exceptions=True)
                    f.manager._admission_lock.release();view.delete()

    async def test_busy_review_entry_never_loads_temporary_plan(self):
        async with self.flow(movie=True,automatic=False,admission_only=True,admit=self.admit_auto) as f:
            view=self.dialog(f);await f.manager._admission_lock.acquire()
            try:
                request=await work.submit(view)
                with view.client:
                    with ui.column() as workspace:
                        front=DashboardView(render_card=lambda row: None,refresh=lambda **kw: None)
                tabs=next(e for e in workspace.descendants() if e.tag=='q-tabs')
                tabs.set_value('review')
                with patch.object(dashboard,'task_queue',f.manager),patch.dict(dashboard._dashboard_views,{view.client.id:front}):
                    dashboard._render_dashboard(view.client.id,([dashboard._card_projection(f.manager._read(f.task_id))],(),{}))
                with view.client,patch.dict(dashboard._dashboard_views,{view.client.id:front}),patch.object(page.PlanStore,'load',side_effect=AssertionError('不可读取处理中间计划')),patch.object(page,'_prepare_visible_plan_dialog',new=AsyncMock()) as prepare,patch.object(dashboard,'task_queue',f.manager):
                    await asyncio.wait_for(page._open_plan_review_page(f.task_id,view.client),1)
                    prepare.assert_not_awaited()
                    self.assertEqual('active',tabs.value)
                    self.assertEqual([f.task_id],[r['uuid'] for r in front.sections['active'].rows])
                self.assertFalse(request.worker.done())
            finally:
                if 'request' in locals():await work.cancel(f.manager,f.task_id)
                f.manager._admission_lock.release();view.delete()

    async def test_registration_and_read_failure_both_notify_refresh(self):
        async with self.flow(movie=True,automatic=False,admission_only=True,admit=self.admit_auto) as f:
            view=self.dialog(f);entered,release=asyncio.Event(),asyncio.Event()
            async def reader(_):entered.set();await release.wait();raise ValueError('链接读取失败')
            try:
                before=revision()
                with patch.object(page,'_read_manual_provider_url',side_effect=reader):
                    request=await work.submit(view)
                    self.assertGreater(revision(),before)
                    await entered.wait();reading=revision()
                    release.set();await request.worker
                    self.assertGreater(revision(),reading)
                rows=work.overlay_rows(f.manager,[dashboard._card_projection(f.manager._read(f.task_id))])
                self.assertFalse(rows[0].get('_selection_wait'))
                self.assertIn('链接读取失败',rows[0]['_display']['feedback'])
            finally:
                release.set()
                if 'request' in locals():await request.worker
                view.delete()

    async def test_dialog_has_no_wait_button_or_progress_labels(self):
        async with self.flow(movie=True,automatic=False,admission_only=True,admit=self.admit_auto) as f:
            view=self.dialog(f)
            try:
                texts=[str(getattr(e,'text','')) for e in view.descendants()]
                self.assertIsNone(view.plan)
                self.assertIsNone(f.service.decision_state_store.load_optional(f.task_id))
                self.assertFalse(f.store.path_for(f.task_id).exists())
                self.assertFalse(any(e.tag=='q-expansion-item' and view.source_file_list.container in list(e.descendants()) for e in view.descendants()))
                self.assertIn(view.source_file_list.container,list(view.plan_region.descendants()))
                self.assertTrue({'bar-review-entries','w-full','gap-2'} <= set(view.source_file_list.entries_container._classes))
                self.assertNotIn('取消等待',texts)
                self.assertFalse(any('已用时' in t or '正在准备任务' in t or '正在读取作品链接' in t for t in texts))
            finally:view.delete()

    def test_running_and_selection_cards_are_compact_and_have_blue_badges(self):
        for status,selection in [('running',False),('need_confirm',True)]:
            with self.subTest(status=status):
                data=dict(uuid='sample',workflow_status=status,path='sample/source',name='等待识别',error='[作品搜索] 旧搜索提示',plan_entry_count=5)
                row=dashboard._card_projection(data)
                if selection:row.update(_selection_wait=True,_selection_stage='准备任务',_selection_note='正在准备任务 已用时 20 秒',_selection_cancel=True)
                with Client.auto_index_client.content:
                    with ui.column() as box:dashboard._task_card(row)
                try:
                    texts=[str(getattr(e,'text','')) for e in box.descendants()]
                    self.assertFalse(any('旧搜索提示' in t or '已用时' in t or '整理计划 5' in t or t=='等待识别' for t in texts),texts)
                    badges=[e for e in box.descendants() if e.tag=='q-badge' and getattr(e,'text','') in ('处理中','准备任务')]
                    self.assertEqual(1,len(badges))
                    self.assertEqual('amber-7' if selection else 'blue-7',badges[0]._props.get('color'))
                    self.assertFalse(badges[0]._props.get('rounded'))
                finally:box.delete()

    def test_real_failure_in_review_keeps_reason(self):
        with Client.auto_index_client.content:
            with ui.column() as box:dashboard._task_card(dict(uuid='failed',workflow_status='failed',error='本次链接读取失败'),title_authority=None)
        try:self.assertIn('本次链接读取失败',[getattr(e,'text','') for e in box.descendants()])
        finally:box.delete()
