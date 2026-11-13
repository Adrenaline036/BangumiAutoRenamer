"""Registered movie form: pick files first, edit only selected labels."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from nicegui import ui
from nicegui.client import Client
from nicegui.page import page
from src.pages.asset_choice_forms import show_movie_versions
from tests.test_content_choice_contract import migrated, records, change
from tests import _gui_helpers as gui_fixture


class MovieFilePicker(unittest.IsolatedAsyncioTestCase):
    def open_form(self, service, plan):
        owner = SimpleNamespace(plan=plan, review_service=service)
        owner._video_options = lambda: {str(e.source_path): e.source_path.name
            for e in owner.plan.entries if e.media_kind == 'video'}
        owner._selected_movie_primary_sources = lambda: [p for g in
            owner.plan.metadata.get('movie_version_selection', {}).get('groups', ()) for p in g['sources']]
        owner._adopt_revision = lambda revised: setattr(owner, 'plan', revised)
        client = Client(page('/isolated-file-picker'), request=None)
        self.addCleanup(client.delete)
        driver = gui_fixture.GuiDriver('runTest')
        driver.client = client
        with client:
            show_movie_versions(owner, plan.presentation_groups[0].group_id, ui=ui,
                notify=lambda *_a, **_k: None,
                complete_movie=lambda *_a, **_k: self.fail('unexpected legacy completion'))
        selectors = [e for e in client.elements.values()
            if e._props.get('label') == '电影正片文件（可多选）']
        self.assertEqual(1, len(selectors), 'missing file-first multiple selector')
        self.assertTrue(selectors[0]._props.get('multiple'))
        from nicegui.helpers import event_type_to_camel_case
        position_handler = f"() => requestAnimationFrame(() => runMethod({selectors[0].id}, 'updateMenuPosition', []))"
        self.assertTrue(any(listener.type == event_type_to_camel_case('popup-show')
            and listener.js_handler == position_handler
            for listener in selectors[0]._event_listeners.values()),
            'first popup must position after its portal is mounted')
        self.assertFalse(any(getattr(e, 'text', '') == '保留' for e in client.elements.values()))
        return owner, client, driver, selectors[0]

    def labels(self, client):
        return [e for e in client.elements.values() if e._props.get('label') == '版本标签']

    async def test_initial_empty_pick_then_one_file_only_builds_selected_primary(self):
        from tests.test_initial_movie_completion_contract import InitialMovieFullCompletionContracts
        helper = InitialMovieFullCompletionContracts('runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        root, service, plan, primary, extra = await helper.fixture(multiple=True)
        before = records(root)
        owner, client, driver, picker = self.open_form(service, plan)
        self.assertEqual([], picker.value)
        self.assertEqual([], self.labels(client))
        await driver.click('保存正片版本')
        self.assertEqual(before, records(root), 'empty choice must not write')
        picker.value = [str(primary), str(extra['second'])]
        self.assertEqual(2, len(self.labels(client)))
        for field in self.labels(client):
            field.value = ''
        picker.value = [str(primary)]
        self.assertEqual(1, len(self.labels(client)))
        self.labels(client)[0].value = '人工选择的正片'
        await driver.click('保存正片版本')
        self.assertNotEqual(plan, owner.plan)
        self.assertEqual({primary}, {e.source_path for e in owner.plan.entries if e.media_kind == 'video'})
        self.assertIn(extra['second'], owner.plan.ignored_sources)
        self.assertEqual('人工选择的正片', owner.plan.metadata['movie_version_selection']['groups'][0]['label'])

    async def test_edit_deselect_removes_label_and_excludes_video_with_companions(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root)
            before = records(root)
            owner, client, driver, picker = self.open_form(service, plan)
            self.assertEqual(2, len(self.labels(client)))
            picker.value = [str(videos[0])]
            self.assertEqual(1, len(self.labels(client)))
            self.labels(client)[0].value = '所选正片'
            self.assertEqual(before, records(root), 'editing controls is not submission')
            await driver.click('保存正片版本')
            kept = {e.source_path for e in owner.plan.entries}
            self.assertNotIn(videos[1], kept)
            self.assertFalse(any(p.stem.startswith(videos[1].stem) for p in kept))
            self.assertIn(videos[1], owner.plan.ignored_sources)
            self.assertEqual('所选正片', owner.plan.metadata['movie_version_selection']['groups'][0]['label'])
            self.assertFalse(service.load_or_create(owner.plan).approved_entry_ids)

    async def test_continuous_group_is_one_selection_and_cancel_changes_nothing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root, segments=True)
            plan = change(service, plan, [videos[2]])
            before = records(root)
            owner, client, driver, picker = self.open_form(service, plan)
            self.assertEqual(1, len(picker.value), 'existing continuous parts remain atomic')
            self.assertIn(str(videos[0]), picker.options)
            self.assertNotIn(str(videos[1]), picker.options, 'a later segment is not an independent version')
            self.assertEqual(1, len(self.labels(client)))
            picker.value = []
            self.assertEqual([], self.labels(client))
            await driver.click('关闭')
            self.assertEqual(before, records(root))


if __name__ == '__main__':
    unittest.main()
