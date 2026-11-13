"""One local work choice over the existing, locked Catalog observation.

No search, scanning or publication lives here. Titles recommend; a confirmed
choice or an exact registered Provider identity owns the local work key.
"""
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
from collections.abc import Mapping

from .catalog_namespace import NamespaceSnapshot
from .content_associations import encode
from .target_routing import resolve_target_root

POLICY = 'work-affiliation-v1'


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def identity(value):
    value = value.to_dict() if hasattr(value, 'to_dict') else value
    if not isinstance(value, Mapping):
        raise ValueError('作品归属缺少资料条目身份')
    row = tuple(value.get(k) for k in ('namespace', 'entity_type', 'provider_id'))
    if (row[:2] not in {('bangumi', 'subject'), ('tmdb', 'tv'), ('tmdb', 'movie')}
            or any(not isinstance(s, str) or not s or s.strip() != s for s in row)
            or value.get('status', 'confirmed') != 'confirmed'):
        raise ValueError('作品归属资料条目身份无效')
    return row


def members(record, backend):
    if backend == 'series':
        from .series_catalog import SeriesCatalog
        rows = SeriesCatalog._namespace_providers(record)
    elif backend == 'work':
        rows = record['providers']
    else:
        raise ValueError('作品登记类型无效')
    return tuple(sorted({identity(row) for row in rows}))


def compatible(record, backend, work, roots):
    """Shared domain/form/root gate, also used by explicit manual choices."""
    kind = 'tv_episodes' if backend == 'series' else 'movie_continuous'
    provider = identity(work.primary_provider)
    if (work.status != 'confirmed' or work.domain not in {'general', 'animation'}
            or provider[:2] == ('tmdb', 'movie') and kind != 'movie_continuous'
            or provider[:2] == ('tmdb', 'tv') and kind != 'tv_episodes'
            or record.get('domain') != work.domain
            or backend == 'work' and record.get('group_kind') != kind):
        raise ValueError('所选登记的媒体库或观看形式与本次作品不兼容')
    root = Path(record['canonical_root'])
    target = resolve_target_root(work.domain, kind, roots)
    if not root.is_absolute() or root == target or not root.is_relative_to(target):
        raise ValueError('所选作品登记根不属于当前冻结媒体库')
    members(record, backend)
    return record


@dataclass(frozen=True)
class AffiliationChoice:
    action: str
    key: str
    namespace_fingerprint: str
    reason: str

    def __post_init__(self):
        if (self.action not in {'existing', 'separate'} or not self.reason.strip()
                or re.fullmatch('[0-9a-f]{64}', self.namespace_fingerprint) is None
                or (self.action == 'existing') != bool(self.key)):
            raise ValueError('作品归属选择缺少当前登记观察或明确决定')


