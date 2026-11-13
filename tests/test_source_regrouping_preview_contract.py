"""N-way source previews from actual disposable queue workspaces, not Plans injected by tests."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest

from src.rename.plan_store import PlanStore
from tests import test_partition_content_contract as previous
from tests.test_content_choice_contract import change


def five_seasons(root, source):
    payload = next(source.glob('*S01E01*.mkv')).read_bytes()
    mixed = root / 'incoming' / 'Mixed Works'
    for season in range(1, 6):
        folder = mixed / f'Season {season}'
        folder.mkdir(parents=True)
        video = folder / f'Ordinary Show S{season:02d}E01 [1080p].mkv'
        video.write_bytes(payload)
        video.with_suffix('.en.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nText\n', encoding='utf-8')
        if season == 1:
            video.with_suffix('.mka').write_bytes(b'owned synthetic audio')
    (mixed / 'README.txt').write_text('unassigned attachment', encoding='utf-8')
    (mixed / 'Example.ttf').write_bytes(b'owned synthetic font; preview must not parse it')
    return mixed


class SourceRegroupingPreviewContracts(unittest.IsolatedAsyncioTestCase):
    flow = previous.PartitionContentContracts.flow
    current = previous.PartitionContentContracts.current
    video = previous.PartitionContentContracts.video
    link_subtitle = previous.PartitionContentContracts.link_subtitle

    def preview(self, flow, plan, groups=None):
        command = getattr(flow.manager, 'preview_source_regrouping', None)
        self.assertTrue(callable(command), '真实任务入口缺少一次完整N组预览，不能循环旧二分接口')
        return command(task_id=flow.task_id, expected_plan_fingerprint=PlanStore.fingerprint(plan), groups=groups)

    async def test_actual_mixed_card_previews_five_seasons_and_all_excluded_companions(self):
        async with self.flow(automatic=False, configure_source=five_seasons) as flow:
            plan, _ = self.current(flow)
            plan = self.link_subtitle(flow, plan)
            first = self.video(plan).source_path
            plan = change(flow.service, plan, [first.with_suffix('.mka'), first.with_suffix('.en.srt')])
            third = next(e.source_path for e in plan.entries if e.media_kind == 'video' and 'S03E01' in e.source_path.name)
            plan = change(flow.service, plan, [third])
            before = previous.records(flow.root)
            state = flow.service.decision_state_store.load(flow.task_id)
            old = state.content_selection.to_dict()
            result = self.preview(flow, plan).to_dict()
            video_groups = [g for g in result['groups'] if g['video_count']]
            self.assertEqual(5, len(video_groups))
            self.assertEqual({f'Season {i}' for i in range(1, 6)}, {g['label'] for g in video_groups})
            self.assertEqual(1, len([g for g in result['groups'] if not g['video_count']]))
            all_paths = [p for g in result['groups'] for p in g['sources']]
            self.assertEqual(len(all_paths), len(set(all_paths)))
            self.assertEqual({r['source_path'] for r in old['materials']}, set(all_paths))
            self.assertIn(str(flow.source / 'Example.ttf'), all_paths)
            self.assertEqual(set(old['excluded_sources']), {p for g in result['groups'] for p in g['excluded_sources']})
            group = next(g for g in video_groups if str(first) in g['sources'])
            self.assertTrue({str(first.with_suffix('.en.srt')), str(first.with_suffix('.mka'))} <= set(group['sources']))
            self.assertEqual(before, previous.records(flow.root))
            self.assertEqual(PlanStore.fingerprint(plan), result['plan_fingerprint'])
            self.assertEqual(flow.task_id, result['origin_task_id'])

    async def test_manual_three_groups_and_bad_complete_allocations(self):
        async with self.flow(automatic=False, configure_source=five_seasons) as flow:
            plan, _ = self.current(flow)
            initial = self.preview(flow, plan).to_dict()['groups']
            seasons = [g['sources'] for g in initial if g['video_count']]
            rest = [p for g in initial if not g['video_count'] for p in g['sources']]
            groups = [seasons[0] + seasons[1], seasons[2] + seasons[3], seasons[4] + rest]
            before = previous.records(flow.root)
            self.assertEqual(3, len(self.preview(flow, plan, groups).to_dict()['groups']))
            malformed = [groups[:-1], [*groups, []], [*groups, groups[0]], 'not groups',
                         [[*groups[0], str(flow.root / 'foreign.mkv')], *groups[1:]]]
            broken = [list(g) for g in groups]
            companion = next(p for p in broken[0] if p.endswith('.mka'))
            broken[0].remove(companion)
            broken[2].append(companion)
            malformed.append(broken)
            for bad in malformed:
                with self.subTest(case=malformed.index(bad)), self.assertRaises((ValueError, RuntimeError)):
                    self.preview(flow, plan, bad)
            self.assertEqual(before, previous.records(flow.root))

    async def test_preview_never_starts_worker_reclassifies_or_opens_source_and_detects_disconnect(self):
        from tests.test_partition_subtitle_transition_contract import no_media_io
        async with self.flow(automatic=False, configure_source=five_seasons) as flow:
            plan, _ = self.current(flow)
            expected = self.preview(flow, plan)
            await flow.manager.stop()
            before = previous.records(flow.root)
            scandir = os.scandir
            def only_business_directory(path):
                if Path(path).is_relative_to(flow.source):
                    raise AssertionError('preview scanned media')
                return scandir(path)
            with no_media_io(flow.source), patch('os.scandir', side_effect=only_business_directory), \
                    patch.object(flow.manager, 'start', side_effect=AssertionError('worker started')), \
                    patch('src.rename.default_content.observe_watchable_sources', side_effect=AssertionError('reclassified')), \
                    patch('src.rename.input_directory_discovery.discover_sources', side_effect=AssertionError('rescanned')), \
                    patch.object(flow.manager.source_partition_store, 'prepare', side_effect=AssertionError('old writer')):
                self.assertEqual(expected, self.preview(flow, plan))
            from src.rename import source_regrouping
            altered = expected.to_dict()
            altered['groups'] = altered['groups'][:-1]
            for bad in (None, object(), replace(expected, payload_json=json.dumps(altered))):
                with self.subTest(bad=type(bad).__name__), \
                        patch.object(source_regrouping, 'project_preview', return_value=bad), self.assertRaises((ValueError, RuntimeError)):
                    self.preview(flow, plan)
            self.assertEqual(before, previous.records(flow.root))

    async def test_stale_plan_task_scope_and_during_preview_change_rejected(self):
        async with self.flow(automatic=False, configure_source=five_seasons) as flow:
            plan, _ = self.current(flow)
            self.preview(flow, plan)
            before = previous.records(flow.root)
            with self.assertRaises((ValueError, RuntimeError)):
                flow.manager.preview_source_regrouping(task_id=flow.task_id, expected_plan_fingerprint='0' * 64)
            real_read = flow.manager._read
            stale = {**real_read(flow.task_id), 'plan_fingerprint': '0' * 64}
            with patch.object(flow.manager, '_read', return_value=stale), self.assertRaises((ValueError, RuntimeError)):
                self.preview(flow, plan)
            from src.rename import source_regrouping
            original = source_regrouping.project_preview
            def drift(*args, **kwargs):
                value = original(*args, **kwargs)
                flow.manager._update(flow.task_id, error='owned test concurrent revision')
                return value
            with patch.object(source_regrouping, 'project_preview', side_effect=drift), self.assertRaises((ValueError, RuntimeError)):
                self.preview(flow, plan)
            after = previous.records(flow.root)
            changed = {key for key in before if before[key] != after[key]}
            self.assertEqual({f'tasks/{flow.task_id}.json'}, {p.replace('\\', '/') for p in changed})

    async def test_persisted_decision_batch_owner_and_foreign_conflict_windows(self):
        from src.rename import source_regrouping
        async with self.flow(automatic=False, configure_source=five_seasons) as flow:
            plan, state = self.current(flow)
            self.preview(flow, plan)
            before = previous.records(flow.root)
            original = source_regrouping.project_preview
            owned = flow.manager.projection_commit_service.ownership_store
            owner = owned.load_optional(flow.task_id)
            scope = flow.manager.batch_scope_store.load(flow.manager._read(flow.task_id)['batch_id'])
            cases = ((flow.manager.decision_state_store, 'load_readonly', replace(state, revision=state.revision + 1)),
                     (flow.manager.batch_scope_store, 'require_active_scope', replace(scope, revision=scope.revision + 1)),
                     (owned, 'load_optional', replace(owner, revision=owner.revision + 1)),
                     (owned, 'observe_conflicts', (replace(owner, task_id='other-observed-task', state='retired'),)))
            for target, method, changed in cases:
                with self.subTest(method=method):
                    with patch.object(source_regrouping, 'project_preview', wraps=original) as projected:
                        # Return a changed persisted observation only after the actual projection.
                        real = getattr(target, method)
                        def observe(*args, **kwargs):
                            return changed if projected.call_count else real(*args, **kwargs)
                        with patch.object(target, method, side_effect=observe), self.assertRaises((ValueError, RuntimeError)):
                            self.preview(flow, plan)
            for target, method in ((flow.manager.batch_scope_store, 'require_active_scope'), (owned, 'observe_conflicts')):
                with self.subTest(disconnect=method), patch.object(target, method, return_value=None), \
                        self.assertRaises((ValueError, RuntimeError)):
                    self.preview(flow, plan)
            self.assertEqual(before, previous.records(flow.root))

    async def test_shared_audio_cannot_be_silently_given_to_one_season(self):
        from src.rename.content_selection import ContentSelection
        from src.rename.source_regrouping import group_sources
        async with self.flow(automatic=False, configure_source=five_seasons) as flow:
            plan, state = self.current(flow)
            initial = self.preview(flow, plan).to_dict()['groups']
            groups = [g['sources'] for g in initial]
            raw = state.content_selection.to_dict()
            audio = next(r for r in raw['associations']['relations'] if r['kind'] == 'audio')
            videos = [r['source_path'] for r in raw['materials'] if r['media_kind'] == 'video']
            audio.update(state='shared', source='manual', owners=videos[:2])
            selection = ContentSelection.from_mapping(raw)
            with self.assertRaisesRegex(ValueError, '共享'):
                group_sources(selection, plan.source_root, groups)
            # Component control: explicit merging all linked owners preserves the bundle.
            self.assertEqual(1, len(group_sources(selection, plan.source_root, [[p for g in groups for p in g]])))

    def test_complete_movie_segments_and_hidden_version_remain_indivisible(self):
        from tests.test_content_choice_contract import migrated
        from src.rename.source_regrouping import group_sources
        with tempfile.TemporaryDirectory(prefix='bar-regroup-movie-') as raw:
            service, plan, videos = migrated(Path(raw), segments=True)
            state = service.decision_state_store.load(plan.task_id)
            selection = state.content_selection
            groups = selection.to_dict()['version_groups']
            self.assertGreaterEqual(len(groups), 2)
            members = [g['sources'] for g in groups]
            self.assertGreater(len(members[0]), 1)
            all_paths = {r['source_path'] for r in selection.to_dict()['materials']}
            # Keep all companion material with the full version group for this component control.
            from src.rename.content_associations import partition_source_closure
            complete = {str(p) for p in partition_source_closure(selection.associations, members[0])}
            rows = [sorted(complete), sorted(all_paths - complete)]
            self.assertEqual(2, len(group_sources(selection, plan.source_root, rows)))
            first = {str(p) for p in partition_source_closure(selection.associations, [members[0][0]])}
            split = [sorted(first), sorted(all_paths - first)]
            with self.assertRaisesRegex(ValueError, '电影版本'):
                group_sources(selection, plan.source_root, split)
            self.assertTrue(set(members[1]) <= all_paths)
