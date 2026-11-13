"""拆卡出生决定：逐文件冻结、首建核对历史，后续只消费本卡决定。"""
from dataclasses import dataclass, replace
import json
from pathlib import Path

from .content_associations import encode
from .content_selection import digest
from .task_batch_scope import TaskSourceScope

ORIGIN = 'regrouped-source-decisions-v1'
TASK_SOURCE = 'regrouping_with_decisions'
FIELD = 'source_decision_transfer'
_PROVENANCE = ('applied_rule_id', 'applied_rule_fingerprint', 'applied_rule_scope', 'applied_rule_engine_version')


@dataclass(frozen=True)
class SourceDecisionTransfer:
    payload_json: str

    def __post_init__(self):
        from .content_selection import _HASH
        from .rename_plan import SubtitlePlanContract
        try:
            v = self.to_dict()
            fields = {'policy', 'parent_task_id', 'parent_plan_fingerprint', 'parent_decision_fingerprint',
                      'child_scope', 'identities', 'subtitles', 'excluded_sources'}
            scope = TaskSourceScope.from_mapping(v['child_scope'])
            paths = {i.source_path for i in scope.source_identities}
            if (set(v) != fields or v['policy'] != ORIGIN or encode(v) != self.payload_json
                    or not isinstance(v['parent_task_id'], str) or v['parent_task_id'] == scope.task_id
                    or any(not isinstance(v[k], str) or _HASH.fullmatch(v[k]) is None
                           for k in ('parent_plan_fingerprint', 'parent_decision_fingerprint'))
                    or v['identities'] != identities(scope) or not paths or len(paths) > 10000):
                raise ValueError('拆卡决定的来源与子卡范围不一致')
            for key in ('excluded_sources',):
                if not isinstance(v[key], list) or v[key] != sorted(set(v[key])) or not set(v[key]) <= paths:
                    raise ValueError('拆卡决定越过本卡来源')
            if not isinstance(v['subtitles'], dict) or not v['subtitles'].keys() <= paths:
                raise ValueError('拆卡字幕来源越界')
            for path, raw in v['subtitles'].items():
                sub = SubtitlePlanContract.from_dict(raw)
                if (sub.to_dict() != raw or sub.decision_source not in {'manual_file', 'manual_series', 'manual_transfer'}
                        or sub.associated_video_source_path is not None and str(sub.associated_video_source_path) not in paths
                        or any(str(p) not in paths for p in sub.association_candidates)):
                    raise ValueError('拆卡字幕决定或关联越界')
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError('拆卡出生决定缺失或无效') from exc

    def to_dict(self):
        return json.loads(self.payload_json)

    @classmethod
    def from_mapping(cls, value):
        return cls(encode(value))

    @property
    def fingerprint(self):
        return digest(self.to_dict())

    def contracts(self):
        from .rename_plan import SubtitlePlanContract
        return {Path(p): replace(SubtitlePlanContract.from_dict(raw),
            decision_source='manual_transfer', inherited_edit_fingerprint=digest({'transfer': self.fingerprint, 'path': p}),
            **{k: '' for k in _PROVENANCE}) for p, raw in self.to_dict()['subtitles'].items()}


def identities(scope):
    return [{k: row[k] for k in ('source_path', 'physical_key', 'size', 'mtime_ns')}
            for row in sorted((i.to_dict() for i in scope.source_identities), key=lambda r: r['source_path'])]


