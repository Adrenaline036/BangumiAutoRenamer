"""User font/omission choices through current public content transactions."""
import hashlib
import unittest

from src.rename.content_associations import source_key
from src.rename.plan_review import PlanReviewError, entry_id
from src.rename.plan_store import PlanStore
from tests import test_default_activation_contract as activation
from tests.test_content_choice_contract import records




def preview(flow, plan, source, action):
    identifier = hashlib.sha256(source_key(source).encode('utf-8')).hexdigest()[:20]
    return flow.service.prepare_content_change(flow.task_id, PlanStore.fingerprint(plan),
        selected_entry_ids=[identifier], action=action, reason='人工更正保留范围')


class ManualContentChoices(unittest.IsolatedAsyncioTestCase):






    async def test_automatic_omission_is_a_default_and_user_can_restore_it(self):
        async with activation.DefaultActivationProducerContracts().flow() as flow:
            plan = flow.store.load(flow.task_id)
            source = next(flow.source.glob('SPs/*CM01*.mkv'))
            self.assertIn(source, plan.ignored_sources)
            original = source.read_bytes()
            plan = flow.service.commit_content_change(preview(flow, plan, source, 'restore'))
            self.assertNotIn(source, plan.ignored_sources)
            self.assertIn(source, {e.source_path for e in plan.entries})
            plan = flow.service.commit_content_change(preview(flow, plan, source, 'exclude'))
            self.assertIn(source, plan.ignored_sources)
            self.assertEqual(original, source.read_bytes())


