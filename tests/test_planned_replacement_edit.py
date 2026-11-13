"""Reported order: a frozen replacement is edited, then explicitly reconfirmed."""
from collections import Counter
from unittest.mock import patch
import unittest

from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from src.rename.plan_verifier import PlanVerifier
from tests import test_special_selection_contract as sp
from tests import test_cross_provider_work_correction as cross
from tests import test_content_workbench_gui as workbench


class PlannedReplacementEdit(unittest.IsolatedAsyncioTestCase):
    fixture = sp.SpecialSelectionContracts.fixture
    describe = sp.SpecialSelectionContracts.describe
    revise = sp.SpecialSelectionContracts.revise
    observe = cross.CrossProviderWorkCorrection.observe
    confirm = cross.CrossProviderWorkCorrection.confirm
    create_page = workbench.ContentWorkbenchGui.create_page

    async def replacement(self):
        root, service, _, result, *_ = await cross.CrossProviderWorkCorrection.original(self, tmdb=True, companions=False)
        video = next(e for e in result.plan.entries if e.media_kind == 'video')
        plan = self.describe(service, result.plan, entry_id(video), '保留特别篇')
        selected = next(e for e in plan.entries if e.source_path == video.source_path)
        selected.target_path.parent.mkdir(parents=True, exist_ok=True)
        selected.target_path.write_bytes(b'previous special')
        operation, _ = service.asset_actions().prepare_replacement(plan)
        plan = service.confirm_asset_replacement(plan.task_id, PlanStore.fingerprint(plan),
            expected_operation_fingerprint=operation.fingerprint)
        return root, service, plan, selected

    async def test_cancel_after_replacement_revokes_old_scope_and_requires_new_confirmation(self):
        root, service, plan, video = await self.replacement()
        fingerprint = PlanStore.fingerprint(plan)
        old = plan.metadata['asset_operation']
        service.approve_entries(plan.task_id, fingerprint, [entry_id(e) for e in plan.entries])
        source = video.source_path.read_bytes()
        cleared = self.revise(service, plan, 'role', {'entry_id': entry_id(video), 'role': 'main_candidate'})
        state = service.decision_state_store.load(plan.task_id)
        self.assertFalse(state.provider_special_decisions)
        self.assertEqual('main_candidate', state.manual_role_decisions[entry_id(video)])
        self.assertEqual(1, next(e for e in cleared.entries if e.source_path == video.source_path).season_id)
        self.assertFalse(service.load_or_create(cleared).approved_entry_ids)
        self.assertNotEqual(fingerprint, PlanStore.fingerprint(cleared))
        operation = cleared.metadata.get('asset_operation', {})
        self.assertNotEqual('replace', operation.get('mode'))
        for key in ('replaces', 'retirements', 'season_directory'):
            self.assertFalse(operation.get(key))
        self.assertNotIn('asset_replacement_retired', cleared.metadata)
        self.assertNotIn('asset_replacement_retained', cleared.metadata)
        with self.assertRaises(Exception):
            service.approve_entries(plan.task_id, fingerprint, [entry_id(e) for e in cleared.entries])
        renewed = self.describe(service, cleared, entry_id(video), '保留特别篇')
        self.assertIn('asset_collision_unresolved', {i.code for i in PlanVerifier().verify(renewed).issues})
        with self.assertRaises(Exception):
            service.confirm_asset_replacement(plan.task_id, fingerprint,
                expected_operation_fingerprint=__import__('src.rename.asset_operation', fromlist=['AssetOperation']).AssetOperation.from_mapping(old).fingerprint)
        fresh, _ = service.asset_actions().prepare_replacement(renewed)
        final = service.confirm_asset_replacement(plan.task_id, PlanStore.fingerprint(renewed),
            expected_operation_fingerprint=fresh.fingerprint)
        self.assertEqual('replace', final.metadata['asset_operation']['mode'])
        self.assertFalse(service.load_or_create(final).approved_entry_ids)
        self.assertEqual(source, video.source_path.read_bytes())
        self.assertEqual(b'previous special', video.target_path.read_bytes())

    async def test_work_correction_after_replacement_uses_same_admission(self):
        root, service, plan, video = await self.replacement()
        changed = self.confirm(service, plan, {**cross.TMDB, 'provider_id': '41658'}, 2)
        self.assertEqual('41658', changed.work_identity.primary_provider.provider_id)
        self.assertNotEqual('replace', changed.metadata.get('asset_operation', {}).get('mode'))
        self.assertEqual(b'previous special', video.target_path.read_bytes())

    async def test_opening_same_frozen_operation_observes_it_once_per_display(self):
        from src.rename.asset_operation import AssetOperation
        from src.pages.plan_review_page import build_plan_review_snapshot
        root, service, plan, _ = await self.replacement()
        payload = AssetOperation.encode(plan.metadata['asset_operation'])
        original = AssetOperation.__post_init__
        observed = Counter()
        def count(value):
            observed[value.payload] += 1
            return original(value)
        with patch.object(AssetOperation, '__post_init__', count):
            build_plan_review_snapshot(plan)
        self.assertEqual(1, observed[payload], 'same replacement repeatedly probes paths inside one read-only snapshot')
        observed.clear()
        with patch.object(AssetOperation, '__post_init__', count):
            build_plan_review_snapshot(plan)
        self.assertEqual(1, observed[payload], 'each next display must start with a fresh observation')
        observed.clear()
        with patch.object(AssetOperation, '__post_init__', count):
            AssetOperation.from_mapping(plan.metadata['asset_operation'])
            AssetOperation.from_mapping(plan.metadata['asset_operation'])
        self.assertEqual(2, observed[payload], 'non-display readers must never reuse a previous observation')

    async def test_started_task_remains_uneditable(self):
        import json
        root, service, plan, video = await self.replacement()
        path = service.task_root / (plan.task_id + '.json')
        value = json.loads(path.read_text('utf-8'))
        value['execution_status'] = 'completed'
        path.write_text(json.dumps(value), encoding='utf-8')
        before = service.plan_store.path_for(plan.task_id).read_bytes()
        with self.assertRaises(Exception):
            self.revise(service, plan, 'role', {'entry_id': entry_id(video), 'role': 'main_candidate'})
        self.assertEqual(before, service.plan_store.path_for(plan.task_id).read_bytes())

    async def test_background_open_keeps_loop_available_and_discards_stale_result(self):
        import asyncio
        import threading
        from dataclasses import replace
        from src.pages import plan_review_page as page
        root, service, plan, _ = await self.replacement()
        self.create_page(prepared=(root, service, plan, []))
        entered, release = threading.Event(), threading.Event()
        self.view.delete()
        threads = []
        def blocked(_plan):
            threads.append(threading.get_ident())
            entered.set()
            if not release.wait(5):
                raise TimeoutError('snapshot must not block the event loop')
            return object()
        changed = replace(plan, revision_reason='newer plan while opening')
        with patch.object(page, 'prepare_plan_review_open', side_effect=blocked) as prepare, \
                patch.object(page.PlanStore, 'load', side_effect=[plan, changed]), \
                patch.object(page.PlanReviewDialog, '_load_plan_view') as hydrate:
            async def open_inside_client():
                with self.client:
                    await page.plan_review_page(plan.task_id)
            opening = asyncio.create_task(open_inside_client())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                with self.client:
                    await page.plan_review_page(plan.task_id)  # duplicate while preparing
                self.assertEqual(1, prepare.call_count)
                self.assertNotEqual(threading.get_ident(), threads[0])
                release.set()
                await asyncio.wait_for(opening, 10)
            finally:
                release.set()
                if not opening.done():
                    opening.cancel()
                    await asyncio.gather(opening, return_exceptions=True)
            hydrate.assert_not_called()
        self.assertTrue(any('计划已更新' in text for text in self.notices))
        self.assertFalse(self.client._bar_review_openings)

    async def test_prepared_open_does_not_repeat_state_or_recovery_reads_during_render(self):
        import asyncio
        from src.pages import plan_review_page as page
        root, service, plan, _ = await self.replacement()
        self.create_page(prepared=(root, service, plan, []))
        snapshot = await asyncio.to_thread(page.prepare_plan_review_open, plan)
        self.assertIsNotNone(snapshot.opening_context[0])
        with patch.object(page.DecisionStateStore, 'load_optional',
                          side_effect=AssertionError('state read on UI thread')), \
                patch.object(page.CatalogDetachService, 'pending_transaction',
                             side_effect=AssertionError('recovery read on UI thread')):
            with self.client:
                dialog = page.PlanReviewDialog(plan, self.view.task_data, snapshot=snapshot)
                self.assertEqual(snapshot.opening_context[0], dialog.task_decision)
                self.assertIsNone(dialog.snapshot.opening_context)
                dialog.delete()


