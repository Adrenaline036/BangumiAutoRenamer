"""提交消费同一冻结材料，并保留发布前漂移核对。"""
from unittest.mock import patch
import unittest
from src.rename import source_regrouping, source_regrouping_creation
from src.rename.plan_store import PlanStore
from tests import test_source_regrouping_preview_contract as sample
from tests.test_partition_content_contract import records


class RegroupingObservation(unittest.IsolatedAsyncioTestCase):
    flow = sample.SourceRegroupingPreviewContracts.flow
    current = sample.SourceRegroupingPreviewContracts.current

    async def test_disconnected_preview_producer_cannot_accept_missing_group(self):
        from dataclasses import replace
        import json
        async with self.flow(automatic=False, configure_source=sample.five_seasons) as flow:
            plan, _ = self.current(flow)
            preview = flow.manager.preview_source_regrouping(task_id=plan.task_id,
                expected_plan_fingerprint=PlanStore.fingerprint(plan))
            value = preview.to_dict()
            value['groups'].pop()
            forged = replace(preview, payload_json=json.dumps(value))
            before = records(flow.root)
            with patch.object(source_regrouping_creation, 'project_preview', return_value=forged, create=True):
                with self.assertRaises((ValueError, RuntimeError)):
                    flow.manager.create_regrouped_tasks(forged)
            self.assertEqual(before, records(flow.root))

    async def test_commit_observes_once_then_checks_drift_before_publication(self):
        async with self.flow(automatic=False, configure_source=sample.five_seasons) as flow:
            plan, _ = self.current(flow)
            preview = flow.manager.preview_source_regrouping(task_id=plan.task_id,
                expected_plan_fingerprint=PlanStore.fingerprint(plan))
            expected = {frozenset(str(p) for p in (flow.source / f'Season {season}').iterdir())
                        for season in range(1, 6)}
            seen = []
            actual = source_regrouping.observe_current
            def observe(*a, **kw):
                seen.append(a[1])
                return actual(*a, **kw)
            with patch.object(source_regrouping, 'observe_current', observe), patch.object(
                    source_regrouping_creation, 'observe_current', observe):
                ids = flow.manager.create_regrouped_tasks(preview)
            cards = {p.stem: flow.manager._read(p.stem) for p in flow.manager.task_root.glob('*.json')}
            self.assertEqual(5, len(cards))
            self.assertEqual(set(ids), cards.keys())
            self.assertNotIn(plan.task_id, cards)
            self.assertEqual(expected, {frozenset(row['source_path'] for row in
                card['task_source_scope']['source_identities']) for card in cards.values()})
            self.assertEqual(5, len(ids))
            self.assertEqual([plan.task_id, plan.task_id], seen)

    async def test_task_changes_during_preparation_cannot_publish(self):
        async with self.flow(automatic=False, configure_source=sample.five_seasons) as flow:
            plan, _ = self.current(flow)
            preview = flow.manager.preview_source_regrouping(task_id=plan.task_id,
                expected_plan_fingerprint=PlanStore.fingerprint(plan))
            real = source_regrouping_creation.policy_for_admission
            changed = []
            def prepare(*a, **kw):
                result = real(*a, **kw)
                if not changed:
                    flow.manager._update(plan.task_id, name='人工改名')
                    changed.append(records(flow.root))
                return result
            with patch.object(source_regrouping_creation, 'policy_for_admission', prepare):
                with self.assertRaisesRegex(ValueError, '准备期间原任务已变化'):
                    flow.manager.create_regrouped_tasks(preview)
            self.assertEqual(changed[0], records(flow.root))
