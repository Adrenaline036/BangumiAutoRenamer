"""Pure content observation contracts; no Provider, media writes or Plan authority."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename import media_classifier as classifier


class SafeWatchableObservationContracts(unittest.TestCase):
    root = Path(tempfile.gettempdir())/'bar-pure-content-observation'

    def observe(self, relative, **kwargs):
        observer = getattr(classifier, 'observe_watchable_content', None)
        self.assertTrue(callable(observer), 'missing distinct safe-watchable observation producer')
        return observer(self.root/relative, source_root=self.root, **kwargs)

    def test_explicit_non_watchable_tokens_and_documents_have_frozen_evidence(self):
        cases = ('Show [Menu].mkv', 'Show [Trailer].mkv', 'Show [PV01].mp4',
                 'Show [MV].mkv', 'Show [NCOP].mkv', 'Show [TV-SPOT].mkv', 'Show [CM02].mkv',
                 'Show [予告編].mp4', 'Scans/001.png', '扫描图/封面.jpg',
                 'README.md', 'CHECKSUMS.txt', 'Show.sfv')
        for relative in cases:
            with self.subTest(relative=relative):
                result = self.observe(relative, work_title='Show')
                self.assertEqual('known_non_watchable', result.disposition)
                self.assertEqual('safe-watchable-content-v1', result.policy)
                self.assertEqual(self.root/relative, result.source_path)
                self.assertTrue(result.reason_codes)

    def test_old_extra_evidence_does_not_authorize_discarding_viewable_content(self):
        for relative in ('SPs/Show 01.mkv', 'Special/Show.mkv', '特典/Show.mkv',
                         'Show [OVA].mkv', 'Show [OAD].mkv', 'Show S00E01.mkv',
                         'Show 12.5.mkv', 'Show CD1.mkv', 'Show CD2.mkv',
                         'Unknown.mkv', 'Unknown.zip'):
            with self.subTest(relative=relative):
                self.assertNotEqual('known_non_watchable', self.observe(relative, work_title='Show').disposition)

    def test_title_words_and_companion_extensions_are_not_miscellaneous_evidence(self):
        for relative in ('The Trailer Park S01E01.mkv', 'MV Story 01.mkv',
                         'Show.flac', 'Show.wav', 'Show.mka', 'Show.sc.ass',
                         'notes.txt', 'poster.png', 'OST/Soundtrack.flac'):
            with self.subTest(relative=relative):
                self.assertNotEqual('known_non_watchable', self.observe(relative).disposition)
        self.assertEqual('known_non_watchable', self.observe('OST/Soundtrack.flac',
                         directory_has_video=False).disposition)
        self.assertNotEqual('known_non_watchable', self.observe('OST/Soundtrack.flac',
                            directory_has_video=True).disposition)

    def test_cds_audio_subtree_requires_complete_video_absence(self):
        self.assertEqual('known_non_watchable', self.observe('CDs/Soundtrack.flac',
            directory_has_video=False).disposition)
        for present in (None, True):
            self.assertNotEqual('known_non_watchable', self.observe('CDs/Soundtrack.flac',
                directory_has_video=present).disposition)
        self.assertEqual('keep_candidate', self.observe('CDs/Show.mka', directory_has_video=False,
            linked_video=self.root/'Show.mkv').disposition)
        self.assertNotEqual('known_non_watchable', self.observe('CDs/Show.mkv',
            directory_has_video=False).disposition)

    def test_typed_content_and_companions_take_priority_and_conflicts_remain_visible(self):
        for role in ('content_version', 'provider_special', 'font_resource'):
            with self.subTest(role=role):
                self.assertEqual('keep_candidate', self.observe('Show [MV].mkv', protected_role=role).disposition)
        self.assertEqual('keep_candidate', self.observe('OST/Show.flac', directory_has_video=False,
                         linked_video=self.root/'Show.mkv').disposition)
        self.assertEqual('needs_confirmation', self.observe('Show S01E01 [Trailer].mkv').disposition)
        self.assertEqual('needs_confirmation', self.observe('Show [OVA] [MV].mkv').disposition)

    def test_unknown_is_not_an_output_plan_and_observation_is_immutable(self):
        result = self.observe('Unknown.mkv')
        self.assertEqual('needs_confirmation', result.disposition)
        self.assertFalse(hasattr(result, 'target_path'))
        self.assertFalse(hasattr(result, 'approved'))
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            result.disposition = 'known_non_watchable'

    def test_outside_scope_and_unknown_protected_roles_are_rejected(self):
        with self.assertRaises(ValueError):
            self.observe('../outside/Menu.mkv')
        with self.assertRaises(ValueError):
            self.observe('Show.mkv', protected_role='arbitrary_override')
        with self.assertRaises(ValueError):
            self.observe('Show.mka', linked_video=self.root.parent/'another-scope'/'Show.mkv')

    def test_whole_residual_labels_round_brackets_and_title_boundaries(self):
        for relative in ('Show (Trailer).mkv', 'Show Trailer 1080p x265.mkv',
                         'Show [PV02][1080p].mkv', 'Show MV.mkv'):
            with self.subTest(relative=relative):
                self.assertEqual('known_non_watchable', self.observe(relative, work_title='Show').disposition)
        for relative, title in (('Showcase Trailer.mkv', 'Show'), ('Trailer.mkv', 'Trailer'),
                                ('Show [Trailer].mkv', 'Show [Trailer]')):
            with self.subTest(relative=relative):
                self.assertNotEqual('known_non_watchable', self.observe(relative, work_title=title).disposition)
        observer = classifier.observe_watchable_content
        for directory, suffix in (('OST', '.flac'), ('Scans', '.png')):
            root = self.root/directory/'Real Work'
            self.assertNotEqual('known_non_watchable', observer(root/('file'+suffix), source_root=root,
                                                               directory_has_video=False).disposition)

    def test_pure_observation_does_not_read_or_scan_files_and_is_deterministic(self):
        observer = getattr(classifier, 'observe_watchable_content', None)
        self.assertTrue(callable(observer))
        denied = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('pure observation did filesystem IO'))
        with patch('builtins.open', side_effect=denied), patch.multiple(Path,
                open=denied, stat=denied, lstat=denied, iterdir=denied, glob=denied, rglob=denied):
            first = self.observe('Missing [MV].mkv')
            second = self.observe('Missing [MV].mkv')
        self.assertEqual(first, second)
        self.assertIsInstance(first.reason_codes, tuple)
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            first.reason_codes += ('forged',)