@dataclass(frozen=True)
class WorkAffiliation:
    canonical_json: str

    def __post_init__(self):
        value = json.loads(self.canonical_json)
        fields = {'schema_version', 'policy_version', 'action', 'source', 'backend', 'key',
            'domain', 'group_kind', 'provider', 'work_members', 'canonical_root', 'title', 'year',
            'catalog_revision', 'record_fingerprint', 'origin', 'reason'}
        if (type(value) is not dict or set(value) != fields or encode(value) != self.canonical_json
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['policy_version'] != POLICY or value['action'] not in {'existing', 'separate'}
                or value['source'] not in {'explicit_user', 'catalog_exact', 'new_work', 'confirmed_choice'}
                or value['backend'] not in {'series', 'work'} or value['domain'] not in {'general', 'animation'}
                or value['group_kind'] != ('tv_episodes' if value['backend'] == 'series' else 'movie_continuous')
                or not isinstance(value['title'], str) or not value['title'].strip()
                or value['year'] is not None and (type(value['year']) is not int or value['year'] < 1)
                or not isinstance(value['reason'], str) or not value['reason'].strip()
                or not Path(value['canonical_root']).is_absolute()
                or type(value['catalog_revision']) is not int or value['catalog_revision'] < 0):
            raise ValueError('作品归属决定的字段或版本无效')
        pattern = r'series-[0-9a-fA-F-]{36}' if value['backend'] == 'series' else r'work-[0-9a-fA-F-]{32,36}'
        if re.fullmatch(pattern, value['key']) is None:
            raise ValueError('作品归属键无效')
        rows = value['work_members']
        if (not isinstance(rows, list) or not rows
                or any(not isinstance(row, list) or len(row) != 3
                       or any(not isinstance(s, str) for s in row) for row in rows)
                or rows != sorted(rows)
                or len({tuple(row) for row in rows}) != len(rows)):
            raise ValueError('作品归属成员不完整或重复')
        for row in rows:
            if not isinstance(row, list) or len(row) != 3:
                raise ValueError('作品归属成员无效')
            identity(dict(zip(('namespace', 'entity_type', 'provider_id'), row)))
        if list(identity(value['provider'])) not in rows:
            raise ValueError('本次资料条目不属于已确认作品')
        frozen = value['record_fingerprint']
        if (value['action'] == 'existing') != (value['catalog_revision'] > 0):
            raise ValueError('作品归属登记版本无效')
        if ((value['action'] == 'existing' and (not isinstance(frozen, str) or re.fullmatch('[0-9a-f]{64}', frozen) is None))
                or value['action'] == 'separate' and frozen is not None):
            raise ValueError('作品归属缺少原登记完整摘要')
        origin = value['origin']
        if (not isinstance(origin, dict) or set(origin) != {'task_id', 'plan', 'decision', 'task'}
                or not isinstance(origin['task_id'], str) or not origin['task_id']
                or any(not isinstance(origin[k], str) or re.fullmatch('[0-9a-f]{64}', origin[k]) is None
                       for k in ('plan', 'decision', 'task'))):
            raise ValueError('作品归属缺少原任务、计划或决定绑定')

    @classmethod
    def from_mapping(cls, value):
        return cls(encode(value))

    def to_dict(self):
        return json.loads(self.canonical_json)

    def work(self, selected):
        value = self.to_dict()
        if identity(selected.primary_provider) != identity(value['provider']) or selected.domain != value['domain']:
            raise ValueError('作品归属不属于本次所选条目')
        return replace(selected, title=value['title'], year=value['year'])


def records(namespace):
    if type(namespace) is not NamespaceSnapshot:
        raise ValueError('作品归属缺少完整登记观察')
    return tuple(json.loads(raw) for _name, raw in namespace.records)


def namespace_fingerprint(namespace):
    return digest(dict(backend=namespace.backend, records=[(name, hashlib.sha256(raw).hexdigest())
        for name, raw in namespace.records]))


def record_key(record, backend):
    return record['series_key' if backend == 'series' else 'work_key']


