"""First complete movie revision from actual source authority and frozen observations."""
from contextlib import contextmanager
from dataclasses import dataclass, replace
import json
from pathlib import Path

from . import content_selection as content
from .content_associations import encode
from .content_choice import _load, _evidence
from .decision_state import DecisionStateStore
from .target_routing import TargetRootSnapshot, resolve_target_root, inspect_frozen_target_routing
from .catalog_namespace import NamespaceSnapshot
from .collision_projection import CollisionProbes, CollisionProjection, observe_collisions, project_collisions, observe_path, verify_probes
from .work_affiliation import WorkAffiliation, AffiliationChoice


@dataclass(frozen=True)
class RootSnapshotSource:
    reader: object

    def observe(self):
        if not callable(self.reader):
            raise ValueError('审核服务未配置只读目标根观察')
        snapshot = self.reader()
        if type(snapshot) is not TargetRootSnapshot or TargetRootSnapshot.from_dict(snapshot.to_dict()) != snapshot:
            raise ValueError('目标配置观察必须包含有效的类型化四根')
        return snapshot


@dataclass(frozen=True)
class InitialMovieCompletionContext:
    service: object
    plan_store: object
    state_store: object
    task_root: Path
    catalog: object
    catalog_root: Path
    roots: RootSnapshotSource

    @classmethod
    def from_service(cls, service):
        from .plan_review import PlanReviewService
        if not isinstance(service, PlanReviewService) or type(service.root_snapshot_source) is not RootSnapshotSource:
            raise ValueError('首次电影完成缺少已配置审核服务及目标观察来源')
        return cls(service, service.plan_store, service.decision_state_store, Path(service.task_root),
                   service.work_catalog, Path(service.work_catalog.root), service.root_snapshot_source)

    def validate(self, transaction=None):
        if type(self).from_service(self.service) != self:
            raise ValueError('首次电影完成的服务、存储或配置来源已变化')
        if transaction is not None and (self.plan_store is not transaction.plan_store
                or self.state_store is not transaction.state_store or self.task_root != transaction.task_root):
            raise ValueError('首次电影完成事务不属于预览服务')

    @contextmanager
    def publication_lock(self, transaction, intent):
        self.validate(transaction)
        if type(intent) is not InitialMovieCompletionIntent or intent.context != self:
            raise ValueError('首次电影完成缺少原服务冻结意图')
        with self.catalog.lock(intent.inputs.work_key, read_only=True):
            self.catalog.verify_namespace_snapshot(intent.inputs.work_key, intent.inputs.namespace)
            yield


@dataclass(frozen=True)
class CompletionInputs:
    task_id: str
    plan_fingerprint: str
    decision_fingerprint: str
    task_fingerprint: str
    work_key: str
    namespace: NamespaceSnapshot
    roots: TargetRootSnapshot
    groups_json: str
    excluded: tuple[str, ...]
    reason: str
    evidence_json: str
    work_observation: object = None
    affiliation_choice: AffiliationChoice | None = None
    previous_affiliation: str = ''
    mode: str = 'initial'
    replace_version_ref: str | None = None


@dataclass(frozen=True)
class InitialMovieCompletionIntent:
    inputs: CompletionInputs
    context: InitialMovieCompletionContext
    probes: CollisionProbes
    root_probes: CollisionProbes

    @property
    def task_id(self):
        return self.inputs.task_id


@dataclass(frozen=True)
class InitialMovieCompletion:
    decision: object
    draft: object


def _catalog_record(current, namespace, roots, *, choice=None, previous=None):
    from .work_affiliation import resolve_record
    if type(namespace) is not NamespaceSnapshot:
        raise ValueError('首次电影完成缺少完整作品登记观察')
    return resolve_record(current.work_identity, namespace, roots, choice=choice, previous=previous)[0]


@contextmanager
def _namespace(service, current, *, roots=None, choice=None, previous=None):
    roots = roots or service.root_snapshot_source.observe()
    from .work_intent import derive_work_intent_key
    key = derive_work_intent_key(primary_provider=current.work_identity.primary_provider, group_kind='movie_continuous')
    with service.work_catalog.lock(key, read_only=True):
        snapshot = service.work_catalog.namespace_snapshot(key)
        record = _catalog_record(current, snapshot, roots, choice=choice, previous=previous)
        actual = record['work_key'] if record else key
        if actual == key:
            yield key, snapshot
            return
    # Release the provisional key before acquiring the actual historical key.
    with service.work_catalog.lock(actual, read_only=True):
        service.work_catalog.verify_namespace_snapshot(actual, snapshot)
        yield actual, snapshot


