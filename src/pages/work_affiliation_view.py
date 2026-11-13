"""A single optional registered-work chooser for TV and movies."""
from ..queue_runtime import ui_disk_call

from .progress_view import bar_progress

from src.component.dialog_frame import dialog_card

from ..rename.plan_store import PlanStore
from ..rename.work_affiliation import pending_from_plan, catalog_choices, preview_choice, preview_correction_choice, commit_choice
from .tv_content_actions import run_revision


def render(owner, *, ui, notify):
    work = owner.plan.work_identity
    if work is None or work.status != 'confirmed' or owner.task_decision is None:
        return False
    from .plan_review_page import is_fresh_unexecuted_task
    if not is_fresh_unexecuted_task(owner.task_data, owner.task_decision):
        return False
    pending = pending_from_plan(owner.plan)
    if pending is not None:
        with ui.card().classes('bar-confirm-card w-full'):
            ui.label('发现可能相同的已登记作品，请确认归属').classes('font-bold text-orange-8')
            ui.label('确认前保留当前来源库存，不生成新的作品目录。').classes('bar-help')
            for row in pending.to_dict()['recommendations']:
                ui.label(f'{row["title"]} · {row["root"]}').classes('bar-path-text')
            with ui.row():
                ui.button('归入已有作品', icon='o_merge_type', on_click=lambda: show(owner, ui=ui, notify=notify)).classes('bar-review-action bg-white').props('outline color=blue-7 no-caps')
                ui.button('作为独立作品整理', icon='o_call_split', on_click=lambda: independent(owner, ui=ui, notify=notify)).classes('bar-review-action bg-white').props('outline color=blue-7 no-caps')
    else:
        ui.button('作为独立作品整理', icon='o_call_split', on_click=lambda: independent(owner, ui=ui, notify=notify)).classes('bar-review-action').props('outline color=blue-7 no-caps')
        ui.button('归入已有作品', icon='o_merge_type', on_click=lambda: show(owner, ui=ui, notify=notify)).classes('bar-review-action bg-white').props('outline color=blue-7 no-caps')
    return pending is not None


async def independent(owner, *, ui, notify):
    if owner.plan.metadata.get('tv_completion_resolution') is not None:
        from ..rename.work_correction import observe, prepare
        revision, fingerprint = owner.task_decision.revision, PlanStore.fingerprint(owner.plan)
        try:
            observation = await ui_disk_call(observe, owner.review_service, owner.plan.task_id, fingerprint,
                expected_revision=revision, candidate=owner.plan.work_identity.primary_provider.to_dict())
            intent = await ui_disk_call(prepare, owner.review_service, observation,
                season_number=owner.task_decision.season_binding['season_number'], reason='作为独立作品整理')
            await show(owner, ui=ui, notify=notify, separate=True, correction_inputs=intent.inputs)
        except ValueError as exc:
            from ..rename.work_affiliation import AffiliationRequired
            if isinstance(exc, AffiliationRequired):
                await show(owner, ui=ui, notify=notify, separate=True, correction_inputs=exc.inputs)
            else:
                notify(str(exc), type='warning')
        return
    await show(owner, ui=ui, notify=notify, separate=True)