def resolve_record(work, namespace, roots, *, choice=None, previous=None, resolution=None, provider_namespace=None):
    """Resolve a chosen key before paths; recommendations never choose it."""
    backend = namespace.backend
    rows = records(namespace)
    provider = identity(work.primary_provider)
    exact = [row for row in rows if provider in members(row, backend)]
    if resolution is not None or provider_namespace is not None:
        from .provider_link_resolver import ProviderLinkResolution
        from .tv_completion_observations import ProviderCatalogSnapshot
        from .provider_link_catalog import match_mapping_provider
        if (backend != 'series' or type(resolution) is not ProviderLinkResolution
                or type(provider_namespace) is not ProviderCatalogSnapshot):
            raise ValueError('跨库归属缺少同一TV资料与登记观察')
        if resolution.anchor_mapping_id:
            anchors = [m for m in provider_namespace.mappings() if m.mapping_id == resolution.anchor_mapping_id]
            if (len(anchors) != 1 or not anchors[0].reusable_offline
                    or anchors[0].revision != resolution.anchor_revision
                    or anchors[0].evidence_fingerprint != resolution.anchor_fingerprint
                    or anchors[0].local_series_key != (resolution.series_key or resolution.proposed_series_key)
                    or match_mapping_provider(anchors[0], work.primary_provider.to_dict()) is None):
                raise ValueError('跨库已确认锚点已变化或不属于所选条目')
            linked = [row for row in rows if row['series_key'] == anchors[0].local_series_key]
            if len(linked) != 1 or any(row['series_key'] != linked[0]['series_key'] for row in exact):
                raise ValueError('跨库已确认作品登记缺失或与条目身份冲突')
            exact = linked
    if len(exact) > 1:
        raise ValueError('同一资料条目指向多个作品登记，请先核查分裂记录')
    selected, source, reason = (exact[0] if exact else None), 'catalog_exact', '沿用已登记的资料条目归属'
    if choice is not None:
        if type(choice) is not AffiliationChoice or choice.namespace_fingerprint != namespace_fingerprint(namespace):
            raise ValueError('作品归属选择的登记观察已变化，请重新确认')
        if choice.action == 'separate':
            if exact:
                raise ValueError('该条目已有明确作品登记，不能再创建另一作品')
            return None, 'explicit_user', choice.reason
        matches = [row for row in rows if record_key(row, backend) == choice.key]
        if len(matches) != 1 or exact and record_key(exact[0], backend) != choice.key:
            raise ValueError('所选作品不存在或与已登记条目身份冲突')
        selected, source, reason = matches[0], 'explicit_user', choice.reason
    elif previous is not None:
        old = WorkAffiliation.from_mapping(previous).to_dict()
        if old['backend'] == backend and identity(old['provider']) == provider:
            matches = [row for row in rows if record_key(row, backend) == old['key']]
            if old['action'] == 'existing' and len(matches) != 1:
                raise ValueError('已确认的作品登记已缺失，请重新核查')
            if exact and record_key(exact[0], backend) != old['key']:
                raise ValueError('已确认作品与当前条目登记冲突')
            selected, source, reason = (matches[0] if matches else None), 'confirmed_choice', old['reason']
    if selected is not None:
        compatible(selected, backend, work, roots)
    if selected is None and source == 'catalog_exact':
        source, reason = 'new_work', '未发现已登记的条目身份，首次确认本地作品'
    return selected, source, reason


def freeze(work, namespace, roots, *, key, root, origin, choice=None, previous=None, additional_members=(),
           resolution=None, provider_namespace=None):
    record, source, reason = resolve_record(work, namespace, roots, choice=choice, previous=previous,
        resolution=resolution, provider_namespace=provider_namespace)
    backend = namespace.backend
    selected = identity(work.primary_provider)
    all_members = {selected, *additional_members}
    if record is not None:
        all_members.update(members(record, backend))
        if key != record_key(record, backend) or Path(root) != Path(record['canonical_root']):
            raise ValueError('作品路径或键偏离已确认登记')
    year = work.year
    if record is not None:
        if backend == 'work':
            year = record['year']
        else:
            from .series_registration import SeriesRegistrationAuthority
            registered = record.get('series_registration_authority')
            proof = SeriesRegistrationAuthority.from_mapping(registered) if registered is not None else None
            year = (json.loads(proof.affiliation)['year'] if proof is not None and proof.affiliation
                    else record['provider_evidence'][0].get('year'))
    return WorkAffiliation.from_mapping(dict(schema_version=1, policy_version=POLICY,
        action='existing' if record else 'separate', source=source, backend=backend, key=key,
        domain=work.domain, group_kind='tv_episodes' if backend == 'series' else 'movie_continuous',
        provider=dict(zip(('namespace', 'entity_type', 'provider_id'), selected)),
        work_members=[list(row) for row in sorted(all_members)], canonical_root=str(root),
        title=record['canonical_display_name' if backend == 'series' else 'title'] if record else work.title,
        year=year,
        catalog_revision=record['revision'] if record else 0,
        record_fingerprint=digest(record) if record else None, origin=origin, reason=reason))


