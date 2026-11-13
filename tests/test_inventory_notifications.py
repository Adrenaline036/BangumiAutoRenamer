import asyncio
import threading
import unittest
from unittest.mock import patch
from nicegui import Client
from nicegui.page import page
from src.pages import data_table_page as dashboard
from src.utils.task_changes import DashboardRefresh, mark_changed


class DashboardInterleaving(unittest.IsolatedAsyncioTestCase):
    async def test_change_during_read_is_not_consumed_by_dropped_refresh(self):
        await self.check_interleaving(False)

    async def test_change_during_explicit_refresh_is_observed_after_busy_skip(self):
        await self.check_interleaving(True)

    async def check_interleaving(self, explicit):
        client = Client(page('/notification-probe'), request=None)
        self.addCleanup(client.delete)
        rows = [dict(uuid='sample', workflow_status='need_confirm', name='Old')]
        entered, release = threading.Event(), threading.Event()
        reads = []
        original = dashboard._prepare_dashboard
        def prepare(*args, **kwargs):
            observed = original(*args, **kwargs)
            reads.append(observed)
            if len(reads) == 1:
                entered.set()
                if not release.wait(3):
                    raise AssertionError('probe release timed out')
            return observed
        with client, patch.object(dashboard, 'load_task_rows', side_effect=lambda: [dict(row) for row in rows]), \
                patch.object(dashboard.DecisionStateStore, 'load_optional', return_value=None):
            await dashboard.create_table()
            await asyncio.sleep(0)
            self.assertIn("Old", [str(getattr(e, "text", "")) for e in client.elements.values()])
            timer = DashboardRefresh(dashboard.refresh_dashboard)
            with patch.object(dashboard, '_prepare_dashboard', side_effect=prepare):
                mark_changed()
                first = dashboard.refresh_dashboard(force=True) if explicit else timer.poll()
                try:
                    for _ in range(200):
                        if entered.is_set():
                            break
                        await asyncio.sleep(.005)
                    self.assertTrue(entered.is_set())
                    rows[0] = {**rows[0], 'name': 'Updated', 'workflow_status': 'completed'}
                    mark_changed()
                    second = timer.poll()
                    if asyncio.isfuture(second) or asyncio.iscoroutine(second):
                        await second
                finally:
                    release.set()
                if asyncio.isfuture(first) or asyncio.iscoroutine(first):
                    await first
                final = timer.poll()
                if asyncio.isfuture(final) or asyncio.iscoroutine(final):
                    await final
            text = [str(getattr(element, 'text', '')) for element in client.elements.values()]
            print('DASHBOARD_INTERLEAVING', dict(reads=len(reads), updated_visible='Updated' in text))
            self.assertIn('Updated', text, 'the only write must become visible without a second write or a periodic fallback')
            self.assertEqual(2, len(reads), 'one follow-up observation consumes the change during the first read')
