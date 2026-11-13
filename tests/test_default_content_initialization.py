"""Default initialization from actual pending history and complete source basis.

Disposable real queue/renamer/movie completion, replayed Provider transport,
valid tiny MKV/font fixtures. Never the user media root or retained fixture.
"""
from dataclasses import replace
import importlib.util
import os
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import _media_flow_helpers as movie_fixture
from tests import _media_flow_helpers as source_fixture
from tests import test_content_policy_contract as policy_fixture
from tests.test_content_choice_contract import records
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.plan_review import entry_id
from src.rename.decision_state import DecisionCommandService
from src.rename.projection_authority import commit_projected_plan, ProjectionCommitService


class DefaultInitializationContracts(unittest.IsolatedAsyncioTestCase):
    async def pre_activation_movie(self, root, source, primaries):
        """Real admission and pre-activation journals, never strip a new v2 Plan.

        This is an explicit historical protocol component control. Current
        queue/GUI automatic initialization is tested by default_activation.
        """
        from src.rename.decision_state import DecisionStateStore
        from src.rename.projection_authority import commit_recognition_projection, RecognitionDecisionInput
        from src.rename.rename_plan import WorkIdentity
        from src.rename.presentation import provider_identity_from_metadata
        from src.rename.target_routing import TargetRootSnapshot
        from src.rename.inventory_plan import build_inventory_review_draft
        from src.rename.movie_assets import inventory_movie_assets
        helper = policy_fixture.ContentPolicyContracts('runTest')
        helper.sources = lambda _root: source
        manager, service, state, plan = await helper.pending(root)
        self.addAsyncCleanup(manager.stop)
        task = manager._read(plan.task_id)
        provider = provider_identity_from_metadata(namespace='tmdb', entity_type='movie', provider_id='533514',
            title='紫罗兰永恒花园 剧场版', year=2020, decision_source='manual_review',
            evidence=('explicit pre-activation protocol fixture',))
        draft = build_inventory_review_draft({**task, 'movie_inventory': inventory_movie_assets(source).to_dict(),
            'draft_target_root': str(root / 'AnimeMovies'), 'provider_identity': provider.to_dict()}, dry_run=False)
        recognition = RecognitionDecisionInput(plan.task_id,
            WorkIdentity('confirmed', 'animation', provider.title, provider.year, provider, 'manual_library_selection'))
        draft = DecisionStateStore.project_plan(recognition.candidate(state), draft)
        _, plan = commit_recognition_projection(service.decision_state_store, service.plan_store,
            recognition, draft, task_terminal=service._revision_task_terminal(plan.task_id))
        roots = TargetRootSnapshot.capture({'bangumi_path': root/'TV', 'movie_path': root/'Movies',
            'anime_path': root/'Anime', 'anime_movie_path': root/'AnimeMovies'})
        plan = service.complete_inventory_draft(plan.task_id, PlanStore.fingerprint(plan),
            group_id=plan.presentation_groups[0].group_id, group_kind='movie_continuous',
            target_root=root/'AnimeMovies', root_snapshot=roots.to_dict(), work_domain='animation',
            primary_video_sources=tuple(primaries),
            movie_version_groups=[dict(sources=[str(p)], label=f'原版本{i + 1}') for i, p in enumerate(primaries)],
            reason='显式历史协议保留原电影版本，等待默认内容迁移')
        self.assertIsNone(service.decision_state_store.load(plan.task_id).content_selection)
        return service, plan

    def root(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / 'default-content'
        root.mkdir()
        return root

    async def ready(self, root, *, bad_cm=False, tagged_primary=False):
        source, primary, font = source_fixture._make_version(root / 'sources', '1080p')
        if tagged_primary:
            stem = primary.stem
            for path in source.iterdir():
                if path.is_file() and path.name.startswith(stem):
                    path.rename(path.with_name(path.name.replace(stem, stem + ' [CM]', 1)))
            primary = primary.with_stem(stem + ' [CM]')
        (source / 'README.txt').write_text('fixture documentation', encoding='utf-8')
        (source / 'Scans/deep').mkdir(parents=True)
        (source / 'Scans/deep/cover.jpg').write_bytes(b'scan fixture')
        (source / 'OST').mkdir()
        (source / 'OST/track.flac').write_bytes(b'audio-only fixture')
        if bad_cm:
            cm = next(source.glob('SPs/*CM01*.mkv'))
            cm.write_bytes(b'uninspectable CM container')
        service, plan = await self.pre_activation_movie(root, source, (primary,))
        from src.rename.projection_authority import validate_plan_projection_receipt
        validate_plan_projection_receipt(plan)
        state = service.decision_state_store.load(plan.task_id)
        self.assertTrue(state.initial_source_basis)
        self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(plan).issues})
        self.assertFalse((service.record_root / f'{plan.task_id}.ledger.json').exists())
        return service, plan, source, primary, font

    def prepare(self, service, plan):
        method = getattr(service, 'prepare_default_content_initialization', None)
        self.assertTrue(callable(method), 'missing unique default content initializer')
        return method(plan.task_id, PlanStore.fingerprint(plan))

    def commit(self, service, preview):
        method = getattr(service, 'commit_default_content_initialization', None)
        self.assertTrue(callable(method), 'missing canonical default content commit')
        return method(preview)

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('src.rename.default_content'),
                             'missing shared canonical default initialization builder')
        from src.rename import default_content
        return default_content

    async def test_real_first_pending_bad_cm_valid_primary_and_independent_font_is_reachable(self):
        root = self.root()
        service, plan, source, primary, font = await self.ready(root, bad_cm=True)
        self.assertTrue(primary.is_file())
        self.assertTrue(font.is_file())
        before = records(root)
        with self.assertRaisesRegex((ValueError, RuntimeError), '默认|pending'):
            service.approve_entries(plan.task_id, PlanStore.fingerprint(plan), [entry_id(e) for e in plan.entries])
        self.assertEqual(before, records(root))

    async def test_actual_pending_history_becomes_complete_default_v2_and_stays_unapproved(self):
        root = self.root()
        service, plan, source, primary, font = await self.ready(root)
        before = records(root)
        preview = self.prepare(service, plan)
        self.assertEqual(before, records(root), 'default preview wrote business state')
        revised = self.commit(service, preview)
        state = service.decision_state_store.load(plan.task_id)
        choice = state.content_selection.to_dict()
        self.assertEqual('content-selection-v2', choice['policy'])
        self.assertEqual(PlanStore.fingerprint(plan), choice['origin_plan_fingerprint'])
        self.assertEqual(state.initial_source_basis.fingerprint, choice['initial_source_basis_fingerprint'])
        self.assertEqual({r['source_path'] for r in state.initial_source_basis.to_dict()['sources']},
                         {str(p) for p in revised.expected_sources})
        self.assertEqual({str(p) for p in revised.expected_sources}, {r['source_path'] for r in choice['materials']})
        omitted = set(revised.ignored_sources)
        self.assertIn(source / 'README.txt', omitted)
        self.assertIn(source / 'Scans/deep/cover.jpg', omitted)
        self.assertIn(source / 'OST/track.flac', omitted)
        self.assertNotIn(primary, omitted)
        self.assertNotIn(font, omitted)
        self.assertEqual(set(), {i.code for i in PlanVerifier().verify(revised).issues})
        self.assertFalse(service.load_or_create(revised).approved_entry_ids)
        after = records(root)
        repeated = self.commit(service, self.prepare(service, revised))
        self.assertEqual(PlanStore.fingerprint(revised), PlanStore.fingerprint(repeated))
        self.assertEqual(after, records(root), 'repeat initialization must be idempotent')

    async def test_later_real_revision_invalidates_default_preview_without_overwriting(self):
        root = self.root()
        service, plan, *_ = await self.ready(root)
        preview = self.prepare(service, plan)
        state = service.decision_state_store.load(plan.task_id)
        _, newer = DecisionCommandService(service.decision_state_store, service.plan_store).update(
            plan.task_id, expected_revision=state.revision, changes={}, planner=lambda _: plan,
            task_terminal=service._revision_task_terminal(plan.task_id))
        before = records(root)
        with self.assertRaisesRegex((ValueError, RuntimeError), '变化|过期|revision|预览'):
            self.commit(service, preview)
        self.assertEqual(before, records(root))
        self.assertEqual(PlanStore.fingerprint(newer), PlanStore.fingerprint(service.plan_store.load(plan.task_id)))

    async def test_canonical_rebuild_rejects_forged_origin_material_scope_and_hash(self):
        root = self.root()
        service, plan, *_ = await self.ready(root)
        preview = self.prepare(service, plan)
        module = self.module()
        # Preview carries a typed selection, but canonical must rebuild from
        # actual origin history instead of trusting this self-consistent value.
        for attack in ('origin', 'missing', 'episode', 'hash'):
            with self.subTest(attack=attack):
                value = preview.to_dict()
                selection = value['selection']
                if attack == 'origin':
                    selection['origin_plan_fingerprint'] = '0' * 64
                elif attack == 'missing':
                    selection['materials'].pop()
                elif attack == 'episode':
                    selection['materials'][0]['episode_id'] = 999
                else:
                    self.assertTrue(selection['evidence'])
                    selection['evidence'][0]['sha256'] = '0' * 64
                before = records(root)
                with self.assertRaises((ValueError, RuntimeError)):
                    self.commit(service, type(preview).from_mapping(value))
                self.assertEqual(before, records(root))
        before = records(root)
        with patch.object(module, 'build_default_initialization', return_value=None):
            with self.assertRaises((ValueError, RuntimeError, TypeError)):
                self.commit(service, preview)
        self.assertEqual(before, records(root))


    async def test_default_transaction_interruption_recovers_real_pending_history(self):
        for interruption, window in ((kind, phase) for kind in (RuntimeError, SystemExit)
                                     for phase in ('prepared', 'decision_written', 'ownership_claimed', 'plan_written')):
            with self.subTest(interruption=interruption.__name__, window=window):
                root = self.root()
                service, plan, *_ = await self.ready(root)
                preview = self.prepare(service, plan)
                before = {k: v for k, v in records(root).items() if not k.startswith('projection_journal/')}
                def fail(_service, phase):
                    if phase == window:
                        raise interruption('default journal interruption')
                with patch.object(ProjectionCommitService, '_checkpoint', fail):
                    with self.assertRaises(interruption):
                        self.commit(service, preview)
                fresh = ProjectionCommitService(service.decision_state_store, service.plan_store,
                                                task_root=service.task_root)
                if interruption is SystemExit:
                    self.assertEqual({plan.task_id: 'failed_closed'}, fresh.recover_pending())
                after = records(root)
                self.assertEqual(before, {k: after.get(k) for k in before})
                restored = service.plan_store.load(plan.task_id)
                self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(restored).issues})
                revised = self.commit(service, self.prepare(service, restored))
                self.assertEqual('content-selection-v2', revised.metadata['content_selection']['policy'])


    async def test_canonical_first_install_rebuild_is_required_even_with_valid_preview(self):
        from src.rename.content_selection import ContentSelection
        from src.rename.movie_version_selection import MovieVersionSelection
        root = self.root()
        service, plan, *_ = await self.ready(root)
        preview = self.prepare(service, plan)
        value = preview.to_dict()
        module = self.module()
        state = service.decision_state_store.load(plan.task_id)
        choice = ContentSelection.from_mapping(value['selection'])
        movie = MovieVersionSelection.from_mapping(value['movie_selection']) if value['movie_selection'] else None
        draft = module.build_default_draft(service, plan, state, choice, movie)
        for attack in ('disconnect', 'origin', 'scope', 'episode', 'role', 'review', 'sources'):
            with self.subTest(attack=attack):
                chosen = choice
                if attack in ('origin', 'scope'):
                    altered = choice.to_dict()
                    if attack == 'origin':
                        altered['origin_plan_fingerprint'] = '0' * 64
                    else:
                        altered['initial_source_basis_fingerprint'] = '0' * 64
                    chosen = ContentSelection.from_mapping(altered)
                proposed = replace(candidate, content_selection=chosen)
                raw = replace(draft, metadata={**draft.metadata, 'content_selection': chosen.to_dict(),
                                              'content_selection_fingerprint': chosen.fingerprint})
                if attack in ('episode', 'role', 'review'):
                    entry = next(e for e in raw.entries if e.media_kind == 'video')
                    changes = ({'episode_id': 987} if attack == 'episode' else
                               {'role': 'unknown'} if attack == 'role' else
                               {'requires_review': not entry.requires_review})
                    raw = replace(raw, entries=tuple(replace(e, **changes) if e == entry else e for e in raw.entries))
                elif attack == 'sources':
                    raw = replace(raw, expected_sources=raw.expected_sources[:-1])
                before = records(root)
                from contextlib import nullcontext
                with (patch.object(module, 'build_default_initialization', return_value=None)
                      if attack == 'disconnect' else nullcontext()):
                    with self.assertRaises((ValueError, RuntimeError, TypeError)):
                        commit_projected_plan(service.decision_state_store, service.plan_store,
                            previous=state, candidate=proposed, draft_plan=raw,
                            task_terminal=service._revision_task_terminal(plan.task_id))
                self.assertEqual(before, records(root))

    async def test_source_stat_drift_rejects_but_excluded_same_stat_bytes_are_not_compared(self):
        for same_stat in (False, True):
            with self.subTest(same_stat=same_stat):
                root = self.root()
                service, plan, source, *_ = await self.ready(root)
                preview = self.prepare(service, plan)
                target = source / 'README.txt'
                previous_stat = target.stat()
                target.write_bytes(b'x' * previous_stat.st_size if same_stat else b'changed length')
                if same_stat:
                    os.utime(target, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
                before = records(root)
                if same_stat:
                    revised = self.commit(service, preview)
                    self.assertIn(target, revised.ignored_sources)
                    self.assertNotIn(target, {e.source_path for e in revised.entries})
                    self.assertFalse(service.load_or_create(revised).approved_entry_ids)
                    self.assertFalse((service.record_root / f'{plan.task_id}.ledger.json').exists())
                else:
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.commit(service, preview)
                    self.assertEqual(before, records(root))


    async def test_v2_origin_fields_and_policy_cannot_change_but_manual_choice_still_works(self):
        from src.rename.content_selection import ContentSelection, DEFAULT_FIELDS, POLICY
        from tests.test_content_choice_contract import change
        root = self.root()
        service, plan, source, *_ = await self.ready(root)
        revised = self.commit(service, self.prepare(service, plan))
        state = service.decision_state_store.load(plan.task_id)
        for field in ('initial_source_basis_fingerprint', 'material_origins', 'policy'):
            with self.subTest(field=field):
                value = state.content_selection.to_dict()
                if field == 'policy':
                    value['policy'] = POLICY
                    for key in DEFAULT_FIELDS:
                        value.pop(key)
                elif field == 'material_origins':
                    row = next(r for r in value[field] if r['kind'] == 'actual_plan_history')
                    row['plan_fingerprint'] = '0' * 64
                else:
                    value[field] = '0' * 64
                chosen = ContentSelection.from_mapping(value)
                before = records(root)
                with self.assertRaises((ValueError, RuntimeError)):
                    DecisionCommandService(service.decision_state_store, service.plan_store).update(
                        plan.task_id, expected_revision=state.revision, changes={'content_selection': chosen},
                        planner=lambda _: replace(revised, metadata={**revised.metadata,
                            'content_selection': chosen.to_dict(), 'content_selection_fingerprint': chosen.fingerprint}),
                        task_terminal=service._revision_task_terminal(plan.task_id))
                self.assertEqual(before, records(root))
        revised = change(service, revised, [source / 'README.txt'], action='restore')
        self.assertNotIn(source / 'README.txt', revised.ignored_sources)
        self.assertFalse(service.load_or_create(revised).approved_entry_ids)
        audio = next(e.source_path for e in revised.entries if e.source_path.suffix == '.mka')
        removed = change(service, revised, [audio], action='exclude')
        self.assertIn(audio, removed.ignored_sources)
        restored = change(service, removed, [audio], action='restore')
        self.assertNotIn(audio, restored.ignored_sources)
        self.assertIn(audio, {e.source_path for e in restored.entries})
        self.assertEqual(state.content_selection.to_dict()['material_origins'],
                         service.decision_state_store.load(plan.task_id).content_selection.to_dict()['material_origins'])

    async def test_real_tv_producer_defaults_preserve_typed_episode_and_font_inputs(self):
        from types import SimpleNamespace
        from tests import test_r1_real_media_production_red_contract as queue_fixture
        from src.rename import process
        from src.rename.plan_review import PlanReviewService
        from src.rename.selection_gate import validate_selection_for_plan
        root = self.root()
        source = queue_fixture._make_season(root / 'sources',
            '[VCB-Studio] Yama no Susume [Ma10p_1080p]', '[VCB-Studio] Yama no Susume', ova=True)
        for video in source.glob('*.mkv'):
            video.rename(video.with_suffix('.mp4'))
        (source / 'README.txt').write_text('documentation', encoding='utf-8')
        harness = SimpleNamespace(_NoopReviewDialog=queue_fixture.R1RealQueueWorkerContracts._NoopReviewDialog)
        with (patch.object(process, 'SERIES_CATALOG_PATH', root / 'series_catalog'),
              patch.object(process, 'RECORD_PATH', root / 'records'),
              patch.object(queue_fixture._FrozenSearch, 'get_season_info', return_value={
                  'season_number': 0, 'episodes': ({'id': 999, 'episode_number': 1, 'name': 'OVA'},)})):
            result = await queue_fixture.R1RealQueueWorkerContracts._run_flow(harness, root=root, source=source,
                task_id='default-tv', candidate=queue_fixture._candidate('41658'), is_anime=True, is_movie=False)
        validate_selection_for_plan(result.task)
        service = PlanReviewService(result.plan_store, root / 'reviews', root / 'work_catalog',
            decision_state_root=root / 'decisions', task_root=root / 'tasks', record_root=root / 'records')
        special = next(e for e in result.plan.entries if '(OVA)' in e.source_path.name)
        candidates = result.plan.metadata['provider_special_candidates'][entry_id(special)]
        self.assertTrue(candidates)
        from src.rename.initial_movie_completion import RootSnapshotSource
        from src.rename.target_routing import TargetRootSnapshot
        roots = TargetRootSnapshot.capture({'bangumi_path': root/'TV', 'movie_path': root/'Movies',
            'anime_path': root/'Anime', 'anime_movie_path': root/'AnimeMovies'})
        service.root_snapshot_source = RootSnapshotSource(lambda: roots)
        state = service.decision_state_store.load(result.plan.task_id)
        result.plan = service.revise_tv_content(result.plan.task_id, PlanStore.fingerprint(result.plan),
            expected_revision=state.revision, mode='special', edit={'entry_id': entry_id(special),
                'action': 'confirmed_official_special', 'provider_episode_id': candidates[0]['provider_episode_id']})
        before = {e.source_path: (e.season_id, e.episode_id) for e in result.plan.entries if e.media_kind == 'video'}
        revised = self.commit(service, self.prepare(service, result.plan))
        self.assertEqual(before, {e.source_path: (e.season_id, e.episode_id) for e in revised.entries if e.media_kind == 'video'})
        self.assertIn(source / 'README.txt', revised.ignored_sources)
        self.assertNotIn('font_contribution', revised.metadata)
        self.assertEqual(PlanStore.fingerprint(result.plan), PlanStore.fingerprint(revised))

    async def test_confirmed_movie_version_overrides_misc_name_and_archive_stays_excluded(self):
        root = self.root()
        service, plan, _, primary, font = await self.ready(root, tagged_primary=True)
        self.assertIn('[CM]', primary.name)
        revised = self.commit(service, self.prepare(service, plan))
        self.assertNotIn(primary, revised.ignored_sources)
        self.assertIn(primary, {e.source_path for e in revised.entries})
        self.assertIn(font, revised.ignored_sources)

    async def manually_excluded_version(self, root):
        original, old_primary, _ = source_fixture._make_version(root / 'sources', '1080p')
        source = original.parent / 'Gekijouban Violet Evergarden'
        self.assertTrue(original.resolve().is_relative_to(root.resolve()))
        self.assertTrue(source.resolve().is_relative_to(root.resolve()))
        original.rename(source)
        primary = source / old_primary.name
        second = source / primary.name.replace('1080p', '2160p')
        shutil.copyfile(primary, second)
        service, plan = await self.pre_activation_movie(root, source, (primary, second))
        groups = service.decision_state_store.load(plan.task_id).movie_version_selection.to_dict()['groups']
        self.assertEqual(2, len(groups))
        kept = next(g for g in groups if str(primary) in g['sources'])
        changed = service.revise_movie_versions(plan.task_id, PlanStore.fingerprint(plan),
            groups=[dict(sources=kept['sources'], label=kept['label'], technical_descriptor=kept['descriptor'])],
            excluded_sources=[str(second)])
        state = service.decision_state_store.load(plan.task_id)
        self.assertIsNone(state.content_selection)
        self.assertIn(str(second), state.movie_version_selection.to_dict()['excluded_sources'])
        self.assertIn(second, changed.ignored_sources)
        self.assertNotIn(second, {e.source_path for e in changed.entries})
        return service, changed, second

    async def test_real_manual_version_exclusion_survives_default_as_complete_version_authority(self):
        root = self.root()
        service, plan, second = await self.manually_excluded_version(root)
        revised = self.commit(service, self.prepare(service, plan))
        state = service.decision_state_store.load(plan.task_id)
        self.assertIn(second, revised.ignored_sources)
        self.assertNotIn(second, {e.source_path for e in revised.entries})
        self.assertIn(str(second), state.movie_version_selection.to_dict()['excluded_sources'])
        choice = state.content_selection.to_dict()
        self.assertNotIn(str(second), choice['excluded_sources'], 'complete version must not become ordinary miscellaneous exclusion')
        self.assertTrue(any(str(second) in g['sources'] for g in choice['version_groups']))
        from tests.test_content_choice_contract import change
        restored = change(service, revised, [second], action='restore')
        self.assertIn(second, {e.source_path for e in restored.entries})
        self.assertEqual(2, len(service.decision_state_store.load(plan.task_id).movie_version_selection.to_dict()['groups']))

    async def test_missing_actual_version_history_refuses_default_without_writes(self):
        from src.rename.plan_store import PlanStoreError
        root = self.root()
        service, plan, _ = await self.manually_excluded_version(root)
        load = service.plan_store.load_version
        target = plan.revision_parent_fingerprint
        seen = []
        def missing(task_id, fingerprint):
            if fingerprint == target:
                seen.append(fingerprint)
                raise PlanStoreError('required movie selection history missing')
            return load(task_id, fingerprint)
        before = records(root)
        with patch.object(service.plan_store, 'load_version', missing):
            with self.assertRaises((ValueError, RuntimeError)):
                self.prepare(service, plan)
        self.assertTrue(seen, 'actual parent history was never consumed')
        self.assertEqual(before, records(root))

    async def test_version_history_required_at_public_and_canonical_boundary(self):
        from src.rename.content_selection import ContentSelection
        from src.rename.movie_version_selection import MovieVersionSelection
        from src.rename.plan_store import PlanStoreError
        root = self.root()
        service, plan, _ = await self.manually_excluded_version(root)
        module = self.module()
        preview = self.prepare(service, plan).to_dict()
        state = service.decision_state_store.load(plan.task_id)
        choice = ContentSelection.from_mapping(preview['selection'])
        movie = MovieVersionSelection.from_mapping(preview['movie_selection'])
        draft = module.build_default_draft(service, plan, state, choice, movie)
        load = service.plan_store.load_version
        def missing(task_id, fingerprint):
            if fingerprint == plan.revision_parent_fingerprint:
                raise PlanStoreError('required actual history absent')
            return load(task_id, fingerprint)
        for attack in ('disconnect', 'missing'):
            for boundary in ('public', 'canonical'):
                with self.subTest(attack=attack, boundary=boundary):
                    before = records(root)
                    with (patch.object(module, 'initial_movie_history', return_value=None)
                          if attack == 'disconnect' else patch.object(service.plan_store, 'load_version', missing)) as gate:
                        with self.assertRaises((ValueError, RuntimeError)):
                            if boundary == 'public':
                                self.prepare(service, plan)
                            else:
                                commit_projected_plan(service.decision_state_store, service.plan_store,
                                    previous=state, candidate=candidate, draft_plan=draft,
                                    task_terminal=service._revision_task_terminal(plan.task_id))
                        if attack == 'disconnect':
                            self.assertTrue(gate.called)
                    self.assertEqual(before, records(root))

    async def test_manual_version_exclusion_survives_later_unrelated_real_revision(self):
        root = self.root()
        service, plan, second = await self.manually_excluded_version(root)
        state = service.decision_state_store.load(plan.task_id)
        draft = replace(plan, revision_parent_fingerprint=PlanStore.fingerprint(plan),
                        revision_action='review_note', revision_reason='isolated unrelated revision')
        _, newer = DecisionCommandService(service.decision_state_store, service.plan_store).update(
            plan.task_id, expected_revision=state.revision, changes={}, planner=lambda _: draft,
            task_terminal=service._revision_task_terminal(plan.task_id))
        self.assertEqual('review_note', newer.revision_action)
        revised = self.commit(service, self.prepare(service, newer))
        self.assertIn(second, revised.ignored_sources)
        self.assertNotIn(second, {e.source_path for e in revised.entries})


class DefaultSourceCompletenessContracts(unittest.IsolatedAsyncioTestCase):
    root = policy_fixture.ContentPolicyContracts.root
    manager = policy_fixture.ContentPolicyContracts.manager
    request = policy_fixture.ContentPolicyContracts.request
    admission = policy_fixture.ContentPolicyContracts.admission
    pending = policy_fixture.ContentPolicyContracts.pending
    assert_policy = policy_fixture.ContentPolicyContracts.assert_policy
    prepare = DefaultInitializationContracts.prepare
    commit = DefaultInitializationContracts.commit

    def sources(self, root):
        source = root / 'source'
        source.mkdir()
        for name in ('Movie.mkv', 'Unlisted.mkv', 'Unlisted.ass', 'Fonts.ttf', 'README.txt'):
            (source / name).write_bytes(b'isolated unmodeled source')
        return source

    async def test_first_producer_omission_is_rejected_before_publishing_history(self):
        original = policy_fixture.commit_recognition_projection
        def incomplete_producer(states, plans, recognition, draft, **kwargs):
            # Reproduce a producer omission BEFORE the first real canonical
            # history exists. No fabricated Decision, basis or origin receipt.
            kept = tuple(e for e in draft.entries if e.source_path.name == 'Movie.mkv')
            raw = replace(draft, entries=kept, expected_sources=tuple(e.source_path for e in kept), ignored_sources=())
            return original(states, plans, recognition, raw, **kwargs)
        root = self.root()
        with patch.object(policy_fixture, 'commit_recognition_projection', incomplete_producer):
            with self.assertRaisesRegex((ValueError, RuntimeError), '覆盖|完整|来源'):
                await self.pending(root)
        self.assertFalse(list((root / 'plans').rglob('*.json')), 'no current or historical Plan may be published')
        self.assertFalse(list((root / 'decisions').glob('*.json')))
        self.assertEqual(5, len(list((root / 'source').iterdir())))
        self.assertTrue(all(p.read_bytes() == b'isolated unmodeled source' for p in (root / 'source').iterdir()))

    async def test_complete_first_pending_preserves_all_sources_without_font_approval(self):
        root = self.root()
        _, service, _, plan = await self.pending(root)
        revised = self.commit(service, self.prepare(service, plan))
        state = service.decision_state_store.load(plan.task_id)
        value = state.content_selection.to_dict()
        material = {Path(row['source_path']).name: row for row in value['materials']}
        origins = {Path(row['source_path']).name: row for row in value['material_origins']}
        self.assertEqual({'Movie.mkv', 'Unlisted.mkv', 'Unlisted.ass', 'Fonts.ttf', 'README.txt'}, set(material))
        self.assertEqual('actual_plan_history', origins['Movie.mkv']['kind'])
        for name in ('Unlisted.mkv', 'Unlisted.ass', 'Fonts.ttf'):
            self.assertEqual('actual_plan_history', origins[name]['kind'])
            self.assertEqual(PlanStore.fingerprint(plan), origins[name]['plan_fingerprint'])
            original_material = next(e.to_dict() for e in plan.entries if e.source_path.name == name)
            for field in ('episode_id', 'season_id', 'subtitle', 'presentation_asset'):
                self.assertEqual(original_material[field], material[name][field])
            self.assertTrue(material[name]['requires_review'])
            self.assertNotIn(root / 'source' / name, revised.ignored_sources)
        self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(revised).issues})
        self.assertNotIn('font_contribution', revised.metadata)
        self.assertFalse(service.load_or_create(revised).approved_entry_ids)
