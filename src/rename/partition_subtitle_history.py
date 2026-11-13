"""Read-only subtitle rule history for current plan transitions.

This is a repeatable observation, not an execution capability or a publication lease.
It never opens source media and never authorizes publishing either workspace.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import stat

from .path_safety import first_reparse_component
from .plan_store import PlanStore


def _read(path):
    path = Path(path)
    if first_reparse_component(path) is not None:
        raise ValueError('字幕历史前检不接受链接或reparse记录')
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('字幕历史前检记录必须是普通文件')
    raw = path.read_bytes()
    after = path.stat()
    def identity(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                value.st_nlink, value.st_mode, getattr(value, 'st_file_attributes', 0))
    if identity(before) != identity(after) or first_reparse_component(path) is not None:
        raise ValueError('字幕历史记录在读取期间变化')
    return raw, (str(path), hashlib.sha256(raw).hexdigest(), identity(after))


@dataclass(frozen=True)
class SubtitleHistory:
    task_id: str
    plan_fingerprint: str
    records: tuple


def read_subtitle_history(plan_store, current):
    """Read every actual local rule base, including overwritten predecessors."""
    from . import content_selection as content
    from .content_choice import subtitle_edit_view
    from .plan_review import entry_id, _subtitle_matcher
    from .plan_verifier import PlanVerifier
    from .projection_authority import validate_plan_projection_receipt
    from .rename_plan import RenamePlan, SubtitleOverrideScope
    if type(plan_store) is not PlanStore or type(current) is not RenamePlan:
        raise ValueError('字幕历史读取器缺少真实计划存储或计划')
    validate_plan_projection_receipt(current)
    choice = content.validate_plan_content(current)
    issues = []
    PlanVerifier()._verify_subtitle_rules(current, issues)
    if issues:
        raise ValueError('当前字幕规则不符合历史继承合同')
    from .subtitle_batch import rule_batches
    starts = {i: start for start, end in rule_batches(current.subtitle_override_rules) for i in range(start, end)}
    records = []
    for index, rule in enumerate(current.subtitle_override_rules):
        path = plan_store._history_path(current.task_id, rule.base_plan_fingerprint)
        _raw, record = _read(path)
        base = plan_store.load_version(current.task_id, rule.base_plan_fingerprint)
        if (type(base) is not RenamePlan or base.task_id != current.task_id
                or plan_store.fingerprint(base) != rule.base_plan_fingerprint
                or base.source_root != current.source_root
                or base.subtitle_override_rules != current.subtitle_override_rules[:starts[index]]):
            raise ValueError('字幕历史读取结果、归属或规则前缀不一致')
        validate_plan_projection_receipt(base)
        base_choice = content.validate_plan_content(base)
        if choice is not None:
            if base_choice is None or any(base_choice.to_dict()[key] != choice.to_dict()[key]
                    for key in ('identities', 'scope_fingerprint')):
                raise ValueError('字幕规则历史来源身份与当前材料不一致')
        selected = [e for e in subtitle_edit_view(base).entries
                    if e.subtitle is not None and entry_id(e) == rule.selected_entry_id]
        if len(selected) != 1 or _subtitle_matcher(selected[0].subtitle) != rule.matcher:
            raise ValueError('字幕规则的真实历史所选材料或匹配器不一致')
        if rule.scope == SubtitleOverrideScope.FILE:
            if rule.scope_id != f'{current.task_id}:{rule.selected_entry_id}:{rule.base_plan_fingerprint}':
                raise ValueError('文件字幕规则未绑定真实历史来源')
        elif base.series_scope is None or rule.scope_id != base.series_scope.scope_id:
            raise ValueError('系列字幕规则未绑定真实历史系列')
        if _read(path)[1] != record:
            raise ValueError('字幕历史在核验期间变化')
        records.append(record)
    return SubtitleHistory(current.task_id, plan_store.fingerprint(current), tuple(records))


