"""Production callback contracts with UI doubles, not browser acceptance."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.pages import plan_review_page as page
from src.rename.plan_execution import PlanExecutionError
from src.pages.queue_confirmation import QueueConfirmation


class QueueGuiContracts(unittest.IsolatedAsyncioTestCase):
    def context(self):
        review = SimpleNamespace(plan=SimpleNamespace(task_id='task'),
            _refresh_execute_button=Mock(), _close_review=Mock())
        confirm = SimpleNamespace(close=Mock())
        return review, confirm

    async def test_preflight_keeps_dialog_visible_and_rejects_second_callback(self):
        review, confirm = self.context()
        entered, release = asyncio.Event(), asyncio.Event()
        async def enqueue(*_):
            entered.set()
            await release.wait()
        queue = AsyncMock(side_effect=enqueue)
        jobs = []
        with patch.object(page.task_queue, 'enqueue_approved', queue), patch.object(page, 'notify'), patch.object(page, 'get_task', return_value={}):
            try:
                jobs.append(asyncio.create_task(page.PlanReviewDialog._queue_execution(review, confirm, 'fp')))
                await asyncio.wait_for(entered.wait(), 2)
                confirm.close.assert_not_called()
                jobs.append(asyncio.create_task(page.PlanReviewDialog._queue_execution(review, confirm, 'fp')))
                await asyncio.sleep(0)
                self.assertEqual(1, queue.call_count)
                release.set()
                await asyncio.gather(*jobs)
                confirm.close.assert_called_once()
            finally:
                release.set()
                await asyncio.gather(*jobs, return_exceptions=True)

    async def test_failure_keeps_dialog_and_review_open(self):
        review, confirm = self.context()
        with patch.object(page.task_queue, 'enqueue_approved', AsyncMock(side_effect=PlanExecutionError('状态已变化'))), patch('src.pages.queue_confirmation.notify'):
            await page.PlanReviewDialog._queue_execution(review, confirm, 'fp')
        confirm.close.assert_not_called()
        review._close_review.assert_not_called()

    async def test_shared_progress_cancel_and_failure_are_visible(self):
        dialog, message, progress, button = Mock(), Mock(), Mock(), Mock()
        state = QueueConfirmation(dialog, message, progress)
        state.button = button
        entered = asyncio.Event()
        async def pause():
            entered.set()
            await asyncio.Event().wait()
        done = Mock()
        job = asyncio.create_task(state.run(pause, done))
        await asyncio.wait_for(entered.wait(), 2)
        progress.set_visibility.assert_called_with(True)
        self.assertIn('尚未入队', message.set_text.call_args.args[0])
        state.cancel()
        await job
        done.assert_not_called()
        self.assertIsNone(state.pending)
        progress.set_visibility.assert_called_with(False)
        state = QueueConfirmation(dialog, message, progress)
        with patch('src.pages.queue_confirmation.notify'):
            await state.run(AsyncMock(side_effect=ValueError('来源已变化')), done)
        self.assertEqual('来源已变化', message.set_text.call_args.args[0])
        self.assertEqual(1, dialog.close.call_count)
        done.assert_not_called()

    async def test_closed_confirmation_never_accepts_late_commands(self):
        for finish in ('cancel_before_start', 'cancel_after_wait', 'success'):
            with self.subTest(finish=finish):
                state = QueueConfirmation(Mock())
                command = AsyncMock()
                if finish == 'cancel_after_wait':
                    entered = asyncio.Event()
                    async def wait():
                        entered.set()
                        await asyncio.Event().wait()
                    job = asyncio.create_task(state.run(wait, Mock()))
                    await entered.wait()
                    state.cancel()
                    await job
                elif finish == 'success':
                    await state.run(AsyncMock(), Mock())
                else:
                    state.cancel()
                await state.run(command, Mock())
                command.assert_not_called()

    async def test_recovery_and_rollback_use_shared_progress_and_cancel(self):
        from src.pages import asset_recovery_view as recovery
        from tests.test_simplify_rollback_gui import Node
        for mode in ('resume', 'rollback'):
            with self.subTest(mode=mode):
                buttons = {}
                dialog, message, progress = Node(), Mock(), Mock()
                def button(label, **kwargs):
                    buttons[label] = kwargs['on_click']
                    return Mock()
                widgets = SimpleNamespace(dialog=lambda: dialog, card=Node, row=Node,
                    label=lambda _: message, linear_progress=lambda **_: progress,
                    button=button)
                message.classes.return_value = message
                progress.props.return_value = progress
                progress.classes.return_value = progress
                progress.style.return_value = progress
                plan = SimpleNamespace(metadata={'asset_operation': {'mode': 'replace', 'slot_key': 'slot'}})
                prepared = SimpleNamespace(state='resume', detail='可恢复', fingerprint='recovery-fp')
                entered = asyncio.Event()
                async def pause(*_, **__):
                    entered.set()
                    await asyncio.Event().wait()
                queue = SimpleNamespace(plan_store=SimpleNamespace(load=lambda _: plan),
                    execution_service=SimpleNamespace(prepare_asset_rollback=Mock()),
                    enqueue_asset_recovery=AsyncMock(side_effect=pause),
                    enqueue_asset_rollback=AsyncMock(side_effect=pause))
                refresh = Mock()
                with patch.object(recovery, 'dialog_card', Node), patch.object(recovery, 'ui', widgets), patch.object(recovery, 'notify'), patch.object(recovery.run, 'io_bound', AsyncMock(return_value=prepared)), patch.object(recovery.PlanStore, 'fingerprint', return_value='plan-fp'), patch.object(recovery, 'AssetExecutionRecovery'), patch('src.rename.asset_replacement.approved_batch', return_value=SimpleNamespace(transaction_directory='backup')):
                    if mode == 'resume':
                        await recovery.open_asset_recovery(queue, 'task', refresh)
                        command, cancel = buttons['继续'], buttons['关闭']
                    else:
                        await recovery.open_asset_rollback(queue, 'task', refresh)
                        command, cancel = buttons['确认回滚'], buttons['取消']
                    job = asyncio.create_task(command())
                    await asyncio.wait_for(entered.wait(), 2)
                    try:
                        self.assertTrue(dialog.opened)
                        progress.set_visibility.assert_called_with(True)
                        await command()
                        cancel()
                        await job
                        refresh.assert_not_called()
                        self.assertFalse(dialog.opened)
                    finally:
                        job.cancel()
                        await asyncio.gather(job, return_exceptions=True)

    async def test_dashboard_retry_requires_confirmation_and_handles_cancel_failure(self):
        from src.pages import data_table_page as dashboard
        from src.pages import queue_confirmation as confirmation
        from tests.test_simplify_rollback_gui import Node
        buttons = {}
        dialog, message, progress = Node(), Mock(), Mock()
        def button(label, **kwargs):
            buttons[label] = kwargs['on_click']
            return Mock()
        message.classes.return_value = message
        progress.props.return_value = progress
        progress.classes.return_value = progress
        progress.style.return_value = progress
        widgets = SimpleNamespace(dialog=lambda: dialog, card=Node, row=Node,
            label=lambda _: message, linear_progress=lambda **_: progress, button=button)
        entered = asyncio.Event()
        async def pause(*_):
            entered.set()
            await asyncio.Event().wait()
        queue = AsyncMock(side_effect=pause)
        with patch.object(confirmation, 'dialog_card', Node), patch.object(dashboard, 'get_task', return_value={'execution_requested_fingerprint': 'fp'}), patch.object(dashboard, '_is_safe_approved_retry', return_value=True), patch.object(dashboard.task_queue, 'enqueue_approved', queue), patch.object(confirmation, 'ui', widgets), patch.object(confirmation, 'notify'):
            await dashboard._retry_approved_execution('task')
            queue.assert_not_called()
            job = asyncio.create_task(buttons['确认入队']())
            await asyncio.wait_for(entered.wait(), 2)
            progress.set_visibility.assert_called_with(True)
            buttons['取消']()
            await job
            await asyncio.wait_for(buttons['确认入队'](), 2)
            self.assertEqual(1, queue.call_count)
            await dashboard._retry_approved_execution('task')
            queue.side_effect = PlanExecutionError('原批准已失效')
            await buttons['确认入队']()
            self.assertTrue(dialog.opened)
            self.assertEqual('原批准已失效', message.set_text.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
