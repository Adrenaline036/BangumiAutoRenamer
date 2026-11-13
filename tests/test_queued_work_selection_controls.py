"""作品选择完成后的普通页面、人工输入及事务断开控制。"""
import asyncio
from contextlib import contextmanager
from unittest.mock import patch
import unittest

from tests import test_queued_work_selection as queued
from src.pages import plan_review_page as page


class QueuedSelectionControls(unittest.IsolatedAsyncioTestCase):
    original = queued.QueuedWorkSelection.original
    create_page = queued.QueuedWorkSelection.create_page
    visible = queued.QueuedWorkSelection.visible
    text = queued.QueuedWorkSelection.text
    setup_work = queued.QueuedWorkSelection.setup_work
    candidate = queued.QueuedWorkSelection.candidate
    select = queued.QueuedWorkSelection.select
    operation = queued.QueuedWorkSelection.operation
    changed = queued.QueuedWorkSelection.changed

    def reopen(self, service, task_id):
        with self.client:
            self.view = page.PlanReviewDialog(service.plan_store.load(task_id), page.task_queue._read(task_id))
            self.view.open()

    async def test_tv_roundtrip_preserves_manual_zero_and_content(self):
        root, service, old = await self.setup_work()
        state = service.decision_state_store.load(old.task_id)
        before = state.content_selection.to_dict()
        source = {str(e.source_path): e.source_path.read_bytes() for e in old.entries}
        with queued.provider():
            first = await self.select(self.candidate(), 0)
            self.assertFalse(first.error, first.error)
            self.reopen(service, old.task_id)
            target = dict(queued.cross.TMDB, title='新的动画', snapshot_at='2026-11-07T01:00:00Z')
            second = await self.select(target)
        self.assertFalse(second.error, second.error)
        state = service.decision_state_store.load(old.task_id)
        self.assertEqual('tmdb', state.confirmed_work['primary_provider']['namespace'])
        self.assertEqual(0, state.season_binding['season_number'])
        self.assertEqual('explicit_user', state.season_binding['decision_source'])
        for key in ('excluded_sources', 'associations', 'version_groups'):
            self.assertEqual(before[key], state.content_selection.to_dict()[key])
        self.assertEqual(source, {str(e.source_path): e.source_path.read_bytes() for e in old.entries})

    async def test_success_reopen_has_no_required_provider_read_or_extra_confirmation(self):
        root, service, old, changed = await self.changed()
        with patch('src.rename.get_info.Search', side_effect=AssertionError('打开页面不能读取Provider')), \
             patch('src.pages.season_reference_view.load', side_effect=AssertionError('打开页面不能预读参考')):
            self.reopen(service, old.task_id)
            await asyncio.sleep(.03)
        text = self.text(self.view)
        self.assertIn('已确认作品：新的动画', text)
        for label in ('预览更正后的计划', '确认作品更正', '原作品：'):
            self.assertNotIn(label, text)
        self.assertIn('批准整理计划', text)

    async def test_error_page_exposes_normal_link_input_and_blocks_old_controls(self):
        root, service, old = await self.setup_work()
        with queued.provider(fail=True):
            result = await self.select(self.candidate(), 1)
        self.assertTrue(result.error)
        self.reopen(service, old.task_id)
        text = self.text(self.view)
        self.assertIsNotNone(self.view.provider_url_input)
        self.assertIn('新的动画', text)
        for label in ('已确认作品：', '批准整理计划', '执行整理', '保存并重新识别'):
            self.assertNotIn(label, text)
        self.assertIn('安全结束任务', text)
        self.assertIn('关闭', text)
        from src.rename.plan_review import entry_id
        from src.rename.plan_review import PlanReviewError
        with self.assertRaises(PlanReviewError):
            service.approve_entries(old.task_id, page.PlanStore.fingerprint(old), [entry_id(e) for e in old.entries])
        from src.rename.plan_execution import PlanExecutionError
        with self.assertRaises((ValueError, PlanExecutionError)):
            page.task_queue.execution_service.validate_approved_authority(old.task_id, page.PlanStore.fingerprint(old))

    async def test_disconnected_request_validator_never_replaces_plan(self):
        root, service, old = await self.setup_work()
        before = service.plan_store.path_for(old.task_id).read_bytes()
        with queued.provider(), patch('src.rename.queued_work_selection.validate_request', return_value=None):
            result = await self.select(self.candidate(), 2)
        self.assertTrue(result.error)
        self.assertEqual(before, service.plan_store.path_for(old.task_id).read_bytes())

    async def pending(self, movie):
        root, service, old = await self.setup_work(movie=movie)
        previous = service.decision_state_store.load(old.task_id).content_selection.to_dict()
        # 固定登记观察建议，实际pending类型、投影、事务和队列终态均使用生产实现。
        row = dict(key='work-peer', title='新的动画', root=str(root/'isolated-peer'), revision=1, fingerprint='a'*64)
        with queued.provider(movie=movie), patch('src.rename.work_affiliation.recommendations', return_value=(row,)):
            result = await self.select(self.candidate(movie), None if movie else 2)
        self.assertFalse(result.error, result.error)
        changed = service.plan_store.load(old.task_id)
        from src.rename.work_affiliation import pending_from_plan
        pending = pending_from_plan(changed)
        self.assertIsNotNone(pending)
        self.assertEqual('bangumi', changed.work_identity.primary_provider.namespace)
        self.assertEqual('succeeded', self.operation(service, old.task_id)['lifecycle'])
        self.assertEqual('need_confirm', page.task_queue._read(old.task_id)['workflow_status'])
        current = service.decision_state_store.load(old.task_id).content_selection.to_dict()
        for key in ('excluded_sources', 'associations'):
            self.assertEqual(previous[key], current[key])
        if movie:
            self.assertEqual([g['sources'] for g in previous['version_groups']], [g['sources'] for g in pending.to_dict()['completion']['groups']])
        else:
            self.assertEqual(previous['version_groups'], current['version_groups'])
        self.assertTrue(all(not e.target_path.exists() for e in changed.entries))
        self.reopen(service, old.task_id)
        self.assertIn('归入已有作品', self.text(self.view))
        self.assertNotIn('预览更正后的计划', self.text(self.view))
        self.assertNotIn('批准整理计划', self.text(self.view))
        from src.rename.work_affiliation import preview_choice, commit_choice
        intent, preview = preview_choice(service, old.task_id, page.PlanStore.fingerprint(changed), key='', separate=True)
        completed = commit_choice(service, intent)
        self.assertIsNone(pending_from_plan(completed))
        self.assertEqual('bangumi', completed.work_identity.primary_provider.namespace)
        if movie:
            restored = service.decision_state_store.load(old.task_id).content_selection.to_dict()['version_groups']
            self.assertEqual([g['sources'] for g in previous['version_groups']], [g['sources'] for g in restored])
        with self.client:
            await self.view._adopt_revision(completed)
        self.assertIn('批准整理计划', self.text(self.view))

    async def test_tv_returns_to_normal_affiliation_choice(self):
        await self.pending(False)

    async def test_movie_returns_to_normal_affiliation_choice(self):
        await self.pending(True)

    async def resolution_page(self):
        helper = queued.cross.CrossProviderWorkCorrection('runTest')
        self.addCleanup(helper.doCleanups)
        select = page.PlanReviewDialog._select_provider_candidate
        async def wait_selected(view, candidate, **kwargs):
            await select(view, candidate, **kwargs)
            manager = page.task_queue
            await asyncio.wait_for(manager._provider_reads[view.plan.task_id].worker, 20)
            await asyncio.wait_for(manager._queue.join(), 10)
        from dataclasses import replace
        from src.rename import initial_tv_completion as completion
        prepare = completion.prepare
        def historical_selection(service, recognition, **kwargs):
            # 通过原类型化事务创建历史系列计划，消费固定的Provider关系观察。
            if kwargs.get('mode', 'initial') == 'initial':
                from src.rename.provider_link_resolver import collect_subject_observations
                from src.rename.provider_link_decision import resolve_provider_subject_cohort_segment
                subjects = [dict(namespace='bangumi', entity_type='subject', provider_id=identity,
                    title='向山进发' + suffix, original_title='Yama no Susume' + suffix,
                    air_date=date, episode_count=count, platform='TV')
                    for identity, suffix, date, count in [('41658', '', '2013-01-03', 13), ('86670', ' Second Season', '2014-07-09', 24)]]
                relations = [dict(**{'from': '41658', 'to': '86670'}, relation='sequel')]
                batch = collect_subject_observations(selected_subject_id='41658', bangumi_subjects=subjects,
                    bangumi_relations=relations, max_subjects=2, search_subject=lambda subject: dict(
                        bangumi_subject_id=subject['provider_id'], observed_at='2026-11-07T00:00:00Z',
                        policy_version='subject-candidate-v1', queries=[dict(locale='original',
                            query=subject['original_title'], year=int(subject['air_date'][:4]),
                            outcome='no_match', truncated=False, candidates=[])]))
                kwargs['resolution'] = resolve_provider_subject_cohort_segment(selected_subject_id='41658',
                    bangumi_subjects=subjects, bangumi_relations=relations, subject_observations=batch).resolution
                recognition = replace(recognition, season_number=1, season_source='explicit_user')
            return prepare(service, recognition, **kwargs)
        with patch.object(page.PlanReviewDialog, '_select_provider_candidate', wait_selected), \
             patch.object(completion, 'prepare', historical_selection):
            root, service, _, result, extras, *_ = await helper.fixture(anime=True, companions=True, font_archive=True, selected_season=1)
        self.assertTrue(result.state.confirmed_work, result.task.get('error'))
        self.assertIsNotNone(result.plan.metadata['tv_completion_resolution'])
        self.create_page(prepared=(root, service, result.plan, []))
        return root, service, result.plan, extras

    async def test_resolution_backed_selection_preserves_manual_content(self):
        root, service, plan, extras = await self.resolution_page()
        from tests.test_content_choice_contract import change
        videos = [e.source_path for e in plan.entries if e.media_kind == 'video']
        plan = change(service, plan, [videos[-1]])
        self.plan = plan
        self.reopen(service, plan.task_id)
        before = service.decision_state_store.load(plan.task_id)
        with queued.provider():
            result = await self.select(dict(queued.cross.TMDB, title='新的动画'), 2)
        self.assertFalse(result.error, result.error)
        after = service.decision_state_store.load(plan.task_id)
        for key in ('excluded_sources', 'associations'):
            self.assertEqual(before.content_selection.to_dict()[key], after.content_selection.to_dict()[key])
        self.assertEqual(before.content_selection.associations, after.content_selection.associations)
        self.assertEqual(2, after.season_binding['season_number'])

    async def test_resolution_season_button_uses_normal_choice_then_queue(self):
        root, service, plan, extras = await self.resolution_page()
        with self.client:
            await self.view._change_general_tv_season()
            selected = self.view.general_season_choice
            selected.manual.set_value(True)
            selected.custom.set_value(2)
            button = next(e for e in self.client.elements.values() if getattr(e, 'text', '') == '确认季度并生成新计划')
            with queued.provider():
                await self.driver.click_element(button)
                work = page.task_queue._provider_reads[plan.task_id]
                await asyncio.wait_for(work.worker, 15)
                await asyncio.wait_for(page.task_queue._queue.join(), 5)
        self.assertFalse(work.error, work.error)
        self.assertEqual(2, service.decision_state_store.load(plan.task_id).season_binding['season_number'])

    async def test_resolution_independent_uses_normal_affiliation_dialog(self):
        root, service, plan, extras = await self.resolution_page()
        from src.pages.work_affiliation_view import independent
        with queued.provider(), self.client:
            await independent(self.view, ui=page.ui, notify=lambda *_a, **_k: None)
        texts = ' '.join(str(getattr(e, 'text', '')) for e in self.client.elements.values())
        self.assertIn('确认本地作品归属', texts)
        self.assertNotIn('预览更正后的计划', texts)
        self.assertEqual(page.PlanStore.fingerprint(plan), page.PlanStore.fingerprint(service.plan_store.load(plan.task_id)))

    async def test_link_read_error_reopens_input_without_old_plan_controls(self):
        root, service, old = await self.setup_work()
        raw = service.plan_store.path_for(old.task_id).read_bytes()
        with self.client:
            self.view._show_provider_candidates()
            self.view.provider_url_input.value = 'https://bgm.tv/subject/53918'
            with patch.object(page, '_read_manual_provider_url', side_effect=ValueError('隔离链接读取失败')):
                await self.view._select_manual_provider_url()
                work = page.task_queue._provider_reads[old.task_id]
                await asyncio.wait_for(work.worker, 10)
        self.assertIn('隔离链接读取失败', work.error)
        self.reopen(service, old.task_id)
        text = self.text(self.view)
        self.assertNotIn('已确认作品：', text)
        self.assertNotIn('批准整理计划', text)
        self.assertEqual('https://bgm.tv/subject/53918', self.view.provider_url_input.value)
        self.assertEqual(raw, service.plan_store.path_for(old.task_id).read_bytes())
