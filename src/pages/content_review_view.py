"""Two-column content choices on the existing page; no media execution."""

from src.component.icons import outlined_icon
from ..queue_runtime import ui_disk_call

from .tv_content_actions import await_result

from .progress_view import bar_progress

from src.component.dialog_frame import dialog_card
import asyncio
import hashlib
from pathlib import Path

from ..rename.content_associations import source_key
from ..rename.content_choice import ContentChangePreview
from ..rename.content_selection import ContentSelection, from_plan, omitted_sources
from ..rename.movie_version_selection import MovieVersionSelection
from ..rename.plan_store import PlanStore
from ..rename.rename_plan import RenamePlan


PAGE_SIZE = 10
PAGE_OPTIONS = (5, 10, 15, 20)


def selection_rows(choice, movie, *, work_title=''):
    """Display only the canonical frozen closure, never infer source roles."""
    value = choice.to_dict()
    omitted = omitted_sources(choice, movie)
    explicit = set(value['excluded_sources'])
    versions = set(movie.to_dict()['excluded_sources']) if movie is not None else set()
    kinds = {row['path']: row['kind'] for row in value['associations']['sources']}
    locked = {Path(path) for path, kind in kinds.items() if kind == "other"}
    rows = [dict(source=Path(row['source_path']), kind=kinds[row['source_path']],
        locked=Path(row['source_path']) in locked,
        included=Path(row['source_path']) not in omitted,
        identifier=hashlib.sha256(source_key(row['source_path']).encode('utf-8')).hexdigest()[:20],
        status=('不属于媒体配套' if Path(row['source_path']) in locked
                else '独立不整理' if row['source_path'] in explicit
                else '该版本不整理' if row['source_path'] in versions
                else '跟随所属视频不整理' if Path(row['source_path']) in omitted else '保留整理'))
        for row in value['materials']]
    materials = {Path(row['source_path']): row for row in value['materials']}

    def display_priority(row):
        if row['locked']:
            return 5
        if row['source'] in omitted:
            return 3
        material = materials[row['source']]
        asset = material.get('presentation_asset') or {}
        if (row['kind'] == 'video' and
                (material['role'] == 'main_episode' or asset.get('asset_role', {}).get('kind') == 'content_version')):
            return 0
        if row['kind'] in {'subtitle', 'audio'}:
            return 1
        return 2

    # Stable display ordering only; frozen material order and choices stay intact.
    return sorted(rows, key=display_priority)


def open_content_workbench(owner, *, ui, notify, selected_ids=None):
    if getattr(owner, '_content_choice_worker', None) is not None:
        notify('原内容操作仍在后台处理，请等待真实结果后刷新。', type='info')
        return
    previous = getattr(owner, '_content_workbench', None)
    if previous is not None and previous.alive():
        notify('内容工作区已经打开，请在当前窗口继续调整或关闭。', type='info')
        return
    owner._content_workbench = ContentWorkbench(owner, ui=ui, notify=notify,
                                              selected_ids=selected_ids)


