"""字幕保存与未归属音轨、人工排除的局部决定边界。"""
from dataclasses import replace
import unittest
from src.rename.content_choice import subtitle_edit_view
from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from src.rename.plan_verifier import PlanVerifier
from src.rename.subtitle_batch import build_batch, verify_edit, subtitle_statuses
from src.rename.audio_projection import project_audio
from tests.test_partition_content_contract import PartitionContentContracts, records
from tests.test_content_workbench_gui import ContentWorkbenchGui


def package(root, source):
    first = next(source.glob('*S01E01*.mkv'))
    first.with_suffix('.ass').write_text('[Script Info]\nTitle: Example\n', encoding='utf-8')
    folder = source/'SPs'; folder.mkdir(exist_ok=True)
    (folder/'Show [Menu].flac').write_bytes(b'disposable menu audio')
    return source


class SubtitleAudio(unittest.IsolatedAsyncioTestCase):
    flow = PartitionContentContracts.flow
    current = PartitionContentContracts.current
    selected = PartitionContentContracts.selected

    def chosen(self, plan):
        return next(e for e in subtitle_edit_view(plan).entries if e.subtitle and e.source_path.name.endswith('].ass'))

    async def test_pending_audio_save_keeps_audio_and_approval_block(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, _ = await self.selected(flow)
            audio = next(e for e in plan.entries if e.role == 'external_audio_unresolved')
            self.assertEqual(plan.entries, project_audio(plan).entries)
            old = PlanVerifier().verify(plan, validate_projection_receipt=False).issues
            for code in ('external_audio_projection_invalid', 'presentation_asset_missing'):
                self.assertIn(code, {i.code for i in old})
            chosen = self.chosen(plan)
            result = flow.service.adjust_subtitles(plan.task_id, PlanStore.fingerprint(plan),
                [entry_id(chosen)], main_language='zh-Hans')
            loaded = flow.store.load(plan.task_id)
            self.assertEqual(PlanStore.fingerprint(result), PlanStore.fingerprint(loaded))
            self.assertEqual([e for e in plan.entries if not e.subtitle], [e for e in loaded.entries if not e.subtitle])
            self.assertEqual(plan.ignored_sources, loaded.ignored_sources)
            self.assertEqual(('已修正',''), subtitle_statuses(loaded)[chosen.source_path])
            new = PlanVerifier().verify(loaded).issues
            self.assertTrue(all(i in new for i in old if i.code in {'external_audio_projection_invalid','presentation_asset_missing'}))
            self.assertIn(audio, loaded.entries)
            with self.assertRaisesRegex((ValueError, RuntimeError), 'external_audio_projection_invalid'):
                flow.service.approve_entries(plan.task_id, PlanStore.fingerprint(loaded), [entry_id(e) for e in loaded.entries])
            self.assertFalse(flow.service.load_or_create(loaded).approved_entry_ids)

    async def test_excluded_audio_gui_save_reload_then_subtitle_save(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, _ = await self.selected(flow)
            audio = next(e for e in plan.entries if e.role == 'external_audio_unresolved')
            holder = ContentWorkbenchGui('runTest')
            holder.create_page(prepared=(flow.root,flow.service,plan,[]),cleanup_owner=self)
            await holder.driver.click('内容保留与排除')
            panel = holder.view._content_workbench
            row = next(r for r in panel.rows if r['source'] == audio.source_path)
            panel.selected_change(row['identifier'],True)
            await holder.driver.click_element(panel.exclude)
            saved = flow.store.load(plan.task_id)
            self.assertIn(audio.source_path,saved.ignored_sources,panel.message.text)
            self.assertNotIn(audio.source_path,{e.source_path for e in saved.entries})
            self.assertFalse(next(r for r in panel.rows if r['source']==audio.source_path)['included'])
            panel.close()
            await holder.driver.click('内容保留与排除')
            self.assertFalse(next(r for r in holder.view._content_workbench.rows if r['source']==audio.source_path)['included'])
            holder.view._content_workbench.close()
            await holder.driver.click('调整字幕结构')
            editor = holder.view._subtitle_editor
            chosen = next(k for k,e in editor.entries.items() if e.source_path.name.endswith('].ass'))
            editor.set_selection([chosen]);editor.main.value='zh-Hans';editor.has_additional.value=False
            await holder.driver.click_element(editor.save_button)
            self.assertEqual('所选字幕调整已保存',editor.feedback.text)
            self.assertTrue(editor.dialog.value)
            loaded=flow.store.load(plan.task_id)
            self.assertIn(audio.source_path,loaded.ignored_sources)
            self.assertNotIn(audio.source_path,{e.source_path for e in loaded.entries})
            self.assertFalse({'external_audio_projection_invalid','presentation_asset_missing'} & {i.code for i in PlanVerifier().verify(loaded).issues})

    async def test_audio_drift_and_non_audio_missing_asset_remain_blocked(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan,_=await self.selected(flow)
            audio=next(e for e in plan.entries if e.role=='external_audio_unresolved')
            video=next(e for e in plan.entries if e.media_kind=='video')
            chosen=self.chosen(plan)
            for label, base in (
                ('audio_projection_drift',replace(plan,entries=tuple(replace(e,target_path=e.target_path.with_name('wrong.flac')) if e==audio else e for e in plan.entries))),
                ('non_audio_missing_asset',replace(plan,entries=tuple(replace(e,presentation_asset=None) if e==video else e for e in plan.entries))),
            ):
                with self.subTest(label=label):
                    revised=build_batch(base,PlanStore.fingerprint(base),[entry_id(chosen)],main_language='zh-Hans')
                    with self.assertRaises(ValueError):verify_edit(base,revised)
            revised=build_batch(subtitle_edit_view(plan),PlanStore.fingerprint(plan),[entry_id(chosen)],main_language='zh-Hans')
            changed=replace(revised,entries=tuple(replace(e,requires_review=False) if e==audio else e for e in revised.entries))
            before=records(flow.root)
            with self.assertRaises(ValueError):verify_edit(plan,changed)
            self.assertEqual(before,records(flow.root))
