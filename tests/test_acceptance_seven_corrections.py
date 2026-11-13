"""Seven-item acceptance incidents through public queues and actual GUI events."""
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace
from src.rename.plan_store import PlanStore
from src.pages.plan_review_page import build_plan_review_snapshot
from tests import test_content_workbench_gui as gui
from tests import test_initial_tv_completion_contract as tv
from tests import test_r1_real_media_production_red_contract as transport
from tests.test_content_choice_contract import change, records


class SevenAcceptanceIncidents(unittest.IsolatedAsyncioTestCase):
    fixture = tv.InitialTVCompletionContracts.fixture
    create_page = gui.ContentWorkbenchGui.create_page
    source_box = gui.ContentWorkbenchGui.source_box
    text = gui.ContentWorkbenchGui.text

    async def test_title_zero_is_not_a_special_season_in_real_selection(self):
        from pathlib import Path
        import tempfile
        from tests.test_r22_revised_c_c0_red_contract import _run_direct_tv
        from src.rename.plan_verifier import tv_authority_issue
        with tempfile.TemporaryDirectory() as tmp:
            result = await _run_direct_tv(Path(tmp), task_id='title-zero-selection',
                provider_id=78102, folder='[VCB-Studio] Steins;Gate 0 [Ma10p_1080p]',
                stem='[VCB-Studio] Steins;Gate 0', count=23)
            plan = result.plan_store.load(result.task_id)
            self.assertIsNotNone(plan.work_identity, result.task.get('error'))
            self.assertEqual(1, plan.metadata.get('season_number'))
            videos = [e for e in plan.entries if e.media_kind == 'video']
            self.assertEqual(23, len(videos))
            self.assertEqual({1}, {e.season_id for e in videos})
            self.assertTrue(all('Season 01' in str(e.target_path) for e in videos))
            self.assertIsNone(tv_authority_issue(plan))
            self.assertFalse(any(e.target_path.exists() for e in plan.entries))

    async def test_explicit_season_markers_and_user_choice_survive_strict_inference(self):
        from dataclasses import replace
        from src.rename.season_identity import resolve_initial_tv_season
        from src.rename.cleaner import extract_season
        reached = []
        def inspect(root, transaction, envelope, terminal, forward):
            reached.append(True)
            inputs = envelope.initial_tv_completion.inputs
            work = inputs.recognition.work_identity
            multi = replace(inputs.seasons, available=(0, 1, 2))
            for marker in ('S00', 'Season 00', '第0季'):
                self.assertEqual((0, 'inferred_path'), resolve_initial_tv_season(
                    multi, work, root / marker, None, None))
            for number in (0, 2):
                path = root / f'[Studio] Title {number} [1080p]'
                self.assertEqual(number, extract_season(path.name), 'legacy cleaning stays compatible')
                self.assertEqual((number, 'explicit_user'), resolve_initial_tv_season(
                    multi, work, root / 'Season 01', number, None))
                with self.assertRaisesRegex(ValueError, '无法唯一确定'):
                    resolve_initial_tv_season(multi, work, path, None, None)
                self.assertEqual((1, 'provider_unique'), resolve_initial_tv_season(
                    replace(multi, available=(0, 1)), work, path, None, None))
            return forward()
        await self.fixture(anime=False, commit_hook=inspect)
        self.assertEqual([True], reached)

    async def test_animation_tmdb_choice_reaches_current_plan_and_manual_season(self):
        candidate = dict(namespace='tmdb', entity_type='tv', provider_id='53918',
            title='向山进发', original_title='ヤマノススメ', year=2013,
            source_url='https://www.themoviedb.org/tv/53918', snapshot_at='2026-09-27T00:00:00Z')
        gates = []
        capability = tv.review_page.provider_selection_capability
        def observe_gate(*args, **kwargs):
            result = capability(*args, **kwargs)
            gates.append(result)
            return result
        with patch.object(transport, '_candidate', return_value=candidate), \
                patch.object(tv.review_page, 'provider_selection_capability', side_effect=observe_gate):
            root, service, before, result, *_ = await self.fixture(anime=True, selected_season=1, guard_legacy=True)
        self.assertTrue(before, 'actual registered selection callback must run')
        self.assertTrue(gates)
        self.assertTrue(gates[-1].actionable, gates[-1].message)
        self.assertTrue(result.state.confirmed_work, result.task.get('error'))
        self.assertEqual('tmdb', result.plan.work_identity.primary_provider.namespace)
        self.assertEqual('53918', result.plan.work_identity.primary_provider.provider_id)
        self.assertEqual('animation', result.plan.work_identity.domain)
        self.assertIsNone(result.state.tv_planning_authority)
        old = PlanStore.fingerprint(result.plan)
        plan = service.revise_tv_content(result.plan.task_id, old,
            expected_revision=result.state.revision, mode='season', edit={'season_number':9})
        state = service.decision_state_store.load(plan.task_id)
        self.assertEqual(9, state.season_binding['season_number'])
        self.assertEqual('explicit_user', state.season_binding['decision_source'])
        videos = [e for e in plan.entries if e.media_kind == 'video']
        expected = {p for p in before['plan'].expected_sources if p.suffix.lower() == '.mkv'}
        self.assertTrue(expected)
        self.assertEqual(expected, {e.source_path for e in videos})
        self.assertTrue(all('Season 09' in str(e.target_path) for e in videos))
        self.assertEqual('animation', service.plan_store.load(plan.task_id).work_identity.domain)
        self.assertFalse(service.load_or_create(plan).approved_entry_ids)
        self.assertFalse(any(e.target_path.exists() for e in plan.entries))

    async def test_animation_tmdb_plan_verifier_consumes_frozen_registration(self):
        from dataclasses import replace
        from src.rename.plan_verifier import PlanVerifier, tv_authority_issue
        candidate = dict(namespace='tmdb', entity_type='tv', provider_id='53918',
            title='向山进发', year=2013, snapshot_at='2026-09-27T00:00:00Z')
        with patch.object(transport, '_candidate', return_value=candidate):
            _, service, _, result, *_ = await self.fixture(anime=True, selected_season=1)
        plan = service.revise_tv_content(result.plan.task_id, PlanStore.fingerprint(result.plan),
            expected_revision=result.state.revision, mode='fonts', edit={})
        self.assertIsNone(tv_authority_issue(plan))
        self.assertFalse(PlanVerifier().verify(plan).issues)
        for field, value in (('series_registration_authority', None), ('series_binding_snapshot', {}),
                             ('season_binding_id', ''), ('series_key', 'wrong-series')):
            with self.subTest(field=field):
                invalid = replace(plan, metadata={**plan.metadata, field: value})
                self.assertIsNotNone(tv_authority_issue(invalid))
                self.assertFalse(PlanVerifier().verify(invalid).accepted)
        with patch('src.rename.series_registration.validate_plan_series_registration', return_value=None):
            self.assertIsNotNone(tv_authority_issue(plan))
        self.assertFalse(service.load_or_create(plan).approved_entry_ids)

    async def test_existing_bangumi_first_selection_control(self):
        _, _, _, result, *_ = await self.fixture(anime=True, selected_season=1, guard_legacy=True)
        self.assertEqual('bangumi', result.plan.work_identity.primary_provider.namespace)
        self.assertTrue(result.state.confirmed_work)

    async def test_ambiguous_tmdb_season_can_be_supplied_in_same_failed_task(self):
        import asyncio
        import json
        from src.pages import plan_review_page as page
        candidate = dict(namespace='tmdb', entity_type='tv', provider_id='53918',
            title='向山进发', year=2013, snapshot_at='2026-09-27T00:00:00Z')
        lookup = transport._FrozenSearch.get_tv_info_by_id
        def multi(search, provider_id):
            title, info = lookup(search, provider_id)
            return title, {**info, 'seasons': [{'season_number': n} for n in (0, 1, 2)]}
        async def repair(root, service, dialog, selected, original):
            manager = page.task_queue
            await asyncio.wait_for(manager._queue.join(), timeout=8)
            task = json.loads((root/'tasks'/f'{dialog.plan.task_id}.json').read_text('utf-8'))
            self.assertIn('无法唯一确定当前季度', task['error'])
            self.assertFalse(service.decision_state_store.load(dialog.plan.task_id).confirmed_work)
            self.create_page(prepared=(root, service, service.plan_store.load(dialog.plan.task_id), []))
            with patch.object(page, 'task_queue', manager), \
                    patch.object(page.PlanReviewDialog, '_select_provider_candidate', original):
                self.assertIn('确认季度并继续', self.text())
                unchanged = records(root)
                for value in (None, 1.5):
                    self.view.unresolved_season_input.value = value
                    await self.driver.click('确认季度并继续')
                    self.assertEqual(unchanged, records(root))
                revision = self.view.task_decision.revision
                for value in (True, -1, 1.5):
                    with self.assertRaises((ValueError, RuntimeError)):
                        await manager.enqueue_reidentify(dialog.plan.task_id, selected, revision, season_number=value)
                    self.assertEqual(unchanged, records(root))
                with self.assertRaises((ValueError, RuntimeError)):
                    await manager.enqueue_reidentify(dialog.plan.task_id, selected, revision - 1, season_number=1)
                self.assertEqual(unchanged, records(root))
                self.view.unresolved_season_input.value = 1
                await self.driver.click('确认季度并继续')
                await asyncio.wait_for(manager._queue.join(), timeout=8)
            state = service.decision_state_store.load(dialog.plan.task_id)
            plan = service.plan_store.load(dialog.plan.task_id)
            self.assertTrue(state.confirmed_work, (self.notices, manager._read(dialog.plan.task_id).get('error')))
            self.assertEqual('53918', state.confirmed_work['primary_provider']['provider_id'])
            self.assertEqual(1, state.season_binding['season_number'])
            self.assertEqual('explicit_user', state.season_binding['decision_source'])
            self.assertFalse(service.load_or_create(plan).approved_entry_ids)
            self.assertTrue(plan.entries)
            self.assertFalse(any(e.target_path.exists() for e in plan.entries))
            unchanged = records(root)
            with self.assertRaises((ValueError, RuntimeError)):
                await manager.enqueue_reidentify(plan.task_id, {**selected, 'provider_id': '999'},
                    state.revision, season_number=1)
            self.assertEqual(unchanged, records(root))
        with patch.object(transport, '_candidate', return_value=candidate), \
                patch.object(transport._FrozenSearch, 'get_tv_info_by_id', multi):
            await self.fixture(anime=True, guard_legacy=True, selection_hook=repair)

    async def test_invalid_content_keeps_review_errors_visible_without_resource_rows(self):
        from dataclasses import replace
        self.create_page()
        invalid = replace(self.plan, source_root=self.plan.source_root.parent)
        snapshot = build_plan_review_snapshot(invalid)
        self.assertIn('content_selection_invalid', {item['code'] for item in snapshot.issues})
        self.assertEqual(len(self.plan.entries), len(snapshot.rows))
        self.assertFalse(any(row.get('resource_only') for row in snapshot.rows))

    async def test_animation_tmdb_catalog_domain_and_same_provider_correction(self):
        import json
        from src.rename import series_registration as registration
        from src.rename.series_catalog import SeriesCatalog, SeriesCatalogError
        from tests.test_general_work_correction_contract import TARGET, DETAIL
        candidate = dict(namespace='tmdb', entity_type='tv', provider_id='53918',
            title='向山进发', year=2013, snapshot_at='2026-09-27T00:00:00Z')
        with patch.object(transport, '_candidate', return_value=candidate):
            root, service, _, result, *_ = await self.fixture(anime=True, selected_season=1)
        plan, state = result.plan, result.state
        proof = registration.validate_plan_series_registration(plan)
        self.assertEqual('tmdb', proof.namespace)
        with self.assertRaisesRegex(ValueError, '旧计划缺少已完成只读资格'):
            registration.validate_completed_peer_identity(result.task, plan, state, root)
        catalog = SeriesCatalog(root/'consumer-catalog')
        captured = []
        original = catalog._require_namespace_candidate
        def validate(key, payload):
            captured.append(payload)
            return original(key, payload)
        with catalog.lock(proof.series_key), patch.object(catalog, '_require_namespace_candidate', side_effect=validate):
            catalog.require_namespace_plan(plan, proof.series_key)
        self.assertEqual('animation', captured[0]['domain'])
        raw = captured[0]
        path = catalog._path(proof.series_key)
        path.write_text(json.dumps(raw), encoding='utf-8')
        registration.validate_series_catalog_snapshot(proof, catalog, 0, raw['canonical_root'], library_domain='animation')
        raw['domain'] = 'general'
        path.write_text(json.dumps(raw), encoding='utf-8')
        with catalog.lock(proof.series_key), self.assertRaisesRegex(SeriesCatalogError, '媒体库'):
            catalog.require_namespace_plan(plan, proof.series_key)
        with self.assertRaisesRegex(ValueError, '媒体域'):
            registration.validate_series_catalog_snapshot(proof, catalog, 0, raw['canonical_root'], library_domain='animation')
        # A read-only observed record must also match the current domain exactly.
        binding = dict(provider_identity=plan.work_identity.primary_provider.to_dict(), season_number=1,
                       season_binding_id=plan.metadata['season_binding_id'])
        raw.update(domain='animation', season_bindings=[binding])
        match = SimpleNamespace(series_key=proof.series_key, revision=0,
            canonical_root=plan.metadata['canonical_work_root'], season_bindings=[binding])
        self.assertIsNotNone(registration.materialized_evidence_from_record(proof, raw, match, library_domain='animation'))
        with self.assertRaisesRegex(ValueError, '媒体域'):
            registration.materialized_evidence_from_record(proof, raw, match, library_domain='general')
        detail = {**DETAIL, 'genres':[{'id':16,'name':'Animation'}]}
        lookup = SimpleNamespace(get_tv_info_by_id=lambda _id: ('动画新作品', detail),
            get_season_info=lambda _id, season: {'season_number': season, 'episodes': []})
        with patch('src.rename.get_info.Search', return_value=lookup):
            observed = service.observe_work_correction(plan.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, candidate={**TARGET,'library_domain':'animation'})
        intent = service.prepare_work_correction(observed, season_number=3, reason='人工更正动画作品')
        with patch.object(lookup, 'get_tv_info_by_id', side_effect=AssertionError('no refetch')):
            revised = service.commit_work_correction(intent)
        self.assertEqual(('animation','5252'), (revised.work_identity.domain,revised.work_identity.primary_provider.provider_id))
        self.assertFalse(service.load_or_create(revised).approved_entry_ids)
        self.assertFalse(any(e.target_path.exists() for e in revised.entries))


    async def test_two_columns_direct_roundtrip_keeps_companions_and_no_approval(self):
        self.create_page()
        video = self.videos[1]
        await self.driver.click('内容保留与排除')
        self.assertIn('纳入整理', self.text())
        self.assertNotIn('预览不整理', self.text())
        self.source_box(video).value = True
        await self.driver.click('排除整理')
        current = self.service.plan_store.load(self.plan.task_id)
        self.assertTrue({video, video.with_suffix('.sc.ass'), video.with_suffix('.mka')} <= set(current.ignored_sources))
        self.assertTrue(self.view._content_workbench.alive())
        unchanged = records(self.root)
        self.source_box(video.with_suffix('.sc.ass')).value = True
        await self.driver.click('纳入整理')
        self.assertEqual(unchanged, records(self.root))
        self.assertIn('一并纳入所属视频', self.text())
        self.source_box(video.with_suffix('.sc.ass')).value = False
        self.source_box(video).value = True
        await self.driver.click('纳入整理')
        current = self.service.plan_store.load(self.plan.task_id)
        self.assertFalse({video, video.with_suffix('.sc.ass'), video.with_suffix('.mka')} & set(current.ignored_sources))
        self.assertFalse(self.service.load_or_create(current).approved_entry_ids)

    async def test_direct_action_rejects_old_window_without_writes(self):
        self.create_page()
        await self.driver.click('内容保留与排除')
        later = change(self.service, self.plan, [self.videos[0].with_suffix('.sc.ass')])
        with self.client:
            await self.view._adopt_revision(later)
        self.source_box(self.videos[1]).value = True
        before = records(self.root)
        await self.driver.click('排除整理')
        self.assertEqual(before, records(self.root))
        self.assertIn('重新打开', self.text())

    async def test_existing_stale_service_preparation_control(self):
        self.create_page()
        await self.driver.click('内容保留与排除')
        later = change(self.service, self.plan, [self.videos[0].with_suffix('.sc.ass')])
        with self.client:
            await self.view._adopt_revision(later)
        self.source_box(self.videos[1]).value = True
        before = records(self.root)
        await self.view._content_workbench.prepare('exclude')
        self.assertEqual(before, records(self.root))
        self.assertIn('重新打开', self.text())



if __name__ == '__main__':
    unittest.main()
