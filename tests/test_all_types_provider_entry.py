"""Official-shape replay through public queue selection, not a capability-only proof."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
import unittest

from tests import test_r1_real_media_production_red_contract as flow
from tests import _media_flow_helpers as media
from src.rename.provider_search import BangumiProviderClient, build_search_policy
from src.rename.provider_selection_authority import provider_selection_capability

class AllTypesProviderEntry(unittest.IsolatedAsyncioTestCase):
    async def run_selected(self, *, anime, movie, namespace, actual_anime=None, rejected=False, execute=False):
        actual_anime = anime if actual_anime is None else actual_anime
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, video, _font = media._make_version(root/'sources', '1080p')
            if not movie:
                old_stem=video.stem
                for path in tuple(source.iterdir()):
                    if path.name.startswith(old_stem+'.'):
                        path.rename(path.with_name('Sample Work S01E01'+path.name[len(old_stem):]))
                video=source/'Sample Work S01E01.mkv'
            ident = '242216' if anime and movie else '114365' if movie else '5337'
            detail = dict(id=int(ident), type=2 if actual_anime else 6, platform='剧场版' if actual_anime and movie else '电影' if movie else 'TV' if actual_anime else '欧美剧', name='Sample Work', name_cn='测试作品', date='2020-01-01')
            candidate = dict(namespace='bangumi', entity_type='subject', provider_id=ident,
                title=detail['name_cn'], original_title=detail['name'], year=2020, platform=detail['platform'],
                library_domain='animation' if actual_anime else 'general', source_url='https://bgm.tv/subject/'+ident)
            if namespace == 'tmdb':
                candidate.update(namespace='tmdb',entity_type='movie' if movie else 'tv',provider_id='763096' if movie else '4242')
            configured = flow._configured_renamer
            calls = []
            def make(path):
                renamer = configured(path)
                def get(endpoint):
                    calls.append(endpoint)
                    if endpoint == '/v0/subjects/'+ident:return detail
                    if endpoint.startswith('/v0/episodes'):return dict(total=0,limit=100,offset=0,data=[])
                    if endpoint.endswith('/subjects'):return []
                    raise AssertionError('unexpected provider query '+endpoint)
                renamer.bangumi_search._get_json = get
                return renamer
            from src.pages.plan_review_page import PlanReviewDialog
            original_select = PlanReviewDialog._select_provider_candidate
            async def select(dialog, selected, **kwargs):
                if not movie:
                    kwargs['season_number'] = 1
                return await original_select(dialog, selected, **kwargs)
            harness=SimpleNamespace(_NoopReviewDialog=flow.R1RealQueueWorkerContracts._NoopReviewDialog)
            with patch.object(PlanReviewDialog, '_select_provider_candidate', select), patch.object(flow,'_configured_renamer',side_effect=make), patch('src.rename.process.SERIES_CATALOG_PATH',root/'series_catalog'), patch('src.rename.process.RECORD_PATH',root/'records'):
                result=await flow.R1RealQueueWorkerContracts._run_flow(harness,root=root/'flow',source=source,task_id='all-type-entry',candidate=candidate,is_anime=anime,is_movie=movie)
            if rejected:
                self.assertFalse(result.state.confirmed_work)
                self.assertEqual(result.initial_state, result.state)
                self.assertEqual(result.initial_plan, result.plan)
                return
            self.assertTrue(result.state.confirmed_work, result.task.get('error'))
            primary=result.plan.work_identity.primary_provider
            self.assertEqual(namespace,primary.namespace)
            self.assertEqual('animation' if anime else 'general',result.plan.work_identity.domain)
            self.assertEqual(candidate['provider_id'],primary.provider_id)
            self.assertIsNone(result.state.execution_failure)
            from src.rename.plan_review import PlanReviewService
            from src.rename.initial_movie_completion import RootSnapshotSource
            from src.rename.target_routing import ROOT_KEYS, TargetRootSnapshot
            from src.rename.plan_store import PlanStore
            from src.rename.series_catalog import SeriesCatalog
            base = root/'flow'
            service = PlanReviewService(result.plan_store, base/'reviews', base/'work_catalog',
                decision_state_root=base/'decisions', task_root=base/'tasks', record_root=base/'records')
            service.series_catalog = SeriesCatalog(root/'series_catalog')
            service.root_snapshot_source = RootSnapshotSource(lambda: TargetRootSnapshot.capture(
                {key: root/'destinations'/key for key in ROOT_KEYS}))
            if movie:
                from src.pages.asset_choice_forms import render_movie_choices
                from tests.test_complex_library_special_red_contract import _CaptureUi
                ui = _CaptureUi()
                render_movie_choices(SimpleNamespace(plan=result.plan), result.plan.presentation_groups[0],
                    'movie_continuous', {}, [], ui=ui, edit_versions=lambda *_: None)
                intent = service.prepare_initial_movie_completion(result.plan.task_id, PlanStore.fingerprint(result.plan),
                    groups=[dict(sources=[str(video)], label='1080p')], reason='确认完整电影')
                completed = service.commit_initial_movie_completion(intent)
                state = service.decision_state_store.load(completed.task_id)
                self.assertEqual('movie_versions', state.layout_mode)
                self.assertEqual(namespace, completed.work_identity.primary_provider.namespace)
                self.assertTrue(any(e.role == 'external_audio' for e in completed.entries))
                self.assertEqual(result.state.initial_source_basis, state.initial_source_basis)
                # Both movie domains must support complete-plan round trips,
                # not only their first selection buttons.
                bgm = dict(candidate,namespace='bangumi',entity_type='subject',provider_id=ident)
                tmdb = dict(namespace='tmdb',entity_type='movie',provider_id='8000')
                movie_detail = dict(id=8000,title='新电影',original_title='New Film',release_date='2021-01-01',
                    genres=[dict(id=16 if anime else 18)])
                for selected in (tmdb,bgm):
                    with patch('src.rename.get_info.Search',return_value=SimpleNamespace(get_movie_info_by_id=lambda _:('New Film',movie_detail))), \
                            patch.object(BangumiProviderClient,'_get_json',return_value=detail):
                        intent = service.prepare_movie_work_correction(completed.task_id,PlanStore.fingerprint(completed),
                            expected_revision=state.revision,candidate=selected)
                    with patch.object(BangumiProviderClient,'_get_json',side_effect=AssertionError('commit lookup')):
                        completed = service.commit_initial_movie_completion(intent)
                    after = service.decision_state_store.load(completed.task_id)
                    self.assertEqual(selected['namespace'],completed.work_identity.primary_provider.namespace)
                    self.assertEqual(state.content_selection.associations,after.content_selection.associations)
                    self.assertEqual(state.initial_source_basis,after.initial_source_basis)
                    self.assertFalse(service.load(completed.task_id).approved_entry_ids)
                    state = after
                if execute:
                    import json, os
                    from tests._media_flow_helpers import MediaFlow
                    task,ledger,execution = await MediaFlow('runTest')._approve_and_execute(
                        root=base,result=SimpleNamespace(plan=completed,store=service.plan_store,review=service,
                            task=json.loads((base/'tasks'/f'{completed.task_id}.json').read_text('utf-8'))))
                    self.assertEqual('completed',task.get('execution_status'),task.get('error'))
                    self.assertEqual('completed',ledger.get('status'))
                    self.assertTrue(all(os.path.samefile(e.source_path,e.target_path) for e in completed.entries))
                    from src.rename.work_catalog import WorkCatalog
                    record = WorkCatalog(base/'work_catalog').load(completed.metadata['proposed_work_key'])
                    self.assertEqual('animation' if anime else 'general',record['domain'])
                    self.assertTrue(any(p['namespace']=='bangumi' for p in record['providers']))
            else:
                from src.rename.series_registration import validate_plan_series_registration
                proof = validate_plan_series_registration(result.plan, result.state)
                self.assertEqual('series-registration-affiliation-v1', proof.policy_version)
                self.assertEqual((), proof.season_membership, 'manual output must not become official season membership')
                from src.pages.work_correction_view import is_general_work_correction
                from src.pages.general_season_view import general_tv_provider
                owner=SimpleNamespace(task_decision=result.state, plan=result.plan)
                self.assertEqual('bangumi', general_tv_provider(owner)['namespace'])
                target=dict(namespace='tmdb', entity_type='tv', provider_id='8000', title='新剧集')
                self.assertTrue(is_general_work_correction(owner, candidate))
                search=SimpleNamespace(get_tv_info_by_id=lambda _: ('新剧集', dict(id=8000, name='新剧集',
                    original_name='New Show', first_air_date='2021-01-01', genres=[dict(id=18)],
                    seasons=[dict(season_number=2)])), get_season_info=lambda *_:dict(episodes=[]),
                    get_tv_subject_observation_info=lambda *_:('',{}))
                changed=result.plan
                for selected in (target,candidate):
                    with patch('src.rename.get_info.Search',return_value=search), patch.object(BangumiProviderClient,'search_subjects',return_value=[]), patch.object(BangumiProviderClient,'_get_json',
                            side_effect=lambda endpoint: dict(total=0,limit=100,offset=0,data=[]) if endpoint.startswith('/v0/episodes') else detail):
                        old=service.decision_state_store.load(changed.task_id)
                        observation=service.observe_work_correction(changed.task_id,PlanStore.fingerprint(changed),
                            expected_revision=old.revision,candidate=selected)
                        intent=service.prepare_work_correction(observation,season_number=2,reason='人工更换资料')
                    with patch.object(BangumiProviderClient,'_get_json',side_effect=AssertionError('commit must be offline')):
                        changed=service.commit_work_correction(intent)
                    now=service.decision_state_store.load(changed.task_id)
                    self.assertEqual('general',changed.work_identity.domain)
                    self.assertEqual(selected['namespace'],changed.work_identity.primary_provider.namespace)
                    self.assertEqual(old.content_selection.associations, now.content_selection.associations)
                changed=service.revise_tv_content(changed.task_id,PlanStore.fingerprint(changed),
                    expected_revision=now.revision,mode='season',edit={'season_number':3})
                self.assertEqual(3,changed.metadata['season_number'])
                if execute:
                    import os
                    from tests._media_flow_helpers import MediaFlow
                    result.plan=changed
                    task,ledger,_execution=await MediaFlow('runTest')._approve_and_execute(
                        root=base,result=result)
                    self.assertEqual('completed',task.get('execution_status'),task.get('error'))
                    self.assertEqual('completed',ledger.get('status'))
                    self.assertTrue(all(os.path.samefile(e.source_path,e.target_path) for e in result.plan.entries))
                    reopened=SeriesCatalog(base/'series_catalog').load(result.plan.metadata['series_key'])
                    self.assertTrue(any(row['task_id']==result.plan.task_id for row in reopened.season_bindings))

    async def test_bangumi_animation_movie_real_queue(self):
        await self.run_selected(anime=True,movie=True,namespace='bangumi')
    async def test_bangumi_general_movie_real_queue(self):
        await self.run_selected(anime=False,movie=True,namespace='bangumi')
    async def test_bangumi_general_tv_real_queue(self):
        await self.run_selected(anime=False,movie=False,namespace='bangumi')
    async def test_tmdb_animation_movie_control(self):
        await self.run_selected(anime=True,movie=True,namespace='tmdb')
    async def test_animation_bangumi_movie_executes_and_registers(self):
        await self.run_selected(anime=True,movie=True,namespace='bangumi',execute=True)
    async def test_general_bangumi_movie_executes_and_registers(self):
        await self.run_selected(anime=False,movie=True,namespace='bangumi',execute=True)
    async def test_general_bangumi_tv_executes_and_registers(self):
        await self.run_selected(anime=False,movie=False,namespace='bangumi',execute=True)
    def test_general_discovery_schedules_configured_bangumi(self):
        for kind in ('movie','tv'):
            with self.subTest(kind=kind):
                policy=build_search_policy(domain='general',media_hint=kind,tmdb_configured=False,bangumi_configured=True)
                self.assertEqual(('bangumi',),policy.scheduled_sources)
                self.assertEqual(('bangumi', 'tmdb_'+kind),policy.expected_sources)
                from src.rename.provider_search import ProviderSearchCoordinator,ProviderWorkerOutput
                row=dict(namespace='bangumi',entity_type='subject',provider_id='5337',title='测试作品',platform='电影' if kind=='movie' else '欧美剧')
                batch=ProviderSearchCoordinator({'bangumi':lambda:ProviderWorkerOutput((row,))}).search(policy,query='测试作品',year=None)
                self.assertEqual(['5337'],[c['provider_id'] for c in batch.candidates])
                self.assertEqual('success',next(r.status.value for r in batch.results if r.source=='bangumi'))
    def test_unknown_platform_and_wrong_kind_stay_blocked(self):
        for platform,kind in (('未知','movie'),('剧场版','tv')):
            candidate=dict(namespace='bangumi',entity_type='subject',provider_id='1',platform=platform)
            self.assertFalse(provider_selection_capability(candidate,group_kind='movie_continuous' if kind=='movie' else 'tv_episodes',task_media_kind=kind,task_library_domain='animation').actionable)

    def test_general_subject_registration_is_explicit_domain(self):
        from src.rename.rename_plan import WorkIdentity,ProviderIdentity
        from src.rename.series_registration import build_series_registration_authority
        work=WorkIdentity(status='confirmed',domain='general',title='测试剧集',year=2020,
            primary_provider=ProviderIdentity(status='confirmed',namespace='bangumi',entity_type='subject',provider_id='5337',title='测试剧集',year=2020,decision_source='explicit_user'),domain_source='provider_selection')
        proof=build_series_registration_authority(work,manual_season=2)
        self.assertEqual('bangumi',proof.namespace)
        self.assertEqual((('5337',2),),proof.season_membership)
        self.assertEqual('series-registration-manual-general-v1',proof.policy_version)

    def test_general_tv_correction_uses_actual_type6(self):
        from src.rename.work_correction import _work
        from src.rename.content_associations import encode
        info=dict(id=5337,type=6,platform='欧美剧',name='Breaking Bad',name_cn='绝命毒师',date='2008-01-20')
        work,seasons=_work('5337',encode(info),'2026-09-29T00:00:00Z','general','bangumi')
        self.assertEqual('general',work.domain)
        self.assertEqual((),seasons.available)
        self.assertEqual('subject',work.primary_provider.entity_type)

    def test_registration_missing_season_and_unconfirmed_work_stay_rejected(self):
        from dataclasses import replace
        from src.rename.rename_plan import WorkIdentity,ProviderIdentity
        from src.rename.series_registration import build_series_registration_authority
        provider=ProviderIdentity(status='confirmed',namespace='bangumi',entity_type='subject',provider_id='5337',title='测试剧集',year=2020,decision_source='explicit_user')
        work=WorkIdentity(status='confirmed',domain='general',title='测试剧集',year=2020,primary_provider=provider,domain_source='provider_selection')
        for season in (None,-1,True,'2'):
            with self.subTest(season=season), self.assertRaises(ValueError):
                build_series_registration_authority(work,manual_season=season)
        with self.assertRaises(ValueError):
            build_series_registration_authority(replace(work,status='unresolved'),manual_season=2)

    def test_wrong_subject_type_cannot_be_confirmed_as_general_tv(self):
        from src.rename.work_correction import _work
        from src.rename.content_associations import encode
        for changes in (dict(type=2),dict(platform='电影'),dict(id=999),dict(type=1)):
            info=dict(id=5337,type=6,platform='欧美剧',name='Breaking Bad',name_cn='绝命毒师',date='2008-01-20')
            info.update(changes)
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                _work('5337',encode(info),'2026-09-29T00:00:00Z','general','bangumi')

    def test_search_and_link_producers_keep_actual_bangumi_domain(self):
        from tests.test_provider_search_v5 import _Session
        from src.rename.provider_search import resolve_provider_item_url
        for domain, kind, platform in (('animation',2,'剧场版'),('general',6,'电影'),('general',6,'欧美剧')):
            with self.subTest(domain=domain, platform=platform):
                row=dict(id=5337,type=kind,platform=platform,name='Example',name_cn='示例',date='2020-01-01')
                session=_Session(dict(data=[row]))
                client=BangumiProviderClient(user_agent='BAR-test',session=session)
                rows=client.search_subjects('Example',domain=domain)
                self.assertEqual(1,len(session.calls))
                self.assertEqual([kind],session.calls[0][1]['json']['filter']['type'])
                self.assertEqual(domain,rows[0]['library_domain'])
                link_session=_Session(row)
                candidate=resolve_provider_item_url('https://bgm.tv/subject/5337',bangumi_user_agent='BAR-test',session=link_session)
                self.assertEqual(domain,candidate['library_domain'])
                self.assertEqual(1,len(link_session.calls))

    async def test_actual_type6_tv_cannot_enter_animation_queue(self):
        await self.run_selected(anime=True,movie=False,namespace='bangumi',actual_anime=False,rejected=True)

    async def test_actual_type2_tv_cannot_enter_general_queue(self):
        await self.run_selected(anime=False,movie=False,namespace='bangumi',actual_anime=True,rejected=True)

    def test_type6_platform_is_rejected_before_animation_legacy_route(self):
        row=dict(namespace='bangumi',entity_type='subject',provider_id='5337',platform='欧美剧')
        self.assertFalse(provider_selection_capability(row,group_kind='tv_episodes',task_media_kind='tv',task_library_domain='animation').actionable)
