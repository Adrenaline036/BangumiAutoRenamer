"""Pure frozen links: observation is explicit; closures never guess again."""
from dataclasses import FrozenInstanceError, replace
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename.rename_plan import PlanEntry, SubtitlePlanContract


class ContentAssociationsContract(unittest.TestCase):
    def test_ambiguous_and_explicit_shared_audio_are_not_owned_by_one_video(self):
        module, root, entries, _ = self.fixture()
        second = replace(entries[1], source_path=entries[0].source_path.with_suffix('.mp4'))
        values = (entries[0], second, *entries[2:])
        ambiguous = module.observe_content_associations(values, task_id='pure-content', source_root=root)
        for video in (entries[0], second):
            self.assertNotIn(entries[3].source_path,
                module.associated_source_closure(ambiguous, [video.source_path]))
        shared = module.observe_content_associations(values, task_id='pure-content', source_root=root,
            choices={entries[3].source_path: (entries[0].source_path, second.source_path)})
        self.assertNotIn(entries[3].source_path,
            module.associated_source_closure(shared, [entries[0].source_path]))
        self.assertNotIn(entries[3].source_path,
            module.associated_source_closure(shared, [second.source_path]))

    def fixture(self):
        module = importlib.import_module('src.rename.content_associations')
        root = Path(tempfile.gettempdir()) / 'bar-content-association-pure'
        video = PlanEntry(root / 'Show.mkv', root / 'out' / 'Target.mkv', media_kind='video')
        other = replace(video, source_path=root / 'Other.mkv', target_path=root / 'out' / 'Other.mkv')
        subtitle = PlanEntry(root / 'unrelated.ass', root / 'out' / 'Target.zh.ass',
            media_kind='subtitle', subtitle=SubtitlePlanContract(video.source_path, 'SC', 'zh', 'Hans'))
        audio = PlanEntry(root / 'Show.mka', root / 'out' / 'Target.mka', media_kind='associated')
        unknown = replace(audio, source_path=root / 'unknown.wav')
        font = replace(audio, source_path=root / '[Fonts].zip', role='font')
        entries = (video, other, subtitle, audio, unknown, font)
        links = module.observe_content_associations(entries, task_id='pure-content', source_root=root)
        return module, root, entries, links

    def test_typed_subtitle_and_unique_audio_but_no_unknown_or_shared_font_in_closure(self):
        module, _, entries, links = self.fixture()
        expected = {entries[i].source_path for i in (0, 2, 3)}
        self.assertEqual(expected, set(module.associated_source_closure(links, [entries[0].source_path])))
        self.assertEqual({entries[2].source_path},
            set(module.associated_source_closure(links, [entries[2].source_path])))

    def test_explicit_reassociation_wins_and_is_independent_of_old_target_and_stem(self):
        module, root, entries, links = self.fixture()
        choices = {entries[2].source_path: entries[1].source_path,
                   entries[3].source_path: entries[1].source_path}
        revised = module.observe_content_associations(entries, task_id='pure-content',
            source_root=root, choices=choices)
        self.assertEqual({entries[0].source_path},
            set(module.associated_source_closure(revised, [entries[0].source_path])))
        self.assertEqual({entries[i].source_path for i in (1, 2, 3)},
            set(module.associated_source_closure(revised, [entries[1].source_path])))
        self.assertNotEqual(links.fingerprint, revised.fingerprint)

    def test_scope_duplicate_font_and_missing_owner_choices_are_rejected(self):
        module, root, entries, _ = self.fixture()
        for values, choices in (
            ((*entries, entries[0]), {}),
            ((replace(entries[0], source_path=root.parent / 'outside.mkv'),), {}),
            (entries, {entries[5].source_path: entries[0].source_path}),
            (entries, {entries[3].source_path: root / 'absent.mkv'}),
            (entries, {entries[0].source_path: entries[1].source_path}),
        ):
            with self.subTest(choices=choices), self.assertRaises(ValueError):
                module.observe_content_associations(values, task_id='pure-content', source_root=root, choices=choices)

    def test_serialization_is_immutable_strict_and_closure_has_no_io_or_reobservation(self):
        module, _, entries, links = self.fixture()
        plain = links.to_dict()
        restored = module.ContentAssociations.from_mapping(plain)
        self.assertEqual(links, restored)
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            restored.payload_json = '{}'
        with self.assertRaises(ValueError):
            module.ContentAssociations.from_mapping({**plain, 'unrecognized': True})
        with patch.multiple(Path, stat=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('filesystem')),
                resolve=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('filesystem'))), \
                patch('src.rename.content_associations.observe_content_associations',
                      side_effect=AssertionError('reobserved')):
            self.assertEqual({entries[i].source_path for i in (0, 2, 3)},
                set(module.associated_source_closure(restored, [entries[0].source_path])))
