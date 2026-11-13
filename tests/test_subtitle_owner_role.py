"""字幕角色随冻结的视频用途投影，保留关联与核查边界。"""
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from src.rename.content_associations import observe_content_associations, project_subtitles, source_key
from src.rename.rename_plan import AssetRole, PlanEntry, PresentationAssetPlanContract, RenamePlan
from src.rename.plan_verifier import PlanVerifier
from src.rename.subtitle_workflow import build_subtitle_decisions, decision_to_plan_contract


class SubtitleOwnerRole(unittest.TestCase):
    def fixture(self, kind='extra', *, label='en', group='movie', companion=True, duplicate=False):
        root=Path('C:/isolated-subtitle-role'); video=root/'Film.mkv'
        target=Path('C:/isolated-subtitle-output/Extras/Clip.mkv')
        subtitle=root/f'Film.{label}.ass'
        owner=PlanEntry.from_paths(video,target)
        owner=replace(owner,presentation_asset=PresentationAssetPlanContract(
            group,AssetRole(kind,'other' if kind=='extra' else None),None,None))
        videos=(owner,replace(owner,source_path=video.with_suffix('.mp4'))) if duplicate else (owner,)
        mapping=tuple((e.source_path,e.target_path) for e in videos) if companion else ()
        decision=build_subtitle_decisions((subtitle,),mapping)[0]
        entry=replace(PlanEntry.from_paths(subtitle,target.with_suffix(f'.{label}.ass')),
            role='subtitle',subtitle=decision_to_plan_contract(decision),requires_review=True)
        entries=(*videos,entry) if companion else (entry,)
        links=observe_content_associations(entries,task_id='role',source_root=root)
        return RenamePlan('role',root,entries),links

    def check(self, plan):
        entry=plan.entries[-1];issues=[]
        videos={}
        for video in plan.entries:
            if video.media_kind=='video':videos.setdefault(source_key(video.source_path),[]).append(video)
        PlanVerifier()._verify_subtitle_entry(entry,entry.subtitle,videos,{},set(),{'movie'},issues)
        return {issue.code for issue in issues}

    def project(self, plan, links):
        with patch.object(Path,'open',side_effect=AssertionError('不能读取源内容')), \
                patch.object(Path,'stat',side_effect=AssertionError('不能查询源属性')), \
                patch.object(Path,'iterdir',side_effect=AssertionError('不能扫描源目录')):
            return project_subtitles(plan,links)

    def test_confirmed_extra_role_and_normal_control(self):
        for kind,role in (('extra','extra_subtitle'),('content_version','subtitle')):
            with self.subTest(kind=kind):
                plan,links=self.fixture(kind)
                if kind=='extra':
                    self.assertIn('subtitle_video_not_content_version',self.check(plan))
                projected=self.project(plan,links)
                self.assertEqual(role,projected.entries[-1].role)
                self.assertEqual(plan.entries[-1].subtitle,projected.entries[-1].subtitle)
                self.assertEqual(plan.entries[-1].requires_review,projected.entries[-1].requires_review)
                self.assertFalse(self.check(projected))

    def test_manual_language_and_frozen_annotations_survive(self):
        plan,_=self.fixture()
        entry=plan.entries[-1]
        contract=replace(entry.subtitle,decision_source='manual',manual_reason='人工确认',
            primary_language='ja',track_title_tokens=('手工字幕',),applied_rule_id='manual-rule',
            applied_rule_fingerprint='frozen-rule',applied_rule_scope='task',
            applied_rule_engine_version='1',association_candidates=(plan.entries[0].source_path,))
        plan=replace(plan,entries=(*plan.entries[:-1],replace(entry,subtitle=contract)))
        links=observe_content_associations(plan.entries,task_id='role',source_root=plan.source_root,
            choices={entry.source_path:plan.entries[0].source_path})
        projected=self.project(plan,links)
        self.assertEqual(contract,projected.entries[-1].subtitle)
        self.assertEqual(entry.requires_review,projected.entries[-1].requires_review)
        self.assertEqual('extra_subtitle',projected.entries[-1].role)
        # 未绑定的规则哨兵保持原样，不能因角色修正而获得有效凭据。
        self.assertEqual({'subtitle_rule_provenance_unexpected'},self.check(projected))

    def test_former_extra_role_does_not_survive_owner_becoming_main(self):
        plan,links=self.fixture('content_version')
        plan=replace(plan,entries=(*plan.entries[:-1],replace(plan.entries[-1],role='extra_subtitle')))
        projected=self.project(plan,links)
        self.assertEqual('subtitle',projected.entries[-1].role)
        self.assertFalse(self.check(projected))

    def test_unknown_video_and_wrong_group_still_fail_verification(self):
        for kind,group in (('unknown','movie'),('extra','missing-group')):
            with self.subTest(kind=kind,group=group):
                plan,links=self.fixture(kind,group=group)
                projected=self.project(plan,links)
                self.assertIn('subtitle_video_not_content_version',self.check(projected))

    def test_missing_and_ambiguous_video_are_not_resolved_by_role(self):
        for companion,duplicate in ((False,False),(True,True)):
            with self.subTest(companion=companion,duplicate=duplicate):
                plan,links=self.fixture(companion=companion,duplicate=duplicate)
                projected=self.project(plan,links)
                self.assertIsNone(projected.entries[-1].subtitle.associated_video_source_path)
                self.assertEqual('subtitle',projected.entries[-1].role)
                self.assertIn('subtitle_association_unresolved',self.check(projected))
                self.assertIn('subtitle_video_entry_not_unique',self.check(projected))

    def test_unknown_language_remains_blocked_for_extra_video(self):
        plan,links=self.fixture(label='mystery')
        projected=self.project(plan,links)
        self.assertEqual(plan.entries[-1].subtitle.parse_state,projected.entries[-1].subtitle.parse_state)
        self.assertIn('subtitle_parse_unresolved',self.check(projected))


if __name__=='__main__':
    unittest.main()
