"""Versioned series identity; observations and display names are not keys."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
from pathlib import Path
from typing import Mapping
import uuid

POLICY = 'series-registration-v2'
MANUAL_SUBJECT_POLICY = 'series-registration-manual-subject-v1'
MANUAL_GENERAL_POLICY = 'series-registration-manual-general-v1'
MANUAL_POLICIES = frozenset({MANUAL_SUBJECT_POLICY, MANUAL_GENERAL_POLICY})
AFFILIATION_POLICY = 'series-registration-affiliation-v1'
RELOCATION_POLICY = 'series-registration-relocated-v1'

def registration_domain(proof):
    if proof.affiliation:
        return json.loads(proof.affiliation)['domain']
    return 'general' if proof.policy_version == MANUAL_GENERAL_POLICY or proof.namespace == 'tmdb' else 'animation'


class CatalogStateChanged(ValueError):
    """Known durable catalog drift requires a new unapproved plan."""
    code = 'catalog_changed_requires_replan'


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(_plain(value), sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class SeriesRegistrationAuthority:
    namespace: str
    anchor_provider_id: str
    season_membership: tuple[tuple[str, int], ...]
    series_key: str
    materialized_mapping: str = ''
    policy_version: str = POLICY
    affiliation: str = ''
    relocation: str = ''

    def to_dict(self):
        if self.relocation:
            payload = dict(schema_version=3, policy_version=RELOCATION_POLICY,
                original_registration=replace(self, relocation='').to_dict(),
                relocation=json.loads(self.relocation))
            return dict(payload, fingerprint=_digest(payload))
        payload = dict(schema_version=1, policy_version=self.policy_version,
            namespace=self.namespace, anchor_provider_id=self.anchor_provider_id,
            season_membership=[list(row) for row in self.season_membership],
            series_key=self.series_key,
            materialized_mapping=json.loads(self.materialized_mapping) if self.materialized_mapping else None)
        if self.affiliation:
            payload.update(schema_version=2, affiliation=json.loads(self.affiliation))
        return dict(payload, fingerprint=_digest(payload))

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping):
            raise ValueError('系列登记权威缺失，请重新选择作品并生成未批准计划')
        value = _plain(value)
        if value.get('schema_version') == 3:
            return _relocated_from_mapping(value)
        if value.get('schema_version') == 2:
            return _affiliated_from_mapping(value)
        try:
            rows = tuple((row[0], row[1]) for row in value['season_membership'] if len(row) == 2)
            if (value['schema_version'] != 1 or type(value['schema_version']) is not int
                or value['policy_version'] not in {POLICY, 'series-registration-v1', *MANUAL_POLICIES} or value['namespace'] not in {'bangumi', 'tmdb'}
                or not rows or len(rows) != len(value['season_membership'])
                or len({row[0] for row in rows}) != len(rows)
                or any(not isinstance(a, str) or not a or a.strip() != a or type(b) is not int or b < (0 if value['namespace'] == 'tmdb' or value['policy_version'] in MANUAL_POLICIES else 1) for a, b in rows)
                or (value['policy_version'] in MANUAL_POLICIES and (value['namespace'] != 'bangumi' or len(rows) != 1))
                or (value['namespace'] == 'tmdb' and rows != ((value['anchor_provider_id'], 0),))
                or value['anchor_provider_id'] != rows[0][0]):
                raise ValueError('系列成员或锚点不完整')
            materialized = value.get('materialized_mapping')
            if materialized is not None:
                if (not isinstance(materialized, Mapping) or materialized.get('series_key') != value['series_key']
                    or type(materialized.get('revision')) is not int or materialized['revision'] < 1
                    or not Path(str(materialized.get('canonical_root') or '')).is_absolute()
                    or re.fullmatch(r'[0-9a-f]{64}', str(materialized.get('payload_fingerprint') or '')) is None
                    or re.fullmatch(r'series-[0-9a-fA-F-]{36}', str(value['series_key'])) is None
                    or set(materialized) - {'series_key', 'revision', 'canonical_root', 'payload_fingerprint', 'provider_link', 'asset_bindings'}):
                    raise ValueError('已登记系列证据不完整')
                uuid.UUID(value['series_key'][7:])
                link = materialized.get('provider_link')
                if link is not None and (not isinstance(link, Mapping)
                    or set(link) != {'mapping_id', 'revision', 'payload_fingerprint'}
                    or not isinstance(link.get('mapping_id'), str) or not link['mapping_id']
                    or type(link.get('revision')) is not int or link['revision'] < 0
                    or re.fullmatch(r'[0-9a-f]{64}', str(link.get('payload_fingerprint') or '')) is None):
                    raise ValueError('跨站登记证据不完整')
            else:
                seed = ('bar:bangumi:subject:' if value['namespace'] == 'bangumi' else 'bar:tmdb:tv:') + rows[0][0]
                if value['series_key'] != f'series-{uuid.uuid5(uuid.NAMESPACE_URL, seed)}':
                    raise ValueError('系列键不属于权威锚点')
            result = cls(value['namespace'], rows[0][0], rows, value['series_key'],
                json.dumps(materialized, sort_keys=True) if materialized is not None else '', value['policy_version'])
            if result.to_dict() != dict(value):
                raise ValueError('系列登记权威指纹或字段已变化')
            return result
        except (KeyError, TypeError, IndexError) as exc:
            raise ValueError('系列登记权威结构无效') from exc


def _relocated_from_mapping(value):
    """Catalog location projection; the historical registration stays intact."""
    try:
        location = value['relocation']
        original = value['original_registration']
        if (set(value) != {'schema_version', 'policy_version', 'original_registration', 'relocation', 'fingerprint'}
                or type(value['schema_version']) is not int or value['policy_version'] != RELOCATION_POLICY
                or not isinstance(original, dict) or original.get('schema_version') not in {1, 2}
                or not isinstance(location, dict)
                or set(location) != {'from_root', 'canonical_root', 'prior_catalog_revision', 'prior_catalog_fingerprint'}
                or any(not isinstance(location[k], str) or not Path(location[k]).is_absolute()
                       for k in ('from_root', 'canonical_root'))
                or Path(location['from_root']) == Path(location['canonical_root'])
                or type(location['prior_catalog_revision']) is not int or location['prior_catalog_revision'] < 1
                or not isinstance(location['prior_catalog_fingerprint'], str)
                or re.fullmatch('[0-9a-f]{64}', location['prior_catalog_fingerprint']) is None):
            raise ValueError('系列重新定位观察无效')
        proof = SeriesRegistrationAuthority.from_mapping(original)
        result = replace(proof, relocation=json.dumps(location, sort_keys=True))
        if result.to_dict() != value:
            raise ValueError('系列重新定位指纹或字段已变化')
        return result
    except (KeyError, TypeError) as exc:
        raise ValueError('系列重新定位结构无效') from exc


def registration_current_root(proof):
    if proof.relocation:
        return json.loads(proof.relocation)['canonical_root']
    if proof.affiliation:
        return json.loads(proof.affiliation)['canonical_root']
    return json.loads(proof.materialized_mapping)['canonical_root'] if proof.materialized_mapping else None


def relocated_registration(proof, prior_record, new_root):
    """Called only after the existing relocate transaction verifies every asset."""
    proof = SeriesRegistrationAuthority.from_mapping(proof.to_dict())
    if (prior_record.get('series_registration_authority') != proof.to_dict()
            or prior_record.get('series_key') != proof.series_key
            or registration_current_root(proof) not in {None, prior_record.get('canonical_root')}):
        raise ValueError('重新定位的原登记或当前位置不一致')
    # Flatten repeat projections without rebasing the original materialized fact.
    location = dict(from_root=prior_record['canonical_root'], canonical_root=str(new_root),
        prior_catalog_revision=prior_record['revision'], prior_catalog_fingerprint=_digest(prior_record))
    return SeriesRegistrationAuthority.from_mapping(replace(proof,
        relocation=json.dumps(location, sort_keys=True)).to_dict())


def _affiliated_from_mapping(value):
    from .work_affiliation import WorkAffiliation
    try:
        affiliation = WorkAffiliation.from_mapping(value['affiliation'])
        bound = affiliation.to_dict()
        rows = tuple(tuple(row) for row in value['season_membership'])
        if (type(value['schema_version']) is not int or value['policy_version'] != AFFILIATION_POLICY
                or bound['backend'] != 'series' or bound['key'] != value['series_key']
                or value['namespace'] not in {'bangumi', 'tmdb'}
                or not isinstance(value['anchor_provider_id'], str) or not value['anchor_provider_id']
                or len({row[0] for row in rows}) != len(rows)
                or any(len(row) != 2 or not isinstance(row[0], str) or not row[0]
                       or type(row[1]) is not int or row[1] < 1 for row in rows)):
            raise ValueError('系列作品归属或官方季度资料无效')
        materialized = value['materialized_mapping']
        if bound['action'] == 'existing':
            if (not isinstance(materialized, dict)
                    or set(materialized) - {'series_key', 'revision', 'canonical_root', 'payload_fingerprint', 'asset_bindings', 'provider_link'}
                    or materialized.get('series_key') != bound['key']
                    or materialized.get('revision') != bound['catalog_revision']
                    or materialized.get('canonical_root') != bound['canonical_root']
                    or materialized.get('payload_fingerprint') != bound['record_fingerprint']
                    or not isinstance(materialized.get('asset_bindings'), list)):
                raise ValueError('系列归属与原登记证据不一致')
        elif materialized is not None:
            raise ValueError('新作品不能携带旧登记证据')
        link = (materialized or {}).get('provider_link')
        if link is not None and (not isinstance(link, Mapping)
                or set(link) != {'mapping_id', 'revision', 'payload_fingerprint'}
                or not isinstance(link.get('mapping_id'), str) or not link['mapping_id']
                or type(link.get('revision')) is not int or link['revision'] < 0
                or re.fullmatch(r'[0-9a-f]{64}', str(link.get('payload_fingerprint') or '')) is None):
            raise ValueError('跨站登记证据不完整')
        result = SeriesRegistrationAuthority(value['namespace'], value['anchor_provider_id'], rows,
            value['series_key'], json.dumps(materialized, sort_keys=True) if materialized else '',
            AFFILIATION_POLICY, affiliation.canonical_json)
        if result.to_dict() != value:
            raise ValueError('系列归属指纹或字段已变化')
        return result
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError('系列作品归属结构无效') from exc


def affiliated_registration(work, affiliation, *, resolution=None, record=None):
    """Official membership is reference data; manual output lives in season_binding."""
    from .work_affiliation import WorkAffiliation
    if type(affiliation) is not WorkAffiliation:
        raise ValueError('系列登记缺少类型化作品归属')
    bound = affiliation.to_dict()
    affiliation.work(work)
    rows = tuple(resolution.season_assignments) if resolution is not None else ()
    namespace = 'bangumi' if rows else work.primary_provider.namespace
    anchor = rows[0][0] if rows else work.primary_provider.provider_id
    materialized = None
    if record is not None:
        if _digest(record) != bound['record_fingerprint']:
            raise ValueError('系列归属原登记摘要不同')
        materialized = dict(series_key=bound['key'], revision=bound['catalog_revision'],
            canonical_root=bound['canonical_root'], payload_fingerprint=bound['record_fingerprint'],
            asset_bindings=_plain(record.get('season_bindings', [])))
    proof = SeriesRegistrationAuthority(namespace, anchor, rows, bound['key'],
        json.dumps(materialized, sort_keys=True) if materialized else '', AFFILIATION_POLICY,
        affiliation.canonical_json)
    return SeriesRegistrationAuthority.from_mapping(proof.to_dict())


def build_series_registration_authority(resolution, *, materialized_mapping=None, manual_season=None):
    from .provider_link_resolver import ProviderLinkResolution
    from .rename_plan import WorkIdentity
    if isinstance(resolution, WorkIdentity):
        provider = resolution.primary_provider
        manual = (provider is not None and provider.namespace == 'bangumi' and provider.entity_type == 'subject'
                  and resolution.domain in {'animation', 'general'})
        if (resolution.status != 'confirmed' or resolution.domain not in {'general', 'animation'} or provider is None
            or not (manual or (provider.namespace == 'tmdb' and provider.entity_type == 'tv')) or provider.status != 'confirmed'):
            raise ValueError('剧集必须先确认 TMDB TV 身份和媒体库')
        if manual:
            from .season_identity import require_manual_season
            require_manual_season(manual_season)
        key = f"series-{uuid.uuid5(uuid.NAMESPACE_URL, ('bar:bangumi:subject:' if manual else 'bar:tmdb:tv:') + provider.provider_id)}"
        if materialized_mapping is not None:
            if not isinstance(materialized_mapping, MaterializedSeriesEvidence):
                raise ValueError('缺少可信登记上下文')
            key = materialized_mapping.payload['series_key']
        return SeriesRegistrationAuthority.from_mapping(SeriesRegistrationAuthority(provider.namespace, provider.provider_id,
            ((provider.provider_id, manual_season if manual else 0),), key,
            json.dumps(materialized_mapping.payload, sort_keys=True) if materialized_mapping else '',
            (MANUAL_SUBJECT_POLICY if resolution.domain == 'animation' else MANUAL_GENERAL_POLICY) if manual else POLICY).to_dict())
    if not isinstance(resolution, ProviderLinkResolution) or resolution.status not in {'inferred', 'materialized_confirmed'}:
        raise ValueError('系列登记必须来自已裁决的季度关系')
    assignments = tuple(resolution.season_assignments)
    order = tuple((resolution.work_segment or {}).get('subject_ids') or ())
    if order:
        if set(order) != {row[0] for row in assignments}:
            raise ValueError('系列分段与季度成员不一致')
        assignments = tuple((subject, dict(assignments)[subject]) for subject in order)
    if (not assignments or not resolution.evidence_path
        or any(not isinstance(item, str) or not item.strip() for item in resolution.evidence_path)
        or resolution.status == 'inferred' and not resolution.requires_plan_review):
        raise ValueError('系列登记缺少完整成员或冻结证据')
    seed = 'bar:bangumi:subject:' + assignments[0][0]
    key = f'series-{uuid.uuid5(uuid.NAMESPACE_URL, seed)}'
    if materialized_mapping is not None:
        if not isinstance(materialized_mapping, MaterializedSeriesEvidence):
            raise ValueError('已登记系列只能由 Catalog 只读适配器提供')
        key = materialized_mapping.payload['series_key']
    result = SeriesRegistrationAuthority('bangumi', assignments[0][0], assignments, key,
        json.dumps(materialized_mapping.payload, sort_keys=True) if materialized_mapping else '')
    return SeriesRegistrationAuthority.from_mapping(result.to_dict())


@dataclass(frozen=True)
class MaterializedSeriesEvidence:
    payload: Mapping[str, object]


def _materialized_evidence(proof, catalog, match, provider_mapping=None, *, library_domain=None):
    raw = json.loads(catalog._path(match.series_key).read_text('utf-8'))
    catalog.validate_archived_payload(raw, match.series_key)
    return materialized_evidence_from_record(proof, raw, match, provider_mapping, library_domain=library_domain)


def materialized_evidence_from_record(proof, raw, match, provider_mapping=None, *, library_domain=None):
    """Pure registration rule; raw records are observed/validated at the boundary."""
    if (raw.get('series_key') != match.series_key or raw.get('revision') != match.revision
            or raw.get('canonical_root') != str(match.canonical_root)
            or _plain(raw.get('season_bindings')) != _plain(match.season_bindings)):
        raise ValueError('系列登记观察与匹配记录不同')
    expected_domain = library_domain or registration_domain(proof)
    if expected_domain not in {'animation', 'general'} or raw.get('domain') != expected_domain:
        raise ValueError('已登记作品的媒体域与新决定不同')
    if proof.affiliation:
        from .work_affiliation import members, identity
        bound = json.loads(proof.affiliation)
        actual = set(members(raw, 'series'))
        if (bound['key'] != match.series_key or bound['canonical_root'] != str(match.canonical_root)
                or identity(bound['provider']) not in actual
                or not {tuple(row) for row in bound['work_members']} <= actual):
            raise ValueError('作品归属与实际登记成员或目录不一致')
        return MaterializedSeriesEvidence(dict(series_key=match.series_key, revision=match.revision,
            canonical_root=str(match.canonical_root), payload_fingerprint=_digest(raw),
            asset_bindings=_plain(raw.get('season_bindings', []))))
    previous = raw.get('series_registration_authority')
    if previous is not None:
        old = SeriesRegistrationAuthority.from_mapping(previous)
        if (old.series_key != match.series_key or old.namespace != proof.namespace or old.anchor_provider_id != proof.anchor_provider_id
            or any(dict(proof.season_membership).get(s) != n for s, n in old.season_membership)):
            raise ValueError('新系列成员与已登记权威矛盾')
    positive = False
    for binding in match.season_bindings:
        provider = binding.get('provider_identity') or {}
        if provider.get('namespace') != proof.namespace:
            # A historical animation Catalog may name its TMDB work. Only a
            # separately validated materialized crosswalk can authenticate the
            # Bangumi members; a metadata provider string alone cannot do so.
            if (proof.namespace == 'bangumi' and provider_mapping is not None
                and provider.get('namespace') == 'tmdb' and provider.get('entity_type') == 'tv'
                and provider_mapping.local_series_key == match.series_key):
                positive = positive or any(
                    row.get('tmdb_tv_id') == str(provider.get('provider_id'))
                    and row.get('season_number') == binding.get('season_number')
                    and dict(proof.season_membership).get(str(row.get('provider_id'))) == row.get('season_number')
                    for row in provider_mapping.season_edges)
            continue
        subject = str(provider.get('provider_id') or '')
        if (subject not in dict(proof.season_membership)
            or proof.namespace == 'bangumi' and dict(proof.season_membership)[subject] != binding.get('season_number')
            or provider.get('entity_type') != ('subject' if proof.namespace == 'bangumi' else 'tv')):
            raise ValueError('季度登记成员与新决定矛盾')
        positive = True
    if not positive:
        raise ValueError('已登记系列缺少成员身份正证，不可仅凭系列键复用')
    return MaterializedSeriesEvidence(dict(series_key=match.series_key, revision=match.revision,
        canonical_root=str(match.canonical_root), payload_fingerprint=_digest(raw),
        asset_bindings=_plain(raw.get('season_bindings', []))))


def bind_resolution(resolution, catalog=None, *, provider_catalog_root=None):
    """Freeze a fresh identity, or the exact current materialized registration."""
    records = []
    if catalog is not None and catalog.root.exists():
        for path in sorted(catalog.root.glob('series-*.json')):
            raw = json.loads(path.read_text('utf-8'))
            catalog.validate_archived_payload(raw, path.stem)
            records.append(raw)
    from .provider_link_catalog import ProviderLinkCatalog
    mappings = (ProviderLinkCatalog(provider_catalog_root).list_mappings()
                if provider_catalog_root is not None else ())
    return bind_resolution_from_records(resolution, tuple(records), mappings,
        series_observed=catalog is not None, provider_observed=provider_catalog_root is not None)


def match_observed_series(raw):
    """Project an already schema-validated record without inspecting live paths."""
    from .series_catalog import SeriesMatch
    return SeriesMatch(raw['schema_version'], raw['series_key'], raw['revision'],
        Path(raw['canonical_root']), raw['canonical_display_name'], tuple(raw['season_bindings']))


def find_observed_series(records, provider):
    from .series_catalog import SeriesCatalog
    needle = SeriesCatalog._provider_key(provider)
    matches = [raw for raw in records if needle is not None and any(
        SeriesCatalog._provider_key(item) == needle for item in raw.get('provider_evidence', ())
        if isinstance(item, Mapping))]
    if len(matches) > 1:
        raise ValueError('同一Provider系列证据指向多个本地series_key')
    return matches[0] if matches else None


def bind_resolution_from_records(resolution, records, mappings, *, series_observed=True, provider_observed=True):
    """Canonical registration rules over complete frozen catalog observations."""
    fresh = build_series_registration_authority(resolution)
    match = None
    anchor = None
    if provider_observed:
        if resolution.anchor_mapping_id:
            anchors = [item for item in mappings if item.mapping_id == resolution.anchor_mapping_id]
            if len(anchors) != 1:
                raise ValueError('原跨站登记锚点缺失或重复')
            anchor = anchors[0]
            if (not anchor.reusable_offline or anchor.revision != resolution.anchor_revision
                or anchor.evidence_fingerprint != resolution.anchor_fingerprint
                or anchor.local_series_key != resolution.proposed_series_key):
                raise ValueError('原跨站登记锚点与当前物化记录不同')
    if series_observed:
        matches = {}
        for subject, _season in fresh.season_membership:
            raw = find_observed_series(records, dict(namespace='bangumi', entity_type='subject', provider_id=subject))
            item = match_observed_series(raw) if raw is not None else None
            if item is not None:
                matches[item.series_key] = item
        if anchor is not None:
            matching = [raw for raw in records if raw['series_key'] == anchor.local_series_key]
            if len(matching) != 1:
                raise ValueError('跨站登记缺少唯一系列登记')
            item = match_observed_series(matching[0])
            matches[item.series_key] = item
        if len(matches) > 1:
            raise ValueError('同一系列成员已登记到多个作品，请人工核对')
        match = next(iter(matches.values()), None)
        if match is None:
            matching = [raw for raw in records if raw['series_key'] == fresh.series_key]
            if len(matching) > 1:
                raise ValueError('系列登记键重复')
            match = match_observed_series(matching[0]) if matching else None
        if match is not None:
            raw = next(raw for raw in records if raw['series_key'] == match.series_key)
            evidence = materialized_evidence_from_record(fresh, raw, match, anchor)
            fresh = build_series_registration_authority(resolution, materialized_mapping=evidence)
    return _bind_provider_registration(resolution, fresh, match, mappings, provider_observed=provider_observed), match


def bind_affiliated_resolution(resolution, proof, record, mappings):
    """The same official-edge checks, after an explicit local-work decision."""
    if not proof.affiliation:
        raise ValueError('跨库关系缺少已确认作品归属')
    if resolution.anchor_mapping_id:
        anchors = [m for m in mappings if m.mapping_id == resolution.anchor_mapping_id]
        if (len(anchors) != 1 or not anchors[0].reusable_offline
                or anchors[0].revision != resolution.anchor_revision
                or anchors[0].evidence_fingerprint != resolution.anchor_fingerprint
                or anchors[0].local_series_key != proof.series_key):
            raise ValueError('原跨站登记锚点与已确认作品归属不同')
    return _bind_provider_registration(resolution, proof,
        match_observed_series(record) if record is not None else None, mappings)


def _bind_provider_registration(resolution, fresh, match, mappings, *, provider_observed=True):
    if provider_observed and resolution.tmdb_tv_id:
        from .provider_link_catalog import season_edges_from_snapshot
        mappings = {m.mapping_id: m for m in mappings
            if m.local_series_key == fresh.series_key and m.reusable_offline}
        if len(mappings) > 1:
            raise ValueError('同一系列有多个跨站登记')
        current = next(iter(mappings.values()), None)
        if current is not None:
            if match is None:
                raise ValueError('跨站登记缺少系列登记，不可生成可执行计划')
            if not any(e.get('namespace') == 'tmdb' and e.get('entity_type') == 'tv'
                and str(e.get('provider_id')) == str(resolution.tmdb_tv_id) for e in current.provider_edges):
                raise ValueError('跨站登记作品身份与冻结资料矛盾')
            snapshot = dict(tmdb_tv_id=resolution.tmdb_tv_id,
                season_assignments=[dict(bangumi_subject_id=s, season_number=n) for s,n in resolution.season_assignments])
            if resolution.subject_observations is not None or resolution.derived_episode_ranges:
                snapshot.update(schema_version=2, derived_episode_ranges=resolution.derived_episode_ranges)
            expected = season_edges_from_snapshot(snapshot)
            old = {str(e['provider_id']): e for e in current.season_edges}
            fields = ('tmdb_tv_id', 'season_number', 'episode_start', 'episode_end')
            for edge in expected:
                existing = old.get(str(edge['provider_id']))
                if existing and any(existing.get(f) != edge.get(f) for f in fields):
                    raise ValueError('跨站已登记分集范围与新系列决定矛盾')
            complete = all(str(e['provider_id']) in old for e in expected)
            resolution = replace(resolution, automation_level='L0' if complete else 'L1',
                status='materialized_confirmed' if complete else 'inferred',
                anchor_mapping_id=current.mapping_id, anchor_revision=current.revision,
                anchor_fingerprint=current.evidence_fingerprint)
            materialized = json.loads(fresh.materialized_mapping)
            materialized['provider_link'] = dict(mapping_id=current.mapping_id, revision=current.revision,
                payload_fingerprint=_digest(current.to_dict()))
            fresh = replace(fresh, materialized_mapping=json.dumps(materialized, sort_keys=True))
        elif resolution.anchor_mapping_id:
            raise ValueError('原跨站登记锚点丢失')
    return replace(resolution, proposed_series_key=fresh.series_key,
        registration_authority=fresh.to_dict())


def validate_plan_series_registration(plan, decision=None):
    """正式计划的登记内容和回执绑定必须同时有效。"""
    from .projection_authority import validate_plan_projection_receipt, _fingerprint, _without_correlation
    proof = _validate_series_registration_contents(plan, decision)
    if proof is None:
        return None
    try:
        validate_plan_projection_receipt(plan)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    snapshot = plan.metadata['series_binding_snapshot']
    if plan.projection_receipt.authority.get('series_binding_fingerprint') != _fingerprint(
            _without_correlation(snapshot), domain='bar.series-binding'):
        raise ValueError('系列登记权威未绑定当前计划回执')
    if proof.affiliation and decision is not None and _fingerprint(
            _without_correlation(decision.season_binding or {}), domain='bar.season-binding') != plan.projection_receipt.authority['season_binding_fingerprint']:
        raise ValueError('本次输出季度未绑定当前计划回执')
    return proof


def _validate_series_registration_contents(plan, decision=None):
    """检查登记内容；无回执草稿不能据此获得批准或执行权。"""
    from .catalog_binding import binding_from_plan
    binding = binding_from_plan(plan)
    if binding is None or binding.backend != 'series':
        if is_series_plan(plan):
            raise ValueError('剧集缺少当前系列登记权威，请重新生成')
        return None
    proof = SeriesRegistrationAuthority.from_mapping(plan.metadata.get('series_registration_authority'))
    if proof.relocation:
        raise ValueError('Catalog重新定位观察不能充当计划执行权威，请重新生成计划')
    snapshot = plan.metadata.get('series_binding_snapshot')
    if (not isinstance(snapshot, Mapping) or _plain(snapshot.get('registration_authority')) != proof.to_dict()
        or snapshot.get('series_key') != binding.key or snapshot.get('canonical_root') != plan.metadata.get('canonical_work_root')
        or snapshot.get('catalog_revision') != binding.revision):
        raise ValueError('系列登记内容与当前计划不一致')
    if proof.series_key != binding.key:
        raise ValueError('计划系列键与登记权威不一致')
    link = plan.metadata.get('provider_link_snapshot')
    if link is not None and (
            not isinstance(link, Mapping) or link.get('series_key') != proof.series_key
            or not isinstance(link.get('season_assignments'), (list, tuple))
            or any(not isinstance(row, Mapping) for row in link['season_assignments'])
            or tuple((row.get('bangumi_subject_id'), row.get('season_number'))
                     for row in link['season_assignments']) != proof.season_membership):
        raise ValueError('跨站季度快照与系列登记权威不一致')
    if proof.affiliation:
        from .work_affiliation import from_plan
        affiliation = from_plan(plan)
        if (affiliation is None or affiliation.canonical_json != proof.affiliation
                or _plain(snapshot.get('affiliation')) != affiliation.to_dict()):
            raise ValueError('系列登记缺少同一作品归属决定')
        frozen_resolution = plan.metadata.get('tv_completion_resolution')
        if frozen_resolution is not None:
            from .tv_completion_observations import FrozenTVResolution
            from .content_associations import encode
            official = FrozenTVResolution(encode(frozen_resolution)).resolution()
            if tuple(official.season_assignments) != proof.season_membership:
                raise ValueError('系列官方季度资料与原冻结观察不同')
        elif proof.season_membership:
            raise ValueError('不能从人工输出季度构造官方季度关系')
        if decision is not None:
            from .decision_state import DecisionStateStore
            season = decision.season_binding or {}
            if (_plain(decision.series_binding or {}) != _plain(snapshot)
                    or DecisionStateStore.fingerprint(decision) != plan.metadata.get('decision_state_fingerprint')
                    or season.get('season_number') != plan.metadata.get('season_number')
                    or season.get('decision_source') != plan.metadata.get('season_source')):
                raise ValueError('本次输出季度或作品归属与冻结决定不一致')
        return proof
    if proof.policy_version in MANUAL_POLICIES and (
            snapshot.get('decision_source') != 'explicit_user'
            or plan.metadata.get('season_source') != 'explicit_user'
            or 'tv_completion_resolution' not in plan.metadata
            or plan.metadata.get('tv_completion_resolution') is not None or link is not None):
        raise ValueError('人工Bangumi季度必须绑定当前人工决定，不能混用跨库推断')
    if link is not None and proof.namespace != 'bangumi':
        raise ValueError('跨站季度快照与系列登记权威不一致')
    work = plan.work_identity
    provider = work.primary_provider if work else None
    if (provider is None or (proof.namespace == 'bangumi' and work.domain != registration_domain(proof)) or provider.namespace != proof.namespace or str(provider.provider_id) not in dict(proof.season_membership)
        or (proof.namespace == 'bangumi' and dict(proof.season_membership).get(str(provider.provider_id)) != plan.metadata.get('season_number'))):
        raise ValueError('所选作品和季度不属于系列登记权威')
    if decision is not None:
        from .decision_state import DecisionStateStore
        if (_plain(decision.series_binding or {}) != _plain(snapshot)
            or DecisionStateStore.fingerprint(decision) != plan.metadata.get('decision_state_fingerprint')):
            raise ValueError('当前任务决定与计划系列登记权威不一致')
    return proof


def is_manual_subject_registration(plan, decision=None):
    return _is_manual_subject_registration(plan, validate_plan_series_registration(plan, decision))


def _is_manual_subject_registration(plan, proof):
    provider = plan.work_identity.primary_provider if plan.work_identity else None
    return (proof is not None and provider is not None
        and (provider.namespace, provider.entity_type) == ('bangumi', 'subject')
        and proof.policy_version in MANUAL_POLICIES | {AFFILIATION_POLICY}
        and 'tv_completion_resolution' in plan.metadata
        and plan.metadata['tv_completion_resolution'] is None
        and plan.metadata.get('provider_link_snapshot') is None)


def _comparison_row(plan, namespace, members):
    value = plan.metadata.get('work_affiliation')
    if value is not None:
        from .work_affiliation import from_plan
        affiliation = from_plan(plan).to_dict()
        namespace, members = 'affiliation:' + affiliation['domain'], [(tuple(row), None) for row in affiliation['work_members']]
    return (namespace, str(plan.metadata['series_key']),
        str(Path(plan.metadata['canonical_work_root']).resolve()).casefold(), dict(members))


def _compare_rows(rows):
    issues, seen = [], []
    for namespace, key, root, members in rows:
        for previous_namespace, previous_key, previous_root, previous_members in seen:
            if namespace.startswith('affiliation:') or previous_namespace.startswith('affiliation:'):
                def identities(ns, values):
                    return set(values) if ns.startswith('affiliation:') else {
                        (ns, 'tv' if ns == 'tmdb' else 'subject', s) for s in values}
                overlap = identities(namespace, members) & identities(previous_namespace, previous_members)
                domain_conflict = (namespace.startswith('affiliation:') and previous_namespace.startswith('affiliation:')
                                   and namespace != previous_namespace)
                if key == previous_key and (root != previous_root or domain_conflict) or overlap and key != previous_key:
                    issues.append('跨任务作品身份、媒体库或规范目录矛盾')
                continue
            overlap = set(members) & set(previous_members) if namespace == previous_namespace else set()
            if (key == previous_key and (root != previous_root or namespace != previous_namespace) or overlap and
                (key != previous_key or any(members[s] != previous_members[s] for s in overlap))):
                issues.append('跨任务系列身份、季度或作品根矛盾，请重新生成媒体计划后重新审计')
        seen.append((namespace, key, root, members))
    return tuple(issues)


def inspect_cross_task_series_consistency(plans):
    issues, rows = [], []
    for plan in plans:
        try:
            proof = validate_plan_series_registration(plan)
            if proof is None:
                continue
            rows.append(_comparison_row(plan, proof.namespace, proof.season_membership))
        except (ValueError, RuntimeError) as exc:
            issues.append(str(exc))
    return tuple(issues) + _compare_rows(rows)


def validate_current_task_set(plan_store, decision_store, task_root, *, expected_plan=None,
                              record_root=None, series_catalog=None, provider_catalog=None):
    """Read a stable current task closure, never historical Plan revisions."""
    from .plan_store import PlanStore
    task_root = Path(task_root)
    if not task_root.is_dir():
        raise ValueError('跨任务核验缺少当前任务目录')
    before = {p: p.read_bytes() for p in task_root.glob('*.json')}
    if expected_plan is not None and task_root / f'{expected_plan.task_id}.json' not in before:
        raise ValueError('被批准或执行的任务不在当前任务集合中')
    reads, rows, task_reads = {}, [], {}
    for path, data in before.items():
        task_id = path.stem
        plan_path = plan_store.path_for(task_id)
        try:
            from .recognition_reset import require_ready
            require_ready(plan_store.root.parent, task_id)
            plan_data = plan_path.read_bytes()
            payload = json.loads(plan_data)
            if not isinstance(payload, dict):
                raise ValueError('Plan root must be an object')
            if expected_plan is not None and task_id != expected_plan.task_id:
                work = payload.get('work_identity')
                provider = work.get('primary_provider') if isinstance(work, dict) else None
                provider = provider if isinstance(provider, dict) else {}
                selected = expected_plan.work_identity.primary_provider if expected_plan.work_identity else None
                identity = tuple(provider.get(k) for k in ('namespace', 'entity_type', 'provider_id'))
                metadata = payload.get('metadata')
                season = metadata.get('season_number') if isinstance(metadata, dict) else None
                if (selected is not None and all(isinstance(value, str) and value for value in identity)
                        and type(season) is int and payload.get('task_id') == task_id
                        and (identity != (selected.namespace, selected.entity_type, selected.provider_id)
                             or season != expected_plan.metadata.get('season_number'))):
                    continue
            plan = plan_store._plan_from_payload(payload, task_id)
        except (OSError, ValueError, RuntimeError):
            if expected_plan is not None and task_id != expected_plan.task_id:
                continue
            raise
        if expected_plan is not None and task_id != expected_plan.task_id:
            # Catalog is the authority for other completed slots. Their current
            # task/retry state is not a dependency of this operation.
            provider = plan.work_identity.primary_provider if plan.work_identity else None
            selected = expected_plan.work_identity.primary_provider if expected_plan.work_identity else None
            if (provider is None or selected is None
                    or (provider.namespace, provider.entity_type, provider.provider_id)
                    != (selected.namespace, selected.entity_type, selected.provider_id)
                    or plan.metadata.get('season_number') != expected_plan.metadata.get('season_number')):
                continue
        task = json.loads(data)
        reads[plan_path], task_reads[path] = plan_data, data
        if expected_plan is not None and task_id == expected_plan.task_id and PlanStore.fingerprint(plan) != PlanStore.fingerprint(expected_plan):
            raise ValueError('被批准或执行的计划在跨任务核验期间变化')
        if not is_series_plan(plan):
            continue
        plan_path, decision_path = plan_store.path_for(task_id), decision_store.path_for(task_id)
        reads[decision_path] = decision_path.read_bytes()
        decision = decision_store.load(task_id)
        if (task.get('uuid') != task_id or plan.task_id != task_id or decision.task_id != task_id
            or task.get('plan_fingerprint') != PlanStore.fingerprint(plan)
            or plan.metadata.get('decision_state_fingerprint') != decision_store.fingerprint(decision)
            or plan.metadata.get('decision_state_revision') != decision.revision):
            raise ValueError('跨任务当前计划或决定已变化，请重新生成')
        proof = validate_plan_series_registration(plan, decision)
        rows.append(_comparison_row(plan, proof.namespace, proof.season_membership))
    issues = _compare_rows(rows)
    if issues:
        raise ValueError('; '.join(issues))
    if (expected_plan is None and before != {p: p.read_bytes() for p in Path(task_root).glob('*.json')}
            or any(p.read_bytes() != data for p, data in {**reads, **task_reads}.items())):
        raise ValueError('跨任务读取期间发生变化，请刷新后重试')


def validate_current_catalog(plan, catalog, provider_catalog=None):
    proof = validate_plan_series_registration(plan)
    if proof is None:
        return
    validate_series_catalog_snapshot(proof, catalog, plan.metadata.get('series_catalog_revision'),
        plan.metadata.get('canonical_work_root'), season_number=plan.metadata.get('season_number'), task_id=plan.task_id, plan=plan)
    frozen = json.loads(proof.materialized_mapping) if proof.materialized_mapping else None
    link = frozen.get('provider_link') if frozen else None
    if link is not None:
        if provider_catalog is None:
            raise ValueError('缺少跨站登记核验上下文')
        current = provider_catalog.load(link['mapping_id'])
        if current.revision != link['revision'] or _digest(current.to_dict()) != link['payload_fingerprint']:
            raise CatalogStateChanged('跨站登记完整内容已变化，请重新生成并批准计划')


def validate_series_catalog_snapshot(proof, catalog, revision, canonical_root, *, season_number=None, task_id=None, plan=None, library_domain=None, completed=False):
    proof = SeriesRegistrationAuthority.from_mapping(proof.to_dict())
    expected_domain = plan.work_identity.domain if plan is not None and plan.work_identity is not None else library_domain
    if expected_domain is None:
        expected_domain = registration_domain(proof)
    if expected_domain not in {'animation', 'general'}:
        raise CatalogStateChanged('缺少已确认作品域')
    path = catalog._path(proof.series_key)
    frozen = json.loads(proof.materialized_mapping) if proof.materialized_mapping else None
    if proof.policy_version in {POLICY, *MANUAL_POLICIES, AFFILIATION_POLICY} and path.exists():
        from .series_catalog import SeriesCatalogError
        try:
            raw = json.loads(path.read_text('utf-8'))
            catalog.validate_archived_payload(raw, proof.series_key)
        except (OSError, ValueError, SeriesCatalogError) as exc:
            raise CatalogStateChanged(f'作品季度登记无法核验，请重新生成计划：{exc}') from exc
        if (raw.get('canonical_root') != canonical_root
                or raw.get('domain') != expected_domain):
            raise CatalogStateChanged('作品根或媒体域已变化，请重新生成计划')
        if plan is not None and (plan.metadata.get('asset_operation') or {}).get('mode')!='replace':
            from .series_asset_scope import validate_peer_append
            for row in raw.get('season_bindings',()):
                if row.get('task_id') != task_id:
                    validate_peer_append(plan,catalog,row,completed=completed)
        if frozen is not None and frozen.get('payload_fingerprint') == _digest(raw):
            return
        current = SeriesRegistrationAuthority.from_mapping(raw.get('series_registration_authority'))
        if proof.affiliation:
            from .work_affiliation import members, identity
            allowed = {tuple(row) for row in json.loads(proof.affiliation)['work_members']}
            if not set(members(raw, 'series')) <= allowed or current.series_key != proof.series_key:
                raise CatalogStateChanged('已登记作品成员与批准的作品归属不同')
        elif (current.namespace != proof.namespace or current.anchor_provider_id != proof.anchor_provider_id
                or current.series_key != proof.series_key
                or any(dict(proof.season_membership).get(subject) != number
                       for subject, number in current.season_membership)):
            raise CatalogStateChanged('作品季度身份关系已变化，请重新生成计划')
        old_rows = (frozen or {}).get('asset_bindings', [])
        rows = raw.get('season_bindings', [])
        from .season_identity import binding_signature
        current_signatures = {binding_signature(row) for row in rows}
        old_signatures = {binding_signature(row) for row in old_rows}
        for old in old_rows:
            if old.get('season_number') != season_number:
                continue
            if binding_signature(old) not in current_signatures:
                raise CatalogStateChanged('所选季度登记已变化，请重新核查')
        for row in rows:
            provider = row.get('provider_identity', {})
            subject = str(provider.get('provider_id') or '')
            if proof.affiliation:
                if identity(provider) not in allowed:
                    raise CatalogStateChanged('登记季度条目不属于批准的作品归属')
            elif (provider.get('namespace') != proof.namespace
                    or provider.get('entity_type') != ('subject' if proof.namespace == 'bangumi' else 'tv')
                    or subject not in dict(proof.season_membership)
                    or proof.namespace == 'bangumi' and dict(proof.season_membership)[subject] != row.get('season_number')):
                raise CatalogStateChanged('登记季度身份与批准关系不一致')
            if (task_id is not None and row.get('season_number') == season_number
                    and binding_signature(row) not in old_signatures and row.get('task_id') != task_id):
                if plan is None:
                    raise CatalogStateChanged('所选季度已有新登记，缺少分集范围核验，请重新生成计划')
        return
    if frozen is None:
        if revision != 0 or path.exists():
            raise CatalogStateChanged('作品季度登记已变化，请重新生成并批准计划')
    else:
        raw = json.loads(path.read_text('utf-8'))
        catalog.validate_archived_payload(raw, proof.series_key)
        if (raw.get('revision') != revision or frozen['revision'] != revision
            or raw.get('canonical_root') != canonical_root
            or frozen['payload_fingerprint'] != _digest(raw)):
            raise CatalogStateChanged('作品登记版本或完整内容已变化，请重新生成并批准计划')






def is_series_plan(plan):
    from .catalog_binding import binding_from_plan
    binding = binding_from_plan(plan)
    if binding is not None:
        return binding.backend == 'series'
    primary = plan.work_identity.primary_provider if plan.work_identity else None
    return bool(plan.metadata.get('series_key')) or (
        primary is not None and primary.entity_type == 'tv'
        and any(group.kind == 'tv_episodes' for group in plan.presentation_groups))


def manual_tv_binding(work, catalog, canonical_root):
    """Explicit live-action TV identity, not a random or title-derived intent."""
    match = catalog.find_by_provider(work.primary_provider.to_dict())
    raw = json.loads(catalog._path(match.series_key).read_text('utf-8')) if match is not None else None
    if raw is not None:
        catalog.validate_archived_payload(raw, match.series_key)
    return manual_tv_binding_from_record(work, raw, canonical_root)


def manual_tv_binding_from_record(work, raw, canonical_root, *, manual_season=None):
    """The same ordinary-TV registration rule, consuming frozen observations."""
    proof = build_series_registration_authority(work, manual_season=manual_season)
    match = match_observed_series(raw) if raw is not None else None
    if match is not None:
        evidence = materialized_evidence_from_record(proof, raw, match, library_domain=work.domain)
        proof = build_series_registration_authority(work, materialized_mapping=evidence, manual_season=manual_season)
    return dict(series_key=proof.series_key, canonical_root=str(match.canonical_root if match else canonical_root),
        catalog_revision=match.revision if match else 0, decision_source='explicit_user', registration_authority=proof.to_dict())


def rebind_materialized_registration(proof, materialized):
    """Refresh catalog evidence without changing the user's registration policy."""
    proof = SeriesRegistrationAuthority.from_mapping(proof.to_dict())
    if not isinstance(materialized, MaterializedSeriesEvidence):
        raise ValueError('缺少可信登记上下文')
    if proof.affiliation:
        from .work_affiliation import WorkAffiliation
        bound = json.loads(proof.affiliation)
        if (bound['key'] != materialized.payload['series_key']
                or bound['canonical_root'] != materialized.payload['canonical_root']):
            raise ValueError('登记复核不能更换已确认的作品或目录')
        bound.update(action='existing', source='confirmed_choice',
            catalog_revision=materialized.payload['revision'],
            record_fingerprint=materialized.payload['payload_fingerprint'])
        proof = replace(proof, affiliation=WorkAffiliation.from_mapping(bound).canonical_json)
    return SeriesRegistrationAuthority.from_mapping(replace(proof,
        materialized_mapping=json.dumps(dict(materialized.payload), sort_keys=True),
        policy_version=POLICY if proof.policy_version == 'series-registration-v1' else proof.policy_version).to_dict())