def _routing(current, inputs):
    if inputs.work_observation is not None:
        from .movie_work_correction import MovieWorkObservation
        if type(inputs.work_observation) is not MovieWorkObservation:
            raise ValueError('电影更正缺少类型化资料观察')
        current = replace(current, work_identity=inputs.work_observation.target())
    from .canonical_naming import decision_from_work_identity, build_canonical_work_root, fresh_collision_candidate
    from .work_intent import derive_work_intent_key
    naming = decision_from_work_identity(current.work_identity)
    target = resolve_target_root(current.work_identity.domain, 'movie_continuous', inputs.roots)
    record = _catalog_record(current, inputs.namespace, inputs.roots, choice=inputs.affiliation_choice,
        previous=json.loads(inputs.previous_affiliation) if inputs.previous_affiliation else None)
    actual_key = record['work_key'] if record else derive_work_intent_key(
        primary_provider=current.work_identity.primary_provider, group_kind='movie_continuous')
    if inputs.work_key != actual_key:
        raise ValueError('首次完成作品键不属于实际电影及登记观察')
    generated = build_canonical_work_root(resolved_target_root=target, decision=naming, year=current.work_identity.year)
    root = Path(record['canonical_root']) if record else fresh_collision_candidate(
        generated_root=generated, decision=naming,
        registered_roots=tuple(Path(json.loads(raw)['canonical_root']) for _name, raw in inputs.namespace.records))
    from .work_affiliation import freeze
    affiliation = freeze(current.work_identity, inputs.namespace, inputs.roots, key=inputs.work_key, root=root,
        origin=dict(task_id=inputs.task_id, plan=inputs.plan_fingerprint, decision=inputs.decision_fingerprint,
                    task=inputs.task_fingerprint), choice=inputs.affiliation_choice,
        previous=json.loads(inputs.previous_affiliation) if inputs.previous_affiliation else None)
    naming = decision_from_work_identity(affiliation.work(current.work_identity))
    _, issues = inspect_frozen_target_routing(root_snapshot=inputs.roots.to_dict(),
        root_config_fingerprint=inputs.roots.fingerprint, domain=current.work_identity.domain,
        group_kind='movie_continuous', target_root=target, canonical_work_root=root)
    if issues:
        raise ValueError(issues[0].message)
    return target, root, naming, record, root != generated and record is None, affiliation


def _semantic(current, state, inputs):
    """Pure full source/companion/MVS reconstruction; observers run outside."""
    from .movie_content import _groups_input
    if type(inputs) is not CompletionInputs or inputs.task_id != current.task_id or inputs.mode not in {'initial', 'affiliation'}:
        raise ValueError('首次电影完成输入无效')
    if (inputs.plan_fingerprint != content_fingerprint(current)
            or inputs.decision_fingerprint != DecisionStateStore.fingerprint(state)):
        raise ValueError('首次电影完成的原计划或决定已变化')
    if inputs.work_observation is not None:
        from .movie_work_correction import rebuild
        return rebuild(current, state, inputs)
    rows = _groups_input(json.loads(inputs.groups_json))
    if encode(rows) != inputs.groups_json or not inputs.reason.strip() or not rows:
        raise ValueError('首次电影组缺少冻结输入与明确理由')
    old = state.content_selection
    value = old.to_dict()
    members = [p for row in rows for p in row['sources']]
    all_videos = {row['path'] for row in value['associations']['sources'] if row['kind'] == 'video'}
    explicit = set(inputs.excluded)
    if (len(members) != len(set(members)) or not set(members) <= all_videos
            or set(members).intersection(value['excluded_sources'])
            or len(explicit) != len(inputs.excluded) or not explicit <= all_videos
            or explicit.intersection(members) or any(not row['sources'] for row in rows)):
        raise ValueError('首次电影组不能重复、越界、恢复隐藏来源或同时保留与排除同一视频')
    if inputs.mode == 'affiliation' and state.movie_version_selection is not None:
        from .movie_content import validate_added_movie_sources
        validate_added_movie_sources(current, old, rows)
    from .work_affiliation import pending_for, retain_inventory
    pending = pending_for(current.work_identity, inputs, dict(groups=rows, excluded=list(inputs.excluded)))
    if pending is not None:
        if inputs.replace_version_ref is not None:
            raise ValueError('请先明确归入已有作品，再选择它的版本换源')
        candidate, draft = retain_inventory(current, state, pending)
        return InitialMovieCompletion(candidate, draft)
    return _project_movie_content(current, current, state, inputs, rows=rows,
        previous=state.movie_version_selection if inputs.mode == 'affiliation' else None,
        preferred_version_ref=current.presentation_groups[0].preferred_version_ref,
        revision_action='movie_work_affiliation' if inputs.mode == 'affiliation' else 'initial_movie_completion')


