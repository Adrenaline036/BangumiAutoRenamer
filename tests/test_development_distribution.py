"""生产默认行为与显式开发适配的方向边界。"""
import inspect
import os
import unittest
from unittest.mock import patch

from src.pages import data_table_page as history
from src.task_queue import TaskQueueManager
from development.preview import preview_session


class DevelopmentDistribution(unittest.TestCase):
    def test_queue_constructor_has_no_development_mode(self):
        self.assertNotIn('test_mode', inspect.signature(TaskQueueManager).parameters)

    def test_old_environment_flags_cannot_activate_development_history(self):
        rows=[{'uuid':'ordinary-task','workflow_status':'need_confirm'}]
        for value in ('false', 'true'):
            with self.subTest(value=value), patch.dict(os.environ,
                    BAR_LOCAL_GUI_PREVIEW=value, BAR_UI_TEST_MODE=value):
                with patch('development.gui_simulation.history_rows', side_effect=AssertionError('production imported development')):
                    self.assertIs(rows, history._display_task_rows(rows))

    def test_explicit_development_session_projects_history_and_restores_product(self):
        rows=[{'uuid':'ordinary-task'}]
        original=history._display_task_rows
        with preview_session(), patch('development.gui_simulation.history_rows', return_value=['preview']) as project:
            self.assertEqual(['preview'], history._display_task_rows(rows))
            project.assert_called_once_with(rows)
        self.assertIs(original, history._display_task_rows)
        self.assertIs(rows, history._display_task_rows(rows))
