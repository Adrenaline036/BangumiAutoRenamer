"""Frozen source relations shared by all content projectors.

Observation is an explicit planning step. This pure value does not authorize a
file operation: ContentSelection binds its sources to physical evidence first.
Consumers never infer a new relation from a target name or a source stem.
"""
from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata

POLICY = 'content-associations-v1'
AUDIO_SUFFIXES = frozenset({'.mka', '.flac', '.wav', '.aac', '.ac3', '.eac3', '.dts', '.m4a', '.mp3', '.ogg'})


def source_key(path):
    return os.path.normpath(str(path)).casefold()


def _plain(value):
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return str(value) if isinstance(value, Path) else value


def encode(value):
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def scoped_path(value, root):
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError('内容来源路径无效')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or not path.is_relative_to(root) or path == root:
        raise ValueError('内容来源不在冻结范围内')
    return path


def source_kind(entry):
    """来源格式决定种类，角色标签不能把杂项变成媒体。"""
    from .subtitle_labels import SUBTITLE_SUFFIXES
    from .utils import VIDEO_SUFFIX
    suffix = unicodedata.normalize('NFKC', entry.source_path.suffix).casefold()
    return ('video' if suffix in VIDEO_SUFFIX else 'subtitle' if suffix in SUBTITLE_SUFFIXES
            else 'audio' if suffix in AUDIO_SUFFIXES else 'other')


@dataclass(frozen=True)
class ContentAssociations:
    payload_json: str

    def __post_init__(self):
        try:
            value = json.loads(self.payload_json)
            if (not isinstance(value, dict) or set(value) != {'policy', 'task_id', 'source_root', 'sources', 'relations'}
                    or value['policy'] != POLICY or not isinstance(value['task_id'], str)
                    or not re.fullmatch(r'[A-Za-z0-9._-]+', value['task_id'])
                    or not isinstance(value['source_root'], str) or not Path(value['source_root']).is_absolute()
                    or '..' in Path(value['source_root']).parts
                    or not isinstance(value['sources'], list) or not isinstance(value['relations'], list)
                    or encode(value) != self.payload_json):
                raise ValueError('内容关联合同不完整')
            root, sources, keys = Path(value['source_root']), {}, set()
            for row in value['sources']:
                if not isinstance(row, dict) or set(row) != {'path', 'kind'} or row['kind'] not in {'video', 'subtitle', 'audio', 'other'}:
                    raise ValueError('内容关联来源种类无效')
                path = scoped_path(row['path'], root)
                if source_key(path) in keys or str(path) != row['path']:
                    raise ValueError('内容关联来源重复或路径非规范')
                keys.add(source_key(path)); sources[str(path)] = row['kind']
            relations = set()
            for row in value['relations']:
                if (not isinstance(row, dict) or set(row) != {'source_path', 'owners', 'state', 'kind', 'source'}
                        or row['kind'] not in {'subtitle', 'audio'} or row['state'] not in {'linked', 'unresolved', 'shared'}
                        or row['source'] not in {'typed_subtitle', 'unique_audio_stem', 'manual', 'unresolved'}
                        or row['source_path'] in relations or sources.get(row['source_path']) != row['kind']
                        or not isinstance(row['owners'], list) or len(set(row['owners'])) != len(row['owners'])
                        or any(sources.get(owner) != 'video' for owner in row['owners'])
                        or (row['state'] == 'linked' and len(row['owners']) != 1)
                        or (row['state'] == 'unresolved' and row['owners'])
                        or (row['state'] == 'shared' and len(row['owners']) < 2)):
                    raise ValueError('内容配套关联缺失、重复或归属无效')
                relations.add(row['source_path'])
            if relations != {path for path, kind in sources.items() if kind in {'subtitle', 'audio'}}:
                raise ValueError('内容配套必须具有明确关联或未决记录')
        except (TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError('内容关联合同无效') from exc

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping):
            raise ValueError('内容关联必须使用类型化合同')
        return cls(encode(value))

    def to_dict(self):
        return json.loads(self.payload_json)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.payload_json.encode('utf-8')).hexdigest()


