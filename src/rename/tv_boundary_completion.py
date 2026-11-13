"""Complete a frozen TV boundary command without Provider I/O or legacy writers."""
from dataclasses import dataclass, replace
import json
import hashlib

from .content_associations import encode
from .pending_tv_selection import PendingTVSelection
from .review_operation import (ReviewCommand, TaskWorkflowState, build_review_operation,
    current_provider_selection_review_operation, finish_provider_selection)
from .provider_link_decision import (resolve_provider_subject_boundary_decision,
    selected_provider_special_observation, provider_subject_cohort_request_fingerprint)
from .tv_completion_observations import FrozenTVResolution, TVSeasonObservation


@dataclass(frozen=True)
class BoundaryResolution:
    recognition: object
    resolution: FrozenTVResolution
    seasons: TVSeasonObservation
    special_json: str
    boundary_json: str
    title_json: str
    provider_json: str
    command: ReviewCommand
    proof: PendingTVSelection
    request_fingerprint: str


def receipt_fingerprint(plan):
    raw = plan.metadata.get('tv_boundary_completion_receipt')
    if raw is None:
        return None
    value = json.loads(encode(raw))
    if (set(value) != {'schema_version', 'pending_fingerprint', 'command', 'request_id',
            'request_fingerprint', 'result_decision_revision'} or value['schema_version'] != 1
            or type(value['result_decision_revision']) is not int or value['result_decision_revision'] < 1
            or not value['request_id']):
        raise ValueError('季度完成凭据格式无效')
    ReviewCommand.from_mapping(value['command'])
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def resolve(service, current, state, raw_command):
    """合包写入口已退役，历史数据只供读取。"""
    raise ValueError('合包功能已退役，请按单条目重新整理；原任务保留')


def decorate(current, state, built, inputs, resolved):
    """合包写入口已退役，历史数据只供读取。"""
    raise ValueError('合包功能已退役，请按单条目重新整理；原任务保留')


def validate_inputs(service, current, state, inputs):
    """合包写入口已退役，历史数据只供读取。"""
    raise ValueError('合包功能已退役，请按单条目重新整理；原任务保留')


def prepare(service, task_id, fingerprint, *, review_command, affiliation_choice=None):
    """合包写入口已退役，历史数据只供读取。"""
    raise ValueError('合包功能已退役，请按单条目重新整理；原任务保留')


def commit(service, intent):
    """合包写入口已退役，历史数据只供读取。"""
    raise ValueError('合包功能已退役，请按单条目重新整理；原任务保留')


def validate_terminal(previous, payload, decision, plan):
    if plan.revision_action not in {'tv_content_boundary', 'tv_boundary_cancel'}:
        return
    old, old_request = current_provider_selection_review_operation(previous)
    active, request = current_provider_selection_review_operation(payload)
    expected = 'cancelled' if plan.revision_action == 'tv_boundary_cancel' else 'succeeded'
    if (old_request.get('lifecycle') != 'awaiting_user' or request.get('lifecycle') != expected
            or request.get('request_id') != old_request.get('request_id')
            or request.get('result_decision_revision') != decision.revision
            or active.commands or active.state != TaskWorkflowState.RESOLVED
            or decision.pending_tv_selection is not None):
        raise ValueError('季度确认或取消的任务终态与本次事务不一致')
