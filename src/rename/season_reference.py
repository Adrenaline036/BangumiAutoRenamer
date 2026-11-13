"""Optional, read-only season labels. Nothing here creates or persists decisions."""
from collections.abc import Mapping
from dataclasses import dataclass
import json

from .provider_operation import provider_operation
from .provider_link_resolver import _resolve_frozen_evidence, _title_series_root, _parse_date


LIMIT = 12


@dataclass(frozen=True)
class SeasonReferences:
    subjects: tuple = ()
    assignments: tuple = ()
    status: str = 'not_found'
    relation_json: str | None = None

    def frozen_relation(self):
        return json.loads(self.relation_json) if self.relation_json is not None else None

    def names_by_season(self):
        titles = {str(row['provider_id']): str(row['title']) for row in self.subjects}
        names = {}
        for identity, number in self.assignments:
            names.setdefault(number, []).append(titles[identity])
        return {number: ' / '.join(values) for number, values in names.items()}


def _reference(subjects, relations, tmdb, assignments):
    from .special_catalog import encode
    return SeasonReferences(tuple(subjects), assignments, 'found', encode(dict(
        subjects=subjects, relations=relations, tmdb=tmdb, assignments=assignments)))


def complete_component(subjects, relations):
    ids = {str(row.get('provider_id') or '') for row in subjects}
    return (bool(subjects) and len(subjects) < LIMIT and len(ids) == len(subjects) and '' not in ids
            and all(str(edge.get(key) or '') in ids for edge in relations for key in ('from', 'to')))


def tmdb_snapshot(info, expected_id=None):
    if not isinstance(info, Mapping) or not str(info.get('id', '')).isdigit():
        raise ValueError('缺少 TMDB 剧集资料')
    identity = str(info['id'])
    if expected_id is not None and identity != str(expected_id):
        raise ValueError('TMDB 返回的作品身份不一致')
    seasons = info.get('seasons', ())
    if (not isinstance(seasons, (list, tuple))
            or any(not isinstance(row, Mapping) or type(row.get('season_number')) is not int
                   or row['season_number'] < 0 for row in seasons)
            or len({row['season_number'] for row in seasons}) != len(seasons)):
        raise ValueError('TMDB 季度资料不完整')
    return dict(namespace='tmdb', entity_type='tv', provider_id=identity,
                title=info.get('name', ''), original_title=info.get('original_name', ''),
                first_air_date=info.get('first_air_date', ''), seasons=seasons)


def match_component(subjects, relations, tmdb):
    if not complete_component(subjects, relations):
        return ()
    if (any(_parse_date(row.get('air_date')) is None for row in subjects)
            or any(_parse_date(row.get('air_date')) is None for row in tmdb.get('seasons', ())
                   if row.get('season_number', 0) > 0)):
        return ()
    # Use the existing pure resolver, never its registration/catalog wrapper.
    resolution = _resolve_frozen_evidence(selected_provider=subjects[0],
        bangumi_subjects=tuple(subjects), bangumi_relations=tuple(relations),
        tmdb_candidates=(tmdb,), fetch_callbacks_present=False)
    assignments = tuple(sorted(resolution.season_assignments))
    if (resolution.status != 'inferred' or resolution.tmdb_tv_id != tmdb['provider_id']
            or {identity for identity, _ in assignments} != {row['provider_id'] for row in subjects}):
        return ()
    return assignments


def _names(row):
    return tuple(dict.fromkeys(str(row.get(key) or '').strip() for key in ('original_title', 'title')
                               if str(row.get(key) or '').strip()))


