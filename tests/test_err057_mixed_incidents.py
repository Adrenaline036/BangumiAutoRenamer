"""Mixed-library live counterexamples; isolated files and frozen providers only."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.rename.plan_review import _infer_movie_version_descriptor, PlanReviewError, entry_id
from src.rename.plan_store import PlanStore


class MovieEvidenceIncidents(unittest.TestCase):
    def test_cropped_dual_movie_completion_has_two_valid_frozen_versions(self):
        from tests.test_r22_f3_movie_version_evidence_red_contract import MovieVersionEvidenceBoundaryRedContracts
        from src.rename.plan_verifier import PlanVerifier
        from src.rename.projection_authority import validate_plan_projection_receipt
        files=('Example (BD HEVC 2576x1080).mkv','Example (BD HEVC 3840x1608 DV HDR10+).mkv')
        with tempfile.TemporaryDirectory() as temporary:
            fixture=MovieVersionEvidenceBoundaryRedContracts()
            source, plan, content, store, service=fixture._complete_files(
                Path(temporary),'dual-resolution',files,selected_sources=files)
            self.assertEqual({'1080p','2160p Dolby Vision HDR10+'},
                {e.presentation_asset.version_descriptor.display_label for e in content})
            self.assertEqual(2,len({str(e.target_path) for e in content}))
            validate_plan_projection_receipt(plan)
            verified=PlanVerifier().verify(plan)
            self.assertFalse(any(i.code=='presentation_structural_default_not_unique' for i in verified.issues))
            self.assertFalse(any(e.target_path.exists() for e in content))

    def descriptor(self, name, root='[4K+1080p]'):
        source = Path('D:/Synthetic') / root
        return _infer_movie_version_descriptor(source / name, 'movie', source)

    def test_remaster_label_is_not_encoded_resolution(self):
        value = self.descriptor('Example RM4K (1080p BluRay).mkv')
        self.assertEqual('1080p', value.resolution.value)

    def test_cropped_uhd_dimensions_have_distinct_version(self):
        low = self.descriptor('Example (BD HEVC 2576x1080).mkv')
        high = self.descriptor('Example (BD HEVC 3840x1608).mkv')
        self.assertEqual('2160p', high.resolution.value)
        self.assertNotEqual(low.version_ref, high.version_ref)
        self.assertNotEqual('default', high.technical_variant_key)

    def test_compatible_hdr_layers_are_not_mutually_exclusive(self):
        value = self.descriptor('Example.2160p.DV.HDR10+.mkv')
        self.assertEqual('Dolby Vision HDR10+', value.dynamic_range.value)
        self.assertIn('HDR10+', value.display_label)

    def test_incompatible_resolution_and_sdr_still_fail_closed(self):
        for name in ('Example 1080p 2160p.mkv', 'Example SDR HDR10.mkv'):
            with self.subTest(name=name), self.assertRaises(PlanReviewError):
                self.descriptor(name)


class SourcePartitionFontIncident(unittest.IsolatedAsyncioTestCase):
    async def test_retired_partition_never_reaches_old_fault_or_backup_hooks(self):
        from tests.test_r24_err030_unified_exceptional_episode_incident_red_contract import ERR030UnifiedExceptionalEpisodeIncidentRed
        fixture=ERR030UnifiedExceptionalEpisodeIncidentRed()
        for phase in ('backup_hook', 'decision_written', 'plan_written', 'tasks_and_plan_written'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                result, package, paths=await fixture._parent_flow(root,include_fonts=True)
                manager=fixture._manager(root,result)
                await manager.start()
                folders=(result.tasks,result.plans,result.decisions,manager.review_root,
                         manager.projection_commit_service.ownership_store.root,
                         manager.batch_scope_store.root,manager.source_partition_store.root)
                def snapshot():
                    return {str(p):p.read_bytes() for folder in folders for p in folder.rglob('*')
                            if p.is_file() and p.suffix != '.lock'}
                before=snapshot()
                sources={p:p.read_bytes() for p in package.rglob('*') if p.is_file()}
                plan=result.plan_store.load(result.task_id)
                reached=[]
                def checkpoint(value):
                    reached.append(value)
                    if value==phase:
                        raise KeyboardInterrupt('synthetic crash')
                from src.rename.source_partition import SourcePartitionStore
                from src.rename.projection_authority import ProjectionCommitService
                class FailingStore(SourcePartitionStore):
                    def add_backup(self, partition_id, path):
                        reached.append('backup_hook')
                        if phase=='backup_hook': raise RuntimeError('backup failed')
                        return super().add_backup(partition_id,path)
                    def checkpoint(self, partition_id, value):
                        super().checkpoint(partition_id,value)
                        checkpoint(value)
                manager.source_partition_store=FailingStore(manager.source_partition_store.root,
                    allowed_roots=manager.source_partition_store.allowed_roots)
                manager.projection_commit_service=ProjectionCommitService(manager.decision_state_store,
                    manager.plan_store,task_root=manager.task_root,_phase_hook=checkpoint)
                try:
                    from src.rename.plan_execution import PlanExecutionError
                    with self.assertRaisesRegex(PlanExecutionError, '旧二分拆卡已停用'):
                        await manager.partition_task_sources(parent_task_id=result.task_id,
                            expected_plan_fingerprint=PlanStore.fingerprint(plan),
                            selected_entry_ids=(entry_id(fixture._entry_for(plan,paths['cross_work'])),),reason='独立作品')
                finally:
                    await manager.stop()
                restarted=fixture._manager(root,result)
                await restarted.start()
                try:
                    self.assertEqual([],reached)
                    self.assertEqual(before,snapshot())
                    self.assertEqual(sources,{p:p.read_bytes() for p in sources})
                    self.assertEqual([],list(restarted._pending))
                finally:
                    await restarted.stop()

    async def test_retired_partition_preserves_media_authority_and_rejects_domain_override(self):
        from tests.test_r24_err030_unified_exceptional_episode_incident_red_contract import ERR030UnifiedExceptionalEpisodeIncidentRed
        fixture = ERR030UnifiedExceptionalEpisodeIncidentRed()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            result, package, paths = await fixture._parent_flow(root, include_fonts=True)
            manager=fixture._manager(root,result)
            before=result.plan_store.load(result.task_id)
            from src.rename.plan_review import PlanReviewService
            review=PlanReviewService(result.plan_store, root / 'reviews', root / 'work_catalog',
                decision_state_root=result.decisions, task_root=result.tasks)
            sources={p:p.read_bytes() for p in package.rglob('*') if p.is_file()}
            await manager.start()
            try:
                # The live-action override must never change animation authority.
                from src.rename.plan_execution import PlanExecutionError
                state=manager.decision_state_store.load(result.task_id)
                with self.assertRaises(PlanExecutionError):
                    await manager.enqueue_reidentify(result.task_id,
                        dict(state.confirmed_work['primary_provider']),state.revision,season_number=2)
                self.assertEqual(state,manager.decision_state_store.load(result.task_id))
                business_before={str(p):p.read_bytes() for p in result.root.rglob('*')
                    if p.is_file() and p.suffix != '.lock'}
                with self.assertRaisesRegex(PlanExecutionError, '旧二分拆卡已停用'):
                    await manager.partition_task_sources(parent_task_id=result.task_id,
                        expected_plan_fingerprint=PlanStore.fingerprint(before),
                        selected_entry_ids=(entry_id(fixture._entry_for(before,paths['cross_work'])),),
                        reason='用户确认独立作品')
                after=result.plan_store.load(result.task_id)
                self.assertEqual(before,after)
                self.assertIn(paths['cross_work'],after.expected_sources)
                self.assertEqual(business_before,{str(p):p.read_bytes() for p in result.root.rglob('*')
                    if p.is_file() and p.suffix != '.lock'})
                self.assertEqual(sources,{p:p.read_bytes() for p in sources})
            finally:
                await manager.stop()


class InitialWorkspaceIncident(unittest.TestCase):
    def test_disconnected_projector_cannot_create_an_accepted_workspace(self):
        from tests.test_r24_tmdb_optional_animation_entry_incident_red_contract import TMDBOptionalAnimationEntryIncidentContract as Fixture, _AwaitableDialog
        from src.rename.projection_authority import ProjectionAuthorityError
        fixture=Fixture()
        with fixture._persisted_workspace('disconnected') as (tasks,plans,decisions,task):
            before={str(p):p.read_bytes() for folder in (tasks,plans,decisions) for p in folder.glob('*.json')}
            with fixture._public_entry_environment(tasks,plans,decisions), \
                 patch('src.rename.default_content.build_initial_default',side_effect=ProjectionAuthorityError('权威不可用')) as builder, \
                 patch('src.pages.plan_review_page.PlanReviewDialog',return_value=_AwaitableDialog()) as normal, \
                 patch('src.pages.plan_review_page.RecognitionRecoveryDialog',return_value=_AwaitableDialog()) as recovery:
                from src.pages.initial_workspace import create_initial_workspace
                with self.assertRaisesRegex(ProjectionAuthorityError, '权威不可用'):
                    asyncio.run(create_initial_workspace(task['uuid'],task))
            builder.assert_called_once()
            normal.assert_not_called(); recovery.assert_not_called()
            self.assertEqual(before,{str(p):p.read_bytes() for folder in (tasks,plans,decisions) for p in folder.glob('*.json')})

    def test_initial_workspace_does_not_erase_corruption_drift_or_execution(self):
        from tests.test_r24_tmdb_optional_animation_entry_incident_red_contract import TMDBOptionalAnimationEntryIncidentContract as Fixture, _AwaitableDialog
        from src.utils.utils import write_task_at
        fixture=Fixture()
        for incident in ('corrupt_plan','corrupt_decision','source_drift','executed'):
            with self.subTest(incident=incident), fixture._persisted_workspace('unsafe') as (tasks,plans,decisions,task):
                if incident=='corrupt_plan': (plans/'unsafe.json').write_bytes(b'broken-plan')
                elif incident=='corrupt_decision': (decisions/'unsafe.json').write_bytes(b'broken-decision')
                elif incident=='source_drift': (Path(task['path'])/'01.mkv').write_bytes(b'changed')
                else:
                    task['execution_status']='completed'
                    write_task_at(tasks,'unsafe',task)
                before={str(p):p.read_bytes() for folder in (tasks,plans,decisions) for p in folder.glob('*.json')}
                with fixture._public_entry_environment(tasks,plans,decisions), \
                     patch('src.pages.plan_review_page.PlanReviewDialog',return_value=_AwaitableDialog()) as normal, \
                     patch('src.pages.plan_review_page.RecognitionRecoveryDialog',return_value=_AwaitableDialog()) as recovery:
                    if incident in ('corrupt_plan','corrupt_decision'):
                        asyncio.run(fixture._open_from_ui_slot('unsafe'))
                        normal.assert_not_called(); recovery.assert_called_once()
                    else:
                        from src.pages.initial_workspace import create_initial_workspace
                        with self.assertRaises((ValueError, RuntimeError)):
                            asyncio.run(create_initial_workspace('unsafe',task))
                        normal.assert_not_called(); recovery.assert_not_called()
                self.assertEqual(before,{str(p):p.read_bytes() for folder in (tasks,plans,decisions) for p in folder.glob('*.json')})
                # The recovery URL callback must not turn damaged or stale
                # state into a fresh initial Decision either.
                from types import SimpleNamespace
                from src.pages.recognition_recovery_view import RecognitionRecoveryDialog
                dialog=SimpleNamespace(task_id='unsafe',media_type=SimpleNamespace(value='anime'),
                    provider_url=SimpleNamespace(value='https://bgm.tv/subject/123'),
                    path_input=SimpleNamespace(value=task['path']),close=lambda:None,
                    _selecting=False,_closed=False,is_deleted=False,selection_progress=Mock())
                candidate=dict(namespace='bangumi',entity_type='subject',provider_id='123',title='测试作品')
                with fixture._public_entry_environment(tasks,plans,decisions), \
                     patch('src.pages.recognition_recovery_view.notify'), \
                     patch('src.pages.plan_review_page.resolve_provider_item_url',return_value=candidate):
                    asyncio.run(RecognitionRecoveryDialog._select_url(dialog))
                self.assertEqual(before,{str(p):p.read_bytes() for folder in (tasks,plans,decisions) for p in folder.glob('*.json')})

    def test_search_detail_requests_same_locale_as_direct_link(self):
        from src.rename.get_info import Search
        class Query:
            def movie(self, **kwargs):
                self.results=[dict(id=123,title='测试电影',original_title='Example')]
        class Movie:
            def __init__(self, provider_id):
                self.id=provider_id
            def info(self, **kwargs):
                self.title='测试电影' if kwargs.get('language')=='zh-CN' else 'Example'
                self.original_title='Example'
                return dict(self.__dict__)
        with patch('src.rename.get_info.tmdb.Search',Query), patch('src.rename.get_info.tmdb.Movies',Movie):
            search=Search.__new__(Search)
            _, searched=search.get_movie_info('测试电影',2020)
            _, linked=search.get_movie_info_by_id(123)
        self.assertEqual(linked['title'],searched['title'])

    def test_general_tv_has_explicit_season_change_entry(self):
        from src.pages.plan_review_page import PlanReviewDialog
        self.assertTrue(callable(getattr(PlanReviewDialog,'_change_general_tv_season',None)),
                        '真人TV需要复用正常工作区的季度更改入口')

    def test_first_failure_keeps_direct_link_workspace_without_name(self):
        from tests.test_r24_tmdb_optional_animation_entry_incident_red_contract import TMDBOptionalAnimationEntryIncidentContract as Fixture, _AwaitableDialog
        from src.utils.utils import write_task_at
        fixture=Fixture()
        with fixture._persisted_workspace('failed-first-recognition',is_anime=False,is_movie=True) as (tasks,plans,decisions,task):
            task.update(plan_status='invalid', name='', error='作品标题尚未确认')
            write_task_at(tasks,task['uuid'],task)
            opened=_AwaitableDialog()
            with fixture._public_entry_environment(tasks,plans,decisions), \
                 patch('src.pages.plan_review_page.PlanReviewDialog',return_value=opened) as normal, \
                 patch('src.pages.plan_review_page.RecognitionRecoveryDialog',return_value=_AwaitableDialog()) as recovery:
                asyncio.run(fixture._open_from_ui_slot(task['uuid']))
            normal.assert_called_once()
            recovery.assert_not_called()
            self.assertIsNone(normal.call_args.args[0])
            self.assertFalse(PlanStore(plans).path_for(task['uuid']).exists())
            self.assertFalse(list(decisions.glob('*.json')))
