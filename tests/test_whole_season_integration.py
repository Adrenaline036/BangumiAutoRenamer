"""Additional integration cases to install after RED closes."""

from tests._historical_replacement import historical_replacement
from tests.test_whole_season import WholeSeason
from src.rename.plan_store import PlanStore
from src.rename.asset_execution_recovery import AssetExecutionRecovery
from src.rename.asset_resume import resume_asset
from src.rename.asset_replacement import approved_batch
from src.rename.series_catalog import SeriesCatalog
from unittest.mock import patch
import json
import os
from pathlib import Path


class WholeSeasonIntegration(WholeSeason):
    from tests.test_cross_provider_work_correction import CrossProviderWorkCorrection
    create_page=CrossProviderWorkCorrection.create_page
    async def prepare_old(self):
        result=await self.incoming()
        target=next(e.target_path for e in result.plan.entries if e.media_kind=='video')
        target.parent.mkdir(parents=True);target.write_bytes(b'old-video')
        extra=target.parent/'Scans'/'old.txt';extra.parent.mkdir();extra.write_bytes(b'old-scan')
        other=target.parent.parent/'Season 02'/'keep.mkv';other.parent.mkdir();other.write_bytes(b'other')
        op,_=self.review.asset_actions().prepare_replacement(result.plan)
        result.plan=self.review.confirm_asset_replacement(result.plan.task_id,PlanStore.fingerprint(result.plan),expected_operation_fingerprint=op.fingerprint)
        return result,target,extra,other

    async def test_unregistered_registers_then_second_new_task_replaces_without_old_card(self):
        result,target,extra,other=await self.prepare_old()
        task,ledger,_service=await self.h._approve_and_execute(root=self.root,result=result)
        self.assertEqual('completed',task.get('execution_status'),task.get('error'))
        self.assertEqual('completed',ledger.get('status'))
        self.assertFalse(extra.exists());self.assertEqual(b'other',other.read_bytes())
        source=next(e.source_path for e in result.plan.entries if e.target_path==target)
        self.assertTrue(os.path.samefile(source,target))
        (self.root/'tasks'/f'{result.plan.task_id}.json').unlink()
        second=await self.incoming('second')
        op,_=self.review.asset_actions().prepare_replacement(second.plan)
        self.assertTrue(op.to_dict()['catalog_before']['rows']['bindings'])
        second.plan=self.review.confirm_asset_replacement(second.plan.task_id,PlanStore.fingerprint(second.plan),expected_operation_fingerprint=op.fingerprint)
        task,ledger,_=await self.h._approve_and_execute(root=self.root,result=second)
        self.assertEqual('completed',task.get('execution_status'),task.get('error'))
        match=SeriesCatalog(self.root/'series_catalog').load(second.plan.metadata['series_key'])
        self.assertEqual({second.plan.task_id},{r['task_id'] for r in match.season_bindings if r['season_number']==1})
        self.assertEqual(b'other',other.read_bytes())

    async def test_unregistered_completed_media_can_resume_missing_catalog_after_restart(self):
        result,target,extra,other=await self.prepare_old()
        task,ledger,service=await self.h._approve_and_execute(root=self.root,result=result,fail_series_registration=True)
        self.assertEqual('completed',ledger.get('status'))
        fingerprint=PlanStore.fingerprint(result.plan)
        recovery=AssetExecutionRecovery(service).inspect(result.plan.task_id,fingerprint)
        self.assertEqual('catalog_pending',recovery.state)
        with patch('src.rename.plan_execution.is_dry_run_enabled',return_value=False):
            resumed=resume_asset(service,result.plan.task_id,fingerprint)
        self.assertEqual('completed',resumed.ledger_status)
        self.assertEqual('completed',AssetExecutionRecovery(service).inspect(result.plan.task_id,fingerprint).state)
        self.assertFalse(extra.exists());self.assertEqual(b'other',other.read_bytes())

    @historical_replacement
    async def test_inverse_restores_unregistered_old_files_and_empty_binding(self):
        from src.rename.asset_rollback import prepare,rollback
        result,target,extra,other=await self.prepare_old()
        task,ledger,service=await self.h._approve_and_execute(root=self.root,result=result)
        self.assertEqual('completed',task.get('execution_status'),task.get('error'))
        fingerprint=PlanStore.fingerprint(result.plan)
        inverse=prepare(service,result.plan.task_id,fingerprint)
        with patch('src.rename.plan_execution.is_dry_run_enabled',return_value=False):
            rollback(service,result.plan.task_id,fingerprint,expected_recovery_fingerprint=inverse.fingerprint)
        self.assertEqual(b'old-video',target.read_bytes())
        self.assertEqual(b'old-scan',extra.read_bytes())
        self.assertEqual(b'other',other.read_bytes())
        match=service.series_catalog.load(result.plan.metadata['series_key'])
        self.assertFalse([r for r in match.season_bindings if r['season_number']==1])

    async def test_mid_switch_crash_resumes_frozen_season_without_retiring_early(self):
        from src.rename.trans import Trans
        result,target,extra,other=await self.prepare_old()
        with patch.object(Trans,'trans_file',return_value='fixture stops before first write'):
            _task,_ledger,service=await self.h._approve_and_execute(root=self.root,result=result)
        fingerprint=PlanStore.fingerprint(result.plan)
        transfer=Trans(result.plan.as_mapping(),result.plan.task_id,fingerprint,mode='hardlink',
            dry_run=False,record_root=self.root/'records',
            operations=service._build_execution_operations(result.plan),
            replacement_authority=approved_batch(result.plan,fingerprint))
        original=os.replace
        def crash(source,destination):
            original(source,destination)
            if Path(destination)==target:
                self.assertEqual(b'old-scan',extra.read_bytes())
                raise SystemExit('fixture process death after target switch')
        with patch('src.rename.trans.os.replace',side_effect=crash):
            with self.assertRaises(SystemExit):transfer.trans_file()
        inspection=AssetExecutionRecovery(service).inspect(result.plan.task_id,fingerprint)
        self.assertEqual('resume_media',inspection.state)
        with patch('src.rename.plan_execution.is_dry_run_enabled',return_value=False):
            resumed=resume_asset(service,result.plan.task_id,fingerprint)
        self.assertEqual('completed',resumed.ledger_status)
        self.assertFalse(extra.exists());self.assertEqual(b'other',other.read_bytes())
        self.assertTrue(os.path.samefile(target,next(e.source_path for e in result.plan.entries if e.target_path==target)))

    async def test_real_producer_reaches_confirmation_and_cancel_leaves_old_season(self):
        from src.rename.season_reference import SeasonReferences
        from tests.test_content_choice_contract import records
        result=await self.incoming()
        target=next(e.target_path for e in result.plan.entries if e.media_kind=='video')
        target.parent.mkdir(parents=True);target.write_bytes(b'old')
        extra=target.parent/'Scans'/'old.txt';extra.parent.mkdir();extra.write_bytes(b'scan')
        with patch('src.pages.season_reference_view.load',return_value=SeasonReferences()):
            self.create_page(prepared=(self.root,self.review,result.plan,[]))
            worker=getattr(self.view,'_season_reference_task',None)
            if worker is not None:await worker
            before=records(self.root)
            old_ids=set(self.client.elements)
            await self.driver.click('换源')
            text=' '.join(str(getattr(e,'text','')) for key,e in self.client.elements.items() if key not in old_ids)
            self.assertIn(str(extra),text);self.assertIn(str(target),text)
            self.assertNotIn('未知归属文件不会被自动删除',text)
            await self.driver.click('取消')
            self.assertEqual(before,records(self.root))
            self.assertEqual(b'old',target.read_bytes());self.assertEqual(b'scan',extra.read_bytes())
