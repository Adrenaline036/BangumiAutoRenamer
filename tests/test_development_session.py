"""显式开发会话在生产调用前阻断副作用，并完整还原补丁。"""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from development.preview import GuiPreviewBlocked, preview_session
from src.task_queue import TaskQueueManager
from src.config.config_manager import cm
from src.pages.config_page import ConfigPage
from src.rename import replacement_completion


class DevelopmentSession(unittest.IsolatedAsyncioTestCase):
    async def test_queue_and_recovery_commands_stop_before_reading_authority(self):
        original=TaskQueueManager.enqueue_batch
        with preview_session():
            for name,args in (
                ('enqueue_batch',()), ('_enqueue_execution',('id','fingerprint')),
                ('enqueue_reidentify',('id',{},0)), ('retry_current',('id',)),
                ('replan_failed_execution',('id','fingerprint')),
                ('restart_rolled_back',('id',None))):
                with self.subTest(command=name), self.assertRaises(GuiPreviewBlocked):
                    await getattr(TaskQueueManager,name)(None,*args)
            await TaskQueueManager.start(None)
            await TaskQueueManager.stop(None)
        self.assertIs(original,TaskQueueManager.enqueue_batch)

    def test_config_and_replacement_completion_are_blocked(self):
        original=cm.set_configs
        with preview_session():
            with self.assertRaises(GuiPreviewBlocked):
                cm.set_configs({'mode':'copy'})
            with self.assertRaises(GuiPreviewBlocked):
                replacement_completion.finish_replacement(None,'id','fingerprint')
            closed=[]
            with patch('nicegui.ui.notify') as notify:
                ConfigPage._handle_ok(SimpleNamespace(close=lambda:closed.append(True)))
            self.assertEqual([True],closed)
            self.assertIn('没有保存设置',notify.call_args.args[0])
        self.assertEqual(original,cm.set_configs)

    def test_failed_session_body_restores_production(self):
        original=TaskQueueManager.start
        with self.assertRaisesRegex(RuntimeError,'body failed'):
            with preview_session():
                raise RuntimeError('body failed')
        self.assertIs(original,TaskQueueManager.start)