def from_plan(plan):
    raw = plan.metadata.get('work_affiliation')
    if raw is None:
        receipt = plan.projection_receipt
        if (receipt is not None and receipt.authority.get('work_affiliation_fingerprint') is not None
                and plan.metadata.get('pending_work_affiliation') is None):
            raise ValueError('当前计划缺少回执已冻结的作品归属')
        return None
    value = WorkAffiliation.from_mapping(raw)
    body = value.to_dict()
    from .catalog_binding import binding_from_plan
    binding = binding_from_plan(plan)
    key_fields = ('series_key',) if body['backend'] == 'series' else ('proposed_work_key', 'catalog_work_key')
    revision_field = 'series_catalog_revision' if body['backend'] == 'series' else 'catalog_revision'
    if (binding is None or binding.backend != body['backend'] or binding.key != body['key']
            or binding.revision != body['catalog_revision']
            or any(plan.metadata.get(k) is not None and plan.metadata[k] != body['key'] for k in key_fields)
            or plan.metadata.get(revision_field) != body['catalog_revision']
            or body['canonical_root'] != plan.metadata.get('canonical_work_root')
            or value.work(plan.work_identity) != plan.work_identity
            or body['group_kind'] not in {g.kind for g in plan.presentation_groups}):
        raise ValueError('计划与已确认作品归属不一致')
    return value


def refresh_registered(value, record):
    """Refresh the same registered decision after a validated completion."""
    bound = WorkAffiliation.from_mapping(value).to_dict()
    if (record_key(record, bound['backend']) != bound['key']
            or record['canonical_root'] != bound['canonical_root']
            or identity(bound['provider']) not in members(record, bound['backend'])
            or not set(map(tuple, bound['work_members'])) <= set(members(record, bound['backend']))):
        raise ValueError('登记复核不能更换已确认的作品或目录')
    bound.update(action='existing', source='confirmed_choice', catalog_revision=record['revision'],
                 record_fingerprint=digest(record))
    return WorkAffiliation.from_mapping(bound).to_dict()


def recommendations(work, namespace, roots, *, related=()):
    """Reuse Catalog's title comparison only as a recommendation, never identity."""
    from .work_catalog import _canonical_title, _titles_related
    names = {_canonical_title(s) for s in (work.title, work.primary_provider.original_title,
             *work.primary_provider.aliases) if s}
    found = []
    for row in records(namespace):
        try:
            compatible(row, namespace.backend, work, roots)
        except ValueError:
            continue
        candidates = [row.get('title'), row.get('canonical_display_name'), *row.get('aliases', ())]
        for provider in (*row.get('providers', ()), *row.get('provider_evidence', ())):
            candidates.extend((provider.get('title'), provider.get('original_title')))
            candidates.extend(provider.get('aliases', ()))
        if (set(related).intersection(members(row, namespace.backend))
                or any(_titles_related(name, _canonical_title(other))
                       for name in names for other in candidates if isinstance(other, str))):
            found.append(dict(key=record_key(row, namespace.backend), title=row.get('title') or row['canonical_display_name'],
                root=row['canonical_root'], revision=row['revision'], fingerprint=digest(row)))
    return tuple(found)


@dataclass(frozen=True)
class PendingAffiliation:
    """Frozen reference for an unresolved choice, with no output root or key."""
    canonical_json: str

    def __post_init__(self):
        from .rename_plan import WorkIdentity
        from .target_routing import TargetRootSnapshot
        value = json.loads(self.canonical_json)
        fields = {'schema_version', 'policy_version', 'backend', 'work', 'origin', 'roots',
                  'namespace_fingerprint', 'recommendations', 'completion'}
        if (not isinstance(value, dict) or set(value) != fields or encode(value) != self.canonical_json
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['policy_version'] != POLICY or value['backend'] not in {'work', 'series'}):
            raise ValueError('待确认作品归属格式无效')
        work = WorkIdentity.from_dict(value['work'])
        identity(work.primary_provider)
        roots = TargetRootSnapshot.from_dict(value['roots'])
        resolve_target_root(work.domain, 'tv_episodes' if value['backend'] == 'series' else 'movie_continuous', roots)
        origin = value['origin']
        if (not isinstance(origin, dict) or set(origin) != {'task_id', 'plan', 'decision', 'task'}
                or not isinstance(origin['task_id'], str) or not origin['task_id']
                or any(not isinstance(origin[k], str) or re.fullmatch('[0-9a-f]{64}', origin[k]) is None
                       for k in ('plan', 'decision', 'task'))
                or work.status != 'confirmed'
                or not isinstance(value['namespace_fingerprint'], str)
                or re.fullmatch('[0-9a-f]{64}', value['namespace_fingerprint']) is None
                or not isinstance(value['recommendations'], list) or not value['recommendations']):
            raise ValueError('待确认归属缺少原计划与登记观察')
        for row in value['recommendations']:
            if (not isinstance(row, dict) or set(row) != {'key', 'title', 'root', 'revision', 'fingerprint'}
                    or any(not isinstance(row[k], str) or not row[k] for k in ('key', 'title', 'root', 'fingerprint'))
                    or type(row['revision']) is not int or row['revision'] < 1
                    or re.fullmatch('[0-9a-f]{64}', row['fingerprint']) is None):
                raise ValueError('待确认作品建议缺少完整登记依据')
        completion = value['completion']
        if not isinstance(completion, dict) or set(completion) != (
                {'resolution', 'seasons', 'special'} if value['backend'] == 'series' else {'groups', 'excluded'}):
            raise ValueError('待确认作品缺少完整来源整理选择')

    @classmethod
    def from_mapping(cls, value):
        return cls(encode(value))

    def to_dict(self):
        return json.loads(self.canonical_json)


