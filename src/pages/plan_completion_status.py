"""Display the completion of the current approved media plan."""
from ..rename.replacement_completion import has_completed_media
from ..rename.plan_store import PlanStore


def completion_status(plan, task):
    if plan is None:
        return ''
    fingerprint = PlanStore.fingerprint(plan)
    if task.get('execution_fingerprint') != fingerprint or task.get('plan_fingerprint') != fingerprint:
        return ''
    if (not has_completed_media(task) or task.get('execution_requires_manual_check')
            or task.get('execution_may_have_written_targets') or task.get('asset_rollback_phase')):
        return '整理结果待核查' if task.get('execution_status') else ''
    if task.get('execution_phase') in {'replacement_finalizing', 'replacement_cleanup_failed'}:
        return '媒体已完成 · 换源待收尾'
    return '整理完成'


def entry_status(plan, task):
    status = completion_status(plan, task)
    return '已整理' if status and status != '整理结果待核查' else status
