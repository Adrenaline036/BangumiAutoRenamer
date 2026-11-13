from development.preview import preview_session
"""Interactive preview runs recognition, but never commits media operations."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.pages.plan_review_page import can_execute_approved_plan
from src.rename.plan_review import ReviewStatus
from src.rename.rename_plan import ExecutionPolicy
from src.rename.trans import Trans
from tests.test_task_queue import _isolated_manager, _source_file
from tests.source_collection_fixture import admit_task


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class InteractivePreviewContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, BAR_LOCAL_GUI_PREVIEW='true',
                              BAR_UI_TEST_MODE='false', BAR_DRY_RUN='true')
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_recognition_worker_runs_and_does_not_force_stub_preview(self):
        called = []
        async def recognize(job):
            called.append(job.task_id)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = _isolated_manager(task_root=root/'tasks', executor=recognize)
            try:
                await manager.start()
                task = await admit_task(manager, _source_file(root, 'one.mkv'), is_anime=False, is_movie=False)
                await asyncio.wait_for(manager._queue.join(), 3)
                self.assertEqual([task], called)
                saved = json.loads((root/'tasks'/f'{task}.json').read_text('utf-8'))
                self.assertFalse(saved['preview_only'])
            finally:
                await manager.stop()

    async def test_production_dry_plan_remains_non_executable_under_old_preview_flag(self):
        policy = ExecutionPolicy(target_root=Path.cwd()/'unused-target', dry_run=True)
        def available(p=policy, status=ReviewStatus.APPROVED):
            return can_execute_approved_plan(status, 'approved',  execution_policy=p)
        self.assertFalse(available())
        self.assertFalse(available(None))
        with patch.dict(os.environ, BAR_LOCAL_GUI_PREVIEW='false'):
            self.assertFalse(available())

    async def test_final_queue_confirmation_does_not_publish_or_report_success(self):
        from src.pages.queue_confirmation import QueueConfirmation
        from tests._media_flow_helpers import approved_tv
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # A real approved task, not a missing-task rejection that merely
            # happens to leave the queue unchanged.
            with patch.dict(os.environ, BAR_LOCAL_GUI_PREVIEW='false', BAR_DRY_RUN='false'):
                service, plan, fingerprint = await approved_tv(self)
            store, tasks = service.plan_store, service.task_root
            root = tasks.parent
            manager = _isolated_manager(task_root=tasks, plan_root=store.root,
                                        execution_service=service)
            self.assertEqual('approved', json.loads(
                (tasks/f'{plan.task_id}.json').read_text('utf-8'))['workflow_status'])
            baseline = files(root)
            messages, notices, success = [], [], []
            state = QueueConfirmation(SimpleNamespace(close=lambda: None),
                                      SimpleNamespace(set_text=messages.append))
            with preview_session(), patch('src.rename.queue_preflight.prepare_queue_admission',
                       side_effect=AssertionError('GUI must stop before admission')) as admission, \
                 patch('src.pages.queue_confirmation.notify', side_effect=lambda text, **kw: notices.append((text,kw))):
                await state.run(lambda: manager.enqueue_approved(plan.task_id, fingerprint),
                                lambda: success.append(True))
            admission.assert_not_called()
            self.assertEqual([], success)
            self.assertIn('GUI 预览', messages[-1])
            self.assertNotEqual('positive', notices[-1][1]['type'])
            self.assertEqual(baseline, files(root))
            self.assertFalse(manager._pending)





class PreviewMediaBoundaryContracts(unittest.TestCase):
    def test_real_transfer_still_runs_with_preview_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, target = root/'source.mkv', root/'target'/'one.mkv'
            source.write_bytes(b'preview-source')
            with patch.dict(os.environ, BAR_LOCAL_GUI_PREVIEW='false'):
                Trans({source: target}, 'gui-normal', mode='hardlink', dry_run=False,
                      record_root=root/'records').trans_file()
            self.assertTrue(os.path.samefile(source, target))

    def test_valid_approved_plan_still_executes_with_preview_disabled(self):
        from tests._media_flow_helpers import approved_tv
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, BAR_LOCAL_GUI_PREVIEW='false', BAR_DRY_RUN='false'):
                service, plan, fingerprint = asyncio.run(approved_tv(self))
                result = service.execute(plan.task_id, fingerprint)
                self.assertEqual('completed', result.ledger_status)
                self.assertTrue(all(os.path.samefile(e.source_path, e.target_path) for e in plan.entries))

    def test_real_transfer_is_blocked_even_with_explicit_dry_run_false(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root/'source.mkv'
            source.write_bytes(b'preview-source')
            target = root/'target'/'one.mkv'
            transfer = Trans({source: target}, 'gui-preview', mode='hardlink', dry_run=False,
                             record_root=root/'records')
            baseline = files(root)
            with preview_session():
                with self.assertRaisesRegex(ValueError, 'GUI 预览'):
                    transfer.trans_file()
            self.assertEqual(baseline, files(root))
            self.assertEqual(1, source.stat().st_nlink)

    def test_service_boundaries_refuse_before_consuming_any_authority_or_state(self):
        from src.rename.plan_execution import PlanExecutionService
        from src.rename import asset_rollback
        # No usable authority: preview must refuse before any lookup/lock/write.
        calls = [
            lambda: PlanExecutionService.execute(None, 'task', 'fp'),
            lambda: PlanExecutionService.rollback_asset_replacement(None, 'task', 'fp', expected_recovery_fingerprint='r'),
            lambda: asset_rollback.rollback(None, 'task', 'fp', expected_recovery_fingerprint='r'),
            lambda: Trans.recover_replacements(None),
            lambda: Trans._trans_replacements(None),
        ]
        with preview_session():
            for i, call in enumerate(calls):
                with self.subTest(boundary=i), self.assertRaisesRegex(ValueError, 'GUI 预览'):
                    call()

    def test_valid_approved_plan_refuses_without_transfer_or_record_changes(self):
        from tests._media_flow_helpers import approved_tv
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict(os.environ, BAR_LOCAL_GUI_PREVIEW='false', BAR_DRY_RUN='false'):
                service, plan, fingerprint = asyncio.run(approved_tv(self))
            root = service.task_root.parent
            baseline = files(root)
            with preview_session():
                with self.assertRaisesRegex(ValueError, 'GUI 预览'):
                    service.execute(plan.task_id, fingerprint)
            self.assertEqual(baseline, files(root))


if __name__ == '__main__':
    unittest.main()
