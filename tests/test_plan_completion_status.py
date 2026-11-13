"""同计划媒体完成、待核查及过期执行记录的显示边界。"""
import unittest
from unittest.mock import patch
from src.pages.plan_completion_status import completion_status, entry_status


class CompletionStatus(unittest.TestCase):
    def test_media_and_fingerprint_states(self):
        plan = object()
        base = dict(plan_fingerprint='current', execution_fingerprint='current',
            execution_status='completed', workflow_status='completed', execution_phase='completed')
        with patch('src.pages.plan_completion_status.PlanStore.fingerprint', return_value='current'):
            self.assertEqual('整理完成', completion_status(plan, base))
            self.assertEqual('已整理', entry_status(plan, base))
            self.assertEqual('', completion_status(plan, {**base, 'execution_fingerprint': 'old'}))
            self.assertEqual('', completion_status(plan, {'workflow_status': 'need_confirm'}))
            self.assertEqual('整理结果待核查', completion_status(plan, {**base,
                'execution_status': 'failed', 'execution_may_have_written_targets': True}))
