"""User special decisions, canonical descriptive naming and offline validation."""
from collections.abc import Mapping
from dataclasses import replace
import hashlib
from pathlib import Path
import re

from .episode_inference import normalize_target_stem
from .rename_plan import build_default_version_descriptor, AssetRole
from .special_catalog import encode, identity, plain

DESCRIPTIVE_POLICY = 'descriptive-special.v1'
CORRESPONDENCE_POLICY = 'special-correspondence.v1'
SELECTION_POLICY = 'special-selection.v2'
BANGUMI_POLICY = 'bangumi-special.v1'
_NUMBER_TOKEN = re.compile(r'(?i)(?<![a-z0-9])(?:s\d+\s*e\d+(?:\.\d+)?|ep?\s*\d+(?:\.\d+)?)(?![a-z0-9])')


def observation(plan):
    from .provider_special_evidence import ProviderSpecialObservation, validate_special_identity
    value = plan.metadata.get('provider_special_observation')
    if not isinstance(value, Mapping):
        raise ValueError('请先获取特别篇资料')
    frozen = ProviderSpecialObservation.from_mapping(value)
    validate_special_identity(plan, frozen)
    return frozen


def references(plan, namespace):
    frozen = observation(plan)
    if frozen.schema_version not in (2, 3):
        return {}
    from .specials_v3 import digest
    payload = frozen.to_dict()
    catalog = list(payload['catalog'])
    if frozen.schema_version == 3:
        catalog = [s for item in payload['sources'].values() for s in item['history'].values()] + catalog
    return {f'{source["provider"]["provider_id"]}:{row["provider_episode_id"]}':
        dict(provider=source['provider'], row=row, snapshot_fingerprint=digest(source))
        for source in catalog if source['provider']['namespace'] == namespace for row in source['rows']}



def _numbered(ref):
    from .specials_v3 import numberable
    if ref is None or ref['provider']['namespace'] != 'tmdb' or not numberable(ref['row']):
        return None
    row = ref['row']
    return dict(provider_namespace='tmdb', provider_entity_type='tv', provider_id=ref['provider']['provider_id'],
        provider_episode_id=row['provider_episode_id'], season_number=0, episode_number=int(row['source_number']),
        title=row['title'], air_date=row['air_date'], runtime=row['runtime'])


def _freeze(plan, identifier, action, bgm, tmdb, title='', source='user', reference=None):
    value = dict(action=action, policy=SELECTION_POLICY, decision_source='explicit_user',
        work=identity(plan.work_identity.primary_provider.to_dict()), entry_id=identifier,
        bangumi_reference=bgm, tmdb_reference=tmdb, tmdb_row=_numbered(tmdb),
        title=title, title_source=source, source_reference=reference)
    value['selection_fingerprint'] = hashlib.sha256(encode(value).encode('utf-8')).hexdigest()
    validate_decision_shape(value)
    return value


def build_correspondence(plan, tmdb_episode_id, bangumi_key=None, identifier=''):
    matches = [ref for ref in references(plan, 'tmdb').values() if ref['row']['provider_episode_id'] == tmdb_episode_id]
    if len(matches) != 1 or _numbered(matches[0]) is None:
        raise ValueError('请选择带有有效集号的唯一 TMDB 特别篇条目')
    bgm = references(plan, 'bangumi').get(bangumi_key) if bangumi_key is not None else None
    if bangumi_key is not None and bgm is None:
        raise ValueError('所选 Bangumi 特别篇不属于当前资料')
    if identifier:
        _video(plan, identifier)
    return _freeze(plan, identifier, 'confirmed_special_correspondence', bgm, matches[0])


def validate_correspondence(plan, value, identifier=None):
    validate_decision_shape(value)
    if value.get('policy') != SELECTION_POLICY:
        raise ValueError('旧特别篇对应仅可查看，请重新确认或先刷新资料完成快照转换')
    if (value['action'] != 'confirmed_special_correspondence'
            or value['work'] != identity(plan.work_identity.primary_provider.to_dict())
            or identifier is not None and value['entry_id'] != identifier):
        raise ValueError('特别篇决定不属于当前作品或视频')
    return value['tmdb_row']


