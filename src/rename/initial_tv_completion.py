"""One first-TV/review transaction from frozen source and Provider observations."""
from contextlib import contextmanager
from dataclasses import dataclass, replace
import json
from pathlib import Path

from . import content_selection as content
from .content_associations import encode
from .content_choice import _evidence
from .decision_state import DecisionStateStore
from .initial_movie_completion import RootSnapshotSource
from .initial_movie_selection import InitialMovieRequest, _task
from .projection_authority import RecognitionDecisionInput
from .rename_plan import RenamePlan
from .target_routing import TargetRootSnapshot, resolve_target_root
from .catalog_namespace import NamespaceSnapshot
from .tv_completion_observations import (FrozenTVResolution, ProviderCatalogSnapshot, TVSeasonObservation,
    observe_provider_catalog, verify_provider_catalog)
from .source_episode_binding import SourceEpisodeObservation, SourceEpisodeIdentity
from .collision_projection import (CollisionProjection, observe_collisions,
    project_collisions, observe_path)
from .source_basis import SourceInputProof
from .work_affiliation import WorkAffiliation, AffiliationChoice


@dataclass(frozen=True)
class InitialTVCompletionContext:
    service: object
    plan_store: object
    state_store: object
    task_root: Path
    series: object
    series_root: Path
    provider: object
    provider_root: Path
    roots: RootSnapshotSource

    @classmethod
    def from_service(cls, service):
        from .plan_review import PlanReviewService
        from .series_catalog import SeriesCatalog
        from .provider_link_catalog import ProviderLinkCatalog
        if (not isinstance(service, PlanReviewService) or type(service.root_snapshot_source) is not RootSnapshotSource
                or type(service.series_catalog) is not SeriesCatalog
                or type(service.provider_link_catalog) is not ProviderLinkCatalog):
            raise ValueError('TV完成缺少实际审核服务、登记与目标配置观察')
        return cls(service, service.plan_store, service.decision_state_store, Path(service.task_root),
            service.series_catalog, Path(service.series_catalog.root), service.provider_link_catalog,
            Path(service.provider_link_catalog.root), service.root_snapshot_source)

    def validate(self, transaction=None):
        if type(self).from_service(self.service) != self:
            raise ValueError('TV完成的原服务、登记根或配置来源已变化')
        if transaction is not None and (transaction.plan_store is not self.plan_store
                or transaction.state_store is not self.state_store or transaction.task_root != self.task_root):
            raise ValueError('TV完成不属于当前实际事务服务')

    @contextmanager
    def publication_lock(self, transaction, intent):
        self.validate(transaction)
        if type(intent) is not InitialTVCompletionIntent or intent.context != self:
            raise ValueError('TV完成缺少原冻结上下文')
        with self.series.lock(intent.inputs.series_key, read_only=True), self.provider.lock():
            self.series.verify_namespace_snapshot(intent.inputs.series_key, intent.inputs.namespace)
            verify_provider_catalog(self.provider, intent.inputs.provider_namespace)
            yield


@dataclass(frozen=True)
class TVProjectionInputs:
    task_id: str
    plan_fingerprint: str
    decision_fingerprint: str
    task_fingerprint: str
    recognition: RecognitionDecisionInput
    roots: TargetRootSnapshot
    series_key: str
    namespace: NamespaceSnapshot
    provider_namespace: ProviderCatalogSnapshot
    resolution: FrozenTVResolution | None
    scope: object
    sources: SourceEpisodeObservation
    seasons: TVSeasonObservation
    special_json: str
    evidence_json: str
    affiliation_choice: AffiliationChoice | None = None
    previous_affiliation: str = ''


@dataclass(frozen=True)
class TVCompletionInputs(TVProjectionInputs):
    mode: str = 'initial'
    edit_json: str = '{}'
    source_input: SourceInputProof | None = None
    refresh_request: object = None
    work_request: object = None
    refresh_dry_run: bool | None = None


@dataclass(frozen=True)
class InitialTVCompletionIntent:
    inputs: TVCompletionInputs
    context: InitialTVCompletionContext
    refresh_context: object = None

    @property
    def task_id(self):
        return self.inputs.task_id


@dataclass(frozen=True)
class InitialTVCompletion:
    decision: object
    draft: object
    cohort: object = None


def _registration(recognition, resolution, namespace, provider_namespace, roots, *, choice=None, previous=None):
    from .series_registration import build_series_registration_authority
    from .work_affiliation import resolve_record
    if type(namespace) is not NamespaceSnapshot or namespace.backend != 'series' or type(provider_namespace) is not ProviderCatalogSnapshot:
        raise ValueError('TV完成缺少两类登记的完整观察')
    if resolution is not None:
        if type(resolution) is not FrozenTVResolution:
            raise ValueError('TV资料观察类型无效')
    work = recognition.work_identity
    raw, _source, _reason = resolve_record(work, namespace, roots, choice=choice, previous=previous,
        resolution=resolution.resolution() if resolution else None,
        provider_namespace=provider_namespace if resolution else None)
    fresh = build_series_registration_authority(resolution.resolution() if resolution else work,
        manual_season=recognition.season_number)
    return raw['series_key'] if raw else fresh.series_key, resolution.resolution() if resolution else None, None, raw


@contextmanager
def _namespace(context, recognition, resolution, *, roots=None, choice=None, previous=None):
    roots = roots or context.roots.observe()
    from .series_registration import build_series_registration_authority
    key = build_series_registration_authority(resolution.resolution() if resolution else recognition.work_identity,
        manual_season=recognition.season_number).series_key
    observed = None
    for _ in range(2):
        with context.series.lock(key, read_only=True), context.provider.lock():
            series = context.series.namespace_snapshot(key)
            provider = observe_provider_catalog(context.provider)
            if observed is not None and observed != (series, provider):
                raise ValueError('切换实际系列键期间登记观察变化')
            actual, *_ = _registration(recognition, resolution, series, provider, roots, choice=choice, previous=previous)
            if actual == key:
                yield key, series, provider
                return
            observed, key = (series, provider), actual
    raise ValueError('无法稳定确定实际系列登记键')


