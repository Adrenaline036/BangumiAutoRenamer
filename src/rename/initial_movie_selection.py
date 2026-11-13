"""Movie identity selection before content completion; sources stay frozen."""
from dataclasses import dataclass, replace
import json
from collections.abc import Mapping
import hashlib
import re

from . import content_selection as content
from .decision_state import DecisionStateStore
from .projection_authority import RecognitionDecisionInput, TaskTerminalMutation
from .review_operation import current_provider_selection_review_operation


@dataclass(frozen=True)
class InitialMovieSelectionResult:
    request_id: str
    operation_fingerprint: str
    expected_revision: int
    result_revision: int
    candidate_identity: tuple[str, str, str]

    def __post_init__(self):
        InitialMovieRequest('selection-result', self.request_id, self.expected_revision, self.candidate_identity)
        if (not isinstance(self.operation_fingerprint, str)
                or re.fullmatch('[0-9a-f]{64}', self.operation_fingerprint) is None
                or type(self.result_revision) is not int or self.result_revision != self.expected_revision + 1):
            raise ValueError('首次作品选择结果缺少实际请求或修订绑定')

    def to_dict(self):
        return dict(schema_version=1, request_id=self.request_id, operation_fingerprint=self.operation_fingerprint,
            expected_revision=self.expected_revision, result_revision=self.result_revision,
            candidate_identity=list(self.candidate_identity))

    @classmethod
    def from_mapping(cls, value):
        keys = {'schema_version', 'request_id', 'operation_fingerprint', 'expected_revision',
                'result_revision', 'candidate_identity'}
        if (not isinstance(value, Mapping) or set(value) != keys or type(value['schema_version']) is not int
                or value['schema_version'] != 1 or not isinstance(value['candidate_identity'], (list, tuple))):
            raise ValueError('首次作品选择结果格式无效')
        return cls(value['request_id'], value['operation_fingerprint'], value['expected_revision'],
                   value['result_revision'], tuple(value['candidate_identity']))

    @property
    def fingerprint(self):
        from .content_associations import encode
        return hashlib.sha256(('bar.initial-movie-selection-result-v1:' + encode(self.to_dict())).encode('utf-8')).hexdigest()


def selection_result_fingerprint(plan):
    value = plan.metadata.get('initial_movie_selection_result') if plan is not None else None
    return InitialMovieSelectionResult.from_mapping(value).fingerprint if value is not None else None


def validate_successful_selection(service, plan, state):
    """Bind actual successful operation to its receipted C1a history, not a UI flag."""
    if plan.metadata.get('initial_movie_selection_result') is None and plan.metadata.get('automatic_recognition') is not None:
        from .automatic_recognition import validate_automatic_movie_selection
        return validate_automatic_movie_selection(service, plan, state)
    from ..utils.utils import read_task_at
    from .projection_authority import validate_plan_projection_receipt
    from ..task_queue import TaskQueueManager
    task = read_task_at(service.task_root, plan.task_id)
    _operation, context = current_provider_selection_review_operation(task)
    proof = InitialMovieSelectionResult.from_mapping(plan.metadata.get('initial_movie_selection_result'))
    identity = tuple(str((context.get('candidate') or {}).get(k) or '').strip().casefold()
                     for k in ('namespace', 'entity_type', 'provider_id'))
    if (context.get('lifecycle') != 'succeeded' or context.get('request_id') != proof.request_id
            or type(context.get('expected_decision_revision')) is not int
            or context['expected_decision_revision'] != proof.expected_revision
            or type(context.get('result_decision_revision')) is not int
            or context['result_decision_revision'] != proof.result_revision or identity != proof.candidate_identity
            or not TaskQueueManager._selected_provider_was_persisted(
                context.get('candidate'), state.provider_evidence, plan)):
        raise ValueError('首次电影完成需要原作品选择的真实成功结果，请重新核对选择')
    latest, selected_work = proof, plan.work_identity
    seen, cursor = set(), plan
    for _ in range(256):
        fp = service.plan_store.fingerprint(cursor)
        if fp in seen:
            raise ValueError('首次作品选择历史出现循环')
        seen.add(fp)
        validate_plan_projection_receipt(cursor)
        if (cursor.task_id != plan.task_id or cursor.source_root != plan.source_root
                or cursor.work_identity != selected_work
                or cursor.initial_source_basis != state.initial_source_basis
                or selection_result_fingerprint(cursor) != proof.fingerprint):
            raise ValueError('首次作品选择历史与当前来源或作品不一致')
        primary = cursor.work_identity.primary_provider
        if (primary.namespace, primary.entity_type, primary.provider_id) != proof.candidate_identity:
            raise ValueError('作品选择历史凭据与条目身份不一致')
        revision = cursor.projection_receipt.authority['decision_revision']
        if cursor.revision_action == 'initial_movie_selection':
            if revision != proof.result_revision or not cursor.revision_parent_fingerprint:
                raise ValueError('首次作品选择结果修订没有实际历史凭据')
            parent = service.plan_store.load_version(plan.task_id, cursor.revision_parent_fingerprint)
            validate_plan_projection_receipt(parent)
            if (parent.projection_receipt.authority['decision_revision'] != proof.expected_revision
                    or parent.task_id != plan.task_id or parent.source_root != plan.source_root
                    or parent.initial_source_basis != state.initial_source_basis):
                raise ValueError('首次作品选择的原修订或来源不一致')
            if selection_result_fingerprint(parent) is None:
                return latest
            # Re-selection may change the work only at this actual, receipted
            # handoff. Walk the older selection too, including intervening
            # content edits; a newer request cannot invent a missing history.
            proof = InitialMovieSelectionResult.from_mapping(parent.metadata['initial_movie_selection_result'])
            selected_work, cursor = parent.work_identity, parent
            continue
        if type(revision) is not int or revision < proof.result_revision or not cursor.revision_parent_fingerprint:
            raise ValueError('首次作品选择缺少真实提交历史')
        parent = service.plan_store.load_version(plan.task_id, cursor.revision_parent_fingerprint)
        from .movie_work_correction import preceding_work
        selected_work = preceding_work(cursor, parent)
        cursor = parent
    raise ValueError('首次作品选择历史超出有界检查范围')


