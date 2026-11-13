"""Exact-anchor retry must reach bounded cohort confirmation, not remote ambiguity."""
import asyncio
import copy
from contextlib import ExitStack
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import src.rename.provider_link_decision as decision
from src.rename.provider_search import ProviderSearchError
from src.rename.review_operation import ProviderSelectionAwaitingUserOutcome
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_execution import PlanExecutionError
from src.rename.plan_store import PlanStore
from src.pages.review_operation_view import ReviewCommandApplyResult
from src.utils.utils import read_task_at
import tests.test_provider_subject_plan_wiring_red_contract as flow
from tests.test_r24_phase_d_f7_f8_remediation_contract import _provider_failure, _worker_for_failure
from tests.test_r24_phase_c_review_operation_migration_contract import current_provider_selection_context
from tests.test_r24_yama_special_order_dependence_incident_red_contract import _apply_boundary, _configured_result_roots


def failure(selected='248175'):
    evidence = _provider_failure(live_conflict=True)
    evidence['selected_subject_id'] = selected
    batch = evidence['subject_observations']
    batch['selected_subject_id'] = selected
    batch.pop('evidence_fingerprint', None)
    batch['evidence_fingerprint'] = flow._sha256_json(batch)
    evidence['evidence_fingerprint'] = batch['evidence_fingerprint']
    return evidence


