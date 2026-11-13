"""Inventory completion -> explicit Extras -> real worker retry, isolated files."""
from contextlib import contextmanager
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.rename import process
from src.rename.inventory_plan import build_inventory_review_draft
from src.rename.movie_assets import inventory_movie_assets
from src.rename.plan_review import PlanReviewError, PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.decision_state import DecisionCommandService
from src.rename.task_batch_scope import TaskSourceScope
from src.rename.projection_authority import RecognitionDecisionInput
from src.task_queue import TaskQueueManager
from tests.projection_test_support import save_plan_fixture
from tests.test_isolated_sample_acceptance import routing_snapshot_for
from tests.test_r1_real_media_production_red_contract import _configured_renamer
from tests.test_r1_real_media_production_red_contract import _candidate
from tests.source_collection_fixture import admit_task


def completed_tv(root, *, anime=False):
    from concurrent.futures import ThreadPoolExecutor
    from tests.test_initial_tv_completion_contract import InitialTVCompletionContracts
    from tests.test_content_choice_contract import change
    async def complete():
        helper = InitialTVCompletionContracts('runTest')
        def seed(source, extras):
            for name in ('CDs/Album/Disc/Hands Up to the Sky MV.mkv',
                         'CDs/Album/Disc/Hands Up to the Sky MV.mka',
                         'CDs/Album/Disc/Hands Up to the Sky MV.sc.ass',
                         'CDs/Other/Disc/Collaboration Movie.mkv'):
                path=source/name
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(b'isolated companion')
        _, service, _, result, *_ = await helper.fixture(anime=anime,root=root,source_setup=seed)
        helper.assert_success(result)
        plan=result.plan
        videos=[plan.source_root/name for name in ('CDs/Album/Disc/Hands Up to the Sky MV.mkv',
                                                  'CDs/Other/Disc/Collaboration Movie.mkv')]
        hidden=[p for p in videos if p in plan.ignored_sources]
        if hidden:
            plan=change(service,plan,hidden,action='restore')
        for video in videos:
            state=service.decision_state_store.load(plan.task_id)
            plan=service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),
                expected_revision=state.revision,mode='special',
                edit={'entry_id':entry_id(next(e for e in plan.entries if e.source_path==video)),
                      'action':'unmatched_extra','provider_episode_id':None})
        return service,plan,videos[0]
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda:asyncio.run(complete())).result()


@contextmanager
def worker_environment(root, service):
    worker = _configured_renamer(root)
    worker.decision_state_root = service.decision_state_store.root
    worker.provider_link_catalog_root = root/'provider_links'
    settings = dict(mode='链接', anime_path=str(root/'Anime'), bangumi_path=str(root/'TV'),
        movie_path=str(root/'Movies'), anime_movie_path=str(root/'AnimeMovies'))
    with patch.multiple(process, TASK_PATH=service.task_root, PLAN_PATH=service.plan_store.root,
            SERIES_CATALOG_PATH=service.series_catalog.root, cm=SimpleNamespace(get_config=settings.get)), \
            patch.dict(os.environ, BAR_DRY_RUN='false'):
        yield worker


def retry(root, service, plan, *, selection=None):
    task = json.loads((service.task_root/f'{plan.task_id}.json').read_text('utf-8'))
    anime=plan.work_identity.domain=='animation'
    selection=selection or (_candidate('41658') if anime else
        dict(namespace='tmdb', entity_type='tv', provider_id='4242', title='普通剧集', year=2020))
    with worker_environment(root, service) as worker:
        reply = worker.process(plan.source_root, _is_anime=anime, _is_movie=False,
            _tuuid=plan.task_id, cus_season_id=1, use_ai=False,
            provider_selection=selection,
            task_source_scope=TaskSourceScope.from_mapping(task['task_source_scope']))
    return reply, service.plan_store.load(plan.task_id)


