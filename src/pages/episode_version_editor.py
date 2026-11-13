"""同一任务内按集选择版本，可批量填写同一版本标签。"""
from nicegui import ui
from ..component.dialog_frame import dialog_card
from ..rename.plan_review import entry_id
from ..rename.plan_store import PlanStore
from ..rename.episode_versions import from_plan, safe_label
from . import tv_content_actions


def open_episode_version_editor(owner):
    previous = getattr(owner, '_episode_version_editor', None)
    if previous is not None and previous.dialog.value:
        return
    owner._episode_version_editor = EpisodeVersionEditor(owner)


class EpisodeVersionEditor:
    def __init__(self, owner):
        self.owner, self.busy = owner, False
        self.revision = owner.task_decision.revision
        self.fingerprint = PlanStore.fingerprint(owner.plan)
        self.entries = {entry_id(e): e for e in owner.plan.entries if e.media_kind == 'video'}
        self.fields = {}
        decisions = {d.source_path: d for d in from_plan(owner.plan)}
        with ui.dialog() as self.dialog, dialog_card().classes('bar-dialog bar-content-dialog'):
            ui.label('设置剧集版本').classes('bar-dialog-title')
            ui.label('同一集的每个保留版本都需勾选并填写标签，例如 4x3、16x9。普通单版本可不选。').classes('bar-help')
            self.batch_files = ui.select({k: e.source_path.name for k, e in self.entries.items()},
                label='批量选择视频', multiple=True, value=[]).props('outlined dense use-chips').classes('w-full')
            with ui.row().classes('w-full items-center'):
                self.batch_label = ui.input('批量版本标签').props('outlined dense').classes('grow')
                ui.button('应用标签', on_click=self.apply_batch, icon='o_label').props('outline')
            with ui.column().classes('w-full gap-3').style('max-height:50vh;overflow:auto'):
                for identifier, entry in self.entries.items():
                    d = decisions.get(str(entry.source_path))
                    episode = d.episode if d else entry.episode_id
                    if episode is None:
                        from ..rename.episode_inference import build_episode_hints, EpisodeInferenceError
                        try:
                            episode = build_episode_hints((entry.source_path,), '').get(entry.source_path)
                        except EpisodeInferenceError:
                            pass
                    with ui.column().classes('w-full gap-1'):
                        selected = ui.checkbox(entry.source_path.name, value=d is not None)
                        with ui.row().classes('w-full'):
                            number = ui.input('集数', value='' if episode is None else str(episode)).props('outlined dense inputmode=numeric')
                            label = ui.input('版本标签', value=d.label if d else '').props('outlined dense').classes('grow')
                        self.fields[identifier] = dict(selected=selected, episode=number, label=label)
            self.feedback = ui.label('').classes('bar-help')
            with ui.row().classes('bar-modal-footer w-full'):
                self.save_button = ui.button('保存剧集版本', icon='o_save', on_click=self.save).props('outline color=blue-7')
                ui.button('关闭', on_click=self.dialog.close, icon='o_close').props('flat').classes('ml-auto')
        self.dialog.open()

    def apply_batch(self):
        try:
            label = safe_label(self.batch_label.value)
            for identifier in self.batch_files.value or ():
                self.fields[identifier]['selected'].set_value(True)
                self.fields[identifier]['label'].set_value(label)
            self.feedback.set_text('标签已填入，保存后生效')
        except ValueError as exc:
            self.feedback.set_text(str(exc))

    async def save(self):
        if self.busy:
            return
        try:
            rows = []
            for identifier, fields in self.fields.items():
                if fields['selected'].value:
                    text = str(fields['episode'].value or '').strip()
                    if not text.isdecimal():
                        raise ValueError('请填写整数集数')
                    rows.append(dict(entry_id=identifier, episode=int(text), label=safe_label(fields['label'].value)))
            self.busy = True
            self.save_button.disable()
            def notice(message, **_kwargs):
                self.feedback.set_text('未保存：' + str(message))
            committed = await tv_content_actions.revise(self.owner, mode='versions', edit={'rows': rows},
                expected_revision=self.revision, expected_fingerprint=self.fingerprint,
                notify=notice, message='剧集版本已保存', quiet=True)
            if committed is not None:
                plan, state = committed
                self.revision = state.revision
                self.fingerprint = PlanStore.fingerprint(plan)
                self.feedback.set_text('剧集版本已保存')
        except (ValueError, RuntimeError) as exc:
            self.feedback.set_text('未保存：' + str(exc))
        finally:
            self.busy = False
            self.save_button.enable()
