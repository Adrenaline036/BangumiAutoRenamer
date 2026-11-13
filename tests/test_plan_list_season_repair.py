"""Public provider incident, real scope dialogs and confirmation boundaries."""
import copy
import json
from pathlib import Path
import unittest

from src.rename import provider_link_resolver as resolver
from src.rename import provider_link_decision as decision
from src.rename.review_operation import provider_selection_command_is_capable


def fixture():
    return json.loads(Path(__file__).with_name('fixtures').joinpath('ranma_aggregate.json').read_text(encoding='utf-8'))


def rehash(data):
    batch = data['subject_observations']
    for row in batch['subject_observations']:
        row['response_fingerprint'] = resolver._canonical_sha256({k:v for k,v in row.items() if k != 'response_fingerprint'})
    batch['evidence_fingerprint'] = resolver._canonical_sha256({k:v for k,v in batch.items() if k != 'evidence_fingerprint'})
    return data


def arguments(data):
    return dict(selected_subject_id=data['selected_provider']['provider_id'],
                **{k:data[k] for k in ('subject_observations','bangumi_subjects','bangumi_relations')})


class Aggregate(unittest.TestCase):
    def test_real_pair_resolves_and_confirms_from_either_subject(self):
        for selected in ('2789', '65883'):
            with self.subTest(selected=selected):
                data = fixture()
                data['selected_provider']['provider_id'] = selected
                data['subject_observations']['selected_subject_id'] = selected
                rehash(data)
                before = copy.deepcopy(data)
                resolved = resolver.resolve_provider_link(**data)
                self.assertEqual('reviewable', resolved.review_disposition)
                self.assertEqual('many_subjects_one_season', resolved.mapping_shape)
                self.assertEqual([('2789',1,18),('65883',19,161)], [
                    (r['bangumi_subject_id'],r['episode_start'],r['episode_end']) for r in resolved.derived_episode_ranges])
                segment = decision.resolve_provider_subject_cohort_segment(**arguments(data))
                self.assertEqual(('2789',), segment.anchor_subject_ids)
                self.assertEqual(data['subject_observations'], segment.working_batch)
                edges = decision.freeze_provider_subject_boundary_edges(**arguments(data))
                self.assertEqual(1, len(edges))
                batch = data['subject_observations']
                evidence = dict(arguments(data), evidence_fingerprint=batch['evidence_fingerprint'], provider_subject_boundary_edges=edges)
                confirmed, payload = decision.resolve_provider_subject_boundary_decision(evidence, same_work=True, boundary_edge=edges[0])
                self.assertIsNotNone(payload)
                self.assertEqual(resolved.derived_episode_ranges, confirmed.derived_episode_ranges)
                context = dict(kind='provider_selection', lifecycle='failed', failure_code='relation_unresolved',
                    expected_decision_revision=3, evidence_fingerprint=batch['evidence_fingerprint'],provider_subject_evidence=evidence)
                from tests.test_r24_phase_d_f7_f8_remediation_contract import ACTIONABLE_SNAPSHOT
                target = ACTIONABLE_SNAPSHOT.to_dict()
                context['provider_subject_cohort_preview'] = decision.build_provider_subject_cohort_preview(
                    evidence,expected_decision_revision=3,boundary_edge=edges[0],
                    target_root_snapshot=target,target_root_config_fingerprint=ACTIONABLE_SNAPSHOT.fingerprint,
                    work_domain='animation',group_kind='tv_episodes')
                task = dict(draft_root_snapshot=target,draft_root_config_fingerprint=ACTIONABLE_SNAPSHOT.fingerprint)
                command = dict(task=task,context=context,kind='confirm_cohort',expected_revision=3,
                    evidence_fingerprint=batch['evidence_fingerprint'],boundary_edge=edges[0],same_work=True)
                self.assertTrue(provider_selection_command_is_capable(**command))
                self.assertFalse(provider_selection_command_is_capable(**{**command,'expected_revision':4}))
                self.assertFalse(provider_selection_command_is_capable(**{**command,'boundary_edge':{**edges[0],'evidence_fingerprint':'0'*64}}))
                self.assertEqual(before, data, '不得把弱候选重写成exact')

    def test_bad_evidence_never_becomes_aggregate_authority(self):
        for defect in ('different_id','truncated','transport','no_anchor','no_animation','bad_year','count','boolean_count','metadata','fingerprint','single'):
            with self.subTest(defect=defect):
                data = fixture(); batch = data['subject_observations']
                weak = batch['subject_observations'][1]
                if defect == 'different_id':
                    for q in weak['queries']:
                        q['candidates'][0]['tmdb_tv_id'] = q['candidates'][0]['provider_id'] = '999'
                elif defect == 'truncated': weak['queries'][0]['truncated'] = True
                elif defect == 'transport': weak['queries'][0].update(outcome='transport_error',candidates=[])
                elif defect == 'no_anchor':
                    for q in batch['subject_observations'][0]['queries']: q['candidates'][0]['match_tier']='fuzzy'
                elif defect in ('no_animation','bad_year'):
                    weak['queries'][0]['candidates'][0]['score_components'][
                        'animation_confirmed' if defect=='no_animation' else 'year_compatible']=False
                elif defect in ('count','boolean_count'): data['bangumi_subjects'][1]['episode_count'] = 142 if defect=='count' else True
                elif defect=='metadata': weak['queries'][0]['candidates'][0]['seasons'][1]['episode_count']=162
                elif defect=='single':
                    data['selected_provider']['provider_id']='65883'; batch['selected_subject_id']='65883'
                    data['bangumi_subjects']=data['bangumi_subjects'][1:]; data['bangumi_relations']=[]
                    batch['subject_observations']=[weak]
                rehash(data)
                if defect=='fingerprint': batch['evidence_fingerprint']='0'*64
                result = resolver.resolve_provider_link(**data)
                self.assertNotIn('same_identity_aggregate_support', result.reason_codes)
                self.assertNotEqual('reviewable', result.review_disposition)


