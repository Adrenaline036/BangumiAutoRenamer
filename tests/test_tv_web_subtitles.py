"""WEB平台证据与已确认作品的局部字幕修订。"""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.rename.provider_search import BangumiProviderClient
from src.rename.provider_link_resolver import _is_tv_subject
from src.rename.season_reference import _tmdb_references, match_component, tmdb_snapshot
from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import SubtitleOverrideScope
from src.rename.projection_authority import validate_plan_projection_receipt
from src.rename.series_registration import validate_plan_series_registration
from tests import test_season_reference as references
from tests import test_episode_versions_contract as versions
from tests.test_partition_content_contract import records


class WebEvidence(unittest.TestCase):
    def test_web_snapshot_and_resolver_accept_same_platform(self):
        for platform in ('TV', 'WEB', 'web', 'ＷＥＢ'):
            with self.subTest(platform=platform):
                value = BangumiProviderClient._crosswalk_subject_snapshot(dict(
                    id=101437, type=2, name='Example', date='2013-01-01', eps=12, platform=platform))
                self.assertIsNotNone(value)
                self.assertTrue(_is_tv_subject(value))
        for platform in ('Movie', 'OVA', '剧场版'):
            with self.subTest(platform=platform):
                self.assertIsNone(BangumiProviderClient._crosswalk_subject_snapshot(
                    dict(id=9, type=2, platform=platform)))
                self.assertFalse(_is_tv_subject(dict(platform=platform)))
        self.assertIsNone(BangumiProviderClient._crosswalk_subject_snapshot(dict(id=9, type=1, platform='WEB')))

    def test_web_season_references_use_existing_complete_match(self):
        subjects, relations, info = references.evidence()
        subjects = tuple({**row, 'platform':'WEB'} for row in subjects)
        bgm = SimpleNamespace(search_animation=lambda *_a, **_k: [subjects[0]],
            load_crosswalk_evidence=lambda *_a, **_k: (subjects, relations))
        result = _tmdb_references({'provider_id':'50'}, bgm,
            SimpleNamespace(get_tv_info_by_id=lambda _id: ('Example', info)))
        self.assertEqual({1:'Example', 2:'Example Season 2'}, result.names_by_season())
        unrelated = ({'from':'100', 'to':'101', 'relation':'不同演绎'},)
        self.assertEqual((), match_component(subjects, unrelated, tmdb_snapshot(info)))

    def test_client_does_not_traverse_alternate_adaptation(self):
        client = BangumiProviderClient(user_agent='BAR-test/1.0')
        calls = []
        def get(endpoint):
            calls.append(endpoint)
            if endpoint == '/v0/subjects/101437':
                return dict(id=101437, type=2, platform='WEB', name='Example', date='2013-01-01', eps=12)
            if endpoint == '/v0/subjects/101437/subjects':
                return [dict(id=175137, relation='不同演绎')]
            raise AssertionError(endpoint)
        with patch.object(client, '_get_json', side_effect=get):
            subjects, relations = client.load_crosswalk_evidence('101437')
        self.assertEqual(['101437'], [s['provider_id'] for s in subjects])
        self.assertEqual((), relations)
        self.assertEqual(2, len(calls))


class ConfirmedSubtitle(unittest.IsolatedAsyncioTestCase):
    def change(self, flow, plan, language='zh'):
        subtitle = next(e for e in plan.entries if e.media_kind == 'subtitle')
        video = next(e for e in plan.entries if e.media_kind == 'video' and '1920x1080' in e.source_path.name)
        return flow.service.apply_subtitle_override_rule(plan.task_id, PlanStore.fingerprint(plan),
            entry_id(subtitle), scope=SubtitleOverrideScope.FILE, associated_video_source_path=video.source_path,
            primary_language=language, script=None, reason='确认中文及16x9版本归属')

    async def prepared(self, f, flow):
        plan, state = await f.selected(flow)
        self.assertTrue(state.confirmed_work, flow.manager._read(flow.task_id).get('error'))
        return f.save(flow, plan)

    async def test_confirmed_anime_subtitle_saves_and_reloads_new_receipt(self):
        f = versions.EpisodeVersionsContracts('runTest')
        async with f.flow(automatic=False, is_anime=True, configure_source=versions.package) as flow:
            plan = await self.prepared(f, flow)
            validate_plan_series_registration(plan)
            revised = self.change(flow, plan)
            self.assertNotEqual(plan.projection_receipt.fingerprint, revised.projection_receipt.fingerprint)
            loaded = flow.store.load(plan.task_id)
            validate_plan_projection_receipt(loaded)
            validate_plan_series_registration(loaded)
            self.assertEqual([], list(PlanVerifier().verify(loaded).issues))
            self.assertEqual(plan.work_identity, loaded.work_identity)
            self.assertEqual(plan.metadata['episode_versions'], loaded.metadata['episode_versions'])
            subtitle = next(e for e in loaded.entries if e.media_kind == 'subtitle')
            self.assertTrue(subtitle.target_path.name.endswith('16x9.zh.srt'))
            second = self.change(flow, loaded, 'ja')
            validate_plan_series_registration(flow.store.load(second.task_id))
            self.assertNotEqual(loaded.projection_receipt.fingerprint, second.projection_receipt.fingerprint)
            before = records(flow.root)
            with self.assertRaises((ValueError, RuntimeError)):
                self.change(flow, plan)
            self.assertEqual(before, records(flow.root))

    async def test_bad_parent_and_changed_content_cannot_save(self):
        from src.rename import subtitle_batch, series_registration
        f = versions.EpisodeVersionsContracts('runTest')
        async with f.flow(automatic=False, is_anime=True, configure_source=versions.package) as flow:
            plan = await self.prepared(f, flow)
            before = records(flow.root)
            no_flag = dict(plan.metadata)
            no_flag.pop('projection_authority_required', None)
            for bad in (replace(plan, projection_receipt=None),
                    replace(plan, projection_receipt=None, metadata=no_flag),
                    replace(plan, metadata={**plan.metadata, 'decision_state_revision':999}),
                    replace(plan, metadata={**plan.metadata, 'series_registration_authority':{}})):
                with self.subTest(metadata=bad.metadata.get('decision_state_revision')):
                    self.assertTrue(PlanVerifier().verify(bad).issues)
                    with self.assertRaises((ValueError, RuntimeError)):
                        subtitle_batch.verify_edit(bad, replace(bad, projection_receipt=None))
            original = subtitle_batch.verify_edit
            for mutate in (
                    lambda draft: replace(draft, metadata={**draft.metadata, 'season_number':9}),
                    lambda draft: replace(draft, work_identity=replace(draft.work_identity, title='Other work')),
                    lambda draft: replace(draft, entries=tuple(replace(e, episode_id=9)
                        if e.media_kind == 'video' else e for e in draft.entries))):
                def changed(current, draft):
                    return original(current, mutate(draft))
                with patch.object(subtitle_batch, 'verify_edit', side_effect=changed):
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.change(flow, plan)
                self.assertEqual(before, records(flow.root))
            with patch.object(series_registration, '_validate_series_registration_contents', return_value=None):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.change(flow, plan)
            self.assertEqual(before, records(flow.root))
