"""Real queue/worker and opt-in GUI season change for live-action TV."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.pages.plan_review_page import PlanReviewDialog
from src.rename.plan_execution import PlanExecutionError
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.task_queue import TaskQueueManager
from tests.test_provider_subject_plan_wiring_red_contract import _configured_renamer, _root_config_patch
from tests.test_complex_library_special_red_contract import _CaptureUi
from tests.source_collection_fixture import admit_task


class GeneralSeasonFlow(unittest.IsolatedAsyncioTestCase):
    async def test_opt_in_picker_updates_real_worker_and_invalidates_approval(self):
        await self._flow()

    async def test_initial_auto_can_select_link_without_entering_type_or_name(self):
        await self._flow(initial_auto=True)

    async def _flow(self, initial_auto=False):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root=Path(temporary)
            source=root/'Example Season 1'; source.mkdir()
            for n in (1,2): (source/f'Example.S01E{n:02d}.mkv').write_bytes(b'video')
            tasks, plans, decisions=(root/name for name in ('tasks','plans','decisions'))
            renamer=_configured_renamer(root)
            info=dict(id=123,name='测试剧集',original_name='Example',first_air_date='2020-01-01',
                      genres=[dict(id=18,name='Drama')],seasons=[dict(season_number=n,name=f'第{n}季',episode_count=2,air_date='2020-01-01') for n in (0,1,2)])
            renamer.search=SimpleNamespace(TMDB_KEY='offline',get_tv_info_by_id=lambda _id: ('测试剧集',info),
                get_season_info=lambda _id,n:dict(season_number=n,episodes=[]))
            manager=TaskQueueManager(task_root=tasks,plan_root=plans,decision_state_root=decisions,
                recovery_abandon_audit_root=root/'abandon')
            for name,value in {'TASK_PATH':tasks,'PLAN_PATH':plans,'SERIES_CATALOG_PATH':root/'series_catalog','RECORD_PATH':root/'record'}.items():
                stack.enter_context(patch(f'src.rename.process.{name}',value))
            stack.enter_context(_root_config_patch(dict(bangumi_path=str(renamer.BANGUMI_PATH),
                movie_path=str(renamer.MOVIE_PATH),anime_path=str(renamer.ANIME_PATH),anime_movie_path=str(renamer.ANIME_MOVIE_PATH))))
            stack.enter_context(patch('src.task_queue.Rename',return_value=renamer))
            stack.enter_context(patch('src.pages.plan_review_page.task_queue',manager))
            stack.enter_context(patch('src.pages.plan_review_page.get_task',side_effect=manager._read))
            for name,value in {'TASK_PATH':tasks,'PLAN_PATH':plans,'DECISION_STATE_PATH':decisions}.items():
                stack.enter_context(patch(f'src.pages.plan_review_page.{name}',value))
            stack.enter_context(patch('src.pages.plan_review_page.notify'))
            stack.enter_context(patch('src.pages.recognition_recovery_view.notify'))
            stack.enter_context(patch('src.pages.general_season_view.notify'))
            stack.enter_context(patch('src.rename.get_info.Search',return_value=renamer.search))
            candidate=dict(namespace='tmdb',entity_type='tv',provider_id='123',title='测试剧集',
                           original_title='Example',year=2020,library_domain='general')
            await manager.start()
            try:
                renamer._provider_search_batch=Mock(return_value=SimpleNamespace(candidates=()))
                renamer._task_type_from_provider_batch=Mock(return_value='需要人工选择作品')
                task_id=await admit_task(manager, source,is_anime=None if initial_auto else False,
                    is_movie=None if initial_auto else False)
                await asyncio.wait_for(manager._queue.join(),12)
                from src.pages.plan_review_page import plan_review_page
                from tests.test_r24_tmdb_optional_animation_entry_incident_red_contract import _AwaitableDialog
                from nicegui.client import Client
                with Client.auto_index_client.content, patch('src.pages.plan_review_page.PlanReviewDialog',return_value=_AwaitableDialog()), \
                     patch('src.pages.plan_review_page.RecognitionRecoveryDialog',return_value=_AwaitableDialog()):
                    await plan_review_page(task_id)
                with patch('src.pages.plan_review_page.resolve_provider_item_url',return_value=candidate) as transport:
                    if initial_auto:
                        from src.pages.provider_selection_work import select
                        self.assertFalse(manager.decision_state_store.path_for(task_id).exists())
                        first=SimpleNamespace(task_id=task_id,plan=None,exit_requested=False,is_deleted=False,
                            client=Client.auto_index_client, task_data=manager._read(task_id),
                            rerecognition_media_type=SimpleNamespace(value='auto'),
                            rerecognition_path=SimpleNamespace(value=str(source)),
                            provider_url_input=SimpleNamespace(value='https://www.themoviedb.org/tv/123',is_deleted=False),
                            initial_progress=Mock(),initial_options=Mock(),_load_plan_view=Mock())
                        with patch('src.pages.plan_review_page._prepare_visible_plan_dialog',new_callable=AsyncMock) as prepare:
                            await select(first)
                        prepare.assert_awaited_once()
                        self.assertIs(first,prepare.await_args.args[0])
                        self.assertEqual(task_id,prepare.await_args.args[1].task_id)
                    else:
                        from nicegui import Client
                        first=SimpleNamespace(client=Client.auto_index_client, task_decision=manager.decision_state_store.load(task_id),
                            plan=manager.plan_store.load(task_id),task_data=manager._read(task_id),
                            provider_url_input=SimpleNamespace(value='https://www.themoviedb.org/tv/123'))
                        first._select_provider_candidate=PlanReviewDialog._select_provider_candidate.__get__(first)
                        await PlanReviewDialog._select_manual_provider_url(first)
                    await asyncio.wait_for(manager._queue.join(),12)
                transport.assert_called_once()
                plan=manager.plan_store.load(task_id)
                state=manager.decision_state_store.load(task_id)
                self.assertEqual(1,state.season_binding['season_number'])
                self.assertEqual('inferred_path',state.season_binding['decision_source'])
                original_fp=PlanStore.fingerprint(plan)
                from src.rename.initial_movie_completion import RootSnapshotSource
                from src.rename.target_routing import TargetRootSnapshot
                roots = RootSnapshotSource(lambda: TargetRootSnapshot.capture(dict(bangumi_path=renamer.BANGUMI_PATH,
                    movie_path=renamer.MOVIE_PATH, anime_path=renamer.ANIME_PATH, anime_movie_path=renamer.ANIME_MOVIE_PATH)))
                review=PlanReviewService(manager.plan_store,manager.review_root,
                    catalog_root=manager.execution_service.work_catalog.root, task_root=tasks,decision_state_root=decisions,
                    record_root=manager.execution_service.record_root, provider_link_catalog_root=manager.provider_link_catalog_root,
                    root_snapshot_source=roots)
                review.series_catalog=manager.execution_service.series_catalog
                plan=review.revise_tv_content(task_id,original_fp,expected_revision=state.revision,mode='fonts',edit={})
                state=manager.decision_state_store.load(task_id)
                original_fp=PlanStore.fingerprint(plan)
                review.approve_entries(task_id,original_fp,[entry_id(e) for e in plan.entries])
                peer=tasks/'unrelated.json'; peer.write_text('{"uuid":"unrelated","workflow_status":"need_confirm"}',encoding='utf-8')
                peer_before=peer.read_bytes()
                dialog=SimpleNamespace(task_decision=state,plan=plan,task_data=manager._read(task_id), review_service=review)
                dialog._adopt_revision=lambda value:setattr(dialog,'plan',value)
                for method in ('_general_tv_provider','_change_general_tv_season','_select_provider_candidate'):
                    setattr(dialog,method,getattr(PlanReviewDialog,method).__get__(dialog))
                ui=_CaptureUi()
                with patch('src.pages.plan_review_page.ui',ui):
                    PlanReviewDialog._render_general_tv_season(dialog)
                    self.assertIn('更改季度',ui.callbacks)
                    await ui.callbacks['更改季度']()
                    options,picker=ui.selects[-1]
                    self.assertEqual([0,1,2],list(options))
                    picker.value=2
                    await ui.callbacks['确认季度并生成新计划']()
                    await asyncio.wait_for(manager._queue.join(),12)
                updated=manager.plan_store.load(task_id)
                updated_state=manager.decision_state_store.load(task_id)
                self.assertEqual(2,updated_state.season_binding['season_number'], manager._read(task_id).get('error'))
                self.assertEqual('explicit_user',updated_state.season_binding['decision_source'])
                self.assertTrue(all(e.season_id==2 for e in updated.entries))
                self.assertTrue(all('Season 02' in str(e.target_path) for e in updated.entries))
                self.assertGreater(updated_state.revision,state.revision)
                self.assertNotEqual(original_fp,PlanStore.fingerprint(updated))
                with self.assertRaises(PlanExecutionError): manager.execution_service.validate_approved(task_id,original_fp)
                with self.assertRaises(PlanExecutionError):
                    await manager.enqueue_reidentify(task_id,candidate,state.revision,season_number=1)
                self.assertEqual(peer_before,peer.read_bytes())
                self.assertFalse(any(e.target_path.exists() for e in updated.entries))
                self.assertEqual([b'video',b'video'],[p.read_bytes() for p in source.iterdir()])
                for bad in (-1, True, 1.5):
                    with self.subTest(bad=bad), self.assertRaises(PlanExecutionError):
                        await manager.enqueue_reidentify(task_id,candidate,updated_state.revision,season_number=bad)
                with self.assertRaises(PlanExecutionError):
                    await manager.enqueue_reidentify(task_id,{**candidate,'provider_id':'456'},updated_state.revision,season_number=2)
                # Season editing uses the canonical content command, never the old work-selection queue.
                with self.assertRaises((ValueError, RuntimeError)):
                    review.revise_tv_content(task_id,PlanStore.fingerprint(updated),
                        expected_revision=updated_state.revision,mode='season',edit={'season_number':-1})
                self.assertEqual(PlanStore.fingerprint(updated),PlanStore.fingerprint(manager.plan_store.load(task_id)))
                self.assertEqual(updated_state,manager.decision_state_store.load(task_id))
                # Season 0 is only selected explicitly, never the default.
                review.revise_tv_content(task_id,PlanStore.fingerprint(updated),
                    expected_revision=updated_state.revision,mode='season',edit={'season_number':0})
                special=manager.decision_state_store.load(task_id)
                self.assertEqual(0,special.season_binding['season_number'])
                self.assertEqual('explicit_user',special.season_binding['decision_source'])
                self.assertEqual(peer_before,peer.read_bytes())
            finally:
                await manager.stop()
