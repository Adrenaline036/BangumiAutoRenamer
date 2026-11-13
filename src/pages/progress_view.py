"""The task-card's existing Quasar bar appearance, for other progress views."""


def bar_progress(ui_api, value=0, *, indeterminate=True):
    progress = ui_api.linear_progress(value=value, show_value=False).props(
        'color=blue-7 track-color=blue-2' + (' indeterminate' if indeterminate else '')
    ).classes('w-full').style('height: 4px; border-radius: 999px;')
    return progress
