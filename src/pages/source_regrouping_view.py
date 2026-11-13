"""Preview first; explicit confirmation creates ordinary pending task cards."""
from ..queue_runtime import ui_disk_call

from .progress_view import bar_progress

from src.component.dialog_frame import dialog_card
from pathlib import Path
from functools import partial

from nicegui import ui

from ..rename.content_associations import from_plan, partition_source_closure
from ..rename.plan_store import PlanStore
from ..rename.source_regrouping import SourceRegroupingPreview


class SourceRegroupingDialog(ui.dialog):
    def __init__(self, work, manager, *, preview, selected_entry_ids=None):
        self.work = work
        self.manager = manager
        self.plan = work.plan
        self.fingerprint = PlanStore.fingerprint(self.plan)
        self.associations = from_plan(self.plan)
        self.selected_entry_ids = selected_entry_ids
        self.preview = preview
        if (type(self.preview) is not SourceRegroupingPreview
                or self.preview.to_dict()['plan_fingerprint'] != self.fingerprint
                or self.preview.to_dict().get('selected_entry_ids') !=
                    (sorted(selected_entry_ids) if selected_entry_ids is not None else None)):
            raise ValueError('所选视频预览已变化，请重新打开')
        self.groups = self.preview.to_dict()['groups']
        self.excluded_sources = {p for g in self.groups for p in g['excluded_sources']}
        super().__init__()
        self.props('persistent')
        self.closed = False
        self.created_ids = ()
        self.result_dialog = None
        self.busy = False
        self.edit_controls = []
        self.group_controls = []
        self.on_value_change(lambda e: setattr(self, 'closed', not e.value))
        work.on_value_change(lambda e: self.close() if not e.value and not self.is_deleted else None)
        self.client.on_disconnect(self.close)
        with self, dialog_card(header=4).classes('bar-dialog bar-regroup-dialog bar-modal-fixed'):
            ui.label('作为独立作品整理 · 核对所选视频' if self.selected_entry_ids is not None
                     else '拆卡 · 核对分组并创建新卡').classes('bar-dialog-title')
            self.error = ui.label('').classes('text-negative whitespace-pre-wrap')
            self.summary = ui.label('').classes('text-subtitle1')
            with ui.column().classes('w-full text-blue-8') as self.progress:
                bar_progress(ui)
                self.progress_text = ui.label('')
            self.progress.set_visibility(False)
            self.destinations = {}
            self.body = ui.column().classes('bar-modal-body scroll w-full gap-3')
            with ui.row().classes('bar-modal-footer w-full items-center gap-3'):
                if self.selected_entry_ids is None:
                    self.edit_controls.append(ui.button('恢复默认分组', on_click=self.reset, icon='o_restart_alt').props('outline color=blue-7'))
                self.confirm_button = ui.button('确认创建新卡', on_click=self.confirm, icon='o_check').props('color=red-7')
                self.close_button = ui.button('关闭', on_click=self.close, icon='o_close').props('flat color=grey-7').classes('ml-auto')
        self._render()

    def _sync_controls(self):
        if self.is_deleted:
            return
        for control in self.edit_controls + self.group_controls:
            control.set_enabled(not self.busy)
        self.confirm_button.set_enabled(not self.busy)
        for table in self.tables:
            table.set_selection('multiple' if not self.busy and self.selected_entry_ids is None else None)

    def _set_busy(self, value, message=''):
        self.busy = value
        if self.is_deleted:
            return
        self.progress.set_visibility(value)
        self.progress_text.set_text(message)
        self.close_button.set_text('关闭')
        self._sync_controls()

    def _query(self, groups):
        result = self.manager.preview_source_regrouping(task_id=self.plan.task_id,
            expected_plan_fingerprint=self.fingerprint,
            groups=groups if self.selected_entry_ids is None else None,
            selected_entry_ids=self.selected_entry_ids,
            excluded_sources=sorted(self.excluded_sources) if self.selected_entry_ids is None else None)
        if type(result) is not SourceRegroupingPreview:
            raise ValueError('完整分组预览不可用，请关闭后重新打开原任务')
        return result

    def _usable(self):
        if self.is_deleted or self.closed or self.work.is_deleted or getattr(self.work, 'exit_requested', False):
            return False
        if PlanStore.fingerprint(self.work.plan) != self.fingerprint:
            self.error.set_text('原计划已变化，请关闭后重新打开分组预览')
            return False
        return True

    def _render(self):
        self.tables = []
        self.group_controls = []
        self.body.clear()
        options = {i: f'第{i + 1}组 · {g["label"]}' for i, g in enumerate(self.groups)}
        options['new'] = '新建一个分组（仅预览）'
        self.destinations = {}
        source_info = self.associations.to_dict()
        kinds = {r['path']: r['kind'] for r in source_info['sources']}
        relations = {r['source_path']: r for r in source_info['relations']}
        labels = {'video': '视频', 'subtitle': '字幕', 'audio': '音轨'}
        self.summary.set_text(f'共 {len(self.groups)} 组 / {sum(len(g["sources"]) for g in self.groups)} 个文件 · 尚未创建任务')
        with self.body:
            for i, group in enumerate(self.groups):
                if self.selected_entry_ids is not None and i > 0:
                    ui.label(f'其余 {group["video_count"]} 个视频及配套保留在另一张未批准的新卡，不需要逐项重新拆分。')
                    continue
                with ui.card().classes('w-full'):
                    with ui.row().classes('w-full items-center gap-3'):
                        ui.label(f'第{i + 1}组 · {group["label"]} · {group["video_count"]} 个视频 / {len(group["sources"])} 个文件').classes('text-subtitle1 mr-auto')
                        if self.selected_entry_ids is None:
                            destination = ui.select({key: value for key, value in options.items() if key != i},
                                value='new', label='移动到').props('dense outlined').classes('w-56')
                            self.destinations[i] = destination
                            self.group_controls.extend([destination, ui.button('移动所选文件及配套',
                                on_click=partial(self.move_selected, i), icon='o_drive_file_move').props('outline color=blue-7')])
                    if self.selected_entry_ids is None:
                        with ui.row().classes('items-center gap-3'):
                            for label, action, icon in (('纳入所选文件及配套', 'restore', 'o_add'), ('排除所选文件及配套', 'exclude', 'o_remove')):
                                self.group_controls.append(ui.button(label, icon=icon,
                                    on_click=partial(self.change_content, i, action)).props('outline color=blue-7'))
                    ui.label(group['reason']).classes('text-grey-7')
                    rows = []
                    for source in group['sources']:
                        relation = relations.get(source, {})
                        owners = relation.get('owners', [])
                        rows.append(dict(source=source, file=str(Path(source).relative_to(self.plan.source_root)),
                            kind=labels.get(kinds[source], '附件'),
                            companion='配套：' + '、'.join(Path(p).name for p in owners) if owners else '',
                            excluded='不纳入整理' if source in group['excluded_sources'] else ''))
                    selection_bar = ui.row().classes('items-center gap-3') if self.selected_entry_ids is None else None
                    table = ui.table(columns=[
                        dict(name='file', label='来源文件', field='file', align='left',
                             style='white-space: normal; overflow-wrap: anywhere; max-width: 420px'),
                        dict(name='kind', label='类型', field='kind', align='left'),
                        dict(name='companion', label='配套关系', field='companion', align='left',
                             style='white-space: normal; overflow-wrap: anywhere; max-width: 280px'),
                        dict(name='excluded', label='整理范围', field='excluded', align='left'),
                    ], rows=rows, row_key='source',
                        selection='multiple' if self.selected_entry_ids is None else None, pagination=10
                    ).classes('w-full').props('rows-per-page-label="每页文件数"')
                    table._props[':pagination-label'] = '(first,last,total) => `${first}-${last} / ${total}`' 
                    self.tables.append(table)
                    if selection_bar is not None:
                        with selection_bar:
                            count = ui.label(f'已选 0 / 共 {len(rows)} 项')
                        with table.add_slot('header-selection'):
                            select_all = ui.checkbox('', on_change=lambda e, t=table, c=count:
                                self._select_group(t, c, bool(e.value)) if not getattr(t, '_selection_sync', False) else None).props('aria-label="全选本组文件（含其他页）"')
                        table.group_checkbox = select_all
                        self.group_controls.append(select_all)
                        table.on_select(lambda event, t=table, c=count: self._selection_changed(t, c))
            if self.selected_entry_ids is not None:
                omitted = len(self.preview.to_dict()['omitted_sources'])
                ui.label(f'原计划已排除的 {omitted} 个文件不进入新卡，源文件不变。').classes('text-grey-8')
        self._sync_controls()

    def _select_group(self, table, count, selected):
        if not self._usable() or self.busy or self.selected_entry_ids is not None or table not in self.tables:
            return
        table.selected = list(table.rows) if selected else []
        table.update()
        self._selection_changed(table, count)

    def _selection_changed(self, table, count):
        count.set_text(f'已选 {len(table.selected)} / 共 {len(table.rows)} 项')
        table._selection_sync = True
        try:
            table.group_checkbox.set_value(True if len(table.selected) == len(table.rows)
                else None if table.selected else False)
        finally:
            table._selection_sync = False

    async def _accept(self, groups):
        self._set_busy(True, '正在核对分组……')
        try:
            preview = await ui_disk_call(self._query, groups)
            if not self._usable():
                return
            self.preview = preview
            self.groups = preview.to_dict()['groups']
            self.error.set_text('')
            self._render()
        finally:
            self._set_busy(False)

    async def move_selected(self, group_index):
        if not self._usable() or self.busy or self.selected_entry_ids is not None:
            return
        try:
            if type(group_index) is not int or group_index not in self.destinations:
                raise ValueError('请选择有效的来源分组')
            chosen = {row['source'] for row in self.tables[group_index].selected}
            if not chosen:
                raise ValueError('请先勾选要移动的文件')
            kinds = {r['path']: r['kind'] for r in self.associations.to_dict()['sources']}
            videos = [p for p in chosen if kinds[p] == 'video']
            if videos:
                chosen.update(str(p) for p in partition_source_closure(self.associations, videos))
            groups = [list(g['sources']) for g in self.groups]
            target = self.destinations[group_index].value
            if target == group_index:
                raise ValueError('不能移动到当前分组')
            if target == 'new':
                target = len(groups)
                groups.append([])
            if type(target) is not int or not 0 <= target < len(groups):
                raise ValueError('请选择有效的目标分组')
            groups = [[p for p in g if p not in chosen] for g in groups]
            groups[target].extend(sorted(chosen))
            await self._accept([g for g in groups if g])
        except (ValueError, RuntimeError, OSError) as exc:
            if self._usable():
                self.error.set_text(str(exc))

    async def change_content(self, index, action):
        if not self._usable() or self.busy or self.selected_entry_ids is not None:
            return
        old = set(self.excluded_sources)
        try:
            chosen = {row['source'] for row in self.tables[index].selected}
            if not chosen:
                raise ValueError('请先勾选要调整的文件')
            kinds = {row['path']: row['kind'] for row in self.associations.to_dict()['sources']}
            videos = [p for p in chosen if kinds[p] == 'video']
            if videos:
                chosen.update(str(p) for p in partition_source_closure(self.associations, videos))
            if action == 'restore':
                self.excluded_sources.difference_update(chosen)
            else:
                self.excluded_sources.update(chosen)
            await self._accept([g['sources'] for g in self.groups])
        except (ValueError, RuntimeError, OSError) as exc:
            self.excluded_sources = old
            self.error.set_text(str(exc))

    async def reset(self):
        if self._usable() and not self.busy:
            try:
                await self._accept(None)
            except (ValueError, RuntimeError, OSError) as exc:
                if self._usable():
                    self.error.set_text(str(exc))

    async def check(self):
        if self._usable() and not self.busy:
            self._set_busy(True, '正在核对分组，请稍候；此时尚未创建新卡。')
            try:
                await ui_disk_call(self._query, [g['sources'] for g in self.groups])
                if not self._usable():
                    return
                self.error.set_text('')
                self.summary.set_text(f'核对通过：{len(self.groups)} 组，文件无重复或遗漏；仅预览，未保存或创建任务。')
            except (ValueError, RuntimeError, OSError) as exc:
                if self._usable():
                    self.error.set_text(str(exc))
            finally:
                self._set_busy(False)

    async def confirm(self):
        if not self._usable() or self.busy:
            return
        self._set_busy(True, '正在核对分组，请稍候；请勿重复提交。')
        try:
            current = await ui_disk_call(self._query, [g['sources'] for g in self.groups])
            if not self._usable():
                return
            # After explicit confirmation the existing transaction finishes even
            # if the browser disconnects. Do not cancel or replace its writer.
            self.progress_text.set_text('正在创建新卡并提交识别；关闭窗口后可从任务列表查看结果，请勿重复添加。')
            ids, failures = await self.manager.create_and_start_regrouped_tasks(current)
            if (type(ids) is not tuple or len(ids) != len(current.task_groups)
                    or len(set(ids)) != len(ids) or not all(isinstance(i, str) and i for i in ids)):
                raise ValueError('未取得完整建卡结果，请回任务列表核对，不能视为完成')
            self.created_ids = ids
            if not self._usable():
                return
            # Keep the result outside the old workspace's lifetime/slots.
            with self.client.content:
                self.result_dialog = ui.dialog()
                with self.result_dialog, dialog_card().classes('w-full').style('max-width: 800px'):
                    ui.label(f'拆卡完成 · 已创建 {len(ids)} 张新卡').classes('text-h5')
                    ui.label('原卡已移除。含保留视频的新卡已提交识别；全排除视频组仍待核对。杂项不创建任务，源文件保留。新卡均未批准。')
                    for index, (task_id, group) in enumerate(zip(ids, current.task_groups), 1):
                        pending = self.manager._read(task_id).get('source_group_pending')
                        status = failures.get(task_id) or ('来源归属待核对' if pending else '已提交识别，请稍后核对作品')
                        ui.label(f'{group["label"]} · {len(group["sources"])} 个文件 · {status}')
                        ui.label(task_id).classes('text-caption')
                        ui.button(f'打开第{index}张新卡', on_click=partial(self.open_created, task_id), icon='o_open_in_new')
                    ui.button('关闭结果', on_click=self.result_dialog.close, icon='o_close').classes('bar-modal-footer')
            self.work.exit_requested = True
            self.work.close()
            self.close()
            self.result_dialog.open()
        except (ValueError, RuntimeError, OSError) as exc:
            if self._usable():
                self.error.set_text(str(exc))
        finally:
            self._set_busy(False)

    async def open_created(self, task_id):
        if (task_id not in self.created_ids or self.result_dialog is None
                or self.result_dialog.is_deleted or not self.result_dialog.value):
            return
        self.result_dialog.close()
        from .plan_review_page import plan_review_page
        await plan_review_page(task_id)