def _routing(inputs):
    from .canonical_naming import decision_from_work_identity, build_canonical_work_root, fresh_collision_candidate
    from .series_registration import affiliated_registration
    from .work_affiliation import freeze
    work = inputs.recognition.work_identity
    recognition = inputs.recognition
    if getattr(inputs, 'mode', None) in {'season', 'range'}:
        from .season_identity import require_manual_season
        key = 'season' if inputs.mode == 'range' else 'season_number'
        recognition = replace(recognition, season_number=require_manual_season(json.loads(inputs.edit_json).get(key)))
    actual, resolution, match, raw = _registration(recognition, inputs.resolution,
        inputs.namespace, inputs.provider_namespace, inputs.roots, choice=inputs.affiliation_choice,
        previous=json.loads(inputs.previous_affiliation) if inputs.previous_affiliation else None)
    if actual != inputs.series_key:
        raise ValueError('TV完成使用的系列键不属于实际作品和登记')
    naming = decision_from_work_identity(work)
    target = resolve_target_root(work.domain, 'tv_episodes', inputs.roots)
    generated = build_canonical_work_root(resolved_target_root=target, decision=naming, year=work.year)
    root = (match.canonical_root if match is not None else Path(raw['canonical_root']) if raw else
            fresh_collision_candidate(generated_root=generated, decision=naming,
                registered_roots=tuple(Path(json.loads(payload)['canonical_root']) for _, payload in inputs.namespace.records)))
    if not root.is_relative_to(target):
        raise ValueError('已登记系列根不属于当前冻结目标配置')
    additional = tuple(('bangumi', 'subject', s) for s, _ in resolution.season_assignments) if resolution else ()
    if resolution is not None and resolution.tmdb_tv_id:
        additional += (('tmdb', 'tv', str(resolution.tmdb_tv_id)),)
    affiliation = freeze(work, inputs.namespace, inputs.roots, key=actual, root=root,
        origin=dict(task_id=inputs.task_id, plan=inputs.plan_fingerprint, decision=inputs.decision_fingerprint,
                    task=inputs.task_fingerprint), choice=inputs.affiliation_choice,
        previous=json.loads(inputs.previous_affiliation) if inputs.previous_affiliation else None,
        additional_members=additional, resolution=resolution,
        provider_namespace=inputs.provider_namespace if resolution is not None else None)
    naming = decision_from_work_identity(affiliation.work(work))
    revision = raw['revision'] if raw else 0
    proof = affiliated_registration(work, affiliation, resolution=resolution, record=raw)
    if resolution is not None:
        from .series_registration import bind_affiliated_resolution
        resolution = bind_affiliated_resolution(resolution, proof, raw, inputs.provider_namespace.mappings())
        from .series_registration import SeriesRegistrationAuthority
        proof = SeriesRegistrationAuthority.from_mapping(resolution.registration_authority)
    manual = dict(series_key=actual, canonical_root=str(root), catalog_revision=revision,
        decision_source='explicit_user', registration_authority=proof.to_dict(), affiliation=affiliation.to_dict())
    return target, root, naming, resolution, revision, manual, match is None and raw is None and root != generated


def _projected_materials(current, selection, projected):
    """Retain frozen omitted facts; kept media must still be projected."""
    content.require_plan_binding(current, selection)
    omitted = content.omitted_sources(selection, None)
    if set(current.ignored_sources) != omitted:
        raise ValueError('TV材料保留范围与原内容决定不一致')
    preserved_sources = omitted
    original = selection.to_dict()['materials']
    material = {row['source_path']: row for row in original if Path(row['source_path']) in preserved_sources}
    material.update({str(e.source_path): content.source_material(e) for e in projected.entries})
    if any(row['source_path'] not in material for row in original):
        raise ValueError('TV完整修订丢失保留来源材料')
    return [material[row['source_path']] for row in original]


def _inherit_manual_episode_projection(current, state, fresh_plan, automatic):
    """A content edit cannot silently replace a prior explicit episode choice."""
    decision = state.source_episode_projection_decision
    if not decision or decision.get('decision') == 'automatic_unique':
        return automatic, None
    decision = json.loads(encode(decision))
    original = json.loads(encode(state.source_episode_projection))
    stored = current.to_dict()['metadata'].get('source_episode_projection')
    if (set(decision) != {'decision', 'projection_fingerprint'}
            or decision['decision'] not in {'absolute', 'relative'}
            or not isinstance(original, dict) or original != stored
            or original.get('mode') != decision['decision']
            or original.get('projection_fingerprint') != decision['projection_fingerprint']):
        raise ValueError('原人工集号选择与当前计划不一致，不能自动重建')
    candidates = list(fresh_plan.to_dict()['metadata'].get('source_episode_projection_candidates', ()))
    if automatic is not None:
        candidates.append(json.loads(encode(automatic)))
    matches = [row for row in candidates if row == original]
    if len(matches) != 1:
        raise ValueError('原人工集号选择不再唯一匹配当前完整来源，请先核查')
    return matches[0], decision


