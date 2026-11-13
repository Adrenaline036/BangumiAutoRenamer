"""普通动画季度建议与原窗口人工继续。"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest
from nicegui import Client
from nicegui.page import page as nice_page
from src.rename.process import Rename
from src.rename.tv_completion_observations import TVSeasonObservation
from src.rename.season_identity import resolve_initial_tv_season
from src.pages import plan_review_page as page
from tests import test_selection_season_same_window as same_window
flat_package = same_window.flat_package
from tests import test_default_activation_contract as activation
from tests import test_initial_auto_link_contract as initial_link


def detail(n, platform='TV'):
    return dict(id=int(n), type=2, name='Ordinary Animation', name_cn='普通动画', platform=platform, date='2020-01-01')


def client_for(edges=(), platforms=None, unavailable=False):
    client = Mock()
    def read(endpoint, **kwargs):
        if '/episodes' in endpoint:
            return dict(total=0, limit=100, offset=0, data=[])
        n=endpoint.split('/')[3]
        if endpoint.endswith('/subjects'):
            if unavailable: raise OSError('季度资料不可用')
            return [dict(id=int(b), relation='续集') for a,b in edges if a==n]+[dict(id=int(a), relation='前传') for a,b in edges if b==n]
        return detail(n, (platforms or {}).get(n,'TV'))
    client._get_json.side_effect=read
    return client


class AdviceContracts(unittest.TestCase):
    def resolve(self, client, selected='101', manual=None):
        worker=SimpleNamespace(bangumi_search=client, search=Mock(), active_consumer_task_id='isolated', initial_movie_request=SimpleNamespace(requested_season=manual))
        candidate=dict(namespace='bangumi', entity_type='subject', provider_id=selected, title='普通动画', library_domain='animation', platform='TV')
        name,info,resolution=Rename._resolve_bangumi_provider_selection(worker,candidate)
        provider=info['_bar_primary_provider']
        work=SimpleNamespace(primary_provider=SimpleNamespace(to_dict=lambda:provider,provider_id=selected))
        observation=TVSeasonObservation.capture(work,info)
        result=resolve_initial_tv_season(observation,work,Path('Ordinary Animation'),manual,resolution)
        self.assertEqual([], worker.search.mock_calls)
        return result

    def test_ordinary_animation_without_folder_season(self):
        self.assertEqual((1,'provider_unique'),self.resolve(client_for()))

    def test_second_tv_subject_from_bangumi_only(self):
        self.assertEqual((2,'provider_unique'),self.resolve(client_for((('101','102'),)), '102'))

    def test_chain_head_and_middle_are_recommendable(self):
        edges=(('101','102'),('102','103'))
        self.assertEqual((1,'provider_unique'),self.resolve(client_for(edges),'101'))
        self.assertEqual((2,'provider_unique'),self.resolve(client_for(edges),'102'))

    def test_multiple_prequels_are_ambiguous(self):
        with self.assertRaisesRegex(ValueError,'季度'):
            self.resolve(client_for((('101','103'),('102','103'))),'103')

    def test_movie_prequel_does_not_count_tv_season(self):
        self.assertEqual((1,'provider_unique'),self.resolve(client_for((('100','101'),), {'100':'剧场版'})))

    def test_branch_cycle_and_unavailable_require_manual(self):
        for client in (client_for((('101','102'),('101','103'))), client_for((('101','102'),('102','101'))), client_for(unavailable=True)):
            with self.subTest(client=client):
                with self.assertRaisesRegex(ValueError,'季度'):
                    self.resolve(client)

    def test_movie_between_tv_seasons_is_not_counted_or_truncated(self):
        for platform in ('剧场版','OVA'):
            with self.subTest(platform=platform):
                self.assertEqual((2,'provider_unique'),self.resolve(client_for((('101','100'),('100','102')), {'100':platform}), '102'))

    def test_twenty_node_chain_is_complete(self):
        edges=tuple((str(n),str(n+1)) for n in range(101,120))
        client=client_for(edges)
        self.assertEqual((20,'provider_unique'),self.resolve(client,'120'))
        self.assertLessEqual(client._get_json.call_count,40)

    def test_twenty_one_node_chain_requires_manual(self):
        edges=tuple((str(n),str(n+1)) for n in range(101,121))
        with self.assertRaisesRegex(ValueError,'季度'):
            self.resolve(client_for(edges),'121')

    def test_manual_input_skips_relations(self):
        client=client_for(unavailable=True)
        self.assertEqual((7,'explicit_user'),self.resolve(client,manual=7))
        client._get_json.assert_called_once_with('/v0/subjects/101')


class OriginalWindowContracts(unittest.IsolatedAsyncioTestCase):
    flow=activation.DefaultActivationProducerContracts.flow
    admit_auto=initial_link.InitialAutoLinkContracts.admit_auto
    click=same_window.SameWindowSeasonContracts.click

    async def exercise(self, *, bangumi=True, fallback_season=None, ova=False, late_status=False):
        workers=[]
        async with self.flow(automatic=False, admission_only=True, admit=self.admit_auto, configure_source=flat_package, configure_worker=workers.append) as flow:
            before_executor={}
            submitted_seasons=[]
            execute=flow.manager._execute_rename
            async def observed_execute(job):
                submitted_seasons.append(job.custom_season_id)
                paths=(flow.manager.plan_store.path_for(job.task_id),flow.manager.decision_state_store.path_for(job.task_id))
                before_executor.clear()
                before_executor.update({p:p.read_bytes() for p in paths})
                return await execute(job)
            flow.manager.executor=observed_execute
            workers[0].bangumi_search=client_for(platforms={'101':'OVA'} if ova else None, unavailable=fallback_season is not None)
            workers[0].search.get_tv_info_by_id=lambda *_: ('普通剧集',{**initial_link.detail(),'seasons':[] if fallback_season is not None else [{'season_number':1}]})
            candidate=dict(namespace='bangumi' if bangumi else 'tmdb',entity_type='subject' if bangumi else 'tv',provider_id='101' if bangumi else '4242',title='普通动画' if bangumi else '普通剧集',original_title='Ordinary Animation',year=2020,library_domain='animation' if bangumi else 'general',platform='OVA' if ova else 'TV',source_url='https://bgm.tv/subject/101' if bangumi else 'https://www.themoviedb.org/tv/4242',snapshot_at='2026-11-01T00:00:00Z')
            client=Client(nice_page('/provider-season-advice'),request=None)
            with client:
                dialog=page.PlanReviewDialog(None,flow.manager._read(flow.task_id)); dialog.open()
            original_id=dialog.id
            from src.rename import ova_selection
            persist=ova_selection.persist_pending
            checked=[]
            def inspect_pending(manager, job, outcome):
                paths=[manager.plan_store.path_for(job.task_id),manager.decision_state_store.path_for(job.task_id)]
                original={p:p.read_bytes() for p in paths}
                task_path=manager.task_root/(job.task_id+'.json')
                task_before=task_path.read_bytes()
                for attribute,value in (('expected_revision',job.expected_revision-1),('request_id','replaced-request')):
                    stale=SimpleNamespace(**vars(job)); setattr(stale,attribute,value)
                    with self.assertRaises(ValueError): persist(manager,stale,outcome)
                    self.assertEqual(task_before,task_path.read_bytes())
                    self.assertEqual(original,{p:p.read_bytes() for p in paths})
                persist(manager,job,outcome)
                self.assertEqual(original,{p:p.read_bytes() for p in paths})
                task=manager._read(job.task_id)
                from src.rename.review_operation import current_provider_selection_review_operation
                _,context=current_provider_selection_review_operation(task)
                key='ova_subject_selection' if ova else 'tv_season_selection'
                self.assertEqual(outcome.to_dict(),context[key])
                self.assertEqual(type(outcome),type(type(outcome).from_mapping(context[key])))
                self.assertIsNotNone(ova_selection.pending_subject(task,context))
                with self.assertRaises(ValueError):
                    ova_selection.pending_subject(task,{**context,'result_decision_revision':-1})
                forged=dict(context[key]); forged['candidate']={**forged['candidate'],'provider_id':'999'}
                with self.assertRaises(ValueError): type(outcome).from_mapping(forged)
                checked.append(key)

            try:
                dialog.provider_url_input.set_value(candidate['source_url'])
                with patch.object(page,'_read_manual_provider_url',return_value=candidate), patch.object(ova_selection,'persist_pending',side_effect=inspect_pending):
                    await self.click(dialog,'读取链接并选择此作品')
                    await asyncio.wait_for(flow.manager._queue.join(),15)
                    if fallback_season is not None:
                        self.assertEqual([None], submitted_seasons, '首次选择未传人工季号')
                        from src.rename.review_operation import current_provider_selection_review_operation
                        task=flow.manager._read(flow.task_id)
                        _,context=current_provider_selection_review_operation(task)
                        self.assertEqual('awaiting_season',context['lifecycle'],task.get('error'))
                        self.assertTrue(before_executor)
                        self.assertEqual(before_executor,{p:p.read_bytes() for p in before_executor})
                        self.assertFalse(task.get('error'))
                        self.assertFalse(flow.manager.decision_state_store.load(flow.task_id).confirmed_work)
                        self.assertEqual(['ova_subject_selection' if ova else 'tv_season_selection'],checked)
                        self.assertTrue(dialog.unresolved_season_input.enabled)
                        if late_status:
                            await flow.manager._disk(flow.manager._update,flow.task_id,review_requested_at='2026-11-01T06:00:00+00:00')
                        dialog.unresolved_season_input.set_value(fallback_season)
                        await self.click(dialog,'确认季度编号')
                        await asyncio.wait_for(flow.manager._queue.join(),15)
                state=flow.manager.decision_state_store.load(flow.task_id)
                self.assertTrue(state.confirmed_work,flow.manager._read(flow.task_id).get('error'))
                self.assertEqual(fallback_season or 1,state.season_binding['season_number'])
                from src.rename.plan_verifier import tv_authority_issue
                self.assertIsNone(tv_authority_issue(dialog.plan))
                from dataclasses import replace
                for invalid in (replace(dialog.plan,projection_receipt=None),
                        replace(dialog.plan,metadata={**dialog.plan.metadata,'season_number':999})):
                    from src.rename.series_registration import validate_plan_series_registration
                    with self.assertRaises(ValueError):
                        validate_plan_series_registration(invalid)
                    if bangumi:
                        self.assertIsNotNone(tv_authority_issue(invalid))
                self.assertEqual(original_id,dialog.id)
                self.assertFalse(dialog.is_deleted)
                labels=[e._props.get('label') for e in dialog.descendants()]
                self.assertIn('作为独立作品整理',labels)
                self.assertIn('更改季度',labels)
                self.assertFalse(any(e.target_path.exists() for e in dialog.plan.entries if e.target_path!=e.source_path))
            finally:
                client.delete()

    async def test_ordinary_bangumi_selection_without_fallback_season_season_original_window(self):
        await self.exercise()

    async def test_unavailable_bangumi_original_window_fallback_season_continue(self):
        await self.exercise(fallback_season=5)

    async def test_empty_tmdb_original_window_fallback_season_continue(self):
        await self.exercise(bangumi=False,fallback_season=4)

    async def test_legacy_ova_pending_remains_readable(self):
        await self.exercise(fallback_season=3,ova=True)

    async def test_ordinary_tmdb_selection_without_manual_season(self):
        await self.exercise(bangumi=False)

    async def test_original_window_after_queue_updates_review_timestamp(self):
        await self.exercise(fallback_season=1,late_status=True)

class TaskSnapshotContracts(unittest.TestCase):
    def make_snapshot(self):
        original=dict(uuid='card',path='/source',review_requested_at='old',review_operation={'request':'old'})
        file=SimpleNamespace(exists=lambda:True,read_bytes=lambda:b'original')
        work=SimpleNamespace(task_id='card',snapshot=SimpleNamespace(task_data=original),files=[(file,b'original'),(file,b'original')])
        return original,work

    def test_only_review_timestamp_is_non_semantic(self):
        from src.pages.provider_selection_work import _check
        original,work=self.make_snapshot()
        _check(SimpleNamespace(_read=lambda _:{**original,'review_requested_at':'new'}),work)

    def test_timestamp_plus_semantic_change_remains_rejected(self):
        from src.pages.provider_selection_work import _check
        original,work=self.make_snapshot()
        for name,value in [('path','/different'),('review_operation',{'request':'new'}),('provider_selection',{'provider_id':'changed'})]:
            current={**original,'review_requested_at':'new',name:value}
            with self.subTest(name=name), self.assertRaises(ValueError):
                _check(SimpleNamespace(_read=lambda _:current),work)

    def test_timestamp_plus_plan_or_decision_bytes_remains_rejected(self):
        from src.pages.provider_selection_work import _check
        original,work=self.make_snapshot()
        for label in ('plan','decision'):
            unchanged=SimpleNamespace(exists=lambda:True,read_bytes=lambda:b'original')
            changed=SimpleNamespace(exists=lambda:True,read_bytes=lambda:b'changed')
            work.files=[(changed if label=='plan' else unchanged,b'original'),
                        (changed if label=='decision' else unchanged,b'original')]
            with self.subTest(file=label), self.assertRaisesRegex(ValueError,'整理计划或人工决定'):
                _check(SimpleNamespace(_read=lambda _:{**original,'review_requested_at':'new'}),work)
