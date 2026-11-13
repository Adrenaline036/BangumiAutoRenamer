"""First-link GUI transport for the shared default workspace producer."""
from ..queue_runtime import ui_disk_call


async def create_initial_workspace(task_id, task_data, *, media_type=None):
    from . import plan_review_page as page
    from .add_task_page import media_flags
    from ..rename.target_routing import snapshot_from_config_manager
    from ..rename.default_activation import ensure_workspace
    from ..rename.plan_review import PlanReviewService
    from ..rename.initial_movie_completion import RootSnapshotSource
    current=page.get_task(task_id)
    if not current or current != task_data:
        raise page.InventoryDraftError('任务已变化，请重新打开整理页面')
    page.task_queue._require_recognition_retry_safe(current)
    if current.get('workflow_status') in {'queued','processing','executing','completed'}:
        raise page.InventoryDraftError('任务仍在处理或已有完成记录，请等待或使用重建入口')
    observed=dict(current)
    if media_type is not None:
        try:
            anime,movie=media_flags(media_type)
        except (KeyError,TypeError):
            raise page.InventoryDraftError('请选择有效媒体类型') from None
        observed.update(is_anime=anime,is_movie=movie)
    plans = page.PlanStore(page.PLAN_PATH)
    execution = page.task_queue.execution_service
    roots = RootSnapshotSource(lambda: snapshot_from_config_manager(page.cm))
    service = PlanReviewService(plans, plans.root.parent / 'plan_review',
        catalog_root=execution.work_catalog.root, decision_state_root=page.DECISION_STATE_PATH,
        task_root=page.TASK_PATH, record_root=execution.record_root,
        provider_link_catalog_root=execution.provider_link_catalog.root, root_snapshot_source=roots)
    service.series_catalog = execution.series_catalog
    return await ui_disk_call(ensure_workspace, service, task_id, roots=roots.observe(), observed_task=observed)
