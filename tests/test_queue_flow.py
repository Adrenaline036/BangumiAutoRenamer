"""队列失败回正常页面、来源恢复与删除短路径；仅隔离状态。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch, Mock, AsyncMock
import unittest
from nicegui import ui, Client
from src.pages import plan_review_page as page, provider_selection_work as work, data_table_page as dashboard
from src.pages.recognition_recovery_view import RecognitionRecoveryDialog
from src.rename.plan_execution import PlanExecutionError
from tests import test_planning_lifecycle_controls as lifecycle
from tests.test_content_choice_contract import records

class QueueFlow(unittest.IsolatedAsyncioTestCase):
    flow=lifecycle.PlanningLifecycleControls.flow
    admit_auto=lifecycle.PlanningLifecycleControls.admit_auto
    dialog=lifecycle.PlanningLifecycleControls.dialog
    candidate=staticmethod(lifecycle.PlanningLifecycleControls.candidate)

    async def test_failure_returns_review_and_reopens_normal_input_without_shared_wait(self):
        for message in ('[作品选择] Bangumi 条目读取失败：请求超时', '[作品选择] 无法按所选 TMDB ID 获取完整详情，请稍后重试'):
            with self.subTest(message=message):
                async with self.flow(movie=True,automatic=False,admission_only=True,admit=self.admit_auto) as f:
                    view=self.dialog(f); key=view.client.id; published=[]
                    front=SimpleNamespace(publish=lambda *a:published.append(a),show_queue=Mock())
                    before=records(f.root); url=view.provider_url_input.value
                    try:
                        with patch.object(dashboard,'task_queue',f.manager),patch.dict(dashboard._dashboard_views,{key:front}):
                            dashboard._render_dashboard(key,([dashboard._card_projection(f.manager._read(f.task_id))],(),{}))
                            with patch.object(page,'_read_manual_provider_url',new=AsyncMock(side_effect=ValueError(message))):
                                await asyncio.wait_for(view._select_manual_provider_url(),1)
                                request=f.manager._provider_reads[f.task_id]
                                await asyncio.wait_for(request.worker,5)
                                await asyncio.sleep(.05)
                            self.assertFalse(view.value)
                            self.assertEqual([f.task_id],[r['uuid'] for r in published[-1][0]['review']])
                            self.assertFalse(published[-1][0]['active'])
                            self.assertIn(message,published[-1][0]['review'][0]['_display']['feedback'])
                            self.assertEqual(before,records(f.root))
                            seen=[]
                            def inspect_open(dialog):
                                async def finish():
                                    seen.append(dialog)
                                    self.assertEqual(url,dialog.provider_url_input.value)
                                    self.assertTrue(dialog.provider_submit_button.enabled)
                                    dialog._close_review()
                                return finish().__await__()
                            await f.manager._admission_lock.acquire()
                            try:
                                with view.client,patch.object(page.PlanReviewDialog,'__await__',inspect_open),patch.object(f.manager.batch_scope_store,'_mutation_lock',side_effect=AssertionError('开窗不等共享登记')):
                                    await asyncio.wait_for(page._open_plan_review_page(f.task_id,view.client),1)
                            finally:f.manager._admission_lock.release()
                            self.assertEqual(1,len(seen))
                    finally:view.delete()

    async def test_terminal_observation_outranks_running_observer_and_old_search_error_does_not(self):
        async with self.flow(movie=True,automatic=False,admission_only=True,admit=self.admit_auto) as f:
            view=self.dialog(f); await f.manager._admission_lock.acquire()
            try:
                request=await work.submit(view)
                row=dashboard._card_projection({**f.manager._read(f.task_id),'error':'旧搜索提示'})
                self.assertTrue(work.overlay_rows(f.manager,[row])[0].get('_selection_wait'))
                request.error='本次读取失败'
                request.status='无法应用作品选择：本次读取失败'
                row=work.overlay_rows(f.manager,[row])[0]
                self.assertFalse(row.get('_selection_wait'))
                self.assertTrue(row.get('_selection_review'))
                self.assertEqual('need_confirm',row['workflow_status'])
                request.error='';request.route='tv';request.candidate=self.candidate()
                row=work.overlay_rows(f.manager,[dashboard._card_projection(f.manager._read(f.task_id))])[0]
                self.assertTrue(row.get('_selection_review'))
            finally:
                await work.cancel(f.manager,f.task_id)
                f.manager._admission_lock.release();view.delete()

    async def test_recovery_uses_normal_frame_and_two_shared_footer_buttons(self):
        async with self.flow(movie=True,automatic=False,admission_only=True,admit=self.admit_auto) as f:
            raw=b'broken historical plan'
            f.store.root.mkdir(parents=True,exist_ok=True)
            f.store.path_for(f.task_id).write_bytes(raw)
            seen=[]
            constructors=[]
            original_init=page.PlanReviewDialog.__init__
            def shared_init(dialog,*args,**kwargs):
                constructors.append(dialog)
                return original_init(dialog,*args,**kwargs)
            def inspect_open(dialog):
                async def finish():
                    seen.append(dialog)
                    self.assertIsInstance(dialog,page.PlanReviewDialog)
                    self.assertTrue(hasattr(dialog,'selection_region'))
                    elements=list(dialog.descendants())
                    self.assertLess(elements.index(dialog.provider_url_input),elements.index(dialog.source_file_list.container))
                    self.assertFalse(any(e.tag=='q-expansion-item' and dialog.source_file_list.container in list(e.descendants()) for e in elements))
                    self.assertIn(dialog.source_file_list.container,list(dialog.plan_region.descendants()))
                    self.assertTrue({'bar-review-entries','w-full','gap-2'} <= set(dialog.source_file_list.entries_container._classes))
                    foot=[e for e in dialog.actions_region.descendants() if e.tag=='q-btn']
                    self.assertEqual(['安全结束任务','关闭'],[e.text for e in foot])
                    self.assertEqual('close',foot[-1]._props.get('icon'))
                    self.assertIn('ml-auto',foot[-1]._classes)
                    dialog._close_review()
                return finish().__await__()
            with Client.auto_index_client,patch.object(RecognitionRecoveryDialog,'__await__',inspect_open),patch.object(page.PlanReviewDialog,'__init__',shared_init):
                await page._open_plan_review_page(f.task_id,Client.auto_index_client)
            self.assertEqual(1,len(seen))
            self.assertEqual(seen,constructors)
            self.assertEqual(raw,f.store.path_for(f.task_id).read_bytes())
            self.assertIsNone(f.service.decision_state_store.load_optional(f.task_id))

    async def test_delete_queued_task_does_not_read_or_renumber_other_tasks(self):
        async with self.flow(movie=True,automatic=False) as f:
            original_task=f.manager._read(f.task_id)
            ownership=f.manager.projection_commit_service.ownership_store.load_optional(f.task_id)
            self.assertIsNotNone(ownership)
            self.assertEqual('active',ownership.state)
            before_plan=f.store.path_for(f.task_id).read_bytes()
            catalog=f.manager.execution_service.work_catalog.root
            catalog_before={p:p.read_bytes() for p in catalog.rglob('*.json')}
            f.manager._pending.extend([f.task_id,'unrelated-queued'])
            f.manager.active_task_id='unrelated-running'
            original=f.manager._read
            def only_target(task_id):
                if task_id=='unrelated-queued':raise AssertionError('删除不能等待其他任务的序号读写')
                return original(task_id)
            media={p:p.read_bytes() for p in f.source.rglob('*') if p.is_file()}
            try:
                with patch.object(f.manager,'_read',side_effect=only_target):
                    await asyncio.wait_for(f.manager.abandon_and_remove(f.task_id,record_root=f.manager.execution_service.record_root,plan_root=f.store.root,review_root=f.manager.review_root),2)
                self.assertFalse(original(f.task_id))
                self.assertEqual(['unrelated-queued'],f.manager._pending)
                self.assertEqual(media,{p:p.read_bytes() for p in f.source.rglob('*') if p.is_file()})
                manifest=f.manager.batch_scope_store.load(original_task['batch_id'])
                self.assertTrue(all(r.retired for r in manifest.reservations if r.owner_task_id==f.task_id))
                retired=f.manager.projection_commit_service.ownership_store.load_optional(f.task_id)
                self.assertEqual('retired',retired.state)
                self.assertGreater(retired.revision,ownership.revision)
                self.assertEqual(before_plan,f.store.path_for(f.task_id).read_bytes())
                self.assertEqual(catalog_before,{p:p.read_bytes() for p in catalog.rglob('*.json')})
            finally:
                f.manager.active_task_id=None;f.manager._pending.clear()

    async def test_delete_active_task_fails_before_any_disk_transaction(self):
        async with self.flow(movie=True,automatic=False,admission_only=True,admit=self.admit_auto) as f:
            f.manager.active_task_id=f.task_id; before=records(f.root)
            try:
                with self.assertRaises(PlanExecutionError):
                    await asyncio.wait_for(f.manager.abandon_and_remove(f.task_id,record_root=f.manager.execution_service.record_root,plan_root=f.store.root,review_root=f.manager.review_root),.2)
                self.assertEqual(before,records(f.root))
            finally:f.manager.active_task_id=None

    async def test_worker_start_during_delete_acquisition_preserves_original_records(self):
        async with self.flow(movie=True,automatic=False,admission_only=True,admit=self.admit_auto) as f:
            before=records(f.root);original=f.manager._abandon_and_remove
            def raced(*args,**kwargs):
                f.manager.active_task_id=f.task_id
                return original(*args,**kwargs)
            try:
                with patch.object(f.manager,'_abandon_and_remove',side_effect=raced),self.assertRaises(PlanExecutionError):
                    await f.manager.abandon_and_remove(f.task_id,record_root=f.manager.execution_service.record_root,plan_root=f.store.root,review_root=f.manager.review_root)
                self.assertEqual(before,records(f.root))
            finally:f.manager.active_task_id=None

    async def test_delete_button_is_busy_and_deduplicated_until_real_result(self):
        with Client.auto_index_client.content:
            with ui.column() as box:
                dashboard._task_card(dict(uuid='delete-sample',workflow_status='queued',path='sample'),title_authority=None)
        button=next(e for e in box.descendants() if e.tag=='q-btn' and e.text=='删除')
        entered,release=asyncio.Event(),asyncio.Event()
        async def deleting(_):entered.set();await release.wait();return False
        try:
            with patch.object(dashboard,'_submit_safe_end',side_effect=deleting) as submit,patch.object(dashboard,'get_task',return_value={}):
                import inspect
                callbacks=[]
                with patch('nicegui.elements.button.handle_event',side_effect=lambda cb,event:callbacks.append((cb,event))):
                    next(e for e in button._event_listeners.values() if e.type=='click').handler(None)
                callback,event=callbacks[0]
                required=[p for p in inspect.signature(callback).parameters.values() if p.default is inspect.Parameter.empty]
                first=asyncio.create_task(callback(event) if required else callback())
                await asyncio.wait_for(entered.wait(),1)
                self.assertFalse(button.enabled)
                self.assertEqual('正在删除',button.text)
                self.assertFalse(await dashboard._delete_task('delete-sample'))
                self.assertEqual(1,submit.await_count)
                release.set();await first
                self.assertTrue(button.enabled)
                self.assertEqual('删除',button.text)
        finally:release.set();box.delete()

    async def test_pending_source_group_uses_normal_frame_and_keeps_confirmation(self):
        async with self.flow(movie=True,automatic=False,admission_only=True,admit=self.admit_auto) as f:
            data={**f.manager._read(f.task_id),'source_group_pending':True}
            with Client.auto_index_client.content:
                view=RecognitionRecoveryDialog(f.task_id,data,ValueError('来源待确认'))
            try:
                self.assertIsInstance(view,page.PlanReviewDialog)
                buttons=[e for e in view.descendants() if e.tag=='q-btn']
                self.assertIn('继续识别来源',[b.text for b in buttons])
                self.assertFalse(any(e.tag=='q-expansion-item' and view.source_file_list.container in list(e.descendants()) for e in view.descendants()))
                self.assertIn(view.source_file_list.container,list(view.plan_region.descendants()))
                self.assertTrue({'bar-review-entries','w-full','gap-2'} <= set(view.source_file_list.entries_container._classes))
                self.assertFalse(any(b.text=='读取链接并选择此作品' and b.enabled for b in buttons))
                self.assertEqual(['安全结束任务','关闭'],[e.text for e in view.actions_region.descendants() if e.tag=='q-btn'])
            finally:view.delete()

    def test_pending_order_after_delete_and_readmission_ignores_old_disk_positions(self):
        manager=SimpleNamespace(_pending=['B','C','old-D'],_provider_reads={})
        rows=[dashboard._card_projection(dict(uuid=task_id,workflow_status='queued',queue_position=position,created_at=created))
              for task_id,position,created in [('B',2,'2026-11'),('C',3,'2026-11'),('old-D',3,'2020-01')]]
        projected=work.overlay_rows(manager,rows)
        self.assertEqual(manager._pending,[r['uuid'] for r in dashboard.sort_active_tasks(projected)])
        self.assertEqual([1,2,3],[r['queue_position'] for r in dashboard.sort_active_tasks(projected)])


class ResumedSelectionNotification(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_race_observer_failure_publishes_review_immediately(self):
        entered, release = asyncio.Event(), asyncio.Event()
        manager = SimpleNamespace(_provider_reads={}, _pending=[], is_active=lambda _: True)
        request = SimpleNamespace(task_id='resume', request_id='request', admitted=True,
            cancelling=True, cancelled=False, error='', route='', result=None, status='waiting')
        manager._provider_reads[request.task_id] = request
        request.worker = asyncio.create_task(asyncio.Event().wait())
        async def observe(*_):
            entered.set()
            await release.wait()
            raise ValueError('恢复观察失败')
        published=[]
        row={'uuid':request.task_id,'workflow_status':'running','_display':{'feedback':''}}
        with patch('src.rename.selection_wait.withdraw',new=AsyncMock(return_value=False)), \
             patch.object(work,'_observe_result',side_effect=observe), \
             patch.object(work,'_changed',side_effect=lambda *_:published.append(work.overlay_rows(manager,[row])[0])), \
             patch.object(work,'_publish_result',new=AsyncMock()):
            self.assertFalse(await work._cancel(manager,request))
            await entered.wait()
            self.assertTrue(published[-1]['_selection_wait'])
            release.set()
            await request.worker
            self.assertTrue(published[-1]['_selection_review'])
            self.assertEqual('need_confirm',published[-1]['workflow_status'])
