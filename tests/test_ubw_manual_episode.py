"""UBW 单卡人工正片、零集号与原窗口修订合同。"""
import logging
from pathlib import Path
import unittest
from src.rename.rename_plan import PlanEntry, PresentationAssetPlanContract, AssetRole, build_default_version_descriptor
from src.rename.content_policy import unresolved_video_reason
from src.rename.content_selection import canonical_episode_target
from src.rename.tv_video_projection import TVVideoInput, project_tv_videos
from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from src.rename.plan_verifier import PlanVerifier
from tests import test_episode_range_contract as fixtures

class ManualEpisodeContracts(unittest.IsolatedAsyncioTestCase):
    def test_complete_first_card_video_sequence_keeps_zero(self):
        root=Path.cwd()/'isolated-ubw-source'
        videos=tuple(PlanEntry(root/f'[VCB-Studio] Fate stay night Unlimited Blade Works [{n:02}][Ma10p_1080p][x265_flac].mkv',root/'draft',media_kind='video') for n in range(13))
        result=project_tv_videos(TVVideoInput(videos,root,'Fate/stay night [Unlimited Blade Works]','g',Path.cwd()/'UBW',1,season_source='explicit_user'))
        self.assertEqual(list(range(13)),[r.episode_id for r in result.mappings])
        self.assertFalse(result.unresolved)
        self.assertIn('S01E00',result.mappings[0].target.name)

    def test_zero_content_and_negative_numbers(self):
        from src.rename.episode_range import numbers
        for n in (0,1):
            e=PlanEntry(Path.cwd()/'video.mkv',Path.cwd()/f'S01E{n:02}.mkv',season_id=1,episode_id=n,media_kind='video',role='main_episode',presentation_asset=PresentationAssetPlanContract('g',AssetRole('content_version'),None,build_default_version_descriptor('g',1,n)))
            self.assertIsNone(unresolved_video_reason(e,movie=False))
            self.assertEqual((1,n,n),numbers(1,n,n))
        for n in (-1,True,1.5):
            with self.assertRaises(ValueError):canonical_episode_target(Path.cwd(),1,n,'.mkv')

    def test_negative_guards_reject_invalid_numbers(self):
        from src.rename.episode_range import numbers
        from dataclasses import replace
        entry=PlanEntry(Path.cwd()/'video.mkv',Path.cwd()/'S01E01.mkv',season_id=1,episode_id=1,media_kind='video',role='main_episode',presentation_asset=PresentationAssetPlanContract('g',AssetRole('content_version'),None,build_default_version_descriptor('g',1,1)))
        for season,start,end in ((1,-1,1),(-1,1,1),(1,True,1),(1,1.5,2),(1,1,False)):
            with self.subTest(values=(season,start,end)),self.assertRaises(ValueError):
                numbers(season,start,end)
        for value in (-1,True,1.5):
            self.assertIsNotNone(unresolved_video_reason(replace(entry,episode_id=value),movie=False))
        self.assertIsNotNone(unresolved_video_reason(replace(entry,season_id=-1),movie=False))

    async def test_zero_save_reload_and_collision(self):
        fixture=fixtures.EpisodeRangeContracts('runTest')
        async with fixture.flow(automatic=False) as flow:
            plan,state=await fixture.selected(flow)
            videos=[e for e in plan.entries if e.media_kind=='video']
            chosen=videos[0]
            edit=dict(entry_id=entry_id(chosen),season=1,start=0,end=0,naming_mode='tmdb')
            changed=flow.service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=state.revision,mode='range',edit=edit)
            state=flow.service.decision_state_store.load(plan.task_id)
            saved=flow.store.load(plan.task_id)
            self.assertEqual(PlanStore.fingerprint(changed),PlanStore.fingerprint(saved))
            self.assertEqual(0,state.episode_ranges[0].start)
            self.assertNotIn('invalid_episode',{i.code for i in PlanVerifier().verify(saved).issues})
            old=PlanStore.fingerprint(saved)
            with self.assertRaises((ValueError,RuntimeError)):
                flow.service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=state.revision-1,mode='range',edit=edit)
            self.assertEqual(old,PlanStore.fingerprint(flow.store.load(plan.task_id)))
            other=next(e for e in videos if e.source_path!=chosen.source_path)
            edit['entry_id']=entry_id(other)
            duplicate=flow.service.revise_tv_content(plan.task_id,old,expected_revision=state.revision,mode='range',edit=edit)
            self.assertIn('duplicate_video_episode',{i.code for i in PlanVerifier().verify(duplicate).issues})

    async def test_multiple_manual_numbers_single_transaction(self):
        fixture=fixtures.EpisodeRangeContracts('runTest')
        async with fixture.flow(automatic=False,configure_source=fixtures.pending_package) as flow:
            plan,state=await fixture.selected(flow)
            videos=[e for e in plan.entries if e.media_kind=='video']
            rows=[dict(entry_id=entry_id(e),season=1,start=8-3*i,end=8-3*i,naming_mode='tmdb') for i,e in enumerate(videos)]
            result=flow.service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=state.revision,mode='range',edit={'season':1,'rows':rows})
            saved=flow.service.decision_state_store.load(plan.task_id)
            self.assertEqual(len(videos),len(saved.episode_ranges))
            self.assertEqual(state.revision+1,saved.revision)
            expected={r['entry_id']:(r['season'],r['start']) for r in rows}
            self.assertEqual(expected,{entry_id(e):(e.season_id,e.episode_id) for e in result.entries if e.media_kind=='video'})
            saved_numbers={d.source_path:(d.season,d.start,d.end) for d in saved.episode_ranges}
            self.assertEqual({str(e.source_path):(expected[entry_id(e)][0],expected[entry_id(e)][1],expected[entry_id(e)][1]) for e in videos},saved_numbers)
            self.assertFalse(result.metadata.get('smart_completion_unresolved_videos'))

    async def test_real_editor_stays_open_and_saves_zero(self):
        from src.pages.main_episode_editor import open_main_episode_editor
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        fixture=fixtures.EpisodeRangeContracts('runTest')
        async with fixture.flow(automatic=False) as flow:
            plan,state=await fixture.selected(flow)
            holder=ContentWorkbenchGui('runTest')
            holder.create_page(prepared=(flow.root,flow.service,plan,[]),cleanup_owner=self)
            with holder.client:
                open_main_episode_editor(holder.view)
                editor=holder.view._main_episode_editor
                video=next(e for e in plan.entries if e.media_kind=='video')
                editor.files.set_value([entry_id(video)])
                editor.start.set_value('0')
                await editor.save()
                self.assertTrue(editor.dialog.value)
                self.assertGreater(holder.view.task_decision.revision,state.revision)
                saved=next(e for e in holder.view.plan.entries if e.source_path==video.source_path)
                self.assertEqual((1,0,'main_episode'),(saved.season_id,saved.episode_id,saved.role))
                # 同一窗口的旧编辑快照不能把别处的成功保存误报为本次成功。
                editor.revision -= 1
                before = PlanStore.fingerprint(flow.store.load(plan.task_id))
                await editor.save()
                self.assertEqual(before, PlanStore.fingerprint(flow.store.load(plan.task_id)))
                self.assertIn('页面已经更新', editor.feedback.text)
                self.assertTrue(editor.dialog.value)

    async def test_zero_nfo_subtitles_and_manual_season_keep_same_authority(self):
        from xml.etree.ElementTree import fromstring
        from src.rename.generated_outputs import validate_manifest
        fixture=fixtures.EpisodeRangeContracts('runTest')
        async with fixture.flow(automatic=False,configure_source=fixtures.merged_package) as flow:
            plan,state=await fixture.selected(flow)
            chosen=next(e for e in plan.entries if e.media_kind=='video' and '[03-05]' in e.source_path.name)
            for mode in ('bangumi','tmdb'):
                state=flow.service.decision_state_store.load(plan.task_id)
                plan=flow.service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),
                    expected_revision=state.revision,mode='range',edit=dict(
                        entry_id=entry_id(chosen),season=1,start=0,end=0,naming_mode=mode))
                rows=[r for r in plan.metadata.get('generated_outputs',{}).get('files',()) if r['kind']=='bangumi_episode_nfo']
                self.assertEqual(mode=='bangumi',bool(rows))
                validate_manifest(plan)
                if rows:
                    xml=fromstring(rows[0]['content'])
                    self.assertEqual(('1','0'),tuple(xml.findtext(k) for k in ('season','episode')))
                subtitles=[e for e in plan.entries if e.subtitle and e.subtitle.associated_video_source_path==chosen.source_path]
                self.assertTrue(subtitles)
                self.assertTrue(all((e.season_id,e.episode_id)==(1,0) for e in subtitles))
            state=flow.service.decision_state_store.load(plan.task_id)
            plan=flow.service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),
                expected_revision=state.revision,mode='season',edit={'season_number':2})
            video=next(e for e in plan.entries if e.source_path==chosen.source_path)
            self.assertEqual((2,0),(video.season_id,video.episode_id))
            saved=flow.service.decision_state_store.load(plan.task_id)
            self.assertEqual((2,0),(saved.episode_ranges[0].season,saved.episode_ranges[0].start))

    async def test_manual_main_replaces_selected_version_only(self):
        from tests import test_episode_versions_contract as versions
        fixture=versions.EpisodeVersionsContracts('runTest')
        async with fixture.flow(automatic=False,configure_source=versions.package) as flow:
            plan,state=await fixture.selected(flow)
            plan=fixture.save(flow,plan)
            state=flow.service.decision_state_store.load(plan.task_id)
            chosen=next(e for e in plan.entries if e.media_kind=='video' and '[01]' in e.source_path.name)
            original={d.source_path:d for d in state.episode_versions}
            plan=flow.service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),
                expected_revision=state.revision,mode='range',edit={'season':1,'rows':[
                    dict(entry_id=entry_id(chosen),season=1,start=0,end=0,naming_mode='tmdb')]})
            saved=flow.service.decision_state_store.load(plan.task_id)
            self.assertEqual({k:v for k,v in original.items() if k!=str(chosen.source_path)},
                             {d.source_path:d for d in saved.episode_versions})
            video=next(e for e in plan.entries if e.source_path==chosen.source_path)
            self.assertEqual((0,'main_episode'),(video.episode_id,video.role))
            self.assertNotEqual('episode-version.v1',video.presentation_asset.version_descriptor.identity_policy)
            self.assertFalse(PlanVerifier().verify(plan).issues)

    def test_background_exception_preserves_origin(self):
        from src.log_dispatch import LogDispatch
        rows=[]
        class Sink(logging.Handler):
            def emit(self,record): rows.append(record)
        dispatch=LogDispatch([Sink()])
        try:
            raise ValueError('ubw-origin')
        except ValueError as exc:
            error=exc
            dispatch.handle(logging.LogRecord('probe',logging.ERROR,'',0,{'event':'failed','exc_info':True},(),None))
        self.assertTrue(dispatch.drain()['stopped'])
        self.assertIsInstance(rows[0].msg['exc_info'],tuple)
        self.assertIs(error,rows[0].msg['exc_info'][1])
