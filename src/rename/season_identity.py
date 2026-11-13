"""One semantic season binding producer for recognition and manual confirmation."""
from collections.abc import Mapping
from copy import deepcopy
import uuid


def provider_key(provider):
    if not isinstance(provider, Mapping):
        raise ValueError('季度身份缺少已确认作品')
    key = tuple(str(provider.get(field) or '').strip().casefold()
                for field in ('namespace', 'entity_type', 'provider_id'))
    if not all(key):
        raise ValueError('季度身份缺少已确认作品')
    return key


class SeasonConfirmationRequired(ValueError):
    """季度资料不足以唯一决定，允许原窗口继续人工确认。"""


def resolve_initial_tv_season(observation, work, source_root, requested_season, resolution):
    """One pure choice from original request/path and frozen Provider facts."""
    from .tv_completion_observations import TVSeasonObservation
    from .cleaner import extract_season
    if (type(observation) is not TVSeasonObservation
            or observation.provider != provider_key(work.primary_provider.to_dict())
            or requested_season is not None and (type(requested_season) is not int or requested_season < 0)):
        raise ValueError('TV季度观察或原请求身份不一致')
    if requested_season is not None:
        season, source = requested_season, 'explicit_user'
    elif observation.ova_subject is not None:
        raise ValueError('独立OVA需要人工确认季号')
    elif resolution is not None:
        season = dict(resolution.season_assignments).get(work.primary_provider.provider_id)
        source = 'provider_crosswalk'
    else:
        inferred = extract_season(source_root.name, allow_bare_number=False)
        if type(inferred) is int and inferred >= 0:
            season, source = inferred, 'inferred_path'
        else:
            regular = tuple(n for n in observation.available if n > 0)
            if len(regular) != 1:
                raise SeasonConfirmationRequired('无法唯一确定当前季度，请在当前窗口确认季号')
            season, source = regular[0], 'provider_unique'
    if (type(season) is not int or season < 0
            or source != 'explicit_user' and observation.available and season not in observation.available):
        raise SeasonConfirmationRequired('当前季度与作品资料不一致，请在当前窗口确认季号')
    return season, source


def require_manual_season(value):
    """Provider season lists are suggestions; the user's number is authoritative."""
    if type(value) is not int or value < 0:
        raise ValueError('季号必须是非负整数（特别篇为 0）')
    return value


def confirmed_season_binding(provider, season_number, decision_source, *, previous=None):
    """Preserve a legacy ID only when its previous Decision proves the same slot."""
    key = provider_key(provider)
    require_manual_season(season_number)
    if decision_source in {None, '', 'unset', 'legacy_placeholder'}:
        raise ValueError('季度身份缺少确认来源')
    binding = getattr(previous, 'season_binding', None)
    old_provider = (getattr(previous, 'confirmed_work', None) or {}).get('primary_provider')
    if (binding and old_provider and provider_key(old_provider) == key
            and binding.get('season_number') == season_number
            and binding.get('season_binding_id')):
        identifier = str(binding['season_binding_id'])
    else:
        seed = '|'.join(('bar:season:v1', *key, str(season_number)))
        identifier = f'season-binding-{uuid.uuid5(uuid.NAMESPACE_URL, seed)}'
    return dict(season_binding_id=identifier, season_number=season_number,
                decision_source=decision_source)


def binding_signature(row):
    return (str(row.get('task_id') or ''), row.get('season_number'), provider_key(row.get('provider_identity')))


def normalize_bindings(rows):
    """Collapse only proven same-task/provider/season aliases, preserving old rows."""
    result = {}
    for item in rows:
        row = deepcopy(dict(item))
        key = binding_signature(row)
        previous = result.get(key)
        if previous is None:
            result[key] = row
        elif row != previous:
            history = list(previous.get('binding_history', []))
            if row not in history:
                history.append(row)
            previous['binding_history'] = history
    return sorted(result.values(), key=lambda r: (r['season_number'], provider_key(r['provider_identity']), r.get('task_id', '')))


def binding_contains(row, identifier):
    return (row.get('season_binding_id') == identifier or any(
        item.get('season_binding_id') == identifier and binding_signature(item) == binding_signature(row)
        for item in row.get('binding_history', ())))
