"""Same-card movie identity correction through the existing complete projector."""
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
import json
import re
from weakref import WeakKeyDictionary

from .content_associations import encode
from .content_choice import _load
from .decision_state import DecisionStateStore
from .initial_movie_completion import InitialMovieCompletionContext
from .plan_store import PlanStore


def supported(provider):
    return (provider.get('namespace'), provider.get('entity_type')) in {('tmdb','movie'),('bangumi','subject')}


def target(detail_json, namespace, identity, domain, observed_at):
    from .provider_selection_authority import validate_bangumi_subject
    from .rename_plan import ProviderIdentity, WorkIdentity
    from .tmdb_genre import classify_tmdb_animation_evidence
    from .process import _planning_provider_identity, _resolve_plan_naming_decision
    info = json.loads(detail_json)
    if encode(info) != detail_json or not observed_at:
        raise ValueError('电影资料观察格式无效')
    if namespace == 'bangumi':
        validate_bangumi_subject(info, identity, domain, 'movie_continuous')
        title = str(info.get('name_cn') or info['name']).strip()
        date = str(info.get('date') or '')
        year = int(date[:4]) if date[:4].isdigit() else None
        provider = ProviderIdentity('confirmed','bangumi','subject',identity,title,year,'explicit_user',
            ('人工更正电影，已读取同ID完整Bangumi资料',), original_title=info.get('name'),
            source_url=f'https://bgm.tv/subject/{identity}',snapshot_at=observed_at)
    elif namespace == 'tmdb':
        genres = info.get('genres') if isinstance(info,dict) else None
        if (type(info.get('id')) is not int or str(info['id']) != identity
                or not isinstance(info.get('title'),str) or not info['title'].strip()
                or not isinstance(genres,list) or not genres
                or any(not isinstance(r,dict) or type(r.get('id')) is not int or r['id'] <= 0 for r in genres)
                or any(k.startswith('_bar_') for k in info)
                or domain not in {'animation','general'}
                or classify_tmdb_animation_evidence(genres=genres).is_anime is not (domain=='animation')):
            raise ValueError('新电影身份或完整类型资料与当前媒体库不一致')
        naming = _resolve_plan_naming_decision(name=info['title'],info=info,entity_type='movie',provider_selection=None)
        title = naming.display_title
        provider,year = _planning_provider_identity(info=info,name=title,is_movie=True,
            selection={'snapshot_at':observed_at,'original_title':info.get('original_title')},
            decision_source='explicit_user',evidence=('人工更正电影，已读取同ID完整TMDB资料',))
    else:
        raise ValueError('请选择电影资料来源')
    return WorkIdentity(status='confirmed',domain=domain,title=title,year=year,
        primary_provider=provider,domain_source='provider_selection')


def original(service,current,state, *, domain_only=False, queue_request=None):
    from .default_content import _origin, require_unexecuted_task
    from . import content_selection as content
    from .selection_gate import validate_selection_for_plan
    from ..utils.utils import task_record_snapshot_at
    from .provider_selection_authority import is_confirmed_movie_plan
    _origin(service,current,state)
    _raw, task, fingerprint = task_record_snapshot_at(service.task_root,current.task_id)
    if queue_request is not None:
        from .queued_work_selection import validate_request
        validate_request(service, current, state, queue_request)
    else:
        validate_selection_for_plan(task)
    require_unexecuted_task(task, recognition_worker=queue_request is not None)
    if domain_only and state.movie_version_selection is None:
        from .initial_movie_completion import _validate_current
        _validate_current(service, current, state)
        return fingerprint
    if (not is_confirmed_movie_plan(current) or state.layout_mode != 'movie_versions'
            or state.movie_version_selection is None or state.recovery_state not in (None,'healthy')
            or any(getattr(state,k) is not None for k in ('execution_failure',
                'completed_rebuild_observation','pending_tv_selection'))):
        raise ValueError('电影更正需要未执行且无恢复风险的完整电影计划')
    if state.asset_operation is not None:
        operation = state.asset_operation.to_dict()
        if operation['mode'] not in {'append','replace'}:
            raise ValueError('当前恢复操作不能更换电影身份')
    choice = content.validate_plan_content(current,state)
    content.validate_source_identities(choice,plan=current)
    content.validate_omission_manifest(choice,movie_selection=state.movie_version_selection)
    return fingerprint


