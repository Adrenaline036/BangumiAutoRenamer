"""审核页移除与放弃登记恢复的事件循环响应。"""
import asyncio
from contextlib import ExitStack
import inspect
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from src.pages import data_table_page, plan_review_page
from src.task_queue import TaskQueueManager
from src.rename import execution_recovery


class SynchronousExitCallbacks(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, route, require_responsive):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = TaskQueueManager(task_root=root/'tasks', plan_root=root/'plans',
                decision_state_root=root/'decisions', recovery_abandon_audit_root=root/'abandon')
            task = dict(uuid='retained', workflow_status='failed',
                        selection_operation={'expected_decision_revision':1})
            owner = SimpleNamespace(task_data=task, plan=None, _close_review=Mock())
            seen, ticks = [], []
            main = threading.get_ident()
            marker = root/'finished.txt'
            def backend(*args, **kwargs):
                seen.append(threading.get_ident())
                time.sleep(.08)
                marker.write_text('finished', encoding='utf-8')
            running = True
            async def tick():
                while running:
                    ticks.append(time.monotonic())
                    await asyncio.sleep(.005)
            ticking = asyncio.create_task(tick())
            await asyncio.sleep(.01)
            baseline = len(ticks)
            try:
                with ExitStack() as stack:
                    for module in (data_table_page, plan_review_page):
                        stack.enter_context(patch.object(module,'task_queue',queue))
                        stack.enter_context(patch.object(module,'notify'))
                        for name in ('TASK_PATH','PLAN_PATH','RECORD_PATH','PLAN_REVIEW_PATH',
                                     'DECISION_STATE_PATH','RECOVERY_ABANDON_AUDIT_PATH'):
                            if hasattr(module,name):
                                stack.enter_context(patch.object(module,name,root/name.lower()))
                    stack.enter_context(patch.object(data_table_page,'refresh_dashboard'))
                    if route == 'review_remove':
                        service = stack.enter_context(patch.object(queue,'_abandon_and_remove',side_effect=backend))
                        value = plan_review_page.PlanReviewDialog._remove_provider_failure_task(owner)
                    else:
                        service = stack.enter_context(patch.object(execution_recovery,'abandon_catalog_update_recovery',side_effect=backend))
                        value = data_table_page._abandon_catalog_update(Mock(),Mock(),'retained','a'*64)
                    if inspect.isawaitable(value):
                        await value
                    service.assert_called_once()
                    self.assertEqual('finished',marker.read_text(encoding='utf-8'))
                    if require_responsive:
                        self.assertNotIn(main,seen,'持久写入在页面事件线程上执行')
                        self.assertGreaterEqual(len(ticks)-baseline,3)
            finally:
                running=False
                await ticking
                await queue.stop()

    async def test_normal_callbacks_reach_service(self):
        for route in ('review_remove','catalog_abandon'):
            with self.subTest(route=route):
                await self.exercise(route,False)

    async def test_callbacks_leave_event_loop_responsive(self):
        for route in ('review_remove','catalog_abandon'):
            with self.subTest(route=route):
                await self.exercise(route,True)
