"""真实补齐按钮生成计划时的生命周期与历史保留。"""
import asyncio
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from src.pages import plan_review_page
from src.rename.plan_store import PlanStore
from tests import test_missing_target_repair as test_missing_target_repair
from tests import test_content_workbench_gui as test_content_workbench_gui
from tests.test_completed_rebuild_work_contract import file_tree


class AssetConfirmationLifecycle(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, mode, *, repair=True):
        fixture = test_missing_target_repair.MissingTargetRepair('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        old, current, peer, other = await fixture.old_and_incoming(partial=True)
        holder = test_content_workbench_gui.ContentWorkbenchGui('runTest')
        holder.create_page(prepared=(fixture.root, fixture.review, current.plan, []), cleanup_owner=self)
        manager = plan_review_page.task_queue
        await manager.start()
        await holder.driver.click('核查并补齐' if repair else '换源')
        original_fp = PlanStore.fingerprint(current.plan)
        original_task = manager._read(current.plan.task_id)
        source_before = file_tree(current.plan.source_root)
        service_type = type(holder.view.review_service)
        method = 'confirm_asset_repair' if repair else 'confirm_asset_replacement'
        original = getattr(service_type, method)
        if mode in {'disconnect', 'stale'}:
            try:
                if mode == 'disconnect':
                    with patch.object(manager, 'confirm_asset_plan', side_effect=RuntimeError('确认入口已断开')) as entry, \
                            patch.object(service_type, method, autospec=True) as backend:
                        await holder.driver.click('生成计划')
                        entry.assert_called_once()
                        backend.assert_not_called()
                else:
                    actions = holder.view.review_service.asset_actions()
                    operation, _ = (actions.prepare_missing if repair else actions.prepare_replacement)(current.plan)
                    with self.assertRaises((RuntimeError, ValueError)):
                        await manager.confirm_asset_plan(current.plan.task_id, '0'*64, operation)
                self.assertEqual(original_fp, PlanStore.fingerprint(fixture.review.plan_store.load(current.plan.task_id)))
                self.assertEqual(original_task, manager._read(current.plan.task_id))
                self.assertEqual(source_before, file_tree(current.plan.source_root))
            finally:
                await manager.stop()
            return
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def confirm(service, *args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('isolated confirmation barrier')
            try:
                return original(service, *args, **kwargs)
            finally:
                finished.set()
        clicking = stopping = None
        try:
            with patch.object(service_type, method, autospec=True, side_effect=confirm):
                clicking = asyncio.create_task(holder.driver.click('生成计划'))
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
                self.assertTrue(pending, '后台计划生成尚未结束，页面或停止已返回')
                self.assertTrue(finished.is_set())
            plan = fixture.review.plan_store.load(current.plan.task_id)
            self.assertEqual('repair' if repair else 'replace', plan.metadata['asset_operation']['mode'])
            self.assertEqual(original_fp, PlanStore.fingerprint(
                fixture.review.plan_store.load_version(plan.task_id, original_fp)))
            self.assertFalse(fixture.review.load_or_create(plan).approved_entry_ids)
            task = manager._read(plan.task_id)
            for key in ('uuid','name','season_id','season_number'):
                self.assertEqual(original_task.get(key), task.get(key), key)
            self.assertEqual(source_before, file_tree(current.plan.source_root))
            self.assertEqual(other, {path:fixture.fact(Path(path)) for path in other})
            self.assertTrue(all(not entry.target_path.exists() for entry in current.plan.entries))
        finally:
            release.set()
            if clicking is not None:
                await asyncio.gather(clicking, return_exceptions=True)
            if stopping is not None:
                await asyncio.gather(stopping, return_exceptions=True)
            if entered.is_set():
                await asyncio.to_thread(finished.wait, 10)
            await manager.stop()

    async def test_normal_confirmation_preserves_history_and_other_season(self):
        await self.exercise('normal')

    async def test_cancel_waits_and_propagates(self):
        await self.exercise('cancel')

    async def test_stop_waits_for_real_confirmation(self):
        await self.exercise('stop')

    async def test_replacement_confirmation_preserves_history(self):
        await self.exercise('normal', repair=False)

    async def test_replacement_cancel_waits_and_propagates(self):
        await self.exercise('cancel', repair=False)

    async def test_replacement_stop_waits_for_confirmation(self):
        await self.exercise('stop', repair=False)

    async def test_repeated_cancel_waits_for_confirmation(self):
        await self.exercise('cancel_twice')

    async def test_replacement_repeated_cancel_waits_for_confirmation(self):
        await self.exercise('cancel_twice',repair=False)

    async def test_disconnected_entry_cannot_publish_repair_or_replacement(self):
        for repair in (True, False):
            with self.subTest(repair=repair):
                await self.exercise('disconnect', repair=repair)

    async def test_stale_plan_cannot_publish_repair_or_replacement(self):
        for repair in (True, False):
            with self.subTest(repair=repair):
                await self.exercise('stale', repair=repair)
