"""New-policy TV first selection, real public services and disposable media."""
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from src.rename.plan_verifier import PlanVerifier
from tests.test_content_choice_contract import change
from tests.test_content_choice_contract import records
from src.rename.projection_authority import ProjectionCommitService
from src.utils.utils import merge_task_at
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tests import test_r1_real_media_production_red_contract as flow_fixture
from src.rename.plan_review import PlanReviewService, PlanReviewError
from src.rename.plan_review import entry_id
from src.rename.decision_state import DecisionCommandService
from src.rename.plan_store import PlanStore
from src.rename.selection_gate import validate_selection_for_plan
from src.task_queue import TaskQueueManager
import src.pages.plan_review_page as review_page
import src.rename.process as process


class _ContractStop(ValueError):
    """An intentional worker failure after a negative assertion completed."""


class InitialTVCompletionContracts(unittest.IsolatedAsyncioTestCase):
    async def test_unresolved_v2_public_bangumi_tv_selection_can_complete(self):
        await self.first_tv(anime=True)

    async def test_publication_scope_drift_preserves_decision_plan_and_approval(self):
        from src.utils.utils import read_task_at, write_task_at
        from src.rename import public_revision
        visited = []

        def drift(root, transaction, envelope, terminal, commit):
            task_id = envelope.decision.task_id
            task = read_task_at(root / 'tasks', task_id)
            task['task_source_scope']['reservation_revision'] += 1
            write_task_at(root / 'tasks', task_id, task)
            before = records(root)
            validate = public_revision.validate_publication
            def observed(*args, **kwargs):
                visited.append(True)
                return validate(*args, **kwargs)
            from src.rename.task_batch_scope import TaskBatchScopeError
            with patch.object(public_revision, 'validate_publication', observed):
                with self.assertRaisesRegex(ValueError, '公共修订的来源范围已变化'):
                    public_revision.validate_publication(transaction, envelope)
                with self.assertRaisesRegex(TaskBatchScopeError, 'active reservation lineage'):
                    commit()
            self.assertEqual(before, records(root))
            raise _ContractStop('来源范围变化已拒绝，结束隔离任务')

        await self.fixture(anime=True, commit_hook=drift)
        self.assertEqual([True], visited, '必须到达真实事务内的公共范围验证')

    async def test_initial_publication_cannot_skip_source_identity_guard(self):
        from src.rename import content_selection
        for error_type in (ValueError, TypeError):
            with self.subTest(error_type=error_type.__name__):
                visited = []

                def refuse(root, transaction, envelope, terminal, commit):
                    before = records(root)
                    def reject(choice, *, plan, **kwargs):
                        visited.append((choice, plan))
                        raise error_type('冻结来源身份拒绝提交')
                    with patch.object(content_selection, 'validate_source_identities', reject):
                        with self.assertRaisesRegex(error_type, '冻结来源身份拒绝提交'):
                            commit()
                    self.assertEqual(before, records(root))
                    self.assertEqual(1, len(visited))
                    self.assertEqual(envelope.decision.content_selection, visited[0][0])
                    self.assertEqual(transaction._public_revision.current, visited[0][1])
                    raise _ContractStop('来源身份校验拒绝已传播，结束隔离任务')

                await self.fixture(anime=True, commit_hook=refuse)
                self.assertEqual(1, len(visited))

    async def test_unresolved_v2_public_tmdb_tv_selection_can_complete(self):
        await self.first_tv(anime=False)

    async def fixture(self, *, anime, root=None, duplicate=False, exclude_duplicate=False,
                      companions=False, companion_language='chs', manual_subtitle=False, selected_season=None,
                      guard_legacy=False, commit_hook=None, seed_catalog=None, source_setup=None, exceptional=False, font_archive=False, excluded_misc=False, selection_hook=None, exceptional_name=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        temp = temporary.name
        root = root or Path(temp) / 'initial-tv'
        title = 'Yama no Susume' if anime else 'Ordinary Show'
        source = flow_fixture._make_season(root / 'sources', title, title)
        payload = next(source.glob('*.mkv'))
        service = PlanReviewService(PlanStore(root / 'plans'), root / 'reviews', root / 'work_catalog',
            decision_state_root=root / 'decisions', task_root=root / 'tasks', record_root=root / 'records',
            provider_link_catalog_root=root / 'provider_links')
        before = {}
        extras = {}
        if font_archive:
            extras['font'] = source / 'Fonts.7z'
            extras['font'].write_bytes(b'excluded archive')
        if excluded_misc:
            extras['misc'] = source / 'README.txt'
            extras['misc'].write_text('excluded fixture documentation', encoding='utf-8')
        primary_source = next(source.glob('*.mkv'))
        if exceptional:
            primary_source = source / (exceptional_name or 'Yama no Susume S01E99.mkv')
            shutil.copyfile(payload, primary_source)
            extras['exceptional'] = primary_source
        if seed_catalog is not None:
            seed_catalog(root)
        hook_errors = []
        original_commit = ProjectionCommitService._commit_locked
        def commit(transaction, envelope, *, task_terminal=None):
            if envelope.draft_plan.revision_action == 'initial_tv_completion' and commit_hook is not None:
                try:
                    return commit_hook(root, transaction, envelope, task_terminal,
                        lambda candidate=envelope: original_commit(transaction, candidate, task_terminal=task_terminal))
                except _ContractStop:
                    raise
                except BaseException as error:
                    hook_errors.append(error)
                    raise
            return original_commit(transaction, envelope, task_terminal=task_terminal)
        if duplicate:
            extra = source / 'Duplicate S01E01.mkv'
            shutil.copyfile(next(source.glob('*.mkv')), extra)
            extras['duplicate'] = extra
        if companions:
            owner = primary_source
            audio, subtitle = owner.with_suffix('.mka'), owner.with_suffix(f'.{companion_language}.srt')
            audio.write_bytes(b'isolated external audio')
            subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\n测试字幕\n', encoding='utf-8')
            extras.update(owner=owner, audio=audio, subtitle=subtitle)
        if source_setup is not None:
            source_setup(source, extras)
        original = review_page.PlanReviewDialog._select_provider_candidate
        async def select(dialog, candidate, **kwargs):
            self.assertFalse(dialog.task_decision.confirmed_work)
            preview = service.prepare_default_content_initialization(dialog.plan.task_id, PlanStore.fingerprint(dialog.plan))
            initialized = service.commit_default_content_initialization(preview)
            if exclude_duplicate:
                initialized = change(service, initialized, [extras['duplicate']])
            if manual_subtitle:
                from tests.test_content_subtitle_lifecycle import apply
                initialized = apply(service, initialized, extras['subtitle'], owner=extras['owner'])
            if selected_season is not None:
                merge_task_at(root / 'tasks', initialized.task_id,
                    {'season_id': selected_season, 'season_number': selected_season, 'season_source': 'explicit_user'})
            state = service.decision_state_store.load(initialized.task_id)
            before.update(plan=initialized, state=state)
            dialog.plan, dialog.task_decision = initialized, state
            dialog.task_data = json.loads((root / 'tasks' / f'{initialized.task_id}.json').read_text('utf-8'))
            value = await original(dialog, candidate, **kwargs)
            if selection_hook is not None:
                await selection_hook(root, service, dialog, candidate, original)
            return value
        wait = TaskQueueManager.wait_for_selection_result
        async def bounded_wait(manager, task_id, **kwargs):
            return await wait(manager, task_id, timeout=0.3)
        worker_factory = flow_fixture._configured_renamer
        old_traditional = process.Rename._process_traditional
        legacy_calls = []
        def worker(root_path):
            renamer = worker_factory(root_path)
            from src.rename.initial_movie_completion import RootSnapshotSource
            from src.rename.target_routing import TargetRootSnapshot
            service.root_snapshot_source = RootSnapshotSource(lambda: TargetRootSnapshot.capture({
                'bangumi_path': renamer.BANGUMI_PATH, 'movie_path': renamer.MOVIE_PATH,
                'anime_path': renamer.ANIME_PATH, 'anime_movie_path': renamer.ANIME_MOVIE_PATH}))
            return renamer
        def traditional(renamer, *args, **kwargs):
            if guard_legacy and renamer.active_provider_selection:
                legacy_calls.append(True)
                raise ValueError('new TV must not reach legacy mapping')
            return old_traditional(renamer, *args, **kwargs)
        harness = flow_fixture.R1RealQueueWorkerContracts('runTest')
        search_tv = flow_fixture._FrozenSearch.get_tv_info
        lookup_tv = flow_fixture._FrozenSearch.get_tv_info_by_id
        season_lookup = flow_fixture._FrozenSearch.get_season_info
        def season_info(search, provider_id, season_number):
            if exceptional and int(season_number) == 0:
                return {'season_number': 0, 'episodes': [
                    {'id': 9001, 'episode_number': 1, 'season_number': 0, 'name': 'Isolated special',
                     'air_date': '2020-01-01', 'runtime': 24}]}
            return season_lookup(search, provider_id, season_number)
        def initial_search(search, query, year=None):
            return search_tv(search, query, year) if anime else ('', None)
        def exact_lookup(search, provider_id):
            title, info = search_tv(search, 'Ordinary Show') if not anime and int(provider_id) == 4242 else lookup_tv(search, provider_id)
            if not anime and selected_season is not None:
                info = {**info, 'seasons': [{'season_number': n, 'name': f'Season {n}'} for n in (0, 1, 2)]}
            return title, info
        candidate = flow_fixture._candidate('41658') if anime else dict(namespace='tmdb',
            entity_type='tv', provider_id='4242', title='普通剧集', original_title='Ordinary Show',
            year=2020, source_url='https://www.themoviedb.org/tv/4242', snapshot_at='2026-09-13T00:00:00Z')
        with patch.object(flow_fixture, '_configured_renamer', side_effect=worker), \
                patch.object(ProjectionCommitService, '_commit_locked', commit), \
                patch.object(process.Rename, '_process_traditional', traditional), \
                patch.object(review_page.PlanReviewDialog, '_select_provider_candidate', select), \
                patch.object(flow_fixture._FrozenSearch, 'get_tv_info', initial_search), \
                patch.object(flow_fixture._FrozenSearch, 'get_tv_info_by_id', exact_lookup), \
                patch.object(flow_fixture._FrozenSearch, 'get_season_info', season_info), \
                patch.object(TaskQueueManager, 'wait_for_selection_result', bounded_wait), \
                patch.object(process, 'SERIES_CATALOG_PATH', root / 'series_catalog'), \
                patch.object(process, 'RECORD_PATH', root / 'records'):
            result = await harness._run_flow(root=root, source=source, task_id='first-tv-content',
                candidate=candidate, is_anime=anime, is_movie=False)
        if hook_errors:
            raise hook_errors[0]
        return root, service, before, result, extras, legacy_calls, [], []

    async def test_special_content_revision_preserves_bundle_and_rejects_stale_choice(self):
        for action in ('confirmed_official_special', 'unmatched_extra'):
            with self.subTest(action=action):
                root, service, _, result, extras, *_ = await self.fixture(anime=True, exceptional=True, companions=True, font_archive=True)
                self.assert_success(result)
                plan = result.plan
                identifier = entry_id(next(e for e in plan.entries if e.source_path == extras['exceptional']))
                from src.rename.provider_special_evidence import official_special_rows
                rows = official_special_rows(plan, identifier)
                self.assertEqual(1, len(rows), 'actual frozen special candidate must be reached')
                edit = {'entry_id': identifier, 'action': action,
                        'provider_episode_id': '9001' if action == 'confirmed_official_special' else None}
                original = records(root)
                with patch.object(service, 'apply_provider_special_decision', side_effect=AssertionError('legacy writer')):
                    revised = service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                        expected_revision=result.state.revision, mode='special', edit=edit)
                self.assertNotEqual(original, records(root))
                state = service.decision_state_store.load(plan.task_id)
                self.assertEqual(result.state.initial_source_basis, state.initial_source_basis)
                self.assertEqual(result.state.content_selection.associations, state.content_selection.associations)
                self.assertEqual('confirmed_special_correspondence' if action == 'confirmed_official_special' else action,
                                 state.provider_special_decisions[identifier]['action'])
                old_font = [row for row in result.state.content_selection.to_dict()['materials']
                    if row['source_path'] == str(extras['font'])]
                self.assertEqual(1, len(old_font))
                new_font = [row for row in state.content_selection.to_dict()['materials']
                    if row['source_path'] == str(extras['font'])]
                # Reprojection can repeat observation text, not change identity.
                self.assertEqual([{**r, 'evidence': sorted(set(r['evidence']))} for r in old_font],
                                 [{**r, 'evidence': sorted(set(r['evidence']))} for r in new_font])
                video = next(e for e in revised.entries if e.source_path == extras['exceptional'])
                self.assertEqual('content_version' if action == 'confirmed_official_special' else 'extra',
                                 video.presentation_asset.asset_role.kind)
                for key in ('audio', 'subtitle'):
                    child = next(e for e in revised.entries if e.source_path == extras[key])
                    self.assertEqual(video.target_path.parent, child.target_path.parent)
                    self.assertTrue(child.target_path.name.startswith(video.target_path.stem))
                self.assertFalse(service.load_or_create(revised).approved_entry_ids)
                from src.rename.content_policy import require_media_ready
                require_media_ready(revised)
                self.assertNotIn('subtitle_video_not_content_version',
                    {issue.code for issue in PlanVerifier().verify(revised).issues})
                from src.rename.presentation import associate_resolved_extra_subtitles
                self.assertEqual(associate_resolved_extra_subtitles(revised),
                    associate_resolved_extra_subtitles(associate_resolved_extra_subtitles(revised)))
                saved = records(root)
                with self.assertRaises(PlanReviewError):
                    service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                        expected_revision=result.state.revision, mode='special', edit=edit)
                self.assertEqual(saved, records(root))

    async def test_role_revision_with_font_archive_preserves_materials_without_special_decisions(self):
        root, service, _, result, extras, *_ = await self.fixture(anime=True, duplicate=True, font_archive=True, excluded_misc=True)
        self.assert_success(result)
        self.assertFalse(result.state.provider_special_decisions)
        identifier = entry_id(next(e for e in result.plan.entries if e.source_path == extras['duplicate']))
        revised = service.revise_tv_content(result.plan.task_id, PlanStore.fingerprint(result.plan),
            expected_revision=result.state.revision, mode='role', edit={'entry_id': identifier, 'role': 'extra'})
        state = service.decision_state_store.load(revised.task_id)
        self.assertEqual(result.state.initial_source_basis, state.initial_source_basis)
        self.assertFalse(state.provider_special_decisions)
        self.assertEqual({row['source_path'] for row in result.state.content_selection.to_dict()['materials']},
            {row['source_path'] for row in state.content_selection.to_dict()['materials']})
        self.assertEqual(set(), {i.code for i in PlanVerifier().verify(revised).issues})

    async def test_tv_material_refresh_rejects_missing_retained_video(self):
        from src.rename.initial_tv_completion import _projected_materials
        root, service, _, result, extras, *_ = await self.fixture(anime=True, font_archive=True, excluded_misc=True)
        self.assert_success(result)
        result.state = service.decision_state_store.load(result.plan.task_id)
        original = records(root)
        choice = result.state.content_selection
        refreshed = _projected_materials(result.plan, choice, result.plan)
        self.assertEqual({row['source_path'] for row in choice.to_dict()['materials']},
                         {row['source_path'] for row in refreshed})
        victim = next(e for e in result.plan.entries if e.media_kind == 'video')
        self.assertIn(extras['misc'], result.plan.ignored_sources)
        with self.assertRaisesRegex(ValueError, '保留范围'):
            _projected_materials(replace(result.plan,
                ignored_sources=(*result.plan.ignored_sources, victim.source_path)), choice, result.plan)
        with self.assertRaisesRegex(ValueError, '来源材料'):
            _projected_materials(result.plan, choice,
                replace(result.plan, entries=tuple(e for e in result.plan.entries if e != victim)))
        self.assertEqual(original, records(root))

    async def test_special_commit_rejects_disconnected_validator_and_changed_choice(self):
        for fault in ('disconnect', 'choice'):
            with self.subTest(fault=fault):
                root, service, _, result, extras, *_ = await self.fixture(anime=True, exceptional=True, companions=True)
                self.assert_success(result)
                plan = result.plan
                identifier = entry_id(next(e for e in plan.entries if e.source_path == extras['exceptional']))
                original = records(root)
                with self.assertRaises(PlanReviewError):
                    service.apply_provider_special_decision(plan.task_id, PlanStore.fingerprint(plan), identifier,
                        decision={'action': 'unmatched_extra'}, reason='old entry must reject', persist_revision=False)
                self.assertEqual(original, records(root))
                reached = []
                expected = [original]
                commit = ProjectionCommitService._commit_locked
                def interfere(transaction, envelope, *, task_terminal=None):
                    reached.append(envelope.draft_plan.revision_action)
                    if fault == 'choice':
                        intent = envelope.initial_tv_completion
                        edit = json.loads(intent.inputs.edit_json)
                        edit['provider_episode_id'] = '9002'
                        envelope = replace(envelope, initial_tv_completion=replace(intent,
                            inputs=replace(intent.inputs, edit_json=json.dumps(edit))))
                    if fault == 'disconnect':
                        with patch('src.rename.initial_tv_completion.validate_commit', return_value=None):
                            return commit(transaction, envelope, task_terminal=task_terminal)
                    return commit(transaction, envelope, task_terminal=task_terminal)
                with patch.object(ProjectionCommitService, '_commit_locked', interfere), self.assertRaises((ValueError, TypeError, PlanReviewError)):
                    service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                        expected_revision=result.state.revision, mode='special',
                        edit={'entry_id': identifier, 'action': 'confirmed_official_special', 'provider_episode_id': '9001'})
                self.assertEqual(['tv_content_special'], reached, 'actual canonical must be reached')
                self.assertEqual(expected[0], records(root))

    async def test_special_unapproved_revision_does_not_compare_companion_bytes(self):
        root, service, _, result, extras, *_ = await self.fixture(anime=True, exceptional=True, companions=True)
        self.assert_success(result)
        plan = result.plan
        identifier = entry_id(next(e for e in plan.entries if e.source_path == extras['exceptional']))
        commit = ProjectionCommitService._commit_locked
        reached = []
        def update_companion(transaction, envelope, *, task_terminal=None):
            reached.append(envelope.draft_plan.revision_action)
            extras['audio'].write_bytes(b'changed isolated audio after prepare')
            return commit(transaction, envelope, task_terminal=task_terminal)
        with patch.object(ProjectionCommitService, '_commit_locked', update_companion):
            revised = service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                expected_revision=result.state.revision, mode='special',
                edit={'entry_id': identifier, 'action': 'confirmed_official_special', 'provider_episode_id': '9001'})
        self.assertEqual(['tv_content_special'], reached)
        self.assertIn(extras['audio'], {e.source_path for e in revised.entries})
        self.assertFalse(service.load_or_create(revised).approved_entry_ids)
        self.assertFalse((service.record_root / f'{plan.task_id}.ledger.json').exists())

    async def first_tv(self, *, anime):
        _, service, before, result, *_ = await self.fixture(anime=anime)
        self.assertTrue(before, 'real public selection hook not reached')
        from src.rename.review_operation import current_provider_selection_review_operation
        _, context = current_provider_selection_review_operation(result.task)
        self.assertEqual('succeeded', context.get('lifecycle'), json.dumps({
            'task_error': result.task.get('error'), 'reasons': context.get('reason_codes'),
            'failure': context.get('failure_code')}, ensure_ascii=False))
        validate_selection_for_plan(result.task)
        self.assertEqual(before['state'].initial_source_basis, result.state.initial_source_basis)
        self.assertTrue(result.state.confirmed_work)
        self.assertEqual('tv_episodes', result.state.layout_mode)
        self.assertIsNotNone(result.state.content_selection)
        self.assertIsNotNone(result.state.series_binding)
        if anime:
            self.assertIsNotNone(result.state.tv_planning_authority)
        else:
            self.assertIsNone(result.state.tv_planning_authority, 'ordinary TV must not fabricate Bangumi authority')

        choice = result.state.content_selection.to_dict()
        self.assertEqual({str(p) for p in result.plan.expected_sources},
                         {row['source_path'] for row in choice['materials']})
        videos = [entry for entry in result.plan.entries if entry.media_kind == 'video']
        self.assertEqual(2, len(videos))
        self.assertEqual({(1, 1), (1, 2)}, {(e.season_id, e.episode_id) for e in videos})
        for video in videos:
            self.assertIn(f'S01E{video.episode_id:02d}', video.target_path.name)
            self.assertEqual('content_version', video.presentation_asset.asset_role.kind)
            self.assertIsNotNone(video.presentation_asset.version_descriptor)
        self.assertEqual('initial_tv_completion', result.plan.revision_action)
        self.assertEqual(set(), {i.code for i in PlanVerifier().verify(result.plan).issues})
        self.assertFalse(service.load_or_create(result.plan).approved_entry_ids)
        return result

    async def test_first_tv_bypasses_legacy_mapping_with_duplicate_and_preserves_full_materials(self):
        _, _, before, result, extras, legacy_calls, *_ = await self.fixture(anime=False, duplicate=True, guard_legacy=True)
        self.assert_success(result)
        self.assertFalse(legacy_calls)
        choice = result.state.content_selection.to_dict()
        self.assertEqual({row['source_path'] for row in before['state'].content_selection.to_dict()['materials']},
                         {row['source_path'] for row in choice['materials']})
        self.assertIn('episode_assignment_conflict', {i.code for i in PlanVerifier().verify(result.plan).issues})
        self.assertIn(extras['duplicate'], {e.source_path for e in result.plan.entries})

    async def test_first_tv_keeps_explicit_exclusion_and_subtitle_audio_owners(self):
        _, _, before, result, extras, *_ = await self.fixture(anime=False, duplicate=True,
                                                              exclude_duplicate=True, companions=True)
        self.assert_success(result)
        before_choice = before['state'].content_selection.to_dict()
        after_choice = result.state.content_selection.to_dict()
        self.assertEqual(before_choice['excluded_sources'], after_choice['excluded_sources'])
        self.assertIn(extras['duplicate'], result.plan.ignored_sources)
        self.assertNotIn(extras['duplicate'], {e.source_path for e in result.plan.entries})
        self.assertNotIn('episode_assignment_conflict', {i.code for i in PlanVerifier().verify(result.plan).issues})
        owner = next(e for e in result.plan.entries if e.source_path == extras['owner'])
        audio = next(e for e in result.plan.entries if e.source_path == extras['audio'])
        subtitle = next(e for e in result.plan.entries if e.source_path == extras['subtitle'])
        self.assertEqual(owner.target_path.with_suffix('.mka'), audio.target_path)
        self.assertEqual(owner.presentation_asset.version_descriptor.version_ref, audio.presentation_asset.owner_version_ref)
        self.assertEqual(owner.source_path, subtitle.subtitle.associated_video_source_path)
        self.assertEqual((owner.season_id, owner.episode_id), (audio.season_id, audio.episode_id))
        self.assertEqual((owner.season_id, owner.episode_id), (subtitle.season_id, subtitle.episode_id))
        self.assertTrue(subtitle.target_path.name.startswith(owner.target_path.stem + '.'))


    async def test_duplicate_has_working_gui_exclusion_without_legacy_completion(self):
        root, service, _, result, extras, *_ = await self.fixture(anime=False, duplicate=True)
        self.assert_success(result)
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        gui = ContentWorkbenchGui('runTest')
        gui.create_page(prepared=(root, service, result.plan, (extras['duplicate'],)), cleanup_owner=self)
        with patch.object(service, 'recomplete_inventory_with_role_decisions',
                          side_effect=AssertionError('new TV exclusion called old inventory writer')):
            await gui.driver.click('内容保留与排除')
            with gui.client:
                gui.source_box(extras['duplicate']).set_value(True)
            await __import__('asyncio').sleep(0)
            await gui.driver.click('排除整理')
        plan = service.plan_store.load(result.plan.task_id)
        self.assertNotEqual(PlanStore.fingerprint(result.plan), PlanStore.fingerprint(plan),
            gui.view._content_workbench.message.text)
        revised = service.decision_state_store.load(result.plan.task_id)
        self.assertEqual(result.state.revision + 1, revised.revision)
        self.assertIn(extras['duplicate'], plan.ignored_sources)
        self.assertEqual(result.state.initial_source_basis, revised.initial_source_basis)
        videos = [e for e in plan.entries if e.media_kind == 'video']
        self.assertEqual(2, len(videos))
        self.assertEqual({(1, 1), (1, 2)}, {(e.season_id, e.episode_id) for e in videos})
        for video in videos:
            self.assertIn(f'S01E{video.episode_id:02d}', video.target_path.name)
            self.assertEqual('content_version', video.presentation_asset.asset_role.kind)
        subtitles = [e for e in plan.entries if e.media_kind == 'subtitle']
        self.assertEqual(2, len(subtitles))
        for subtitle in subtitles:
            owner = next(e for e in videos if e.source_path == subtitle.subtitle.associated_video_source_path)
            self.assertEqual(owner.target_path.parent, subtitle.target_path.parent)
            self.assertTrue(subtitle.target_path.name.startswith(owner.target_path.stem))
        issues = {i.code for i in PlanVerifier().verify(plan).issues}
        self.assertNotIn('episode_assignment_conflict', issues)
        self.assertEqual(set(), issues)
        self.assertFalse(service.load_or_create(plan).approved_entry_ids)
        self.assertFalse(any(e.target_path.exists() for e in plan.entries))

    async def test_unrelated_content_exclusion_cannot_clear_duplicate_episode_conflict(self):
        from tests.test_content_choice_contract import change
        from src.rename.plan_review import PlanReviewError
        root, service, _, result, extras, *_ = await self.fixture(anime=False, duplicate=True)
        source_bytes = {p: p.read_bytes() for p in result.plan.expected_sources}
        subtitle = next(e.source_path for e in result.plan.entries if e.media_kind == 'subtitle')
        revised = change(service, result.plan, [subtitle])
        self.assertIn(subtitle, revised.ignored_sources)
        self.assertIn(extras['duplicate'], {e.source_path for e in revised.entries})
        self.assertEqual(3, sum(e.media_kind == 'video' for e in revised.entries))
        self.assertIn('episode_assignment_conflict', {i.code for i in PlanVerifier().verify(revised).issues})
        self.assertFalse(service.load_or_create(revised).approved_entry_ids)
        with self.assertRaisesRegex(PlanReviewError, 'episode_assignment_conflict'):
            service.approve_entries(revised.task_id, PlanStore.fingerprint(revised),
                [entry_id(e) for e in revised.entries])
        self.assertEqual(source_bytes, {p: p.read_bytes() for p in source_bytes})
        self.assertFalse(any(e.target_path.exists() for e in revised.entries))

    async def test_tv_content_change_rejects_raw_writer_tamper_and_disconnected_mapper(self):
        from src.rename.content_choice import _draft, ContentChangePreview
        from src.rename.content_associations import encode
        from src.rename.content_selection import ContentSelection
        from src.rename.decision_state import DecisionCommandService
        from src.rename import tv_video_projection
        root, service, _, result, extras, *_ = await self.fixture(anime=False, duplicate=True)
        plan, state = result.plan, result.state
        preview = service.prepare_content_change(plan.task_id, PlanStore.fingerprint(plan),
            selected_entry_ids=[entry_id(next(e for e in plan.entries if e.source_path == extras['duplicate']))],
            action='exclude', reason='人工排除重复视频')
        choice = ContentSelection.from_mapping(preview.to_dict()['selection'])
        draft = _draft(service, plan, state, choice, None, 'exclude')
        before = records(root)
        with self.assertRaisesRegex(ValueError, '完整TV重建事务'):
            DecisionCommandService(service.decision_state_store, service.plan_store).update(
                plan.task_id, expected_revision=state.revision, changes={'content_selection': choice},
                planner=lambda _: draft, task_terminal=service._revision_task_terminal(plan.task_id))
        self.assertEqual(before, records(root))
        forged = preview.to_dict()
        forged['kind'] = 'restore'  # The frozen selection still says exclude.
        with self.assertRaisesRegex(ValueError, '当前人工预览'):
            service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, mode='content', edit=ContentChangePreview(encode(forged)).to_dict())
        self.assertEqual(before, records(root))
        with patch.object(tv_video_projection, 'project_tv_videos', return_value=None):
            with self.assertRaisesRegex(ValueError, '唯一投影器已断开'):
                service.commit_content_change(preview)
        self.assertEqual(before, records(root))
        fixed = service.commit_content_change(preview)
        from tests.test_content_choice_contract import change
        restored = change(service, fixed, [extras['duplicate']], action='restore')
        self.assertIn('episode_assignment_conflict', {i.code for i in PlanVerifier().verify(restored).issues})
        self.assertEqual(state.revision + 2, service.decision_state_store.load(plan.task_id).revision)
        self.assertFalse(service.load_or_create(restored).approved_entry_ids)
        self.assertFalse(any(e.target_path.exists() for e in restored.entries))

    async def split_cour_candidate(self, *, source_factory=None, subjects=None):
        """One real add/select producer; every caller owns a fresh disposable tree."""
        from tests import test_provider_subject_plan_wiring_red_contract as segmented
        from src.pages.plan_review_page import build_plan_review_snapshot
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / 'split-cour'
        source = (source_factory or segmented._make_ambiguous_episode_versions)(root / 'sources')
        sample = flow_fixture._make_season(root / 'valid-container-control', 'Sample', 'Sample')
        for video in source.glob('*.mkv'):
            shutil.copyfile(next(sample.glob('*.mkv')), video)
        flow_root = root / 'flow'
        service = PlanReviewService(PlanStore(flow_root / 'plans'), flow_root / 'reviews', flow_root / 'work_catalog',
            decision_state_root=flow_root / 'decisions', task_root=flow_root / 'tasks',
            provider_link_catalog_root=flow_root / 'provider_links', record_root=flow_root / 'records')
        dialog_class = review_page.PlanReviewDialog
        original = dialog_class._select_provider_candidate
        async def select(dialog, candidate, **kwargs):
            preview = service.prepare_default_content_initialization(dialog.plan.task_id, PlanStore.fingerprint(dialog.plan))
            dialog.plan = service.commit_default_content_initialization(preview)
            dialog.task_decision = service.decision_state_store.load(dialog.plan.task_id)
            dialog.task_data = json.loads((flow_root / 'tasks' / f'{dialog.plan.task_id}.json').read_text('utf-8'))
            self.assertFalse(dialog.task_decision.confirmed_work)
            return await original(dialog, candidate, **kwargs)
        wait = TaskQueueManager.wait_for_selection_result
        async def bounded_wait(manager, task_id, **kwargs):
            return await wait(manager, task_id, timeout=0.3)
        with patch.object(dialog_class, '_select_provider_candidate', select), \
                patch.object(TaskQueueManager, 'wait_for_selection_result', bounded_wait):
            result = await segmented.ProviderSubjectPlanWiringRedContracts('runTest')._run_real_add_select(
                root=flow_root, source=source, subjects=subjects or segmented.EIGHTY_SIX_SUBJECTS,
                relations=segmented.EIGHTY_SIX_RELATIONS, selected_subject_id='331887')
        self.assert_success(result)
        from src.rename.initial_movie_completion import RootSnapshotSource
        from src.rename.target_routing import TargetRootSnapshot
        service.root_snapshot_source = RootSnapshotSource(lambda: TargetRootSnapshot.capture({
            'bangumi_path': result.renamer.BANGUMI_PATH, 'movie_path': result.renamer.MOVIE_PATH,
            'anime_path': result.renamer.ANIME_PATH, 'anime_movie_path': result.renamer.ANIME_MOVIE_PATH}))
        dialog = object.__new__(dialog_class)
        dialog._change_handlers = []
        dialog.plan, dialog.task_decision, dialog.task_data = result.plan, result.state, result.task
        dialog.snapshot = build_plan_review_snapshot(result.plan)
        dialog.issues_container = segmented.ui_fixture._UiNode()
        dialog.review_service = service
        dialog.decision_command_service = DecisionCommandService(service.decision_state_store, service.plan_store)
        dialog.selected_ids = set()
        dialog._adopt_revision = lambda plan: setattr(dialog, 'plan', plan)
        return SimpleNamespace(root=root, flow_root=flow_root, source=source, service=service,
            result=result, dialog=dialog, dialog_class=dialog_class, segmented=segmented)

    async def test_new_content_split_cour_gui_choice_preserves_source_owners_and_rejects_stale_action(self):
        fixture = await self.split_cour_candidate()
        result, service, dialog = fixture.result, fixture.service, fixture.dialog
        dialog_class, segmented, flow_root = fixture.dialog_class, fixture.segmented, fixture.flow_root
        videos = [e for e in result.plan.entries if e.media_kind == 'video']
        self.assertEqual(2, len(videos))
        self.assertEqual([12, 12], [e.episode_id for e in videos], json.dumps({
            'entries': [{'name': e.source_path.name, 'episode': e.episode_id,
                         'role': e.presentation_asset.asset_role.kind if e.presentation_asset else None} for e in videos],
            'conflicts': result.plan.to_dict()['metadata'].get('episode_assignment_conflict_sources'),
            'issues': [i.code for i in PlanVerifier().verify(result.plan).issues]}, ensure_ascii=False))
        ui = segmented.ui_fixture._CaptureUi()
        failures = []
        revise = service.revise_tv_content
        def capture_revision(*args, **kwargs):
            try:
                return revise(*args, **kwargs)
            except (ValueError, RuntimeError) as error:
                failures.append(str(error))
                raise
        with patch.object(review_page, 'ui', ui), patch.object(review_page, 'notify'), \
                patch.object(service, 'revise_tv_content', side_effect=capture_revision), \
                patch.object(review_page, 'TASK_PATH', flow_root / 'tasks'):
            self.assertFalse(hasattr(review_page, 'apply_source_episode_projection'))
            dialog_class._render_issues(dialog)
            first, stale = ui.callbacks.get('沿用文件中的集号'), ui.callbacks.get('从本段第1集重新编号')
            self.assertTrue(callable(first) and callable(stale))
            await first()
            current = service.plan_store.load(result.task_id)
            state = service.decision_state_store.load(result.task_id)
            self.assertGreater(state.revision, result.state.revision, '\n'.join(failures))
            self.assertEqual('absolute', current.metadata['source_episode_projection']['mode'])
            self.assertEqual(result.plan.metadata['provider_link_snapshot'], current.metadata['provider_link_snapshot'])
            self.assertEqual(result.state.initial_source_basis, state.initial_source_basis)
            for subtitle in (e for e in current.entries if e.media_kind == 'subtitle'):
                owner = next(e for e in current.entries if e.source_path == subtitle.subtitle.associated_video_source_path)
                self.assertEqual((owner.season_id, owner.episode_id), (subtitle.season_id, subtitle.episode_id))
                self.assertTrue(subtitle.target_path.name.startswith(owner.target_path.stem + '.'))
                self.assertIsNone(subtitle.presentation_asset)
            self.assertEqual(2, len([e for e in current.entries if e.media_kind == 'subtitle']))
            self.assertEqual(2, len({e.presentation_asset.version_descriptor.version_ref
                for e in current.entries if e.media_kind == 'video'}))
            self.assertEqual(2, len({e.target_path for e in current.entries if e.media_kind == 'video'}))
            self.assertEqual(set(), {i.code for i in PlanVerifier().verify(current).issues})
            saved = records(flow_root)
            import inspect
            stale_result = stale()
            if inspect.isawaitable(stale_result):
                await stale_result
            self.assertEqual(saved, records(flow_root))

    def assert_success(self, result):
        from src.rename.review_operation import current_provider_selection_review_operation
        _, context = current_provider_selection_review_operation(result.task)
        self.assertEqual('succeeded', context.get('lifecycle'), str(result.task.get('error')))

    async def test_complete_builder_uses_frozen_sources_and_provider_observations(self):
        from tests.test_inventory_reuse import source_access
        from src.rename.decision_state import DecisionStateStore
        from src.rename.initial_tv_completion import build_initial_tv_completion
        import threading,requests
        reached=[]
        def inspect(root,transaction,envelope,terminal,forward):
            reached.append(True)
            parent=transaction.plan_store.load(envelope.decision.task_id)
            state=transaction.state_store.load(envelope.decision.task_id)
            thread=threading.get_ident()
            def guarded(original):
                def invoke(*args,**kwargs):
                    if threading.get_ident()==thread: raise AssertionError('builder repeated metadata IO')
                    return original(*args,**kwargs)
                return invoke
            with source_access(parent.source_root) as reads, \
                    patch.object(PlanStore,'load',guarded(PlanStore.load)), \
                    patch.object(DecisionStateStore,'load',guarded(DecisionStateStore.load)), \
                    patch.object(requests.Session,'request',guarded(requests.Session.request)):
                built=build_initial_tv_completion(parent,state,envelope.initial_tv_completion)
            self.assertEqual([],reads)
            self.assertEqual(envelope.draft_plan,built.draft)
            self.assertEqual(envelope.decision,built.decision)
            return forward()
        *_,=await self.fixture(anime=True,commit_hook=inspect)
        self.assertEqual([True],reached)


    async def test_missing_or_wrong_dual_transport_and_changed_draft_cannot_commit(self):
        for fault in ('intent_missing', 'intent_wrong', 'context_missing', 'context_wrong', 'draft'):
            with self.subTest(fault=fault):
                reached = []
                def reject(root, transaction, envelope, terminal, forward):
                    reached.append(True)
                    before = records(root)
                    if fault.startswith('intent'):
                        altered = replace(envelope, initial_tv_completion=None if fault.endswith('missing') else object())
                    else:
                        altered = envelope
                        if fault.startswith('context'):
                            altered = replace(envelope, initial_tv_completion=replace(envelope.initial_tv_completion,
                                context=None if fault.endswith('missing') else object()))
                        else:
                            entry = envelope.draft_plan.entries[0]
                            draft = replace(envelope.draft_plan, entries=(replace(entry,
                                target_path=entry.target_path.with_name('unreviewed.mkv')), *envelope.draft_plan.entries[1:]))
                            altered = replace(envelope, draft_plan=draft)
                    with self.assertRaises((ValueError, RuntimeError, AttributeError)):
                        forward(altered)
                    self.assertEqual(before, records(root))
                    raise _ContractStop('expected isolated TV contract rejection')
                _, _, _, result, *_ = await self.fixture(anime=False, commit_hook=reject)
                self.assertEqual([True], reached, 'negative did not reach canonical TV boundary')
                self.assertFalse(result.state.confirmed_work)

    async def test_original_request_drift_rejects_and_source_changes_remain_unobserved(self):
        for fault in ('request', 'source'):
            with self.subTest(fault=fault):
                reached = []
                def reject(root, transaction, envelope, terminal, forward):
                    reached.append(True)
                    plan = transaction.plan_store.load(envelope.decision.task_id)
                    state = transaction.state_store.load(plan.task_id)
                    if fault == 'source':
                        source = next(e.source_path for e in envelope.draft_plan.entries if e.media_kind == 'video')
                        source.write_bytes(source.read_bytes() + b' disposable source drift')
                        result = forward()
                        self.assertEqual(state.initial_source_basis,
                            transaction.state_store.load(plan.task_id).initial_source_basis)
                        self.assertEqual(state.initial_source_basis.to_dict(),
                            transaction.plan_store.load(plan.task_id).initial_source_basis.to_dict())
                        return result
                    else:
                        from tests.test_default_movie_completion import InitialMovieCompletionContracts
                        InitialMovieCompletionContracts('runTest').replace_request(
                            root / 'tasks' / f'{plan.task_id}.json', request_id='later-tv-request')
                    task_path = root / 'tasks' / f'{plan.task_id}.json'
                    latest_task = task_path.read_bytes()
                    with self.assertRaises((ValueError, RuntimeError)):
                        forward()
                    self.assertEqual(plan, transaction.plan_store.load(plan.task_id))
                    self.assertEqual(state, transaction.state_store.load(plan.task_id))
                    self.assertEqual(latest_task, task_path.read_bytes())
                    raise _ContractStop('expected isolated TV drift rejection')
                await self.fixture(anime=True, commit_hook=reject)
                self.assertEqual([True], reached, 'drift did not reach actual TV transaction')

    async def test_disconnected_full_builder_cannot_sign_worker_draft(self):
        from src.rename import initial_tv_completion
        with patch.object(initial_tv_completion,'build_initial_tv_completion',return_value=None) as builder:
            root,service,before,result,*_=await self.fixture(anime=True)
        self.assertTrue(builder.called)
        self.assertFalse(result.state.confirmed_work)
        self.assertEqual(before['plan'],service.plan_store.load(result.plan.task_id))
        self.assertEqual(before['state'],service.decision_state_store.load(result.plan.task_id))


    async def test_both_catalog_leases_are_required_at_publication(self):
        from src.rename.provider_link_catalog import ProviderLinkCatalog
        from src.rename.series_catalog import SeriesCatalog
        for kind in ('series', 'provider'):
            with self.subTest(kind=kind):
                reached = []
                def reject(root, transaction, envelope, terminal, forward):
                    reached.append(True)
                    before = records(root)
                    lock = (SeriesCatalog(root / 'series_catalog').lock('series-00000000-0000-0000-0000-000000000001')
                            if kind == 'series' else ProviderLinkCatalog(root / 'provider_links').lock())
                    with self.assertRaises((ValueError, RuntimeError)):
                        with lock:
                            self.fail('competing catalog lease acquired during publication')
                    self.assertEqual(before, records(root))
                    raise _ContractStop('expected held catalog lease rejection')
                await self.fixture(anime=True, commit_hook=reject)
                self.assertEqual([True], reached, 'catalog lease negative did not reach actual TV commit')

    async def test_four_roots_and_actual_series_key_cannot_be_forged(self):
        from src.rename.initial_movie_completion import RootSnapshotSource
        from src.rename.target_routing import ROOT_KEYS, TargetRootSnapshot
        for fault in ('configuration', 'series_key'):
            with self.subTest(fault=fault):
                reached = []
                def reject(root, transaction, envelope, terminal, forward):
                    reached.append(True)
                    before = records(root)
                    intent = envelope.initial_tv_completion
                    if fault == 'configuration':
                        roots = TargetRootSnapshot.capture({key: root / 'changed-roots' / key for key in ROOT_KEYS})
                        altered = replace(envelope, initial_tv_completion=replace(intent, inputs=replace(intent.inputs, roots=roots)))
                        with self.assertRaises((ValueError, RuntimeError)):
                            forward(altered)
                    else:
                        altered = replace(envelope, initial_tv_completion=replace(intent, inputs=replace(intent.inputs,
                            series_key='series-00000000-0000-0000-0000-000000000099')))
                        with self.assertRaises((ValueError, RuntimeError)):
                            forward(altered)
                    self.assertEqual(before, records(root))
                    raise _ContractStop('expected forged TV observation rejection')
                await self.fixture(anime=True, commit_hook=reject)
                self.assertEqual([True], reached, 'observation negative did not reach actual TV commit')

    async def test_catalog_changes_after_preparation_cannot_publish(self):
        from src.rename import initial_tv_completion as tv
        from src.rename.provider_link_catalog import ProviderLinkMapping
        from tests.test_provider_link_catalog import _payload,_id
        from tests.test_series_catalog import canonical_series_key,tmdb_tv
        from tests._media_flow_helpers import MediaFlow
        import shutil
        history=self.enterContext(tempfile.TemporaryDirectory())
        historical,_=await MediaFlow('runTest')._materialize_s1(Path(history))
        series_file=next((Path(history)/'series_catalog').glob('*.json'))
        for kind in ('series','provider'):
            for mutation in ('same_revision_bytes','add','delete'):
                with self.subTest(kind=kind,mutation=mutation):
                    root,service,_,result,*_=await self.fixture(anime=False)
                    if kind=='series':
                        directory=service.series_catalog.root
                        directory.mkdir(parents=True,exist_ok=True)
                        path=directory/series_file.name
                        shutil.copyfile(series_file,path)
                    else:
                        provider=tmdb_tv('990001')
                        mapping=ProviderLinkMapping.from_dict(_payload(mapping_id=_id('provider-link'),
                            identity_kind='series',identity_key=canonical_series_key(provider),provider=provider))
                        saved=service.provider_link_catalog.save(mapping,expected_revision=-1)
                        path=service.provider_link_catalog._path(saved.mapping_id)
                    reached=[];prepare=tv.prepare
                    def change_catalog(*args,**kwargs):
                        intent=prepare(*args,**kwargs)
                        if mutation=='same_revision_bytes': path.write_bytes(path.read_bytes()+b'\n')
                        elif mutation=='delete': path.rename(path.with_suffix('.held'))
                        else:
                            # Corrupt new rows must also invalidate the observed namespace.
                            (path.parent/(("series-" if kind=='series' else "provider-link-")+"00000000-0000-0000-0000-000000000001.json")).write_text('{}',encoding='utf8')
                        reached.append(records(root))
                        return intent
                    video=next(e for e in result.plan.entries if e.media_kind=='video')
                    with patch.object(tv,'prepare',change_catalog):
                        with self.assertRaises((ValueError,RuntimeError)):
                            service.revise_tv_content(result.plan.task_id,PlanStore.fingerprint(result.plan),
                                expected_revision=result.state.revision,mode='role',
                                edit={'entry_id':entry_id(video),'role':'extra'})
                    self.assertEqual(1,len(reached))
                    self.assertEqual(reached[0],records(root))

    async def test_interrupted_first_tv_transaction_recovers_original_parent_in_fresh_instance(self):
        for phase in ('prepared','decision_written','ownership_claimed','plan_written'):
            with self.subTest(phase=phase):
                observed={}
                def interrupt(root,transaction,envelope,terminal,forward):
                    observed.update(root=root,transaction=transaction,task=envelope.decision.task_id,
                        before={k:v for k,v in records(root).items() if not k.startswith(('projection_journal/','tasks/'))})
                    def crash(_transaction,actual):
                        if actual==phase:
                            observed['reached']=actual
                            raise SystemExit('isolated TV interruption')
                    with patch.object(ProjectionCommitService,'_checkpoint',crash):
                        with self.assertRaises(SystemExit): forward()
                    raise _ContractStop('release original transaction before fresh recovery')
                await self.fixture(anime=True,commit_hook=interrupt)
                self.assertEqual(phase,observed['reached'])
                old=observed['transaction'];root=observed['root']
                fresh=ProjectionCommitService(old.state_store,old.plan_store,task_root=root/'tasks')
                result=fresh.recover_pending()
                self.assertEqual({observed['task']:'failed_closed'},result)
                after=records(root)
                self.assertEqual(observed['before'],{k:after.get(k) for k in observed['before']})
                self.assertFalse(old.state_store.load(observed['task']).confirmed_work)



    async def test_catalog_locks_cover_all_publication_checkpoints(self):
        from concurrent.futures import ThreadPoolExecutor
        from src.rename.provider_link_catalog import ProviderLinkCatalog
        from src.rename.series_catalog import SeriesCatalog
        reached = []
        phases = ('prepared', 'decision_written', 'ownership_claimed', 'plan_written', 'task_terminal_written')
        def inspect(root, transaction, envelope, terminal, forward):
            def contend(kind):
                lock = (SeriesCatalog(root / 'series_catalog').lock(envelope.initial_tv_completion.inputs.series_key)
                        if kind == 'series' else ProviderLinkCatalog(root / 'provider_links').lock())
                try:
                    with lock:
                        return False
                except (ValueError, RuntimeError):
                    return True
            def checkpoint(_transaction, phase):
                if phase in phases:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        blocked = list(pool.map(contend, ('series', 'provider')))
                    self.assertEqual([True, True], blocked, phase)
                    reached.append(phase)
            with patch.object(ProjectionCommitService, '_checkpoint', checkpoint):
                return forward()
        _, _, _, result, *_ = await self.fixture(anime=True, commit_hook=inspect)
        self.assertEqual(list(phases), reached)
        self.assert_success(result)

    async def test_incomplete_source_set_and_forged_task_scope_cannot_publish(self):
        for fault in ('source_set', 'task_scope', 'different_task_scope'):
            with self.subTest(fault=fault):
                reached = []
                def reject(root, transaction, envelope, terminal, forward):
                    reached.append(True)
                    before = records(root)
                    intent = envelope.initial_tv_completion
                    inputs = intent.inputs
                    altered_inputs = (replace(inputs, sources=replace(inputs.sources, rows=inputs.sources.rows[:-1]))
                                      if fault == 'source_set' else replace(inputs, scope=None if fault == 'task_scope'
                                          else replace(inputs.scope, reservation_revision=inputs.scope.reservation_revision + 1)))
                    altered = replace(intent, inputs=altered_inputs)
                    with self.assertRaises((ValueError, RuntimeError)):
                        forward(replace(envelope, initial_tv_completion=altered))
                    self.assertEqual(before, records(root))
                    raise _ContractStop('incomplete frozen task observations rejected')
                await self.fixture(anime=False, commit_hook=reject)
                self.assertEqual([True], reached)

    async def test_first_tv_preserves_manual_subtitle_rule_and_exact_owner(self):
        _, _, before, result, extras, *_ = await self.fixture(anime=False, companions=True, manual_subtitle=True)
        self.assert_success(result)
        self.assertEqual(before['plan'].subtitle_override_rules, result.plan.subtitle_override_rules)
        subtitle = next(e.subtitle for e in result.plan.entries if e.source_path == extras['subtitle'])
        self.assertEqual(extras['owner'], subtitle.associated_video_source_path)
        self.assertEqual('en', subtitle.primary_language)

    async def test_late_task_season_and_decision_changes_are_preserved(self):
        for fault in ('season', 'decision'):
            with self.subTest(fault=fault):
                reached = []
                def reject(root, transaction, envelope, terminal, forward):
                    reached.append(True)
                    if fault == 'season':
                        merge_task_at(root / 'tasks', envelope.decision.task_id, {'season_id': 2})
                    else:
                        # A newer valid Decision is durable evidence; do not overwrite it
                        # merely because its Plan has not yet been projected.
                        current = transaction.state_store.load(envelope.decision.task_id)
                        transaction.state_store.save(replace(current, recovery_state='healthy'),
                                                     expected_revision=current.revision)
                    changed = records(root)
                    with self.assertRaises((ValueError, RuntimeError)):
                        forward()
                    protected = lambda values: {k:v for k,v in values.items() if k.startswith(('decisions/', 'plans/'))}
                    self.assertEqual(protected(changed), protected(records(root)))
                    raise _ContractStop('late user state preserved')
                await self.fixture(anime=False, commit_hook=reject)
                self.assertEqual([True], reached)

    async def test_wrong_actual_transaction_store_or_catalog_root_cannot_publish(self):
        from contextlib import contextmanager
        from src.rename.initial_tv_completion import InitialTVCompletionContext
        original = InitialTVCompletionContext.publication_lock
        for fault in ('plan_store', 'series_root', 'provider_root'):
            with self.subTest(fault=fault):
                reached = []
                @contextmanager
                def reject(context, transaction, intent):
                    if intent.inputs.mode != 'initial':
                        with original(context, transaction, intent):
                            yield
                        return
                    reached.append(True)
                    root = context.task_root.parent
                    before = records(root)
                    target, field, value = ((transaction, 'plan_store', PlanStore(root / 'wrong-plans'))
                        if fault == 'plan_store' else
                        (context.series if fault == 'series_root' else context.provider, 'root', root / ('wrong-' + fault)))
                    with patch.object(target, field, value):
                        with self.assertRaises((ValueError, RuntimeError)):
                            with original(context, transaction, intent):
                                self.fail('wrong backend reached publication')
                    self.assertEqual(before, records(root))
                    raise _ContractStop('wrong actual TV backend rejected')
                    yield
                with patch.object(InitialTVCompletionContext, 'publication_lock', reject):
                    await self.fixture(anime=True)
                self.assertEqual([True], reached)


    async def test_reprepare_cannot_change_original_requested_season(self):
        from src.rename.initial_tv_completion import prepare, build_initial_tv_completion
        reached = []
        def reject(root, transaction, envelope, terminal, forward):
            reached.append(True)
            intent = envelope.initial_tv_completion
            before = records(root)
            parent = transaction.plan_store.load(intent.task_id)
            state = transaction.state_store.load(intent.task_id)
            with self.assertRaises((ValueError, RuntimeError)):
                altered = prepare(intent.context.service,
                    replace(intent.inputs.recognition, season_number=2), resolution=None, seasons=intent.inputs.seasons)
                self.assertEqual(intent.inputs.task_fingerprint, altered.inputs.task_fingerprint)
                self.assertEqual(intent.inputs.recognition.initial_movie_request,
                                 altered.inputs.recognition.initial_movie_request)
                built = build_initial_tv_completion(parent, state, altered)
                forward(replace(envelope, initial_tv_completion=altered,
                                decision=built.decision, draft_plan=built.draft))
            self.assertEqual(before, records(root))
            raise _ContractStop('changed originating season rejected')
        await self.fixture(anime=False, commit_hook=reject)
        self.assertEqual([True], reached)

    async def test_explicit_task_season_is_carried_by_actual_request(self):
        observed = []
        def inspect(root, transaction, envelope, terminal, forward):
            from src.utils.utils import read_task_at
            from src.rename.review_operation import current_provider_selection_review_operation
            task = read_task_at(root / 'tasks', envelope.decision.task_id)
            _, operation = current_provider_selection_review_operation(task)
            observed.append((task.get('season_id'), operation.get('requested_season_number'),
                             envelope.initial_tv_completion.inputs.recognition.initial_movie_request.requested_season))
            return forward()
        _, _, _, result, *_ = await self.fixture(anime=False, selected_season=2, commit_hook=inspect)
        self.assert_success(result)
        self.assertEqual([(2, 2, 2)], observed)
        self.assertEqual(2, result.state.season_binding['season_number'])
        self.assertEqual(2, result.task['season_id'])
        self.assertEqual({2}, {e.season_id for e in result.plan.entries if e.media_kind == 'video'})

    async def test_shared_season_choice_uses_frozen_provider_path_and_original_request(self):
        from src.rename.season_identity import resolve_initial_tv_season
        reached = []
        def inspect(root, transaction, envelope, terminal, forward):
            reached.append(True)
            intent = envelope.initial_tv_completion
            work = intent.inputs.recognition.work_identity
            multi = replace(intent.inputs.seasons, available=(0, 1, 2))
            plain = root / 'Unmarked'
            with patch('builtins.open', side_effect=AssertionError('season resolver attempted IO')), \
                    patch.object(Path, 'stat', side_effect=AssertionError('season resolver attempted IO')):
                self.assertEqual((2, 'inferred_path'), resolve_initial_tv_season(multi, work, root / 'Season 02', None, None))
                self.assertEqual((0, 'explicit_user'), resolve_initial_tv_season(multi, work, plain, 0, None))
                self.assertEqual((1, 'provider_unique'), resolve_initial_tv_season(
                    replace(multi, available=(0, 1)), work, plain, None, None))
                for observation in (multi,):
                    self.assertEqual((3, 'explicit_user'),
                        resolve_initial_tv_season(observation, work, plain, 3, None))
                for observation, path, request in ((multi, plain, None),
                        (replace(multi, provider=('tmdb', 'tv', '999')), plain, 1),
                        (replace(multi, available=()), plain, 1)):
                    with self.assertRaises(ValueError):
                        resolve_initial_tv_season(observation, work, path, request, None)
            return forward()
        _, _, _, result, *_ = await self.fixture(anime=False, commit_hook=inspect)
        self.assertEqual([True], reached)
        self.assert_success(result)

    async def test_terminal_cannot_publish_a_different_task_season(self):
        from src.rename.projection_authority import TaskTerminalMutation
        reached = []
        def reject(root, transaction, envelope, terminal, forward):
            reached.append(True)
            task_id = envelope.decision.task_id
            parent = transaction.plan_store.load(task_id)
            state = transaction.state_store.load(task_id)
            task_path = root / 'tasks' / f'{task_id}.json'
            task_bytes = task_path.read_bytes()
            original = TaskTerminalMutation.render
            def altered(mutation, decision, plan):
                payload = original(mutation, decision, plan)
                if mutation.task_id == task_id:
                    payload.update(season_id=99, season_number=99)
                return payload
            with patch.object(TaskTerminalMutation, 'render', altered):
                with self.assertRaises((ValueError, RuntimeError)):
                    forward()
            self.assertEqual(parent, transaction.plan_store.load(task_id))
            self.assertEqual(state, transaction.state_store.load(task_id))
            self.assertEqual(task_bytes, task_path.read_bytes())
            raise _ContractStop('wrong terminal season rejected')
        _, _, _, result, *_ = await self.fixture(anime=False, commit_hook=reject)
        self.assertEqual([True], reached)
        self.assertFalse(result.state.confirmed_work)

    async def test_registered_tv_role_event_runs_off_ui_thread(self):
        for fault in ('normal', 'cancel_waiter', 'closed', 'later_view', 'worker_error'):
            with self.subTest(fault=fault):
                await self.tv_role_event(fault)

    async def test_registered_tv_special_event_owns_worker_and_preserves_later_view(self):
        for fault in ('normal', 'cancel_waiter', 'closed', 'disconnect', 'later_view', 'worker_error'):
            with self.subTest(fault=fault):
                await self.tv_role_event(fault, special=True)

    async def tv_role_event(self, fault, special=False):
        import asyncio
        import inspect
        import threading
        from tests.test_complex_library_special_red_contract import _CaptureUi
        # Retain the legacy v1 GUI event contract used by frozen boundary
        # observations; v2 picker events are exercised in special_crosswalk_gui.
        if special:
            from src.rename.provider_special_evidence import ProviderSpecialObservation
            observed = ProviderSpecialObservation.build(state='completed_with_rows',
                provider_namespace='tmdb', provider_entity_type='tv', provider_id='53918',
                episodes=({'provider_namespace':'tmdb','provider_entity_type':'tv','provider_id':'53918',
                    'provider_episode_id':'9001','episode_number':1,'season_number':0,
                    'title':'Isolated special','air_date':'2020-01-01','runtime':24},))
            with patch('src.rename.special_crosswalk.observe', return_value=observed):
                root, service, _, result, extras, *_ = await self.fixture(anime=True,
                    exceptional=True, companions=True)
        else:
            root, service, _, result, extras, *_ = await self.fixture(anime=False, duplicate=True)
        self.assert_success(result)
        dialog = SimpleNamespace(plan=result.plan, task_decision=result.state, task_data=result.task,
            review_service=service, selected_ids=set(),
            snapshot=review_page.build_plan_review_snapshot(result.plan))
        dialog._adopt_revision = lambda plan: setattr(dialog, 'plan', plan)
        lifecycle = {}
        dialog.on_value_change = lambda callback: lifecycle.update(close=callback)
        from nicegui import Client
        from nicegui.page import page as nice_page
        dialog.client = Client(nice_page('/isolated-tv-role-' + fault), request=None)
        self.addCleanup(dialog.client.delete)
        register_disconnect = dialog.client.on_disconnect
        def disconnected(callback):
            lifecycle.update(disconnect=callback)
            return register_disconnect(callback)
        dialog.client.on_disconnect = disconnected
        dialog._rebuild_inventory_role = lambda *args: review_page.PlanReviewDialog._rebuild_inventory_role(dialog, *args)
        identifier = entry_id(next(e for e in result.plan.entries
            if e.source_path == extras['exceptional' if special else 'duplicate']))
        capture = _CaptureUi(selected=identifier)
        if special:
            dialog._exceptional_entry_ids = review_page.PlanReviewDialog._exceptional_entry_ids
            dialog._apply_exceptional_special_inline = lambda *args: review_page.PlanReviewDialog._apply_exceptional_special_inline(dialog, *args)
            dialog._apply_provider_special_inline = lambda *args: review_page.PlanReviewDialog._apply_provider_special_inline(dialog, *args)
        ui_thread = threading.get_ident()
        entered, release = threading.Event(), threading.Event()
        calls = []
        original = service.revise_tv_content
        def blocked(*args, **kwargs):
            calls.append(threading.get_ident())
            entered.set()
            release.wait(2)
            if fault == 'worker_error':
                raise ValueError('isolated observer failure')
            return original(*args, **kwargs)
        async def invoke():
            value = capture.callbacks['归入特别篇' if special else '确认为正片候选']()
            if inspect.isawaitable(value):
                await value
        with patch.object(review_page, 'ui', capture), patch.object(review_page, 'notify'), \
                patch.object(service, 'revise_tv_content', side_effect=blocked):
            if special:
                from tests.legacy_exceptional_ui_fixture import render_legacy_events
                render_legacy_events(dialog, ui_api=capture)
            else:
                review_page.PlanReviewDialog._render_asset_role_repair(dialog)
            first = asyncio.create_task(invoke())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.assertNotEqual(ui_thread, calls[0], 'registered GUI event blocked the UI thread')
                worker = dialog._tv_content_worker
                expected_view = dialog.plan
                if fault == 'cancel_waiter':
                    first.cancel()
                    await first
                    self.assertIs(worker, dialog._tv_content_worker)
                    self.assertFalse(worker.done())
                elif fault == 'closed':
                    lifecycle['close'](SimpleNamespace(value=False))
                elif fault == 'disconnect':
                    lifecycle['disconnect']()
                elif fault == 'later_view':
                    expected_view = replace(dialog.plan, revision_reason='a later view revision')
                    dialog.plan = expected_view
                second = asyncio.create_task(invoke())
                await asyncio.sleep(0.02)
                self.assertEqual(1, len(calls), 'duplicate click launched a second worker')
                release.set()
                await asyncio.wait_for(asyncio.gather(first, second), 5)
                try:
                    await asyncio.wait_for(asyncio.shield(worker), 5)
                except ValueError:
                    self.assertEqual('worker_error', fault)
                await asyncio.sleep(0)
                self.assertIsNone(dialog._tv_content_worker)
            finally:
                release.set()
                await first
        if fault in ('closed', 'disconnect', 'later_view', 'worker_error'):
            self.assertEqual(expected_view, dialog.plan)
        else:
            self.assertNotEqual(PlanStore.fingerprint(result.plan), PlanStore.fingerprint(dialog.plan))


    async def test_current_media_execution_and_catalog_relocation(self):
        from tests._media_flow_helpers import MediaFlow
        from src.rename.series_catalog import SeriesCatalogError
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                root, review, _, result, extras, *_ = await self.fixture(anime=True, companions=True, companion_language='en')
                self.assert_success(result)
                task, ledger, execution = await MediaFlow._approve_and_execute(self, root=root, result=result)
                self.assertEqual('completed', task['workflow_status'], task.get('error'))
                self.assertEqual('completed', ledger['status'])
                plan = result.plan
                key = plan.metadata['series_key']
                old_root = Path(plan.metadata['canonical_work_root'])
                moved = old_root.with_name('Moved')
                old_root.rename(moved)
                target = next(e.target_path for e in plan.entries if e.media_kind == 'video')
                target = moved / target.relative_to(old_root)
                if foreign:
                    retained = target.with_suffix('.saved')
                    target.rename(retained)
                    target.write_bytes(retained.read_bytes())
                path = execution.series_catalog._path(key)
                before = path.read_bytes()
                with patch('src.rename.small_file_identity.digest_file', side_effect=AssertionError('媒体定位不得读取内容')):
                    if foreign:
                        with self.assertRaises(SeriesCatalogError):
                            execution.series_catalog.relocate(key, moved, allowed_root=moved.parent,
                                record_root=root/'records', task_root=root/'tasks')
                        self.assertEqual(before, path.read_bytes())
                    else:
                        relocated = execution.series_catalog.relocate(key, moved, allowed_root=moved.parent,
                            record_root=root/'records', task_root=root/'tasks')
                        self.assertEqual(moved, relocated.canonical_root)
