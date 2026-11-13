"""First and subsequent movie-version commands share one async GUI transport."""
from copy import deepcopy
from pathlib import Path

from ..rename.plan_store import PlanStore
from .tv_content_actions import run_revision


async def complete(owner, group_id, *, selected, groups=None, excluded=(), notify):
    plan, state, service = owner.plan, owner.task_decision, owner.review_service
    fingerprint, revision = PlanStore.fingerprint(plan), state.revision
    selected, excluded, groups = tuple(map(str, selected)), tuple(map(str, excluded)), deepcopy(groups)
    if group_id not in {group.group_id for group in plan.presentation_groups}:
        notify('电影版本所属分组已变化，请刷新。', type='warning')
        return

    def submit():
        from ..rename.plan_review import _infer_movie_version_descriptor
        choices = groups
        if choices is None:
            choices = []
            for path in selected:
                descriptor = _infer_movie_version_descriptor(Path(path), group_id, plan.source_root)
                choices.append(dict(sources=[path], label=descriptor.display_label or '默认版本'))
        if state.movie_version_selection is None:
            intent = service.prepare_initial_movie_completion(plan.task_id, fingerprint,
                groups=choices, excluded_sources=excluded, reason='明确确认电影完整版本及保留范围')
            return service.commit_initial_movie_completion(intent)
        return service.revise_movie_versions(plan.task_id, fingerprint, groups=choices, excluded_sources=excluded)

    await run_revision(owner, submit=submit, expected_revision=revision, expected_fingerprint=fingerprint,
        worker_field='_movie_version_worker', closed_field='_movie_content_closed', notify=notify,
        message='电影完整版本已保存为新未批准计划；源文件保持原状。')