async def show(owner, *, ui, notify, separate=False, selected_key=None, correction_inputs=None, is_current=None):
    if (getattr(owner, '_affiliation_open', False) or getattr(owner, 'is_deleted', False)
            or getattr(owner, 'exit_requested', False)):
        return
    revision, fingerprint = owner.task_decision.revision, PlanStore.fingerprint(owner.plan)
    active, busy = True, False
    owner._affiliation_open = True
    def close(*_args):
        nonlocal active
        active = False
        owner._affiliation_open = False
    def current():
        return (active and not owner.is_deleted and not owner.exit_requested
                and owner.task_decision.revision == revision and PlanStore.fingerprint(owner.plan) == fingerprint
                and (is_current is None or is_current()))
    owner.client.on_disconnect(close)
    owner.on_value_change(lambda event: close() if not event.value else None)
    with owner.client:
        dialog = ui.dialog()
        dialog.on_value_change(lambda event: close() if not event.value else None)
        with dialog, dialog_card().classes('bar-confirm-card w-full'):
            ui.label('确认本地作品归属').classes('font-bold')
            ui.label('只列出本程序已登记的作品；目录名称沿用首次登记，不扫描媒体库。').classes('bar-help')
            content = ui.column().classes('w-full')
            with content:
                bar_progress(ui)
                ui.label('正在读取已登记作品…')
            with ui.row().classes('bar-modal-footer w-full items-center'):
                primary_actions = ui.row().classes('gap-2')
                ui.button('取消', on_click=dialog.close, icon='o_close').props('flat no-caps').classes('ml-auto')
        dialog.open()

    async def preview(key, as_separate=False):
        nonlocal busy
        if not current() or busy:
            return
        busy = True
        with owner.client:
            content.clear()
            primary_actions.clear()
            with content:
                bar_progress(ui)
                ui.label('正在准备归属预览…')
        try:
            if correction_inputs is None:
                intent, plan = await ui_disk_call(preview_choice, owner.review_service,
                    owner.plan.task_id, fingerprint, key=key, separate=as_separate)
            else:
                intent, plan = await ui_disk_call(preview_correction_choice, owner.review_service,
                    correction_inputs, key=key, separate=as_separate)
            if not current():
                return
            with owner.client:
                content.clear()
                primary_actions.clear()
                with content:
                    ui.label(f'作品：{plan.work_identity.title}')
                    ui.label(f'目标目录：{plan.metadata["canonical_work_root"]}').classes('bar-path-text')
                    if plan.metadata.get('season_number') is not None:
                        ui.label(f'本次输出：第 {plan.metadata["season_number"]} 季')
                    ui.label('确认后生成未批准的新计划；此处不创建或移动媒体。').classes('bar-help')
                    async def confirm():
                        if not current():
                            return
                        await run_revision(owner, submit=lambda: commit_choice(owner.review_service, intent),
                            expected_revision=revision, expected_fingerprint=fingerprint, notify=notify,
                            on_success=accepted, message='作品归属已确认，请检查新计划。',
                            started_message='正在保存作品归属…')
                    def accepted():
                        owner.provider_selection_expanded = False
                        dialog.close()
                    ui.button('确认作品归属', on_click=confirm, icon='o_check').props('color=blue-7 no-caps').move(primary_actions)
        except Exception as exc:
            if current():
                with owner.client:
                    content.clear()
                    primary_actions.clear()
                    with content:
                        ui.label(f'未能准备归属：{exc}').classes('text-negative')
        finally:
            busy = False

    try:
        if separate:
            await preview('', True)
            return
        rows = await ui_disk_call(catalog_choices, owner.review_service, owner.plan.task_id, fingerprint,
                                      correction_inputs=correction_inputs)
        if not current():
            return
        with owner.client:
            content.clear()
            primary_actions.clear()
            with content:
                if not rows:
                    ui.label('没有兼容的已登记作品。')
                else:
                    options = {row['key']: f'{row["title"]} · {row["root"]}' for row in rows}
                    selected = ui.select(options, value=selected_key if selected_key in options else None,
                        label='已登记作品', with_input=True).classes('w-full')
                    async def selected_preview():
                        if selected.value not in options:
                            notify('请选择已登记作品。', type='warning')
                            return
                        from .asset_choice_forms import is_initial_movie_selection
                        if (correction_inputs is None
                                and {g.kind for g in owner.plan.presentation_groups} == {'movie_continuous'}
                                and pending_from_plan(owner.plan) is None
                                and is_initial_movie_selection(owner.plan)):
                            open_movie(False)
                            return
                        await preview(selected.value)
                    def open_movie(replacement_only):
                        if not current() or busy:
                            return
                        if selected.value not in options:
                            notify('请选择已登记作品。', type='warning')
                            return
                        from .asset_choice_forms import open_registered_movie
                        key = selected.value
                        dialog.close()
                        with owner.client:
                            open_registered_movie(owner, ui=ui, notify=notify, work_key=key,
                                                  replacement_only=replacement_only)
                    ui.button('预览归属', on_click=selected_preview, icon='o_preview').props('outline no-caps').move(primary_actions)
                    if correction_inputs is None and {g.kind for g in owner.plan.presentation_groups} == {'movie_continuous'}:
                        ui.button('选择并换源', icon='o_swap_horiz', on_click=lambda: open_movie(True)).props(
                            'outline color=blue-7 no-caps').move(primary_actions)
                if correction_inputs is not None:
                    ui.button('作为独立作品整理', on_click=lambda: preview('', True), icon='o_call_split').props('outline no-caps').move(primary_actions)
    except Exception as exc:
        if current():
            with owner.client:
                content.clear()
                primary_actions.clear()
                with content:
                    ui.label(f'无法读取作品登记：{exc}').classes('text-negative')