def _semantic(current, state, inputs, *, service=None):
    from .plan_review import entry_id
    from .provider_link_decision import freeze_source_episode_projection, apply_source_episode_projection
    from .process import _project_provider_episode_ownership
    from .provider_special_evidence import project_provider_special_evidence
    if (type(inputs) is not TVCompletionInputs or inputs.task_id != current.task_id
            or inputs.plan_fingerprint != _fingerprint(current)
            or inputs.decision_fingerprint != DecisionStateStore.fingerprint(state)
            or type(inputs.sources) is not SourceEpisodeObservation
            or type(inputs.seasons) is not TVSeasonObservation
            or inputs.mode not in {'initial', 'boundary', 'role', 'episode', 'special', 'special_refresh', 'refresh', 'season', 'work', 'content', 'affiliation', 'range', 'versions'}):
        raise ValueError('TV完成缺少冻结原计划、决定或来源观察')
    if inputs.mode == 'refresh':
        if type(inputs.refresh_dry_run) is not bool:
            raise ValueError('原作品刷新缺少冻结执行模式')
    elif inputs.refresh_dry_run is not None:
        raise ValueError('非刷新事务不能更换执行模式')
    if inputs.mode == 'work':
        from .episode_range import work_key
        if state.episode_ranges and work_key(state.confirmed_work) != work_key(inputs.recognition.work_identity):
            raise ValueError('当前任务已有连续集数决定，请先核对并取消该决定，再更换作品；原任务保留')
        from .work_correction import WorkCorrectionRequest
        if (type(inputs.work_request) is not WorkCorrectionRequest
                or inputs.work_request.recognition() != inputs.recognition):
            raise ValueError('TV作品更正必须使用同一明确请求')
    elif inputs.work_request is not None:
        raise ValueError('非作品更正不能夹带更正请求')
    resolution = inputs.resolution.resolution() if inputs.resolution else None
    from .single_subject import require_single_subject_resolution
    require_single_subject_resolution(resolution)
    if inputs.mode == 'boundary':
        raise ValueError('合包功能已退役，请按单条目重新整理')
    if inputs.mode == 'initial':
        from .season_identity import resolve_initial_tv_season
        if (type(inputs.source_input) is not SourceInputProof or inputs.source_input.task_id != current.task_id
                or inputs.source_input.layout_root != current.source_root
                or inputs.source_input.basis_fingerprint != state.initial_source_basis.fingerprint):
            raise ValueError('TV完成缺少一致的原输入路径证明')
        pin = inputs.recognition.initial_movie_request
        from .automatic_recognition import AutomaticRecognition
        automatic = inputs.recognition.automatic_recognition
        if automatic is not None:
            if type(automatic) is not AutomaticRecognition or pin is not None or resolution is not None:
                raise ValueError('自动TV不能混用人工请求或未冻结的跨作品资料')
            requested_season = automatic.origin.requested_season
        elif type(pin) is InitialMovieRequest:
            requested_season = pin.requested_season
        else:
            raise ValueError('TV季度决定缺少原作业或人工请求')
        if resolve_initial_tv_season(inputs.seasons,
                inputs.recognition.work_identity, inputs.source_input.observation_path, requested_season,
                resolution) != (inputs.recognition.season_number, inputs.recognition.season_source):
            raise ValueError('TV季度决定不属于原请求和冻结资料')
        candidate = inputs.recognition.candidate(state)
        candidate = replace(candidate, manual_role_decisions=state.manual_role_decisions)
    elif inputs.mode == 'boundary':
        candidate = inputs.recognition.candidate(state)
        candidate = replace(candidate, manual_role_decisions=state.manual_role_decisions)
    elif inputs.mode == 'work':
        candidate = inputs.recognition.candidate(state)
        roles = dict(state.manual_role_decisions)
        specials = {}
        # A different work cannot inherit an old official episode identity.
        # Keep its source and companions visible until explicitly rematched.
        for identifier, decision in state.provider_special_decisions.items():
            if decision.get('action') == 'unmatched_extra':
                specials[identifier] = decision
            else:
                roles[identifier] = 'needs_correspondence'
        candidate = replace(candidate, manual_role_decisions=roles, provider_special_decisions=specials)
    else:
        if (inputs.recognition.work_identity.to_dict() != state.confirmed_work
                or inputs.recognition.season_number != (state.season_binding or {}).get('season_number')):
            raise ValueError('TV局部确认不能更换作品或季度')
        from .special_selection import freeze_existing
        candidate = replace(state, provider_special_decisions=freeze_existing(current, state.provider_special_decisions))
    from .work_affiliation import pending_for, retain_inventory
    pending = pending_for(inputs.recognition.work_identity, inputs, dict(
        resolution=json.loads(inputs.resolution.payload) if inputs.resolution else None,
        seasons=inputs.seasons.to_dict(), special=json.loads(inputs.special_json)))
    if pending is not None:
        candidate, view = retain_inventory(current, candidate, pending, inputs)
        return InitialTVCompletion(candidate, view)
    target, root, naming, resolution, revision, manual, _ = _routing(inputs)
    edit = json.loads(inputs.edit_json)
    from . import episode_versions
    episode_versions.require_compatible_edit(current, state, inputs.mode, edit)
    if inputs.mode == 'versions':
        candidate = replace(candidate, episode_versions=episode_versions.choose(current, state, edit))
    if inputs.mode == 'content':
        from .content_choice import ContentChangePreview
        preview = ContentChangePreview(encode(edit)).to_dict()
        if (preview['kind'] not in {'exclude', 'restore'} or preview['movie_selection'] is not None
                or preview['plan_fingerprint'] != inputs.plan_fingerprint
                or preview['decision_fingerprint'] != inputs.decision_fingerprint):
            raise ValueError('TV内容选择缺少原冻结预览')
        choice = content.ContentSelection.from_mapping(preview['selection'])
        candidate = replace(candidate, content_selection=choice)
        # Unsigned projection view; the original state/preview is revalidated
        # at the commit boundary before this result can become authoritative.
        current = replace(current, ignored_sources=tuple(sorted(content.omitted_sources(choice, None), key=str)),
            metadata={**current.metadata, 'content_selection': choice.to_dict(),
                      'content_selection_fingerprint': choice.fingerprint})
    if inputs.mode == 'season':
        from .season_identity import provider_key, confirmed_season_binding, require_manual_season
        primary = state.confirmed_work.get('primary_provider') or {}
        number = require_manual_season(edit.get('season_number'))
        from .series_registration import is_manual_subject_registration
        manual_subject = is_manual_subject_registration(current, state)
        if (set(edit) != {'season_number'}
                or state.library_domain not in {'general', 'animation'}
                or not (manual_subject or (primary.get('namespace') == 'tmdb' and primary.get('entity_type') == 'tv'))
                or resolution is not None or inputs.seasons.provider != provider_key(primary)):
            raise ValueError('季度更正必须属于当前作品和已保存资料；不能借更季更换作品')
        if state.source_episode_projection_decision:
            raise ValueError('当前任务包含需重新核对的特殊集号或官方特别篇决定；原计划未改变，请重新生成计划')
        candidate = replace(candidate, season_binding=confirmed_season_binding(
            primary, number, 'explicit_user', previous=state),
            episode_ranges=tuple(replace(d, season=number) for d in candidate.episode_ranges),
            episode_versions=tuple(replace(d, season=number) for d in candidate.episode_versions))
    if inputs.mode in {'refresh', 'special_refresh'} and edit:
        raise ValueError('原作品刷新不能夹带内容修改')
    roles = dict(candidate.manual_role_decisions)
    special_decisions = dict(candidate.provider_special_decisions)
    old = candidate.content_selection
    if old is None or old.to_dict()['policy'] != content.DEFAULT_POLICY:
        raise ValueError('TV完成需要原默认内容来源权威')
    value = old.to_dict()
    entries = content.material_entries(current, old)
    if inputs.mode == 'range':
        from .episode_range import choose_many
        ranges, selected_ids = choose_many(current, state, edit)
        if edit['season'] != (candidate.season_binding or {}).get('season_number'):
            remaining = [e for e in current.entries if e.media_kind == 'video'
                and entry_id(e) not in selected_ids and e.source_path not in current.ignored_sources
                and e.role not in {'extra', 'provider_special'}]
            if remaining:
                raise ValueError('任务中还有其他视频；请先在季度设置中统一更正季号，或调整计划范围后再保存连续集数')
            from .season_identity import confirmed_season_binding
            candidate = replace(candidate, season_binding=confirmed_season_binding(
                candidate.confirmed_work.get('primary_provider') or {}, edit['season'],
                'explicit_user', previous=state))
        selected_sources = {str(e.source_path) for e in entries if entry_id(e) in selected_ids}
        for identifier in selected_ids:
            roles.pop(identifier, None)
            special_decisions.pop(identifier, None)
        candidate = replace(candidate, episode_ranges=ranges, manual_role_decisions=roles,
                            provider_special_decisions=special_decisions,
                            episode_versions=tuple(d for d in candidate.episode_versions if d.source_path not in selected_sources))
    if inputs.mode == 'role':
        if set(edit) != {'entry_id', 'role'} or edit['role'] not in {'extra', 'main_candidate'}:
            raise ValueError('TV用途选择无效')
        selected = [e for e in entries if entry_id(e) == edit['entry_id'] and e.media_kind == 'video']
        if len(selected) != 1 or str(selected[0].source_path) in value['excluded_sources']:
            raise ValueError('TV用途选择不属于唯一当前视频')
        candidate = replace(candidate, episode_ranges=tuple(d for d in candidate.episode_ranges if d.source_path != str(selected[0].source_path)))
        roles[edit['entry_id']] = edit['role']
        special_decisions.pop(edit['entry_id'], None)
        candidate = replace(candidate, manual_role_decisions=roles, provider_special_decisions=special_decisions)
    if inputs.mode == 'special':
        if (edit.get('action') == 'confirmed_special_correspondence'
                and set(edit) == {'entry_id', 'action', 'tmdb_episode_id', 'bangumi_key'}):
            special_ids = [edit['entry_id']]
        elif (edit.get('action') == 'bangumi_special'
                and set(edit) == {'entry_id', 'action', 'bangumi_key', 'title', 'local_episode'}):
            special_ids = [edit['entry_id']]
        elif (edit.get('action') == 'descriptive_special'
                and set(edit) in ({'entry_id', 'action', 'title', 'title_source', 'reference_key'},
                                  {'entry_id', 'action', 'title', 'title_source', 'reference_key', 'bangumi_key', 'tmdb_episode_id'})):
            special_ids = [edit['entry_id']]
        elif (set(edit) == {'entry_id', 'action', 'provider_episode_id'}
              and edit['action'] in {'confirmed_official_special', 'unmatched_extra'}):
            special_ids = [edit['entry_id']]
        elif (set(edit) == {'entry_ids', 'action', 'provider_episode_id'}
              and edit['action'] == 'unmatched_extra' and edit['provider_episode_id'] is None
              and isinstance(edit['entry_ids'], list)):
            special_ids = edit['entry_ids']
        else:
            raise ValueError('TV特别篇选择无效')
        if (edit['action'] not in {'confirmed_official_special', 'unmatched_extra', 'confirmed_special_correspondence', 'descriptive_special', 'bangumi_special'}
                or not special_ids or any(not isinstance(key, str) or not key for key in special_ids)
                or len(set(special_ids)) != len(special_ids)):
            raise ValueError('TV特别篇选择无效')
        for identifier in special_ids:
            selected = [e for e in entries if entry_id(e) == identifier and e.media_kind == 'video']
            if len(selected) != 1 or str(selected[0].source_path) in value['excluded_sources']:
                raise ValueError('TV特别篇选择不属于唯一当前视频')
            roles.pop(identifier, None)
            candidate = replace(candidate, episode_ranges=tuple(d for d in candidate.episode_ranges if d.source_path != str(selected[0].source_path)))
        candidate = replace(candidate, manual_role_decisions=roles)
    view, candidate = _project_tv_base(current, state, candidate, inputs, roles=roles,
        action='initial_tv_completion' if inputs.mode == 'initial' else 'tv_content_' + inputs.mode,
        reason=(inputs.work_request.reason if inputs.mode == 'work' else
                '完整生成首次TV内容' if inputs.mode == 'initial' else '确认当前TV内容选择'))
    projection = None
    special = json.loads(inputs.special_json)
    if special is not None:
        view = project_provider_special_evidence(view, special,
            selected_subject_id=inputs.recognition.work_identity.primary_provider.provider_id,
            provider_link_resolution=resolution)
    if resolution is not None:
        subject = inputs.recognition.work_identity.primary_provider.provider_id
        view, projection = freeze_source_episode_projection(view, resolution, selected_subject_id=subject,
            source_observation=inputs.sources, source_only=True)
        inherited_decision = None
        if inputs.mode == 'episode':
            fingerprint = edit.get('projection_fingerprint')
            saved = current.to_dict()['metadata'].get('source_episode_projection_candidates', ())
            allowed = [row for row in saved if row.get('projection_fingerprint') == fingerprint]
            fresh = view.to_dict()['metadata'].get('source_episode_projection_candidates', ())
            actual = [row for row in fresh if row.get('projection_fingerprint') == fingerprint]
            if set(edit) != {'projection_fingerprint'} or len(allowed) != 1 or actual != allowed:
                raise ValueError('TV集号候选不属于当前修订或已发生变化')
            projection = actual[0]
            view = apply_source_episode_projection(view, projection, source_only=True)
        elif inputs.mode not in {'initial', 'boundary', 'work'}:
            projection, inherited_decision = _inherit_manual_episode_projection(current, state, view, projection)
            if inherited_decision is not None:
                view = apply_source_episode_projection(view, projection, source_only=True)
        candidate = replace(candidate, source_episode_projection=projection,
            source_episode_projection_decision=inherited_decision or ({'decision': projection['mode'] if inputs.mode == 'episode' else 'automatic_unique',
                'projection_fingerprint': projection['projection_fingerprint']} if projection else None))
        view = _project_provider_episode_ownership(view, resolution, inputs.recognition.work_identity, selected_subject_id=subject)
    elif inputs.mode == 'episode':
        raise ValueError('普通TV没有跨段集号候选')
    elif inputs.mode not in {'initial', 'boundary', 'work'} and state.source_episode_projection_decision:
        _inherit_manual_episode_projection(current, state, view, None)
    if inputs.mode == 'special':
        decision = {'action': edit['action']}
        if edit['action'] in {'confirmed_special_correspondence', 'descriptive_special', 'bangumi_special'}:
            from .special_selection import build_correspondence, build_descriptive_decision, build_bangumi_decision
            def chosen(plan):
                if edit['action'] == 'bangumi_special':
                    return build_bangumi_decision(plan, special_ids[0], edit['bangumi_key'], edit['title'], edit['local_episode'])
                if edit['action'] == 'confirmed_special_correspondence':
                    return build_correspondence(plan, edit['tmdb_episode_id'], edit['bangumi_key'], special_ids[0])
                return build_descriptive_decision(plan, special_ids[0], edit['title'], edit['title_source'], edit['reference_key'],
                    edit.get('bangumi_key'), edit.get('tmdb_episode_id'))
            decision = chosen(current)
            if decision != chosen(view):
                raise ValueError('特别篇选择与本次冻结资料不一致')
        elif edit['action'] == 'confirmed_official_special':
            identifier = special_ids[0]
            key = edit['provider_episode_id']
            from .provider_special_evidence import official_special_rows
            saved = official_special_rows(current, identifier)
            fresh = official_special_rows(view, identifier)
            allowed = [row for row in saved if row.get('provider_episode_id') == key]
            actual = [row for row in fresh if row.get('provider_episode_id') == key]
            if not isinstance(key, str) or not key or len(allowed) != 1 or actual != allowed:
                raise ValueError('特别篇候选不属于原冻结资料或已发生变化')
            decision.update({k: actual[0][k] for k in ('provider_namespace', 'provider_entity_type',
                'provider_id', 'season_number', 'episode_number', 'provider_episode_id')})
        elif edit['provider_episode_id'] is not None:
            raise ValueError('附加内容选择不能混入特别篇身份')
        for identifier in special_ids:
            special_decisions[identifier] = decision
        from .special_selection import freeze_existing
        special_decisions = freeze_existing(view, special_decisions)
        candidate = replace(candidate, provider_special_decisions=special_decisions)
    view, candidate = _finish_tv_materials(current, old, view, candidate, inputs,
        special_decisions=special_decisions, resolution=resolution, service=service)
    if inputs.mode == 'refresh':
        from .current_tv_refresh import CurrentTVRefreshRequest, FIELD
        request = inputs.refresh_request
        if (type(request) is not CurrentTVRefreshRequest
                or request.to_dict()['parent_plan_fingerprint'] != inputs.plan_fingerprint):
            raise ValueError('原作品刷新缺少原冻结请求')
        view = replace(view, metadata={**view.metadata, FIELD: dict(request_fingerprint=request.fingerprint,
            parent_plan_fingerprint=inputs.plan_fingerprint)})
    elif inputs.refresh_request is not None:
        raise ValueError('非刷新事务不能夹带刷新请求')
    reservations = [row for _, data in inputs.namespace.records
                    for row in json.loads(data).get('generated_outputs', ())
                    if row.get('kind') == 'bangumi_episode_nfo'
                    and Path(row['target']).parent.parent == Path(view.metadata['canonical_work_root'])]
    view = replace(view, metadata={**view.metadata, 'special_slot_reservations': reservations})
    return InitialTVCompletion(candidate, DecisionStateStore.project_plan(candidate, view))


