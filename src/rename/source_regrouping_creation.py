"""Small confirmed replacement: new pending cards, never media execution.

Before publication ordinary failures clean this attempt. The retired old card
can be safely abandoned and sources re-added; no automatic rollback promise.
"""
import uuid
import json
from dataclasses import replace

from . import input_directory_discovery as discovery
from .content_policy import policy_for_admission
from .file_lock import task_operation_lock
from .plan_review import PlanReviewService
from .source_basis import _exact_layout_root, _manifest_request_layout
from .source_regrouping import SourceRegroupingPreview, observe_current, validate_preview
from .task_batch_scope import BatchTaskSourceRequest, TaskSourceScope
from ..utils.utils import safe_task_path


def create(manager, preview):
    if type(preview) is not SourceRegroupingPreview:
        raise ValueError('缺少完整分组预览，请重新打开')
    value = preview.to_dict()
    task_id = value['origin_task_id']
    with task_operation_lock(manager.task_root, task_id, operation='source_regrouping'), \
            manager.execution_service.recovery_transaction_store.lock(task_id):
        try:
            return _create_locked(manager, preview, value)
        finally:
            from ..utils.task_changes import mark_changed
            mark_changed()


def _create_locked(manager, preview, value):
    from .recovery_transaction import require_no_pending_detach
    from ..task_queue import utc_now

    task_id = value['origin_task_id']
    require_no_pending_detach(plan_store=manager.plan_store, task_root=manager.task_root,
        decision_root=manager.decision_state_store.root, task_id=task_id)
    groups = [g['sources'] for g in value['groups']]
    selected = value.get('selected_entry_ids')
    final_excluded = value.get('final_excluded_sources')
    before = observe_current(manager, task_id, value['plan_fingerprint'])
    if validate_preview(preview, before[1], before[2], groups if selected is None else None, selected, final_excluded) is not preview:
        raise ValueError('当前分组已变化，请重新预览并核对')
    if final_excluded is not None:
        from .content_choice import prepare_change, commit
        from .content_associations import source_key
        from .plan_store import PlanStore
        from .source_regrouping import project_preview
        import hashlib
        execution = manager.execution_service
        service = PlanReviewService(manager.plan_store, manager.review_root,
            catalog_root=execution.work_catalog.root, decision_state_root=manager.decision_state_store.root,
            task_root=manager.task_root, record_root=execution.record_root,
            provider_link_catalog_root=execution.provider_link_catalog.root,
            root_snapshot_source=manager.root_snapshot_source)
        service.series_catalog = execution.series_catalog
        current = before[1]
        old = set(before[2].content_selection.to_dict()['excluded_sources'])
        final = set(final_excluded)
        for action, paths in (('restore', old - final), ('exclude', final - old)):
            if paths:
                identifiers = tuple(hashlib.sha256(source_key(p).encode()).hexdigest()[:20] for p in sorted(paths))
                change = prepare_change(service, task_id, PlanStore.fingerprint(current),
                    selected_entry_ids=identifiers, action=action, reason='确认拆卡来源范围')
                current = commit(service, change)
        before = observe_current(manager, task_id, PlanStore.fingerprint(current))
        preview = project_preview(before[1], before[2], groups)
        validate_preview(preview, before[1], before[2], groups)
        value = preview.to_dict()
    task = json.loads(before[0])
    manager._require_recognition_retry_safe(task)
    old_scope = TaskSourceScope.from_mapping(task['task_source_scope'])
    identities = {i.source_path: i for i in old_scope.source_identities}
    prepared = []
    for group in preview.task_groups:
        new_id = str(uuid.uuid4())
        paths = tuple(group['sources'])
        observed = discovery.SourceGroup(path=_exact_layout_root(paths), input_scope=paths,
            scope_kind='exact_file', identities=tuple(identities[p] for p in paths),
            reason='拆卡后请核对本组来源，再选择或识别作品')
        discovery.validate_group_sources(observed.to_dict(), observed.identities)
        request = policy_for_admission(manager, BatchTaskSourceRequest(task_id=new_id,
            input_scope=paths, scope_kind='exact_file', source_identities=observed.identities))
        prepared.append((request, observed, group['label']))
    ids = tuple(r.task_id for r, _, _ in prepared)
    if len(set(ids)) != len(ids) or task_id in ids:
        raise ValueError('新任务编号冲突，请重新打开')
    if observe_current(manager, task_id, value['plan_fingerprint']) != before:
        raise ValueError('准备期间原任务已变化，请重新打开')

    batch_id = 'regrouping-' + uuid.uuid4().hex
    # From here failures may retire the old admission. The ordinary safe-end
    # entry deliberately remains available; do not recreate its old authority.
    try:
        manager._retire_task_authorities(task_id, reason='confirmed-source-regrouping')
        manifest = manager.batch_scope_store.reserve_batch(batch_id=batch_id,
            task_requests=tuple(r for r, _, _ in prepared))
        scopes = {s.task_id: s for s in manifest.task_source_scopes}
        payloads = {}
        for request, observed, label in prepared:
            scope = scopes[request.task_id]
            _, _, observation_path = _manifest_request_layout(manager.batch_scope_store, manifest, scope)
            observed = replace(observed, path=observation_path)
            payload = dict(uuid=request.task_id, path=str(observed.path), created_at=utc_now(),
                is_anime=None, is_movie=None, name=label, season_id=None, season_number=None,
                season_source='unset', use_ai=None, provider_selection=None,
                task_source='regrouping', preview_only=False, workflow_status='need_confirm',
                queue_position=None, batch_id=batch_id, batch_scope_fingerprint=scope.batch_scope_fingerprint,
                reservation_lineage_id=scope.reservation_lineage_id,
                reservation_revision=scope.reservation_revision, task_source_scope=scope.to_dict(),
                source_group_observation=observed.to_dict(), source_group_pending=True,
                queue_operation='await_source_confirmation', error=None)
            from .source_decision_transfer import prepare, FIELD, TASK_SOURCE
            transfer = prepare(before[1], before[2], scope)
            payload.update(source_decision_transfer_policy=TASK_SOURCE, **{FIELD: transfer.to_dict()})
            discovery.validate_pending_group(payload, scope)
            manager._write(request.task_id, payload)
            payloads[request.task_id] = payload
        # The store transition alone checks IDs, not durable task contents.
        for new_id, payload in payloads.items():
            if manager._read(new_id) != payload:
                raise ValueError('新任务未完整保存，原卡不能移除')
        manifest = manager.batch_scope_store.mark_tasks_enqueued(batch_id,
            expected_revision=manifest.revision, task_ids=ids)
        manager.batch_scope_store.activate_batch(batch_id, expected_revision=manifest.revision)
        for new_id in ids:
            manager.batch_scope_store.require_active_scope(scopes[new_id], batch_id=batch_id)
    except (ValueError, RuntimeError, OSError) as exc:
        # Only this new batch/these fresh IDs belong to this attempt.
        if manager.batch_scope_store.path_for(batch_id).exists():
            manager.batch_scope_store.fail_closed(batch_id)
        for new_id in ids:
            safe_task_path(manager.task_root, new_id).unlink(missing_ok=True)
        raise RuntimeError('拆卡未完成；请关闭窗口，安全移除原任务后重新添加来源。' + str(exc)) from exc

    # All new cards are durable/active now. If removing old UI records fails,
    # keep these ready cards and explicitly ask for safe removal of the old one.
    try:
        PlanReviewService(manager.plan_store, manager.review_root).delete(task_id)
        manager.plan_store.delete_current(task_id)
        safe_task_path(manager.task_root, task_id).unlink()
    except (ValueError, RuntimeError, OSError) as exc:
        raise RuntimeError(f'已生成 {len(ids)} 张新卡，但旧卡移除失败；请回任务列表核对新卡并安全移除旧卡。' + str(exc)) from exc
    return ids
