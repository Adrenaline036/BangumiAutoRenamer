"""NAS split-root incidents through public planning and disposable registration.

Only Provider transport is replayed. No source or record is copied from a NAS.
"""
import json
from contextlib import contextmanager
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from src.rename.plan_review import PlanReviewService, PlanReviewError
from src.rename.provider_search import BangumiProviderClient
from src.rename.target_routing import TargetRootSnapshot
from src.rename.initial_movie_completion import RootSnapshotSource
from src.pages.plan_review_page import PlanReviewDialog
from tests import test_r1_real_media_production_red_contract as flow
from tests import _media_flow_helpers as tv
from tests import _media_flow_helpers as movie


TV = dict(id=1396, name='绝命毒师', original_name='Breaking Bad',
    first_air_date='2008-01-20', genres=[dict(name='Drama')], seasons=[
        dict(season_number=n, name=f'Season {n}', episode_count=13,
             air_date=f'{2007+n}-01-20') for n in range(1, 6)])
MOVIE = dict(id=533514, title='紫罗兰永恒花园 剧场版', original_title='劇場版 ヴァイオレット・エヴァーガーデン',
    release_date='2020-09-18', genres=[dict(name='Animation')])
SUBJECTS = {
    '5337': dict(id=5337, type=6, platform='欧美剧', name='Breaking Bad',
                 name_cn='绝命毒师', date='2008-01-20', eps=7),
    '20324': dict(id=20324, type=6, platform='欧美剧', name='Breaking Bad Season 4',
                  name_cn='绝命毒师 第四季', date='2011-07-17', eps=13),
    '242216': dict(id=242216, type=2, platform='剧场版', name=MOVIE['original_title'],
                   name_cn='剧场版 紫罗兰永恒花园', date='2020-09-18', eps=1),
}


class Search(flow._FrozenSearch):
    def get_tv_info(self, *args): return '', None
    def get_movie_info(self, *args): return '', None
    def get_tv_info_by_id(self, provider_id):
        return (TV['name'], dict(TV)) if str(provider_id) == '1396' else ('', None)
    def get_movie_info_by_id(self, provider_id):
        return (MOVIE['title'], dict(MOVIE)) if str(provider_id) == '533514' else ('', None)
    def get_season_info(self, provider_id, season_number):
        if str(provider_id) != '1396': raise AssertionError(provider_id)
        return dict(season_number=season_number, episodes=[])


class Session:
    def post(self, *_args, **_kwargs): return flow._Response(dict(data=[]))
    def get(self, url, **_kwargs):
        endpoint = str(url).split('https://api.bgm.tv', 1)[-1]
        if endpoint.startswith('/v0/episodes?'):
            return flow._Response(dict(total=0, offset=0, limit=100, data=[]))
        parts = endpoint.strip('/').split('/')
        if parts[:2] != ['v0', 'subjects']: raise AssertionError(endpoint)
        if parts[-1] == 'subjects': return flow._Response([])
        return flow._Response(dict(SUBJECTS[parts[2]]))


def candidate(namespace, kind):
    if namespace == 'tmdb':
        info = TV if kind == 'tv' else MOVIE
        return dict(namespace=namespace, entity_type=kind, provider_id=str(info['id']),
            title=info.get('name', info.get('title')), original_title=info.get('original_name', info.get('original_title')),
            year=2008 if kind == 'tv' else 2020, library_domain='general' if kind == 'tv' else 'animation')
    subject = SUBJECTS['5337' if kind == 'tv' else '242216']
    return dict(namespace='bangumi', entity_type='subject', provider_id=str(subject['id']),
        title=subject['name_cn'], original_title=subject['name'], year=int(subject['date'][:4]),
        platform=subject['platform'], library_domain='general' if kind == 'tv' else 'animation')