def normalized_title(value):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in ('/', '\\', '\x00')) or value.strip() in {'.', '..'}:
        raise ValueError('描述标题不能为空或包含路径')
    title = normalize_target_stem(value)
    if not title or len(title) > 180 or _NUMBER_TOKEN.search(title):
        raise ValueError('请填写清楚的描述标题，并移除 SxxExx 或 EP 集号')
    return title


def title_suggestion(value):
    """Visible editable suggestion, never silently applied to a source file."""
    return normalize_target_stem(_NUMBER_TOKEN.sub('', value)).strip(' .-_')


def _video(plan, identifier):
    from .special_decision_projection import special_entry_id
    rows = [e for e in plan.entries if special_entry_id(e) == identifier and e.media_kind == 'video']
    if len(rows) != 1:
        raise ValueError('特别篇文件不存在或不唯一')
    return rows[0]


def build_descriptive_decision(plan, identifier, title, source='user', reference_key=None, bangumi_key=None, tmdb_episode_id=None):
    video = _video(plan, identifier)
    if video.source_path in plan.ignored_sources or plan.metadata.get('content_selection') is None:
        raise ValueError('描述性特别篇必须属于当前保留的剧集视频')
    reference = None
    if reference_key is not None:
        namespace, _, key = reference_key.partition(':')
        reference = references(plan, namespace).get(key)
        if namespace not in {'bangumi', 'tmdb'} or reference is None:
            raise ValueError('描述标题的官方条目不属于当前资料')
    bgm = references(plan, 'bangumi').get(bangumi_key) if bangumi_key else None
    candidates = [r for r in references(plan, 'tmdb').values() if r['row']['provider_episode_id'] == tmdb_episode_id] if tmdb_episode_id else []
    if bangumi_key and bgm is None or tmdb_episode_id and len(candidates) != 1:
        raise ValueError('所选特别篇不属于当前资料')
    tmdb = candidates[0] if candidates else None
    if reference:
        if reference['provider']['namespace'] == 'bangumi':
            bgm = reference
        else:
            tmdb = reference
    title = normalized_title(title)
    if source == 'official' and (reference is None or normalized_title(reference['row']['title']) != title):
        raise ValueError('官方描述标题与所选条目不一致；修改后请作为人工标题确认')
    if source == 'local' and normalized_title(video.source_path.stem) != title:
        raise ValueError('本地描述标题已编辑，请作为人工标题确认')
    return _freeze(plan, identifier, 'descriptive_special', bgm, tmdb, title, source, reference)


def _validate_reference(ref):
    if ref is None:
        return
    from .special_catalog import _validate_source
    if (not isinstance(ref, Mapping) or set(ref) != {'provider', 'row', 'snapshot_fingerprint'}
            or not re.fullmatch('[0-9a-f]{64}', str(ref['snapshot_fingerprint']))):
        raise ValueError('特别篇来源快照无效')
    _validate_source(dict(provider=plain(ref['provider']), title='', complete=True, state='completed_with_rows',
                         reason='found', rows=[plain(ref['row'])], issues=[]))


def validate_decision_shape(value):
    if isinstance(value, Mapping) and value.get('policy') == BANGUMI_POLICY:
        return validate_bangumi_shape(value)
    if not isinstance(value, Mapping) or value.get('policy') != SELECTION_POLICY:
        return _validate_legacy_shape(value)
    fields = {'action', 'policy', 'decision_source', 'work', 'entry_id', 'bangumi_reference', 'tmdb_reference',
              'tmdb_row', 'title', 'title_source', 'source_reference', 'selection_fingerprint'}
    if (set(value) != fields or value['decision_source'] != 'explicit_user'
            or value['action'] not in {'confirmed_special_correspondence', 'descriptive_special'}
            or identity(value['work']) != plain(value['work']) or not isinstance(value['entry_id'], str)
            or value['title_source'] not in {'user', 'official', 'local'}):
        raise ValueError('特别篇人工决定无效')
    for field, namespace in (('bangumi_reference', 'bangumi'), ('tmdb_reference', 'tmdb')):
        ref = value[field]
        _validate_reference(ref)
        if ref is not None and ref['provider']['namespace'] != namespace:
            raise ValueError('特别篇来源侧别无效')
    if value['source_reference'] is not None and value['source_reference'] not in (value['bangumi_reference'], value['tmdb_reference']):
        raise ValueError('描述标题来源不属于所选资料')
    if plain(value['tmdb_row']) != _numbered(value['tmdb_reference']):
        raise ValueError('特别篇编号与所选快照不一致')
    if value['action'] == 'confirmed_special_correspondence':
        if value['tmdb_row'] is None or value['title'] != '' or value['title_source'] != 'user':
            raise ValueError('TMDB编号决定无效')
    elif (normalized_title(value['title']) != value['title'] or value['title_source'] == 'official'
          and (value['source_reference'] is None or normalized_title(value['source_reference']['row']['title']) != value['title'])):
        raise ValueError('描述性特别篇标题无效')
    expected = hashlib.sha256(encode({k:v for k,v in value.items() if k != 'selection_fingerprint'}).encode('utf-8')).hexdigest()
    if expected != value['selection_fingerprint']:
        raise ValueError('特别篇人工决定快照被改变')


