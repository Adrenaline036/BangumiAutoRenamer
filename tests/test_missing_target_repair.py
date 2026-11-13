"""缺失目标补齐的隔离事故验证；文件移至保留目录模拟删除。"""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tests import _media_flow_helpers as tv
from src.rename.plan_store import PlanStore
from src.rename.plan_review import PlanReviewService
from src.rename.series_catalog import SeriesCatalog
from src.rename import series_registration
from src.rename.asset_resume import resume_asset

class MissingTargetRepair(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.h=tv.MediaFlow('runTest')
        self.review=PlanReviewService(PlanStore(self.root/'plans'), self.root/'reviews',
            decision_state_root=self.root/'decisions', task_root=self.root/'tasks',
            catalog_root=self.root/'work_catalog',record_root=self.root/'records',
            provider_link_catalog_root=self.root/'provider_links')
        self.counter=0

    def move_as_missing(self,path):
        path=Path(path); assert path.resolve().is_relative_to(self.root.resolve())
        self.counter+=1
        saved=self.root/'retained-samples'/str(self.counter)/path.name
        saved.parent.mkdir(parents=True)
        path.rename(saved)
        self.assertFalse(path.exists())
        return saved

    async def incoming(self,name, *, source=None, second=False, fonts=False):
        if source is None:
            source,font=tv.make_season(self.root/name,second=second)
            if not fonts:self.move_as_missing(font)
        result=await self.h._run_flow(root=self.root,source=source,task_id=name,
            subject_id='86670' if second else '41658')
        return result

    async def execute(self,result):
        task,ledger,service=await self.h._approve_and_execute(root=self.root,result=result)
        self.assertEqual('completed',task.get('execution_status'),task.get('error'))
        self.assertEqual('completed',ledger.get('status'))
        return service

    async def old_and_incoming(self,partial=False):
        old=await self.incoming('old')
        await self.execute(old)
        peers=await self.incoming('season-two',second=True)
        await self.execute(peers)
        other={str(e.target_path):self.fact(e.target_path) for e in peers.plan.entries}
        entries=old.plan.entries
        self.assertGreaterEqual(len(entries),3)
        for e in (entries[:2] if partial else entries):
            self.move_as_missing(e.target_path)
        source=entries[0].source_path.parent
        incoming=self.root/'incoming-source'/source.name
        incoming.mkdir(parents=True)
        for p in source.rglob('*'):
            if p.is_file():
                if partial and p != entries[0].source_path:
                    continue
                dest=incoming/p.relative_to(source)
                dest.parent.mkdir(parents=True,exist_ok=True)
                dest.write_bytes(p.read_bytes())
        # A distinct download avoids taking the original task's source reservation.
        current=await self.incoming('current',source=incoming)
        return old,current,peers,other

    @staticmethod
    def fact(p):
        st=p.stat()
        return st.st_dev,st.st_ino,st.st_mtime_ns,hashlib.sha256(p.read_bytes()).hexdigest()

    def confirm(self,result):
        actions=self.review.asset_actions()
        self.assertTrue(callable(getattr(actions,'prepare_missing',None)),
            '缺失目标应提供可冻结核查的原任务补齐入口')
        op,draft=actions.prepare_missing(result.plan)
        self.assertEqual('repair',op.to_dict()['mode'])
        self.assertFalse(op.to_dict()['replaces'])
        self.assertFalse(op.to_dict()['retirements'])
        result.plan=self.review.confirm_asset_repair(result.plan.task_id,PlanStore.fingerprint(result.plan),
            expected_operation_fingerprint=op.fingerprint)
        self.assertEqual('current',result.plan.task_id)
        return result

    def entry_set(self,plan):
        return {(str(e.source_path),str(e.target_path)) for e in plan.entries}

    def assert_materialized(self,plan,expected):
        self.assertTrue(expected)
        self.assertEqual(expected,self.entry_set(plan))
        for entry in plan.entries:
            self.assertTrue(entry.target_path.is_file(),str(entry.target_path))
            self.assertEqual(hashlib.sha256(entry.source_path.read_bytes()).hexdigest(),
                             hashlib.sha256(entry.target_path.read_bytes()).hexdigest())
            self.assertTrue(os.path.samefile(entry.source_path,entry.target_path))

    async def test_old_owner_whole_missing_repaired_and_restart_idempotent(self):
        old,current,peer,other=await self.old_and_incoming()
        self.review.asset_actions()
        with self.assertRaisesRegex(ValueError,'已经登记'):
            series_registration.validate_current_catalog(current.plan,self.review.series_catalog,
                self.review.provider_link_catalog)
        expected=self.entry_set(current.plan)
        old_record={str(p):p.read_bytes() for p in (self.root/'records').rglob('*') if p.is_file()}
        self.assertTrue(old_record)
        self.confirm(current)
        service=await self.execute(current)
        self.assert_materialized(current.plan,expected)
        for path,data in old_record.items():
            self.assertEqual(data,Path(path).read_bytes())
        match=service.series_catalog.load(current.plan.metadata['series_key'])
        self.assertEqual({'current'},{r['task_id'] for r in match.season_bindings if r['season_number']==1})
        self.assertEqual(other,{p:self.fact(Path(p)) for p in other})
        before=(service.series_catalog._path(match.series_key)).read_bytes()
        from src.rename.asset_execution_recovery import AssetExecutionRecovery
        fp=PlanStore.fingerprint(current.plan)
        self.assertEqual('completed',AssetExecutionRecovery(service).inspect('current',fp).state)
        with patch('src.rename.plan_execution.is_dry_run_enabled',return_value=False):
            resume_asset(service,'current',fp)
        self.assertEqual(before,service.series_catalog._path(match.series_key).read_bytes())

    async def test_partial_missing_keeps_existing_files_and_other_season(self):
        old,current,peer,other=await self.old_and_incoming(partial=True)
        kept={str(e.target_path):self.fact(e.target_path) for e in old.plan.entries if e.target_path.exists()}
        expected=self.entry_set(current.plan)
        self.assertIn((str(current.plan.entries[0].source_path),str(old.plan.entries[0].target_path)),expected)
        self.confirm(current)
        await self.execute(current)
        self.assert_materialized(current.plan,expected)
        self.assertFalse(old.plan.entries[1].target_path.exists())
        self.assertEqual(kept,{p:self.fact(Path(p)) for p in kept})
        self.assertEqual(other,{p:self.fact(Path(p)) for p in other})

    async def test_foreign_target_rejected(self):
        old,current,peer,other=await self.old_and_incoming()
        target=current.plan.entries[0].target_path
        target.write_bytes(b'foreign')
        actions=self.review.asset_actions()
        self.assertTrue(callable(getattr(actions,'prepare_missing',None)),'缺少补齐核查')
        with self.assertRaises((ValueError,RuntimeError)):
            actions.prepare_missing(current.plan)
        self.assertEqual(b'foreign',target.read_bytes())
        self.assertEqual(other,{p:self.fact(Path(p)) for p in other})

    async def test_permission_root_source_drift_and_disconnect_rejected(self):
        old,current,peer,other=await self.old_and_incoming()
        from src.rename import series_repair
        from src.rename.asset_operation import AssetOperation
        actions=self.review.asset_actions()
        op,_=actions.prepare_missing(current.plan)
        value=op.to_dict()
        with patch.object(series_repair.os,'scandir',side_effect=PermissionError('isolated permission denied')):
            with self.assertRaises(PermissionError):actions.prepare_missing(current.plan)
        original=current.plan.entries[0].source_path
        saved=self.move_as_missing(original)
        with self.assertRaises((ValueError,RuntimeError,OSError)):actions.prepare_missing(current.plan)
        saved.rename(original)
        tampered={**value,'repair_root':{**value['repair_root'],'inode':value['repair_root']['inode']+1}}
        with self.assertRaisesRegex(ValueError,'身份已变化'):
            series_repair.validate(actions,current.plan,tampered)
        bad={**value,'retirements':[{}]}
        with self.assertRaises((ValueError,KeyError)):AssetOperation.from_mapping(bad)
        self.confirm(current)
        from src.rename.plan_review import entry_id
        with patch.object(series_repair,'validate',side_effect=ValueError('isolated disconnected repair authority')):
            with self.assertRaisesRegex((ValueError,RuntimeError),'isolated disconnected repair authority'):
                self.review.approve_entries('current',PlanStore.fingerprint(current.plan),
                    [entry_id(e) for e in current.plan.entries])
        self.assertTrue(all(not e.target_path.exists() for e in current.plan.entries))
        self.assertEqual(other,{p:self.fact(Path(p)) for p in other})
        boundary=Path(value['repair_root']['path'])
        self.move_as_missing(boundary)
        boundary.mkdir()
        with self.assertRaisesRegex(ValueError,'身份已变化'):
            series_repair.validate(actions,current.plan,value)


    async def test_gui_repair_entry_and_confirmation_reach_service(self):
        old,current,peer,other=await self.old_and_incoming(partial=True)
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        from src.pages import asset_actions_view
        holder=ContentWorkbenchGui('runTest')
        holder.create_page(prepared=(self.root,self.review,current.plan,[]),cleanup_owner=self)
        original=asset_actions_view.preview_asset_replacement
        calls=[]
        async def observed(view,**kwargs):
            calls.append(kwargs)
            await original(view,**kwargs)
        with patch.object(asset_actions_view,'preview_asset_replacement',observed):
            await holder.driver.click('核查并补齐')
        self.assertEqual([{'repair':True}],calls)
        await holder.driver.click('生成计划')
        current.plan=self.review.plan_store.load('current')
        self.assertEqual('repair',current.plan.metadata['asset_operation']['mode'])
        self.assertEqual('need_confirm',json.loads((self.root/'tasks/current.json').read_text('utf-8'))['workflow_status'])
        self.assertTrue(all(not e.target_path.exists() for e in current.plan.entries))

    async def test_interrupted_repair_resumes_same_approval(self):
        old,current,peer,other=await self.old_and_incoming()
        self.confirm(current)
        from src.rename.trans import Trans
        original=Trans._execute_replacement_entry
        transfer=Trans.trans_file
        count=[]
        def stop(transfer,batch,operation,index,journal):
            original(transfer,batch,operation,index,journal)
            count.append(index)
            if len(count)==2: raise SystemExit('isolated process interruption')
        def disconnected(worker):
            try:return transfer(worker)
            except SystemExit as exc:raise RuntimeError(str(exc)) from exc
        # Approval is real; interruption is injected only in the file executor.
        with patch.object(Trans,'_execute_replacement_entry',stop), patch.object(Trans,'trans_file',disconnected):
            task,ledger,service=await self.h._approve_and_execute(root=self.root,result=current)
            self.assertNotEqual('completed',task.get('execution_status'))
        self.assertEqual(2,len(count))
        present={str(e.target_path):self.fact(e.target_path) for e in current.plan.entries if e.target_path.exists()}
        self.assertEqual(2,len(present))
        expected=self.entry_set(current.plan)
        from src.rename.plan_execution import PlanExecutionService
        restarted=PlanExecutionService(service.plan_store,service.review_service,task_root=service.task_root,
            record_root=service.record_root,catalog_root=service.work_catalog.root,
            series_catalog_root=service.series_catalog.root,decision_state_root=service.decision_state_store.root,
            provider_link_catalog_root=service.provider_link_catalog.root,
            recovery_transaction_root=self.root/'recovery_transactions')
        with patch('src.rename.plan_execution.is_dry_run_enabled',return_value=False):
            result=resume_asset(restarted,'current',PlanStore.fingerprint(current.plan))
        self.assertEqual('completed',result.ledger_status)
        self.assert_materialized(current.plan,expected)
        self.assertEqual(present,{p:self.fact(Path(p)) for p in present})
        self.assertEqual(other,{p:self.fact(Path(p)) for p in other})

