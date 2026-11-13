"""隔离 NiceGUI 控件的真实事件驱动。"""
import asyncio
import unittest
from unittest.mock import patch

class GuiDriver(unittest.IsolatedAsyncioTestCase):
    def button(self, text):
        buttons = [element for element in self.client.elements.values()
                   if type(element).__name__ == 'Button' and getattr(element, 'text', '') == text
                   and all(not parent.is_deleted and parent.client.elements.get(parent.id) is parent
                           and getattr(parent, 'visible', True) and
                           (type(parent).__name__ != 'Dialog' or parent.value)
                           for parent in element.ancestors(include_self=True))]
        self.assertEqual(1, len(buttons), text)
        return buttons[0]

    async def click(self, text):
        await self.click_element(self.button(text))

    async def click_element(self, button):
        # Feed the actual framework event, including its async dispatch loop.
        from nicegui import core
        listener = next(value for value in button._event_listeners.values() if value.type == 'click')
        with self.client, patch.multiple(core, loop=asyncio.get_running_loop()):
            before = asyncio.all_tasks()
            button._handle_event({'listener_id': listener.id, 'args': []})
            created = asyncio.all_tasks() - before
            if created:
                await asyncio.gather(*created)

class _Node:
    def __init__(self, *, value=None, ui=None, button_label=None):
        self.value = value
        self._ui = ui
        self._button_label = button_label

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def classes(self, value=""):
        if self._ui is not None:
            self._ui.classes.append(str(value))
        return self

    def props(self, value=""):
        if self._ui is not None:
            self._ui.props.append(str(value))
            if self._button_label is not None:
                self._ui.button_props[self._button_label] = str(value)
        return self

    def tooltip(self, value=""):
        if self._ui is not None:
            self._ui.visible.append(str(value))
        return self

    def clear(self):
        return None

    def update(self):
        return None

    def add_slot(self, *_args, **_kwargs):
        return self

    def set_visibility(self, *_args, **_kwargs):
        return None

    def on(self, *_args, **_kwargs):
        return self


class _RoleUi:
    def __init__(self, selected):
        self.selected = selected
        self.callbacks = {}
        self.visible = []
        self.props = []
        self.classes = []
        self.button_props = {}

    def label(self, value):
        self.visible.append(str(value))
        return _Node(ui=self)

    def row(self):
        return _Node(ui=self)

    def column(self):
        return _Node(ui=self)

    def expansion(self, *_args, **_kwargs):
        return _Node(ui=self)

    def icon(self, *_args, **_kwargs):
        return _Node(ui=self)

    def separator(self):
        return _Node(ui=self)

    def input(self, *_args, **kwargs):
        return _Node(value=kwargs.get("value"), ui=self)

    def number(self, *_args, **kwargs):
        return _Node(value=kwargs.get("value"), ui=self)

    def checkbox(self, *_args, **kwargs):
        return _Node(value=kwargs.get("value", False), ui=self)

    def select(self, _options, **_kwargs):
        return _Node(value=self.selected, ui=self)

    def button(self, label, *, on_click=None, **_kwargs):
        self.visible.append(str(label))
        if on_click is not None:
            self.callbacks[str(label)] = on_click
        return _Node(ui=self, button_label=str(label))

