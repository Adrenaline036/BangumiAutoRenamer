"""Production source workspace and default initialization, never approval.

The queue and first-link page use this one producer. New admitted inventory and
default content publish together and remain media-unready.
"""
from dataclasses import replace
from pathlib import Path
from .provider_operation import provider_operation
from .canonical_naming import TitleEvidencePending

from .automatic_recognition import AutomaticRecognitionOrigin, validate_original_job
from .decision_state import DecisionStateStore
from .inventory_plan import InventoryDraftError, build_inventory_review_draft
from .movie_assets import inventory_movie_assets
from .plan_store import PlanStore
from .projection_authority import (TaskTerminalMutation, UnresolvedRecognitionInput,
    commit_recognition_projection, validate_plan_projection_receipt)
from .target_routing import TargetRootSnapshot, resolve_target_root


def require_supported_tv_selection(state):
    """Reject an unsupported identity replacement before staging any request."""
    if state is not None and state.pending_tv_selection is not None:
        raise ValueError('请先完成或取消当前季度关系确认，再选择作品')
    if state is not None and state.content_selection is not None and state.confirmed_work:
        raise ValueError('已确认的TV任务暂不支持重新识别，请保留原计划并使用当前内容确认入口')


def ensure_workspace(service, task_id, *, roots, origin=None, observed_task=None, automatic_observation=None):
    """Publish admitted sources and their default selection in one transaction.

    A caller may supply only the already checked first-link domain/kind fields;
    all other task values must be the current record. Running queue work needs
    the actual original admission proof, not a caller's 'running=True' flag.
    """
    from ..utils.utils import read_task_at
    from .content_policy import ContentPolicy
    from .default_content import require_unexecuted_task
    if type(roots) is not TargetRootSnapshot:
        raise ValueError('来源工作区缺少实际目标配置观察')
    current = read_task_at(service.task_root, task_id)
    if not current:
        raise ValueError('任务已不存在，不能建立默认内容工作区')
    from .input_directory_discovery import require_source_group_confirmed
    require_source_group_confirmed(current)
    if origin is not None:
        checked, _scope = validate_original_job(service, origin)
        if checked != current:
            raise ValueError('来源工作区的原作业已变化')
    require_unexecuted_task(current, recognition_worker=origin is not None)
    observed = dict(current)
    if automatic_observation is not None:
        from .automatic_recognition import AutomaticRecognition, parse_batch, parse_search_policy
        if (type(automatic_observation) is not AutomaticRecognition or automatic_observation.origin != origin
                or observed_task is not None
                or parse_batch(automatic_observation.batch_json).policy !=
                   parse_search_policy(current.get('automatic_search_policy'), origin)):
            raise ValueError('自动类型工作区缺少同一原作业的完整资料观察')
        anime, movie = automatic_observation.resolved_task_flags()
        observed.update(is_anime=anime, is_movie=movie)
    if observed_task is not None:
        differences = {key for key in set(current) | set(observed_task) if current.get(key) != observed_task.get(key)}
        if differences - {'is_anime', 'is_movie'}:
            raise ValueError('首次链接只能明确媒体类型，不能顺带更换来源或任务字段')
        observed.update({key: observed_task.get(key) for key in differences})
    plans, states = service.plan_store, service.decision_state_store
    plan_exists = plans.path_for(task_id).exists()
    state_exists = states.path_for(task_id).exists()
    if plan_exists != state_exists:
        raise ValueError('已有Plan与作品决定不完整，请先恢复原事务')
    if plan_exists:
        plan, state = plans.load(task_id), states.load(task_id)
        validate_plan_projection_receipt(plan)
        if plan.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(state):
            raise ValueError('来源工作区的Plan和作品决定已分裂')
        if observed != current:
            raise ValueError('已有计划不能通过首次链接入口更换媒体类型，请使用作品更正')
    else:
        anime, movie = observed.get('is_anime'), observed.get('is_movie')
        if type(anime) is not bool or type(movie) is not bool:
            raise InventoryDraftError('请先选择媒体类型，再读取作品链接；不需要填写检索名称')
        target = resolve_target_root('animation' if anime else 'general',
                                    'movie_continuous' if movie else 'tv_episodes', roots)
        # Do not trust a prior 'known movie' observation as work authority.
        # 库存由已接纳清单投影，来源变化通过明确核查处理。
        from .content_policy import _scope_binding
        from .source_basis import InitialSourceBasis, observe_admitted_basis
        binding = _scope_binding(service, task_id)
        if binding.policy is None:
            raise ValueError('来源记录不完整，无法建立整理计划。请安全结束此任务后重新添加来源；源文件不会删除。')
        source_basis = None
        if binding.policy is not None:
            source_basis = observe_admitted_basis(service, task_id)
            if type(source_basis) is not InitialSourceBasis:
                raise ValueError('已接纳库存来源观察已断开，不能回退目录扫描')
            from .movie_assets import inventory_from_source_basis
            inventory = inventory_from_source_basis(source_basis)
        else:
            inventory = inventory_movie_assets(Path(str(observed.get('path') or '')))
        draft_task = {**observed, 'movie_inventory': inventory.to_dict(), 'provider_identity': None,
            'linked_provider_identities': (), 'draft_target_root': str(target),
            'draft_initial_target_root': str(target)}
        from .trans import is_dry_run_enabled
        initial_default = None
        if source_basis is not None:
            from .inventory_plan import observe_inventory_workspace
            from .default_content import InitialDefaultDraft, build_initial_default
            from .content_associations import encode
            from .content_choice import _scope
            observation = observe_inventory_workspace(draft_task, dry_run=is_dry_run_enabled(),
                                                       source_basis=source_basis)
            metadata = {} if origin is None else {'automatic_recognition_origin': origin.to_dict(),
                                                  'automatic_search_policy': current.get('automatic_search_policy')}
            from .source_decision_transfer import load, require_task
            source_decisions = require_task(load(service, task_id), current)
            initial_default = InitialDefaultDraft(observation, source_basis, _scope(service, task_id),
                                                  encode(metadata), source_decisions)
            built = build_initial_default(initial_default)
            if not isinstance(built, tuple) or len(built) != 2:
                raise ValueError('默认首建已断开，不能回退旧初始化')
            candidate, draft = built
        else:
            draft = build_inventory_review_draft(draft_task, dry_run=is_dry_run_enabled())
        if origin is not None:
            if origin.plan_fingerprint or origin.decision_fingerprint:
                raise ValueError('原作业已有历史计划，不能重新建立空工作区')
            if initial_default is None:
                draft = replace(draft, metadata={**draft.metadata,
                    'automatic_recognition_origin': origin.to_dict(),
                    'automatic_search_policy': current.get('automatic_search_policy')})

        def finalize(_state, projected, saved):
            if saved != current:
                raise ValueError('任务在观察来源期间已变化，默认工作区未生效')
            saved.update(inventory_draft_created=True, movie_inventory=inventory.to_dict(),
                draft_target_root=str(target), draft_initial_target_root=str(target),
                workflow_status='invalid', plan_status='invalid', plan_entry_count=len(projected.entries),
                plan_file=plans.path_for(task_id).name)
            if origin is None:
                saved.update(is_anime=anime, is_movie=movie)
            return saved

        terminal = TaskTerminalMutation.prepare(service.task_root, task_id, finalize)
        if initial_default is not None:
            from .projection_authority import commit_projected_plan
            state, plan = commit_projected_plan(states, plans, previous=None, candidate=candidate,
                draft_plan=draft, task_terminal=terminal, initial_default=initial_default)
        else:
            state, plan = commit_recognition_projection(states, plans,
                UnresolvedRecognitionInput(task_id=task_id), draft, task_terminal=terminal)
    if state.content_policy != ContentPolicy.SAFE_WATCHABLE:
        return state, plan  # Genuine historical no-policy records are unchanged.
    if state.content_selection is not None:
        from .content_selection import validate_plan_content
        validate_plan_content(plan, state)
        return state, plan  # Never reset v1/v2 choices or auto-upgrade history.
    from .default_content import source_only_pending
    if not source_only_pending(plan, state):
        raise ValueError('既有已完成语义必须明确预览内容迁移，不能重新套新任务默认')
    preview = service.prepare_default_content_initialization(task_id, PlanStore.fingerprint(plan))
    plan = service.commit_default_content_initialization(preview)
    return states.load(task_id), plan


