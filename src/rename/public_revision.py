"""公共人工命令在现有事务锁内生成并发布一次；不接受外部成品计划。"""
from contextlib import nullcontext
from dataclasses import dataclass, replace

from . import content_selection as content
from .content_choice import _load
from .projection_authority import (ProjectionCommitService, ProjectionAuthorityEnvelope,
    TaskTerminalMutation, inventory_fingerprint_for_plan, _episode_rows_from_plan)


@dataclass(frozen=True)
class SubtitleChange:
    task_id: str
    plan_fingerprint: str
    rules: tuple
    owners: tuple
    action: str
    reason: str
    batch: tuple = ()


@dataclass(frozen=True)
class Publication:
    current: object
    command: object
    envelope: ProjectionAuthorityEnvelope
    built: object


def commit(service, command, *, task_terminal, persist=True):
    from .initial_tv_completion import InitialTVCompletionIntent
    from .movie_content import MovieChangeIntent, MovieChangeContext
    from .initial_movie_selection import InitialMovieSelectionIntent
    if type(command) not in (InitialTVCompletionIntent, MovieChangeIntent, SubtitleChange, InitialMovieSelectionIntent):
        raise ValueError('公共修订只接受明确的人工命令')
    selection = command if type(command) is InitialMovieSelectionIntent else None
    tv = command if type(command) is InitialTVCompletionIntent else None
    movie = command if type(command) is MovieChangeIntent else None
    context = tv.context if tv else MovieChangeContext.from_service(service) if movie else None
    if tv and context.service is not service:
        raise ValueError('TV命令不属于当前服务')
    transaction = ProjectionCommitService(service.decision_state_store, service.plan_store,
        task_root=service.task_root, initial_tv_completion_context=context if tv else None,
        movie_change_context=context if movie else None)
    transaction.initial_tv_completion_terminal = task_terminal if tv else None
    # 生成只占本任务事务锁，登记快照在生成前后分别核对。
    with nullcontext():
        with transaction._transaction_lock(command.task_id):
            fingerprint = command.inputs.plan_fingerprint if tv else command.plan_fingerprint
            current, state = _load(service, command.task_id, fingerprint)
            if not tv and not selection:
                from .default_content import require_unexecuted_task
                from ..utils.utils import read_task_at
                require_unexecuted_task(read_task_at(service.task_root, command.task_id))
            if tv:
                from .initial_tv_completion import _validate_observations
                with context.publication_lock(transaction, command):
                    if _validate_observations(service, current, state, command) is not command.inputs:
                        raise ValueError('TV输入核查已断开')
            movie_observation = None
            if movie:
                from .movie_content import _catalog, _key
                with context.publication_lock(transaction, command):
                    key = _key(current)
                    movie_observation = (context.catalog.namespace_snapshot(key), _catalog(context.catalog, current))
                    if movie_observation[1][0] != command.catalog_fingerprint:
                        raise ValueError('电影版本预览后的作品登记已变化')
            built, candidate, draft = generate(service, command, current, state, task_terminal,
                movie_observation=movie_observation)
            if draft.task_id != current.task_id or candidate.task_id != state.task_id:
                raise ValueError('公共生成结果属于其它任务')
            envelope = ProjectionAuthorityEnvelope(state, candidate, draft,
                inventory_fingerprint_for_plan(draft), _episode_rows_from_plan(draft),
                cohort_authority=built.cohort if tv else None, initial_tv_completion=tv, movie_change=movie, initial_movie_selection=selection,
                automatic_recognition=tv.inputs.recognition.automatic_recognition if tv else None)
            transaction._public_revision = Publication(current, command, envelope, built)
            try:
                require_publication(transaction, envelope)
                with context.publication_lock(transaction, command) if tv or movie else nullcontext():
                    if tv:
                        # 生成期间允许其他作品登记，保存必须仍匹配原冻结输入。
                        if _validate_observations(service, current, state, command) is not command.inputs:
                            raise ValueError('TV输入核查已断开')
                    if movie:
                        context.catalog.verify_namespace_snapshot(key, movie_observation[0])
                        if _catalog(context.catalog, current)[0] != command.catalog_fingerprint:
                            raise ValueError('电影版本预览后的作品登记已变化')
                    if not persist:
                        return candidate, draft
                    return transaction._commit_locked(envelope, task_terminal=task_terminal)
            finally:
                transaction._public_revision = None


