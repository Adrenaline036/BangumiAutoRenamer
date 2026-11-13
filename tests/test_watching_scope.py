"""观看范围与严格配套的定向合同；不访问真实媒体。"""
from pathlib import Path
import unittest
from unittest.mock import patch

from src.rename.content_associations import AUDIO_SUFFIXES, observe_content_associations, source_kind
from src.rename.generated_outputs import expected_files
from src.rename.rename_plan import PlanEntry, RenamePlan

EXPECTED_AUDIO_SUFFIXES = ('.mka', '.flac', '.wav', '.aac', '.ac3', '.eac3', '.dts', '.m4a', '.mp3', '.ogg')

class WatchingScope(unittest.TestCase):
    root = Path('C:/isolated-watching-source')
    target = Path('C:/isolated-watching-target')

    def entry(self, name):
        return PlanEntry.from_paths(self.root / name, self.target / name)

    def relations(self, names):
        entries = tuple(self.entry(name) for name in names)
        return observe_content_associations(entries, task_id='watching', source_root=self.root).to_dict()['relations']

    def test_font_is_non_media_without_opening_source(self):
        with patch.object(Path, 'open', side_effect=AssertionError('源内容不应打开')):
            for name in ('Fonts.7z', 'Fonts.zip', 'Fonts.rar', 'font.ttf', 'font.otf', 'font.ttc', 'font.otc', 'README.txt', 'cover.jpg'):
                with self.subTest(name=name):
                    self.assertEqual('other', source_kind(self.entry(name)))
            for name in ('Archive.7z', 'README.txt', 'untagged.bin'):
                with self.subTest(role='font', name=name):
                    entry = PlanEntry(self.root / name, self.target / name, role='font')
                    self.assertEqual('other', source_kind(entry))

    def test_subtitle_containers_use_one_media_classification(self):
        for suffix in ('.mks', '.idx', '.sub', '.sup', '.ass', '.srt'):
            with self.subTest(suffix=suffix):
                self.assertEqual('subtitle', self.entry('Film.zh-Hans' + suffix).media_kind)

    def test_strict_non_mka_audio_is_linked(self):
        self.assertEqual(frozenset(EXPECTED_AUDIO_SUFFIXES), AUDIO_SUFFIXES)
        for suffix in EXPECTED_AUDIO_SUFFIXES:
            with self.subTest(suffix=suffix):
                rows = self.relations(['Film.mkv', 'Film' + suffix])
                self.assertEqual('linked', rows[0]['state'])
                self.assertEqual([str(self.root / 'Film.mkv')], rows[0]['owners'])

    def test_audio_with_two_matching_video_formats_is_unresolved(self):
        for suffix in EXPECTED_AUDIO_SUFFIXES:
            with self.subTest(suffix=suffix):
                rows = self.relations(['Film.mkv', 'Film.mp4', 'Film' + suffix])
                self.assertEqual('unresolved', rows[0]['state'])
                self.assertEqual([], rows[0]['owners'])

    def test_near_names_other_directory_and_cd_music_are_not_linked(self):
        for name in ('Film 01.flac', 'Film 010.mka', 'CD/Film.mka', 'Soundtrack.flac'):
            with self.subTest(name=name):
                rows = self.relations(['Film.mkv', name])
                self.assertEqual('unresolved', rows[0]['state'])
                self.assertEqual([], rows[0]['owners'])

    def test_pure_relation_observation_has_no_source_io(self):
        with (patch.object(Path, 'open', side_effect=AssertionError('源内容打开')),
              patch.object(Path, 'stat', side_effect=AssertionError('额外属性查询')),
              patch.object(Path, 'iterdir', side_effect=AssertionError('重复扫描'))):
            rows = self.relations(['Film.mkv', 'Film.mka'])
        self.assertEqual('linked', rows[0]['state'])

    def test_no_ignore_output_is_generated(self):
        entry = PlanEntry(self.root / 'README.txt', self.target / 'Associated' / 'README.txt')
        plan = RenamePlan('watching', self.root, (entry,), metadata={'canonical_work_root': str(self.target)})
        self.assertEqual([], expected_files(plan))

    def test_retired_font_metadata_cannot_generate_ignore(self):
        from src.rename.generated_outputs import freeze_outputs
        data = dict(policy='automatic-font-union-v2', backend='series', key='series-test',
            execution_root=str(self.target.parent), work_root=str(self.target),
            directory_name=self.target.name + ' [Fonts]', archive=False, operation_id='retired-fonts',
            inputs=[dict(path=str(self.root / 'Fonts.7z'), size=1, mtime_ns=1, dev=1, ino=1,
                         members={'face.ttf': {'key': 'xxh3-128-' + '0' * 32, 'size': 1}}, error=None)])
        plan = RenamePlan('watching', self.root, (), metadata={
            'canonical_work_root': str(self.target), 'font_contribution': data})
        with self.subTest(operation='expected_files'):
            self.assertEqual([], expected_files(plan))
        with self.subTest(operation='freeze_outputs'):
            with patch('src.rename.generated_outputs.inspect_target', return_value=None):
                self.assertNotIn('generated_outputs', freeze_outputs(plan).metadata)

    def test_movie_extra_is_video_and_does_not_need_ignore(self):
        entry = PlanEntry.from_paths(self.root / 'SP01.mkv', self.target / 'Extras' / 'SP01.mkv')
        self.assertEqual('video', source_kind(entry))
        plan = RenamePlan('watching', self.root, (entry,), metadata={'canonical_work_root': str(self.target)})
        self.assertEqual([], expected_files(plan))


if __name__ == '__main__':
    unittest.main()