def observe_content_associations(entries, *, task_id, source_root, choices=None):
    """Explicit pure producer. Inputs are observations, not executable consent."""
    root = Path(source_root)
    entries = tuple(entries)
    sources, kinds = {}, {}
    for entry in entries:
        path = scoped_path(entry.source_path, root)
        if source_key(path) in sources:
            raise ValueError('内容关联来源重复')
        sources[source_key(path)] = entry
        kinds[str(path)] = source_kind(entry)
    explicit = {str(Path(path)): value for path, value in (choices or {}).items()}
    if any(kinds.get(path) not in {'subtitle', 'audio'} for path in explicit):
        raise ValueError('只能为当前字幕或音轨指定视频')
    videos = {path for path, kind in kinds.items() if kind == 'video'}
    pairs = subtitle_pair_map(Path(p) for p, kind in kinds.items() if kind == 'subtitle')
    relations = []
    for entry in entries:
        path = str(entry.source_path)
        kind = kinds[path]
        if kind not in {'subtitle', 'audio'}:
            continue
        owners, origin = [], 'unresolved'
        if path in explicit:
            choice = explicit[path]
            owners = ([] if choice is None else [str(Path(p)) for p in choice]
                      if isinstance(choice, (tuple, list)) else [str(Path(choice))])
            origin = 'manual'
        elif kind == 'subtitle':
            from .subtitle_labels import associate_subtitle_to_video
            matched = associate_subtitle_to_video(entry.source_path, videos).associated_video
            if matched is not None:
                owners, origin = [str(matched)], 'typed_subtitle'
        elif kind == 'audio':
            stem = unicodedata.normalize('NFKC', entry.source_path.stem).casefold()
            matches = [p for p in videos if Path(p).parent == entry.source_path.parent
                and unicodedata.normalize('NFKC', Path(p).stem).casefold() == stem]
            if len(matches) == 1:
                owners, origin = matches, 'unique_audio_stem'
        if kind == 'subtitle' and entry.source_path.suffix.casefold() in {'.idx', '.sub'} and entry.source_path not in pairs:
            owners, origin = [], 'unresolved'
        if any(owner not in videos for owner in owners) or len(set(owners)) != len(owners):
            raise ValueError('所选配套视频不在冻结清单或重复')
        relations.append(dict(source_path=path, owners=sorted(owners), kind=kind, source=origin,
            state='linked' if len(owners) == 1 else 'shared' if owners else 'unresolved'))
    return ContentAssociations.from_mapping(dict(policy=POLICY, task_id=task_id, source_root=str(root),
        sources=[dict(path=path, kind=kinds[path]) for path in sorted(kinds)],
        relations=sorted(relations, key=lambda row: row['source_path'])))


def subtitle_pair_map(paths):
    """从冻结文件名识别同目录同主体的 IDX/SUB，不读取内容。"""
    groups = {}
    for path in paths:
        path = Path(path)
        suffix = path.suffix.casefold()
        if suffix in {'.idx', '.sub'}:
            groups.setdefault(source_key(path.with_suffix('')), {}).setdefault(suffix, []).append(path)
    return {members[ext][0]: members[other][0] for members in groups.values()
            if set(members) == {'.idx', '.sub'} and all(len(v) == 1 for v in members.values())
            for ext, other in (('.idx', '.sub'), ('.sub', '.idx'))}


def subtitle_track_keys(rows):
    """两个物理文件只有在完整配对且归属和语义相同时才算一条轨道。"""
    rows = {Path(path): (owner, semantics) for path, owner, semantics in rows}
    pairs = subtitle_pair_map(rows)
    return {path: min(source_key(path), source_key(pairs[path]))
            if path in pairs and owner is not None and rows[pairs[path]] == (owner, semantics)
            else source_key(path) for path, (owner, semantics) in rows.items()}


def associated_source_closure(associations, selected):
    """Directional only: selecting a sidecar never selects its video."""
    if not isinstance(associations, ContentAssociations):
        raise ValueError('配套闭包缺少冻结关联')
    value = associations.to_dict()
    sources = {row['path']: row['kind'] for row in value['sources']}
    paths = {str(Path(p)) for p in selected}
    if not paths <= sources.keys():
        raise ValueError('所选来源不在已登记材料内')
    pairs = subtitle_pair_map(Path(p) for p, kind in sources.items() if kind == 'subtitle')
    result = paths | {str(pairs[Path(p)]) for p in paths if Path(p) in pairs}
    for row in value['relations']:
        if row['state'] == 'linked' and row['owners'][0] in paths:
            result.add(row['source_path'])
    return tuple(Path(path) for path in sorted(result))


