"""真实换源执行失败后，恢复窗口重新生成计划的生命周期。"""
import asyncio
import threading
import unittest
from unittest.mock import Mock, patch
from nicegui.client import Client
from nicegui.page import page
from src.pages import asset_recovery_view
from src.rename.plan_execution import PlanExecutionError
from src.rename.plan_store import PlanStore
from src.task_queue import TaskQueueManager
from tests import test_asset_execution_recovery_contract as test_asset_execution_recovery_contract
from tests.test_completed_rebuild_work_contract import file_identity
from tests._gui_helpers import GuiDriver


class RecoveryReplanLifecycle(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, mode):
        helper = test_asset_execution_recovery_contract.AssetExecutionRecoveryContracts('runTest')
        helper.addCleanup = self.addCleanup
        helper.setUp()
        plan, service, transfer = await helper.approved()
        old_fp = PlanStore.fingerprint(plan)
        before = {entry.source_path:file_identity(entry.source_path) for entry in plan.entries}
        before.update({entry.target_path:file_identity(entry.target_path) for entry in plan.entries})
        with patch('src.rename.trans.Trans._write_replacement_journal',
                   side_effect=RuntimeError('isolated prejournal failure')):
            with self.assertRaises(PlanExecutionError):
                service.execute(plan.task_id, old_fp)
        preparation = helper.recovery(service).inspect(plan.task_id, old_fp)
        self.assertEqual('replan_required', preparation.state)
        manager = TaskQueueManager(task_root=helper.root/'tasks', plan_root=helper.root/'plans',
            decision_state_root=helper.root/'decisions', execution_service=service,
            recovery_abandon_audit_root=helper.root/'abandon')
        original_task = manager._read(plan.task_id)
        client = Client(page('/recovery-replan-lifecycle'), request=None)
        self.addCleanup(client.delete)
        driver = GuiDriver('runTest')
        driver.client = client
        refresh = Mock()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = service.review_service.confirm_asset_replacement
        def confirm(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('isolated recovery confirmation barrier')
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()
        clicking = stopping = None
        await manager.start()
        try:
            with patch.object(asset_recovery_view, 'notify'):
                with client:
                    self.assertTrue(await asset_recovery_view.open_asset_recovery(manager, plan.task_id, refresh))
                if mode == 'disconnect':
                    with patch.object(manager, 'confirm_asset_plan', side_effect=RuntimeError('确认入口已断开')) as entry, \
                            patch.object(service.review_service, 'confirm_asset_replacement') as backend:
                        await driver.click('重新生成')
                        entry.assert_called_once()
                        backend.assert_not_called()
                    self.assertEqual(old_fp, PlanStore.fingerprint(service.plan_store.load(plan.task_id)))
                    self.assertEqual(original_task, manager._read(plan.task_id))
                    self.assertEqual(before, {path:file_identity(path) for path in before})
                    return
                with patch.object(service.review_service, 'confirm_asset_replacement', side_effect=confirm):
                    clicking = asyncio.create_task(driver.click('重新生成'))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    if mode in {'cancel', 'cancel_twice'}:
                        clicking.cancel()
                        if mode == 'cancel_twice':
                            await asyncio.sleep(.01)
                            clicking.cancel()
                    elif mode == 'stop':
                        stopping = asyncio.create_task(manager.stop())
                    await asyncio.sleep(.05)
                    pending = not (stopping if stopping is not None else clicking).done()
                    release.set()
                    if mode in {'cancel', 'cancel_twice'}:
                        with self.assertRaises(asyncio.CancelledError):
                            await clicking
                    else:
                        await clicking
                    if stopping is not None:
                        await stopping
                    self.assertTrue(pending, '恢复计划尚未生成，页面或停止已返回')
                    self.assertTrue(finished.is_set())
            current = service.plan_store.load(plan.task_id)
            self.assertNotEqual(old_fp, PlanStore.fingerprint(current))
            self.assertEqual(old_fp, PlanStore.fingerprint(service.plan_store.load_version(plan.task_id, old_fp)))
            self.assertFalse(service.review_service.load_or_create(current).approved_entry_ids)
            self.assertEqual(before, {path:file_identity(path) for path in before})
            for key in ('uuid','name','season_id','season_number'):
                self.assertEqual(original_task.get(key), manager._read(plan.task_id).get(key), key)
        finally:
            release.set()
            if clicking is not None:
                await asyncio.gather(clicking, return_exceptions=True)
            if stopping is not None:
                await asyncio.gather(stopping, return_exceptions=True)
            if entered.is_set():
                await asyncio.to_thread(finished.wait, 10)
            await manager.stop()

    async def test_normal_replan_preserves_old_approval_history_and_media(self):
        await self.exercise('normal')

    async def test_cancel_waits_for_real_replan(self):
        await self.exercise('cancel')

    async def test_stop_waits_for_real_replan(self):
        await self.exercise('stop')

    async def test_repeated_cancel_waits_for_revision(self):
        await self.exercise('cancel_twice')

    async def test_disconnected_confirmation_preserves_recovery(self):
        await self.exercise('disconnect')
