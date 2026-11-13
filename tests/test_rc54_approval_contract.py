"""rc54 incidents at the real dialog callback and public approval boundaries."""
import asyncio
import builtins
from collections import Counter
from contextlib import contextmanager
import inspect
import io
import json
import os
import time
import threading
import subprocess
import unittest
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from src.rename.plan_review import PlanReviewError, entry_id
from src.rename.plan_execution import PlanExecutionError, PlanExecutionService
from tests import test_initial_tv_completion_contract as initial
from tests import test_content_workbench_gui as gui
from tests._media_flow_helpers import revise


@contextmanager
def source_reads(plan):
    """Count both Python file APIs and external processes at the tested boundary."""
    sources = {os.path.normcase(os.path.abspath(p)) for p in plan.expected_sources}
    count = Counter()
    class Reader:
        def __init__(self, stream, kind):
            self.stream, self.kind = stream, kind
        def __getattr__(self, key):
            return getattr(self.stream, key)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, *args):
            data = self.stream.read(*args)
            count[self.kind + '_bytes'] += len(data)
            return data
        def readinto(self, *args):
            size = self.stream.readinto(*args)
            count[self.kind + '_bytes'] += size or 0
            return size
    def wrap(original):
        def opened(path, mode='r', *args, **kwargs):
            stream = original(path, mode, *args, **kwargs)
            if isinstance(path, (str, os.PathLike)) and os.path.normcase(os.path.abspath(path)) in sources:
                kind = 'mkv' if str(path).lower().endswith('.mkv') else 'companion'
                count[kind + '_opens'] += 1
                return Reader(stream, kind)
            return stream
        return opened
    with patch('builtins.open', wrap(builtins.open)), patch('io.open', wrap(io.open)), \
            patch('subprocess.Popen', wraps=subprocess.Popen) as processes:
        yield count, processes


