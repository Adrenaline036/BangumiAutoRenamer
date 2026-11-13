"""Visible status words must not confuse planning with approved file writes."""
from tests._approval_stage_helpers import approve_dialog
import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from nicegui import Client
from nicegui.page import page as nice_page
import src.main_page as main
from src.pages import data_table_page as dashboard
from src.pages import plan_review_page as review_page
from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from tests import test_general_season_correction_contract as season
from tests.test_task_queue import _isolated_manager, _source_file
from tests.source_collection_fixture import admit_task


class StatusLanguageContracts(unittest.IsolatedAsyncioTestCase):
    flow = season.GeneralSeasonCorrectionContracts.flow
    current = season.GeneralSeasonCorrectionContracts.current

    def client(self):
        client = Client(nice_page('/isolated-status-language'), request=None)
        self.addCleanup(client.delete)
        return client

    def texts(self, client):
        return [str(getattr(e, 'text', '')) for e in client.elements.values()]

    async def test_actual_header_states_approval_requirement_and_retains_readonly_modes(self):
        for test, dry, expected in ((False, True, '只读预演 · 不创建文件'), (False, False, '文件整理须批准后执行')):
            with self.subTest(test=test, dry=dry):
                client = self.client()
                with (client,
patch.object(main, 'is_dry_run_enabled', return_value=dry),
patch.object(main, 'create_table'),
patch.object(main.ui, 'timer')):
                    main.main_page()
                self.assertIn(expected, self.texts(client))
                self.assertNotIn('真实执行已启用', self.texts(client))

    def card(self, row):
        client = self.client()
        with client:
            dashboard._task_card(row)
        self.last_card = client
        return self.texts(client)

    def assert_single_activity(self, labels):
        from nicegui.elements.spinner import Spinner
        from nicegui.elements.progress import LinearProgress
        self.assertEqual(1, labels.count('处理中'))
        bars = [e for e in self.last_card.elements.values() if isinstance(e, LinearProgress)]
        self.assertEqual(1, len(bars))
        self.assertTrue(bars[0]._props['indeterminate'])
        self.assertEqual('blue-7', bars[0]._props['color'])
        self.assertEqual('blue-2', bars[0]._props['track-color'])
        self.assertEqual(0, sum(isinstance(e, Spinner) for e in self.last_card.elements.values()))

    async def test_actual_worker_running_does_not_claim_file_writes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(job):
            entered.set()
            await release.wait()
            manager._update(job.task_id, workflow_status='need_confirm')
        with tempfile.TemporaryDirectory(prefix='bar-status-queue-') as directory:
            root = Path(directory)
            manager = _isolated_manager(task_root=root / 'tasks', executor=blocked)
            try:
                await admit_task(manager, _source_file(root, 'Sample S01E01.mkv'), task_id='status-recognition',
                    is_anime=False, is_movie=False)
                await asyncio.wait_for(entered.wait(), 3)
                row = manager._read('status-recognition')
                self.assertEqual('running', row['workflow_status'])
                labels = self.card(row)
                self.assert_single_activity(labels)
                self.assertNotIn('执行中', labels)
            finally:
                release.set()
                await manager.stop()

    async def test_actual_approved_worker_summarizes_activity_and_keeps_failure_feedback(self):
        async with self.current() as (flow, plan, _state):
            fingerprint = PlanStore.fingerprint(plan)
            owner = SimpleNamespace(plan=plan, task_data=flow.manager._read(flow.task_id), review_service=flow.service,
                review_badge=SimpleNamespace(text=''), _refresh_plan_header=lambda: None,
                _render_entries=lambda: None, _refresh_execute_button=lambda: None)
            with patch.object(review_page, 'get_task', side_effect=flow.manager._read), patch.object(review_page, 'notify'):
                await approve_dialog(owner, tuple(entry_id(e) for e in plan.entries))
            entered, release = asyncio.Event(), asyncio.Event()
            async def blocked(job):
                entered.set()
                await release.wait()
                raise ValueError('来源文件缺失，请核对来源')
            flow.manager.approved_executor = blocked
            try:
                await flow.manager.enqueue_approved(flow.task_id, fingerprint)
                await asyncio.wait_for(entered.wait(), 3)
                row = flow.manager._read(flow.task_id)
                self.assertEqual(('running', 'execute_approved'), (row['workflow_status'], row['queue_operation']))
                with self.subTest(surface='approved_worker'):
                    labels = self.card(row)
                    self.assert_single_activity(labels)
                    self.assertNotIn('执行已批准的整理计划', labels)
                    self.assertNotIn('执行中', labels)
                # Render execution-service state without performing media writes.
                for status in ('analyzing','executing'):
                    with self.subTest(surface=status):
                        labels = self.card({**row, 'workflow_status': status, 'execution_status': 'executing',
                            'execution_phase': 'media'})
                        self.assert_single_activity(labels)
                        for obsolete in ('分析中', '文件整理中', '字体整理中', '执行已批准的整理计划', '收集字体：1 / 52'):
                            self.assertNotIn(obsolete, labels)
                        self.assertNotIn('只读预演 · 不创建文件', labels)
                # New display requirement, not an old wording control.
                with self.subTest(surface='queued_position'):
                    labels = self.card({**row, 'workflow_status': 'queued', 'queue_position': 2})
                    self.assertIn('排队中 · 第2位', labels)
                    self.assertNotIn('处理中', labels)
                    from nicegui.elements.progress import LinearProgress
                    bars = [e for e in self.last_card.elements.values() if isinstance(e, LinearProgress)]
                    self.assertEqual(1, len(bars))
                    self.assertFalse(bars[0]._props.get('indeterminate', False))
                    self.assertEqual('amber-7', bars[0]._props['color'])
                self.assertTrue(all(not e.target_path.exists() for e in plan.entries))
            finally:
                release.set()
                await asyncio.wait_for(flow.manager._queue.join(), 5)
            failed = flow.manager._read(flow.task_id)
            self.assertEqual('failed', failed['workflow_status'])
            self.assertEqual('failed_before_media', failed['execution_phase'])
            self.assertIs(False, failed['execution_requires_manual_check'])
            self.assertIs(False, failed['execution_may_have_written_targets'])
            self.assertIn('来源文件缺失，请核对来源', failed['error'])
            labels = self.card(failed)
            self.assertIn('执行准备未完成，文件尚未处理。请稍后重新执行。', labels)
            self.assertNotIn('处理中', labels)
            self.assertNotIn('执行结果需要核查，请先确认目标文件。', labels)


    async def test_actual_mixed_summary_keeps_counts_without_calling_all_work_execution(self):
        rows = [dict(uuid=str(i), workflow_status=status) for i, status in enumerate(
            ('queued', 'running', 'analyzing', 'executing', 'need_confirm', 'approved', 'completed'))]
        self.assertEqual({'queued': 1, 'running': 3, 'review': 2, 'completed': 1}, dashboard.task_counts(rows))
        client = self.client()
        with client, patch.object(dashboard, 'load_task_rows', return_value=rows):
            dashboard._dashboard_summary()
        labels = self.texts(client)
        self.assertIn('处理中', labels)
        self.assertIn('3', labels)
        self.assertNotIn('执行中', labels)

    async def test_actual_failure_after_media_boundary_requires_manual_check(self):
        async with self.current() as (flow, plan, _state):
            owner = SimpleNamespace(plan=plan, task_data=flow.manager._read(flow.task_id), review_service=flow.service,
                review_badge=SimpleNamespace(text=''), _refresh_plan_header=lambda: None,
                _render_entries=lambda: None, _refresh_execute_button=lambda: None)
            with patch.object(review_page, 'get_task', side_effect=flow.manager._read), patch.object(review_page, 'notify'):
                await approve_dialog(owner)
            async def fail_after_boundary(job):
                flow.manager._update(job.task_id, execution_phase='media_boundary_entered', execution_status='executing')
                raise ValueError('隔离故障：已进入媒体边界')
            flow.manager.approved_executor = fail_after_boundary
            await flow.manager.enqueue_approved(flow.task_id, PlanStore.fingerprint(plan))
            await asyncio.wait_for(flow.manager._queue.join(), 5)
            failed = flow.manager._read(flow.task_id)
            self.assertEqual('failed', failed['workflow_status'])
            self.assertEqual('media_boundary_entered', failed['execution_phase'])
            self.assertIs(True, failed['execution_requires_manual_check'])
            self.assertIs(True, failed['execution_may_have_written_targets'])
            self.assertIn('已进入媒体边界', failed['error'])
            labels = self.card(failed)
            self.assertIn('执行结果需要核查，请先确认目标文件。', labels)
            self.assertNotIn('处理中', labels)
            self.assertNotIn('重建', labels)
            self.assertNotIn('删除', labels)
            self.assertTrue(all(not e.target_path.exists() for e in plan.entries))

    async def test_actual_recognition_failure_keeps_specific_feedback(self):
        async def fail(_job):
            raise ValueError('来源文件缺失，请核对来源')
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manager = _isolated_manager(task_root=root/'tasks', executor=fail)
            try:
                await admit_task(manager, _source_file(root, 'Sample S01E01.mkv'), task_id='failed-recognition')
                await asyncio.wait_for(manager._queue.join(), 5)
                failed = manager._read('failed-recognition')
                self.assertEqual('failed', failed['workflow_status'])
                self.assertTrue(any('来源文件缺失，请核对来源' in text for text in self.card(failed)))
                self.assertNotIn('处理中', self.texts(self.last_card))
            finally:
                await manager.stop()

