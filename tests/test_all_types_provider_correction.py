"""Completed movie correction via the existing candidate UI, disposable sources."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_initial_movie_completion_contract as initial
from tests import test_content_workbench_gui as gui
from src.pages import plan_review_page as page
from src.rename.plan_store import PlanStore
from src.task_queue import TaskQueueManager


class CompletedMovieCorrection(unittest.IsolatedAsyncioTestCase):
    create_page = gui.ContentWorkbenchGui.create_page

    async def test_movie_lookup_immediate_feedback_and_close_discards_result(self):
        import asyncio, threading
        fixture = initial.InitialMovieFullCompletionContracts('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root,service,plan,video,_ = await fixture.fixture()
        plan = service.commit_initial_movie_completion(fixture.prepare(service,plan,[dict(sources=[str(video)],label='1080p')]))
        self.create_page(prepared=(root,service,plan,[video]))
        before = service.decision_state_store.load(plan.task_id)
        entered, release = threading.Event(),threading.Event()
        def slow(_endpoint):
            entered.set()
            if not release.wait(5):raise TimeoutError('test did not release reader')
            return dict(id=242216,type=2,platform='剧场版',name='Violet',name_cn='紫罗兰',date='2020-09-18')
        candidate = page.normalize_provider_candidates({'provider_candidates': {
            'movie_continuous': [dict(namespace='bangumi', entity_type='subject',
                provider_id='242216', platform='剧场版', library_domain='animation', title='紫罗兰')],
        }})[0]
        with self.client, patch.object(page, 'normalize_provider_candidates', return_value=[candidate]):
            self.view._show_provider_candidates()
        buttons = [e for e in self.view.descendants()
                   if e._props.get('label') == '选择这个作品']
        self.assertEqual(1, len(buttons))
        self.assertTrue(buttons[0].enabled, 'a real typed movie card must remain actionable on a Plan')
        with patch('src.rename.provider_search.BangumiProviderClient._get_json',side_effect=slow):
            with self.client:
                pending=asyncio.create_task(self.view._select_provider_candidate(
                    candidate))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,3))
                self.assertTrue(any('正在读取电影资料' in str(getattr(e,'text','')) for e in self.client.elements.values()))
                with self.client:
                    self.view.close()
            finally:
                release.set()
                await pending
        self.assertEqual(before,service.decision_state_store.load(plan.task_id))
        self.assertFalse(any(getattr(e,'text','')=='确认更换作品' for e in self.client.elements.values()))

    async def test_movie_correction_rejects_forgery_stale_and_disconnected_projector(self):
        from dataclasses import replace
        from tests.test_content_choice_contract import records
        from src.rename import initial_movie_completion as completion
        fixture = initial.InitialMovieFullCompletionContracts('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root, service, plan, video, extra = await fixture.fixture(multiple=True)
        plan = service.commit_initial_movie_completion(fixture.prepare(service,plan,
            [dict(sources=[str(video)],label='1080p'),dict(sources=[str(extra['second'])],label='备用版')]))
        state = service.decision_state_store.load(plan.task_id)
        detail = dict(id=242216,type=2,platform='剧场版',name='Violet',name_cn='紫罗兰',date='2020-09-18')
        with patch('src.rename.provider_search.BangumiProviderClient._get_json', return_value=detail):
            intent = service.prepare_movie_work_correction(plan.task_id,PlanStore.fingerprint(plan),
                expected_revision=state.revision,candidate=dict(namespace='bangumi',entity_type='subject',provider_id='242216'))
        before = records(root)
        fake_observation = replace(intent.inputs.work_observation)
        for forged in (replace(intent,inputs=replace(intent.inputs,work_observation=fake_observation)),
                replace(intent,inputs=replace(intent.inputs,groups_json='[]'))):
            with self.subTest(fault=forged.inputs.groups_json):
                with self.assertRaises((ValueError,RuntimeError)):
                    service.commit_initial_movie_completion(forged)
                self.assertEqual(before,records(root))
        with patch.object(completion,'_project_movie_content',return_value=None):
            with self.assertRaises((ValueError,TypeError,AttributeError)):
                service.commit_initial_movie_completion(intent)
        self.assertEqual(before,records(root))
        changed = service.commit_initial_movie_completion(intent)
        after = service.decision_state_store.load(plan.task_id)
        self.assertEqual([g['sources'] for g in state.content_selection.to_dict()['version_groups']],
            [g['sources'] for g in after.content_selection.to_dict()['version_groups']])
        self.assertEqual([g['label'] for g in state.content_selection.to_dict()['version_groups']],
            [g['label'] for g in after.content_selection.to_dict()['version_groups']])
        saved = records(root)
        with self.assertRaises((ValueError,RuntimeError)):
            service.commit_initial_movie_completion(intent)
        self.assertEqual(saved,records(root))
        self.assertTrue(all(not e.target_path.exists() for e in changed.entries))

    async def test_completed_movie_candidate_reaches_preview_and_confirmation(self):
        fixture = initial.InitialMovieFullCompletionContracts('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root, service, plan, video, _ = await fixture.fixture()
        intent = fixture.prepare(service, plan, [dict(sources=[str(video)], label='1080p')])
        plan = service.commit_initial_movie_completion(intent)
        before = service.decision_state_store.load(plan.task_id)
        self.create_page(prepared=(root, service, plan, [video]))
        manager = TaskQueueManager(task_root=service.task_root, plan_root=service.plan_store.root,
            decision_state_root=service.decision_state_store.root)
        candidate = dict(namespace='bangumi', entity_type='subject', provider_id='242216',
            title='剧场版 紫罗兰永恒花园', platform='剧场版', library_domain='animation')
        detail = dict(id=242216, type=2, platform='剧场版', name='Violet Evergarden',
            name_cn='剧场版 紫罗兰永恒花园', date='2020-09-18')
        with self.client:
            self.view._show_provider_candidates()
        with patch.object(page, 'task_queue', manager), patch(
                'src.rename.provider_search.BangumiProviderClient._get_json', return_value=detail) as lookup:
            with self.client:
                await self.view._select_provider_candidate(candidate)
        self.assertEqual(1, lookup.call_count, self.notices)
        self.assertTrue(any(getattr(e,'text','')=='确认更换作品' for e in self.client.elements.values()),self.notices)
        self.assertEqual(before, service.decision_state_store.load(plan.task_id), 'preview must not publish')
        with patch('src.rename.provider_search.BangumiProviderClient._get_json', side_effect=AssertionError('no commit lookup')):
            await self.driver.click('确认更换作品')
        changed = service.plan_store.load(plan.task_id)
        after = service.decision_state_store.load(plan.task_id)
        self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(changed),self.notices)
        self.assertEqual('bangumi', changed.work_identity.primary_provider.namespace)
        self.assertEqual(before.content_selection.associations, after.content_selection.associations)
        self.assertEqual(before.content_selection.to_dict()['excluded_sources'], after.content_selection.to_dict()['excluded_sources'])
        self.assertEqual(before.initial_source_basis, after.initial_source_basis)
        self.assertFalse(service.load(plan.task_id).approved_entry_ids)
        self.assertTrue(all(not e.target_path.exists() for e in changed.entries))


class SingleProviderTV(unittest.IsolatedAsyncioTestCase):
    async def test_general_correction_keeps_official_specials(self):
        import json
        from tests.test_initial_tv_completion_contract import InitialTVCompletionContracts
        from src.rename.provider_search import BangumiProviderClient
        fixture = InitialTVCompletionContracts('runTest')
        self.addCleanup(fixture.doCleanups)
        _root, service, _, result, *_ = await fixture.fixture(anime=False)
        def get(_client, endpoint):
            if endpoint == '/v0/subjects/5337':
                return dict(id=5337,type=6,platform='欧美剧',name='Breaking Bad',date='2008-01-20')
            if endpoint.startswith('/v0/episodes'):
                return dict(total=1,limit=100,offset=0,data=[dict(id=9001,subject_id=5337,type=1,
                    sort=1,name='Official Special',name_cn='官方特别篇')])
            raise AssertionError(endpoint)
        with patch.object(BangumiProviderClient,'_get_json',get):
            observation = service.observe_work_correction(result.plan.task_id,PlanStore.fingerprint(result.plan),
                expected_revision=result.state.revision,
                candidate=dict(namespace='bangumi',entity_type='subject',provider_id='5337'))
        special = json.loads(observation.special_json)
        self.assertIsNotNone(special, 'general TV must retain the same official SP channel')
        self.assertEqual('5337',special['provider_identity']['provider_id'])
        self.assertEqual('1',special['catalog'][0]['rows'][0]['source_number'])
        self.assertEqual('官方特别篇',special['catalog'][0]['rows'][0]['title'])

    async def test_explicit_animation_subject_and_season_do_not_require_crosswalk(self):
        from tests.test_initial_tv_completion_contract import InitialTVCompletionContracts
        from src.rename.provider_search import BangumiProviderClient, ProviderSearchError
        fixture = InitialTVCompletionContracts('runTest')
        self.addCleanup(fixture.doCleanups)
        def get(_client, endpoint):
            if endpoint == '/v0/subjects/41658':
                return dict(id=41658,type=2,platform='TV',name='Yama no Susume',name_cn='向山进发',date='2013-01-02')
            if endpoint.startswith('/v0/episodes'):
                return dict(total=0,limit=100,offset=0,data=[])
            raise AssertionError(endpoint)
        with patch.object(BangumiProviderClient,'load_crosswalk_evidence',side_effect=ProviderSearchError('opposite unavailable')) as crosswalk, \
                patch.object(BangumiProviderClient,'_get_json',get):
            _root, _service, _, result, *_ = await fixture.fixture(anime=True,selected_season=1)
        self.assertTrue(result.state.confirmed_work,result.task.get('error'))
        self.assertEqual('bangumi',result.plan.work_identity.primary_provider.namespace)
        self.assertEqual(1,result.state.season_binding['season_number'])
        self.assertEqual('explicit_user',result.state.season_binding['decision_source'])
        self.assertEqual(1,crosswalk.call_count,'optional special reference is attempted once, never a selection gate')
        self.assertEqual('unavailable',result.plan.metadata['tv_completion_special']['relation_reason'])


class GeneralSeasonReference(unittest.TestCase):
    def test_general_page_prefetches_reference_in_same_record_path(self):
        from src.pages.season_reference_view import prepare
        owner = SimpleNamespace(task_decision=SimpleNamespace(library_domain='general'),
            plan=None, task_id='general', review_service=SimpleNamespace(task_root='unused'))
        provider = dict(namespace='tmdb', entity_type='tv', provider_id='1396')
        with patch('src.rename.season_reference_record.start', return_value='future') as start:
            result = prepare(owner, provider)
        self.assertEqual('future', result)
        self.assertEqual(1, start.call_count)

    def test_general_tmdb_reference_uses_type6_and_shared_matcher(self):
        from tests.test_season_reference import evidence
        from src.rename.season_reference import _tmdb_references
        from unittest.mock import Mock
        subjects, relations, info = evidence()
        subjects = tuple({**row, 'platform':'欧美剧', 'library_domain':'general'} for row in subjects)
        info['genres'] = [dict(id=18)]
        bgm = SimpleNamespace(search_animation=Mock(return_value=[]),
            search_subjects=Mock(return_value=[subjects[0]]),
            load_crosswalk_evidence=Mock(return_value=(subjects,relations)))
        result = _tmdb_references(dict(provider_id='50'), bgm,
            SimpleNamespace(get_tv_info_by_id=lambda _: ('Example',info)))
        self.assertEqual({1:'Example',2:'Example Season 2'}, result.names_by_season())
        self.assertEqual(0,bgm.search_animation.call_count)
        self.assertTrue(all(call.kwargs['domain']=='general' for call in bgm.search_subjects.call_args_list))

    def test_general_crosswalk_detail_preserves_subject_type(self):
        from src.rename.provider_search import BangumiProviderClient
        from src.rename.provider_selection_authority import validate_bangumi_subject, provider_selection_capability
        for platform in ('日剧','欧美剧','华语剧','电视剧'):
            with self.subTest(platform=platform):
                detail=dict(id=5337,type=6,platform=platform,name='Sample',name_cn='剧集',date='2008-01-20',eps=7)
                row = BangumiProviderClient._crosswalk_subject_snapshot(detail)
                self.assertIsNotNone(row)
                self.assertEqual('general',row['library_domain'])
                self.assertEqual(platform,row['platform'])
                validate_bangumi_subject(detail,'5337','general','tv_episodes')
                self.assertFalse(provider_selection_capability(row,group_kind='tv_episodes',
                    task_media_kind='tv',task_library_domain='animation').actionable)