def _project_tv_base(current, state, candidate, inputs, *, roles, action, reason):
    """Shared unsigned TV material projection; no admission or episode choice."""
    from .rename_plan import PlanEntry, PresentationGroup
    from .presentation import default_presentation_group_id
    from .tv_video_projection import TVVideoInput, TVVideoProjection, project_tv_videos
    from .content_choice import parse_subtitle_placeholders
    from .inventory_completion_projection import project_completion_metadata
    from .plan_review import InventoryCompletionContext, entry_id
    from .provider_link_decision import stage_provider_link_series_decision
    if type(inputs) not in (TVProjectionInputs, TVCompletionInputs):
        raise ValueError('TV材料投影缺少类型化冻结资料')
    old = candidate.content_selection
    content.require_plan_binding(current, old)
    value = old.to_dict()
    entries = content.material_entries(current, old)
    target, root, naming, resolution, revision, manual, _ = _routing(inputs)
    work = WorkAffiliation.from_mapping(manual['affiliation']).work(inputs.recognition.work_identity)
    candidate = replace(candidate, confirmed_work=work.to_dict())
    season = (candidate.season_binding or {}).get('season_number')
    if type(season) is not int or season < 0:
        raise ValueError('TV完成需要实际已裁决季度')
    group = PresentationGroup(default_presentation_group_id('tv_episodes'), 'tv_episodes', 'TV 分集版', None)
    excluded = set(value['excluded_sources'])
    videos = tuple(e for e in entries if e.media_kind == 'video' and str(e.source_path) not in excluded)
    mapping = project_tv_videos(TVVideoInput(videos, current.source_root, work.title,
        group.group_id, root, season, tuple((e.source_path, roles[entry_id(e)]) for e in videos if entry_id(e) in roles),
        season_source='explicit_user' if candidate.season_binding['decision_source'] == 'explicit_user' else 'source_observation',
        ranges=tuple(d for d in candidate.episode_ranges if d.source_path not in excluded),
        versions=tuple(d for d in candidate.episode_versions if d.source_path not in excluded)))
    if type(mapping) is not TVVideoProjection:
        raise ValueError('TV视频唯一投影器已断开')
    mapped = {row.source: row for row in mapping.mappings}
    projected = []
    for entry in entries:
        if str(entry.source_path) in excluded:
            # Retain the frozen material shape for replay of an existing
            # official-special choice. The final content filter omits it.
            projected.append(entry)
            continue
        row = mapped.get(entry.source_path)
        if row is not None:
            entry = replace(entry, target_path=row.target, season_id=row.season_id, episode_id=row.episode_id, episode_end=row.episode_end,
                role=row.role, requires_review=True, presentation_asset=row.asset, collision_resolution=None)
            # The legacy classifier's SP-directory hint is not a current
            # user's asset-role decision. Default-safe content keeps ambiguous
            # clips visible until the typed role/special command resolves them.
            from .media_classifier import LocalRole, observe_watchable_content
            from .rename_plan import AssetRole
            if (row.asset.asset_role.kind == 'extra' and roles.get(entry_id(entry)) != 'extra'
                    and observe_watchable_content(entry.source_path, source_root=current.source_root,
                        work_title=inputs.recognition.work_identity.title).disposition != 'known_non_watchable'):
                relative = entry.source_path.relative_to(current.source_root)
                observed_role = (row.role if row.role in {LocalRole.EPISODE_LIKE_BONUS.value,
                    LocalRole.PROVIDER_SPECIAL.value} else 'unknown')
                entry = replace(entry, target_path=root / 'ManualReview' / relative, role=observed_role,
                    presentation_asset=replace(row.asset, asset_role=AssetRole('unknown')))
        elif entry.media_kind in {'video', 'subtitle'}:
            relative = entry.source_path.relative_to(current.source_root) if entry.source_path != current.source_root else Path(entry.source_path.name)
            entry = replace(entry, target_path=root / 'ManualReview' / relative, season_id=None, episode_id=None,
                presentation_asset=None, collision_resolution=None)
        projected.append(entry)
    context = InventoryCompletionContext(inputs.plan_fingerprint, current.presentation_groups[0].group_id,
        'tv_episodes', inputs.recognition.work_identity.domain, target, inputs.roots.to_dict(), {},
        season, candidate.season_binding['decision_source'])
    metadata = project_completion_metadata(current.metadata, context=context, work_root=root, naming_decision=naming,
        proposed_work_key=None, catalog_work_key=None, catalog_revision=0, catalog_match_source='series',
        completion_mode='initial_tv_completion', unresolved_videos=mapping.unresolved,
        episode_assignment_conflicts=mapping.episode_conflicts, series_key=inputs.series_key,
        season_binding_id=candidate.season_binding['season_binding_id'], series_catalog_revision=revision)
    for key in ('episode_ranges', 'episode_versions', 'source_episode_projection', 'source_episode_projection_candidates'):
        metadata.pop(key, None)
    if inputs.mode == 'work':
        for key in ('provider_special_observation', 'provider_special_candidates',
                    'provider_special_eligibility_unavailable', 'provider_special_observation_required',
                    'provider_regular_episode_overflow', 'provider_regular_episode_ranges'):
            metadata.pop(key, None)
    metadata.update(tv_authority_required=resolution is not None,
        tv_completion_resolution=json.loads(inputs.resolution.payload) if inputs.resolution else None,
        tv_completion_seasons=inputs.seasons.to_dict(),
        tv_completion_special=json.loads(inputs.special_json))
    view = replace(current, entries=parse_subtitle_placeholders(tuple(projected)), metadata=metadata,
        work_identity=work,
        linked_provider_identities=inputs.recognition.linked_provider_identities,
        presentation_groups=(group,), projection_receipt=None,
        execution_policy=replace(current.execution_policy, target_root=target,
            dry_run=inputs.refresh_dry_run if inputs.mode == 'refresh' else current.execution_policy.dry_run),
        revision_parent_fingerprint=inputs.plan_fingerprint,
        revision_action=action, revision_reason=reason)
    if resolution is not None:
        candidate = stage_provider_link_series_decision(candidate, resolution, canonical_root=root, catalog_revision=revision)
    candidate = replace(candidate, series_binding=manual, layout_mode='tv_episodes', catalog_link_intent=None)
    return view, candidate


