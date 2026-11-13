"""独立OVA通过真实队列及来源读取边界，人工季度与分集类型保持独立。"""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
import unittest

from tests.test_initial_tv_completion_contract import InitialTVCompletionContracts
from tests import test_r1_real_media_production_red_contract as fixture
from src.rename.review_operation import build_review_operation
from src.rename.special_catalog import read_source
from src.rename.plan_store import PlanStore
from src.rename.selection_gate import validate_selection_for_plan
from src.pages.general_season_view import render_unresolved_tmdb_season
import src.pages.plan_review_page as page
from nicegui import Client, ui
from nicegui.page import page as Page


def ova_subject():
    return {**fixture.YAMA_SUBJECTS['41658'], 'platform': 'OVA', 'eps': 2}


class OvaSelection(InitialTVCompletionContracts):
    async def test_ova_link_waits_for_manual_season_without_replacing_original_plan(self):
        observed = []
        async def continue_season(root, service, dialog, candidate, original):
            await page.task_queue._queue.join()
            task_path = root/'tasks'/f'{dialog.plan.task_id}.json'
            task = json.loads(task_path.read_text('utf-8'))
            operation, _ = build_review_operation(task)
            self.assertEqual('awaiting_season', operation.context.get('lifecycle'), task.get('error'))
            self.assertEqual('blocked', operation.state.value)
            self.assertTrue(operation.blocking_reasons)
            self.assertIsNone(task.get('error'))
            self.assertEqual('need_confirm', task['workflow_status'])
            self.assertEqual(dialog.task_decision, service.decision_state_store.load(dialog.plan.task_id))
            self.assertEqual(dialog.plan.to_dict(), service.plan_store.load(dialog.plan.task_id).to_dict())
            with self.assertRaises(ValueError):
                validate_selection_for_plan(task)
            from src.rename.ova_selection import OvaSeasonPending, pending_subject, persist_pending
            from src.rename.special_catalog import plain
            context = plain(operation.context)
            self.assertEqual('41658', pending_subject(task, context).provider_id)
            with self.assertRaises(ValueError):
                pending_subject({**task, 'uuid': 'different-task'}, context)
            with self.assertRaises(ValueError):
                pending_subject(task, {**context, 'result_decision_revision': -1})
            unchanged = task_path.read_bytes()
            stale = SimpleNamespace(task_id=dialog.plan.task_id, request_id='outdated',
                expected_revision=dialog.task_decision.revision, provider_selection=candidate)
            with self.assertRaises(ValueError):
                persist_pending(page.task_queue, stale, OvaSeasonPending.from_mapping(context['ova_subject_selection']))
            self.assertEqual(unchanged, task_path.read_bytes())
            original_plan = service.plan_store.load(dialog.plan.task_id).to_dict()
            original_state = service.decision_state_store.load(dialog.plan.task_id)
            dialog.task_data = task
            dialog.task_id = dialog.plan.task_id
            dialog.exit_requested = dialog.is_deleted = False
            dialog._select_provider_candidate = lambda value, **kw: original(dialog, value, **kw)
            client = Client(Page('/ova-season-contract'), request=None)
            with client, patch.object(page, 'get_task', lambda _: json.loads(task_path.read_text('utf-8'))):
                render_unresolved_tmdb_season(dialog, ui)
                controls = [e for e in client.elements.values() if isinstance(e, ui.button) and e.text == '确认季度并继续']
                self.assertEqual(1, len(controls), '真实人工季号入口必须可用')
                self.assertIsNone(dialog.unresolved_season_input.value, '不能自动套用续作季号')
                dialog.unresolved_season_input.set_value(3)
                click = next(e for e in controls[0]._event_listeners.values() if e.type == 'click')
                scheduled = []
                def schedule(coro, **kwargs):
                    task = asyncio.create_task(coro)
                    scheduled.append(task)
                    return task
                with patch('nicegui.core.loop', asyncio.get_running_loop()), patch('nicegui.background_tasks.create', side_effect=schedule):
                    click.handler(None)
                    self.assertEqual(1, len(scheduled))
                    await scheduled[0]
                await page.task_queue._queue.join()
            observed.append((task, original_plan, original_state))
        with patch.dict(fixture.YAMA_SUBJECTS, {'41658': ova_subject()}):
            root, service, before, result, *_ = await self.fixture(anime=True, duplicate=True, exclude_duplicate=True, excluded_misc=True, selection_hook=continue_season)
        self.assertEqual(1, len(observed))
        task, pending_plan, pending_state = observed[0]
        self.assertEqual(before['plan'].to_dict(), pending_plan)
        self.assertEqual(before['state'], pending_state)
        operation, _ = build_review_operation(task)
        self.assertEqual(before['state'].revision, operation.context['expected_decision_revision'])
        self.assertEqual(before['state'].revision, operation.context['result_decision_revision'])
        self.assertEqual(PlanStore.fingerprint(before['plan']), operation.context['result_plan_fingerprint'])
        self.assert_ova_completed(result, 3)

    async def test_ova_manual_season_three_does_not_need_tmdb_or_sequel_assignment(self):
        with patch.dict(fixture.YAMA_SUBJECTS, {'41658': ova_subject()}), \
                patch.dict(fixture.YAMA_RELATIONS, {'41658': ()}), \
                patch.object(fixture._FrozenSearch, 'get_tv_info', return_value=('', None)), \
                patch.object(fixture._FrozenSearch, 'get_tv_info_by_id', return_value=('', None)), \
                patch.object(fixture._FrozenSearch, 'get_season_info', return_value=None):
            root, service, before, result, *_ = await self.fixture(anime=True, selected_season=3)
        self.assertEqual(3, (result.state.season_binding or {}).get('season_number'), result.task.get('error'))
        self.assertEqual('explicit_user', result.state.season_binding.get('decision_source'))
        self.assertEqual('41658', result.state.confirmed_work['primary_provider']['provider_id'])
        self.assert_ova_completed(result, 3)

    def assert_ova_completed(self, result, season):
        operation, _ = build_review_operation(result.task)
        self.assertEqual('succeeded', operation.context.get('lifecycle'), result.task.get('error'))
        self.assertIsNone(result.task.get('error'))
        validate_selection_for_plan(result.task)
        entries = [e for e in result.plan.entries if e.media_kind in {'video', 'subtitle'}]
        self.assertEqual(4, len(entries))
        for episode in (1, 2):
            pair = [e for e in entries if e.episode_id == episode]
            self.assertEqual(['subtitle', 'video'], sorted(e.media_kind for e in pair))
            video = next(e for e in pair if e.media_kind == 'video')
            subtitle = next(e for e in pair if e.media_kind == 'subtitle')
            self.assertTrue(subtitle.target_path.name.startswith(video.target_path.stem + '.'))
            self.assertEqual(video.source_path.stem, subtitle.source_path.stem.rsplit('.', 1)[0])
        self.assertEqual({season}, {e.season_id for e in entries})
        self.assertEqual({1, 2}, {e.episode_id for e in entries})
        self.assertEqual({f'Season {season:02d}'}, {e.target_path.parent.name for e in entries})