def pending_for(work, inputs, completion):
    if inputs.affiliation_choice is not None or inputs.previous_affiliation:
        return None
    frozen = getattr(inputs, 'resolution', None)
    resolution = frozen.resolution() if frozen is not None else None
    record, _, _ = resolve_record(work, inputs.namespace, inputs.roots, resolution=resolution,
        provider_namespace=inputs.provider_namespace if resolution is not None else None)
    if record is not None:
        return None
    related = ()
    if frozen is not None:
        related = tuple(('bangumi', 'subject', s) for s, _ in resolution.season_assignments)
        if resolution.tmdb_tv_id:
            related += (('tmdb', 'tv', str(resolution.tmdb_tv_id)),)
    rows = recommendations(work, inputs.namespace, inputs.roots, related=related)
    if not rows:
        return None
    return PendingAffiliation.from_mapping(dict(schema_version=1, policy_version=POLICY,
        backend=inputs.namespace.backend, work=work.to_dict(),
        origin=dict(task_id=inputs.task_id, plan=inputs.plan_fingerprint, decision=inputs.decision_fingerprint,
                    task=inputs.task_fingerprint), roots=inputs.roots.to_dict(),
        namespace_fingerprint=namespace_fingerprint(inputs.namespace), recommendations=list(rows), completion=completion))


def pending_from_plan(plan):
    raw = plan.metadata.get('pending_work_affiliation')
    if raw is None:
        return None
    pending = PendingAffiliation.from_mapping(raw)
    value = pending.to_dict()
    if (value['origin']['task_id'] != plan.task_id or value['work'] != plan.work_identity.to_dict()
            or plan.metadata.get('mapping_source') != 'inventory_review_draft'
            or plan.metadata.get('work_affiliation') is not None):
        raise ValueError('待确认归属与来源库存不一致')
    return pending


def authority_fingerprint(plan):
    """Bind both completed and pending choices to the existing projection receipt."""
    value = from_plan(plan)
    pending = pending_from_plan(plan)
    return digest((value or pending).to_dict()) if value or pending else None


class AffiliationRequired(ValueError):
    """A read-only work-change preview needs the same explicit Catalog choice."""
    def __init__(self, pending, inputs):
        super().__init__('发现可能相同的已登记作品，请确认归属；原计划未改变')
        self.pending = pending
        self.inputs = inputs