class DisplayReadScope(unittest.TestCase):
    def test_scope_is_per_payload_thread_and_call_and_unwinds_on_error(self):
        from concurrent.futures import ThreadPoolExecutor
        from src.rename.asset_operation import AssetOperation, reuse_display_asset_reads
        row = dict(policy='asset-operation-v1', mode='append', slot_key='a',
                   replaces=[], font_deltas=[], previous_assets=[])
        other = {**row, 'slot_key': 'b'}
        original = AssetOperation.__post_init__
        seen = Counter()
        def count(value):
            seen[value.payload] += 1
            return original(value)
        @reuse_display_asset_reads
        def nested():
            return AssetOperation.from_mapping(row)
        @reuse_display_asset_reads
        def snapshot(fail=False):
            self.assertIs(AssetOperation.from_mapping(row), AssetOperation.from_mapping(row))
            self.assertIs(AssetOperation.from_mapping(row), nested())
            AssetOperation.from_mapping(other)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(AssetOperation.from_mapping, row).result()
                pool.submit(AssetOperation.from_mapping, row).result()
            if fail:
                raise RuntimeError('display interrupted')
        with patch.object(AssetOperation, '__post_init__', count):
            snapshot()
            self.assertEqual(3, seen[AssetOperation.encode(row)])
            self.assertEqual(1, seen[AssetOperation.encode(other)])
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                snapshot(fail=True)
            AssetOperation.from_mapping(row)
            AssetOperation.from_mapping(row)
            self.assertEqual(8, seen[AssetOperation.encode(row)])
            self.assertEqual(2, seen[AssetOperation.encode(other)])
        async def unsupported():
            pass
        with self.assertRaises(TypeError):
            reuse_display_asset_reads(unsupported)


if __name__ == '__main__':
    unittest.main()