def generate(service, command, current, state, terminal, *, movie_observation=None):
    from .initial_tv_completion import (InitialTVCompletionIntent, InitialTVCompletion,
        build_initial_tv_completion)
    from .movie_content import MovieChangeIntent, MovieChange, _catalog, build_movie_change
    from .initial_movie_selection import InitialMovieSelectionIntent, InitialMovieSelection, build as build_selection
    if type(command) is InitialMovieSelectionIntent:
        built = build_selection(service, command, current, state, terminal)
        if type(built) is not InitialMovieSelection:
            raise ValueError('首次作品唯一生成器已断开')
        return built, built.decision, built.draft
    if type(command) is InitialTVCompletionIntent:
        if (type(terminal) is not TaskTerminalMutation or terminal.task_id != command.task_id
                or terminal.expected_sha256 != command.inputs.task_fingerprint):
            raise ValueError('TV实际任务CAS与原命令不一致')
        built = build_initial_tv_completion(current, state, command)
        if type(built) is not InitialTVCompletion:
            raise ValueError('TV唯一生成器已断开')
        return built, built.decision, built.draft
    if type(command) is MovieChangeIntent:
        fingerprint, catalog = movie_observation[1]
        if fingerprint != command.catalog_fingerprint:
            raise ValueError('电影版本预览后的作品登记已变化')
        built = build_movie_change(service, current, state, command, catalog)
        if type(built) is not MovieChange:
            raise ValueError('电影唯一生成器已断开')
        candidate = replace(state, content_selection=built.choice, movie_version_selection=built.movie,
            asset_operation=None)
        return built, candidate, built.draft
    from .content_choice import build_subtitle_revision
    candidate, draft = build_subtitle_revision(service, current, state, command)
    return None, candidate, draft


def require_publication(transaction, envelope):
    result = getattr(transaction, '_public_revision', None)
    if type(result) is not Publication or result.envelope != envelope:
        raise ValueError('公共修订必须由同一锁内命令生成，不能提交外部成品计划')
    return result


def validate_publication(transaction, envelope, *, current_plan=None):
    result = getattr(transaction, '_public_revision', None)
    if result is None:
        if any(value is not None for value in (envelope.movie_change, envelope.initial_tv_completion, envelope.initial_movie_selection)):
            raise ValueError('公共命令必须经过唯一生成入口')
        if envelope.previous_decision is not None:
            current = current_plan or transaction.plan_store.load(envelope.decision.task_id)
            if current.subtitle_override_rules != envelope.draft_plan.subtitle_override_rules:
                raise ValueError('字幕规则必须经过公共命令入口')
        return None
    result = require_publication(transaction, envelope)
    choice = envelope.decision.content_selection
    if choice is None:
        raise ValueError('公共修订缺少内容决定')
    value = choice.to_dict()
    if content.scope_fingerprint(transaction.task_root, envelope.decision.task_id) != value['scope_fingerprint']:
        raise ValueError('公共修订的来源范围已变化')
    old = envelope.previous_decision.content_selection
    if old is not None:
        previous = old.to_dict()
        if any(value.get(k) != previous.get(k) for k in
               ('policy', 'scope_fingerprint', 'origin_plan_fingerprint', 'identities', *content.DEFAULT_FIELDS)):
            raise ValueError('公共修订不能替换原来源身份或策略')
    content.validate_source_identities(choice, plan=result.current)
    content.validate_omission_manifest(choice, movie_selection=envelope.decision.movie_version_selection)
    return result