class Rc54ApprovalContracts(unittest.IsolatedAsyncioTestCase):
    def execution(self, service):
        return PlanExecutionService(service.plan_store, service, task_root=service.task_root,
            record_root=service.record_root, catalog_root=service.work_catalog.root,
            series_catalog_root=service.series_catalog.root, decision_state_root=service.decision_state_store.root,
            provider_link_catalog_root=service.provider_link_catalog.root,
            recovery_transaction_root=service.plan_store.root.parent / 'recovery_transactions')

    async def until(self, event):
        for _ in range(200):
            if event.is_set():
                return
            await asyncio.sleep(.01)
        self.fail('background worker did not enter bounded barrier')
    async def ready(self):
        root, service, _, result, extras, *_ = await initial.InitialTVCompletionContracts.fixture(
            self, anime=False)
        return root, service, result.plan, extras

    async def click(self, view):
        with view.client:
            pending = view._approve_all()
            if inspect.isawaitable(pending):
                await pending

    async def test_slow_approval_keeps_loop_responsive_and_gives_feedback(self):
        root, service, plan, _ = await self.ready()
        page = gui.ContentWorkbenchGui('runTest')
        page.create_page(prepared=(root, service, plan, []), cleanup_owner=self)
        original = service.approve_entries
        ticks, notices_at_start = [], []
        def slow(*args, **kwargs):
            notices_at_start.extend(page.notices)
            time.sleep(.25)
            return original(*args, **kwargs)
        async def pulse():
            while True:
                ticks.append(time.perf_counter())
                await asyncio.sleep(.01)
        pulse_task = asyncio.create_task(pulse())
        await asyncio.sleep(.02)
        try:
            with patch.object(service, 'approve_entries', side_effect=slow) as approval:
                await self.click(page.view)
            await asyncio.sleep(.02)
        finally:
            pulse_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pulse_task
        self.assertEqual(1, approval.call_count)
        self.assertLess(max(b-a for a, b in zip(ticks, ticks[1:])), .15,
                        'approval blocks the GUI event loop')
        self.assertTrue(any('后台' in n or '正在批准' in n for n in notices_at_start),
                        'feedback must precede background work, including executor queueing')
        self.assertEqual('approved', page.view.task_data['workflow_status'])
        self.assertFalse(list(service.record_root.glob('*.ledger.json')), 'approval must not execute')

    async def test_duplicate_approval_clicks_submit_once(self):
        root, service, plan, _ = await self.ready()
        page = gui.ContentWorkbenchGui('runTest')
        page.create_page(prepared=(root, service, plan, []), cleanup_owner=self)
        original = service.approve_entries
        def slow(*args, **kwargs):
            time.sleep(.2)
            return original(*args, **kwargs)
        with patch.object(service, 'approve_entries', side_effect=slow) as approval:
            first = asyncio.create_task(self.click(page.view))
            await asyncio.sleep(.02)
            await self.click(page.view)
            await first
        self.assertEqual(1, approval.call_count)




    async def test_stale_task_workflow_rejects_without_approval(self):
        root, service, plan, _ = await self.ready()
        page = gui.ContentWorkbenchGui('runTest')
        page.create_page(prepared=(root, service, plan, []), cleanup_owner=self)
        path = service.task_root / f'{plan.task_id}.json'
        task = json.loads(path.read_text('utf-8'))
        task['workflow_status'] = 'executing'
        path.write_text(json.dumps(task), encoding='utf-8')
        before = path.read_bytes()
        with patch.object(service, 'approve_entries', wraps=service.approve_entries):
            await self.click(page.view)
        self.assertEqual(before, path.read_bytes())
        self.assertFalse(service.load_or_create(plan).approved_entry_ids)

    async def test_dialog_rejection_never_reports_or_saves_approval(self):
        root, service, plan, extras = await self.ready()
        page = gui.ContentWorkbenchGui('runTest')
        page.create_page(prepared=(root, service, plan, []), cleanup_owner=self)
        service.approve_entries = unittest.mock.Mock(side_effect=PlanReviewError('当前计划核查未通过'))
        task_path = service.task_root / f'{plan.task_id}.json'
        task_before = task_path.read_bytes()
        with patch.object(service, 'approve_entries', wraps=service.approve_entries) as approval:
            await self.click(page.view)
        self.assertEqual(1, approval.call_count)
        self.assertEqual(task_before, task_path.read_bytes())
        self.assertNotEqual('approved', page.view.task_data['workflow_status'])
        self.assertFalse(service.load_or_create(plan).approved_entry_ids)
        self.assertFalse(any('已全部批准' in n or '批准决定已保存' in n or ('已记录' in n and '条批准' in n)
                             for n in page.notices), page.notices)

    async def test_three_boundaries_read_no_mkv_content_or_external_process(self):
        _, service, plan, _ = await self.ready()
        fingerprint = PlanStore.fingerprint(plan)
        execution = self.execution(service)
        boundaries = (
            ('approval', lambda: service.approve_entries(plan.task_id, fingerprint, [entry_id(e) for e in plan.entries])),
            ('admission', lambda: execution.validate_approved(plan.task_id, fingerprint)),
        )
        for name, call in boundaries:
            with self.subTest(boundary=name), source_reads(plan) as (counts, processes):
                call()
                self.assertEqual(0, counts['mkv_opens'])
                self.assertEqual(0, counts['mkv_bytes'])
                self.assertEqual(0, processes.call_count)
                self.assertEqual(0, counts['companion_bytes'])
                print(json.dumps(dict(boundary=name, external_processes=processes.call_count, **counts)))



    async def test_close_disconnect_and_cancel_do_not_adopt_background_approval(self):
        for mode in ('close', 'disconnect', 'cancel'):
            with self.subTest(mode=mode):
                root, service, plan, _ = await self.ready()
                page = gui.ContentWorkbenchGui('runTest')
                page.create_page(prepared=(root, service, plan, []), cleanup_owner=self)
                original, entered, release = service.approve_entries, threading.Event(), threading.Event()
                def blocked(*args, **kwargs):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('bounded test barrier')
                    return original(*args, **kwargs)
                before = page.view.task_data.copy()
                with patch.object(service, 'approve_entries', side_effect=blocked), \
                        patch.object(page.view, '_render_entries', wraps=page.view._render_entries) as render:
                    waiter = asyncio.create_task(self.click(page.view))
                    try:
                        await self.until(entered)
                        if mode == 'close':
                            page.view._close_review()
                        elif mode == 'disconnect':
                            page.view._tv_content_worker_disconnected = True
                        else:
                            page.view._close_review()
                            waiter.cancel()
                        worker = page.view._tv_content_worker
                    finally:
                        release.set()
                        await waiter
                        await asyncio.wait_for(asyncio.shield(worker), 5)
                        await asyncio.sleep(0)
                    self.assertEqual(before, page.view.task_data)
                    self.assertEqual(0, render.call_count)
                saved = json.loads((service.task_root / f'{plan.task_id}.json').read_text('utf-8'))
                self.assertEqual('approved', saved['workflow_status'])
                self.assertTrue(service.load(plan.task_id).approved_entry_ids)
                self.assertFalse(list(service.record_root.glob('*.ledger.json')))

    async def test_queued_task_or_decision_drift_cannot_approve(self):
        from dataclasses import replace
        for mode in ('task', 'decision'):
            with self.subTest(mode=mode):
                root, service, plan, _ = await self.ready()
                page = gui.ContentWorkbenchGui('runTest')
                page.create_page(prepared=(root, service, plan, []), cleanup_owner=self)
                original, entered, release = service.approve_entries, threading.Event(), threading.Event()
                def blocked(*args, **kwargs):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('bounded test barrier')
                    return original(*args, **kwargs)
                with patch.object(service, 'approve_entries', side_effect=blocked):
                    waiter = asyncio.create_task(self.click(page.view))
                    try:
                        await self.until(entered)
                        if mode == 'task':
                            from src.utils.utils import merge_task_at
                            merge_task_at(service.task_root, plan.task_id, {'workflow_status': 'executing'})
                        else:
                            state = service.decision_state_store.load(plan.task_id)
                            service.decision_state_store.save(replace(state, revision=state.revision + 1),
                                                              expected_revision=state.revision)
                        task_path = service.task_root / f'{plan.task_id}.json'
                        before = task_path.read_bytes()
                    finally:
                        release.set()
                        await waiter
                self.assertEqual(before, task_path.read_bytes())
                self.assertFalse(service.load_or_create(plan).approved_entry_ids)

    async def test_task_cas_at_save_never_overwrites_later_workflow(self):
        from src.utils.utils import merge_task_at
        root, service, plan, _ = await self.ready()
        page = gui.ContentWorkbenchGui('runTest')
        page.create_page(prepared=(root, service, plan, []), cleanup_owner=self)
        original = service._save
        def drift(decision):
            if decision.status.value == 'approved':
                merge_task_at(service.task_root, plan.task_id, {'workflow_status': 'executing'})
            return original(decision)
        with patch.object(service, '_save', side_effect=drift):
            await self.click(page.view)
        task = json.loads((service.task_root / f'{plan.task_id}.json').read_text('utf-8'))
        self.assertEqual('executing', task['workflow_status'])
        self.assertNotEqual('approved', page.view.task_data['workflow_status'])
        self.assertTrue(any('批准记录已保存' in n for n in page.notices))

    async def test_partial_approval_does_not_promote_task(self):
        root, service, plan, _ = await self.ready()
        page = gui.ContentWorkbenchGui('runTest')
        page.create_page(prepared=(root, service, plan, []), cleanup_owner=self)
        self.assertGreater(len(plan.entries), 1)
        before = page.view.task_data['workflow_status']
        with page.view.client:
            await page.view._approve((entry_id(plan.entries[0]),))
        self.assertEqual('draft', service.load(plan.task_id).status.value)
        self.assertEqual(before, page.view.task_data['workflow_status'])
        await self.click(page.view)
        self.assertEqual('approved', page.view.task_data['workflow_status'])
