"""Frozen identity controls, no transport or filesystem observations."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from src.rename import provider_special_evidence as pe
from src.rename.rename_plan import RenamePlan, PlanEntry, WorkIdentity, ProviderIdentity, AssetRole, PresentationAssetPlanContract
from src.rename.special_catalog import build_observation, read_source
from src.rename.season_reference import _reference, tmdb_snapshot, match_component
from tests.test_season_reference import evidence

class SpecialIdentityControls(unittest.TestCase):
    def fixture(self, bangumi):
        subjects, relations, info=evidence()
        snapshot=tmdb_snapshot(info)
        reference=_reference(subjects,relations,snapshot,match_component(subjects,relations,snapshot))
        bgm=dict(namespace='bangumi',entity_type='subject',provider_id='100')
        tmdb=dict(namespace='tmdb',entity_type='tv',provider_id='50')
        bsource=dict(provider=bgm,title='Example',complete=True,state='completed_empty',reason='not_found',rows=[],issues=[])
        tsource=read_source(tmdb,client=SimpleNamespace(get_season_info=lambda *_:dict(episodes=[dict(id=9001,episode_number=1,name='OVA')])))
        root=bgm if bangumi else tmdb
        frozen=build_observation(root,[bsource,tsource],relation=reference.frozen_relation(),relation_reason='found')
        primary=ProviderIdentity('confirmed',root['namespace'],root['entity_type'],root['provider_id'],'Example',2013,'explicit_user')
        entry=PlanEntry(source_path=Path('source/Example E13.mkv'),target_path=Path('library/Example/Season 01/Example S01E13.mkv'),
            season_id=1,episode_id=13,media_kind='video',presentation_asset=PresentationAssetPlanContract(
                presentation_group='tv',asset_role=AssetRole('content_version'),segment=None,version_descriptor=None))
        plan=RenamePlan(task_id='identity-control',source_root=Path('source'),entries=(entry,),expected_sources=(entry.source_path,),
            work_identity=WorkIdentity('confirmed','animation','Example',2013,primary),metadata={'canonical_work_root':'library/Example'})
        resolution=SimpleNamespace(tmdb_tv_id='50',season_assignments=(('100',1),),derived_episode_ranges=(
            dict(bangumi_subject_id='100',tmdb_tv_id='50',season_number=1,episode_start=1,episode_end=12),))
        return plan,frozen,resolution

    def test_both_primary_namespaces_use_same_confirmed_tmdb_range(self):
        for bangumi in (True,False):
            with self.subTest(bangumi=bangumi):
                plan,frozen,resolution=self.fixture(bangumi)
                result=pe.project_provider_special_evidence(plan,frozen,selected_subject_id='100',provider_link_resolution=resolution)
                self.assertEqual([13],[row['projected_episode'] for row in result.metadata['provider_regular_episode_overflows']])
                self.assertTrue(result.metadata['provider_special_candidates'])

    def test_missing_or_wrong_range_identity_never_creates_overflow_mapping(self):
        for change in ('missing_authority','wrong_range','wrong_subject','wrong_season'):
            with self.subTest(change=change):
                plan,frozen,resolution=self.fixture(True)
                row=dict(resolution.derived_episode_ranges[0])
                if change=='missing_authority':resolution.tmdb_tv_id=''
                if change=='wrong_range':row['tmdb_tv_id']='999'
                if change=='wrong_subject':row['bangumi_subject_id']='101'
                if change=='wrong_season':row['season_number']=2
                resolution.derived_episode_ranges=(row,)
                result=pe.project_provider_special_evidence(plan,frozen,selected_subject_id='100',provider_link_resolution=resolution)
                self.assertFalse(result.metadata.get('provider_regular_episode_overflows'))
                self.assertFalse(result.metadata.get('provider_special_candidates'))
                self.assertEqual(plan.entries,result.entries)
