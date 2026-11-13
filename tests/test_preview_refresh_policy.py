"""Runtime mode is observed only for an explicit new TV retry, never an edit."""
import asyncio
import os
import unittest
from unittest.mock import patch, Mock
from tests import test_default_activation_contract as fixture


class PreviewRefreshPolicyContracts(unittest.IsolatedAsyncioTestCase):
    async def sequence(self, old_dry, new_dry):
        with patch.dict(os.environ, {'BAR_DRY_RUN': '1' if old_dry else '0'}):
            async with fixture.DefaultActivationProducerContracts().flow() as flow:
                old = flow.store.load(flow.task_id)
                old_fp = flow.store.fingerprint(old)
                self.assertIs(old.execution_policy.dry_run, old_dry)
                forbidden = Mock(side_effect=AssertionError('retry repeated provider recognition'))
                with patch.dict(os.environ, {'BAR_DRY_RUN': '1' if new_dry else '0'}), patch('src.task_queue.Rename', forbidden):
                    await flow.manager.retry_current(flow.task_id)
                    await asyncio.wait_for(flow.manager._queue.join(), 12)
                task = flow.manager._read(flow.task_id)
                self.assertEqual('completed', task['current_tv_refresh']['status'], task.get('error'))
                new = flow.store.load(flow.task_id)
                self.assertIs(new.execution_policy.dry_run, new_dry)
                self.assertNotEqual(old_fp, flow.store.fingerprint(new))
                self.assertEqual(old.execution_policy, flow.store.load_version(flow.task_id, old_fp).execution_policy)
                self.assertEqual(old.execution_policy.mode, new.execution_policy.mode)
                self.assertTrue(new.execution_policy.preserve_source)
                forbidden.assert_not_called()
                self.assertFalse(any(e.target_path.exists() for e in new.entries))

    async def test_preview_to_real(self):
        await self.sequence(True, False)

    async def test_real_to_preview(self):
        await self.sequence(False, True)

    async def test_same_modes(self):
        for mode in (False, True):
            with self.subTest(mode=mode):
                await self.sequence(mode, mode)


    async def test_policy_read_failure_preserves_original(self):
        with patch.dict(os.environ, {'BAR_DRY_RUN': '1'}):
            async with fixture.DefaultActivationProducerContracts().flow() as flow:
                old_bytes = flow.store.path_for(flow.task_id).read_bytes()
                old_state = flow.service.decision_state_store.load(flow.task_id)
                with patch('src.rename.trans.is_dry_run_enabled', side_effect=RuntimeError('policy observation unavailable')) as read:
                    await flow.manager.retry_current(flow.task_id)
                    await asyncio.wait_for(flow.manager._queue.join(), 12)
                read.assert_called_once()
                self.assertEqual('failed', flow.manager._read(flow.task_id)['current_tv_refresh']['status'])
                self.assertEqual(old_bytes, flow.store.path_for(flow.task_id).read_bytes())
                self.assertEqual(old_state, flow.service.decision_state_store.load(flow.task_id))

    async def test_policy_is_read_once_before_projection_and_not_at_commit(self):
        from src.rename import initial_tv_completion as completion
        from src.rename.trans import is_dry_run_enabled
        original_commit = completion.commit
        def switch_after_prepare(*args, **kwargs):
            with patch.dict(os.environ, {'BAR_DRY_RUN': '1'}):
                return original_commit(*args, **kwargs)
        with patch.dict(os.environ, {'BAR_DRY_RUN': '1'}):
            async with fixture.DefaultActivationProducerContracts().flow() as flow:
                with patch.dict(os.environ, {'BAR_DRY_RUN': '0'}), patch('src.rename.trans.is_dry_run_enabled', wraps=is_dry_run_enabled) as read, patch.object(completion, 'commit', switch_after_prepare):
                    await flow.manager.retry_current(flow.task_id)
                    await asyncio.wait_for(flow.manager._queue.join(), 12)
                read.assert_called_once()
                self.assertEqual('completed', flow.manager._read(flow.task_id)['current_tv_refresh']['status'])
                self.assertFalse(flow.store.load(flow.task_id).execution_policy.dry_run)
