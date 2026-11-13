import unittest
from unittest.mock import patch
from tests import test_all_types_provider_entry as entry
from tests import test_r1_real_media_production_red_contract as flow
from src.pages.plan_review_page import PlanReviewDialog
from src.task_queue import TaskQueueManager

class ActualCandidatePresentation(unittest.IsolatedAsyncioTestCase):
    run_selected = entry.AllTypesProviderEntry.run_selected
    async def official(self,anime,movie):
        original=PlanReviewDialog._select_provider_candidate
        run = flow.R1RealQueueWorkerContracts._run_flow
        captured=[]
        async def with_actual_fields(owner,candidate,**kwargs):
            selected=dict(candidate,poster_url='https://lain.bgm.tv/pic/cover/l/test.jpg',snapshot_at='2026-09-29T03:09:30+00:00')
            captured.append(selected)
            return await original(owner,selected,**kwargs)
        async def checked(*args,**kwargs):
            result=await run(*args,**kwargs)
            expected=captured[-1]
            self.assertTrue(result.state.confirmed_work,result.task.get('error'))
            primary=result.plan.work_identity.primary_provider.to_dict()
            evidence=next(row for row in result.state.provider_evidence if str(row.get('provider_id'))==expected['provider_id'])
            for field in ('poster_url','source_url','snapshot_at'):
                self.assertEqual(expected[field],primary[field],field)
                self.assertEqual(expected[field],evidence[field],field)
                self.assertFalse(TaskQueueManager._selected_provider_was_persisted(
                    dict(expected,**{field:'different-observation'}),result.state.provider_evidence,result.plan),field)
            self.assertTrue(TaskQueueManager._selected_provider_was_persisted(expected,result.state.provider_evidence,result.plan))
            return result
        with patch.object(PlanReviewDialog,'_select_provider_candidate',with_actual_fields),patch.object(flow.R1RealQueueWorkerContracts,'_run_flow',checked):
            await self.run_selected(anime=anime,movie=movie,namespace='bangumi')
    async def test_animation_movie_official_poster(self):await self.official(True,True)
    async def test_general_movie_official_poster(self):await self.official(False,True)
    async def test_general_tv_official_poster(self):await self.official(False,False)



import asyncio
import threading
import time
import unittest
from unittest.mock import patch
from tests import test_initial_auto_link_contract as old
from src.pages import plan_review_page as page

class InitialHydrationFeedback(unittest.IsolatedAsyncioTestCase):
    flow=old.InitialAutoLinkContracts.flow
    invoke=old.InitialAutoLinkContracts.invoke
    admit_auto=old.InitialAutoLinkContracts.admit_auto

    async def test_initial_close_or_edited_link_during_snapshot_discards_late_read(self):
        for action in ('close','edit'):
            with self.subTest(action=action):
                async with self.flow(movie=True,automatic=False,admission_only=True,admit=self.admit_auto) as flow:
                    loop=asyncio.get_running_loop()
                    created=[]
                    constructor=page.PlanReviewDialog.__init__
                    prepare=page.prepare_plan_review_open
                    def opened(view,*args,**kwargs):
                        constructor(view,*args,**kwargs);created.append(view);view.open()
                    changed=threading.Event()
                    def change():
                        if action=='close':created[0]._close_review()
                        else:created[0].provider_url_input.value='https://www.themoviedb.org/movie/999'
                        changed.set()
                    def delayed(plan):
                        loop.call_soon_threadsafe(change)
                        if not changed.wait(3):raise TimeoutError('GUI loop blocked')
                        return prepare(plan)
                    with patch.object(page.PlanReviewDialog,'__init__',new=opened), \
                            patch.object(page,'prepare_plan_review_open',side_effect=delayed), \
                            patch.object(page.PlanReviewDialog,'_load_plan_view') as hydrated:
                        await self.invoke(flow,old.detail(movie=True))
                    hydrated.assert_not_called()
                    self.assertTrue(changed.is_set())

    async def test_initial_real_link_keeps_event_loop_alive_and_reuses_dialog(self):
        async with self.flow(movie=True,automatic=False,admission_only=True,admit=self.admit_auto) as flow:
            loop=asyncio.get_running_loop(); main=threading.get_ident()
            original=page.build_plan_review_snapshot
            load=page.PlanReviewDialog._load_plan_view
            samples=[]; hydrated=[]; prepared=[]
            created=[]
            constructor=page.PlanReviewDialog.__init__
            def opened(view,*args,**kwargs):
                constructor(view,*args,**kwargs)
                created.append(view)
                view.open()
            prepare=page.prepare_plan_review_open
            def prepare_spy(*a,**k):
                prepared.append(threading.get_ident());return prepare(*a,**k)
            def blocked(plan):
                tick=threading.Event(); release=threading.Event()
                loop.call_soon_threadsafe(tick.set)
                timer=threading.Timer(.3,release.set);timer.start()
                release.wait(2)
                samples.append(dict(thread=threading.get_ident(),main=main,heartbeat_during_read=tick.is_set()))
                timer.join()
                return original(plan)
            def hydrated_spy(view,plan,**kwargs):
                self.assertEqual(1,len(created))
                self.assertIs(view,created[0])
                result=load(view,plan,**kwargs)
                hydrated.append((id(created[0]),id(view),view.plan.task_id,hasattr(view,'snapshot')))
                return result
            with patch.object(page.PlanReviewDialog,'__init__',new=opened),patch.object(page,'build_plan_review_snapshot',side_effect=blocked),patch.object(page,'prepare_plan_review_open',side_effect=prepare_spy),patch.object(page.PlanReviewDialog,'_load_plan_view',new=hydrated_spy):
                _,messages=await self.invoke(flow,old.detail(movie=True))
            self.assertTrue(hydrated,'real link must reach final readable same dialog')
            self.assertEqual(1,len(created),'selection must not replace the open dialog')
            self.assertTrue(all(a==b and c==flow.task_id and d for a,b,c,d in hydrated))
            self.assertTrue(samples,'real snapshot must be exercised')
            self.assertTrue(all(s['heartbeat_during_read'] and s['thread']!=main for s in samples),'initial link snapshot blocks the GUI loop instead of using existing background preparation')
            self.assertTrue(prepared,'initial hydration must share the opening preparation')


