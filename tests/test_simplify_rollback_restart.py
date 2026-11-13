"""Rollback generation incidents through disposable real service fixtures."""

from tests._historical_replacement import historical_replacement
import asyncio
import json
from dataclasses import replace
from unittest.mock import patch
import unittest

from src.rename.plan_store import PlanStore
from src.task_queue import TaskQueueManager
from tests import test_asset_actions_contract as action_fixture
from tests.source_collection_fixture import admit_task


class RolledBackRestartContracts(unittest.IsolatedAsyncioTestCase):
    @historical_replacement
    async def asyncSetUp(self):
        self.helper = action_fixture.AssetActionContracts('runTest')
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.root = self.helper.root
        self.old = await self.helper.fixture.single(with_font=False)
        self.original = await self.helper.complete_replacement(
            await self.helper.incoming(omit_companion=True))
        self.service = self.helper.fixture.execution_service()
        self.queue = TaskQueueManager(task_root=self.root/'tasks', execution_service=self.service,
            decision_state_root=self.root/'decisions', plan_root=self.root/'plans',
            recovery_abandon_audit_root=self.root/'abandon_audit')
        self.fp = PlanStore.fingerprint(self.original)
        prepared = self.service.prepare_asset_rollback(self.original.task_id, self.fp)
        await self.queue.start()
        try:
            with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False):
                await self.queue.enqueue_asset_rollback(self.original.task_id, self.fp,
                    expected_recovery_fingerprint=prepared.fingerprint)
                await asyncio.wait_for(self.queue._queue.join(), 20)
        finally:
            await self.queue.stop()
        self.assertEqual('rolled_back', self.task()['workflow_status'])

    def task(self):
        return json.loads((self.root/'tasks'/f'{self.original.task_id}.json').read_text('utf-8'))

    def business(self):
        roots = ('tasks', 'plans', 'decisions', 'work_catalog', 'series_catalog', 'records')
        return {p.relative_to(self.root): p.read_bytes() for folder in roots
                for p in (self.root/folder).rglob('*') if p.is_file() and p.suffix != '.lock'}

    async def restart(self):
        preparation = await self.queue.prepare_rolled_back_restart(self.original.task_id)
        return await self.queue.restart_rolled_back(self.original.task_id, preparation)

    async def test_new_unapproved_generation_preserves_history_and_choices(self):
        before = self.business()
        plan = await self.restart()
        self.assertNotEqual(self.fp, PlanStore.fingerprint(plan))
        self.assertEqual(self.original.task_id, plan.task_id)
        self.assertEqual(self.original.ignored_sources, plan.ignored_sources)
        state = self.service.decision_state_store.load(plan.task_id)
        self.assertNotEqual('replace', state.asset_operation.to_dict()['mode'])
        self.assertIsNone(state.completed_rebuild_observation)
        self.assertIsNone(state.movie_version_selection.to_dict()['replacement'])
        self.assertEqual([g['label'] for g in self.original.metadata['movie_version_selection']['groups']],
                         [g['label'] for g in state.movie_version_selection.to_dict()['groups']])
        self.assertFalse(self.service.review_service.load_or_create(plan).approved_entry_ids)
        self.assertFalse(self.task().get('asset_rollback_phase'))
        self.assertIsNone(self.task().get('execution_fingerprint'))
        for path, raw in before.items():
            if path.parts[0] in {'records', 'work_catalog', 'series_catalog'} or 'history' in path.parts:
                self.assertEqual(raw, (self.root/path).read_bytes(), str(path))
        self.assertEqual(self.fp, PlanStore.fingerprint(self.service.plan_store.load_version(plan.task_id, self.fp)))
        with self.assertRaises((ValueError, RuntimeError)):
            self.service.validate_approved(plan.task_id, self.fp)

    async def test_current_review_revision_does_not_replace_original_rollback_identity(self):
        before = self.business()
        with self.assertRaises((ValueError, RuntimeError)):
            self.service.review_service.persist_unapproved_revision(replace(self.original,
                metadata={**self.original.metadata, 'review_note': 'operator changed preview'}))
        self.assertEqual(before, self.business())
        plan = await self.restart()
        self.assertNotEqual(self.fp, PlanStore.fingerprint(plan))
        historical = self.service.plan_store.load_version(self.original.task_id, self.fp)
        self.assertEqual(self.fp, PlanStore.fingerprint(historical))
        self.assertFalse(self.task().get('asset_rollback_phase'))

    async def test_restart_cannot_clear_replacement_without_archive_intent(self):
        from src.rename.decision_state import DecisionCommandService
        original = DecisionCommandService.update
        before = self.business()

        def disconnected(command, *args, **kwargs):
            kwargs['media_archive'] = None
            return original(command, *args, **kwargs)

        with patch.object(DecisionCommandService, 'update', disconnected):
            with self.assertRaisesRegex(ValueError, '执行或恢复风险'):
                await self.restart()
        self.assertEqual(before, self.business())

    async def test_forged_task_terminal_without_completed_journal_is_rejected(self):
        from src.rename.asset_rollback import _journal_path, _read_journal, _write_journal, AssetRollbackPreparation
        from src.rename.asset_replacement import approved_batch
        from src.rename.asset_operation import AssetOperation
        path = _journal_path(self.service, approved_batch(self.original, self.fp))
        journal = _read_journal(path)
        _write_journal(path, AssetRollbackPreparation(AssetOperation.encode(journal['preparation'])), 'fonts_restored')
        before = self.business()
        with self.assertRaisesRegex((ValueError, RuntimeError), '回滚|完成'):
            await self.restart()
        self.assertEqual(before, self.business())

    async def test_disconnected_projection_keeps_current_business_state(self):
        before = self.business()
        with patch.object(type(self.service.decision_state_store), 'project_plan', side_effect=ValueError('disconnected')):
            with self.assertRaisesRegex(ValueError, 'disconnected'):
                await self.restart()
        after = self.business()
        for path, raw in before.items():
            self.assertEqual(raw, after[path], str(path))

    async def test_stale_preparation_cannot_reset_new_generation(self):
        preparation = await self.queue.prepare_rolled_back_restart(self.original.task_id)
        await self.queue.restart_rolled_back(self.original.task_id, preparation)
        before = self.business()
        with self.assertRaises((ValueError, RuntimeError)):
            await self.queue.restart_rolled_back(self.original.task_id, preparation)
        self.assertEqual(before, self.business())

    async def test_new_generation_can_explicitly_select_replace_approve_and_execute(self):
        from src.rename.asset_replacement import approved_batch
        new = await self.restart()
        review = self.service.review_service
        selection = self.service.decision_state_store.load(new.task_id).movie_version_selection.to_dict()
        selected = review.revise_movie_versions(new.task_id, PlanStore.fingerprint(new),
            groups=[{'sources': row['sources'], 'label': row['label']} for row in selection['groups']],
            excluded_sources=selection['excluded_sources'], replace_version_ref=selection['groups'][0]['version_ref'])
        operation, _ = review.asset_actions().prepare_replacement(selected)
        confirmed = review.confirm_asset_replacement(new.task_id, PlanStore.fingerprint(selected),
            expected_operation_fingerprint=operation.fingerprint)
        self.assertNotEqual(approved_batch(self.original, self.fp).transaction_key,
            approved_batch(confirmed, PlanStore.fingerprint(confirmed)).transaction_key)
        task, ledger, _ = await self.helper.fixture.harness._approve_and_execute(root=self.root,
            result=self.helper.fixture.current(new.task_id))
        self.assertEqual('completed', task['execution_status'], task.get('error'))
        self.assertEqual(PlanStore.fingerprint(confirmed), ledger['plan_fingerprint'])

    async def test_safe_end_preserves_loadable_plan_and_media_audit(self):
        before = self.business()
        await self.queue.abandon_and_remove(self.original.task_id, record_root=self.root/'records',
            plan_root=self.root/'plans', review_root=self.root/'reviews')
        historical = self.service.plan_store.load_version(self.original.task_id, self.fp)
        self.assertEqual(self.fp, PlanStore.fingerprint(historical))
        for path, raw in before.items():
            if path.parts[0] in {'records', 'work_catalog', 'series_catalog'}:
                self.assertEqual(raw, (self.root/path).read_bytes(), str(path))
        from src.rename.plan_review import entry_id
        from src.rename.asset_execution_recovery import AssetExecutionRecovery
        with self.assertRaises((ValueError, RuntimeError)):
            self.service.review_service.approve_entries(self.original.task_id, self.fp,
                [entry_id(e) for e in self.original.entries])
        with self.assertRaises((ValueError, RuntimeError)):
            self.service.validate_approved_authority(self.original.task_id, self.fp)
        with self.assertRaises((ValueError, RuntimeError)):
            AssetExecutionRecovery(self.service).inspect(self.original.task_id, self.fp)
        with self.assertRaises((ValueError, RuntimeError)):
            self.service.execute(self.original.task_id, self.fp)

    async def test_missing_retirement_evidence_never_restores_approval(self):
        from src.rename.plan_review import entry_id
        await self.queue.abandon_and_remove(self.original.task_id, record_root=self.root/'records',
            plan_root=self.root/'plans', review_root=self.root/'reviews')
        self.queue.projection_commit_service.ownership_store._path(self.original.task_id).rename(
            self.root/'retained-ownership.json')
        with self.assertRaises((ValueError, RuntimeError)):
            self.service.review_service.approve_entries(self.original.task_id, self.fp,
                [entry_id(e) for e in self.original.entries])

    async def test_safe_end_uuid_cannot_reenter_single_or_batch_admission(self):
        await self.queue.abandon_and_remove(self.original.task_id, record_root=self.root/'records',
            plan_root=self.root/'plans', review_root=self.root/'reviews')
        self.queue.projection_commit_service.ownership_store._path(self.original.task_id).rename(
            self.root/'retained-ownership.json')
        try:
            with self.assertRaises((ValueError, RuntimeError)):
                await admit_task(self.queue, self.original.source_root, task_id=self.original.task_id, preview_only=True)
            batch = self.queue.prepare_input_batch((self.original.source_root,), is_anime=True,
                is_movie=True,  source='test', preview_only=True)
            with self.assertRaises((ValueError, RuntimeError)):
                await self.queue.enqueue_batch(batch_id=batch['batch_id'],
                    task_requests=(replace(batch['task_requests'][0], task_id=self.original.task_id),),
                    jobs=(replace(batch['jobs'][0], task_id=self.original.task_id),))
        finally:
            await self.queue.stop()

    async def test_safe_end_and_approval_share_commit_lock(self):
        import threading
        from src.rename.plan_review import entry_id
        review = self.service.review_service
        entered, release = threading.Event(), threading.Event()
        original_save = review._save
        # A completed append is now intentionally ineligible for reapproval.
        # Exercise the lock at a real, unexecuted append's approval commit.
        old = await self.helper.incoming('2160p', generation='approval-race')

        def delayed_save(decision):
            if decision.task_id == old.task_id and decision.approved_entry_ids:
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test barrier timed out')
            return original_save(decision)

        async def abandon():
            await self.queue.abandon_and_remove(old.task_id, record_root=self.root/'records',
                plan_root=self.root/'plans', review_root=self.root/'reviews')

        with patch.object(review, '_save', side_effect=delayed_save):
            job = asyncio.create_task(asyncio.to_thread(review.approve_entries, old.task_id,
                PlanStore.fingerprint(old), [entry_id(e) for e in old.entries]))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 10))
                try:
                    await abandon()
                    ended = True
                except (ValueError, RuntimeError):
                    ended = False  # A shared lock may explicitly reject the concurrent command.
            finally:
                release.set()
                # Always retrieve the background exception if the barrier was
                # never reached; a failed preflight must not leak a test task.
                await job
        if not ended:
            await abandon()
        self.assertFalse((review.review_root/f'{old.task_id}.json').exists(),
                         'an approval must not reappear after safe end')


if __name__ == '__main__':
    unittest.main()
