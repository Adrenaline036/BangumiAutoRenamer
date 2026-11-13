"""TMDB 标题回退、Bangumi 控制与实际链接到计划流程。"""
import asyncio
import json
import unittest
from contextlib import nullcontext
from unittest.mock import patch
import pytest
from src.rename.canonical_naming import (
    CanonicalNamingDecision, CanonicalNamingError, TitleAuthorityConflict,
    TitleEvidencePending, resolve_title_authority,
)


def selected(kind, title, original):
    return dict(namespace='tmdb', entity_type=kind, provider_id='8181' if kind == 'movie' else '4242',
                localized_title=title, original_title=original)


@pytest.mark.parametrize('kind', ['tv', 'movie'])
@pytest.mark.parametrize('title,original,expected,source', [
    ('中文标题', 'Original Title', '中文标题', 'tmdb:zh-CN'),
    ('CLANNAD', 'CLANNAD', 'CLANNAD', 'tmdb:original'),
    ('同名标题', '同名标题', '同名标题', 'tmdb:original'),
    ('', 'CLANNAD', 'CLANNAD', 'tmdb:original'),
    ('  ', 'CLANNAD', 'CLANNAD', 'tmdb:original'),
    ('中文标题', '', '中文标题', 'tmdb:zh-CN'),
])
def test_tmdb_title_priority(kind, title, original, expected, source):
    decision = resolve_title_authority(selection=selected(kind, title, original))
    assert decision.display_title == expected
    assert decision.title_source == source
    assert decision.primary_provider_identity == {k: selected(kind, title, original)[k]
                                                for k in ('namespace', 'entity_type', 'provider_id')}
    restored = CanonicalNamingDecision.from_dict(json.loads(json.dumps(decision.to_dict())))
    assert restored == decision
    with pytest.raises(CanonicalNamingError, match='fingerprint'):
        CanonicalNamingDecision.from_dict({**decision.to_dict(), 'display_title': '另一个标题'})


@pytest.mark.parametrize('kind', ['tv', 'movie'])
def test_empty_title_and_identity_conflict_still_block(kind):
    with pytest.raises(TitleEvidencePending, match='缺少可用标题'):
        resolve_title_authority(selection=selected(kind, '', ''))
    identity = dict(namespace='tmdb', entity_type=kind, provider_id='9999')
    with pytest.raises(TitleAuthorityConflict) as error:
        resolve_title_authority(selection=selected(kind, 'CLANNAD', 'CLANNAD'),
                                primary_provider_identity=identity)
    assert not isinstance(error.value, TitleEvidencePending)


@pytest.mark.parametrize('title,original,expected,source', [
    ('中文短名', 'Original', '中文短名', 'tmdb:zh-CN'),
    ('CLANNAD', 'CLANNAD', 'CLANNAD 〜AFTER STORY〜', 'bangumi:name_cn'),
    ('', '', 'CLANNAD 〜AFTER STORY〜', 'bangumi:name_cn'),
])
def test_bangumi_title_flow_unchanged(title, original, expected, source):
    bgm = dict(namespace='bangumi', entity_type='subject', provider_id='9001',
               localized_title='CLANNAD 〜AFTER STORY〜', original_title='CLANNAD 〜AFTER STORY〜')
    tmdb = selected('tv', title, original)
    decision = resolve_title_authority(selection=bgm, mapped_candidates=(tmdb,),
                                       primary_provider_identity=tmdb)
    assert (decision.display_title, decision.title_source) == (expected, source)


