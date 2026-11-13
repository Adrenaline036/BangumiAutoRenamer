"""历史格式构造仅用于隔离测试，不是生产兼容写入口。"""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from src.rename.source_partition import SourcePartitionDecision
from src.rename.plan_review import PlanReviewError, entry_id, provider_special_entry_closure

def new_partition_decision(
    *,
    parent_task_id: str,
    child_task_id: str,
    expected_parent_plan_fingerprint: str,
    expected_parent_batch_id: str,
    expected_parent_scope_fingerprint: str,
    expected_parent_reservation_revision: int,
    selected_source_identities: tuple[SourceIdentity, ...],
    reason: str,
) -> SourcePartitionDecision:
    return SourcePartitionDecision(
        partition_id=f"partition-{uuid.uuid4().hex}",
        parent_task_id=parent_task_id,
        child_task_id=child_task_id,
        expected_parent_plan_fingerprint=expected_parent_plan_fingerprint,
        expected_parent_batch_id=expected_parent_batch_id,
        expected_parent_scope_fingerprint=expected_parent_scope_fingerprint,
        expected_parent_reservation_revision=expected_parent_reservation_revision,
        selected_source_identities=selected_source_identities,
        reason=str(reason).strip(),
        created_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
    )

def source_partition_entry_closure(plan: RenamePlan, selected_entry_ids: Iterable[str]) -> tuple[tuple[PlanEntry, ...], tuple[PlanEntry, ...]]:
    """Conserve the same typed video bundle used by special decisions."""
    identifiers = tuple(str(value or "").strip() for value in selected_entry_ids)
    if not identifiers or any(not value for value in identifiers):
        raise PlanReviewError("请至少选择一个需要独立整理的视频")
    if len(set(identifiers)) != len(identifiers):
        raise PlanReviewError("独立整理选择包含重复条目")
    from src.rename import content_selection
    selection = content_selection.from_plan(plan)
    if selection is not None:
        from src.rename.content_associations import partition_source_closure
        from src.rename.projection_authority import validate_plan_projection_receipt
        validate_plan_projection_receipt(plan)
        content_selection.validate_plan_content(plan)
        visible = {entry_id(e): e for e in plan.entries if e.media_kind == 'video'}
        if any(identifier not in visible for identifier in identifiers):
            raise PlanReviewError('只能从当前保留视频选择独立整理来源')
        videos = tuple(visible[identifier] for identifier in identifiers)
        selected = {str(e.source_path) for e in videos}
        # A movie version may span several files. Selection is explicit: never
        # expand a half-version silently, or use only the visible MVS subset as
        # evidence of the original complete group boundaries.
        for group in selection.to_dict()['version_groups']:
            members = set(group['sources'])
            if selected & members and not members <= selected:
                raise PlanReviewError('请选中完整电影版本的所有分段后再独立整理')
        paths = set(partition_source_closure(selection.associations, selected))
        materials = content_selection.material_entries(plan, selection)
        closure = tuple(e for e in materials if e.source_path in paths)
        if len(closure) != len(paths):
            raise PlanReviewError('独立整理缺少完整冻结来源材料')
        return videos, closure
    closures = [provider_special_entry_closure(plan, identifier) for identifier in identifiers]
    indices = {i for closure in closures for i, _entry in closure}
    return tuple(closure[0][1] for closure in closures), tuple(
        entry for i, entry in enumerate(plan.entries) if i in indices
    )
