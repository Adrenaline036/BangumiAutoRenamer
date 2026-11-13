"""实际页面回调在服务边界被阻塞时的取消与停止。"""
import asyncio
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from src.pages import data_table_page as dashboard
from src.pages import plan_review_page
from src.task_queue import TaskQueueManager
from src.rename import execution_recovery


class DashboardTransactions(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, route, mode):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = TaskQueueManager(task_root=root/'tasks', plan_root=root/'plans',
                decision_state_root=root/'decisions', recovery_abandon_audit_root=root/'abandon')
            manager._write('retained', dict(uuid='retained', name='保留的名称',
                workflow_status='failed' if route == 'review_remove' else 'need_confirm',
                selection_operation={'expected_decision_revision': 1}))
            before = (manager.task_root/'retained.json').read_bytes()
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            marker = root/'service-finished.txt'
            def write(*args, **kwargs):
                entered.set()
                if not release.wait(8):
                    raise TimeoutError('isolated service barrier')
                marker.write_text('finished', encoding='utf-8')
                finished.set()
            call = stopping = None
            dialog = Mock(is_deleted=False)
            await manager.start()
            try:
                with ExitStack() as stack:
                    stack.enter_context(patch.object(dashboard, 'task_queue', manager))
                    stack.enter_context(patch.object(dashboard, 'get_task', side_effect=manager._read))
                    stack.enter_context(patch.object(dashboard, 'notify'))
                    stack.enter_context(patch.object(dashboard, 'refresh_dashboard'))
                    stack.enter_context(patch.object(plan_review_page, 'task_queue', manager))
                    stack.enter_context(patch.object(plan_review_page, 'notify'))
                    for name in ('TASK_PATH','PLAN_PATH','RECORD_PATH','PLAN_REVIEW_PATH',
                                 'DECISION_STATE_PATH','WORK_CATALOG_PATH','SERIES_CATALOG_PATH',
                                 'PROVIDER_LINK_CATALOG_PATH','RECOVERY_TRANSACTION_PATH'):
                        stack.enter_context(patch.object(dashboard, name, root/name.lower()))
                    if route in {'safe_end', 'review_remove'}:
                        stack.enter_context(patch.object(manager, '_abandon_and_remove', side_effect=write))
                        if route == 'safe_end':
                            operation = dashboard._submit_safe_end('retained')
                        else:
                            owner = SimpleNamespace(task_data=manager._read('retained'), plan=None, _close_review=Mock())
                            operation = plan_review_page.PlanReviewDialog._remove_provider_failure_task(owner)
                    elif route == 'catalog':
                        stack.enter_context(patch.object(execution_recovery, 'reconcile_catalog_update', side_effect=write))
                        operation = dashboard._reconcile_catalog_update(dialog, 'retained')
                    else:
                        stack.enter_context(patch.object(execution_recovery, 'abandon_catalog_update_recovery', side_effect=write))
                        operation = dashboard._abandon_catalog_update(dialog, dialog, 'retained', 'a'*64)
                    call = asyncio.create_task(operation)
                    self.assertTrue(await asyncio.to_thread(entered.wait, 4))
                    if mode in {'cancel', 'cancel_twice'}:
                        call.cancel()
                        if mode == 'cancel_twice':
                            await asyncio.sleep(.01)
                            call.cancel()
                    elif mode == 'stop':
                        stopping = asyncio.create_task(manager.stop())
                    await asyncio.sleep(.05)
                    still_waiting = not (stopping if stopping is not None else call).done()
                    release.set()
                    if mode in {'cancel', 'cancel_twice'}:
                        with self.assertRaises(asyncio.CancelledError):
                            await call
                    else:
                        result = await call
                        self.assertEqual(True if route == 'safe_end' else None, result)
                    if stopping is not None:
                        await stopping
                    self.assertTrue(still_waiting, '后台写入尚未结束，页面调用或停止已返回')
                    self.assertTrue(finished.is_set())
                    self.assertEqual(before, (manager.task_root/'retained.json').read_bytes())
            finally:
                release.set()
                if call is not None:
                    await asyncio.gather(call, return_exceptions=True)
                if stopping is not None:
                    await asyncio.gather(stopping, return_exceptions=True)
                if entered.is_set():
                    await asyncio.to_thread(finished.wait, 8)
                await manager.stop()

    async def test_normal_completion_waits_for_service(self):
        for route in ('safe_end','catalog','review_remove','catalog_abandon'):
            with self.subTest(route=route):
                await self.exercise(route, 'normal')

    async def test_cancel_waits_and_propagates(self):
        for route in ('safe_end','catalog','review_remove','catalog_abandon'):
            with self.subTest(route=route):
                await self.exercise(route, 'cancel')

    async def test_stop_waits_for_service(self):
        for route in ('safe_end','catalog','review_remove','catalog_abandon'):
            with self.subTest(route=route):
                await self.exercise(route, 'stop')

    async def test_repeated_cancel_waits_and_propagates(self):
        for route in ('safe_end','catalog','review_remove','catalog_abandon'):
            with self.subTest(route=route):
                await self.exercise(route,'cancel_twice')