def retain_inventory(current, candidate, pending, inputs=None):
    observation = getattr(getattr(inputs, 'work_request', None), 'observation', None) or getattr(inputs, 'work_observation', None)
    queued = getattr(observation, 'queue_request', None) is not None
    if (getattr(inputs, 'mode', None) == 'boundary'
            or current.metadata.get('mapping_source') != 'inventory_review_draft' and not queued):
        # A cancelled work-change preview must not replace an existing approved plan.
        raise AffiliationRequired(pending, inputs)
    value = pending.to_dict()
    binding = {'pending_affiliation': value}
    if value['backend'] == 'work' and candidate.content_selection.to_dict()['version_groups']:
        from .content_selection import ContentSelection
        content = candidate.content_selection.to_dict()
        # 版本组保存在既有pending completion中；当前库存不携带旧作品版本权威。
        content['version_groups'] = []
        content['parent_plan_fingerprint'] = value['origin']['plan']
        content['excluded_sources'] = sorted(set(content['excluded_sources']) | set(value['completion']['excluded']))
        candidate = replace(candidate, content_selection=ContentSelection.from_mapping(content))
    candidate = replace(candidate, layout_mode=None, movie_version_selection=None,
        tv_planning_authority=None, asset_operation=None,
        series_binding=binding if value['backend'] == 'series' else None,
        catalog_link_intent=binding if value['backend'] == 'work' else None,
        provider_link_binding=None, source_episode_projection=None, source_episode_projection_decision=None)
    metadata = dict(current.metadata)
    metadata['mapping_source'] = 'inventory_review_draft'
    metadata.pop('movie_version_selection', None)
    for key in ('canonical_work_root', 'canonical_naming', 'series_key', 'series_catalog_revision',
                'catalog_work_key', 'proposed_work_key', 'catalog_revision',
                'associated_asset_manifest', 'asset_operation'):
        metadata.pop(key, None)
    if value['backend'] == 'series':
        metadata.update(tv_completion_resolution=value['completion']['resolution'],
            tv_completion_seasons=value['completion']['seasons'], tv_completion_special=value['completion']['special'])
    from .decision_state import DecisionStateStore
    draft = replace(current, metadata=metadata, work_identity=None, projection_receipt=None,
        revision_action='work_affiliation_pending', revision_parent_fingerprint=value['origin']['plan'],
        revision_reason='已有可能相同的作品，请确认归属后生成目标路径')
    return candidate, DecisionStateStore.project_plan(candidate, draft)


def prepare_existing(service, task_id, fingerprint, *, key, reason, separate=False,
                     groups=None, excluded_sources=None, replace_version_ref=None):
    """Read once for the explicit choice; full completers freeze and recheck it."""
    from .content_choice import _load
    from .initial_tv_completion import InitialTVCompletionContext, _namespace as tv_namespace, prepare as tv_prepare
    from .initial_movie_completion import _namespace as movie_namespace, prepare as movie_prepare
    from .tv_completion_observations import FrozenTVResolution, TVSeasonObservation
    from .projection_authority import RecognitionDecisionInput
    from .rename_plan import WorkIdentity, ProviderIdentity
    current, state = _load(service, task_id, fingerprint)
    kind = {g.kind for g in current.presentation_groups}
    if service.root_snapshot_source is None:
        raise ValueError('作品归属缺少当前媒体库配置观察，请重新生成计划')
    roots = service.root_snapshot_source.observe()
    pending = pending_from_plan(current)
    action = 'separate' if separate else 'existing'
    if separate and replace_version_ref is not None:
        raise ValueError('作为另一部作品整理不能同时替换已有版本')
    if kind == {'tv_episodes'}:
        if groups is not None or excluded_sources is not None or replace_version_ref is not None:
            raise ValueError('剧集归属不能携带电影版本选择')
        if 'tv_completion_resolution' not in current.metadata:
            raise ValueError('旧剧集计划缺少完整观察，请重新生成')
        recognition = RecognitionDecisionInput(task_id, WorkIdentity.from_dict(state.confirmed_work),
            tuple(ProviderIdentity.from_dict(row) for row in state.linked_provider_identities),
            (state.season_binding or {}).get('season_number'),
            (state.season_binding or {}).get('decision_source'), 'tv_episodes',
            service.decision_state_store.fingerprint(state))
        raw = current.metadata['tv_completion_resolution']
        frozen = FrozenTVResolution(encode(raw)) if raw is not None else None
        context = InitialTVCompletionContext.from_service(service)
        with tv_namespace(context, recognition, frozen, roots=roots) as (_, namespace, provider):
            choice = AffiliationChoice(action, key, namespace_fingerprint(namespace), reason)
            resolve_record(current.work_identity, namespace, roots, choice=choice,
                resolution=frozen.resolution() if frozen else None,
                provider_namespace=provider if frozen else None)
        return tv_prepare(service, recognition, resolution=frozen.resolution() if frozen else None,
            seasons=TVSeasonObservation.from_mapping(current.to_dict()['metadata']['tv_completion_seasons']),
            special=current.to_dict()['metadata'].get('tv_completion_special'),
            mode='affiliation', edit={}, affiliation_choice=choice)
    first = state.movie_version_selection is None and pending is None and groups is not None
    if kind != {'movie_continuous'} or (state.movie_version_selection is None and pending is None and not first):
        raise ValueError('作品归属需要完整电影版本或剧集内容决定')
    with movie_namespace(service, current, roots=roots) as (_, namespace):
        choice = AffiliationChoice(action, key, namespace_fingerprint(namespace), reason)
        resolve_record(current.work_identity, namespace, roots, choice=choice)
    from .movie_work_correction import rows_for
    rows = groups if groups is not None else pending.to_dict()['completion']['groups'] if pending else rows_for(state)
    excluded = (excluded_sources if excluded_sources is not None else
        pending.to_dict()['completion']['excluded'] if pending else
        state.movie_version_selection.to_dict()['excluded_sources'] if state.movie_version_selection else ())
    return movie_prepare(service, task_id, fingerprint, groups=rows,
        excluded_sources=excluded, replace_version_ref=replace_version_ref,
        reason=reason, affiliation_choice=choice, mode='initial' if first else 'affiliation')


