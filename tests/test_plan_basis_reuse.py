"""Pure frozen-Plan parse reuse; never reuse observations of live files."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from src.rename.source_basis import InitialSourceBasis
from src.rename.rename_plan import RenamePlan
from src.rename.plan_store import PlanStore
from src.rename.projection_authority import validate_plan_projection_receipt


class PlanBasisReuse(unittest.IsolatedAsyncioTestCase):
    async def plan(self):
        from tests.test_default_content_initialization import DefaultInitializationContracts
        helper = DefaultInitializationContracts('runTest')
        self.addCleanup(helper.doCleanups)
        _, plan, *_ = await helper.ready(helper.root())
        return replace(plan)  # A new immutable object, with no parsed properties.

    async def test_receipt_consumers_parse_one_frozen_basis_only_once(self):
        plan = await self.plan()
        before = plan.to_dict(), PlanStore.fingerprint(plan)
        with patch.object(InitialSourceBasis, 'from_mapping', wraps=InitialSourceBasis.from_mapping) as parse:
            validate_plan_projection_receipt(plan)
            validate_plan_projection_receipt(plan)
        self.assertEqual(1, parse.call_count, 'same immutable Plan repeatedly parses source basis')
        self.assertEqual(before, (plan.to_dict(), PlanStore.fingerprint(plan)))

    async def test_replace_and_reload_cannot_inherit_cached_validation(self):
        plan = await self.plan()
        self.assertTrue(hasattr(RenamePlan, 'initial_source_basis'), 'missing Plan-owned parsed basis')
        basis = plan.initial_source_basis
        value = plan.to_dict()
        value['metadata']['initial_source_basis']['sources'] = []
        for bad in (replace(plan, metadata=value['metadata']), RenamePlan.from_dict(value)):
            with self.subTest(reload=bad is not plan), self.assertRaises(ValueError):
                _ = bad.initial_source_basis
        self.assertEqual(basis, plan.initial_source_basis)
        copy = RenamePlan.from_dict(plan.to_dict())
        with patch.object(InitialSourceBasis, 'from_mapping', wraps=InitialSourceBasis.from_mapping) as parse:
            self.assertEqual(basis, copy.initial_source_basis)
            self.assertEqual(1, parse.call_count, 'a newly loaded Plan must validate afresh')

    async def test_metadata_is_deep_frozen_and_missing_still_rejected(self):
        plan = await self.plan()
        self.assertTrue(hasattr(RenamePlan, 'initial_source_basis'), 'missing Plan-owned parsed basis')
        raw = plan.to_dict()['metadata']
        copy = replace(plan, metadata=raw)
        expected = copy.initial_source_basis
        raw['initial_source_basis']['sources'].clear()
        self.assertEqual(expected, copy.initial_source_basis)
        with self.assertRaises(TypeError):
            copy.metadata['initial_source_basis']['sources'] = ()
        missing = dict(copy.metadata)
        missing.pop('initial_source_basis')
        with self.assertRaises(ValueError):
            _ = replace(copy, metadata=missing).initial_source_basis
