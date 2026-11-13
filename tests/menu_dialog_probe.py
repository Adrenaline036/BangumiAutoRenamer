"""菜单关闭后，独立弹窗仍可操作，且打开入口不改业务记录。"""
import asyncio
import os
import sys
from pathlib import Path
import unittest
from unittest.mock import patch


class MenuDialogLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from nicegui import core
        from nicegui.client import Client
        from nicegui.page import page
        from src.main_page import main_page

        self.client = Client(page('/isolated-menu-lifetime'), request=None)
        self.addCleanup(self.client.delete)
        self.loop_patch = patch.object(core, 'loop', asyncio.get_running_loop())
        self.loop_patch.start()
        self.addCleanup(self.loop_patch.stop)
        with self.client:
            main_page()

    def records(self):
        from src.utils.path import DATA_PATH
        return {p.relative_to(DATA_PATH).as_posix(): p.read_bytes()
                for p in Path(DATA_PATH).rglob('*.json') if p.is_file()}

    async def check_menu(self, text, expected):
        from nicegui import background_tasks, core
        sections = [e for e in self.client.elements.values() if getattr(e, 'text', None) == text]
        self.assertEqual(1, len(sections))
        item = sections[0].parent_slot.parent
        menu = item.menu
        before = self.records()
        previous = None
        for _ in range(2):
            tasks, errors = [], []
            create = background_tasks.create

            def tracked_create(coroutine, **kwargs):
                task = create(coroutine, **kwargs)
                tasks.append(task)
                return task

            with self.client, patch.object(background_tasks, 'create', side_effect=tracked_create), \
                    patch.object(core.app, 'handle_exception', side_effect=errors.append):
                menu.open()
                # 同时触发真实业务监听器和框架注册的菜单自动关闭监听器。
                for listener in list(item._event_listeners.values()):
                    if listener.type == 'click':
                        item._handle_event({'listener_id': listener.id, 'args': {}})
                if tasks:
                    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
            self.assertEqual([], errors)
            self.assertFalse(menu.value)
            dialogs = [e for e in self.client.elements.values()
                       if type(e).__name__ == 'Dialog' and e.value]
            self.assertEqual(1, len(dialogs))
            dialog = dialogs[0]
            self.assertIsNot(previous, dialog)
            self.assertIs(dialog.parent_slot.parent, self.client.content,
                          '弹窗必须独立于自动关闭的菜单，第一次点击后即可使用')
            self.assertTrue(any(getattr(e, 'text', None) == expected
                                for e in self.client.elements.values()))
            self.assertEqual(before, self.records())
            dialog.close()
            self.assertFalse(dialog.value)
            previous = dialog

    async def test_recovery_menu_dialog_survives_menu_close(self):
        await self.check_menu('恢复记录', '未发现待处理的孤立恢复记录。')

    async def test_library_menu_dialog_survives_menu_close(self):
        await self.check_menu('媒体库', '没有找到已整理资产')



if __name__ == '__main__':
    root = Path(sys.argv[2]).resolve()
    os.environ['BAR_DATA_DIR'] = str(root)
    os.environ['BAR_UI_TEST_MODE'] = 'false'
    os.environ['BAR_LOCAL_GUI_PREVIEW'] = 'false'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    unittest.main(argv=[sys.argv[0], 'MenuDialogLifetimeTests.' + sys.argv[1]], verbosity=2)