@dataclass(frozen=True,eq=False)
class MovieWorkObservation:
    context: InitialMovieCompletionContext
    task_id: str
    plan_fingerprint: str
    decision_fingerprint: str
    task_fingerprint: str
    roots: object
    namespace: str
    identity: str
    domain: str
    detail_json: str
    observed_at: str
    domain_work: object = None
    queue_request: object = None
    candidate_json: str = ''

    def values(self):
        return tuple(getattr(self,f.name) for f in fields(self))

    def target(self):
        if self.domain_work is not None:
            return replace(self.domain_work, domain=self.domain, domain_source='manual_library_selection')
        work = target(self.detail_json,self.namespace,self.identity,self.domain,self.observed_at)
        if self.queue_request is not None:
            from .queued_work_selection import frozen_work
            work = frozen_work(work, self.candidate_json)
        return work

    def validate(self,service,current,state):
        issued = getattr(service,'_movie_work_observations',None)
        if (type(issued) is not WeakKeyDictionary or issued.get(self) != self.values()
                or self.context.service is not service):
            raise ValueError('电影更正资料不是本服务实际读取的原件，请重新预览')
        self.context.validate()
        if (self.task_id != current.task_id or PlanStore.fingerprint(current) != self.plan_fingerprint
                or DecisionStateStore.fingerprint(state) != self.decision_fingerprint
                or self.context.roots.observe() != self.roots
                or (self.domain_work is None and self.domain != state.library_domain)
                or (self.domain_work is not None and (self.domain_work != current.work_identity
                    or self.domain not in {'animation', 'general'}))
                or original(service,current,state, domain_only=self.domain_work is not None, queue_request=self.queue_request) != self.task_fingerprint):
            raise ValueError('电影计划、来源或配置已变化，请重新预览')
        if self.queue_request is not None:
            from .queued_work_selection import validate_request
            if encode(validate_request(service, current, state, self.queue_request)) != self.candidate_json:
                raise ValueError('电影选择候选已变化')
        return self.target()


def observe(service,task_id,fingerprint,*,expected_revision,candidate,queue_request=None):
    current,state = _load(service,task_id,fingerprint)
    if type(expected_revision) is not int or expected_revision != state.revision:
        raise ValueError('电影选择页面已过期')
    if not isinstance(candidate,dict) or not supported(candidate):
        raise ValueError('请选择Bangumi电影或TMDB电影')
    identity = str(candidate.get('provider_id') or '')
    if not re.fullmatch(r'[1-9][0-9]{0,11}',identity):
        raise ValueError('电影条目ID无效')
    task_fp = original(service,current,state,queue_request=queue_request)
    context = InitialMovieCompletionContext.from_service(service)
    roots = context.roots.observe()
    namespace = candidate['namespace']
    if namespace == 'bangumi':
        from .provider_special_evidence import bangumi_client
        info = bangumi_client()._get_json(f'/v0/subjects/{identity}')
        keep = ('id','type','platform','name','name_cn','date')
    else:
        from .get_info import Search
        _,info = Search().get_movie_info_by_id(int(identity))
        keep = ('id','title','original_title','release_date','genres')
    if not isinstance(info,dict):
        raise ValueError('无法取得电影完整资料；原计划未改变')
    observation = MovieWorkObservation(context,task_id,fingerprint,DecisionStateStore.fingerprint(state),
        task_fp,roots,namespace,identity,state.library_domain,
        encode({k:info[k] for k in keep if k in info}),datetime.now(timezone.utc).isoformat(),
        queue_request=queue_request, candidate_json=encode(candidate) if queue_request else '')
    observation.target()
    if getattr(service,'_movie_work_observations',None) is None:
        service._movie_work_observations = WeakKeyDictionary()
    service._movie_work_observations[observation] = observation.values()
    current,state = _load(service,task_id,fingerprint)
    observation.validate(service,current,state)
    return observation


def observe_domain(service, task_id, fingerprint, *, expected_revision, domain):
    current, state = _load(service, task_id, fingerprint)
    if domain not in {'animation', 'general'} or expected_revision != state.revision:
        raise ValueError('保存位置选择已变化，请重新核对')
    task_fp = original(service, current, state, domain_only=True)
    context = InitialMovieCompletionContext.from_service(service)
    provider = current.work_identity.primary_provider
    observation = MovieWorkObservation(context, task_id, fingerprint, DecisionStateStore.fingerprint(state),
        task_fp, context.roots.observe(), provider.namespace, provider.provider_id, domain, '{}',
        datetime.now(timezone.utc).isoformat(), current.work_identity)
    if getattr(service, '_movie_work_observations', None) is None:
        service._movie_work_observations = WeakKeyDictionary()
    service._movie_work_observations[observation] = observation.values()
    observation.validate(service, current, state)
    return observation


def rows_for(state):
    return [dict(sources=r['sources'],label=r['label'],technical_descriptor=r['descriptor'])
        for r in state.content_selection.to_dict()['version_groups']]


def preceding_work(cursor, parent):
    """Only a receipted domain correction may change the historical domain."""
    if cursor.revision_action != 'movie_domain_correction':
        return cursor.work_identity
    if (parent.work_identity is None or cursor.work_identity.domain not in {'animation', 'general'}
            or replace(parent.work_identity, domain=cursor.work_identity.domain,
                       domain_source='manual_library_selection') != cursor.work_identity
            or cursor.metadata.get('mapping_source') != 'inventory_review_draft'):
        raise ValueError('保存位置更正不能改变原电影身份或完成状态')
    return parent.work_identity


