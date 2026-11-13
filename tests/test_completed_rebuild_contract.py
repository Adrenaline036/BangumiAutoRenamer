"""Completed recovery through real temporary recognition/approval/Trans.

Provider transports are offline fixtures. No production task or media is read.
The retry notification negative is a UI component test, not execution proof.
"""
import asyncio
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.rename.decision_state import DecisionStateStore
from src.rename.plan_store import PlanStore
from src.rename.plan_execution import PlanExecutionError
from src.rename.recovery_service import RecoveryService
from tests import _media_flow_helpers as fixture


def snapshot_files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and p.suffix != '.lock'}


def recovery(root):
    from src.rename.completed_rebuild import CompletedRebuildService
    return CompletedRebuildService(plan_store=PlanStore(root/'plans'),
        decision_store=DecisionStateStore(root/'decisions'), task_root=root/'tasks',
        record_root=root/'records', catalog_root=root/'work_catalog',
        series_catalog_root=root/'series_catalog', provider_link_catalog_root=root/'provider_links')


class CompletedRebuildContracts(unittest.IsolatedAsyncioTestCase):

    async def complete(self, root, index=0):
        harness = fixture.MediaFlow(methodName='runTest')
        source, _ = fixture.make_season(root if index < 2 else root/'third', second=index == 1)
        result = await harness._run_flow(root=root, source=source,
            task_id=f'rebuild-season-{index}', subject_id=('41658','86670','290980')[index])
        task, ledger, executor = await harness._approve_and_execute(root=root, result=result)
        self.assertEqual('completed', task.get('workflow_status'), task.get('error'))
        self.assertEqual('completed', ledger.get('status'))
        return result, executor


    async def test_retry_guard_error_is_visible_not_uncaught(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); result,_=await self.complete(root)
            # Preserve real fixture completion bytes in the configured runtime
            # directory names. No fake completed task or queue guard response.
            for old,new in (('plans','plan'),('decisions','decision_state'),('tasks','task'),
                            ('records','record'),('reviews','plan_review'),('provider_links','provider_link_catalog')):
                if (root/old).exists(): shutil.copytree(root/old, root/new, dirs_exist_ok=True)
            child=subprocess.run([sys.executable,'-X','utf8','-B','tests/completed_rebuild_gui_probe.py',
                str(root),result.plan.task_id], capture_output=True, text=True, encoding='utf-8', timeout=60)
            self.assertEqual(0,child.returncode,child.stdout+child.stderr)
            self.assertIn('REBUILD_GUI_PASS',child.stdout)

    async def test_existing_diagnosis_offers_rebuild_when_registered_root_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            result, _ = await self.complete(root)
            plan=PlanStore(root/'plans').load(result.plan.task_id)
            target=Path(plan.metadata['canonical_work_root'])
            self.assertTrue(target.is_relative_to(root))
            shutil.rmtree(target)
            state=DecisionStateStore(root/'decisions').load(plan.task_id)
            before=snapshot_files(root)
            answer=RecoveryService(catalog_root=root/'work_catalog', record_root=root/'records',
                series_catalog_root=root/'series_catalog').diagnose(state,plan)
            self.assertTrue(answer.can_rebuild_plan, (answer.state, answer.detail))
            self.assertEqual(before,snapshot_files(root))

    async def test_partial_missing_new_revision_preserves_approval_gate_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); result, executor=await self.complete(root)
            plans=executor.plan_store; old=plans.load(result.plan.task_id); oldfp=plans.fingerprint(old)
            ledger=(root/'records'/f'{old.task_id}.ledger.json').read_bytes()
            missing=old.entries[0].target_path; missing.unlink()
            kept={e.target_path:(e.target_path.stat().st_ino,e.target_path.stat().st_mtime_ns,
                hashlib.sha256(e.target_path.read_bytes()).hexdigest()) for e in old.entries[1:]}
            source_before={e.source_path:hashlib.sha256(e.source_path.read_bytes()).hexdigest() for e in old.entries}
            service=recovery(root); before=snapshot_files(root); view=service.inspect(old.task_id)
            self.assertEqual(before,snapshot_files(root))
            self.assertEqual(('missing',1,len(old.entries)-1), (view.state,view.missing_count,view.reuse_count))
            fresh=service.generate(old.task_id,expected_fingerprint=view.fingerprint)
            self.assertEqual(old.task_id,fresh.task_id)
            self.assertEqual(oldfp,fresh.revision_parent_fingerprint)
            self.assertEqual(old.work_identity,fresh.work_identity)
            self.assertEqual(old.metadata['season_number'],fresh.metadata['season_number'])
            self.assertFalse(missing.exists())
            self.assertNotEqual(oldfp,plans.fingerprint(fresh))
            with self.assertRaises(PlanExecutionError): executor.validate_approved(old.task_id,oldfp)
            self.assertEqual(ledger,(root/'records'/f'{old.task_id}.ledger.json').read_bytes())
            archived=root/'records'/'history'/old.task_id/f'{oldfp}.ledger.json'
            self.assertEqual(ledger,archived.read_bytes())
            repeated = service.generate(old.task_id,expected_fingerprint=view.fingerprint)
            self.assertEqual(plans.fingerprint(fresh), plans.fingerprint(repeated))
            self.assertEqual(plans.fingerprint(fresh),plans.fingerprint(plans.load(old.task_id)))
            harness=fixture.MediaFlow(methodName='runTest')
            task, newledger, _=await harness._approve_and_execute(root=root,result=SimpleNamespace(
                plan=fresh,task=json.loads((root/'tasks'/f'{old.task_id}.json').read_text('utf-8'))))
            self.assertEqual('completed',task.get('workflow_status'),task.get('error'))
            self.assertEqual(1,sum(r['status']=='created' for r in newledger['entries']))
            self.assertEqual(len(kept),sum(r['status']=='reused_identical_asset' for r in newledger['entries']))
            self.assertEqual(kept,{p:(p.stat().st_ino,p.stat().st_mtime_ns,
                hashlib.sha256(p.read_bytes()).hexdigest()) for p in kept})
            self.assertEqual(source_before,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in source_before})
            self.assertEqual(ledger,archived.read_bytes())

            from src.rename.recovery_service import RecoveryService
            diagnosed = RecoveryService(catalog_root=root/'work_catalog', record_root=root/'records',
                series_catalog_root=root/'series_catalog', rebuild_service=service).diagnose(service.decisions.load_readonly(old.task_id), fresh)
            self.assertEqual('healthy', diagnosed.state, diagnosed.detail)
            archived.write_bytes(ledger + b' ')
            denied = RecoveryService(catalog_root=root/'work_catalog', record_root=root/'records',
                series_catalog_root=root/'series_catalog', rebuild_service=service).diagnose(service.decisions.load_readonly(old.task_id), fresh)
            self.assertEqual('evidence_conflict', denied.state)
            self.assertFalse(denied.can_rebuild_catalog)
            archived.write_bytes(ledger)

    async def test_projection_failure_preserves_current_and_retry_archives_idempotently(self):
        from src.rename.projection_authority import ProjectionCommitService
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); result,_=await self.complete(root)
            plans=PlanStore(root/'plans'); old=plans.load(result.plan.task_id)
            old.entries[0].target_path.unlink()
            for stage in ('prepared','decision_written','ownership_claimed','plan_written'):
                with self.subTest(stage=stage):
                    service=recovery(root); view=service.inspect(old.task_id)
                    before={p:p.read_bytes() for p in (
                        root/'tasks'/f'{old.task_id}.json',root/'decisions'/f'{old.task_id}.json',
                        plans.path_for(old.task_id),root/'records'/f'{old.task_id}.ledger.json')}
                    fired=[]
                    def fault(self,phase):
                        if phase==stage:
                            fired.append(phase)
                            raise RuntimeError('fixture projection interruption:'+stage)
                    with patch.object(ProjectionCommitService,'_checkpoint',fault):
                        with self.assertRaisesRegex(RuntimeError,'fixture projection interruption:'+stage):
                            service.generate(old.task_id,expected_fingerprint=view.fingerprint)
                    self.assertEqual([stage],fired)
                    self.assertEqual(before,{p:p.read_bytes() for p in before})
                    self.assertFalse(old.entries[0].target_path.exists())
            # A fresh service instance can use the old completion without stale success.
            service=recovery(root); view=service.inspect(old.task_id)
            fresh=service.generate(old.task_id,expected_fingerprint=view.fingerprint)
            self.assertEqual(plans.fingerprint(old),fresh.revision_parent_fingerprint)
            self.assertFalse(old.entries[0].target_path.exists())

    async def test_three_seasons_rebuild_out_of_order_without_catalog_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); results=[]
            for i in range(3):
                result,_=await self.complete(root,i); results.append(result)
            plans=PlanStore(root/'plans'); old=[plans.load(r.plan.task_id) for r in results]
            catalog_before=snapshot_files(root/'series_catalog')
            work=Path(old[0].metadata['canonical_work_root'])
            self.assertEqual({str(work)},{p.metadata['canonical_work_root'] for p in old})
            self.assertTrue(work.is_relative_to(root)); shutil.rmtree(work)
            sources={e.source_path:hashlib.sha256(e.source_path.read_bytes()).hexdigest()
                for p in old for e in p.entries}
            service=recovery(root); harness=fixture.MediaFlow(methodName='runTest')
            # Only the first card gets a new Plan; other deleted peers remain completed.
            for i in (2,0,1):
                view=service.inspect(old[i].task_id)
                self.assertEqual('missing',view.state,view.detail)
                fresh=service.generate(old[i].task_id,expected_fingerprint=view.fingerprint)
                task,ledger,_=await harness._approve_and_execute(root=root,result=SimpleNamespace(
                    plan=fresh,task=json.loads((root/'tasks'/f'{fresh.task_id}.json').read_text('utf-8'))))
                self.assertEqual('completed',task.get('execution_status'),task.get('error'))
                self.assertEqual('completed',ledger.get('status'))
                self.assertEqual(catalog_before,snapshot_files(root/'series_catalog'))
            self.assertEqual(sources,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
            self.assertTrue(fixture.contribution(plans.load(old[0].task_id)).destination.is_dir())

    async def test_source_or_foreign_target_drift_rejected_without_new_plan(self):
        for drift in ('source','target'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as directory:
                root=Path(directory); result,_=await self.complete(root)
                old=PlanStore(root/'plans').load(result.plan.task_id)
                old.entries[0].target_path.unlink()
                if drift=='source': old.entries[0].source_path.write_bytes(b'changed')
                else: old.entries[0].target_path.write_bytes(b'foreign')
                before=snapshot_files(root); service=recovery(root)
                view=service.inspect(old.task_id)
                self.assertEqual('blocked',view.state)
                with self.assertRaises((ValueError,RuntimeError)):
                    service.generate(old.task_id,expected_fingerprint=view.fingerprint)
                self.assertEqual(before,snapshot_files(root))

    async def test_rebuild_history_tamper_and_producer_disconnect_cannot_be_approved(self):
        from dataclasses import replace
        from src.rename.projection_authority import FinalPlanProjector, ProjectionAuthorityEnvelope, SourceOwnershipStore, inventory_fingerprint_for_plan
        from src.rename.plan_review import entry_id
        from src.rename.projection_authority import validate_plan_projection_receipt
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); result,executor=await self.complete(root)
            old=executor.plan_store.load(result.plan.task_id); old.entries[0].target_path.unlink()
            service=recovery(root); view=service.inspect(old.task_id)
            fresh=service.generate(old.task_id,expected_fingerprint=view.fingerprint)
            state=service.decisions.load_readonly(old.task_id)
            ownership=SourceOwnershipStore(root/'source_ownership').load_optional(old.task_id)
            before=snapshot_files(root)
            envelope=ProjectionAuthorityEnvelope(previous_decision=state,
                decision=replace(state,completed_rebuild_observation=None), draft_plan=fresh,
                inventory_fingerprint=inventory_fingerprint_for_plan(fresh),episode_ownership=())
            with self.assertRaisesRegex(RuntimeError,'重建权威已断开'):
                FinalPlanProjector.project(envelope,ownership)
            altered=dict(fresh.metadata); altered.pop('completed_rebuild_observation')
            with self.assertRaises(RuntimeError): validate_plan_projection_receipt(replace(fresh,metadata=altered))
            self.assertEqual(before,snapshot_files(root))
            archive=root/'records'/'history'/old.task_id/f'{service.plans.fingerprint(old)}.ledger.json'
            mapping = archive.with_name(archive.name.replace('.ledger.json', '.mapping.json'))
            original_mapping = mapping.read_bytes()
            mapping.write_bytes(original_mapping + b' ')
            with self.assertRaisesRegex(ValueError, '映射历史'):
                service.validate_plan(fresh)
            mapping.write_bytes(original_mapping)
            payload=json.loads(archive.read_text('utf-8')); payload['entries'][0]['source_inode']+=1
            archive.write_text(json.dumps(payload),encoding='utf-8')
            before=snapshot_files(root)
            with self.assertRaises((ValueError,RuntimeError)):
                executor.review_service.approve_entries(fresh.task_id,service.plans.fingerprint(fresh),
                    [entry_id(e) for e in fresh.entries])
            with self.assertRaises((ValueError,RuntimeError)): service.validate_plan(fresh)
            self.assertEqual(before,snapshot_files(root))

    async def test_missing_frozen_archive_cannot_fall_back_to_current_ledger(self):
        from src.rename.plan_review import entry_id
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); result,executor=await self.complete(root)
            old=executor.plan_store.load(result.plan.task_id)
            old.entries[0].target_path.unlink()
            service=recovery(root); view=service.inspect(old.task_id)
            fresh=service.generate(old.task_id,expected_fingerprint=view.fingerprint)
            archive=root/'records'/'history'/old.task_id/f'{service.plans.fingerprint(old)}.ledger.json'
            self.assertEqual(archive.read_bytes(),(root/'records'/f'{old.task_id}.ledger.json').read_bytes())
            archive.unlink()
            before=snapshot_files(root)
            with self.assertRaises((ValueError,RuntimeError)):
                service.validate_plan(fresh)
            with self.assertRaises((ValueError,RuntimeError)):
                executor.review_service.approve_entries(fresh.task_id,service.plans.fingerprint(fresh),
                    [entry_id(e) for e in fresh.entries])
            self.assertEqual(before,snapshot_files(root))


if __name__=='__main__': unittest.main()