@dataclass(frozen=True)
class InitialMovieRequest:
    task_id: str
    request_id: str
    expected_revision: int
    candidate_identity: tuple[str, str, str]
    requested_season: int | None = None

    def __post_init__(self):
        if (not isinstance(self.task_id, str) or not self.task_id
                or not isinstance(self.request_id, str) or not self.request_id
                or type(self.expected_revision) is not int or self.expected_revision < 0
                or type(self.candidate_identity) is not tuple or len(self.candidate_identity) != 3
                or any(not isinstance(v, str) or not v or v != v.strip().casefold() for v in self.candidate_identity)
                or self.requested_season is not None and (type(self.requested_season) is not int or self.requested_season < 0)):
            raise ValueError('worker请求绑定格式无效')

    @classmethod
    def from_job(cls, job):
        from ..task_queue import TaskJob
        if (type(job) is not TaskJob or job.operation != 'reidentify'
                or not job.request_id or type(job.expected_revision) is not int
                or job.expected_revision < 0 or not isinstance(job.provider_selection, dict)):
            raise ValueError('作品选择worker缺少真实请求绑定')
        identity = tuple(str(job.provider_selection.get(k) or '').strip().casefold()
                         for k in ('namespace', 'entity_type', 'provider_id'))
        if not all(identity):
            raise ValueError('作品选择worker缺少候选身份')
        return cls(job.task_id, job.request_id, job.expected_revision, identity, job.custom_season_id)


@dataclass(frozen=True)
class InitialMovieSelectionIntent:
    task_id: str
    plan_fingerprint: str
    decision_fingerprint: str
    basis_fingerprint: str
    operation_fingerprint: str
    request_id: str
    recognition: RecognitionDecisionInput


@dataclass(frozen=True)
class InitialMovieSelection:
    decision: object
    draft: object


def _task(service, task_id):
    from ..utils.utils import safe_task_path
    path = safe_task_path(service.task_root, task_id)
    raw = path.read_bytes()
    task = json.loads(raw)
    operation, context = current_provider_selection_review_operation(task)
    if context.get('lifecycle') != 'pending' or not context.get('request_id'):
        raise ValueError('首次作品交接缺少实际待处理选择请求')
    return raw, task, operation, context


def pending_movie_selection(plan, state):
    """Shared routing/build boundary, never a permit to reopen finished work."""
    from .provider_selection_authority import is_confirmed_movie_plan
    return (plan is not None and state is not None and state.content_selection is not None
        and state.content_selection.to_dict()['policy'] == content.DEFAULT_POLICY
        and plan.metadata.get('mapping_source') == 'inventory_review_draft'
        and (not state.confirmed_work or is_confirmed_movie_plan(plan))
        and state.recovery_state in (None, 'healthy')
        and not state.manual_role_decisions and not state.provider_special_decisions
        and not state.provider_subject_boundary_decisions
        and all(getattr(state, name) is None for name in (
            'layout_mode', 'movie_version_selection', 'tv_planning_authority', 
             'completed_rebuild_observation', 'asset_operation', 'execution_failure',
            'series_binding', 'catalog_link_intent', 'season_binding', 'pending_tv_selection',
            'provider_link_binding', 'source_episode_projection', 'source_episode_projection_decision')))