class WorkAffiliationIncidents(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.movie = movie.MediaFlow('runTest')
        self.addCleanup(self.movie.doCleanups)
        self.tv = tv.MediaFlow('runTest')
        self.addCleanup(self.tv.doCleanups)
        self.flow = flow.R1RealQueueWorkerContracts('runTest')
        self.addCleanup(self.flow.doCleanups)
        sample, _ = tv.make_season(self.root / 'valid-container')
        self.video_bytes = next(sample.glob('*.mkv')).read_bytes()

    def service(self):
        root = self.root
        service = PlanReviewService(PlanStore(root/'plans'), root/'reviews', root/'work_catalog',
            decision_state_root=root/'decisions', task_root=root/'tasks', record_root=root/'records',
            provider_link_catalog_root=root/'provider_links')
        service.root_snapshot_source = RootSnapshotSource(lambda: TargetRootSnapshot.capture(dict(
            bangumi_path=root/'TV', movie_path=root/'Movies', anime_path=root/'Anime', anime_movie_path=root/'AnimeMovies')))
        return service

    @contextmanager
    def transport(self, season=None):
        configured = flow._configured_renamer
        def renamer(root):
            value = configured(root)
            value.search = Search()
            value.bangumi_search = BangumiProviderClient(user_agent='BAR-rc54-test/1',
                session=Session(), sleeper=lambda _seconds: None)
            return value
        select = PlanReviewDialog._select_provider_candidate
        async def choose(view, row, **kwargs):
            if season is not None: kwargs['season_number'] = season
            return await select(view, row, **kwargs)
        with patch.object(flow, '_configured_renamer', side_effect=renamer), \
                patch.object(PlanReviewDialog, '_select_provider_candidate', choose):
            yield

    async def add(self, namespace, kind, *, season=1, generation='first', provider=None):
        source = self.root/'sources'/generation
        source.mkdir(parents=True)
        video = source/('Breaking Bad S%02dE01.mkv' % season if kind == 'tv' else 'Violet 1080p.mkv')
        video.write_bytes(self.video_bytes + generation.encode())
        domain = getattr(self, 'domain_override', 'animation' if kind == 'movie' else 'general')
        selected = {**(provider or candidate(namespace, kind)), 'library_domain': domain}
        with self.transport(season if kind == 'tv' else None):
            result = await self.flow._run_flow(root=self.root, source=source, task_id=generation,
                candidate=selected, is_anime=domain == 'animation', is_movie=kind == 'movie')
        if kind == 'movie':
            result = await self.movie._complete_movie(root=self.root, result=result, primary=video,
                version_groups=[dict(sources=[str(video)], label=generation)])
        return result

    async def finish(self, result, kind):
        if kind == 'movie':
            task, ledger, service = await self.movie._approve_and_execute(root=self.root, result=result)
        else:
            task, ledger, service = await self.tv._approve_and_execute(root=self.root, result=result)
        self.assertEqual('completed', task.get('execution_status'), task.get('error'))
        self.assertEqual('completed', ledger.get('status'), ledger)
        return service

    def stable_files(self):
        """Confirmation cannot change sources or already registered media."""
        folders = ('sources', 'work_catalog', 'series_catalog', 'TV', 'Movies', 'Anime', 'AnimeMovies')
        return {str(p.relative_to(self.root)): p.read_bytes() for name in folders
                for p in (self.root/name).rglob('*') if p.is_file() and not p.name.endswith('.lock')}

    async def movie_pair(self, first_provider, second_provider):
        first = await self.add(first_provider, 'movie')
        await self.finish(first, 'movie')
        second = await self.add(second_provider, 'movie', generation='second')
        self.assertEqual(second_provider, second.plan.work_identity.primary_provider.namespace)
        service = self.service()
        key = first.plan.metadata['proposed_work_key']
        self.assertNotEqual(first.plan.work_identity.title, second.plan.work_identity.title)
        self.assert_pending(second.plan)
        before = self.stable_files()
        linked = service.link_existing_catalog_work(second.plan.task_id, PlanStore.fingerprint(second.plan),
            work_key=key, reason='人工确认这是同一部电影的另一版本')
        self.assertEqual(key, linked.metadata['catalog_work_key'])
        self.assertEqual(first.plan.metadata['canonical_work_root'], linked.metadata['canonical_work_root'])
        self.assertEqual(first.plan.work_identity.title, linked.work_identity.title)
        self.assertEqual(second_provider, linked.work_identity.primary_provider.namespace)
        videos = [e for e in linked.entries if e.media_kind == 'video']
        self.assertTrue(videos, 'the completed movie must still have its video')
        for entry in videos:
            self.assertTrue(entry.target_path.is_relative_to(Path(first.plan.metadata['canonical_work_root'])),
                f'video still routed outside registered work: {entry.target_path}')
        from src.rename.projection_authority import validate_plan_projection_receipt
        from src.rename.movie_version_selection import from_plan
        validate_plan_projection_receipt(linked)
        self.assertEqual(key, from_plan(linked).to_dict()['work_key'])
        state = service.decision_state_store.load(linked.task_id)
        self.assertEqual(key, state.catalog_link_intent['work_key'])
        self.assertEqual(service.decision_state_store.fingerprint(state), linked.metadata['decision_state_fingerprint'])
        self.assertEqual(before, self.stable_files())
        self.assertEqual(1, len(list((self.root/'work_catalog').glob('work-*.json'))), 'preview cannot register')
        await self.finish(SimpleNamespace(**{**vars(second), 'plan': linked,
            'task': json.loads((self.root/'tasks'/f'{linked.task_id}.json').read_text('utf-8'))}), 'movie')
        after = self.service().work_catalog.load(key)
        self.assertEqual(2, len(after['versions']))
        self.assertEqual({'tmdb', 'bangumi'}, {p['namespace'] for p in after['providers']})
        third = await self.add(second_provider, 'movie', generation='third')
        self.assertNotIn('pending_work_affiliation', third.plan.metadata)
        self.assertEqual(key, third.plan.metadata['catalog_work_key'])
        self.assertEqual(first.plan.metadata['canonical_work_root'], third.plan.metadata['canonical_work_root'])

    async def test_fourth_season_subject_keeps_series_title_year_and_root(self):
        first = await self.add('tmdb', 'tv')
        await self.finish(first, 'tv')
        row = {**candidate('bangumi', 'tv'), 'provider_id': '20324', 'title': '绝命毒师 第四季',
               'original_title': 'Breaking Bad Season 4', 'year': 2011}
        fourth = await self.add('bangumi', 'tv', season=4, generation='fourth', provider=row)
        self.assert_pending(fourth.plan)
        linked = self.service().link_existing_catalog_work('fourth', PlanStore.fingerprint(fourth.plan),
            work_key=first.plan.metadata['series_key'], reason='第四季属于已登记的绝命毒师')
        self.assertEqual('绝命毒师', linked.work_identity.title)
        self.assertEqual(2008, linked.work_identity.year)
        self.assertEqual(first.plan.metadata['canonical_work_root'], linked.metadata['canonical_work_root'])
        for entry in linked.entries:
            if entry.media_kind == 'video':
                self.assertTrue(entry.target_path.is_relative_to(Path(first.plan.metadata['canonical_work_root'])/'Season 04'))
        await self.finish(SimpleNamespace(plan=linked), 'tv')

    async def correction(self, kind):
        from src.rename.work_affiliation import AffiliationRequired, preview_correction_choice, commit_choice
        from tests.test_content_choice_contract import records
        from dataclasses import replace
        first = await self.add('tmdb', kind)
        await self.finish(first, kind)
        second = await self.add('tmdb', kind, season=2, generation='second')
        service = self.service()
        # Finish already scheduled font work before defining the readonly baseline.
        if kind == 'tv':
            state = service.decision_state_store.load('second')
            second.plan = service.revise_tv_content('second', PlanStore.fingerprint(second.plan),
                expected_revision=state.revision, mode='fonts', edit={})
        state = service.decision_state_store.load('second')
        before = records(self.root)
        def get(_client, endpoint):
            return Session().get('https://api.bgm.tv'+endpoint).json()
        with patch.object(BangumiProviderClient, '_get_json', get):
            with self.assertRaises(AffiliationRequired) as raised:
                if kind == 'movie':
                    service.prepare_movie_work_correction('second', PlanStore.fingerprint(second.plan),
                        expected_revision=state.revision, candidate=candidate('bangumi', kind))
                else:
                    observation = service.observe_work_correction('second', PlanStore.fingerprint(second.plan),
                        expected_revision=state.revision, candidate=candidate('bangumi', kind))
                    service.prepare_work_correction(observation, season_number=2, reason='更换资料来源')
        inputs = raised.exception.inputs
        self.assertEqual(before, records(self.root), 'the initial unresolved preview cannot change the complete plan')
        key = first.plan.metadata['series_key' if kind == 'tv' else 'proposed_work_key']
        with patch.object(BangumiProviderClient, '_get_json', side_effect=AssertionError('no second Provider request')):
            from tests.test_rc54_approval_contract import source_reads
            with source_reads(second.plan) as (reads, processes):
                intent, preview = preview_correction_choice(service, inputs, key=key)
            self.assertEqual(0, reads['mkv_bytes'])
            self.assertEqual(0, processes.call_count)
            self.assertEqual(before, records(self.root), 'closing without commit preserves the old decision')
            self.assertEqual(first.plan.metadata['canonical_work_root'], preview.metadata['canonical_work_root'])
            # A copied observation is not the service-issued original.
            fake = (replace(inputs, work_request=replace(inputs.work_request,
                    observation=replace(inputs.work_request.observation))) if kind == 'tv' else
                    replace(inputs, work_observation=replace(inputs.work_observation)))
            with self.assertRaisesRegex(ValueError, '原件|实际读取'):
                preview_correction_choice(service, fake, key=key)
            self.assertEqual(before, records(self.root))
            chosen = commit_choice(service, intent)
            self.assertEqual('bangumi', chosen.work_identity.primary_provider.namespace)
            self.assertFalse(service.load('second').approved_entry_ids)
            after = records(self.root)
            with self.assertRaises((ValueError, RuntimeError)):
                commit_choice(service, intent)
            self.assertEqual(after, records(self.root), 'stale confirmation cannot publish again')

    async def test_movie_correction_uses_shared_affiliation_preview_and_cancel_preserves_plan(self):
        await self.correction('movie')

    async def test_tv_correction_uses_shared_affiliation_preview_and_cancel_preserves_plan(self):
        await self.correction('tv')

    async def test_animation_tv_both_orders_use_the_same_affiliation_contract(self):
        base = self.root
        self.domain_override = 'animation'
        with patch.dict(TV, genres=[dict(id=16, name='Animation')]), \
                patch.dict(SUBJECTS['5337'], type=2, platform='TV'):
            for left, right in [('tmdb', 'bangumi'), ('bangumi', 'tmdb')]:
                with self.subTest(first=left):
                    self.root = base/left
                    await self.tv_pair(left, right)

    async def test_general_movie_both_orders_use_the_same_affiliation_contract(self):
        base = self.root
        self.domain_override = 'general'
        with patch.dict(MOVIE, genres=[dict(id=18, name='Drama')]), \
                patch.dict(SUBJECTS['242216'], type=6, platform='电影'):
            for left, right in [('tmdb', 'bangumi'), ('bangumi', 'tmdb')]:
                with self.subTest(first=left):
                    self.root = base/left
                    await self.movie_pair(left, right)

    async def test_affiliation_rejects_changed_namespace_forged_paths_and_missing_receipt(self):
        from dataclasses import replace
        from src.rename.work_affiliation import preview_choice, commit_choice
        from src.rename.projection_authority import validate_plan_projection_receipt
        from tests.test_content_choice_contract import records
        first = await self.add('tmdb', 'movie')
        await self.finish(first, 'movie')
        second = await self.add('bangumi', 'movie', generation='second')
        service = self.service()
        key = first.plan.metadata['proposed_work_key']
        intent, _ = preview_choice(service, 'second', PlanStore.fingerprint(second.plan), key=key)
        path = service.work_catalog._path(key)
        original = path.read_bytes()
        altered = json.loads(original)
        altered['aliases'].append('changed after preview')
        path.write_text(json.dumps(altered, ensure_ascii=False), encoding='utf-8')
        before = records(self.root)
        with self.assertRaises((ValueError, RuntimeError)):
            commit_choice(service, intent)
        self.assertEqual(before, records(self.root))
        path.write_bytes(original)
        chosen = commit_choice(service, intent)
        for field, value in [('canonical_work_root', str(self.root/'outside')), ('catalog_work_key', 'work-'+'f'*32),
                             ('proposed_work_key', 'work-'+'f'*32), ('catalog_revision', 999)]:
            forged = replace(chosen, metadata={**chosen.metadata, field: value})
            with self.subTest(field=field), self.assertRaises((ValueError, RuntimeError)):
                validate_plan_projection_receipt(forged)
        forged = replace(chosen, metadata={k:v for k,v in chosen.metadata.items() if k != 'work_affiliation'})
        with self.assertRaises((ValueError, RuntimeError)):
            validate_plan_projection_receipt(forged)
        with self.assertRaises((ValueError, RuntimeError)):
            validate_plan_projection_receipt(replace(chosen, projection_receipt=None))
        # Old explicit_user text alone is not cross-Provider permission.
        self.assertFalse(service.work_catalog.compatible_base(service.work_catalog.load(key), forged,
                                                            allow_explicit_provider_append=True))

    async def test_explicit_choice_cannot_cross_library_domain(self):
        first = await self.add('tmdb', 'movie')
        await self.finish(first, 'movie')
        self.domain_override = 'general'
        with patch.dict(SUBJECTS['242216'], type=6, platform='电影'):
            other = await self.add('bangumi', 'movie', generation='other')
        from tests.test_content_choice_contract import records
        before = records(self.root)
        with self.assertRaisesRegex(PlanReviewError, '媒体库|不兼容'):
            self.service().link_existing_catalog_work('other', PlanStore.fingerprint(other.plan),
                work_key=first.plan.metadata['proposed_work_key'], reason='不能跨媒体库')
        self.assertEqual(before, records(self.root))

    async def test_movie_correction_gui_can_cancel_then_confirm_without_second_lookup(self):
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        from tests.test_content_choice_contract import records
        from src.pages import movie_work_correction_view
        first = await self.add('tmdb', 'movie')
        await self.finish(first, 'movie')
        second = await self.add('tmdb', 'movie', generation='second')
        service = self.service()
        gui = ContentWorkbenchGui('runTest')
        gui.create_page(prepared=(self.root, service, second.plan, []), cleanup_owner=self)
        with gui.client:
            gui.view._show_provider_candidates()
        before = records(self.root)
        async def show():
            with patch.object(BangumiProviderClient, '_get_json', return_value=dict(SUBJECTS['242216'])) as lookup:
                await movie_work_correction_view.show(gui.view, candidate('bangumi', 'movie'),
                    ui=__import__('nicegui').ui, notify=lambda *_a, **_kw: None)
                self.assertEqual(1, lookup.call_count)
        await show()
        await gui.driver.click('取消')
        self.assertEqual(before, records(self.root))
        await show()
        selected = next(e for e in gui.client.elements.values() if type(e).__name__ == 'Select' and e.label == '已登记作品'
                        and all(not p.is_deleted and (type(p).__name__ != 'Dialog' or p.value)
                                for p in e.ancestors(include_self=True)))
        selected.set_value(first.plan.metadata['proposed_work_key'])
        with patch.object(BangumiProviderClient, '_get_json', side_effect=AssertionError('no repeated lookup')):
            await gui.driver.click('预览归属')
            self.assertEqual(before, records(self.root))
            await gui.driver.click('确认作品归属')
        self.assertEqual(first.plan.metadata['canonical_work_root'], service.plan_store.load('second').metadata['canonical_work_root'])
        self.assertFalse(gui.view.provider_selection_expanded)

    async def test_movie_other_provider_and_different_title_can_choose_registered_work(self):
        await self.movie_pair('tmdb', 'bangumi')

    async def test_affiliation_gui_discards_cancelled_or_superseded_preview(self):
        import asyncio
        import threading
        from nicegui import ui
        from src.pages import work_affiliation_view as view
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        from tests.test_content_choice_contract import records
        from tests.test_unified_provider_link_contract import UnifiedProviderLinkContracts
        first = await self.add('tmdb', 'movie')
        await self.finish(first, 'movie')
        second = await self.add('bangumi', 'movie', generation='second')
        gui = ContentWorkbenchGui('runTest')
        gui.create_page(prepared=(self.root, self.service(), second.plan, []), cleanup_owner=self)
        def visible(element):
            return all(not p.is_deleted and (type(p).__name__ != 'Dialog' or p.value)
                       for p in element.ancestors(include_self=True))
        before = records(self.root)
        for mode in ('cancel', 'superseded'):
            with self.subTest(mode=mode):
                live = [True]
                await view.show(gui.view, ui=ui, notify=lambda *_a, **_k: None, is_current=lambda: live[0])
                selected = next(e for e in gui.client.elements.values()
                    if type(e).__name__ == 'Select' and e.label == '已登记作品' and visible(e))
                selected.set_value(first.plan.metadata['proposed_work_key'])
                button = next(e for e in gui.client.elements.values()
                    if type(e).__name__ == 'Button' and e.text == '预览归属' and visible(e))
                callback = UnifiedProviderLinkContracts('runTest').callback(button)
                entered, release = threading.Event(), threading.Event()
                real = view.preview_choice
                def delayed(*args, **kwargs):
                    entered.set()
                    if not release.wait(10):
                        raise TimeoutError('test preview barrier')
                    return real(*args, **kwargs)
                with patch.object(view, 'preview_choice', delayed):
                    with gui.client:
                        worker = asyncio.create_task(callback())
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                        if mode == 'cancel':
                            await gui.driver.click('取消')
                        else:
                            live[0] = False  # A newer selection owns the page now.
                    finally:
                        release.set()
                        await asyncio.wait_for(worker, 10)
                self.assertFalse(any(type(e).__name__ == 'Button' and e.text == '确认作品归属' and visible(e)
                                     for e in gui.client.elements.values()))
                self.assertEqual(before, records(self.root))
                if mode == 'superseded':
                    await gui.driver.click('取消')

    async def test_movie_reverse_order_keeps_first_registered_title_and_root(self):
        await self.movie_pair('bangumi', 'tmdb')

    async def tv_pair(self, first_provider, second_provider):
        first = await self.add(first_provider, 'tv')
        await self.finish(first, 'tv')
        second = await self.add(second_provider, 'tv', season=2, generation='second')
        self.assertEqual(2, second.plan.metadata.get('season_number'), second.task.get('error'))
        self.assert_pending(second.plan)
        key = first.plan.metadata['series_key']
        before = self.stable_files()
        # The existing public catalog selection boundary must support both
        # backends, dispatching to the full TV completer rather than rebasing.
        linked = self.service().link_existing_catalog_work(second.plan.task_id, PlanStore.fingerprint(second.plan),
            work_key=key, reason='人工确认属于已有绝命毒师，本次输出第二季')
        self.assertEqual(key, linked.metadata['series_key'])
        self.assertEqual(first.plan.metadata['canonical_work_root'], linked.metadata['canonical_work_root'])
        self.assertEqual(2, linked.metadata['season_number'])
        videos = [e for e in linked.entries if e.media_kind == 'video']
        self.assertTrue(videos, 'the completed TV plan must still have its video')
        for entry in videos:
            self.assertTrue(entry.target_path.is_relative_to(Path(first.plan.metadata['canonical_work_root'])/'Season 02'),
                f'video still routed outside registered season: {entry.target_path}')
            self.assertEqual(2, entry.season_id)
        from src.rename.series_registration import validate_plan_series_registration
        state = self.service().decision_state_store.load(linked.task_id)
        proof = validate_plan_series_registration(linked, state)
        self.assertEqual(key, proof.series_key)
        self.assertEqual(2, proof.to_dict()['schema_version'])
        self.assertEqual(2, state.season_binding['season_number'])
        self.assertEqual(before, self.stable_files())

        await self.finish(SimpleNamespace(plan=linked), 'tv')
        after = self.service().series_catalog.load(key)
        self.assertEqual({1, 2}, {row['season_number'] for row in after.season_bindings})
        third = await self.add(second_provider, 'tv', season=3, generation='third')
        self.assertNotIn('pending_work_affiliation', third.plan.metadata)
        self.assertEqual(key, third.plan.metadata['series_key'])
        self.assertEqual(first.plan.metadata['canonical_work_root'], third.plan.metadata['canonical_work_root'])

    async def test_tv_other_provider_can_choose_registered_series_for_output_second_season(self):
        await self.tv_pair('tmdb', 'bangumi')

    async def test_tv_reverse_order_keeps_first_registered_root(self):
        await self.tv_pair('bangumi', 'tmdb')

    async def test_same_bangumi_subject_manual_output_is_not_permanent_official_season(self):
        first = await self.add('bangumi', 'tv')
        await self.finish(first, 'tv')
        second = await self.add('bangumi', 'tv', season=2, generation='second')
        self.assertEqual(2, second.plan.metadata.get('season_number'), second.task.get('error'))
        self.assertEqual(first.plan.metadata['series_key'], second.plan.metadata.get('series_key'))
        self.assertEqual(first.plan.metadata['canonical_work_root'], second.plan.metadata.get('canonical_work_root'))

    async def test_valid_legacy_manual_membership_does_not_veto_new_output_season(self):
        from src.rename.series_registration import build_series_registration_authority, validate_plan_series_registration
        first = await self.add('bangumi', 'tv')
        await self.finish(first, 'tv')
        service = self.service()
        key = first.plan.metadata['series_key']
        path = service.series_catalog._path(key)
        record = json.loads(path.read_text('utf-8'))
        # Explicit supported old-schema observation, not a claim that today's
        # completer still produces schema 1. Source/targets/old Plan stay intact.
        legacy = build_series_registration_authority(first.plan.work_identity, manual_season=1)
        record['series_registration_authority'] = legacy.to_dict()
        service.series_catalog.validate_archived_payload(record, key)
        path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        before = self.stable_files()
        second = await self.add('bangumi', 'tv', season=2, generation='second')
        proof = validate_plan_series_registration(second.plan)
        self.assertEqual(key, proof.series_key)
        self.assertEqual((), proof.season_membership, 'manual output 2 is not an official relation')
        self.assertEqual(2, second.plan.metadata['season_number'])
        self.assertEqual(record, json.loads(path.read_text('utf-8')), 'preview cannot upgrade old registration')
        for name, data in before.items():
            self.assertEqual(data, (self.root / name).read_bytes())
        await self.finish(second, 'tv')
        actual = service.series_catalog.load(key)
        self.assertEqual({1, 2}, {row['season_number'] for row in actual.season_bindings})
        self.assertEqual(first.plan, service.plan_store.load(first.plan.task_id))

    async def test_manual_movie_affiliation_accepts_compatible_record_outside_recommendations(self):
        first = await self.add('tmdb', 'movie')
        await self.finish(first, 'movie')
        with patch.dict(SUBJECTS['242216'], name='Completely different catalog wording', name_cn='人工选择的另一种标题', date='2015-01-01'):
            second = await self.add('bangumi', 'movie', generation='second')
        self.assertNotIn('pending_work_affiliation', second.plan.metadata, 'no candidate means no mandatory question')
        self.assertNotEqual(first.plan.metadata['canonical_work_root'], second.plan.metadata['canonical_work_root'])
        before = self.stable_files()
        linked = self.service().link_existing_catalog_work('second', PlanStore.fingerprint(second.plan),
            work_key=first.plan.metadata['proposed_work_key'], reason='人工明确确认属于此电影，不受推荐集合限制')
        self.assertEqual(first.plan.metadata['canonical_work_root'], linked.metadata['canonical_work_root'])
        self.assertEqual(first.plan.work_identity.title, linked.work_identity.title)
        self.assertEqual(before, self.stable_files())

    async def test_exact_tmdb_binding_reuses_one_series_without_new_affiliation_question(self):
        first = await self.add('tmdb', 'tv')
        await self.finish(first, 'tv')
        second = await self.add('tmdb', 'tv', season=2, generation='second')
        self.assertEqual(2, second.plan.metadata.get('season_number'), second.task.get('error'))
        self.assertEqual(first.plan.metadata['series_key'], second.plan.metadata['series_key'])
        self.assertEqual(first.plan.metadata['canonical_work_root'], second.plan.metadata['canonical_work_root'])
        self.assertNotIn('pending_work_affiliation', second.plan.metadata)

    def assert_pending(self, plan):
        from src.rename.work_affiliation import pending_from_plan
        from src.rename.projection_authority import validate_plan_projection_receipt
        from src.rename.content_policy import require_content_complete
        self.assertIsNotNone(pending_from_plan(plan))
        validate_plan_projection_receipt(plan)
        self.assertEqual('inventory_review_draft', plan.metadata['mapping_source'])
        self.assertNotIn('canonical_work_root', plan.metadata)
        self.assertIsNone(self.service().decision_state_store.load(plan.task_id).asset_operation)
        with self.assertRaisesRegex(ValueError, '归入已有作品'):
            require_content_complete(plan)

    async def test_same_title_other_work_requires_explicit_separate_and_cancel_is_read_only(self):
        first = await self.add('tmdb', 'tv')
        await self.finish(first, 'tv')
        second = await self.add('bangumi', 'tv', season=2, generation='second')
        self.assert_pending(second.plan)
        from src.rename.work_affiliation import preview_choice, commit_choice
        service = self.service()
        before = self.stable_files()
        state_before = service.decision_state_store.load(second.plan.task_id)
        intent, preview = preview_choice(service, second.plan.task_id, PlanStore.fingerprint(second.plan), separate=True)
        self.assertNotEqual(first.plan.metadata['series_key'], preview.metadata['series_key'])
        self.assertIn('[同名-', preview.metadata['canonical_work_root'])
        self.assertEqual(before, self.stable_files())
        self.assertEqual(state_before, service.decision_state_store.load(second.plan.task_id))
        self.assertEqual(second.plan, service.plan_store.load(second.plan.task_id), 'cancelled preview leaves inventory unchanged')
        chosen = commit_choice(service, intent)
        self.assertNotIn('pending_work_affiliation', chosen.metadata)
        self.assertNotEqual(first.plan.metadata['canonical_work_root'], chosen.metadata['canonical_work_root'])
        self.assertEqual(before, self.stable_files())

    async def test_pending_gui_can_choose_registered_work_without_provider_requests(self):
        first = await self.add('tmdb', 'tv')
        await self.finish(first, 'tv')
        second = await self.add('bangumi', 'tv', season=2, generation='second')
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        gui = ContentWorkbenchGui('runTest')
        gui.create_page(prepared=(self.root, self.service(), second.plan, []), cleanup_owner=self)
        self.assertTrue(any('发现可能相同' in str(getattr(e, 'text', '')) for e in gui.client.elements.values()))
        before = self.stable_files()
        with patch.object(Search, 'get_tv_info_by_id', side_effect=AssertionError('no Provider request')), \
                patch.object(Session, 'get', side_effect=AssertionError('no Provider request')):
            await gui.driver.click('归入已有作品')
            select = next(e for e in gui.client.elements.values() if type(e).__name__ == 'Select' and e.label == '已登记作品')
            select.set_value(first.plan.metadata['series_key'])
            await gui.driver.click('预览归属')
            self.assertEqual(before, self.stable_files())
            await gui.driver.click('确认作品归属')
        actual = self.service().plan_store.load(second.plan.task_id)
        self.assertEqual(first.plan.metadata['series_key'], actual.metadata['series_key'])
        self.assertEqual(before, self.stable_files())


async def current_movie_pair(test, root):
    """Replacement for retired raw-rebase fixtures; actual admission and completion."""
    fixture = WorkAffiliationIncidents('runTest')
    fixture.setUp()
    test.addCleanup(fixture.doCleanups)
    fixture.root = root
    first = await fixture.add('tmdb', 'movie')
    await fixture.finish(first, 'movie')
    second = await fixture.add('bangumi', 'movie', generation='second')
    service = fixture.service()
    linked = service.link_existing_catalog_work(second.plan.task_id, PlanStore.fingerprint(second.plan),
        work_key=first.plan.metadata['proposed_work_key'], reason='人工确认属于已登记电影')
    return fixture, first.plan, linked, second, service

