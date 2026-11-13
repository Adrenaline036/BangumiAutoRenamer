"""Regression shapes from CD volume packages and the modern review exits."""
from pathlib import Path
import inspect
import shutil
import tempfile
import unittest

from src.rename.media_classifier import observe_watchable_sources
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from tests import test_default_activation_contract as activation
from tests.test_content_choice_contract import records
from tests import test_content_workbench_gui as workbench_fixture


class CDVolumeObservation(unittest.TestCase):
    def observe(self, *names, **kwargs):
        root = Path.cwd() / 'isolated-name-observation'
        return observe_watchable_sources([root / name for name in names], source_root=root, **kwargs)

    def test_audio_disc_volume_names_exclude_standalone_wav(self):
        for folder in ('特典CD (Vol.1)', '特典CD （Ｖｏｌ．６）', 'CDs Vol.2', 'OST (Vol.12)'):
            with self.subTest(folder=folder):
                self.assertEqual('known_non_watchable', self.observe(folder+'/独立音频.WAV')[0].disposition)

    def test_cd_text_notices_excluded_but_unrelated_text_not(self):
        self.assertEqual('known_non_watchable', self.observe('CDs/Album/转载声明.txt')[0].disposition)
        self.assertEqual('needs_confirmation', self.observe('notes.txt')[0].disposition)

    def test_narrow_controls(self):
        for folder in ('Menu (Vol.1)', 'Scan (Vol.1)', 'Some CD (Vol.1)', 'CD (Vol.1) bonus', 'CD Vol.x'):
            with self.subTest(folder=folder):
                self.assertEqual('needs_confirmation', self.observe(folder+'/audio.wav')[0].disposition)
        for name in ('CDs/Font.ttf', 'CDs/dialogue.ass', 'CDs/Fonts.zip'):
            self.assertEqual('needs_confirmation', self.observe(name)[0].disposition)
        self.assertEqual('needs_confirmation', self.observe('特典CD (Vol.1)/audio.wav',
            '特典CD (Vol.1)/Episode.mkv')[0].disposition)
        root = Path.cwd()/'isolated-name-observation'
        audio = root/'特典CD (Vol.1)/audio.wav'
        self.assertEqual('keep_candidate', self.observe(str(audio.relative_to(root)),
            linked_videos={audio:root/'Episode.mkv'})[0].disposition)

    def test_discovery_keeps_inventory_but_does_not_create_cd_task(self):
        from src.rename.input_directory_discovery import discover_sources, discovery_task_groups
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = (root/'Season 1/Show S01E01.mkv', root/'特典CD (Vol.1)/Soundtrack.wav',
                     root/'特典CD (Vol.1)/転載声明.txt')
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'isolated discovery fixture')
            value = discover_sources([root])
            self.assertEqual(set(paths), {Path(i.source_path) for g in value.groups for i in g.identities})
            self.assertEqual({paths[0]}, {Path(i.source_path) for g in discovery_task_groups(value) for i in g.identities})
            extra = root/'特典CD (Vol.1)/Episode.mkv'
            extra.write_bytes(b'isolated video control')
            value = discover_sources([root])
            self.assertIn(extra, {Path(i.source_path) for g in discovery_task_groups(value) for i in g.identities})


