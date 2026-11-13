"""ERR059: real recognition/completion/approval entries in disposable roots."""
import inspect
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from src.rename.decision_state import DecisionStateStore
from src.rename.plan_store import PlanStore
from tests import _media_flow_helpers as movie_fixture
from tests import _media_flow_helpers as tv_fixture
from tests import test_complex_library_special_red_contract as special_fixture


class DeliveryIncidents(unittest.IsolatedAsyncioTestCase):
    async def test_single_file_movie_uses_same_completion_without_sibling_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            fixture,result,primary=await self.movie(root,file_only=True)
            self.assertEqual('inventory_review_draft',result.plan.metadata['mapping_source'])
            completed=await fixture._complete_movie(root=root,result=result,primary=primary)
            self.assertEqual({primary[0]},set(completed.plan.expected_sources))
            task,ledger,_=await fixture._approve_and_execute(root=root,result=completed)
            self.assertEqual('completed',task.get('execution_status'),task.get('error'))
            self.assertEqual(1,len(ledger['entries']))

    async def test_ambiguous_audio_projector_cannot_accept_old_guessed_target(self):
        from dataclasses import replace
        from src.rename.audio_projection import project_audio, validate_audio
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            fixture,result,primary=await self.movie(root,ambiguous=True)
            plan= (await fixture._complete_movie(root=root,result=result,primary=primary)).plan
            video=next(e for e in plan.entries if e.source_path==primary[0])
            # Both videos and the audio were observed before real admission.
            audio=next(e for e in plan.entries if e.source_path==primary[0].with_suffix('.mka'))
            self.assertEqual('external_audio_unresolved',audio.role)
            guessed=replace(audio,target_path=video.target_path.with_suffix('.mka'),
                season_id=video.season_id,episode_id=video.episode_id,role='external_audio')
            ambiguous=replace(plan,entries=tuple(guessed if e==audio else e for e in plan.entries))
            with self.assertRaises(ValueError): validate_audio(ambiguous)
            projected=project_audio(ambiguous)
            unresolved=next(e for e in projected.entries if e.source_path==audio.source_path)
            self.assertEqual('external_audio_unresolved',unresolved.role)
            self.assertIsNone(unresolved.episode_id)
            with self.assertRaises(ValueError): validate_audio(projected)
            self.assertEqual(PlanStore.fingerprint(plan),PlanStore.fingerprint(PlanStore(root/'plans').load(plan.task_id)))

    async def test_unmatched_audio_blocks_until_explicit_gui_exclusion(self):
        from src.rename.plan_review import entry_id, PlanReviewError
        from src.rename.plan_verifier import PlanVerifier
        from src.pages.plan_review_page import PlanReviewDialog
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            fixture,result,primary=await self.movie(root,orphan=True)
            # Input exists before source admission, without duplicate AddTask.
            orphan=primary[0].parent/'Unmatched.mka'
            completed=await fixture._complete_movie(root=root,result=result,primary=primary)
            plan=completed.plan
            self.assertIn('external_audio_projection_invalid',{i.code for i in PlanVerifier().verify(plan).issues})
            selected=next(e for e in plan.entries if e.source_path==orphan)
            self.assertEqual('external_audio_unresolved',selected.role)
            self.assertIsNone(selected.episode_id)
            with self.assertRaises(PlanReviewError):
                completed.review.approve_entries(plan.task_id,PlanStore.fingerprint(plan),[entry_id(e) for e in plan.entries])
            from tests.test_content_workbench_gui import ContentWorkbenchGui
            gui=ContentWorkbenchGui('runTest')
            gui.create_page(prepared=(root,completed.review,plan,primary),cleanup_owner=self)
            with gui.client:
                gui.view._exclude_unresolved_audio(entry_id(selected))
            self.assertEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(completed.review.plan_store.load(plan.task_id)))
            await gui.driver.click('排除整理')
            revised=completed.review.plan_store.load(plan.task_id)
            self.assertIn(orphan,revised.ignored_sources)
            self.assertEqual(b'audio',orphan.read_bytes())
            self.assertNotIn('external_audio_projection_invalid',{i.code for i in PlanVerifier().verify(revised).issues})
            completed.review.approve_entries(revised.task_id,PlanStore.fingerprint(revised),[entry_id(e) for e in revised.entries])

    async def test_combined_movie_owner_allows_one_version_replacement(self):
        from tests.test_completed_rebuild_work_contract import CompletedMovieRebuildContracts, file_identity
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            fixture,result,primary=await self.movie(root,multi=True)
            completed=await fixture._complete_movie(root=root,result=result,primary=primary)
            task,_,_=await fixture._approve_and_execute(root=root,result=completed)
            retained=next(e for e in completed.plan.entries if e.source_path==primary[1])
            before=file_identity(retained.target_path)
            incoming=root/'replacement'/'Example Movie [1080p]'
            incoming.mkdir(parents=True)
            new=incoming/primary[0].name
            shutil.copyfile(primary[0],new)
            with new.open('ab') as stream: stream.write(b'replacement')
            initial=await fixture._run_provider_flow(root,incoming,'replace-one')
            pending=await fixture._complete_movie(root=root,result=initial,primary=new,
                version_groups=[dict(sources=[str(new)],label='1080p-待换源')])
            h=CompletedMovieRebuildContracts('runTest'); h.root=root
            review=h.execution_service().review_service
            selected_ref=next(e.presentation_asset.version_descriptor.version_ref for e in completed.plan.entries
                if e.source_path==primary[0])
            pending.plan=review.revise_movie_versions(pending.plan.task_id,PlanStore.fingerprint(pending.plan),
                groups=[dict(sources=[str(new)],label='1080p')],excluded_sources=[],replace_version_ref=selected_ref)
            operation,_=review.asset_actions().prepare_replacement(pending.plan)
            revised=review.confirm_asset_replacement(pending.plan.task_id,PlanStore.fingerprint(pending.plan),
                expected_operation_fingerprint=operation.fingerprint)
            current=h.current(revised.task_id)
            task,ledger,_=await fixture._approve_and_execute(root=root,result=current)
            self.assertEqual('completed',task.get('execution_status'),task.get('error'))
            self.assertEqual(before,file_identity(retained.target_path))
            self.assertTrue(all(row['target']!=str(retained.target_path) for row in ledger['entries']))
            # Fresh service, not the in-memory owner used at approval.
            scope=h.execution_service().review_service.asset_actions()
            scope._catalog_slot(current.plan)

    async def test_retired_tv_episode_is_not_resurrected_as_later_append_collision(self):
        from tests.test_completed_rebuild_work_contract import CompletedMovieRebuildContracts
        from src.rename.series_asset_scope import validate_peer_append
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            harness=tv_fixture.MediaFlow('runTest')
            old, _archive = await harness._materialize_s1(root)
            source,archive=tv_fixture.make_season(root/'partial')
            archive.unlink()
            for path in list(source.iterdir()):
                if '[02]' in path.name: path.unlink()
            for path in source.glob('*.mkv'):
                with path.open('ab') as stream: stream.write(b'new')
            result=await harness._run_flow(root=root,source=source,task_id='partial',subject_id='41658')
            h=CompletedMovieRebuildContracts('runTest'); h.root=root
            review=h.execution_service().review_service
            operation,_=review.asset_actions().prepare_replacement(result.plan)
            revised=review.confirm_asset_replacement(result.plan.task_id,PlanStore.fingerprint(result.plan),
                expected_operation_fingerprint=operation.fingerprint)
            task,_,_=await harness._approve_and_execute(root=root,result=h.current(revised.task_id))
            self.assertEqual('completed',task.get('execution_status'),task.get('error'))
            another,_=tv_fixture.make_season(root/'append')
            for path in list(another.iterdir()):
                if '[01]' in path.name: path.unlink()
            result=await harness._run_flow(root=root,source=another,task_id='duplicate',subject_id='41658')
            old_e2=next(e.target_path for e in old.plan.entries if e.media_kind=='video' and e.episode_id==2)
            self.assertFalse(old_e2.exists())
            validate_peer_append(result.plan,review.series_catalog,{'task_id':revised.task_id})
            # A new E02 is a real append, not resurrection of retired history.
            e2=next(e for e in result.plan.entries if e.media_kind=='video' and e.episode_id==2)
            from dataclasses import replace
            with patch('src.rename.series_asset_scope.validate_plan_projection_receipt'):
                counterexample=replace(result.plan,entries=(replace(e2,target_path=e2.target_path.with_name('different-E02.mkv')),))
                validate_peer_append(counterexample,review.series_catalog,{'task_id':revised.task_id})

    async def movie(self, root, *, task='movie', multi=False, fonts=True, orphan=False, file_only=False, ambiguous=False):
        # Reuse a genuine tiny Matroska container. Nothing touches user media.
        sample, _ = tv_fixture.make_season(root/'sample')
        source=root/'sources'/'[Group] Example Movie [1080p+2160p]'
        source.mkdir(parents=True)
        first=source/'Example Movie [1080p].mkv'
        shutil.copyfile(next(sample.glob('*.mkv')),first)
        primary=[first]
        if ambiguous:
            other=first.with_suffix('.mp4')
            shutil.copyfile(first,other)
            first.with_suffix('.mka').write_bytes(b'ambiguous companion')
            primary.append(other)
        if orphan:
            (source/'Unmatched.mka').write_bytes(b'audio')
        if multi:
            second=source/'Example Movie [2160p].mkv'
            shutil.copyfile(first,second)
            primary.append(second)
        if fonts:
            with zipfile.ZipFile(source/'Example [Fonts].zip','w') as archive:
                archive.writestr('nested/Face.ttf',b'excluded archive member')
        with zipfile.ZipFile(source/'Example [Subtitles].zip','w') as archive:
            archive.writestr('Example.sc.ass','[Script Info]')
        fixture=movie_fixture.MediaFlow(methodName='runTest')
        result=await fixture._run_provider_flow(root,first if file_only else source,task)
        return fixture,result,tuple(primary)

    async def test_single_video_completion_excludes_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            fixture,result,primary=await self.movie(root)
            self.assertEqual('inventory_review_draft',result.plan.metadata.get('mapping_source'))
            completed=await fixture._complete_movie(root=root,result=result,primary=primary)
            plan=completed.plan
            self.assertIsNotNone(plan.metadata.get('asset_operation'))
            self.assertNotIn('font_contribution',plan.metadata)
            self.assertFalse(any(e.source_path.name.endswith('[Fonts].zip') for e in plan.entries))
            self.assertTrue(any(p.name.endswith('[Subtitles].zip') for p in plan.ignored_sources))
            descriptor=next(e for e in plan.entries if e.source_path==primary[0]).presentation_asset.version_descriptor
            self.assertEqual('resolved',descriptor.resolution.status)
            self.assertEqual('1080p',descriptor.resolution.value)
            self.assertTrue(descriptor.version_ref)

    async def test_uncompleted_movie_without_asset_authority_cannot_be_approved(self):
        from src.rename.plan_review import PlanReviewService, PlanReviewError, entry_id
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            _,result,_=await self.movie(root,fonts=False)
            plan=result.plan
            self.assertIsNone(plan.metadata.get('asset_operation'))
            service=PlanReviewService(PlanStore(root/'plans'),root/'reviews',root/'work_catalog',
                decision_state_root=root/'decisions',task_root=root/'tasks',record_root=root/'records')
            with self.assertRaises(PlanReviewError):
                service.approve_entries(plan.task_id,PlanStore.fingerprint(plan),
                    [entry_id(e) for e in plan.entries])
            review_path=root/'reviews'/f'{plan.task_id}.json'
            if review_path.exists():
                self.assertNotEqual('approved',json.loads(review_path.read_text('utf-8'))['status'])
            self.assertFalse(list((root/'records').glob('*.ledger.json')))
            self.assertFalse(list((root/'AnimeMovies').rglob('*.mkv')))


    async def test_season_number_in_audio_name_cannot_override_confirmed_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            sample,_=tv_fixture.make_season(root/'template')
            source=root/'sources'/'[LoliHouse] Yama no Susume Season 2 [1080p]'
            source.mkdir(parents=True)
            for number in range(1,14):
                name=f'[LoliHouse] Yama no Susume Season 2 - {number:02d} [BDRip 1080p HEVC-10bit FLACx2].mkv'
                shutil.copyfile(next(sample.glob('*.mkv')),source/name)
                (source/name).with_suffix('.mka').write_bytes(f'audio-{number}'.encode())
            new=source/'[LoliHouse] Yama no Susume Season 2 - 01 [BDRip 1080p HEVC-10bit FLACx2].mkv'
            audio=new.with_suffix('.mka'); audio.write_bytes(b'external-audio')
            fixture=tv_fixture.MediaFlow(methodName='runTest')
            result=await fixture._run_flow(root=root,source=source,task_id='audio',subject_id='86670')
            video_entry=next(e for e in result.plan.entries if e.source_path==new)
            audio_entry=next(e for e in result.plan.entries if e.source_path==audio)
            self.assertNotIn('ManualReview',video_entry.target_path.parts)
            self.assertEqual(video_entry.target_path.with_suffix('.mka'),audio_entry.target_path)
            for video in [e for e in result.plan.entries if e.media_kind=='video']:
                pair=next(e for e in result.plan.entries if e.source_path==video.source_path.with_suffix('.mka'))
                self.assertEqual(video.target_path.with_suffix('.mka'),pair.target_path)

    async def test_completed_main_does_not_reserve_disjoint_official_special(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            main=root/'sources'/'[Group] Steins;Gate [1080p]'
            for number in range(1,25):
                special_fixture._write(main/f'[Group] Steins;Gate [{number:02d}][1080p].mkv')
            child=special_fixture._write(root/'sources'/'Special'/'[Group] Steins;Gate [23β][1080p].mkv')
            fixture=special_fixture.ComplexLibrarySpecialRedContracts(methodName='runTest')
            with patch('src.rename.process.SERIES_CATALOG_PATH',root/'series_catalog'), \
                 patch('src.rename.process.RECORD_PATH',root/'records'), \
                 patch('src.rename.process.is_dry_run_enabled',return_value=False):
                first=await fixture._run(root,main,'main','10380')
                second=await fixture._run(root,child,'special','10380')
            second.plan_store=PlanStore(root/'plans')
            second.decision_store=DecisionStateStore(root/'decisions')
            dialog,ui,_=fixture._render_issue_actions(second,selected_source=child)
            self.addCleanup(fixture.doCleanups)
            self.assertTrue(special_fixture._select_special_episode(ui,6))
            with patch.object(special_fixture.review_page,'notify'), \
                 patch.object(special_fixture.review_page,'TASK_PATH',root/'tasks'), \
                 patch('src.utils.utils.TASK_PATH',root/'tasks'):
                outcome=ui.callbacks['归入特别篇']()
                if inspect.isawaitable(outcome): await outcome
            second.plan=second.plan_store.load('special')
            special_entry=next(e for e in second.plan.entries if e.source_path==child)
            self.assertEqual((0,6),(special_entry.season_id,special_entry.episode_id))
            second.task=json.loads((root/'tasks'/'special.json').read_text('utf-8'))
            # Shared production approval/queue/execution path, not fabricated Catalog.
            execute=tv_fixture.MediaFlow(methodName='runTest')
            from src.rename.plan_verifier import PlanVerifier
            self.assertEqual(set(),{i.code for i in PlanVerifier().verify(first.plan).issues})
            first_task,_,_=await execute._approve_and_execute(root=root,result=first)
            self.assertEqual('completed',first_task.get('execution_status'),first_task.get('error'))
            from src.rename.plan_review import entry_id
            dialog.review_service.approve_entries('special',PlanStore.fingerprint(second.plan),
                [entry_id(e) for e in second.plan.entries])
            second_task,_,_=await execute._approve_and_execute(root=root,result=second)
            self.assertEqual('completed',second_task.get('execution_status'),second_task.get('error'))