def partition_source_closure(associations, selected):
    """Source-only partition: all owners of a shared companion stay together."""
    selected = tuple(str(Path(path)) for path in selected)
    result = set(associated_source_closure(associations, selected))
    value = associations.to_dict()
    kinds = {row['path']: row['kind'] for row in value['sources']}
    if not selected or len(set(selected)) != len(selected) or any(kinds[p] != 'video' for p in selected):
        raise ValueError('独立整理必须选择不重复的视频来源')
    for row in value['relations']:
        owners = set(row['owners'])
        if row['state'] == 'shared' and owners.intersection(selected):
            if not owners.issubset(selected):
                raise ValueError('配套来源由两侧视频共享，请先明确归属再拆分')
            result.add(Path(row['source_path']))
    return tuple(sorted(result, key=str))


def from_plan(plan):
    from .content_selection import from_plan as content_from_plan
    selection = content_from_plan(plan)
    return selection.associations if selection is not None else None


def relation_map(associations):
    if not isinstance(associations, ContentAssociations):
        raise ValueError('内容投影缺少冻结关联')
    return {Path(row['source_path']): row for row in associations.to_dict()['relations']}


def complete_subtitle_associations(plan, associations):
    """Fill only never-decided placeholder links using the shared pure producer."""
    old = associations.to_dict()
    manual = {row['source_path']: row['owners'] for row in old['relations'] if row['source'] == 'manual'}
    observed = observe_content_associations(plan.entries, task_id=plan.task_id,
        source_root=plan.source_root, choices=manual).to_dict()
    fresh = {row['source_path']: row for row in observed['relations']}
    old['relations'] = [fresh[row['source_path']] if row['kind'] == 'subtitle'
        and row['source'] == 'unresolved' and row['state'] == 'unresolved'
        and fresh[row['source_path']]['state'] == 'linked' else row for row in old['relations']]
    return ContentAssociations.from_mapping(old)


def project_subtitle_contract(contract, row):
    """One pure projection of an already frozen subtitle relation."""
    from .subtitle_labels import DimensionResolution
    if row is None or row['kind'] != 'subtitle':
        raise ValueError('字幕缺少冻结配套关系')
    owner = Path(row['owners'][0]) if row['state'] == 'linked' else None
    return replace(contract, associated_video_source_path=owner,
        association_state=DimensionResolution.RESOLVED if owner is not None else DimensionResolution.UNRESOLVED,
        association_candidates=(tuple(Path(p) for p in row['owners']) if row['source'] == 'manual'
            else contract.association_candidates))


def subtitle_target(entry, contract, video_target):
    """语言待确认时保留草案目标，归属决定不猜测字幕语言。"""
    from .subtitle_workflow import format_subtitle_target_fields
    if video_target is None or not contract.primary_language:
        return entry.target_path
    return format_subtitle_target_fields(target_video=video_target,
        source_subtitle=entry.source_path, primary_language=contract.primary_language,
        script=contract.script, track_title_tokens=contract.track_title_tokens, flags=contract.flags)


def subtitle_entries(source_entries, associations, *, tv_identity=False):
    links = relation_map(associations)
    videos = {e.source_path: e for e in source_entries if e.media_kind == 'video'}
    entries = []
    for entry in source_entries:
        if entry.subtitle is not None:
            row = links.get(entry.source_path)
            contract = project_subtitle_contract(entry.subtitle, row)
            video = videos.get(contract.associated_video_source_path)
            target = subtitle_target(entry, contract, video.target_path if video else None)
            role = 'extra_subtitle' if video and video.presentation_asset and (
                video.presentation_asset.asset_role.kind == 'extra') else 'subtitle'
            identity = dict(season_id=video.season_id if video else None,
                episode_id=video.episode_id if video else None,
                episode_end=video.episode_end if video else None,
                presentation_asset=None) if tv_identity else {}
            entry = replace(entry, role=role, subtitle=contract, target_path=target, **identity,
                collision_resolution=entry.collision_resolution if entry.target_path == target else None)
        entries.append(entry)
    return tuple(entries)


def project_subtitles(plan, associations, *, tv_identity=False):
    return replace(plan, entries=subtitle_entries(plan.entries, associations, tv_identity=tv_identity))