def _finish_tv_materials(current, old, view, candidate, inputs, *, special_decisions, resolution, service=None):
    """Common owner/subtitle/audio/special/material/TV-authority finish."""
    from .content_associations import project_subtitles, complete_subtitle_associations
    from .audio_projection import project_audio
    from .process import build_process_tv_planning_input
    from .tv_planning_authority import TVPlanningAuthorityBuilder, TVPlanningAuthority
    value = old.to_dict()
    from .episode_range import project as project_ranges
    view = project_ranges(view, candidate.episode_ranges)
    from . import episode_versions
    view = episode_versions.project(view, candidate.episode_versions)
    if type(view) is not RenamePlan:
        raise ValueError('剧集版本唯一投影器已断开')
    associations = complete_subtitle_associations(view, old.associations)
    value['associations'] = associations.to_dict()
    view = replace(view, metadata={**view.metadata, 'content_selection': {**value}})
    view = project_audio(project_subtitles(view, associations, tv_identity=True))
    from .presentation import associate_resolved_extra_subtitles
    view = associate_resolved_extra_subtitles(view)
    if special_decisions:
        from .special_decision_projection import project_special_decisions
        # Replay uses content materials, so give the shared projector the current
        # full TV projection, not the previous revision's paths and roles.
        value['materials'] = _projected_materials(current, old, view)
        view = replace(view, metadata={**view.metadata, 'content_selection': dict(value)})
        view = project_special_decisions(view, special_decisions)
        if type(view) is not RenamePlan:
            raise ValueError('特别篇唯一投影器已断开')
    value.update(materials=_projected_materials(current, old, view),
        evidence=json.loads(inputs.evidence_json), parent_plan_fingerprint=inputs.plan_fingerprint,
        reason=view.revision_reason)
    if service is not None:
        value['evidence'] = _evidence(value, None, old=old)
    choice = content.ContentSelection.from_mapping(value)
    candidate = replace(candidate, content_selection=choice,
                        tv_planning_authority=None, asset_operation=None)
    view = replace(view, ignored_sources=tuple(sorted(content.omitted_sources(choice, None), key=str)),
        metadata={**view.metadata, 'content_selection': choice.to_dict(), 'content_selection_fingerprint': choice.fingerprint})
    view = replace(view, entries=content.material_entries(view, choice))
    metadata = dict(view.metadata)
    for key in ('asset_operation', 'asset_replacement_retained', 'asset_replacement_retired'):
        metadata.pop(key, None)
    view = replace(view, metadata=metadata)
    if resolution is not None:
        authority_input = build_process_tv_planning_input(state=candidate, plan=view,
            resolution=resolution, task_source_scope=inputs.scope,
            observed_paths=tuple((str(row.source), str(row.source)) for row in inputs.sources.rows))
        authority = TVPlanningAuthorityBuilder().build(authority_input)
        if (type(authority) is not TVPlanningAuthority
                or authority != TVPlanningAuthorityBuilder._project(authority_input)):
            raise ValueError('TV规划权威与本次冻结输入不一致或构造器已断开')
        candidate = replace(candidate, tv_planning_authority=authority)
    return view, candidate