import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
from nicegui import ui
from nicegui.client import Client
from nicegui.page import page as nice_page
from src.pages.general_season_view import render_unresolved_tmdb_season,general_tv_provider
from src.rename.season_reference import SeasonReferences
from tests import test_all_types_provider_entry as entry

class MissingSeasonReference(unittest.IsolatedAsyncioTestCase):
    async def render(self,domain,expect_reference):
        client=Client(nice_page('/isolated-unresolved-season'),request=None)
        self.addCleanup(client.delete)
        candidate=dict(namespace='bangumi',entity_type='subject',provider_id='5337',platform='欧美剧' if domain=='general' else 'TV',title='第一季',library_domain=domain)
        context=dict(lifecycle='failed',failure_code='relation_unresolved',candidate=candidate)
        state=SimpleNamespace(confirmed_work={},revision=1,library_domain=domain)
        owner=SimpleNamespace(task_decision=state,task_data={},task_id='failed-first',plan=SimpleNamespace(metadata={}),client=client,exit_requested=False,is_deleted=False,on_value_change=lambda _:None,_select_provider_candidate=AsyncMock())
        reference=SeasonReferences(subjects=(candidate,{**candidate,'provider_id':'5338','title':'第二季'}),assignments=(('5337',1),('5338',2)),status='found')
        calls=[]
        def prepared(*a,**k):
            calls.append((a,k));future=asyncio.get_running_loop().create_future();future.set_result(reference);return future
        with patch('src.rename.review_operation.build_review_operation',return_value=(SimpleNamespace(context=context),None)),patch('src.pages.plan_review_page.PlanStore.fingerprint',return_value='frozen'),patch('src.pages.season_reference_view.prepare',side_effect=prepared):
            with client:render_unresolved_tmdb_season(owner,ui)
            for _ in range(5):await asyncio.sleep(0)
            numbers=[e for e in client.elements.values() if type(e).__name__=='Number']
            self.assertTrue(numbers,'manual season must remain available')
            self.assertEqual({},state.confirmed_work);self.assertEqual(1,state.revision);owner._select_provider_candidate.assert_not_awaited()
            if expect_reference:
                pickers=[e for e in client.elements.values() if type(e).__name__=='Select' and e._props.get('label')=='Bangumi 季度条目']
                self.assertEqual(1,len(pickers),'failed first Bangumi selection must use shared named season reference picker')
                self.assertIn('第二季',str(pickers[0].options));self.assertIn('第 1 季',str(pickers[0].options));self.assertEqual(1,len(calls))
    async def test_animation_unresolved_has_named_reference(self):await self.render('animation',True)
    async def test_general_unresolved_has_named_reference(self):await self.render('general',True)
    async def test_manual_fallback_display_does_not_publish(self):await self.render('general',False)

class MovieReopen(unittest.IsolatedAsyncioTestCase):
    run_selected=entry.AllTypesProviderEntry.run_selected
    async def actual_movie(self,anime):
        from src.rename.plan_review import PlanReviewService
        original=PlanReviewService.commit_initial_movie_completion
        observations=[]
        def committed(service,*a,**k):
            plan=original(service,*a,**k)
            state=service.decision_state_store.load(plan.task_id)
            if plan.work_identity.primary_provider.namespace=='bangumi':
                observations.append(plan.work_identity.domain)
                self.assertIsNone(general_tv_provider(SimpleNamespace(plan=plan,task_decision=state)),'completed Bangumi movie must not enter TV-only season reference guard')
            return plan
        with patch.object(PlanReviewService,'commit_initial_movie_completion',new=committed):
            await self.run_selected(anime=anime,movie=True,namespace='bangumi')
        self.assertTrue(observations)
    async def test_animation_bangumi_movie_reopen_guard(self):await self.actual_movie(True)
    async def test_general_bangumi_movie_reopen_guard(self):await self.actual_movie(False)
