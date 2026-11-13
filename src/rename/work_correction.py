"""Explicit same-card TV work correction; observations never approve."""
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import json
import re
from weakref import WeakKeyDictionary

from .content_associations import encode
from .content_choice import _load
from .decision_state import DecisionStateStore
from .initial_tv_completion import InitialTVCompletionContext
from .plan_store import PlanStore
from .tv_completion_observations import TVSeasonObservation
from .provider_operation import provider_operation


def _target_id(candidate):
    if (not isinstance(candidate, dict) or not supported_identity(candidate)
            or not re.fullmatch(r'[1-9][0-9]{0,11}', str(candidate.get('provider_id') or ''))):
        raise ValueError('请选择 TMDB 剧集或 Bangumi 动画分集条目；原计划未改变')
    return str(candidate['provider_id'])


def supported_identity(provider):
    return (provider.get('namespace'), provider.get('entity_type')) in {('tmdb', 'tv'), ('bangumi', 'subject')}


def _same_identity(left, right):
    from .season_identity import provider_key
    return provider_key(left) == provider_key(right)


def _work(provider_id, detail_json, observed_at, library_domain='general', namespace='tmdb'):
    from .tmdb_genre import classify_tmdb_animation_evidence
    from .process import _planning_provider_identity, _resolve_plan_naming_decision
    from .rename_plan import WorkIdentity
    info = json.loads(detail_json)
    if namespace == 'bangumi':
        from .rename_plan import ProviderIdentity
        from .season_identity import provider_key
        from .provider_selection_authority import validate_bangumi_subject
        validate_bangumi_subject(info, provider_id, library_domain, 'tv_episodes')
        if encode(info) != detail_json or not isinstance(observed_at, str) or not observed_at:
            raise ValueError('Bangumi 观察格式无效')
        title = str(info.get('name_cn') or info.get('name') or '').strip()
        if not title:
            raise ValueError('Bangumi 条目缺少标题；原计划未改变')
        date = str(info.get('date') or '')
        year = int(date[:4]) if date[:4].isdigit() else None
        provider = ProviderIdentity('confirmed', 'bangumi', 'subject', provider_id, title, year,
            'explicit_user', ('人工更正作品，已读取同ID的Bangumi动画资料',),
            original_title=str(info.get('name') or ''), source_url=f'https://bgm.tv/subject/{provider_id}',
            snapshot_at=observed_at)
        work = WorkIdentity(status='confirmed', domain=library_domain, title=title, year=year,
            primary_provider=provider, domain_source='provider_selection')
        # A Bangumi subject does not publish a season list; never invent one.
        if str(info.get('platform', '')).casefold() == 'ova':
            from .ova_selection import OvaSubject
            return work, TVSeasonObservation.capture(work, OvaSubject.capture(info).info())
        return work, TVSeasonObservation(provider_key(provider.to_dict()), ())
    genres = info.get('genres') if isinstance(info, dict) else None
    if (not isinstance(info, dict) or encode(info) != detail_json
            or type(info.get('id')) is not int or str(info['id']) != provider_id
            or not isinstance(info.get('name'), str) or not info['name'].strip()
            or not isinstance(genres, list) or not genres
            or any(not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0 for row in genres)
            or 'genre_ids' in info and (not isinstance(info['genre_ids'], list)
                or any(type(value) is not int or value <= 0 for value in info['genre_ids']))
            or any(key.startswith('_bar_') for key in info)
            or not isinstance(observed_at, str) or not observed_at):
        raise ValueError('新作品详情的 ID、标题或完整类型资料无法核对；原计划未改变')
    if (library_domain not in {'general', 'animation'} or
            classify_tmdb_animation_evidence(genres=genres, genre_ids=info.get('genre_ids', ())).is_anime is not (library_domain == 'animation')):
        raise ValueError('新作品类型与当前媒体库不一致，或类型证据冲突；原计划未改变')
    naming = _resolve_plan_naming_decision(name=info['name'], info=info, entity_type='tv', provider_selection=None)
    provider, year = _planning_provider_identity(info=info, name=naming.display_title, is_movie=False,
        selection={'snapshot_at': observed_at, 'original_title': info.get('original_name')},
        decision_source='explicit_user', evidence=('人工更正作品，已读取同ID完整TMDB剧集资料',))
    work = WorkIdentity(status='confirmed', domain=library_domain, title=naming.display_title, year=year,
        primary_provider=provider, domain_source='provider_selection')
    seasons = TVSeasonObservation.capture(work, info)
    return work, seasons


