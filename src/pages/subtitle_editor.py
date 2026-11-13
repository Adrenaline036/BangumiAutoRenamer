"""当前计划的字幕标签与文件批选窗口。"""

from src.component.icons import outlined_icon
from ..queue_runtime import ui_disk_call
from nicegui import ui

from ..component.dialog_frame import dialog_card
from ..rename.content_choice import subtitle_edit_view
from ..rename.plan_review import entry_id
from ..rename.plan_store import PlanStore
from ..rename.subtitle_batch import build_batch, subtitle_statuses
from ..rename.subtitle_languages import COMMON, language_key, language_name
from .content_review_view import PAGE_SIZE, PAGE_OPTIONS


async def open_subtitle_editor(owner):
    previous = getattr(owner, '_subtitle_editor', None)
    if previous is not None and previous.dialog.value:
        return
    view, statuses = await ui_disk_call(editor_snapshot, owner.plan)
    with owner.client:
        owner._subtitle_editor = SubtitleEditor(owner, view, statuses)


def editor_snapshot(plan):
    return subtitle_edit_view(plan), subtitle_statuses(plan)


class SubtitleEditor:
    def __init__(self, owner, view, statuses):
        self.owner, self.plan, self.view = owner, owner.plan, view
        self.statuses = statuses
        self.busy = self.sync_pending = self.loading = False
        self.pages = dict(files=0, tags=0)
        self.page_sizes = dict(files=PAGE_SIZE, tags=PAGE_SIZE)
        self.panes = {}
        self.selected = []
        self.entries = {entry_id(e): e for e in view.entries if e.subtitle is not None}
        self.tags = {}
        for key, entry in self.entries.items():
            self.tags.setdefault(entry.subtitle.raw_label or '（无标签）', []).append(key)
        with ui.dialog() as self.dialog, dialog_card().classes('bar-dialog bar-content-dialog bar-modal-fixed'):
            ui.label('调整字幕结构').classes('bar-dialog-title')
            with ui.column().classes('w-full gap-3'):
                with ui.element('div').classes('bar-content-columns w-full'):
                    for kind, title in (('files', '全部字幕文件'), ('tags', '已获取的标签')):
                        with ui.column().classes('bar-content-pane gap-2'):
                            with ui.row().classes('w-full items-center gap-2'):
                                ui.label(title).classes('text-lg mr-auto')
                                select = ui.button('本页全选', on_click=lambda k=kind: self.select_page(k), icon='o_select_all').props('outline color=blue-7')
                                clear = ui.button('取消全选', on_click=lambda: self.set_selection([]), icon='o_deselect').props('outline color=blue-7')
                            container = ui.column().classes('bar-content-files scroll w-full')
                            label = ui.label('').classes('bar-help w-full' + (' text-right' if kind == 'tags' else ''))
                            with ui.row().classes('w-full items-center gap-2'):
                                with ui.row().classes('items-center gap-0 flex-nowrap'):
                                    buttons = [ui.button(icon=outlined_icon(icon), on_click=lambda d=delta, k=kind: self.move_page(k, d)
                                        ).props(f'flat dense aria-label="{name}"') for icon, delta, name in (
                                        ('first_page', -1000000, '第一页'), ('chevron_left', -1, '上一页'),
                                        ('chevron_right', 1, '下一页'), ('last_page', 1000000, '最后一页'))]
                                size = ui.select(list(PAGE_OPTIONS), value=PAGE_SIZE, label='每页文件数',
                                    on_change=lambda e, k=kind: self.resize_page(k, e.value)).props('dense outlined').classes('w-28')
                            self.panes[kind] = dict(container=container, label=label, buttons=buttons,
                                select=select, clear=clear, size=size, checks={})
                ui.separator().classes('my-2')
                with ui.row().classes('w-full gap-3'):
                    self.main = ui.select({**COMMON, 'other': '其他语言'}, label='主语言',
                        on_change=self.edited).props('outlined dense').classes('grow')
                    self.other_main = ui.input('语言代码，例如 fr', on_change=self.edited
                        ).props('outlined dense').classes('grow')
                self.has_additional = ui.checkbox('包含附加语言', on_change=self.edited)
                self.additional = ui.select({**COMMON, 'other': '其他语言'}, multiple=True, value=[],
                    label='附加语言', on_change=self.edited
                ).props('outlined dense use-chips').props(":option-disable=\"opt => opt.label.endsWith(' · 主语言')\"").classes('w-full')
                self.other_additional = ui.input('附加语言代码，用逗号分隔', on_change=self.edited
                    ).props('outlined dense').classes('w-full')
                self.remove = ui.input('应删除的字幕标签（可选，用逗号分隔）', on_change=self.edited
                    ).props('outlined dense').classes('w-full')
                self.preview = ui.column().classes('w-full gap-1')
            with ui.row().classes('bar-modal-footer w-full items-center gap-2'):
                self.save_button = ui.button('保存字幕调整', icon='o_save', on_click=self.save).props('outline color=blue-7')
                self.feedback = ui.label('').classes('bar-help')
                self.retry_button = ui.button('刷新整理页面', icon='o_refresh', on_click=self.sync_parent).props('flat color=blue-7')
                self.close_button = ui.button('关闭', icon='o_close', on_click=self.dialog.close).props('flat color=grey-8').classes('ml-auto')
        self.render_lists()
        self.changed()
        self.dialog.open()

    def filename(self, entry):
        try:
            return entry.source_path.relative_to(self.plan.source_root).as_posix()
        except ValueError:
            return entry.source_path.name

    def row_keys(self, kind):
        return list(self.entries if kind == 'files' else self.tags)

    def row_members(self, kind, key):
        return [key] if kind == 'files' else self.tags[key]

    def set_selection(self, selected):
        if self.busy or self.sync_pending:
            return
        wanted = set(selected)
        self.selected = [key for key in self.entries if key in wanted]
        self.selection_changed()
        self.render_lists()

    def select_row(self, kind, key, value):
        chosen, members = set(self.selected), set(self.row_members(kind, key))
        self.set_selection(chosen | members if value else chosen - members)

    def select_page(self, kind):
        start = self.pages[kind] * self.page_sizes[kind]
        chosen = set(self.selected)
        for key in self.row_keys(kind)[start:start+self.page_sizes[kind]]:
            chosen.update(self.row_members(kind, key))
        self.set_selection(chosen)

    def move_page(self, kind, delta):
        self.pages[kind] = max(0, min(self.pages[kind] + delta,
            max(0, (len(self.row_keys(kind))-1)//self.page_sizes[kind])))
        self.render_lists()

    def resize_page(self, kind, size):
        if size in PAGE_OPTIONS:
            self.page_sizes[kind], self.pages[kind] = size, 0
            self.render_lists()

    def render_lists(self):
        chosen = set(self.selected)
        for kind, pane in self.panes.items():
            keys, size = self.row_keys(kind), self.page_sizes[kind]
            self.pages[kind] = min(self.pages[kind], max(0, (len(keys)-1)//size))
            start = self.pages[kind] * size
            pane['container'].clear()
            pane['checks'] = {}
            with pane['container']:
                if not keys:
                    ui.label('暂无字幕' if kind == 'files' else '暂无标签').classes('bar-help')
                for key in keys[start:start+size]:
                    members = set(self.row_members(kind, key))
                    value = True if members <= chosen else None if members & chosen else False
                    name = self.filename(self.entries[key]) if kind == 'files' else key
                    with ui.column().classes('w-full gap-0'):
                        pane['checks'][key] = ui.checkbox(name, value=value,
                            on_change=lambda e, k=kind, item=key: self.select_row(k, item, e.value)
                        ).props('color=blue-7').classes('w-full min-w-0').tooltip(name)
                        if kind == 'tags':
                            ui.label(f'{len(members)} 个文件').classes('bar-help')
                        else:
                            path = self.entries[key].source_path
                            state, hint = self.statuses.get(path, ('', ''))
                            text = ' · '.join(filter(None, (state, hint,
                                '不整理' if path in self.plan.ignored_sources else '')))
                            if text:
                                ui.label(text).classes('bar-help')
            selected = sum(set(self.row_members(kind, key)) <= chosen for key in keys)
            pane['label'].text = f'{start+1 if keys else 0}–{min(len(keys),start+size)} / {len(keys)}  已选择 {selected} 项'
            for button, enabled in zip(pane['buttons'], (start > 0, start > 0, start+size < len(keys), start+size < len(keys))):
                button.set_enabled(enabled)

    def selection_changed(self, _=None):
        if self.loading:
            return
        contracts = [self.entries[key].subtitle for key in self.selected]
        self.loading = True
        mains = {f'zh-{c.script}' if c.primary_language == 'zh' and c.script else c.primary_language for c in contracts}
        main = next(iter(mains)) if len(mains) == 1 else None
        self.main.value = main if main in COMMON else 'other' if main else None
        self.other_main.value = main if main and main not in COMMON else ''
        additions = {tuple(c.additional_languages) for c in contracts}
        additional = next(iter(additions)) if len(additions) == 1 else ()
        self.has_additional.value = bool(additional) if len(additions) <= 1 else None
        self.additional.value = [v for v in additional if v in COMMON] + (['other'] if any(v not in COMMON for v in additional) else [])
        self.other_additional.value = ','.join(v for v in additional if v not in COMMON)
        self.remove.value = ''
        self.loading = False
        self.feedback.text = '尚未保存本次调整'
        self.changed()

    def arguments(self):
        if self.has_additional.value is None:
            raise ValueError('已选字幕包含不同附加语言，请确认是否包含附加语言')
        main = self.other_main.value if self.main.value == 'other' else self.main.value
        additional = list(self.additional.value or []) if self.has_additional.value else []
        if 'other' in additional:
            additional.remove('other')
            codes = self.split(self.other_additional.value)
            if not codes:
                raise ValueError('请填写附加语言代码')
            additional.extend(codes)
        if self.has_additional.value and not additional:
            raise ValueError('请选择附加语言')
        return dict(main_language=main, additional_languages=additional, remove_tags=self.split(self.remove.value))

    @staticmethod
    def split(value):
        return tuple(v.strip() for v in str(value or '').replace('，', ',').split(',') if v.strip())

    def edited(self, event):
        if not self.loading and not self.busy and not self.sync_pending:
            self.feedback.text = '尚未保存本次调整'
        self.changed()

    def refresh_languages(self):
        try:
            main = language_key(self.other_main.value if self.main.value == 'other' else self.main.value)
        except ValueError:
            main = None
        options = dict(COMMON)
        if main and main not in options:
            options[main] = language_name(main)
        options = {key: name + (' · 主语言' if key == main else '') for key, name in options.items()}
        options['other'] = '其他语言'
        self.loading = True
        self.additional.set_options(options, value=[v for v in self.additional.value or [] if v != main])
        def same_main(code):
            try:
                return language_key(code) == main
            except ValueError:
                return False
        codes = self.split(self.other_additional.value)
        if any(same_main(code) for code in codes):
            self.other_additional.value = ','.join(code for code in codes if not same_main(code))
        self.loading = False

    def changed(self, _=None):
        if self.loading or not hasattr(self, 'save_button'):
            return
        self.other_main.set_visibility(self.main.value == 'other')
        self.additional.set_visibility(bool(self.has_additional.value))
        self.other_additional.set_visibility(bool(self.has_additional.value) and 'other' in (self.additional.value or []))
        self.retry_button.set_visibility(self.sync_pending)
        self.refresh_languages()
        self.preview.clear()
        valid = False
        with self.preview:
            if not self.selected:
                ui.label('请选择要调整的字幕文件').classes('bar-help')
            else:
                try:
                    arguments = self.arguments()
                    revised = build_batch(self.view, PlanStore.fingerprint(self.plan), self.selected, **arguments)
                    names = {entry_id(e): e.target_path.name for e in revised.entries}
                    ui.label('预览').classes('bar-help')
                    ui.label(names[self.selected[0]]).classes('bar-help break-all')
                    valid = True
                except (ValueError, RuntimeError) as exc:
                    ui.label(str(exc)).classes('bar-help')
        self.save_button.set_enabled(valid and not self.busy and not self.sync_pending)

    async def save(self):
        if self.busy or self.sync_pending or not self.dialog.value:
            return
        try:
            arguments = self.arguments()
        except ValueError as exc:
            self.feedback.text = f'未保存：{exc}'
            return
        await self.commit(self.owner.review_service.adjust_subtitles, tuple(self.selected), **arguments)

    async def commit(self, command, *args, **kwargs):
        self.busy = True
        self.changed()
        self.feedback.text = '正在保存字幕调整…'
        try:
            revised = await ui_disk_call(command, self.plan.task_id, PlanStore.fingerprint(self.plan), *args, **kwargs)
        except Exception as exc:
            self.feedback.text = f'未保存：{exc}'
        else:
            self.plan = revised
            self.sync_pending = True
            self.saved_message = '所选字幕调整已保存'
            self.feedback.text = self.saved_message
            await self.sync_parent()
        finally:
            self.busy = False
            self.changed()

    async def sync_parent(self):
        try:
            self.view, self.statuses = await ui_disk_call(editor_snapshot, self.plan)
            self.entries = {entry_id(e): e for e in self.view.entries if e.subtitle is not None}
            self.render_lists()
            await self.owner._adopt_revision(self.plan)
        except Exception:
            self.feedback.text = '字幕调整已保存，页面刷新失败；请点击“刷新整理页面”'
        else:
            self.sync_pending = False
            self.feedback.text = self.saved_message
        self.changed()
