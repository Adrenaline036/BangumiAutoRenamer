"""Reusable NiceGUI renderer for exceptional-episode classification."""

from __future__ import annotations

from src.component.dialog_frame import dialog_card

from collections.abc import Callable, Mapping
from typing import Any


EXCEPTIONAL_ISSUE_GUIDANCE = {
    "provider_regular_episode_overflow_unresolved": (
        "普通集号超出已确认季度范围",
        "请在“特别篇 / OVA”中获取官方资料并选择对应条目，或作为独立作品整理。",
    ),
    "provider_special_eligibility_unavailable": (
        "附加视频尚无可信的官方特别篇资格",
        "自动分类尚不明确；仍可在“特别篇 / OVA”中选择对应官方条目。",
    ),
    "provider_special_evidence_unavailable": (
        "官方特别篇资料当前不可用",
        "请打开“特别篇 / OVA”获取资料。读取失败不表示没有特别篇，也不会替你排除文件。",
    ),
}


def render_cross_work_partition_action(
    ui_api: Any, on_click: Callable[[], object]
) -> None:
    ui_api.button(
        "作为独立作品整理", on_click=on_click, icon='o_call_split'
    ).props("outline color=orange-8").classes("grow")


def exceptional_entry_ids(plan: Any, rows: Any) -> set[str]:
    """Shared display eligibility; this never changes verifier/approval issues."""
    candidate_value = plan.metadata.get("provider_special_candidates")
    candidates = candidate_value if isinstance(candidate_value, Mapping) else {}
    unavailable_value = plan.metadata.get(
        "provider_special_eligibility_unavailable"
    )
    unavailable_ids = {
        str(value) for value in unavailable_value
        if isinstance(value, str) and value
    } if isinstance(unavailable_value, (list, tuple)) else set()
    affected = set()
    for row in rows:
        identifier = str(row.get('entry_id') or '')
        if (
            row
            and set(row.get("issue_codes", ())).intersection({
                "provider_special_decision_required",
                "provider_regular_episode_overflow_unresolved",
                "provider_special_eligibility_unavailable",
                "provider_special_evidence_unavailable",
            })
            and (
                identifier in unavailable_ids
                or isinstance(candidates.get(identifier), (list, tuple))
                or bool(set(row.get("issue_codes", ())).intersection({
                    "provider_special_eligibility_unavailable",
                    "provider_special_evidence_unavailable",
                }))
            )
        ):
            affected.add(identifier)
    return affected


