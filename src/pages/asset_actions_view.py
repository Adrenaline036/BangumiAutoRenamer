'''Asset replacement preview; no media effects before normal approval.'''
from ..queue_runtime import ui_disk_call

from .tv_content_actions import await_result

from .progress_view import bar_progress

from src.component.dialog_frame import dialog_card
from nicegui import ui
from ..rename.plan_store import PlanStore
from ..rename.replacement_completion import uses_completion_policy
from ..element.red import notify


def retention_message(operation):
    if uses_completion_policy(operation):
        return '旧内容仅临时保留，整理全部完成后自动清理。'
    return '旧目标备份保留在媒体库外。'


def occupied_season(plan):
    """Cheap presence hint only; the confirmed preview freezes the full scope."""
    from pathlib import Path
    from ..rename.season_replacement import season_root
    if {g.kind for g in plan.presentation_groups}!={'tv_episodes'}:
        return False
    try:
        root=season_root(plan.metadata.get('canonical_work_root',''),plan.metadata.get('season_number'))
        if root.is_dir() and next(root.iterdir(),None) is not None:
            return True
        from ..rename.season_replacement import special_targets
        special = special_targets(plan, root.parent, plan.metadata.get('season_number'))
        return any(str(entry.target_path) in special and entry.target_path.exists() for entry in plan.entries)
    except (OSError,ValueError):
        return False


def render_repair_choice(self):
    if ({g.kind for g in self.plan.presentation_groups} != {'movie_continuous'}
            and self.plan.metadata.get('series_key')
            and getattr(self, 'task_data', {}).get('workflow_status') in {'need_confirm', 'invalid', 'approved'}
            and self.plan.metadata.get('completed_rebuild_observation') is None):
        ui.button('核查并补齐', icon='o_fact_check',
            on_click=lambda: preview_asset_replacement(self, repair=True)).classes(
                'bar-review-action bg-white').props('outline color=blue-7 no-caps')


def render_asset_choice(self, *, embedded=False, movie_work_key=None, action_row=None):
    operation = self.plan.metadata.get('asset_operation') or {}
    is_movie = {g.kind for g in self.plan.presentation_groups} == {'movie_continuous'}
    if action_row is not None:
        action_row.move(ui.context.slot.parent)
    def choose():
        if is_movie and not (self.plan.metadata.get('movie_version_selection') or {}).get('replacement'):
            from .asset_choice_forms import open_registered_movie
            open_registered_movie(self, ui=ui, notify=notify, work_key=movie_work_key,
                                  replacement_only=True)
        else:
            import asyncio
            self._movie_replacement_preview_task = asyncio.create_task(preview_asset_replacement(self))
    if operation.get('mode') == 'replace':
        with action_row if action_row is not None else ui.row().classes(('' if embedded else 'bar-confirm-card ') + 'w-full items-center gap-3'):
            ui.button('确认换源范围', icon='o_swap_horiz', on_click=self._preview_asset_replacement).classes('bar-review-action bg-white').props('outline color=blue-7 no-caps')
            ui.label(f"已选择整体换源：原位替换 {len(operation.get('replaces', ()))} 项，旧内容退场 {len(operation.get('retirements', ()))} 项；批准后执行。" + retention_message(operation)).classes('bar-help text-blue-8')
    elif (self.plan.work_identity is not None and self.plan.work_identity.status == 'confirmed'
            and (movie_work_key or self.plan.metadata.get('catalog_revision') or operation.get('requires_choice') or occupied_season(self.plan))
            and getattr(self, 'task_data', {}).get('workflow_status') != 'completed'
            and self.plan.metadata.get('completed_rebuild_observation') is None):
        with action_row if action_row is not None else ui.row().classes(('' if embedded else 'bar-confirm-card ') + 'w-full items-center gap-3'):
            ui.button('换源', icon='o_swap_horiz', on_click=choose if is_movie else self._preview_asset_replacement).classes(
                'bar-review-action bg-white').props('outline color=blue-7 no-caps')
            ui.label('目标季度／版本已有内容。换源会整体替换所选范围，新包未带的旧文件也会退出。').classes('bar-help')

