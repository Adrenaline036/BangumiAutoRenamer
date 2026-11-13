"""独立条目、人工季号与合包退役的事件形状验证。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tempfile
import unittest

from src.rename.season_identity import resolve_initial_tv_season
from src.rename.tv_completion_observations import TVSeasonObservation


def work(namespace='tmdb'):
    primary = dict(namespace=namespace, entity_type='tv' if namespace == 'tmdb' else 'subject', provider_id='45845')
    return SimpleNamespace(primary_provider=SimpleNamespace(to_dict=lambda: primary, provider_id='45845'))


class IndependentSeasonContracts(unittest.TestCase):
    def test_single_subject_history_is_not_a_pack(self):
        from src.rename.single_subject import require_single_subject_plan
        plan = SimpleNamespace(metadata={'provider_link_snapshot': {'season_assignments': [
            {'bangumi_subject_id': '101', 'season_number': 1}, {'bangumi_subject_id': '102', 'season_number': 2}]}})
        state = SimpleNamespace(tv_planning_authority=SimpleNamespace(season_assignments={'101': 1, '102': 2}))
        require_single_subject_plan(plan, state)
        state.tv_planning_authority.season_assignments = {'101': 1, '102': 1}
        with self.assertRaisesRegex(ValueError, '合包.*退役'):
            require_single_subject_plan(plan, state)

    def test_tmdb_work_details_allow_missing_season_advice(self):
        from src.rename.work_correction import _work
        from src.rename.content_associations import encode
        detail = dict(id=45845, name='命运之夜 前传', original_name='Fate/Zero', genres=[{'id': 16}], seasons=None)
        subject, observation = _work('45845', encode(detail), '2026-10-31T00:00:00Z', 'animation')
        self.assertEqual((), observation.available)
        self.assertEqual((3, 'explicit_user'), resolve_initial_tv_season(observation, subject, Path('Fate Zero'), 3, None))

    def test_special_observations_do_not_automatically_fetch_other_provider(self):
        from src.rename.special_crosswalk import observe
        bgm, tmdb = Mock(), Mock()
        bgm._get_json.return_value = dict(total=0, limit=100, offset=0, data=[])
        with patch('src.rename.special_crosswalk._counterpart', side_effect=AssertionError('禁止自动跨库')):
            result = observe(dict(namespace='bangumi', entity_type='subject', provider_id='101'), 1, bgm=bgm, tmdb=tmdb)
        self.assertTrue(result.displayable)
        self.assertEqual([], tmdb.mock_calls)

    def test_empty_tmdb_seasons_accept_manual_number(self):
        subject = work()
        observation = TVSeasonObservation.capture(subject, {'seasons': []})
        self.assertEqual((7, 'explicit_user'), resolve_initial_tv_season(observation, subject, Path('Fate Zero'), 7, None))

    def test_invalid_advisory_seasons_do_not_block_manual_number(self):
        for rows in (None, 'unavailable', [{'season_number': '1'}], [{'season_number': 1}, {'season_number': 1}]):
            with self.subTest(rows=rows):
                subject = work()
                observation = TVSeasonObservation.capture(subject, {'seasons': rows})
                self.assertEqual((2, 'explicit_user'), resolve_initial_tv_season(observation, subject, Path('Fate Zero'), 2, None))

    def test_valid_suggestion_and_manual_override_control(self):
        subject = work()
        observation = TVSeasonObservation.capture(subject, {'seasons': [{'season_number': 2}]})
        self.assertEqual((2, 'provider_unique'), resolve_initial_tv_season(observation, subject, Path('Fate Zero'), None, None))
        self.assertEqual((9, 'explicit_user'), resolve_initial_tv_season(observation, subject, Path('Fate Zero'), 9, None))
        with self.assertRaises(ValueError):
            resolve_initial_tv_season(observation, subject, Path('Fate Zero'), -1, None)

    def test_bangumi_selection_does_not_read_relations_or_tmdb(self):
        from src.rename.process import Rename
        client = Mock()
        client._get_json.return_value = dict(id=101, type=2, name='Fate/Zero', name_cn='命运之夜 前传', platform='TV', date='2011-10-01')
        client.load_crosswalk_evidence.side_effect = AssertionError('不应读取关系网')
        root = Path(tempfile.mkdtemp())
        worker = SimpleNamespace(bangumi_search=client, search=Mock(), active_consumer_task_id='isolated',
            decision_state_root=root / 'decisions', provider_link_catalog_root=root / 'links',
            initial_movie_request=SimpleNamespace(requested_season=1))
        worker.search.get_tv_info_by_id.side_effect = AssertionError('不应请求另一Provider')
        name, info, resolution = Rename._resolve_bangumi_provider_selection(worker,
            dict(namespace='bangumi', entity_type='subject', provider_id='101', title='命运之夜 前传'))
        self.assertEqual('命运之夜 前传', name)
        self.assertEqual('101', info['_bar_primary_provider']['provider_id'])
        self.assertIsNone(resolution)
        client._get_json.assert_called_once_with('/v0/subjects/101')
        client.load_crosswalk_evidence.assert_not_called()
        worker.search.get_tv_info_by_id.assert_not_called()

    def test_retired_boundary_write_is_rejected_before_access(self):
        from src.rename import tv_boundary_completion as boundary
        service = Mock()
        service._current_plan.side_effect = ValueError('旧合包写路径仍然读取任务')
        with self.assertRaisesRegex(ValueError, '合包.*退役'):
            boundary.prepare(service, 'old', 'fingerprint', review_command={})
        with self.assertRaisesRegex(ValueError, '合包.*退役'):
            boundary.commit(None, None)

    def test_old_multi_subject_plan_is_readonly_even_without_content_policy(self):
        from src.rename.content_policy import require_media_ready
        plan = SimpleNamespace(projection_receipt=None, metadata={'provider_link_snapshot': {'season_assignments': [
            {'bangumi_subject_id': '101', 'season_number': 1}, {'bangumi_subject_id': '102', 'season_number': 1}]}})
        with self.assertRaisesRegex(ValueError, '合包.*退役'):
            require_media_ready(plan)
        require_media_ready(SimpleNamespace(metadata={}, projection_receipt=None))

    def test_confirmed_bangumi_always_offers_manual_season(self):
        from src.pages.general_season_view import general_tv_provider
        primary = dict(namespace='bangumi', entity_type='subject', provider_id='101')
        owner = SimpleNamespace(task_decision=SimpleNamespace(confirmed_work={'primary_provider': primary}, library_domain='animation'),
                                plan=SimpleNamespace(metadata={}))
        with patch('src.rename.series_registration.is_manual_subject_registration', return_value=False):
            self.assertEqual(primary, general_tv_provider(owner))


class IndependentSeasonQueue(unittest.IsolatedAsyncioTestCase):
    async def test_empty_tmdb_seasons_can_continue_from_failure_control(self):
        from tests.test_initial_tv_completion_contract import InitialTVCompletionContracts
        from tests import test_r1_real_media_production_red_contract as flow
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        from src.pages import plan_review_page as page
        visited = []
        async def continue_manually(root, service, dialog, candidate, original):
            manager = page.task_queue
            plan = service.plan_store.load(dialog.plan.task_id)
            videos = [e.source_path for e in plan.entries if e.media_kind == 'video']
            ContentWorkbenchGui.create_page(self, prepared=(root, service, plan, videos))
            self.driver.button('作为独立作品整理')
            self.view.unresolved_season_input.set_value(4)
            with patch.object(page, 'task_queue', manager), patch.object(page.PlanReviewDialog, '_select_provider_candidate', original):
                await self.driver.click('作为独立作品整理')
            visited.append(service.decision_state_store.load(dialog.plan.task_id).season_binding['season_number'])
        detail = dict(id=4242, name='普通剧集', original_name='Ordinary Show', genres=[{'id': 18, 'name': 'Drama'}], seasons=[])
        with patch.object(flow._FrozenSearch, 'get_tv_info', return_value=('普通剧集', detail)):
            root, service, _, result, *_ = await InitialTVCompletionContracts.fixture(
                self, anime=False, selection_hook=continue_manually)
        self.assertEqual([4], visited)
        self.assertEqual(4, service.decision_state_store.load(result.plan.task_id).season_binding['season_number'])

    async def test_real_review_controls_remain_after_success_and_save(self):
        from tests.test_initial_tv_completion_contract import InitialTVCompletionContracts
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        from src.rename.plan_store import PlanStore
        root, service, _, result, *_ = await InitialTVCompletionContracts.fixture(self, anime=True, selected_season=1)
        videos = [e.source_path for e in result.plan.entries if e.media_kind == 'video']
        ContentWorkbenchGui.create_page(self, prepared=(root, service, result.plan, videos))
        self.driver.button('作为独立作品整理')
        self.driver.button('归入已有作品')
        await self.driver.click('更改季度')
        self.view.general_season_choice.manual.set_value(True)
        self.view.general_season_choice.custom.set_value(8)
        await self.driver.click('确认季度并生成新计划')
        state = service.decision_state_store.load(result.plan.task_id)
        self.assertEqual(8, state.season_binding['season_number'], self.notices)
        self.driver.button('作为独立作品整理')
        await self.driver.click('作为独立作品整理')
        await self.driver.click('确认作品归属')
        reopened = service.plan_store.load(result.plan.task_id)
        self.assertEqual(8, service.decision_state_store.load(result.plan.task_id).season_binding['season_number'])
        self.assertEqual(PlanStore.fingerprint(reopened), PlanStore.fingerprint(self.view.plan))
        self.driver.button('更改季度')
        self.assertTrue(all(not e.target_path.exists() for e in reopened.entries))

    async def test_single_subject_plan_save_reopen_and_manual_season(self):
        from tests.test_initial_tv_completion_contract import InitialTVCompletionContracts
        from src.rename.plan_store import PlanStore
        from src.rename.plan_verifier import tv_authority_issue
        root, service, _, result, extras, *_ = await InitialTVCompletionContracts.fixture(
            self, anime=True, selected_season=1, companions=True)
        self.assertIsNotNone(result.plan, result.task.get('error'))
        self.assertIsNone(result.plan.metadata.get('tv_completion_resolution'))
        self.assertIsNone(result.plan.metadata.get('provider_link_snapshot'))
        self.assertIsNone(result.state.provider_link_binding)
        self.assertFalse(result.state.linked_provider_identities)
        self.assertIsNone(tv_authority_issue(result.plan))
        state = service.decision_state_store.load(result.plan.task_id)
        associations = state.content_selection.associations
        revised = service.revise_tv_content(result.plan.task_id, PlanStore.fingerprint(result.plan),
            expected_revision=state.revision, mode='season', edit={'season_number': 7})
        reopened = service.plan_store.load(revised.task_id)
        self.assertEqual(PlanStore.fingerprint(revised), PlanStore.fingerprint(reopened))
        state = service.decision_state_store.load(revised.task_id)
        self.assertEqual(7, state.season_binding['season_number'])
        self.assertEqual('explicit_user', state.season_binding['decision_source'])
        self.assertIsNone(tv_authority_issue(reopened))
        self.assertIsNone(reopened.metadata.get('provider_link_snapshot'))
        self.assertIsNone(state.provider_link_binding)
        self.assertEqual(associations, state.content_selection.associations)
        self.assertTrue(all(not e.target_path.exists() for e in reopened.entries))
