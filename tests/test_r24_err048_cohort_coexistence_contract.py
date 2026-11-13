"""ERR048 persisted coexistence: explicit historical fixtures, no real data.

Component fixtures intentionally reconstruct old signed records/Decisions. The
separate AddTask test builds its current successful Decision/Plan in production.
"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from src.rename.decision_state import DecisionStateStore
from src.rename.provider_link_resolver import ProviderLinkResolution
from src.rename.successor_authority import (CohortAuthority, CohortAuthorityDraft,
    CohortAuthorityReader, CohortAuthorityStore, successor_authority_root,
    cohort_projection_fingerprint_for_decision, CohortAuthorityLookup)
from tests.test_r24_successor_authority_incident_red_contract import _confirmed_s1_authority
from tests.test_r24_err048_series_registration_contract import MultiTaskFlow, MEMBERS

YAMA = ('41658','86670','216310','290980')

def snapshot(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*.json')}

def historical_record(states, reference_state, reference_record, selected, task_id, *, current=False):
    """Historical component state only. Never use as current executable Plan."""
    work = deepcopy(reference_state.to_dict()['confirmed_work'])
    work['primary_provider'].update(namespace='bangumi',entity_type='subject',provider_id=selected)
    resolution = deepcopy(reference_record.resolution)
    proof = deepcopy(resolution.get('registration_authority'))
    if not current:
        resolution['registration_authority'] = None
        # Old records really used distinct selected-subject series keys.
        from tests.test_r24_err048_series_registration_contract import expected_key
        resolution['proposed_series_key'] = expected_key(selected)
    series = deepcopy(reference_state.to_dict()['series_binding'])
    series['series_key'] = resolution['proposed_series_key']
    if current:
        series['registration_authority'] = proof
    else:
        series.pop('registration_authority',None)
    provider_link = deepcopy(reference_state.to_dict()['provider_link_binding'])
    if provider_link:
        provider_link['series_key'] = series['series_key']
    state = replace(reference_state, task_id=task_id, revision=1, content_policy=None, initial_source_basis=None, content_selection=None, pending_tv_selection=None, tv_planning_authority=None, asset_operation=None, confirmed_work=work, series_binding=series, provider_link_binding=provider_link, linked_provider_identities=tuple((p for p in reference_state.linked_provider_identities if p.get('namespace') != 'bangumi')), season_binding={**dict(reference_state.season_binding), 'season_number': reference_record.season_assignments[selected]})
    # Explicit read-only historical storage fixture, not a modern writer bypass
    # presented as an executable Plan. No Task, source lease or approval exists.
    path = states.path_for(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), ensure_ascii=False), encoding='utf-8')
    state = states.load_readonly(task_id)
    from src.rename.successor_authority import _resolution_from_payload
    boundary = deepcopy(next(iter(state.provider_subject_boundary_decisions.values())))
    draft = CohortAuthorityDraft.from_confirmation(boundary_decision=boundary,work_identity=work,
        resolution=_resolution_from_payload(resolution),provider_snapshot=reference_record.provider_snapshot,
        title_selection=reference_record.title_selection)
    record = draft.bind(state,authority_revision=1)
    if not current:
        # Byte-valid retired task-coupled schema, not a current V3 fact.
        values = {**record.__dict__, 'schema_version': 2,
                  'policy_version': 'provider-subject-cohort-v2'}
        values.pop('authority_fingerprint')
        record = CohortAuthority.create(**values)
    store = CohortAuthorityStore(successor_authority_root(states.root))
    store.publish_prepared(record,expected_previous_fingerprint='')
    return state,record

def material(root, *, current_count=1):
    ref = root/'reference'/'decisions'
    reference = _confirmed_s1_authority(ref)
    template = CohortAuthorityStore(successor_authority_root(ref)).list_current()[0]
    states = DecisionStateStore(root/'data'/'decisions')
    records = [historical_record(states,reference,template,s,'producer-'+s,current=i<current_count)
               for i,s in enumerate(YAMA)]
    return states,records

def overwrite_record(states, record, **changes):
    payload = {**record.__dict__,**changes}
    payload.pop('authority_fingerprint')
    changed = CohortAuthority.create(**payload)
    CohortAuthorityStore(successor_authority_root(states.root)).path_for(record.authority_id).write_text(
        json.dumps(changed.to_dict(),ensure_ascii=False),encoding='utf-8')
    return changed

class CohortCoexistenceComponents(unittest.TestCase):
    def test_historical_fixture_projection_fingerprints_are_exact_before_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            states,records=material(Path(tmp))
            for state,record in records:
                self.assertEqual(record.source_cohort_projection_fingerprint,
                    cohort_projection_fingerprint_for_decision(states.load(state.task_id)))
                self.assertEqual(record,CohortAuthority.from_dict(record.to_dict()))

    def test_four_persisted_mixed_records_inherit_current_without_writes_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            states,records=material(Path(tmp))
            before=snapshot(states.root.parent)
            for subject in YAMA[1:]:
                result=CohortAuthorityReader.from_decision_root(states.root).lookup(subject,consumer_task_id='consumer-'+subject)
                self.assertEqual('matched',result.status,result.reason_code)
                self.assertEqual(records[0][1].authority_id,result.authority.authority_id)
                self.assertEqual(records[0][1].resolution['registration_authority'],result.authority.project(subject)[2].registration_authority)
            self.assertEqual(before,snapshot(states.root.parent))

    def test_current_equivalent_records_are_not_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            states,records=material(Path(tmp),current_count=4)
            result=CohortAuthorityReader.from_decision_root(states.root).lookup(YAMA[1],consumer_task_id='consumer')
            self.assertEqual('matched',result.status,result.reason_code)

    def test_consumer_retry_may_leave_equivalent_legacy_record_but_never_supply_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            states,records=material(Path(tmp))
            old=states.load('producer-'+YAMA[1])
            states.save(replace(old,confirmed_work={},series_binding=None),expected_revision=old.revision)
            result=CohortAuthorityReader.from_decision_root(states.root).lookup(YAMA[1],consumer_task_id=old.task_id)
            self.assertEqual('matched',result.status,result.reason_code)
            self.assertNotEqual(old.task_id,result.authority.source_task_id)

    def test_legacy_only_never_becomes_current_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            states,_=material(Path(tmp),current_count=0)
            self.assertNotEqual('matched',CohortAuthorityReader.from_decision_root(states.root).lookup(YAMA[1],consumer_task_id='consumer').status)

    def test_equivalent_legacy_producer_replanned_with_current_proof_is_not_elected(self):
        with tempfile.TemporaryDirectory() as tmp:
            states,records=material(Path(tmp))
            old=records[1][0]
            current=records[0][0]
            new_binding={**dict(old.series_binding),'series_key':current.series_binding['series_key'],
                         'registration_authority':deepcopy(current.series_binding['registration_authority'])}
            states.save(replace(old,series_binding=new_binding,provider_link_binding={
                **dict(old.provider_link_binding),'series_key':new_binding['series_key']}),expected_revision=old.revision)
            result=CohortAuthorityReader.from_decision_root(states.root).lookup(YAMA[2],consumer_task_id='consumer')
            self.assertEqual('matched',result.status,result.reason_code)
            self.assertEqual(current.task_id,result.authority.source_task_id)

    def test_missing_or_drifted_producer_does_not_revoke_frozen_v3_relationship(self):
        for missing in (True,False):
            with self.subTest(missing=missing),tempfile.TemporaryDirectory() as tmp:
                states,records=material(Path(tmp))
                state=records[0][0]
                if missing:
                    states.path_for(state.task_id).unlink()
                else:
                    states.save(replace(state,season_binding={**dict(state.season_binding),
                        'season_number':9}),expected_revision=state.revision)
                before=snapshot(successor_authority_root(states.root))
                result=CohortAuthorityReader.from_decision_root(states.root).lookup(YAMA[1],consumer_task_id='consumer')
                self.assertEqual('matched',result.status,result.reason_code)
                self.assertEqual(records[0][1],result.authority)
                self.assertEqual(before,snapshot(successor_authority_root(states.root)))

    def test_different_range_metadata_or_canonical_title_is_not_equivalent(self):
        for mutation in ('range','metadata','title'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as tmp:
                # A conflicting CURRENT fact must reject; retired schema2 is
                # not a competing authority and cannot revoke its successor.
                states,records=material(Path(tmp),current_count=2)
                old=records[1][1]
                if mutation=='range':
                    resolution=deepcopy(old.resolution)
                    resolution['derived_episode_ranges'][0]['episode_end']+=1
                    overwrite_record(states,old,resolution=resolution)
                elif mutation=='metadata':
                    provider=deepcopy(old.provider_snapshot)
                    provider['first_air_date']='2020-01-01'
                    overwrite_record(states,old,provider_snapshot=provider)
                else:
                    title=deepcopy(old.title_selection)
                    title['title']='Different canonical title'
                    overwrite_record(states,old,title_selection=title)
                result=CohortAuthorityReader.from_decision_root(states.root).lookup(YAMA[1],consumer_task_id='consumer')
                self.assertNotEqual('matched',result.status)

    def test_corruption_and_new_competitor_during_read_fail_closed(self):
        for mutation in ('corrupt','new_record','relationship_revision'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as tmp:
                states,records=material(Path(tmp))
                changed=False
                class ObservedStore(CohortAuthorityStore):
                    def list_for_subject(self, subject):
                        result=super().list_for_subject(subject)
                        changing()
                        return result
                def changing():
                    nonlocal changed
                    if not changed:
                        changed=True
                        if mutation=='corrupt':
                            reader.store.path_for(records[-1][1].authority_id).write_text('{}',encoding='utf-8')
                        elif mutation=='relationship_revision':
                            record=records[0][1]
                            overwrite_record(states,record,authority_revision=record.authority_revision+1)
                        else:
                            from src.rename.successor_authority import _sha256
                            payload=deepcopy(records[0][1].__dict__)
                            payload.pop('authority_fingerprint')
                            provider=str(payload['provider_snapshot']['provider_id'])
                            payload['work_identity']['primary_provider'].update(namespace='tmdb',entity_type='tv',provider_id=provider)
                            payload['authority_id']='cohort-'+_sha256({'work_identity':('tmdb','tv',provider),
                                'cohort_subject_ids':list(YAMA)},domain='bar.successor-authority-identity')
                            competitor=CohortAuthority.create(**payload)
                            reader.store.publish_prepared(competitor,expected_previous_fingerprint='')
                class AuditOnlyDecisionStore(DecisionStateStore):
                    def load(self, task):
                        raise AssertionError('mutable producer Decision is not relationship authority')
                reader=CohortAuthorityReader(ObservedStore(successor_authority_root(states.root)),
                                            AuditOnlyDecisionStore(states.root))
                result=reader.lookup(YAMA[1],consumer_task_id='consumer')
                self.assertEqual(('invalid','authority_read_drift'),(result.status,result.reason_code))
                self.assertTrue(changed,'mutation must follow completed authority enumeration')

    def test_conflicting_current_registration_and_corrupted_legacy_are_rejected(self):
        for mutation in ('registration','corrupt'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as tmp:
                states,records=material(Path(tmp),current_count=2)
                store=CohortAuthorityStore(successor_authority_root(states.root))
                if mutation=='corrupt':
                    store.path_for(records[-1][1].authority_id).write_text('{}',encoding='utf-8')
                else:
                    record=records[1][1]
                    resolution=deepcopy(record.resolution)
                    resolution['registration_authority']['series_key']='series-contradiction'
                    overwrite_record(states,record,resolution=resolution)
                self.assertNotEqual('matched',CohortAuthorityReader.from_decision_root(states.root).lookup(YAMA[1],consumer_task_id='consumer').status)


class CohortCoexistenceProduction(unittest.IsolatedAsyncioTestCase):
    async def test_reader_disconnection_or_conflict_cannot_publish_successful_inheritance(self):
        from src.rename.plan_review import PlanReviewError
        for reason in ('authority_disconnected','competing_current_authority'):
            with self.subTest(reason=reason),tempfile.TemporaryDirectory() as tmp:
                async with MultiTaskFlow(Path(tmp)) as runtime:
                    first=await runtime.add(MEMBERS[0])
                    store=CohortAuthorityStore(successor_authority_root(runtime.decisions))
                    record=store.list_current()[0]
                    if reason=='authority_disconnected':
                        store.path_for(record.authority_id).write_text('{}',encoding='utf-8')
                    else:
                        states=DecisionStateStore(runtime.decisions)
                        _,competitor=historical_record(states,first.state,record,MEMBERS[1],'conflicting-producer',current=True)
                        provider=deepcopy(competitor.provider_snapshot)
                        provider['first_air_date']='2020-01-01'
                        overwrite_record(states,competitor,provider_snapshot=provider)
                    observed=CohortAuthorityReader.from_decision_root(runtime.decisions).lookup(
                        MEMBERS[1],consumer_task_id='not-yet-admitted')
                    self.assertEqual('conflict' if reason=='competing_current_authority' else 'invalid',observed.status)
                    from unittest.mock import patch
                    calls = []
                    original_lookup = CohortAuthorityReader.lookup
                    def lookup(reader, subject_id, **kwargs):
                        calls.append(kwargs.get('consumer_task_id'))
                        return original_lookup(reader, subject_id, **kwargs)
                    with patch.object(CohortAuthorityReader, 'lookup', lookup):
                        result=await runtime.add(MEMBERS[1],confirm=False)
                    self.assertIn(result.task_id, calls)
                    with self.assertRaises((PlanReviewError,ValueError)):
                        await runtime.approve(result)
                    # A failed selection keeps the last identity as read-only
                    # history; it must not keep an approvable successful state.
                    self.assertFalse(result.state.series_binding,
                        'new consumer must not inherit the damaged relationship')
                    from tests.test_r24_phase_c_review_operation_migration_contract import current_provider_selection_context
                    context=current_provider_selection_context(result.task)
                    self.assertTrue(result.task.get('error'))
                    self.assertIsNotNone(context)
                    self.assertEqual('failed',context['state'])
                    self.assertNotEqual('approved',runtime.review.load_or_create(result.plan).status)
                    self.assertFalse((runtime.root/'Anime').exists())
                    runtime.assert_sources_unchanged(self)

    async def test_addtask_current_producer_with_equivalent_historical_peers_then_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with MultiTaskFlow(Path(tmp)) as runtime:
                first=await runtime.add(MEMBERS[0])
                store=CohortAuthorityStore(successor_authority_root(runtime.decisions))
                template=store.list_current()[0]
                states=DecisionStateStore(runtime.decisions)
                for selected in MEMBERS[1:]:
                    historical_record(states,first.state,template,selected,'historical-'+selected)
                before=len(runtime.confirmation_states)
                second=await runtime.add(MEMBERS[1],confirm=False)
                self.assertEqual(MEMBERS[1],second.state.confirmed_work.get('primary_provider',{}).get('provider_id'))
                self.assertEqual(first.state.series_binding['series_key'],second.state.series_binding['series_key'])
                self.assertEqual(before,len(runtime.confirmation_states))
                await runtime.manager.retry_current(second.task_id)
                await runtime.joined()
                second=runtime.result(second.task_id)
                self.assertEqual(MEMBERS[1],second.state.confirmed_work.get('primary_provider',{}).get('provider_id'))
                self.assertEqual(first.state.series_binding['series_key'],second.state.series_binding['series_key'])
                self.assertEqual(before,len(runtime.confirmation_states))
                runtime.assert_sources_unchanged(self)
