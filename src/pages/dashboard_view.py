"""每个浏览器独立的轻量任务列表；切页只读取已经发布的显示快照。"""

from src.component.icons import outlined_icon
import json
from math import ceil
from nicegui import ui


PAGE_SIZE = 20


class TaskSection:
    def __init__(self, render_card, empty_title, empty_hint):
        self.render_card = render_card
        self.rows, self.cards, self.page = [], {}, 1
        self.page_controls = []
        with ui.column().classes('w-full gap-3'):
            with ui.row().classes('w-full items-center justify-between'):
                self.caption = ui.label('尚未读取').classes('bar-muted')
                self.previous, self.next = self._render_pagination()
            self.container = ui.column().classes('w-full gap-3')
            with ui.column().classes('bar-empty-state w-full items-center') as self.empty:
                ui.icon('o_inbox', size='42px').classes('bar-muted')
                ui.label(empty_title).classes('text-lg font-medium')
                ui.label(empty_hint).classes('bar-muted text-center')
            self.empty.set_visibility(False)
            with ui.row().classes('w-full justify-end pt-2'):
                self._render_pagination()

    def _render_pagination(self):
        with ui.row().classes('gap-2 no-wrap'):
            previous = ui.button('上一页', on_click=lambda: self.change_page(-1), icon='o_chevron_left').props('flat dense')
            following = ui.button('下一页', on_click=lambda: self.change_page(1)).props('flat dense icon-right=o_chevron_right')
        previous.disable()
        following.disable()
        self.page_controls.append((previous, following))
        return previous, following

    def change_page(self, delta):
        self.page = max(1, min(self.page + delta, max(1, ceil(len(self.rows)/PAGE_SIZE))))
        self.publish(self.rows)

    def publish(self, rows):
        self.rows = rows
        pages = max(1, ceil(len(rows)/PAGE_SIZE))
        self.page = min(self.page, pages)
        selected = rows[(self.page-1)*PAGE_SIZE:self.page*PAGE_SIZE]
        ids = {str(row['uuid']) for row in selected}
        for task_id in tuple(self.cards):
            if task_id not in ids:
                self.cards.pop(task_id)[1].delete()
        for index, row in enumerate(selected):
            task_id = str(row['uuid'])
            signature = json.dumps({k:v for k,v in row.items() if k != '_order'},
                                   ensure_ascii=False, sort_keys=True)
            old = self.cards.get(task_id)
            if old is None:
                with self.container:
                    wrapper = ui.column().classes('w-full gap-0')
            else:
                wrapper = old[1]
            if old is None or old[0] != signature:
                wrapper.clear()
                with wrapper:
                    self.render_card(row)
                self.cards[task_id] = (signature, wrapper)
            wrapper.move(self.container, target_index=index)
        self.caption.set_text(f'共 {len(rows)} 个任务 · 第 {self.page} / {pages} 页')
        self.empty.set_visibility(not rows)
        for previous, following in self.page_controls:
            previous.set_enabled(self.page > 1)
            following.set_enabled(self.page < pages)


class DashboardView:
    def __init__(self, *, render_card, refresh):
        self.loaded = False
        self.sections = {}
        with ui.column().classes('bar-workspace w-full'):
            with ui.row().classes('w-full items-end justify-between gap-4'):
                with ui.column().classes('gap-1'):
                    ui.label('任务工作台').classes('bar-page-title')
                    ui.label('排队、处理、待处理和历史结果分区展示；状态变化时自动更新。').classes('bar-muted')
                self.total = ui.badge('正在读取', color='grey-7').props('outline')
            with ui.row().classes('bar-stat-grid w-full'):
                self.counts = {}
                for key, label, icon, tone in [('queued','排队中','schedule','amber'),
                    ('running','处理中','autorenew','blue'), ('review','待处理','fact_check','red'),
                    ('completed','已完成','task_alt','green')]:
                    with ui.card().classes(f'bar-stat-card bar-tone-{tone}'):
                        with ui.row().classes('items-center justify-between w-full'):
                            ui.icon(outlined_icon(icon)).classes('bar-stat-icon')
                            self.counts[key] = ui.label('—').classes('bar-stat-value')
                        ui.label(label).classes('bar-stat-label')
            with ui.column().classes('w-full gap-1').props('role=status aria-live=polite') as self.status:
                with ui.row().classes('items-center gap-2'):
                    self.status_text = ui.label('正在读取任务列表…').classes('bar-muted')
                    self.retry = ui.button('重新读取', on_click=lambda: refresh(force=True), icon='o_refresh').props('flat dense')
                self.progress = ui.linear_progress(show_value=False).props('indeterminate')
                self.retry.set_visibility(False)
            with ui.tabs().classes('bar-tabs w-full') as tabs:
                active = ui.tab('active', label='任务队列', icon='o_playlist_play')
                review = ui.tab('review', label='待处理', icon='o_rule')
                history = ui.tab('history', label='任务历史', icon='o_history')
            self.tabs = tabs
            with ui.tab_panels(tabs, value=active).classes('bar-tab-panels w-full'):
                for key, tab, title, hint in [
                    ('active', active, '当前没有排队或处理中的任务', '点击右上角“添加任务”后，任务会出现在这里。'),
                    ('review', review, '没有等待处理的整理任务', '识别存在不确定项时会在这里暂停。'),
                    ('history', history, '还没有历史任务', '已完成、失败或中断的任务会保留在这里。')]:
                    with ui.tab_panel(tab).classes('p-0 pt-4'):
                        self.sections[key] = TaskSection(render_card, title, hint)

    def show_queue(self):
        self.tabs.set_value('active')

    def reading(self):
        self.status.set_visibility(True)
        self.progress.set_visibility(True)
        self.retry.set_visibility(False)
        self.status_text.set_text('正在更新任务列表…' if self.loaded else '正在读取任务列表…')

    def finished(self, *, failed=False):
        self.progress.set_visibility(False)
        self.status.set_visibility(failed)
        self.retry.set_visibility(failed)
        if failed:
            self.status_text.set_text('任务列表暂时无法读取，已保留上次显示。' if self.loaded else '任务列表暂时无法读取，请重试。')

    def publish(self, sections, counts):
        for key, rows in sections.items():
            if key == 'review':
                # 新返回的任务立即置顶；全表刷新沿用已显示的顺序。
                positions = {str(row['uuid']): index for index, row in enumerate(self.sections[key].rows)}
                rows = sorted(rows, key=lambda row: positions.get(str(row['uuid']), -1))
            self.sections[key].publish(rows)
        for key, value in counts.items():
            self.counts[key].set_text(str(value))
        self.total.set_text(f'共 {sum(len(rows) for rows in sections.values())} 个任务')
        self.loaded = True
