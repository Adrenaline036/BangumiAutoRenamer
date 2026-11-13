"""单视频连续集数的人工调整窗口。"""

from src.component.icons import outlined_icon
from nicegui import ui
from ..component.dialog_frame import dialog_card
from ..rename.plan_review import entry_id
from ..rename.plan_store import PlanStore
from ..rename.episode_range import numbers, from_plan
from . import tv_content_actions


def open_episode_range_editor(owner):
    previous = getattr(owner, '_episode_range_editor', None)
    if previous is not None and previous.dialog.value:
        return
    owner._episode_range_editor = EpisodeRangeEditor(owner)


class EpisodeRangeEditor:
    def __init__(self, owner):
        self.owner = owner
        self.busy = False
        self.entries = {entry_id(e): e for e in owner.plan.entries if e.media_kind == 'video'}
        with ui.dialog() as self.dialog, dialog_card().classes('bar-dialog bar-content-dialog'):
            ui.label('设置连续集数').classes('bar-dialog-title')
            self.video = ui.select({k: e.source_path.name for k, e in self.entries.items()},
                label='选择视频', on_change=self.selected).props('outlined dense').classes('w-full')
            with ui.row().classes('w-full gap-3'):
                self.season = self.integer_input('季号', 0)
                self.start = self.integer_input('起始集数', 0)
                self.end = self.integer_input('结束集数', 0)
            self.mode = ui.select({'bangumi': 'Bangumi', 'tmdb': 'TMDB'}, label='命名方式',
                value='bangumi', on_change=self.changed).props('outlined dense').classes('w-full')
            self.preview = ui.label('请选择视频并填写连续集数').classes('bar-help')
            ui.label('一个文件保留为一个视频，关联字幕使用相同季号和起止集数。').classes('bar-help')
            ui.label('尚无逐集对应资料时，Bangumi 使用范围标题，不填写单集 ID。').classes('bar-help')
            with ui.row().classes('bar-modal-footer w-full items-center gap-2'):
                self.save_button = ui.button('保存连续集数', icon='o_save', on_click=self.save).props('outline color=blue-7')
                self.feedback = ui.label('').classes('bar-help')
                ui.button('关闭', icon='o_close', on_click=self.dialog.close).props('flat color=grey-8').classes('ml-auto')
        self.dialog.open()

    def integer_input(self, label, minimum):
        # 保留完整键盘输入；步进区按整个输入框居中，整数校验在预览及保存时进行。
        field = ui.input(label, on_change=self.changed).props('outlined dense inputmode=numeric').classes('grow')
        with field.add_slot('append'):
            with ui.column().classes('gap-0 justify-center').style('height:100%'):
                for delta, icon, action in ((1, 'arrow_drop_up', '增加'), (-1, 'arrow_drop_down', '减少')):
                    ui.button(icon=outlined_icon(icon), on_click=lambda d=delta: self.step(field, d, minimum)).props(
                        f'flat round dense color=grey-7 aria-label="{action}{label}"').style(
                        'min-height:16px;height:16px;min-width:24px;width:24px;padding:0')
        field.on('keydown.up.prevent', lambda: self.step(field, 1, minimum))
        field.on('keydown.down.prevent', lambda: self.step(field, -1, minimum))
        return field

    def step(self, field, delta, minimum):
        text = str(field.value or '').strip()
        if text and not text.isdecimal():
            self.feedback.set_text('请填写整数季号及起止集数')
            return
        field.set_value(str(max(minimum, int(text) + delta) if text else minimum))

    def selected(self, _event=None):
        entry = self.entries.get(self.video.value)
        if entry is None:
            return
        decision = next((d for d in from_plan(self.owner.plan) if d.source_path == str(entry.source_path)), None)
        values = (decision.season, decision.start, decision.end) if decision else (
            entry.season_id, entry.episode_id, entry.episode_end or entry.episode_id)
        for field, value in zip((self.season, self.start, self.end), values):
            field.set_value('' if value is None else str(value))
        if decision:
            self.mode.set_value(decision.naming_mode)
        self.changed()

    def values(self):
        values = tuple('' if field.value is None else str(field.value).strip()
                       for field in (self.season, self.start, self.end))
        if any(not value.isdecimal() for value in values):
            raise ValueError('请填写整数季号及起止集数')
        return numbers(*(int(value) for value in values))

    def changed(self, _event=None):
        if not hasattr(self, 'preview'):
            return
        self.feedback.set_text('尚未保存本次调整')
        try:
            season, start, end = self.values()
            entry = self.entries.get(self.video.value)
            if entry is None:
                raise ValueError('请选择一个视频')
            from ..rename.content_selection import canonical_episode_target
            from pathlib import Path
            target = canonical_episode_target(Path(self.owner.plan.metadata['canonical_work_root']),
                season, start, entry.source_path.suffix, episode_end=end)
            self.preview.set_text('预览：' + target.name)
        except (ValueError, KeyError):
            self.preview.set_text('请选择视频并填写连续集数')

    async def save(self):
        if self.busy:
            return
        try:
            season, start, end = self.values()
            if self.video.value not in self.entries:
                raise ValueError('请选择一个视频')
            self.busy = True
            self.save_button.disable()
            self.feedback.set_text('正在保存…')
            before = self.owner.task_decision.revision
            def notice(message, **_kwargs):
                self.feedback.set_text(str(message))
            await tv_content_actions.revise(self.owner, mode='range', edit=dict(
                entry_id=self.video.value, season=season, start=start, end=end, naming_mode=self.mode.value),
                expected_revision=before, expected_fingerprint=PlanStore.fingerprint(self.owner.plan),
                notify=notice, message='连续集数已保存', quiet=True)
            if self.owner.task_decision.revision > before:
                self.entries = {entry_id(e): e for e in self.owner.plan.entries if e.media_kind == 'video'}
                self.feedback.set_text('连续集数已保存，可继续调整')
        except (ValueError, RuntimeError) as exc:
            self.feedback.set_text('未保存：' + str(exc))
        finally:
            self.busy = False
            self.save_button.enable()
