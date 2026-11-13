"""Search recovery incidents: actual widgets/queue, disposable sources only."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from nicegui.client import Client
from src.pages import plan_review_page as page
from src.rename.process import Rename, _provider_candidate_options
from src.rename.provider_search import resolve_provider_item_url
from tests import test_initial_auto_link_contract as initial
from tests.test_initial_auto_link_contract import detail
from tests.test_provider_search_v5 import _Session
from tests import test_source_regrouping_view_contract as regroup


class PendingCandidateContracts(unittest.IsolatedAsyncioTestCase):
    flow = initial.InitialAutoLinkContracts.flow
    admit_auto = initial.InitialAutoLinkContracts.admit_auto

    async def test_pending_candidates_are_visible_selectable_and_unapproved(self):
        async with self.flow(admission_only=True, automatic=False, admit=self.admit_auto) as flow:
            candidate = _provider_candidate_options(detail(), entity_type='tv', selected_title='普通剧集')[0]
            candidate.update(match_eligible=False, match_reasons=['需要人工核对'])
            resolved = resolve_provider_item_url('https://www.themoviedb.org/tv/4242',
                tmdb_api_key='fixture-only', bangumi_user_agent='BAR/test', session=_Session(detail()))
            flow.manager._update(flow.task_id, provider_candidates={'work': [candidate, {**candidate, 'provider_id': '4243'}]})
            flow.manager.executor = flow.manager._execute_rename
            with Client.auto_index_client.content:
                dialog = page.PlanReviewDialog(None, flow.manager._read(flow.task_id))
            try:
                buttons = [e for e in dialog.descendants() if e._props.get('label') == '选择这个作品']
                self.assertEqual(2, len(buttons), '搜到的备选必须在尚无计划时显示')
                self.assertFalse(buttons[0]._props.get('disable'))
                with patch.object(page, '_read_manual_provider_url', return_value=resolved), \
                        patch.object(dialog, '_load_plan_view', wraps=dialog._load_plan_view) as load:
                    await dialog._select_provider_candidate(candidate)
                self.assertEqual(flow.task_id, load.call_args.args[0].task_id)
                self.assertIsNotNone(dialog.task_decision.confirmed_work)
                self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))
                self.assertTrue(all(not e.target_path.exists() for e in flow.store.load(flow.task_id).entries))
                for review in flow.manager.review_root.glob('*.json'):
                    value = json.loads(review.read_text(encoding='utf-8'))
                    self.assertEqual('draft', value['status'])
                    self.assertFalse(value['approved_entry_ids'])
            finally:
                dialog.delete()

    async def test_stale_candidate_cannot_create_workspace(self):
        async with self.flow(admission_only=True, admit=self.admit_auto) as flow:
            candidate = _provider_candidate_options(detail(), entity_type='tv', selected_title='普通剧集')[0]
            flow.manager._update(flow.task_id, provider_candidates={'work': [candidate]})
            with Client.auto_index_client.content:
                dialog = page.PlanReviewDialog(None, flow.manager._read(flow.task_id))
            try:
                flow.manager._update(flow.task_id, provider_candidates={})
                with patch.object(page, '_read_manual_provider_url', new_callable=AsyncMock) as read:
                    await dialog._select_provider_candidate(candidate)
                read.assert_not_awaited()
                self.assertFalse(flow.store.path_for(flow.task_id).exists())
            finally:
                dialog.delete()

    async def test_mismatched_detail_or_unsupported_candidate_keeps_no_plan(self):
        for unsupported in (False, True):
            with self.subTest(unsupported=unsupported):
                async with self.flow(admission_only=True, admit=self.admit_auto) as flow:
                    candidate = (dict(namespace='bangumi', entity_type='subject', provider_id='42', title='Movie')
                        if unsupported else _provider_candidate_options(detail(), entity_type='tv', selected_title='普通剧集')[0])
                    resolved = ({**candidate, 'library_domain': 'animation', 'platform': '音乐'}
                        if unsupported else {**candidate, 'provider_id': 'different', 'library_domain': 'general'})
                    flow.manager._update(flow.task_id, provider_candidates={'work': [candidate]})
                    with Client.auto_index_client.content:
                        dialog = page.PlanReviewDialog(None, flow.manager._read(flow.task_id))
                    try:
                        with patch.object(page, '_read_manual_provider_url', return_value=resolved):
                            await dialog._select_provider_candidate(candidate)
                        self.assertFalse(flow.store.path_for(flow.task_id).exists())
                        self.assertFalse(flow.service.decision_state_store.path_for(flow.task_id).exists())
                    finally:
                        dialog.delete()

    async def test_double_click_and_late_changed_task_do_not_publish(self):
        async with self.flow(admission_only=True, admit=self.admit_auto) as flow:
            candidate = _provider_candidate_options(detail(), entity_type='tv', selected_title='普通剧集')[0]
            flow.manager._update(flow.task_id, provider_candidates={'work': [candidate]})
            with Client.auto_index_client.content:
                dialog = page.PlanReviewDialog(None, flow.manager._read(flow.task_id))
            started, release = asyncio.Event(), asyncio.Event()
            async def delayed(_):
                started.set()
                await release.wait()
                return candidate
            try:
                with patch.object(page, '_read_manual_provider_url', side_effect=delayed) as read:
                    pending = asyncio.create_task(dialog._select_provider_candidate(candidate))
                    await started.wait()
                    await dialog._select_provider_candidate(candidate)
                    flow.manager._update(flow.task_id, provider_candidates={})
                    release.set()
                    await pending
                self.assertEqual(1, read.await_count)
                self.assertFalse(flow.store.path_for(flow.task_id).exists())
            finally:
                release.set()
                dialog.delete()


class SplitAdmissionContracts(unittest.IsolatedAsyncioTestCase):
    flow = regroup.SourceRegroupingViewContracts.flow
    current = regroup.SourceRegroupingViewContracts.current
    video = regroup.SourceRegroupingViewContracts.video
    link_subtitle = regroup.SourceRegroupingViewContracts.link_subtitle
    workspace = regroup.SourceRegroupingViewContracts.workspace
    click = regroup.SourceRegroupingViewContracts.click
    open_preview = regroup.SourceRegroupingViewContracts.open_preview

    async def test_confirmed_split_starts_each_video_group_but_not_attachments(self):
        async with self.workspace() as (flow, work):
            admitted = []
            async def capture(job):
                admitted.append(job.task_id)
            flow.manager.executor = capture
            popup = await self.open_preview(work)
            await popup.confirm()
            await asyncio.wait_for(flow.manager._queue.join(), 12)
            self.assertEqual(5, len(popup.created_ids))
            self.assertEqual(5, len(admitted), '五季已确认来源应各自动进入识别一次')
            self.assertEqual(5, len(set(admitted)))
            pending = [flow.manager._read(key) for key in popup.created_ids if key not in admitted]
            self.assertEqual([], pending)

    async def test_partial_admission_failure_keeps_cards_and_continues_after_close(self):
        async with self.workspace() as (flow, work):
            admitted = []
            async def capture(job):
                admitted.append(job.task_id)
            flow.manager.executor = capture
            original = flow.manager.confirm_source_group
            calls = []
            popup = await self.open_preview(work)
            async def admit(task_id, **kwargs):
                calls.append(task_id)
                if len(calls) == 1:
                    popup.close()
                    raise ValueError('fixture admission failure')
                return await original(task_id, **kwargs)
            with patch.object(flow.manager, 'confirm_source_group', side_effect=admit), \
                    patch.object(flow.manager, 'create_regrouped_tasks', wraps=flow.manager.create_regrouped_tasks) as create:
                await popup.confirm()
                await popup.confirm()
            await asyncio.wait_for(flow.manager._queue.join(), 12)
            self.assertEqual(1, create.call_count)
            self.assertEqual(5, len(calls))
            self.assertEqual(4, len(admitted))
            self.assertEqual(5, len(popup.created_ids))
            failed = flow.manager._read(calls[0])
            self.assertTrue(failed['source_group_pending'])
            self.assertIn('fixture admission failure', failed['error'])
            self.assertFalse(flow.manager._read(flow.task_id))


class TitleClueContracts(unittest.TestCase):
    def test_scope_does_not_read_sibling_video_or_accept_library_root(self):
        from src.rename.task_batch_scope import TaskSourceScope
        from src.rename.input_directory_discovery import discover_sources, SourceTitleUncertain
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'Anime' / 'Season 2'
            folder.mkdir(parents=True)
            selected = folder / 'Ordinary Show (2020) S02E01.mkv'
            selected.write_bytes(b'fixture')
            identities = discover_sources((selected,)).groups[0].identities
            scope = TaskSourceScope(1, 'owned', 'tv', 'fixture', 'fixture', 0, 'fixture', identities)
            (folder / 'Different Show S02E02.mkv').write_bytes(b'not assigned')
            with patch.object(Path, 'iterdir', side_effect=AssertionError('must use frozen scope')):
                self.assertEqual(('Ordinary Show', 2020), Rename.discovery_title_candidates(folder, task_source_scope=scope)[0].key)
            numbered = folder / '01.mkv'
            numbered.write_bytes(b'fixture')
            scope = TaskSourceScope(1, 'owned', 'tv', 'fixture', 'fixture', 0, 'fixture', discover_sources((numbered,)).groups[0].identities)
            with self.assertRaises(SourceTitleUncertain):
                Rename.discovery_title_candidates(folder, task_source_scope=scope)
            self.assertEqual('User Title', Rename.discovery_title_candidates(folder, 'User Title', task_source_scope=scope)[0].title)

    def test_numbered_season_uses_meaningful_parent_without_searching_season(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'Ordinary Show (2020)' / 'Season 2'
            folder.mkdir(parents=True)
            (folder / '01.mkv').write_bytes(b'fixture')
            values = Rename.discovery_title_candidates(folder)
            self.assertEqual(('Ordinary Show', 2020), values[0].key)
            self.assertNotIn('Season 2', [v.title for v in values])

    def test_one_atypical_video_does_not_erase_consistent_episode_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'Season 2'
            folder.mkdir()
            for name in ('Ordinary Show (2020) S02E01.mkv', 'Menu.mkv'):
                (folder / name).write_bytes(b'fixture')
            values = Rename.discovery_title_candidates(folder)
            self.assertEqual(('Ordinary Show', 2020), values[0].key)