class ContentWorkbench:
    def __init__(self, owner, *, ui, notify, selected_ids=None):
        self.owner, self.ui, self.notify = owner, ui, notify
        self.service, self.plan = owner.review_service, owner.plan
        self.fingerprint = PlanStore.fingerprint(self.plan)
        self.choice = from_plan(self.plan)
        raw_movie = self.plan.metadata.get('movie_version_selection')
        self.movie = MovieVersionSelection.from_mapping(raw_movie) if raw_movie is not None else None
        self.work_title = self.plan.work_identity.title if self.plan.work_identity else ''
        self.rows = selection_rows(self.choice, self.movie, work_title=self.work_title) if self.choice is not None else []
        allowed = {row['identifier'] for row in self.rows if not row['locked']}
        self.selected = set(getattr(owner, 'selected_ids', ()) if selected_ids is None else selected_ids) & allowed
        self.preview = None
        self.closed = self.busy = False
        self.pages = dict(exclude=0, restore=0)
        self.page_sizes = dict(exclude=PAGE_SIZE, restore=PAGE_SIZE)
        self.panels = {}
        self.boxes = []
        with ui.dialog() as self.dialog, dialog_card().classes('bar-dialog bar-content-dialog bar-modal-fixed'):
            ui.label('内容保留与排除').classes('bar-dialog-title')
            if self.choice is None:
                ui.label('尚未生成内容清单，请先生成整理计划。').classes('bar-help')
            with ui.element('div').classes('bar-content-columns w-full'):
                for action, heading, caption in (('exclude', '纳入整理', '排除整理'),
                                                  ('restore', '不整理', '纳入整理')):
                    with ui.column().classes('bar-content-pane gap-2'):
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.label(heading).classes('text-lg mr-auto')
                            if action == 'exclude' and self.choice is not None:
                                ui.button('选择建议排除项', on_click=self.select_recommended, icon='o_checklist').props('outline color=blue-7')
                            select = ui.button('本页全选', on_click=lambda a=action: self.select_current_page(True, a), icon='o_select_all').props('outline color=blue-7')
                            clear = ui.button('取消全选', on_click=lambda a=action: self.clear_selection(a), icon='o_deselect').props('outline color=blue-7')
                        container = ui.column().classes('bar-content-files scroll w-full')
                        label = ui.label('').classes('bar-help w-full' + (' text-right' if action == 'restore' else ''))
                        with ui.row().classes('w-full items-center gap-2') as footer:
                            with ui.row().classes('items-center gap-0 flex-nowrap') as paging:
                                first = ui.button(icon='o_first_page', on_click=lambda a=action: self.move(-len(self.column_rows(a)), a)).props('flat dense aria-label="第一页"')
                                previous = ui.button(icon='o_chevron_left', on_click=lambda a=action: self.move(-1, a)).props('flat dense aria-label="上一页"')
                                following = ui.button(icon='o_chevron_right', on_click=lambda a=action: self.move(1, a)).props('flat dense aria-label="下一页"')
                                last = ui.button(icon='o_last_page', on_click=lambda a=action: self.move(len(self.column_rows(a)), a)).props('flat dense aria-label="最后一页"')
                            size = ui.select(list(PAGE_OPTIONS), value=PAGE_SIZE, label='每页文件数',
                                on_change=lambda e, a=action: self.change_page_size(a, e.value)).props('dense outlined').classes('w-28')
                            button = ui.button(caption, icon=outlined_icon('arrow_forward' if action == 'exclude' else 'arrow_back'),
                                on_click=lambda a=action: self.apply(a)).props('outline color=blue-7')
                            button.set_visibility(self.choice is not None)
                            if action == 'exclude':
                                button.classes('ml-auto')
                            else:
                                button.move(footer, target_index=0)
                                size.classes('ml-auto')
                                paging.move(footer)
                        self.panels[action] = dict(container=container, label=label, previous=previous,
                            following=following, first=first, last=last, select=select, clear=clear, button=button, size=size)
            self.exclude = self.panels['exclude']['button']
            self.restore = self.panels['restore']['button']
            self.message = ui.label('').classes('bar-help text-negative')
            self.progress = bar_progress(ui)
            self.progress.set_visibility(False)
            self.preview_area = ui.column().classes('w-full')
            with ui.row().classes('bar-modal-footer w-full gap-2'):
                self.confirm = ui.button('确认本次内容变化', on_click=self.commit, icon='o_check')
                self.close_button = ui.button('关闭', icon='o_close', on_click=self.close).props('flat color=grey-7').classes('ml-auto')
            self.exclude.set_visibility(self.choice is not None)
            self.restore.set_visibility(self.choice is not None)
            self.confirm.set_visibility(False)
            self.render_rows()
        self.dialog.client.on_disconnect(self.close)
        self.dialog.on_value_change(lambda event: self.close() if not event.value else None)
        self.dialog.open()

    def alive(self):
        return (not self.closed and not self.dialog.is_deleted
            and not getattr(self.owner, 'is_deleted', False) and not getattr(self.owner, 'exit_requested', False))

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.preview = None
        if not self.dialog.is_deleted:
            self.dialog.close()
            self.dialog.delete()
        # Closing the view does not release or cancel a live worker.

    def invalidate(self):
        if self.preview is not None:
            self.preview = None
            self.confirm.set_visibility(False)
            self.confirm.disable()
            self.preview_area.clear()
            self.message.set_text('选择已变化，请重新预览。')

    def selected_change(self, identifier, value):
        if self.busy:
            return
        self.invalidate()
        self.selected.add(identifier) if value else self.selected.discard(identifier)
        self.update_controls()

    def display_rows(self):
        if self.preview is not None:
            value = self.preview.to_dict()
            movie = MovieVersionSelection.from_mapping(value['movie_selection']) if value['movie_selection'] else None
            return selection_rows(ContentSelection.from_mapping(value['selection']), movie, work_title=self.work_title)
        return self.rows

    def column_rows(self, action):
        return [row for row in self.display_rows() if row['included'] == (action == 'exclude')]

    def change_page_size(self, action, size):
        if self.busy or not self.alive() or size not in PAGE_OPTIONS:
            return
        self.page_sizes[action] = size
        self.pages[action] = 0
        self.render_rows()

    def move(self, delta, action='exclude'):
        if self.busy:
            return
        self.pages[action] = min(max(0, self.pages[action] + delta),
            max(0, (len(self.column_rows(action))-1)//self.page_sizes[action]))
        self.render_rows()

    def select_current_page(self, selected, action='exclude'):
        if self.busy or self.choice is None or not self.alive():
            return
        offset = self.pages[action]*self.page_sizes[action]
        keys = {row['identifier'] for row in self.column_rows(action)[offset:offset+self.page_sizes[action]] if not row['locked']}
        self.invalidate()
        self.selected.update(keys) if selected else self.selected.difference_update(keys)
        self.render_rows()

    def select_recommended(self):
        if self.busy or self.choice is None or not self.alive():
            return
        from ..rename.default_content import non_watchable_selection_sources
        suggested = non_watchable_selection_sources(self.choice, work_title=self.work_title)
        self.invalidate()
        self.selected.update(row['identifier'] for row in self.rows if row['included'] and row['source'] in suggested)
        self.render_rows()
        self.notify('已勾选建议项，请核对后点击排除整理；尚未更改计划。', type='info')

    def clear_selection(self, action):
        if self.busy:
            return
        self.invalidate()
        self.selected.difference_update(row['identifier'] for row in self.column_rows(action))
        self.render_rows()

    def update_controls(self):
        for action, pane in self.panels.items():
            rows = self.column_rows(action)
            count, page = len(rows), self.pages[action]
            selected = len(self.selected & {row['identifier'] for row in rows})
            pane['label'].set_text(f'{page*self.page_sizes[action]+1 if count else 0}–{min(count,(page+1)*self.page_sizes[action])} / {count}  已选择 {selected} 项')
            for key in ('first', 'previous'):
                pane[key].set_enabled(not self.busy and page > 0)
            for key in ('following', 'last'):
                pane[key].set_enabled(not self.busy and (page+1)*self.page_sizes[action] < count)
            for key in ('select','clear','button','size'):
                pane[key].set_enabled(not self.busy and self.choice is not None)

    def render_rows(self):
        self.boxes = []
        for action, pane in self.panels.items():
            pane['container'].clear()
            rows = self.column_rows(action)
            self.pages[action] = min(self.pages[action], max(0,(len(rows)-1)//self.page_sizes[action]))
            offset = self.pages[action]*self.page_sizes[action]
            with pane['container']:
                if not rows:
                    self.ui.label('暂无文件').classes('bar-help')
                for row in rows[offset:offset+self.page_sizes[action]]:
                    with self.ui.column().classes('w-full gap-0'):
                        filename = row['source'].relative_to(self.plan.source_root).as_posix()
                        box = self.ui.checkbox(filename,
                            value=row['identifier'] in self.selected,
                            on_change=lambda event, key=row['identifier']: self.selected_change(key, bool(event.value))).classes('w-full min-w-0').tooltip(filename)
                        if row['locked'] or self.busy or self.choice is None:
                            box.disable()
                        self.boxes.append((box, 'locked' if row['locked'] else row['kind']))
                        self.ui.label(row['status']).classes('bar-help')
        self.update_controls()

    def set_busy(self, value):
        self.busy = value
        self.progress.set_visibility(value)
        for widget in (self.confirm,):
            widget.set_enabled(not value)
        for box, kind in self.boxes:
            box.set_enabled(not value and kind != 'locked' and self.choice is not None)
        self.update_controls()

    async def apply(self, action):
        if not self.alive() or self.busy:
            return
        try:
            self.require_view_revision()
            selectors = tuple(sorted(self.selected & {row['identifier'] for row in self.column_rows(action)}))
            if not selectors:
                raise ValueError('请在这一栏至少选择一项内容。')
        except ValueError as exc:
            self.message.set_text(str(exc))
            return
        def operation():
            preview = self.service.prepare_content_change(self.plan.task_id, self.fingerprint,
                selected_entry_ids=selectors, action=action,
                reason='用户选择本次不整理所选内容' if action=='exclude' else '用户选择恢复所选内容整理')
            if not isinstance(preview, ContentChangePreview):
                raise ValueError('内容核查未完成，原计划未改变。')
            if action == 'restore':
                value = preview.to_dict()
                choice = ContentSelection.from_mapping(value['selection'])
                movie = MovieVersionSelection.from_mapping(value['movie_selection']) if value['movie_selection'] else None
                after = {row['identifier']:row for row in selection_rows(choice, movie)}
                if any(not after[key]['included'] for key in selectors):
                    raise ValueError('所选配套文件仍随所属视频排除，请先一并纳入所属视频。')
            return self.commit_result(preview)
        await self.run(operation, self.adopt, saved=True)

    def require_view_revision(self):
        if PlanStore.fingerprint(self.owner.plan) != self.fingerprint:
            raise ValueError('计划已更新，请关闭并重新打开内容工作区；旧选择不会覆盖后来的修订。')

    async def run(self, operation, success, *, saved=False):
        if not self.alive() or self.busy or getattr(self.owner, '_content_choice_worker', None) is not None:
            return
        self.set_busy(True)
        self.message.set_text('正在后台核查；关闭窗口或中断等待不等于取消后台操作。')
        worker = asyncio.create_task(ui_disk_call(operation))
        self.owner._content_choice_worker = worker
        async def finish(done):
            if done is not getattr(self.owner, '_content_choice_worker', None):
                return
            if done.cancelled():
                if self.alive():
                    self.message.set_text('后台终态未知，保留操作锁定；请刷新核对，不要重复提交。')
                return
            try:
                result = done.result()
                if saved:
                    if (not self.owner.is_deleted and not self.owner.exit_requested
                            and self.owner.value):
                        with self.owner.client:
                            await await_result(success(result))
                elif self.alive():
                    with self.dialog:
                        await await_result(success(result))
            except Exception as exc:
                if self.alive():
                    self.message.set_text(str(exc))
            finally:
                self.owner._content_choice_worker = None
                if self.alive():
                    self.set_busy(False)
                else:
                    self.busy = False
        async def complete():
            try:
                await asyncio.shield(worker)
            except (Exception, asyncio.CancelledError):
                pass
            await finish(worker)
        completion = asyncio.create_task(complete())
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError:
            if self.alive():
                self.message.set_text('等待已中断，后台仍在处理；尚不能认定操作已取消。')
        except Exception:
            pass  # The one terminal callback owns errors and release.

    async def prepare(self, action):
        if not self.alive() or self.busy:
            return
        try:
            self.require_view_revision()
            reason = {
                'exclude': '用户选择本次不整理所选内容',
                'restore': '用户选择恢复所选内容整理',
            }[action]
            selectors = tuple(sorted(self.selected))
            if not selectors:
                raise ValueError('请至少选择一项内容。')
        except ValueError as exc:
            self.message.set_text(str(exc))
            return
        self.invalidate()
        def operation():
            if not isinstance(preview, ContentChangePreview):
                raise ValueError('内容预览校验未完成，不能确认；请刷新后重试。')
            current = self.service.plan_store.load(self.plan.task_id)
            if PlanStore.fingerprint(current) != self.fingerprint:
                raise ValueError('计划已更新，请重新打开内容工作区。')
            return preview
        def show(preview):
            self.require_view_revision()
            self.preview = preview
            value = preview.to_dict()
            choice = ContentSelection.from_mapping(value['selection'])
            movie = MovieVersionSelection.from_mapping(value['movie_selection']) if value['movie_selection'] else None
            explicit = set(value['selection']['excluded_sources'])
            versions = set(movie.to_dict()['excluded_sources']) if movie is not None else set()
            omitted = omitted_sources(choice, movie)
            with self.preview_area:
                self.ui.label(f'预览后：独立排除 {len(explicit)} 项；整组排除视频 {len(versions)} 项；'
                    f'连带配套 {len(omitted - {Path(p) for p in explicit | versions})} 项。')
                self.ui.label('上方逐项显示本次预览的保留状态；可分页核对。确认后仅生成新未批准计划。')
            self.message.set_text('请核对本次内容变化；源文件保持原状。')
            self.confirm.set_visibility(True)
            self.confirm.enable()
            self.render_rows()
        await self.run(operation, show)

    async def commit(self):
        if not self.alive() or self.busy:
            return
        try:
            self.require_view_revision()
            preview = self.preview
            if not isinstance(preview, ContentChangePreview):
                raise ValueError('请先重新预览本次内容变化。')
        except ValueError as exc:
            self.message.set_text(str(exc))
            return
        await self.run(lambda: self.commit_result(preview), self.adopt, saved=True)

    def commit_result(self, preview):
        revised = self.service.commit_content_change(preview)
        if not isinstance(revised, RenamePlan):
            raise ValueError('内容提交未返回有效结果，请刷新核对状态；不会回退旧操作。')
        current = self.service.plan_store.load(self.plan.task_id)
        state = self.service.decision_state_store.load(self.plan.task_id)
        if (PlanStore.fingerprint(current) != PlanStore.fingerprint(revised)
                or current.metadata.get('decision_state_fingerprint') != self.service.decision_state_store.fingerprint(state)):
            raise ValueError('本次内容操作已提交，但任务已有后续更新；请刷新核对，不采用旧结果。')
        return revised, state

    async def adopt(self, result):
        revised, state = result
        self.require_view_revision()
        self.owner.task_decision = state
        await await_result(self.owner._adopt_revision(revised))
        if not self.alive():
            return
        self.plan, self.fingerprint = revised, PlanStore.fingerprint(revised)
        self.choice = from_plan(revised)
        raw = revised.metadata.get('movie_version_selection')
        self.movie = MovieVersionSelection.from_mapping(raw) if raw else None
        self.rows = selection_rows(self.choice, self.movie, work_title=self.work_title)
        self.selected.clear()
        self.preview = None
        self.preview_area.clear()
        self.confirm.set_visibility(False)
        self.exclude.set_visibility(True)
        self.restore.set_visibility(True)
        self.render_rows()
        self.message.set_text('已更新整理计划，源文件保持原状。')
        self.notify('内容变化已保存为新未批准计划；源文件保持原状。', type='positive')
