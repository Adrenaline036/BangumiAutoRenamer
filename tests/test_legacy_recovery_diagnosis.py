"""ERR088: readable historical inputs are not current recovery authority.

Owned disposable files only. Historical shapes are explicit fixtures, never a
current production Plan with its receipt removed. Provider transport is frozen
only in the separate real canonical/approval/execution control.
"""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from src.pages import plan_review_page as page
from src.rename.decision_state import DecisionState, DecisionStateStore
from src.rename.plan_store import PlanStore, PlanStoreError
from src.rename.projection_authority import ProjectionAuthorityError
from src.rename.recovery_service import RecoveryService
from src.rename.rename_plan import PlanEntry, PresentationGroup, RenamePlan
from src.rename.series_catalog import SeriesCatalog
from src.rename.work_catalog import WorkCatalog
from tests import test_default_activation_contract as activation
from tests import _media_flow_helpers as execution


SERIES = 'series-12345678-1234-1234-1234-123456789abc'


def business_bytes(root):
    # Namespace lock files are synchronization infrastructure, not data records.
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and p.suffix != '.lock'}


class LegacyDiagnosisContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.target = self.root/'source.mkv', self.root/'target.mkv'
        self.source.write_bytes(b'owned historical fixture')
        os.link(self.source, self.target)
        self.store = PlanStore(self.root/'plans')
        self.store.root.mkdir()
        self.series_root, self.record_root = self.root/'series', self.root/'records'
        self.series_root.mkdir()
        self.record_root.mkdir()
        # Pre-receipt current-schema shape, not modified modern authority.
        self.plan = RenamePlan('old-readable', self.root,
            (PlanEntry(self.source, self.target, media_kind='video', role='main_episode'),),
            expected_sources=(self.source,),
            presentation_groups=(PresentationGroup('legacy-tv', 'tv_episodes', 'Old show', None),))
        self.store.path_for(self.plan.task_id).write_text(json.dumps(self.plan.to_dict()), encoding='utf-8')
        self.plan = self.store.load(self.plan.task_id)
        self.decision = DecisionState(task_id=self.plan.task_id, revision=1,
            series_binding={'series_key': SERIES})
        self.ledger_path = self.record_root/f'{self.plan.task_id}.ledger.json'
        self.write_ledger(PlanStore.fingerprint(self.plan))

    def write_ledger(self, fingerprint):
        self.ledger_path.write_text(json.dumps({'schema_version': 2, 'task_id': self.plan.task_id,
            'status': 'completed', 'plan_fingerprint': fingerprint, 'entries': []}), encoding='utf-8')

    def service(self):
        return RecoveryService(catalog_root=self.root/'work', record_root=self.record_root,
                               series_catalog_root=self.series_root)

    def assert_blocked(self, snapshot):
        self.assertEqual('evidence_conflict', snapshot.state)
        self.assertFalse(snapshot.can_rebuild_catalog)
        self.assertFalse(snapshot.can_rebuild_plan)
        self.assertTrue(snapshot.requires_external_action)
        self.assertEqual((), snapshot.allowed_actions)

    def test_unsigned_series_domain_target_catalog_matrix_is_readonly_and_repeatable(self):
        catalog_path = self.series_root/f'{SERIES}.json'
        for domain in ('animation', 'general'):
            for target_exists in (True, False):
                for catalog_exists in (True, False):
                    with self.subTest(domain=domain, target=target_exists, catalog=catalog_exists):
                        if target_exists and not self.target.exists():
                            os.link(self.source, self.target)
                        elif not target_exists:
                            self.target.unlink(missing_ok=True)
                        if catalog_exists:
                            catalog_path.write_text(json.dumps({'schema_version': 1, 'series_key': SERIES,
                                'revision': 1, 'canonical_root': str(self.root),
                                'canonical_display_name': 'Old show', 'provider_evidence': [],
                                'season_bindings': []}), encoding='utf-8')
                        else:
                            catalog_path.unlink(missing_ok=True)
                        decision = replace(self.decision, library_domain=domain)
                        before = business_bytes(self.root)
                        service = self.service()
                        first = service.diagnose(decision, self.plan)
                        self.assert_blocked(first)
                        self.assertIn('凭据', first.detail)
                        self.assertNotIn('记录损坏', first.detail)
                        self.assertEqual(first, service.diagnose(decision, self.plan))
                        self.assertEqual(first, self.service().diagnose(decision, self.store.load(self.plan.task_id)))
                        self.assertEqual(before, business_bytes(self.root))

    def test_current_schema_history_preserves_bytes_and_ledger_fingerprint(self):
        before = business_bytes(self.root)
        original_ledger = self.ledger_path.read_bytes()
        loaded = self.store.load(self.plan.task_id)
        self.assertEqual(self.plan, loaded)
        self.assertEqual(PlanStore.fingerprint(loaded),
                         json.loads(original_ledger)['plan_fingerprint'])
        self.assert_blocked(self.service().diagnose(self.decision, loaded))
        self.assertEqual(original_ledger, self.ledger_path.read_bytes())
        self.assertEqual(before, business_bytes(self.root))

    def test_original_legacy_schema_and_original_ledger_hash_are_not_resigned(self):
        for version in (4, 5, 6):
            with self.subTest(schema=version):
                raw = {'schema_version': version, 'task_id': self.plan.task_id,
                       'source_root': str(self.root), 'entries': [self.plan.entries[0].to_dict()],
                       'expected_sources': [str(self.source)], 'metadata': {}}
                original = json.dumps(raw, ensure_ascii=False).encode('utf-8')
                path = self.store.path_for(self.plan.task_id)
                path.write_bytes(original)
                self.write_ledger(PlanStore.fingerprint_payload(raw))
                before = business_bytes(self.root)
                with self.assertRaises(PlanStoreError):
                    self.store.load(self.plan.task_id)
                self.assertEqual(original, path.read_bytes())
                self.assertEqual(before, business_bytes(self.root))

    def test_public_rebuild_commands_reject_and_preserve_all_business_bytes(self):
        retained = self.root / "retained-target.mkv"
        self.assertEqual(self.root.resolve(), self.target.resolve().parent)
        self.assertFalse(retained.exists())
        original = self.target.read_bytes()
        self.target.rename(retained)
        self.assertFalse(self.target.exists())
        self.assertEqual(original, retained.read_bytes())
        service = self.service()
        before = business_bytes(self.root)
        with self.assertRaises((ValueError, ProjectionAuthorityError)):
            service.rebuild_missing_materialization_plan(self.decision, self.plan, self.store)
        self.assertEqual(before, business_bytes(self.root))
        with self.assertRaises((ValueError, ProjectionAuthorityError)):
            service.rebuild_catalog(self.decision, self.plan, WorkCatalog(self.root/'work'),
                                    SeriesCatalog(self.series_root))
        self.assertEqual(before, business_bytes(self.root))

    def test_unrelated_programming_error_is_not_disguised_as_legacy_record(self):
        before = business_bytes(self.root)
        with patch.object(SeriesCatalog, 'require_namespace_plan', side_effect=AttributeError('programming defect')):
            with self.assertRaisesRegex(AttributeError, 'programming defect'):
                self.service().diagnose(self.decision, self.plan)
        self.assertEqual(before, business_bytes(self.root))

    def render_legacy_gui(self, *, exercise_catalog_branch=False, pending_error=None, pending_result=None):
        labels, buttons = [], []
        widget = MagicMock()
        widget.classes.return_value = widget
        widget.props.return_value = widget
        ui = SimpleNamespace(row=lambda: widget, column=lambda: widget, icon=lambda *_: widget,
            label=lambda text: (labels.append(text) or widget),
            button=lambda text, **kwargs: (buttons.append(text) or widget))
        # Real frozen failed binding routes the actual UI; missing Catalog load
        # invokes actual RecoveryService, not a mocked diagnostic snapshot.
        decision = replace(self.decision, execution_failure={
            'code': 'CATALOG_RECORD_MISSING',
            'catalog_backend': 'series', 'catalog_key': SERIES, 'catalog_revision': 0})
        from src.rename.recovery_transaction import CatalogDetachService, RecoveryTransactionStore
        dialog = SimpleNamespace(plan=self.plan, task_decision=decision,
            task_data={'execution_status': 'completed'}, series_catalog=SeriesCatalog(self.series_root),
            catalog_detach_service=CatalogDetachService(
                transaction_store=RecoveryTransactionStore(self.root/'transactions'),
                decision_store=DecisionStateStore(self.root/'decisions'), plan_store=self.store,
                work_catalog=WorkCatalog(self.root/'work'), series_catalog=SeriesCatalog(self.series_root),
                task_root=self.root/'tasks'),
            recovery_service=self.service(), _restart_recognition=Mock(),
            _recover_missing_catalog=Mock(), _recover_missing_materialization=Mock())
        dialog.snapshot = SimpleNamespace(opening_context=None)
        dialog._render_detach_transaction_recovery = Mock()
        before = business_bytes(self.root)
        from contextlib import nullcontext
        # Separate downstream component control: the real pending checker
        # itself rejects unsigned Plans before this branch. The first test
        # leaves it fully connected; this cut tests only the later consumer.
        pending = (patch.object(dialog.catalog_detach_service, 'pending_transaction',
                               side_effect=pending_error, return_value=pending_result)
                   if exercise_catalog_branch or pending_error is not None or pending_result is not None
                   else nullcontext())
        with patch.object(page, 'ui', ui), pending:
            page.PlanReviewDialog._render_catalog_context_content(dialog)
        if pending_error is not None:
            self.assertEqual([f'作品登记恢复记录需核查：{pending_error}'], labels)
            self.assertFalse(any('凭据' in str(text) for text in labels))
        elif pending_result is not None:
            dialog._render_detach_transaction_recovery.assert_called_once_with('series', SERIES)
            self.assertEqual([], labels)
        else:
            self.assertTrue(any('凭据' in str(text) for text in labels), labels)
        self.assertNotIn('从历史记录重建作品登记', buttons)
        self.assertNotIn('核查缺失内容并选择恢复方式', buttons)
        self.assertEqual(before, business_bytes(self.root))

    def test_actual_gui_pending_gate_shows_chinese_readonly_conflict(self):
        self.render_legacy_gui()

    def test_actual_gui_catalog_consumer_shows_block_without_rebuild_buttons(self):
        self.render_legacy_gui(exercise_catalog_branch=True)

    def test_gui_nonreceipt_error_retains_its_original_hint(self):
        self.render_legacy_gui(pending_error=ValueError('恢复事务记录损坏'))

    def test_gui_pending_snapshot_keeps_existing_reconciliation_route(self):
        self.render_legacy_gui(pending_result=SimpleNamespace(phase='prepared',
            catalog_backend='series', catalog_key=SERIES))


