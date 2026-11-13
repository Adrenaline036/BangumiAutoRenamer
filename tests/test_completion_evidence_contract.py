"""已批准换源的真实完成凭据与只读恢复校验。"""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from src.rename.execution_recovery import _validate_completed_hardlink_ledger
from src.rename.plan_store import PlanStore
from tests import test_asset_execution_recovery_contract as fixture
from tests import test_asset_actions_contract as actions


class CompletionEvidenceContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flow = fixture.AssetExecutionRecoveryContracts('runTest')
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)

    async def completed(self):
        make_version = actions.pair_fixture._make_version
        def incoming_version(root, *args, **kwargs):
            result = make_version(root, *args, **kwargs)
            if root.name == 'new-release':
                for subtitle in result[0].glob('*.ass'):
                    subtitle.write_bytes(subtitle.read_bytes() + b'\n; new companion revision')
            return result
        with patch.object(actions.pair_fixture, '_make_version', incoming_version):
            plan, service, transfer = await self.flow.approved()
        self.assertIsNone(transfer.trans_file())
        path = transfer.record_root / (plan.task_id + '.ledger.json')
        return plan, service, json.loads(path.read_bytes())

    def validate(self, plan, ledger):
        _validate_completed_hardlink_ledger(plan=plan, ledger=ledger,
            fingerprint=PlanStore.fingerprint(plan))

    async def test_current_media_and_companion_completion_reaches_recovery(self):
        plan, service, ledger = await self.completed()
        replaced = [row for row in ledger['entries'] if row['action'] == 'replace']
        self.assertEqual({'hardlink-identity-v1', 'content-xxh3-128-v1'},
                         {row['evidence_kind'] for row in replaced})
        paths = [service.plan_store.path_for(plan.task_id),
                 service.decision_state_store.path_for(plan.task_id),
                 service.task_root / (plan.task_id + '.json')]
        before = {p:p.read_bytes() for p in paths}
        self.validate(plan, ledger)
        self.assertEqual('catalog_pending', self.flow.recovery(service).inspect(
            plan.task_id, PlanStore.fingerprint(plan)).state)
        self.assertEqual(before, {p:p.read_bytes() for p in paths})

    async def test_missing_or_changed_typed_fields_are_rejected(self):
        plan, _service, ledger = await self.completed()
        kinds = {'hardlink-identity-v1', 'content-xxh3-128-v1'}
        for kind in sorted(kinds):
            index = next(i for i,r in enumerate(ledger['entries'])
                         if r['action'] == 'replace' and r['evidence_kind'] == kind)
            fields = ['evidence_kind', 'source_size', 'target_size',
                      'source_device', 'target_device', 'source_inode', 'target_inode']
            if kind == 'content-xxh3-128-v1':
                fields += ['source_xxh3_128', 'target_xxh3_128']
            for field in fields:
                for attack in ('missing', 'changed', 'wrong_type'):
                    with self.subTest(kind=kind, field=field, attack=attack):
                        forged = copy.deepcopy(ledger)
                        row = forged['entries'][index]
                        if attack == 'missing':
                            row.pop(field)
                        elif attack == 'wrong_type':
                            row[field] = True
                        else:
                            value = row[field]
                            row[field] = value + 1 if type(value) is int else 'forged'
                        with self.assertRaises(ValueError):
                            self.validate(plan, forged)
            for count in (None, 0, 1, True, '2'):
                with self.subTest(kind=kind, recorded_links=count):
                    forged = copy.deepcopy(ledger)
                    forged['entries'][index]['target_link_count'] = count
                    with self.assertRaises(ValueError):
                        self.validate(plan, forged)

    async def test_plan_and_journal_binding_remain_required(self):
        plan, _service, ledger = await self.completed()
        for field in ('task_id', 'plan_fingerprint'):
            with self.subTest(field=field):
                forged = copy.deepcopy(ledger)
                forged[field] = 'unrelated'
                with self.assertRaises(ValueError):
                    self.validate(plan, forged)
        index = next(i for i,r in enumerate(ledger['entries']) if r['action'] == 'replace')
        forged = copy.deepcopy(ledger)
        forged['entries'][index]['replacement_backup'] = str(Path('unrelated-backup'))
        with self.assertRaises(ValueError):
            self.validate(plan, forged)