def _validate_legacy_shape(value):
    if not isinstance(value, Mapping) or value.get('decision_source') != 'explicit_user':
        raise ValueError('特别篇决定缺少人工确认')
    if value.get('action') == 'confirmed_special_correspondence':
        if (set(value) != {'action', 'policy', 'decision_source', 'tmdb_row', 'bangumi_reference', 'selection_fingerprint'}
                or value['policy'] != CORRESPONDENCE_POLICY or not isinstance(value['tmdb_row'], Mapping)
                or type(value['tmdb_row'].get('episode_number')) is not int or value['tmdb_row']['episode_number'] < 1
                or value['tmdb_row'].get('season_number') != 0
                or not re.fullmatch('[0-9a-f]{64}', str(value['selection_fingerprint']))):
            raise ValueError('双侧特别篇决定无效')
    elif value.get('action') == 'descriptive_special':
        if (set(value) != {'action', 'policy', 'decision_source', 'title', 'title_source', 'source_reference', 'work'}
                or value['policy'] != DESCRIPTIVE_POLICY or value['title_source'] not in {'user', 'official', 'local'}
                or value['title'] != normalized_title(value['title']) or identity(value['work']) != value['work']):
            raise ValueError('描述性特别篇决定无效')
        reference = value['source_reference']
        if reference is not None and (not isinstance(reference, Mapping) or set(reference) != {'provider', 'row'}
                or identity(reference['provider']) != reference['provider'] or not isinstance(reference['row'], Mapping)):
            raise ValueError('描述性特别篇来源无效')
        if value['title_source'] == 'official' and (reference is None or normalized_title(reference['row'].get('title')) != value['title']):
            raise ValueError('描述性特别篇官方标题无效')
    else:
        raise ValueError('特别篇决定动作无效')


def build_descriptive_special(plan, identifier, value):
    """Single target/identity builder used by projection and every admission check."""
    validate_decision_shape(value)
    if (value.get('action') != 'descriptive_special' or value['work'] != identity(plan.work_identity.primary_provider.to_dict())
            or value.get('policy') == SELECTION_POLICY and value['entry_id'] != identifier):
        raise ValueError('描述性特别篇不属于已确认作品')
    if {g.kind for g in plan.presentation_groups} != {'tv_episodes'}:
        raise ValueError('描述性特别篇只能归入已确认剧集')
    video = _video(plan, identifier)
    if video.presentation_asset is None:
        raise ValueError('描述性特别篇缺少视频分组')
    root = Path(str(plan.metadata.get('canonical_work_root') or ''))
    if not root.is_absolute():
        raise ValueError('描述性特别篇缺少已确认作品目录')
    title = normalized_title(value['title'])
    target = root / 'Season 00' / f'{normalize_target_stem(root.name + " - " + title, root.name)}{video.source_path.suffix.lower()}'
    seed = dict(entry_id=identifier, work=value['work'], title=title, policy=DESCRIPTIVE_POLICY)
    descriptor = replace(build_default_version_descriptor(video.presentation_asset.presentation_group, 0, None),
        identity_policy=DESCRIPTIVE_POLICY, version_ref='ds-' + hashlib.sha256(encode(seed).encode('utf-8')).hexdigest()[:32],
        display_label=title)
    return target, descriptor


