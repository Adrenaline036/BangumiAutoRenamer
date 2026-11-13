"""Current TV official-special incidents; use queue and registered GUI events."""
import unittest
from unittest.mock import patch, Mock
from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id, PlanReviewError
from src.rename.plan_verifier import PlanVerifier
from tests import test_initial_tv_completion_contract as tv
from tests import test_content_workbench_gui as gui
from tests.test_content_choice_contract import change, records


class OfficialSpecialIncidents(unittest.IsolatedAsyncioTestCase):
    fixture = tv.InitialTVCompletionContracts.fixture
    create_page = gui.ContentWorkbenchGui.create_page
    text = gui.ContentWorkbenchGui.text

    async def test_direct_tmdb_official_special_keeps_video_and_companions(self):
        for anime,filename in ((False,'Show [13(OVA)].mkv'),(False,'Show 2011 SP.mkv'),(True,'Show 2011 SP.mkv')):
            with self.subTest(anime=anime,filename=filename):
                root, service, _, result, extras, *_ = await self.fixture(anime=anime,
                    exceptional=True, exceptional_name=filename, companions=True)
                plan=result.plan
                self.assertIsNotNone(plan.work_identity, result.task.get('error'))
                observation=plan.metadata.get('provider_special_observation')
                self.assertIsNotNone(observation, 'official Season0 must be fetched on direct TMDB')
                self.assertEqual('completed_with_rows',next(row['state'] for row in observation['catalog'] if row['provider']['namespace']=='tmdb'))
                key=entry_id(next(e for e in plan.entries if e.source_path==extras['exceptional']))
                revised=service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),
                    expected_revision=result.state.revision,mode='special',
                    edit=dict(entry_id=key,action='confirmed_official_special',provider_episode_id='9001'))
                for name in ('exceptional','subtitle','audio'):
                    row=next(e for e in revised.entries if e.source_path==extras[name])
                    self.assertEqual((0,1),(row.season_id,row.episode_id))
                    self.assertIn('Season 00',str(row.target_path))
                    self.assertNotIn(row.source_path,revised.ignored_sources)
                errors={i.code for i in PlanVerifier().verify(revised).issues if i.source_path==extras['exceptional']}
                self.assertFalse(errors,errors)
                # Reopen and explicit exclusion/restoration keep the official mapping.
                excluded=change(service,revised,[extras['exceptional']])
                restored=change(service,excluded,[extras['exceptional']],action='restore')
                saved=service.plan_store.load(plan.task_id)
                self.assertEqual(PlanStore.fingerprint(restored),PlanStore.fingerprint(saved))
                self.assertEqual((0,1),next((e.season_id,e.episode_id) for e in saved.entries if e.source_path==extras['exceptional']))
                self.assertFalse(service.load_or_create(saved).approved_entry_ids)
                self.assertFalse(any(e.target_path.exists() for e in saved.entries))

    async def test_existing_task_can_fetch_official_specials_without_losing_choices(self):
        with patch('src.rename.special_crosswalk.observe',return_value=None):
            root,service,_,result,extras,*_=await self.fixture(anime=False,duplicate=True)
        plan=result.plan; before=set(plan.ignored_sources)
        self.assertIsNone(plan.metadata.get('tv_completion_special'),'existing null observation is the incident')
        payload={'episodes':[{'id':9001,'episode_number':1,'name':'Official OVA','runtime':24}]}
        with patch('src.rename.get_info.Search',return_value=Mock(get_season_info=Mock(return_value=payload))):
            updated=service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),
                expected_revision=result.state.revision,mode='special_refresh',edit={})
        self.assertEqual(before,set(updated.ignored_sources))
        from src.rename.provider_special_evidence import ProviderSpecialObservation
        self.assertEqual('9001',ProviderSpecialObservation.from_mapping(
            updated.metadata['provider_special_observation']).episodes[0]['provider_episode_id'])
        self.assertFalse(service.load_or_create(updated).approved_entry_ids)

    async def test_failed_fetch_stale_edit_tamper_and_collisions_do_not_bypass_review(self):
        from dataclasses import replace
        from src.rename import special_decision_projection as projection
        root,service,_,result,extras,*_=await self.fixture(anime=False,exceptional=True,exceptional_name='Show SP.mkv',companions=True)
        plan=result.plan
        key=entry_id(next(e for e in plan.entries if e.source_path==extras['exceptional']))
        edit=dict(entry_id=key,action='confirmed_official_special',provider_episode_id='9001')
        before=records(root)
        with patch('src.rename.get_info.Search',return_value=Mock(get_season_info=Mock(return_value=None))):
            with self.assertRaisesRegex(PlanReviewError,'读取失败'):
                service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=result.state.revision,mode='special_refresh',edit={})
        self.assertEqual(before,records(root))
        with patch.object(projection,'project_special_decisions',return_value=None):
            with self.assertRaisesRegex(ValueError,'投影器已断开'):
                service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=result.state.revision,mode='special',edit=edit)
        self.assertEqual(before,records(root))
        with patch('src.rename.get_info.Search',side_effect=AssertionError('confirmation must not refetch')):
            revised=service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=result.state.revision,mode='special',edit=edit)
        before=records(root)
        with self.assertRaises(PlanReviewError):
            service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=result.state.revision,mode='special',edit=edit)
        self.assertEqual(before,records(root))
        raw=revised.to_dict()['metadata']['provider_special_decisions']
        raw[key]['provider_id']='99999'
        tampered=replace(revised,metadata={**revised.metadata,'provider_special_decisions':raw})
        self.assertEqual('provider_special_decision_invalid',projection.validated_special_decisions(tampered)[1])
        other=next(e for e in revised.entries if e.media_kind=='video' and e.source_path!=extras['exceptional'])
        state=service.decision_state_store.load(plan.task_id)
        duplicate=service.revise_tv_content(plan.task_id,PlanStore.fingerprint(revised),expected_revision=state.revision,
            mode='special',edit={**edit,'entry_id':entry_id(other)})
        self.assertIn('duplicate_target',{i.code for i in PlanVerifier().verify(duplicate).issues})
        self.assertFalse(service.load_or_create(duplicate).approved_entry_ids)

    async def test_official_picker_remains_available_and_content_panes_stack(self):
        root,service,_,result,extras,*_=await self.fixture(anime=False,exceptional=True,exceptional_name='Show SP.mkv')
        self.create_page(prepared=(root,service,result.plan,[]))
        await self.driver.click('特别篇 / OVA')
        self.assertIn('TMDB 编号',str([getattr(e,'label','') for e in self.client.elements.values()]))
        controls=self.view._official_special_controls
        identifier=entry_id(next(e for e in result.plan.entries if e.source_path==extras['exceptional']))
        with self.client:
            controls['files'].set_value(identifier)
            controls['episode'].set_value('9001')
        await self.driver.click_element(controls['confirm'])
        saved=service.plan_store.load(result.plan.task_id)
        video=next(e for e in saved.entries if e.source_path==extras['exceptional'])
        self.assertEqual((0,1),(video.season_id,video.episode_id))
        await self.driver.click('特别篇 / OVA')
        close = next(e for e in self.view._official_special_picker.descendants() if e._props.get('label') == '关闭')
        await self.driver.click_element(close)
        await self.driver.click('内容保留与排除')
        panes=self.view._content_workbench.panels
        a=panes['exclude']['container'].parent_slot.parent
        b=panes['restore']['container'].parent_slot.parent
        self.assertIs(a.parent_slot.parent,b.parent_slot.parent)
        self.assertIn('bar-content-columns', a.parent_slot.parent.classes)

    async def test_toolbar_special_entry_tracks_plan_changes_without_reopening(self):
        from dataclasses import replace
        root,service,_,result,*_=await self.fixture(anime=False)
        self.create_page(prepared=(root,service,result.plan,[]))
        def visible():
            return [e for e in self.client.elements.values()
                if type(e).__name__=='Button' and getattr(e,'text','')=='特别篇 / OVA' and e.visible]
        self.assertEqual(1,len(visible()))
        with self.client:
            self.view.plan=replace(result.plan,metadata={k:v for k,v in result.plan.metadata.items() if k!='tv_completion_resolution'})
            self.view._refresh_plan_header()
        self.assertEqual([],visible(),'unselected/non-TV view must hide the action')
        with self.client:
            self.view.plan=result.plan
            self.view._refresh_plan_header()
        self.assertEqual(1,len(visible()),'confirmation must show the action without reopening')

    async def test_foreign_special_observation_is_rejected(self):
        from src.rename.provider_special_evidence import freeze_provider_special_observation,project_provider_special_evidence
        _,_,_,result,*_=await self.fixture(anime=False)
        observation=freeze_provider_special_observation(provider_namespace='tmdb',provider_entity_type='tv',provider_id='99999',
            resolver=lambda *_:{'episodes':[dict(id=99,episode_number=1,name='foreign')]},enabled=True)
        with self.assertRaises(ValueError): project_provider_special_evidence(result.plan,observation)

    async def test_official_choice_can_change_and_stale_popup_cannot_overwrite_it(self):
        root,service,_,result,extras,*_=await self.fixture(anime=False,exceptional=True,exceptional_name='Show SP.mkv',companions=True)
        plan=result.plan
        key=entry_id(next(e for e in plan.entries if e.source_path==extras['exceptional']))
        def select(current,episode):
            state=service.decision_state_store.load(current.task_id)
            return service.revise_tv_content(current.task_id,PlanStore.fingerprint(current),expected_revision=state.revision,
                mode='special',edit=dict(entry_id=key,action='confirmed_official_special',provider_episode_id=episode))
        selected=select(plan,'9001')
        payload={'episodes':[dict(id=9001,episode_number=1,name='Official one'),dict(id=9002,episode_number=2,name='Official two')]}
        with patch('src.rename.get_info.Search',return_value=Mock(get_season_info=Mock(return_value=payload))):
            updated=service.revise_tv_content(plan.task_id,PlanStore.fingerprint(selected),
                expected_revision=service.decision_state_store.load(plan.task_id).revision,mode='special_refresh',edit={})
        self.create_page(prepared=(root,service,updated,[]))
        await self.driver.click('特别篇 / OVA')
        controls=self.view._official_special_controls
        with self.client:
            controls['files'].set_value(key); controls['episode'].set_value('9001')
        later=select(updated,'9002')
        before=records(root)
        await self.driver.click_element(controls['confirm'])
        self.assertEqual(before,records(root),'old browser cannot overwrite a later choice')
        self.assertEqual(PlanStore.fingerprint(later),PlanStore.fingerprint(service.plan_store.load(plan.task_id)))
        for name in ('exceptional','subtitle','audio'):
            row=next(e for e in later.entries if e.source_path==extras[name])
            self.assertEqual((0,2),(row.season_id,row.episode_id))
        # A refresh that drops the chosen official episode must preserve the old plan.
        with patch('src.rename.get_info.Search',return_value=Mock(get_season_info=Mock(return_value={'episodes':[]}))):
            with self.assertRaises(ValueError):
                service.revise_tv_content(plan.task_id,PlanStore.fingerprint(later),
                    expected_revision=service.decision_state_store.load(plan.task_id).revision,mode='special_refresh',edit={})
        self.assertEqual(before,records(root))

    async def test_normal_tmdb_episode_control(self):
        _,_,_,result,*_=await self.fixture(anime=False)
        self.assertIsNotNone(result.plan.work_identity,result.task.get('error'))
        self.assertFalse(result.state.provider_special_decisions)
        self.assertTrue(all(e.season_id==1 for e in result.plan.entries if e.media_kind=='video'))
