"""Frozen movie groups: identity is independent of technical hints and labels.

Only review commands create selections. Readers project/validate their frozen
values without consulting filenames, providers, or live configuration.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re
import unicodedata
import uuid

POLICY = 'movie-version-selection-v1'
_ILLEGAL = str.maketrans('\\/:*?"<>|', '＼／：＊？＂＜＞｜')


def safe_label(value):
    text = ' '.join(str(value or '').split()).translate(_ILLEGAL).strip(' .')
    if not text or any(ord(c) < 32 for c in text):
        raise ValueError('电影版本标签不能为空或包含控制字符')
    if len(text) > 120 or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])', text):
        raise ValueError('电影版本标签过长或为系统保留名称')
    return text


def label_key(value):
    return ' '.join(unicodedata.normalize('NFKC', safe_label(value)).casefold().split())


def _plain(value):
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value


def _encode(value):
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'))


@dataclass(frozen=True)
class MovieGroupFacts:
    payload_json: str
    video_sources: frozenset[str] | None = None
    sources: frozenset[str] = field(init=False)
    refs: frozenset[str] = field(init=False)

    def __post_init__(self):
        groups = json.loads(self.payload_json)
        if (_encode(groups) != self.payload_json
                or self.video_sources is not None and type(self.video_sources) is not frozenset):
            raise ValueError('电影来源组验证必须绑定完整冻结输入')
        sources, refs = _group_members(groups, video_sources=self.video_sources)
        object.__setattr__(self, 'sources', sources)
        object.__setattr__(self, 'refs', refs)


def validate_group_facts(groups, *, video_sources=None):
    """One pure member/descriptor contract, without work or target authority."""
    return MovieGroupFacts(_encode(groups), None if video_sources is None else frozenset(video_sources))


def _group_members(groups, *, video_sources):
    from .rename_plan import VersionDescriptor
    if not isinstance(groups, list):
        raise ValueError('电影来源组必须为完整有序列表')
    sources, refs, labels = set(), set(), set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != {'sources', 'version_ref', 'label', 'descriptor'}:
            raise ValueError('电影版本组字段无效')
        paths = group['sources']
        if (not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not Path(p).is_absolute() for p in paths)
                or len(set(paths)) != len(paths) or sources.intersection(paths)
                or video_sources is not None and not set(paths) <= set(video_sources)):
            raise ValueError('电影版本组来源无效、越界或重复')
        ref, label = group['version_ref'], group['label']
        if not isinstance(ref, str) or not re.fullmatch(r'mv-[0-9a-f]{32}', ref) or ref in refs:
            raise ValueError('电影版本内部身份无效或重复')
        if safe_label(label) != label or label_key(label) in labels:
            raise ValueError('电影版本标签必须安全且唯一')
        descriptor = VersionDescriptor.from_dict(group['descriptor'])
        if descriptor.version_ref != ref or descriptor.display_label != label or descriptor.identity_policy != POLICY:
            raise ValueError('电影版本规格与组身份不一致')
        sources.update(paths); refs.add(ref); labels.add(label_key(label))
    return frozenset(sources), frozenset(refs)


@dataclass(frozen=True)
class MovieVersionSelection:
    payload_json: str

    def __post_init__(self):
        value = json.loads(self.payload_json)
        if (set(value) != {'policy', 'task_id', 'work_key', 'group_id', 'groups', 'excluded_sources', 'replacement'}
                or value['policy'] != POLICY or not all(isinstance(value[k], str) and value[k]
                    for k in ('task_id', 'work_key', 'group_id'))
                or not isinstance(value['groups'], list) or not value['groups']
                or not isinstance(value['excluded_sources'], list) or _encode(value) != self.payload_json):
            raise ValueError('电影版本选择合同不完整')
        facts = validate_group_facts(value['groups'])
        if (type(facts) is not MovieGroupFacts or facts.payload_json != _encode(value['groups'])
                or facts.video_sources is not None):
            raise ValueError('电影来源组共享验证已断开')
        sources, refs = facts.sources, facts.refs
        excluded = value['excluded_sources']
        if (any(not isinstance(p, str) or not Path(p).is_absolute() for p in excluded)
                or sorted(set(excluded)) != excluded or sources.intersection(excluded)):
            raise ValueError('电影排除来源无效或与选中版本重叠')
        replacement = value['replacement']
        if replacement is not None and (not isinstance(replacement, dict)
                or set(replacement) != {'version_ref', 'versions'}
                or len(refs) != 1 or replacement['version_ref'] not in refs
                or not isinstance(replacement['versions'], list) or not replacement['versions']
                or any(row.get('version_ref') != replacement['version_ref'] for row in replacement['versions'])):
            raise ValueError('换源必须冻结一个已登记版本')

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping):
            raise ValueError('电影版本选择缺少类型化合同；请重新生成')
        return cls(_encode(value))

    def to_dict(self):
        return json.loads(self.payload_json)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.payload_json.encode('utf-8')).hexdigest()


def from_plan(plan):
    return MovieVersionSelection.from_mapping(plan.metadata.get('movie_version_selection'))


def rebind_selection(selection, work_key):
    """Explicit authority transition; never carry an old replacement intent."""
    if not isinstance(selection, MovieVersionSelection):
        raise ValueError('电影版本迁移必须消费类型化选择')
    return MovieVersionSelection.from_mapping({**selection.to_dict(),
        'work_key': work_key, 'replacement': None})


def validate_catalog_labels(selection, rows):
    for group in selection.to_dict()['groups']:
        if any(row.get('version_ref') != group['version_ref'] and
               label_key(row.get('display_label') or '默认版本') == label_key(group['label']) for row in rows):
            raise ValueError('该作品已有相同版本标签，请编辑标签或明确选择已登记版本换源')


def replacement_from_catalog(record, version_ref, *, kept_groups):
    """One registered slot, including every segment; never infer replacement."""
    if version_ref is None:
        return None
    if len(kept_groups) != 1 or record is None:
        raise ValueError('换源必须只保留一个完整组并具有当前作品登记')
    rows = [dict(row) for row in record['versions'] if row.get('version_ref') == version_ref]
    if (not rows or not str(version_ref).startswith('mv-')
            or any(not row.get('target') for row in rows)
            or len({row['target'] for row in rows}) != len(rows)):
        raise ValueError('所选换源版本不在当前完整作品登记内')
    return dict(version_ref=version_ref, versions=rows)


def exclusive_companions(videos, *, associations):
    from .content_associations import associated_source_closure
    return set(associated_source_closure(associations, videos))


def create_selection(plan, groups=None, excluded_sources=(), *, associations, registered_versions, previous=None, replacement=None):
    """Explicit producer; supplied groups refer to the frozen inventory entries."""
    from .rename_plan import VersionDescriptor, build_default_version_descriptor
    movie_groups = [g for g in plan.presentation_groups if g.kind == 'movie_continuous']
    if len(movie_groups) != 1:
        raise ValueError('请先确认唯一电影呈现组')
    group_id = movie_groups[0].group_id
    entries = {str(e.source_path): e for e in plan.entries}
    old = previous.to_dict() if previous is not None else None
    old_by_sources = {frozenset(g['sources']): g for g in old['groups']} if old else {}
    if groups is None:
        grouped = {}
        for entry in plan.entries:
            asset = entry.presentation_asset
            if asset and asset.presentation_group == group_id and asset.asset_role.kind == 'content_version':
                descriptor = asset.version_descriptor
                grouped.setdefault(descriptor.version_ref, []).append(entry)
        groups = [dict(sources=[str(e.source_path) for e in sorted(rows,
                       key=lambda e: e.presentation_asset.segment.index if e.presentation_asset.segment else 0)],
                       label=rows[0].presentation_asset.version_descriptor.display_label or '默认版本')
                  for rows in grouped.values()]
    result = []
    for row in groups:
        sources = tuple(str(Path(p)) for p in row['sources'])
        if not sources or any(p not in entries or entries[p].media_kind != 'video' for p in sources):
            raise ValueError('所选电影版本不在冻结视频清单内')
        old_group = old_by_sources.get(frozenset(sources))
        registered_sources = {str(row['source']) for row in registered_versions
                              if old_group and row.get('version_ref') == old_group['version_ref']}
        foreign_slot = bool(registered_sources) and registered_sources != set(sources)
        ref = old_group['version_ref'] if old_group and not foreign_slot else 'mv-' + uuid.uuid5(
            uuid.NAMESPACE_URL, _encode([plan.task_id, group_id, sorted(sources)])).hex
        if replacement is not None:
            ref = replacement['version_ref']
        first = entries[sources[0]].presentation_asset
        descriptor = VersionDescriptor.from_dict(old_group['descriptor']) if old_group else (
            first.version_descriptor if first and first.version_descriptor else build_default_version_descriptor(group_id, None, None))
        if row.get('technical_descriptor') is not None:
            descriptor = VersionDescriptor.from_dict(row['technical_descriptor'])
        label = safe_label(row.get('label') or descriptor.display_label or '默认版本')
        descriptor = replace(descriptor, version_ref=ref, display_label=label, identity_policy=POLICY)
        result.append(dict(sources=list(sources), version_ref=ref, label=label, descriptor=descriptor.to_dict()))
    excluded = exclusive_companions(excluded_sources, associations=associations)
    if any(not p.is_relative_to(plan.source_root) or str(p) not in entries for p in excluded):
        raise ValueError('排除来源不在当前冻结目录内')
    return MovieVersionSelection.from_mapping(dict(policy=POLICY, task_id=plan.task_id,
        work_key=str(plan.metadata.get('catalog_work_key') or plan.metadata.get('proposed_work_key') or ''),
        group_id=group_id, groups=result, excluded_sources=sorted(str(p) for p in excluded), replacement=replacement))


def project_selection(plan, selection):
    """Project the selected complete groups and their attached targets purely."""
    from .rename_plan import AssetRole, PresentationAssetPlanContract, SegmentDescriptor, VersionDescriptor
    if not isinstance(selection, MovieVersionSelection):
        raise ValueError('电影投影必须消费类型化版本选择')
    value = selection.to_dict()
    if value['task_id'] != plan.task_id or value['work_key'] != str(
            plan.metadata.get('catalog_work_key') or plan.metadata.get('proposed_work_key') or ''):
        raise ValueError('电影版本选择与任务/作品不一致')
    root = Path(str(plan.metadata.get('canonical_work_root') or ''))
    if not root.is_absolute():
        raise ValueError('电影版本缺少规范作品根')
    excluded = set(value['excluded_sources'])
    by_source, targets, old_refs = {}, {}, {}
    originals = {str(e.source_path): e for e in plan.entries}
    for group in value['groups']:
        for index, source in enumerate(group['sources'], 1):
            if source not in originals:
                raise ValueError('已选择电影来源缺失，请重新生成')
            descriptor = VersionDescriptor.from_dict(group['descriptor'])
            old = originals[source].presentation_asset
            if old and old.version_descriptor:
                old_refs.setdefault(old.version_descriptor.version_ref, set()).add(group['version_ref'])
            segment = SegmentDescriptor('cd', index, len(group['sources'])) if len(group['sources']) > 1 else None
            by_source[source] = PresentationAssetPlanContract(value['group_id'], AssetRole('content_version'), segment, descriptor)
            suffix = f'-cd{index}' if segment else ''
            targets[source] = root / f"{root.name} - {group['label']}{suffix}{Path(source).suffix.lower()}"
    revised = []
    for entry in plan.entries:
        source = str(entry.source_path)
        if source in excluded:
            continue
        if source in by_source:
            revised.append(replace(entry, target_path=targets[source], presentation_asset=by_source[source],
                season_id=None, episode_id=None, role='movie_content',
                collision_resolution=entry.collision_resolution if entry.target_path == targets[source] else None))
            continue
        asset = entry.presentation_asset
        if asset and asset.asset_role.kind == 'content_version' and asset.presentation_group == value['group_id']:
            raise ValueError('未保留的电影版本必须明确排除整个组')
        owner = asset.owner_version_ref if asset else None
        candidates = old_refs.get(owner, set())
        if len(candidates) == 1:
            asset = replace(asset, owner_version_ref=next(iter(candidates)))
        revised.append(replace(entry, presentation_asset=asset))
    # Labels change paths, not the already resolved subtitle language/association.
    from .subtitle_workflow import format_subtitle_target_fields
    video_targets = {e.source_path: e.target_path for e in revised if e.media_kind == 'video'}
    final = []
    for entry in revised:
        subtitle = entry.subtitle
        if subtitle is not None and subtitle.primary_language and subtitle.associated_video_source_path in video_targets:
            target = format_subtitle_target_fields(target_video=video_targets[subtitle.associated_video_source_path],
                source_subtitle=entry.source_path, primary_language=subtitle.primary_language, script=subtitle.script,
                track_title_tokens=subtitle.track_title_tokens, flags=subtitle.flags)
            entry = replace(entry, target_path=target)
        final.append(entry)
    from .audio_projection import project_audio
    unresolved = tuple(e.source_path.name for e in final if e.media_kind == 'video' and
                       e.presentation_asset and e.presentation_asset.asset_role.kind == 'unknown')
    metadata = {**plan.metadata, 'movie_version_selection': value, 'movie_version_selection_fingerprint': selection.fingerprint,
                'smart_completion_unresolved_videos': unresolved,
                'presentation_assignment_state': 'partial' if unresolved else 'confirmed'}
    presentation_groups = tuple(replace(group, preferred_version_ref=next(iter(old_refs[group.preferred_version_ref])))
        if group.preferred_version_ref in old_refs and len(old_refs[group.preferred_version_ref]) == 1 else group
        for group in plan.presentation_groups)
    return project_audio(replace(plan, entries=tuple(final), metadata=metadata, presentation_groups=presentation_groups,
        ignored_sources=tuple(sorted((set(plan.ignored_sources) - {Path(p) for p in by_source}) | {Path(p) for p in excluded}, key=str))))


def validate_selection(plan):
    selection = from_plan(plan)
    value = selection.to_dict()
    if plan.metadata.get('movie_version_selection_fingerprint') != selection.fingerprint:
        raise ValueError('电影版本选择指纹不一致')
    projected = project_selection(plan, selection)
    # Collision decisions and review flags are independent approved contracts.
    for before, after in zip(plan.entries, projected.entries):
        if (before.source_path, before.target_path, before.presentation_asset) != (after.source_path, after.target_path, after.presentation_asset):
            raise ValueError('电影版本没有使用唯一版本投影')
    if len(plan.entries) != len(projected.entries) or not {Path(p) for p in value['excluded_sources']} <= set(plan.ignored_sources):
        raise ValueError('电影排除闭包与当前计划不一致')
    return selection
