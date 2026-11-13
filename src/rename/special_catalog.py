"""Bounded raw special observations. Raw provider numbers never imply output numbers."""
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import hashlib
import json
from types import MappingProxyType
import unicodedata


def plain(value):
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def encode(value):
    return json.dumps(plain(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(('bar.provider-special-observation.v2\n' + encode(value)).encode('utf-8')).hexdigest()


def identity(provider):
    result = {k: str(provider.get(k) or '') for k in ('namespace', 'entity_type', 'provider_id')}
    if ((result['namespace'], result['entity_type']) not in {('bangumi', 'subject'), ('tmdb', 'tv')}
            or not result['provider_id'].isdigit() or int(result['provider_id']) < 1):
        raise ValueError('特别篇资料来源身份无效')
    return result


def source_number(value):
    if value is None:
        return None
    if type(value) not in (int, float, str) or value == '':
        raise ValueError('invalid_number')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 <= number <= 100000:
            raise ValueError('invalid_number')
        return format(number.normalize(), 'f')
    except InvalidOperation as exc:
        raise ValueError('invalid_number') from exc


def _runtime(value):
    if value in (None, ''):
        return None
    if isinstance(value, str) and ':' in value:
        try:
            parts = [int(p) for p in value.split(':')]
            if len(parts) == 3 and all(p >= 0 for p in parts):
                value = round((parts[0] * 3600 + parts[1] * 60 + parts[2]) / 60)
        except ValueError:
            pass
    return value if type(value) is int and 0 < value <= 1000 else None


def raw_row(item, provider, *, ova_subject=None):
    if not isinstance(item, Mapping) or type(item.get('id')) is not int or item['id'] < 1:
        raise ValueError('invalid_episode_identity')
    bgm = provider['namespace'] == 'bangumi'
    if bgm and (type(item.get('subject_id')) is not int or str(item['subject_id']) != provider['provider_id']
                or type(item.get('type')) is not int or item['type'] not in ((0, 1) if ova_subject else (1,))):
        raise ValueError('episode_ownership_conflict')
    if not bgm and ('season_number' in item and item['season_number'] != 0):
        raise ValueError('episode_ownership_conflict')
    episode_id = str(item['id'])
    number = source_number(item.get('sort') if bgm else item.get('episode_number'))
    row = dict(provider_episode_id=episode_id,
        source_number=number,
        title=str(item.get('name_cn') or item.get('name') or ''), original_title=str(item.get('name') or ''),
        air_date=str(item.get('airdate') or item.get('air_date') or ''),
        runtime=_runtime(item.get('duration') if bgm else item.get('runtime')),
        overview=str(item.get('desc') or item.get('overview') or ''),
        source_url=(f'https://bgm.tv/ep/{episode_id}' if bgm else
                    f'https://www.themoviedb.org/tv/{provider["provider_id"]}/season/0'
                    + (f'/episode/{int(number)}' if number and number.isdigit() and int(number)>0 else '')))
    if ova_subject is not None:
        row.update(episode_type=item['type'], ova_subject=plain(ova_subject))
    return row


def bangumi_episode_type(provider, row):
    """旧SP记录默认type1；新增EP必须带同一OVA的冻结来源证明。"""
    additions = {'episode_type', 'ova_subject'} & set(row)
    if not additions:
        return 1
    if additions != {'episode_type', 'ova_subject'}:
        raise ValueError('OVA分集来源证明不完整')
    from .ova_selection import OvaSubject
    subject = OvaSubject(encode(row['ova_subject']))
    if (identity(provider) != dict(namespace='bangumi', entity_type='subject', provider_id=subject.provider_id)
            or type(row['episode_type']) is not int or row['episode_type'] not in (0, 1)):
        raise ValueError('OVA分集来源身份或类型无效')
    return row['episode_type']


def read_source(provider, *, client):
    """Transport errors and arrived-but-incomplete data have distinct results."""
    provider = identity(provider)
    rows, issues, seen = [], [], set()
    ova_subject = None
    source = dict(provider=provider, title='', complete=False, state='incomplete', reason='incomplete', rows=rows, issues=issues)

    def add(item, index):
        item_id = str(item.get('id') or '') if isinstance(item, Mapping) else ''
        try:
            if item_id:
                if item_id in seen:
                    rows[:] = [r for r in rows if r['provider_episode_id'] != item_id]
                    raise ValueError('duplicate_episode_identity')
                seen.add(item_id)
            if provider['namespace'] == 'bangumi' and isinstance(item, Mapping) and 'subject_id' not in item:
                if type(item.get('id')) is not int or item['id'] < 1:
                    raise ValueError('invalid_episode_identity')
                detail = fetch(f'/v0/episodes/{item["id"]}')
                if not isinstance(detail, Mapping) or detail.get('id') != item['id']:
                    raise ValueError('episode_ownership_conflict')
                item = detail
            row = raw_row(item, provider, ova_subject=ova_subject)
            rows.append(row)
        except ValueError as exc:
            issues.append(dict(provider_episode_id=item_id, index=index, reason=str(exc)))

    class TransportFailure(Exception):
        pass

    def fetch(path):
        try:
            return client._get_json(path)
        except Exception as exc:
            raise TransportFailure() from exc

    try:
        if provider['namespace'] == 'tmdb':
            try:
                payload = client.get_season_info(int(provider['provider_id']), 0)
            except Exception as exc:
                raise TransportFailure() from exc
            if payload is None:
                raise TransportFailure()
            if (not isinstance(payload, Mapping) or not isinstance(payload.get('episodes'), (list, tuple))
                    or len(payload['episodes']) > 1000 or payload.get('season_number', 0) != 0):
                raise ValueError('invalid_season_response')
            for index, item in enumerate(payload['episodes']):
                add(item, index)
        else:
            detail = fetch(f'/v0/subjects/{provider["provider_id"]}')
            if isinstance(detail, Mapping) and str(detail.get('platform', '')).casefold() == 'ova':
                from .ova_selection import OvaSubject
                subject = OvaSubject.capture(detail)
                if subject.provider_id != provider['provider_id']:
                    raise ValueError('episode_ownership_conflict')
                ova_subject = json.loads(subject.payload)
            count, total = 0, None
            # OVA读取全部分集后仅接收正篇和SP；其它分集类型不进入可选资料。
            type_query = '' if ova_subject is not None else '&type=1'
            for offset in range(0, 1000, 100):
                payload = fetch(f'/v0/episodes?subject_id={provider["provider_id"]}{type_query}&limit=100&offset={offset}')
                if payload is None:
                    raise TransportFailure()
                if (not isinstance(payload, Mapping) or type(payload.get('total')) is not int
                        or not 0 <= payload['total'] <= 1000 or payload.get('offset') != offset
                        or not isinstance(payload.get('data'), list) or len(payload['data']) > 100
                        or total is not None and total != payload['total']):
                    raise ValueError('pagination_incomplete')
                total = payload['total']
                for index, item in enumerate(payload['data'], start=count):
                    if ova_subject is None or not isinstance(item, Mapping) or item.get('type') in (0, 1):
                        add(item, index)
                count += len(payload['data'])
                if count == total:
                    break
                if count > total or len(payload['data']) != 100:
                    raise ValueError('pagination_incomplete')
            if count != total:
                raise ValueError('pagination_incomplete')
    except TransportFailure:
        source.update(state='unavailable_or_error', reason='unavailable')
        issues.append(dict(provider_episode_id='', index=None, reason='unavailable'))
        return source
    except ValueError as exc:
        issues.append(dict(provider_episode_id='', index=None, reason=str(exc)))
    if not issues:
        if provider['namespace'] == 'tmdb':
            numbers = [r['source_number'] for r in rows if r['source_number'] is not None]
            if len(set(numbers)) != len(numbers):
                issues.append(dict(provider_episode_id='', index=None, reason='duplicate_output_number'))
                return source
        source.update(complete=True, state='completed_with_rows' if rows else 'completed_empty', reason='found' if rows else 'not_found')
    return source


def _validate_source(source):
    if not isinstance(source, dict) or set(source) != {'provider', 'title', 'complete', 'state', 'reason', 'rows', 'issues'}:
        raise ValueError('特别篇原始来源字段无效')
    if (identity(source['provider']) != source['provider'] or type(source['complete']) is not bool
            or not isinstance(source['title'], str)):
        raise ValueError('特别篇原始来源身份无效')
    rows, issues = source['rows'], source['issues']
    if (not isinstance(rows, list) or len(rows) > 1000 or not isinstance(issues, list)
            or source['state'] not in {'completed_with_rows', 'completed_empty', 'incomplete', 'unavailable_or_error'}
            or source['complete'] != (source['state'] in {'completed_with_rows', 'completed_empty'})
            or source['complete'] and (bool(issues) or (source['state'] == 'completed_with_rows') != bool(rows))
            or not source['complete'] and not issues):
        raise ValueError('特别篇来源完整性状态无效')
    if source['reason'] != {'completed_with_rows': 'found', 'completed_empty': 'not_found',
                           'incomplete': 'incomplete', 'unavailable_or_error': 'unavailable'}[source['state']]:
        raise ValueError('特别篇来源诊断与状态不一致')
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) - {'episode_type', 'ova_subject'} != {'provider_episode_id', 'source_number', 'title', 'original_title',
                'air_date', 'runtime', 'overview', 'source_url'}
                or not isinstance(row['provider_episode_id'], str) or not row['provider_episode_id'].isdigit()
                or int(row['provider_episode_id']) < 1 or row['provider_episode_id'] in seen
                or row['source_number'] != source_number(row['source_number'])
                or row['runtime'] != _runtime(row['runtime'])
                or any(not isinstance(row[k], str) for k in ('title', 'original_title', 'air_date', 'overview', 'source_url'))):
            raise ValueError('特别篇原始行无效')
        bangumi_episode_type(source['provider'], row)
        seen.add(row['provider_episode_id'])
    for issue in issues:
        if (not isinstance(issue, dict) or set(issue) != {'provider_episode_id', 'index', 'reason'}
                or not isinstance(issue['provider_episode_id'], str) or not isinstance(issue['reason'], str)
                or not issue['reason'] or issue['index'] is not None and type(issue['index']) is not int):
            raise ValueError('特别篇异常诊断无效')


