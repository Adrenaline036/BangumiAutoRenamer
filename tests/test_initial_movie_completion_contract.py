"""First complete movie groups after real v2 selection, disposable only.

No policy override, legacy admission, real Provider transport or user media.
"""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.projection_authority import ProjectionCommitService
from tests import test_default_movie_completion as first_fixture
from tests import _media_flow_helpers as source_fixture
from tests.test_content_choice_contract import change, records


class InitialMovieFullCompletionContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = first_fixture.InitialMovieCompletionContracts('runTest')
        self.addCleanup(self.h.doCleanups)

    async def fixture(self, *, multiple=False):
        root = self.h.root()
        original = source_fixture._make_version
        created = {}
        def sources(*args, **kwargs):
            source, primary, font = original(*args, **kwargs)
            # Seed before admission/basis capture, not by refreshing authority.
            subtitle = source / 'Unrelated.sc.ass'
            subtitle.write_text('[Script Info]\nScriptType: v4.00+\n', encoding='utf-8')
            second = primary.with_name('Movie Alternate [1080p].mkv')
            if multiple:
                second.write_bytes(primary.read_bytes() + b' second disposable version')
            created.update(subtitle=subtitle, second=second if multiple else None, font=font)
            return source, primary, font
        with patch.object(source_fixture, '_make_version', side_effect=sources):
            service, result, prior, primary = await self.h.fixture(root, initialize_before_selection=True)
        plan = service.plan_store.load(result.plan.task_id)
        from src.rename.initial_movie_completion import RootSnapshotSource
        from src.rename.target_routing import ROOT_KEYS, TargetRootSnapshot
        service.root_snapshot_source = RootSnapshotSource(lambda: TargetRootSnapshot.capture(
            {key: root / 'isolated-targets' / key for key in ROOT_KEYS}))
        self.assertTrue(service.decision_state_store.load(plan.task_id).confirmed_work)
        self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(plan).issues})
        return root, service, plan, primary, created

    def prepare(self, service, plan, groups, excluded=()):
        method = getattr(service, 'prepare_initial_movie_completion', None)
        self.assertTrue(callable(method), 'missing typed first complete movie command')
        return method(plan.task_id, PlanStore.fingerprint(plan), groups=groups,
            excluded_sources=list(map(str, excluded)), reason='明确确认首次完整电影组')

    async def test_routing_comes_from_current_service_not_missing_or_forged_history(self):
        from src.rename.initial_movie_completion import RootSnapshotSource
        from src.rename.target_routing import ROOT_KEYS, TargetRootSnapshot
        root, service, plan, primary, _ = await self.fixture()
        self.assertNotIn('target_root_snapshot', plan.metadata)
        self.assertFalse(plan.metadata.get('draft_root_snapshot'))
        before = records(root)
        configured = service.root_snapshot_source
        for bad in (None, object(), RootSnapshotSource(lambda: {})):
            with self.subTest(kind=type(bad).__name__), patch.object(service, 'root_snapshot_source', bad):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
            self.assertEqual(before, records(root))
        intent = self.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
        other_roots = TargetRootSnapshot.capture({key: root / 'other-targets' / key for key in ROOT_KEYS})
        for fault in ('configuration', 'forged_input'):
            with self.subTest(fault=fault):
                if fault == 'configuration':
                    # Same source object, changed observed configuration.
                    with patch.object(RootSnapshotSource, 'observe', return_value=other_roots):
                        with self.assertRaises((ValueError, RuntimeError)):
                            service.commit_initial_movie_completion(intent)
                else:
                    forged = replace(intent, inputs=replace(intent.inputs, roots=other_roots))
                    with self.assertRaises((ValueError, RuntimeError)):
                        service.commit_initial_movie_completion(forged)
                self.assertEqual(before, records(root))
        self.assertIs(configured, service.root_snapshot_source)

    async def test_complete_builder_is_pure_and_whole_segment_exclusion_is_required(self):
        root, service, plan, primary, sources = await self.fixture(multiple=True)
        members = [str(primary), str(sources['second'])]
        before = records(root)
        with self.assertRaises((ValueError, RuntimeError)):
            self.prepare(service, plan, [dict(sources=members, label='上下篇')], excluded=[primary])
        self.assertEqual(before, records(root))
        intent = self.prepare(service, plan, [dict(sources=members, label='上下篇')])
        state = service.decision_state_store.load(plan.task_id)
        module = self.module()
        expected = module.build_initial_movie_completion(plan, state, intent)
        with patch.object(Path, 'stat', side_effect=AssertionError('pure completion read filesystem')), \
                patch.object(Path, 'lstat', side_effect=AssertionError('pure completion read filesystem')), \
                patch.object(Path, 'open', side_effect=AssertionError('pure completion read filesystem')):
            self.assertEqual(expected, module.build_initial_movie_completion(plan, state, intent))
        self.assertEqual(before, records(root))

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('src.rename.initial_movie_completion'),
                             'missing first movie completion reconstruction')
        from src.rename import initial_movie_completion
        return initial_movie_completion

    async def test_completion_requires_successful_original_selection_before_preview(self):
        for mutation in ({'request_id': 'later-same-provider-request'}, {'state': 'pending'},
                         {'state': 'failed'}, {'result_decision_revision': 999}):
            with self.subTest(mutation=mutation):
                root, service, plan, primary, _ = await self.fixture()
                self.h.replace_request(root / 'tasks' / f'{plan.task_id}.json', **mutation)
                before = records(root)
                with self.assertRaises((ValueError, RuntimeError)):
                    self.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
                self.assertEqual(before, records(root))

    async def test_required_root_observation_and_actual_work_key_cannot_be_replaced(self):
        from src.rename.collision_projection import CollisionProbes, observe_path
        root, service, plan, primary, _ = await self.fixture()
        intent = self.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
        before = records(root)
        for fault in ('empty', 'unrelated', 'work_key'):
            with self.subTest(fault=fault):
                altered = (replace(intent, root_probes=CollisionProbes(())) if fault == 'empty' else
                    replace(intent, root_probes=CollisionProbes((observe_path(root / 'unrelated'),)))
                    if fault == 'unrelated' else replace(intent, inputs=replace(intent.inputs, work_key='work-other-valid')))
                with self.assertRaises((ValueError, RuntimeError)):
                    service.commit_initial_movie_completion(altered)
                self.assertEqual(before, records(root))

    async def test_audio_collision_preserves_playable_sibling_or_explicit_conflict(self):
        for identical in (False, True):
            with self.subTest(identical=identical):
                root, service, plan, primary, _ = await self.fixture()
                groups = [dict(sources=[str(primary)], label='1080p')]
                preview = self.prepare(service, plan, groups)
                built = self.module().build_initial_movie_completion(
                    plan, service.decision_state_store.load(plan.task_id), preview)
                audio = next(e for e in built.draft.entries if e.role == 'external_audio')
                audio.target_path.parent.mkdir(parents=True, exist_ok=True)
                audio.target_path.write_bytes(audio.source_path.read_bytes() if identical else b'conflicting audio')
                intent = self.prepare(service, plan, groups)
                expected = self.module().build_initial_movie_completion(
                    plan, service.decision_state_store.load(plan.task_id), intent)
                projected = next(e for e in expected.draft.entries if e.source_path == audio.source_path)
                self.assertEqual(audio.target_path, projected.target_path, 'external audio must remain a video sibling')
                # Ordinary review does not read whole tracks to decide reuse.
                # Same-name targets remain explicit conflicts, even if this fixture knows the bytes match.
                self.assertIsNone(projected.collision_resolution)
                self.assertIn(audio.source_path.name, expected.draft.metadata['asset_collision_unresolved'])
                completed = service.commit_initial_movie_completion(intent)
                final_audio = next(e for e in completed.entries if e.source_path == audio.source_path)
                self.assertEqual(projected, final_audio, 'canonical re-projection must not undo collision semantics')

    async def test_selection_result_is_receipted_and_cannot_be_forged_by_other_writers(self):
        from src.rename.projection_authority import FinalPlanProjector, validate_plan_projection_receipt
        from src.rename.decision_state import DecisionCommandService
        root, service, plan, primary, _ = await self.fixture()
        state = service.decision_state_store.load(plan.task_id)
        before = records(root)
        for fault in ('missing', 'changed'):
            with self.subTest(fault=fault):
                metadata = dict(plan.metadata)
                proof = dict(metadata.pop('initial_movie_selection_result'))
                if fault == 'changed':
                    metadata['initial_movie_selection_result'] = {**proof, 'request_id': 'forged-successful-request'}
                draft = replace(plan, metadata=metadata)
                with self.assertRaises((ValueError, RuntimeError)):
                    validate_plan_projection_receipt(draft)
                with self.assertRaises((ValueError, RuntimeError)):
                    FinalPlanProjector.revise(plan, draft)
                command = DecisionCommandService(service.decision_state_store, service.plan_store)
                with self.assertRaises((ValueError, RuntimeError)):
                    command.update(plan.task_id, expected_revision=state.revision, changes={}, planner=lambda _: draft)
                self.assertEqual(before, records(root))
        with patch.object(service.plan_store, 'load_version', side_effect=ValueError('missing actual history')):
            with self.assertRaises((ValueError, RuntimeError)):
                self.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
        self.assertEqual(before, records(root))

    async def test_disambiguated_root_cannot_be_reobserved_as_existing(self):
        from src.rename.collision_projection import CollisionProbes, observe_path
        root, service, plan, primary, _ = await self.fixture()
        other_work = replace(plan.work_identity, primary_provider=replace(
            plan.work_identity.primary_provider, provider_id='999001'))
        await self.register_historical_work(root, service, replace(plan, work_identity=other_work))
        from src.rename.work_affiliation import AffiliationChoice, namespace_fingerprint
        with self.module()._namespace(service, plan) as (_, namespace):
            separate = AffiliationChoice('separate', '', namespace_fingerprint(namespace), '确认同名但属于另一作品')
        intent = self.module().prepare(service, plan.task_id, PlanStore.fingerprint(plan),
            groups=[dict(sources=[str(primary)], label='1080p')], reason='明确另建作品', affiliation_choice=separate)
        _target, destination, _naming, _record, needs_absence, _affiliation = self.module()._routing(plan, intent.inputs)
        self.assertTrue(needs_absence)
        destination.mkdir(parents=True)
        before = records(root)
        for refreshed in (False, True):
            with self.subTest(refreshed=refreshed):
                altered = replace(intent, root_probes=CollisionProbes((observe_path(destination),))) if refreshed else intent
                with self.assertRaises((ValueError, RuntimeError)):
                    service.commit_initial_movie_completion(altered)
                self.assertEqual(before, records(root))

    async def test_actual_single_version_completes_one_revision_and_keeps_pending(self):
        root, service, plan, primary, _ = await self.fixture()
        old = service.decision_state_store.load(plan.task_id)
        before = records(root)
        intent = self.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
        self.assertEqual(before, records(root), 'preview must not write business records')
        completed = service.commit_initial_movie_completion(intent)
        state = service.decision_state_store.load(plan.task_id)
        self.assertEqual(old.revision + 1, state.revision)
        self.assertEqual('movie_versions', state.layout_mode)
        self.assertEqual(PlanStore.fingerprint(plan), completed.revision_parent_fingerprint)
        self.assertIsNotNone(state.movie_version_selection)
        self.assertNotIn('font_contribution', completed.metadata)
        for field in ('origin_plan_fingerprint', 'initial_source_basis_fingerprint',
                      'material_origins', 'identities'):
            self.assertEqual(old.content_selection.to_dict()[field], state.content_selection.to_dict()[field])
        self.assertEqual(old.initial_source_basis, state.initial_source_basis)
        self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(completed).issues})
        self.assertFalse(tuple(service.work_catalog.root.glob('work-*.json')))
        self.assertFalse(any(e.target_path.exists() for e in completed.entries))

    async def test_versions_segments_and_independent_exclusions_keep_complete_materials(self):
        for segmented in (False, True):
            with self.subTest(segmented=segmented):
                root, service, plan, primary, sources = await self.fixture(multiple=True)
                plan = change(service, plan, [sources['subtitle']])
                state = service.decision_state_store.load(plan.task_id)
                excluded_before = set(state.content_selection.to_dict()['excluded_sources'])
                members = [str(primary), str(sources['second'])]
                groups = [dict(sources=members, label='完整上下篇')] if segmented else [
                    dict(sources=[p], label=label) for p, label in zip(members, ('1080p - A', '1080p - B'))]
                completed = service.commit_initial_movie_completion(self.prepare(service, plan, groups))
                state = service.decision_state_store.load(plan.task_id)
                selected = state.movie_version_selection.to_dict()['groups']
                self.assertEqual(1 if segmented else 2, len(selected))
                self.assertEqual(set(members), {p for group in selected for p in group['sources']})
                self.assertTrue(excluded_before <= set(state.content_selection.to_dict()['excluded_sources']))
                self.assertTrue({str(sources['subtitle']), str(plan.source_root / 'README.txt')} <= set(map(str, completed.ignored_sources)))
                self.assertEqual(len(completed.expected_sources), len(state.content_selection.to_dict()['materials']))

    async def test_hidden_video_cannot_be_silently_restored_as_first_complete_group(self):
        root, service, plan, primary, sources = await self.fixture(multiple=True)
        plan = change(service, plan, [sources['second']])
        before = records(root)
        with self.assertRaisesRegex((ValueError, RuntimeError), '排除|确认|恢复'):
            self.prepare(service, plan, [dict(sources=[str(primary), str(sources['second'])], label='不应恢复')])
        self.assertEqual(before, records(root))

    async def test_existing_manual_subtitle_rule_is_preserved_by_first_completion(self):
        from tests.test_content_subtitle_lifecycle import apply, subtitle
        root, service, plan, primary, sources = await self.fixture()
        # This is a real public edit on a pending, unapproved task.
        plan = apply(service, plan, sources['subtitle'], owner=primary)
        contract = subtitle(plan, sources['subtitle'])
        rules = plan.subtitle_override_rules
        completed = service.commit_initial_movie_completion(self.prepare(service, plan,
            [dict(sources=[str(primary)], label='首次电影')]))
        self.assertEqual(contract, subtitle(completed, sources['subtitle']))
        self.assertEqual(rules, completed.subtitle_override_rules)

    async def test_preview_rejects_current_operation_source_and_catalog_drift(self):
        for fault in ('current', 'operation', 'catalog_corrupt'):
            with self.subTest(fault=fault):
                root, service, plan, primary, _ = await self.fixture()
                intent = self.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
                if fault == 'current':
                    from src.rename.decision_state import DecisionCommandService
                    state = service.decision_state_store.load(plan.task_id)
                    DecisionCommandService(service.decision_state_store, service.plan_store).update(
                        plan.task_id, expected_revision=state.revision, changes={}, planner=lambda _: plan,
                        task_terminal=service._revision_task_terminal(plan.task_id))
                elif fault == 'operation':
                    self.h.replace_request(root / 'tasks' / f'{plan.task_id}.json', request_id='later-request')
                else:
                    service.work_catalog.root.mkdir(parents=True, exist_ok=True)
                    (service.work_catalog.root / 'work-92345678-1234-1234-1234-123456789abc.json').write_text('{', encoding='utf-8')
                before = records(root)
                with self.assertRaises((ValueError, RuntimeError)):
                    service.commit_initial_movie_completion(intent)
                self.assertEqual(before, records(root))

    async def test_disconnected_builder_cannot_write_new_revision(self):
        root, service, plan, primary, _ = await self.fixture()
        intent = self.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
        module = self.module()
        before = records(root)
        with patch.object(module, 'build_initial_movie_completion', return_value=None):
            with self.assertRaises((ValueError, RuntimeError)):
                service.commit_initial_movie_completion(intent)
        self.assertEqual(before, records(root))

    async def test_first_completion_journal_failure_restores_actual_parent(self):
        for phase in ('prepared', 'decision_written', 'ownership_claimed', 'plan_written'):
            with self.subTest(phase=phase):
                root, service, plan, primary, _ = await self.fixture()
                intent = self.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
                before = {k: v for k, v in records(root).items() if not k.startswith('projection_journal/')}
                reached = []
                def crash(_transaction, actual):
                    if actual == phase:
                        reached.append(actual)
                        raise SystemExit('first completion crash fixture')
                with patch.object(ProjectionCommitService, '_checkpoint', crash):
                    with self.assertRaises(SystemExit):
                        service.commit_initial_movie_completion(intent)
                self.assertEqual([phase], reached)
                fresh = ProjectionCommitService(service.decision_state_store, service.plan_store, task_root=root / 'tasks')
                self.assertEqual({plan.task_id: 'failed_closed'}, fresh.recover_pending())
                after = records(root)
                self.assertEqual(before, {k: after.get(k) for k in before})

    async def test_pending_source_edits_do_not_observe_fonts_or_unlock_media(self):
        from tests.test_content_subtitle_lifecycle import apply, subtitle
        root, service, plan, primary, sources = await self.fixture()
        changed = change(service, plan, [sources['subtitle']])
        restored = change(service, changed, [sources['subtitle']], action='restore')
        revised = apply(service, restored, sources['subtitle'], owner=primary)
        self.assertEqual('en', subtitle(revised, sources['subtitle']).primary_language)
        reverted = service.revert_subtitle_rule(revised.task_id, PlanStore.fingerprint(revised),
            revised.subtitle_override_rules[-1].rule_id, reason='撤销本次初始字幕规则')
        state = service.decision_state_store.load(plan.task_id)
        self.assertNotIn('font_contribution', reverted.metadata)
        self.assertEqual((), reverted.subtitle_override_rules)
        self.assertIn('content_policy_pending', {i.code for i in PlanVerifier().verify(reverted).issues})
        from src.rename.plan_review import entry_id
        with self.assertRaisesRegex((ValueError, RuntimeError), 'content_policy_pending'):
            service.approve_entries(reverted.task_id, PlanStore.fingerprint(reverted), [entry_id(e) for e in reverted.entries])
        self.assertFalse(any(e.target_path.exists() for e in reverted.entries))

    async def test_unfinished_subtitle_canonical_rejects_unselected_targets_and_rule_forgery(self):
        from tests.test_content_subtitle_lifecycle import apply
        from src.rename.decision_state import DecisionCommandService
        for fault in ('other_subtitle', 'video', 'outside', 'owner', 'missing_rule', 'bad_rule'):
            with self.subTest(fault=fault):
                root, service, plan, primary, sources = await self.fixture()
                original = ProjectionCommitService._commit_locked
                reached = []
                def corrupt(transaction, envelope, **kwargs):
                    draft = envelope.draft_plan
                    reached.append(True)
                    if fault in ('missing_rule', 'bad_rule'):
                        draft = replace(draft, subtitle_override_rules=() if fault == 'missing_rule' else (
                            replace(draft.subtitle_override_rules[0], rule_fingerprint='0' * 64),))
                    else:
                        chosen = next(entry for entry in draft.entries if (
                            entry.media_kind == 'subtitle' and entry.source_path != sources['subtitle']
                            if fault == 'other_subtitle' else entry.source_path == (
                                primary if fault == 'video' else sources['subtitle'])))
                        altered = replace(chosen, subtitle=replace(chosen.subtitle,
                            associated_video_source_path=root / 'outside-owner.mkv')) if fault == 'owner' else replace(
                                chosen, target_path=root / 'outside' / 'injected.ass')
                        draft = replace(draft, entries=tuple(altered if entry is chosen else entry for entry in draft.entries))
                    return original(transaction, replace(envelope, draft_plan=draft), **kwargs)
                before = records(root)
                with patch.object(ProjectionCommitService, '_commit_locked', corrupt):
                    with self.assertRaises((ValueError, RuntimeError)):
                        apply(service, plan, sources['subtitle'], owner=primary)
                self.assertTrue(reached)
                self.assertEqual(before, records(root))

    async def test_unfinished_subtitle_preserves_unselected_materials_and_actual_task_risk_blocks(self):
        from tests.test_content_subtitle_lifecycle import apply
        root, service, plan, primary, sources = await self.fixture()
        old = service.decision_state_store.load(plan.task_id).content_selection.to_dict()
        old_entries = {entry.source_path: entry for entry in plan.entries}
        revised = apply(service, plan, sources['subtitle'], owner=primary)
        new = service.decision_state_store.load(plan.task_id).content_selection.to_dict()
        self.assertEqual([row for row in old['materials'] if row['source_path'] != str(sources['subtitle'])],
                         [row for row in new['materials'] if row['source_path'] != str(sources['subtitle'])])
        self.assertTrue(all(entry == old_entries[entry.source_path] for entry in revised.entries
                            if entry.source_path != sources['subtitle']))
        task_path = service.task_root / f'{plan.task_id}.json'
        raw = task_path.read_bytes()
        for risk in ({'workflow_status': 'executing'}, {'execution_may_have_written_targets': True},
                     {'execution_phase': 'font_processing'}, {'execution_requires_manual_check': True}):
            with self.subTest(risk=next(iter(risk))):
                payload = json.loads(raw)
                payload.update(risk)
                task_path.write_text(json.dumps(payload), encoding='utf-8')
                before = records(root)
                with self.assertRaisesRegex((ValueError, RuntimeError), '实际任务已有执行或恢复风险'):
                    apply(service, revised, sources['subtitle'], owner=primary)
                self.assertEqual(before, records(root))

    async def test_bad_basis_and_implicit_completion_cannot_use_source_edit_branch(self):
        from src.rename.decision_state import DecisionCommandService
        for fault in ('layout', 'basis', 'work'):
            with self.subTest(fault=fault):
                root, service, plan, primary, sources = await self.fixture()
                original = ProjectionCommitService._commit_locked
                reached = []
                def corrupt(transaction, envelope, **kwargs):
                    draft = envelope.draft_plan
                    reached.append(True)
                    if fault == 'layout':
                        draft = replace(draft, presentation_groups=())
                    elif fault == 'basis':
                        envelope = replace(envelope, decision=replace(envelope.decision, initial_source_basis=None))
                    else:
                        envelope = replace(envelope, decision=replace(envelope.decision, confirmed_work={**dict(envelope.decision.confirmed_work), 'title': 'changed work'}))
                    return original(transaction, replace(envelope, draft_plan=draft), **kwargs)
                before = records(root)
                with patch.object(ProjectionCommitService, '_commit_locked', corrupt):
                    with self.assertRaises((ValueError, RuntimeError)):
                        change(service, plan, [sources['subtitle']])
                self.assertTrue(reached, 'must reach canonical candidate rather than an earlier observer failure')
                self.assertEqual(before, records(root))

    async def test_ignored_archive_is_not_reopened_but_target_appearance_rejects(self):
        from tests.test_content_lifecycle_contract import alter_same_stat
        from src.rename.canonical_naming import decision_from_work_identity, build_canonical_work_root
        from src.rename.target_routing import TargetRootSnapshot, resolve_target_root
        for fault in ('font', 'target'):
            with self.subTest(fault=fault):
                root, service, plan, primary, sources = await self.fixture()
                intent = self.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
                if fault == 'font':
                    alter_same_stat(sources['font'])
                else:
                    snapshot = service.root_snapshot_source.observe()
                    directory = build_canonical_work_root(resolved_target_root=resolve_target_root(
                        plan.work_identity.domain, 'movie_continuous', snapshot),
                        decision=decision_from_work_identity(plan.work_identity), year=plan.work_identity.year)
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / f'{directory.name} - 1080p.mkv').write_bytes(b'foreign target after preview')
                before = records(root)
                if fault=='font':
                    from tests.test_inventory_reuse import source_access
                    with source_access(sources['font']):
                        changed=service.commit_initial_movie_completion(intent)
                    self.assertEqual(plan.task_id,changed.task_id)
                    self.assertNotIn('font_contribution', changed.metadata)
                else:
                    with self.assertRaises((ValueError, RuntimeError)):
                        service.commit_initial_movie_completion(intent)
                    self.assertEqual(before, records(root))

    async def test_wrong_service_roots_and_invalid_source_groups_are_rejected(self):
        root, service, plan, primary, sources = await self.fixture(multiple=True)
        before = records(root)
        for paths in ([str(primary), str(primary)], [str(root / 'outside.mkv')]):
            with self.subTest(paths=len(paths)), self.assertRaises((ValueError, RuntimeError)):
                self.prepare(service, plan, [dict(sources=paths, label='错误来源')])
        intent = self.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
        from src.rename.work_catalog import WorkCatalog
        with patch.object(service, 'work_catalog', WorkCatalog(root / 'wrong-catalog')):
            with self.assertRaises((ValueError, RuntimeError)):
                service.commit_initial_movie_completion(intent)
        self.assertEqual(before, records(root))

    async def register_historical_work(self, root, service, plan):
        from tests import test_r1_real_media_production_red_contract as transport
        from types import SimpleNamespace
        helper=source_fixture.MediaFlow('runTest')
        provider=plan.work_identity.primary_provider
        info=dict(id=provider.provider_id,title=plan.work_identity.title,
            original_title=plan.work_identity.title,
            release_date=f'{plan.work_identity.year}-01-01',genres=[dict(name='Animation')])
        candidate={**provider.to_dict(),'title':plan.work_identity.title}
        source,primary,_=source_fixture._make_version(root/'historical-source','720p')
        with patch.object(source_fixture,'_movie_candidate',return_value=candidate), \
                patch.object(transport._FrozenSearch,'get_movie_info_by_id',return_value=(info['title'],info)):
            result=await helper._run_provider_flow(root,source,'historical-component')
        prepared=service.prepare_initial_movie_completion(result.plan.task_id,PlanStore.fingerprint(result.plan),
            groups=[dict(sources=[str(primary)],label='历史独立版本')],reason='建立独立已完成控制')
        result.plan=service.commit_initial_movie_completion(prepared)
        result.state=service.decision_state_store.load(result.plan.task_id)
        task,ledger,execution=await helper._approve_and_execute(root=root,result=result)
        self.assertEqual('completed',task.get('execution_status'),task.get('error'))
        return service.work_catalog.load(result.plan.metadata['proposed_work_key'])


    async def test_catalog_missing_creation_and_same_revision_byte_drift_reject(self):
        for fault in ('created', 'same_revision'):
            with self.subTest(fault=fault):
                root, service, plan, primary, _ = await self.fixture()
                if fault == 'same_revision':
                    record = await self.register_historical_work(root, service, plan)
                intent = self.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
                if fault == 'created':
                    await self.register_historical_work(root, service, plan)
                else:
                    path = service.work_catalog._path(record['work_key'])
                    path.write_bytes(path.read_bytes() + b'\n')
                before = records(root)
                with self.assertRaises((ValueError, RuntimeError)):
                    service.commit_initial_movie_completion(intent)
                self.assertEqual(before, records(root))


    async def test_canonical_requires_both_transports_and_exact_candidate(self):
        for fault in ('no_intent', 'no_context', 'mixed', 'entry', 'decision'):
            with self.subTest(fault=fault):
                root, service, plan, primary, _ = await self.fixture()
                intent = self.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
                original = ProjectionCommitService.commit
                reached = []
                def corrupt(transaction, envelope, **kwargs):
                    reached.append(True)
                    if fault == 'no_intent':
                        envelope = replace(envelope, initial_movie_completion=None)
                    elif fault == 'no_context':
                        transaction.initial_movie_completion_context = None
                    elif fault == 'mixed':
                        envelope = replace(envelope, movie_change=object())
                    elif fault == 'entry':
                        draft = envelope.draft_plan
                        entry = draft.entries[0]
                        envelope = replace(envelope, draft_plan=replace(draft, entries=(
                            replace(entry, target_path=entry.target_path.with_name('injected-target.mkv')),
                            *draft.entries[1:])))
                    else:
                        envelope = replace(envelope, decision=replace(envelope.decision,
                            manual_role_decisions={**envelope.decision.manual_role_decisions, 'injected': 'extra'}))
                    return original(transaction, envelope, **kwargs)
                before = records(root)
                with patch.object(ProjectionCommitService, 'commit', corrupt):
                    with self.assertRaises((ValueError, RuntimeError)):
                        service.commit_initial_movie_completion(intent)
                self.assertTrue(reached)
                self.assertEqual(before, records(root))

    async def test_terminal_race_preserves_later_request_and_blocks_fresh_recovery(self):
        root, service, plan, primary, _ = await self.fixture()
        intent = self.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
        path = root / 'tasks' / f'{plan.task_id}.json'
        reached = []
        def race(_transaction, phase):
            if phase == 'plan_written':
                self.h.replace_request(path, request_id='newer-request-during-completion')
                reached.append(path.read_bytes())
        with patch.object(ProjectionCommitService, '_checkpoint', race):
            with self.assertRaises((ValueError, RuntimeError)):
                service.commit_initial_movie_completion(intent)
        self.assertEqual(1, len(reached))
        self.assertEqual(reached[0], path.read_bytes())
        before = records(root)
        fresh = ProjectionCommitService(service.decision_state_store, service.plan_store, task_root=root / 'tasks')
        with self.assertRaises((ValueError, RuntimeError)):
            fresh.recover_pending()
        self.assertEqual(before, records(root))


if __name__ == '__main__':
    unittest.main()
