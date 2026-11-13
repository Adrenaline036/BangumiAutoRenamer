"""Finite frozen subtitle facts. Parsing/projecting never grants publication."""
from dataclasses import dataclass, field
import json
from pathlib import Path
import re

from .content_associations import encode

POLICY = 'content-selection-v3'
ORIGIN = 'partition-transfer-v1'
FIELDS = {'origin_kind', 'initial_source_basis_fingerprint', 'partition_origin', 'inherited_subtitle_edits'}
_HASH = re.compile(r'[0-9a-f]{64}')
_PROVENANCE = ('applied_rule_id', 'applied_rule_fingerprint', 'applied_rule_scope', 'applied_rule_engine_version')
_LOCAL = {'manual_file', 'manual_series'}


def _fields(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError('有限字幕' + label + '字段无效')


def _hash(value, *, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError('有限字幕指纹无效')


def _digest(value):
    from .content_selection import digest
    return digest(value)


def _identity(row):
    return {key: row[key] for key in ('source_path', 'physical_key', 'size', 'mtime_ns')}


def _sources(basis):
    return {row['source_path']: row for row in basis['sources']}


def _relation(value, path, sources):
    _fields(value, {'source_path', 'owners', 'state', 'kind', 'source'}, '关联')
    owners = value['owners']
    if (value['source_path'] != path or value['kind'] != 'subtitle'
            or value['source'] not in {'typed_subtitle', 'manual', 'unresolved'}
            or not isinstance(owners, list) or len(owners) != len(set(owners))
            or any(p not in sources or sources[p]['kind'] != 'video' for p in owners)
            or value['state'] not in {'linked', 'unresolved', 'shared'}
            or (value['state'] == 'linked' and len(owners) != 1)
            or (value['state'] == 'unresolved' and owners)
            or (value['state'] == 'shared' and len(owners) < 2)):
        raise ValueError('有限字幕关联超出对应历史上下文')


def _material(value, relation, path, sources):
    from .rename_plan import SubtitlePlanContract
    _fields(value, {'subtitle', 'requires_review', 'evidence'}, '材料')
    subtitle = SubtitlePlanContract.from_dict(value['subtitle'])
    if (subtitle.to_dict() != value['subtitle'] or type(value['requires_review']) is not bool
            or not isinstance(value['evidence'], list)
            or not all(isinstance(item, str) for item in value['evidence'])):
        raise ValueError('有限字幕材料格式无效')
    _relation(relation, path, sources)
    owner = relation['owners'][0] if relation['state'] == 'linked' else None
    if (value['subtitle']['associated_video_source_path'] != owner
            or any(str(p) not in sources or sources[str(p)]['kind'] != 'video'
                   for p in subtitle.association_candidates)):
        raise ValueError('有限字幕材料与对应关联不一致')


def _partition(value, contexts):
    _fields(value, {'partition_fingerprint', 'parent_task_id', 'parent_plan_fingerprint',
        'parent_decision_fingerprint', 'parent_content_fingerprint', 'source_context', 'destination_context',
        'destination_task_id', 'destination_scope_fingerprint'}, '交接')
    for key in set(value) - {'parent_task_id', 'destination_task_id'}:
        _hash(value[key])
    before, after = contexts[value['source_context']], contexts[value['destination_context']]
    old, new = _sources(before), _sources(after)
    if (value['parent_task_id'] != before['task_id'] or value['destination_task_id'] != after['task_id']
            or value['destination_scope_fingerprint'] != after['task_source_scope']['source_identity_set_fingerprint']
            or not new.keys() < old.keys() or any(new[path] != old[path] for path in new)):
        raise ValueError('有限字幕交接必须严格缩小整卡且保留物理身份')


def _parse(value):
    from .source_basis import InitialSourceBasis
    _fields(value, {'policy', 'contexts', 'partitions', 'steps', 'terminal',
        'terminal_active_count', 'active_count'}, '历史')
    if value['policy'] != 'finite-subtitle-edits-v1':
        raise ValueError('有限字幕历史版本无效')
    contexts, partitions, steps = value['contexts'], value['partitions'], value['steps']
    if (not isinstance(contexts, dict) or not 2 <= len(contexts) <= 65
            or not isinstance(partitions, list) or not 1 <= len(partitions) <= 64
            or not isinstance(steps, list) or len(steps) > 256
            or type(value['terminal_active_count']) is not int or value['terminal_active_count'] != len(steps)
            or type(value['active_count']) is not int or not 0 <= value['active_count'] <= len(steps)):
        raise ValueError('有限字幕集合或撤销游标无效/超限')
    for fingerprint, raw in contexts.items():
        basis = InitialSourceBasis.from_mapping(raw)
        if fingerprint != basis.fingerprint or len(raw['sources']) > 10000:
            raise ValueError('有限字幕上下文与实际basis指纹不符')
    seen_partitions, used_contexts, previous_context = {}, set(), None
    for index, part in enumerate(partitions):
        _partition(part, contexts)
        if (part['partition_fingerprint'] in seen_partitions
                or previous_context is not None and part['source_context'] != previous_context
                or part['destination_context'] in used_contexts):
            raise ValueError('有限字幕交接链重复、循环或断开')
        seen_partitions[part['partition_fingerprint']] = index
        used_contexts.update((part['source_context'], part['destination_context']))
        previous_context = part['destination_context']
    if used_contexts != contexts.keys():
        raise ValueError('有限字幕上下文存在未绑定材料')
    destination = _sources(contexts[previous_context])
    previous_fp, seen_origins, seen_fps, affected, aliases = None, set(), set(), set(), {}
    effect_count = 0
    for index, step in enumerate(steps):
        _fields(step, {'fingerprint', 'previous_step_fingerprint', 'origin', 'context',
            'restriction_chain', 'effects'}, '步骤')
        _hash(step['fingerprint'])
        _hash(step['previous_step_fingerprint'], nullable=True)
        if (step['fingerprint'] != _digest({k: v for k, v in step.items() if k != 'fingerprint'})
                or step['previous_step_fingerprint'] != previous_fp or step['fingerprint'] in seen_fps):
            raise ValueError('有限字幕步骤指纹或前序链无效')
        origin = step['origin']
        _fields(origin, {'task_id', 'base_plan_fingerprint', 'post_plan_fingerprint',
            'rule_id', 'rule_fingerprint', 'rule_index'}, '原规则')
        for key in ('base_plan_fingerprint', 'post_plan_fingerprint', 'rule_fingerprint'):
            _hash(origin[key])
        context = contexts[step['context']]
        origin_key = (origin['task_id'], origin['rule_id'], origin['rule_fingerprint'])
        if (origin['task_id'] != context['task_id'] or not isinstance(origin['rule_id'], str)
                or not origin['rule_id'].strip() or origin_key in seen_origins
                or type(origin['rule_index']) is not int or not 0 <= origin['rule_index'] < 256
                or origin['base_plan_fingerprint'] == origin['post_plan_fingerprint']):
            raise ValueError('有限字幕原规则不唯一或归属无效')
        restrictions = step['restriction_chain']
        if not isinstance(restrictions, list) or not restrictions or len(restrictions) > 64:
            raise ValueError('有限字幕缺少有限缩减链')
        first_index = seen_partitions[restrictions[0]['partition_fingerprint']]
        if len(restrictions) != len(partitions) - first_index or partitions[first_index]['source_context'] != step['context']:
            raise ValueError('有限字幕缩减链不从原上下文连续到本卡')
        for hop, (restriction, part) in enumerate(zip(restrictions, partitions[first_index:])):
            _fields(restriction, {'partition_fingerprint', 'prior_edit_fingerprint'}, '缩减')
            prior = restriction['prior_edit_fingerprint']
            _hash(prior, nullable=hop == 0)
            if restriction['partition_fingerprint'] != part['partition_fingerprint'] or (hop == 0 and prior is not None):
                raise ValueError('有限字幕缩减顺序或初次来源无效')
            if prior is not None:
                key = (part['source_context'], prior)
                if key in aliases:
                    raise ValueError('旧有限字幕引用映射不唯一')
                aliases[key] = index
        effects = step['effects']
        if not isinstance(effects, list) or not effects:
            raise ValueError('有限字幕不得包含空规则步骤')
        seen_sources, original = set(), _sources(context)
        for effect in effects:
            _fields(effect, {'source_identity', 'before', 'after', 'before_relation', 'after_relation'}, '效果')
            identity = effect['source_identity']
            path = identity['source_path']
            if (path in seen_sources or path not in destination or destination[path]['kind'] != 'subtitle'
                    or identity != _identity(destination[path]) or path not in original
                    or identity != _identity(original[path])):
                raise ValueError('有限字幕效果越界、重复或物理身份不一致')
            _material(effect['before'], effect['before_relation'], path, original)
            _material(effect['after'], effect['after_relation'], path, original)
            seen_sources.add(path)
        effect_count += len(effects)
        affected.update(seen_sources)
        seen_origins.add(origin_key)
        seen_fps.add(step['fingerprint'])
        previous_fp = step['fingerprint']
    terminal = value['terminal']
    _fields(terminal, {'parent_content_fingerprint', 'materials'}, '出生末态')
    if terminal['parent_content_fingerprint'] != partitions[-1]['parent_content_fingerprint']:
        raise ValueError('有限字幕末态不属于实际父内容锚点')
    if not isinstance(terminal['materials'], list) or effect_count + len(terminal['materials']) > 65536:
        raise ValueError('有限字幕完整效果数量超限')
    seen_sources, original = set(), _sources(contexts[partitions[-1]['source_context']])
    for row in terminal['materials']:
        _fields(row, {'source_identity', 'subtitle', 'requires_review', 'evidence', 'relation'}, '末态材料')
        path = row['source_identity']['source_path']
        if (path in seen_sources or path not in affected or row['source_identity'] != _identity(destination[path])):
            raise ValueError('有限字幕末态来源缺失、重复或越界')
        _material({k: row[k] for k in ('subtitle', 'requires_review', 'evidence')}, row['relation'], path, original)
        seen_sources.add(path)
    if seen_sources != affected:
        raise ValueError('有限字幕末态必须完整覆盖可见与隐藏的影响来源')
    for index, step in enumerate(steps):
        for effect in step['effects']:
            path = effect['source_identity']['source_path']
            before = _reference(effect['before']['subtitle'], step['context'], path, value, contexts, aliases)
            after = _reference(effect['after']['subtitle'], step['context'], path, value, contexts, aliases)
            if before is not None and before >= index or after != index:
                raise ValueError('有限字幕步骤存在前向引用或原规则结果不符')
    return contexts, aliases


@dataclass(frozen=True)
class InheritedSubtitleEdits:
    payload_json: str

    def __post_init__(self):
        try:
            value = json.loads(self.payload_json)
            if encode(value) != self.payload_json:
                raise ValueError('有限字幕历史必须规范序列化')
            _parse(value)
        except (TypeError, KeyError, IndexError, AttributeError, json.JSONDecodeError) as exc:
            raise ValueError('有限字幕历史格式无效') from exc

    @classmethod
    def from_mapping(cls, value):
        return cls(encode(value))

    def to_dict(self):
        return json.loads(self.payload_json)


def _reference(subtitle, context, path, value, contexts, aliases):
    source = subtitle['decision_source']
    ref = subtitle.get('inherited_edit_fingerprint', '')
    if source == 'manual_transfer':
        if not ref or any(subtitle[k] for k in _PROVENANCE) or not subtitle['manual_reason'].strip():
            raise ValueError('有限字幕来源不能混入本地规则字段')
        matches = [i for i, step in enumerate(value['steps']) if
            (aliases.get((context, ref)) == i or context == value['partitions'][-1]['destination_context']
             and ref == step['fingerprint']) and any(e['source_identity']['source_path'] == path for e in step['effects'])]
    elif source in _LOCAL:
        from .rename_plan import SUBTITLE_OVERRIDE_ENGINE_VERSION
        if (ref or not all(subtitle[k] for k in _PROVENANCE) or not subtitle['manual_reason'].strip()
                or subtitle['applied_rule_scope'] != source.removeprefix('manual_')
                or subtitle['applied_rule_engine_version'] != SUBTITLE_OVERRIDE_ENGINE_VERSION):
            raise ValueError('本地字幕规则不能夹带有限引用')
        matches = [i for i, step in enumerate(value['steps']) if
            step['origin']['task_id'] == contexts[context]['task_id']
            and step['origin']['rule_id'] == subtitle['applied_rule_id']
            and step['origin']['rule_fingerprint'] == subtitle['applied_rule_fingerprint']
            and any(e['source_identity']['source_path'] == path for e in step['effects'])]
    else:
        if ref or any(subtitle[k] for k in _PROVENANCE):
            raise ValueError('普通字幕不能夹带继承或本地规则来源')
        return None
    if len(matches) != 1:
        raise ValueError('有限字幕原引用不存在、不唯一或不属于该来源')
    return matches[0]


def project(value):
    """Pure birth-terminal/undo projection; no origin history is reopened."""
    parsed = InheritedSubtitleEdits.from_mapping(value)
    value = parsed.to_dict()
    contexts, aliases = _parse(value)
    source_context = value['partitions'][-1]['source_context']
    rows = {row['source_identity']['source_path']: row for row in value['terminal']['materials']}
    row_contexts = {path: source_context for path in rows}
    # Resolve even with active_count == terminal_active_count. Birth is not a
    # license to copy the parent's local provenance to the destination task.
    for path, row in rows.items():
        _reference(row['subtitle'], source_context, path, value, contexts, aliases)
    for index in reversed(range(value['active_count'], value['terminal_active_count'])):
        step = value['steps'][index]
        for effect in step['effects']:
            path = effect['source_identity']['source_path']
            row = rows[path]
            if _reference(row['subtitle'], row_contexts[path], path, value, contexts, aliases) == index:
                row['subtitle'] = effect['before']['subtitle']
                row['relation'] = effect['before_relation']
                row_contexts[path] = step['context']
    destination = _sources(contexts[value['partitions'][-1]['destination_context']])
    for path, row in rows.items():
        index = _reference(row['subtitle'], row_contexts[path], path, value, contexts, aliases)
        if index is not None:
            if index >= value['active_count']:
                raise ValueError('有限字幕仍引用已撤销步骤')
            row['subtitle'].update(decision_source='manual_transfer',
                inherited_edit_fingerprint=value['steps'][index]['fingerprint'], **{k: '' for k in _PROVENANCE})
        # Candidate hints are not chosen owners. Restrict their finite set to
        # this card without guessing a new owner or resolving an ambiguity.
        row['subtitle']['association_candidates'] = [p for p in row['subtitle']['association_candidates']
            if p in destination and destination[p]['kind'] == 'video']
        # Share the ordinary subtitle relation projection, including the
        # unresolved/conflict state normalization used by local undo.
        from .content_associations import project_subtitle_contract
        from .rename_plan import SubtitlePlanContract
        row['subtitle'] = project_subtitle_contract(SubtitlePlanContract.from_dict(row['subtitle']), row['relation']).to_dict()
        _material({k: row[k] for k in ('subtitle', 'requires_review', 'evidence')}, row['relation'], path, destination)
    return rows


def _validate_selection(value):
    """Common content/static verifier, with no permission to mint v3."""
    inherited = InheritedSubtitleEdits.from_mapping(value['inherited_subtitle_edits']).to_dict()
    if value['origin_kind'] != ORIGIN or value['partition_origin'] != inherited['partitions'][-1]:
        raise ValueError('有限字幕内容出生来源不一致')
    basis_fp = inherited['partitions'][-1]['destination_context']
    basis = inherited['contexts'][basis_fp]
    sources = _sources(basis)
    if (value['initial_source_basis_fingerprint'] != basis_fp or value['task_id'] != basis['task_id']
            or value['origin_plan_fingerprint'] != value['partition_origin']['parent_plan_fingerprint']
            or value['source_root'] != basis['source_root']
            or value['scope_fingerprint'] != _digest(basis['task_source_scope'])
            or {row['source_path']: row for row in value['identities']} != {p: _identity(r) for p, r in sources.items()}
            or {row['source_path'] for row in value['materials']} != sources.keys()):
        raise ValueError('有限字幕内容未绑定目的卡完整basis/身份/scope')
    if 'inherited_movie_groups' in value:
        from .movie_version_selection import MovieGroupFacts, validate_group_facts
        groups = value['inherited_movie_groups']
        _fields(groups, {'origin_content_fingerprint', 'groups'}, '电影来源组')
        if (groups['origin_content_fingerprint'] != value['partition_origin']['parent_content_fingerprint']
                or not isinstance(groups['groups'], list) or not groups['groups']):
            raise ValueError('电影来源组缺少完整出生锚点或来源')
        checked = validate_group_facts(groups['groups'],
            video_sources={row['source_path'] for row in value['materials'] if row['media_kind'] == 'video'})
        if (type(checked) is not MovieGroupFacts or checked.payload_json != encode(groups['groups'])
                or checked.video_sources != frozenset(row['source_path'] for row in value['materials'] if row['media_kind'] == 'video')):
            raise ValueError('继承电影来源组共享验证已断开')
    expected = project(inherited)
    relations = {row['source_path']: row for row in value['associations']['relations']}
    inherited_origins = {(step['origin']['rule_id'], step['origin']['rule_fingerprint']) for step in inherited['steps']}
    for material in value['materials']:
        subtitle, path = material['subtitle'], material['source_path']
        if subtitle is None:
            if path in expected:
                raise ValueError('有限字幕受影响来源不能清除必需字幕材料')
            continue
        ref = subtitle.get('inherited_edit_fingerprint', '')
        local = subtitle['decision_source'] in _LOCAL
        if local and not ref and all(subtitle[k] for k in _PROVENANCE):
            if (subtitle['applied_rule_id'], subtitle['applied_rule_fingerprint']) in inherited_origins:
                raise ValueError('父字幕规则不能原样复制成本卡规则')
            continue  # The existing Plan rule verifier must validate local rules.
        if path in expected:
            row = expected[path]
            if (any(material[k] != row[k] for k in ('subtitle', 'requires_review', 'evidence'))
                    or relations.get(path) != row['relation']):
                raise ValueError('有限字幕当前材料/关联与唯一末态投影不符')
        elif ref or subtitle['decision_source'] == 'manual_transfer':
            raise ValueError('有限字幕引用扩张到未登记来源')
    return expected


@dataclass(frozen=True)
class InheritedSelectionProof:
    payload_json: str
    expected_json: str = field(init=False)

    def __post_init__(self):
        value = json.loads(self.payload_json)
        if encode(value) != self.payload_json:
            raise ValueError('有限字幕验证对象必须为规范冻结内容')
        object.__setattr__(self, 'expected_json', encode(_validate_selection(value)))


def validate_selection(value):
    return InheritedSelectionProof(encode(value))


@dataclass(frozen=True)
class InheritanceWriteCheck:
    previous: object
    candidate: object


@dataclass(frozen=True)
class InheritedPlanProof:
    task_id: str
    content_fingerprint: str
    contracts: tuple


def validate_plan(plan):
    from .content_selection import from_plan, require_plan_binding
    from .rename_plan import SubtitlePlanContract
    choice = from_plan(plan)
    from .source_decision_transfer import ORIGIN, validate_selection as validate_regrouped
    if choice is not None and choice.to_dict().get('origin_kind') == ORIGIN:
        require_plan_binding(plan, choice)
        proof = validate_regrouped(choice.to_dict())
        contracts = tuple((Path(row['source_path']), SubtitlePlanContract.from_dict(row['subtitle']))
            for row in choice.to_dict()['materials'] if row['subtitle'] is not None
            and row['subtitle']['decision_source'] == 'manual_transfer')
        if proof.to_dict() != choice.to_dict()['source_decision_transfer']:
            raise ValueError('拆卡字幕验证已断开')
        return InheritedPlanProof(plan.task_id, choice.fingerprint, contracts)
    if choice is None or choice.to_dict()['policy'] != POLICY:
        return None
    require_plan_binding(plan, choice)
    value = choice.to_dict()
    expected = validate_selection(value)
    if type(expected) is not InheritedSelectionProof or expected.payload_json != choice.payload_json:
        raise ValueError('有限字幕材料验证已断开')
    contracts = tuple((Path(row['source_path']), SubtitlePlanContract.from_dict(row['subtitle']))
        for row in value['materials'] if row['subtitle'] is not None
        and row['subtitle']['decision_source'] == 'manual_transfer')
    return InheritedPlanProof(plan.task_id, choice.fingerprint, contracts)


def require_unchanged(previous, candidate):
    """No publisher/undo issuer exists in this static-contract stage."""
    from .content_selection import ContentSelection
    if (previous is not None and type(previous) is not ContentSelection
            or candidate is not None and type(candidate) is not ContentSelection):
        raise ValueError('有限字幕普通提交必须使用完整内容合同')
    old = previous.to_dict() if previous is not None else None
    new = candidate.to_dict() if candidate is not None else None
    from .source_decision_transfer import ORIGIN, FIELD
    if old is not None and (old.get('origin_kind') == ORIGIN or new is not None and new.get('origin_kind') == ORIGIN):
        if new is None or old.get(FIELD) != new.get(FIELD) or old.get('origin_kind') != new.get('origin_kind'):
            raise ValueError('拆卡出生决定不能在后续编辑中替换或丢失')
    if old is not None and old['policy'] == POLICY or new is not None and new['policy'] == POLICY:
        if (old is None or new is None or old['policy'] != POLICY or new['policy'] != POLICY
                or any(old[key] != new[key] for key in FIELDS)
                or old.get('inherited_movie_groups') != new.get('inherited_movie_groups')):
            raise ValueError('有限字幕交接/撤销尚未获得实际发布权限，不能改写出生事实或游标')
    return InheritanceWriteCheck(previous, candidate)