def commit_choice(service, intent):
    from .initial_tv_completion import InitialTVCompletionIntent, commit as tv_commit
    from .initial_movie_completion import InitialMovieCompletionIntent, commit as movie_commit
    if type(intent) is InitialTVCompletionIntent:
        if intent.inputs.mode == 'boundary':
            from .tv_boundary_completion import commit
            return commit(service, intent)
        _, projected = tv_commit(service, intent, task_terminal=service._revision_task_terminal(intent.task_id))
        return service._record_revision(projected)
    if type(intent) is InitialMovieCompletionIntent:
        return movie_commit(service, intent)
    raise ValueError('作品归属缺少本服务的完整冻结预览')


def catalog_choices(service, task_id, fingerprint, *, correction_inputs=None):
    """One bounded read of BAR registrations, only when the user opens the chooser."""
    from .content_choice import _load
    from .catalog_binding import binding_from_plan
    from .work_intent import derive_work_intent_key
    from .series_registration import build_series_registration_authority
    current, state = _load(service, task_id, fingerprint)
    work = (_correction_work(service, current, state, correction_inputs)
            if correction_inputs is not None else current.work_identity)
    kind = {g.kind for g in current.presentation_groups}
    if work is None or work.status != 'confirmed' or kind not in ({'tv_episodes'}, {'movie_continuous'}):
        raise ValueError('请先选择作品和观看形式')
    if service.root_snapshot_source is None:
        raise ValueError('缺少当前媒体库配置，请重新生成计划')
    roots = service.root_snapshot_source.observe()
    backend = 'series' if kind == {'tv_episodes'} else 'work'
    binding = binding_from_plan(current)
    from .initial_tv_completion import TVCompletionInputs
    if backend == 'series' and type(correction_inputs) is TVCompletionInputs:
        # A boundary preview has no confirmed season yet; use its validated observation.
        key = correction_inputs.series_key
    else:
        key = (binding.key if binding is not None else
               build_series_registration_authority(work, manual_season=(state.season_binding or {}).get('season_number')).series_key
               if backend == 'series' else derive_work_intent_key(primary_provider=work.primary_provider, group_kind='movie_continuous'))
    catalog = service.series_catalog if backend == 'series' else service.work_catalog
    with catalog.lock(key, read_only=True):
        snapshot = catalog.namespace_snapshot(key)
        result = []
        for row in records(snapshot):
            try:
                compatible(row, backend, work, roots)
            except ValueError:
                continue
            result.append(dict(key=record_key(row, backend), title=row.get('title') or row['canonical_display_name'],
                root=row['canonical_root']))
        return result


