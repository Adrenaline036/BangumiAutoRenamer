"""ANK image and retained-video incidents; disposable canonical transactions."""
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.rename.content_policy import require_content_complete
from src.rename.default_content import non_watchable_selection_sources
from src.pages.content_review_view import PAGE_SIZE
from tests import test_default_activation_contract as activation
from tests import test_content_workbench_gui as gui
from tests.test_content_choice_contract import change, records


class AnkContentIncidents(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow
    current = activation.DefaultActivationProducerContracts.current

    @staticmethod
    def images(root, source):
        for name in ('Scan (Vol.1)/page.PNG', 'Menu (Vol.2)/page.bmp', 'booklet.jpg'):
            path = source / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'disposable image fixture')
        return source

    async def test_new_default_excludes_images_and_explicit_restore_is_available(self):
        async with self.flow(configure_source=self.images) as flow:
            plan, state = self.current(flow)
            images = {flow.source / name for name in
                      ('Scan (Vol.1)/page.PNG', 'Menu (Vol.2)/page.bmp', 'booklet.jpg')}
            self.assertTrue(images <= set(plan.ignored_sources))
            self.assertFalse(images & {e.source_path for e in plan.entries})
            self.assertEqual(2, len([e for e in plan.entries if e.role == 'main_episode']))
            # Default exclusion is not a new lock on old or current selections.
            self.assertFalse(images & non_watchable_selection_sources(state.content_selection))
            restored = change(flow.service, plan, [flow.source/'booklet.jpg'], action='restore')
            self.assertIn(flow.source/'booklet.jpg', {e.source_path for e in restored.entries})
            restored_state = flow.service.decision_state_store.load(plan.task_id)
            self.assertNotIn(flow.source/'booklet.jpg', non_watchable_selection_sources(restored_state.content_selection))
            from src.pages.content_review_view import selection_rows
            row = next(r for r in selection_rows(restored_state.content_selection, None)
                       if r['source'] == flow.source/'booklet.jpg')
            self.assertFalse(row['locked'])
            self.assertEqual('保留整理', row['status'])

    @staticmethod
    def menu(root, source):
        shutil.copyfile(next(source.glob('*S01E01*.mkv')), source/'Menu (Vol.1).mkv')
        return source

    async def test_excluded_menu_does_not_leave_stale_blocker_and_restore_blocks(self):
        async with self.flow(configure_source=self.menu) as flow:
            plan, _ = self.current(flow)
            menu = flow.source/'Menu (Vol.1).mkv'
            self.assertIn(menu.name, plan.metadata['smart_completion_unresolved_videos'])
            with self.assertRaises(ValueError):
                require_content_complete(plan)
            revised = change(flow.service, plan, [menu])
            self.assertNotIn(menu, {e.source_path for e in revised.entries})
            require_content_complete(revised)
            restored = change(flow.service, revised, [menu], action='restore')
            with self.assertRaises(ValueError):
                require_content_complete(restored)

    async def test_same_basename_projection_uses_retained_entry_not_display_name(self):
        from src.rename.content_selection import filter_projection
        from src.rename.rename_plan import AssetRole
        def two(root, source):
            self.menu(root, source)
            (source/'Disc2').mkdir()
            shutil.copyfile(source/'Menu (Vol.1).mkv', source/'Disc2/Menu (Vol.1).mkv')
            return source
        async with self.flow(configure_source=two) as flow:
            plan, state = self.current(flow)
            a, b = flow.source/'Menu (Vol.1).mkv', flow.source/'Disc2/Menu (Vol.1).mkv'
            # Pure final projector control: distinct typed entry identities, same label.
            choice = state.content_selection
            value = choice.to_dict()
            from src.rename.content_choice import _evidence
            from src.rename.content_selection import ContentSelection
            value['excluded_sources'] = sorted(set(value['excluded_sources']) | {str(a)})
            value['evidence'] = _evidence(value, None, old=choice)
            omitted = ContentSelection.from_mapping(value)
            extra = next(e for e in plan.entries if e.source_path == b)
            extra = replace(extra, role='disc_extra', presentation_asset=replace(
                extra.presentation_asset, asset_role=AssetRole('extra', 'other')))
            projected = replace(plan, entries=tuple(extra if e.source_path == b else e for e in plan.entries))
            result = filter_projection(projected, omitted, None)
            self.assertFalse(result.metadata['smart_completion_unresolved_videos'])
            self.assertIn(b, {e.source_path for e in result.entries})
            # Excluded a must not clear still-unresolved b, even with identical basename.
            result = filter_projection(plan, omitted, None)
            self.assertTrue(result.metadata['smart_completion_unresolved_videos'])
            conflicts = replace(projected, metadata={**projected.metadata,
                'episode_assignment_conflict_sources': (str(b),), 'asset_collision_unresolved': ('collision',)})
            result = filter_projection(conflicts, omitted, None)
            self.assertEqual((str(b),), result.metadata['episode_assignment_conflict_sources'])
            self.assertEqual(('collision',), result.metadata['asset_collision_unresolved'])

    async def test_old_v2_image_selection_is_not_reclassified(self):
        from src.rename import default_content
        original = default_content.observe_watchable_sources
        def old_observation(*args, **kwargs):
            kwargs.pop('exclude_source_images', None)
            return original(*args, **kwargs)
        # Recreate the previous initial observation; unmodified typed writer persists it.
        with patch.object(default_content, 'observe_watchable_sources', side_effect=old_observation):
            async with self.flow(configure_source=self.images) as flow:
                plan, state = self.current(flow)
                image = flow.source/'booklet.jpg'
                self.assertIn(image, {e.source_path for e in plan.entries})
                choice = state.content_selection
                with patch.object(default_content, 'observe_watchable_sources', original):
                    self.assertNotIn(image, non_watchable_selection_sources(choice))
                    excluded = change(flow.service, plan, [image])
                    restored = change(flow.service, excluded, [image], action='restore')
                    self.assertIn(image, {e.source_path for e in restored.entries})
                    # Canonical TV rebuilding may repeat descriptive evidence;
                    # every material fact and the evidence set must still match.
                    before = choice.to_dict()['materials']
                    after = flow.service.decision_state_store.load(plan.task_id).content_selection.to_dict()['materials']
                    self.assertEqual([{**r, 'evidence': sorted(set(r['evidence']))} for r in before],
                                     [{**r, 'evidence': sorted(set(r['evidence']))} for r in after])

    async def test_page_buttons_include_fonts_and_disable_unmigrated_choice(self):
        helper = gui.ContentWorkbenchGui('runTest')
        before = helper.create_page(with_font=True, cleanup_owner=self)
        await helper.driver.click('内容保留与排除')
        view = helper.view._content_workbench
        await helper.driver.click_element(view.panels['exclude']['select'])
        fonts = {r['identifier'] for r in view.rows if r['kind'] == 'font'}
        self.assertTrue(fonts)
        self.assertTrue(fonts <= view.selected)
        await helper.driver.click_element(helper.view._content_workbench.close_button)
        self.assertEqual(before, records(helper.root))
        other = gui.ContentWorkbenchGui('runTest')
        before = other.create_page(legacy=True, cleanup_owner=self)
        await other.driver.click('内容保留与排除')
        view = other.view._content_workbench
        self.assertFalse(view.panels['exclude']['select'].enabled)
        self.assertFalse(view.panels['exclude']['clear'].enabled)
        with other.client:
            view.select_current_page(True)
        self.assertFalse(view.selected)
        await other.driver.click_element(other.view._content_workbench.close_button)
        self.assertEqual(before, records(other.root))

    @staticmethod
    def many(root, source):
        for index in range(PAGE_SIZE * 2 + 7):
            (source/f'note-{index:03}.txt').write_text('disposable', encoding='utf-8')
        return source

    async def test_page_buttons_preserve_other_pages_cancel_and_invalidate_once(self):
        async with self.flow(configure_source=self.many, queue_timeout=40) as flow:
            plan, _ = self.current(flow)
            helper = gui.ContentWorkbenchGui('runTest')
            before = helper.create_page(prepared=(flow.root, flow.service, plan, ()), cleanup_owner=flow)
            await helper.driver.click('内容保留与排除')
            view = helper.view._content_workbench
            def eligible():
                return {r['identifier'] for r in view.column_rows('exclude')[view.pages['exclude']*PAGE_SIZE:(view.pages['exclude']+1)*PAGE_SIZE]
                        if r['kind'] != 'font' and not r['locked']}
            first = eligible()
            with patch.object(view, 'invalidate', wraps=view.invalidate) as invalidate:
                await helper.driver.click_element(view.panels['exclude']['select'])
                self.assertEqual(1, invalidate.call_count)
            self.assertEqual(first, view.selected)
            await helper.driver.click_element(view.panels['exclude']['following'])
            second = eligible()
            await helper.driver.click_element(view.panels['exclude']['select'])
            self.assertEqual(first | second, view.selected)
            await helper.driver.click_element(view.panels['exclude']['clear'])
            self.assertFalse(view.selected)
            await helper.driver.click_element(view.panels['exclude']['following'])
            third = eligible()
            await helper.driver.click_element(view.panels['exclude']['select'])
            self.assertEqual(third, view.selected)
            await helper.driver.click_element(view.panels['exclude']['clear'])
            self.assertFalse(view.selected)
            with helper.client:
                view.set_busy(True)
            buttons = [e for e in helper.client.elements.values()
                       if getattr(e, 'text', '') in {'本页全选', '取消全选'}]
            self.assertEqual(4, len(buttons))
            self.assertTrue(all(not e.enabled for e in buttons))
            with helper.client:
                view.set_busy(False)
            await helper.driver.click_element(helper.view._content_workbench.close_button)
            self.assertEqual(before, records(flow.root))


