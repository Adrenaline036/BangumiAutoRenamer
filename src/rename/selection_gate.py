"""A failed or running replacement choice cannot authorize the retained Plan."""
from .review_operation import build_review_operation


def require_not_retired(task_id, plan_root, *, task_root, plan_fingerprint):
    """Retained history is readable, but cannot reacquire execution authority."""
    from .projection_authority import SourceOwnershipStore
    ownership = SourceOwnershipStore(plan_root.parent / 'source_ownership').load_optional(task_id)
    if ownership is None or ownership.state != 'active':
        raise ValueError('任务缺少有效活动来源权限；历史计划为只读，请创建新任务')
    from .current_plan_retirement import require_not_retired_plan
    require_not_retired_plan(task_id=task_id, plan_root=plan_root,
                            task_root=task_root, plan_fingerprint=plan_fingerprint)
    from .current_tv_refresh import require_completed_request
    require_completed_request(task_id=task_id, plan_root=plan_root,
                              task_root=task_root, plan_fingerprint=plan_fingerprint)


def validate_selection_for_plan(task):
    if not task:
        return  # Legacy component APIs have no task projection; Plan gates remain.
    try:
        operation, _ = build_review_operation(task)
        context = operation.context
    except ValueError as exc:
        raise ValueError('作品选择状态无法验证，请重新选择作品并生成计划') from exc
    if (context.get('kind') in {'legacy_provider_selection', 'invalid_current'}
            or 'review_operation_invalid' in operation.blocking_reasons
            or (context.get('kind') == 'provider_selection' and context.get('lifecycle') != 'succeeded')):
        raise ValueError('作品选择尚未成功，旧计划仅供查看；请完成选择后重新生成并批准计划')
