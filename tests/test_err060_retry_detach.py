"""Real temporary movie retry/detach boundaries; never operate user media."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from tests.test_err060_role_entry import movie_dialog, change_content
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.movie_assets import inventory_movie_assets, MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION
from src.rename.projection_authority import RecognitionDecisionInput
from src.rename.recovery_transaction import CatalogDetachService, RecoveryTransactionStore
from src.rename.work_catalog import WORK_CATALOG_SCHEMA_VERSION
from src.rename import process
from src.rename.decision_state import DecisionStateStore


def business_bytes(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and 'transactions' not in p.relative_to(root).parts and p.suffix != '.lock'}


class DisconnectedProjectionStore(DecisionStateStore):
    """Fault at the injected public projection boundary, never a private spy."""
    def __init__(self, root):
        super().__init__(root)
        self.projection_calls = 0

    def project_plan(self, state, plan):
        if plan.metadata.get('recovery_reason') == 'catalog_detached':
            self.projection_calls += 1
            raise ValueError('canonical projection disconnected')
        return super().project_plan(state, plan)


class CheckpointCrashStore(RecoveryTransactionStore):
    """Exercise durable-store failure through the service constructor dependency."""
    crash_enabled = True

    def save(self, transaction):
        if self.crash_enabled and transaction.phase == 'catalog_archived' and transaction.prepared_plan_fingerprint:
            raise OSError('simulated checkpoint crash')
        return super().save(transaction)


def retry(dialog, root):
    service=dialog.review_service
    worker=process.Rename.__new__(process.Rename)
    worker.active_provider_selection=dialog.plan.work_identity.primary_provider.to_dict()
    worker.active_linked_provider_identities=()
    worker.decision_state_root=service.decision_state_store.root
    worker.ANIME_MOVIE_PATH=root/'library'/'AnimeMovies'
    worker.MOVIE_PATH=root/'library'/'Movies'
    worker.ANIME_PATH=root/'library'/'Anime'
    worker.BANGUMI_PATH=root/'library'/'TV'
    config=dict(anime_movie_path=str(worker.ANIME_MOVIE_PATH),movie_path=str(worker.MOVIE_PATH),
        anime_path=str(worker.ANIME_PATH),bangumi_path=str(worker.BANGUMI_PATH))
    with patch.multiple(process, TASK_PATH=service.task_root, PLAN_PATH=service.plan_store.root,
                        cm=SimpleNamespace(get_config=config.get)), patch.dict(os.environ, BAR_DRY_RUN='false'):
        return worker._movie_inventory_reply(dialog.plan.task_id, dialog.plan.source_root, True, 'Movie',
            inventory_movie_assets(dialog.plan.source_root), MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION,
            provider_info=dict(id=dialog.plan.work_identity.primary_provider.provider_id,title='Movie',release_date='2026-01-01'))


def detach_service(dialog, root, *, transaction_store=None, decision_store=None):
    svc=dialog.review_service
    key=dialog.task_decision.movie_version_selection.to_dict()['work_key']
    svc.work_catalog.root.mkdir(exist_ok=True)
    payload=dict(schema_version=WORK_CATALOG_SCHEMA_VERSION,work_key=key,revision=1,title='Movie',year=2026,
        domain='animation',group_kind='movie_continuous',canonical_root=dialog.plan.metadata['canonical_work_root'],
        aliases=[],providers=[dialog.plan.work_identity.primary_provider.to_dict()],versions=[],assets=[])
    (svc.work_catalog.root/f'{key}.json').write_text(json.dumps(payload),encoding='utf-8')
    return CatalogDetachService(transaction_store=transaction_store or RecoveryTransactionStore(root/'transactions'),
        decision_store=decision_store or svc.decision_state_store,plan_store=svc.plan_store,work_catalog=svc.work_catalog,
        series_catalog=svc.series_catalog,task_root=svc.task_root), key


class RetryDetachContract(unittest.TestCase):
    def test_corrupt_nonce_is_rejected_before_any_witness_write(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); dialog,_=movie_dialog(root)
            service,key=detach_service(dialog,root)
            transaction=service._prepare(dialog.plan.task_id,catalog_backend='work',catalog_key=key)
            service._archive_catalog(transaction)
            payload={**transaction.to_dict(),'phase':'catalog_archived','recovery_nonce':'x/../../../escaped-witness'}
            service.transaction_store.path_for(dialog.plan.task_id).write_text(json.dumps(payload),encoding='utf-8')
            with self.assertRaises((ValueError,RuntimeError)):
                service.execute(dialog.plan.task_id,catalog_backend='work',catalog_key=key)
            self.assertFalse((root/'escaped-witness.json').exists())
            self.assertFalse((service.transaction_store.root/'witness').exists())

    def test_pending_reselection_discards_old_movie_selection(self):
        from src.rename.projection_authority import ProviderReselectionPendingInput, commit_provider_reselection_pending
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); dialog,_=movie_dialog(root)
            service=dialog.review_service
            context=service._completion_context_for_current(dialog.plan)
            draft=service._load_inventory_draft(dialog.plan.task_id,context.origin_plan_fingerprint)
            state,plan=commit_provider_reselection_pending(service.decision_state_store,service.plan_store,
                ProviderReselectionPendingInput(dialog.plan.task_id,dialog.task_decision.revision),draft,
                task_terminal=None)
            self.assertIsNone(state.movie_version_selection)
            self.assertFalse(state.confirmed_work)
            self.assertNotIn('movie_version_selection',plan.metadata)
            self.assertFalse(service.load_or_create(plan).approved_entry_ids)

    def test_reconcile_and_cancel_missing_task_do_not_recreate_it(self):
        for method in ('reconcile_and_execute','cancel_prepared'):
            with self.subTest(method=method),tempfile.TemporaryDirectory() as raw:
                root=Path(raw); dialog,_=movie_dialog(root)
                service,key=detach_service(dialog,root)
                transaction=service._prepare(dialog.plan.task_id,catalog_backend='work',catalog_key=key)
                if method=='reconcile_and_execute':
                    service._archive_catalog(transaction)
                    service.transaction_store.delete(dialog.plan.task_id)
                (service.task_root/f'{dialog.plan.task_id}.json').unlink()
                before=service.decision_store.path_for(dialog.plan.task_id).read_bytes()
                kwargs={'catalog_backend':'work','catalog_key':key} if method=='reconcile_and_execute' else {}
                with self.assertRaises((ValueError,RuntimeError)):
                    getattr(service,method)(dialog.plan.task_id,**kwargs)
                self.assertFalse((service.task_root/f'{dialog.plan.task_id}.json').exists())
                self.assertEqual(before,service.decision_store.path_for(dialog.plan.task_id).read_bytes())

    def test_recovery_ui_requires_confirmation_and_calls_audit_only_service(self):
        from src.pages import recovery_records_page as page
        from tests.test_err060_role_entry import Node
        class Widget(Node):
            def style(self,*args): return self
            def open(self): pass
            def close(self): pass
        class UI:
            def __init__(self):
                self.buttons={}; self.checks=[]; self.messages=[]
                self.context=SimpleNamespace(client=SimpleNamespace(content=Widget()))
            def dialog(self): return Widget()
            def card(self): return Widget()
            def label(self,text): return Widget()
            def checkbox(self,text):
                node=Widget(False); self.checks.append(node); return node
            def button(self,text,**kw): self.buttons[text]=kw.get('on_click'); return Widget()
            def notify(self,text,**kw): self.messages.append(text)
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); dialog,_=movie_dialog(root)
            service,key=detach_service(dialog,root)
            transaction=service._prepare(dialog.plan.task_id,catalog_backend='work',catalog_key=key)
            service._archive_catalog(transaction)
            service.transaction_store.save(replace(transaction,phase='catalog_archived'))
            (service.task_root/f'{dialog.plan.task_id}.json').unlink()
            fake=UI()
            before=business_bytes(root)
            with patch.multiple(page,ui=fake, TASK_PATH=service.task_root,
                    RECOVERY_TRANSACTION_PATH=service.transaction_store.root,
                    DECISION_STATE_PATH=service.decision_store.root, PLAN_PATH=service.plan_store.root,
                    WORK_CATALOG_PATH=service.work_catalog.root, SERIES_CATALOG_PATH=service.series_catalog.root):
                page.show_recovery_records()
                fake.buttons['安全结束']()
                self.assertIsNotNone(service.transaction_store.load_optional(dialog.plan.task_id))
                fake.checks[0].value=True
                fake.buttons['安全结束']()
                self.assertIsNone(service.transaction_store.load_optional(dialog.plan.task_id))
                self.assertTrue(any('已安全结束' in m for m in fake.messages))
            self.assertEqual(before,business_bytes(root))
            self.assertFalse((service.task_root/f'{dialog.plan.task_id}.json').exists())

    def test_role_edit_after_retry_keeps_new_execution_policy(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); dialog,videos=movie_dialog(root)
            self.assertEqual('need_confirm',retry(dialog,root))
            dialog.plan=dialog.review_service.plan_store.load(dialog.plan.task_id)
            dialog.task_decision=dialog.review_service.decision_state_store.load(dialog.plan.task_id)
            key=entry_id(next(e for e in dialog.plan.entries if e.source_path==videos[2]))
            change_content(dialog,[videos[2]])
            self.assertFalse(dialog.plan.execution_policy.dry_run)

    def test_queue_remove_guard_precedes_retiring_task_authority(self):
        from src.task_queue import TaskQueueManager
        from src.rename.plan_execution import PlanExecutionService
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); dialog,_=movie_dialog(root)
            service,key=detach_service(dialog,root)
            service._prepare(dialog.plan.task_id,catalog_backend='work',catalog_key=key)
            manager=TaskQueueManager(task_root=service.task_root,plan_root=service.plan_store.root,
                decision_state_root=service.decision_store.root,recovery_abandon_audit_root=root/'audit',
                execution_service=PlanExecutionService(service.plan_store, dialog.review_service,
                    task_root=service.task_root, record_root=root/'records',
                    catalog_root=service.work_catalog.root, series_catalog_root=service.series_catalog.root,
                    decision_state_root=service.decision_store.root,
                    recovery_transaction_root=service.transaction_store.root,
                    provider_link_catalog_root=root/'provider_links'))
            before=business_bytes(root)
            with self.assertRaisesRegex(RuntimeError,'恢复尚未完成'):
                asyncio.run(manager.abandon_and_remove(dialog.plan.task_id,record_root=root/'records',
                    plan_root=service.plan_store.root,review_root=root/'reviews'))
            self.assertEqual(before,business_bytes(root))
            service.transaction_store.delete(dialog.plan.task_id)
            with self.assertRaisesRegex(RuntimeError,'阶段记录缺失'):
                asyncio.run(manager.abandon_and_remove(dialog.plan.task_id,record_root=root/'records',
                    plan_root=service.plan_store.root,review_root=root/'reviews'))
            self.assertEqual(before,business_bytes(root))
            self.assertTrue((service.task_root/f'{dialog.plan.task_id}.json').exists())

    def test_retry_disconnected_recompletion_does_not_replace_current_plan(self):
        from src.rename import plan_review
        class DisconnectedCompletionService(plan_review.PlanReviewService):
            completion_calls = 0
            def complete_inventory_draft(self, *args, **kwargs):
                type(self).completion_calls += 1
                raise ValueError('canonical completion disconnected')
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); dialog,_=movie_dialog(root)
            store=dialog.review_service.plan_store
            before=store.path_for(dialog.plan.task_id).read_bytes()
            decision=dialog.review_service.decision_state_store.path_for(dialog.plan.task_id).read_bytes()
            with patch.multiple(plan_review,PlanReviewService=DisconnectedCompletionService):
                self.assertNotEqual('need_confirm',retry(dialog,root))
            self.assertEqual(1,DisconnectedCompletionService.completion_calls)
            self.assertEqual(before,store.path_for(dialog.plan.task_id).read_bytes())
            self.assertEqual(decision,dialog.review_service.decision_state_store.path_for(dialog.plan.task_id).read_bytes())

    def test_fresh_source_drift_and_provider_switch_do_not_replay_choices(self):
        for change in ('bytes', 'provider'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as raw:
                root=Path(raw); dialog,videos=movie_dialog(root)
                if change=='bytes':
                    videos[0].write_bytes(b'changed media')
                else:
                    work=dialog.plan.work_identity
                    dialog.plan=replace(dialog.plan,work_identity=replace(work,
                        primary_provider=replace(work.primary_provider,provider_id='101')))
                retry(dialog,root)
                state=dialog.review_service.decision_state_store.load(dialog.plan.task_id)
                self.assertIsNone(state.movie_version_selection)
                plan=dialog.review_service.plan_store.load(dialog.plan.task_id)
                self.assertEqual('inventory_review_draft',plan.metadata['mapping_source'])

    def test_committed_detach_before_phase_checkpoint_resumes(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); dialog,_=movie_dialog(root)
            crash_store=CheckpointCrashStore(root/'transactions')
            service,key=detach_service(dialog,root,transaction_store=crash_store)
            with self.assertRaisesRegex(OSError,'simulated checkpoint crash'):
                service.execute(dialog.plan.task_id,catalog_backend='work',catalog_key=key)
            crash_store.crash_enabled=False
            decision,plan,transaction=service.execute(dialog.plan.task_id,catalog_backend='work',catalog_key=key)
            self.assertEqual('completed',transaction.phase)
            self.assertEqual('catalog_detached',plan.metadata['recovery_reason'])

    def test_orphan_rejects_active_task_wrong_fingerprint_and_retains_all_other_data(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); dialog,_=movie_dialog(root)
            service,key=detach_service(dialog,root)
            transaction=service._prepare(dialog.plan.task_id,catalog_backend='work',catalog_key=key)
            service._archive_catalog(transaction)
            service.transaction_store.save(replace(transaction,phase='catalog_archived'))
            import hashlib
            fingerprint=hashlib.sha256(service.transaction_store.path_for(dialog.plan.task_id).read_bytes()).hexdigest()
            with self.assertRaises(RuntimeError):
                service.archive_orphaned_transaction(dialog.plan.task_id,expected_fingerprint=fingerprint)
            (service.task_root/f'{dialog.plan.task_id}.json').unlink()
            before={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file() and 'transactions' not in p.parts and p.suffix != '.lock'}
            with self.assertRaises(RuntimeError):
                service.archive_orphaned_transaction(dialog.plan.task_id,expected_fingerprint='0'*64)
            service.archive_orphaned_transaction(dialog.plan.task_id,expected_fingerprint=fingerprint)
            service.archive_orphaned_transaction(dialog.plan.task_id,expected_fingerprint=fingerprint)
            after={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file() and 'transactions' not in p.parts and p.suffix != '.lock'}
            self.assertEqual(before,after)

    def test_actual_inventory_retry_preserves_choices_and_updates_policy(self):
        for segments in (False, True):
            with self.subTest(segments=segments), tempfile.TemporaryDirectory() as raw:
                root=Path(raw); dialog,videos=movie_dialog(root,segments=segments)
                key=entry_id(next(e for e in dialog.plan.entries if e.source_path==videos[2]))
                change_content(dialog,[videos[2]])
                before=dialog.plan
                result=retry(dialog,root)
                self.assertEqual('need_confirm',result)
                revised=dialog.review_service.plan_store.load(before.task_id)
                self.assertNotEqual(PlanStore.fingerprint(before),PlanStore.fingerprint(revised))
                self.assertFalse(revised.execution_policy.dry_run)
                self.assertEqual(before.metadata['movie_version_selection'],revised.metadata['movie_version_selection'])
                self.assertEqual([(e.source_path,e.target_path) for e in before.entries],
                                 [(e.source_path,e.target_path) for e in revised.entries])
                review=dialog.review_service.load_or_create(revised)
                self.assertEqual('draft',review.status.value)
                self.assertFalse(review.approved_entry_ids)

    def test_identity_change_does_not_inherit_movie_choice(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); dialog,_=movie_dialog(root)
            work=dialog.plan.work_identity
            other=replace(work,primary_provider=replace(work.primary_provider,provider_id='other'))
            candidate=RecognitionDecisionInput(dialog.plan.task_id,other).candidate(dialog.task_decision)
            self.assertIsNone(candidate.movie_version_selection)

    def test_movie_detach_and_archived_restart_preserve_choices(self):
        for archived in (False,True):
            with self.subTest(archived=archived),tempfile.TemporaryDirectory() as raw:
                root=Path(raw); dialog,_=movie_dialog(root)
                service,key=detach_service(dialog,root)
                before=dialog.task_decision.movie_version_selection.to_dict()
                if archived:
                    transaction=service._prepare(dialog.plan.task_id,catalog_backend='work',catalog_key=key)
                    service._archive_catalog(transaction)
                    service.transaction_store.save(replace(transaction,phase='catalog_archived'))
                try:
                    decision,plan,transaction=service.execute(dialog.plan.task_id,catalog_backend='work',catalog_key=key)
                except ValueError as exc:
                    self.fail(f'movie detach must complete: {exc}')
                self.assertEqual('completed',transaction.phase)
                self.assertEqual('catalog_detached',plan.metadata['recovery_reason'])
                self.assertEqual(before['groups'],decision.movie_version_selection.to_dict()['groups'])
                self.assertEqual(before['excluded_sources'],decision.movie_version_selection.to_dict()['excluded_sources'])
                self.assertEqual('work-' + transaction.recovery_nonce,decision.movie_version_selection.to_dict()['work_key'])
                self.assertNotEqual(key,decision.movie_version_selection.to_dict()['work_key'])
                again=service.execute(plan.task_id,catalog_backend='work',catalog_key=key)
                self.assertEqual(transaction,again[2])

    def test_projection_failure_does_not_archive_catalog(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); dialog,_=movie_dialog(root)
            fault_store=DisconnectedProjectionStore(dialog.review_service.decision_state_store.root)
            service,key=detach_service(dialog,root,decision_store=fault_store)
            path=service.work_catalog.root/f'{key}.json'; before=path.read_bytes()
            with self.assertRaisesRegex(ValueError,'canonical projection disconnected'):
                service.execute(dialog.plan.task_id,catalog_backend='work',catalog_key=key)
            self.assertEqual(1,fault_store.projection_calls)
            self.assertTrue(path.exists(),'projection preflight must happen before catalog archive')
            self.assertEqual(before,path.read_bytes())

    def test_archived_resume_rejects_missing_tombstone_and_changed_decision_before_projection(self):
        for damage in ('tombstone','decision'):
            with self.subTest(damage=damage),tempfile.TemporaryDirectory() as raw:
                root=Path(raw); dialog,_=movie_dialog(root)
                fault_store=DisconnectedProjectionStore(dialog.review_service.decision_state_store.root)
                service,key=detach_service(dialog,root,decision_store=fault_store)
                transaction=service._prepare(dialog.plan.task_id,catalog_backend='work',catalog_key=key)
                service._archive_catalog(transaction)
                service.transaction_store.save(replace(transaction,phase='catalog_archived'))
                if damage=='tombstone':
                    next((service.work_catalog.root/'.deleted').glob('*.json')).unlink()
                else:
                    state=service.decision_store.load(dialog.plan.task_id)
                    service.decision_store.save(replace(state,manual_role_decisions={'unrelated': 'extra'}),expected_revision=state.revision)
                before=business_bytes(root)
                with self.assertRaises((ValueError,RuntimeError)):
                    service.execute(dialog.plan.task_id,catalog_backend='work',catalog_key=key)
                self.assertEqual(0,fault_store.projection_calls)
                self.assertEqual(before,business_bytes(root))

    def test_orphan_recovery_has_explicit_safe_archive_without_task_recreation(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); dialog,_=movie_dialog(root)
            service,key=detach_service(dialog,root)
            transaction=service._prepare(dialog.plan.task_id,catalog_backend='work',catalog_key=key)
            service._archive_catalog(transaction)
            transaction=replace(transaction,phase='catalog_archived')
            service.transaction_store.save(transaction)
            (service.task_root/f'{dialog.plan.task_id}.json').unlink(missing_ok=True)
            close=getattr(service,'archive_orphaned_transaction',None)
            self.assertTrue(callable(close),'orphan must have an explicit safe audit exit')
            before=service.decision_store.path_for(dialog.plan.task_id).read_bytes()
            expected=service.transaction_store.path_for(dialog.plan.task_id).read_bytes()
            import hashlib
            close(dialog.plan.task_id,expected_fingerprint=hashlib.sha256(expected).hexdigest())
            self.assertFalse((service.task_root/f'{dialog.plan.task_id}.json').exists())
            self.assertEqual(before,service.decision_store.path_for(dialog.plan.task_id).read_bytes())
            self.assertIsNone(service.transaction_store.load_optional(dialog.plan.task_id))
            self.assertEqual(1,len(list((service.transaction_store.root/'abandoned').glob('*.json'))))


class MovieQueueRetryContract(unittest.IsolatedAsyncioTestCase):
    async def test_real_queue_reidentify_commits_movie_retry_and_pending_outcome(self):
        from tests.test_default_activation_contract import DefaultActivationProducerContracts
        from tests.test_r22_revised_c_c0_red_contract import _run_boundary_incident
        harness = DefaultActivationProducerContracts('runTest')
        for mode in ('selected', 'retry', 'workspace'):
            async with harness.flow(movie=True) as flow:
                plan, state = harness.current(flow)
                def protected():
                    return {str(p.relative_to(flow.root)): p.read_bytes() for p in flow.root.rglob('*')
                            if p.is_file() and p.suffix != '.lock'}
                before = protected()
                from src.rename.plan_execution import PlanExecutionError
                selection = plan.work_identity.primary_provider.to_dict()
                with self.subTest(mode=mode), patch.object(flow.manager, '_prepare_reidentify_job',
                        wraps=flow.manager._prepare_reidentify_job) as legacy_job:
                    if mode == 'selected':
                        operation = flow.manager.enqueue_reidentify(flow.task_id, selection, state.revision)
                    elif mode == 'retry':
                        operation = flow.manager.retry_current(flow.task_id)
                    else:
                        current = flow.manager._read(flow.task_id)
                        operation = flow.manager.reenqueue_workspace(flow.task_id, flow.source,
                            is_anime=current.get('is_anime'), is_movie=current.get('is_movie'),
                            custom_name=current.get('name'), custom_season_id=current.get('season_id'),
                             preview_only=current.get('preview_only', False))
                    with self.assertRaisesRegex(PlanExecutionError, '安全结束.*重新添加'):
                        await operation
                    legacy_job.assert_not_called()
                    self.assertEqual(before, protected())
                    self.assertTrue(flow.manager._queue.empty())
                self.assertEqual(state.movie_version_selection,
                                 flow.service.decision_state_store.load(flow.task_id).movie_version_selection)
                self.assertFalse(flow.service.load_or_create(plan).approved_entry_ids)
        # A separate genuine TV admission, not changing an admitted movie's
        # type flags to fabricate a pending TV result.
        with tempfile.TemporaryDirectory() as raw:
            result = await _run_boundary_incident(Path(raw), 'kaguya')
            state = result.manager.decision_state_store.load(result.task_id)
            self.assertIsNone(state.movie_version_selection)
            self.assertFalse(state.confirmed_work)
            self.assertIsNotNone(state.pending_tv_selection)
            self.assertEqual('need_confirm', result.task['workflow_status'])
            self.assertFalse(result.business_json or result.target_files)
            self.assertTrue(result.source_unchanged)



if __name__=='__main__': unittest.main()