class WorkerContract(unittest.TestCase):
    def test_exact_anchor_and_no_match_both_offer_bounded_confirmation(self):
        for selected in ('248175', '293049', '317613'):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as tmp:
                worker = _worker_for_failure(failure(selected), Path(tmp))
                outcome = worker._resolve_bangumi_provider_selection({
                    'namespace':'bangumi','entity_type':'subject','provider_id':selected})
                self.assertIsInstance(outcome, ProviderSelectionAwaitingUserOutcome)
                e = outcome.provider_subject_evidence
                segment = decision.resolve_provider_subject_cohort_segment(
                    selected_subject_id=selected, subject_observations=e['subject_observations'],
                    bangumi_subjects=e['bangumi_subjects'], bangumi_relations=e['bangumi_relations'])
                self.assertEqual({'248175':1,'293049':2,'317613':3,'425211':4,'565802':5}, dict(segment.resolution.season_assignments))
                self.assertEqual('', segment.anchor_tmdb_tv_id)

    def test_edge_tampering_blocks_exact_anchor(self):
        # Canonical disconnection is covered by the retained F7/F8 worker
        # control; these exact-anchor cases cover the newly admitted input.
        for mode in ('anchor', 'fingerprint', 'endpoint'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                e = failure()
                worker = _worker_for_failure(e, Path(tmp))
                original = decision.freeze_provider_subject_boundary_edges
                def corrupt(**kwargs):
                    edges = [dict(x) for x in original(**kwargs)]
                    self.assertEqual(1,len(edges))
                    if mode == 'anchor': edges[0]['anchor_tmdb_tv_id']='99999'
                    if mode == 'fingerprint': edges[0]['anchor_evidence_fingerprints']={'248175':'a'*64}
                    if mode == 'endpoint': edges[0]['to_subject_id']='425211'
                    return tuple(edges)
                target = patch('src.rename.process.freeze_provider_subject_boundary_edges',side_effect=corrupt)
                with target, self.assertRaises(ProviderSearchError):
                    worker._resolve_bangumi_provider_selection({'namespace':'bangumi','entity_type':'subject','provider_id':'248175'})

    def test_optional_tmdb_failure_preserves_bangumi_confirmation(self):
        for mode in ('transport','truncated'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                e=failure()
                row=e['subject_observations']['subject_observations'][0]
                query=row['queries'][0]
                if mode == 'transport': query.update(outcome='transport_error',candidates=[])
                else: query['truncated']=True
                worker=_worker_for_failure(e,Path(tmp))
                outcome=worker._resolve_bangumi_provider_selection({'namespace':'bangumi','entity_type':'subject','provider_id':'248175'})
                self.assertIsInstance(outcome, ProviderSelectionAwaitingUserOutcome)
                self.assertFalse(outcome.provider_subject_evidence['retry_requires_refresh'])


class RealEntryContract(unittest.IsolatedAsyncioTestCase):
    async def test_add_confirm_then_retry_restart_invalidates_old_approval(self):
        e=failure()
        rows={s['bangumi_subject_id']:s for s in e['subject_observations']['subject_observations']}
        subjects={str(s['provider_id']):dict(s,id=int(s['provider_id']),name_cn=s['title'],
            name=s.get('original_title',s['title']),date=s['air_date'],eps=s['episode_count'],type=2,platform='TV')
            for s in e['bangumi_subjects']}
        relations={key:[] for key in subjects}
        for rel in e['bangumi_relations']:
            if rel['from'] not in subjects or rel['to'] not in subjects:
                continue  # Fixture also includes unrelated non-TV special edges.
            relations[rel['from']].append((rel['to'],rel['relation']))
        original_search = flow._ProviderSearch
        class FrozenSearch(original_search):
            def get_tv_info(self, query, _year=None):
                for row in rows.values():
                    for request in row['queries']:
                        if request['query'] == query and request['candidates']:
                            candidate=request['candidates'][0]
                            value={
                                'id':int(candidate['tmdb_tv_id']), 'name':candidate['title'],
                                'original_name':candidate['original_title'],
                                'first_air_date':candidate['first_air_date'],
                                'genres':[{'id':16,'name':'Animation'}],
                                'seasons':copy.deepcopy(candidate['seasons']),
                                '_bar_search_alternatives':[{'id':int(candidate['tmdb_tv_id']),
                                    'name':candidate['title'],'original_name':candidate['original_title'],
                                    'first_air_date':candidate['first_air_date'],'genre_ids':[16]}],
                            }
                            return value['name'], value
                return '',None
        # Inject the external search collaborator, not Rename's private
        # observation method; real matching and observation freezing now run.
        with tempfile.TemporaryDirectory() as tmp, patch('tests.test_provider_subject_plan_wiring_red_contract._ProviderSearch',FrozenSearch):
            root=Path(tmp)/'flow'
            source=flow._make_episode_source(Path(tmp)/'source','Kaguya Season 1',tuple(range(1,13)))
            source_before=flow._source_manifest(source)
            harness=flow.ProviderSubjectPlanWiringRedContracts(methodName='runTest')
            with patch.dict(os.environ,{'BAR_DRY_RUN':'1'}):
                result=await harness._run_real_add_select(root=root,source=source,
                    subjects=subjects,relations=relations,selected_subject_id='248175')
            manager=result.manager
            def reload():
                result.task=read_task_at(result.tasks,result.task_id)
                result.plan=manager.plan_store.load(result.task_id)
                return result.plan
            self.assertEqual('awaiting_user',current_provider_selection_context(result.task)['state'])
            self.assertEqual(ReviewCommandApplyResult.APPLIED,_apply_boundary(result))
            old=reload()
            self.assertEqual(1,old.metadata['season_number'])
            self.assertEqual('248175',old.work_identity.primary_provider.provider_id)
            service=PlanReviewService(manager.plan_store,result.reviews,decision_state_root=result.decisions,task_root=result.tasks)
            old_fp=PlanStore.fingerprint(old)
            self.assertEqual('approved',service.approve_entries(result.task_id,old_fp,[entry_id(x) for x in old.entries]).status)
            lineage=result.task['task_source_scope']
            with ExitStack() as stack:
                stack.enter_context(patch.dict(flow.production_fixture.YAMA_SUBJECTS,subjects,clear=True))
                stack.enter_context(patch.dict(flow.production_fixture.YAMA_RELATIONS,relations,clear=True))
                stack.enter_context(patch('src.task_queue.Rename',return_value=flow._configured_renamer(root)))
                stack.enter_context(_configured_result_roots(result))
                for name,path in {'TASK_PATH':result.tasks,'PLAN_PATH':result.plans,'SERIES_CATALOG_PATH':root/'series_catalog','RECORD_PATH':root/'records'}.items():
                    stack.enter_context(patch(f'src.rename.process.{name}',path))
                await manager.start()
                try:
                    with patch.dict(os.environ,{'BAR_DRY_RUN':'0'}):
                        await manager.retry_current(result.task_id)
                        await asyncio.wait_for(manager._queue.join(),12)
                    reload()
                    # V3 confirmation is an independent relationship fact.
                    # Retry consumes it; it must not prompt for it again.
                    self.assertEqual('succeeded',current_provider_selection_context(result.task)['state'])
                    await manager.stop()
                    await manager.start()
                    reload()
                    self.assertEqual('succeeded',current_provider_selection_context(result.task)['state'])
                    with self.assertRaisesRegex(PlanExecutionError,'Plan 版本已变化'):
                        manager.execution_service.validate_approved(result.task_id,old_fp)
                    new=reload()
                    # Retry freezes current policy in a new, unapproved plan.
                    self.assertFalse(new.execution_policy.dry_run)
                    self.assertEqual(replace(old.execution_policy, dry_run=False).to_dict(),
                                     new.execution_policy.to_dict())
                    self.assertNotEqual(old_fp,PlanStore.fingerprint(new))
                    self.assertEqual('248175',new.work_identity.primary_provider.provider_id)
                    self.assertEqual(1,new.metadata['season_number'])
                    self.assertEqual(lineage,result.task['task_source_scope'])
                    with self.assertRaises(PlanExecutionError):
                        manager.execution_service.validate_approved(result.task_id,old_fp)
                    self.assertEqual('draft',service.load_or_create(new).status)
                    with self.assertRaises(PlanExecutionError):
                        manager.execution_service.validate_approved(result.task_id,PlanStore.fingerprint(new))
                    self.assertFalse((root/'Anime').exists())
                    self.assertEqual(source_before,flow._source_manifest(source))
                finally:
                    await manager.stop()