def service_for_worker(renamer):
    from . import process
    from .plan_review import PlanReviewService
    from .initial_movie_completion import RootSnapshotSource
    from .series_catalog import SeriesCatalog
    roots = RootSnapshotSource(lambda: TargetRootSnapshot.capture({
        'bangumi_path': renamer.BANGUMI_PATH, 'movie_path': renamer.MOVIE_PATH,
        'anime_path': renamer.ANIME_PATH, 'anime_movie_path': renamer.ANIME_MOVIE_PATH}))
    service = PlanReviewService(PlanStore(process.PLAN_PATH), process.PLAN_PATH.parent / 'plan_review',
        catalog_root=process.PLAN_PATH.parent / 'work_catalog', decision_state_root=renamer.decision_state_root,
        task_root=process.TASK_PATH, provider_link_catalog_root=renamer.provider_link_catalog_root,
        record_root=process.RECORD_PATH, root_snapshot_source=roots)
    service.series_catalog = SeriesCatalog(process.SERIES_CATALOG_PATH)
    return service


def initialize_worker(renamer, origin):
    if type(origin) is not AutomaticRecognitionOrigin:
        raise ValueError('默认新任务缺少原队列作业证明')
    service = service_for_worker(renamer)
    return ensure_workspace(service, origin.task_id, roots=service.root_snapshot_source.observe(), origin=origin)