class ModernContentExits(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow
    current = activation.DefaultActivationProducerContracts.current

    async def test_invalid_and_cancelled_extra_does_not_change_parent_selection_or_records(self):
        async with self.flow(configure_source=self.source) as flow:
            plan, _ = self.current(flow)
            helper = workbench_fixture.ContentWorkbenchGui('runTest')
            before = helper.create_page(prepared=(flow.root, flow.service, plan, ()), cleanup_owner=flow)
            view = helper.view
            view.selected_ids = {'unrelated-parent-selection'}
            with helper.client:
                view._apply_exceptional_special_inline(['not-a-current-file'], 'unmatched_extra', None)
            self.assertIsNone(getattr(view, '_content_workbench', None))
            video = next(e for e in plan.entries if e.source_path.name == 'Unknown OVA.mkv')
            with helper.client:
                view._apply_exceptional_special_inline([entry_id(video)], 'unmatched_extra', None)
            self.assertEqual({entry_id(video)}, view._content_workbench.selected)
            self.assertEqual({'unrelated-parent-selection'}, view.selected_ids)
            self.assertEqual(before, records(flow.root))
            await helper.driver.click_element(helper.view._content_workbench.close_button)
            self.assertEqual(before, records(flow.root))

    async def test_previously_approved_plan_loses_approval_after_extra_exclusion(self):
        async with self.flow() as flow:
            plan, _ = self.current(flow)
            approved = flow.service.approve_entries(plan.task_id, PlanStore.fingerprint(plan),
                [entry_id(e) for e in plan.entries])
            self.assertTrue(approved.approved_entry_ids)
            helper = workbench_fixture.ContentWorkbenchGui('runTest')
            helper.create_page(prepared=(flow.root, flow.service, plan, ()), cleanup_owner=flow)
            video = next(e for e in plan.entries if e.media_kind == 'video')
            with helper.client:
                result = helper.view._apply_exceptional_special_inline([entry_id(video)], 'unmatched_extra', None)
                if inspect.isawaitable(result):
                    await result
            self.assertIsNotNone(getattr(helper.view, '_content_workbench', None))
            await helper.driver.click('排除整理')
            revised = flow.store.load(plan.task_id)
            self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(revised))
            self.assertFalse(flow.service.load_or_create(revised).approved_entry_ids)
            self.assertIn(video.source_path, revised.ignored_sources)

    def source(self, root, source):
        payload = next(source.glob('*S01E01*'))
        for name in ('Unknown OVA.mkv', 'Unnumbered Bonus.mkv'):
            shutil.copyfile(payload, source/name)
        (source/'Standalone.wav').write_bytes(b'isolated audio')
        return source

    async def test_extra_routes_open_shared_preview_then_exclude_only_selection(self):
        for route in ('batch', 'single', 'role'):
            with self.subTest(route=route):
                async with self.flow(configure_source=self.source) as flow:
                    plan, _ = self.current(flow)
                    helper = workbench_fixture.ContentWorkbenchGui('runTest')
                    helper.create_page(prepared=(flow.root, flow.service, plan, ()), cleanup_owner=flow)
                    view = helper.view
                    wanted = [entry_id(e) for e in plan.entries if e.source_path.name in
                              ('Unknown OVA.mkv', 'Unnumbered Bonus.mkv')]
                    self.assertEqual(2, len(wanted))
                    selected = wanted if route == 'batch' else wanted[:1]
                    originals = {e.source_path:e.source_path.read_bytes() for e in plan.entries}
                    before = records(flow.root)
                    with helper.client:
                        if route == 'batch':
                            result = view._apply_exceptional_special_inline(selected, 'unmatched_extra', None)
                        elif route == 'single':
                            result = view._apply_provider_special_inline(selected[0], 'unmatched_extra', None)
                        else:
                            result = view._rebuild_inventory_role(selected[0], 'extra')
                        if inspect.isawaitable(result):
                            await result
                    work = getattr(view, '_content_workbench', None)
                    self.assertIsNotNone(work, 'extra must open shared exclusion preview, not publish Extras')
                    self.assertEqual(set(selected), work.selected)
                    self.assertEqual(before, records(flow.root))
                    self.assertEqual(before, records(flow.root))
                    await helper.driver.click('排除整理')
                    revised = flow.store.load(plan.task_id)
                    removed = {e.source_path for e in plan.entries if entry_id(e) in selected}
                    self.assertTrue(removed <= set(revised.ignored_sources))
                    self.assertEqual({e.source_path for e in plan.entries}-removed,
                                     {e.source_path for e in revised.entries})
                    self.assertFalse(flow.service.load_or_create(revised).approved_entry_ids)
                    self.assertEqual(originals, {p:p.read_bytes() for p in originals})

    async def test_audio_error_has_direct_shared_preview_and_cancel_is_readonly(self):
        async with self.flow(configure_source=self.source) as flow:
            plan, _ = self.current(flow)
            helper = workbench_fixture.ContentWorkbenchGui('runTest')
            before = helper.create_page(prepared=(flow.root, flow.service, plan, ()), cleanup_owner=flow)
            self.assertIn('external_audio_projection_invalid', {i['code'] for i in helper.view.snapshot.issues})
            await helper.driver.click('选择本次不整理的音轨')
            work = helper.view._content_workbench
            audio = next(e for e in plan.entries if e.source_path.name == 'Standalone.wav')
            self.assertEqual({entry_id(audio)}, work.selected)
            self.assertEqual(before, records(flow.root))
            await helper.driver.click_element(helper.view._content_workbench.close_button)
            self.assertEqual(before, records(flow.root))


if __name__ == '__main__':
    unittest.main()
