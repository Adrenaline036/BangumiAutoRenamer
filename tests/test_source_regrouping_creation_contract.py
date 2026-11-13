"""Personal-use regrouping: ordinary new cards, no inherited transaction framework."""
from tests.source_collection_fixture import collected_identity
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from src.rename.content_policy import ContentPolicy
from src.rename.task_batch_scope import TaskSourceScope
from src.rename.recognition_queue_admission import capture_workspace_open
from tests import test_source_regrouping_preview_contract as preview
from tests import test_partition_content_contract as previous
from tests.source_collection_fixture import admit_task


class SourceRegroupingCreationContracts(unittest.IsolatedAsyncioTestCase):
    flow = previous.PartitionContentContracts.flow
    current = previous.PartitionContentContracts.current
    video = previous.PartitionContentContracts.video

    def create(self, flow, plan):
        command = getattr(flow.manager, 'create_regrouped_tasks', None)
        self.assertTrue(callable(command), '确认分组后必须创建全部普通新卡，不仅返回预览')
        proposed = flow.manager.preview_source_regrouping(task_id=flow.task_id,
            expected_plan_fingerprint=PlanStore.fingerprint(plan))
        return command(proposed)

    async def test_video_cards_replace_mixed_old_card_unapproved_and_continue_normally(self):
        def worker(value):
            self.worker = value
        async with self.flow(automatic=False, configure_source=preview.five_seasons, configure_worker=worker) as flow:
            plan, _ = self.current(flow)
            plan = preview.SourceRegroupingPreviewContracts.link_subtitle(self, flow, plan)
            original_sources = {p: p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
            unlink = Path.unlink
            def require_ready(path, *args, **kwargs):
                if path == flow.manager.task_root / f'{flow.task_id}.json':
                    cards = [p for p in flow.manager.task_root.glob('*.json') if p.stem != flow.task_id]
                    self.assertEqual(5, len(cards))
                    for card in cards:
                        task = flow.manager._read(card.stem)
                        flow.manager.batch_scope_store.require_active_scope(
                            TaskSourceScope.from_mapping(task['task_source_scope']), batch_id=task['batch_id'])
                return unlink(path, *args, **kwargs)
            with patch.object(Path, 'unlink', require_ready), patch.object(
                    flow.manager, 'partition_task_sources', side_effect=AssertionError('legacy binary writer used')):
                ids = self.create(flow, plan)
            self.assertEqual(5, len(ids))
            self.assertEqual(5, len(set(ids)))
            self.assertNotIn(flow.task_id, ids)
            self.assertFalse((flow.manager.task_root / f'{flow.task_id}.json').exists())
            self.assertFalse(flow.store.path_for(flow.task_id).exists())
            self.assertFalse((flow.manager.review_root / f'{flow.task_id}.json').exists())
            assigned, season_tasks = [], []
            for task_id in ids:
                data = flow.manager._read(task_id)
                self.assertEqual('need_confirm', data['workflow_status'])
                self.assertTrue(data['source_group_pending'])
                self.assertIsNone(data.get('is_anime'))
                self.assertIsNone(data.get('is_movie'))
                self.assertFalse(flow.store.path_for(task_id).exists())
                self.assertFalse((flow.manager.review_root / f'{task_id}.json').exists())
                scope = TaskSourceScope.from_mapping(data['task_source_scope'])
                self.assertEqual(ContentPolicy.SAFE_WATCHABLE, scope.content_policy)
                flow.manager.batch_scope_store.require_active_scope(scope, batch_id=data['batch_id'])
                paths = {Path(r.source_path) for r in scope.source_identities}
                assigned.extend(paths)
                if any(p.suffix == '.mkv' for p in paths):
                    season_tasks.append(task_id)
                first = next(flow.source.glob('Season 1/*.mkv'))
                if first in paths:
                    self.assertTrue({first.with_suffix('.en.srt'), first.with_suffix('.mka')} <= paths)
            self.assertEqual({p for p in original_sources if p.suffix not in {'.txt', '.ttf'}}, set(assigned))
            self.assertEqual(len(set(assigned)), len(assigned))
            self.assertFalse(flow.manager._pending)
            self.assertTrue(flow.manager._queue.empty())
            # New card's existing source-confirmation entry, not an invented Plan/Decision.
            self.worker.search.get_tv_info = lambda *_: self.worker.search.get_tv_info_by_id('4242')
            selected = season_tasks[0]
            opened = capture_workspace_open(flow.manager, selected, flow.manager._read(selected))
            await flow.manager.confirm_source_group(selected, opened_evidence=opened)
            await asyncio.wait_for(flow.manager._queue.join(), 12)
            data = flow.manager._read(selected)
            self.assertFalse(data.get('source_group_pending'))
            self.assertEqual('need_confirm', data['workflow_status'], data.get('error'))
            self.assertTrue(flow.store.path_for(selected).exists(), data.get('error'))
            continued = flow.service.decision_state_store.load(selected)
            self.assertEqual(ContentPolicy.SAFE_WATCHABLE, continued.content_policy)
            self.assertEqual(data['task_source_scope'], continued.initial_source_basis.to_dict()['task_source_scope'])
            self.assertEqual({r.source_path for r in TaskSourceScope.from_mapping(data['task_source_scope']).source_identities},
                {r['source_path'] for r in continued.initial_source_basis.to_dict()['sources']})
            self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))
            self.assertEqual(original_sources, {p: p.read_bytes() for p in original_sources})
            with self.assertRaises((ValueError, RuntimeError, OSError)):
                self.create(flow, plan)

    async def test_exact_files_keep_cross_directory_manual_subtitle_without_misc_card(self):
        from src.rename import content_choice
        from src.rename.plan_review import entry_id
        from src.rename.rename_plan import SubtitleOverrideScope
        from src.rename.task_batch_scope import BatchTaskSourceRequest
        from src.task_queue import TaskJob

        def configure(root, source):
            previous.package(root, source)
            for path in tuple(source.iterdir()):
                if path.name == 'README.txt':
                    continue
                folder = source / ('Subtitle Files' if path.suffix == '.srt' else
                                   'Quarter A' if 'S01E01' in path.name else 'Quarter B')
                folder.mkdir(exist_ok=True)
                destination = folder / path.name
                assert path.is_relative_to(root) and destination.is_relative_to(root)
                path.rename(destination)  # Only owned temporary fixture, before admission.
            return source

        async def admit(manager, source, task_id):
            paths = tuple(sorted((p for p in source.rglob('*') if p.is_file()), key=str))
            request = BatchTaskSourceRequest(task_id, tuple(str(p) for p in paths), 'exact_file',
                tuple(collected_identity(p) for p in paths))
            await manager.enqueue_batch(batch_id='exact-regroup-admission', task_requests=(request,),
                jobs=(TaskJob(task_id, source, is_anime=False, is_movie=False,
                    custom_name='Ordinary Show', use_ai=False, source='ui'),))

        def worker(value):
            self.worker = value

        async with self.flow(automatic=False, configure_source=configure, admit=admit,
                             configure_worker=worker) as flow:
            plan, _ = self.current(flow)
            first = self.video(plan).source_path
            subtitle = next(e for e in content_choice.subtitle_edit_view(plan).entries
                            if e.source_path.suffix == '.srt')
            self.assertNotEqual(first.parent, subtitle.source_path.parent)
            plan = flow.service.apply_subtitle_override_rule(flow.task_id, PlanStore.fingerprint(plan),
                entry_id(subtitle), scope=SubtitleOverrideScope.FILE, associated_video_source_path=first,
                primary_language='en', script=None, reason='跨目录字幕随人工选择的正片分组')
            original = {p: p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
            self.assertIn(flow.source / 'README.txt', original)
            with patch.object(flow.manager, 'partition_task_sources', side_effect=AssertionError('retired writer')):
                ids = self.create(flow, plan)
            assigned, selected = [], None
            for task_id in ids:
                data = flow.manager._read(task_id)
                self.assertEqual('need_confirm', data['workflow_status'])
                self.assertTrue(data['source_group_pending'])
                self.assertFalse(flow.store.path_for(task_id).exists())
                scope = TaskSourceScope.from_mapping(data['task_source_scope'])
                flow.manager.batch_scope_store.require_active_scope(scope, batch_id=data['batch_id'])
                paths = {Path(i.source_path) for i in scope.source_identities}
                assigned.extend(paths)
                if first in paths:
                    selected = task_id
                    self.assertIn(subtitle.source_path, paths)
            self.assertEqual(2, len(ids))
            self.assertEqual(set(original) - {flow.source / 'README.txt'}, set(assigned))
            self.assertEqual(len(assigned), len(set(assigned)))
            self.assertIsNotNone(selected)
            self.assertFalse(flow.manager._read(flow.task_id))
            self.worker.search.get_tv_info = lambda *_: self.worker.search.get_tv_info_by_id('4242')
            opened = capture_workspace_open(flow.manager, selected, flow.manager._read(selected))
            await flow.manager.confirm_source_group(selected, opened_evidence=opened)
            await asyncio.wait_for(flow.manager._queue.join(), 12)
            data = flow.manager._read(selected)
            self.assertTrue(flow.store.path_for(selected).exists(), data.get('error'))
            basis = flow.service.decision_state_store.load(selected).initial_source_basis.to_dict()
            self.assertEqual(str(flow.source), basis['source_root'])
            self.assertEqual(data['task_source_scope'], basis['task_source_scope'])
            self.assertEqual({i['source_path'] for i in data['task_source_scope']['source_identities']},
                             {i['source_path'] for i in basis['sources']})
            self.assertEqual(original, {p: p.read_bytes() for p in original})
            self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))

    async def test_disconnected_write_cannot_remove_old_and_delete_failure_keeps_ready_cards(self):
        async with self.flow(automatic=False, configure_source=preview.five_seasons) as flow:
            plan, _ = self.current(flow)
            with patch.object(flow.manager, '_write', return_value=None):
                with self.assertRaisesRegex(RuntimeError, '未完整保存'):
                    self.create(flow, plan)
            self.assertTrue(flow.manager._read(flow.task_id))
            self.assertEqual(1, len(list(flow.manager.task_root.glob('*.json'))))
        async with self.flow(automatic=False, configure_source=preview.five_seasons) as flow:
            plan, _ = self.current(flow)
            unlink = Path.unlink
            def fail_old(path, *args, **kwargs):
                if path == flow.manager.task_root / f'{flow.task_id}.json':
                    raise OSError('owned old card delete failure')
                return unlink(path, *args, **kwargs)
            with patch.object(Path, 'unlink', fail_old):
                with self.assertRaisesRegex(RuntimeError, '旧卡移除失败'):
                    self.create(flow, plan)
            self.assertEqual(6, len(list(flow.manager.task_root.glob('*.json'))))
            await flow.manager.abandon_and_remove(flow.task_id, record_root=flow.manager.execution_service.record_root,
                plan_root=flow.store.root, review_root=flow.manager.review_root)
            self.assertEqual(5, len(list(flow.manager.task_root.glob('*.json'))))

    async def test_third_card_write_failure_keeps_original_and_allows_safe_abandon_then_recreate(self):
        async with self.flow(automatic=False, configure_source=preview.five_seasons) as flow:
            plan, _ = self.current(flow)
            original = flow.manager._read(flow.task_id)
            original_plan = flow.store.path_for(flow.task_id).read_bytes()
            write = flow.manager._write
            writes = []
            def third(task_id, data):
                if task_id != flow.task_id:
                    writes.append(task_id)
                    if len(writes) == 3:
                        raise OSError('owned third card write failed')
                return write(task_id, data)
            with patch.object(flow.manager, '_write', side_effect=third), self.assertRaises((ValueError, RuntimeError, OSError)):
                self.create(flow, plan)
            self.assertEqual(3, len(writes))
            self.assertEqual(original, flow.manager._read(flow.task_id))
            self.assertEqual(original_plan, flow.store.path_for(flow.task_id).read_bytes())
            self.assertEqual({flow.task_id}, {p.stem for p in flow.manager.task_root.glob('*.json')})
            self.assertFalse(flow.manager._pending)
            await flow.manager.abandon_and_remove(flow.task_id,
                record_root=flow.manager.execution_service.record_root,
                plan_root=flow.store.root, review_root=flow.manager.review_root)
            self.assertFalse(flow.manager._read(flow.task_id))
            await admit_task(flow.manager, flow.source, task_id='recreated-after-error',
                is_anime=False, is_movie=False)
            await asyncio.wait_for(flow.manager._queue.join(), 12)
            self.assertTrue(flow.store.path_for('recreated-after-error').exists())

    async def test_prepare_failure_preserves_every_record_and_shared_batch_peer_survives_exit(self):
        async def admit(manager, source, task_id):
            peer = source.parent / 'Peer'
            peer.mkdir()
            (peer / 'Ordinary Show S01E01.mkv').write_bytes(next(source.rglob('*.mkv')).read_bytes())
            batch = manager.prepare_input_batch((source, peer), is_anime=False, is_movie=False,
                 source='owned-two-task-fixture', preview_only=False)
            batch['task_requests'] = (replace(batch['task_requests'][0], task_id=task_id), batch['task_requests'][1])
            batch['jobs'] = (replace(batch['jobs'][0], task_id=task_id), batch['jobs'][1])
            self.peer_id = batch['jobs'][1].task_id
            await manager.enqueue_batch(**batch)
        async with self.flow(automatic=False, configure_source=preview.five_seasons, admit=admit) as flow:
            plan, _ = self.current(flow)
            before = previous.records(flow.root)
            with patch('src.rename.input_directory_discovery.validate_group_sources', side_effect=ValueError('owned prepare failed')):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.create(flow, plan)
            self.assertEqual(before, previous.records(flow.root))
            peer_before = flow.manager._read(self.peer_id)
            old = flow.manager._read(flow.task_id)
            with patch.object(flow.manager, '_write', side_effect=OSError('owned write failed')):
                with self.assertRaises((ValueError, RuntimeError, OSError)):
                    self.create(flow, plan)
            retired = flow.manager.batch_scope_store.load(old['batch_id'])
            self.assertTrue(all(r.retired for r in retired.reservations if r.owner_task_id == flow.task_id))
            self.assertFalse(any(s.task_id == flow.task_id for s in retired.task_source_scopes))
            # Retried retirement must be exact and must not retire the peer.
            with self.assertRaises((ValueError, RuntimeError)):
                flow.manager.batch_scope_store.retire_task(old['batch_id'], task_id=flow.task_id,
                    expected_lineage_id='wrong-lineage')
            await flow.manager.abandon_and_remove(flow.task_id, record_root=flow.manager.execution_service.record_root,
                plan_root=flow.store.root, review_root=flow.manager.review_root)
            self.assertEqual(peer_before, flow.manager._read(self.peer_id))
            flow.manager.batch_scope_store.require_active_scope(
                TaskSourceScope.from_mapping(peer_before['task_source_scope']), batch_id=peer_before['batch_id'])

    async def test_changed_scope_rejected_before_first_card_and_invalid_groups_do_not_write(self):
        async with self.flow(automatic=False, configure_source=preview.five_seasons) as flow:
            plan, _ = self.current(flow)
            before = previous.records(flow.root)
            command = getattr(flow.manager, 'create_regrouped_tasks', None)
            self.assertTrue(callable(command), '缺少确认建卡入口')
            proposed = flow.manager.preview_source_regrouping(task_id=flow.task_id,
                expected_plan_fingerprint=PlanStore.fingerprint(plan))
            value = proposed.to_dict()
            value['groups'][1]['sources'].append(value['groups'][0]['sources'][0])
            with self.assertRaises((ValueError, RuntimeError)):
                command(replace(proposed, payload_json=json.dumps(value)))
            self.assertEqual(before, previous.records(flow.root))
            scope=flow.manager._read(flow.task_id)['task_source_scope']
            scope['source_identities'][0]['size'] += 1
            flow.manager._update(flow.task_id,task_source_scope=scope)
            before=previous.records(flow.root)
            with self.assertRaises((ValueError, RuntimeError)):
                command(proposed)
            self.assertEqual(before, previous.records(flow.root))
