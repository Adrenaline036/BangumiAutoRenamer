"""特别篇单侧资料、显式补取及原窗口操作状态。"""
import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from src.rename.special_crosswalk import observe
from src.rename.provider_special_evidence import ProviderSpecialObservation
from tests import test_initial_tv_completion_contract as tv
from tests import test_content_workbench_gui as gui

ROOT = dict(namespace='bangumi', entity_type='subject', provider_id='100')
OTHER = dict(namespace='tmdb', entity_type='tv', provider_id='50')


def clients(root=ROOT, search=None):
    def get(path):
        if '/subjects/' in path:
            return dict(id=int(root['provider_id']), type=2, platform='TV', name='Adachi Fixture', date='2020-10-01')
        return dict(total=0, offset=0, data=[])
    row = dict(id=50, name='Adachi Fixture', original_name='Adachi Fixture')
    return SimpleNamespace(_get_json=get), SimpleNamespace(
        get_tv_subject_observation_info=search or (lambda *_: ('Adachi Fixture', row)),
        get_season_info=lambda *_: dict(episodes=[dict(id=9001, episode_number=1, name='Special')]))


class CounterpartData(unittest.TestCase):
    def test_default_observation_stays_single_side(self):
        bgm, tmdb = clients()
        with patch('src.rename.special_crosswalk._counterpart', return_value=((OTHER,), 'found')) as search:
            result = observe(ROOT, 1, bgm=bgm, tmdb=tmdb)
        search.assert_not_called()
        self.assertEqual(1, len(result.to_dict()['catalog']))

    def test_explicit_fetch_empty_bgm_and_available_tmdb(self):
        bgm, tmdb = clients()
        result = observe(ROOT, 1, bgm=bgm, tmdb=tmdb, discover_counterpart=True)
        self.assertEqual([0, 1], [len(s['rows']) for s in result.to_dict()['catalog']])
        self.assertEqual('found', result.to_dict()['relation_reason'])
        self.assertTrue(result.output_candidates_available)
        self.assertEqual(OTHER, next(s['provider'] for s in result.to_dict()['catalog'] if s['provider']['namespace'] == 'tmdb'))
        self.assertEqual(result.to_dict(), ProviderSpecialObservation.from_mapping(result.to_dict()).to_dict())

    def test_discovery_failure_keeps_primary_and_reason(self):
        for reason in ('not_found', 'ambiguous', 'incomplete', 'unavailable'):
            with self.subTest(reason=reason):
                bgm, tmdb = clients()
                with patch('src.rename.special_crosswalk._counterpart', return_value=((), reason)):
                    result = observe(ROOT, 1, bgm=bgm, tmdb=tmdb, discover_counterpart=True)
                self.assertEqual(reason, result.to_dict()['relation_reason'])
                self.assertTrue(result.displayable)
                self.assertEqual(1, len(result.to_dict()['catalog']))
        bgm, tmdb = clients()
        with patch('src.rename.special_crosswalk._counterpart', side_effect=TimeoutError('fixture')):
            result = observe(ROOT, 1, bgm=bgm, tmdb=tmdb, discover_counterpart=True)
        self.assertEqual('unavailable', result.to_dict()['relation_reason'])

    def test_known_counterpart_does_not_search_again(self):
        bgm, tmdb = clients()
        with patch('src.rename.special_crosswalk._counterpart', return_value=((OTHER,), 'found')) as search:
            result = observe(ROOT, 1, bgm=bgm, tmdb=tmdb, providers=(OTHER,), discover_counterpart=True)
        search.assert_not_called()
        self.assertEqual('found', result.to_dict()['relation_reason'])
        self.assertEqual(2, len(result.to_dict()['catalog']))
        self.assertEqual(OTHER, next(s['provider'] for s in result.to_dict()['catalog'] if s['provider']['namespace'] == 'tmdb'))


