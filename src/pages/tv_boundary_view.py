"""已确认的季度命令在后台完成准备和保存。"""
from ..rename.plan_store import PlanStore
from ..rename.work_affiliation import AffiliationRequired
from .tv_content_actions import revision_owner_alive, run_revision


async def confirm(owner, command, *, ui, notify):
    if getattr(owner, '_affiliation_open', False):
        return
    revision, fingerprint = owner.task_decision.revision, PlanStore.fingerprint(owner.plan)
    service, task_id = owner.review_service, owner.plan.task_id
    command = dict(command)

    def operation():
        intent = service.prepare_tv_boundary_completion(task_id, fingerprint, review_command=command)
        return service.commit_tv_boundary_completion(intent)

    async def failed(exc):
        if isinstance(exc, AffiliationRequired):
            from .work_affiliation_view import show
            await show(owner, correction_inputs=exc.inputs, ui=ui, notify=notify,
                       is_current=lambda: revision_owner_alive(owner)
                       and owner.task_decision.revision == revision
                       and PlanStore.fingerprint(owner.plan) == fingerprint)
        else:
            notify(str(exc), type='negative')

    await run_revision(owner, submit=operation, expected_revision=revision, expected_fingerprint=fingerprint,
        notify=notify, on_error=failed, message='季度关系已确认，原内容取舍已保留。',
        started_message='正在后台生成季度整理计划；关闭窗口后继续。')
