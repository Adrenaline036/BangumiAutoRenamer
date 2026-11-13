"""冻结清单到执行/恢复的来源观察合同。"""
from contextlib import ExitStack, contextmanager
from collections import Counter
from pathlib import Path
import builtins,io,json,os,sys,tempfile,unittest
from unittest.mock import patch

@contextmanager
def source_reads(root):
    counts=Counter();prefix=str(root).casefold()
    def wrap(name,fn):
        def call(value,*args,**kwargs):
            if isinstance(value,(str,os.PathLike)):
                path=str(Path(value).absolute()).casefold()
                if path==prefix or path.startswith(prefix+os.sep):
                    frame=sys._getframe(1);instrumentation=False
                    while frame:
                        filename=frame.f_code.co_filename.replace('\\','/')
                        if '/runtime/src/' in filename:break
                        if filename.endswith('/runner.py'):
                            instrumentation=True;break
                        frame=frame.f_back
                    if not instrumentation:counts[name]+=1
            return fn(value,*args,**kwargs)
        return call
    with ExitStack() as stack:
        for obj,names in ((Path,('stat','lstat','resolve')),(os,('scandir',)),(builtins,('open',)),(io,('open',))):
            for name in names:stack.enter_context(patch.object(obj,name,wrap(name,getattr(obj,name))))
        yield counts

