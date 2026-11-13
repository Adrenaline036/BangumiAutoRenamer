"""实际审核工具栏、版本编辑窗和保存回调。"""
import unittest
from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from tests import test_content_workbench_gui as workbench
from tests import test_episode_versions_contract as versions


class EpisodeVersionsUI(unittest.IsolatedAsyncioTestCase):
    async def test_stale_dialog_does_not_report_success_or_adopt_new_fingerprint(self):
        fixture = versions.EpisodeVersionsContracts('runTest')
        async with fixture.flow(automatic=False, configure_source=versions.package) as flow:
            plan, _ = await fixture.selected(flow)
            holder = workbench.ContentWorkbenchGui('runTest')
            holder.create_page(prepared=(flow.root, flow.service, plan, []), cleanup_owner=self)
            with holder.client:
                await holder.driver.click('设置剧集版本')
                editor = holder.view._episode_version_editor
                before = editor.revision, editor.fingerprint
                for row in fixture.rows(plan):
                    editor.batch_files.set_value([row['entry_id']])
                    editor.batch_label.set_value(row['label'])
                    editor.apply_batch()
                    editor.fields[row['entry_id']]['episode'].set_value('1')
                updated = fixture.save(flow, plan)
                await holder.view._adopt_revision(updated)
                first = fixture.rows(updated)[0]['entry_id']
                editor.fields[first]['label'].set_value('尚未保存的新标签')
                await holder.driver.click('保存剧集版本')
                self.assertTrue(editor.feedback.text.startswith('未保存：'), editor.feedback.text)
                self.assertEqual(before, (editor.revision, editor.fingerprint))
                self.assertEqual('尚未保存的新标签', editor.fields[first]['label'].value)
                self.assertEqual(PlanStore.fingerprint(updated), PlanStore.fingerprint(flow.store.load(plan.task_id)))

    async def test_editor_batch_label_save_reload_and_invalid_keeps_plan(self):
        fixture = versions.EpisodeVersionsContracts('runTest')
        async with fixture.flow(automatic=False, configure_source=versions.package) as flow:
            plan, _ = await fixture.selected(flow)
            holder = workbench.ContentWorkbenchGui('runTest')
            holder.create_page(prepared=(flow.root, flow.service, plan, []), cleanup_owner=self)
            with holder.client:
                self.assertTrue(any(getattr(e, 'text', '') == '设置剧集版本' for e in holder.client.elements.values()))
                await holder.driver.click('设置剧集版本')
                editor = holder.view._episode_version_editor
                for row in fixture.rows(plan):
                    editor.batch_files.set_value([row['entry_id']])
                    editor.batch_label.set_value(row['label'])
                    editor.apply_batch()
                    editor.fields[row['entry_id']]['episode'].set_value('1')
                await holder.driver.click('保存剧集版本')
                self.assertEqual('剧集版本已保存', editor.feedback.text)
                saved = flow.store.load(plan.task_id)
                self.assertEqual(2, len(saved.metadata['episode_versions']))
                first = fixture.rows(saved)[0]['entry_id']
                editor.fields[first]['episode'].set_value('1.5')
                await holder.driver.click('保存剧集版本')
                self.assertTrue(editor.feedback.text.startswith('未保存：'))
                self.assertEqual(PlanStore.fingerprint(saved), PlanStore.fingerprint(flow.store.load(plan.task_id)))
                editor.dialog.close()
                await holder.driver.click('设置剧集版本')
                reopened = holder.view._episode_version_editor
                self.assertEqual({'4x3', '16x9'}, {f['label'].value for f in reopened.fields.values() if f['selected'].value})