def _original(service, current, state, queue_request=None):
    from .default_content import _origin, require_unexecuted_task
    from .content_selection import validate_plan_content, validate_source_identities
    from .selection_gate import validate_selection_for_plan
    from ..utils.utils import task_record_snapshot_at
    _origin(service, current, state)
    choice = validate_plan_content(current, state)
    _raw, task, task_fingerprint = task_record_snapshot_at(service.task_root, current.task_id)
    if queue_request is not None:
        from .queued_work_selection import validate_request
        validate_request(service, current, state, queue_request)
    else:
        validate_selection_for_plan(task)
    require_unexecuted_task(task, recognition_worker=queue_request is not None)
    primary = state.confirmed_work.get('primary_provider') or {}
    if (state.library_domain not in {'general', 'animation'} or state.layout_mode != 'tv_episodes'
            or not supported_identity(primary)
            or 'tv_completion_resolution' not in current.metadata
            or state.recovery_state not in (None, 'healthy')
            or any(getattr(state, key) is not None for key in ('execution_failure', 
                'completed_rebuild_observation', 'pending_tv_selection'))):
        raise ValueError('仅未执行、无换源或恢复风险的完整剧集计划支持此更正')
    from .asset_operation import require_tv_editable_operation
    require_tv_editable_operation(state.asset_operation, 'work')
    validate_source_identities(choice, plan=current)
    return task_fingerprint


@dataclass(frozen=True, eq=False)
class WorkCorrectionObservation:
    context: InitialTVCompletionContext
    task_id: str
    plan_fingerprint: str
    decision_fingerprint: str
    task_fingerprint: str
    roots: object
    provider_id: str
    detail_json: str
    observed_at: str
    library_domain: str = 'general'
    namespace: str = 'tmdb'
    special_json: str = 'null'
    queue_request: object = None
    candidate_json: str = ''

    def _issued_values(self):
        return tuple(getattr(self, field.name) for field in fields(self))

    def target(self):
        work, seasons = _work(self.provider_id, self.detail_json, self.observed_at, self.library_domain, self.namespace)
        if self.queue_request is not None:
            from .queued_work_selection import frozen_work
            work = frozen_work(work, self.candidate_json)
        return work, seasons

    def validate(self, service, current, state):
        if type(self.context) is not InitialTVCompletionContext or self.context.service is not service:
            raise ValueError('作品更正不属于原审核服务')
        issued = getattr(service, '_work_correction_observations', None)
        if type(issued) is not WeakKeyDictionary or issued.get(self) != self._issued_values():
            raise ValueError('新作品观察不是本服务实际读取的原资料，或已失效；请重新读取')
        self.context.validate()
        if (current.task_id != self.task_id or PlanStore.fingerprint(current) != self.plan_fingerprint
                or DecisionStateStore.fingerprint(state) != self.decision_fingerprint
                or self.context.roots.observe() != self.roots
                or _original(service, current, state, self.queue_request) != self.task_fingerprint):
            raise ValueError('原计划、任务、来源或目标配置已变化，请重新打开作品更正')
        if self.queue_request is not None:
            from .queued_work_selection import validate_request
            if encode(validate_request(service, current, state, self.queue_request)) != self.candidate_json:
                raise ValueError('作品选择候选已变化')
        if self.library_domain != state.library_domain:
            raise ValueError('作品更正不能更换已确认的媒体库')
        work, seasons = self.target()
        return work, seasons


@dataclass(frozen=True)
class WorkCorrectionRequest:
    observation: WorkCorrectionObservation
    season_number: int
    reason: str
    season_source: str = 'explicit_user'

    def recognition(self):
        from .projection_authority import RecognitionDecisionInput
        from .season_identity import require_manual_season
        if type(self.observation) is not WorkCorrectionObservation:
            raise ValueError('作品更正缺少原完整观察')
        work, seasons = self.observation.target()
        require_manual_season(self.season_number)
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError('作品更正缺少操作说明')
        return RecognitionDecisionInput(self.observation.task_id, work, (), self.season_number,
            self.season_source, 'tv_episodes', self.observation.decision_fingerprint,
            initial_movie_request=self.observation.queue_request)

    def validate(self, service, current, state):
        recognition = self.recognition()
        work, seasons = self.observation.validate(service, current, state)
        if self.observation.queue_request is not None:
            from .season_identity import resolve_initial_tv_season
            if resolve_initial_tv_season(seasons, work, current.source_root, self.observation.queue_request.requested_season, None) != (self.season_number, self.season_source):
                raise ValueError('季号与本次选择请求不一致')
        return recognition


