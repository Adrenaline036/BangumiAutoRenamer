"""已核实的独立OVA作品观察与等待人工季号的队列结果。"""
from dataclasses import dataclass
import json

from .content_associations import encode
from .provider_selection_authority import validate_bangumi_subject


@dataclass(frozen=True)
class OvaSubject:
    payload: str

    def __post_init__(self):
        value = json.loads(self.payload)
        if (set(value) != {'id', 'type', 'platform', 'name', 'name_cn', 'date'}
                or encode(value) != self.payload or value['platform'] != 'OVA'):
            raise ValueError('独立OVA作品资料无效')
        validate_bangumi_subject(value, str(value['id']), 'animation', 'tv_episodes')

    @classmethod
    def capture(cls, detail):
        value = {key: detail.get(key, '') for key in ('id', 'type', 'platform', 'name', 'name_cn', 'date')}
        value['platform'] = str(value['platform']).upper()
        return cls(encode(value))

    @property
    def provider_id(self):
        return str(json.loads(self.payload)['id'])

    def info(self):
        value = json.loads(self.payload)
        name = value['name_cn'] or value['name']
        return dict(id=value['id'], name=name, title=name, original_name=value['name'],
                    first_air_date=value['date'], _bar_ova_subject=value,
                    _bar_primary_provider=dict(namespace='bangumi', entity_type='subject', provider_id=self.provider_id))


@dataclass(frozen=True)
class OvaSeasonPending:
    task_id: str
    candidate_json: str
    subject: OvaSubject

    def __post_init__(self):
        candidate = json.loads(self.candidate_json)
        if (not self.task_id or type(self.subject) is not OvaSubject or encode(candidate) != self.candidate_json
                or tuple(str(candidate.get(k, '')) for k in ('namespace', 'entity_type', 'provider_id'))
                != ('bangumi', 'subject', self.subject.provider_id)):
            raise ValueError('独立OVA等待季号结果与作品不一致')

    def to_dict(self):
        return dict(schema_version=1, task_id=self.task_id, candidate=json.loads(self.candidate_json),
                    subject=json.loads(self.subject.payload))

    @classmethod
    def from_mapping(cls, value):
        if (type(value) is not dict or set(value) != {'schema_version', 'task_id', 'candidate', 'subject'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1):
            raise ValueError('独立OVA等待季号结果版本无效')
        return cls(value['task_id'], encode(value['candidate']), OvaSubject(encode(value['subject'])))


@dataclass(frozen=True)
class TVSeasonPending:
    """已核实作品的季度观察；只允许原选择请求继续人工确认。"""
    task_id: str
    candidate_json: str
    observation_json: str

    def __post_init__(self):
        from .tv_completion_observations import TVSeasonObservation
        from .provider_selection_authority import provider_candidate_group_kind
        from .season_identity import provider_key
        candidate = json.loads(self.candidate_json)
        observation = TVSeasonObservation.from_mapping(json.loads(self.observation_json))
        if (not self.task_id or encode(candidate) != self.candidate_json
                or encode(observation.to_dict()) != self.observation_json
                or provider_candidate_group_kind(candidate) != 'tv_episodes'
                or observation.provider != provider_key(candidate)):
            raise ValueError('等待季度确认的资料与所选作品不一致')

    @property
    def subject(self):
        return self

    @property
    def available(self):
        return tuple(json.loads(self.observation_json)['available'])

    def to_dict(self):
        return dict(schema_version=1, task_id=self.task_id, candidate=json.loads(self.candidate_json),
                    seasons=json.loads(self.observation_json))

    @classmethod
    def from_mapping(cls, value):
        if (type(value) is not dict or set(value) != {'schema_version', 'task_id', 'candidate', 'seasons'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1):
            raise ValueError('等待季度确认的资料版本无效')
        return cls(value['task_id'], encode(value['candidate']), encode(value['seasons']))


def persist_pending(manager, job, outcome):
    """任务操作锁与原计划事务锁内只发布待确认状态，保留原计划字节。"""
    from .recognition_queue_admission import _authority_locks
    from .decision_state import DecisionStateStore
    from .plan_store import PlanStore
    from .review_operation import current_provider_selection_review_operation, transition_provider_selection_review_operation, set_review_operation
    from ..utils.utils import mutate_task_at
    from ..task_queue import utc_now
    if type(outcome) not in (OvaSeasonPending, TVSeasonPending) or not job.request_id or job.expected_revision is None:
        raise ValueError('等待季号结果缺少原请求')
    result = type(outcome).from_mapping(outcome.to_dict())
    selection_key = 'tv_season_selection' if type(result) is TVSeasonPending else 'ova_subject_selection'
    if result.task_id != job.task_id or json.loads(result.candidate_json) != job.provider_selection:
        raise ValueError('等待季号结果不属于本次选择')
    with _authority_locks(manager, job.task_id):
        state = manager.decision_state_store.load(job.task_id)
        plan = manager.plan_store.load(job.task_id)
        if (state.revision != job.expected_revision or plan.metadata.get('decision_state_revision') != state.revision
                or plan.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(state)):
            raise ValueError('等待季号期间原任务已更新')
        fingerprint = PlanStore.fingerprint(plan)
        def apply(data):
            operation, context = current_provider_selection_review_operation(data)
            if (context.get('request_id') != job.request_id or context.get('lifecycle') != 'pending'
                    or context.get('expected_decision_revision') != state.revision):
                raise ValueError('等待季号结果已被新的选择取代')
            revised = transition_provider_selection_review_operation(data, dict(state='awaiting_season',
                result_decision_revision=state.revision, result_plan_fingerprint=fingerprint,
                **{selection_key: result.to_dict()}, failure_code=None, reason_codes=['manual_season_required'],
                retry_requires_refresh=False, retryable=False, finished_at=utc_now()), expected_fingerprint=operation.fingerprint)
            set_review_operation(data, revised)
            data.update(workflow_status='need_confirm', error=None, queue_position=None, completed_at=None,
                        provider_selection=json.loads(result.candidate_json))
            data.pop('provider_selection_failure', None)
            return data
        if mutate_task_at(manager.task_root, job.task_id, apply) is None:
            raise ValueError('等待季号的原任务已不存在')


def pending_subject(task, context):
    """验证当前任务所保存的OVA选择；未知状态或缺证据不能开启手动继续。"""
    from .special_catalog import plain
    if context.get('lifecycle') != 'awaiting_season':
        return None
    result = (TVSeasonPending.from_mapping(plain(context['tv_season_selection'])) if 'tv_season_selection' in context
              else OvaSeasonPending.from_mapping(plain(context.get('ova_subject_selection'))))
    if (result.task_id != str(task.get('task_id') or task.get('uuid') or '')
            or json.loads(result.candidate_json) != plain(context.get('candidate'))
            or context.get('expected_decision_revision') != context.get('result_decision_revision')
            or not context.get('result_plan_fingerprint')):
        raise ValueError('等待季号资料与当前任务不一致')
    return result.subject
