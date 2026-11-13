"""Explicit original-task rebuild; no recognition, duplicate task or old approval."""
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path

from .completed_history import read_bytes, ledger_for, inspect_completed_media, archive_exact
from .rebuild_observation import CompletedRebuildObservation, digest
from .plan_store import PlanStore
from .decision_state import DecisionStateStore, DecisionCommandService
from .catalog_binding import binding_from_plan, binding_from_decision
from .series_catalog import SeriesCatalog
from .provider_link_catalog import ProviderLinkCatalog
from .work_catalog import WorkCatalog
from .projection_authority import TaskTerminalMutation, validate_plan_projection_receipt, SourceOwnershipStore
from .asset_collision import build_reuse_resolution
from .rename_plan import PlanStatus
from .file_lock import advisory_file_lock
from .path_safety import first_reparse_component


@dataclass(frozen=True)
class RebuildSnapshot:
    state: str
    detail: str
    missing_count: int = 0
    reuse_count: int = 0
    fingerprint: str = ''


class CompletedRebuildService:
    def __init__(self, *, plan_store, decision_store, task_root, record_root,
                 catalog_root, series_catalog_root, provider_link_catalog_root):
        self.plans, self.decisions = plan_store, decision_store
        self.tasks, self.records = Path(task_root), Path(record_root)
        self.work = WorkCatalog(catalog_root)
        self.series = SeriesCatalog(series_catalog_root)
        self.links = ProviderLinkCatalog(provider_link_catalog_root)

    @classmethod
    def from_execution_context(cls, *, plan_store, task_root, decision_root, record_root,
                               catalog_root=None, series_catalog_root=None, provider_link_catalog_root=None):
        base = Path(task_root).parent
        return cls(plan_store=plan_store, decision_store=DecisionStateStore(decision_root),
            task_root=task_root, record_root=record_root, catalog_root=catalog_root or base/'work_catalog',
            series_catalog_root=series_catalog_root or base/'series_catalog',
            provider_link_catalog_root=provider_link_catalog_root or base/'provider_link_catalog')

    def _current(self, task_id):
        # path_for validates the id before using it with other roots.
        self.plans.path_for(task_id)
        read_paths = (self.tasks/f'{task_id}.json', self.plans.path_for(task_id), self.decisions.path_for(task_id))
        before = {p: read_bytes(p) for p in read_paths}
        task = json.loads(before[read_paths[0]])
        plan, state = self.plans.load(task_id), self.decisions.load_readonly(task_id)
        fp = self.plans.fingerprint(plan)
        validate_plan_projection_receipt(plan)
        if (task.get('uuid') != task_id or task.get('plan_fingerprint') != fp
            or plan.metadata.get('decision_state_fingerprint') != self.decisions.fingerprint(state)
            or plan.metadata.get('decision_state_revision') != state.revision
            or (binding_from_decision(state) is not None and binding_from_plan(plan) != binding_from_decision(state))
            or task.get('catalog_detach_pending')):
            raise ValueError('当前任务、身份决定与计划不一致，请先核查')
        ownership = SourceOwnershipStore(self.plans.root.parent/'source_ownership').load_optional(task_id)
        if ownership is None or ownership.state != 'active' or ownership.fingerprint != plan.metadata.get('source_ownership_fingerprint'):
            raise ValueError('源文件所有权与原任务不一致')
        journal = self.plans.root.parent/'projection_journal'/f'{task_id}.json'
        if journal.exists() and json.loads(read_bytes(journal)).get('status') == 'pending':
            raise ValueError('该任务仍有未完成的计划事务，请先恢复事务')
        if any(read_bytes(p) != value for p, value in before.items()):
            raise ValueError('核查期间任务或计划已变化，请刷新')
        return task, plan, state

    def peer_loader(self, task_id, plan_fingerprint=None):
        task, current, state = self._current(task_id)
        current_fp = self.plans.fingerprint(current)
        observation = state.completed_rebuild_observation
        if observation is not None:
            raw = self._signed_observation(current, state)
            fingerprint = plan_fingerprint or raw['origin_plan_fingerprint']
            if fingerprint not in {raw['origin_plan_fingerprint'], raw['parent_plan_fingerprint'], current_fp}:
                raise ValueError('登记计划不属于该任务的重建历史')
        else:
            if (task.get('workflow_status') not in {'completed', 'partial_failed'} or task.get('execution_status') != 'completed'
                or task.get('execution_fingerprint') != current_fp or task.get('execution_requires_manual_check')):
                raise ValueError('同作品任务缺少可信完成历史')
            fingerprint = plan_fingerprint or current_fp
        plan, ledger, _ = ledger_for(self.plans, self.records, task_id, fingerprint,
            require_archive=observation is not None and fingerprint != current_fp)
        self.inspect_media(plan, ledger)
        if (plan.work_identity != current.work_identity or plan.metadata.get('canonical_work_root') != current.metadata.get('canonical_work_root')):
            raise ValueError('历史作品身份或目标根已变化')
        return plan, ledger

    def inspect_media(self, plan, ledger):
        return inspect_completed_media(plan, ledger)

    def _signed_observation(self, plan, state=None):
        validate_plan_projection_receipt(plan)
        state = state or self.decisions.load_readonly(plan.task_id)
        observation = state.completed_rebuild_observation
        if observation is None or observation.to_dict() != _plain(plan.metadata.get('completed_rebuild_observation')):
            raise ValueError('重建计划没有对应的类型化决定')
        raw = observation.to_dict()
        if raw['task_id'] != plan.task_id:
            raise ValueError('重建观察属于其他任务')
        link, seen = raw, set()
        while True:
            parent_fp = link['parent_plan_fingerprint']
            if parent_fp in seen or link['task_id'] != plan.task_id or link['origin_plan_fingerprint'] != raw['origin_plan_fingerprint']:
                raise ValueError('重建历史链循环或身份不一致')
            seen.add(parent_fp)
            parent, _, data = ledger_for(self.plans, self.records, plan.task_id, parent_fp, require_archive=True)
            if hashlib.sha256(data).hexdigest() != link['ledger_sha256']:
                raise ValueError('冻结的原执行历史已变化')
            mapping = self.records/'history'/plan.task_id/f'{parent_fp}.mapping.json'
            if hashlib.sha256(read_bytes(mapping)).hexdigest() != link['mapping_sha256']:
                raise ValueError('冻结的原映射历史已变化')
            validate_plan_projection_receipt(parent)
            if parent_fp == raw['origin_plan_fingerprint']:
                break
            link = CompletedRebuildObservation.from_mapping(_plain(parent.metadata.get('completed_rebuild_observation'))).to_dict()
        return raw

    def _catalog(self, plan, *, scoped=True):
        binding = binding_from_plan(plan)
        if binding is None:
            raise ValueError('重建缺少作品登记权威')
        if binding.backend == 'work':
            from .completed_rebuild_work import inspect_work_rebuild
            return {'work_proof': inspect_work_rebuild(self.work, plan, peer_loader=self.peer_loader)}
        from .recovery_service import _series_catalog_materializes_plan
        match = self.series.load(binding.key)
        path = self.series._path(binding.key)
        catalog_data = json.loads(read_bytes(path))
        self.series.validate_archived_payload(catalog_data, binding.key)
        peers = []
        for row in match.season_bindings:
            if scoped and row['task_id'] != plan.task_id:
                continue
            peer, ledger = self.peer_loader(row['task_id'],
                self.plans.fingerprint(plan) if scoped else None)
            if (peer.metadata.get('series_key') != binding.key
                or peer.metadata.get('canonical_work_root') != str(match.canonical_root)
                or not _series_catalog_materializes_plan(match, peer, completed_ledger=ledger)):
                raise ValueError('同系列历史季度登记与完成证明不一致')
            item = {'task_id': peer.task_id, 'plan_fingerprint': self.plans.fingerprint(peer),
                    'ledger_fingerprint': digest(ledger)}
            if item not in peers:
                peers.append(item)
        if not any(row['task_id'] == plan.task_id for row in peers):
            raise ValueError('原任务不在系列登记中')
        if scoped:
            from .season_identity import provider_key
            selected = sorted({(r['task_id'], r['season_number'], provider_key(r['provider_identity']))
                for r in match.season_bindings if r['season_number'] == plan.metadata.get('season_number')})
            catalog_data = dict(series_key=binding.key, canonical_root=str(match.canonical_root),
                domain=catalog_data.get('domain'), selected_bindings=selected)
        return {'series_proof': {'key': binding.key, 'revision': match.revision,
            'catalog_fingerprint': digest(catalog_data), 'peers': peers}}

    def _inspect(self, task_id):
        task, plan, state = self._current(task_id)
        current_plan = plan
        fp = self.plans.fingerprint(plan)
        pending = (state.completed_rebuild_observation is not None
            and task.get('workflow_status') in {'need_confirm', 'invalid', 'approved'}
            and task.get('execution_status') in {None, 'not_started'})
        if (not pending and (task.get('workflow_status') not in {'completed', 'partial_failed'} or task.get('execution_status') != 'completed'
            or task.get('execution_fingerprint') != fp)
            or task.get('execution_requires_manual_check') or task.get('execution_may_have_written_targets')
            or state.recovery_state not in {None, 'healthy'}):
            raise ValueError('当前任务不是可重建的已完成状态，请先核查')
        if pending:
            signed = self._signed_observation(plan, state)
            parent = signed['parent_plan_fingerprint']
            old_plan, ledger, ledger_data = ledger_for(self.plans, self.records, task_id, parent, require_archive=True)
            if (plan.work_identity != old_plan.work_identity or plan.expected_sources != old_plan.expected_sources
                    or plan.metadata.get('canonical_work_root') != old_plan.metadata.get('canonical_work_root')):
                raise ValueError('待处理修复计划已改变作品或来源，不能自动续用历史')
            plan, fp = old_plan, parent
        else:
            ledger_data = read_bytes(self.records/f'{task_id}.ledger.json')
            ledger = json.loads(ledger_data)
        missing, kept = self.inspect_media(plan, ledger)
        if missing:
            from .content_selection import validate_media_intent
            validate_media_intent(current_plan, state, task_root=self.tasks)
        origin_fp = (state.completed_rebuild_observation.to_dict()['origin_plan_fingerprint']
            if state.completed_rebuild_observation is not None else fp)
        origin = self.plans.load_version(task_id, origin_fp)
        catalog = self._catalog(origin)
        observation = CompletedRebuildObservation.from_mapping(dict(schema_version=1,
            policy_version='completed-rebuild-v2', task_id=task_id,
            origin_plan_fingerprint=origin_fp, parent_plan_fingerprint=fp,
            ledger_sha256=hashlib.sha256(ledger_data).hexdigest(),
            mapping_sha256=hashlib.sha256(read_bytes(self.records/f'{task_id}.json')).hexdigest(),
            missing_targets=list(missing), reuse_targets=list(kept),
            **catalog))
        view = RebuildSnapshot('missing' if missing else 'complete',
            '发现缺失目标，可生成新的待批准计划' if missing else '已核对，现有目标完整，无需重建',
            len(missing), len(kept), observation.fingerprint)
        return view, observation, task, plan, state, ledger_data


    def inspect(self, task_id):
        try:
            return self._inspect(task_id)[0]
        except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
            return RebuildSnapshot('blocked', str(exc))

    def generate(self, task_id, *, expected_fingerprint):
        # Reject known-invalid read-only requests without touching diagnostic
        # lock metadata. The full inspection below remains under the task lock.
        initial = self.inspect(task_id)
        if initial.state != 'missing' or initial.fingerprint != expected_fingerprint:
            raise ValueError('重建核查已变化，或当前证据阻断；请刷新')
        from .file_lock import task_operation_lock
        with task_operation_lock(self.tasks, task_id, operation='generate_completed_rebuild'):
            return self._generate_locked(task_id, expected_fingerprint=expected_fingerprint)

    def _generate_locked(self, task_id, *, expected_fingerprint):
        with advisory_file_lock(self.tasks/f'.{task_id}.rebuild.lock', require_single_link=True):
            view, observation, task, old, state, ledger_data = self._inspect(task_id)
            if view.state != 'missing' or view.fingerprint != expected_fingerprint:
                raise ValueError('重建核查已变化，或没有缺失媒体；请刷新')
            current = self.plans.load(task_id)
            if (task.get('workflow_status') in {'need_confirm', 'invalid', 'approved'}
                    and state.completed_rebuild_observation == observation):
                self.validate_plan(current)
                return current
            changes = {'completed_rebuild_observation': observation, 'recovery_state': 'healthy', 'execution_failure': None}
            binding = binding_from_plan(old)
            metadata = dict(old.metadata)
            if binding.backend == 'series':
                from .series_registration import SeriesRegistrationAuthority, _materialized_evidence, rebind_materialized_registration
                match = self.series.load(binding.key)
                current_binding = dict(state.series_binding)
                proof = SeriesRegistrationAuthority.from_mapping(current_binding['registration_authority'])
                materialized = _materialized_evidence(proof, self.series, match, library_domain=old.work_identity.domain)
                proof = rebind_materialized_registration(proof, materialized)
                current_binding.update(catalog_revision=match.revision, registration_authority=proof.to_dict())
                if proof.affiliation:
                    current_binding['affiliation'] = json.loads(proof.affiliation)
                changes['series_binding'] = current_binding
            else:
                proof = observation.to_dict()['work_proof']
                current = self.work.load(binding.key)
                intent = dict(state.catalog_link_intent or {})
                intent.update(work_key=binding.key, catalog_revision=current['revision'])
                if intent.get('affiliation') is not None:
                    from .work_affiliation import refresh_registered
                    intent['affiliation'] = refresh_registered(intent['affiliation'], current)
                changes['catalog_link_intent'] = intent
                metadata.update(catalog_work_key=binding.key, catalog_revision=current['revision'])
            entries = tuple(replace(e, collision_resolution=(build_reuse_resolution(e.source_path, e.target_path)
                if e.target_path.is_file() else None)) for e in old.entries)
            draft = replace(old, entries=entries, metadata=metadata, projection_receipt=None,
                revision_action='generate_completed_rebuild',
                revision_parent_fingerprint=self.plans.fingerprint(old))
            if view.state == 'missing':
                from .content_selection import validate_media_intent
                validate_media_intent(self.plans.load(task_id), state, task_root=self.tasks)
            from .media_archive import MediaArchiveContext, MediaArchiveIntent
            archive_context = MediaArchiveContext.for_rebuild(self)
            archive = MediaArchiveIntent.prepare(archive_context, self.plans.load(task_id), state,
                action='generate_completed_rebuild', archive_fingerprint=self.plans.fingerprint(old),
                ledger_bytes=ledger_data, mapping_sha256=observation.to_dict()['mapping_sha256'])
            def terminal(decision, plan, payload):
                retained = {k: v for k, v in payload.items() if not k.startswith('execution_')}
                return {**retained, 'workflow_status': 'need_confirm', 'queue_operation': None,
                    'execution_status': None, 'approved_at': None, 'completed_at': None,
                    'error': None}
            _, new = DecisionCommandService(self.decisions, self.plans,
                media_archive_context=archive_context).update(task_id,
                media_archive=archive,
                expected_revision=state.revision, changes=changes, planner=lambda candidate: draft,
                task_terminal=TaskTerminalMutation.prepare(self.tasks, task_id, terminal))
            return new

    def validate_plan(self, plan):
        raw = self._signed_observation(plan)
        origin, ledger = self.peer_loader(plan.task_id, raw['origin_plan_fingerprint'])
        # Catalog semantic closure may become physically complete but its
        # content identity must never change under an approved rebuild.
        if raw.get('work_proof') is not None:
            from .completed_rebuild_work import validate_work_rebuild
            validate_work_rebuild(plan, self.work, raw['work_proof'], peer_loader=self.peer_loader)
        else:
            fresh = self._catalog(origin)['series_proof']
            frozen = raw.get('series_proof')
            fresh = {k:v for k,v in fresh.items() if k != 'revision'}
            frozen = {k:v for k,v in frozen.items() if k != 'revision'}
            if fresh != frozen:
                raise ValueError('重建系列登记或历史证据已变化，请重新核查')
        def semantic(entries):
            return [{k: v for k, v in e.to_dict().items() if k != 'collision_resolution'} for e in entries]
        if (origin.work_identity != plan.work_identity or origin.execution_policy != plan.execution_policy
            or origin.expected_sources != plan.expected_sources or semantic(origin.entries) != semantic(plan.entries)):
            raise ValueError('重建不能改变原作品或媒体目标')
        return raw

    def work_context(self):
        from .completed_rebuild_work import WorkRebuildContext
        def proof(plan):
            if plan.metadata.get('completed_rebuild_observation') is None:
                return None
            return self._signed_observation(plan)['work_proof']
        return WorkRebuildContext(proof, self.peer_loader)




def _plain(value):
    from .projection_authority import _thaw
    return _thaw(value)