def _fingerprint(plan):
    from .plan_store import PlanStore
    return PlanStore.fingerprint(plan)


def build_initial_tv_completion(current, state, intent):
    """从冻结命令生成一次完整映射；预览不赋予提交许可。"""
    if type(intent) is not InitialTVCompletionIntent:
        raise ValueError('TV完整命令缺少冻结意图')
    built = _semantic(current, state, intent.inputs, service=intent.context.service)
    if type(built) is not InitialTVCompletion:
        raise ValueError('TV完整生成器已断开')
    from .work_affiliation import pending_from_plan
    if pending_from_plan(built.draft) is not None:
        return built
    if built.decision.episode_versions:
        from .episode_versions import require_new_season
        require_new_season(intent.context.service, built.draft, built.decision)
    _target, root, _, _, _, _, needs_absence = _routing(intent.inputs)
    if needs_absence and observe_path(root).exists:
        raise ValueError('确定性同名系列目录已存在，需要重新核对')
    probes, _ = observe_collisions(built.draft.entries, root, package_label=current.source_root.name)
    result = project_collisions(built.draft.entries, root, probes, package_label=current.source_root.name)
    if type(result) is not CollisionProjection:
        raise ValueError('TV碰撞投影器已断开')
    built = replace(built, draft=replace(built.draft, entries=result.entries,
        metadata={**built.draft.metadata, 'asset_collision_unresolved': result.unresolved}))
    if intent.inputs.mode == 'boundary':
        from .tv_boundary_completion import resolve, decorate
        resolved = resolve(intent.context.service, current, state, json.loads(intent.inputs.edit_json))
        built = decorate(current, state, built, intent.inputs, resolved)
    return built


