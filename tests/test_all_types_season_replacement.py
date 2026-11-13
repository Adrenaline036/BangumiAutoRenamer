"""Run the existing whole-season transaction/recovery cases with general BGM TV."""
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_whole_season_integration as integration
from tests import _media_flow_helpers as tv
from tests import test_r1_real_media_production_red_contract as flow
from src.rename.plan_review import PlanReviewService
from src.rename.plan_store import PlanStore


class GeneralBangumiSeasonReplacement(integration.WholeSeasonIntegration):
    async def incoming(self,name='incoming'):
        source,font=tv.make_season(self.root/name)
        font.unlink()
        factory=flow._configured_renamer
        def worker(root):
            renamer=factory(root)
            def get(endpoint):
                if endpoint=='/v0/subjects/5337':
                    return dict(id=5337,type=6,platform='欧美剧',name='Ordinary Show',name_cn='普通剧集',date='2020-01-01')
                if endpoint.startswith('/v0/episodes'):
                    return dict(total=0,limit=100,offset=0,data=[])
                raise AssertionError(endpoint)
            renamer.bangumi_search._get_json=get
            return renamer
        from src.pages.plan_review_page import PlanReviewDialog
        select=PlanReviewDialog._select_provider_candidate
        async def selected(owner,candidate,**kwargs):
            return await select(owner,candidate,season_number=1)
        candidate=dict(namespace='bangumi',entity_type='subject',provider_id='5337',
            title='普通剧集',platform='欧美剧',library_domain='general')
        harness=SimpleNamespace(_NoopReviewDialog=flow.R1RealQueueWorkerContracts._NoopReviewDialog)
        with patch.object(flow,'_configured_renamer',side_effect=worker), \
                patch.object(PlanReviewDialog,'_select_provider_candidate',selected), \
                patch('src.rename.process.SERIES_CATALOG_PATH',self.root/'series_catalog'), \
                patch('src.rename.process.RECORD_PATH',self.root/'records'):
            result=await flow.R1RealQueueWorkerContracts._run_flow(harness,root=self.root,source=source,
                task_id=name,candidate=candidate,is_anime=False,is_movie=False)
        self.assertEqual('general',result.plan.work_identity.domain,result.task.get('error'))
        self.assertEqual('bangumi',result.plan.work_identity.primary_provider.namespace)
        self.review=PlanReviewService(PlanStore(self.root/'plans'),self.root/'reviews',
            decision_state_root=self.root/'decisions',task_root=self.root/'tasks',catalog_root=self.root/'work_catalog',
            record_root=self.root/'records',provider_link_catalog_root=self.root/'provider_links')
        return result
