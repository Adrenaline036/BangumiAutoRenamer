"""Real attachments and actual GUI transport for the Ranma wait incident."""
import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nicegui import ui
from nicegui.client import Client
from nicegui.page import page as nice_page
from src.pages.tv_content_actions import run_revision
from src.rename.plan_store import PlanStore
from tests import test_general_season_correction_contract as season




class BusyRevisionContracts(unittest.IsolatedAsyncioTestCase):
    flow = season.GeneralSeasonCorrectionContracts.flow
    current = season.GeneralSeasonCorrectionContracts.current

    async def test_reopened_same_task_does_not_launch_second_operation(self):
        async with self.current() as (flow, plan, state):
            release = threading.Event()
            entered = threading.Event()
            calls, messages, tasks = [], [], []
            def submit():
                calls.append('submit')
                entered.set()
                if not release.wait(5):
                    raise ValueError('bounded fixture wait')
                return plan
            owners = []
            for i in range(2):
                client = Client(nice_page(f'/ranma-reopen-{i}'), request=None)
                self.addCleanup(client.delete)
                owners.append(SimpleNamespace(client=client, plan=plan, task_decision=state,
                    review_service=flow.service, _adopt_revision=lambda _plan: None))
            async def invoke(owner):
                with owner.client:
                    await run_revision(owner, submit=submit, expected_revision=state.revision,
                        expected_fingerprint=PlanStore.fingerprint(plan),
                        notify=lambda text, **_kw: messages.append(text), message='已完成')
            try:
                tasks.append(asyncio.create_task(invoke(owners[0])))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                tasks.append(asyncio.create_task(invoke(owners[1])))
                await asyncio.sleep(.1)
                self.assertEqual(1, len(calls), 'Reopening the same task must reuse the in-flight guard')
                self.assertTrue(any('处理中' in s for s in messages), messages)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_same_owner_repeat_has_visible_feedback(self):
        async with self.current() as (flow, plan, state):
            entered, release = threading.Event(), threading.Event()
            messages = []
            client = Client(nice_page('/ranma-repeat'), request=None)
            self.addCleanup(client.delete)
            owner = SimpleNamespace(client=client, plan=plan, task_decision=state,
                review_service=flow.service, _adopt_revision=lambda _p: None)
            def submit():
                entered.set()
                release.wait(5)
                return plan
            async def invoke():
                with client:
                    await run_revision(owner, submit=submit, expected_revision=state.revision,
                        expected_fingerprint=PlanStore.fingerprint(plan),
                        notify=lambda text, **_kw: messages.append(text), message='完成')
            task = asyncio.create_task(invoke())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                before = len(messages)
                await invoke()
                self.assertGreater(len(messages), before, 'Repeat click must not disappear silently')
            finally:
                release.set()
                await task

    async def test_running_operation_has_persistent_stage_and_reconnect_feedback(self):
        async with self.current() as (flow, plan, state):
            entered, release = threading.Event(), threading.Event()
            messages = []
            client = Client(nice_page('/ranma-progress-reconnect'), request=None)
            self.addCleanup(client.delete)
            owner = SimpleNamespace(client=client, plan=plan, task_decision=state,
                review_service=flow.service, _adopt_revision=lambda _p: None)
            def submit():
                entered.set()
                release.wait(5)
                return plan
            async def invoke():
                with client:
                    await run_revision(owner, submit=submit, expected_revision=state.revision,
                        expected_fingerprint=PlanStore.fingerprint(plan),
                        notify=lambda text, **_kw: messages.append(text), message='完成')
            task = asyncio.create_task(invoke())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                progress = getattr(owner, '_tv_content_progress', None)
                self.assertIsNotNone(progress, 'Transient toast is not persistent operation feedback')
                self.assertTrue(progress.running)
                self.assertTrue(progress.status)
                with client:
                    for handler in client.disconnect_handlers:
                        handler()
                    before = len(messages)
                    for handler in client.connect_handlers:
                        handler()
                self.assertGreater(len(messages), before, 'Reconnect must restore visible running feedback')
                self.assertFalse(getattr(owner, '_tv_content_closed', False))
            finally:
                release.set()
                await task

    async def test_actual_operation_controls_bind_busy_and_leave_close_enabled(self):
        from src.pages.review_operation_view import render_review_operation, ReviewOperationCallbacks
        from src.pages.tv_content_actions import RevisionProgress
        from tests.test_default_cohort_completion_contract import DefaultCohortCompletionContracts
        from tests.test_err056_cohort import IncompleteMetadataFlow
        from tests.test_r24_err048_series_registration_contract import MEMBERS
        from nicegui.binding import _refresh_step
        harness = DefaultCohortCompletionContracts(methodName='runTest')
        with tempfile.TemporaryDirectory() as directory:
            async with IncompleteMetadataFlow(Path(directory)) as flow:
                result, _before = await harness.pending(flow, MEMBERS[0])
                client = Client(nice_page('/ranma-real-controls'), request=None)
                self.addCleanup(client.delete)
                progress = RevisionProgress()
                noop = lambda *_a, **_kw: None
                with client:
                    render_review_operation(ui, result.task, ReviewOperationCallbacks(
                        select_provider_candidate=noop, dispatch_review_command=noop,
                        show_provider_candidates=noop, remove_provider_failure_task=noop,
                        close_review=noop), progress=progress)
                buttons = [e for e in client.elements.values() if isinstance(e, ui.button)]
                progress.running, progress.status = True, '正在准备 · 核对内嵌字体 1/174 个文件'
                _refresh_step()
                self.assertTrue(any(isinstance(e, ui.label) and e.text == progress.status
                                    for e in client.elements.values()))
                self.assertTrue(all(not e.enabled for e in buttons if e.text != '关闭'))
                self.assertTrue(next(e for e in buttons if e.text == '关闭').enabled)
                progress.running = False
                _refresh_step()
                self.assertTrue(all(e.enabled for e in buttons))
