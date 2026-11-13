"""共享登记失败后的真实注册按钮和原任务恢复。"""
import unittest
from unittest.mock import patch
from src.rename.task_batch_scope import TaskBatchScopeError
from src.rename.file_lock import FileLockBusyError
class OriginalTaskBusyRetry(unittest.IsolatedAsyncioTestCase):
    async def test_original_button_retries_same_work_preserving_inputs_and_saved_state(self):
        await self.exercise()

    async def test_movie_failure_retains_diagnostic_and_retries_original_task(self):
        await self.exercise(movie=True)

    async def test_rc69_failed_task_retries_without_recreating_or_migrating(self):
        await self.exercise(legacy=True)

    async def exercise(self, movie=False, legacy=False):
        from nicegui import Client
        from nicegui.page import page as Page
        from src.pages import plan_review_page as page
        from src.rename.review_operation import current_provider_selection_review_operation
        from tests.test_default_activation_contract import DefaultActivationProducerContracts
        from tests.test_provider_season_advice import client_for
        from tests.test_selection_season_same_window import SameWindowSeasonContracts
        helper = DefaultActivationProducerContracts('runTest')
        workers = []
        async with helper.flow(automatic=False, movie=movie, is_anime=True, requested_season=None if movie else 7,
                               configure_worker=workers.append) as flow:
            bgm = client_for(platforms={'101': '剧场版'} if movie else None)
            read = bgm._get_json.side_effect
            fail = True
            def request(endpoint, **kwargs):
                if fail:
                    raise TaskBatchScopeError('共享登记暂时忙') from FileLockBusyError('等待共享登记超时')
                return read(endpoint, **kwargs)
            bgm._get_json.side_effect = request
            workers[0].bangumi_search = bgm
            flow.manager.executor = flow.manager._execute_rename
            client = Client(Page('/bgm-retry'), request=None)
            plan, state = helper.current(flow)
            paths = [flow.store.path_for(flow.task_id), flow.service.decision_state_store.path_for(flow.task_id)]
            saved = {p: p.read_bytes() for p in paths}
            source = {p: p.read_bytes() for p in flow.source.iterdir() if p.is_file()}
            candidate = dict(namespace='bangumi', entity_type='subject', provider_id='101',
                             title='普通动画', library_domain='animation', platform='剧场版' if movie else 'TV',
                             source_url='https://bgm.tv/subject/101')
            try:
                with client:
                    dialog = page.PlanReviewDialog(plan, flow.manager._read(flow.task_id))
                    dialog.open()
                    dialog.provider_selection_expanded = True
                    dialog._render_groups()
                    dialog.provider_url_input.set_value('https://bgm.tv/subject/101')
                    dialog.rerecognition_name.set_value('尚未提交的手动名称')
                    dialog.rerecognition_season.set_value(9)
                input_values = (dialog.rerecognition_name.value, dialog.rerecognition_season.value,
                                dialog.rerecognition_media_type.value, dialog.rerecognition_path.value)
                original_id = dialog.id
                with client:
                    await dialog._select_provider_candidate(candidate)
                if legacy:
                    from src.utils.utils import mutate_task_at
                    from src.rename.review_operation import build_provider_selection_review_operation, set_review_operation
                    def old_record(data):
                        _, context = current_provider_selection_review_operation(data)
                        context = dict(context, failure_code='selection_failed', reason_codes=['selection_failed'])
                        set_review_operation(data, build_provider_selection_review_operation(data, context))
                        data['error'] = 'batch source reservation 正由另一进程修改，拒绝并发写入'
                        return data
                    mutate_task_at(flow.manager.task_root, flow.task_id, old_record)
                    with client:
                        dialog.task_data = flow.manager._read(flow.task_id)
                        dialog._render_groups()
                task = flow.manager._read(flow.task_id)
                _, failed = current_provider_selection_review_operation(task)
                self.assertEqual('failed', failed['lifecycle'])
                self.assertEqual('101', failed['candidate']['provider_id'])
                self.assertEqual(saved, {p: p.read_bytes() for p in paths})
                self.assertEqual('https://bgm.tv/subject/101', dialog.provider_url_input.value)
                self.assertEqual('selection_failed' if legacy else 'internal_busy', failed['failure_code'])
                self.assertEqual(input_values, (dialog.rerecognition_name.value, dialog.rerecognition_season.value,
                                               dialog.rerecognition_media_type.value, dialog.rerecognition_path.value))
                fail = False
                button = next(e for e in dialog.descendants() if e._props.get('label') == '重新读取资料')
                self.assertFalse(button._props.get('disable'), '原任务的现有重试按钮必须可用')
                await SameWindowSeasonContracts.click(self, dialog, '重新读取资料')
                _, recovered = current_provider_selection_review_operation(flow.manager._read(flow.task_id))
                self.assertEqual('succeeded', recovered['lifecycle'])
                self.assertNotEqual(failed['request_id'], recovered['request_id'])
                self.assertEqual(original_id, dialog.id)
                result = flow.service.decision_state_store.load(flow.task_id)
                self.assertEqual('101', result.confirmed_work['primary_provider']['provider_id'])
                if not movie:
                    self.assertEqual(7, result.season_binding['season_number'])
                self.assertEqual(input_values, (dialog.rerecognition_name.value, dialog.rerecognition_season.value,
                                               dialog.rerecognition_media_type.value, dialog.rerecognition_path.value))
                self.assertEqual(source, {p: p.read_bytes() for p in source})
                self.assertFalse(list((flow.root/'records').glob('*.json')))
                self.assertFalse(any(e.target_path.exists() for e in dialog.plan.entries if e.target_path != e.source_path))
            finally:
                client.delete()