@provider_operation
def discover_initial_type(renamer, origin):
    """Unknown-type admission: observe first, then reuse existing completion."""
    from .automatic_recognition import AutomaticRecognition, freeze_batch, parse_batch, parse_search_policy
    service = service_for_worker(renamer)
    task, _scope = validate_original_job(service, origin)
    if (origin.plan_fingerprint or origin.decision_fingerprint
            or service.plan_store.path_for(origin.task_id).exists()
            or service.decision_state_store.path_for(origin.task_id).exists()):
        raise ValueError('已有自动计划请使用原资料续做或作品选择，不能重新初始化')
    from .input_directory_discovery import SourceTitleUncertain
    try:
        candidates = renamer.discovery_title_candidates(Path(origin.path), origin.custom_name, task_source_scope=_scope)
    except SourceTitleUncertain as exc:
        from ..utils.utils import mutate_task_at
        detail = str(exc)
        def wait_for_title(current):
            actual, _ = validate_original_job(service, origin)
            if actual != current:
                raise ValueError('来源标题核查期间任务已变化')
            current.update(workflow_status='need_confirm', error=detail)
            return current
        mutate_task_at(service.task_root, origin.task_id, wait_for_title)
        return None
    batch = renamer._provider_search_batch(candidates, is_anime=origin.is_anime, is_movie=origin.is_movie)
    proof = AutomaticRecognition(origin, freeze_batch(batch), candidates[0].title, candidates[0].year or None)
    if parse_batch(proof.batch_json).policy != parse_search_policy(task.get('automatic_search_policy'), origin):
        raise ValueError('资料查询改变了原冻结Provider范围')
    try:
        selected, _name, _info = proof.resolve()
    except ValueError as exc:
        return preserve_unresolved_observation(renamer, origin, batch, '[作品搜索] ' + str(exc))
    from .recognition_reset import bind_discovered_type
    proof = bind_discovered_type(service, proof)
    origin = proof.origin
    renamer.automatic_recognition_origin = origin
    ensure_workspace(service, origin.task_id, roots=service.root_snapshot_source.observe(),
                     origin=origin, automatic_observation=proof)
    return (complete_movie_observation(renamer, proof) if selected['entity_type'] == 'movie'
            else complete_tv_observation(renamer, proof))