def validate_descriptive_special(plan, entry):
    if plan is None:
        return False
    from .special_decision_projection import special_entry_id
    key = special_entry_id(entry)
    value = plan.metadata.get('provider_special_decisions', {}).get(key)
    if not isinstance(value, Mapping) or value.get('action') != 'descriptive_special':
        return False
    try:
        target, descriptor = build_descriptive_special(plan, key, value)
        asset = entry.presentation_asset
        return (entry.media_kind == 'video' and type(entry.season_id) is int and entry.season_id == 0
                and entry.episode_id is None and entry.role == 'main_episode' and entry.target_path == target
                and asset is not None and asset.asset_role == AssetRole('content_version')
                and asset.segment is None and asset.version_descriptor == descriptor)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def freeze_existing(plan, decisions):
    """从原计划的已确认资料一次性转换历史决定，不能使用刷新后的资料。"""
    result = {}
    for identifier, raw in decisions.items():
        value = plain(raw)
        if value.get('policy') in {SELECTION_POLICY, BANGUMI_POLICY} or value['action'] == 'unmatched_extra':
            result[identifier] = value
            continue
        if value['action'] == 'confirmed_special_correspondence':
            _validate_legacy_shape(value)
            bgm = value['bangumi_reference']
            bgm_key = f'{bgm["provider"]["provider_id"]}:{bgm["row"]["provider_episode_id"]}' if bgm else None
            converted = build_correspondence(plan, value['tmdb_row']['provider_episode_id'], bgm_key, identifier)
            old_tmdb = {k: v for k, v in converted['tmdb_reference'].items() if k != 'snapshot_fingerprint'}
            payload = dict(work=converted['work'], tmdb=old_tmdb, bangumi=bgm, relation=observation(plan).to_dict()['relation'])
            if (hashlib.sha256(encode(payload).encode('utf-8')).hexdigest() != value['selection_fingerprint']
                    or converted['tmdb_row'] != value['tmdb_row']):
                raise ValueError('历史特别篇决定无法核对，请重新确认')
            result[identifier] = converted
        elif value['action'] == 'descriptive_special':
            _validate_legacy_shape(value)
            ref = value['source_reference']
            key = (f'{ref["provider"]["namespace"]}:{ref["provider"]["provider_id"]}:{ref["row"]["provider_episode_id"]}' if ref else None)
            converted = build_descriptive_decision(plan, identifier, value['title'], value['title_source'], key)
            if ref and {k:v for k,v in converted['source_reference'].items() if k != 'snapshot_fingerprint'} != ref:
                raise ValueError('历史描述性特别篇来源已变化，请重新确认')
            result[identifier] = converted
        else:
            # 旧单侧编号只从原计划已冻结的真实行转换，不重新访问Provider。
            from .provider_special_evidence import official_special_rows
            rows = [r for r in official_special_rows(plan, identifier, include_omitted=True)
                    if all(r.get(k) == value.get(k) for k in ('provider_namespace', 'provider_entity_type',
                        'provider_id', 'provider_episode_id', 'season_number', 'episode_number'))]
            if len(rows) != 1 or rows[0]['provider_namespace'] != 'tmdb':
                raise ValueError('历史特别篇编号不能转换，请重新确认')
            row = rows[0]
            provider = dict(namespace='tmdb', entity_type='tv', provider_id=row['provider_id'])
            ref = dict(provider=provider, snapshot_fingerprint=observation(plan).observation_fingerprint,
                row=dict(provider_episode_id=row['provider_episode_id'], source_number=str(row['episode_number']),
                    title=row['title'], original_title='', air_date=row['air_date'], runtime=row['runtime'],
                    overview='', source_url=''))
            result[identifier] = _freeze(plan, identifier, 'confirmed_special_correspondence', None, ref)
    return result


def validate_bangumi_shape(value):
    fields = {'action', 'policy', 'decision_source', 'work', 'entry_id', 'bangumi_reference',
              'episode_type', 'local_episode', 'title', 'selection_fingerprint'}
    if (set(value) != fields or value['action'] != 'bangumi_special'
            or value['decision_source'] != 'explicit_user' or type(value['episode_type']) is not int or value['episode_type'] not in (0, 1)
            or identity(value['work']) != plain(value['work'])
            or not isinstance(value['entry_id'], str) or not value['entry_id']
            or type(value['local_episode']) is not int or value['local_episode'] < 1
            or normalized_title(value['title']) != value['title']):
        raise ValueError('Bangumi特别篇决定无效')
    ref = value['bangumi_reference']
    _validate_reference(ref)
    if ref is None or ref['provider']['namespace'] != 'bangumi':
        raise ValueError('Bangumi命名需要明确的分集资料')
    from .special_catalog import bangumi_episode_type
    if value['episode_type'] != bangumi_episode_type(ref['provider'], ref['row']):
        raise ValueError('Bangumi分集类型与冻结来源不一致')
    expected = hashlib.sha256(encode({k:v for k,v in value.items() if k != 'selection_fingerprint'}).encode('utf-8')).hexdigest()
    if value['selection_fingerprint'] != expected:
        raise ValueError('Bangumi特别篇决定已变化')