class ExecutionInventoryTests(unittest.TestCase):
    def fixture(self,root):
        import asyncio
        from tests._media_flow_helpers import approved_tv
        return asyncio.run(approved_tv(self))

    def test_execution_and_recovery_consume_inventory(self):
        from src.rename.execution_recovery import _validate_completed_hardlink_ledger
        with tempfile.TemporaryDirectory() as tmp, patch('src.rename.trans.cm.get_config',return_value='链接'),patch.dict(os.environ,{'BAR_DRY_RUN':'false'}):
            root=Path(tmp);service,plan,fp=self.fixture(root)
            original=service.plan_store.load(plan.task_id).to_dict()
            with source_reads(plan.source_root) as execution:
                result=service.execute(plan.task_id,fp)
            ledger=json.loads(result.ledger_path.read_text('utf8'))
            with source_reads(plan.source_root) as recovery:
                _validate_completed_hardlink_ledger(plan=plan,ledger=ledger,fingerprint=fp)
            counts=dict(execution=dict(execution),recovery=dict(recovery))
            (root/'source-counts.json').write_text(json.dumps(counts),'utf8');print('EXECUTION_SOURCE_COUNTS='+json.dumps(counts))
            self.assertEqual(service.plan_store.load(plan.task_id).to_dict(),original)
            self.assertEqual(ledger['status'],'completed')
            self.assertEqual(dict(execution),{})
            self.assertEqual(dict(recovery),{})

    def test_source_replacement_never_completes(self):
        with tempfile.TemporaryDirectory() as tmp,patch('src.rename.trans.cm.get_config',return_value='链接'),patch.dict(os.environ,{'BAR_DRY_RUN':'false'}):
            root=Path(tmp);service,plan,fp=self.fixture(root)
            source=plan.entries[0].source_path;source.rename(source.with_suffix('.old'))
            source.write_bytes(b'changed-source')
            with self.assertRaises(Exception):service.execute(plan.task_id,fp)
            self.assertFalse(plan.entries[0].target_path.exists())
            self.assertEqual(source.read_bytes(),b'changed-source')

    def test_recovery_rejects_replaced_target(self):
        from src.rename.execution_recovery import _validate_completed_hardlink_ledger
        with tempfile.TemporaryDirectory() as tmp,patch('src.rename.trans.cm.get_config',return_value='链接'),patch.dict(os.environ,{'BAR_DRY_RUN':'false'}):
            root=Path(tmp);service,plan,fp=self.fixture(root);result=service.execute(plan.task_id,fp)
            ledger=json.loads(result.ledger_path.read_text('utf8'));target=plan.entries[0].target_path
            target.rename(target.with_suffix('.saved'));target.write_bytes(b'synthetic-video')
            with self.assertRaises(ValueError):_validate_completed_hardlink_ledger(plan=plan,ledger=ledger,fingerprint=fp)

    def test_same_entity_subtitle_reuse_needs_no_hash(self):
        from src.rename.asset_collision import build_reuse_resolution,validate_reuse_resolution
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.ass';target=root/'target.ass';source.write_bytes(b'subtitle');os.link(source,target)
            from src.rename.small_file_identity import digest_file
            with patch('src.rename.small_file_identity.digest_file',wraps=digest_file) as digest:
                decision=build_reuse_resolution(source,target)
                validate_reuse_resolution(source,target,decision)
            print('SUBTITLE_HASH_COUNTS='+json.dumps(dict(calls=digest.call_count,bytes=digest.call_count*8)))
            self.assertEqual(digest.call_count,0)
            self.assertEqual(decision.evidence_kind,'hardlink-identity-v1')



    def test_completion_rejects_ledger_identity_not_bound_to_plan(self):
        from src.rename.execution_recovery import _validate_completed_hardlink_ledger
        with tempfile.TemporaryDirectory() as tmp,patch('src.rename.trans.cm.get_config',return_value='链接'),patch.dict(os.environ,{'BAR_DRY_RUN':'false'}):
            root=Path(tmp);service,plan,fp=self.fixture(root);result=service.execute(plan.task_id,fp)
            ledger=json.loads(result.ledger_path.read_text('utf8'))
            ledger['entries'][0]['source_inode']+=1
            with self.assertRaises(ValueError):_validate_completed_hardlink_ledger(plan=plan,ledger=ledger,fingerprint=fp)

    def test_legacy_capture_rejects_source_swap_between_capture_and_link(self):
        from src.rename.trans import Trans
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'file.mkv';target=root/'library'/'file.mkv';source.write_bytes(b'original')
            real_link=os.link
            def changed_source(src,dst,*args,**kwargs):
                source.rename(root/'saved.mkv');source.write_bytes(b'replaced')
                return real_link(src,dst,*args,**kwargs)
            with patch('src.rename.trans.os.link',side_effect=changed_source):
                error=Trans({source:target},'old-task','old-fp',mode='hardlink',dry_run=False,record_root=root/'records').trans_file()
            self.assertIsNotNone(error)
            self.assertFalse(target.exists())
            self.assertEqual(source.read_bytes(),b'replaced')



    def test_movie_catalog_completion_uses_target_identity(self):
        from tests.test_work_catalog import trusted,movie_plan,complete_ledger
        from src.rename.work_catalog import WorkCatalog
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);plan=trusted(movie_plan(root,'work-12345678-1234-1234-1234-123456789abc'))
            ledger=complete_ledger(plan);catalog=WorkCatalog(root/'catalog')
            record=catalog.register_completed(plan,ledger)
            before=json.dumps(record,sort_keys=True)
            with source_reads(plan.entries[0].source_path) as counts:
                self.assertTrue(catalog.materializes_completed_plan(record,plan,ledger))
            self.assertEqual(dict(counts),{})
            self.assertEqual(before,json.dumps(record,sort_keys=True))



    def test_rollback_failure_requires_check(self):
        from src.rename.trans import Trans
        from src.rename.execution_recovery import safe_approved_execution_retry
        with tempfile.TemporaryDirectory() as tmp,patch('src.rename.trans.cm.get_config',return_value='链接'),patch.dict(os.environ,{'BAR_DRY_RUN':'false'}):
            root=Path(tmp);service,plan,fp=self.fixture(root);target=plan.entries[0].target_path
            service._update_task(plan.task_id,workflow_status='queued',queue_operation='execute_approved',execution_requested_fingerprint=fp)
            original_write=Trans._atomic_json_write;original_unlink=Path.unlink;rollback_attempts=[]
            def write_after_link(path,payload):
                if any(row.get('status')=='created' for row in payload.get('entries',())):
                    raise OSError('synthetic ledger write failure after hardlink')
                return original_write(path,payload)
            def deny_target(path,*a,**kw):
                if path==target:
                    rollback_attempts.append(path)
                    raise PermissionError('synthetic rollback denied')
                return original_unlink(path,*a,**kw)
            with patch.object(Trans,'_atomic_json_write',side_effect=write_after_link),patch.object(Path,'unlink',deny_target):
                with self.assertRaises(Exception):service.execute(plan.task_id,fp)
            self.assertEqual(rollback_attempts,[target])
            ledger=json.loads((service.record_root/(plan.task_id+'.ledger.json')).read_text('utf8'))
            self.assertEqual(ledger['status'],'rollback_failed')
            self.assertEqual(ledger['entries'][0]['status'],'rollback_failed')
            self.assertIn('; rollback:',ledger['entries'][0]['error'])
            task=service._read_task(plan.task_id)
            self.assertEqual(task['execution_requested_fingerprint'],fp)
            self.assertEqual(task['queue_operation'],'execute_approved')
            self.assertEqual(task['workflow_status'],'failed')
            self.assertEqual(task['execution_phase'],'media_boundary_entered')
            self.assertTrue(task['execution_requires_manual_check'])
            self.assertTrue(task['execution_may_have_written_targets'])
            self.assertFalse(safe_approved_execution_retry(task))
            self.assertTrue(safe_approved_execution_retry(dict(task,execution_phase='failed_before_media',execution_requires_manual_check=False,execution_may_have_written_targets=False)))
            self.assertTrue(target.samefile(plan.entries[0].source_path))

    def test_legacy_capture_rejects_reparse_ancestor(self):
        from src.rename.trans import Trans
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);parent=root/'source';parent.mkdir();source=parent/'source.mkv';source.write_bytes(b'outside')
            target=root/'library'/'target.mkv';original=Path.lstat
            def reparse(path,*a,**kw):
                value=original(path,*a,**kw)
                if path==parent:
                    frame=sys._getframe(1)
                    while frame:
                        filename=frame.f_code.co_filename.replace('\\','/')
                        if filename.endswith('/runner.py'):return value
                        if '/runtime/src/' in filename:return SimpleNamespace(st_mode=value.st_mode,st_file_attributes=0x400)
                        frame=frame.f_back
                return value
            with patch.object(Path,'lstat',reparse):
                error=Trans({source:target},'reparse','fp',mode='hardlink',dry_run=False,record_root=root/'records').trans_file()
            self.assertIsNotNone(error)
            self.assertFalse(target.exists())

class ExecutionQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_preserves_no_media_outcome(self):
        import asyncio
        from types import SimpleNamespace
        from tests.test_task_queue import _isolated_manager
        from src.rename.execution_recovery import safe_approved_execution_retry
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);task_id='queued-no-media';fp='a'*64
            (root/f'{task_id}.json').write_text(json.dumps(dict(uuid=task_id,workflow_status='approved')),'utf8')
            class Service:
                def validate_approved(self,*args):return SimpleNamespace(execution_policy=SimpleNamespace(dry_run=False))
            async def execute(job):
                manager._update(task_id, workflow_status='failed',execution_status='failed',execution_phase='failed_without_media_changes',
                    execution_requires_manual_check=False,execution_may_have_written_targets=False)
                raise RuntimeError('source missing; no media remains')
            manager=_isolated_manager(task_root=root,execution_service=Service(),approved_executor=execute)
            try:
                await manager.enqueue_approved(task_id,fp);await asyncio.wait_for(manager._queue.join(),10)
                saved=manager._read(task_id)
                self.assertEqual(saved['execution_phase'],'failed_without_media_changes')
                self.assertFalse(saved['execution_requires_manual_check'])
                self.assertTrue(safe_approved_execution_retry(saved))
            finally:await manager.stop()

    async def test_actual_queue_missing_source_retry_same_task(self):
        import asyncio
        from tests.test_task_queue import _isolated_manager
        from src.rename.execution_recovery import safe_approved_execution_retry
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'BAR_DRY_RUN':'false'}):
            root=Path(tmp);service,plan,fp=ExecutionInventoryTests('runTest').fixture(root)
            source=plan.entries[0].source_path;saved=source.with_suffix('.saved');source.rename(saved)
            async def execute(job):return service.execute(job.task_id,job.expected_fingerprint)
            manager=_isolated_manager(task_root=service.task_root,execution_service=service,approved_executor=execute)
            try:
                await manager.enqueue_approved(plan.task_id,fp);await asyncio.wait_for(manager._queue.join(),10)
                failed=manager._read(plan.task_id)
                self.assertEqual(failed['execution_phase'],'failed_without_media_changes')
                self.assertTrue(safe_approved_execution_retry(failed))
                saved.rename(source)
                await manager.enqueue_approved(plan.task_id,fp);await asyncio.wait_for(manager._queue.join(),10)
                self.assertEqual(manager._read(plan.task_id)['execution_status'],'completed')
                self.assertTrue(os.path.samefile(source,plan.entries[0].target_path))
            finally:await manager.stop()

    async def test_generated_nfo_residual_never_allows_safe_retry(self):
        from tests.test_episode_range_contract import EpisodeRangeContracts,merged_package
        from src.rename.generated_outputs import approved_outputs
        from src.rename.plan_review import entry_id
        from src.rename.plan_store import PlanStore
        from src.rename.trans import Trans
        helper=EpisodeRangeContracts('runTest')
        async with helper.flow(automatic=False,configure_source=merged_package) as flow:
            plan,state=await helper.selected(flow)
            chosen=next(e for e in plan.entries if '[03-05]' in e.source_path.name and e.media_kind=='video')
            plan=flow.service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),expected_revision=state.revision,
                mode='range',edit=dict(entry_id=entry_id(chosen),season=1,start=3,end=5,naming_mode='bangumi'))
            from types import SimpleNamespace
            from tests._media_flow_helpers import MediaFlow
            from src.rename.execution_recovery import safe_approved_execution_retry
            result=SimpleNamespace(plan=plan)
            with patch('src.rename.trans.os.link',side_effect=OSError('synthetic link failure')):
                task,ledger,service=await MediaFlow('runTest')._approve_and_execute(root=flow.root,result=result)
            self.assertEqual(task['execution_phase'],'media_boundary_entered')
            self.assertTrue(task['execution_requires_manual_check'])
            self.assertTrue(task['execution_may_have_written_targets'])
            self.assertEqual(task['queue_operation'],'execute_approved')
            self.assertFalse(safe_approved_execution_retry(task))
            self.assertTrue(any(Path(r['target']).exists() for r in result.plan.metadata['generated_outputs']['files']))


