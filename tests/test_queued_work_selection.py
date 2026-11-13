"""完整作品更换由现有队列保存，窗口只显示持久结果。"""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from src.pages import plan_review_page as page
from src.rename.review_operation import current_provider_selection_review_operation
from tests import test_work_correction_lifecycle as lifecycle
from tests import test_cross_provider_work_correction as cross


@contextmanager
def provider(*, movie=False, fail=False, entered=None, release=None):
    def bgm(_client, endpoint):
        if entered is not None:
            entered.set()
        if release is not None and not release.wait(5):
            raise AssertionError('隔离Provider未释放')
        if fail:
            raise ValueError('隔离作品详情读取失败')
        if endpoint.startswith('/v0/subjects/'):
            return dict(id=int(endpoint.rsplit('/', 1)[-1]), type=2, name='New Show',
                name_cn='新的动画电影' if movie else '新的动画', platform='剧场版' if movie else 'TV', date='2013-01-01')
        if endpoint.startswith('/v0/episodes?'):
            return dict(total=0, limit=100, offset=0, data=[])
        raise AssertionError(endpoint)
    search = SimpleNamespace(get_tv_info_by_id=lambda identity: ('新的动画', dict(id=identity,
        name='新的动画', original_name='New Show', first_air_date='2013-01-01',
        genres=[{'id': 16, 'name': 'Animation'}], seasons=[{'season_number': 1}, {'season_number': 2}])),
        get_season_info=lambda *_: {'episodes': []}, get_tv_subject_observation_info=lambda *_: ('', None))
    with patch('src.rename.get_info.Search', return_value=search), \
         patch('src.rename.provider_search.BangumiProviderClient._get_json', bgm):
        yield


class QueuedWorkSelection(unittest.IsolatedAsyncioTestCase):
    original = lifecycle.WorkCorrectionLifecycle.original
    create_page = lifecycle.WorkCorrectionLifecycle.create_page
    visible = lifecycle.WorkCorrectionLifecycle.visible
    text = lifecycle.WorkCorrectionLifecycle.text

    async def setup_work(self, *, movie=False):
        root, service, plan = await self.original(movie=movie)
        self.create_page(prepared=(root, service, plan, []))
        return root, service, plan

    def candidate(self, movie=False):
        return dict(cross.BANGUMI, platform='剧场版' if movie else 'TV',
            snapshot_at='2026-11-07T00:00:00Z', poster_url='https://example.invalid/poster.jpg')

    async def select(self, target, season=None):
        with self.client:
            self.view._show_provider_candidates()
            await self.view._select_provider_candidate(target, season_number=season)
        work = page.task_queue._provider_reads[self.plan.task_id]
        await asyncio.wait_for(work.worker, 15)
        await asyncio.wait_for(page.task_queue._queue.join(), 5)
        return work

    def operation(self, service, task_id):
        return current_provider_selection_review_operation(page.task_queue._read(task_id))[1]

    async def changed(self, *, movie=False):
        root, service, old = await self.setup_work(movie=movie)
        target = self.candidate(movie)
        with provider(movie=movie):
            work = await self.select(target, None if movie else 1)
        self.assertFalse(work.error, work.error)
        changed = service.plan_store.load(old.task_id)
        self.assertEqual('bangumi', changed.work_identity.primary_provider.namespace)
        self.assertEqual('succeeded', self.operation(service, old.task_id)['lifecycle'])
        self.assertEqual(target['snapshot_at'], changed.work_identity.primary_provider.snapshot_at)
        self.assertEqual(target['poster_url'], changed.work_identity.primary_provider.poster_url)
        self.assertEqual(old.expected_sources, changed.expected_sources)
        self.assertFalse(service.load(old.task_id).approved_entry_ids)
        self.assertTrue(all(not e.target_path.exists() for e in changed.entries))
        return root, service, old, changed

    async def test_complete_tv_returns_committed_new_work(self):
        await self.changed()

    async def test_complete_movie_returns_committed_new_work(self):
        await self.changed(movie=True)

    async def test_complete_tv_admission_accepts_new_work_and_explicit_zero(self):
        root, service, old = await self.setup_work()
        manager = page.task_queue
        with provider():
            await manager.enqueue_reidentify(old.task_id, self.candidate(),
                service.decision_state_store.load(old.task_id).revision, season_number=0)
            await asyncio.wait_for(manager._queue.join(), 15)
        operation = self.operation(service, old.task_id)
        self.assertEqual(0, operation['requested_season_number'])
        self.assertEqual('53918', operation['candidate']['provider_id'])
        self.assertEqual('succeeded', operation['lifecycle'])
        changed = service.plan_store.load(old.task_id)
        state = service.decision_state_store.load(old.task_id)
        self.assertEqual('bangumi', changed.work_identity.primary_provider.namespace)
        self.assertEqual(0, state.season_binding['season_number'])
        self.assertEqual('explicit_user', state.season_binding['decision_source'])

    async def test_missing_season_returns_normal_selected_work_then_resubmits(self):
        root, service, old = await self.setup_work()
        with provider():
            work = await self.select(self.candidate())
        self.assertFalse(work.error, work.error)
        self.assertEqual('awaiting_season', self.operation(service, old.task_id)['lifecycle'])
        self.assertEqual(page.PlanStore.fingerprint(old), page.PlanStore.fingerprint(service.plan_store.load(old.task_id)))
        with self.client:
            self.view = page.PlanReviewDialog(service.plan_store.load(old.task_id), page.task_queue._read(old.task_id))
            self.view.open()
        self.assertIn('新的动画', self.text(self.view))
        self.assertNotIn('已确认作品：', self.text(self.view))
        self.assertIsNotNone(self.view.unresolved_season_input)
        with provider():
            work = await self.select(self.candidate(), 0)
        self.assertFalse(work.error, work.error)
        self.assertEqual('succeeded', self.operation(service, old.task_id)['lifecycle'])
        self.assertEqual(0, service.decision_state_store.load(old.task_id).season_binding['season_number'])

    async def test_provider_error_finishes_in_review_without_changing_plan(self):
        root, service, old = await self.setup_work()
        with provider(fail=True):
            work = await self.select(self.candidate(), 1)
        self.assertIn('隔离作品详情读取失败', work.error)
        self.assertEqual('failed', self.operation(service, old.task_id)['lifecycle'])
        self.assertEqual(page.PlanStore.fingerprint(old), page.PlanStore.fingerprint(service.plan_store.load(old.task_id)))
        state = service.decision_state_store.load(old.task_id)
        with self.assertRaises(ValueError):
            service.revise_tv_content(old.task_id, page.PlanStore.fingerprint(old),
                expected_revision=state.revision, mode='season', edit={'season_number': 2})

    async def test_first_selection_control(self):
        root, service, plan = await self.original()
        self.assertEqual('tmdb', plan.work_identity.primary_provider.namespace)
        self.assertEqual('tv_episodes', service.decision_state_store.load(plan.task_id).layout_mode)

    async def test_plain_movie_retry_remains_rejected(self):
        root, service, old = await self.setup_work(movie=True)
        from src.rename.recognition_queue_admission import _candidate
        from src.rename.plan_execution import PlanExecutionError
        manager = page.task_queue
        before = service.plan_store.path_for(old.task_id).read_bytes()
        with self.assertRaises(PlanExecutionError):
            _candidate(manager, old.task_id, 'retry', {}, manager._read(old.task_id), True)
        self.assertEqual(before, service.plan_store.path_for(old.task_id).read_bytes())
