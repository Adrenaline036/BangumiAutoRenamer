"""标题缺资料的实际队列、原任务继续与隔离控制。"""
import asyncio
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import patch, Mock, AsyncMock
import unittest

from tests import test_default_activation_contract as activation
from src.rename.canonical_naming import TitleAuthorityConflict
from src.rename.plan_store import PlanStore
from tests.source_collection_fixture import admit_task


class TitleConfirmation(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow
    current = activation.DefaultActivationProducerContracts.current

    async def admit(self, manager, source, task_id):
        await admit_task(manager, source, task_id=task_id, is_anime=None, is_movie=None,
                              custom_name='Ordinary Movie')

    def configure(self, worker, *, mixed=False, valid=False):
        worker.bangumi_search.search_subjects = lambda *args, **kwargs: []
        movie = dict(id=8181, title='普通电影' if valid else 'Ordinary Movie',
            original_title='Ordinary Movie', release_date='2024-01-01', genres=[dict(id=16, name='Animation')])
        tv = dict(id=4242, name='普通剧集', original_name='Example Television',
            first_air_date='2020-01-01', genres=[dict(id=16, name='Animation')],
            seasons=[dict(season_number=1, name='Season 1', episode_count=2)])
        worker.search.get_movie_info = lambda *args: (movie['title'], dict(movie))
        worker.search.get_tv_info = lambda *args: (tv['name'], dict(tv)) if mixed else ('', None)
        worker.search.get_movie_info_by_id = lambda value: (movie['title'], dict(movie))
        worker.search.get_tv_info_by_id = lambda value: (tv['name'], dict(tv))

    @contextmanager
    def legacy_failure(self):
        # 复现旧版的普通标题异常，保留真实队列及持久化事务生成的库存。
        from src.rename.automatic_recognition import resolve_batch_candidate
        def old_qualification(batch, *, is_anime, is_movie):
            return resolve_batch_candidate(batch, is_anime=is_anime,
                                           is_movie=True if is_movie is None else is_movie)
        with patch('src.rename.process._resolve_plan_naming_decision',
                   side_effect=TitleAuthorityConflict('TMDB 条目缺少可确认的中文标题')), \
                patch('src.rename.automatic_recognition.resolve_batch_candidate', old_qualification):
            yield

    def candidate(self):
        return dict(namespace='tmdb', entity_type='tv', provider_id='4242', title='普通剧集',
            original_title='Ordinary Show', year=2020, library_domain='animation',
            source_url='https://www.themoviedb.org/tv/4242', snapshot_at='2026-10-20T00:00:00Z')

    async def test_untranslated_title_keeps_inventory_and_confirms_work(self):
        async with self.flow(admit=self.admit, configure_worker=self.configure) as flow:
            task = flow.manager._read(flow.task_id)
            self.assertEqual('need_confirm', task['workflow_status'], task.get('error'))
            plan, state = self.current(flow)
            self.assertEqual('8181', state.confirmed_work['primary_provider']['provider_id'])
            self.assertIsNotNone(state.content_selection)
            self.assertIsNone(task.get('completed_at'))
            self.assertEqual('Ordinary Movie', plan.work_identity.title)
            self.assertEqual('Ordinary Movie', state.confirmed_work['title'])
            from src.rename.content_selection import validate_plan_content
            validate_plan_content(plan, state)
            self.assertEqual({p for p in flow.source.iterdir() if p.is_file()}, set(plan.expected_sources))
            self.assertEqual(set(flow.source.glob('*S01E*.mkv')),
                             {e.source_path for e in plan.entries if e.media_kind == 'video'})
            self.assertTrue(all(not row.target_path.exists() for row in plan.entries))

    async def test_unknown_mixed_movie_tv_never_commits_movie_identity(self):
        async with self.flow(admit=self.admit,
                configure_worker=lambda w: self.configure(w, mixed=True, valid=True)) as flow:
            task = flow.manager._read(flow.task_id)
            self.assertEqual('need_confirm', task['workflow_status'], task.get('error'))
            self.assertTrue(task.get('provider_candidates'), '混合候选必须可供人工选择')
            self.assertIsNone(task.get('is_movie'))
            self.assertIsNone(task.get('is_anime'))
            self.assertFalse(task.get('provider_identity'))
            self.assertFalse(task.get('provider_selection'))
            if flow.service.decision_state_store.path_for(flow.task_id).exists():
                self.assertFalse(flow.service.decision_state_store.load(flow.task_id).confirmed_work)

    async def test_old_failed_inventory_can_choose_tv_without_rebuild(self):
        with self.legacy_failure():
            async with self.flow(admit=self.admit,
                    configure_worker=lambda w: self.configure(w, mixed=True)) as flow:
                self.assertEqual('failed', flow.manager._read(flow.task_id)['workflow_status'])
                plan, state = self.current(flow)
                before = [p.read_bytes() for p in (flow.store.path_for(flow.task_id),
                    flow.service.decision_state_store.path_for(flow.task_id))]
                from src.pages import data_table_page as table
                with patch.multiple(table, get_task=flow.manager._read, task_queue=flow.manager,
                        plan_review_page=AsyncMock(), refresh_dashboard=Mock()):
                    await table._open_review(flow.task_id)
                self.assertEqual(before, [p.read_bytes() for p in (flow.store.path_for(flow.task_id),
                    flow.service.decision_state_store.path_for(flow.task_id))])
                self.assertEqual('need_confirm', flow.manager._read(flow.task_id)['workflow_status'])
                scope = flow.manager._read(flow.task_id)['task_source_scope']
                candidate = next(row for row in flow.manager._read(flow.task_id)['provider_candidates']['work']
                                 if row.get('entity_type') == 'tv')
                self.assertNotIn('library_domain', candidate)
                from tests.test_content_choice_contract import change
                plan = change(flow.service, plan, [flow.source / 'Ordinary Show S01E02 [1080p].mkv'])
                state = flow.service.decision_state_store.load(flow.task_id)
                from src.pages import plan_review_page as page
                view = SimpleNamespace(plan=plan, task_decision=state, task_data=flow.manager._read(flow.task_id),
                    client=nullcontext(), close=Mock(), decision_state_store=flow.service.decision_state_store,
                    _render_groups=Mock(), _render_completion_workflow=Mock(), _render_issues=Mock(),
                    _render_entries=Mock(), _adopt_revision=Mock())
                # 解除旧版异常注入后由正式队列处理人工选择。
                with patch('src.rename.process._resolve_plan_naming_decision', self.naming):
                    await page.PlanReviewDialog._select_provider_candidate(view, candidate, season_number=1)
                    await asyncio.wait_for(flow.manager._queue.join(), 12)
                current, decided = self.current(flow)
                self.assertEqual('tv', decided.confirmed_work.get('primary_provider', {}).get('entity_type'),
                                 flow.manager._read(flow.task_id).get('error'))
                self.assertEqual(scope, flow.manager._read(flow.task_id)['task_source_scope'])
                self.assertEqual(state.initial_source_basis, decided.initial_source_basis)
                self.assertEqual(state.content_selection.to_dict()['excluded_sources'],
                                 decided.content_selection.to_dict()['excluded_sources'])
                self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(current))
                view.close.assert_not_called()
                view._adopt_revision.assert_called_once_with(current)
                view._render_completion_workflow.assert_called_once()
                self.assertEqual(decided.revision, view.task_decision.revision)

    async def asyncSetUp(self):
        from src.rename.process import _resolve_plan_naming_decision
        self.naming = _resolve_plan_naming_decision

    async def test_normal_single_movie_control(self):
        async with self.flow(admit=self.admit,
                configure_worker=lambda w: self.configure(w, valid=True)) as flow:
            self.assertEqual('movie', self.current(flow)[1].confirmed_work.get('primary_provider', {}).get('entity_type'),
                             flow.manager._read(flow.task_id).get('error'))

    async def test_explicit_movie_cannot_be_changed_to_tv_by_auto_context(self):
        from src.rename.plan_execution import PlanExecutionError
        from tests.test_content_choice_contract import records
        async with self.flow(movie=True, is_anime=True, configure_worker=self.configure) as flow:
            plan, state = self.current(flow)
            before = records(flow.root)
            with self.assertRaises(PlanExecutionError):
                await flow.manager.enqueue_reidentify(flow.task_id, self.candidate(), state.revision)
            self.assertEqual(before, records(flow.root))

    async def test_recovery_rejects_execution_drift_and_corruption(self):
        from src.rename.recognition_queue_admission import restore_title_confirmation
        for fault in ('execution', 'fingerprint', 'stale', 'different_error'):
            with self.subTest(fault=fault), self.legacy_failure():
                async with self.flow(admit=self.admit, configure_worker=self.configure) as flow:
                    task = flow.manager._read(flow.task_id)
                    expected = dict(task)
                    if fault == 'execution': task['execution_status'] = 'completed'
                    elif fault == 'fingerprint': task['plan_fingerprint'] = '0' * 64
                    elif fault == 'stale': task['name'] = 'later manual edit'
                    else: task['error'] = '[任务队列] 标题证据与最终作品身份不一致'
                    flow.manager._write(flow.task_id, task)
                    from tests.test_content_choice_contract import records
                    before = records(flow.root)
                    if fault == 'different_error':
                        self.assertFalse(restore_title_confirmation(flow.manager, flow.task_id, expected_task=task))
                    else:
                        with self.assertRaises((ValueError, RuntimeError)):
                            restore_title_confirmation(flow.manager, flow.task_id,
                                expected_task=expected if fault == 'stale' else task)
                    self.assertEqual(before, records(flow.root))

    def test_title_evidence_pending_does_not_absorb_identity_conflict(self):
        from src.rename.canonical_naming import resolve_title_authority, TitleEvidencePending
        for title, original in (('', ''), ('  ', '  ')):
            with self.subTest(title=title, original=original), self.assertRaises(TitleEvidencePending):
                resolve_title_authority(selection=dict(namespace='tmdb', entity_type='movie', provider_id='8181',
                    title=title, original_title=original))
        try:
            resolve_title_authority(selection=dict(namespace='tmdb', entity_type='movie', provider_id='8181',
                title='中文', original_title='Original'),
                primary_provider_identity=dict(namespace='tmdb', entity_type='movie', provider_id='8182'))
        except TitleAuthorityConflict as error:
            self.assertNotIsInstance(error, TitleEvidencePending)
        else:
            self.fail('身份冲突不得成为可用标题')
