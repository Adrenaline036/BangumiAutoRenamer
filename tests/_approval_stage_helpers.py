"""Await the real GUI approval callback in older headless dialog fixtures."""
from unittest.mock import MagicMock
from src.pages.plan_review_page import PlanReviewDialog, build_plan_review_snapshot


async def approve_dialog(owner, selected_ids=None):
    from src.rename.plan_review import entry_id
    if getattr(owner, 'task_decision', None) is None:
        owner.task_decision = owner.review_service.decision_state_store.load_optional(owner.plan.task_id)
    if getattr(owner, 'snapshot', None) is None:
        owner.snapshot = build_plan_review_snapshot(owner.plan)
    for name in ('approve_selected_button', 'approve_all_button', 'execute_button'):
        if getattr(owner, name, None) is None:
            setattr(owner, name, MagicMock())
    await PlanReviewDialog._approve(owner, tuple(entry_id(e) for e in owner.plan.entries)
                                   if selected_ids is None else selected_ids)
