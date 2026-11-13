"""CD covers/logs are existing non-watchable content, not a new workflow."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename import media_classifier as classifier
from src.rename.input_directory_discovery import discover_sources, discovery_task_groups, validate_discovery
from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from src.pages.content_review_view import selection_rows
from tests import test_default_activation_contract as activation
from tests.test_content_choice_contract import change, records


class CDObservationContracts(unittest.TestCase):
    def test_cd_images_and_logs_follow_existing_audio_only_rule(self):
        root = Path.cwd() / 'disposable-observation-root'
        for directory in classifier.AUDIO_DISC_DIRECTORY_NAMES:
            for suffix in ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.log', '.txt'):
                with self.subTest(directory=directory, suffix=suffix):
                    path = root / directory / 'Album' / ('Cover' + suffix.upper())
                    rows = classifier.observe_watchable_sources([root/'Show S01E01.mkv', path], source_root=root)
                    self.assertEqual('known_non_watchable', rows[1].disposition)

    def test_ordinary_video_unknown_and_protected_controls(self):
        root = Path.cwd() / 'disposable-observation-root'
        for relative in ('Cover.jpg', 'Album/rip.log', 'Album/notes.txt',
                         'CDs/Album/Fonts.zip', 'CDs/Album/Font.ttf', 'CDs/Album/dialogue.ass'):
            with self.subTest(relative=relative):
                row, = classifier.observe_watchable_sources([root/relative], source_root=root)
                self.assertEqual('needs_confirmation', row.disposition)
        cover = root/'CDs/Album/Cover.jpg'
        log = cover.with_name('rip.log')
        video = root/'CDs/Other/Unknown OVA.mkv'
        rows = classifier.observe_watchable_sources([cover, log, video], source_root=root)
        self.assertFalse(any(r.disposition == 'known_non_watchable' for r in rows))
        for role in ('content_version', 'provider_special', 'font_resource'):
            row, = classifier.observe_watchable_sources([cover], source_root=root, protected_roles={cover:role})
            self.assertEqual('keep_candidate', row.disposition)
        for path in (cover, log, root/'CDs/audio.mka'):
            row, = classifier.observe_watchable_sources([path], source_root=root,
                linked_videos={path:root/'Show S01E01.mkv'})
            self.assertEqual('keep_candidate', row.disposition)
        row, = classifier.observe_watchable_sources([cover], source_root=cover.parent)
        self.assertEqual('needs_confirmation', row.disposition)

    def test_discovery_retains_inventory_skips_cd_card_and_rejects_added_video(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = [root/'Season 1/Show S01E01.mkv', root/'CDs/Album/Cover.jpg', root/'CDs/Album/rip.log']
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'isolated discovery fixture')
            discovery = discover_sources([root])
            self.assertEqual(set(paths), {Path(i.source_path) for g in discovery.groups for i in g.identities})
            self.assertEqual({paths[0]}, {Path(i.source_path) for g in discovery_task_groups(discovery) for i in g.identities})
            (root/'CDs/Unknown OVA.mkv').write_bytes(b'unknown video')
            with self.assertRaisesRegex(ValueError, '变化'):
                validate_discovery(discovery)
            current = discover_sources([root])
            self.assertEqual(set(root.rglob('*')) - {p for p in root.rglob('*') if p.is_dir()},
                {Path(i.source_path) for g in discovery_task_groups(current) for i in g.identities})


class CDDefaultContracts(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow
    current = activation.DefaultActivationProducerContracts.current

    def with_cd(self, root, source):
        self.companions = set()
        for index in range(8):
            album = source/'CDs'/f'Album {index}'
            album.mkdir(parents=True)
            names = ['RIP-0001.log'] + (['Cover.jpg'] if index < 2 else [])
            for name in names:
                path = album/name
                path.write_bytes(b'isolated CD companion')
                self.companions.add(path)
        self.originals = {p:p.read_bytes() for p in source.rglob('*') if p.is_file()}
        return source

    async def test_current_tv_movie_defaults_exclude_cd_but_user_can_restore(self):
        for movie in (False, True):
            with self.subTest(movie=movie):
                async with self.flow(movie=movie, configure_source=self.with_cd) as flow:
                    plan, state = self.current(flow)
                    self.assertTrue(self.companions <= set(plan.ignored_sources))
                    self.assertFalse(self.companions & {e.source_path for e in plan.entries})
                    self.assertTrue(self.companions <= {Path(r['source_path']) for r in state.content_selection.to_dict()['materials']})
                    rows = selection_rows(state.content_selection, state.movie_version_selection)
                    self.assertFalse(any(r['locked'] for r in rows if r['source'] in self.companions))
                    for source in self.companions:
                        plan = change(flow.service, plan, [source], action='restore')
                        self.assertNotIn(source, plan.ignored_sources)
                        self.assertIn(source, {e.source_path for e in plan.entries})
                        plan = change(flow.service, plan, [source], action='exclude')
                        self.assertIn(source, plan.ignored_sources)
                    self.assertEqual(self.originals, {p:p.read_bytes() for p in self.originals})
                    self.assertFalse(flow.service.load_or_create(plan).approved_entry_ids)
                    self.assertTrue(all(not e.target_path.exists() for e in plan.entries))

    async def test_old_v2_retained_cd_stays_readable_and_manually_excludable(self):
        current_observer = classifier.observe_watchable_sources
        def old_observer(paths, **kwargs):
            return tuple(replace(row, disposition='needs_confirmation',
                reason_codes=('unresolved_companion_or_resource',))
                if row.source_path in self.companions else row
                for row in current_observer(paths, **kwargs))
        # Actual old-rule producer path; no fabricated persisted Decision/Plan.
        with patch('src.rename.default_content.observe_watchable_sources', old_observer) as old:
            async with self.flow(configure_source=self.with_cd) as flow:
                plan, state = self.current(flow)
                self.assertFalse(self.companions & set(plan.ignored_sources))
                before = records(flow.root)
                # Switch just the rule version, leaving the already frozen records alone.
                with patch('src.rename.default_content.observe_watchable_sources', current_observer):
                    loaded = flow.store.load(plan.task_id)
                    loaded_state = flow.service.decision_state_store.load(plan.task_id)
                    self.assertEqual(plan, loaded)
                    self.assertEqual(state, loaded_state)
                    rows = selection_rows(loaded_state.content_selection, loaded_state.movie_version_selection)
                    self.assertTrue(all(not r['locked'] for r in rows if r['source'] in self.companions))
                    self.assertEqual(before, records(flow.root))
                    updated = change(flow.service, loaded, self.companions)
                    self.assertTrue(self.companions <= set(updated.ignored_sources))
                    self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(updated))
                    self.assertFalse(flow.service.load_or_create(updated).approved_entry_ids)
                    self.assertEqual(self.originals, {p:p.read_bytes() for p in self.originals})


if __name__ == '__main__':
    unittest.main()
