"""Selected-work incident through the existing preview and replacement writer."""
from dataclasses import replace
import asyncio
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.content_associations import encode
from tests import test_source_regrouping_view_contract as view
from tests.test_partition_content_contract import records
from tests.test_content_choice_contract import change


class IndependentWorkSelectionContracts(unittest.IsolatedAsyncioTestCase):
    workspace = view.SourceRegroupingViewContracts.workspace
    flow = view.SourceRegroupingViewContracts.flow
    current = view.SourceRegroupingViewContracts.current
    video = view.SourceRegroupingViewContracts.video
    link_subtitle = view.SourceRegroupingViewContracts.link_subtitle
    click = view.SourceRegroupingViewContracts.click

    def preview(self, flow, plan, identifiers):
        return flow.manager.preview_source_regrouping(task_id=flow.task_id,
            expected_plan_fingerprint=PlanStore.fingerprint(plan), selected_entry_ids=identifiers)

    async def test_selected_video_companions_and_remaining_do_not_restore_exclusions(self):
        async with self.workspace(configure_worker=lambda worker: setattr(self, 'worker', worker)) as (flow, work):
            first = self.video(work.plan)
            extra = next(e for e in work.plan.entries if e.media_kind == 'video' and 'S03E01' in e.source_path.name)
            plan = change(flow.service, work.plan, [extra.source_path])
            before = records(flow.root)
            preview = self.preview(flow, plan, [entry_id(first)])
            data = preview.to_dict()
            self.assertEqual(2, len(data['groups']))
            self.assertEqual({str(first.source_path), str(first.source_path.with_suffix('.en.srt')),
                              str(first.source_path.with_suffix('.mka'))}, set(data['groups'][0]['sources']))
            self.assertIn(str(extra.source_path), data['omitted_sources'])
            self.assertTrue(all(g['video_count'] > 0 for g in data['groups']))
            self.assertEqual(before, records(flow.root))
            with patch.object(flow.manager, 'partition_task_sources', side_effect=AssertionError('old writer')):
                ids = flow.manager.create_regrouped_tasks(preview)
            self.assertEqual(2, len(ids))
            self.assertFalse(flow.manager._read(flow.task_id))
            admitted = {r['source_path'] for i in ids
                        for r in flow.manager._read(i)['task_source_scope']['source_identities']}
            self.assertFalse(admitted.intersection(data['omitted_sources']))
            self.assertEqual({p for g in data['groups'] for p in g['sources']}, admitted)
            self.assertFalse(flow.manager._pending)
            self.assertIsNotNone(flow.service.decision_state_store.load(flow.task_id))
            from src.rename.recognition_queue_admission import capture_workspace_open
            self.worker.search.get_tv_info = lambda *_: self.worker.search.get_tv_info_by_id('4242')
            selected = ids[0]
            opened = capture_workspace_open(flow.manager, selected, flow.manager._read(selected))
            await flow.manager.confirm_source_group(selected, opened_evidence=opened)
            await asyncio.wait_for(flow.manager._queue.join(), 12)
            continued = flow.store.load(selected)
            self.assertEqual(set(data['groups'][0]['sources']), {str(p) for p in continued.expected_sources})
            self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))

    async def test_both_buttons_use_selection_and_show_only_chosen_group(self):
        async with self.workspace() as (flow, work):
            selected = entry_id(self.video(work.plan))
            work.selected_ids = {selected}
            with work.client:
                for callback in (lambda: work._partition_selected_as_cross_work(),
                                 lambda: work._partition_exceptional_as_cross_work([selected])):
                    await callback()
                    popup = work.regrouping_dialog
                    self.assertTrue(popup.value)
                    self.assertEqual(1, len(popup.tables))
                    self.assertEqual(1, popup.groups[0]['video_count'])
                    self.assertEqual({}, popup.destinations)
                    self.assertFalse(any('CDs' in row['file'] for row in popup.tables[0].rows))
                    popup.close()

    async def test_selected_dialog_check_and_confirm_keep_the_same_scope(self):
        async with self.workspace() as (flow, work):
            with work.client:
                await work._partition_exceptional_as_cross_work([entry_id(self.video(work.plan))])
                popup = work.regrouping_dialog
                expected = popup.preview.to_dict()
                await popup.check()
                self.assertFalse(popup.error.text)
                await self.click(popup, '确认创建新卡')
                self.assertFalse(popup.error.text)
                self.assertFalse(work.value)
                self.assertEqual(2, len(popup.created_ids))
                for task_id, group in zip(popup.created_ids, expected['groups']):
                    task = flow.manager._read(task_id)
                    self.assertEqual(set(group['sources']), {r['source_path']
                        for r in task['task_source_scope']['source_identities']})
                    self.assertFalse(task['source_group_pending'])
                await asyncio.wait_for(flow.manager._queue.join(), 30)
                self.assertFalse(flow.manager._pending)
                self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))

    async def test_slow_preview_does_not_block_ui_or_reopen_closed_workspace(self):
        async with self.workspace() as (flow, work):
            original = flow.manager.preview_source_regrouping
            main_thread = threading.get_ident()
            def slow(**kwargs):
                self.assertNotEqual(main_thread, threading.get_ident())
                time.sleep(.1)
                return original(**kwargs)
            before = records(flow.root)
            with work.client, patch.object(flow.manager, 'preview_source_regrouping', side_effect=slow):
                opening = asyncio.create_task(work._partition_exceptional_as_cross_work([entry_id(self.video(work.plan))]))
                await asyncio.sleep(.02)
                self.assertFalse(opening.done())
                work.close()
                await opening
            self.assertIsNone(getattr(work, 'regrouping_dialog', None))
            self.assertEqual(before, records(flow.root))

    async def test_confirm_recheck_close_prevents_write_and_duplicate_does_not_repeat_transaction(self):
        async with self.workspace() as (flow, work):
            with work.client:
                await work._partition_exceptional_as_cross_work([entry_id(self.video(work.plan))])
                popup = work.regrouping_dialog
                original = popup._query
                def slow_query(groups):
                    time.sleep(.1)
                    return original(groups)
                before = records(flow.root)
                with patch.object(popup, '_query', side_effect=slow_query), \
                        patch.object(flow.manager, 'create_regrouped_tasks', wraps=flow.manager.create_regrouped_tasks) as create:
                    waiting = asyncio.create_task(popup.confirm())
                    await asyncio.sleep(.02)
                    popup.close()
                    await waiting
                    create.assert_not_called()
                self.assertEqual(before, records(flow.root))
                await work._partition_exceptional_as_cross_work([entry_id(self.video(work.plan))])
                popup = work.regrouping_dialog
                original_create = flow.manager.create_regrouped_tasks
                main_thread = threading.get_ident()
                def slow_create(preview):
                    self.assertNotEqual(main_thread, threading.get_ident())
                    time.sleep(.1)
                    return original_create(preview)
                with patch.object(flow.manager, 'create_regrouped_tasks', side_effect=slow_create) as create:
                    await asyncio.gather(popup.confirm(), popup.confirm())
                    create.assert_called_once()
                self.assertEqual(2, len(popup.created_ids))

    async def test_invalid_empty_excluded_and_forged_scope_do_not_write(self):
        async with self.workspace() as (flow, work):
            first = self.video(work.plan)
            before = records(flow.root)
            subtitle = next(e for e in work.plan.entries if e.media_kind == 'subtitle')
            for bad in ([], ['unknown'], [entry_id(first)] * 2, 'not a list', [entry_id(subtitle)]):
                with self.subTest(bad=bad), self.assertRaises((ValueError, RuntimeError)):
                    self.preview(flow, work.plan, bad)
            good = self.preview(flow, work.plan, [entry_id(first)])
            for field, value in [('omitted_sources', []), ('groups', good.to_dict()['groups'][:1]),
                                 ('selected_entry_ids', [])]:
                forged = good.to_dict()
                forged[field] = value
                with self.subTest(field=field), self.assertRaises((ValueError, RuntimeError)):
                    flow.manager.create_regrouped_tasks(replace(good, payload_json=encode(forged)))
            self.assertEqual(before, records(flow.root))
            excluded = change(flow.service, work.plan, [first.source_path])
            after = records(flow.root)
            with self.assertRaises((ValueError, RuntimeError)):
                self.preview(flow, excluded, [entry_id(first)])
            self.assertEqual(after, records(flow.root))

    def test_movie_groups_and_shared_audio_keep_existing_boundaries(self):
        from tests.test_content_choice_contract import migrated
        from src.rename.source_regrouping import independent_groups, group_sources
        from src.rename.content_selection import ContentSelection
        from src.rename.content_associations import partition_source_closure
        with tempfile.TemporaryDirectory(prefix='bar-independent-movie-') as raw:
            service, plan, videos = migrated(Path(raw), segments=True)
            state = service.decision_state_store.load(plan.task_id)
            member_ids = [entry_id(e) for e in plan.entries if e.source_path in videos[:2]]
            groups, omitted = independent_groups(plan, state, member_ids[:1])
            with self.assertRaisesRegex(ValueError, '电影版本'):
                group_sources(state.content_selection, plan.source_root, groups, omitted=omitted)
            plan = service.revise_movie_versions(plan.task_id, PlanStore.fingerprint(plan),
                groups=[dict(sources=g['sources'], label=g['label'])
                        for g in state.content_selection.to_dict()['version_groups']],
                excluded_sources=[videos[2]])
            state = service.decision_state_store.load(plan.task_id)
            groups, omitted = independent_groups(plan, state, member_ids)
            self.assertIn(str(videos[2]), omitted)
            self.assertFalse(set(p for g in groups for p in g) & omitted)
            group_sources(state.content_selection, plan.source_root, groups, omitted=omitted)
            value = state.content_selection.to_dict()
            relation = next(r for r in value['associations']['relations'] if r['kind'] == 'subtitle')
            relation.update(state='shared', source='manual', owners=[str(p) for p in videos[:2]])
            shared = ContentSelection.from_mapping(value)
            with self.assertRaisesRegex(ValueError, '共享'):
                partition_source_closure(shared.associations, [str(videos[0])])

    async def test_explicitly_excluded_companions_are_not_reintroduced(self):
        async with self.workspace() as (flow, work):
            first = self.video(work.plan)
            excluded = {first.source_path.with_suffix('.en.srt'), first.source_path.with_suffix('.mka')}
            plan = change(flow.service, work.plan, excluded)
            result = self.preview(flow, plan, [entry_id(first)]).to_dict()
            self.assertEqual([str(first.source_path)], result['groups'][0]['sources'])
            self.assertTrue({str(p) for p in excluded} <= set(result['omitted_sources']))

    async def test_multiple_selected_videos_and_no_video_remainder_exit(self):
        async with self.workspace() as (flow, work):
            videos = [e for e in work.plan.entries if e.media_kind == 'video']
            data = self.preview(flow, work.plan, [entry_id(e) for e in videos[:2]]).to_dict()
            self.assertEqual(2, data['groups'][0]['video_count'])
            # An unassigned font remains: never create a card containing only attachments.
            before = records(flow.root)
            with self.assertRaisesRegex(ValueError, '附件.*完整拆卡'):
                self.preview(flow, work.plan, [entry_id(e) for e in videos])
            self.assertEqual(before, records(flow.root))


if __name__ == '__main__':
    unittest.main()