def _project_movie_content(origin, current, state, inputs, *, rows, previous=None,
                           preferred_version_ref=None, revision_action='initial_movie_completion'):
    """Shared pure movie body. Origin is not the destination material view.

    Ordinary completion validates its unchanged admission contract above;
    partition validates its complete source/group restriction independently.
    Neither this body nor its unsigned result is a publication capability.
    """
    from .movie_video_projection import MovieVideoInput, MovieVideoProjection, project_movie_videos
    from .movie_version_selection import create_selection, project_selection, MovieVersionSelection, validate_catalog_labels
    from .presentation import default_presentation_group_id
    from .rename_plan import PlanEntry
    from .content_choice import parse_subtitle_placeholders
    from .content_associations import project_subtitles, complete_subtitle_associations
    from .audio_projection import project_audio
    from .inventory_completion_projection import project_completion_metadata
    from .plan_review import InventoryCompletionContext
    old = state.content_selection
    content.require_plan_binding(current, old)
    value = old.to_dict()
    members = [p for row in rows for p in row['sources']]
    explicit = set(inputs.excluded)
    # First selection omits unselected videos without inventing versions.
    # Partition can still hide members of an already frozen version registry.
    version_excluded = explicit.intersection(members)
    value['excluded_sources'] = sorted(set(value['excluded_sources']) | (explicit - set(members)))
    target, root, naming, record, _, affiliation = _routing(origin, inputs)
    work = affiliation.work(origin.work_identity)
    if record is not None and record['work_key'] != inputs.work_key:
        raise ValueError('首次完成没有使用实际登记作品键')
    if len(current.presentation_groups) != 1:
        raise ValueError('首次完成需要唯一实际库存组')
    group = replace(current.presentation_groups[0], group_id=default_presentation_group_id('movie_continuous'),
        kind='movie_continuous', display_name='电影连续版', preferred_version_ref=preferred_version_ref, provider_identity=None)
    entries = content.material_entries(current, old)
    videos = tuple(entry for entry in entries if entry.media_kind == 'video')
    mapping = project_movie_videos(MovieVideoInput(videos, current.source_root, work.title,
        group.group_id, root, primary_sources=tuple(Path(path) for path in members),
        excluded_sources=tuple(Path(path) for path in explicit | set(value['excluded_sources'])), groups_supplied=True))
    if type(mapping) is not MovieVideoProjection:
        raise ValueError('电影视频唯一投影器已断开')
    mapped = {row.source: row for row in mapping.mappings}
    projected = []
    for entry in entries:
        source = entry.source_path
        if source in mapped:
            row = mapped[source]
            entry = replace(entry, target_path=row.target, season_id=None, episode_id=None, role=row.role,
                requires_review=True, presentation_asset=row.asset, collision_resolution=None)
        elif entry.media_kind == 'subtitle':
            relative = source.relative_to(current.source_root) if source != current.source_root else Path(source.name)
            entry = replace(entry, target_path=root / 'ManualReview' / 'Subtitles' / relative,
                season_id=None, episode_id=None,
                presentation_asset=None, collision_resolution=None)
        projected.append(entry)
    context = InventoryCompletionContext(inputs.plan_fingerprint, current.presentation_groups[0].group_id,
        'movie_continuous', origin.work_identity.domain, target, inputs.roots.to_dict(), {})
    metadata = project_completion_metadata(current.metadata, context=context, work_root=root,
        naming_decision=naming, proposed_work_key=inputs.work_key,
        catalog_work_key=record['work_key'] if record else None, catalog_revision=record['revision'] if record else 0,
        catalog_match_source='exact_provider' if record else 'new_work', completion_mode='initial_movie_versions',
        unresolved_videos=mapping.unresolved)
    if not isinstance(metadata, dict):
        raise ValueError('首次电影完成上下文投影器已断开')
    metadata.pop('associated_asset_manifest', None)
    view = replace(current, entries=parse_subtitle_placeholders(tuple(projected)), metadata=metadata, presentation_groups=(group,),
        work_identity=work,
        execution_policy=replace(current.execution_policy, target_root=target), projection_receipt=None,
        revision_parent_fingerprint=inputs.plan_fingerprint, revision_action=revision_action, revision_reason=inputs.reason)
    associations = complete_subtitle_associations(view, old.associations)
    value['associations'] = associations.to_dict()
    view = replace(view, metadata={**view.metadata, 'content_selection': {**value}})
    view = project_subtitles(view, associations)
    from .movie_version_selection import replacement_from_catalog
    kept = [row for row in rows if not set(row['sources']) <= explicit]
    replacement = replacement_from_catalog(record, inputs.replace_version_ref, kept_groups=kept)
    full = create_selection(view, groups=rows, associations=associations, registered_versions=record['versions'] if record else (), previous=previous)
    if replacement is not None:
        replacement_group = create_selection(view, groups=kept, associations=associations, registered_versions=record['versions'] if record else (), previous=previous,
            replacement=replacement).to_dict()['groups'][0]
        full = MovieVersionSelection.from_mapping({**full.to_dict(), 'groups': [
            replacement_group if set(row['sources']) == set(kept[0]['sources']) else row
            for row in full.to_dict()['groups']]})
    view = project_audio(project_selection(view, full))
    full_value = full.to_dict()
    movie = MovieVersionSelection.from_mapping({**full_value,
        'groups': [row for row in full_value['groups'] if not set(row['sources']) <= explicit],
        'excluded_sources': sorted(version_excluded), 'replacement': replacement})
    if record is not None:
        validate_catalog_labels(movie, record['versions'])
    material_movie = (MovieVersionSelection.from_mapping(current.metadata['movie_version_selection'])
        if current.metadata.get('movie_version_selection') is not None else None)
    preserved = content.omitted_sources(old, material_movie)
    materials = {row['source_path']: row for row in value['materials'] if Path(row['source_path']) in preserved}
    materials.update({str(entry.source_path): content.source_material(entry) for entry in view.entries})
    value.update(materials=[materials[row['source_path']] for row in value['materials']],
        version_groups=full_value['groups'], evidence=json.loads(inputs.evidence_json),
        parent_plan_fingerprint=inputs.plan_fingerprint, reason=inputs.reason)
    choice = content.ContentSelection.from_mapping(value)
    candidate = replace(state, confirmed_work=work.to_dict(), layout_mode='movie_versions', content_selection=choice, movie_version_selection=movie,
        catalog_link_intent={'work_key': inputs.work_key, 'catalog_revision': record['revision'] if record else 0,
                             'affiliation': affiliation.to_dict()})
    view = replace(view, metadata={**view.metadata, 'content_selection': choice.to_dict(),
        'content_selection_fingerprint': choice.fingerprint},
        ignored_sources=tuple(sorted(content.omitted_sources(choice, movie), key=str)))
    return InitialMovieCompletion(candidate, DecisionStateStore.project_plan(candidate, view))


