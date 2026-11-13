"""两项真实服务/界面入口事故，使用隔离源文件与冻结Provider传输。"""
import unittest
from unittest.mock import patch
from src.rename.recognition_queue_admission import capture_workspace_open
from src.rename.special_catalog import build_observation
from tests.test_default_activation_contract import DefaultActivationProducerContracts
from tests.test_special_crosswalk_gui import SpecialCrosswalkGui

class ResetEntryIncident(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_resets_original_task_with_and_without_plan(self):
        fixture = DefaultActivationProducerContracts()
        for admission_only in (True, False):
            with self.subTest(admission_only=admission_only):
                async with fixture.flow(admission_only=admission_only, requested_season=1) as flow:
                    before=flow.manager._read(flow.task_id)
                    opened=capture_workspace_open(flow.manager,flow.task_id,before)
                    result=await flow.manager.reenqueue_workspace(flow.task_id,flow.source,
                        is_anime=False,is_movie=True,custom_name='Manual Movie',custom_season_id=None,
                        preview_only=False,opened_evidence=opened)
                    after=flow.manager._read(flow.task_id)
                    self.assertEqual(flow.task_id,result)
                    self.assertEqual(before['uuid'],after['uuid'])
                    self.assertEqual(before['path'],after['path'])
                    self.assertIs(after['is_movie'],True)
                    self.assertEqual('movie',after['task_source_scope']['media_domain'])
                    self.assertEqual('Manual Movie',after['name'])
                    self.assertIs(after['is_anime'], False)
                    self.assertIsNone(after['season_id'])
                    self.assertIsNone(after['season_number'])
                    self.assertEqual('unset', after['season_source'])
                    self.assertEqual(before['task_source_scope']['source_identities'], after['task_source_scope']['source_identities'])
                    self.assertFalse(flow.store.path_for(flow.task_id).exists())
                    self.assertEqual('rename',flow.manager._queue._queue[0].operation)
                    self.assertIsNone(flow.manager._queue._queue[0].provider_selection)

class SpecialWindowIncident(unittest.IsolatedAsyncioTestCase):
    fixture=SpecialCrosswalkGui.fixture
    create_page=SpecialCrosswalkGui.create_page
    text=SpecialCrosswalkGui.text
    prepared=SpecialCrosswalkGui.prepared

    async def test_fetch_keeps_original_window_and_selected_rows(self):
        _,service,plan,extras,key=await self.prepared()
        await self.driver.click('特别篇 / OVA')
        popup=self.view._official_special_picker
        controls=self.view._official_special_controls
        with self.client:
            controls['files'].set_value(key)
        left,right=controls['bangumi'].value,controls['episode'].value
        self.assertEqual(('100:1011', '9001'), (left, right), '实际选择必须非空，不能用None自比较')
        with self.client:
            controls['mode'].set_value('descriptive')
            controls['title'].set_value('人工未保存标题')
            controls['details'].set_value(True)
        value=plan.metadata['provider_special_observation']
        # 相同Provider资料也会产生资料刷新修订，必须保持当前窗口。
        observed=build_observation(dict(value['provider_identity']),value['catalog'],
            relation=value['relation'],relation_reason=value['relation_reason'])
        button=next(e for e in popup.descendants() if e._props.get('icon')=='cloud_download')
        with patch('src.rename.special_crosswalk.observe',return_value=observed):
            await self.driver.click_element(button)
        self.assertIs(popup,self.view._official_special_picker)
        self.assertTrue(popup.value)
        self.assertIs(controls, self.view._official_special_controls)
        self.assertEqual('人工未保存标题', controls['title'].value)
        self.assertTrue(controls['details'].value)
        self.assertEqual(left,self.view._official_special_controls['bangumi'].value)
        self.assertEqual(right,self.view._official_special_controls['episode'].value)
        self.assertEqual('获取特别篇资料',button.text)