def _tmdb_references(provider, bangumi, search):
    _, info = search.get_tv_info_by_id(int(provider['provider_id']))
    tmdb = tmdb_snapshot(info, provider['provider_id'])
    roots = {_title_series_root(name) for name in _names(tmdb)}
    date = str(tmdb.get('first_air_date') or '')
    year = int(date[:4]) if date[:4].isdigit() else None
    candidates = {}
    from .tmdb_genre import classify_tmdb_animation_evidence
    from .provider_selection_authority import is_bangumi_serial_platform
    general = classify_tmdb_animation_evidence(genres=info.get('genres', ()),
        genre_ids=info.get('genre_ids', ())).is_anime is False
    for name in _names(tmdb):
        found = (bangumi.search_subjects(name, domain='general', year=year, limit=LIMIT) if general
                 else bangumi.search_animation(name, year=year, limit=LIMIT))
        if len(found) >= LIMIT:
            return SeasonReferences(status='incomplete')
        for row in found:
            if (is_bangumi_serial_platform(row.get('platform'), include_general=general)
                    and any(_title_series_root(title) in roots for title in _names(row))):
                candidates[str(row['provider_id'])] = row
    if len(candidates) > 4:
        return SeasonReferences(status='incomplete')
    matches, visited = {}, set()
    for identity in candidates:
        if identity in visited:
            continue
        subjects, relations = bangumi.load_crosswalk_evidence(identity, max_subjects=LIMIT)
        if not complete_component(subjects, relations):
            return SeasonReferences(status='incomplete')
        visited.update(row['provider_id'] for row in subjects)
        assignments = match_component(subjects, relations, tmdb)
        if assignments:
            matches[assignments] = (subjects, relations)
    if len(matches) != 1:
        return SeasonReferences(status='ambiguous' if matches else 'not_found')
    assignments, (subjects, relations) = next(iter(matches.items()))
    return _reference(subjects, relations, tmdb, assignments)


def _bangumi_references(provider, bangumi, search=None):
    subjects, relations = bangumi.load_crosswalk_evidence(str(provider['provider_id']), max_subjects=LIMIT)
    if not complete_component(subjects, relations):
        return SeasonReferences(status='incomplete')
    subjects = tuple(sorted(subjects, key=lambda row: (str(row.get('air_date') or ''), row['provider_id'])))
    candidates, matches = {}, {}
    try:
        if search is None:
            from .get_info import Search
            search = Search()
        # The subject list stays useful even if optional TMDB numbering is absent.
        first = subjects[0]
        date = str(first.get('air_date') or '')
        year = int(date[:4]) if date[:4].isdigit() else 0
        for name in _names(first):
            _, info = search.get_tv_subject_observation_info(name, year)
            if info is None:
                continue
            if info.get('_bar_search_truncated'):
                return SeasonReferences(subjects, status='incomplete')
            rows = info.get('_bar_search_alternatives') or [info]
            for row in rows:
                identity = str(row.get('id') or '')
                if identity.isdigit():
                    candidates.setdefault(identity, info if identity == str(info.get('id')) else None)
        if len(candidates) > 4:
            return SeasonReferences(subjects, status='incomplete')
        for identity, info in candidates.items():
            if info is None:
                _, info = search.get_tv_info_by_id(int(identity))
            tmdb = tmdb_snapshot(info, identity)
            assignments = match_component(subjects, relations, tmdb)
            if assignments:
                matches[identity] = (assignments, tmdb)
    except Exception:
        return SeasonReferences(subjects, status='unavailable')
    if len(matches) != 1:
        return SeasonReferences(subjects, status='ambiguous' if matches else 'not_found')
    assignments, tmdb = next(iter(matches.values()))
    return _reference(subjects, relations, tmdb, assignments)


@provider_operation
def load(provider):
    """Read provider data in one bounded operation; no Plan/store access."""
    from .get_info import Search
    from .provider_special_evidence import bangumi_client
    if (provider.get('namespace'), provider.get('entity_type')) == ('tmdb', 'tv'):
        return _tmdb_references(provider, bangumi_client(), Search())
    if (provider.get('namespace'), provider.get('entity_type')) == ('bangumi', 'subject'):
        return _bangumi_references(provider, bangumi_client())
    return SeasonReferences()