def prepare(service, recognition):
    if type(recognition) is not RecognitionDecisionInput:
        raise ValueError('首次作品选择必须消费类型化识别结果')
    plan = service.plan_store.load(recognition.task_id)
    state = service.decision_state_store.load(recognition.task_id)
    _, _, operation, context = _task(service, recognition.task_id)
    intent = InitialMovieSelectionIntent(recognition.task_id, service.plan_store.fingerprint(plan),
        DecisionStateStore.fingerprint(state), state.initial_source_basis.fingerprint if state.initial_source_basis else '',
        operation.fingerprint, str(context['request_id']), recognition)
    return intent


def build(service, intent, current, state, terminal):
    from .default_content import validate_origin, require_unexecuted_task
    from .provider_selection_authority import (provider_selection_capability,
        task_media_kind_from_task, task_library_domain_from_task)
    from ..task_queue import TaskQueueManager
    if type(intent) is not InitialMovieSelectionIntent or type(intent.recognition) is not RecognitionDecisionInput:
        raise ValueError('首次作品交接缺少类型化意图')
    basis = validate_origin(service, current, state)
    if (intent.plan_fingerprint != service.plan_store.fingerprint(current)
            or intent.decision_fingerprint != DecisionStateStore.fingerprint(state)
            or intent.basis_fingerprint != basis.fingerprint
            or intent.recognition.task_id != current.task_id
            or intent.recognition.expected_previous_fingerprint != intent.decision_fingerprint):
        raise ValueError('首次作品选择的当前记录或来源已变化')
    choice = content.validate_plan_content(current, state)
    if choice is None or not pending_movie_selection(current, state):
        raise ValueError('仅允许正片内容尚未完成的v2库存选择电影')
    raw, task, operation, context = _task(service, intent.task_id)
    if (type(terminal) is not TaskTerminalMutation or terminal.task_id != intent.task_id
            or terminal.task_root != service.task_root or terminal.previous_bytes != raw):
        raise ValueError('作品选择请求与实际任务提交快照不一致')
    require_unexecuted_task(task, recognition_worker=True)
    if (operation.fingerprint != intent.operation_fingerprint or context['request_id'] != intent.request_id
            or context.get('expected_decision_revision') != state.revision):
        raise ValueError('实际作品选择请求已变化')
    observation = intent.recognition
    pin = observation.initial_movie_request
    identity = tuple(str((context.get('candidate') or {}).get(k) or '').strip().casefold()
                     for k in ('namespace', 'entity_type', 'provider_id'))
    if (type(pin) is not InitialMovieRequest or pin.task_id != intent.task_id
            or pin.request_id != intent.request_id or pin.expected_revision != state.revision
            or pin.candidate_identity != identity):
        raise ValueError('原worker请求与当前作品选择不一致，不能重新认领后来请求')
    work = observation.work_identity
    candidate = context.get('candidate')
    if (work.status != 'confirmed' or (work.primary_provider.namespace, work.primary_provider.entity_type) not in {('tmdb', 'movie'), ('bangumi', 'subject')}
            or observation.layout_mode is not None or observation.season_number is not None
            or work.domain != task_library_domain_from_task(task)):
        raise ValueError('首次电影交接不能改为TV、跨域或携带完成语义')
    capability = provider_selection_capability(candidate, group_kind='movie_continuous',
        task_media_kind=task_media_kind_from_task(task), task_library_domain=task_library_domain_from_task(task))
    decision = observation.candidate(state)
    # Reuse the queue's provider evidence predicate. A typed object alone is
    # not proof that this is the actual pending user-selected provider.
    draft = DecisionStateStore.project_plan(decision, replace(current, projection_receipt=None,
        metadata={**current.metadata, 'initial_movie_selection_result': InitialMovieSelectionResult(
            intent.request_id, intent.operation_fingerprint, state.revision, state.revision + 1, identity).to_dict()},
        revision_parent_fingerprint=intent.plan_fingerprint, revision_action='initial_movie_selection',
        revision_reason='确认电影作品身份，保留待完成内容'))
    if not capability.actionable or not TaskQueueManager._selected_provider_was_persisted(
            candidate, tuple(decision.provider_evidence), draft):
        raise ValueError('识别结果与实际选择的Provider证据不一致')
    content.validate_source_identities(choice, plan=current)
    content.validate_omission_manifest(choice, movie_selection=None)
    return InitialMovieSelection(decision, draft)


def commit(service, intent, *, task_terminal):
    from .public_revision import commit as publish
    return publish(service, intent, task_terminal=task_terminal)
