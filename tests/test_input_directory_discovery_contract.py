"""Complex-root AddTask discovery must publish complete, separate source cards."""
from tests.queue_fixture import TaskQueueManager as ControlledQueue
import asyncio
import inspect
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock
from types import SimpleNamespace

from nicegui.client import Client
from nicegui.page import page as nice_page

from src.pages import add_task_page as add_page
from src.rename.process import Rename
from src.rename.task_batch_scope import TaskSourceScope
from tests import test_default_activation_contract as activation


class InputDirectoryDiscoveryContracts(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow

    def complex_source(self, root, source):
        payload = next(source.glob('*S01E01*.mkv'))
        incoming = root / 'incoming' / 'Ordinary Show (2020) Season 1-5 S01-S05 [1080p]'
        for season in range(1, 6):
            folder = incoming / f'Season {season}'
            folder.mkdir(parents=True)
            video = folder / f'Ordinary Show (2020) - S{season:02d}E01 - Opening [1080p].mkv'
            shutil.copyfile(payload, video)
            video.with_suffix('.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nsubtitle\n', encoding='utf-8')
        for folder in ('Featurettes/Season 1', 'SPs', 'CDs'):
            destination = incoming / folder
            destination.mkdir(parents=True)
            shutil.copyfile(payload, destination / 'Behind the Scenes.mkv')
        (incoming / 'Shared Fonts').mkdir()
        (incoming / 'Shared Fonts' / 'Fonts.zip').write_bytes(b'owned pending package must not be opened')
        (incoming / 'notes.txt').write_text('unassigned release note', encoding='utf-8')
        self.original_files = {path: path.read_bytes() for path in incoming.rglob('*') if path.is_file()}
        return incoming

    def configure(self, worker):
        worker.bangumi_search = None
        self.provider_calls = []
        original = worker._provider_search_batch
        def batch(*args, **kwargs):
            self.provider_calls.append(str(worker.automatic_recognition_origin.path)
                if worker.automatic_recognition_origin is not None else 'legacy')
            return original(*args, **kwargs)
        worker._provider_search_batch = batch
        tv = worker.search.get_tv_info
        def television(*args):
            name, info = tv(*args)
            if info:
                info = {**info, 'seasons': [{'season_number': i, 'name': f'Season {i}'} for i in range(1, 6)]}
            return name, info
        worker.search.get_tv_info = television

    async def admit_ui(self, manager, source, task_id):
        client = Client(nice_page('/isolated-directory-discovery'), request=None)
        self.addCleanup(client.delete)
        # This fixture checks admission/worker results, not a modal interaction.
        # The separate single-source navigation tests exercise its real wait.
        with patch.object(add_page, 'task_queue', manager), \
                client:
            dialog = add_page.AddTaskDialog()
            try:
                dialog.path = str(source)
                next_step = dialog._next()
                if inspect.isawaitable(next_step):
                    await next_step
                await dialog._submit()
                self.actual_ids = [path.stem for path in manager.task_root.glob('*.json')]
            finally:
                dialog.delete()

    async def test_actual_add_task_creates_five_auto_seasons_and_three_pending_kinds_plus_attachments(self):
        async with self.flow(configure_source=self.complex_source, queue_timeout=30,
                configure_worker=self.configure, admit=self.admit_ui) as flow:
            self.assertEqual(8, len(self.actual_ids), 'one complex root must not remain one job')
            assigned, normal, pending = [], [], []
            for task_id in self.actual_ids:
                task = flow.manager._read(task_id)
                scope = TaskSourceScope.from_mapping(task['task_source_scope'])
                self.assertEqual({row.source_path for row in scope.source_identities},
                    {row['source_path'] for row in task['source_group_observation']['identities']})
                assigned.extend(Path(row.source_path) for row in scope.source_identities)
                self.assertEqual('need_confirm', task['workflow_status'], task.get('error'))
                if task.get('source_group_pending'):
                    pending.append(task)
                    self.assertTrue(task.get('error'))
                    self.assertFalse(flow.store.path_for(task_id).exists())
                    self.assertFalse(flow.service.decision_state_store.path_for(task_id).exists())
                else:
                    normal.append(task)
                    state = flow.service.decision_state_store.load(task_id)
                    self.assertTrue(state.confirmed_work)
                    self.assertEqual('tv', state.confirmed_work['primary_provider']['entity_type'])
                    self.assertIsNone(task['automatic_recognition_origin']['is_movie'])
                    self.assertIsNone(task['automatic_recognition_origin']['requested_season'])
                    self.assertTrue(all(not entry.target_path.exists() for entry in flow.store.load(task_id).entries))
            self.assertEqual({1, 2, 3, 4, 5}, {task['season_id'] for task in normal})
            self.assertEqual(3, len(pending))
            self.assertEqual(len(set(assigned)), len(assigned), 'source ownership overlapped')
            retired = {path for path in self.original_files if path.name in {'Fonts.zip', 'notes.txt'}}
            self.assertEqual(2, len(retired))
            self.assertTrue(retired.isdisjoint(assigned))
            self.assertEqual(set(self.original_files) - retired, set(assigned), '媒体及其伴随文件归属必须完整')
            self.assertEqual({path: self.original_files[path] for path in retired},
                             {path: path.read_bytes() for path in retired}, '退役杂项必须仍在原位且内容不变')
            self.assertEqual({task['path'] for task in normal}, set(self.provider_calls))
            self.assertEqual(self.original_files, {path: path.read_bytes() for path in self.original_files})
            self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))
            for path in flow.manager.review_root.glob('*.json'):
                self.assertFalse(json.loads(path.read_text(encoding='utf-8')).get('approved_entry_ids'))

    async def test_fresh_queue_and_ordinary_retry_do_not_clear_pending_source_group(self):
        from src.task_queue import TaskQueueManager
        async with self.flow(configure_source=self.complex_source, queue_timeout=30,
                configure_worker=self.configure, admit=self.admit_ui) as flow:
            pending = [task_id for task_id in self.actual_ids
                if flow.manager._read(task_id).get('source_group_pending')]
            self.assertEqual(3, len(pending), 'uncertain sources need durable pending cards')
            await flow.manager.stop()
            fresh = TaskQueueManager(task_root=flow.manager.task_root, plan_root=flow.store.root,
                decision_state_root=flow.service.decision_state_store.root,
                provider_link_catalog_root=flow.manager.provider_link_catalog_root)
            await fresh.start()
            try:
                before = list(self.provider_calls)
                for task_id in pending:
                    with self.assertRaises((ValueError, RuntimeError)):
                        await fresh.retry_current(task_id)
                    self.assertTrue(fresh._read(task_id)['source_group_pending'])
                    self.assertFalse(flow.store.path_for(task_id).exists())
                self.assertEqual(before, self.provider_calls)
            finally:
                await fresh.stop()

    async def test_pending_actual_workspace_link_and_explicit_same_group_confirmation(self):
        from src.pages import plan_review_page as page
        from src.pages.recognition_recovery_view import RecognitionRecoveryDialog
        from src.rename.recognition_queue_admission import capture_workspace_open, admit
        from src.rename.default_activation import ensure_workspace
        async with self.flow(configure_source=self.complex_source, queue_timeout=30,
                configure_worker=self.configure, admit=self.admit_ui) as flow:
            task_id = next(key for key in self.actual_ids if flow.manager._read(key).get('source_group_pending')
                           and Path(flow.manager._read(key)['path']).name == 'SPs')
            current = flow.manager._read(task_id)
            opened = capture_workspace_open(flow.manager, task_id, current)
            with self.assertRaisesRegex(ValueError, '完整来源组'):
                ensure_workspace(flow.service, task_id, roots=flow.service.root_snapshot_source.observe())
            for mode in ('retry', 'workspace', 'selected'):
                with self.subTest(mode=mode), self.assertRaises((ValueError, RuntimeError)):
                    await admit(flow.manager, task_id, mode=mode, request={})
            client = Client(nice_page('/pending-source-contract'), request=None)
            try:
                with client, patch.object(page, '_read_manual_provider_url', new=AsyncMock()) as lookup:
                    dialog = RecognitionRecoveryDialog(task_id, current, ValueError('no plan'))
                    try:
                        labels = [getattr(element, 'text', '') for element in dialog.descendants()]
                        self.assertIn('继续识别来源', labels)
                        self.assertNotIn('读取链接并选择此作品', labels)
                        await dialog._select_url()  # Even a stale/direct handler cannot read Provider.
                        lookup.assert_not_awaited()
                    finally:
                        dialog.delete()
                    class PendingView(RecognitionRecoveryDialog):
                        def __await__(self):
                            async def shown():
                                self.was_shown = True
                            return shown().__await__()
                    with patch.object(page, 'RecognitionRecoveryDialog', PendingView), patch(
                            'src.pages.initial_workspace.create_initial_workspace', new=AsyncMock()) as workspace:
                        await page.plan_review_page(task_id)
                        workspace.assert_not_awaited()
                before = len(self.provider_calls)
                await flow.manager.confirm_source_group(task_id, opened_evidence=opened)
                await asyncio.wait_for(flow.manager._queue.join(), 12)
                result = flow.manager._read(task_id)
                self.assertFalse(result['source_group_pending'])
                self.assertEqual(current['task_source_scope'], result['task_source_scope'])
                self.assertGreater(len(self.provider_calls), before)
                self.assertIsNone(result['automatic_recognition_origin']['is_movie'])
                self.assertIsNone(result['automatic_recognition_origin']['custom_name'])
                with self.assertRaises((ValueError, RuntimeError)):
                    await flow.manager.confirm_source_group(task_id, opened_evidence=opened)
            finally:
                client.delete()

    async def test_pending_later_task_scope_or_authority_cannot_be_confirmed(self):
        from src.rename.recognition_queue_admission import capture_workspace_open
        async with self.flow(configure_source=self.complex_source, queue_timeout=30,
                configure_worker=self.configure, admit=self.admit_ui) as flow:
            ids = [key for key in self.actual_ids if flow.manager._read(key).get('source_group_pending')]
            before_calls = list(self.provider_calls)
            for key, change in zip(ids, ('task', 'source', 'authority', 'executed')):
                with self.subTest(change=change):
                    current = flow.manager._read(key)
                    opened = capture_workspace_open(flow.manager, key, current)
                    if change == 'task':
                        flow.manager._update(key, error='later task')
                    elif change == 'source':
                        scope = current['task_source_scope']
                        scope['source_identities'][0]['size'] += 1
                        flow.manager._update(key,task_source_scope=scope)
                    elif change == 'authority':
                        flow.store.path_for(key).write_text('{}', encoding='utf-8')
                    else:
                        flow.manager._update(key, execution_status='completed')
                        opened = capture_workspace_open(flow.manager, key, flow.manager._read(key))
                    with self.assertRaises((ValueError, RuntimeError)):
                        await flow.manager.confirm_source_group(key, opened_evidence=opened)
                    self.assertTrue(flow.manager._read(key)['source_group_pending'])
            self.assertEqual(before_calls, self.provider_calls)

    async def test_empty_or_conflicting_season_title_stays_unconfirmed_without_provider(self):
        for shape in ('empty', 'conflict', 'wrong-season'):
            with self.subTest(shape=shape):
                def source(root, original):
                    folder = root / 'incoming' / 'Season 2'
                    folder.mkdir(parents=True)
                    video = next(original.glob('*S01E01*.mkv'))
                    names = ('S02E01.mkv',) if shape == 'empty' else (
                        'Ordinary Show (2020) - S02E01 - Opening.mkv',
                        'Other Show (2021) - S02E02 - Opening.mkv' if shape == 'conflict' else
                        'Ordinary Show (2020) - S03E02 - Opening.mkv')
                    for name in names:
                        shutil.copyfile(video, folder / name)
                    return folder
                async with self.flow(configure_source=source, configure_worker=self.configure, admit=self.admit_ui) as flow:
                    self.assertEqual(1, len(self.actual_ids))
                    key = self.actual_ids[0]
                    task = flow.manager._read(key)
                    self.assertEqual('need_confirm', task['workflow_status'], task.get('error'))
                    self.assertIn('非空一致', task['error'])
                    self.assertFalse(flow.store.path_for(key).exists())
                    self.assertFalse(flow.service.decision_state_store.path_for(key).exists())
                    self.assertEqual([], self.provider_calls)

    async def test_structure_season_movie_bait_does_not_pollute_actual_tv_queue(self):
        queries = []
        def source(root, original):
            folder = root / 'incoming' / 'Season 2'
            folder.mkdir(parents=True)
            shutil.copyfile(next(original.glob('*S01E01*.mkv')),
                folder / 'Ordinary Show (2020) - S02E01 - Opening.mkv')
            return folder
        def configure(worker):
            self.configure(worker)
            original = worker.search.get_movie_info
            def movie(title, year=0):
                queries.append((title, year))
                if title == 'Season 2':
                    _, info = original('Ordinary Movie')
                    return 'Mantras Season 2', {**info, 'title': 'Mantras Season 2',
                        'original_title': 'Mantras Season 2', 'release_date': '2023-01-01'}
                return '', None
            worker.search.get_movie_info = movie
        async with self.flow(configure_source=source, configure_worker=configure, admit=self.admit_ui) as flow:
            self.assertEqual([('Ordinary Show', 2020)], queries)
            key = self.actual_ids[0]
            self.assertEqual('need_confirm', flow.manager._read(key)['workflow_status'])
            self.assertEqual(2, flow.manager._read(key)['season_id'])
            self.assertEqual('tv', flow.service.decision_state_store.load(key).confirmed_work['primary_provider']['entity_type'])
            self.assertTrue(all(not entry.target_path.exists() for entry in flow.store.load(key).entries))


