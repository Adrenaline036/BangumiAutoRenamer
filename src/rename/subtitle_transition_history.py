"""Read exact signed subtitle revisions into finite facts; never publish them."""
from dataclasses import dataclass
import json

from . import content_selection as content
from .content_associations import encode
from .plan_store import PlanStore
from .rename_plan import RenamePlan


@dataclass(frozen=True)
class VerifiedSubtitleTransition:
    """An immutable observation, not an independently trusted authority."""

    payload_json: str

    def __post_init__(self):
        value = json.loads(self.payload_json)
        if (not isinstance(value, dict) or set(value) != {'origin', 'effects'}
                or encode(value) != self.payload_json or not isinstance(value['origin'], dict)
                or set(value['origin']) != {'task_id', 'base_plan_fingerprint', 'post_plan_fingerprint',
                    'rule_id', 'rule_fingerprint', 'rule_index'}
                or not isinstance(value['effects'], list) or not value['effects']):
            raise ValueError('字幕逐步历史观察格式无效')

    def to_dict(self):
        return json.loads(self.payload_json)


@dataclass(frozen=True)
class SubtitleTransitionHistory:
    task_id: str
    plan_fingerprint: str
    transitions: tuple[VerifiedSubtitleTransition, ...]
    records: tuple


def _observe(plan_store, current, base_history):
    from .partition_subtitle_history import SubtitleHistory, read_subtitle_history, _read
    from .projection_authority import validate_plan_projection_receipt
    from .content_choice import subtitle_revision_materials_from_store
    from .movie_version_selection import MovieVersionSelection
    if (type(plan_store) is not PlanStore or type(current) is not RenamePlan
            or type(base_history) is not SubtitleHistory
            or base_history != read_subtitle_history(plan_store, current)):
        raise ValueError('字幕逐步历史缺少实际同源base证明')
    current_fp = plan_store.fingerprint(current)
    rules = current.subtitle_override_rules
    if not rules:
        return SubtitleTransitionHistory(current.task_id, current_fp, (), ())
    choice = content.validate_plan_content(current)
    if choice is None:
        raise ValueError('字幕逐步历史缺少完整冻结内容来源')
    current_value = choice.to_dict()
    oldest = rules[0].base_plan_fingerprint
    cursor_fp, chain, records = current_fp, {}, []
    for _ in range(256):
        if cursor_fp in chain:
            raise ValueError('字幕实际历史链出现循环')
        path = plan_store._history_path(current.task_id, cursor_fp)
        _, record = _read(path)
        plan = plan_store.load_version(current.task_id, cursor_fp)
        if (type(plan) is not RenamePlan or plan_store.fingerprint(plan) != cursor_fp
                or plan.task_id != current.task_id or plan.source_root != current.source_root):
            raise ValueError('字幕实际历史版本身份或布局根不符')
        validate_plan_projection_receipt(plan)
        old_choice = content.validate_plan_content(plan)
        if old_choice is None:
            raise ValueError('字幕实际历史缺少完整内容材料')
        value = old_choice.to_dict()
        if any(value[key] != current_value[key] for key in ('identities', 'scope_fingerprint')):
            raise ValueError('字幕实际历史跨越来源身份或scope')
        movie_raw = plan.metadata.get('movie_version_selection')
        movie = MovieVersionSelection.from_mapping(movie_raw) if movie_raw is not None else None
        content.validate_omission_manifest(old_choice, movie_selection=movie)
        if _read(path)[1] != record:
            raise ValueError('字幕实际历史在解析期间变化')
        chain[cursor_fp] = (plan, value)
        records.append(record)
        if cursor_fp == oldest:
            break
        cursor_fp = plan.revision_parent_fingerprint
        if not cursor_fp:
            raise ValueError('字幕实际历史未连接到最早规则base')
    else:
        raise ValueError('字幕实际历史超出256个版本的核对范围')

    from .subtitle_batch import rule_batches
    groups = {i: (start, end) for start, end in rule_batches(rules) for i in range(start, end)}
    transitions = []
    for index, rule in enumerate(rules):
        candidates = [(fp, plan, value) for fp, (plan, value) in chain.items()
                      if plan.revision_parent_fingerprint == rule.base_plan_fingerprint]
        if rule.base_plan_fingerprint not in chain or len(candidates) != 1:
            raise ValueError('字幕规则没有唯一实际直接postimage')
        base, before = chain[rule.base_plan_fingerprint]
        post_fp, post, after = candidates[0]
        start, end = groups[index]
        action = f'apply_subtitle_rule:{rule.rule_id}' if end - start == 1 else 'apply_subtitle_batch'
        if (base.subtitle_override_rules != rules[:start] or post.subtitle_override_rules != rules[:end]
                or post.revision_action not in ({action, 'apply_subtitle_batch'} if end - start == 1 else {action})
                or post.revision_reason != rule.reason
                or after['parent_plan_fingerprint'] != rule.base_plan_fingerprint
                or after['reason'] != rule.reason):
            raise ValueError('字幕直接postimage不是对应规则追加修订')
        allowed = {'materials', 'associations', 'parent_plan_fingerprint', 'reason', 'evidence'}
        if set(before) != set(after) or any(before[k] != after[k] for k in before.keys() - allowed):
            raise ValueError('字幕直接postimage夹带其它内容决定')
        expected = subtitle_revision_materials_from_store(plan_store, base, post.subtitle_override_rules,
                                                         after['associations'])
        if expected != (after['materials'], after['associations']):
            raise ValueError('字幕直接postimage未使用唯一规则重算')
        old_evidence = {r['source_path']: r for r in before['evidence']}
        new_evidence = {r['source_path']: r for r in after['evidence']}
        if any(old_evidence[p] != new_evidence[p] and new_evidence[p] != {'source_path': p, 'kind': 'omission-v1'}
               for p in old_evidence.keys() & new_evidence.keys()):
            raise ValueError('字幕直接postimage改写了已有冻结来源证据')
        old_materials = {r['source_path']: r for r in before['materials']}
        identities = {r['source_path']: r for r in before['identities']}
        old_relations = {r['source_path']: r for r in before['associations']['relations']}
        new_relations = {r['source_path']: r for r in after['associations']['relations']}
        effects = []
        for material in after['materials']:
            path = material['source_path']
            if end - start > 1 and (material.get('subtitle') or {}).get('applied_rule_id') != rule.rule_id:
                continue
            original = old_materials[path]
            if material == original and old_relations.get(path) == new_relations.get(path):
                continue
            if (material['media_kind'] != 'subtitle' or path not in old_relations or path not in new_relations):
                raise ValueError('字幕规则修改了非字幕来源')
            effects.append(dict(source_identity=identities[path],
                before={k: original[k] for k in ('subtitle', 'requires_review', 'evidence')},
                after={k: material[k] for k in ('subtitle', 'requires_review', 'evidence')},
                before_relation=old_relations[path], after_relation=new_relations[path]))
        transitions.append(VerifiedSubtitleTransition(encode(dict(origin=dict(task_id=current.task_id,
            base_plan_fingerprint=rule.base_plan_fingerprint, post_plan_fingerprint=post_fp,
            rule_id=rule.rule_id, rule_fingerprint=rule.rule_fingerprint, rule_index=index), effects=effects))))
    for record in records:
        if _read(record[0])[1] != record:
            raise ValueError('字幕逐步历史在完整核对后变化')
    return SubtitleTransitionHistory(current.task_id, current_fp, tuple(transitions), tuple(records))


def observe(plan_store, current, base_history):
    return _observe(plan_store, current, base_history)


def validate(plan_store, current, base_history, proof):
    if type(proof) is not SubtitleTransitionHistory or proof != _observe(plan_store, current, base_history):
        raise ValueError('字幕逐步历史证明或完整依赖集合已变化')
    return proof
