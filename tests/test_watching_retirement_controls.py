"""退役后的媒体配套、批准边界和来源读取定向控制。"""
import builtins
from contextlib import contextmanager
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from src.rename.content_associations import observe_content_associations, subtitle_pair_map, subtitle_track_keys, associated_source_closure
from src.rename.rename_plan import RenamePlan, PlanEntry
from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from tests.test_initial_tv_completion_contract import InitialTVCompletionContracts

@contextmanager
def source_reads(root):
    counts = dict(open=0, stat=0, enumerate=0)
    root = os.path.normcase(os.path.abspath(root)) + os.sep
    def wrap(fn, kind):
        def call(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                value = os.path.normcase(os.path.abspath(os.fsdecode(path)))
                if value.startswith(root) or value == root.rstrip(os.sep):
                    counts[kind] += 1
            return fn(path, *args, **kwargs)
        return call
    with patch('builtins.open', wrap(builtins.open, 'open')), patch('io.open', wrap(io.open, 'open')), \
            patch('os.stat', wrap(os.stat, 'stat')), patch('os.lstat', wrap(os.lstat, 'stat')), \
            patch('os.scandir', wrap(os.scandir, 'enumerate')):
        yield counts

class WatchingRetirementControls(unittest.IsolatedAsyncioTestCase):
    def test_idx_sub_is_one_track_only_for_matching_owner_and_semantics(self):
        root=Path('C:/isolated-pair'); idx=root/'Film.en.idx'; sub=root/'Film.en.sub'; owner=root/'Film.mkv'
        self.assertEqual({idx:sub,sub:idx},subtitle_pair_map([idx,sub]))
        valid=subtitle_track_keys([(idx,owner,('en','default')),(sub,owner,('en','default'))])
        self.assertEqual(1,len(set(valid.values())))
        for other_owner,other_semantics in ((None,('en','default')),(root/'Other.mkv',('en','default')),(owner,('ja',))):
            with self.subTest(owner=str(other_owner),semantics=other_semantics):
                keys=subtitle_track_keys([(idx,owner,('en','default')),(sub,other_owner,other_semantics)])
                self.assertEqual(2,len(set(keys.values())))
        duplicate=root/'Film.en.SUB'
        self.assertEqual({},subtitle_pair_map([idx,sub,duplicate]))

    def test_incomplete_pairs_stay_unresolved_and_closure_keeps_pair(self):
        root=Path('C:/isolated-pair'); video=root/'Film.mkv'; idx=root/'Film.en.idx'; sub=root/'Film.en.sub'
        def observe(paths):
            return observe_content_associations(tuple(PlanEntry.from_paths(p,p) for p in paths), task_id='pair',source_root=root)
        single=observe([video,idx]).to_dict()['relations'][0]
        self.assertEqual('unresolved',single['state'])
        pair=observe([video,idx,sub])
        self.assertEqual({idx,sub},set(associated_source_closure(pair,[idx])))
        self.assertEqual({video,idx,sub},set(associated_source_closure(pair,[video])))

    def test_old_plan_schema_is_not_migrated(self):
        plan=RenamePlan('schema',Path('C:/isolated'),())
        for version in range(plan.to_dict()['schema_version']):
            raw=plan.to_dict();raw['schema_version']=version
            with self.subTest(version=version), self.assertRaises(ValueError):RenamePlan.from_dict(raw)
        self.assertEqual(plan,RenamePlan.from_dict(plan.to_dict()))

    async def test_pair_batch_special_and_exclusion_use_frozen_inventory(self):
        h=InitialTVCompletionContracts('runTest');self.addCleanup(h.doCleanups)
        def seed(source,extras):
            video=next(source.glob('*.mkv'))
            for suffix in ('.en.idx','.en.sub','.flac'):
                path=video.with_suffix(suffix);path.write_bytes(b'isolated sidecar')
                extras[suffix]=path
        root,service,_,result,extras,*_=await h.fixture(anime=False, exceptional=True,
            exceptional_name='Show SP.mkv',font_archive=True,excluded_misc=True,source_setup=seed)
        plan=result.plan
        self.assertTrue({extras['font'],extras['misc']}<=set(plan.ignored_sources))
        self.assertIn(extras['.flac'],{e.source_path for e in plan.entries})
        idx=next(e for e in plan.entries if e.source_path==extras['.en.idx'])
        special=next(e for e in plan.entries if e.source_path==extras['exceptional'])
        with source_reads(plan.source_root) as counts:
            plan=service.adjust_subtitles(plan.task_id,PlanStore.fingerprint(plan),[entry_id(idx)],main_language='ja')
            updated=[e for e in plan.entries if e.source_path in {extras['.en.idx'],extras['.en.sub']}]
            self.assertEqual(2,len(updated));self.assertEqual({'ja'},{e.subtitle.primary_language for e in updated})
            state=service.decision_state_store.load(plan.task_id)
            plan=service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=state.revision,
                mode='special',edit={'entry_id':entry_id(special),'action':'confirmed_official_special','provider_episode_id':'9001'})
            from tests.test_content_choice_contract import change
            plan=change(service,plan,[extras['.flac']])
        print('SOURCE_IO_AFTER_ADMISSION='+json.dumps(counts,sort_keys=True))
        self.assertEqual({'open':0,'stat':0,'enumerate':0},counts)
        self.assertIn(extras['.flac'],plan.ignored_sources)
        self.assertFalse(service.load_or_create(plan).approved_entry_ids)
        from src.rename.plan_verifier import PlanVerifier
        codes=[issue.code for issue in PlanVerifier().verify(plan).issues]
        self.assertNotIn('duplicate_subtitle_semantics',codes)
        self.assertNotIn('multiple_default_subtitles',codes)
        self.assertNotIn('generated_outputs',plan.metadata)

    async def test_movie_extra_keeps_strict_subtitle_and_audio_without_misc_outputs(self):
        from tests.test_initial_movie_completion_contract import InitialMovieFullCompletionContracts
        from tests import _media_flow_helpers as media
        from tests.test_content_choice_contract import change
        h=InitialMovieFullCompletionContracts('runTest');h.setUp();self.addCleanup(h.doCleanups)
        original=media._make_version
        sources={}
        def seed(*args,**kwargs):
            source,primary,archive=original(*args,**kwargs)
            extra=next(source.glob('SPs/*CM01*.mkv'))
            audio=extra.with_suffix('.flac');audio.write_bytes(b'paired extra audio')
            subtitle=extra.with_suffix('.en.ass');subtitle.write_bytes(b'paired extra subtitle')
            sources.update(extra=extra,audio=audio,subtitle=subtitle)
            return source,primary,archive
        with patch.object(media,'_make_version',side_effect=seed):
            root,service,plan,primary,other=await h.fixture()
        with source_reads(plan.source_root) as counts:
            plan=change(service,plan,[other['subtitle']])
            plan=change(service,plan,[sources['extra']],action='restore')
            plan=service.commit_initial_movie_completion(h.prepare(service,plan,[dict(sources=[str(primary)],label='1080p')]))
            chosen=next(e for e in plan.entries if e.source_path==sources['subtitle'])
            plan=service.adjust_subtitles(plan.task_id,PlanStore.fingerprint(plan),[entry_id(chosen)],main_language='en')
        print('MOVIE_SOURCE_IO_AFTER_ADMISSION='+json.dumps(counts,sort_keys=True))
        self.assertEqual({'open':0,'stat':0,'enumerate':0},counts)
        bundle={e.source_path:e for e in plan.entries if e.source_path in set(sources.values())}
        self.assertEqual(set(sources.values()),set(bundle))
        self.assertTrue(all('Extras' in e.target_path.parts for e in bundle.values()))
        self.assertEqual(bundle[sources['extra']].target_path.with_suffix('.flac'),bundle[sources['audio']].target_path)
        self.assertEqual(bundle[sources['extra']].target_path.stem+'.en.ass',bundle[sources['subtitle']].target_path.name)
        self.assertNotIn('generated_outputs',plan.metadata)
        self.assertFalse(service.load_or_create(plan).approved_entry_ids)
        from src.rename.plan_verifier import PlanVerifier
        self.assertEqual([],list(PlanVerifier().verify(plan).issues))
        reloaded=service.plan_store.load(plan.task_id)
        self.assertEqual(PlanStore.fingerprint(plan),PlanStore.fingerprint(reloaded))
        self.assertEqual('extra_subtitle',next(e.role for e in reloaded.entries if e.source_path==sources['subtitle']))
        main_subtitle=next(e for e in reloaded.entries if e.subtitle and e.subtitle.associated_video_source_path==primary)
        self.assertEqual('subtitle',main_subtitle.role)
        approved=service.approve_entries(plan.task_id,PlanStore.fingerprint(plan),[entry_id(e) for e in plan.entries])
        self.assertEqual({entry_id(e) for e in plan.entries},set(approved.approved_entry_ids))
