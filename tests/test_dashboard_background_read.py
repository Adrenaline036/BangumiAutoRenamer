"""Real refreshable sections; transport regression for repeated synchronous reads."""
import asyncio
import inspect
import threading
import time
import unittest
from unittest.mock import patch
from nicegui import Client
from nicegui.page import page
import src.main_page as main
from src.pages import data_table_page as dashboard
from src.utils.task_changes import DashboardRefresh, mark_changed


class DashboardReadContracts(unittest.IsolatedAsyncioTestCase):
    async def test_pending_read_is_single_and_stale_or_closed_result_is_dropped(self):
        for action in ('changed','closed'):
            with self.subTest(action=action):
                client=Client(page('/dashboard-late-'+action),request=None)
                self.addCleanup(lambda c=client: c.delete() if c.id in c.instances else None)
                entered=threading.Event()
                release=threading.Event()
                def prepare(*args,**kwargs):
                    entered.set()
                    if not release.wait(2):raise AssertionError('read not released')
                    return [],('old-result',),{}
                with client,patch.object(dashboard,'_prepare_dashboard',side_effect=prepare) as reader, \
                        patch.object(dashboard,'_render_dashboard') as render:
                    async def refresh():
                        with client:
                            return await main.refresh_dashboard()
                    task=asyncio.create_task(refresh())
                    try:
                        for _ in range(200):
                            if entered.is_set():break
                            await asyncio.sleep(0.005)
                        self.assertTrue(entered.is_set())
                        self.assertFalse(await main.refresh_dashboard())
                        reader.assert_called_once()
                        if action=='changed':dashboard._dashboard_signatures[client.id]=('newer',)
                        else:client.delete()
                    finally:
                        release.set()
                    self.assertFalse(await task)
                    render.assert_not_called()
                    self.assertNotIn(client.id,dashboard._dashboard_reads)

    async def test_timer_refresh_reads_once_off_loop_and_projects_same_rows(self):
        client=Client(page('/dashboard-read-red'),request=None)
        self.addCleanup(client.delete)
        rows=[dict(uuid='sample',workflow_status='need_confirm',name='Example')]
        main_thread=threading.get_ident()
        reads=[]
        decision_reads=[]
        heartbeats=[]
        reading=threading.Event()
        stop=False
        def load():
            reads.append(threading.get_ident());reading.set()
            # Controlled bound makes the real timer contract deterministic;
            # actual slow callback evidence is separate, not this sleep.
            time.sleep(0.05)
            reading.clear()
            return rows
        def decision(*args,**kwargs):
            decision_reads.append(threading.get_ident())
            return None
        async def heartbeat():
            while not stop:
                if reading.is_set():heartbeats.append(True)
                await asyncio.sleep(0.005)
        with client, patch.object(dashboard,'load_task_rows',return_value=rows), \
                patch.object(dashboard.DecisionStateStore,'load_optional',return_value=None):
            await dashboard.create_table()
            dashboard._dashboard_signatures[client.id]=()
            beat=asyncio.create_task(heartbeat())
            try:
                with patch.object(dashboard,'load_task_rows',side_effect=load), \
                        patch.object(dashboard.DecisionStateStore,'load_optional',side_effect=decision):
                    timer=DashboardRefresh(main.refresh_dashboard)
                    mark_changed()
                    result=timer.poll()
                    if inspect.isawaitable(result):result=await result
                await asyncio.sleep(0)
            finally:
                stop=True
                await beat
        print('DASHBOARD_READ_TRACE',dict(read_count=len(reads),main_reads=reads.count(main_thread),heartbeats=len(heartbeats)))
        self.assertTrue(result)
        self.assertEqual(1,len(reads),'one task observation per refresh')
        self.assertNotIn(main_thread,reads,'disk reads must not block the timer event loop')
        self.assertTrue(heartbeats,'socket/event loop can progress during disk reads')
        self.assertEqual(1,len(decision_reads))
        self.assertNotIn(main_thread,decision_reads,'card metadata reads also stay off loop')
        self.assertIn('Example',[str(getattr(e,'text','')) for e in client.elements.values()])

    async def test_unchanged_snapshot_does_not_rebuild(self):
        client=Client(page('/dashboard-control-red'),request=None)
        self.addCleanup(client.delete)
        rows=[dict(uuid='sample',workflow_status='need_confirm',name='Example')]
        with client,patch.object(dashboard,'load_task_rows',return_value=rows), \
                patch.object(dashboard,'_task_card') as render:
            await dashboard.create_table()
            render.reset_mock()
            result=main.refresh_dashboard()
            if inspect.isawaitable(result):result=await result
            self.assertFalse(result)
            render.assert_not_called()
