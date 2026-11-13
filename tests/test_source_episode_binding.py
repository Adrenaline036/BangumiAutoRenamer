"""Frozen per-source episode binding; disposable component media, no Provider."""
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename import provider_link_decision as legacy
from src.rename.provider_link_resolver import ProviderLinkResolution, derive_source_episode_projection
from src.rename.rename_plan import PlanEntry, RenamePlan


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


class SourceEpisodeBindingContracts(unittest.TestCase):
    def plan(self, episodes, *, versions=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / 'source'
        source.mkdir()
        entries = []
        for index, episode in enumerate(episodes):
            label = ('1080p', '2160p')[index % 2] if versions else str(index)
            path = source / f'Show S01E{episode:02d} [{label}].mkv'
            path.write_bytes(f'isolated video {index}'.encode())
            target = root / 'TV' / 'Show' / 'Season 01' / path.name
            entries.append(replace(PlanEntry.from_paths(path, target),
                                   media_kind='video', role='main_episode', season_id=1, episode_id=episode))
        return RenamePlan('source-episode', source, tuple(entries), tuple(e.source_path for e in entries),
                          metadata={'season_number': 1})

    def resolution(self, *, shape='many_subjects_one_season'):
        return ProviderLinkResolution('L1', 'inferred', mapping_shape=shape,
            derived_episode_ranges=({'bangumi_subject_id': 'second', 'episode_start': 12,
                                     'episode_end': 23, 'season_number': 1},))

    def freeze(self, plan):
        return legacy.freeze_source_episode_projection(plan, self.resolution(), selected_subject_id='second')

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('src.rename.source_episode_binding'),
                             'missing pure per-source episode binder')
        from src.rename import source_episode_binding
        return source_episode_binding

    def captured(self, plan):
        module = self.module()
        calls = []
        original = module.bind_source_episodes
        def capture(value):
            result = original(value)
            calls.append((value, result))
            return result
        with patch.object(module, 'bind_source_episodes', capture):
            projected, selected = self.freeze(plan)
        self.assertTrue(calls, 'real legacy freeze wrapper must use pure binder')
        return module, calls, projected, selected

    def assert_record(self, original, record):
        paths = {str(e.source_path): e for e in original.entries}
        self.assertEqual(set(paths), {r['source_path'] for r in record['episode_mapping']})
        self.assertEqual(sorted(paths, key=str.casefold), [r['source_path'] for r in record['episode_mapping']])
        for row in record['episode_mapping']:
            self.assertEqual({'source_path', 'source_episode', 'target_episode', 'source_size',
                              'source_mtime_ns', 'source_device', 'source_inode'}, set(row))
            entry = paths[row['source_path']]
            actual = entry.source_path.stat()
            self.assertEqual((entry.episode_id, actual.st_size, actual.st_mtime_ns, actual.st_dev, actual.st_ino),
                             tuple(row[k] for k in ('source_episode', 'source_size', 'source_mtime_ns', 'source_device', 'source_inode')))
        inventory = {'inventory_scope': record['inventory_scope'], 'source_episodes': tuple(
            {'source_path': row['source_path'], 'source_episode': row['source_episode']} for row in record['episode_mapping'])}
        self.assertEqual(canonical_hash(inventory), record['inventory_fingerprint'])
        self.assertEqual(canonical_hash({k: v for k, v in record.items() if k != 'projection_fingerprint'}),
                         record['projection_fingerprint'])

    def test_existing_wrapper_relative_absolute_and_ambiguous_byte_contract(self):
        for episodes, mode in ((range(1, 13), 'relative'), (range(12, 24), 'absolute'), ((12,), None)):
            with self.subTest(mode=mode):
                plan = self.plan(episodes)
                projected, selected = self.freeze(plan)
                if mode is None:
                    self.assertIsNone(selected)
                    candidates = projected.to_dict()['metadata']['source_episode_projection_candidates']
                    self.assertEqual({'absolute', 'relative'}, {r['mode'] for r in candidates})
                    self.assertEqual({12, 23}, {r['episode_mapping'][0]['target_episode'] for r in candidates})
                    for candidate in candidates:
                        self.assert_record(plan, candidate)
                else:
                    self.assertEqual(mode, selected['mode'])
                    self.assert_record(plan, selected)
                    self.assertEqual(set(range(12, 24)), {e.episode_id for e in projected.entries})

    def test_wrapper_delegation_preserves_both_sources_with_same_episode(self):
        plan = self.plan((12, 12), versions=True)
        module, calls, projected, selected = self.captured(plan)
        self.assertIsNone(selected)
        self.assertEqual(2, len(calls))
        for value, result in calls:
            self.assertEqual(result, module.bind_source_episodes(value))
            self.assert_record(plan, result.to_dict())
            self.assertEqual(2, len(result.to_dict()['episode_mapping']))
        self.assertEqual([r.to_dict() for _, r in calls], projected.to_dict()['metadata']['source_episode_projection_candidates'])

    def test_pure_replay_cannot_observe_files_provider_or_stores(self):
        module, calls, *_ = self.captured(self.plan(range(1, 13)))
        value, expected = calls[0]
        before = repr(value)
        with ExitStack() as stack:
            for target in ('builtins.open', 'pathlib.Path.stat', 'pathlib.Path.lstat', 'pathlib.Path.resolve',
                           'pathlib.Path.exists', 'pathlib.Path.read_bytes', 'requests.sessions.Session.request',
                           'src.rename.plan_store.PlanStore.load', 'src.rename.decision_state.DecisionStateStore.load'):
                stack.enter_context(patch(target, side_effect=AssertionError('pure binder attempted I/O')))
            self.assertEqual(expected, module.bind_source_episodes(value))
            self.assertEqual(expected, module.bind_source_episodes(value))
        self.assertEqual(before, repr(value))
        with self.assertRaises(FrozenInstanceError):
            value.projection_json = '{}'
        for construct in (lambda: replace(value, videos=list(value.videos)),
                          lambda: replace(value, projection_json=json.loads(value.projection_json)),
                          lambda: replace(value.observation, rows=list(value.observation.rows))):
            with self.assertRaises((ValueError, TypeError)):
                construct()

    def test_missing_duplicate_wrong_source_or_invalid_observation_rejected(self):
        module, calls, *_ = self.captured(self.plan(range(1, 13)))
        value = calls[0][0]
        rows = value.observation.rows
        bad_rows = (lambda: rows[:-1], lambda: rows + (rows[0],),
                    lambda: (replace(rows[0], source=rows[0].source.with_name('wrong.mkv')), *rows[1:]),
                    lambda: (replace(rows[0], size=True), *rows[1:]),
                    lambda: (replace(rows[0], inode=-1), *rows[1:]))
        for index, construct in enumerate(bad_rows):
            with self.subTest(case=index), self.assertRaises((ValueError, TypeError)):
                observation = replace(value.observation, rows=construct())
                with patch('pathlib.Path.stat', side_effect=AssertionError('cannot repair observation by stat')):
                    module.bind_source_episodes(replace(value, observation=observation))

    def test_ambiguous_candidates_share_one_physical_observation(self):
        module = self.module()
        plan = self.plan((12, 12), versions=True)
        with patch.object(module, 'observe_source_episode_identities', wraps=module.observe_source_episode_identities) as observe:
            self.freeze(plan)
        self.assertEqual(1, observe.call_count, 'candidate generation must not rescan each alternative')

    def test_ambiguous_observation_cannot_hide_unrelated_extra_rows(self):
        module = self.module()
        plan = self.plan((12,))
        observed = module.observe_source_episode_identities(tuple(e.source_path for e in plan.entries))
        extra = replace(observed.rows[0], source=plan.source_root / 'wrong.mkv')
        wrong = replace(observed, rows=(*observed.rows, extra))
        with patch.object(module, 'observe_source_episode_identities', return_value=wrong), \
                patch('pathlib.Path.stat', side_effect=AssertionError('cannot re-observe')):
            with self.assertRaises(ValueError):
                self.freeze(plan)

    def test_disconnected_or_wrong_type_binder_has_no_accepted_fallback(self):
        module = self.module()
        for invalid in ('raise', None, object()):
            with self.subTest(invalid=type(invalid).__name__):
                plan = self.plan(range(1, 13))
                before = plan.to_dict()
                def fault(_value):
                    if invalid == 'raise':
                        raise ValueError('binding disconnected')
                    return invalid
                with patch.object(module, 'bind_source_episodes', side_effect=fault) as binder:
                    with self.assertRaises((ValueError, TypeError)):
                        self.freeze(plan)
                self.assertTrue(binder.called)
                self.assertEqual(before, plan.to_dict())

    def test_disconnected_observer_cannot_fall_back_to_stat(self):
        module = self.module()
        for episodes in ((1, 2), (12,)):
            for invalid in ('raise', None, object()):
                with self.subTest(episodes=episodes, invalid=type(invalid).__name__):
                    plan = self.plan(episodes)
                    def fault(_sources):
                        if invalid == 'raise':
                            raise ValueError('observer disconnected')
                        return invalid
                    with patch.object(module, 'observe_source_episode_identities', side_effect=fault) as observe, \
                            patch.object(module, 'bind_source_episodes', side_effect=AssertionError('must not bind')) as bind, \
                            patch('pathlib.Path.stat', side_effect=AssertionError('must not fall back to stat')):
                        with self.assertRaises((ValueError, TypeError)):
                            self.freeze(plan)
                    observe.assert_called_once()
                    bind.assert_not_called()

    def test_noneligible_video_and_subtitle_are_not_observed(self):
        module = self.module()
        plan = self.plan((1, 2))
        videos = plan.entries
        noneligible = replace(videos[0], source_path=plan.source_root / 'not-present-E99.mkv', episode_id=99)
        subtitle = replace(videos[0], source_path=plan.source_root / 'not-present.ass', media_kind='subtitle')
        plan = replace(plan, entries=(*videos, noneligible, subtitle))
        projection = derive_source_episode_projection(source_episode_numbers=(1, 2),
            target_episode_start=12, target_episode_end=23, inventory_scope='complete')
        with patch.object(module, 'observe_source_episode_identities', wraps=module.observe_source_episode_identities) as observe:
            result = legacy._bind_projection_to_plan_sources(plan, projection)
        self.assertEqual(tuple(e.source_path for e in videos), observe.call_args.args[0])
        self.assertEqual({str(e.source_path) for e in videos}, {r['source_path'] for r in result['episode_mapping']})

    def test_unrelated_shape_never_observes_or_binds(self):
        module = self.module()
        plan = self.plan((1,))
        with patch.object(module, 'observe_source_episode_identities', side_effect=AssertionError) as observe, \
                patch.object(module, 'bind_source_episodes', side_effect=AssertionError) as bind:
            result = legacy.freeze_source_episode_projection(plan, self.resolution(shape='one_subject_one_season'),
                                                              selected_subject_id='second')
        self.assertEqual((plan, None), result)
        observe.assert_not_called()
        bind.assert_not_called()
