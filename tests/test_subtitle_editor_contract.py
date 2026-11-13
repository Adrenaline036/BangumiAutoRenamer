"""批量字幕保存与当前审核页面的定向验证。"""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename.content_choice import subtitle_edit_view
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.subtitle_batch import build_batch
from src.rename.subtitle_languages import language_key, multilingual_title
from tests.test_content_choice_contract import records, change
from tests.test_content_subtitle_lifecycle import movie, series


class BatchContract(unittest.TestCase):
    def test_movie_batch_restart_history_then_next_batch_and_undo(self):
        from src.rename.partition_subtitle_history import read_subtitle_history
        from src.rename.subtitle_transition_history import observe as observe_subtitle_transitions
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = movie(root)
            entries = [e for e in subtitle_edit_view(plan).entries if e.source_path in
                       {v.with_suffix('.sc.ass') for v in videos[:2]}]
            result = service.adjust_subtitles(plan.task_id, PlanStore.fingerprint(plan),
                [entry_id(e) for e in entries], main_language='zh-Hans', additional_languages=['ja'])
            self.assertEqual(2, len(result.subtitle_override_rules))
            for e in result.entries:
                if e.source_path in {v.source_path for v in entries}:
                    self.assertTrue(e.target_path.name.endswith('.zh-Hans.简日双语.ass'), e.target_path)
            loaded = service.plan_store.load(plan.task_id)
            history = read_subtitle_history(service.plan_store, loaded)
            observe_subtitle_transitions(service.plan_store, loaded, history)
            result2 = service.adjust_subtitles(plan.task_id, PlanStore.fingerprint(loaded),
                [entry_id(entries[0])], main_language='fr')
            self.assertEqual(3, len(result2.subtitle_override_rules))
            reverted = service.revert_subtitle_rule(plan.task_id, PlanStore.fingerprint(result2),
                result2.subtitle_override_rules[-1].rule_id, '撤销上次字幕调整')
            reverted = service.revert_subtitle_rule(plan.task_id, PlanStore.fingerprint(reverted),
                reverted.subtitle_override_rules[-1].rule_id, '撤销上次字幕调整')
            self.assertFalse(reverted.subtitle_override_rules)
            self.assertEqual(plan.ignored_sources, reverted.ignored_sources)

    def test_tv_batch_keeps_explicit_exclusion_and_manual_choices(self):
        with tempfile.TemporaryDirectory() as raw:
            service, plan = series(Path(raw))
            subtitles = [e for e in plan.entries if e.subtitle is not None]
            excluded = subtitles[0].source_path
            plan = change(service, plan, [excluded])
            result = service.adjust_subtitles(plan.task_id, PlanStore.fingerprint(plan),
                [entry_id(e) for e in subtitles[:2]], main_language='zh-Hant', additional_languages=['ja'])
            self.assertIn(excluded, result.ignored_sources)
            self.assertNotIn(excluded, {e.source_path for e in result.entries})
            hidden = next(e for e in subtitle_edit_view(result).entries if e.source_path == excluded)
            self.assertEqual('Hant', hidden.subtitle.script)

    def test_bad_selection_stale_fingerprint_and_disconnected_guard_do_not_write(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            service, plan, videos = movie(root)
            selected = [entry_id(e) for e in plan.entries if e.subtitle is not None][:2]
            before = records(root)
            for ids, fp in [(selected+['missing'], PlanStore.fingerprint(plan)),
                            ([selected[0]]*2, PlanStore.fingerprint(plan)), (selected, 'stale')]:
                with self.assertRaises((ValueError, RuntimeError)):
                    service.adjust_subtitles(plan.task_id, fp, ids, main_language='ja')
                self.assertEqual(before, records(root))
            with patch('src.rename.content_choice.validate_subtitle_contracts', return_value=None) as guard:
                with self.assertRaises((ValueError, RuntimeError)):
                    service.adjust_subtitles(plan.task_id, PlanStore.fingerprint(plan), selected, main_language='ja')
            guard.assert_called()
            self.assertEqual(before, records(root))

    def test_collision_and_missing_association_reject_without_writes(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            service,plan=series(root)
            selected=[entry_id(e) for e in plan.entries if e.subtitle is not None and 'Ep02' in e.source_path.name]
            before=records(root)
            with self.assertRaises((ValueError,RuntimeError)):
                service.adjust_subtitles(plan.task_id,PlanStore.fingerprint(plan),selected,main_language='en')
            self.assertEqual(before,records(root))
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            service,plan,_=movie(root)
            missing=[entry_id(e) for e in subtitle_edit_view(plan).entries if e.subtitle is not None and e.subtitle.associated_video_source_path is None]
            self.assertTrue(missing)
            before=records(root)
            with self.assertRaisesRegex(ValueError,'没有唯一对应的视频'):
                service.adjust_subtitles(plan.task_id,PlanStore.fingerprint(plan),missing,main_language='ja')
            self.assertEqual(before,records(root))

    def test_unknown_label_can_be_saved_while_other_subtitle_waits(self):
        from tests.test_subtitle_plan_contract import make_series_plan,persist_subtitle_fixture
        from src.rename.plan_review import PlanReviewService
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            store=PlanStore(root/'plans')
            plan=persist_subtitle_fixture(store,make_series_plan(),root)
            service=PlanReviewService(store,root/'reviews',root/'work_catalog')
            plan=service.commit_content_change(service.prepare_content_migration(plan.task_id,
                PlanStore.fingerprint(plan),reason='人工核对字幕'))
            selected=next(e for e in subtitle_edit_view(plan).entries if e.subtitle is not None)
            others={e.source_path:e for e in plan.entries if e.source_path!=selected.source_path}
            result=service.adjust_subtitles(plan.task_id,PlanStore.fingerprint(plan),[entry_id(selected)],
                main_language='zh-Hans',additional_languages=['ja'])
            self.assertEqual(others,{e.source_path:e for e in result.entries if e.source_path!=selected.source_path})
            self.assertIn('Mystery',next(e for e in result.entries if e.source_path==selected.source_path).subtitle.track_title_tokens)

    def test_untouched_duplicate_tracks_allow_other_episode_edit(self):
        from tests.test_subtitle_plan_contract import make_series_plan, persist_subtitle_fixture
        from src.rename.plan_review import PlanReviewService
        from src.rename.plan_verifier import PlanVerifier
        from src.rename.subtitle_labels import DimensionResolution
        from src.rename.subtitle_batch import verify_edit
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            base=make_series_plan()
            entries=tuple(replace(e,requires_review=False,subtitle=replace(e.subtitle,
                raw_label='SC',unknown_tokens=(),parse_state=DimensionResolution.RESOLVED,
                flags=('default',) if e.episode_id==1 else ())) if e.subtitle else e for e in base.entries)
            duplicate=replace(entries[1],source_path=entries[1].source_path.with_suffix('.srt'),
                target_path=entries[1].target_path.with_name('Show S01E01.zh-Hans.default.srt'))
            entries=tuple(replace(e,target_path=e.target_path.with_name('Show S01E01.zh-Hans.default.ass'))
                if e==entries[1] else e for e in entries)
            base=replace(base,entries=(*entries,duplicate),expected_sources=(*base.expected_sources,duplicate.source_path))
            store=PlanStore(root/'plans')
            plan=persist_subtitle_fixture(store,base,root)
            service=PlanReviewService(store,root/'reviews',root/'work_catalog')
            codes={i.code for i in PlanVerifier().verify(plan).issues}
            self.assertTrue({'duplicate_subtitle_semantics','multiple_default_subtitles'} <= codes,codes)
            chosen=next(e for e in plan.entries if e.subtitle and e.episode_id==2)
            result=service.adjust_subtitles(plan.task_id,PlanStore.fingerprint(plan),[entry_id(chosen)],main_language='ja')
            self.assertEqual([e for e in plan.entries if e.episode_id==1],
                             [e for e in result.entries if e.episode_id==1])
            self.assertTrue({'duplicate_subtitle_semantics','multiple_default_subtitles'} <=
                            {i.code for i in PlanVerifier().verify(result).issues})
            # A changed participant must not be hidden by an unchanged later duplicate.
            first=next(e for e in plan.entries if e.subtitle and e.episode_id==1)
            changed=replace(plan,entries=tuple(replace(e,requires_review=True) if e==first else e for e in plan.entries))
            with self.assertRaisesRegex(ValueError,'duplicate_subtitle_semantics'):
                verify_edit(plan,changed)

    def test_language_labels_are_consumed_and_descriptions_flags_preserved(self):
        from src.rename.subtitle_labels import DimensionResolution
        with tempfile.TemporaryDirectory() as raw:
            _, plan, _ = movie(Path(raw))
            view=subtitle_edit_view(plan)
            entry=next(e for e in view.entries if e.subtitle is not None)
            for label, expected in [('zh-sc','Hans'), ('zh-tc','Hant'), ('sc&jp','Hans'), ('tc&jp','Hant'), ('','Hans')]:
                with self.subTest(label=label):
                    contract=replace(entry.subtitle, raw_label=label, unknown_tokens=(label,'Studio') if label else ('Studio',),
                        parse_state=DimensionResolution.UNRESOLVED, track_title_tokens=(), flags=('forced',))
                    probe=replace(view, entries=tuple(replace(e,subtitle=contract) if e==entry else e for e in view.entries))
                    result=build_batch(probe,PlanStore.fingerprint(plan),[entry_id(entry)],
                        main_language='zh-'+expected, additional_languages=['ja'])
                    new=next(e for e in result.entries if e.source_path==entry.source_path).subtitle
                    self.assertEqual(('forced',),new.flags)
                    self.assertIn('Studio',new.track_title_tokens)
                    self.assertNotIn(label,new.track_title_tokens)
                    self.assertEqual(expected,new.script)
                    removed=build_batch(probe,PlanStore.fingerprint(plan),[entry_id(entry)],
                        main_language='en',remove_tags=['Studio','forced'])
                    new=next(e for e in removed.entries if e.source_path==entry.source_path).subtitle
                    self.assertFalse(new.flags)
                    self.assertNotIn('Studio',new.track_title_tokens)
            self.assertEqual('fr',language_key('fra'))
            self.assertEqual('简繁双字幕',multilingual_title('zh-Hans',['zh-Hant']))
            self.assertIn('三语',multilingual_title('zh-Hans',['ja','en']))
            for value in ('xx','../en','ENGLISH'):
                with self.assertRaises(ValueError): language_key(value)
            with self.assertRaises(ValueError): multilingual_title('ja',['ja'])


class EditorGui(unittest.IsolatedAsyncioTestCase):
    async def prepare(self, *, tv=False):
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        if tv:
            service,plan=series(root)
            videos=[]
        else:
            service,plan,videos=movie(root)
        self.enterContext(patch('src.pages.season_reference_view.load',return_value=None))
        self.enterContext(patch('src.pages.season_reference_view.prepare'))
        holder=ContentWorkbenchGui('runTest')
        holder.create_page(prepared=(root,service,plan,videos),cleanup_owner=self)
        await holder.driver.click('调整字幕结构')
        return holder, holder.view._subtitle_editor

    async def test_movie_toolbar_batch_save_feedback_continue_and_reopen(self):
        holder, editor=await self.prepare()
        labels=[getattr(e,'text','') for e in holder.client.elements.values()]
        self.assertIn('调整计划范围',labels)
        self.assertNotIn('导出 JSON',labels)
        self.assertNotIn('字幕结构化决定 · 自动生成唯一目标',labels)
        tag=next(tag for tag,ids in editor.tags.items() if len(ids)>=2)
        editor.select_row('tags',tag,True)
        self.assertGreaterEqual(len(editor.selected),2)
        editor.selected=[key for key in editor.selected if editor.entries[key].subtitle.associated_video_source_path is not None][:2]
        editor.main.value='zh-Hans'
        editor.has_additional.value=True
        editor.additional.value=['ja']
        await holder.driver.click_element(editor.save_button)
        self.assertEqual('所选字幕调整已保存',editor.feedback.text)
        self.assertTrue(editor.dialog.value)
        self.assertEqual(PlanStore.fingerprint(editor.plan),PlanStore.fingerprint(holder.view.plan))
        editor.set_selection([editor.selected[0]])
        editor.main.value='en'
        editor.has_additional.value=False
        await holder.driver.click_element(editor.save_button)
        self.assertTrue(editor.dialog.value)
        editor.dialog.close()
        await holder.driver.click('调整字幕结构')
        self.assertTrue(holder.view._subtitle_editor.dialog.value)
        self.assertEqual(PlanStore.fingerprint(editor.plan),PlanStore.fingerprint(holder.view._subtitle_editor.plan))

    async def test_tv_save_refresh_failure_is_visible_and_retry_keeps_window(self):
        holder,editor=await self.prepare(tv=True)
        editor.set_selection([next(iter(editor.entries))])
        editor.main.value='en'
        with patch.object(holder.view,'_adopt_revision',side_effect=RuntimeError('refresh failed')):
            await holder.driver.click_element(editor.save_button)
        self.assertIn('已保存',editor.feedback.text)
        self.assertIn('刷新失败',editor.feedback.text)
        self.assertTrue(editor.sync_pending)
        self.assertFalse(editor.save_button.enabled)
        self.assertTrue(editor.dialog.value)
        await holder.driver.click_element(editor.retry_button)
        self.assertFalse(editor.sync_pending)
        self.assertTrue(editor.dialog.value)

    async def test_two_columns_paging_selection_and_single_preview(self):
        holder,editor=await self.prepare(tv=True)
        original=list(editor.entries)
        sample=editor.entries[original[0]]
        # Pagination-only rows are isolated display data; saving uses the real fixtures in the other tests.
        for index in range(7):
            key=f'display-{index}'
            editor.entries[key]=replace(sample,source_path=sample.source_path.with_name(f'Example{index}.ass'))
            editor.tags[f'label-{index}']=[key]
        editor.render_lists()
        for kind in ('files','tags'):
            editor.resize_page(kind,5)
            self.assertEqual(5,len(editor.panes[kind]['checks']))
            first=next(iter(editor.panes[kind]['checks']))
            editor.panes[kind]['checks'][first].value=True
            chosen=set(editor.selected)
            await holder.driver.click_element(editor.panes[kind]['buttons'][2])
            self.assertEqual(1,editor.pages[kind])
            self.assertEqual(chosen,set(editor.selected))
            editor.resize_page(kind,10)
            self.assertEqual(0,editor.pages[kind])
            self.assertEqual(chosen,set(editor.selected))
            self.assertIn('已选择',editor.panes[kind]['label'].text)
        editor.set_selection(original[:2])
        editor.main.value='ja'
        self.assertEqual('预览',editor.preview.default_slot.children[0].text)
        self.assertEqual(2,len(editor.preview.default_slot.children))
        self.assertIs(editor.feedback.parent_slot,editor.save_button.parent_slot)
        self.assertIn('ml-auto',editor.close_button.classes)
        self.assertFalse(hasattr(editor,'file_dialog'))
        self.assertFalse(hasattr(editor,'tag_picker'))

    async def test_tag_check_reflects_file_selection_and_main_language_is_disabled(self):
        holder,editor=await self.prepare()
        tag=next(tag for tag,keys in editor.tags.items() if len(keys)>=2)
        keys=editor.tags[tag]
        editor.set_selection([keys[0]])
        self.assertIsNone(editor.panes['tags']['checks'][tag].value)
        editor.select_row('tags',tag,True)
        self.assertTrue(set(keys)<=set(editor.selected))
        editor.select_row('files',keys[0],False)
        self.assertNotIn(keys[0],editor.selected)
        self.assertIsNone(editor.panes['tags']['checks'][tag].value)
        editor.main.value='zh-Hans'
        editor.has_additional.value=True
        editor.additional.value=['ja']
        self.assertEqual('简体中文 · 主语言',editor.additional.options['zh-Hans'])
        self.assertTrue(any('option-disable' in key for key in editor.additional._props))
        editor.main.value='ja'
        self.assertEqual([],editor.additional.value)
        self.assertEqual('日语 · 主语言',editor.additional.options['ja'])
        editor.main.value='other'
        editor.other_main.value='fra'
        self.assertIn('主语言',editor.additional.options['fr'])
        editor.additional.value=['other']
        editor.other_additional.value='fr,de'
        self.assertEqual('de',editor.other_additional.value)