class CounterpartWindow(unittest.IsolatedAsyncioTestCase):
    fixture = tv.InitialTVCompletionContracts.fixture
    create_page = gui.ContentWorkbenchGui.create_page
    text = gui.ContentWorkbenchGui.text

    async def prepared(self):
        root, service, _, result, extras, *_ = await self.fixture(anime=True, exceptional=True,
            exceptional_name='Adachi Special.mkv', companions=True)
        primary = result.plan.work_identity.primary_provider.to_dict()
        self.assertEqual('bangumi', primary['namespace'])
        bgm, tmdb = clients(primary)
        self.enterContext(patch('src.rename.provider_special_evidence.bangumi_client', return_value=bgm))
        self.enterContext(patch('src.rename.get_info.Search', return_value=tmdb))
        self.create_page(prepared=(root, service, result.plan, []))
        await self.driver.click('特别篇 / OVA')
        return service, extras, primary

    async def test_fetch_original_window_obtains_other_side(self):
        service, extras, primary = await self.prepared()
        popup = self.view._official_special_picker
        await self.driver.click('获取特别篇资料')
        self.assertIs(popup, self.view._official_special_picker)
        self.assertTrue(popup.value)
        saved = service.plan_store.load(self.view.plan.task_id)
        self.assertEqual({'bangumi', 'tmdb'}, {s['provider']['namespace'] for s in saved.metadata['provider_special_observation']['catalog']})
        self.assertEqual(primary, saved.work_identity.primary_provider.to_dict())
        self.assertEqual(OTHER, dict(next(s['provider'] for s in saved.metadata['provider_special_observation']['catalog'] if s['provider']['namespace'] == 'tmdb')))

    async def test_manual_link_confirm_save_and_reopen(self):
        service, extras, primary = await self.prepared()
        c = self.view._official_special_controls
        self.assertIn('source_link', c)
        popup = self.view._official_special_picker
        with self.client:
            c['source_link'].set_value('https://www.themoviedb.org/tv/50')
        with patch('src.rename.special_crosswalk._counterpart', return_value=((OTHER,), 'found')) as search:
            await self.driver.click('确认资料链接')
        search.assert_not_called()
        self.assertIs(popup, self.view._official_special_picker)
        key = entry_id(next(e for e in self.view.plan.entries if e.source_path == extras['exceptional']))
        with self.client:
            c['files'].set_value(key)
            c['episode'].set_value('9001')
            c['mode'].set_value('numbered')
        await self.driver.click_element(c['confirm'])
        saved = service.plan_store.load(self.view.plan.task_id)
        choice = service.decision_state_store.load(saved.task_id).provider_special_decisions[key]
        self.assertEqual('50', choice['tmdb_reference']['provider']['provider_id'])
        self.assertEqual(primary, saved.work_identity.primary_provider.to_dict())
        self.assertEqual(OTHER, dict(next(s['provider'] for s in saved.metadata['provider_special_observation']['catalog'] if s['provider']['namespace'] == 'tmdb')))
        self.assertIn('已确认', c['confirmed'].text)
        await self.driver.click('特别篇 / OVA')
        c = self.view._official_special_controls
        with self.client: c['files'].set_value(key)
        self.assertEqual('9001', c['episode'].value)
        self.assertIn('已确认', c['confirmed'].text)

    async def test_invalid_links_cannot_write_or_fetch(self):
        service, _, primary = await self.prepared()
        plan = self.view.plan
        before = PlanStore.fingerprint(plan)
        revision = self.view.task_decision.revision
        for url, reason in (('https://bgm.tv/subject/100', '请填写另一侧数据库的作品链接；当前作品保持不变'), ('https://www.themoviedb.org/movie/50', '特别篇资料来源身份无效'),
                            ('https://example.org/tv/50', '只支持 Bangumi 或 TMDB 的作品详情页链接'), ('https://www.themoviedb.org/tv/0', '特别篇资料来源身份无效')):
            with self.subTest(url=url), patch('src.rename.special_crosswalk.observe', side_effect=AssertionError('不得获取')) as fetch, patch('src.rename.special_catalog.read_source', side_effect=AssertionError('不得读取')) as read:
                with self.assertRaises((ValueError, RuntimeError)) as caught:
                    service.revise_tv_content(plan.task_id, before, expected_revision=revision,
                        mode='special_refresh', edit={'counterpart_url': url})
                self.assertEqual(reason, str(caught.exception))
                fetch.assert_not_called()
                read.assert_not_called()
            self.assertEqual(before, PlanStore.fingerprint(service.plan_store.load(plan.task_id)))

    async def test_valid_manual_backend_uses_exact_source_without_search(self):
        service, _, primary = await self.prepared()
        plan = self.view.plan
        with patch('src.rename.special_crosswalk._counterpart', return_value=((OTHER,), 'found')) as search:
            result = service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                expected_revision=self.view.task_decision.revision, mode='special_refresh',
                edit={'counterpart_url': 'https://www.themoviedb.org/tv/50'})
        search.assert_not_called()
        saved = service.plan_store.load(plan.task_id)
        self.assertEqual(PlanStore.fingerprint(result), PlanStore.fingerprint(saved))
        self.assertGreater(service.decision_state_store.load(plan.task_id).revision, self.view.task_decision.revision)
        self.assertEqual(OTHER, dict(next(s['provider'] for s in saved.metadata['provider_special_observation']['catalog'] if s['provider']['namespace'] == 'tmdb')))
        self.assertEqual(primary, saved.work_identity.primary_provider.to_dict())

    async def test_v2_snapshot_refreshes_to_v3_without_losing_primary(self):
        from src.rename.special_catalog import _read_v2, read_source
        service, _, primary = await self.prepared()
        bgm, _ = clients(primary)
        old_client = SimpleNamespace(_get_json=lambda path: dict(id=int(primary['provider_id']), platform='TV') if '/subjects/' in path else dict(total=1, offset=0, data=[dict(id=88001, subject_id=int(primary['provider_id']), type=1, sort=1, name='旧资料标题')]))
        old = _read_v2(primary, [read_source(primary, client=old_client)])
        old_rows = old.to_dict()['catalog'][0]['rows']
        self.assertEqual(1, len(old_rows))
        with patch('src.rename.special_crosswalk.observe', return_value=old):
            await self.driver.click('获取特别篇资料')
        self.assertEqual(2, self.view.plan.metadata['provider_special_observation']['schema_version'])
        await self.driver.click('获取特别篇资料')
        value = ProviderSpecialObservation.from_mapping(service.plan_store.load(self.view.plan.task_id).metadata['provider_special_observation']).to_dict()
        self.assertEqual(3, value['schema_version'])
        source = value['sources'][':'.join(str(primary[k]) for k in ('namespace', 'entity_type', 'provider_id'))]
        self.assertEqual(old_rows, source['snapshot']['rows'])
        self.assertEqual('completed_empty', source['last_attempt']['state'])
        self.assertEqual({k: primary[k] for k in ('namespace', 'entity_type', 'provider_id')}, value['provider_identity'])
        self.assertEqual({'bangumi', 'tmdb'}, {s['provider']['namespace'] for s in value['catalog']})

    async def test_descriptive_decision_survives_refresh_and_stale_inputs_remain(self):
        service, extras, _ = await self.prepared()
        await self.driver.click('获取特别篇资料')
        c = self.view._official_special_controls
        self.assertIn('source_link', c)
        key = entry_id(next(e for e in self.view.plan.entries if e.source_path == extras['exceptional']))
        with self.client:
            c['files'].set_value(key)
            c['mode'].set_value('descriptive')
            c['title'].set_value('已保存的手工标题')
        await self.driver.click_element(c['confirm'])
        saved = service.plan_store.load(self.view.plan.task_id)
        target = next(e.target_path for e in saved.entries if e.source_path == extras['exceptional'])
        decision = service.decision_state_store.load(saved.task_id).provider_special_decisions[key]
        await self.driver.click('获取特别篇资料')
        refreshed = service.plan_store.load(saved.task_id)
        self.assertEqual(target, next(e.target_path for e in refreshed.entries if e.source_path == extras['exceptional']))
        self.assertEqual(decision, service.decision_state_store.load(saved.task_id).provider_special_decisions[key])
        with self.client:
            c['source_link'].set_value('https://www.themoviedb.org/tv/50')
            c['title'].set_value('尚未保存的手工标题')
        self.assertEqual(key, c['files'].value)
        self.assertEqual('descriptive', c['mode'].value)
        selection = (c['files'].value, c['episode'].value, c['mode'].value)
        before = PlanStore.fingerprint(refreshed)
        original = service.revise_tv_content
        def stale(*args, **kwargs):
            kwargs['expected_revision'] -= 1
            return original(*args, **kwargs)
        with patch.object(service, 'revise_tv_content', side_effect=stale) as called:
            await self.driver.click_element(c['confirm'])
        called.assert_called_once()
        self.assertTrue(any('页面已变化' in text for text in self.notices), self.notices)
        self.assertEqual(selection, (c['files'].value, c['episode'].value, c['mode'].value))
        self.assertEqual(before, PlanStore.fingerprint(service.plan_store.load(saved.task_id)))
        self.assertEqual('尚未保存的手工标题', c['title'].value)
        self.assertEqual('https://www.themoviedb.org/tv/50', c['source_link'].value)
        self.assertIn('失败', c['confirmed'].text)

    async def test_delayed_fetch_and_failure_status_preserve_inputs(self):
        service, extras, primary = await self.prepared()
        c = self.view._official_special_controls
        self.assertIn('fetch_status', c)
        entered, released = threading.Event(), threading.Event()
        original = observe
        def delayed(*args, **kwargs):
            entered.set()
            if not released.wait(10): raise TimeoutError('fixture')
            return original(*args, **kwargs)
        with patch('src.rename.special_crosswalk.observe', side_effect=delayed):
            action = asyncio.create_task(self.driver.click('获取特别篇资料'))
            try:
                for _ in range(200):
                    if entered.is_set(): break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                self.assertIn('正在获取资料', c['fetch_status'].text)
                self.assertFalse(c['fetch'].enabled)
            finally: released.set()
            await action
        self.assertIn('TMDB', c['fetch_status'].text)
        self.assertNotIn('正在', c['fetch_status'].text)
        key = entry_id(next(e for e in self.view.plan.entries if e.source_path == extras['exceptional']))
        with self.client:
            c['files'].set_value(key)
            c['episode'].set_value('9001')
            c['mode'].set_value('descriptive')
            c['source_link'].set_value('https://www.themoviedb.org/tv/50')
            c['title'].set_value('保留手工标题')
        selection = (c['files'].value, c['episode'].value, c['mode'].value)
        with patch.object(service, 'revise_tv_content', side_effect=ValueError('隔离保存失败')) as called:
            await self.driver.click_element(c['confirm'])
        called.assert_called_once()
        self.assertIn('失败', c['confirmed'].text)
        self.assertEqual(selection, (c['files'].value, c['episode'].value, c['mode'].value))
        self.assertEqual('保留手工标题', c['title'].value)
        self.assertEqual('https://www.themoviedb.org/tv/50', c['source_link'].value)

    async def test_confirmation_status_changes_only_after_save(self):
        service, extras, _ = await self.prepared()
        await self.driver.click('获取特别篇资料')
        c = self.view._official_special_controls
        popup = self.view._official_special_picker
        key = entry_id(next(e for e in self.view.plan.entries if e.source_path == extras['exceptional']))
        with self.client:
            c['files'].set_value(key)
            c['episode'].set_value('9001')
            c['mode'].set_value('numbered')
        entered, released = threading.Event(), threading.Event()
        original = service.revise_tv_content
        def delayed(*args, **kwargs):
            entered.set()
            if not released.wait(10): raise TimeoutError('fixture')
            return original(*args, **kwargs)
        with patch.object(service, 'revise_tv_content', side_effect=delayed):
            action = asyncio.create_task(self.driver.click_element(c['confirm']))
            try:
                for _ in range(200):
                    if entered.is_set(): break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                self.assertEqual('正在处理…', c['confirmed'].text)
                self.assertTrue(c['confirmed'].visible)
                self.assertFalse(c['confirm'].enabled)
                self.assertNotIn(key, service.decision_state_store.load(self.view.plan.task_id).provider_special_decisions)
            finally: released.set()
            await action
        self.assertIs(popup, self.view._official_special_picker)
        self.assertIn('已确认', c['confirmed'].text)
        self.assertIn(key, service.decision_state_store.load(self.view.plan.task_id).provider_special_decisions)

    async def test_partial_failure_is_visible_and_manual_link_recovers(self):
        _, _, _ = await self.prepared()
        c = self.view._official_special_controls
        with patch('src.rename.special_crosswalk._counterpart', side_effect=TimeoutError('fixture')):
            await self.driver.click('获取特别篇资料')
        self.assertIn('Bangumi：未找到特别篇资料', c['fetch_status'].text)
        self.assertIn('TMDB：作品资料获取失败', c['fetch_status'].text)
        self.assertNotIn('已确认', c['fetch_status'].text)
        self.assertIs(c['fetch'].parent_slot.parent, c['fetch_status'].parent_slot.parent)
        self.assertIn('bar-help', c['fetch_status'].classes)
        with self.client: c['source_link'].set_value('https://www.themoviedb.org/tv/50')
        with patch('src.rename.special_crosswalk._counterpart', return_value=((), 'unavailable')) as search:
            await self.driver.click('确认资料链接')
        search.assert_not_called()
        self.assertIn('TMDB：可选 1 条资料', c['fetch_status'].text)
        self.assertNotIn('失败', c['fetch_status'].text)

    async def test_tmdb_primary_accepts_manual_bangumi_source(self):
        root, service, _, result, _, *_ = await self.fixture(anime=False, exceptional=True)
        primary = result.plan.work_identity.primary_provider.to_dict()
        bgm, tmdb = clients(ROOT)
        with patch('src.rename.provider_special_evidence.bangumi_client', return_value=bgm), \
                patch('src.rename.get_info.Search', return_value=tmdb), \
                patch('src.rename.special_crosswalk._counterpart', return_value=((), 'unavailable')) as search:
            updated = service.revise_tv_content(result.plan.task_id, PlanStore.fingerprint(result.plan),
                expected_revision=result.state.revision, mode='special_refresh',
                edit={'counterpart_url': 'https://bgm.tv/subject/100'})
        search.assert_not_called()
        self.assertEqual(primary, updated.work_identity.primary_provider.to_dict())
        self.assertEqual(result.plan.metadata['season_number'], updated.metadata['season_number'])
        self.assertEqual(ROOT, dict(next(s['provider'] for s in updated.metadata['provider_special_observation']['catalog'] if s['provider']['namespace'] == 'bangumi')))
