"""TV video extraction: actual inventory entry, pure mapping, no live media."""
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename.inventory_plan import build_inventory_review_draft
from src.rename.movie_assets import inventory_movie_assets
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from tests.projection_test_support import save_plan_fixture
from tests.test_isolated_sample_acceptance import routing_snapshot_for
from tests.test_content_choice_contract import records
from tests.test_err060_tv_inventory_retry import completed_tv
from tests import test_movie_video_projection as movie_fixture


class TVVideoProjectionContracts(unittest.TestCase):
    def root(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('src.rename.tv_video_projection'),
                             'missing unique pure TV video mapping kernel')
        from src.rename import tv_video_projection
        return tv_video_projection

    def captured(self):
        module = self.module()
        calls = []
        original = module.project_tv_videos
        def capture(value):
            result = original(value)
            calls.append((value, result))
            return result
        with patch.object(module, 'project_tv_videos', capture):
            service, plan, extra = completed_tv(self.root())
        self.assertEqual(1, len(calls), 'actual inventory must call unique kernel once')
        return module, *calls[0], service, plan, extra

    def test_existing_public_tv_baseline_and_subtitle_audio_owner(self):
        service, plan, extra = completed_tv(self.root())
        self.assertEqual(plan, service.plan_store.load(plan.task_id))
        self.assertEqual('tv_episodes', plan.metadata['completion_mode'])
        self.assertEqual({1, 2}, {e.episode_id for e in plan.entries if e.role == 'main_episode'})
        bundle = [e for e in plan.entries if e.source_path in {
            extra, extra.with_suffix('.mka'), extra.with_suffix('.sc.ass')}]
        self.assertEqual(3, len(bundle))
        self.assertTrue(all('Extras' in e.target_path.parts for e in bundle))
        subtitle = next(e for e in bundle if e.subtitle is not None)
        self.assertEqual(extra, subtitle.subtitle.associated_video_source_path)

    def test_actual_public_uses_kernel_and_returns_per_source_identity(self):
        module, value, result, _, plan, _ = self.captured()
        self.assertEqual(result, module.project_tv_videos(value))
        self.assertEqual({e.source_path for e in plan.entries if e.media_kind == 'video'},
                         {row.source for row in result.mappings})
        main = [row for row in result.mappings if row.is_content]
        self.assertEqual({(1, 1), (1, 2)}, {(row.season_id, row.episode_id) for row in main})
        for row in main:
            self.assertEqual(f'{value.work_root.name} S01E{row.episode_id:02d}.mkv', row.target.name)
            self.assertEqual('main_episode', row.role)
            final = next(e for e in plan.entries if e.source_path == row.source)
            self.assertEqual((row.target, row.season_id, row.episode_id, row.asset, row.role),
                             (final.target_path, final.season_id, final.episode_id, final.presentation_asset, final.role))
            self.assertIsNotNone(row.asset.version_descriptor)
            self.assertEqual(value.group_id, row.asset.presentation_group)

    def test_conflicts_are_reviewable_and_explicit_roles_repair_them(self):
        module, value, *_ = self.captured()
        first = replace(value.videos[0], source_path=value.source_root / 'Show S01E01.mkv')
        second = replace(first, source_path=value.source_root / 'Alternate S01E01.mkv')
        collision = replace(value, videos=(first, second), roles=())
        result = module.project_tv_videos(collision)
        self.assertEqual({str(first.source_path), str(second.source_path)}, set(result.episode_conflicts))
        self.assertTrue(all(not row.is_content and 'ManualReview' in row.target.parts for row in result.mappings))
        repaired = module.project_tv_videos(replace(collision, roles=((second.source_path, 'extra'),)))
        self.assertFalse(repaired.episode_conflicts)
        self.assertEqual(1, sum(row.is_content for row in repaired.mappings))
        self.assertEqual('disc_extra', repaired.mappings[1].role)
        explicit = replace(collision, videos=(first,), roles=((first.source_path, 'main_candidate'),))
        self.assertTrue(module.project_tv_videos(explicit).mappings[0].is_content)

    def test_public_two_explicit_duplicates_are_reviewable_and_role_replay_repairs(self):
        root = self.root()
        source = root / 'source'
        source.mkdir()
        for name in ('Show S01E01.mkv', 'Alternate S01E01.mkv'):
            (source / name).write_bytes(b'isolated media fixture')
        target = root / 'TV'
        identity = dict(namespace='tmdb', entity_type='tv', provider_id='4242', title='普通剧集', year=2020)
        task = dict(uuid='tv-duplicate-kernel', path=str(source), name=identity['title'],
                    is_anime=False, is_movie=False, season_id=1,
                    movie_inventory=inventory_movie_assets(source).to_dict(),
                    draft_target_root=str(target), provider_identity=identity)
        store = PlanStore(root / 'plans')
        draft = save_plan_fixture(store, build_inventory_review_draft(task, dry_run=True))
        service = PlanReviewService(store, root / 'reviews', root / 'work_catalog')
        plan = service.complete_inventory_draft(draft.task_id, store.fingerprint(draft),
            group_id=draft.presentation_groups[0].group_id, group_kind='tv_episodes',
            target_root=target, root_snapshot=routing_snapshot_for(target, domain='general', group_kind='tv_episodes'),
            work_domain='general', season_number=1, season_source='explicit_user', reason='确认季度')
        codes = {issue.code for issue in PlanVerifier().verify(plan).issues}
        self.assertIn('episode_assignment_conflict', codes, sorted(codes))
        self.assertNotIn('duplicate_target', codes)
        chosen = next(e for e in plan.entries if e.source_path.name.startswith('Alternate'))
        repaired = service.recomplete_inventory_with_role_decisions(plan.task_id, store.fingerprint(plan),
            decisions={entry_id(chosen): 'extra'}, reason='另一视频属于特典')
        self.assertFalse(repaired.metadata['episode_assignment_conflict_sources'])
        self.assertEqual(1, sum(e.role == 'main_episode' for e in repaired.entries))
        self.assertEqual('disc_extra', next(e for e in repaired.entries if e.source_path == chosen.source_path).role)
        self.assertEqual(repaired, store.load(plan.task_id))

    def test_unknown_and_season_zero_not_silently_main_or_season_one(self):
        module, value, *_ = self.captured()
        unknown = replace(value.videos[0], source_path=value.source_root / 'Deep' / 'Unidentified.mkv')
        result = module.project_tv_videos(replace(value, videos=(unknown,), season_id=0, roles=()))
        self.assertEqual(('Unidentified.mkv',), result.unresolved)
        self.assertEqual(0, result.mappings[0].season_id)
        self.assertEqual(value.work_root / 'ManualReview' / 'Deep' / 'Unidentified.mkv', result.mappings[0].target)
        for invalid in (None, -1, True, '1'):
            with self.subTest(season=invalid), self.assertRaisesRegex(RuntimeError, '季度'):
                module.project_tv_videos(replace(value, season_id=invalid))

    def test_same_episode_in_different_explicit_seasons_is_not_a_collision(self):
        module, value, *_ = self.captured()
        first = replace(value.videos[0], source_path=value.source_root / 'Show S01E01.mkv')
        second = replace(first, source_path=value.source_root / 'Show S02E01.mkv')
        result = module.project_tv_videos(replace(value, videos=(first, second), roles=()))
        self.assertFalse(result.episode_conflicts)
        self.assertEqual({(1, 1), (2, 1)}, {(row.season_id, row.episode_id) for row in result.mappings})
        self.assertTrue(all(row.is_content for row in result.mappings))
        self.assertEqual(2, len({row.target for row in result.mappings}))

    def test_frozen_input_and_no_io(self):
        module, value, expected, *_ = self.captured()
        before = repr(value)
        with ExitStack() as stack:
            for target in ('builtins.open', 'pathlib.Path.stat', 'pathlib.Path.exists',
                           'pathlib.Path.is_file', 'pathlib.Path.read_bytes', 'pathlib.Path.read_text',
                           'os.path.lexists', 'requests.sessions.Session.request',
                           'src.rename.plan_store.PlanStore.load',
                           'src.rename.decision_state.DecisionStateStore.load',
                           'src.rename.series_catalog.SeriesCatalog.load'):
                stack.enter_context(patch(target, side_effect=AssertionError('TV kernel attempted I/O')))
            self.assertEqual(expected, module.project_tv_videos(value))
            self.assertEqual(expected, module.project_tv_videos(value))
        self.assertEqual(before, repr(value))
        with self.assertRaises(FrozenInstanceError):
            value.season_id = 2
        for invalid in (object(), replace(value, videos=list(value.videos)), replace(value, roles=[])):
            with self.subTest(invalid=type(invalid).__name__), self.assertRaises(RuntimeError):
                module.project_tv_videos(invalid)

    def test_exact_resolution_versions_and_ambiguous_duplicates(self):
        module, value, *_ = self.captured()
        def project(names):
            videos = tuple(replace(value.videos[0], source_path=value.source_root / name) for name in names)
            return module.project_tv_videos(replace(value, videos=videos, roles=()))
        result = project(('Show [12] [1080p].mkv', 'Show [12] [2160p].mkv'))
        self.assertFalse(result.episode_conflicts)
        self.assertEqual({12}, {row.episode_id for row in result.mappings})
        self.assertTrue(all(row.is_content for row in result.mappings))
        self.assertEqual(2, len({row.target for row in result.mappings}))
        for names in (
            ('Show S01E01 [1080p].mkv', 'Alternate S01E01 [1080p].mkv'),
            ('Show S01E01 [1080p 2160p].mkv', 'Show S01E01 [720p].mkv'),
            ('Show S01E01 [1080p].mkv', 'Show S01E01 [2160p].mkv', 'Another S01E01 [1080p].mkv'),
        ):
            with self.subTest(names=names):
                result = project(names)
                self.assertTrue(result.episode_conflicts)
                self.assertTrue(all(not row.is_content and row.episode_id is None for row in result.mappings))

    def test_disconnect_or_wrong_result_does_not_persist_or_fallback(self):
        module = self.module()
        for broken in ('raise', None, object()):
            with self.subTest(broken=type(broken).__name__):
                root = self.root()
                before = []
                def fault(_value):
                    before.append(records(root))
                    if broken == 'raise':
                        raise RuntimeError('TV kernel disconnected')
                    return broken
                with patch.object(module, 'project_tv_videos', fault):
                    with self.assertRaises(RuntimeError):
                        completed_tv(root)
                self.assertEqual(1, len(before))
                self.assertEqual(before[0], records(root))

    def test_movie_control_does_not_use_tv_kernel(self):
        module = self.module()
        with patch.object(module, 'project_tv_videos', side_effect=AssertionError('movie reached TV kernel')) as kernel:
            _, plan, store, _ = movie_fixture.MovieVideoProjectionContracts('runTest').legacy(self.root())
        kernel.assert_not_called()
        self.assertEqual(plan, store.load(plan.task_id))
        self.assertEqual('movie_content', next(e for e in plan.entries if e.media_kind == 'video').role)
