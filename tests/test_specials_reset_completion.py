"""双侧独立资料与同任务重置的定向完成检查。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_default_activation_contract as activation
from tests import test_special_crosswalk_gui as gui
from tests.test_special_catalog_contract import pair
from src.rename.special_catalog import build_observation, read_source
from src.rename.provider_special_evidence import ProviderSpecialObservation
from src.rename.recognition_queue_admission import capture_workspace_open
from src.rename.task_batch_scope import TaskSourceScope


class SpecialsData(unittest.TestCase):
    def test_tmdb_first_fetch_roundtrip_is_order_independent(self):
        from src.rename.special_crosswalk import observe
        original=pair().to_dict()
        left,right=[source['provider'] for source in original['catalog']]
        bgm=SimpleNamespace(_get_json=lambda *_:dict(total=1,offset=0,data=[dict(id=1011,subject_id=100,type=1,sort=6.5)]))
        tmdb=SimpleNamespace(get_season_info=lambda *_:dict(episodes=[dict(id=9001,episode_number=7,name='特别篇')]))
        observed=observe(right,1,providers=(left,),bgm=bgm,tmdb=tmdb)
        restored=ProviderSpecialObservation.from_mapping(observed.to_dict())
        self.assertEqual(observed.to_dict(),restored.to_dict())
        again=observe(right,1,providers=(left,right),bgm=bgm,tmdb=tmdb,previous=restored.to_dict())
        self.assertEqual(2,len(again.to_dict()['sources']))
        self.assertEqual(again.to_dict(),ProviderSpecialObservation.from_mapping(again.to_dict()).to_dict())
        self.assertEqual(['bangumi','tmdb'],[source['provider']['namespace'] for source in restored.to_dict()['catalog']])
        reversed_catalog=list(reversed(restored.to_dict()['catalog']))
        refreshed=build_observation(right,reversed_catalog,previous=restored.to_dict())
        self.assertEqual(refreshed.to_dict(),ProviderSpecialObservation.from_mapping(refreshed.to_dict()).to_dict())

    def test_all_mixed_states_and_positive_number_gate(self):
        original=pair().to_dict()
        def source(provider, state):
            result=deepcopy(next(s for s in original['catalog'] if s['provider']['namespace']==provider))
            if state!='rows':
                result.update(rows=[], complete=state=='empty', state='completed_empty' if state=='empty' else 'unavailable_or_error',
                    reason='not_found' if state=='empty' else 'unavailable',
                    issues=[] if state=='empty' else [dict(provider_episode_id='',index=None,reason='unavailable')])
            return result
        for left in ('rows','empty','failed'):
            for right in ('rows','empty','failed'):
                with self.subTest(left=left,right=right):
                    observed=build_observation(original['provider_identity'],[source('bangumi',left),source('tmdb',right)])
                    expected='rows_available' if 'rows' in (left,right) else 'confirmed_empty' if 'empty' in (left,right) else 'awaiting_data'
                    self.assertEqual(expected,observed.state)
                    self.assertEqual(expected!='awaiting_data',observed.displayable)
                    self.assertEqual(right=='rows',observed.output_candidates_available)
                    self.assertEqual(observed.to_dict(),ProviderSpecialObservation.from_mapping(observed.to_dict()).to_dict())
        for number in (None,0,1.5):
            observed=pair(tmdb_rows=[dict(id=9001,episode_number=number,name='特别篇')])
            self.assertEqual(1,len(observed.to_dict()['catalog'][1]['rows']))
            self.assertFalse(observed.output_candidates_available)
            self.assertTrue(observed.to_dict()['catalog'][1]['rows'][0]['source_url'].endswith('/season/0'))

    def test_failed_empty_and_partial_refresh_retain_rows(self):
        old=pair().to_dict()
        right=old['catalog'][1]['provider']
        def fail(*args): raise ConnectionError('fixture')
        for client in (SimpleNamespace(get_season_info=fail),SimpleNamespace(get_season_info=lambda *_:dict(episodes=[]))):
            updated=build_observation(old['provider_identity'],[old['catalog'][0],read_source(right,client=client)],previous=old)
            self.assertEqual(old['catalog'][1]['rows'],updated.to_dict()['catalog'][1]['rows'])
        partial=read_source(right,client=SimpleNamespace(get_season_info=lambda *_:dict(episodes=[dict(id=9002,episode_number=8),dict(id=-1)])))
        updated=build_observation(old['provider_identity'],[old['catalog'][0],partial],previous=old)
        from src.rename.special_selection import references
        primary=SimpleNamespace(namespace='bangumi',entity_type='subject',provider_id='100',status='confirmed',to_dict=lambda:old['provider_identity'])
        plan=SimpleNamespace(work_identity=SimpleNamespace(primary_provider=primary),metadata={'provider_special_observation':updated.to_dict()})
        self.assertEqual({'50:9001','50:9002'},set(references(plan,'tmdb')))

    def test_independent_fetch_no_season_match_and_discovery_failure(self):
        from src.rename.special_crosswalk import observe
        old=pair().to_dict(); left,right=[s['provider'] for s in old['catalog']]
        calls=[]
        def bgm_get(path):
            calls.append(path)
            if '/subjects/' in path:
                return dict(id=100, type=2, platform='TV', name='Isolated Show', date='2020-01-01')
            return dict(total=1,offset=0,data=[dict(id=1011,subject_id=100,type=1,sort=6.5)])
        def tmdb_get(*args): calls.append('tmdb'); raise ConnectionError('fixture')
        with patch('src.rename.season_reference.match_component',side_effect=AssertionError('不允许季匹配')):
            observed=observe(left,1,providers=(right,),bgm=SimpleNamespace(_get_json=bgm_get),tmdb=SimpleNamespace(get_season_info=tmdb_get))
        self.assertIn('tmdb',calls)
        self.assertEqual([1,0],[len(s['rows']) for s in observed.to_dict()['catalog']])
        observed=observe(left,1,bgm=SimpleNamespace(_get_json=bgm_get),tmdb=SimpleNamespace())
        self.assertEqual('unavailable',observed.to_dict()['relation_reason'])
        self.assertEqual(1,len(observed.to_dict()['catalog'][0]['rows']))


class ResetCompletion(unittest.IsolatedAsyncioTestCase):
    async def reset(self,flow,movie,anime=False,name='人工检索',season=None):
        return await flow.manager.reenqueue_workspace(flow.task_id,flow.source,is_anime=anime,is_movie=movie,
            custom_name=name,custom_season_id=season,preview_only=False,
            opened_evidence=capture_workspace_open(flow.manager,flow.task_id,flow.manager._read(flow.task_id)))

    async def test_tv_movie_both_directions_complete_new_worker_and_reject_old(self):
        from src.rename.automatic_recognition import AutomaticRecognitionOrigin,validate_original_job
        from src.rename.current_plan_retirement import require_not_retired_plan
        from src.rename.plan_store import PlanStore
        fixture=activation.DefaultActivationProducerContracts()
        for prior in (False,True):
            async with fixture.flow(movie=prior) as flow:
                old=flow.store.load(flow.task_id); previous=flow.service.decision_state_store.load(flow.task_id)
                task=flow.manager._read(flow.task_id)
                origin=AutomaticRecognitionOrigin.from_mapping(task['automatic_recognition_origin'])
                accepted = []
                put = flow.manager._queue.put_nowait
                def capture(job):
                    put(job)
                    accepted.append(job)
                with patch.object(flow.manager._queue, 'put_nowait', side_effect=capture):
                    await self.reset(flow,not prior,name='Ordinary Show' if prior else 'Ordinary Movie',season=1 if prior else None)
                self.assertEqual(1, len(accepted))
                new_job = accepted[0]
                self.assertNotEqual(origin.job_id,new_job.recognition_origin.job_id)
                with self.assertRaises(ValueError): validate_original_job(flow.service,origin)
                with self.assertRaises(ValueError): flow.store.restore_current(old)
                with self.assertRaises((ValueError,RuntimeError)):
                    require_not_retired_plan(task_id=flow.task_id,plan_root=flow.store.root,
                        task_root=flow.manager.task_root,plan_fingerprint=PlanStore.fingerprint(old))
                await asyncio.wait_for(flow.manager._queue.join(),12)
                actual=flow.manager._read(flow.task_id)
                self.assertEqual(not prior,actual['is_movie'],actual.get('error'))
                self.assertEqual('tv' if prior else 'movie',actual['task_source_scope']['media_domain'])
                state=flow.service.decision_state_store.load(flow.task_id)
                self.assertGreater(state.revision,previous.revision)
                self.assertFalse(state.provider_special_decisions)
                self.assertEqual('tv' if prior else 'movie',state.confirmed_work['primary_provider']['entity_type'])
                self.assertEqual(task['task_source_scope']['source_identities'],actual['task_source_scope']['source_identities'])

    async def test_auto_clears_old_type_and_rebinds_discovered_movie(self):
        fixture=activation.DefaultActivationProducerContracts()
        def configure(worker):
            worker.search.get_tv_info=lambda *_: ('',None)
            worker.bangumi_search.search_animation=lambda *_a, **_kw: []
            worker.bangumi_search.search_subjects=lambda *_a, **_kw: []
        async with fixture.flow(movie=True,configure_worker=configure) as flow:
            accepted = []
            put = flow.manager._queue.put_nowait
            def capture(job):
                put(job)
                accepted.append(job)
            with patch.object(flow.manager._queue, 'put_nowait', side_effect=capture):
                await self.reset(flow,None,anime=None,name='Ordinary Movie')
            self.assertEqual(1, len(accepted))
            self.assertIsNone(accepted[0].is_anime)
            self.assertIsNone(accepted[0].is_movie)
            await asyncio.wait_for(flow.manager._queue.join(),12)
            actual=flow.manager._read(flow.task_id)
            self.assertIs(actual['is_movie'],True,actual.get('error'))
            self.assertEqual('movie',actual['task_source_scope']['media_domain'])
            self.assertTrue(flow.service.decision_state_store.load(flow.task_id).confirmed_work)

    async def test_interrupted_reset_blocks_old_paths_and_recovers(self):
        from src.rename.recognition_reset import recover,first_revision
        fixture=activation.DefaultActivationProducerContracts()
        async with fixture.flow() as flow:
            old=flow.store.load(flow.task_id)
            previous=flow.service.decision_state_store.load(flow.task_id)
            def fail(phase):
                if phase=='recognition_reset_decision': raise OSError('fixture interruption')
            flow.manager.projection_commit_service._phase_hook=fail
            with self.assertRaises((ValueError,RuntimeError)):
                await self.reset(flow,True)
            self.assertEqual({},flow.manager._read(flow.task_id))
            for read in (lambda:flow.store.load(flow.task_id),
                         lambda:flow.service.decision_state_store.load(flow.task_id),lambda:flow.store.restore_current(old)):
                with self.assertRaises(ValueError): read()
            flow.manager.projection_commit_service._phase_hook=None
            recover(flow.manager)
            actual=flow.manager._read(flow.task_id)
            self.assertIs(actual['is_movie'],True)
            self.assertFalse(flow.store.path_for(flow.task_id).exists())
            self.assertEqual(previous.revision+1,first_revision(flow.root,flow.task_id))
            flow.manager.batch_scope_store.require_active_scope(TaskSourceScope.from_mapping(actual['task_source_scope']),batch_id=actual['batch_id'])
            recover(flow.manager)
            self.assertEqual(actual,flow.manager._read(flow.task_id))

    async def test_stale_forged_and_disconnected_reset_preserve_records(self):
        from src.rename import recognition_reset as reset
        from tests.test_content_choice_contract import records
        fixture=activation.DefaultActivationProducerContracts()
        async with fixture.flow() as flow:
            current=flow.manager._read(flow.task_id)
            opened=capture_workspace_open(flow.manager,flow.task_id,current)
            request=dict(path=str(flow.source),is_anime=False,is_movie=True,custom_name='New',custom_season_id=None,preview_only=False)
            prepared=reset._prepare(flow.manager,flow.task_id,request,opened)
            before=records(flow.root)
            with self.assertRaises(ValueError):
                reset.commit(flow.manager.projection_commit_service,flow.manager,replace(prepared,job=replace(prepared.job,is_movie=False)))
            self.assertEqual(before,records(flow.root))
            with patch.object(flow.manager.projection_commit_service,'reset_recognition',return_value=None):
                with self.assertRaises((ValueError,RuntimeError)):
                    await self.reset(flow,True)
            self.assertEqual(before,records(flow.root))
            flow.manager._update(flow.task_id,name='后来修改')
            before=records(flow.root)
            with self.assertRaises((ValueError,RuntimeError)):
                await flow.manager.reenqueue_workspace(flow.task_id,flow.source,is_anime=False,is_movie=True,
                    custom_name='New',custom_season_id=None,preview_only=False,opened_evidence=opened)
            self.assertEqual(before,records(flow.root))
            flow.manager._update(flow.task_id,execution_may_have_written_targets=True)
            before=records(flow.root)
            with self.assertRaises((ValueError,RuntimeError)): await self.reset(flow,True)
            self.assertEqual(before,records(flow.root))

    async def test_sibling_reservation_unchanged(self):
        from src.rename.task_batch_scope import BatchTaskSourceRequest
        fixture=activation.DefaultActivationProducerContracts()
        async with fixture.flow(admission_only=True) as flow:
            original=flow.manager._read(flow.task_id)
            scope=TaskSourceScope.from_mapping(original['task_source_scope'])
            store=flow.manager.batch_scope_store
            manifest=store.load(original['batch_id'])
            request=manifest.requests[0]
            # 独立文件的同批次兄弟任务；实际新批次物化与接纳保持既有格式。
            sibling=replace(request,task_id='sibling',source_identities=(replace(scope.source_identities[0],
                location_key=scope.source_identities[0].location_key+'-sibling',physical_key=scope.source_identities[0].physical_key+'-sibling'),))
            mixed=store._materialize(batch_id='mixed',requests=(request,sibling),batch_fingerprint=store._batch_fingerprint((request,sibling)),state='active',revision=3)
            mixed_scope=next(s for s in mixed.task_source_scopes if s.task_id==flow.task_id)
            with patch.object(store,'require_active_scope',return_value=mixed):
                retired,new=store.prepare_recognition_reset(mixed_scope,batch_id='mixed',new_batch_id='new',media_domain='movie')
            self.assertEqual([r for r in mixed.reservations if r.owner_task_id=='sibling'],[r for r in retired.reservations if r.owner_task_id=='sibling'])
            self.assertEqual([s for s in mixed.task_source_scopes if s.task_id=='sibling'],list(retired.task_source_scopes))
            self.assertEqual(mixed_scope.source_identities,new.task_source_scopes[0].source_identities)


class SpecialsInterface(unittest.IsolatedAsyncioTestCase):
    fixture=gui.SpecialCrosswalkGui.fixture
    create_page=gui.SpecialCrosswalkGui.create_page
    text=gui.SpecialCrosswalkGui.text
    prepared=gui.SpecialCrosswalkGui.prepared

    async def test_modes_keep_both_lists_and_empty_failed_distinction(self):
        from src.pages.special_crosswalk_view import render
        from nicegui import ui
        _,service,plan,extras,key=await self.prepared()
        await self.driver.click('特别篇 / OVA')
        c=self.view._official_special_controls
        with self.client:
            c['files'].set_value(key);before=(dict(c['bangumi'].options),dict(c['episode'].options))
            c['mode'].set_value('descriptive')
        self.assertEqual(before,(c['bangumi'].options,c['episode'].options))
        self.assertTrue(c['confirm'].enabled)
        with self.client:c['mode'].set_value('numbered')
        self.assertEqual(before,(c['bangumi'].options,c['episode'].options))
        value=plan.metadata['provider_special_observation']
        catalog=[]
        for source in value['catalog']:
            from src.rename.special_catalog import plain
            row=plain(source); row.update(rows=[],complete=True,state='completed_empty',reason='not_found',issues=[])
            catalog.append(row)
        observed=build_observation(dict(value['provider_identity']),catalog)
        updated=replace(plan,metadata={**plan.metadata,'provider_special_observation':observed.to_dict()})
        with self.client: c['update'](updated)
        self.assertEqual('未找到特别篇资料',c['bangumi']._props['display-value'])
        self.assertEqual('未找到特别篇资料',c['episode']._props['display-value'])
        self.assertFalse(c['bangumi'].enabled)
        with self.client:c['update'](plan)
        self.assertNotIn('display-value',c['bangumi']._props)
        self.assertTrue(c['bangumi'].enabled)


    async def test_no_number_rows_remain_selectable_and_save_descriptive(self):
        from src.rename.plan_store import PlanStore
        from src.rename.special_catalog import plain
        root,service,plan,extras,key=await self.prepared()
        value=plain(plan.metadata['provider_special_observation'])
        value['catalog'][1]['rows'][0]['source_number']=None
        observed=build_observation(value['provider_identity'],value['catalog'])
        with patch('src.rename.special_crosswalk.observe',return_value=observed):
            plan=service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),
                expected_revision=service.decision_state_store.load(plan.task_id).revision,mode='special_refresh',edit={})
        self.view.plan=plan;self.view.task_decision=service.decision_state_store.load(plan.task_id)
        await self.driver.click('特别篇 / OVA');c=self.view._official_special_controls
        with self.client:
            c['files'].set_value(key);c['mode'].set_value('numbered')
        self.assertIn('9001',c['episode'].options);self.assertFalse(c['confirm'].enabled)
        with self.client:c['episode'].set_value(None)
        self.assertNotIn('匹配建议：',self.text())
        with self.client:c['episode'].set_value('9001')
        with self.client:
            c['mode'].set_value('descriptive');c['title'].set_value('无编号特别篇')
        self.assertTrue(c['confirm'].enabled)
        await self.driver.click_element(c['confirm'])
        decision=service.decision_state_store.load(plan.task_id).provider_special_decisions[key]
        self.assertEqual('9001',decision['tmdb_reference']['row']['provider_episode_id'])
        self.assertEqual('1011',decision['bangumi_reference']['row']['provider_episode_id'])
        self.assertEqual('descriptive_special',decision['action'])

    async def test_manual_close_during_fetch_is_not_reopened(self):
        import threading
        _,service,plan,extras,key=await self.prepared()
        await self.driver.click('特别篇 / OVA')
        popup=self.view._official_special_picker
        button=next(e for e in popup.descendants() if e._props.get('icon')=='cloud_download')
        entered,released=threading.Event(),threading.Event()
        observed=ProviderSpecialObservation.from_mapping(plan.metadata['provider_special_observation'])
        def delayed(*args,**kwargs):
            entered.set()
            if not released.wait(5):raise TimeoutError('fixture')
            return observed
        with patch('src.rename.special_crosswalk.observe',side_effect=delayed):
            action=asyncio.create_task(self.driver.click_element(button))
            try:
                for _ in range(100):
                    if entered.is_set():break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                with self.client:popup.close()
            finally:released.set()
            await action
        self.assertFalse(popup.value)
        self.assertIs(popup,self.view._official_special_picker)
