"""默认排除与审核窗口连续操作的隔离定向验证。"""
from pathlib import Path
import unittest
from unittest.mock import patch
from src.rename.media_classifier import observe_watchable_content
from src.rename.plan_store import PlanStore
from tests import test_special_crosswalk_gui as special_gui
from tests.test_content_choice_contract import records


class DefaultNames(unittest.TestCase):
    def test_release_labels_and_manual_protection(self):
        root = Path('D:/isolated-name-only')
        tags = ('Promotion03', 'Review01-12', 'NCED01', 'NCED02_EP06',
                'NCOP', 'Preview09-12', 'Promotion01', 'Promotion02')
        for tag in tags:
            with self.subTest(tag=tag):
                path = root/'SPs'/f'[Group] Example Ver1.1a [{tag}][Ma10p_1080p][x265_flac].mkv'
                self.assertEqual('known_non_watchable', observe_watchable_content(path, source_root=root).disposition)
                self.assertEqual('keep_candidate', observe_watchable_content(path, source_root=root,
                    protected_role='provider_special').disposition)
        for name in ('Review Story 01', 'Promotion Story 01', 'Show [OVA]', 'Show [OAD]',
                     'Show [SP01]', 'Show [Picture Drama]', 'Show S01E01 [Preview09-12]'):
            with self.subTest(name=name):
                self.assertNotEqual('known_non_watchable', observe_watchable_content(
                    root/'SPs'/(name+'.mkv'), source_root=root).disposition)
        self.assertEqual('known_non_watchable', observe_watchable_content(root/'Show [PV03].mkv',source_root=root).disposition)


