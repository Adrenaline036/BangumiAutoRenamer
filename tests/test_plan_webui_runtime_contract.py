"""Actual dialog construction and callback client context missed by fake UI."""
import asyncio
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nicegui import ui
from nicegui.client import Client
from nicegui.context import context
from nicegui.page import page as nice_page
from src.element.red import notify
from src.pages import plan_review_page as page
from src.pages.tv_content_actions import run_revision
from src.rename.plan_store import PlanStore
from tests import test_general_season_correction_contract as season


class PlanWebuiRuntimeContracts(unittest.IsolatedAsyncioTestCase):
    flow = season.GeneralSeasonCorrectionContracts.flow
    current = season.GeneralSeasonCorrectionContracts.current

    async def test_actual_dialog_root_reader_is_callable_after_construction(self):
        async with self.current() as (flow, plan, state):
            client = Client(nice_page('/isolated-real-review-construction'), request=None)
            self.addCleanup(client.delete)
            with client:
                dialog = page.PlanReviewDialog(plan, flow.manager._read(flow.task_id))
            try:
                self.assertEqual(flow.service.root_snapshot_source.observe(),
                                 dialog.review_service.root_snapshot_source.observe())
            finally:
                dialog.delete()

    async def test_background_completion_has_real_client_for_success_failure_and_closed_view(self):
        async with self.current() as (flow, plan, state):
            for mode in ('success', 'failure', 'closed'):
                with self.subTest(mode=mode):
                    client = Client(nice_page('/isolated-real-notification-' + mode), request=None)
                    self.addCleanup(client.delete)
                    messages, adoptions, loop_errors = [], [], []
                    owner = SimpleNamespace(client=client, review_service=flow.service,
                                            plan=plan, task_decision=state)
                    def adopt(revised):
                        self.assertIs(client, context.client)
                        ui.label('已更新的真实页面')
                        adoptions.append(revised)
                    owner._adopt_revision = adopt
                    def report(message, **kwargs):
                        self.assertIs(client, context.client)
                        notify(message, **kwargs)
                        messages.append((message, kwargs.get('type')))
                    def submit():
                        if mode == 'failure':
                            raise ValueError('本轮真实后台失败')
                        if mode == 'closed':
                            owner._tv_content_closed = True
                        return plan
                    loop = asyncio.get_running_loop()
                    previous = loop.get_exception_handler()
                    loop.set_exception_handler(lambda _loop, error: loop_errors.append(error))
                    try:
                        with client:
                            await run_revision(owner, submit=submit, expected_revision=state.revision,
                                expected_fingerprint=PlanStore.fingerprint(plan), notify=report, message='本轮成功')
                        await asyncio.sleep(0)
                    finally:
                        loop.set_exception_handler(previous)
                    self.assertFalse(loop_errors, [str(e.get('exception')) for e in loop_errors])
                    self.assertIsNone(owner._tv_content_worker)
                    if mode == 'success':
                        self.assertEqual([plan], adoptions)
                        self.assertIn(('本轮成功', 'positive'), messages)
                    elif mode == 'failure':
                        self.assertFalse(adoptions)
                        self.assertIn(('本轮真实后台失败', 'negative'), messages)
                    else:
                        self.assertFalse(adoptions)
                        self.assertEqual(1, len(messages))

    async def test_interleaved_clients_keep_their_own_completion_context(self):
        async with self.current() as first, self.current() as second:
            owners, tasks, messages = [], [], []
            entered = [threading.Event(), threading.Event()]
            released = [threading.Event(), threading.Event()]
            def submit(index):
                entered[index].set()
                if not released[index].wait(5):
                    raise ValueError('bounded backend wait expired')
                return (first, second)[index][1]
            async def invoke(index, owner):
                def report(message, **kwargs):
                    self.assertIs(owner.client, context.client)
                    notify(message, **kwargs)
                    if kwargs.get('type') == 'positive':
                        messages.append(index)
                with owner.client:
                    await run_revision(owner, submit=lambda: submit(index), expected_revision=owner.task_decision.revision,
                        expected_fingerprint=PlanStore.fingerprint(owner.plan), notify=report, message='完成')
            try:
                for index in range(2):
                    flow, plan, state = (first, second)[index]
                    client = Client(nice_page('/interleaved-client-' + str(index)), request=None)
                    self.addCleanup(client.delete)
                    owner = SimpleNamespace(client=client, review_service=flow.service,
                        plan=plan, task_decision=state, _adopt_revision=lambda _plan: None)
                    owners.append(owner)
                    tasks.append(asyncio.create_task(invoke(index, owner)))
                for event in entered:
                    self.assertTrue(await asyncio.to_thread(event.wait, 3))
                released[1].set()
                await asyncio.wait_for(tasks[1], 3)
                self.assertEqual([1], messages)
                released[0].set()
                await asyncio.wait_for(tasks[0], 3)
                self.assertEqual([1, 0], messages)
                self.assertTrue(all(owner._tv_content_worker is None for owner in owners))
            finally:
                for event in released:
                    event.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_shared_movie_completion_uses_real_client_and_stays_unapproved(self):
        from src.pages.movie_content_actions import complete
        async with self.flow(movie=True) as flow:
            plan = flow.store.load(flow.task_id)
            state = flow.service.decision_state_store.load(flow.task_id)
            self.assertIsNotNone(state.movie_version_selection)
            groups = [dict(sources=list(row['sources']), label=row['label'])
                      for row in state.movie_version_selection.to_dict()['groups']]
            groups[0]['label'] = '页面明确版本'
            client = Client(nice_page('/movie-shared-real-client'), request=None)
            self.addCleanup(client.delete)
            messages, adopted = [], []
            owner = SimpleNamespace(client=client, review_service=flow.service,
                plan=plan, task_decision=state, _adopt_revision=adopted.append)
            def report(message, **kwargs):
                self.assertIs(client, context.client)
                notify(message, **kwargs)
                messages.append((kwargs.get('type'), str(message)))
            with client:
                await complete(owner, plan.presentation_groups[0].group_id,
                    selected=groups[0]['sources'], groups=groups, notify=report)
            self.assertTrue(any(kind == 'positive' for kind, _text in messages), messages)
            self.assertEqual(1, len(adopted))
            self.assertIsNone(owner._movie_version_worker)
            self.assertFalse(flow.service.load(flow.task_id).approved_entry_ids)
            self.assertTrue(all(not entry.target_path.exists() for entry in adopted[0].entries))
