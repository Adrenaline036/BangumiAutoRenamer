"""Real current TV transactions, disposable files and frozen special choices."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from src.rename.plan_verifier import PlanVerifier
from src.rename.special_catalog import read_source, build_observation
from src.rename.special_selection import validate_descriptive_special
from tests import test_initial_tv_completion_contract as tv
from tests.test_content_choice_contract import change, records


class SpecialSelectionContracts(unittest.IsolatedAsyncioTestCase):
    fixture = tv.InitialTVCompletionContracts.fixture

    def revise(self, service, plan, mode, edit):
        return service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
            expected_revision=service.decision_state_store.load(plan.task_id).revision, mode=mode, edit=edit)

    def describe(self, service, plan, key, title='官方特别篇标题'):
        return self.revise(service, plan, 'special', dict(entry_id=key, action='descriptive_special',
            title=title, title_source='user', reference_key=None))

    async def test_descriptive_special_keeps_companions_reopens_and_restores(self):
        root, service, _, result, extras, *_ = await self.fixture(anime=False, exceptional=True,
            exceptional_name='Show SP.mkv', companions=True)
        key = entry_id(next(e for e in result.plan.entries if e.source_path == extras['exceptional']))
        with patch('src.rename.get_info.Search', side_effect=AssertionError('no provider calls during confirmation')):
            plan = self.describe(service, result.plan, key)
        video = next(e for e in plan.entries if e.source_path == extras['exceptional'])
        self.assertTrue(validate_descriptive_special(plan, video))
        self.assertEqual((0, None), (video.season_id, video.episode_id))
        self.assertIn('Season 00', str(video.target_path))
        self.assertTrue(video.target_path.stem.endswith(' - 官方特别篇标题'))
        self.assertNotIn('S00E', video.target_path.name)
        for name in ('subtitle', 'audio'):
            entry = next(e for e in plan.entries if e.source_path == extras[name])
            self.assertEqual(video.target_path.parent, entry.target_path.parent)
            self.assertTrue(entry.target_path.name.startswith(video.target_path.stem))
        issues = PlanVerifier().verify(plan).issues
        self.assertFalse([i for i in issues if i.source_path == video.source_path], issues)
        self.assertEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(service.plan_store.load(plan.task_id)))
        restored = change(service, change(service, plan, [video.source_path]), [video.source_path], action='restore')
        self.assertTrue(validate_descriptive_special(restored, next(e for e in restored.entries if e.source_path == video.source_path)))
        self.assertFalse(service.load_or_create(restored).approved_entry_ids)
        self.assertFalse(any(e.target_path.exists() for e in restored.entries))

    async def test_distinct_descriptions_cannot_bypass_collision_or_tamper_checks(self):
        root, service, _, result, extras, *_ = await self.fixture(anime=False, exceptional=True, exceptional_name='Show SP.mkv')
        videos = [e for e in result.plan.entries if e.media_kind == 'video']
        a, b = videos[:2]
        first = self.describe(service, result.plan, entry_id(a), '第一段特别篇')
        second = self.describe(service, first, entry_id(b), '第二段特别篇')
        described = [e for e in second.entries if validate_descriptive_special(second, e)]
        self.assertEqual(2, len(described))
        self.assertEqual(2, len({e.presentation_asset.version_descriptor.version_ref for e in described}))
        self.assertNotIn('presentation_structural_default_not_unique', {i.code for i in PlanVerifier().verify(second).issues})
        collision = self.describe(service, second, entry_id(b), '第一段特别篇')
        self.assertIn('duplicate_target', {i.code for i in PlanVerifier().verify(collision).issues})
        entry = described[0]
        tampered = replace(second, entries=tuple(replace(e, target_path=e.target_path.with_name('wrong.mkv')) if e == entry else e for e in second.entries))
        self.assertIn('descriptive_special_invalid', {i.code for i in PlanVerifier().verify(tampered).issues})
        before = records(root)
        for title in ('../outside', 'Episode EP01', '', 'S00E01'):
            with self.assertRaises(ValueError):
                self.describe(service, collision, entry_id(a), title)
        self.assertEqual(before, records(root))

    async def test_paired_frozen_tmdb_selection_refuses_changed_row_refresh(self):
        root, service, _, result, extras, *_ = await self.fixture(anime=False, exceptional=True, exceptional_name='Show SP.mkv')
        provider = result.plan.work_identity.primary_provider.to_dict()
        def observed(title):
            source = read_source(provider, client=SimpleNamespace(get_season_info=lambda *_: {
                'episodes': [dict(id=9001, episode_number=1, name=title)]}))
            return build_observation(provider, [source])
        with patch('src.rename.special_crosswalk.observe', return_value=observed('Original title')):
            plan = self.revise(service, result.plan, 'special_refresh', {})
        key = entry_id(next(e for e in plan.entries if e.source_path == extras['exceptional']))
        plan = self.revise(service, plan, 'special', dict(entry_id=key, action='confirmed_special_correspondence',
            tmdb_episode_id='9001', bangumi_key=None))
        with patch('src.rename.special_crosswalk.observe', return_value=observed('Original title')):
            plan = self.revise(service, plan, 'special_refresh', {})
        before = records(root)
        with patch('src.rename.special_crosswalk.observe', return_value=observed('Changed title')):
            with self.assertRaisesRegex(ValueError, '取消特别篇对应'):
                self.revise(service, plan, 'special_refresh', {})
        self.assertEqual(before, records(root))
        cleared = self.revise(service, plan, 'role', dict(entry_id=key, role='main_candidate'))
        self.assertNotIn(key, service.decision_state_store.load(plan.task_id).provider_special_decisions)
        with patch('src.rename.special_crosswalk.observe', return_value=observed('Changed title')):
            self.revise(service, cleared, 'special_refresh', {})

    async def test_partial_observation_is_visible_but_requires_explicit_decision(self):
        root, service, _, result, extras, *_ = await self.fixture(anime=False, exceptional=True, exceptional_name='Show [13(OVA)].mkv')
        provider = result.plan.work_identity.primary_provider.to_dict()
        source = read_source(provider, client=SimpleNamespace(get_season_info=lambda *_: {
            'episodes': [dict(id=9001, episode_number=1, name='Good'), dict(id=9002, episode_number=True)]}))
        observed = build_observation(provider, [source])
        self.assertEqual('incomplete', observed.state)
        with patch('src.rename.special_crosswalk.observe', return_value=observed):
            plan = self.revise(service, result.plan, 'special_refresh', {})
        self.assertEqual('incomplete', plan.metadata['provider_special_observation']['state'])
        codes = {i.code for i in PlanVerifier().verify(plan).issues}
        self.assertIn('provider_special_decision_required', codes)
        key = entry_id(next(e for e in plan.entries if e.source_path == extras['exceptional']))
        confirmed = self.describe(service, plan, key, '保留的特别篇')
        self.assertNotIn('provider_special_decision_required', {i.code for i in PlanVerifier().verify(confirmed).issues})

    async def test_descriptive_actual_fixture_execution_catalog_and_completed_rebuild(self):
        from tests.test_cross_provider_work_correction import CrossProviderWorkCorrection
        from tests import _media_flow_helpers as execution_fixture
        root, service, _, result, extras, *_ = await CrossProviderWorkCorrection.original(self, tmdb=True, special=True)
        key = entry_id(next(e for e in result.plan.entries if e.source_path == extras['exceptional']))
        changed = self.describe(service, result.plan, key, '官方特别篇')
        self.assertFalse(PlanVerifier().verify(changed).issues)
        harness = execution_fixture.MediaFlow('runTest')
        task, ledger, execution = await harness._approve_and_execute(root=root, result=SimpleNamespace(plan=changed))
        self.assertEqual('completed', task.get('execution_status'), task.get('error'))
        self.assertEqual('completed', ledger['status'])
        completed = service.plan_store.load(changed.task_id)
        video = next(e for e in completed.entries if e.source_path == extras['exceptional'])
        self.assertTrue(validate_descriptive_special(completed, video))
        self.assertTrue(video.target_path.is_relative_to(root))
        self.assertEqual(video.source_path.stat().st_ino, video.target_path.stat().st_ino)
        self.assertEqual(video.source_path.stat().st_dev, video.target_path.stat().st_dev)
        self.assertGreaterEqual(video.source_path.stat().st_nlink, 2)
        binding = completed.metadata['catalog_binding_snapshot']
        self.assertEqual('series', binding['backend'])
        self.assertTrue(list((root / 'series_catalog').rglob('*.json')))
        video.target_path.unlink()  # Disposable target only, verified inside this fixture root.
        with patch('src.rename.get_info.Search', side_effect=AssertionError('rebuild must stay offline')):
            state = execution.rebuild_service.inspect(changed.task_id)
            fresh = execution.rebuild_service.generate(changed.task_id, expected_fingerprint=state.fingerprint)
        rebuilt_video = next(e for e in fresh.entries if e.source_path == extras['exceptional'])
        self.assertTrue(validate_descriptive_special(fresh, rebuilt_video))
        self.assertEqual(video.target_path, rebuilt_video.target_path)
        self.assertFalse(video.target_path.exists())
        self.assertFalse(service.load_or_create(fresh).approved_entry_ids)

    async def test_descriptive_actual_replacement_and_old_source_reentry(self):
        from tests.test_cross_provider_work_correction import CrossProviderWorkCorrection, TMDB
        from tests import _media_flow_helpers as execution_fixture
        from src.rename.completed_source_reentry import generate_reentry
        from src.rename.plan_execution import PlanExecutionError
        root, service, _, result, extras, *_ = await CrossProviderWorkCorrection.original(self, tmdb=True, companions=False)
        video = next(e for e in result.plan.entries if e.media_kind == 'video')
        old = self.describe(service, result.plan, entry_id(video), '保留特别篇')
        harness = execution_fixture.MediaFlow('runTest')
        task, _, execution = await harness._approve_and_execute(root=root, result=SimpleNamespace(plan=old))
        self.assertEqual('completed', task.get('execution_status'), task.get('error'))
        source, _ = execution_fixture.make_season(root / 'replacement',
            archive_bytes=b'excluded archive')
        for candidate in source.rglob('*.mkv'):
            with candidate.open('ab') as stream:
                stream.write(b'new fixture media')
        incoming = await harness._run_flow(root=root, source=source,
            task_id='replacement-descriptive-special', subject_id='41658')
        self.assertIn('pending_work_affiliation', incoming.plan.metadata)
        bound = service.link_existing_catalog_work(incoming.plan.task_id,
            PlanStore.fingerprint(incoming.plan), work_key=old.metadata['series_key'],
            reason='确认新来源属于原特别篇作品')
        correction = CrossProviderWorkCorrection('runTest')
        corrected = correction.confirm(service, bound, TMDB, 1, targets_absent=False)
        incoming_video = next(e for e in corrected.entries if e.media_kind == 'video')
        corrected = self.describe(service, corrected, entry_id(incoming_video), '保留特别篇')
        operation, _ = service.asset_actions().prepare_replacement(corrected)
        selected = service.confirm_asset_replacement(corrected.task_id, PlanStore.fingerprint(corrected),
            expected_operation_fingerprint=operation.fingerprint)
        task, _, execution = await harness._approve_and_execute(root=root, result=SimpleNamespace(plan=selected))
        self.assertEqual('completed', task.get('execution_status'), task.get('error'))
        with patch('src.rename.get_info.Search', side_effect=AssertionError('reentry must stay offline')):
            fresh = generate_reentry(execution.rebuild_service, execution.review_service, old.task_id)
        retained = next(e for e in fresh.entries if e.source_path == video.source_path)
        self.assertTrue(validate_descriptive_special(fresh, retained))
        self.assertEqual('animation', fresh.work_identity.domain)
        for fingerprint in (PlanStore.fingerprint(old), PlanStore.fingerprint(fresh)):
            with self.assertRaises(PlanExecutionError):
                execution.validate_approved_authority(fresh.task_id, fingerprint)

    async def test_special_replacement_with_missing_or_empty_regular_season(self):
        from pathlib import Path
        from tests.test_cross_provider_work_correction import CrossProviderWorkCorrection
        for empty_directory in (False, True):
            with self.subTest(empty_directory=empty_directory):
                root, service, _, result, *_ = await CrossProviderWorkCorrection.original(self, tmdb=True, companions=False)
                video = next(e for e in result.plan.entries if e.media_kind == 'video')
                plan = self.describe(service, result.plan, entry_id(video), '保留特别篇')
                special = next(e for e in plan.entries if e.source_path == video.source_path)
                special.target_path.parent.mkdir(parents=True, exist_ok=True)
                special.target_path.write_bytes(b'previous special')
                untouched = special.target_path.parent / 'unselected.mkv'
                untouched.write_bytes(b'unselected special')
                season = Path(plan.metadata['canonical_work_root']) / 'Season 01'
                self.assertFalse(season.exists())
                if empty_directory:
                    season.mkdir()
                operation, draft = service.asset_actions().prepare_replacement(plan)
                value = operation.to_dict()
                self.assertEqual('asset-operation-v3', value['policy'])
                self.assertIn(str(special.target_path), [r['target'] for r in value['replaces']])
                self.assertEqual([], value['season_directory']['files'])
                self.assertEqual([str(special.target_path)],
                    [r['target'] for r in value['season_directory']['special_files']])
                self.assertEqual([], value['retirements'])
                self.assertEqual(b'unselected special', untouched.read_bytes())
                self.assertFalse([i for i in PlanVerifier().verify(draft).issues
                    if i.code == 'asset_collision_unresolved' and i.source_path == video.source_path])
                selected = service.confirm_asset_replacement(plan.task_id, PlanStore.fingerprint(plan),
                    expected_operation_fingerprint=operation.fingerprint)
                from tests import _media_flow_helpers as execution_fixture
                harness = execution_fixture.MediaFlow('runTest')
                task, ledger, _ = await harness._approve_and_execute(root=root, result=SimpleNamespace(plan=selected))
                self.assertEqual('completed', task.get('execution_status'), task.get('error'))
                self.assertEqual('completed', ledger['status'])
                self.assertTrue(special.target_path.samefile(special.source_path))
                self.assertEqual(b'unselected special', untouched.read_bytes())

    async def test_special_companion_reuse_and_scope_drift(self):
        import copy
        from src.rename.asset_operation import AssetOperation
        from src.rename.season_replacement import validate, validate_directory
        from src.rename.special_decision_projection import video_bundle_closure
        from tests.test_cross_provider_work_correction import CrossProviderWorkCorrection
        root, service, _, result, *_ = await CrossProviderWorkCorrection.original(self, tmdb=True, companions=False)
        video = next(e for e in result.plan.entries if e.media_kind == 'video')
        key = entry_id(video)
        plan = self.describe(service, result.plan, key, '保留特别篇')
        bundle = [entry for _, entry in video_bundle_closure(plan, key)]
        subtitle = next(e for e in bundle if e.subtitle is not None)
        for entry in bundle:
            entry.target_path.parent.mkdir(parents=True, exist_ok=True)
            entry.target_path.write_bytes(entry.source_path.read_bytes() if entry == subtitle else b'old asset')
        operation, _ = service.asset_actions().prepare_replacement(plan)
        value = operation.to_dict()
        rows = value['season_directory']['special_files']
        self.assertEqual('reuse', next(r['disposition'] for r in rows if r['target'] == str(subtitle.target_path)))
        selected = service.confirm_asset_replacement(plan.task_id, PlanStore.fingerprint(plan),
            expected_operation_fingerprint=operation.fingerprint)
        self.assertFalse(PlanVerifier().verify(selected).issues)
        service.approve_entries(selected.task_id, PlanStore.fingerprint(selected), [entry_id(e) for e in selected.entries])
        forged = copy.deepcopy(value)
        forged['season_directory']['special_files'][0]['disposition'] = 'retire'
        with self.assertRaisesRegex(ValueError, '特别篇'):
            AssetOperation.from_mapping(forged)
        unselected = replace(selected, metadata={**selected.metadata, 'provider_special_decisions': {}})
        with self.assertRaisesRegex(ValueError, '特别篇'):
            validate(service.asset_actions(), unselected, value)
        subtitle.target_path.write_bytes(b'changed subtitle after confirmation')
        with self.assertRaisesRegex(ValueError, '特别篇目标已变化'):
            validate_directory(value['season_directory'])
