"""人工合并集与未完成字幕的局部保存合同。"""
import unittest

from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from tests import test_partition_content_contract as partition
from tests.test_content_choice_contract import change
from tests.source_collection_fixture import admit_task
def pending_package(root, source):
    video = next(source.glob('*S01E01*.mkv'))
    video.with_suffix('.zh-sc.ass').write_text('[Script Info]\nTitle: Sample\n', encoding='utf-8')
    (source / 'Ordinary Show [SP01].mkv').write_bytes(video.read_bytes())
    return source


def merged_package(root, source):
    sample = next(source.glob('*S01E01*.mkv')).read_bytes()
    video = source / 'Ordinary Show [03-05][1080p].mkv'
    video.write_bytes(sample)
    video.with_suffix('.en.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nTest\n', encoding='utf-8')
    return source


class EpisodeRangeContracts(unittest.IsolatedAsyncioTestCase):
    flow = partition.PartitionContentContracts.flow
    current = partition.PartitionContentContracts.current
    selected = partition.PartitionContentContracts.selected

    async def test_manual_range_updates_video_subtitle_and_survives_reload(self):
        async with self.flow(automatic=False, configure_source=merged_package) as flow:
            plan, state = await self.selected(flow)
            self.assertTrue(state.confirmed_work)
            excluded = next(e.source_path for e in plan.entries if e.media_kind == 'video' and 'S01E02' in e.source_path.name)
            plan = change(flow.service, plan, [excluded])
            state = flow.service.decision_state_store.load(plan.task_id)
            old_excluded = state.content_selection.to_dict()['excluded_sources']
            self.assertIn(str(excluded), old_excluded)
            chosen = next(e for e in plan.entries if '[03-05]' in e.source_path.name and e.media_kind == 'video')
            revised = flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, mode='range', edit=dict(entry_id=entry_id(chosen),
                    season=1, start=3, end=5, naming_mode='tmdb'))
            video = next(e for e in revised.entries if e.source_path == chosen.source_path)
            subtitle = next(e for e in revised.entries if e.source_path == chosen.source_path.with_suffix('.en.srt'))
            self.assertEqual((1, 3, 5), (video.season_id, video.episode_id, video.episode_end))
            self.assertIn('S01E03-E05', video.target_path.name)
            self.assertEqual((1, 3, 5), (subtitle.season_id, subtitle.episode_id, subtitle.episode_end))
            self.assertTrue(subtitle.target_path.name.startswith(video.target_path.stem + '.'))
            self.assertEqual(plan.ignored_sources, revised.ignored_sources)
            saved = flow.service.decision_state_store.load(plan.task_id)
            self.assertEqual(old_excluded, saved.content_selection.to_dict()['excluded_sources'])
            self.assertNotIn(excluded, {e.source_path for e in revised.entries})
            self.assertIn(excluded, revised.ignored_sources)
            self.assertEqual(PlanStore.fingerprint(revised), PlanStore.fingerprint(flow.store.load(plan.task_id)))
            self.assertFalse(flow.service.load_or_create(revised).approved_entry_ids)

    async def test_subtitle_errors_do_not_block_video_role_save(self):
        async with self.flow(automatic=False, configure_source=pending_package) as flow:
            plan, state = await self.selected(flow)
            self.assertTrue(any(i.code == 'subtitle_parse_unresolved' for i in PlanVerifier().verify(plan).issues))
            chosen = next(e for e in plan.entries if '[SP01]' in e.source_path.name and e.media_kind == 'video')
            old_rules = plan.subtitle_override_rules
            revised = flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, mode='role', edit=dict(entry_id=entry_id(chosen), role='extra'))
            saved = flow.service.decision_state_store.load(plan.task_id)
            self.assertEqual('extra', saved.manual_role_decisions[entry_id(chosen)])
            self.assertEqual(old_rules, revised.subtitle_override_rules)
            self.assertTrue(any(i.code == 'subtitle_parse_unresolved' for i in PlanVerifier().verify(revised).issues))
            with self.assertRaisesRegex((ValueError, RuntimeError), 'subtitle_parse_unresolved'):
                flow.service.approve_entries(plan.task_id, PlanStore.fingerprint(revised), [entry_id(e) for e in revised.entries])
            self.assertFalse(flow.service.load_or_create(revised).approved_entry_ids)

    async def test_existing_single_episode_is_unchanged(self):
        async with self.flow(automatic=False) as flow:
            plan, state = await self.selected(flow)
            self.assertTrue(state.confirmed_work)
            video = next(e for e in plan.entries if e.media_kind == 'video' and 'S01E01' in e.source_path.name)
            self.assertIn('S01E01', video.target_path.name)
            self.assertNotIn('episode_end', video.to_dict())
            self.assertNotIn('episode_ranges', state.to_dict())
            self.assertEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(flow.store.load(plan.task_id)))


    async def test_overlap_and_tampered_range_remain_blocked(self):
        from dataclasses import replace
        from src.rename.series_asset_scope import logical_episodes
        async with self.flow(automatic=False, configure_source=merged_package) as flow:
            plan, state = await self.selected(flow)
            chosen = next(e for e in plan.entries if '[03-05]' in e.source_path.name and e.media_kind == 'video')
            revised = flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, mode='range', edit=dict(entry_id=entry_id(chosen),
                    season=1, start=1, end=3, naming_mode='tmdb'))
            self.assertIn('duplicate_video_episode', {i.code for i in PlanVerifier().verify(revised).issues})
            entry = next(e for e in revised.entries if e.source_path == chosen.source_path)
            self.assertEqual({(1,1),(1,2),(1,3)}, logical_episodes([entry]))
            tampered = replace(revised, entries=tuple(replace(e,episode_end=8) if e == entry else e for e in revised.entries))
            self.assertIn('episode_range_invalid', {i.code for i in PlanVerifier().verify(tampered, validate_projection_receipt=False).issues})

    async def test_bangumi_nfo_and_tmdb_without_nfo(self):
        from xml.etree.ElementTree import fromstring
        from src.rename.generated_outputs import validate_manifest
        async with self.flow(automatic=False, configure_source=merged_package) as flow:
            plan, state = await self.selected(flow)
            chosen = next(e for e in plan.entries if '[03-05]' in e.source_path.name and e.media_kind == 'video')
            for mode in ('bangumi', 'tmdb'):
                state = flow.service.decision_state_store.load(plan.task_id)
                plan = flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                    expected_revision=state.revision, mode='range', edit=dict(entry_id=entry_id(chosen),
                        season=1, start=3, end=5, naming_mode=mode))
                rows = [r for r in plan.metadata.get('generated_outputs',{}).get('files',()) if r['kind']=='bangumi_episode_nfo']
                self.assertEqual(mode=='bangumi', bool(rows))
                validate_manifest(plan)
                if rows:
                    xml=fromstring(rows[0]['content'])
                    self.assertEqual(('1','3','5'), tuple(xml.findtext(k) for k in ('season','episode','episodenumberend')))
                    self.assertFalse(xml.findall('uniqueid'))

    async def test_projection_disconnect_and_invalid_input_keep_original(self):
        from unittest.mock import patch
        from tests.test_partition_content_contract import records
        async with self.flow(automatic=False, configure_source=merged_package) as flow:
            plan, state = await self.selected(flow)
            chosen = next(e for e in plan.entries if '[03-05]' in e.source_path.name and e.media_kind == 'video')
            before = records(flow.root)
            def save(start=3,end=5):
                return flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                    expected_revision=state.revision, mode='range', edit=dict(entry_id=entry_id(chosen),
                        season=1, start=start, end=end, naming_mode='tmdb'))
            with patch('src.rename.episode_range.project_entry', side_effect=lambda e,*args:e):
                with self.assertRaisesRegex(ValueError,'连续集数'):
                    save()
            self.assertEqual(before, records(flow.root))
            for start,end in ((-1,5),(5,3),(True,3)):
                with self.assertRaisesRegex(ValueError,'起止集数'):
                    save(start,end)
                self.assertEqual(before, records(flow.root))

    async def test_range_real_approval_execution_and_catalog(self):
        from types import SimpleNamespace
        from tests import _media_flow_helpers as tv
        from src.rename.series_asset_scope import logical_episodes
        from src.rename.generated_outputs import validate_registered, validate_completed
        from src.rename.special_catalog import plain
        async with self.flow(automatic=False, configure_source=merged_package) as flow:
            plan, state = await self.selected(flow)
            chosen = next(e for e in plan.entries if '[03-05]' in e.source_path.name and e.media_kind == 'video')
            plan = flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, mode='range', edit=dict(entry_id=entry_id(chosen),
                    season=1, start=3, end=5, naming_mode='bangumi'))
            result = SimpleNamespace(plan=plan)
            h = tv.MediaFlow('runTest')
            task, ledger, service = await h._approve_and_execute(root=flow.root, result=result)
            self.assertEqual('completed', task.get('execution_status'), task.get('error'))
            self.assertEqual('completed', ledger.get('status'))
            validate_completed(result.plan, ledger)
            import json, os
            payload = json.loads(service.series_catalog._path(result.plan.metadata['series_key']).read_text('utf-8'))
            validate_registered(payload)
            self.assertEqual((1,3,5), tuple(payload['generated_outputs'][0][k] for k in ('season','local_episode','episode_end')))
            self.assertEqual({(1,n) for n in range(1,6)}, logical_episodes(result.plan.entries))
            for e in result.plan.entries:
                self.assertTrue(os.path.samefile(e.source_path,e.target_path))
            self.assertTrue(chosen.source_path.exists())

    async def test_range_replacement_keeps_subtitles_nfo_and_other_season(self):
        from types import SimpleNamespace
        from tests import _media_flow_helpers as tv
        from pathlib import Path
        import json, os
        async with self.flow(automatic=False, configure_source=merged_package) as flow:
            plan,state=await self.selected(flow)
            chosen=next(e for e in plan.entries if e.media_kind=='video' and '[03-05]' in e.source_path.name)
            plan=flow.service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=state.revision,
                mode='range',edit=dict(entry_id=entry_id(chosen),season=1,start=3,end=5,naming_mode='bangumi'))
            h=tv.MediaFlow('runTest'); old=SimpleNamespace(plan=plan)
            task,ledger,service=await h._approve_and_execute(root=flow.root,result=old)
            self.assertEqual('completed',task.get('execution_status'),task.get('error'))
            tv.resume_fonts(flow.root,old.plan.task_id)
            old_video=next(e.target_path for e in old.plan.entries if e.source_path==chosen.source_path)
            old_nfo=old_video.with_suffix('.nfo')
            other=Path(plan.metadata['canonical_work_root'])/'Season 02'/'keep.txt'
            other.parent.mkdir(); other.write_text('other season',encoding='utf-8')
            incoming=flow.root/'second-source';incoming.mkdir()
            for n in (1,2,3):
                (incoming/f'Ordinary Show S01E{n:02d} [1080p].mkv').write_bytes(chosen.source_path.read_bytes())
            merged=incoming/'Ordinary Show [04-05][1080p].mkv'
            merged.write_bytes(chosen.source_path.read_bytes())
            merged.with_suffix('.en.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nTest\n',encoding='utf-8')
            flow.task_id='range-replacement';flow.source=incoming
            await admit_task(flow.manager, incoming,task_id=flow.task_id,is_anime=False,is_movie=False,custom_name='Ordinary Show',custom_season_id=1)
            import asyncio
            await asyncio.wait_for(flow.manager._queue.join(),20)
            plan,state=await self.selected(flow)
            for token,start,end in (('S01E03',3,3),('[04-05]',4,5)):
                chosen2=next(e for e in plan.entries if e.media_kind=='video' and token in e.source_path.name)
                state=flow.service.decision_state_store.load(plan.task_id)
                plan=flow.service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=state.revision,
                    mode='range',edit=dict(entry_id=entry_id(chosen2),season=1,start=start,end=end,naming_mode='bangumi'))
            self.assertTrue(old_nfo.exists())
            op,_=flow.service.asset_actions().prepare_replacement(plan)
            self.assertIn(str(old_nfo),{r['target'] for r in op.to_dict()['retirements']})
            self.assertNotIn(str(other),{r['target'] for r in op.to_dict()['retirements']})
            plan=flow.service.confirm_asset_replacement(plan.task_id,PlanStore.fingerprint(plan),expected_operation_fingerprint=op.fingerprint)
            current=SimpleNamespace(plan=plan)
            task,ledger,service=await h._approve_and_execute(root=flow.root,result=current)
            self.assertEqual('completed',task.get('execution_status'),task.get('error'))
            self.assertFalse(old_video.exists());self.assertFalse(old_nfo.exists())
            self.assertEqual('other season',other.read_text('utf-8'))
            for e in current.plan.entries:self.assertTrue(os.path.samefile(e.source_path,e.target_path))
            payload=json.loads(service.series_catalog._path(plan.metadata['series_key']).read_text('utf-8'))
            self.assertNotIn(str(old_nfo),{r['target'] for r in payload['generated_outputs']})
            self.assertEqual({3,4},{r['local_episode'] for r in payload['generated_outputs']})

    async def test_original_range_task_repairs_missing_video_and_nfo(self):
        from types import SimpleNamespace
        from tests import _media_flow_helpers as tv
        import os
        async with self.flow(automatic=False, configure_source=merged_package) as flow:
            plan,state=await self.selected(flow)
            chosen=next(e for e in plan.entries if e.media_kind=='video' and '[03-05]' in e.source_path.name)
            plan=flow.service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=state.revision,
                mode='range',edit=dict(entry_id=entry_id(chosen),season=1,start=3,end=5,naming_mode='bangumi'))
            h=tv.MediaFlow('runTest'); result=SimpleNamespace(plan=plan)
            task,ledger,service=await h._approve_and_execute(root=flow.root,result=result)
            self.assertEqual('completed',task.get('execution_status'),task.get('error'))
            tv.resume_fonts(flow.root,plan.task_id)
            video=next(e for e in result.plan.entries if e.source_path==chosen.source_path)
            nfo=video.target_path.with_suffix('.nfo'); nfo_bytes=nfo.read_bytes()
            # 移到隔离保留目录模拟目标缺失，不删除测试对象。
            retained=flow.root/'missing-retained';retained.mkdir()
            video.target_path.rename(retained/video.target_path.name)
            nfo.rename(retained/nfo.name)
            plan=flow.store.load(plan.task_id)
            from tests.test_completed_rebuild_contract import recovery
            rebuild=recovery(flow.root)
            observed=rebuild.inspect(plan.task_id)
            plan=rebuild.generate(plan.task_id,expected_fingerprint=observed.fingerprint)
            self.assertEqual('default-activation',plan.task_id)
            result.plan=plan
            task,ledger,service=await h._approve_and_execute(root=flow.root,result=result)
            self.assertEqual('completed',task.get('execution_status'),task.get('error'))
            self.assertTrue(os.path.samefile(video.source_path,video.target_path))
            self.assertEqual(nfo_bytes,nfo.read_bytes())
            self.assertEqual(1,len([e for e in result.plan.entries if e.source_path==chosen.source_path]))

    async def test_gui_range_save_keeps_dialog_and_refreshes_parent(self):
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        from src.pages.episode_range_editor import open_episode_range_editor
        async with self.flow(automatic=False, configure_source=merged_package) as flow:
            plan,state=await self.selected(flow)
            h=ContentWorkbenchGui('runTest')
            h.create_page(prepared=(flow.root,flow.service,plan,[]),cleanup_owner=self)
            with h.client:
                open_episode_range_editor(h.view)
                editor=h.view._episode_range_editor
                video=next(e for e in h.view.plan.entries if e.media_kind=='video' and '[03-05]' in e.source_path.name)
                editor.video.set_value(entry_id(video));editor.selected()
                editor.season.set_value(1);editor.start.set_value(3);editor.end.set_value(5)
                await editor.save()
                self.assertTrue(editor.dialog.value)
                self.assertEqual('连续集数已保存，可继续调整',editor.feedback.text)
                revised=next(e for e in h.view.plan.entries if e.source_path==video.source_path)
                self.assertEqual(5,revised.episode_end)
                self.assertTrue(h.view.value)

    async def test_jellyfin_range_output_samples(self):
        import json
        from src.rename.generated_outputs import expected_files
        async with self.flow(automatic=False, configure_source=merged_package) as flow:
            plan,state=await self.selected(flow)
            others=[e.source_path for e in plan.entries if e.media_kind=='video' and '[03-05]' not in e.source_path.name]
            plan=change(flow.service,plan,others)
            samples=[]
            for end in (9,12):
                state=flow.service.decision_state_store.load(plan.task_id)
                chosen=next(e for e in plan.entries if e.media_kind=='video')
                plan=flow.service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=state.revision,
                    mode='range',edit=dict(entry_id=entry_id(chosen),season=1,start=1,end=end,naming_mode='bangumi'))
                samples.append(dict(end=end,files=expected_files(plan)))
            (flow.root/'jellyfin-range-outputs.json').write_text(json.dumps(samples,ensure_ascii=False),encoding='utf-8')
            self.assertEqual([9,12],[r['files'][0]['episode_end'] for r in samples])

    async def test_range_inheritance_guards_and_manual_season(self):
        from src.rename.work_correction import observe
        from src.rename.source_regrouping_creation import create
        from src.rename.series_repair import selected_previous
        from types import SimpleNamespace
        from dataclasses import replace
        async with self.flow(automatic=False, configure_source=merged_package) as flow:
            plan,state=await self.selected(flow)
            preview=flow.manager.preview_source_regrouping(task_id=plan.task_id,expected_plan_fingerprint=PlanStore.fingerprint(plan))
            chosen=next(e for e in plan.entries if e.media_kind=='video' and '[03-05]' in e.source_path.name)
            plan=flow.service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=state.revision,
                mode='range',edit=dict(entry_id=entry_id(chosen),season=1,start=3,end=5,naming_mode='tmdb'))
            state=flow.service.decision_state_store.load(plan.task_id)
            before=partition.records(flow.root)
            with self.assertRaisesRegex((ValueError,RuntimeError),'连续集数'):
                flow.manager.preview_source_regrouping(task_id=plan.task_id,expected_plan_fingerprint=PlanStore.fingerprint(plan))
            with self.assertRaises((ValueError,RuntimeError)):
                create(flow.manager,preview)
            with self.assertRaisesRegex((ValueError,RuntimeError),'连续集数'):
                observe(flow.service,plan.task_id,PlanStore.fingerprint(plan),expected_revision=state.revision,
                    candidate=dict(namespace='tmdb',entity_type='tv',provider_id='9999'))
            from src.rename.recognition_queue_admission import capture_workspace_open
            opened=capture_workspace_open(flow.manager,plan.task_id,flow.manager._read(plan.task_id))
            with self.assertRaisesRegex((ValueError,RuntimeError),'连续集数'):
                await flow.manager.reenqueue_workspace(plan.task_id,flow.source,is_anime=False,is_movie=False,
                    custom_name='Ordinary Show',custom_season_id=1,preview_only=False,opened_evidence=opened)
            self.assertEqual(before,partition.records(flow.root))
            ranged=next(e for e in plan.entries if e.source_path==chosen.source_path)
            partial=replace(plan,entries=(replace(ranged,episode_id=4,episode_end=None),))
            context=SimpleNamespace(_history=lambda *_:(plan,{},''))
            rows=[dict(task_id=plan.task_id,plan_fingerprint=PlanStore.fingerprint(plan),source_path=str(ranged.source_path),
                target_path=str(ranged.target_path),target_evidence=None)]
            with self.assertRaisesRegex(ValueError,'完整补齐'):
                selected_previous(context,partial,rows)
            revised=flow.service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=state.revision,
                mode='season',edit=dict(season_number=2))
            range_video=next(e for e in revised.entries if e.source_path==chosen.source_path)
            self.assertEqual((2,3,5),(range_video.season_id,range_video.episode_id,range_video.episode_end))
            self.assertEqual(2,flow.service.decision_state_store.load(plan.task_id).episode_ranges[0].season)

    async def test_manual_range_season_registers_the_selected_season(self):
        from types import SimpleNamespace
        from tests import _media_flow_helpers as tv
        from src.rename.series_asset_scope import logical_episodes
        async with self.flow(automatic=False, configure_source=merged_package) as flow:
            plan, state = await self.selected(flow)
            chosen = next(e for e in plan.entries if e.media_kind == 'video' and '[03-05]' in e.source_path.name)
            before = partition.records(flow.root)
            with self.assertRaisesRegex(ValueError, '还有其他视频'):
                flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                    expected_revision=state.revision, mode='range', edit=dict(entry_id=entry_id(chosen),
                        season=2, start=3, end=5, naming_mode='bangumi'))
            self.assertEqual(before, partition.records(flow.root))
            others = [e.source_path for e in plan.entries if e.media_kind == 'video' and e != chosen]
            plan = change(flow.service, plan, others)
            state = flow.service.decision_state_store.load(plan.task_id)
            plan = flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, mode='range', edit=dict(entry_id=entry_id(chosen),
                    season=2, start=3, end=5, naming_mode='bangumi'))
            self.assertEqual(2, plan.metadata['season_number'])
            self.assertEqual(2, flow.service.decision_state_store.load(plan.task_id).season_binding['season_number'])
            result = SimpleNamespace(plan=plan)
            helper = tv.MediaFlow('runTest')
            task, ledger, service = await helper._approve_and_execute(root=flow.root, result=result)
            self.assertEqual('completed', task.get('execution_status'), task.get('error'))
            self.assertEqual({(2,3),(2,4),(2,5)}, logical_episodes(result.plan.entries))
            import json
            catalog=json.loads(service.series_catalog._path(result.plan.metadata['series_key']).read_text('utf-8'))
            self.assertEqual({2}, {r['season_number'] for r in catalog['season_bindings']})
            self.assertEqual({2}, {r['season'] for r in catalog['generated_outputs']})
