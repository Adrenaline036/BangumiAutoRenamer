"""单队列内完成已有计划的作品选择，复用现有计划事务。"""
from dataclasses import replace
import json

from .content_associations import encode
from .initial_movie_selection import InitialMovieRequest, _task
from .season_identity import provider_key


def complete_kind(plan, state, candidate):
    if plan is None or state is None or state.content_selection is None or not state.confirmed_work:
        return None
    from .work_correction import supported_identity
    from .movie_work_correction import supported
    if state.layout_mode == 'tv_episodes' and 'tv_completion_resolution' in plan.metadata and supported_identity(candidate):
        return 'tv'
    if state.layout_mode == 'movie_versions' and state.movie_version_selection is not None and supported(candidate):
        return 'movie'
    return None


def validate_request(service, current, state, pin):
    from .default_content import require_unexecuted_task
    from .decision_state import DecisionStateStore
    _, task, _, operation = _task(service, current.task_id)
    candidate = operation.get('candidate') or {}
    if (type(pin) is not InitialMovieRequest or pin.task_id != current.task_id
            or pin.request_id != operation.get('request_id') or pin.expected_revision != state.revision
            or operation.get('expected_decision_revision') != state.revision
            or provider_key(candidate) != pin.candidate_identity
            or 'requested_season_number' not in operation
            or operation['requested_season_number'] != pin.requested_season
            or current.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(state)):
        raise ValueError('作品选择不属于当前后台请求，请重新选择')
    require_unexecuted_task(task, recognition_worker=True)
    return candidate


def frozen_work(work, candidate_json):
    candidate = json.loads(candidate_json)
    if provider_key(candidate) != provider_key(work.primary_provider.to_dict()):
        raise ValueError('作品资料与排队条目不一致')
    fields = {k: candidate[k] for k in ('snapshot_at', 'source_url', 'poster_url') if candidate.get(k)}
    return replace(work, primary_provider=replace(work.primary_provider, **fields))


def run(manager, job):
    if job.operation != 'reidentify':
        return False, None
    current = manager.plan_store.load(job.task_id)
    state = manager.decision_state_store.load(job.task_id)
    kind = complete_kind(current, state, job.provider_selection)
    if kind is None:
        return False, None
    service = manager.execution_service.review_service
    if service.root_snapshot_source is None:
        service.root_snapshot_source = manager.root_snapshot_source
    pin = InitialMovieRequest.from_job(job)
    candidate = validate_request(service, current, state, pin)
    fingerprint = service.plan_store.fingerprint(current)
    if kind == 'movie':
        from .movie_work_correction import observe, prepare_observed
        from .initial_movie_completion import commit
        observation = observe(service, job.task_id, fingerprint, expected_revision=state.revision,
                              candidate=candidate, queue_request=pin)
        return True, commit(service, prepare_observed(service, observation))
    from .work_correction import observe, prepare, commit
    from .season_identity import resolve_initial_tv_season, SeasonConfirmationRequired
    from .ova_selection import TVSeasonPending
    observation = observe(service, job.task_id, fingerprint, expected_revision=state.revision,
                          candidate=candidate, queue_request=pin)
    work, seasons = observation.target()
    try:
        if seasons.ova_subject is not None and pin.requested_season is None:
            raise SeasonConfirmationRequired('请选择独立 OVA 的季号')
        season, source = resolve_initial_tv_season(seasons, work, current.source_root, pin.requested_season, None)
    except SeasonConfirmationRequired:
        return True, TVSeasonPending(job.task_id, encode(candidate), encode(seasons.to_dict()))
    intent = prepare(service, observation, season_number=season, season_source=source, reason='更换作品')
    return True, commit(service, intent)
