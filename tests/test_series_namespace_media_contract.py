"""Actual historical TV replacement lifecycle; never admit pending v2 media."""

from tests._historical_replacement import historical_replacement
from tests._approval_stage_helpers import approve_dialog
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from src.rename.trans import Trans
from tests.test_content_policy_contract import historical_admission


class SeriesNamespaceMediaContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.test_asset_actions_contract import AssetActionContracts
        self.h = AssetActionContracts('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.root = self.h.root

    async def approved(self):
        from src.pages.plan_review_page import PlanReviewDialog
        from src.rename.plan_review import entry_id
        from src.rename.asset_replacement import approved_batch
        with historical_admission():
            _harness, _old, _companion, incoming = await self.h.retained_series()
            service = self.h.fixture.execution_service()
            review = service.review_service
            operation, _ = review.asset_actions().prepare_replacement(incoming.plan)
            plan = review.confirm_asset_replacement(incoming.plan.task_id,
                PlanStore.fingerprint(incoming.plan), expected_operation_fingerprint=operation.fingerprint)
            dialog = SimpleNamespace(review_service=review, plan=plan,
                task_data=service._read_task(plan.task_id), review_badge=SimpleNamespace(text=''),
                _refresh_plan_header=lambda: None, _render_entries=lambda: None,
                _refresh_execute_button=lambda: None)
            from nicegui import Client
            from nicegui.page import page as nice_page
            client = Client(nice_page('/isolated-series-namespace-approval'), request=None)
            try:
                with client, patch('src.utils.utils.TASK_PATH', self.root / 'tasks'), patch('src.pages.plan_review_page.notify'):
                    await approve_dialog(dialog, tuple(entry_id(entry) for entry in plan.entries))
            finally:
                client.delete()
            self.assertEqual('approved', review.load(plan.task_id).status.value)
        self.assertNotIn('content_policy', plan.metadata)
        self.assertIsNone(service.decision_state_store.load(plan.task_id).content_policy)
        self.assertEqual(1, service._read_task(plan.task_id)['task_source_scope']['schema_version'])
        fp = PlanStore.fingerprint(plan)
        transfer = Trans(plan.as_mapping(), plan.task_id, fp, mode='hardlink', dry_run=False,
            record_root=service.record_root, operations=service._build_execution_operations(plan),
            replacement_authority=approved_batch(plan, fp))
        return plan, service, transfer

    def duplicate(self, catalog, plan):
        from src.rename.series_catalog import SeriesCatalog
        from tests.test_series_catalog import completed_tv_plan, canonical_series_key, tmdb_tv
        peer = SeriesCatalog(self.root / 'namespace-peer')
        key = canonical_series_key(tmdb_tv('991234'))
        actual = completed_tv_plan(self.root / 'peer', task_id='namespace-peer',
            series_key=key, season=1, provider=tmdb_tv('991234'))
        peer.register_completed(actual)
        record = json.loads(peer._path(key).read_text('utf-8'))
        record['canonical_root'] = str(plan.metadata['canonical_work_root'])
        path = catalog._path(key)
        path.write_text(json.dumps(record), encoding='utf-8')
        return path

    def interrupt(self, transfer):
        from tests.test_asset_execution_recovery_contract import AssetExecutionRecoveryContracts
        AssetExecutionRecoveryContracts.interrupt_after_publish(self, transfer)

    def reconcile(self, plan, service):
        from tests.test_asset_execution_recovery_contract import AssetExecutionRecoveryContracts
        return AssetExecutionRecoveryContracts.reconcile(self, plan, service)

    def business_files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*.json')}

    async def test_completed_recovery_consumers_reject_guard_disconnect_without_writes(self):
        from src.rename.recovery_service import RecoveryService
        from src.rename.series_catalog import SeriesCatalog
        plan, service, _ = await self.approved()
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False):
            service.execute(plan.task_id, PlanStore.fingerprint(plan))
        recovery = RecoveryService(catalog_root=service.work_catalog.root, record_root=service.record_root,
            series_catalog_root=service.series_catalog.root)
        state = service.decision_state_store.load(plan.task_id)
        self.assertEqual('healthy', recovery.diagnose(state, plan).state)
        recovery.rebuild_catalog(state, plan, service.work_catalog, service.series_catalog)
        # Full completed tasks are not pending reconciliation. The pending
        # test below exercises the already-written Catalog early return.
        with self.assertRaisesRegex((ValueError, RuntimeError), '待恢复'):
            self.reconcile(plan, service)
        for consumer in ('diagnose', 'rebuild'):
            with self.subTest(consumer=consumer):
                before = self.business_files()
                with patch.object(SeriesCatalog, 'validate_namespace_candidate', return_value=None):
                    if consumer == 'diagnose':
                        report = recovery.diagnose(state, plan)
                        self.assertEqual('catalog_corrupt', report.state)
                        self.assertFalse(report.can_rebuild_catalog)
                    else:
                        with self.assertRaisesRegex((ValueError, RuntimeError), '候选验证'):
                            recovery.rebuild_catalog(state, plan, service.work_catalog, service.series_catalog)
                self.assertEqual(before, self.business_files())

    async def test_pending_catalog_recovery_checks_guard_then_completes_without_media(self):
        from src.rename.series_catalog import SeriesCatalog, SeriesCatalogError
        plan, service, _ = await self.approved()
        fp = PlanStore.fingerprint(plan)
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False), \
                patch.object(service.series_catalog, 'register_completed', side_effect=SeriesCatalogError('test catalog interruption')):
            with self.assertRaisesRegex((ValueError, RuntimeError), '登记尚未更新'):
                service.execute(plan.task_id, fp)
        self.assertEqual('catalog_update_pending', service._read_task(plan.task_id)['execution_status'])
        before = self.business_files()
        with patch.object(SeriesCatalog, 'validate_namespace_candidate', return_value=None):
            with self.assertRaisesRegex((ValueError, RuntimeError), '候选验证'):
                self.reconcile(plan, service)
        self.assertEqual(before, self.business_files())
        # Actual reconciliation publishes Catalog, then loses the task write.
        # This reaches the already-materialized branch after a fresh restart.
        key = plan.metadata['series_key']
        revision = service.series_catalog.load(key).revision
        with patch('src.utils.utils._atomic_write_task_at', side_effect=OSError('test crash after catalog')):
            with self.assertRaisesRegex((ValueError, RuntimeError), 'test crash after catalog'):
                self.reconcile(plan, service)
        self.assertEqual(revision + 1, service.series_catalog.load(key).revision)
        self.assertEqual('catalog_update_pending', service._read_task(plan.task_id)['execution_status'])
        before = self.business_files()
        with patch.object(SeriesCatalog, 'validate_namespace_candidate', return_value=None):
            with self.assertRaisesRegex((ValueError, RuntimeError), '候选验证'):
                self.reconcile(plan, service)
        self.assertEqual(before, self.business_files())
        fresh = self.h.fixture.execution_service()
        with patch.object(Trans, 'trans_file', side_effect=AssertionError('unexpected media')) as media, \
                patch.object(SeriesCatalog, '_atomic_write', side_effect=AssertionError('unexpected repeated catalog write')) as writer:
            self.reconcile(plan, fresh)
            media.assert_not_called()
            writer.assert_not_called()
        self.assertEqual('completed', fresh._read_task(plan.task_id)['execution_status'])

    async def test_actual_completed_legacy_noop_backfill_requires_namespace_guard(self):
        from tests._media_flow_helpers import MediaFlow
        harness = MediaFlow('runTest')
        with historical_admission():
            old, _archive = await harness._materialize_s1(self.root)
        plan = old.plan
        service = self.h.fixture.execution_service()
        catalog = service.series_catalog
        key = plan.metadata['series_key']
        path = catalog._path(key)
        original = json.loads(path.read_text('utf-8'))
        # Current fonts are independent; this actual completed fixture has no
        # legacy associated-font rows. It proves the no-op path, not migration.
        self.assertEqual([], original['associated_assets'])
        original.update(schema_version=1, associated_assets=[])
        path.write_text(json.dumps(original), encoding='utf-8')
        before = path.read_bytes()
        ledger = json.loads((service.record_root / f'{plan.task_id}.ledger.json').read_text('utf-8'))
        with patch.object(catalog, 'validate_namespace_candidate', return_value=None, create=True):
            with self.assertRaisesRegex((ValueError, RuntimeError), '候选验证'):
                catalog.backfill_legacy_completed(key, ((plan, ledger),))
        self.assertEqual(before, path.read_bytes())

    async def test_execute_checks_series_namespace_before_transfer(self):
        plan, service, _ = await self.approved()
        self.duplicate(service.series_catalog, plan)
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False), \
                patch.object(Trans, 'trans_file', side_effect=AssertionError('unexpected media')) as media:
            with self.assertRaisesRegex((ValueError, RuntimeError), '目录|冲突'):
                service.execute(plan.task_id, PlanStore.fingerprint(plan))
            media.assert_not_called()

    async def test_resume_checks_series_namespace_before_transfer(self):
        from src.rename.asset_resume import resume_asset
        plan, service, transfer = await self.approved()
        self.interrupt(transfer)
        self.duplicate(service.series_catalog, plan)
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False), \
                patch.object(Trans, 'trans_file', side_effect=AssertionError('unexpected media')) as media:
            with self.assertRaisesRegex((ValueError, RuntimeError), '目录|冲突'):
                resume_asset(service, plan.task_id, PlanStore.fingerprint(plan))
            media.assert_not_called()

    @historical_replacement
    async def test_rollback_checks_series_namespace_before_inverse(self):
        from src.rename.asset_rollback import rollback
        plan, service, _ = await self.approved()
        fp = PlanStore.fingerprint(plan)
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False):
            service.execute(plan.task_id, fp)
        prepared = service.prepare_asset_rollback(plan.task_id, fp)
        self.duplicate(service.series_catalog, plan)
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False), \
                patch.object(Trans, 'recover_replacements', side_effect=AssertionError('unexpected inverse')) as media:
            with self.assertRaisesRegex((ValueError, RuntimeError), '目录|冲突'):
                rollback(service, plan.task_id, fp, expected_recovery_fingerprint=prepared.fingerprint)
            media.assert_not_called()

    @historical_replacement
    async def test_final_series_writer_failure_preserves_catalog_then_resumes(self):
        from src.rename.asset_rollback import rollback
        plan, service, _ = await self.approved()
        fp = PlanStore.fingerprint(plan)
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False):
            service.execute(plan.task_id, fp)
        prepared = service.prepare_asset_rollback(plan.task_id, fp)
        catalog = service.series_catalog
        key = plan.metadata['series_key']
        before = catalog._path(key).read_bytes()
        original = getattr(catalog, 'validate_namespace_candidate', None)
        calls = []
        def disconnect_final(key, candidate):
            calls.append(key)
            return None if len(calls) == 2 else original(key, candidate)
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False), \
                patch.object(catalog, 'validate_namespace_candidate', side_effect=disconnect_final, create=True):
            with self.assertRaisesRegex((ValueError, RuntimeError), '候选验证'):
                rollback(service, plan.task_id, fp, expected_recovery_fingerprint=prepared.fingerprint)
        self.assertEqual(2, len(calls))
        self.assertEqual(before, catalog._path(key).read_bytes())
        from src.rename import asset_rollback
        from src.rename.asset_replacement import approved_batch
        journal = asset_rollback._read_journal(asset_rollback._journal_path(service, approved_batch(plan, fp)))
        self.assertEqual('media_restored', journal['phase'])
        fresh = self.h.fixture.execution_service()
        media_root = Path(plan.metadata['canonical_work_root'])
        identities = {str(p): (p.stat().st_dev, p.stat().st_ino, p.stat().st_size, p.read_bytes())
                      for p in media_root.rglob('*') if p.is_file()}
        original_replace, original_unlink = os.replace, os.unlink
        def guarded_replace(source, target, *args, **kwargs):
            if Path(target).is_relative_to(media_root):
                raise AssertionError('resumed rollback must not republish media')
            return original_replace(source, target, *args, **kwargs)
        def guarded_unlink(path, *args, **kwargs):
            if Path(path).is_relative_to(media_root):
                raise AssertionError('resumed rollback must not remove media')
            return original_unlink(path, *args, **kwargs)
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False), \
                patch('os.link', side_effect=AssertionError('resumed rollback must not relink media')), \
                patch('os.replace', side_effect=guarded_replace), patch('os.unlink', side_effect=guarded_unlink):
            result = rollback(fresh, plan.task_id, fp, expected_recovery_fingerprint=prepared.fingerprint)
        self.assertEqual('rolled_back', result.ledger_status)
        self.assertEqual(identities, {str(p): (p.stat().st_dev, p.stat().st_ino, p.stat().st_size, p.read_bytes())
                                    for p in media_root.rglob('*') if p.is_file()})