def validate_relation(root, catalog, relation):
    """One bounded, offline membership proof; no lookup/catalog writer."""
    keys = [tuple(source['provider'][k] for k in ('namespace', 'entity_type', 'provider_id')) for source in catalog]
    if len(set(keys)) != len(keys) or sum(source['provider'] == root for source in catalog) != 1:
        raise ValueError('特别篇根来源缺失或重复')
    if relation is None:
        if any(source['provider'] != root for source in catalog):
            raise ValueError('跨库特别篇缺少冻结作品关系')
        return
    from .season_reference import match_component
    if not isinstance(relation, dict) or set(relation) != {'subjects', 'relations', 'tmdb', 'assignments'}:
        raise ValueError('特别篇作品关系无效')
    assignments = match_component(relation['subjects'], relation['relations'], relation['tmdb'])
    if not assignments or [list(pair) for pair in assignments] != relation['assignments']:
        raise ValueError('特别篇作品关系不完整或不唯一')
    allowed = {('bangumi', 'subject', str(row['provider_id'])) for row in relation['subjects']}
    allowed.add(('tmdb', 'tv', str(relation['tmdb']['provider_id'])))
    if any(tuple(source['provider'][k] for k in ('namespace', 'entity_type', 'provider_id')) not in allowed for source in catalog):
        raise ValueError('特别篇来源不属于已核对作品')


