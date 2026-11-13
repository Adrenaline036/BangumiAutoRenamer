"""按作品身份分别读取特别篇，整季对应只用于正片规划。"""
from .provider_operation import provider_operation
from .special_catalog import identity, read_source, build_observation


def _counterpart(root, bgm, tmdb):
    from .season_reference import _names, _title_series_root
    if root['namespace'] == 'bangumi':
        detail = bgm._get_json(f"/v0/subjects/{root['provider_id']}")
        names = tuple(dict.fromkeys(str(detail.get(k) or '').strip() for k in ('name', 'name_cn') if detail.get(k)))
        date = str(detail.get('date') or '')
        year = int(date[:4]) if date[:4].isdigit() else 0
        hits = {}
        for name in names:
            _, info = tmdb.get_tv_subject_observation_info(name, year)
            if not info:
                continue
            if info.get('_bar_search_truncated'):
                return (), 'incomplete'
            for row in info.get('_bar_search_alternatives') or [info]:
                if any(_title_series_root(str(row.get(k) or '')) == _title_series_root(n)
                       for k in ('name', 'original_name') for n in names):
                    hits[str(row['id'])] = dict(namespace='tmdb', entity_type='tv', provider_id=str(row['id']))
    else:
        _, info = tmdb.get_tv_info_by_id(int(root['provider_id']))
        names = tuple(dict.fromkeys(str(info.get(k) or '').strip() for k in ('name', 'original_name') if info.get(k)))
        date = str(info.get('first_air_date') or '')
        year = int(date[:4]) if date[:4].isdigit() else None
        hits = {}
        for name in names:
            found = bgm.search_subjects(name, domain='unknown', year=year, limit=12)
            if len(found) >= 12:
                return (), 'incomplete'
            for row in found:
                if any(_title_series_root(title) == _title_series_root(n) for title in _names(row) for n in names):
                    hits[str(row['provider_id'])] = identity(row)
    return (tuple(hits.values()), 'found') if len(hits) == 1 else ((), 'ambiguous' if hits else 'not_found')


@provider_operation
def observe(provider, season_number, *, reference=None, bgm=None, tmdb=None, providers=(), previous=None, discover_counterpart=False):
    from .provider_special_evidence import bangumi_client, ProviderSpecialObservation
    from .get_info import Search
    root = identity(provider)
    bgm = bgm if bgm is not None else bangumi_client()
    tmdb = tmdb if tmdb is not None else Search()
    known = {tuple(root.values()): root}
    for row in providers:
        item = identity(row)
        known[tuple(item.values())] = item
    relation = reference.frozen_relation() if reference is not None else None
    if relation is not None:
        rows = [dict(namespace='tmdb', entity_type='tv', provider_id=relation['tmdb']['provider_id']), *relation['subjects']]
        for row in rows:
            item = identity(row)
            known[tuple(item.values())] = item
    if previous is not None:
        prior = ProviderSpecialObservation.from_mapping(previous)
        for source in prior.to_dict().get('catalog', ()):
            item = identity(source['provider'])
            known[tuple(item.values())] = item
    has_other = any(p['namespace'] != root['namespace'] for p in known.values())
    reason = 'found' if has_other else 'not_found'
    # 仅人工请求特别篇资料时补查；正片与季号读取保持单侧。
    if discover_counterpart and not has_other:
        try:
            found, reason = _counterpart(root, bgm, tmdb)
            for row in found:
                item = identity(row)
                known[tuple(item.values())] = item
        except Exception:
            reason = 'unavailable'
    sources = [read_source(p, client=bgm if p['namespace'] == 'bangumi' else tmdb) for p in known.values()]
    return build_observation(root, sources, relation=relation, relation_reason=reason, previous=previous)
