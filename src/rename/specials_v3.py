"""特别篇资料快照：逐侧合并，获取状态与可选资料分开。"""
from datetime import datetime, timezone
import hashlib
from types import MappingProxyType
import uuid

from .special_catalog import plain, encode, identity, _validate_source


def digest(value):
    return hashlib.sha256(('bar.specials.v3\n' + encode(value)).encode('utf-8')).hexdigest()


def key(provider):
    return ':'.join(identity(provider).values())


def availability(sources):
    catalog = [item['snapshot'] for item in sources.values()]
    rows = any(s['rows'] for s in catalog)
    empty = any(s['complete'] and s['state'] == 'completed_empty' for s in catalog)
    numbered = any(s['provider']['namespace'] == 'tmdb' and numberable(r) and sum(numberable(other) and int(other['source_number']) == int(r['source_number']) for other in s['rows']) == 1
                   for s in catalog for r in s['rows'])
    return ('rows_available' if rows else 'confirmed_empty' if empty else 'awaiting_data', rows or empty, numbered)


def numberable(row):
    number = row['source_number']
    return isinstance(number, str) and number.isdigit() and int(number) > 0


def build(root, catalog, *, previous=None, relation=None, relation_reason='not_found'):
    root, catalog = identity(root), plain(catalog)
    if not isinstance(catalog, list) or not 1 <= len(catalog) <= 13:
        raise ValueError('特别篇来源数量无效')
    old = {}
    if previous is not None:
        from .provider_special_evidence import ProviderSpecialObservation
        prior = ProviderSpecialObservation.from_mapping(previous)
        if prior.to_dict()['provider_identity'] != root and prior.to_dict()['provider_identity'] not in [s['provider'] for s in catalog]:
            raise ValueError('不能继承另一作品的特别篇资料')
        if prior.schema_version == 3:
            old = prior.to_dict()['sources']
        elif prior.schema_version == 2:
            old = {key(s['provider']): dict(snapshot=s, snapshot_fingerprint=digest(s), history={},
                last_attempt=dict(request_id='historical', state=s['state'], reason=s['reason'],
                    response_fingerprint=digest(s), requested_at='historical')) for s in prior.to_dict()['catalog']}
    sources = plain(old)
    seen = set()
    now = datetime.now(timezone.utc).isoformat()
    for source in catalog:
        _validate_source(source)
        name = key(source['provider'])
        if name in seen:
            raise ValueError('特别篇来源重复')
        seen.add(name)
        earlier = old.get(name)
        snapshot = source
        if earlier and earlier['snapshot']['rows'] and not source['rows']:
            snapshot = earlier['snapshot']
        history = dict(earlier.get('history', {})) if earlier else {}
        if earlier and earlier['snapshot_fingerprint'] != digest(snapshot):
            history[earlier['snapshot_fingerprint']] = earlier['snapshot']
        sources[name] = dict(snapshot=snapshot, snapshot_fingerprint=digest(snapshot), history=history,
            last_attempt=dict(request_id=uuid.uuid4().hex, state=source['state'], reason=source['reason'],
                              response_fingerprint=digest(source), requested_at=now))
    if key(root) not in sources or len(sources) > 13:
        raise ValueError('特别篇主来源缺失或来源过多')
    payload = dict(schema_version=3, provider_identity=root, season_number=0,
        state=availability(sources)[0], sources=sources, relation=plain(relation), relation_reason=relation_reason)
    return materialize(payload)


def materialize(payload):
    from .provider_special_evidence import ProviderSpecialObservation
    root = payload['provider_identity']
    episodes = []
    catalog = [payload['sources'][source_key]['snapshot'] for source_key in sorted(payload['sources'])]
    for source in catalog:
        if source['provider']['namespace'] != 'tmdb':
            continue
        numbers = [int(r['source_number']) for r in source['rows'] if numberable(r)]
        for row in source['rows']:
            if numberable(row) and numbers.count(int(row['source_number'])) == 1:
                episodes.append(MappingProxyType(dict(provider_namespace='tmdb', provider_entity_type='tv',
                    provider_id=source['provider']['provider_id'], provider_episode_id=row['provider_episode_id'],
                    season_number=0, episode_number=int(row['source_number']), title=row['title'],
                    air_date=row['air_date'], runtime=row['runtime'])))
    # catalog为兼容读取投影，校验时必须与sources逐字一致，不能成为第二个写入源。
    payload = {**payload, 'catalog': catalog}
    return ProviderSpecialObservation(state=payload['state'], provider_namespace=root['namespace'],
        provider_entity_type=root['entity_type'], provider_id=root['provider_id'], episodes=tuple(episodes),
        observation_fingerprint=digest(payload), schema_version=3, catalog_json=encode(payload))


def from_mapping(value):
    value = plain(value)
    fields = {'schema_version', 'provider_identity', 'season_number', 'state', 'sources', 'catalog',
              'relation', 'relation_reason', 'observation_fingerprint'}
    if (not isinstance(value, dict) or set(value) != fields or value['schema_version'] != 3
            or value['season_number'] != 0 or identity(value['provider_identity']) != value['provider_identity']
            or not isinstance(value['sources'], dict) or not 1 <= len(value['sources']) <= 13
            or value['relation_reason'] not in {'found', 'not_found', 'unavailable', 'ambiguous', 'incomplete', 'unsupported'}):
        raise ValueError('特别篇资料记录无效')
    for name, item in value['sources'].items():
        if not isinstance(item, dict) or set(item) != {'snapshot', 'snapshot_fingerprint', 'history', 'last_attempt'}:
            raise ValueError('特别篇来源快照无效')
        source, attempt = item['snapshot'], item['last_attempt']
        if not isinstance(item['history'], dict):
            raise ValueError('特别篇旧资料记录无效')
        for fingerprint, historical in item['history'].items():
            _validate_source(historical)
            if digest(historical) != fingerprint or historical['provider'] != source['provider']:
                raise ValueError('特别篇旧资料归属或指纹无效')
        _validate_source(source)
        if name != key(source['provider']) or item['snapshot_fingerprint'] != digest(source):
            raise ValueError('特别篇快照身份或指纹无效')
        if (not isinstance(attempt, dict) or set(attempt) != {'request_id', 'state', 'reason', 'response_fingerprint', 'requested_at'}
                or attempt['state'] not in {'completed_with_rows', 'completed_empty', 'incomplete', 'unavailable_or_error'}
                or attempt['reason'] != {'completed_with_rows': 'found', 'completed_empty': 'not_found',
                    'incomplete': 'incomplete', 'unavailable_or_error': 'unavailable'}[attempt['state']]
                or any(not isinstance(attempt[k], str) or not attempt[k] for k in attempt)):
            raise ValueError('特别篇本次获取状态无效')
    if key(value['provider_identity']) not in value['sources'] or value['state'] != availability(value['sources'])[0]:
        raise ValueError('特别篇资料可用状态无效')
    observation = materialize({k: v for k, v in value.items() if k not in {'observation_fingerprint', 'catalog'}})
    if encode(observation.to_dict()) != encode(value):
        raise ValueError('特别篇资料指纹或兼容投影无效')
    return observation
