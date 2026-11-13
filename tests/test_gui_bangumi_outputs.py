"""当前窗口与 Bangumi 分集输出的定向合同。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from src.rename.plan_review import entry_id
from src.rename.special_catalog import read_source, build_observation
from tests import test_special_selection_contract as specials
def delayed_flow():
    from tests.test_series_confirmation_refresh import VisibleFlow, MEMBERS

    class DelayedFlow(VisibleFlow):
        delay = True
        async def select(self, *args, **kwargs):
            original = self.manager.wait_for_selection_result
            self.wait_calls = 0
            async def delayed(identifier):
                self.wait_calls += 1
                from src.rename.review_operation import current_provider_selection_review_operation
                _, pending = current_provider_selection_review_operation(self.manager._read(identifier))
                result = await original(identifier)
                if self.delay and self.wait_calls == 1:
                    assert pending['state'] == pending['lifecycle'] == 'pending'
                    return dict(pending)
                return result
            with patch.object(self.manager, 'wait_for_selection_result', side_effect=delayed):
                return await super().select(*args, **kwargs)

    return DelayedFlow, MEMBERS

class OutputContracts(unittest.IsolatedAsyncioTestCase):
    fixture = specials.SpecialSelectionContracts.fixture
    revise = specials.SpecialSelectionContracts.revise

    def test_generated_rollback_preserves_files_before_any_inverse(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        from src.rename import asset_rollback
        execution = Mock()
        execution.validate_approved_authority.return_value = SimpleNamespace(metadata={'generated_outputs': {}})
        execution._execution_lock.return_value = nullcontext()
        with patch.object(asset_rollback, 'require_media_execution'):
            with self.assertRaisesRegex(ValueError, '回收站'):
                asset_rollback.rollback(execution, 'task', 'fingerprint', expected_recovery_fingerprint='recovery')
        execution._validate_runtime_safety_gate.assert_not_called()
        execution._update_task.assert_not_called()

    def test_old_plan_rollback_enters_existing_locked_body(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        from src.rename import asset_rollback
        execution = Mock(record_root=Path('isolated-records'))
        execution._execution_lock.return_value = nullcontext()
        execution.validate_approved_authority.return_value = SimpleNamespace(metadata={
            'asset_operation': {'catalog_before': {'backend': 'series', 'key': 'series'}}})
        execution.series_catalog.lock.return_value = nullcontext()
        execution._read_task.return_value = {}
        with patch.object(asset_rollback, 'require_media_execution'), \
                patch.object(asset_rollback, 'prepare', return_value=SimpleNamespace(fingerprint='recovery')), \
                patch.object(asset_rollback, '_restored_catalog_candidate', return_value=(None, {}, False)), \
                patch.object(asset_rollback, 'approved_batch'), \
                patch.object(asset_rollback, '_journal_path'), \
                patch.object(asset_rollback, '_read_journal', return_value={'phase': 'rolled_back'}):
            result = asset_rollback.rollback(execution, 'task', 'fingerprint', expected_recovery_fingerprint='recovery')
        self.assertEqual('rolled_back', result.ledger_status)
        execution._validate_runtime_safety_gate.assert_called_once()
        execution.series_catalog._require_namespace_candidate.assert_called_once_with('series', {})

    async def test_wait_timeout_keeps_observing_open_window(self):
        DelayedFlow, MEMBERS = delayed_flow()
        with tempfile.TemporaryDirectory() as tmp:
            async with DelayedFlow(Path(tmp)) as runtime:
                result = await runtime.add(MEMBERS[-1], confirm=False)
                runtime.view._adopt_revision.assert_called_once_with(result.plan)
                self.assertEqual(2, runtime.wait_calls)
                runtime.view.close.assert_not_called()

    async def test_immediate_terminal_result_is_adopted_once(self):
        DelayedFlow, MEMBERS = delayed_flow()
        with tempfile.TemporaryDirectory() as tmp:
            async with DelayedFlow(Path(tmp)) as runtime:
                runtime.delay = False
                result = await runtime.add(MEMBERS[-1], confirm=False)
                runtime.view._adopt_revision.assert_called_once_with(result.plan)
                self.assertEqual(1, runtime.wait_calls)

    async def test_explicit_bangumi_single_side_generates_frozen_nfo(self):
        root, service, _, result, extras, *_ = await self.fixture(anime=True, exceptional=True, exceptional_name='Show SP.mkv')
        provider = result.plan.work_identity.primary_provider.to_dict()
        self.assertEqual('bangumi', provider['namespace'])
        row = dict(id=900001, subject_id=int(provider['provider_id']), type=1, sort=7, name='Special', name_cn='特别篇 & 测试')
        source = read_source(provider, client=SimpleNamespace(_get_json=lambda _:dict(total=1, offset=0, data=[row])))
        observed = build_observation(provider,[source])
        with patch('src.rename.special_crosswalk.observe', return_value=observed):
            plan = self.revise(service,result.plan,'special_refresh',{})
        key = entry_id(next(e for e in plan.entries if e.source_path == extras['exceptional']))
        changed = self.revise(service,plan,'special',dict(entry_id=key,action='bangumi_special',bangumi_key=provider['provider_id']+':900001',title='特别篇 & 测试',local_episode=1))
        video = next(e for e in changed.entries if entry_id(e)==key)
        work = Path(changed.metadata['canonical_work_root'])
        self.assertEqual(work/'Season 00'/f'{work.name} - S00E01 - SP07 - 特别篇 & 测试.mkv', video.target_path)
        rows = changed.metadata['generated_outputs']['files']
        nfo = next(row for row in rows if row['kind']=='bangumi_episode_nfo')
        self.assertEqual(str(video.target_path.with_suffix('.nfo')), nfo['target'])
        self.assertIn('<uniqueid type="Bangumi">900001</uniqueid>',nfo['content'])
        self.assertIn('&amp;',nfo['content'])
        self.assertFalse(video.target_path.exists())
        self.assertFalse(Path(nfo['target']).exists())
        self.assertFalse(video.target_path.parent.exists())
        from dataclasses import replace
        from src.rename.plan_store import PlanStore
        from src.rename.plan_verifier import PlanVerifier
        from src.rename.projection_authority import validate_plan_projection_receipt
        from src.rename.generated_outputs import approved_outputs, validate_completed, freeze_outputs, register_outputs
        from src.rename.special_catalog import plain
        from src.rename.trans import Trans
        self.assertFalse([i for i in PlanVerifier().verify(changed).issues if i.source_path == video.source_path])
        validate_plan_projection_receipt(changed)
        damaged = plain(changed.metadata)
        damaged['generated_outputs']['files'][0]['content'] += 'tamper'
        with self.assertRaises(ValueError):
            validate_plan_projection_receipt(replace(changed, metadata=damaged))
        before = PlanStore.fingerprint(plan)
        self.assertEqual(before, PlanStore.fingerprint(service.plan_store.load_version(plan.task_id,before)))
        self.assertNotIn('generated_outputs',plan.metadata)
        capability = approved_outputs(changed, PlanStore.fingerprint(changed))
        from src.rename.generated_outputs import write_outputs
        nfo_path = Path(nfo['target'])
        nfo_path.parent.mkdir(parents=True)
        nfo_path.write_text(nfo['content'], encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '状态已变化'):
            approved_outputs(changed, PlanStore.fingerprint(changed))
        with self.assertRaisesRegex(ValueError, '状态已变化'):
            write_outputs(capability, task_id=changed.task_id, fingerprint=PlanStore.fingerprint(changed), record_root=root/'rejected-record')
        nfo_path.rename(root/'retained-nfo.xml')
        dry = Trans(changed.as_mapping(),changed.task_id,PlanStore.fingerprint(changed),mode='hardlink',
            dry_run=True,record_root=root/'dry-record',generated_outputs=capability)
        self.assertIsNone(dry.trans_file())
        self.assertFalse(Path(nfo['target']).exists())
        import os, json
        original_open = Path.open
        def deny_nfo(path, mode='r', *args, **kwargs):
            if path == Path(nfo['target']) and mode == 'xb':
                raise PermissionError('isolated write failure')
            return original_open(path, mode, *args, **kwargs)
        from src.rename.generated_outputs import write_outputs, resume_outputs
        with patch.object(Path,'open',deny_nfo), self.assertRaisesRegex(ValueError,'未全部完成'):
            write_outputs(capability,task_id=changed.task_id,fingerprint=PlanStore.fingerprint(changed),record_root=root/'failed-record')
        self.assertFalse(video.target_path.exists())
        self.assertTrue(list((root/'failed-record').glob('*needs_manual_check.json')))
        link = os.link
        def after_nfo(source, target, *args, **kwargs):
            self.assertEqual(nfo['content'],Path(nfo['target']).read_text('utf-8'))
            return link(source,target,*args,**kwargs)
        trans = Trans(changed.as_mapping(),changed.task_id,PlanStore.fingerprint(changed),mode='hardlink',
            dry_run=False,record_root=root/'output-record',generated_outputs=capability)
        with patch('src.rename.trans.os.link',side_effect=after_nfo):
            self.assertIsNone(trans.trans_file())
        ledger = json.loads((root/'output-record'/f'{changed.task_id}.ledger.json').read_text('utf-8'))
        validate_completed(changed,ledger)
        self.assertEqual('created',ledger['generated_outputs']['files'][0]['status'])
        resumed = resume_outputs(changed,root/'output-record')
        self.assertEqual(ledger['generated_outputs'],write_outputs(resumed,task_id=changed.task_id,
            fingerprint=PlanStore.fingerprint(changed),record_root=root/'output-record'))
        payload = dict(canonical_root=str(work))
        self.assertTrue(register_outputs(payload,changed,ledger))
        self.assertEqual(1,payload['generated_outputs'][0]['local_episode'])
        with self.assertRaises(ValueError):
            validate_completed(changed,{**ledger,'generated_outputs':None})
        reused = freeze_outputs(changed)
        self.assertIsNotNone(reused.metadata['generated_outputs']['files'][0]['existing'])
        with self.assertRaises(ValueError):
            freeze_outputs(replace(changed,metadata={k:v for k,v in changed.metadata.items()
                if k not in {'generated_outputs','provider_special_decisions'}}))



    async def test_other_naming_modes_do_not_generate_nfo(self):
        from src.rename.plan_store import PlanStore
        root, service, _, result, extras, *_ = await self.fixture(anime=False, exceptional=True, exceptional_name='Show SP.mkv')
        provider = result.plan.work_identity.primary_provider.to_dict()
        source = read_source(provider,client=SimpleNamespace(get_season_info=lambda *_:dict(
            episodes=[dict(id=9001,episode_number=1,name='Special')])) )
        with patch('src.rename.special_crosswalk.observe',return_value=build_observation(provider,[source])):
            plan = self.revise(service,result.plan,'special_refresh',{})
        key = entry_id(next(e for e in plan.entries if e.source_path == extras['exceptional']))
        numbered = self.revise(service,plan,'special',dict(entry_id=key,action='confirmed_special_correspondence',tmdb_episode_id='9001',bangumi_key=None))
        self.assertNotIn('generated_outputs',numbered.metadata)
        descriptive = self.revise(service,numbered,'special',dict(entry_id=key,action='descriptive_special',title='独立特别篇',title_source='user',reference_key=None))
        self.assertNotIn('generated_outputs',descriptive.metadata)
        self.assertEqual(PlanStore.fingerprint(descriptive),PlanStore.fingerprint(service.plan_store.load(plan.task_id)))


    async def test_two_bangumi_subjects_keep_distinct_local_slots(self):
        from src.rename.special_selection import suggest_bangumi_episode
        root, service, _, result, extras, *_ = await self.fixture(anime=False, exceptional=True, exceptional_name='Show SP.mkv')
        provider=result.plan.work_identity.primary_provider.to_dict()
        sources=[read_source(provider,client=SimpleNamespace(get_season_info=lambda *_:dict(episodes=[])))]
        for subject, episode in ((100,1001),(200,2001)):
            ref=dict(namespace='bangumi',entity_type='subject',provider_id=str(subject))
            row=dict(id=episode,subject_id=subject,type=1,sort=1,name='SP',name_cn=f'特别篇{subject}')
            sources.append(read_source(ref,client=SimpleNamespace(_get_json=lambda _,row=row:dict(total=1,offset=0,data=[row]))))
        with patch('src.rename.special_crosswalk.observe',return_value=build_observation(provider,sources)):
            plan=self.revise(service,result.plan,'special_refresh',{})
        videos=[e for e in plan.entries if e.media_kind=='video']
        a,b=map(entry_id,videos[:2])
        first=self.revise(service,plan,'special',dict(entry_id=a,action='bangumi_special',bangumi_key='100:1001',title='特别篇100',local_episode=1))
        self.assertEqual(2,suggest_bangumi_episode(first,b,'200:2001'))
        with self.assertRaises(ValueError):
            self.revise(service,first,'special',dict(entry_id=b,action='bangumi_special',bangumi_key='200:2001',title='特别篇200',local_episode=1))
        second=self.revise(service,first,'special',dict(entry_id=b,action='bangumi_special',bangumi_key='200:2001',title='特别篇200',local_episode=2))
        self.assertEqual({1,2},{r['local_episode'] for r in second.metadata['generated_outputs']['files']})
        self.assertEqual(1,suggest_bangumi_episode(second,a,'100:1001'))
        self.assertEqual(2,suggest_bangumi_episode(second,b,'200:2001'))
        self.assertTrue(all('SP01' in r['target'] for r in second.metadata['generated_outputs']['files']))

from tests import test_special_crosswalk_gui as crosswalk_gui
class BangumiGui(crosswalk_gui.SpecialCrosswalkGui):
    async def test_explicit_bangumi_button_previews_and_saves_nfo_declaration(self):
        root,service,plan,extras,key=await self.prepared()
        await self.driver.click('特别篇 / OVA')
        controls=self.view._official_special_controls
        with self.client:
            controls['files'].set_value(key)
            controls['mode'].set_value('bangumi')
            controls['title'].set_value('人工特别篇标题')
        self.assertTrue(controls['confirm'].enabled)
        self.assertIn('S00E01 - SP6.5 - 人工特别篇标题',controls['preview'].text)
        self.assertEqual({'9001':'TMDB: S00E07'},controls['episode'].options)
        await self.driver.click_element(controls['confirm'])
        saved=service.plan_store.load(plan.task_id)
        self.assertEqual('bangumi_special',saved.metadata['provider_special_decisions'][key]['action'])
        row=saved.metadata['generated_outputs']['files'][0]
        self.assertIn('1011',row['content'])
        self.assertFalse(Path(row['target']).exists())