def open_official_special_picker(owner, *, ui_api, notify):
    """Reuse the same chooser without hiding it after a decision is made."""
    import inspect
    from ..rename.plan_store import PlanStore
    from .tv_content_actions import revise, revision_matches, revision_progress
    if owner.task_decision is None:
        notify('当前任务尚未确认作品，请先确认作品资料。', type='warning')
        return
    previous = getattr(owner, '_official_special_picker', None)
    if previous is not None and not previous.is_deleted:
        previous.close()
        previous.delete()
    fingerprint = PlanStore.fingerprint(owner.plan)
    revision = owner.task_decision.revision

    def current():
        valid = (not popup.is_deleted and popup.value and getattr(owner, '_official_special_picker', None) is popup
                 and revision_matches(owner, fingerprint, revision))
        if not valid:
            notify('计划已更新，请关闭并重新打开特别篇选择；原选择不会覆盖后来修改。', type='warning')
        return valid

    def adopt(result):
        nonlocal fingerprint, revision
        if (result is not None and not popup.is_deleted and popup.value
                and getattr(owner, '_official_special_picker', None) is popup):
            plan, state = result
            fingerprint, revision = PlanStore.fingerprint(plan), state.revision
            owner._official_special_controls['update'](plan)
            refresh_clear()

    async def fetch_with(edit):
        if not current():
            return
        if revision_progress(owner).running:
            notify('本任务正在处理，请等待本次操作完成。', type='info')
            return
        fetch_button.disable()
        link_button.disable()
        feedback.set_text('正在获取资料…')
        try:
            result = await revise(owner, expected_fingerprint=fingerprint, expected_revision=revision,
                mode='special_refresh', edit=edit, notify=notify, quiet=True, message='')
            adopt(result)
            if not popup.is_deleted:
                feedback.set_text(owner._official_special_controls['summary']() if result is not None
                                  else revision_progress(owner).error or '获取尚未完成，请查看任务处理状态')
        finally:
            if not popup.is_deleted:
                enabled = not revision_progress(owner).running
                fetch_button.set_enabled(enabled)
                link_button.set_enabled(enabled)

    async def fetch():
        await fetch_with({})

    async def confirm_link():
        await fetch_with({'counterpart_url': source_link.value})

    async def submit_new(edit):
        if not current():
            return
        result = await revise(owner, expected_fingerprint=fingerprint, expected_revision=revision,
            mode='special', edit=edit, notify=notify,
            message='特别篇对应已保存；视频与配套文件已更新，尚未执行。')
        adopt(result)
        return result

    async def clear_choice():
        controls = owner._official_special_controls or {}
        selected = controls['files'].value
        if not current() or not isinstance(selected, str):
            return
        if selected not in owner.task_decision.provider_special_decisions:
            notify('当前文件没有已确认的特别篇对应。', type='info')
            return
        result = owner._rebuild_inventory_role(selected, 'main_candidate')
        if inspect.isawaitable(result):
            result = await result
        adopt(result)

    def refresh_clear():
        controls = owner._official_special_controls or {}
        value = controls['files'].value
        if 'clear' in controls:
            controls['clear'].set_enabled(isinstance(value, str) and value in owner.task_decision.provider_special_decisions)

    with ui_api.dialog() as popup, dialog_card().classes('scroll bar-dialog bar-confirm-card w-[900px] max-w-full'):
        ui_api.label('特别篇 / OVA').classes('text-h6')
        with ui_api.row().classes('w-full items-center gap-2') as fetch_row:
            fetch_button = ui_api.button('获取特别篇资料', on_click=fetch, icon='o_cloud_download').props('outline color=blue-7')
        with ui_api.row().classes('w-full items-center gap-2'):
            source_link = ui_api.input('另一侧作品链接（自动获取失败时可填写）').props('outlined dense').classes('grow')
            link_button = ui_api.button('确认资料链接', on_click=confirm_link, icon='o_link').props('outline color=blue-7')
        from .special_crosswalk_view import render
        owner._official_special_controls = render(owner.plan, ui_api=ui_api, submit=submit_new, notify=notify)
        feedback = owner._official_special_controls['status']
        feedback.move(fetch_row)
        owner._official_special_controls.update(source_link=source_link, link_confirm=link_button,
                                               fetch=fetch_button, fetch_status=feedback)
        pending = revision_progress(owner)
        if pending.running:
            feedback.set_text('正在处理…')
            fetch_button.disable()
            link_button.disable()

            def finish_pending():
                if popup.is_deleted or not popup.value:
                    timer.cancel()
                elif not pending.running:
                    feedback.set_text(pending.error or '处理已结束，请重新打开特别篇窗口查看最新资料')
                    fetch_button.enable()
                    link_button.enable()
                    timer.cancel()

            timer = ui_api.timer(.25, finish_pending)
        with ui_api.row().classes('bar-modal-footer w-full items-center gap-2') as actions:
            if owner._official_special_controls:
                owner._official_special_controls['confirm'].move(actions)
            if owner._official_special_controls and owner.task_decision.content_selection is not None:
                clear = ui_api.button('移出特别篇', icon='o_playlist_remove', on_click=clear_choice).props('outline color=blue-7')
                files = owner._official_special_controls['files']
                files.on_value_change(lambda _: refresh_clear())
                owner._official_special_controls['clear'] = clear
                refresh_clear()
            if owner._official_special_controls:
                owner._official_special_controls['confirmed'].move(actions)
            ui_api.button('关闭', icon='o_close', on_click=popup.close).props('flat color=grey-8').classes('ml-auto')
    owner._official_special_picker = popup
    popup.open()
