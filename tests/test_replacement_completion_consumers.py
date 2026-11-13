"""Completion consumers and refusal boundaries, real disposable transactions."""
import ast
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.rename.plan_store import PlanStore
from tests import test_movie_finalize_progress as incident


class CompletionConsumers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = incident.ReplacementCompletionIncident('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)





    async def test_catalog_recovery_uses_original_approval_and_finishes(self):
        from src.rename.execution_recovery import reconcile_catalog_update
        from src.rename.work_catalog import WorkCatalogError
        from src.rename.plan_execution import PlanExecutionError
        plan, service, transfer = await self.h.prepared()
        with patch.object(service.work_catalog, 'register_completed', side_effect=WorkCatalogError('catalog unavailable')):
            with self.assertRaisesRegex(PlanExecutionError, '作品登记尚未更新'):
                self.h.execute(service, plan)
        self.assertEqual('catalog_update_pending', service._read_task(plan.task_id)['execution_status'])
        self.assertTrue(list(transfer.replacement_authority.transaction_directory.glob('old-*')))
        self.assertNotEqual('completed', service._read_task(plan.task_id)['workflow_status'])
        with patch('src.rename.trans.Trans.trans_file', side_effect=AssertionError('media replay')):
            reconcile_catalog_update(plan.task_id, task_root=service.task_root, record_root=service.record_root,
                plan_store=service.plan_store, work_catalog=service.work_catalog, series_catalog=service.series_catalog,
                provider_link_catalog=service.provider_link_catalog, decision_state_root=service.decision_state_store.root,
                review_root=service.review_service.review_root, recovery_transaction_root=service.recovery_transaction_store.root)
        self.assertEqual(plan, service.plan_store.load(plan.task_id))
        self.h.assert_finished(plan, service, transfer)

    async def test_cleanup_manifest_tampering_and_new_artifact_refused(self):
        plan, service, transfer = await self.h.prepared()
        self.h.execute(service, plan)
        directory = transfer.replacement_authority.transaction_directory
        path = directory/'journal.json'
        original = path.read_bytes()
        for field, value in [('plan_fingerprint', '0'*64), ('ledger_sha256', '0'*64), ('files', {}), ('policy', 'unknown')]:
            with self.subTest(field=field):
                journal = json.loads(original)
                journal['cleanup'][field] = value
                path.write_text(json.dumps(journal), encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.h.h.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan))
                path.write_bytes(original)
        extra = directory/'old-0'
        extra.write_bytes(b'foreign file must stay')
        with self.assertRaises((ValueError, RuntimeError)):
            self.h.resume(service, plan)
        self.assertEqual(b'foreign file must stay', extra.read_bytes())
        extra.unlink()
        self.h.resume(service, plan)
        self.h.assert_finished(plan, service, transfer)

    async def test_no_cleanup_before_completed_media_and_no_queue_inverse(self):
        from src.rename.replacement_completion import finish_replacement
        from src.rename.queue_preflight import prepare_queue_admission
        plan, service, transfer = await self.h.prepared()
        before = {e.target: Path(e.target).read_bytes() for e in transfer.replacement_authority.evidence}
        with self.assertRaises(ValueError):
            finish_replacement(service, plan.task_id, PlanStore.fingerprint(plan))
        self.assertEqual(before, {p: Path(p).read_bytes() for p in before})
        self.assertFalse(transfer.replacement_authority.transaction_directory.exists())
        # Even forging a legacy continuation marker cannot grant new-policy inverse authority.
        service._update_task(plan.task_id, asset_rollback_phase='prepared')
        from src.pages.data_table_page import can_offer_asset_rollback
        self.assertFalse(can_offer_asset_rollback(service._read_task(plan.task_id), service.decision_state_store.load(plan.task_id)))
        with self.assertRaises(ValueError):
            service.prepare_asset_rollback(plan.task_id, PlanStore.fingerprint(plan))
        with self.assertRaises((ValueError, RuntimeError)):
            prepare_queue_admission(service._read_task, service, plan.task_id, PlanStore.fingerprint(plan),
                mode='asset_rollback', expected_recovery_fingerprint='0'*64)


class ProgressAppearance(unittest.TestCase):
    def test_original_task_card_and_css_unchanged_and_real_popup_bar_matches(self):
        root = Path(__file__).resolve().parents[1]
        source = (root/'src/pages/data_table_page.py').read_text('utf-8')
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == '_task_activity')
        self.assertEqual('195211c781c8346915a4ff9d6bedc545e053bd85de62081d82707049f50eb330',
            hashlib.sha256(ast.get_source_segment(source, node).encode()).hexdigest())
        css = ''
        for statement in ast.parse((root/'src/main_page.py').read_text('utf-8')).body:
            if isinstance(statement, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == 'BAR_CSS' for target in statement.targets):
                css = ast.literal_eval(statement.value)
            elif (isinstance(statement, ast.AugAssign) and isinstance(statement.target, ast.Name)
                    and statement.target.id == 'BAR_CSS'):
                self.assertIsInstance(statement.op, ast.Add)
                css += ast.literal_eval(statement.value)
        self.assertEqual('df37449c765bde4ad5b73a41bd720f41687844ce858d29b528f09bdbc20c6b76',
            hashlib.sha256(css.encode('utf-8')).hexdigest())
        from nicegui import ui
        from nicegui.client import Client
        from nicegui.page import page
        from src.pages.progress_view import bar_progress
        client = Client(page('/progress-contract'), request=None)
        try:
            with client:
                bar = bar_progress(ui)
                counted = bar_progress(ui, .25, indeterminate=False)
            self.assertEqual('blue-7', bar._props['color'])
            self.assertEqual('blue-2', bar._props['track-color'])
            self.assertTrue(bar._props['indeterminate'])
            self.assertEqual('4px', bar._style['height'])
            self.assertEqual('999px', bar._style['border-radius'])
            self.assertIn('w-full', bar._classes)
            self.assertFalse(counted._props.get('indeterminate'))
            counted.set_value(.75)
            self.assertEqual(.75, counted.value)
        finally:
            client.delete()


class CatalogGuiRouting(unittest.IsolatedAsyncioTestCase):
    async def test_new_replacement_recovery_cannot_rewrite_plan_first(self):
        from types import SimpleNamespace
        from src.pages import plan_review_page as page
        from src.rename.replacement_completion import DISCARD_REPLACED_LINKS
        owner = SimpleNamespace(plan=SimpleNamespace(task_id='case', metadata={'asset_operation': {
            'completion_policy': DISCARD_REPLACED_LINKS}}), _close_review=Mock(), recovery_service=Mock())
        with patch('src.pages.asset_recovery_view.open_asset_recovery', new_callable=AsyncMock) as recovery:
            await page.PlanReviewDialog._recover_missing_catalog(owner)
        recovery.assert_awaited_once_with(page.task_queue, 'case', owner._close_review)
        owner.recovery_service.rebuild_catalog.assert_not_called()