def _read_v2(root, catalog, *, relation=None, relation_reason='not_found'):
    from .provider_special_evidence import ProviderSpecialObservation
    root, catalog, relation = identity(root), plain(catalog), plain(relation)
    if not isinstance(catalog, list) or not 1 <= len(catalog) <= 13:
        raise ValueError('特别篇来源数量无效')
    for source in catalog:
        _validate_source(source)
    validate_relation(root, catalog, relation)
    if relation_reason not in {'found', 'not_found', 'unavailable', 'ambiguous', 'incomplete', 'unsupported'}:
        raise ValueError('特别篇跨库状态无效')
    source = next(s for s in catalog if s['provider'] == root)
    payload = dict(schema_version=2, state=source['state'], provider_identity=root, season_number=0,
                   catalog=catalog, relation=relation, relation_reason=relation_reason)
    episodes = []
    for source in catalog:
        if source['provider']['namespace'] != 'tmdb' or not source['complete']:
            continue
        for row in source['rows']:
            number = row['source_number']
            if number is not None and number.isdigit() and int(number) >= 1:
                episodes.append(MappingProxyType(dict(provider_namespace='tmdb', provider_entity_type='tv',
                    provider_id=source['provider']['provider_id'], provider_episode_id=row['provider_episode_id'],
                    season_number=0, episode_number=int(number), title=row['title'], air_date=row['air_date'], runtime=row['runtime'])))
    return ProviderSpecialObservation(state=payload['state'], provider_namespace=root['namespace'],
        provider_entity_type=root['entity_type'], provider_id=root['provider_id'], episodes=tuple(episodes),
        observation_fingerprint=fingerprint(payload), schema_version=2, catalog_json=encode(payload))


def from_mapping(value):
    value = plain(value)
    if set(value) != {'schema_version', 'state', 'provider_identity', 'season_number', 'catalog', 'relation', 'relation_reason', 'observation_fingerprint'}:
        raise ValueError('特别篇v2字段无效')
    observation = _read_v2(value['provider_identity'], value['catalog'], relation=value['relation'], relation_reason=value['relation_reason'])
    if encode(observation.to_dict()) != encode(value):
        raise ValueError('特别篇v2指纹或状态无效')
    return observation


def build_observation(root, catalog, *, relation=None, relation_reason='not_found', previous=None):
    from .specials_v3 import build
    return build(root, catalog, relation=relation, relation_reason=relation_reason, previous=previous)


def suggestions(observation):
    """Mutually unique title + date/duration evidence; never a decision."""
    if observation.schema_version not in (2, 3):
        return {}
    value = observation.to_dict()
    bgm, tmdb = [], []
    for source in value['catalog']:
        for row in source['rows']:
            (bgm if source['provider']['namespace'] == 'bangumi' else tmdb).append((source['provider']['provider_id'], row))
    def titles(row):
        return {unicodedata.normalize('NFKC', row[k]).casefold().strip() for k in ('title', 'original_title') if row[k].strip()}
    pairs = []
    for subject, left in bgm:
        for _, right in tmdb:
            if not titles(left).intersection(titles(right)):
                continue
            a, b = left['air_date'], right['air_date']
            if a and b and a != b:
                continue
            duration = left['runtime'] is not None and right['runtime'] is not None and abs(left['runtime'] - right['runtime']) <= 2
            if (a and b and a == b) or duration:
                pairs.append((f'{subject}:{left["provider_episode_id"]}', right['provider_episode_id']))
    return {a: b for a, b in pairs if sum(x == a for x, _ in pairs) == 1 and sum(y == b for _, y in pairs) == 1}
