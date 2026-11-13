"""Automatic discovery observations bound to the actual admitted queue job.

These objects are observations, not approval or a synthetic manual selection.
Canonical publication must validate the original job and recompute the result.
"""
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import re

from .content_associations import encode


_HASH = re.compile(r'[0-9a-f]{64}')


def _digest(kind, value):
    return hashlib.sha256((kind + ':' + encode(value)).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class AutomaticRecognitionOrigin:
    task_id: str
    job_id: str
    batch_id: str
    scope_fingerprint: str
    plan_fingerprint: str
    decision_fingerprint: str
    original_revision: int
    path: str
    is_anime: bool | None
    is_movie: bool | None
    custom_name: str | None
    use_ai: bool | None
    requested_season: int | None
    source: str

    def __post_init__(self):
        if (any(not isinstance(v, str) or not v.strip() for v in
                (self.task_id, self.job_id, self.batch_id, self.source))
                or not isinstance(self.scope_fingerprint, str) or not _HASH.fullmatch(self.scope_fingerprint)
                or any(not isinstance(v, str) or v != '' and not _HASH.fullmatch(v)
                       for v in (self.plan_fingerprint, self.decision_fingerprint))
                or type(self.original_revision) is not int or self.original_revision < -1
                or bool(self.decision_fingerprint) != (self.original_revision >= 0)
                or bool(self.plan_fingerprint) != bool(self.decision_fingerprint)
                or not isinstance(self.path, str) or not Path(self.path).is_absolute()
                or '..' in Path(self.path).parts
                or any(v is not None and type(v) is not bool for v in (self.is_anime, self.is_movie, self.use_ai))
                or self.custom_name is not None and not isinstance(self.custom_name, str)
                or self.requested_season is not None and
                   (type(self.requested_season) is not int or self.requested_season < 0)):
            raise ValueError('自动识别缺少有效的原作业、范围或修订绑定')

    def to_dict(self):
        return {'schema_version': 1, **asdict(self)}

    @classmethod
    def from_mapping(cls, value):
        if (not isinstance(value, Mapping) or set(value) != {'schema_version', *cls.__dataclass_fields__}
                or type(value['schema_version']) is not int or value['schema_version'] != 1):
            raise ValueError('自动识别原作业格式无效')
        return cls(**{key: value[key] for key in cls.__dataclass_fields__})

    @property
    def fingerprint(self):
        return _digest('bar.automatic-recognition-origin-v1', self.to_dict())

    @classmethod
    def at_admission(cls, manager, job, *, batch_id):
        import uuid
        from ..task_queue import TaskJob
        from .plan_store import PlanStore
        from .decision_state import DecisionStateStore
        from .task_batch_scope import TaskSourceScope
        if (type(job) is not TaskJob or job.operation != 'rename' or job.preview_only
                or job.provider_selection is not None or type(job.task_source_scope) is not TaskSourceScope
                or job.task_source_scope.task_id != job.task_id):
            raise ValueError('自动识别来源只能由实际新建队列作业产生')
        plans = manager.plan_store
        states = DecisionStateStore(manager.decision_state_root)
        current = plans.load(job.task_id) if plans.path_for(job.task_id).exists() else None
        state = states.load_optional(job.task_id)
        if (current is None) != (state is None):
            raise ValueError('原Plan和作品决定不完整，不能创建新的自动识别作业')
        return cls(job.task_id, uuid.uuid4().hex, batch_id,
            _digest_scope(job.task_source_scope), PlanStore.fingerprint(current) if current else '',
            DecisionStateStore.fingerprint(state) if state else '', state.revision if state else -1,
            str(job.path), job.is_anime, job.is_movie, job.custom_name, job.use_ai,
            job.custom_season_id, job.source)


def _digest_scope(scope):
    from .content_selection import digest
    return digest(scope.to_dict())


_INFO_FIELDS = frozenset({'id', 'name', 'title', 'original_name', 'original_title',
    'first_air_date', 'release_date', 'genres', 'genre_ids', 'seasons',
    'poster_path', 'overview', 'number_of_episodes', 'number_of_seasons'})


def freeze_batch(batch):
    from .provider_search import ProviderSearchBatch
    if type(batch) is not ProviderSearchBatch:
        raise ValueError('自动识别需要实际Provider批次观察')
    rows = []
    for result in batch.results:
        runtime = result.runtime if isinstance(result.runtime, Mapping) else {}
        info = runtime.get('info')
        rows.append(dict(source=result.source, status=result.status.value,
            candidates=[dict(row) for row in result.candidates],
            name=runtime.get('name') or '',
            info={key: value for key, value in info.items() if key in _INFO_FIELDS} if isinstance(info, Mapping) else None))
    value = dict(policy=asdict(batch.policy), results=rows, candidates=[dict(row) for row in batch.candidates])
    # Round-trip detaches provider-owned lists/dicts; no credential/config field
    # is read. Observations are bounded before being accepted for persistence.
    frozen = encode(value)
    parse_batch(frozen)
    return frozen


def parse_batch(payload):
    from .provider_search import ProviderSearchBatch, ProviderSearchResult, ProviderSearchStatus, SearchPolicy
    if not isinstance(payload, str) or len(payload.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('自动识别Provider观察超过安全读取边界')
    try:
        value = json.loads(payload)
        if (not isinstance(value, dict) or set(value) != {'policy', 'results', 'candidates'}
                or encode(value) != payload or not isinstance(value['policy'], dict)
                or set(value['policy']) != {'domain', 'media_hint', 'expected_sources', 'scheduled_sources'}
                or not isinstance(value['results'], list) or not 1 <= len(value['results']) <= 3
                or not isinstance(value['candidates'], list) or len(value['candidates']) > 50):
            raise ValueError('自动识别Provider观察格式无效')
        policy = value['policy']
        expected, scheduled = policy['expected_sources'], policy['scheduled_sources']
        allowed = {'bangumi', 'tmdb_tv', 'tmdb_movie'}
        if (policy['domain'] not in {'animation', 'general', 'unknown'}
                or policy['media_hint'] not in {'movie', 'tv', 'unknown'}
                or not isinstance(expected, list) or not isinstance(scheduled, list)
                or not expected or len(expected) != len(set(expected))
                or not set(expected) <= allowed or len(scheduled) != len(set(scheduled))
                or not set(scheduled) <= set(expected)):
            raise ValueError('自动识别Provider范围无效')
        rows = value['results']
        if (any(not isinstance(row, dict) or set(row) != {'source', 'status', 'candidates', 'name', 'info'}
                or not isinstance(row['candidates'], list) or len(row['candidates']) > 50
                or not isinstance(row['name'], str) or row['info'] is not None and not isinstance(row['info'], dict)
                or row['info'] is not None and not set(row['info']) <= _INFO_FIELDS for row in rows)
                or len({row['source'] for row in rows}) != len(rows)
                or {row['source'] for row in rows} != set(expected)
                or any(not isinstance(row, dict) for row in value['candidates'])
                or any(not isinstance(candidate, dict) for row in rows for candidate in row['candidates'])):
            raise ValueError('自动识别Provider观察缺项、重复或候选不完整')
        return ProviderSearchBatch(SearchPolicy(policy['domain'], policy['media_hint'], tuple(expected), tuple(scheduled)),
            tuple(ProviderSearchResult(row['source'], ProviderSearchStatus(row['status']), tuple(row['candidates']),
                runtime={'name': row['name'], 'info': row['info']}) for row in rows), tuple(value['candidates']))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('自动识别Provider观察格式无效') from exc


@dataclass(frozen=True)
class AutomaticRecognition:
    origin: AutomaticRecognitionOrigin
    batch_json: str
    query: str
    year: int | None = None

    def __post_init__(self):
        if type(self.origin) is not AutomaticRecognitionOrigin:
            raise ValueError('自动识别不能使用自报成功或手选来源')
        if (not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 4096
                or self.year is not None and (type(self.year) is not int or not 1800 <= self.year <= 2200)):
            raise ValueError('自动识别缺少原检索标题和年份观察')
        parse_batch(self.batch_json)

    def to_dict(self):
        return dict(schema_version=1, origin=self.origin.to_dict(), batch=json.loads(self.batch_json),
                    query=self.query, year=self.year)

    @classmethod
    def from_mapping(cls, value):
        if (not isinstance(value, Mapping) or set(value) != {'schema_version', 'origin', 'batch', 'query', 'year'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1):
            raise ValueError('自动识别观察合同无效')
        return cls(AutomaticRecognitionOrigin.from_mapping(value['origin']), encode(value['batch']),
                   value['query'], value['year'])

    @property
    def fingerprint(self):
        return _digest('bar.automatic-recognition-v1', self.to_dict())

    def resolve(self):
        """Pure qualification using the same candidate and capability rules."""
        from . import provider_search
        batch = parse_batch(self.batch_json)
        ranked = provider_search.fuse_provider_candidates(self.query,
            (candidate for row in batch.results for candidate in row.candidates),
            year=self.year, animation=batch.policy.domain == 'animation')
        if encode(ranked) != encode(list(batch.candidates)):
            raise ValueError('自动候选排名与完整原始候选重算不一致')
        selected, name, info, _anime, _movie = resolve_batch_candidate(batch,
            is_anime=self.origin.is_anime, is_movie=self.origin.is_movie)
        return selected, name, info

    def resolved_task_flags(self):
        selected, _name, info = self.resolve()
        return _resolved_flags(selected, info, self.origin.is_anime, self.origin.is_movie)


def _resolved_flags(selected, info, anime, movie):
    from .tmdb_genre import classify_tmdb_animation_evidence
    if anime is None:
        anime = classify_tmdb_animation_evidence(genres=info.get('genres', ()),
            genre_ids=info.get('genre_ids', ())).is_anime
    if type(anime) is not bool:
        raise ValueError('资料不能确定动画或普通影视类型，请手动确认作品类型')
    resolved_movie = selected.get('entity_type') == 'movie'
    if movie is not None and movie != resolved_movie:
        raise ValueError('资料条目与原任务的电影/剧集类型不一致')
    return anime, resolved_movie


def resolve_batch_candidate(batch, *, is_anime, is_movie):
    """Shared discovery qualification; observations never become approval."""
    from . import provider_search
    from .provider_selection_authority import (provider_selection_capability, provider_candidate_group_kind,
        task_media_kind_from_flag, task_library_domain_from_flag)
    kinds = {provider_candidate_group_kind(candidate) for row in batch.results
             if row.status == provider_search.ProviderSearchStatus.SUCCESS
             for candidate in row.candidates if candidate.get('provider_id')}
    if is_movie is None and {'tv_episodes', 'movie_continuous'} <= kinds:
        raise ValueError('同时找到电影和剧集资料，请先选择本次整理的作品')
    outcome = provider_search.decide_provider_candidates(batch)
    if type(outcome) is not provider_search.CandidateDecision:
        raise ValueError('自动候选裁决已断开')
    if outcome.status != provider_search.CandidateDecisionStatus.AUTO_ELIGIBLE:
        raise ValueError('需要人工选择作品；' + '；'.join(outcome.evidence))
    selected = outcome.selected_candidate
    if not isinstance(selected, Mapping):
        raise ValueError('自动候选裁决已断开')
    if selected.get('namespace') != 'tmdb':
        raise ValueError('动画剧集请明确选择Bangumi季度条目；此自动结果尚未确认')
    source = {'tv': 'tmdb_tv', 'movie': 'tmdb_movie'}.get(selected.get('entity_type'))
    row = next((item for item in batch.results if item.source == source), None)
    runtime = row.runtime if row is not None else {}
    info = runtime.get('info')
    if (not runtime.get('name') or not isinstance(info, Mapping)
            or str(info.get('id') or '') != str(selected.get('provider_id') or '')):
        raise ValueError('自动候选与Provider详情身份不一致')
    anime, movie = _resolved_flags(selected, info, is_anime, is_movie)
    capability = provider_selection_capability(selected,
        group_kind=provider_candidate_group_kind(selected) or '',
        task_media_kind=task_media_kind_from_flag(movie),
        task_library_domain=task_library_domain_from_flag(anime))
    if not capability.actionable:
        raise ValueError(capability.message)
    if anime and not movie:
        # Preserve automatic discovery's existing recommendation while allowing
        # either provider at the explicit selection boundary.
        raise ValueError('动画剧集自动建议先核对 Bangumi 季度；也可手动选择 TMDB 剧集资料。')
    return dict(selected), str(runtime['name']), dict(info), anime, movie


def _validate_projected_type(service, task, origin):
    """Non-recursive check of actual current output, not caller-supplied flags."""
    from .decision_state import DecisionStateStore
    from .projection_authority import validate_plan_projection_receipt
    plan = service.plan_store.load(origin.task_id)
    state = service.decision_state_store.load(origin.task_id)
    validate_plan_projection_receipt(plan)
    proof = AutomaticRecognition.from_mapping(plan.metadata.get('automatic_recognition'))
    if (proof.origin != origin or plan.metadata.get('automatic_recognition_origin') != origin.to_dict()
            or not state.confirmed_work
            or plan.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(state)
            or plan.work_identity is None or plan.work_identity.to_dict() != state.confirmed_work):
        raise ValueError('自动类型结果缺少当前已确认Plan和决定绑定')
    expected, _seasons = recognition_from_observation(proof, state)
    flags = proof.resolved_task_flags()
    if expected.work_identity.to_dict() != state.confirmed_work:
        _validate_movie_identity_history(service, plan, state, proof)
        expected = replace(expected, work_identity=plan.work_identity)
        flags = (plan.work_identity.domain == 'animation', flags[1])
    policy = parse_search_policy(task.get('automatic_search_policy'), origin)
    if (parse_batch(proof.batch_json).policy != policy
            or encode(plan.metadata.get('automatic_search_policy')) != encode(search_policy_value(policy))
            or any(type(task.get(key)) is not bool for key in ('is_anime', 'is_movie'))
            or expected.work_identity.to_dict() != state.confirmed_work
            or (task.get('is_anime'), task.get('is_movie')) != flags):
        raise ValueError('任务类型不是当前原始资料裁决的结果')


def validate_original_job(service, origin):
    """Validate actual task/scope; stage history is checked by activation."""
    from ..utils.utils import read_task_at
    from .task_batch_scope import TaskSourceScope, TaskBatchScopeManifestStore
    from .review_operation import build_review_operation
    if type(origin) is not AutomaticRecognitionOrigin:
        raise ValueError('自动识别缺少原作业凭据')
    task = read_task_at(service.task_root, origin.task_id)
    if (not task or task.get('automatic_recognition_origin') != origin.to_dict()
            or task.get('recognition_job_id') != origin.job_id
            or task.get('batch_id') != origin.batch_id or str(task.get('path')) != origin.path):
        raise ValueError('自动识别原作业已变化，不能认领后来任务')
    if (task.get('is_anime'), task.get('is_movie')) != (origin.is_anime, origin.is_movie):
        _validate_projected_type(service, task, origin)
    scope = TaskSourceScope.from_mapping(task.get('task_source_scope'))
    if scope.task_id != origin.task_id or _digest_scope(scope) != origin.scope_fingerprint:
        raise ValueError('自动识别原来源范围已变化')
    TaskBatchScopeManifestStore(service.plan_store.root.parent / 'task_batch_scope').require_active_scope(
        scope, batch_id=origin.batch_id)
    operation, _unused = build_review_operation(task)
    if operation.context.get('kind') == 'provider_selection' and operation.context.get('request_id'):
        raise ValueError('已有后来人工作品选择，旧自动观察不能继续发布')
    return task, scope


def search_policy_value(policy):
    from .provider_search import SearchPolicy
    if type(policy) is not SearchPolicy:
        raise ValueError('自动查询范围缺少实际SearchPolicy')
    return json.loads(encode(asdict(policy)))


def parse_search_policy(value, origin):
    from .provider_search import build_search_policy
    if (not isinstance(value, Mapping) or set(value) != {
            'domain', 'media_hint', 'expected_sources', 'scheduled_sources'}):
        raise ValueError('原自动作业未冻结查询范围，请重新选择作品')
    domain = 'animation' if origin.is_anime is True else 'general' if origin.is_anime is False else 'unknown'
    hint = 'movie' if origin.is_movie is True else 'tv' if origin.is_movie is False else 'unknown'
    expected, scheduled = value['expected_sources'], value['scheduled_sources']
    if not isinstance(expected, (list, tuple)) or not isinstance(scheduled, (list, tuple)):
        raise ValueError('原自动查询来源格式无效')
    rebuilt = build_search_policy(domain=domain, media_hint=hint,
        bangumi_configured='bangumi' in expected,
        tmdb_configured=any(item in scheduled for item in ('tmdb_tv', 'tmdb_movie')))
    if encode(value) != encode(search_policy_value(rebuilt)):
        raise ValueError('自动查询范围与原作业类型或配置观察不一致')
    return rebuilt


def validate_search_policy(service, plan, origin, proof=None):
    task, _scope = validate_original_job(service, origin)
    policy = parse_search_policy(plan.metadata.get('automatic_search_policy'), origin)
    if encode(task.get('automatic_search_policy')) != encode(search_policy_value(policy)):
        raise ValueError('原自动查询范围在工作区建立后已变化')
    if proof is not None and parse_batch(proof.batch_json).policy != policy:
        raise ValueError('Provider结果裁剪或改变了原冻结查询范围')
    return policy


def metadata_fingerprints(plan):
    """Receipt binding also protects ordinary revisions and read-only history."""
    metadata = plan.metadata if plan is not None else {}
    origin = metadata.get('automatic_recognition_origin')
    observation = metadata.get('automatic_recognition')
    policy = metadata.get('automatic_search_policy')
    if policy is not None:
        parse_search_policy(policy, AutomaticRecognitionOrigin.from_mapping(origin))
    if observation is not None and policy is None:
        raise ValueError('旧自动结果缺少查询范围证明，请重新选择作品')
    return {
        'automatic_search_policy_fingerprint': _digest('bar.automatic-search-policy-v1', policy) if policy is not None else None,
        'automatic_recognition_origin_fingerprint': (
            AutomaticRecognitionOrigin.from_mapping(origin).fingerprint if origin is not None else None),
        'automatic_recognition_fingerprint': (
            AutomaticRecognition.from_mapping(observation).fingerprint if observation is not None else None),
    }


def validate_lineage(service, plan, state, origin):
    """Walk actual receipted revisions back to the admitted empty workspace.

    Never repin an observation to a later job or reconstruct missing history.
    Source/choice edits before discovery publication are intentionally rejected.
    """
    from .projection_authority import validate_plan_projection_receipt
    from .decision_state import DecisionStateStore
    from .plan_store import PlanStore
    validate_original_job(service, origin)
    validate_search_policy(service, plan, origin)
    from .source_basis import SourceInputProof, observe_initial_input
    source_input = observe_initial_input(service, plan, state)
    if type(source_input) is not SourceInputProof:
        raise ValueError('自动原输入路径证明已断开')
    if (origin.plan_fingerprint or origin.decision_fingerprint or origin.original_revision != -1
            or plan.task_id != origin.task_id or str(source_input.observation_path) != origin.path
            or plan.source_root != source_input.layout_root
            or plan.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(state)):
        raise ValueError('自动来源不是原空工作区的当前Plan和Decision')
    from .recognition_reset import first_revision
    floor = first_revision(service.plan_store.root.parent, plan.task_id)
    seen, cursor = set(), plan
    for _ in range(256):
        fp = PlanStore.fingerprint(cursor)
        if fp in seen:
            raise ValueError('自动来源实际历史出现循环')
        seen.add(fp)
        validate_plan_projection_receipt(cursor)
        if (cursor.task_id != plan.task_id or cursor.source_root != plan.source_root
                or cursor.metadata.get('automatic_recognition_origin') != origin.to_dict()
                or encode(cursor.metadata.get('automatic_search_policy')) != encode(plan.metadata.get('automatic_search_policy'))
                or cursor.initial_source_basis != state.initial_source_basis):
            raise ValueError('自动来源历史与原作业或完整来源不一致')
        revision = cursor.metadata.get('decision_state_revision')
        if revision == floor:
            if cursor.revision_parent_fingerprint or cursor.work_identity is not None:
                raise ValueError('自动来源首工作区不是原未确认来源')
            return
        if (type(revision) is not int or revision <= floor or not cursor.revision_parent_fingerprint
                or cursor.revision_action not in {'content_default', 'automatic_observation'}):
            raise ValueError('自动识别原历史已被人工编辑或缺失，请保留当前选择')
        parent = service.plan_store.load_version(plan.task_id, cursor.revision_parent_fingerprint)
        if parent.metadata.get('decision_state_revision') != revision - 1:
            raise ValueError('自动来源历史修订不连续')
        cursor = parent
    raise ValueError('自动来源实际历史超过有界检查范围')


def observation_draft(plan, state, proof):
    from dataclasses import replace
    from .plan_store import PlanStore
    from .decision_state import DecisionStateStore
    return DecisionStateStore.project_plan(state, replace(plan, projection_receipt=None,
        metadata={**plan.metadata, 'automatic_recognition': proof.to_dict()},
        revision_parent_fingerprint=PlanStore.fingerprint(plan), revision_action='automatic_observation',
        revision_reason='保存原自动作业的完整资料观察；尚未确认作品'))


@dataclass(frozen=True)
class AutomaticPublication:
    decision: object
    draft: object
    stage: str


def movie_identity_publication(plan, state, proof):
    from dataclasses import replace
    from .decision_state import DecisionStateStore
    from .plan_store import PlanStore
    recognition, seasons = recognition_from_observation(proof, state)
    if seasons is not None or state.confirmed_work:
        raise ValueError('自动电影首次确认不能更换已确认作品或TV类型')
    candidate = recognition.candidate(state)
    draft = DecisionStateStore.project_plan(candidate, replace(plan, projection_receipt=None,
        revision_parent_fingerprint=PlanStore.fingerprint(plan), revision_action='automatic_movie_selection',
        revision_reason='自动确认唯一电影作品，保留完整版本待确认'))
    return AutomaticPublication(candidate, draft, 'movie_identity')


def validate_commit(transaction, envelope, terminal):
    """One canonical guard; observation publication cannot confirm anything."""
    from .projection_authority import TaskTerminalMutation
    current = transaction._load_optional_plan(envelope.decision.task_id)
    before, after = metadata_fingerprints(current), metadata_fingerprints(envelope.draft_plan)
    proof = envelope.automatic_recognition
    if before['automatic_recognition_origin_fingerprint'] != after['automatic_recognition_origin_fingerprint']:
        if current is not None or envelope.previous_decision is not None or envelope.decision.confirmed_work:
            raise ValueError('普通修订不能更换自动作业来源')
        origin = AutomaticRecognitionOrigin.from_mapping(envelope.draft_plan.metadata.get('automatic_recognition_origin'))
        validate_original_job(transaction, origin)
        validate_search_policy(transaction, envelope.draft_plan, origin)
        if origin.original_revision != -1 or origin.plan_fingerprint or origin.decision_fingerprint:
            raise ValueError('初始工作区不属于原空作业')
    if proof is None:
        if before != after:
            # Only the first neutral workspace may introduce its admission pin.
            if current is not None or after['automatic_recognition_fingerprint'] is not None:
                raise ValueError('普通写入不能产生或替换自动识别观察')
        return None
    if (type(proof) is not AutomaticRecognition or current is None
            or type(terminal) is not TaskTerminalMutation or terminal.task_root != transaction.task_root
            or terminal.task_id != proof.origin.task_id
            or encode(envelope.draft_plan.metadata.get('automatic_recognition')) != encode(proof.to_dict())):
        raise ValueError('自动识别提交缺少实际作业观察和任务CAS')
    proof.resolve()
    if before['automatic_search_policy_fingerprint'] != after['automatic_search_policy_fingerprint']:
        raise ValueError('自动结果不能更换原冻结查询范围')
    validate_search_policy(transaction, current, proof.origin, proof)
    validate_lineage(transaction, current, envelope.previous_decision, proof.origin)
    if current.metadata.get('automatic_recognition') is None:
        if (envelope.previous_decision.confirmed_work or envelope.decision != envelope.previous_decision
                or envelope.draft_plan != observation_draft(current, envelope.previous_decision, proof)
                or any(value is not None for value in (envelope.initial_movie_selection,
                    envelope.initial_movie_completion, envelope.initial_tv_completion, envelope.movie_change,
                    envelope.media_archive))):
            raise ValueError('保存自动观察不能顺带确认作品、改内容或创建媒体操作')
        return AutomaticPublication(envelope.previous_decision, envelope.draft_plan, 'observation')
    if encode(current.metadata.get('automatic_recognition')) != encode(proof.to_dict()):
        raise ValueError('自动语义完成缺少原已保存观察')
    if envelope.initial_tv_completion is not None:
        return None  # The complete TV validator consumes this exact proof.
    expected = movie_identity_publication(current, envelope.previous_decision, proof)
    if (any(value is not None for value in (envelope.initial_movie_selection,
            envelope.initial_movie_completion, envelope.movie_change, envelope.media_archive))
            or envelope.decision != expected.decision or envelope.draft_plan != expected.draft):
        raise ValueError('自动电影作品交接未使用唯一重建结果')
    return expected


def persist_observation(service, proof):
    from .projection_authority import TaskTerminalMutation, commit_projected_plan
    state = service.decision_state_store.load(proof.origin.task_id)
    current = service.plan_store.load(proof.origin.task_id)
    validate_lineage(service, current, state, proof.origin)
    proof.resolve()
    if current.metadata.get('automatic_recognition') is not None:
        if encode(current.metadata['automatic_recognition']) != encode(proof.to_dict()):
            raise ValueError('已有不同的自动识别观察，不能覆盖')
        return state, current
    return commit_projected_plan(service.decision_state_store, service.plan_store,
        previous=state, candidate=state, draft_plan=observation_draft(current, state, proof),
        automatic_recognition=proof,
        task_terminal=TaskTerminalMutation.prepare(service.task_root, proof.origin.task_id,
            lambda _state, _plan, task: task))


def recognition_from_observation(proof, state):
    from .process import _planning_provider_identity, _resolve_plan_naming_decision
    from .rename_plan import WorkIdentity
    from .projection_authority import RecognitionDecisionInput
    from .decision_state import DecisionStateStore
    from .tv_completion_observations import TVSeasonObservation
    from .season_identity import resolve_initial_tv_season
    selected, name, info = proof.resolve()
    movie = selected['entity_type'] == 'movie'
    naming = _resolve_plan_naming_decision(name=name, info=info, entity_type=selected['entity_type'],
                                         provider_selection=None)
    identity, year = _planning_provider_identity(info=info, name=naming.display_title, is_movie=movie,
        selection=selected, decision_source='provider_unique', evidence=('原自动作业完整批次唯一候选',))
    anime, _movie = proof.resolved_task_flags()
    work = WorkIdentity(status='confirmed', domain='animation' if anime else 'general',
        title=naming.display_title, year=year, primary_provider=identity,
        domain_source='task_metadata' if proof.origin.is_anime is not None else 'provider_selection')
    seasons = None if movie else TVSeasonObservation.capture(work, info)
    season, source = (None, 'unset') if movie else resolve_initial_tv_season(seasons, work,
        Path(proof.origin.path), proof.origin.requested_season, None)
    return RecognitionDecisionInput(proof.origin.task_id, work, (), season, source,
        None if movie else 'tv_episodes', DecisionStateStore.fingerprint(state),
        automatic_recognition=proof), seasons


def validate_pending_recognition(service, plan, state, recognition):
    proof = recognition.automatic_recognition
    if (type(proof) is not AutomaticRecognition or recognition.initial_movie_request is not None
            or encode(plan.metadata.get('automatic_recognition')) != encode(proof.to_dict())):
        raise ValueError('自动完成缺少原已保存观察，不能伪造人工选择')
    validate_lineage(service, plan, state, proof.origin)
    expected, seasons = recognition_from_observation(proof, state)
    if expected != recognition:
        raise ValueError('自动确认结果与原资料观察的唯一裁决不一致')
    return seasons


def validate_resume(service, plan, state):
    """Original unresolved work, or original movie identity awaiting versions.

    Completed version decisions are never replayed. Pre-identity edits retain
    the existing fail-closed rule; they require explicit manual work selection.
    """
    from .default_content import source_only_pending
    from .decision_state import DecisionStateStore
    proof = AutomaticRecognition.from_mapping(plan.metadata.get('automatic_recognition'))
    if plan.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(state):
        raise ValueError('自动续做的Plan与当前作品决定不一致')
    if source_only_pending(plan, state):
        validate_lineage(service, plan, state, proof.origin)
        proof.resolve()
    else:
        if (not state.confirmed_work or state.movie_version_selection is not None
                or state.tv_planning_authority is not None):
            raise ValueError('作品已完成或不是待确认的自动电影，请打开当前计划')
        if validate_automatic_movie_selection(service, plan, state) != proof:
            raise ValueError('自动电影续做与原保存资料不一致')
    return proof


def commit_movie_identity(service, proof):
    from .projection_authority import TaskTerminalMutation, commit_projected_plan
    state, current = persist_observation(service, proof)
    expected = movie_identity_publication(current, state, proof)

    def finish(decision, _plan, task):
        task.update(name=decision.confirmed_work['title'], workflow_status='need_confirm',
                    plan_status='invalid', is_anime=decision.library_domain == 'animation',
                    is_movie=True, error=None, completed_at=None)
        return task

    return commit_projected_plan(service.decision_state_store, service.plan_store,
        previous=state, candidate=expected.decision, draft_plan=expected.draft,
        automatic_recognition=proof,
        task_terminal=TaskTerminalMutation.prepare(service.task_root, proof.origin.task_id, finish))


def validate_automatic_movie_selection(service, plan, state):
    """Actual automatic identity result, distinct from a manual success receipt.

    Subsequent user source/companion choices are retained. Only the original
    unconfirmed-to-confirmed transition proves the movie identity handoff.
    """
    proof = AutomaticRecognition.from_mapping(plan.metadata.get('automatic_recognition'))
    validate_original_job(service, proof.origin)
    validate_search_policy(service, plan, proof.origin, proof)
    return _validate_movie_identity_history(service, plan, state, proof)


def _validate_movie_identity_history(service, plan, state, proof):
    from .projection_authority import validate_plan_projection_receipt
    from .plan_store import PlanStore
    from .movie_work_correction import preceding_work
    recognition, seasons = recognition_from_observation(proof, state)
    if seasons is not None or plan.work_identity.to_dict() != state.confirmed_work:
        raise ValueError('当前电影作品与原自动资料裁决不一致')
    selected_work = plan.work_identity
    cursor, seen = plan, set()
    for _ in range(256):
        fingerprint = PlanStore.fingerprint(cursor)
        if fingerprint in seen:
            raise ValueError('自动电影作品历史出现循环')
        seen.add(fingerprint)
        validate_plan_projection_receipt(cursor)
        if (cursor.task_id != plan.task_id or cursor.source_root != plan.source_root
                or cursor.work_identity != selected_work
                or cursor.initial_source_basis != state.initial_source_basis
                or metadata_fingerprints(cursor) != metadata_fingerprints(plan)
                or not cursor.revision_parent_fingerprint):
            raise ValueError('自动电影作品历史缺失、跨源或身份变化')
        parent = service.plan_store.load_version(plan.task_id, cursor.revision_parent_fingerprint)
        validate_plan_projection_receipt(parent)
        if cursor.revision_action == 'automatic_movie_selection':
            if (cursor.work_identity != recognition.work_identity
                    or parent.work_identity is not None or parent.task_id != cursor.task_id
                    or parent.source_root != cursor.source_root
                    or parent.revision_action != 'automatic_observation'
                    or metadata_fingerprints(parent) != metadata_fingerprints(plan)
                    or parent.metadata.get('decision_state_revision') + 1 != cursor.metadata.get('decision_state_revision')
                    or parent.initial_source_basis != state.initial_source_basis):
                raise ValueError('自动电影确认缺少原未确认观察的真实提交历史')
            return proof
        selected_work = preceding_work(cursor, parent)
        cursor = parent
    raise ValueError('自动电影作品历史超过有界检查范围')
