"""字幕编辑与未完成内容独立，保留正式批准门。"""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from src.rename.content_choice import subtitle_edit_view
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.subtitle_batch import build_batch, verify_edit
from tests.test_partition_content_contract import PartitionContentContracts, records
from tests.test_content_subtitle_lifecycle import series


def pending_package(root, source):
    sample = next(source.glob('*S01E01*.mkv')).read_bytes()
    for index in (1, 2):
        video = source / f'Ordinary Show S01E{index:02} [1080p].mkv'
        video.with_suffix('.zh-sc.ass').write_text('[Script Info]\nTitle: Sample\n', encoding='utf-8')
    for index in range(1, 5):
        (source / f'Ordinary Show [SP{index:02}].mkv').write_bytes(sample)
    return source


class Readiness(unittest.IsolatedAsyncioTestCase):
    flow = PartitionContentContracts.flow
    current = PartitionContentContracts.current
    selected = PartitionContentContracts.selected

    async def test_pending_video_allows_subtitle_save_restart_but_not_approval(self):
        async with self.flow(automatic=False, configure_source=pending_package) as flow:
            plan, _ = await self.selected(flow)
            codes = [i.code for i in PlanVerifier().verify(plan, validate_projection_receipt=False).issues]
            self.assertIn('content_policy_pending', codes)
            self.assertEqual(4, codes.count('provider_special_eligibility_unavailable'), codes)
            self.assertEqual(4, codes.count('presentation_asset_role_unknown'), codes)
            chosen = next(e for e in subtitle_edit_view(plan).entries if e.subtitle)
            unchanged = {e.source_path: e for e in plan.entries if e.source_path != chosen.source_path}
            result = flow.service.adjust_subtitles(plan.task_id, PlanStore.fingerprint(plan),
                [entry_id(chosen)], main_language='zh-Hans')
            self.assertEqual(unchanged, {e.source_path: e for e in result.entries if e.source_path != chosen.source_path})
            self.assertEqual(plan.ignored_sources, result.ignored_sources)
            loaded = flow.store.load(plan.task_id)
            self.assertEqual(PlanStore.fingerprint(result), PlanStore.fingerprint(loaded))
            self.assertEqual('Hans', next(e.subtitle.script for e in loaded.entries if e.source_path == chosen.source_path))
            self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(loaded).issues})
            self.assertFalse(flow.service.load_or_create(loaded).approved_entry_ids)
            with self.assertRaises((ValueError, RuntimeError)):
                flow.service.approve_entries(plan.task_id, PlanStore.fingerprint(loaded), [entry_id(e) for e in loaded.entries])
            self.assertFalse(flow.service.load_or_create(loaded).approved_entry_ids)

    async def test_video_change_and_new_subtitle_conflict_are_rejected(self):
        async with self.flow(automatic=False, configure_source=pending_package) as flow:
            plan, _ = await self.selected(flow)
            chosen = next(e for e in subtitle_edit_view(plan).entries if e.subtitle)
            revised = build_batch(subtitle_edit_view(plan), PlanStore.fingerprint(plan),
                                  [entry_id(chosen)], main_language='zh-Hans')
            video = next(e for e in revised.entries if e.media_kind == 'video')
            changed = replace(revised, entries=tuple(replace(e, target_path=e.target_path.with_name('changed.mkv'))
                                                     if e == video else e for e in revised.entries))
            before = records(flow.root)
            with self.assertRaises(ValueError):
                verify_edit(plan, changed)
            self.assertEqual(before, records(flow.root))
        with tempfile.TemporaryDirectory() as raw:
            service, plan = series(Path(raw))
            ids = [entry_id(e) for e in plan.entries if e.subtitle and 'Ep02' in e.source_path.name]
            before = records(Path(raw))
            with self.assertRaises((ValueError, RuntimeError)):
                service.adjust_subtitles(plan.task_id, PlanStore.fingerprint(plan), ids, main_language='en')
            self.assertEqual(before, records(Path(raw)))

    async def test_states_save_failure_reload_and_video_hint(self):
        from src.rename.subtitle_batch import subtitle_statuses
        async with self.flow(automatic=False, configure_source=pending_package) as flow:
            plan, _ = await self.selected(flow)
            chosen = next(e for e in subtitle_edit_view(plan).entries if e.subtitle)
            self.assertEqual('待修正', subtitle_statuses(plan)[chosen.source_path][0])
            result = flow.service.adjust_subtitles(plan.task_id, PlanStore.fingerprint(plan),
                [entry_id(chosen)], main_language='zh-Hans')
            self.assertEqual('已修正', subtitle_statuses(result)[chosen.source_path][0])
            self.assertEqual(subtitle_statuses(result), subtitle_statuses(flow.store.load(plan.task_id)))
            before = records(flow.root)
            with self.assertRaises((ValueError, RuntimeError)):
                flow.service.adjust_subtitles(plan.task_id, 'stale', [entry_id(chosen)], main_language='en')
            self.assertEqual(before, records(flow.root))
            self.assertEqual('已修正', subtitle_statuses(flow.store.load(plan.task_id))[chosen.source_path][0])
        with tempfile.TemporaryDirectory() as raw:
            _, normal = series(Path(raw), initialize_content=False)
            entry = next(e for e in normal.entries if e.subtitle)
            self.assertEqual(('无需修正', ''), subtitle_statuses(normal)[entry.source_path])
            video = next(e for e in normal.entries if e.source_path == entry.subtitle.associated_video_source_path)
            bad_video = replace(video, presentation_asset=replace(video.presentation_asset,
                asset_role=replace(video.presentation_asset.asset_role, kind='unknown')))
            pending = replace(normal, entries=tuple(bad_video if e == video else e for e in normal.entries))
            self.assertEqual(('无需修正', '关联视频待确认'), subtitle_statuses(pending)[entry.source_path])
            missing = replace(pending, entries=tuple(e for e in pending.entries if e != bad_video))
            self.assertEqual(('无需修正', '对应视频无法确认，需核对源文件'), subtitle_statuses(missing)[entry.source_path])
            from src.rename.subtitle_labels import DimensionResolution
            conflict = replace(entry, subtitle=replace(entry.subtitle,
                associated_video_source_path=None, association_state=DimensionResolution.CONFLICT))
            ambiguous = replace(normal, entries=tuple(conflict if e == entry else e for e in normal.entries))
            self.assertEqual(('无需修正', '对应视频无法确认，需核对源文件'), subtitle_statuses(ambiguous)[entry.source_path])

    async def test_duplicate_status_marks_both_and_invalid_saved_rule_is_not_corrected(self):
        from src.rename.subtitle_batch import subtitle_statuses
        with tempfile.TemporaryDirectory() as raw:
            service, plan = series(Path(raw), initialize_content=False)
            subs = [e for e in plan.entries if e.subtitle and 'Ep02' in e.source_path.name]
            altered = replace(subs[1], subtitle=replace(subs[1].subtitle,
                primary_language=subs[0].subtitle.primary_language, script=subs[0].subtitle.script),
                target_path=subs[0].target_path)
            conflict = replace(plan, entries=tuple(altered if e == subs[1] else e for e in plan.entries))
            self.assertTrue(all(subtitle_statuses(conflict)[e.source_path][0] == '待修正' for e in subs))
            selected = next(e for e in plan.entries if e.subtitle and 'Ep01' in e.source_path.name)
            saved = service.adjust_subtitles(plan.task_id, PlanStore.fingerprint(plan), [entry_id(selected)], main_language='ja')
            broken = replace(saved, subtitle_override_rules=())
            self.assertEqual('待修正', subtitle_statuses(broken)[selected.source_path][0])


    async def test_default_conflict_does_not_mark_third_track_and_series_edit_is_corrected(self):
        from src.rename.subtitle_batch import subtitle_statuses
        from src.rename.rename_plan import SubtitleOverrideScope
        from tests.test_content_subtitle_lifecycle import apply
        with tempfile.TemporaryDirectory() as raw:
            service, plan = series(Path(raw), initialize_content=False)
            subs = [e for e in plan.entries if e.subtitle and 'Ep02' in e.source_path.name]
            altered = [replace(e, subtitle=replace(e.subtitle, flags=('default',)),
                target_path=e.target_path.with_name(e.target_path.stem+'.default.ass')) for e in subs]
            third = replace(subs[0], source_path=subs[0].source_path.with_name('Ep02.en.ass'),
                target_path=subs[0].target_path.with_name('Show S01E02.en.ass'),
                subtitle=replace(subs[0].subtitle, raw_label='en', primary_language='en', script=None))
            probe = replace(plan, entries=tuple(next((a for a in altered if a.source_path == e.source_path), e)
                for e in plan.entries)+(third,), expected_sources=plan.expected_sources+(third.source_path,))
            states = subtitle_statuses(probe)
            self.assertEqual(['待修正', '待修正', '无需修正'], [states[e.source_path][0] for e in (*altered, third)])
            first = next(e for e in plan.entries if e.subtitle and 'Ep01' in e.source_path.name)
            saved = apply(service, plan, first.source_path, scope=SubtitleOverrideScope.SERIES)
            edited = [e for e in saved.entries if e.subtitle and e.subtitle.decision_source == 'manual_series']
            self.assertTrue(edited)
            self.assertTrue(all(subtitle_statuses(saved)[e.source_path][0] == '已修正' for e in edited))

    async def test_inherited_manual_edit_uses_existing_proof(self):
        from src.rename.subtitle_batch import subtitle_statuses
        from tests.test_inherited_subtitle_edits_contract import InheritedSubtitleCodecContracts
        from tests.test_partition_subtitle_history_contract import package
        helper = InheritedSubtitleCodecContracts('runTest')
        async with helper.flow(automatic=False, configure_source=package) as flow:
            edited = helper.edit(flow, helper.current(flow)[0])
            probe, choice = await helper.frozen_codec_input(flow, edited)
            transferred = [e for e in probe.entries if e.subtitle
                and e.subtitle.decision_source == 'manual_transfer']
            self.assertTrue(transferred)
            self.assertEqual((), PlanVerifier().verify_subtitle_rule_materials(probe))
            for e in transferred:
                self.assertEqual('已修正', subtitle_statuses(probe)[e.source_path][0],
                    [(i.code, str(i.source_path), i.message) for i in PlanVerifier().verify(probe, validate_projection_receipt=False).issues if i.source_path in (e.source_path, None)])
            broken = replace(probe, entries=tuple(replace(e, subtitle=replace(e.subtitle,
                inherited_edit_fingerprint='0'*64)) if e in transferred else e for e in probe.entries))
            for e in transferred:
                self.assertEqual('待修正', subtitle_statuses(broken)[e.source_path][0])


    async def test_rule_schema_error_marks_only_its_consumers(self):
        from src.rename.subtitle_batch import subtitle_statuses
        with tempfile.TemporaryDirectory() as raw:
            service, plan = series(Path(raw), initialize_content=False)
            subs=[e for e in plan.entries if e.subtitle]
            first, second=subs[:2]
            saved=service.adjust_subtitles(plan.task_id, PlanStore.fingerprint(plan), [entry_id(first)], main_language='ja')
            saved=service.adjust_subtitles(plan.task_id, PlanStore.fingerprint(saved), [entry_id(second)], main_language='en')
            rules=saved.subtitle_override_rules
            broken=replace(saved, subtitle_override_rules=(replace(rules[0],schema_version=0),*rules[1:]))
            self.assertTrue(any(i.code=='subtitle_rule_schema_stale' and i.source_path is None
                                for i in PlanVerifier().verify_subtitle_rule_materials(broken)))
            states=subtitle_statuses(broken)
            self.assertEqual('待修正',states[first.source_path][0])
            self.assertEqual('已修正',states[second.source_path][0])
            self.assertEqual('无需修正',states[subs[-1].source_path][0])


