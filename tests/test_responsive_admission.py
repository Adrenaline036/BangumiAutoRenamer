"""Reported UI incidents: empty first screen, modal wait, blocked heartbeat."""
from tests.queue_fixture import TaskQueueManager as ControlledQueue
import asyncio
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch
from nicegui.client import Client
from nicegui.page import page as nice_page
from src.pages import add_task_page as add, plan_review_page as review
from src.rename import input_directory_discovery as discovery
from src.task_queue import TaskQueueManager
from tests import test_light_review_contract as light


class ResponsiveAdmission(unittest.IsolatedAsyncioTestCase):
    async def test_success_closes_while_worker_busy_and_never_opens_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'incoming'; source.mkdir()
            (source/'Film.mkv').write_bytes(b'fixture')
            entered, release=asyncio.Event(), asyncio.Event()
            async def executor(job):
                entered.set(); await release.wait()
                manager._update(job.task_id,workflow_status='need_confirm')
            manager=TaskQueueManager(task_root=root/'tasks',plan_root=root/'plans',
                decision_state_root=root/'decisions',recovery_abandon_audit_root=root/'abandon',
                executor=executor)
            client=Client(nice_page('/responsive-admission'),request=None)
            pending=None
            try:
                with client, patch.object(add,'task_queue',manager), patch.object(review,'plan_review_page',new=AsyncMock()) as navigate:
                    dialog=add.AddTaskDialog(); dialog.path=str(source)
                    async def submit():
                        with client: await dialog._next()
                    pending=asyncio.create_task(submit())
                    await asyncio.wait_for(entered.wait(),5)
                    await asyncio.sleep(.05)
                    self.assertTrue(dialog._closed,'accepted job must not retain add modal')
                    self.assertTrue(pending.done())
                    release.set(); await manager._queue.join()
                    navigate.assert_not_awaited()
            finally:
                release.set()
                if pending: await asyncio.wait_for(pending,5)
                await manager.stop(); client.delete()

    async def test_discovery_revalidation_does_not_block_event_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'incoming'; source.mkdir()
            (source/'Film.mkv').write_bytes(b'fixture')
            manager=ControlledQueue(task_root=root/'tasks',plan_root=root/'plans',
                decision_state_root=root/'decisions',recovery_abandon_audit_root=root/'abandon',test_mode=True)
            observed=discovery.discover_sources((source,))
            batch=manager.prepare_discovered_batch(observed,is_anime=None,is_movie=None,source='ui')
            main_thread=threading.get_ident(); seen=[]
            validate=discovery.validate_discovery
            def slow(value):
                seen.append(threading.get_ident()); time.sleep(.2); return validate(value)
            with patch.object(discovery,'validate_discovery',side_effect=slow):
                await manager.enqueue_batch(**batch)
            self.assertTrue(seen)
            self.assertNotIn(main_thread,seen,'source scan occupied socket heartbeat thread')

    async def test_unresolved_workspace_has_review_summary_and_actions_without_fake_plan(self):
        helper=light.SingleWorkspace('runTest')
        async with helper.flow(admission_only=True,admit=helper.admit_auto) as flow:
            with Client.auto_index_client.content:
                dialog=review.PlanReviewDialog(None,flow.manager._read(flow.task_id))
            try:
                self.assertIsNone(dialog.plan)
                self.assertTrue(hasattr(dialog,'header_title_value'),'missing normal summary')
                self.assertTrue(hasattr(dialog,'approve_all_button'),'missing normal actions')
                self.assertFalse(dialog.approve_all_button.enabled)
                self.assertFalse(flow.store.path_for(flow.task_id).exists())
            finally: dialog.delete()

    async def test_stop_during_source_check_rejects_before_writes(self):
        for restart in (False,True):
            with self.subTest(restart=restart),tempfile.TemporaryDirectory() as folder:
                root=Path(folder); source=root/'input'; source.mkdir()
                (source/'Film.mkv').write_bytes(b'fixture')
                manager=ControlledQueue(task_root=root/'tasks',plan_root=root/'plans',
                    decision_state_root=root/'decisions',recovery_abandon_audit_root=root/'abandon',test_mode=True)
                batch=manager.prepare_discovered_batch(discovery.discover_sources((source,)),is_anime=None,is_movie=None,source='ui')
                entered,release=threading.Event(),threading.Event()
                validate=discovery.validate_discovery
                def slow(value):
                    entered.set(); release.wait(5); validate(value)
                with patch.object(discovery,'validate_discovery',side_effect=slow):
                    pending=asyncio.create_task(manager.enqueue_batch(**batch))
                    self.assertTrue(await asyncio.to_thread(entered.wait,5))
                    await manager.stop()
                    if restart: await manager.start()
                    release.set()
                    with self.assertRaisesRegex(RuntimeError,'队列已停止'): await pending
                self.assertEqual([],list(manager.task_root.glob('*.json')))
                self.assertFalse(manager.batch_scope_store.path_for(batch['batch_id']).exists())
                await manager.stop()

    async def test_admission_failure_keeps_input_and_retry_available(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder); (source/'Film.mkv').write_bytes(b'fixture')
            client=Client(nice_page('/failed-admission'),request=None)
            try:
                with client, patch.object(add.task_queue,'enqueue_batch',new=AsyncMock(side_effect=ValueError('来源已变化'))),patch.object(add,'notify'):
                    dialog=add.AddTaskDialog(); dialog.path=str(source)
                    await dialog._next()
                    self.assertFalse(dialog._closed)
                    self.assertFalse(dialog._submitting)
                    self.assertEqual(str(source),dialog.path)
            finally: client.delete()

    async def test_close_or_edit_during_final_read_only_scan_does_not_admit(self):
        from types import SimpleNamespace
        for action in ('close','edit'):
            with self.subTest(action=action),tempfile.TemporaryDirectory() as folder:
                root=Path(folder); source=root/'input'; source.mkdir()
                (source/'Film.mkv').write_bytes(b'fixture')
                manager=ControlledQueue(task_root=root/'tasks',plan_root=root/'plans',
                    decision_state_root=root/'decisions',recovery_abandon_audit_root=root/'abandon',test_mode=True)
                observed=discovery.discover_sources((source,))
                batch=manager.prepare_discovered_batch(observed,is_anime=None,is_movie=None,source='ui')
                entered,release=threading.Event(),threading.Event()
                validate=discovery.validate_discovery
                def slow(value):
                    entered.set(); release.wait(5); validate(value)
                client=Client(nice_page('/cancel-final-scan'),request=None)
                try:
                    with client,patch.object(add,'task_queue',manager),patch.object(manager,'prepare_discovered_batch',return_value=batch),patch.object(discovery,'validate_discovery',side_effect=slow),patch.object(add,'notify'):
                        dialog=add.AddTaskDialog(); dialog.path=str(source); dialog.discovery=observed
                        async def submit():
                            with client: await dialog._submit()
                        pending=asyncio.create_task(submit())
                        self.assertTrue(await asyncio.to_thread(entered.wait,5))
                        if action=='close': dialog.close()
                        else: dialog._path_changed(SimpleNamespace(value=str(root/'changed')))
                        release.set(); await asyncio.wait_for(pending,5)
                    self.assertEqual([],list(manager.task_root.glob('*.json')))
                    self.assertFalse(manager.batch_scope_store.path_for(batch['batch_id']).exists())
                finally:
                    release.set(); await manager.stop(); client.delete()
