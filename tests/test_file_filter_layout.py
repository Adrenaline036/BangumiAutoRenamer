"""文件筛选使用原值事件，显示紧凑单行标签。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from nicegui import ui, Client
from nicegui.page import page
from src.pages.plan_file_list import PlanFileListMixin

class FileFilterLayout(unittest.IsolatedAsyncioTestCase):
    async def test_labels_icons_and_filter_values(self):
        client=Client(page('/isolated-filter'),request=None)
        self.addCleanup(client.delete)
        owner=SimpleNamespace(plan=object(),_set_query=Mock(),_set_view_mode=Mock())
        with client:PlanFileListMixin._render_toolbar(owner)
        toggle=next(e for e in client.elements.values() if isinstance(e,ui.toggle))
        self.assertEqual({'all':'全部文件','issues':'问题文件'},toggle.options)
        self.assertEqual(['o_folder','o_error_outline'],[o['icon'] for o in toggle._props['options']])
        toggle.set_value('issues');toggle.set_value('all')
        self.assertEqual(['issues','all'],[c.args[0] for c in owner._set_view_mode.call_args_list])
        self.assertEqual('all',toggle.value)
