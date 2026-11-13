"""Shared season input; reference labels never own the effective choice."""
from ..rename.season_identity import require_manual_season


def season_label(number):
    return '特别篇（第 0 季）' if number == 0 else f'第 {number} 季'


class SeasonChoice:
    def __init__(self, ui, available, *, current, label):
        self.current, self.edited = current, False
        with ui.column().classes('bar-season-choice w-full gap-2'):
            self.reference_container = ui.column().classes('w-full gap-2')
            options = {n: season_label(n) for n in available}
            with ui.row().classes('bar-season-fields w-full items-center') as self.controls_row:
                self.manual = ui.checkbox('手动填写季号', value=not options or current is not None and current not in options)
                self.picker = ui.select(options, value=current if current in options else None,
                                        label=label).props('outlined dense').classes('w-full')
                self.custom = ui.number(label='自定季号', value=current, min=0, step=1, precision=0).props('outlined dense').classes('w-full')
            self.picker.bind_enabled_from(self.manual, 'value', backward=lambda value: not value)
            self.custom.bind_enabled_from(self.manual, 'value')
            for control in (self.manual, self.picker, self.custom):
                control.on_value_change(self._edited)
            ui.label('勾选后使用自定季号；取消勾选后使用列表选项。特别篇为 0。').classes('bar-help')

    def _edited(self, _event):
        self.edited = True

    def __call__(self):
        value = self.custom.value if self.manual.value else self.picker.value
        if type(value) is float and value.is_integer():
            value = int(value)
        return require_manual_season(value)

    def signature(self):
        return bool(self.manual.value), self()

    def annotate(self, names, *, not_found=False):
        # Preserve the frozen observation's keys, ordering and selected value.
        options = {n: season_label(n) + (' · Bangumi：' + names[n] if n in names else
                   ' · 未找到对应' if not_found and n > 0 else '') for n in self.picker.options}
        self.picker.set_options(options, value=self.picker.value)

    def suggest_current_subject(self, number):
        """A suggestion concerns this Bangumi subject, never a sibling."""
        if number is None:
            return
        was_edited = self.edited
        options = {**self.picker.options, number: season_label(number)}
        self.picker.set_options(options, value=self.picker.value if self.picker.value in options else None)
        if not was_edited and self.current is None:
            self.picker.set_value(number)
            self.manual.set_value(False)


def season_choice(ui, available, *, current=None, label='选择季度'):
    return SeasonChoice(ui, available, current=current, label=label)
