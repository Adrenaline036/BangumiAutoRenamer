"""One fixed header/body/footer layout; existing controls and handlers survive."""
from nicegui import ui


class dialog_card(ui.card):
    """The caller declares header count and marks its footer with bar-modal-footer.

    Organizing after construction keeps existing dynamic containers and callback
    references intact. No button-text inference, timers, browser JS or I/O.
    """

    def __init__(self, *, header=1):
        super().__init__()
        self.header_count = header
        self.classes('bar-modal-fixed')

    def __exit__(self, *exc):
        super().__exit__(*exc)
        if exc and exc[0] is not None:
            return
        children = list(self.default_slot.children)
        if not children or any('bar-modal-header' in child.classes for child in children):
            return
        headings = children[:self.header_count]
        actions = [child for child in children if 'bar-modal-footer' in child.classes]
        content = [child for child in children if child not in headings and child not in actions]
        self.classes(remove='scroll')
        # Use the slot directly: entering this card here would recursively frame it.
        with self.default_slot:
            header = ui.column().classes('bar-modal-header w-full')
            if len(content) == 1 and 'bar-modal-body' in content[0].classes:
                body = content[0]
                body.move(self)
            else:
                body = ui.column().classes('bar-modal-body scroll w-full')
                for child in content:
                    child.move(body)
            footer = ui.row().classes('bar-modal-footer w-full items-center gap-2')
            footer.set_visibility(bool(actions))
        for child in headings:
            child.move(header)
        for child in actions:
            child.classes(remove='bar-modal-footer')
            child.move(footer)