def excluded_for(state):
    choice = state.movie_version_selection or state.content_selection
    return tuple(sorted(choice.to_dict()['excluded_sources']))


def rebuild(current,state,inputs):
    from .initial_movie_completion import _project_movie_content
    from .projection_authority import RecognitionDecisionInput
    from .movie_version_selection import MovieVersionSelection
    observation = inputs.work_observation
    if (type(observation) is not MovieWorkObservation or observation.task_id != inputs.task_id
            or observation.plan_fingerprint != inputs.plan_fingerprint
            or observation.decision_fingerprint != inputs.decision_fingerprint
            or observation.task_fingerprint != inputs.task_fingerprint or observation.roots != inputs.roots
            or inputs.groups_json != encode(rows_for(state))
            or inputs.excluded != excluded_for(state)):
        raise ValueError('电影更正不能改变原正片、分段、版本或排除范围')
    work = observation.target()
    if state.movie_version_selection is None:
        from .initial_movie_selection import pending_movie_selection
        from .initial_movie_completion import InitialMovieCompletion
        if (not pending_movie_selection(current, state) or observation.domain_work != current.work_identity
                or rows_for(state) or inputs.replace_version_ref is not None):
            raise ValueError('待选正片时只能更正同一电影的保存位置')
        candidate = replace(state, confirmed_work=work.to_dict(), library_domain=work.domain)
        draft = replace(current, projection_receipt=None,
            revision_parent_fingerprint=inputs.plan_fingerprint,
            revision_action='movie_domain_correction', revision_reason='更正电影保存位置，保留待选正片')
        return InitialMovieCompletion(candidate, DecisionStateStore.project_plan(candidate, draft))
    from .work_affiliation import pending_for, AffiliationRequired
    pending = pending_for(work, inputs, dict(groups=rows_for(state), excluded=list(inputs.excluded)))
    if pending is not None and observation.queue_request is None:
        raise AffiliationRequired(pending, inputs)
    candidate = RecognitionDecisionInput(current.task_id,work,layout_mode='movie_versions',
        expected_previous_fingerprint=inputs.decision_fingerprint).candidate(state)
    candidate = replace(candidate, manual_role_decisions=state.manual_role_decisions, library_domain=work.domain)
    if pending is not None:
        from .work_affiliation import retain_inventory
        from .initial_movie_completion import InitialMovieCompletion
        candidate, draft = retain_inventory(current, candidate, pending, inputs)
        return InitialMovieCompletion(candidate, draft)
    linked = current.linked_provider_identities if observation.domain_work is not None else ()
    candidate = replace(candidate, linked_provider_identities=state.linked_provider_identities if observation.domain_work is not None else candidate.linked_provider_identities)
    origin = replace(current, work_identity=work, linked_provider_identities=linked)
    previous = MovieVersionSelection.from_mapping({**state.movie_version_selection.to_dict(),
        'groups':state.content_selection.to_dict()['version_groups'],'excluded_sources':[],'replacement':None})
    built = _project_movie_content(origin,current,candidate,inputs,rows=rows_for(state),previous=previous,
        preferred_version_ref=current.presentation_groups[0].preferred_version_ref,
        revision_action='movie_domain_correction' if observation.domain_work is not None else 'movie_work_correction')
    # First-selection metadata remains immutable historical evidence. The
    # confirmed WorkIdentity and the new receipt carry the current decision.
    return replace(built, draft=replace(built.draft, linked_provider_identities=linked))


def prepare(service,task_id,fingerprint,*,expected_revision,candidate):
    observation = observe(service,task_id,fingerprint,expected_revision=expected_revision,candidate=candidate)
    return prepare_observed(service, observation)


def prepare_observed(service, observation, *, affiliation_choice=None):
    from .initial_movie_completion import CompletionInputs, _namespace, _prepare_intent
    if type(observation) is not MovieWorkObservation:
        raise ValueError('电影更正缺少本服务的实际资料观察')
    task_id, fingerprint = observation.task_id, observation.plan_fingerprint
    current,state = _load(service,task_id,fingerprint)
    observation.validate(service, current, state)
    origin = replace(current,work_identity=observation.target(),linked_provider_identities=())
    with _namespace(service,origin, roots=observation.roots, choice=affiliation_choice) as (key,namespace):
        inputs = CompletionInputs(task_id,fingerprint,observation.decision_fingerprint,
            observation.task_fingerprint,key,namespace,observation.roots,encode(rows_for(state)),
            excluded_for(state),
            '人工更正电影作品',encode(state.content_selection.to_dict()['evidence']),observation,
            affiliation_choice=affiliation_choice)
        return _prepare_intent(service,current,state,inputs,observation.context)