async def preview_asset_replacement(self, *, repair=False):
    import asyncio
    from .tv_content_actions import (register_revision_lifecycle, revision_owner_alive,
        revision_matches)
    register_revision_lifecycle(self, notify, quiet=True)
    with self.client, ui.dialog() as dialog, dialog_card().classes('scroll bar-dialog bar-confirm-card w-[760px] max-w-full'):
        ui.label('核查并补齐' if repair else '确认换源范围').classes('text-h6')
        message = ui.label('正在核查缺失目标，请稍候……' if repair else '正在准备换源，请稍候……').classes('bar-help')
        progress = bar_progress(ui)
        details = ui.column().classes('w-full')
        actions = ui.row().classes('w-full justify-end').classes('bar-modal-footer')
        with actions:
            ui.button('取消', on_click=dialog.close, icon='o_close').props('flat')
    dialog.open()
    def alive():
        return not dialog.is_deleted and bool(dialog.value) and revision_owner_alive(self)
    try:
        original = PlanStore.fingerprint(self.plan)
        revision = self.task_decision.revision
        if not revision_matches(self, original, revision):
            raise ValueError('计划已更新，请重新打开换源范围。')
        message.set_text('正在核对所选季度／版本的登记与目标文件……')
        expected = PlanStore.fingerprint(self.plan)
        service=self.review_service.asset_actions()
        operation, _draft = await ui_disk_call(service.prepare_missing if repair else service.prepare_replacement, self.plan)
        if not alive():
            return
        if PlanStore.fingerprint(self.plan) != expected:
            raise ValueError('计划已更新，请重新打开换源范围。')
        value = operation.to_dict()
        if value['mode'] != ('repair' if repair else 'replace'):
            raise ValueError('当前没有可替换的已登记资产；不同电影版本请按正常新增整理')
        if repair:
            missing=sum(e.collision_resolution is None for e in _draft.entries)
            message.set_text(f'本次补齐 {missing} 项、复用 {len(_draft.entries)-missing} 项；现存文件、系列和其他季度登记保留。完成后接续缺失分集登记。')
        else:
            message.set_text(f"整体更换所选季度／版本：原位替换 {len(value['replaces'])} 项，退场 {len(value['retirements'])} 项旧内容。新包未带的旧专属附件也会退出，其他季度／版本和源下载文件不变。" + retention_message(value))
        with details:
            season=value.get('season_directory')
            if season is not None:
                ui.label('整季替换目录：'+season['root']).classes('bar-path-text')
                if season.get('special_files'):
                    ui.label('另含本次选中的同名特别篇及配套；特别篇目录内其他文件不变。').classes('bar-help')
            with ui.expansion('查看文件差异', icon='o_compare_arrows').classes('w-full'):
                rows=season['files'] + season.get('special_files', []) if season is not None else value['previous_assets']
                for row in rows:
                    text = {'replace':'原位替换：','retire':'退出并临时保留：' if uses_completion_policy(value) else '退出并备份：','reuse':'验证复用：','missing':'原文件已缺失：'}
                    ui.label(text.get(row['disposition'], '保留：') + row['target' if season is not None else 'target_path']).classes('bar-path-text')
                if {g.kind for g in self.plan.presentation_groups} == {'movie_continuous'}:
                    ui.label('新来源纳入的文件').classes('font-semibold')
                    for entry in _draft.entries:
                        ui.label(str(entry.source_path) + ' → ' + str(entry.target_path)).classes('bar-path-text')
            ui.label('这里只生成新计划，仍须重新批准并明确执行。'+('此季度内的手工文件、Scans/CDs等子目录文件也会整体退出；目录外不变。' if season is not None else '未知归属文件不会被自动删除。')).classes('bar-help')
        async def confirm():
            if not alive():
                return
            button.disable(); progress.set_visibility(True)
            try:
                revised = await self.queue.confirm_asset_plan(self.plan.task_id, expected, operation)
                if not alive():
                    return  # Dispatched transaction completes; abandoned UI does not adopt it.
                self.task_decision = self.review_service.decision_state_store.load(self.plan.task_id)
                await await_result(self._adopt_revision(revised))
                dialog.close()
                notify('已生成待批准补齐计划，尚未执行' if repair else '已生成待批准换源计划，尚未执行', type='positive')
            except Exception as exc:
                if alive():
                    message.set_text(str(exc)); notify(str(exc), type='negative')
            finally:
                if alive():
                    button.enable(); progress.set_visibility(False)
        actions.clear()
        with actions:
            ui.button('取消', on_click=dialog.close, icon='o_close').props('flat')
            button = ui.button('生成计划', icon='o_swap_horiz', on_click=confirm).classes('bar-review-action').props('unelevated color=blue-7')
    except (OSError, ValueError, RuntimeError) as exc:
        if not alive():
            return
        message.set_text(str(exc))
        actions.clear()
        with actions:
            ui.button('关闭', on_click=dialog.close, icon='o_close').props('flat')
    finally:
        if alive():
            progress.set_visibility(False)
