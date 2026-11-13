"""Pure movie video mapping extraction; no live Provider or source media."""
from contextlib import ExitStack
import asyncio
from dataclasses import FrozenInstanceError, replace
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename.plan_review import PlanReviewService
from src.rename.plan_store import PlanStore
from tests import _media_flow_helpers as fixture
from tests.test_content_choice_contract import records


class MovieVideoProjectionContracts(unittest.TestCase):
    def root(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / 'movie-projection'
        root.mkdir()
        return root

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('src.rename.movie_video_projection'),
                             'missing unique pure movie video mapping kernel')
        from src.rename import movie_video_projection
        return movie_video_projection

    def legacy(self, root):
        async def complete():
            helper=fixture.MediaFlow('runTest')
            source,primary,_=fixture._make_version(root/'incoming','1080p')
            result=await helper._run_provider_flow(root,source,'kernel1')
            completed=await helper._complete_movie(root=root,result=result,primary=primary)
            return source,completed.plan,completed.store,completed.review
        return asyncio.run(complete())

    def captured(self, root):
        module = self.module()
        original = module.project_movie_videos
        calls = []
        def capture(value):
            result = original(value)
            calls.append((value, result))
            return result
        with patch.object(module, 'project_movie_videos', capture):
            _source, completed, _store, _service = self.legacy(root)
        self.assertTrue(calls, 'current completion must consume the shared kernel')
        return module, calls[-1][0], calls[-1][1], completed

    def test_existing_public_movie_completion_baseline(self):
        source, completed, store, _service = self.legacy(self.root())
        self.assertEqual('inventory_smart_completion', completed.metadata['mapping_source'])
        self.assertEqual('movie_content', next(e for e in completed.entries if e.media_kind == 'video').role)
        self.assertEqual(completed, store.load(completed.task_id))
        self.assertTrue(source.exists())

    def test_public_movie_completion_uses_unique_kernel(self):
        module, value, expected, completed = self.captured(self.root())
        self.assertEqual(expected, module.project_movie_videos(value))
        self.assertEqual({e.source_path for e in completed.entries if e.media_kind == 'video'},
                         {row.source for row in expected.mappings} - set(completed.ignored_sources))
        self.assertEqual('movie_content', expected.mappings[0].role)

    def test_frozen_input_is_deterministic_without_files_provider_or_stores(self):
        module, value, expected, _ = self.captured(self.root())
        before = repr(value)
        second = replace(value.videos[0], source_path=value.source_root / 'Part 02.mkv')
        segments = replace(value, videos=(value.videos[0], second), primary_sources=None,
                           confirmed_segments=(second.source_path, value.videos[0].source_path))
        extra = replace(second, source_path=value.source_root / 'Example [CM].mkv')
        mixed = replace(value, videos=(value.videos[0], extra, second),
                        primary_sources=(value.videos[0].source_path,))
        cases = [(item, module.project_movie_videos(item), repr(item)) for item in (segments, mixed)]
        forbidden = AssertionError('pure movie mapping attempted external observation')
        with ExitStack() as stack:
            for name in ('builtins.open', 'pathlib.Path.stat', 'pathlib.Path.exists',
                         'pathlib.Path.is_file', 'pathlib.Path.read_bytes', 'pathlib.Path.read_text',
                         'os.path.lexists', 'requests.sessions.Session.request',
                         'src.rename.plan_store.PlanStore.load',
                         'src.rename.decision_state.DecisionStateStore.load',
                         'src.rename.work_catalog.WorkCatalog.find_exact'):
                stack.enter_context(patch(name, side_effect=forbidden))
            self.assertEqual(expected, module.project_movie_videos(value))
            self.assertEqual(expected, module.project_movie_videos(value))
            for item, result, original in cases:
                self.assertEqual(result, module.project_movie_videos(item))
                self.assertEqual(original, repr(item))
        self.assertEqual(before, repr(value))
        with self.assertRaises(FrozenInstanceError):
            value.work_root = Path('changed')

    def test_disconnected_or_invalid_kernel_cannot_publish(self):
        from tests.test_initial_movie_completion_contract import InitialMovieFullCompletionContracts
        module=self.module()
        for invalid in ('disconnected',None,object()):
            async def verify():
                helper=InitialMovieFullCompletionContracts('runTest')
                helper.setUp()
                self.addCleanup(helper.doCleanups)
                root,service,plan,primary,_=await helper.fixture()
                intent=helper.prepare(service,plan,[dict(sources=[str(primary)],label='1080p')])
                before=records(root)
                def reject(value):
                    if invalid=='disconnected':
                        raise ValueError('movie video kernel disconnected')
                    return invalid
                with patch.object(module,'project_movie_videos',side_effect=reject) as kernel:
                    with self.assertRaises((ValueError,RuntimeError)):
                        service.commit_initial_movie_completion(intent)
                self.assertTrue(kernel.called)
                self.assertEqual(before,records(root))
            asyncio.run(verify())

    def test_tv_completion_never_uses_movie_kernel(self):
        module = self.module()
        from tests.test_err060_tv_inventory_retry import completed_tv
        with patch.object(module, 'project_movie_videos', side_effect=AssertionError('TV reached movie kernel')) as kernel:
            service, plan, _ = completed_tv(self.root())
        kernel.assert_not_called()
        self.assertEqual('initial_tv_completion', plan.metadata['completion_mode'])
        self.assertEqual(plan, service.plan_store.load(plan.task_id))

    def test_real_movie_subtitle_audio_and_auxiliary_consumers_preserved(self):
        module = self.module()
        from tests.test_err059_delivery_incidents import DeliveryIncidents
        root = self.root()
        flow = fixture.MediaFlow._run_provider_flow
        async def seeded(helper, flow_root, source, task_id):
            video = next(source.glob('*.mkv'))
            video.with_suffix('.chs.srt').write_text(
                '1\n00:00:00,000 --> 00:00:01,000\n测试字幕\n', encoding='utf-8')
            video.with_suffix('.mka').write_bytes(b'isolated external audio')
            return await flow(helper, flow_root, source, task_id)
        async def complete():
            with patch.object(fixture.MediaFlow, '_run_provider_flow', seeded):
                helper, initial, primary = await DeliveryIncidents('runTest').movie(root)
            with patch.object(module, 'project_movie_videos', wraps=module.project_movie_videos) as kernel:
                result = await helper._complete_movie(root=root, result=initial, primary=primary)
            # Preview, canonical publication and anti-bypass validation all
            # consume this kernel; its invocation count is not a single writer.
            self.assertGreater(kernel.call_count, 0)
            return result, primary
        result, primary = asyncio.run(complete())
        plan = result.plan
        self.assertEqual(plan, result.review.plan_store.load(plan.task_id))
        video = next(e for e in plan.entries if e.source_path == primary[0])
        paired = [e for e in plan.entries if e.subtitle and e.subtitle.associated_video_source_path == video.source_path]
        self.assertTrue(paired, 'real subtitle contract was not produced')
        self.assertTrue(all(e.subtitle.primary_language and e.target_path.parent == video.target_path.parent for e in paired))
        audio = next(e for e in plan.entries if e.source_path.suffix == '.mka')
        self.assertEqual(video.target_path.with_suffix('.mka'), audio.target_path)
        self.assertEqual(video.presentation_asset.version_descriptor.version_ref, audio.presentation_asset.owner_version_ref)
        self.assertTrue(any(p.suffix=='.zip' for p in plan.ignored_sources))
        self.assertFalse(any(e.role=='subtitle_archive' for e in plan.entries))

    def test_versions_segments_extras_unknown_and_exclusions_keep_mapping_semantics(self):
        module, value, _expected, _ = self.captured(self.root())
        value=replace(value,source_root=value.source_root.parent/'unmarked')
        template = value.videos[0]
        def video(name):
            return replace(template, source_path=value.source_root / name)
        first, second = video('Example [1080p].mkv'), video('Example [2160p].mkv')
        supplied = replace(value, videos=(first, second),
                           primary_sources=(first.source_path, second.source_path))
        versions = module.project_movie_videos(supplied)
        self.assertEqual({'movie_content'}, {row.role for row in versions.mappings})
        self.assertEqual(2, len({row.target for row in versions.mappings}))
        self.assertEqual((), versions.unresolved)
        segment_input = replace(value, videos=(first, second), primary_sources=None,
                                confirmed_segments=(second.source_path, first.source_path))
        segments = module.project_movie_videos(segment_input)
        self.assertEqual([second.source_path, first.source_path], [row.source for row in segments.mappings])
        self.assertEqual([1, 2], [row.asset.segment.index for row in segments.mappings])
        unknown = video('unassigned.mkv')
        extra = video('Example [CM].mkv')
        mixed = replace(value, videos=(first, extra, unknown), primary_sources=(first.source_path,),
                        excluded_sources=(), roles=())
        result = module.project_movie_videos(mixed)
        roles = {row.source: row.role for row in result.mappings}
        self.assertEqual('disc_extra', roles[extra.source_path])
        self.assertEqual('movie_version_unselected', roles[unknown.source_path])
        self.assertIn(unknown.source_path.name, result.unresolved)
        excluded = module.project_movie_videos(replace(mixed, excluded_sources=(unknown.source_path,)))
        self.assertNotIn(unknown.source_path.name, excluded.unresolved)
        explicit = module.project_movie_videos(replace(mixed, roles=((unknown.source_path, 'extra'),)))
        self.assertEqual('disc_extra', next(row.role for row in explicit.mappings if row.source == unknown.source_path))

    def test_conflicts_and_explicit_group_distinction_are_preserved(self):
        module, value, _expected, _ = self.captured(self.root())
        first = value.videos[0]
        second = replace(first, source_path=value.source_root / 'Other [1080p].mkv')
        duplicate = replace(value, videos=(first, second), primary_sources=(first.source_path, second.source_path),
                            groups_supplied=False)
        with self.assertRaisesRegex(RuntimeError, '唯一版本标签'):
            module.project_movie_videos(duplicate)
        self.assertEqual(2, len(module.project_movie_videos(replace(duplicate, groups_supplied=True)).mappings))
        bad = replace(first, source_path=value.source_root / 'Example [1080p 2160p].mkv')
        with self.assertRaisesRegex(RuntimeError, '规格证据冲突'):
            module.project_movie_videos(replace(value, videos=(bad,), primary_sources=(bad.source_path,)))
        with self.assertRaisesRegex(RuntimeError, '全部视频'):
            module.project_movie_videos(replace(duplicate, primary_sources=None,
                                                confirmed_segments=(first.source_path,)))


if __name__ == '__main__':
    unittest.main()