@provider_operation
def observe(service, task_id, fingerprint, *, expected_revision, candidate, queue_request=None):
    from .get_info import Search
    current, state = _load(service, task_id, fingerprint)
    if type(expected_revision) is not int or expected_revision != state.revision:
        raise ValueError('作品更正页面已过期，请刷新')
    context = InitialTVCompletionContext.from_service(service)
    task_fingerprint = _original(service, current, state, queue_request)
    roots = context.roots.observe()
    provider_id = _target_id(candidate)
    from .episode_range import require_unbound
    require_unbound(state)
    namespace = candidate['namespace']
    if namespace == 'bangumi':
        from .provider_special_evidence import bangumi_client
        client = bangumi_client()
        info = client._get_json(f'/v0/subjects/{provider_id}')
        keep = ('id', 'type', 'name', 'name_cn', 'date', 'platform')
    else:
        client = Search()
        _name, info = client.get_tv_info_by_id(int(provider_id))
        keep = ('id', 'name', 'original_name', 'first_air_date', 'genres', 'genre_ids', 'seasons')
    if not isinstance(info, dict):
        raise ValueError('无法取得新作品完整资料，原计划未改变')
    # Freeze only business observations, not transport internals or credentials.
    info = {key: info[key] for key in keep if key in info}
    observed_at = datetime.now(timezone.utc).isoformat()
    work, _ = _work(provider_id, encode(info), observed_at, state.library_domain, namespace)
    from .special_crosswalk import observe as observe_specials
    from .season_reference_record import read as read_reference, record_path
    primary = work.primary_provider.to_dict()
    reference = read_reference(record_path(service.task_root, task_id, primary), primary)
    special = observe_specials(primary, 0, reference=reference,
        **({'bgm': client} if namespace == 'bangumi' else {'tmdb': client}))
    if not special.displayable:
        raise ValueError('新作品的官方特别篇资料读取失败；原计划未改变，请重试')
    observation = WorkCorrectionObservation(context, task_id, fingerprint, DecisionStateStore.fingerprint(state),
        task_fingerprint, roots, provider_id, encode(info), observed_at, state.library_domain,
        namespace, encode(special.to_dict() if special else None), queue_request, encode(candidate) if queue_request else '')
    issued = getattr(service, '_work_correction_observations', None)
    if issued is None:
        # Only live previews retain these observations. No Task, journal or
        # durable permission is created, and a copied dataclass is not issued.
        issued = service._work_correction_observations = WeakKeyDictionary()
    if type(issued) is not WeakKeyDictionary:
        raise ValueError('新作品观察服务状态无效，请重新打开审核页')
    issued[observation] = observation._issued_values()
    try:
        current, state = _load(service, task_id, fingerprint)
        observation.validate(service, current, state)
    except Exception:
        issued.pop(observation, None)
        raise
    return observation


def prepare(service, observation, *, season_number, reason, affiliation_choice=None, season_source='explicit_user'):
    from .initial_tv_completion import prepare as prepare_tv
    if type(observation) is not WorkCorrectionObservation:
        raise ValueError('作品更正必须先读取新作品并预览')
    current, state = _load(service, observation.task_id, observation.plan_fingerprint)
    request = WorkCorrectionRequest(observation, season_number, reason, season_source)
    recognition = request.validate(service, current, state)
    _work_identity, seasons = observation.target()
    return prepare_tv(service, recognition, seasons=seasons, special=json.loads(observation.special_json),
                      mode='work', work_request=request, affiliation_choice=affiliation_choice)


def validate_inputs(service, current, state, inputs):
    request = inputs.work_request
    if type(request) is not WorkCorrectionRequest or inputs.mode != 'work':
        raise ValueError('作品更正缺少明确原件和目标观察请求')
    if (request.validate(service, current, state) != inputs.recognition
            or inputs.seasons != request.observation.target()[1]
            or inputs.resolution is not None or inputs.special_json != request.observation.special_json
            or inputs.edit_json != '{}' or inputs.source_input is not None or inputs.refresh_request is not None):
        raise ValueError('作品更正的身份、季度或内容不属于原完整请求')


def commit(service, intent):
    from .initial_tv_completion import InitialTVCompletionIntent, commit as commit_tv
    if type(intent) is not InitialTVCompletionIntent or intent.inputs.mode != 'work':
        raise ValueError('请先预览本次作品更正，不能提交其它类型操作')
    _state, plan = commit_tv(service, intent, task_terminal=service._revision_task_terminal(intent.task_id))
    return service._record_revision(plan)
