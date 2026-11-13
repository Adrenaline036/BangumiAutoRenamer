"""共享忽略标记只复用同内容文件，原批准、恢复和完成记录保持绑定。"""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch
from src.rename import generated_outputs as outputs
from src.rename.plan_store import PlanStore
from tests import test_initial_tv_completion_contract as tv


class SharedIgnore(unittest.IsolatedAsyncioTestCase):
    async def fixture(self):
        helper = tv.InitialTVCompletionContracts('runTest')
        self.addCleanup(helper.doCleanups)
        root, service, _, result, *_ = await helper.fixture(anime=True, font_archive=True)
        plan = result.plan
        row, = plan.metadata['generated_outputs']['files']
        self.assertEqual('jellyfin_ignore', row['kind'])
        self.assertIsNone(row['existing'])
        return root, plan, Path(row['target'])

    async def reuse(self, after_approval):
        root, plan, target = await self.fixture()
        fingerprint = PlanStore.fingerprint(plan)
        batch = outputs.approved_outputs(plan, fingerprint) if after_approval else None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'')
        before = outputs.inspect_target(plan.metadata['generated_outputs']['files'][0])
        batch = batch or outputs.approved_outputs(plan, fingerprint)
        with patch.object(outputs, 'inspect_target', wraps=outputs.inspect_target) as inspect:
            receipt = outputs.write_outputs(batch, task_id=plan.task_id, fingerprint=fingerprint, record_root=root/'shared-records')
        self.assertEqual(2, inspect.call_count, '复用沿用原预检和完成核验，不增加扫描')
        self.assertEqual('reused_identical', receipt['files'][0]['status'])
        self.assertEqual(before, receipt['files'][0]['witness'])
        self.assertEqual([], receipt['directory_witnesses'])
        outputs.validate_completed(plan, {'generated_outputs': receipt})
        resumed = outputs.resume_outputs(plan, root/'shared-records')
        self.assertEqual(receipt, outputs.write_outputs(resumed, task_id=plan.task_id, fingerprint=fingerprint, record_root=root/'shared-records'))
        payload = dict(canonical_root=plan.metadata['canonical_work_root'])
        self.assertTrue(outputs.register_outputs(payload, plan, {'generated_outputs': receipt}))
        self.assertEqual(fingerprint, PlanStore.fingerprint(plan))
        for field, value in [('task_id','other'), ('plan_fingerprint','0'*64), ('manifest_fingerprint','0'*64)]:
            bad = copy.deepcopy(receipt); bad[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                outputs.validate_completed(plan, {'generated_outputs': bad})
        bad = copy.deepcopy(receipt); bad['files'][0]['witness'][1] = -1
        with self.assertRaises(ValueError):
            outputs.validate_completed(plan, {'generated_outputs': bad})

    async def test_identical_marker_created_after_plan_freeze_is_reused(self):
        await self.reuse(False)

    async def test_identical_marker_created_after_approval_is_reused(self):
        await self.reuse(True)

    async def test_nonempty_marker_and_changed_frozen_identity_are_rejected(self):
        root, plan, target = await self.fixture()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'not empty')
        with self.assertRaises(ValueError):
            outputs.approved_outputs(plan, PlanStore.fingerprint(plan))
        target.write_bytes(b'')
        frozen = outputs.seal_outputs(plan)
        target.rename(target.with_name('retained-original'))
        target.write_bytes(b'')
        with self.assertRaisesRegex(ValueError, '状态已变化'):
            outputs.approved_outputs(frozen, PlanStore.fingerprint(frozen))
        with self.assertRaises(ValueError):
            outputs.approved_outputs(plan, '0'*64)

    async def test_path_link_is_rejected_before_target_read(self):
        root, plan, target = await self.fixture()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'')
        real = Path.lstat
        class Link:
            st_mode = 0o120777
            st_file_attributes = 0x400
        with patch.object(Path, 'lstat', lambda p,*a,**k: Link() if p==target else real(p,*a,**k)):
            with self.assertRaisesRegex(ValueError, '链接或重解析点'):
                outputs.approved_outputs(plan, PlanStore.fingerprint(plan))