def content_fingerprint(plan):
    from .plan_store import PlanStore
    return PlanStore.fingerprint(plan)


def _targets_deferred(built):
    from .work_affiliation import pending_from_plan
    return (pending_from_plan(built.draft) is not None
        or (built.draft.revision_action == 'movie_domain_correction'
            and built.decision.movie_version_selection is None
            and built.draft.metadata.get('mapping_source') == 'inventory_review_draft'))


def build_initial_movie_completion(current, state, intent):
    """Pure typed reconstruction; no Provider, filesystem, or service callback."""
    if type(intent) is not InitialMovieCompletionIntent:
        raise ValueError('首次电影完成缺少冻结输入')
    built = _semantic(current, state, intent.inputs)
    if _targets_deferred(built):
        if intent.probes.rows or intent.root_probes.rows:
            raise ValueError('归属待确认期间不能提前生成目标操作')
        return built
    content.validate_omission_manifest(built.decision.content_selection,
        movie_selection=built.decision.movie_version_selection)
    candidate = built.decision
    draft = DecisionStateStore.project_plan(candidate, built.draft)
    result = project_collisions(draft.entries, Path(draft.metadata['canonical_work_root']), intent.probes,
                                package_label=current.source_root.name)
    if type(result) is not CollisionProjection:
        raise ValueError('首次完成碰撞投影已断开')
    draft = replace(draft, entries=result.entries,
        metadata={**draft.metadata, 'asset_collision_unresolved': result.unresolved})
    return InitialMovieCompletion(candidate, draft)