class OvaReferences(unittest.TestCase):
    def source(self, platform):
        provider = dict(namespace='bangumi', entity_type='subject', provider_id='123')
        rows = [dict(id=1001, subject_id=123, type=0, sort=1, name_cn='正篇'),
                dict(id=1002, subject_id=123, type=1, sort=1, name_cn='总集篇')]
        def fetch(path):
            if path == '/v0/subjects/123':
                return dict(id=123, type=2, platform=platform, name_cn='独立条目', name='OVA', date='2020-01-01')
            query = parse_qs(urlsplit(path).query)
            selected = [r for r in rows if 'type' not in query or str(r['type']) == query['type'][0]]
            return dict(total=len(selected), offset=0, data=deepcopy(selected))
        return read_source(provider, client=SimpleNamespace(_get_json=fetch))

    def test_ova_main_episode_and_special_episode_are_both_available(self):
        result = self.source('OVA')
        self.assertTrue(result['complete'], result)
        self.assertEqual({'1001', '1002'}, {r['provider_episode_id'] for r in result['rows']})

    def test_ordinary_tv_does_not_offer_main_episodes_as_specials(self):
        result = self.source('TV')
        self.assertTrue(result['complete'], result)
        self.assertEqual([], result['issues'])
        self.assertEqual({'1002'}, {r['provider_episode_id'] for r in result['rows']})