class SourceCards(unittest.IsolatedAsyncioTestCase):
    async def test_three_unselected_entries_reuse_plan_cards_not_table(self):
        from nicegui import Client
        from src.pages.plan_review_page import PlanReviewDialog
        from src.pages.recognition_recovery_view import RecognitionRecoveryDialog
        from tests import test_light_review_contract as light
        helper = light.SingleWorkspace('runTest')
        async with helper.flow(admission_only=True, admit=helper.admit_auto) as flow:
            task=flow.manager._read(flow.task_id)
            expected={r['source_path'] for r in task['task_source_scope']['source_identities']}
            self.assertTrue(expected)
            for mode in ('ordinary','recovery','pending'):
                with self.subTest(mode=mode):
                    data=copy.deepcopy(task)
                    if mode=='pending': data['source_group_pending']=True
                    with Client.auto_index_client.content:
                        dialog=PlanReviewDialog(None,data) if mode=='ordinary' else RecognitionRecoveryDialog(flow.task_id,data,ValueError('no plan'))
                    try:
                        cards=[e for e in dialog.descendants() if 'bar-review-entry' in e.classes]
                        self.assertEqual(len(expected),len(cards),mode)
                        self.assertFalse(any(e.__class__.__name__=='Table' for e in dialog.descendants()))
                        self.assertFalse(any(getattr(e,'text','')=='本任务文件' for e in dialog.descendants()))
                        self.assertFalse(any(getattr(e,'text','')=='内容保留与排除' for e in dialog.descendants()))
                        self.assertEqual(expected,{e.text for c in cards for e in c.descendants() if 'bar-path-box' in e.classes})
                        from unittest.mock import patch
                        from src.pages import plan_review_page
                        presenter = dialog.source_file_list
                        self.assertIs(PlanReviewDialog._render_entry, type(presenter)._render_entry)
                        self.assertIs(PlanReviewDialog._render_entries, type(presenter)._render_entries)
                        with Client.auto_index_client.content, patch.object(plan_review_page, 'REVIEW_PAGE_SIZE', 2):
                            presenter._render_entries()
                            first = {e.text for e in presenter.entries_container.descendants() if 'bar-path-box' in e.classes}
                            self.assertEqual(2,len(first))
                            presenter._change_page(1)
                            second = {e.text for e in presenter.entries_container.descendants() if 'bar-path-box' in e.classes}
                            self.assertEqual(expected,first | second)
                            self.assertFalse(first & second)
                            presenter._set_query(next(iter(first)))
                            self.assertEqual(1,presenter.page)
                            self.assertEqual(1,len([e for e in presenter.entries_container.descendants() if 'bar-path-box' in e.classes]))
                    finally: dialog.delete()


class FailureCopy(unittest.IsolatedAsyncioTestCase):
    def test_detailed_reason_is_projection_not_a_new_failure_code(self):
        from src.pages.review_operation_view import build_review_operation_presentation
        from tests.test_r24_phase_c_review_operation_migration_contract import persist_provider_selection_attempt
        for reason, phrase in (('subject_candidate_confidence_insufficient','资料尚不足'),
                               ('subject_query_truncated','截断'),('season_count_mismatch','集数')):
            task = {}
            persist_provider_selection_attempt(task, dict(state='failed',failure_code='relation_unresolved',reason_codes=[reason]))
            view = build_review_operation_presentation(task)
            self.assertEqual('relation_unresolved',view.reason_code)
            self.assertIn(phrase,view.reason_text)

    async def test_non_season_match_error_not_misclassified(self):
        import tempfile
        from tests import test_stage_b_work_reuse as old
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); task_id='failure-copy'
            tasks,decisions,plans=old.operations._seed_authority(root,task_id)
            manager=old.TaskQueueManager(task_root=tasks,decision_state_root=decisions,
                plan_root=plans,recovery_abandon_audit_root=root/'audit',test_mode=True)
            from src.rename.task_batch_scope import TaskSourceScope, TaskBatchScopeError, source_identity_set_fingerprint
            for fingerprint in ('0'*64,source_identity_set_fingerprint(())):
                with self.subTest(fingerprint=fingerprint):
                    raw=dict(task_id=task_id,media_domain='tv',source_identities=[],
                        source_identity_set_fingerprint=fingerprint,batch_scope_fingerprint='a'*64)
                    with self.assertRaises(TaskBatchScopeError) as raised:
                        TaskSourceScope.from_mapping(raw)
                    self.assertIn('不匹配',str(raised.exception))
                    current=manager._read(task_id)
                    old.operations.persist_provider_selection_attempt(current,dict(request_id='request',state='pending',candidate=old.operations._selection()))
                    old.write_task_at(tasks,task_id,current)
                    manager._fail_reidentify(old.TaskJob(task_id=task_id,path=root/'source.mkv',
                        provider_selection=old.operations._selection(),operation='reidentify',expected_revision=0,request_id='request'),raised.exception)
                    self.assertEqual('selection_failed',old.operations.current_provider_selection_context(manager._read(task_id))['failure_code'])