class ReviewInteraction(unittest.IsolatedAsyncioTestCase):
    fixture = special_gui.SpecialCrosswalkGui.fixture
    create_page = special_gui.SpecialCrosswalkGui.create_page
    prepared = special_gui.SpecialCrosswalkGui.prepared

    async def test_existing_task_suggestions_require_manual_confirmation(self):
        from dataclasses import replace
        from src.rename import media_classifier
        original = media_classifier.observe_watchable_content
        def old_rule(path, **kwargs):
            value = original(path, **kwargs)
            return replace(value, disposition='needs_confirmation') if '[Promotion03]' in str(path) else value
        with patch.object(media_classifier, 'observe_watchable_content', old_rule):
            root, service, _, result, extras, *_ = await self.fixture(anime=False, exceptional=True,
                exceptional_name='Show [Promotion03].mkv')
        self.create_page(prepared=(root, service, result.plan, []))
        before = records(root)
        await self.driver.click('内容保留与排除')
        workbench = self.view._content_workbench
        original_selection = workbench.choice.to_dict()
        await self.driver.click('选择建议排除项')
        self.assertEqual(before, records(root))
        self.assertEqual(original_selection, workbench.choice.to_dict())
        row = next(r for r in workbench.rows if r['source'] == extras['exceptional'])
        self.assertIn(row['identifier'], workbench.selected)
        self.assertTrue(row['included'])
        await self.driver.click_element(workbench.close_button)
        self.assertEqual(before, records(root))

    async def test_two_specials_stay_open_marked_and_clear_in_place(self):
        root, service, plan, extras, first = await self.prepared()
        await self.driver.click('特别篇 / OVA')
        popup = self.view._official_special_picker
        controls = self.view._official_special_controls
        with self.client:
            controls['files'].set_value(first)
        await self.driver.click_element(controls['confirm'])
        self.assertTrue(popup.value, '确认第一条后不能关闭窗口')
        self.assertNotIn('已确认', controls['files'].options[first])
        self.assertTrue(controls['file_confirmed'].visible)
        self.assertIs(controls['file_confirmed'].parent_slot.parent, controls['files'])
        self.assertEqual('append', controls['file_confirmed'].parent_slot.name)
        self.assertFalse(controls['files']._props.get('multiple', False))
        self.assertEqual(first, controls['files'].value)
        self.assertEqual('已确认特别篇身份', controls['confirmed'].text)
        self.assertEqual(['bar-help'], list(controls['confirmed'].classes))
        self.assertIs(controls['confirm'].parent_slot.parent, controls['confirmed'].parent_slot.parent)
        self.assertIs(controls['clear'].parent_slot.parent, controls['confirmed'].parent_slot.parent)
        first_saved = service.decision_state_store.load(plan.task_id).provider_special_decisions[first]
        second = next(k for k in controls['files'].options if k != first)
        with self.client:
            controls['files'].set_value(second)
            self.assertEqual(second, controls['files'].value)
            self.assertFalse(controls['file_confirmed'].visible)
            controls['mode'].set_value('descriptive')
            controls['title'].set_value('第二条特别篇')
        await self.driver.click_element(controls['confirm'])
        self.assertIs(popup, self.view._official_special_picker)
        self.assertTrue(popup.value)
        saved = service.decision_state_store.load(plan.task_id)
        self.assertTrue({first,second} <= set(saved.provider_special_decisions), self.notices)
        self.assertEqual(first_saved, saved.provider_special_decisions[first])
        second_saved = saved.provider_special_decisions[second]
        self.assertEqual('第二条特别篇', saved.provider_special_decisions[second]['title'])
        self.assertTrue(controls['file_confirmed'].visible)
        with self.client:
            controls['files'].set_value(first)
        self.assertEqual('已确认特别篇身份', controls['confirmed'].text)
        self.assertEqual(['bar-help'], list(controls['confirmed'].classes))
        self.assertIs(controls['confirm'].parent_slot.parent, controls['confirmed'].parent_slot.parent)
        self.assertIs(controls['clear'].parent_slot.parent, controls['confirmed'].parent_slot.parent)
        await self.driver.click_element(controls['clear'])
        self.assertTrue(popup.value)
        self.assertNotIn(first, service.decision_state_store.load(plan.task_id).provider_special_decisions)
        self.assertEqual(second_saved, service.decision_state_store.load(plan.task_id).provider_special_decisions[second])
        self.assertNotIn('已确认', controls['files'].options[first])
        self.assertFalse(controls['clear'].enabled)
        self.assertFalse(controls['confirmed'].visible)
        self.assertFalse(controls['file_confirmed'].visible)
        before = records(root)
        close = next(e for e in popup.descendants() if e._props.get('label') == '关闭')
        await self.driver.click_element(close)
        await self.driver.click_element(controls['confirm'])
        self.assertFalse(popup.value)
        self.assertEqual(before, records(root))

    async def test_failed_special_keeps_input_and_never_marks_confirmed(self):
        root, service, plan, extras, key = await self.prepared()
        await self.driver.click('特别篇 / OVA')
        controls = self.view._official_special_controls
        with self.client:
            controls['files'].set_value(key)
            controls['mode'].set_value('descriptive')
            controls['title'].set_value('保留未保存输入')
        before = records(root)
        with patch.object(service, 'revise_tv_content', side_effect=ValueError('isolated failure')):
            await self.driver.click_element(controls['confirm'])
        self.assertTrue(self.view._official_special_picker.value)
        self.assertEqual('保留未保存输入', controls['title'].value)
        self.assertNotIn('已确认', controls['files'].options[key])
        self.assertEqual(before, records(root))

    async def test_repair_button_has_one_stable_work_action_parent(self):
        root, service, plan, extras, key = await self.prepared()
        for _ in range(2):
            with self.client:
                await self.view._adopt_revision(self.view.plan)
            buttons = [e for e in self.view.descendants() if e._props.get('label') == '核查并补齐']
            self.assertEqual(1, len(buttons))
            parent = buttons[0].parent_slot.parent
            self.assertTrue(any(e._props.get('label') == '归入已有作品' for e in parent.descendants()),
                            '补齐入口必须与归入已有作品同一操作行')