def preserve_unresolved_observation(renamer, origin, batch, error):
    """No-match discovery updates feedback, never a second source inventory."""
    from .automatic_recognition import freeze_batch, parse_batch, parse_search_policy, validate_lineage, validate_search_policy
    from .default_content import require_unexecuted_task
    from .file_lock import task_operation_lock
    from .projection_authority import ProjectionCommitService
    from ..utils.utils import mutate_task_at, read_task_at
    if type(origin) is not AutomaticRecognitionOrigin or not isinstance(error, str) or not error:
        raise ValueError('未确认作品反馈缺少实际原作业')
    observation = parse_batch(freeze_batch(batch))
    service = service_for_worker(renamer)
    transaction = ProjectionCommitService(service.decision_state_store, service.plan_store,
                                          task_root=service.task_root)
    with task_operation_lock(service.task_root, origin.task_id, operation='recognition_feedback'):
        with transaction._transaction_lock(origin.task_id):
            before = read_task_at(service.task_root, origin.task_id)
            require_unexecuted_task(before, recognition_worker=True)
            actual, _scope = validate_original_job(service, origin)
            if actual != before:
                raise ValueError('未确认反馈到达时原任务已变化')
            has_plan = service.plan_store.path_for(origin.task_id).exists()
            has_state = service.decision_state_store.path_for(origin.task_id).exists()
            if has_plan != has_state:
                raise ValueError('原Plan和作品决定不完整，不能覆盖反馈')
            if has_plan:
                plan, state = service.plan_store.load(origin.task_id), service.decision_state_store.load(origin.task_id)
                validate_lineage(service, plan, state, origin)
                if (state.confirmed_work or state.content_selection is None
                        or observation.policy != validate_search_policy(service, plan, origin)):
                    raise ValueError('未确认作品反馈与原工作区或查询范围不符')
            elif (origin.plan_fingerprint or origin.decision_fingerprint
                    or observation.policy != parse_search_policy(before.get('automatic_search_policy'), origin)):
                raise ValueError('首次未确认反馈缺少原作业查询范围')
            candidates = [dict(c) for c in observation.candidates]
            def feedback(task):
                if task != before:
                    raise ValueError('未确认作品反馈到达时任务已变化，保留后来记录')
                task.update(error=error, workflow_status='need_confirm', execution_blocked=True,
                            queue_position=None, completed_at=None)
                task.pop('provider_candidates', None)
                task.pop('type_match_state', None)
                if candidates:
                    task.update(provider_candidates={'work': candidates}, type_match_state='mismatch')
                return task
            if mutate_task_at(service.task_root, origin.task_id, feedback) is None:
                raise ValueError('未确认作品任务已不存在')
    return error


def complete_tv_observation(renamer, proof):
    """Use frozen discovery and the canonical TV command, with no new search."""
    from .automatic_recognition import persist_observation, recognition_from_observation
    from .initial_tv_completion import prepare, commit
    from .plan_verifier import PlanVerifier
    service = service_for_worker(renamer)
    state, _plan = persist_observation(service, proof)
    try:
        recognition, seasons = recognition_from_observation(proof, state)
    except TitleEvidencePending as exc:
        return _pending_title(renamer, proof, exc)
    if seasons is None:
        raise ValueError('电影需要完整版本确认，不能沿TV自动完成')
    intent = prepare(service, recognition, seasons=seasons)

    def finish(decision, projected, current):
        verification = PlanVerifier().verify(projected)
        binding = decision.season_binding
        current.update(name=decision.confirmed_work['title'], is_anime=decision.library_domain == 'animation',
            is_movie=False, season_id=binding['season_number'], season_number=binding['season_number'],
            season_source=recognition.season_source, workflow_status='need_confirm',
            plan_status=verification.status.value, plan_entry_count=len(projected.entries),
            plan_file=service.plan_store.path_for(projected.task_id).name, error=None, completed_at=None)
        return current

    _, plan = commit(service, intent,
        task_terminal=TaskTerminalMutation.prepare(service.task_root, proof.origin.task_id, finish))
    renamer.last_plan = plan
    renamer.R = {}
    return 'need_confirm'