class TVInventoryRetry(unittest.IsolatedAsyncioTestCase):
    async def test_reselect_tv_callback_binds_persisted_origin_and_remains_retryable(self):
        from src.pages import plan_review_page as page
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); service,plan,_=completed_tv(root)
            dialog=SimpleNamespace(is_deleted=False, exit_requested=False)
            dialog._inventory_draft_for_completion=lambda:page.PlanReviewDialog._inventory_draft_for_completion(dialog)
            dialog._completion_domain=lambda:page.PlanReviewDialog._completion_domain(dialog)
            dialog.plan=plan; dialog.review_service=service; dialog.series_catalog=service.series_catalog
            dialog.task_decision=service.decision_state_store.load(plan.task_id)
            dialog.decision_command_service=DecisionCommandService(service.decision_state_store,service.plan_store)
            dialog.tv_season_input=SimpleNamespace(value=1)
            dialog.snapshot=SimpleNamespace(issues=[])
            dialog._adopt_revision=lambda revised:setattr(dialog,'plan',revised)
            draft=dialog._inventory_draft_for_completion()
            draft_fp=service.plan_store.fingerprint(draft)
            self.assertFalse((service.plan_store.root/'history'/plan.task_id/(draft_fp+'.json')).exists())
            history={p:p.read_bytes() for p in (service.plan_store.root/'history').rglob('*.json')}
            settings=dict(anime_path=str(root/'Anime'),bangumi_path=str(root/'TV'),
                movie_path=str(root/'Movies'),anime_movie_path=str(root/'AnimeMovies'))
            messages=[]
            with patch.multiple(page,TASK_PATH=service.task_root,cm=SimpleNamespace(get_config=settings.get),
                    notify=lambda message,**kw:messages.append(str(message))):
                await page.PlanReviewDialog._complete_inventory_draft(dialog,draft.presentation_groups[0].group_id,'tv_episodes')
            self.assertNotEqual(service.plan_store.fingerprint(plan),service.plan_store.fingerprint(dialog.plan),messages)
            origin=dialog.plan.metadata['inventory_completion_context']['origin_plan_fingerprint']
            self.assertEqual('inventory_review_draft',service.plan_store.load_version(plan.task_id,origin).metadata['mapping_source'])
            self.assertEqual(history,{p:p.read_bytes() for p in history})
            self.assertFalse(service.load_or_create(dialog.plan).approved_entry_ids)
            _,revised=retry(root,service,dialog.plan)
            task=json.loads((service.task_root/f'{plan.task_id}.json').read_text('utf-8'))
            self.assertIsNone(task.get('error'))
            self.assertEqual(origin,revised.metadata['inventory_completion_context']['origin_plan_fingerprint'])

    def test_worker_retry_keeps_deep_extra_decision_and_audio(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); service, plan, video=completed_tv(root)
            before=service.decision_state_store.load(plan.task_id)
            reply, revised=retry(root, service, plan)
            task=json.loads((service.task_root/f'{plan.task_id}.json').read_text('utf-8'))
            self.assertIsNone(task.get('error'))
            self.assertNotIn('计划持久化', str(reply))
            self.assertGreater(service.decision_state_store.load(plan.task_id).revision, before.revision)
            self.assertEqual('inventory_smart_completion', revised.metadata['mapping_source'])
            self.assertEqual(plan.metadata['provider_special_decisions'], revised.metadata['provider_special_decisions'])
            bundle=[e for e in revised.entries if e.source_path in {video, video.with_suffix('.mka'),video.with_suffix('.sc.ass')}]
            self.assertEqual(3, len(bundle))
            self.assertTrue(all('Extras' in e.target_path.parts for e in bundle))
            self.assertTrue(all(e.season_id==1 for e in bundle))
            subtitle=next(e for e in bundle if e.subtitle is not None)
            self.assertIsNone(subtitle.presentation_asset)
            self.assertEqual(video, subtitle.subtitle.associated_video_source_path)
            self.assertFalse(revised.execution_policy.dry_run)
            self.assertEqual({1,2}, {e.episode_id for e in revised.entries if e.role=='main_episode'})
            self.assertFalse(service.load_or_create(revised).approved_entry_ids)

    def test_changed_deep_source_does_not_commit_old_decision(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); service, plan, video=completed_tv(root)
            before=service.decision_state_store.path_for(plan.task_id).read_bytes()
            video.write_bytes(b'changed-source-content')
            _, revised=retry(root, service, plan)
            self.assertEqual(before, service.decision_state_store.path_for(plan.task_id).read_bytes())
            self.assertEqual(service.plan_store.fingerprint(plan), service.plan_store.fingerprint(revised))
            task=json.loads((service.task_root/f'{plan.task_id}.json').read_text('utf-8'))
            self.assertIn('源文件清单已变化', task['error'])

    def test_deleted_source_is_not_resurrected_from_history(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); service, plan, video=completed_tv(root)
            before=service.decision_state_store.path_for(plan.task_id).read_bytes()
            video.rename(video.with_suffix(".removed"))
            _, revised=retry(root,service,plan)
            self.assertEqual(before,service.decision_state_store.path_for(plan.task_id).read_bytes())
            self.assertEqual(service.plan_store.fingerprint(plan),service.plan_store.fingerprint(revised))

    def test_bangumi_retry_rebuilds_provider_authority_with_all_extras(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); service, plan, video=completed_tv(root,anime=True)
            before=service.decision_state_store.load(plan.task_id)
            _, revised=retry(root,service,plan)
            task=json.loads((service.task_root/f'{plan.task_id}.json').read_text('utf-8'))
            self.assertIsNone(task.get('error'))
            self.assertGreater(service.decision_state_store.load(plan.task_id).revision,before.revision)
            self.assertEqual(plan.metadata['provider_special_decisions'],revised.metadata['provider_special_decisions'])
            self.assertIn(video,{e.source_path for e in revised.entries})
            self.assertIsNotNone(service.decision_state_store.load(plan.task_id).tv_planning_authority)

    def test_identity_change_does_not_replay_old_extra_decisions(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); service, plan, _=completed_tv(root)
            work=replace(plan.work_identity, primary_provider=replace(plan.work_identity.primary_provider,provider_id='999'))
            recognition=RecognitionDecisionInput(plan.task_id,work,(),1,'explicit_user','tv_episodes')
            self.assertIsNone(service.retry_tv_inventory(plan,recognition))
            self.assertFalse(recognition.candidate(service.decision_state_store.load(plan.task_id)).provider_special_decisions)

    def test_current_root_and_policy_replace_old_preview_configuration(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); service, plan, _=completed_tv(root)
            _, revised=retry(root/'new-library',service,plan)
            self.assertFalse(revised.execution_policy.dry_run)
            self.assertTrue(all(e.target_path.is_relative_to(root/'new-library'/'TV') for e in revised.entries))

    def test_disconnected_completion_and_projector_preserve_old_plan_decision_and_review(self):
        class DisconnectedCompletion(PlanReviewService):
            calls=0
            def complete_inventory_draft(self,*args,**kwargs):
                type(self).calls+=1
                raise PlanReviewError('库存重建故障注入')
        for boundary in ('completion','projector'):
            with self.subTest(boundary=boundary),tempfile.TemporaryDirectory() as raw:
                root=Path(raw); service,plan,_=completed_tv(root)
                service.load_or_create(plan)
                paths=[service.plan_store.path_for(plan.task_id),service.decision_state_store.path_for(plan.task_id)]
                paths+=list(service.review_root.rglob('*.json'))
                before={p:p.read_bytes() for p in paths}
                fault=(patch('src.rename.plan_review.PlanReviewService',DisconnectedCompletion) if boundary=='completion'
                       else patch('src.rename.special_decision_projection.project_special_decisions',side_effect=ValueError('投影故障注入')))
                with fault as probe:
                    retry(root,service,plan)
                    if boundary=='projector':
                        self.assertGreater(probe.call_count,0)
                    else:
                        self.assertEqual(1,DisconnectedCompletion.calls)
                self.assertEqual(before,{p:p.read_bytes() for p in paths})

    def test_old_plan_fingerprint_cannot_be_approved_after_successful_retry(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); service,plan,_=completed_tv(root)
            old=service.plan_store.fingerprint(plan)
            _,revised=retry(root,service,plan)
            self.assertNotEqual(old,service.plan_store.fingerprint(revised))
            with self.assertRaises(PlanReviewError):
                service.approve_entries(plan.task_id,old,[entry_id(e) for e in plan.entries])
            self.assertFalse(service.load_or_create(revised).approved_entry_ids)

    def test_changes_between_replay_and_commit_are_rejected(self):
        for change in ('decision','source'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as raw:
                root=Path(raw); service,plan,video=completed_tv(root)
                preserved={}
                class Interleaved(PlanReviewService):
                    def retry_tv_inventory(self,observation,recognition):
                        result=super().retry_tv_inventory(observation,recognition)
                        if change=='decision':
                            episode=next(e for e in plan.entries if e.episode_id==1)
                            service.recomplete_inventory_with_role_decisions(plan.task_id,service.plan_store.fingerprint(plan),
                                decisions={entry_id(episode):'extra'},reason='另一用户改变用途')
                        else:
                            video.write_bytes(b'changed-after-scan-and-before-commit')
                        preserved.update({p:p.read_bytes() for p in (
                            service.plan_store.path_for(plan.task_id),service.decision_state_store.path_for(plan.task_id))})
                        return result
                with patch('src.rename.plan_review.PlanReviewService',Interleaved):
                    retry(root,service,plan)
                self.assertEqual(preserved,{p:p.read_bytes() for p in preserved})
                task=json.loads((service.task_root/f'{plan.task_id}.json').read_text('utf-8'))
                self.assertIn('当前记录已变化' if change == 'decision' else '重试期间', task['error'])

    def test_missing_replay_guard_cannot_commit_a_replayed_draft(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); service,plan,_=completed_tv(root)
            original=process.commit_recognition_projection
            before=service.decision_state_store.path_for(plan.task_id).read_bytes()
            def disconnected(*args,**kwargs):
                self.assertIsNotNone(kwargs.pop('inventory_replay_guard'))
                return original(*args,**kwargs)
            with patch('src.rename.process.commit_recognition_projection',disconnected):
                retry(root,service,plan)
            self.assertEqual(before,service.decision_state_store.path_for(plan.task_id).read_bytes())
            task=json.loads((service.task_root/f'{plan.task_id}.json').read_text('utf-8'))
            self.assertIn('库存重试缺少冻结提交守卫',task['error'])

    def test_legacy_transient_origin_is_repaired_only_via_authenticated_ancestry(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); service,plan,_=completed_tv(root)
            context=dict(plan.metadata['inventory_completion_context'])
            actual=context['origin_plan_fingerprint']
            context['origin_plan_fingerprint']='f'*64
            legacy=replace(plan,metadata={**plan.metadata,'inventory_completion_context':context},
                revision_parent_fingerprint=service.plan_store.fingerprint(plan))
            state=service.decision_state_store.load(plan.task_id)
            _,legacy=DecisionCommandService(service.decision_state_store,service.plan_store).update(plan.task_id,
                expected_revision=state.revision,changes={},planner=lambda _:legacy)
            _,revised=retry(root,service,legacy)
            self.assertEqual(actual,revised.metadata['inventory_completion_context']['origin_plan_fingerprint'])
            self.assertFalse((service.plan_store.root/'history'/plan.task_id/('f'*64+'.json')).exists())
            self.assertEqual('inventory_review_draft',service.plan_store.load_version(plan.task_id,actual).metadata['mapping_source'])

    def test_corrupt_existing_origin_is_not_replaced_by_ancestry(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); service,plan,_=completed_tv(root)
            origin=plan.metadata['inventory_completion_context']['origin_plan_fingerprint']
            path=service.plan_store.root/'history'/plan.task_id/(origin+'.json')
            path.write_text('{}',encoding='utf-8')
            before=service.decision_state_store.path_for(plan.task_id).read_bytes()
            retry(root,service,plan)
            self.assertEqual(before,service.decision_state_store.path_for(plan.task_id).read_bytes())


class TVRetryQueue(unittest.IsolatedAsyncioTestCase):
    async def test_regrouped_card_retry_does_not_reclaim_sibling_sources(self):
        from tests.test_partition_content_contract import PartitionContentContracts
        from tests.test_source_regrouping_preview_contract import five_seasons
        from src.rename.recognition_queue_admission import capture_workspace_open
        helper = PartitionContentContracts()
        def configure(worker):
            self.worker = worker
        async with helper.flow(automatic=False, configure_source=five_seasons, configure_worker=configure) as flow:
            plan, _ = helper.current(flow)
            proposal = flow.manager.preview_source_regrouping(task_id=flow.task_id,
                expected_plan_fingerprint=PlanStore.fingerprint(plan))
            ids = flow.manager.create_regrouped_tasks(proposal)
            selected = ids[0]
            peers = {t: (flow.manager.task_root/f'{t}.json').read_bytes() for t in ids if t != selected}
            expected = {i.source_path for i in TaskSourceScope.from_mapping(
                flow.manager._read(selected)['task_source_scope']).source_identities}
            source_before = {p: p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
            self.worker.search.get_tv_info = lambda *_: self.worker.search.get_tv_info_by_id('4242')
            opened = capture_workspace_open(flow.manager, selected, flow.manager._read(selected))
            await flow.manager.confirm_source_group(selected, opened_evidence=opened)
            await asyncio.wait_for(flow.manager._queue.join(), 12)
            before = flow.service.decision_state_store.load(selected)
            await flow.manager.retry_current(selected)
            await asyncio.wait_for(flow.manager._queue.join(), 12)
            revised = flow.store.load(selected)
            self.assertGreater(flow.service.decision_state_store.load(selected).revision,
                               before.revision, flow.manager._read(selected).get('error'))
            self.assertEqual(expected, {str(p) for p in revised.expected_sources})
            self.assertEqual(peers, {t: (flow.manager.task_root/f'{t}.json').read_bytes() for t in peers})
            self.assertEqual(source_before, {p: p.read_bytes() for p in source_before})
            self.assertFalse(flow.manager._read(flow.task_id))

    async def test_both_queue_entries_reach_complete_inventory_and_commit_once(self):
        for reidentify in (False,True):
            with self.subTest(reidentify=reidentify),tempfile.TemporaryDirectory() as raw:
                root=Path(raw); service,plan,video=completed_tv(root)
                before=service.decision_state_store.load(plan.task_id)
                manager=TaskQueueManager(task_root=service.task_root,plan_root=service.plan_store.root,
                    decision_state_root=service.decision_state_store.root,
                    recovery_abandon_audit_root=root/'abandon')
                with worker_environment(root,service) as worker, patch('src.task_queue.Rename',return_value=worker):
                    try:
                        selection=dict(namespace='tmdb',entity_type='tv',provider_id='4242',title='普通剧集',year=2020)
                        if reidentify:
                            await manager.enqueue_reidentify(plan.task_id,selection,before.revision,season_number=1)
                        else:
                            await admit_task(manager, plan.source_root,task_id=plan.task_id,is_anime=False,is_movie=False,
                                provider_selection=selection,custom_season_id=1)
                        await asyncio.wait_for(manager._queue.join(),timeout=15)
                    finally:
                        await manager.stop()
                task=json.loads((service.task_root/f'{plan.task_id}.json').read_text('utf-8'))
                self.assertIsNone(task.get('error'))
                after=service.decision_state_store.load(plan.task_id)
                self.assertEqual(before.revision+1,after.revision)
                revised=service.plan_store.load(plan.task_id)
                self.assertEqual('inventory_smart_completion',revised.metadata['mapping_source'])
                self.assertIn(video,{e.source_path for e in revised.entries})
                self.assertFalse(service.load_or_create(revised).approved_entry_ids)
                self.assertFalse((root/'TV').exists())