class ReuseRecoveryTests(unittest.TestCase):
    def legacy_reuse(self,root):
        from src.rename.rename_plan import RenamePlan,PlanEntry,CollisionResolution
        from src.rename.projection_authority import SourceIdentity
        from src.rename.media_file_identity import frozen_sources
        from src.rename.plan_store import PlanStore
        from src.rename.trans import Trans
        import hashlib
        source=root/'source'/'episode.ass';source.parent.mkdir();source.write_bytes(b'subtitle')
        target=root/'library'/'episode.ass';target.parent.mkdir();target.write_bytes(b'subtitle')
        digest=hashlib.sha256(b'subtitle').hexdigest();st=target.stat()
        collision=CollisionResolution('reuse_existing',8,digest,8,digest,st.st_dev,st.st_ino)
        plan=RenamePlan('legacy-small',source.parent,(PlanEntry(source,target,media_kind='subtitle',role='subtitle',collision_resolution=collision),),metadata={'source_identity_manifest':[SourceIdentity.from_path(source,inventory_fingerprint='saved').to_dict()]})
        fp=PlanStore.fingerprint(plan)
        transfer=Trans(plan.as_mapping(),plan.task_id,fp,mode='hardlink',dry_run=False,record_root=root/'records',source_identities=frozen_sources(plan),operations=[dict(source=str(source),target=str(target),action='reuse_existing',collision_resolution=collision.to_dict())])
        self.assertIsNone(transfer.trans_file())
        ledger=json.loads((root/'records'/(plan.task_id+'.ledger.json')).read_text('utf8'))
        return plan,ledger,fp


    def test_existing_reuse_does_not_require_same_volume(self):
        from dataclasses import replace
        from src.rename.asset_collision import build_reuse_resolution
        from src.rename.media_file_identity import media_identity
        from src.rename.trans import Trans
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.ass';target=root/'target.ass'
            source.write_bytes(b'subtitle');target.write_bytes(b'subtitle')
            resolution=build_reuse_resolution(source,target)
            # 冻结来源设备模拟异卷，真实目标内容复用不得调用硬链接。
            identity=replace(media_identity(source),device=target.stat().st_dev+1)
            transfer=Trans({source:target},'cross-volume-reuse','saved-fp',mode='hardlink',dry_run=False,record_root=root/'records',source_identities={source:identity},operations=[dict(source=str(source),target=str(target),action='reuse_existing',collision_resolution=resolution.to_dict())])
            with patch.object(transfer,'_existing_parent_device',wraps=transfer._existing_parent_device) as parent_device,patch('src.rename.trans.os.link',wraps=os.link) as link,source_reads(source) as counts:
                self.assertIsNone(transfer.trans_file())
            self.assertEqual(dict(counts),{});parent_device.assert_not_called();link.assert_not_called()
            self.assertEqual(source.read_bytes(),b'subtitle');self.assertEqual(target.read_bytes(),b'subtitle')

    def test_new_hardlink_still_rejects_different_volume(self):
        from src.rename.media_file_identity import media_identity
        from src.rename.trans import Trans
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.ass';target=root/'new.ass';source.write_bytes(b'subtitle')
            identity=media_identity(source)
            transfer=Trans({source:target},'cross-volume-create','saved-fp',mode='hardlink',dry_run=False,record_root=root/'records',source_identities={source:identity})
            with patch.object(transfer,'_existing_parent_device',return_value=identity.device+1),patch('src.rename.trans.os.link',wraps=os.link) as link:
                self.assertIn('不在同一文件系统',transfer.trans_file())
            link.assert_not_called();self.assertFalse(target.exists());self.assertEqual(source.read_bytes(),b'subtitle')