class ReadinessGui(unittest.IsolatedAsyncioTestCase):
    async def test_pending_video_save_updates_rows_without_closing_and_reopens(self):
        from unittest.mock import patch
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        helper = Readiness('runTest')
        def with_special_subtitle(root, source):
            pending_package(root, source)
            (source/'Ordinary Show [SP01].zh-sc.ass').write_text('[Script Info]\nTitle: Sample\n', encoding='utf-8')
            return source
        async with helper.flow(automatic=False, configure_source=with_special_subtitle) as flow:
            plan, _ = await helper.selected(flow)
            with patch('src.pages.season_reference_view.load', return_value=None), \
                    patch('src.pages.season_reference_view.prepare'):
                holder = ContentWorkbenchGui('runTest')
                holder.create_page(prepared=(flow.root, flow.service, plan, []), cleanup_owner=self)
                await holder.driver.click('调整字幕结构')
                editor = holder.view._subtitle_editor
                key=next(k for k,e in editor.entries.items() if 'S01E01' in e.source_path.name)
                special=next(k for k,e in editor.entries.items() if '[SP01]' in e.source_path.name)
                def row_labels(k):
                    return [n for n in editor.panes['files']['checks'][k].parent_slot.parent.descendants()
                            if getattr(n,'text','') and 'bar-help' in n.classes]
                def labels():
                    return [n.text for n in row_labels(key)]
                def assert_hints():
                    self.assertEqual(['待修正 · 关联视频待确认'], [n.text for n in row_labels(special)])
                    self.assertFalse(any('关联视频' in n.text or '核对源文件' in n.text for n in row_labels(key)))
                    self.assertTrue(all(n.props.get('color') == 'blue-7' for n in editor.panes['files']['checks'].values()))
                    self.assertTrue(any('个文件' in getattr(n, 'text', '') for n in editor.panes['tags']['container'].descendants()))
                assert_hints()
                self.assertEqual(['待修正'], labels())
                editor.set_selection([key])
                editor.main.value = 'zh-Hans'
                editor.has_additional.value = False
                await holder.driver.click('保存字幕调整')
                self.assertTrue(editor.dialog.value)
                self.assertEqual('所选字幕调整已保存', editor.feedback.text)
                self.assertIn('已修正', labels())
                assert_hints()
                self.assertIn(key, editor.selected)
                before = labels()
                with patch.object(flow.service, 'adjust_subtitles', side_effect=ValueError('定向保存失败')):
                    await editor.save()
                self.assertTrue(editor.feedback.text.startswith('未保存'))
                self.assertEqual(before, labels())
                editor.dialog.close()
                await holder.driver.click('调整字幕结构')
                editor = holder.view._subtitle_editor
                self.assertTrue(editor.dialog.value)
                self.assertIn('已修正', labels())
                assert_hints()


    async def test_missing_video_hint_is_under_its_own_checkbox(self):
        from tests.test_subtitle_editor_contract import EditorGui
        helper=EditorGui('runTest')
        try:
            holder,editor=await helper.prepare()
            missing=next(k for k,e in editor.entries.items() if e.subtitle.associated_video_source_path is None)
            normal=next(k for k,e in editor.entries.items() if e.subtitle.associated_video_source_path is not None)
            def hints(k):
                return [n.text for n in editor.panes['files']['checks'][k].parent_slot.parent.descendants()
                        if getattr(n,'text','') and 'bar-help' in n.classes]
            self.assertTrue(any('对应视频无法确认，需核对源文件' in t for t in hints(missing)))
            self.assertFalse(any('核对源文件' in t for t in hints(normal)))
        finally:
            helper.doCleanups()


    async def test_normal_rows_stay_normal_after_another_row_is_saved(self):
        from tests.test_subtitle_editor_contract import EditorGui
        helper=EditorGui('runTest')
        try:
            holder,editor=await helper.prepare(tv=True)
            keys=list(editor.entries)
            def states():
                return {k:[n.text for n in editor.panes['files']['checks'][k].parent_slot.parent.descendants()
                           if getattr(n,'text','') and 'bar-help' in n.classes] for k in keys}
            self.assertEqual({k:['无需修正'] for k in keys},states())
            editor.set_selection([keys[0]])
            editor.main.value='ja'
            editor.has_additional.value=False
            await holder.driver.click_element(editor.save_button)
            expected={k:['已修正' if k==keys[0] else '无需修正'] for k in keys}
            self.assertEqual(expected,states())
            editor.dialog.close()
            await holder.driver.click('调整字幕结构')
            editor=holder.view._subtitle_editor
            self.assertEqual(expected,states())
        finally:
            helper.doCleanups()