class CurrentAuthorityControls(unittest.IsolatedAsyncioTestCase):
    async def test_actual_canonical_series_execution_and_diagnostic_controls(self):
        fixture = activation.DefaultActivationProducerContracts()
        async with fixture.flow(movie=False, automatic=True) as flow:
            plan = flow.store.load(flow.task_id)
            self.assertIsNotNone(plan.projection_receipt)
            harness = execution.MediaFlow()
            task, ledger, _ = await harness._approve_and_execute(root=flow.root,
                result=SimpleNamespace(plan=plan, task=flow.manager._read(flow.task_id)))
            self.assertEqual('completed', task.get('execution_status'), task.get('error'))
            self.assertEqual('completed', ledger.get('status'))
            plan = flow.store.load(flow.task_id)
            state = flow.service.decision_state_store.load(flow.task_id)
            service = RecoveryService(catalog_root=flow.root/'work_catalog', record_root=flow.root/'records',
                                      series_catalog_root=flow.root/'series_catalog')
            before = business_bytes(flow.root)
            self.assertEqual('healthy', service.diagnose(state, plan).state)
            self.assertEqual(before, business_bytes(flow.root))
            from src.rename.catalog_binding import recovery_binding
            binding = recovery_binding(plan, state)
            self.assertEqual('series', binding.backend)
            record = flow.root/'series_catalog'/f'{binding.key}.json'
            original = record.read_bytes()
            try:
                record.write_text('{invalid-json', encoding='utf-8')
                damaged = business_bytes(flow.root)
                self.assertEqual('catalog_corrupt', service.diagnose(state, plan).state)
                self.assertEqual(damaged, business_bytes(flow.root))
            finally:
                record.write_bytes(original)
            target = plan.entries[0].target_path
            source = plan.entries[0].source_path
            target.unlink()
            target.write_bytes(b'foreign inode')
            damaged = business_bytes(flow.root)
            self.assertEqual('evidence_conflict', service.diagnose(state, plan).state)
            self.assertEqual(damaged, business_bytes(flow.root))
            target.unlink()
            os.link(source, target)
            self.assertEqual('healthy', service.diagnose(state, plan).state)
