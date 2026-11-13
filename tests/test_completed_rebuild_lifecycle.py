"""真实重建按钮的后台所有权与旧计划保留。"""
import asyncio
import threading
import unittest
from unittest.mock import patch

from nicegui.client import Client
from nicegui.page import page
from src.pages import completed_rebuild_view as view
from src.task_queue import TaskQueueManager
from src.rename.plan_store import PlanStore
from tests import test_completed_rebuild_work_contract as fixture
from tests._gui_helpers import GuiDriver


class CompletedRebuildLifecycle(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, mode):
        helper = fixture.CompletedMovieRebuildContracts('runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        old = await helper.single(with_font=False)
        original_fp = PlanStore.fingerprint(old.plan)
        original_task = dict(old.task)
        source_before = fixture.file_tree(helper.root / 'sources')
        target = old.plan.entries[0].target_path
        retained = helper.root / 'retained-materialization.mkv'
        self.assertFalse(retained.exists())
        target.rename(retained)
        execution = helper.execution_service()
        service = execution.rebuild_service
        manager = TaskQueueManager(task_root=helper.root/'tasks', plan_root=helper.root/'plans',
            decision_state_root=helper.root/'decisions', execution_service=execution,
            recovery_abandon_audit_root=helper.root/'abandon')
        client = Client(page('/completed-rebuild-lifecycle'), request=None)
        driver = GuiDriver('runTest')
        driver.client = client
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original_generate = service.generate
        def generate(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('isolated generate barrier')
            try:
                return original_generate(*args, **kwargs)
            finally:
                finished.set()
        clicking = stopping = None
        await manager.start()
        try:
            inspection = await asyncio.to_thread(service.inspect, old.plan.task_id)
            self.assertEqual('missing', inspection.state)
            with patch('src.task_queue.task_queue', manager), patch.object(view, 'notify') as notify:
                with client:
                    dialog = await view.open_rebuild_dialog(old.plan.task_id)
                if mode == 'disconnect':
                    before = fixture.file_tree(helper.root)
                    with patch.object(manager, 'generate_completed_rebuild', create=True,
                            side_effect=ValueError('isolated queue owner disconnected')) as entry, \
                            patch.object(service, 'generate', wraps=original_generate) as backend:
                        await driver.click('生成待批准计划')
                    entry.assert_called_once_with(old.plan.task_id, expected_fingerprint=inspection.fingerprint)
                    backend.assert_not_called()
                    self.assertEqual(before, fixture.file_tree(helper.root))
                    return
                if mode == 'stale':
                    intervening = await asyncio.to_thread(original_generate, old.plan.task_id,
                                                           expected_fingerprint=inspection.fingerprint)
                    retained.rename(target)
                    self.assertEqual("complete", (await asyncio.to_thread(service.inspect, old.plan.task_id)).state)
                    before = fixture.file_tree(helper.root)
                    await driver.click('生成待批准计划')
                    self.assertEqual(before, fixture.file_tree(helper.root))
                    self.assertEqual(PlanStore.fingerprint(intervening),
                        PlanStore.fingerprint(service.plans.load(old.plan.task_id)))
                    return
                with patch.object(service, 'generate', side_effect=generate):
                    clicking = asyncio.create_task(driver.click('生成待批准计划'))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    if mode == 'cancel':
                        clicking.cancel()
                    elif mode == 'stop':
                        stopping = asyncio.create_task(manager.stop())
                    elif mode == 'close':
                        with client:
                            dialog.close()
                    await asyncio.sleep(.05)
                    pending = not (stopping if mode == 'stop' else clicking).done()
                    release.set()
                    if mode == 'cancel':
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(clicking, 10)
                    else:
                        await asyncio.wait_for(clicking, 10)
                    if stopping is not None:
                        await asyncio.wait_for(stopping, 10)
                    self.assertTrue(pending, '实际计划修复尚未完成，调用或停止却已返回')
                    self.assertTrue(finished.is_set())
                current = helper.current(old.plan.task_id)
                self.assertEqual(old.plan.task_id, current.plan.task_id)
                self.assertNotEqual(original_fp, PlanStore.fingerprint(current.plan))
                self.assertEqual(original_fp,
                    PlanStore.fingerprint(service.plans.load_version(old.plan.task_id, original_fp)))
                self.assertFalse(current.review.load_or_create(current.plan).approved_entry_ids)
                for key in ('uuid', 'name', 'season_id', 'season_number'):
                    self.assertEqual(original_task.get(key), current.task.get(key), key)
                self.assertEqual(source_before, fixture.file_tree(helper.root/'sources'))
                self.assertFalse(target.exists(), '生成计划不能执行媒体修复')
                self.assertTrue(retained.exists())
                self.assertFalse(any(call.kwargs.get('type') == 'negative' for call in notify.call_args_list))
        finally:
            release.set()
            if clicking is not None:
                await asyncio.gather(clicking, return_exceptions=True)
            if stopping is not None:
                await asyncio.gather(stopping, return_exceptions=True)
            if entered.is_set():
                await asyncio.to_thread(finished.wait, 10)
            await manager.stop()
            client.delete()

    async def test_generated_plan_preserves_old_history_and_requires_approval(self):
        await self.exercise('normal')

    async def test_close_keeps_background_owner_until_actual_commit(self):
        await self.exercise('close')

    async def test_cancel_waits_for_real_generate_and_does_not_report_failure(self):
        await self.exercise('cancel')

    async def test_stop_waits_for_real_generate(self):
        await self.exercise('stop')

    async def test_disconnected_queue_cannot_fall_back_to_direct_generate(self):
        await self.exercise('disconnect')

    async def test_stale_open_dialog_cannot_replace_new_plan(self):
        await self.exercise('stale')
