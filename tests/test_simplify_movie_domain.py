"""Movie routing drift through real temporary producer/Catalog contracts."""
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id, PlanReviewError
from src.rename.work_catalog import WorkCatalogError
from tests import test_asset_actions_contract as fixture


class MovieDomainContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.helper = fixture.AssetActionContracts('runTest')
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.old = await self.helper.fixture.single(with_font=False)
        self.root = self.helper.root
        self.execution = self.helper.fixture.execution_service()
        self.review = self.execution.review_service
        self.catalog = self.execution.work_catalog
        self.key = self.old.plan.metadata.get('catalog_work_key') or self.old.plan.metadata['proposed_work_key']

    def drift(self, **changes):
        value = self.catalog.load(self.key)
        value.update(changes)
        self.catalog._path(self.key).write_text(json.dumps(value), encoding='utf-8')

    def business_bytes(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*.json')
                if not any(part.startswith('.') for part in p.relative_to(self.root).parts)}

    async def test_same_provider_and_root_does_not_override_movie_domain(self):
        record = self.catalog.load(self.key)
        self.assertTrue(self.catalog.compatible_base(record, self.old.plan))
        for domain in ('general', 'unknown'):
            with self.subTest(domain=domain):
                changed = replace(self.old.plan, work_identity=replace(self.old.plan.work_identity, domain=domain))
                self.assertFalse(self.catalog.compatible_base(record, changed))

    async def test_append_catalog_domain_drift_is_rejected_before_approval(self):
        plan = await self.helper.incoming('2160p')
        self.drift(domain='general')
        before = self.business_bytes()
        with self.assertRaises((ValueError, RuntimeError)):
            self.review.approve_entries(plan.task_id, PlanStore.fingerprint(plan), [entry_id(e) for e in plan.entries])
        self.assertEqual(before, self.business_bytes())

    async def test_append_catalog_root_drift_is_rejected_before_approval(self):
        plan = await self.helper.incoming('2160p')
        self.drift(canonical_root=str(self.root / 'foreign' / 'Example'))
        before = self.business_bytes()
        with self.assertRaises((ValueError, RuntimeError)):
            self.review.approve_entries(plan.task_id, PlanStore.fingerprint(plan), [entry_id(e) for e in plan.entries])
        self.assertEqual(before, self.business_bytes())

    async def test_replace_rejects_domain_drift_without_modifying_history(self):
        plan = await self.helper.incoming()
        self.drift(domain='general')
        before = self.business_bytes()
        with self.assertRaises((ValueError, RuntimeError)):
            self.review.asset_actions().prepare_replacement(plan)
        self.assertEqual(before, self.business_bytes())
        # Read-only Catalog history remains readable, without a migration write.
        self.assertEqual('general', self.catalog.load(self.key)['domain'])

    async def test_explicit_link_cannot_override_domain(self):
        plan = await self.helper.incoming('2160p')
        self.drift(domain='general')
        before = self.business_bytes()
        with self.assertRaisesRegex(PlanReviewError, '媒体库'):
            self.review.link_existing_catalog_work(plan.task_id, PlanStore.fingerprint(plan),
                work_key=self.key, reason='same provider is not domain permission')
        self.assertEqual(before, self.business_bytes())

    async def test_rebase_cannot_override_domain(self):
        plan = await self.helper.incoming('2160p')
        self.drift(domain='general')
        before = self.business_bytes()
        with self.assertRaisesRegex(PlanReviewError, '媒体库'):
            self.review.rebase_movie_plan_to_current_catalog(plan.task_id, PlanStore.fingerprint(plan), reason='rebase')
        self.assertEqual(before, self.business_bytes())

    async def test_inventory_completion_reports_domain_before_new_plan(self):
        plan = await self.helper.incoming('2160p')
        self.drift(domain='general')
        before = self.business_bytes()
        selection = self.review.decision_state_store.load(plan.task_id).movie_version_selection.to_dict()
        with self.assertRaisesRegex(ValueError, '电影版本编辑的作品登记身份不一致'):
            self.review.revise_movie_versions(plan.task_id, PlanStore.fingerprint(plan),
                groups=[dict(sources=g['sources'], label=g['label']) for g in selection['groups']],
                excluded_sources=selection['excluded_sources'])
        self.assertEqual(before, self.business_bytes())

    async def test_shared_domain_gate_disconnect_cannot_authorize_append_or_execution(self):
        plan = await self.helper.incoming('2160p')
        fingerprint = PlanStore.fingerprint(plan)
        ids = [entry_id(e) for e in plan.entries]
        self.review.approve_entries(plan.task_id, fingerprint, ids)
        before = self.business_bytes()
        with patch('src.rename.work_catalog.require_matching_work_domain',
                   side_effect=WorkCatalogError('共享媒体库裁决已断开')):
            with self.assertRaises((ValueError, RuntimeError)):
                self.review.approve_entries(plan.task_id, fingerprint, ids)
            with self.assertRaises((ValueError, RuntimeError)):
                self.execution.validate_approved(plan.task_id, fingerprint)
        self.assertEqual(before, self.business_bytes())

    async def test_normal_same_domain_append_can_still_be_approved(self):
        plan = await self.helper.incoming('2160p')
        self.review.approve_entries(plan.task_id, PlanStore.fingerprint(plan), [entry_id(e) for e in plan.entries])
        self.execution.validate_approved(plan.task_id, PlanStore.fingerprint(plan))


if __name__ == '__main__': unittest.main()