def preview_choice(service, task_id, fingerprint, *, key='', separate=False):
    intent = prepare_existing(service, task_id, fingerprint, key=key, separate=separate,
        reason='人工确认作为另一部作品整理' if separate else '人工确认资料条目属于所选已登记作品')
    from .content_choice import _load
    from .initial_tv_completion import InitialTVCompletionIntent, build_initial_tv_completion
    from .initial_movie_completion import build_initial_movie_completion
    current, state = _load(service, task_id, fingerprint)
    built = (build_initial_tv_completion(current, state, intent) if type(intent) is InitialTVCompletionIntent
             else build_initial_movie_completion(current, state, intent))
    return intent, built.draft


def _correction_work(service, current, state, inputs):
    """Only the service-issued original observation may reopen a correction."""
    from .initial_tv_completion import TVCompletionInputs
    from .initial_movie_completion import CompletionInputs
    if type(inputs) is TVCompletionInputs and inputs.mode == 'boundary':
        from .tv_boundary_completion import BoundaryResolution, validate_inputs
        with service.series_catalog.lock(inputs.series_key, read_only=True), service.provider_link_catalog.lock():
            resolved = validate_inputs(service, current, state, inputs)
        if type(resolved) is not BoundaryResolution:
            raise ValueError('季度确认的原请求验证已断开')
        return resolved.recognition.work_identity
    if type(inputs) is TVCompletionInputs and inputs.mode == 'work':
        from .work_correction import validate_inputs
        validate_inputs(service, current, state, inputs)
        return inputs.recognition.work_identity
    if type(inputs) is CompletionInputs and inputs.work_observation is not None:
        from .movie_work_correction import MovieWorkObservation
        if type(inputs.work_observation) is MovieWorkObservation:
            return inputs.work_observation.validate(service, current, state)
    raise ValueError('作品更正缺少本服务的实际资料观察，请重新预览')


def preview_correction_choice(service, inputs, *, key='', separate=False):
    from .content_choice import _load
    from .initial_tv_completion import TVCompletionInputs, InitialTVCompletionContext, _namespace as tv_namespace
    from .initial_movie_completion import _namespace as movie_namespace
    current, state = _load(service, inputs.task_id, inputs.plan_fingerprint)
    work = _correction_work(service, current, state, inputs)
    reason = '人工确认作为另一部作品整理' if separate else '人工确认更正条目属于所选已登记作品'
    context = (tv_namespace(InitialTVCompletionContext.from_service(service), inputs.recognition,
                           inputs.resolution, roots=inputs.roots) if type(inputs) is TVCompletionInputs else
               movie_namespace(service, replace(current, work_identity=work), roots=inputs.roots))
    with context as values:
        if type(inputs) is TVCompletionInputs and inputs.mode == 'boundary' and (
                values[1] != inputs.namespace or values[2] != inputs.provider_namespace):
            raise ValueError('季度确认的登记观察已变化，请重新预览')
        choice = AffiliationChoice('separate' if separate else 'existing', key,
                                   namespace_fingerprint(values[1]), reason)
        frozen = inputs.resolution if type(inputs) is TVCompletionInputs else None
        resolve_record(work, values[1], inputs.roots, choice=choice,
            resolution=frozen.resolution() if frozen else None,
            provider_namespace=values[2] if frozen else None)
    if type(inputs) is TVCompletionInputs:
        from .initial_tv_completion import build_initial_tv_completion
        if inputs.mode == 'boundary':
            from .tv_boundary_completion import prepare
            intent = prepare(service, inputs.task_id, inputs.plan_fingerprint,
                review_command=json.loads(inputs.edit_json), affiliation_choice=choice)
        else:
            from .work_correction import prepare
            request = inputs.work_request
            intent = prepare(service, request.observation, season_number=request.season_number,
                             reason=reason, affiliation_choice=choice)
        built = build_initial_tv_completion(current, state, intent)
    else:
        from .movie_work_correction import prepare_observed
        from .initial_movie_completion import build_initial_movie_completion
        intent = prepare_observed(service, inputs.work_observation, affiliation_choice=choice)
        built = build_initial_movie_completion(current, state, intent)
    return intent, built.draft