def _validate_current(service, current, state, recognition, mode, *, refresh_context=None, refresh_request=None):
    from .default_content import validate_origin, source_only_pending, require_unexecuted_task
    from .provider_selection_authority import (provider_selection_capability, task_media_kind_from_task,
        task_library_domain_from_task, task_media_kind_from_flag, task_library_domain_from_flag)
    from ..utils.utils import read_task_at
    from ..task_queue import TaskQueueManager
    validate_origin(service, current, state)
    choice = content.validate_plan_content(current, state)
    if (choice is None or choice.to_dict()['policy'] != content.DEFAULT_POLICY
            or state.execution_failure is not None
            or state.recovery_state not in (None, 'healthy')
            or state.completed_rebuild_observation is not None):
        raise ValueError('TV完成仅接受未执行的当前默认内容计划')
    task = read_task_at(service.task_root, current.task_id)
    observed_domain = task_library_domain_from_task(task)
    observed_kind = task_media_kind_from_task(task)
    if mode == 'refresh':
        from .current_tv_refresh import validate_active
        if validate_active(refresh_context, service, current, state, refresh_request) != refresh_request:
            raise ValueError('原作品刷新worker验证已断开')
        require_unexecuted_task(task, recognition_worker=True)
    else:
        if refresh_context is not None or refresh_request is not None:
            raise ValueError('非刷新事务不能夹带worker许可')
        if mode == 'work' and recognition.initial_movie_request is not None:
            from .queued_work_selection import validate_request
            validate_request(service, current, state, recognition.initial_movie_request)
        require_unexecuted_task(task, recognition_worker=mode == 'initial' or (mode == 'work' and recognition.initial_movie_request is not None))
    from .asset_operation import require_tv_editable_operation
    if mode == 'versions' or state.episode_versions:
        from .episode_versions import require_new_season
        require_new_season(service, current, state)
    require_tv_editable_operation(state.asset_operation, mode)
    if mode == 'initial':
        if not source_only_pending(current, state) or state.confirmed_work or state.series_binding is not None:
            raise ValueError('首次TV完成必须来自未确认的原来源任务')
        if recognition.automatic_recognition is not None:
            from .automatic_recognition import validate_pending_recognition
            validate_pending_recognition(service, current, state, recognition)
            candidate, _name, _info = recognition.automatic_recognition.resolve()
            anime, movie = recognition.automatic_recognition.resolved_task_flags()
            observed_domain = task_library_domain_from_flag(anime)
            observed_kind = task_media_kind_from_flag(movie)
        else:
            _raw, task, _operation, operation = _task(service, current.task_id)
            pin = recognition.initial_movie_request
            candidate = operation.get('candidate') or {}
            identity = tuple(str(candidate.get(k) or '').strip().casefold() for k in ('namespace', 'entity_type', 'provider_id'))
            if (type(pin) is not InitialMovieRequest or pin.task_id != current.task_id
                    or pin.request_id != operation.get('request_id') or pin.expected_revision != state.revision
                    or operation.get('expected_decision_revision') != state.revision or pin.candidate_identity != identity):
                raise ValueError('首次TV完成不属于原worker选择请求，不能认领后来请求')
            if ('requested_season_number' not in operation
                    or operation['requested_season_number'] != pin.requested_season
                    or operation['requested_season_number'] is not None
                    and type(operation['requested_season_number']) is not int):
                raise ValueError('首次TV完成缺少原请求的季度意图')
        capability = provider_selection_capability(candidate, group_kind='tv_episodes',
            task_media_kind=observed_kind, task_library_domain=observed_domain)
        draft = DecisionStateStore.project_plan(recognition.candidate(state), replace(current, projection_receipt=None))
        if not capability.actionable or not TaskQueueManager._selected_provider_was_persisted(candidate,
                recognition.candidate(state).provider_evidence, draft):
            raise ValueError('首次TV实际选择与识别结果不一致')
    elif mode == 'boundary':
        if not source_only_pending(current, state) or state.confirmed_work or state.pending_tv_selection is None:
            raise ValueError('季度关系确认仅接受原未完成来源与待确认依据')
    elif not current.metadata.get('tv_completion_resolution') and 'tv_completion_resolution' not in current.metadata:
        raise ValueError('旧TV计划缺少完整冻结规划资料，请重新生成')
    if (recognition.work_identity.status != 'confirmed' or recognition.layout_mode != 'tv_episodes'
            or recognition.work_identity.domain != observed_domain
            or type(recognition.season_number) is not int or recognition.season_number < 0):
        raise ValueError('TV完成必须使用已裁决作品域和季度')


def _validate_content_preview(service, current, state, edit):
    from .content_choice import ContentChangePreview, _prepare_change
    preview = ContentChangePreview(encode(edit))
    value = preview.to_dict()
    if (value['kind'] not in {'exclude', 'restore'} or value['movie_selection'] is not None
            or value['task_id'] != current.task_id or value['plan_fingerprint'] != _fingerprint(current)
            or value['decision_fingerprint'] != DecisionStateStore.fingerprint(state)):
        raise ValueError('TV内容选择预览已变化')
    actual = _prepare_change(service, current, state, value['plan_fingerprint'],
        selected_entry_ids=value['selected_entry_ids'], action=value['kind'], reason=value['selection']['reason'])
    if type(actual) is not ContentChangePreview or actual != preview:
        raise ValueError('TV内容选择必须匹配原来源和当前人工预览')


