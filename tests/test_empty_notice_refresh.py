import unittest
from unittest.mock import patch
from nicegui import ui, Client
from src.pages import plan_review_page as page
from src.pages.content_review_view import open_content_workbench
from tests import test_source_content_recovery as fixture
from tests.test_content_choice_contract import change
from src.rename.plan_store import PlanStore

class EmptyNoticeRefresh(unittest.IsolatedAsyncioTestCase):
    flow=fixture.SourceContentRecovery.flow
    current=fixture.SourceContentRecovery.current

    def notices(self, owner):
        return [e for e in owner.plan_region.descendants() if getattr(e,'text','')=='当前没有纳入的文件']

    async def test_restore_then_exclude_refreshes_same_open_dialog(self):
        async with self.flow(automatic=False) as flow:
            with Client.auto_index_client:
                plan,state=self.current(flow)
                plan=change(flow.service,plan,[e.source_path for e in plan.entries])
                self.assertFalse(plan.entries)
                view=page.PlanReviewDialog(plan,flow.manager._read(plan.task_id));view.open()
                self.addCleanup(view.delete)
                self.assertEqual(1,len(self.notices(view)))
                open_content_workbench(view,ui=ui,notify=lambda *a,**k:None)
                work=view._content_workbench
                self.addCleanup(work.close)
                work.selected={row['identifier'] for row in work.column_rows('restore') if not row['locked']}
                await work.apply('restore')
                self.assertTrue(view.plan.entries,work.message.text)
                self.assertEqual(PlanStore.fingerprint(flow.store.load(plan.task_id)),PlanStore.fingerprint(view.plan))
                self.assertEqual([],self.notices(view))
                self.assertTrue(view.value)
                work.selected={row['identifier'] for row in work.column_rows('exclude') if not row['locked']}
                await work.apply('exclude')
                self.assertFalse(view.plan.entries,work.message.text)
                self.assertEqual(1,len(self.notices(view)))
                card=self.notices(view)[0].parent_slot.parent.parent_slot.parent
                self.assertIn('bar-issue-summary',card._classes)
                self.assertTrue(card.visible)
                self.assertTrue(any('还差一步' in getattr(e,'text','') for e in view.issues_container.descendants()))
                self.assertTrue(view.value)

    async def test_failed_restore_keeps_notice_and_revision(self):
        async with self.flow(automatic=False) as flow:
            with Client.auto_index_client:
                plan,state=self.current(flow)
                plan=change(flow.service,plan,[e.source_path for e in plan.entries])
                view=page.PlanReviewDialog(plan,flow.manager._read(plan.task_id));view.open();self.addCleanup(view.delete)
                open_content_workbench(view,ui=ui,notify=lambda *a,**k:None)
                work=view._content_workbench;self.addCleanup(work.close)
                work.selected={row['identifier'] for row in work.column_rows('restore') if not row['locked']}
                before=PlanStore.fingerprint(view.plan)
                with patch.object(work.service,'prepare_content_change',side_effect=ValueError('隔离失败控制')):
                    await work.apply('restore')
                self.assertEqual(before,PlanStore.fingerprint(view.plan))
                self.assertEqual(1,len(self.notices(view)))
                self.assertIn('隔离失败控制',work.message.text)
