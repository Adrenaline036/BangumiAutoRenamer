"""连续集数文本输入、步进和原保存入口的定向核验。"""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from src.pages.episode_range_editor import EpisodeRangeEditor, open_episode_range_editor
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from tests.test_content_workbench_gui import ContentWorkbenchGui
from tests.test_episode_range_contract import EpisodeRangeContracts, merged_package


class EpisodeRangeInput(unittest.IsolatedAsyncioTestCase):
    def test_integer_text_and_step_boundaries(self):
        editor = EpisodeRangeEditor.__new__(EpisodeRangeEditor)
        editor.feedback = Mock()
        editor.season = SimpleNamespace(value='0')
        editor.start = SimpleNamespace(value='12')
        editor.end = SimpleNamespace(value='123')
        self.assertEqual((0, 12, 123), editor.values())
        for values in (('', '1', '12'), ('1', '1.5', '12'), ('1', '1e2', '123'),
                       ('-1', '1', '12'), ('1', '-1', '12'), ('1', '12', '11'),
                       ('1', '1', '100000'), ('1', 'word', '12')):
            for field, value in zip((editor.season, editor.start, editor.end), values):
                field.value = value
            with self.subTest(values=values), self.assertRaises(ValueError):
                editor.values()
        field = SimpleNamespace(value='12')
        field.set_value = lambda value: setattr(field, 'value', value)
        editor.step(field, 1, 1)
        self.assertEqual('13', field.value)
        editor.step(field, -1, 1)
        self.assertEqual('12', field.value)
        for value, minimum in (('0', 0), ('1', 1), ('', 1)):
            field.value = value
            editor.step(field, -1, minimum)
            self.assertEqual(str(minimum), field.value)
        field.value = '12.5'
        editor.step(field, 1, 1)
        self.assertEqual('12.5', field.value, '无效输入不能被步进按钮悄悄替换')

    async def test_text_save_reload_and_invalid_input_keeps_plan(self):
        fixture = EpisodeRangeContracts('runTest')
        async with fixture.flow(automatic=False, configure_source=merged_package) as flow:
            plan, _ = await fixture.selected(flow)
            holder = ContentWorkbenchGui('runTest')
            holder.create_page(prepared=(flow.root, flow.service, plan, []), cleanup_owner=self)
            with holder.client:
                open_episode_range_editor(holder.view)
                editor = holder.view._episode_range_editor
                video = next(e for e in holder.view.plan.entries if e.media_kind == 'video' and '[03-05]' in e.source_path.name)
                editor.video.set_value(entry_id(video))
                for field, value in zip((editor.season, editor.start, editor.end), ('1', '12', '123')):
                    field.set_value(value)
                await editor.save()
                self.assertEqual('连续集数已保存，可继续调整', editor.feedback.text)
                self.assertTrue(editor.dialog.value)
                saved = flow.store.load(plan.task_id)
                revised = next(e for e in saved.entries if e.source_path == video.source_path)
                self.assertEqual((1, 12, 123), (revised.season_id, revised.episode_id, revised.episode_end))
                editor.selected()
                self.assertEqual(('1', '12', '123'), (editor.season.value, editor.start.value, editor.end.value))
                before = PlanStore.fingerprint(saved)
                for field, value in ((editor.start, ''), (editor.start, '1.5'), (editor.end, '11')):
                    editor.selected()
                    field.set_value(value)
                    await editor.save()
                    self.assertTrue(editor.feedback.text.startswith('未保存：'))
                    self.assertEqual(before, PlanStore.fingerprint(flow.store.load(plan.task_id)))
