"""在原审核窗口确认正片用途及人工季集编号。"""
from nicegui import ui
from ..component.dialog_frame import dialog_card
from ..rename.plan_review import entry_id
from ..rename.plan_store import PlanStore
from ..rename.episode_range import numbers
from .episode_range_editor import EpisodeRangeEditor
from . import tv_content_actions


def open_main_episode_editor(owner):
    previous = getattr(owner, '_main_episode_editor', None)
    if previous is not None and previous.dialog.value:
        return
    owner._main_episode_editor = MainEpisodeEditor(owner)


class MainEpisodeEditor:
    integer_input = EpisodeRangeEditor.integer_input
    step = EpisodeRangeEditor.step

    def __init__(self, owner):
        self.owner = owner
        self.busy = False
        self.capture()
        with ui.dialog() as self.dialog, dialog_card().classes('bar-dialog bar-content-dialog'):
            ui.label('作为正片整理').classes('bar-dialog-title')
            self.files = ui.select({k: e.source_path.name for k, e in self.entries.items()},
                label='选择正片视频（可多选）', multiple=True, value=[],
                on_change=self.selected).props('outlined dense use-chips').classes('w-full')
            ui.button('选择已识别正片', on_click=lambda: self.files.set_value(
                [k for k, e in self.entries.items() if e.role == 'main_episode']), icon='o_checklist').props('flat')
            with ui.row().classes('w-full gap-3'):
                self.season = self.integer_input('季号', 0)
                self.start = self.integer_input('起始集号', 0)
            self.season.set_value(str((owner.task_decision.season_binding or {}).get('season_number', 1)))
            self.start.set_value('1')
            self.preview = ui.label('请选择视频；单个视频直接指定集号，多选按下方顺序编号。').classes('bar-help whitespace-pre-wrap')
            ui.label('视频及关联字幕一起更新；自定编号与资料源不同时，自动刮削可能不匹配。').classes('bar-help')
            with ui.row().classes('bar-modal-footer w-full items-center gap-2'):
                self.save_button = ui.button('保存正片编号', icon='o_save', on_click=self.save).props('outline color=blue-7')
                self.feedback = ui.label('').classes('bar-help')
                ui.button('关闭', icon='o_close', on_click=self.dialog.close).props('flat color=grey-8').classes('ml-auto')
        self.dialog.open()

    def capture(self):
        self.entries = {entry_id(e): e for e in self.owner.plan.entries
            if e.media_kind == 'video' and e.source_path not in self.owner.plan.ignored_sources}
        self.revision = self.owner.task_decision.revision
        self.fingerprint = PlanStore.fingerprint(self.owner.plan)

    def selected(self, _event=None):
        selected = self.files.value or []
        if len(selected) == 1 and hasattr(self, 'start'):
            e = self.entries[selected[0]]
            if e.episode_id is not None:
                self.start.set_value(str(e.episode_id))
        self.changed()

    def rows(self):
        chosen = self.files.value or []
        if not chosen or len(set(chosen)) != len(chosen) or any(k not in self.entries for k in chosen):
            raise ValueError('请选择本次保留的正片视频')
        raw = tuple(str(field.value or '').strip() for field in (self.season, self.start))
        if any(not value.isdecimal() for value in raw):
            raise ValueError('请填写整数季号与集号')
        season, start, _ = numbers(int(raw[0]), int(raw[1]), int(raw[1]) + len(chosen) - 1)
        chosen = sorted(chosen, key=lambda k: (
            self.entries[k].episode_id is None,
            self.entries[k].episode_id if self.entries[k].episode_id is not None else 0,
            self.entries[k].source_path.name))
        provider = self.owner.plan.work_identity.primary_provider.namespace
        return [dict(entry_id=k, season=season, start=start+i, end=start+i, naming_mode=provider)
                for i, k in enumerate(chosen)]

    def changed(self, _event=None):
        if not hasattr(self, 'preview'):
            return
        try:
            rows = self.rows()
            self.preview.set_text('\n'.join(
                f"{self.entries[r['entry_id']].source_path.name} → S{r['season']:02d}E{r['start']:02d}"
                for r in rows))
        except ValueError as exc:
            self.preview.set_text(str(exc))

    async def save(self):
        if self.busy:
            return
        try:
            rows = self.rows()
            self.busy = True
            self.save_button.disable()
            result = await tv_content_actions.revise(self.owner, mode='range',
                edit={'season': rows[0]['season'], 'rows': rows},
                expected_revision=self.revision, expected_fingerprint=self.fingerprint,
                notify=lambda message, **_: self.feedback.set_text(str(message)),
                message='正片编号已保存', quiet=True)
            if result is not None:
                self.capture()
                self.feedback.set_text('正片编号已保存，可继续调整')
        except (ValueError, RuntimeError) as exc:
            self.feedback.set_text('未保存：' + str(exc))
        finally:
            self.busy = False
            if not self.save_button.is_deleted:
                self.save_button.enable()