class SeasonDirectoryQueryObservationContracts(unittest.TestCase):
    def test_generic_season_directory_uses_nonempty_consistent_actual_episode_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'Ordinary Show (2020) Season 1-5 S01-S05 [1080p]' / 'Season 2'
            folder.mkdir(parents=True)
            for episode in (1, 2):
                (folder / f'Ordinary Show (2020) - S02E{episode:02d} - Opening [1080p].mkv').write_bytes(b'owned observation')
            subtitle = folder / 'unassigned.srt'
            subtitle.write_text('kept in original source group', encoding='utf-8')
            values = Rename.discovery_title_candidates(folder)
            self.assertEqual(('Ordinary Show', 2020), values[0].key)
            self.assertTrue(subtitle.exists())
            (folder / 'Different Show (2021) - S02E03 - Opening.mkv').write_bytes(b'owned conflict')
            with self.assertRaisesRegex(ValueError, '非空一致'):
                Rename.discovery_title_candidates(folder)


class SourceDiscoveryObservationContracts(unittest.TestCase):
    def test_mixed_source_and_ordinary_controls_are_lossless(self):
        from src.rename.input_directory_discovery import discover_sources
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'Show.mkv').write_bytes(b'owned metadata fixture')
            (root / 'sub.srt').write_text('owned', encoding='utf-8')
            single = discover_sources((root / 'Show.mkv',))
            self.assertEqual(1, len(single.groups))
            self.assertFalse(single.groups[0].reason)
            ordinary = discover_sources((root,))
            self.assertEqual(2, len(ordinary.groups[0].identities))
            self.assertFalse(ordinary.groups[0].reason)
            (root / 'Season 2').mkdir()
            (root / 'Season 2' / 'Show S02E01.mkv').write_bytes(b'owned second')
            mixed = discover_sources((root,))
            self.assertEqual(1, len(mixed.groups))
            self.assertIn('混合', mixed.groups[0].reason)
            self.assertEqual(3, len(mixed.groups[0].identities))

    def test_capture_limits_and_explicit_refresh_preserve_frozen_preview(self):
        from src.rename import input_directory_discovery as discovery
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'video.mkv').write_bytes(b'owned')
            frozen = discovery.discover_sources((root,))
            with self.assertRaises(ValueError):
                discovery.discover_sources((root,), max_entries=1)
            with self.assertRaises(ValueError):
                discovery.discover_sources((root,), max_depth=0)
            with self.assertRaises(ValueError):
                discovery.discover_sources((root, root / 'video.mkv'))
            with patch.object(discovery, 'first_reparse_component', return_value=root), self.assertRaises(ValueError):
                discovery.discover_sources((root,))
            with patch.object(discovery.os, 'scandir', side_effect=PermissionError('owned denied')), self.assertRaises(OSError):
                discovery.discover_sources((root,))
            (root / 'new.srt').write_text('added', encoding='utf-8')
            discovery.validate_discovery(frozen)
            fresh=discovery.discover_sources((root,))
            self.assertNotEqual(frozen.identities,fresh.identities)


