"""Group-level version decisions through public review/approval services."""
from pathlib import Path
import shutil
import tempfile
import unittest

from src.rename.plan_store import PlanStore
from tests import _media_flow_helpers as movie_fixture
from tests import _media_flow_helpers as media_fixture
from tests import test_err059_delivery_incidents as delivery_fixture


class MovieVersionIncidents(unittest.IsolatedAsyncioTestCase):
    async def incoming(self, root, *, complete=True):
        sample, _archive = media_fixture.make_season(root / 'sample')
        source = root / 'sources' / 'Example Movie [1080p]'
        source.mkdir(parents=True)
        videos = []
        for name in ('Theatrical', 'Extended'):
            video = source / f'Example Movie {name} [1080p].mkv'
            shutil.copyfile(next(sample.glob('*.mkv')), video)
            video.with_suffix('.mka').write_bytes(name.encode())
            videos.append(video)
        fixture = movie_fixture.MediaFlow('runTest')
        initial = await fixture._run_provider_flow(root, source, 'versions')
        if not complete:
            return fixture, initial, videos
        completed = await fixture._complete_movie(root=root, result=initial, primary=tuple(videos),
            version_groups=[dict(sources=[str(video)], label=label) for video, label in zip(
                videos, ('初始院线版', '初始加长版'))])
        self.assertIsNotNone(completed.state.movie_version_selection,
            'the actual first-completion GUI must succeed before testing later version edits')
        self.assertEqual(2, len(completed.state.movie_version_selection.to_dict()['groups']))
        return fixture, completed, videos

    def revise(self, completed, groups, excluded=()):
        command = getattr(completed.review, 'revise_movie_versions', None)
        self.assertTrue(callable(command), 'group-level movie version decision command is missing')
        return command(completed.plan.task_id, PlanStore.fingerprint(completed.plan),
                       groups=groups, excluded_sources=excluded)

    async def test_same_spec_groups_accept_labels_and_label_edit_preserves_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _fixture, completed, videos = await self.incoming(root)
            groups = [dict(sources=[str(video)], label=label) for video, label in zip(
                videos, ('1080p - 院线版', '1080p - 加长版'))]
            plan = self.revise(completed, groups)
            rows = [e for e in plan.entries if e.source_path in videos]
            self.assertEqual(2, len({e.presentation_asset.version_descriptor.version_ref for e in rows}))
            self.assertEqual({'1080p - 院线版', '1080p - 加长版'},
                             {e.presentation_asset.version_descriptor.display_label for e in rows})
            refs = {str(e.source_path): e.presentation_asset.version_descriptor.version_ref for e in rows}
            groups[0]['label'] = '1080p - Airota'
            next_plan = completed.review.revise_movie_versions(plan.task_id, PlanStore.fingerprint(plan),
                groups=groups, excluded_sources=[])
            self.assertEqual(refs, {str(e.source_path): e.presentation_asset.version_descriptor.version_ref
                                   for e in next_plan.entries if e.source_path in videos})
            self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(next_plan))

    async def test_unselected_complete_version_and_audio_are_explicitly_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            _fixture, completed, videos = await self.incoming(Path(temp))
            plan = self.revise(completed, [
                dict(sources=[str(videos[0])], label='1080p'),
                dict(sources=[str(videos[1])], label='1080p - 备选')], [str(videos[1])])
            expected = {videos[1], videos[1].with_suffix('.mka')}
            self.assertTrue(expected <= set(plan.ignored_sources))
            self.assertFalse(expected & {e.source_path for e in plan.entries})
            self.assertTrue(all(p.exists() for p in expected))

    async def test_movie_subtitle_archive_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture, initial, primary = await delivery_fixture.DeliveryIncidents('runTest').movie(root)
            plan = (await fixture._complete_movie(root=root, result=initial, primary=primary)).plan
            self.assertTrue(any(p.name.endswith('[Subtitles].zip') for p in plan.ignored_sources))
            self.assertFalse(any(e.source_path.name.endswith('[Subtitles].zip') for e in plan.entries))


if __name__ == '__main__':
    unittest.main()