def _resume_movie_waiting(service, proof, expected_fingerprint):
    """Publish queue status only; do not manufacture a new identity revision."""
    from .automatic_recognition import validate_resume
    from .default_content import require_unexecuted_task
    from .projection_authority import ProjectionCommitService
    from .plan_verifier import PlanVerifier
    from ..utils.utils import mutate_task_at
    transaction = ProjectionCommitService(service.decision_state_store, service.plan_store,
                                          task_root=service.task_root)
    with transaction._transaction_lock(proof.origin.task_id):
        def waiting(task):
            require_unexecuted_task(task, recognition_worker=True)
            plan = service.plan_store.load(proof.origin.task_id)
            state = service.decision_state_store.load(proof.origin.task_id)
            if (PlanStore.fingerprint(plan) != expected_fingerprint
                    or validate_resume(service, plan, state) != proof):
                raise ValueError('电影续做结束前计划或原作业已变化，请刷新')
            task.update(workflow_status='need_confirm', plan_status=PlanVerifier().verify(plan).status.value,
                        plan_entry_count=len(plan.entries), error=None, completed_at=None)
            return task
        if mutate_task_at(service.task_root, proof.origin.task_id, waiting) is None:
            raise ValueError('电影续做任务不存在')


def complete_movie_observation(renamer, proof):
    """Confirm movie identity, then use the same complete-version command.

    Only a sole root-level file with the exact observed title and technical
    suffix can be preselected. Unnamed clips, nested SPs and multiple versions
    stay on the version panel; video count never determines the work type.
    """
    import re
    import unicodedata
    from .automatic_recognition import commit_movie_identity, validate_resume
    from .content_selection import material_entries
    from .plan_review import _infer_movie_version_descriptor
    service = service_for_worker(renamer)
    state = service.decision_state_store.load(proof.origin.task_id)
    plan = service.plan_store.load(proof.origin.task_id)
    already_confirmed = bool(state.confirmed_work)
    if state.confirmed_work:
        if validate_resume(service, plan, state) != proof:
            raise ValueError('电影续做的原观察已变化')
    else:
        try:
            state, plan = commit_movie_identity(service, proof)
        except TitleEvidencePending as exc:
            return _pending_title(renamer, proof, exc)
    value = state.content_selection.to_dict()
    videos = [entry for entry in material_entries(plan, state.content_selection)
              if entry.media_kind == 'video' and str(entry.source_path) not in value['excluded_sources']]
    if len(videos) == 1 and videos[0].source_path.parent == plan.source_root:
        video = videos[0]
        selected, _name, _info = proof.resolve()
        stem = unicodedata.normalize('NFKC', video.source_path.stem).casefold()
        titles = (proof.query, selected.get('original_title'), selected.get('title'))
        qualified = False
        for title in titles:
            title = unicodedata.normalize('NFKC', str(title or '')).casefold().strip()
            if not title or not re.match(re.escape(title) + r'(?=$|[\s._\-\[\(])', stem):
                continue
            suffix = stem[len(title):]
            suffix = re.sub(r'(?<![a-z0-9])(?:\d{3,4}p|[xh]26[45]|av1|hevc|sdr|hdr10\+?|dovi|dv|\d{1,2}bit)(?![a-z0-9])', '', suffix)
            if not suffix.strip(' ._-[]()'):
                qualified = True
                break
        if qualified:
            if already_confirmed:
                # Restore the same proof-bound planning status used immediately
                # after identity publication; do not weaken the shared editor's
                # rejection of arbitrary running/executing tasks.
                _resume_movie_waiting(service, proof, PlanStore.fingerprint(plan))
            descriptor = _infer_movie_version_descriptor(video.source_path,
                plan.presentation_groups[0].group_id, plan.source_root)
            preview = service.prepare_initial_movie_completion(plan.task_id, PlanStore.fingerprint(plan),
                groups=[dict(sources=[str(video.source_path)], label=descriptor.display_label or '默认版本')],
                reason='唯一电影身份与明确单一完整视频，预选版本等待审核')
            plan = service.commit_initial_movie_completion(preview)
    if already_confirmed and service.decision_state_store.load(proof.origin.task_id).movie_version_selection is None:
        _resume_movie_waiting(service, proof, PlanStore.fingerprint(plan))
    renamer.last_plan = plan
    renamer.R = {}
    return 'need_confirm'


def _pending_title(renamer, proof, error):
    from .automatic_recognition import parse_batch
    return preserve_unresolved_observation(renamer, proof.origin, parse_batch(proof.batch_json),
        f'{error}；请选择资料完整的作品条目后继续')