class UnchangedObservationControls(unittest.TestCase):
    def test_new_image_option_suffixes_and_protection(self):
        from src.rename.media_classifier import observe_watchable_content
        root = Path.cwd()/'disposable-observation'
        for extension in ('png', 'jpg', 'jpeg', 'webp', 'bmp', 'tif', 'tiff', 'jp2'):
            with self.subTest(extension=extension):
                path = root/f'Folder/page.{extension.upper()}'
                self.assertEqual('known_non_watchable', observe_watchable_content(
                    path, source_root=root, exclude_source_images=True).disposition)
                self.assertEqual('keep_candidate', observe_watchable_content(path, source_root=root,
                    exclude_source_images=True, protected_role='font_resource').disposition)
                self.assertEqual('keep_candidate', observe_watchable_content(path, source_root=root,
                    exclude_source_images=True, linked_video=root/'video.mkv').disposition)
        for extension in ('ass', 'srt', 'ttf', 'zip', 'txt'):
            self.assertEqual('needs_confirmation', observe_watchable_content(
                root/f'file.{extension}', source_root=root, exclude_source_images=True).disposition)

    def test_legacy_observation_and_protected_control(self):
        from src.rename.media_classifier import observe_watchable_content
        root = Path.cwd()/'disposable-observation'
        self.assertEqual('needs_confirmation', observe_watchable_content(root/'image.png', source_root=root).disposition)
        self.assertEqual('keep_candidate', observe_watchable_content(
            root/'image.png', source_root=root, protected_role='font_resource').disposition)

    def test_image_only_discovery_group_remains_available(self):
        import tempfile
        from src.rename.input_directory_discovery import discover_sources, discovery_task_groups
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root/'Season 1').mkdir()
            (root/'Season 1/Show S01E01.mkv').write_bytes(b'video fixture')
            (root/'Booklet').mkdir()
            image = root/'Booklet/page.png'
            image.write_bytes(b'image fixture')
            discovery = discover_sources([root])
            self.assertIn(str(image), {r.source_path for g in discovery_task_groups(discovery) for r in g.identities})
