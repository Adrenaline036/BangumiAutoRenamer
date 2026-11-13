"""独立OVA的人工季度、冻结证据和EP/SP投影合同。"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from src.rename.special_catalog import read_source, build_observation, plain, bangumi_episode_type
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from tests.test_special_selection_contract import SpecialSelectionContracts
from tests.test_ova_manual import OvaReferences, ova_subject


class OvaSafety(unittest.TestCase):
    def test_manual_season_is_only_source_and_old_observation_roundtrips(self):
        from src.rename.ova_selection import OvaSubject
        from src.rename.tv_completion_observations import TVSeasonObservation
        from src.rename.season_identity import resolve_initial_tv_season
        subject = OvaSubject.capture(ova_subject())
        provider = dict(namespace='bangumi', entity_type='subject', provider_id=subject.provider_id)
        work = SimpleNamespace(primary_provider=SimpleNamespace(to_dict=lambda:provider))
        observation = TVSeasonObservation.capture(work, subject.info())
        self.assertEqual(observation, TVSeasonObservation.from_mapping(observation.to_dict()))
        with self.assertRaises(ValueError):
            resolve_initial_tv_season(observation, work, Path('Show Season 03'), None, None)
        for number in (0, 3, 7):
            self.assertEqual((number, 'explicit_user'), resolve_initial_tv_season(observation, work, Path('Show Season 03'), number, None))
        with self.assertRaises(ValueError):
            TVSeasonObservation(observation.provider, (3,), observation.ova_subject)
        old = dict(provider=list(observation.provider), available=[1])
        self.assertEqual(old, TVSeasonObservation.from_mapping(old).to_dict())

    def test_ova_row_rejects_wrong_type_subject_or_partial_proof(self):
        source = OvaReferences().source('OVA')
        row = source['rows'][0]
        self.assertEqual(0, bangumi_episode_type(source['provider'], row))
        for key, value in (('episode_type', True), ('episode_type', 2), ('ova_subject', {**row['ova_subject'], 'id': 999}), ('ova_subject', {**row['ova_subject'], 'platform': 'TV'})):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                bangumi_episode_type(source['provider'], {**row, key:value})
        with self.assertRaises(ValueError):
            bangumi_episode_type(source['provider'], {k:v for k,v in row.items() if k != 'ova_subject'})
        ordinary = OvaReferences().source('TV')['rows'][0]
        self.assertNotIn('episode_type', ordinary)
        self.assertEqual(1, bangumi_episode_type(source['provider'], ordinary))


class OvaOutputs(unittest.IsolatedAsyncioTestCase):
    fixture = SpecialSelectionContracts.fixture
    revise = SpecialSelectionContracts.revise

    async def test_ep_sp_keep_distinct_ids_local_numbers_subtitles_and_outputs(self):
        from src.rename.plan_verifier import PlanVerifier
        from src.rename.generated_outputs import approved_outputs, register_outputs, validate_completed
        from src.rename.trans import Trans
        from src.rename.projection_authority import validate_plan_projection_receipt
        import json
        root, service, before, result, *_ = await self.fixture(anime=True)
        provider = result.plan.work_identity.primary_provider.to_dict()
        external = dict(namespace='bangumi', entity_type='subject', provider_id='111081')
        subject = dict(id=111081, type=2, platform='OVA', name='OVA', name_cn='独立OVA', date='2015-06-19')
        rows = [dict(id=484739, subject_id=111081, type=0, sort=1, name_cn='正篇'), dict(id=1169014, subject_id=111081, type=1, sort=1, name_cn='总集篇')]
        source = read_source(external, client=SimpleNamespace(_get_json=lambda p:subject if p.startswith('/v0/subjects/') else dict(total=2, offset=0, data=rows)))
        empty = read_source(provider, client=SimpleNamespace(_get_json=lambda _:dict(total=0, offset=0, data=[])))
        observed = build_observation(provider, [empty, source])
        with patch('src.rename.special_crosswalk.observe', return_value=observed):
            plan = self.revise(service, result.plan, 'special_refresh', {})
        videos = [e for e in plan.entries if e.media_kind == 'video']
        for index, (video, row) in enumerate(zip(videos, rows), start=7):
            plan = self.revise(service, plan, 'special', dict(entry_id=entry_id(video), action='bangumi_special', bangumi_key=f'111081:{row["id"]}', title=row['name_cn'], local_episode=index))
        validate_plan_projection_receipt(plan)
        self.assertFalse(PlanVerifier().verify(plan).issues)
        video_rows = [e for e in plan.entries if e.media_kind == 'video']
        for video, row, prefix, number in zip(video_rows, rows, ('EP', 'SP'), (7, 8)):
            self.assertEqual((0, number), (video.season_id, video.episode_id))
            self.assertIn(f' - {prefix}01 - ', video.target_path.name)
            subtitle = next(e for e in plan.entries if e.media_kind == 'subtitle' and e.source_path.stem.rsplit('.', 1)[0] == video.source_path.stem)
            self.assertTrue(subtitle.target_path.name.startswith(video.target_path.stem + '.'))
            nfo = next(x for x in plan.metadata['generated_outputs']['files'] if x['target'] == str(video.target_path.with_suffix('.nfo')))
            self.assertIn(f'<uniqueid type="Bangumi">{row["id"]}</uniqueid>', nfo['content'])
        # 目标已有不同来源，换源闭包必须包含两个特别篇及其字幕。
        for video in video_rows:
            video.target_path.parent.mkdir(parents=True, exist_ok=True)
            video.target_path.write_bytes(b'previous isolated video')
        operation, draft = service.asset_actions().prepare_replacement(plan)
        value = operation.to_dict()
        self.assertEqual({str(v.target_path) for v in video_rows}, {r['target'] for r in value['season_directory']['special_files']})
        selected = service.confirm_asset_replacement(plan.task_id, PlanStore.fingerprint(plan), expected_operation_fingerprint=operation.fingerprint)
        from tests import _media_flow_helpers as execution_fixture
        harness = execution_fixture.MediaFlow('runTest')
        task, ledger, execution = await harness._approve_and_execute(root=root, result=SimpleNamespace(plan=selected))
        self.assertEqual('completed', task.get('execution_status'), task.get('error'))
        self.assertEqual('completed', ledger['status'])
        plan = service.plan_store.load(plan.task_id)
        self.assertEqual('series', plan.metadata['catalog_binding_snapshot']['backend'])
        self.assertTrue(list((root/'series_catalog').rglob('*.json')))
        validate_completed(plan, ledger)
        registration = dict(canonical_root=plan.metadata['canonical_work_root'])
        self.assertTrue(register_outputs(registration, plan, ledger))
        self.assertEqual({7,8}, {r['local_episode'] for r in registration['generated_outputs']})
        for video in video_rows:
            self.assertTrue(video.target_path.samefile(video.source_path))
        (root/'ova-output-proof.json').write_text(json.dumps(dict(plan=plan.to_dict(),registration=registration), ensure_ascii=False), encoding='utf-8')


class OvaSeasonZero(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_zero_uses_local_special_season(self):
        from tests.test_ova_manual import OvaSelection
        from tests import test_r1_real_media_production_red_contract as f
        case = OvaSelection('runTest')
        self.addCleanup(case.doCleanups)
        with patch.dict(f.YAMA_SUBJECTS, {'41658': ova_subject()}):
            _, _, _, result, *_ = await case.fixture(anime=True, selected_season=0)
        case.assert_ova_completed(result, 0)