class SourceDiscoveryClientContracts(unittest.IsolatedAsyncioTestCase):
    async def test_missing_or_trimmed_discovery_cannot_silently_publish_partial_cards(self):
        from src.task_queue import TaskQueueManager
        from src.rename.input_directory_discovery import discover_sources
        for shape in ('missing', 'none', 'trimmed'):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / 'source'
                for season in (1, 2):
                    folder = source / f'Season {season}'
                    folder.mkdir(parents=True)
                    (folder / f'Show S0{season}E01.mkv').write_bytes(b'owned observation')
                manager = ControlledQueue(task_root=root / 'tasks', plan_root=root / 'plans', test_mode=True)
                original = manager.prepare_discovered_batch
                discovery = discover_sources((source,))
                def trimmed(*args, **kwargs):
                    batch = original(*args, **kwargs)
                    batch['jobs'] = batch['jobs'][:1]
                    batch['task_requests'] = batch['task_requests'][:1]
                    if shape == 'missing':
                        batch.pop('discovery')
                    elif shape == 'none':
                        batch['discovery'] = None
                    return batch
                try:
                    batch = trimmed(discovery, is_anime=None, is_movie=None, source='ui')
                    with self.assertRaises((ValueError, RuntimeError)):
                        await manager.enqueue_batch(**batch)
                    self.assertFalse(list(manager.task_root.glob('*.json')))
                    client = Client(nice_page('/incomplete-group'), request=None)
                    try:
                        with client, patch.object(add_page, 'task_queue', manager), patch.object(manager, 'prepare_discovered_batch', side_effect=trimmed):
                            dialog = add_page.AddTaskDialog()
                            dialog.path = str(source)
                            await dialog._next()
                            await dialog._submit()
                            self.assertFalse(list(manager.task_root.glob('*.json')))
                            dialog.delete()
                    finally:
                        client.delete()
                finally:
                    await manager.stop()

    async def test_cancel_change_late_and_double_next_actual_client(self):
        from src.rename.input_directory_discovery import discover_sources
        for action in ('close', 'change', 'delete', 'dismiss', 'double'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / 'video.mkv').write_bytes(b'owned')
                observed = discover_sources((root,))
                entered, release = asyncio.Event(), asyncio.Event()
                calls = []
                async def delayed(function, *args, **kwargs):
                    calls.append(function)
                    entered.set()
                    await release.wait()
                    return observed
                client = Client(nice_page('/source-lifecycle'), request=None)
                try:
                    with client, patch.object(add_page.run, 'io_bound', side_effect=delayed), \
                            patch.object(add_page.AddTaskDialog, '_submit', new=AsyncMock()) as submit:
                        dialog = add_page.AddTaskDialog()
                        dialog.path = str(root)
                        work = asyncio.create_task(dialog._next())
                        await entered.wait()
                        if action == 'close':
                            dialog.close()
                        elif action == 'change':
                            dialog._path_changed(SimpleNamespace(value=str(root / 'video.mkv')))
                        elif action == 'delete':
                            dialog.delete()
                        elif action == 'dismiss':
                            dialog.open()
                            dialog.set_value(False)
                        else:
                            await dialog._next()
                        release.set()
                        await work
                        self.assertEqual(1, len(calls))
                        self.assertEqual(1, dialog.step)
                        self.assertEqual(1 if action == 'double' else 0, submit.await_count)
                        if not dialog.is_deleted:
                            dialog.delete()
                finally:
                    client.delete()

    async def test_submit_rechecks_input_scope_and_cancelled_prepare_never_enqueues(self):
        from src.task_queue import TaskQueueManager
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'input'
            source.mkdir()
            video = source / 'Show.mkv'
            video.write_bytes(b'owned')
            # Multiple input groups deliberately retain the confirmation page.
            other = root / 'other'
            other.mkdir()
            (other / 'Second.mkv').write_bytes(b'owned second source')
            manager = TaskQueueManager(task_root=root / 'tasks', plan_root=root / 'plans')
            client = Client(nice_page('/source-submit'), request=None)
            try:
                with client, patch.object(add_page, 'task_queue', manager), patch.object(manager, 'enqueue_batch', new=AsyncMock(wraps=manager.enqueue_batch)) as enqueue:
                    dialog = add_page.AddTaskDialog()
                    dialog.path = str(source)
                    dialog.input_paths = (str(source), str(other))
                    await dialog._next()
                    dialog.path = str(video)  # Source model changed without an input event.
                    dialog.input_paths = (str(video), str(other))
                    await dialog._submit()
                    enqueue.assert_not_awaited()
                    dialog.path = str(source)
                    dialog.input_paths = (str(source), str(other))
                    await dialog._next()
                    entered, release = asyncio.Event(), asyncio.Event()
                    async def delayed(function, *args, **kwargs):
                        result = function(*args, **kwargs)
                        entered.set()
                        await release.wait()
                        return result
                    with patch.object(add_page.run, 'io_bound', side_effect=delayed):
                        work = asyncio.create_task(dialog._submit())
                        await entered.wait()
                        await dialog._submit()
                        dialog.close()
                        release.set()
                        await work
                    enqueue.assert_not_awaited()
                    self.assertFalse(list(manager.task_root.glob('*.json')))
                    dialog.delete()
            finally:
                client.delete()