def prepare(service, recognition, *, resolution=None, special=None, seasons=None, mode='initial', edit=None,
            refresh_context=None, refresh_request=None, work_request=None, affiliation_choice=None):
    from ..utils.utils import task_record_snapshot_at
    from .task_batch_scope import TaskSourceScope
    from .provider_special_evidence import ProviderSpecialObservation
    if type(recognition) is not RecognitionDecisionInput:
        raise ValueError('TV完成需要类型化识别决定')
    if special is not None:
        special = (special if type(special) is ProviderSpecialObservation else ProviderSpecialObservation.from_mapping(special)).to_dict()
    current = service.plan_store.load(recognition.task_id)
    state = service.decision_state_store.load(recognition.task_id)
    if type(seasons) is not TVSeasonObservation:
        raise ValueError('TV完成缺少实际Provider季度观察')
    context = InitialTVCompletionContext.from_service(service)
    frozen = FrozenTVResolution.capture(resolution) if resolution is not None else None
    roots = context.roots.observe()
    previous = (state.series_binding or {}).get('affiliation') if mode not in {'initial', 'boundary', 'work'} else None
    with _namespace(context, recognition, frozen, roots=roots, choice=affiliation_choice, previous=previous) as observed:
        key, namespace, providers = observed
    _raw, task, task_fingerprint = task_record_snapshot_at(service.task_root, recognition.task_id)
    scope = TaskSourceScope.from_mapping(task.get('task_source_scope'))
    sources = frozen_sources(current, state, scope)
    if type(sources) is not SourceEpisodeObservation:
        raise ValueError('TV来源观察器已断开')
    inputs = TVCompletionInputs(current.task_id, _fingerprint(current), DecisionStateStore.fingerprint(state),
        task_fingerprint, recognition, roots, key, namespace, providers, frozen, scope, sources, seasons,
        encode(special), encode(state.content_selection.to_dict()['evidence']),
        affiliation_choice=affiliation_choice, previous_affiliation=encode(previous) if previous else '',
        mode=mode, edit_json=encode(edit or {}))
    inputs = replace(inputs, refresh_request=refresh_request, work_request=work_request)
    if mode == 'refresh':
        # Explicit retry creates a new policy; all projections below use
        # this one observation, never live configuration during commit.
        from .trans import is_dry_run_enabled
        inputs = replace(inputs, refresh_dry_run=is_dry_run_enabled())
    if mode == 'work':
        from .work_correction import validate_inputs
        validate_inputs(service, current, state, inputs)
    if mode == 'initial':
        from .source_basis import observe_initial_input
        inputs = replace(inputs, source_input=observe_initial_input(service, current, state))
    return InitialTVCompletionIntent(inputs, context, refresh_context)



def _validate_input_snapshot(service, current, state, inputs):
    """Shared frozen-task/source/catalog checks for preview and locked publication."""
    from ..utils.utils import task_record_snapshot_at
    from .task_batch_scope import TaskSourceScope
    if (type(inputs) is not TVCompletionInputs or inputs.task_id != current.task_id
            or inputs.plan_fingerprint != _fingerprint(current)
            or inputs.decision_fingerprint != DecisionStateStore.fingerprint(state)):
        raise ValueError('TV完成的原计划或决定已变化')
    _raw, task, task_fingerprint = task_record_snapshot_at(service.task_root, current.task_id)
    if task_fingerprint != inputs.task_fingerprint:
        raise ValueError('TV实际任务或选择请求已变化')
    if (type(inputs.scope) is not TaskSourceScope
            or inputs.scope != TaskSourceScope.from_mapping(task.get('task_source_scope'))):
        raise ValueError('TV冻结任务范围与原任务不一致')
    sources = tuple(e.source_path for e in content.material_entries(current, state.content_selection)
                    if e.media_kind == 'video')
    if (type(inputs.sources) is not SourceEpisodeObservation
            or tuple(row.source for row in inputs.sources.rows) != sources):
        raise ValueError('TV冻结来源观察必须完整覆盖原视频集合')
    service.series_catalog.verify_namespace_snapshot(inputs.series_key, inputs.namespace)
    verify_provider_catalog(service.provider_link_catalog, inputs.provider_namespace)
    observed = frozen_sources(current, state, inputs.scope)
    if type(observed) is not SourceEpisodeObservation or observed != inputs.sources:
        raise ValueError('TV逐源物理观察已变化')


def _validate_observations(service, current, state, intent):
    inputs = intent.inputs
    intent.context.validate()
    if intent.context.service is not service or intent.context.roots.observe() != inputs.roots:
        raise ValueError('TV完成服务或四根配置已变化')
    _validate_current(service, current, state, inputs.recognition, inputs.mode,
                      refresh_context=intent.refresh_context, refresh_request=inputs.refresh_request)
    if inputs.mode == 'content':
        _validate_content_preview(service, current, state, json.loads(inputs.edit_json))
    if inputs.mode == 'work':
        from .work_correction import validate_inputs
        validate_inputs(service, current, state, inputs)
    elif inputs.work_request is not None:
        raise ValueError('非作品更正不能夹带更正请求')
    if inputs.mode == 'initial':
        from .source_basis import observe_initial_input
        actual_input = observe_initial_input(service, current, state)
        if type(actual_input) is not SourceInputProof or actual_input != inputs.source_input:
            raise ValueError('TV冻结原输入路径证明与当前请求不一致')
    if inputs.mode == 'boundary':
        from .tv_boundary_completion import BoundaryResolution, validate_inputs
        if type(validate_inputs(service, current, state, inputs)) is not BoundaryResolution:
            raise ValueError('季度确认的原请求验证已断开')
    else:
        _validate_input_snapshot(service, current, state, inputs)
    if inputs.recognition.automatic_recognition is not None:
        from .automatic_recognition import validate_pending_recognition
        if (inputs.mode != 'initial' or inputs.resolution is not None or inputs.special_json != 'null'
                or inputs.seasons != validate_pending_recognition(service, current, state, inputs.recognition)):
            raise ValueError('自动TV季度或特别篇资料不属于原冻结观察')
    if inputs.mode not in {'initial', 'boundary', 'work'} and (
            inputs.resolution != (FrozenTVResolution(encode(current.metadata['tv_completion_resolution']))
                                  if current.metadata.get('tv_completion_resolution') is not None else None)
            or (inputs.mode != 'special_refresh' and inputs.special_json != encode(current.metadata.get('tv_completion_special')))):
        raise ValueError('TV内容修正不能替换原冻结Provider资料')
    if inputs.mode not in {'initial', 'boundary', 'work'} and inputs.seasons != TVSeasonObservation.from_mapping(
            current.to_dict()['metadata'].get('tv_completion_seasons')):
        raise ValueError('TV内容修正不能替换原季度资料')
    return inputs


def commit(service, intent, *, task_terminal):
    from .public_revision import commit as publish
    return publish(service, intent, task_terminal=task_terminal)


def validate_commit(transaction, envelope):
    from .public_revision import require_publication
    result = require_publication(transaction, envelope)
    if type(result.built) is not InitialTVCompletion or envelope.initial_tv_completion != result.command:
        raise ValueError('TV提交未使用锁内唯一生成结果')
    return result.built


def frozen_sources(current, state, scope):
    """从接纳时的物理身份构造逐集观察，不访问来源路径。"""
    available = {Path(row.source_path): row for row in scope.source_identities}
    rows = []
    for entry in content.material_entries(current, state.content_selection):
        if entry.media_kind != 'video':
            continue
        row = available.get(entry.source_path)
        if row is None:
            raise ValueError('冻结来源清单缺少视频')
        pieces = row.physical_key.split(':')
        if len(pieces) != 4 or pieces[0] != 'device' or pieces[2] != 'inode':
            raise ValueError('冻结来源物理身份无法转换')
        rows.append(SourceEpisodeIdentity(entry.source_path, row.size, row.mtime_ns, int(pieces[1]), int(pieces[3])))
    return SourceEpisodeObservation(tuple(rows))