def occupied_bangumi_episodes(plan, identifier):
    from .special_decision_projection import special_entry_id
    occupied = {e.episode_id for e in plan.entries if e.media_kind == 'video'
                and e.season_id == 0 and type(e.episode_id) is int and special_entry_id(e) != identifier}
    for row in plan.metadata.get('special_slot_reservations', ()):
        if row['task_id'] != plan.task_id or row['owner'] != identifier:
            occupied.add(row['local_episode'])
    return occupied


def suggest_bangumi_episode(plan, identifier, reference_key):
    ref = references(plan, 'bangumi').get(reference_key)
    if ref is None:
        raise ValueError('请选择Bangumi特别篇资料')
    existing = plan.metadata.get('provider_special_decisions', {}).get(identifier, {})
    if existing.get('action') == 'bangumi_special' and existing['bangumi_reference'] == ref:
        return existing['local_episode']
    occupied = occupied_bangumi_episodes(plan, identifier)
    folder = Path(plan.metadata['canonical_work_root']) / 'Season 00'
    if folder.exists():
        from .generated_outputs import safe_path
        safe_path(folder, directory=True)
        for path in folder.iterdir():
            match = re.search(r'(?i)S00E(\d+)', path.name)
            if match:
                occupied.add(int(match[1]))
    raw = ref['row']['source_number'] or ''
    preferred = int(raw) if raw.isdigit() and int(raw) > 0 else 1
    if preferred not in occupied:
        return preferred
    number = 1
    while number in occupied:
        number += 1
    return number


def build_bangumi_decision(plan, identifier, reference_key, title, local_episode):
    video = _video(plan, identifier)
    ref = references(plan, 'bangumi').get(reference_key)
    if (ref is None or video.source_path in plan.ignored_sources
            or plan.metadata.get('content_selection') is None
            or type(local_episode) is not int or local_episode < 1
            or local_episode in occupied_bangumi_episodes(plan, identifier)):
        raise ValueError('Bangumi特别篇资料或本地集号无效，请刷新预览')
    from .special_catalog import bangumi_episode_type
    value = dict(action='bangumi_special', policy=BANGUMI_POLICY, decision_source='explicit_user',
        work=identity(plan.work_identity.primary_provider.to_dict()), entry_id=identifier,
        bangumi_reference=ref, episode_type=bangumi_episode_type(ref["provider"], ref["row"]), local_episode=local_episode, title=normalized_title(title))
    value['selection_fingerprint'] = hashlib.sha256(encode(value).encode('utf-8')).hexdigest()
    validate_bangumi_shape(value)
    return value


def build_bangumi_special(plan, identifier, value):
    validate_bangumi_shape(value)
    if (value['entry_id'] != identifier or value['work'] != identity(plan.work_identity.primary_provider.to_dict())
            or {g.kind for g in plan.presentation_groups} != {'tv_episodes'}):
        raise ValueError('Bangumi特别篇不属于当前剧集')
    video = _video(plan, identifier)
    if video.presentation_asset is None:
        raise ValueError('Bangumi特别篇缺少视频分组')
    root = Path(plan.metadata['canonical_work_root'])
    if not root.is_absolute():
        raise ValueError('Bangumi特别篇缺少已确认作品目录')
    raw = value['bangumi_reference']['row']['source_number']
    number = str(raw).zfill(2) if raw is not None else '未知'
    prefix = "EP" if value["episode_type"] == 0 else "SP"
    stem = normalize_target_stem(f'{root.name} - S00E{value["local_episode"]:02d} - {prefix}{number} - {value["title"]}')
    return (root / 'Season 00' / (stem + video.source_path.suffix.lower()),
            build_default_version_descriptor(video.presentation_asset.presentation_group, 0, value['local_episode']))