def _validate_current(service, current, state, *, mode='initial'):
    from .default_content import unfinished_source_edit
    from .initial_movie_selection import validate_successful_selection
    if mode == 'affiliation':
        from .default_content import _origin, require_unexecuted_task
        from ..utils.utils import read_task_at
        from .movie_version_selection import validate_selection
        _origin(service, current, state)
        require_unexecuted_task(read_task_at(service.task_root, current.task_id))
        if (state.recovery_state not in (None, 'healthy') or state.execution_failure is not None
                or state.completed_rebuild_observation is not None
                or state.asset_operation is not None and state.asset_operation.to_dict()['mode'] != 'append'):
            raise ValueError('已执行、换源或恢复中的电影不能由作品归属替换')
        from .work_affiliation import pending_from_plan
        if pending_from_plan(current) is None and validate_selection(current) != state.movie_version_selection:
            raise ValueError("电影版本决定与当前计划不一致")
        content.validate_plan_content(current, state)
    elif mode != 'initial' or not unfinished_source_edit(service, current, state):
        raise ValueError('首次电影完成需要未完成来源权威')
    if (current.work_identity is None
            or current.work_identity.status != 'confirmed'
            or (current.work_identity.primary_provider.namespace, current.work_identity.primary_provider.entity_type) not in {('tmdb', 'movie'), ('bangumi', 'subject')}
            or not state.confirmed_work or state.manual_role_decisions):
        raise ValueError('首次电影完成需要已确认电影及未完成来源权威')
    if mode == 'initial':
        validate_successful_selection(service, current, state)
    content.validate_source_identities(state.content_selection, plan=current)
    content.validate_omission_manifest(state.content_selection, movie_selection=state.movie_version_selection)


def _task_fingerprint(service, task_id):
    from ..utils.utils import task_record_snapshot_at
    _raw, _task, fingerprint = task_record_snapshot_at(service.task_root, task_id)
    return fingerprint


def prepare(service, task_id, fingerprint, *, groups, excluded_sources=(), reason, affiliation_choice=None,
            mode='initial', replace_version_ref=None):
    from .movie_content import _groups_input
    current, state = _load(service, task_id, fingerprint)
    _validate_current(service, current, state, mode=mode)
    context = InitialMovieCompletionContext.from_service(service)
    roots = context.roots.observe()
    previous = (state.catalog_link_intent or {}).get('affiliation')
    with _namespace(service, current, roots=roots, choice=affiliation_choice, previous=previous) as (key, namespace):
        inputs = CompletionInputs(task_id, fingerprint, DecisionStateStore.fingerprint(state),
            _task_fingerprint(service, task_id), key, namespace, roots, encode(_groups_input(groups)),
            tuple(sorted(str(Path(p)) for p in excluded_sources)), str(reason).strip(),
            encode(state.content_selection.to_dict()['evidence']), affiliation_choice=affiliation_choice,
            previous_affiliation=encode(previous) if previous else '', mode=mode,
            replace_version_ref=replace_version_ref)
    return _prepare_intent(service, current, state, inputs, context)


def _prepare_intent(service, current, state, inputs, context):
    first = _semantic(current, state, inputs)
    evidence = _evidence(first.decision.content_selection.to_dict(), first.decision.movie_version_selection,
                         old=state.content_selection)
    inputs = replace(inputs, evidence_json=encode(evidence))
    built = _semantic(current, state, inputs)
    if _targets_deferred(built):
        return InitialMovieCompletionIntent(inputs, context, CollisionProbes(()), CollisionProbes(()))
    content.validate_omission_manifest(built.decision.content_selection,
        movie_selection=built.decision.movie_version_selection)
    _target, root, _naming, _record, needs_absence, _affiliation = _routing(current, inputs)
    root_probe = observe_path(root)
    if needs_absence and root_probe.exists:
        raise ValueError('确定性同名目录仍存在，必须重新核对')
    probes, _result = observe_collisions(built.draft.entries, root, package_label=current.source_root.name)
    return InitialMovieCompletionIntent(inputs, context, probes, CollisionProbes((root_probe,)))