class LinkPlanFlow(unittest.IsolatedAsyncioTestCase):
    async def run_link(self, *, movie, title, original, disconnect=False):
        from tests import test_planning_lifecycle_controls as lifecycle
        from tests import test_initial_auto_link_contract as initial
        from src.pages import plan_review_page as page
        from src.rename import process
        from src.rename.provider_search import resolve_provider_item_url
        helper = lifecycle.PlanningLifecycleControls('runTest')
        payload = initial.detail(movie=movie)
        payload['title' if movie else 'name'] = title
        payload['original_title' if movie else 'original_name'] = original
        if not movie:
            payload['seasons'] = [dict(season_number=1, name='Season 1', episode_count=2)]
        def configure(worker):
            lookup = lambda *args: (title or original, dict(payload))
            if movie:
                worker.search.get_movie_info = lookup
                worker.search.get_movie_info_by_id = lookup
            else:
                worker.search.get_tv_info = lookup
                worker.search.get_tv_info_by_id = lookup
        async with helper.flow(movie=movie, automatic=False, admission_only=True,
                               admit=helper.admit_auto, configure_worker=configure) as f:
            view = helper.dialog(f)
            url = 'https://www.themoviedb.org/' + ('movie/8181' if movie else 'tv/4242')
            view.provider_url_input.value = url
            f.manager.executor = f.manager._execute_rename
            before_sources = {p: p.read_bytes() for p in f.source.rglob('*') if p.is_file()}
            session = initial._Session(payload)
            async def reader(value):
                return resolve_provider_item_url(value, tmdb_api_key='fixture-only',
                    bangumi_user_agent='BAR/test', session=session)
            resolver = patch.object(process, 'resolve_title_authority',
                side_effect=CanonicalNamingError('标题解析已断开')) if disconnect else nullcontext()
            try:
                with patch.object(page, '_read_manual_provider_url', side_effect=reader), resolver:
                    with view.client:
                        await view._select_manual_provider_url()
                        request = f.manager._provider_reads[f.task_id]
                    await asyncio.wait_for(request.worker, 20)
                    await asyncio.wait_for(f.manager._queue.join(), 5)
                plan = f.store.load(f.task_id)
                task = f.manager._read(f.task_id)
                state = f.service.decision_state_store.load(f.task_id)
                if disconnect:
                    self.assertIn('标题解析已断开', request.error)
                    self.assertFalse(plan.metadata.get('canonical_naming_decision'))
                    self.assertFalse(state.confirmed_work)
                else:
                    self.assertFalse(request.error, request.error)
                    self.assertEqual(title or original, state.confirmed_work['title'])
                    if not movie:
                        naming = plan.metadata['canonical_naming_decision']
                        self.assertEqual(title or original, naming['display_title'])
                        self.assertEqual('tmdb:original', naming['title_source'])
                        self.assertEqual(plan.to_dict()['metadata']['canonical_naming_decision'],
                                         CanonicalNamingDecision.from_dict(naming).to_dict())
                    else:
                        # 电影链接先保存作品身份，正片选择后才生成最终命名计划。
                        self.assertEqual(title or original, plan.work_identity.title)
                    self.assertEqual('8181' if movie else '4242', state.confirmed_work['primary_provider']['provider_id'])
                    self.assertEqual(f.task_id, plan.task_id)
                    self.assertIn(task['workflow_status'], {'need_confirm', 'invalid'})
                    self.assertFalse(task.get('error'), task.get('error'))
                    self.assertEqual(url, task['provider_selection']['source_url'])
                self.assertEqual(before_sources, {p: p.read_bytes() for p in f.source.rglob('*') if p.is_file()})
                self.assertFalse(any(p.is_file() for name in ('TV', 'Movies', 'Anime', 'AnimeMovies')
                                     for p in (f.root / name).rglob('*')))
                self.assertFalse(any(p.is_file() and not (p.parent.name == '.locks' and p.suffix == '.lock')
                    for name in ('records', 'work_catalog', 'series_catalog', 'ledger')
                    for p in (f.root / name).rglob('*')))
            finally:
                view.delete()

    async def test_same_title_link_reaches_saved_plan_tv_and_movie(self):
        for movie in (False, True):
            with self.subTest(movie=movie):
                await self.run_link(movie=movie, title='CLANNAD', original='CLANNAD')

    async def test_original_only_link_reaches_saved_plan_tv_and_movie(self):
        for movie in (False, True):
            with self.subTest(movie=movie):
                await self.run_link(movie=movie, title='', original='CLANNAD')

    async def test_disconnected_resolver_cannot_publish_tv_title(self):
        await self.run_link(movie=False, title='CLANNAD', original='CLANNAD', disconnect=True)


def test_existing_v2_decision_keeps_exact_fingerprint():
    frozen = {'display_title': '中文标题', 'title_source': 'tmdb:zh-CN', 'aliases': ['Original Title'], 'original_title': 'Original Title', 'primary_provider_identity': {'namespace': 'tmdb', 'entity_type': 'tv', 'provider_id': '4242'}, 'evidence': ['selection:tmdb/tv/4242', 'primary:tmdb/tv/4242'], 'title_policy_version': 'bar-title-authority-v2', 'leaf_policy_version': 'bar-windows-portable-leaf-v1', 'decision_fingerprint': '2d75468de79fc282f356d0ea225599aa6897f930060a69c5495b9e4c235e023f'}
    assert CanonicalNamingDecision.from_dict(frozen).to_dict() == frozen
    assert resolve_title_authority(selection=selected('tv', '中文标题', 'Original Title')).to_dict() == frozen


@pytest.mark.parametrize('kind', ['tv', 'movie'])
def test_production_title_adapter_uses_shared_resolver(kind):
    from src.rename import process
    identity = selected(kind, 'CLANNAD', 'CLANNAD')
    info = dict(id=int(identity['provider_id']), **({'title': 'CLANNAD', 'original_title': 'CLANNAD'}
                if kind == 'movie' else {'name': 'CLANNAD', 'original_name': 'CLANNAD'}))
    kwargs = dict(name='CLANNAD', info=info, entity_type=kind, provider_selection=identity)
    decision = process._resolve_plan_naming_decision(**kwargs)
    assert decision.display_title == 'CLANNAD'
    assert decision.title_source == 'tmdb:original'
    with patch.object(process, 'resolve_title_authority', side_effect=CanonicalNamingError('resolver disconnected')):
        with pytest.raises(CanonicalNamingError, match='resolver disconnected'):
            process._resolve_plan_naming_decision(**kwargs)