def prepare(plan, state, scope):
    from .content_selection import validate_plan_content, material_entries, omitted_sources
    from .decision_state import DecisionStateStore
    from .plan_store import PlanStore
    choice = validate_plan_content(plan, state)
    if choice is None:
        raise ValueError('拆卡缺少原任务内容决定')
    paths = {i.source_path for i in scope.source_identities}
    old = {i['source_path']: i for i in choice.to_dict()['identities']}
    if not paths <= old.keys() or any(old[row['source_path']] != row for row in identities(scope)):
        raise ValueError('拆卡来源必须在原范围内且保留物理身份')
    entries = [e for e in material_entries(plan, choice) if str(e.source_path) in paths]
    subtitles = {str(e.source_path): e.subtitle.to_dict() for e in entries if e.subtitle is not None
                 and e.subtitle.decision_source in {'manual_file', 'manual_series', 'manual_transfer'}}
    return SourceDecisionTransfer.from_mapping(dict(policy=ORIGIN, parent_task_id=plan.task_id,
        parent_plan_fingerprint=PlanStore.fingerprint(plan), parent_decision_fingerprint=DecisionStateStore.fingerprint(state),
        child_scope=scope.to_dict(), identities=identities(scope), subtitles=subtitles,
        excluded_sources=sorted({str(p) for p in omitted_sources(choice, state.movie_version_selection)} & paths)))


def require_task(proof, task):
    required = task.get('source_decision_transfer_policy') == TASK_SOURCE
    if required:
        if type(proof) is not SourceDecisionTransfer or proof.to_dict() != task.get(FIELD):
            raise ValueError('拆卡首建来源决定缺失或生产链已断开')
    elif proof is not None or task.get(FIELD) is not None:
        raise ValueError('拆卡出生决定与任务策略不符')
    return proof


def load(service, task_id):
    from ..utils.utils import read_task_at
    from .decision_state import DecisionStateStore
    task = read_task_at(service.task_root, task_id)
    raw = task.get(FIELD)
    if task.get('source_decision_transfer_policy') != TASK_SOURCE:
        if raw is not None:
            raise ValueError('拆卡决定不能混入旧任务来源')
        return None
    proof = SourceDecisionTransfer.from_mapping(raw)
    v = proof.to_dict()
    scope = TaskSourceScope.from_mapping(task['task_source_scope'])
    birth = TaskSourceScope.from_mapping(v['child_scope'])
    states = getattr(service, 'decision_state_store', None) or service.state_store
    parent = states.load_readonly(v['parent_task_id'])
    plan = service.plan_store.load_version(v['parent_task_id'], v['parent_plan_fingerprint'])
    # 类型重绑定不改变逐源决定；当前活动范围仍由首建与提交门验证。
    if (scope.task_id != task_id or birth.task_id != task_id
            or scope.source_identity_set_fingerprint != birth.source_identity_set_fingerprint
            or scope.content_policy != birth.content_policy
            or DecisionStateStore.fingerprint(parent) != v['parent_decision_fingerprint']
            or prepare(plan, parent, birth) != proof):
        raise ValueError('拆卡出生决定与实际父历史或子卡范围不一致')
    return proof


def apply(entries, proof):
    subs = proof.contracts()
    return tuple(replace(e, subtitle=subs[e.source_path], requires_review=False)
                 if e.source_path in subs else e for e in entries)


def validate_selection(value):
    proof = SourceDecisionTransfer.from_mapping(value[FIELD])
    v = proof.to_dict()
    if (v['child_scope']['task_id'] != value['task_id']
            or v['identities'] != sorted(value['identities'], key=lambda r: r['source_path'])):
        raise ValueError('拆卡出生决定与本卡内容身份不一致')
    expected = proof.contracts()
    for row in value['materials']:
        raw, path = row['subtitle'], Path(row['source_path'])
        if raw is not None and raw['decision_source'] in {'manual_file', 'manual_series'} and not raw.get('inherited_edit_fingerprint'):
            continue  # 当前卡的人工规则由共享字幕核查器验证。
        if path in expected and raw != expected[path].to_dict():
            raise ValueError('拆卡字幕没有保留冻结人工决定')
        if raw is not None and (raw['decision_source'] == 'manual_transfer' or raw.get('inherited_edit_fingerprint')) and path not in expected:
            raise ValueError('拆卡字幕引用扩张到未登记来源')
    return proof
