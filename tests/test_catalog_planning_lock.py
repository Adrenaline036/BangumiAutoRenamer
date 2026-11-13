"""公共TV修订期间的登记并发、漂移和任务互斥。"""
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests import test_general_season_correction_contract as fixture
from src.rename import initial_tv_completion as tv
from src.rename.plan_store import PlanStore
from src.rename.projection_authority import ProjectionCommitService, ProjectionAuthorityError
from src.rename.work_affiliation import catalog_choices


class CatalogPlanningLockContracts(unittest.IsolatedAsyncioTestCase):
    flow = fixture.GeneralSeasonCorrectionContracts.flow
    current = fixture.GeneralSeasonCorrectionContracts.current

    def revise(self, flow, plan, state):
        return flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(plan),
            expected_revision=state.revision, mode='season', edit={'season_number': 2})

    def durable(self, flow):
        service = flow.service
        return (service.plan_store.path_for(flow.task_id).read_bytes(),
                service.decision_state_store.path_for(flow.task_id).read_bytes())

    async def test_control_catalog_choices_without_planning_lock(self):
        async with self.current() as (flow, plan, state):
            with ThreadPoolExecutor(max_workers=1) as pool:
                rows = pool.submit(catalog_choices, flow.service, flow.task_id,
                                   PlanStore.fingerprint(plan)).result(timeout=3)
            self.assertEqual([], rows)

    async def test_public_builder_allows_other_thread_catalog_read(self):
        async with self.current() as (flow, plan, state):
            original = tv.build_initial_tv_completion
            visited = []
            def build(*args, **kwargs):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    rows = pool.submit(catalog_choices, flow.service, flow.task_id,
                                       PlanStore.fingerprint(plan)).result(timeout=3)
                visited.append(rows)
                return original(*args, **kwargs)
            with patch.object(tv, 'build_initial_tv_completion', build):
                updated = self.revise(flow, plan, state)
            self.assertEqual([[]], visited)
            self.assertEqual(2, updated.metadata['season_number'])

    async def test_collision_observation_allows_other_thread_catalog_read(self):
        async with self.current() as (flow, plan, state):
            original = tv.observe_collisions
            visited = []
            def observe(*args, **kwargs):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    rows = pool.submit(catalog_choices, flow.service, flow.task_id,
                                       PlanStore.fingerprint(plan)).result(timeout=3)
                visited.append(rows)
                return original(*args, **kwargs)
            with patch.object(tv, 'observe_collisions', observe):
                updated = self.revise(flow, plan, state)
            self.assertEqual([[]], visited)
            self.assertEqual(2, updated.metadata['season_number'])

    async def test_prepare_source_projection_does_not_hold_namespace(self):
        async with self.current() as (flow, plan, state):
            original = tv.frozen_sources
            visited = []
            def sources(*args, **kwargs):
                if not visited:
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        rows = pool.submit(catalog_choices, flow.service, flow.task_id,
                                           PlanStore.fingerprint(plan)).result(timeout=3)
                    visited.append(rows)
                return original(*args, **kwargs)
            with patch.object(tv, 'frozen_sources', sources):
                self.revise(flow, plan, state)
            self.assertEqual([[]], visited)

    async def test_task_change_during_build_rejects_publish(self):
        from src.utils.utils import merge_task_at
        async with self.current() as (flow, plan, state):
            original = tv.build_initial_tv_completion
            before = self.durable(flow)
            visited = []
            def build(*args, **kwargs):
                built = original(*args, **kwargs)
                merge_task_at(flow.service.task_root, flow.task_id, {'name': '另一人工决定'})
                visited.append(True)
                return built
            with patch.object(tv, 'build_initial_tv_completion', build):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.revise(flow, plan, state)
            self.assertEqual([True], visited)
            self.assertEqual(before, self.durable(flow))

    async def test_same_thread_catalog_reentry_is_rejected(self):
        async with self.current() as (flow, plan, state):
            key = plan.metadata['series_key']
            with flow.service.series_catalog.lock(key):
                with self.assertRaisesRegex((ValueError, RuntimeError), '不支持嵌套'):
                    catalog_choices(flow.service, flow.task_id, PlanStore.fingerprint(plan))

    async def test_concurrent_registration_during_build_rejects_publish(self):
        async with self.current() as (flow, plan, state):
            original = tv.build_initial_tv_completion
            before = self.durable(flow)
            changed = []
            def build(*args, **kwargs):
                built = original(*args, **kwargs)
                def register():
                    flow.service.series_catalog.register_completed(plan)
                    changed.append(True)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(register).result(timeout=5)
                return built
            with patch.object(tv, 'build_initial_tv_completion', build):
                with self.assertRaisesRegex((ValueError, RuntimeError), '快照已变化'):
                    self.revise(flow, plan, state)
            self.assertEqual([True], changed)
            self.assertEqual(before, self.durable(flow))

    async def test_same_task_transaction_stays_exclusive_during_build(self):
        async with self.current() as (flow, plan, state):
            original = tv.build_initial_tv_completion
            visited = []
            def build(*args, **kwargs):
                transaction = ProjectionCommitService(flow.service.decision_state_store,
                    flow.service.plan_store, task_root=flow.service.task_root)
                def attempt():
                    with self.assertRaisesRegex(ProjectionAuthorityError, 'already active'):
                        with transaction._transaction_lock(flow.task_id):
                            self.fail('同一卡片事务不能重入')
                    visited.append(True)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(attempt).result(timeout=3)
                return original(*args, **kwargs)
            with patch.object(tv, 'build_initial_tv_completion', build):
                self.revise(flow, plan, state)
            self.assertEqual([True], visited)

    async def test_builder_failure_preserves_plan_and_releases_locks(self):
        async with self.current() as (flow, plan, state):
            before = self.durable(flow)
            with patch.object(tv, 'build_initial_tv_completion', side_effect=ValueError('生成失败')):
                with self.assertRaisesRegex(ValueError, '生成失败'):
                    self.revise(flow, plan, state)
            self.assertEqual(before, self.durable(flow))
            self.assertEqual([], catalog_choices(flow.service, flow.task_id, PlanStore.fingerprint(plan)))
            updated = self.revise(flow, plan, state)
            self.assertEqual(2, updated.metadata['season_number'])