def _validate_observations(service, current, state, intent, built):
    intent.context.validate()
    if intent.context.service is not service or intent.context.roots.observe() != intent.inputs.roots:
        raise ValueError('首次电影完成服务或目标配置已变化，请重新预览')
    if intent.inputs.work_observation is None:
        _validate_current(service, current, state, mode=intent.inputs.mode)
    else:
        from .movie_work_correction import MovieWorkObservation
        if type(intent.inputs.work_observation) is not MovieWorkObservation:
            raise ValueError('电影更正缺少原观察')
        intent.inputs.work_observation.validate(service, current, state)
    if _task_fingerprint(service, current.task_id) != intent.inputs.task_fingerprint:
        raise ValueError('首次电影完成的实际任务或选择请求已变化')
    service.work_catalog.verify_namespace_snapshot(intent.inputs.work_key, intent.inputs.namespace)
    if _targets_deferred(built):
        if intent.probes.rows or intent.root_probes.rows:
            raise ValueError('归属待确认不能携带目标操作')
        return
    _target, root, _naming, _record, needs_absence, _affiliation = _routing(current, intent.inputs)
    if (type(intent.root_probes) is not CollisionProbes or len(intent.root_probes.rows) != 1
            or intent.root_probes.rows[0].path != root):
        raise ValueError('首次完成缺少实际目标目录的完整观察')
    verify_probes(intent.root_probes)
    if needs_absence and intent.root_probes.rows[0].exists:
        raise ValueError('确定性同名目录必须保持缺失，请重新核对')
    verify_probes(intent.probes)


def commit(service, intent):
    from .decision_state import DecisionCommandService
    if type(intent) is not InitialMovieCompletionIntent or intent.context.service is not service:
        raise ValueError('只能确认本服务准备的首次电影完成预览')
    intent.context.validate()
    current, state = _load(service, intent.task_id, intent.inputs.plan_fingerprint)
    built = build_initial_movie_completion(current, state, intent)
    if type(built) is not InitialMovieCompletion:
        raise ValueError('首次电影共享重建器已断开')
    fields = ('confirmed_work', 'layout_mode', 'content_selection', 'movie_version_selection', 'catalog_link_intent')
    if intent.inputs.work_observation is not None:
        fields += ('confirmed_work', 'provider_evidence', 'linked_provider_identities', 'asset_operation', 'library_domain')
    command = DecisionCommandService(service.decision_state_store, service.plan_store,
                                     initial_movie_completion_context=intent.context)
    _, projected = command.update(intent.task_id, expected_revision=state.revision,
        changes={name: getattr(built.decision, name) for name in fields}, planner=lambda _: built.draft,
        initial_movie_completion=intent, task_terminal=service._revision_task_terminal(intent.task_id))
    return service._record_revision(projected)


def validate_commit(transaction, envelope):
    from .projection_authority import TaskTerminalMutation
    context, intent = transaction.initial_movie_completion_context, envelope.initial_movie_completion
    if type(context) is not InitialMovieCompletionContext or type(intent) is not InitialMovieCompletionIntent:
        raise ValueError('首次电影完成缺少类型化意图或配置上下文')
    context.validate(transaction)
    if intent.context != context:
        raise ValueError('首次电影完成预览来自其它服务')
    current, state = _load(context.service, intent.task_id, intent.inputs.plan_fingerprint)
    expected = build_initial_movie_completion(current, state, intent)
    if type(expected) is not InitialMovieCompletion:
        raise ValueError('首次电影共享重建器已断开')
    _validate_observations(context.service, current, state, intent, expected)
    terminal = transaction.initial_movie_completion_terminal
    if (type(terminal) is not TaskTerminalMutation or terminal.task_id != intent.task_id
            or terminal.expected_sha256 != intent.inputs.task_fingerprint):
        raise ValueError('首次电影完成任务CAS不是预览时的实际任务')
    if (type(expected) is not InitialMovieCompletion or state != envelope.previous_decision
            or expected.decision != envelope.decision or expected.draft != envelope.draft_plan):
        raise ValueError('首次电影完成实际决定或完整Plan偏离冻结重建')
    return expected
