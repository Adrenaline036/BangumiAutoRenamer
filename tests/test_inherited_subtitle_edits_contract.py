"""Finite inherited-reference codec boundaries; never manufacture a receipt.

Subtitle history comes from disposable canonical producers; reduced scopes are
untrusted codec/verifier probes only, not admitted or persisted new plans.
"""
from dataclasses import replace
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename import content_selection as content, inherited_subtitle_edits as inherited
from src.rename.decision_state import DecisionStateStore
from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from src.rename.projection_authority import inventory_fingerprint_for_plan
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import SubtitlePlanContract, PlanEntry
from tests import test_content_subtitle_lifecycle as lifecycle
from tests import test_partition_subtitle_history_contract as previous
from tests import test_partition_subtitle_transition_contract as transitions


REFERENCE = 'a' * 64


def first_subtitle(plan):
    return next(entry for entry in plan.entries if entry.subtitle is not None)


class InheritedSubtitleCodecContracts(unittest.IsolatedAsyncioTestCase):
    flow = previous.PartitionSubtitleHistoryContracts.flow
    current = previous.PartitionSubtitleHistoryContracts.current
    video = previous.PartitionSubtitleHistoryContracts.video
    edit = previous.PartitionSubtitleHistoryContracts.edit
    def revisions(self, flow):
        from tests.test_content_choice_contract import change
        revisions = list(transitions.PartitionSubtitleTransitionContracts.revisions(self, flow))
        final = revisions[3]
        source = next(r['source_path'] for r in content.from_plan(final).to_dict()['materials']
                      if r['media_kind'] == 'video' and 'S01E03' in r['source_path'])
        # Retain the selected probe video and create a legitimate history tail
        # after step 2 using the ordinary public content restoration command.
        revisions[3] = change(flow.service, final, [Path(source)], action='restore')
        return tuple(revisions)

    async def frozen_codec_input(self, flow, plan, *, episode='S01E03', side='parent'):
        """Read genuine subtitle history into an untrusted historical codec probe.

        The reduced scope is NOT admitted or persisted. It carries no receipt,
        reservation or authority to publish. Retired queue/store APIs are unused.
        """
        from src.rename.content_associations import partition_source_closure
        from src.rename.source_basis import project_source_basis
        from src.rename.task_batch_scope import (TaskSourceScope,
            source_identity_set_fingerprint, canonical_reservation_lineage_id)
        from src.rename.partition_subtitle_history import read_subtitle_history
        from src.rename.subtitle_transition_history import observe as observe_subtitle_transitions
        state = flow.service.decision_state_store.load(flow.task_id)
        old = state.content_selection.to_dict()
        entries = content.material_entries(plan, state.content_selection)
        episodes = (episode,) if isinstance(episode, str) else tuple(episode)
        selected = [next(e for e in entries if e.media_kind == 'video' and token in e.source_path.name) for token in episodes]
        before = lifecycle.records(flow.root)
        source_basis = state.initial_source_basis
        scope = TaskSourceScope.from_mapping(source_basis.to_dict()['task_source_scope'])
        closure = {str(p) for p in partition_source_closure(state.content_selection.associations,
            [entry.source_path for entry in selected])}
        identities = tuple(i for i in scope.source_identities if (i.source_path in closure) == (side == 'child'))
        self.assertTrue(0 < len(identities) < len(scope.source_identities))
        destination_id = flow.task_id if side == 'parent' else 'codec-child'
        set_digest = source_identity_set_fingerprint(identities)
        batch_digest = content.digest({'codec_probe_only': set_digest, 'task_id': destination_id})
        reduced_scope = replace(scope, task_id=destination_id, source_identities=identities,
            source_identity_set_fingerprint=set_digest, batch_scope_fingerprint=batch_digest,
            reservation_lineage_id=canonical_reservation_lineage_id(owner_task_id=destination_id,
                origin_batch_scope_fingerprint=batch_digest, source_identity_set_fingerprint=set_digest))
        destination = project_source_basis(task_id=destination_id, source_root=plan.source_root,
            batch_id='codec-only-batch', scope=reduced_scope)
        history = read_subtitle_history(flow.store, plan)
        observed_transitions = observe_subtitle_transitions(flow.store, plan, history)
        partition_fingerprint = content.digest({'codec_probe_only': True,
            'source': source_basis.fingerprint, 'destination': destination.fingerprint})
        raw_basis = destination.to_dict()
        paths = {r['source_path'] for r in raw_basis['sources']}
        origin = dict(partition_fingerprint=partition_fingerprint, parent_task_id=flow.task_id,
            parent_plan_fingerprint=PlanStore.fingerprint(plan), parent_decision_fingerprint=DecisionStateStore.fingerprint(state),
            parent_content_fingerprint=state.content_selection.fingerprint, source_context=source_basis.fingerprint,
            destination_context=destination.fingerprint, destination_task_id=raw_basis['task_id'],
            destination_scope_fingerprint=raw_basis['task_source_scope']['source_identity_set_fingerprint'])
        steps = []
        for observed in observed_transitions.transitions:
            value = observed.to_dict()
            effects = [e for e in value['effects'] if e['source_identity']['source_path'] in paths]
            if not effects:
                continue
            step = dict(previous_step_fingerprint=steps[-1]['fingerprint'] if steps else None,
                origin=value['origin'], context=source_basis.fingerprint,
                restriction_chain=[dict(partition_fingerprint=partition_fingerprint, prior_edit_fingerprint=None)], effects=effects)
            steps.append({**step, 'fingerprint': content.digest(step)})
        affected = {e['source_identity']['source_path'] for step in steps for e in step['effects']}
        identities = {r['source_path']: r for r in old['identities']}
        relations = {r['source_path']: r for r in old['associations']['relations']}
        terminal = [dict(source_identity=identities[r['source_path']], relation=relations[r['source_path']],
                        **{k: r[k] for k in ('subtitle', 'requires_review', 'evidence')})
                    for r in old['materials'] if r['source_path'] in affected]
        finite = dict(policy='finite-subtitle-edits-v1',
            contexts={source_basis.fingerprint: source_basis.to_dict(), destination.fingerprint: raw_basis},
            partitions=[origin], steps=steps,
            terminal=dict(parent_content_fingerprint=state.content_selection.fingerprint, materials=terminal),
            terminal_active_count=len(steps), active_count=len(steps))
        value = {k: copy.deepcopy(v) for k, v in old.items() if k not in content.DEFAULT_FIELDS}
        value.update(policy=content.PARTITION_POLICY, origin_kind=inherited.ORIGIN,
            task_id=raw_basis['task_id'], source_root=raw_basis['source_root'],
            scope_fingerprint=content.digest(raw_basis['task_source_scope']), initial_source_basis_fingerprint=destination.fingerprint,
            partition_origin=origin, inherited_subtitle_edits=finite,
            origin_plan_fingerprint=origin['parent_plan_fingerprint'],
            parent_plan_fingerprint=origin['parent_plan_fingerprint'])
        value['associations'].update(task_id=raw_basis['task_id'], source_root=raw_basis['source_root'])
        value['associations']['sources'] = [r for r in value['associations']['sources'] if r['path'] in paths]
        value['associations']['relations'] = [r for r in value['associations']['relations'] if r['source_path'] in paths]
        for key in ('materials', 'identities', 'evidence'):
            value[key] = [r for r in value[key] if r['source_path'] in paths]
        value['excluded_sources'] = [p for p in value['excluded_sources'] if p in paths]
        view = replace(plan, task_id=raw_basis['task_id'], source_root=Path(raw_basis['source_root']),
            expected_sources=tuple(Path(p) for p in sorted(paths)),
            ignored_sources=tuple(p for p in plan.ignored_sources if str(p) in paths),
            entries=tuple(e for e in plan.entries if str(e.source_path) in paths), subtitle_override_rules=(),
            projection_receipt=None, metadata={**plan.metadata, 'initial_source_basis': raw_basis})
        value['inventory_fingerprint'] = inventory_fingerprint_for_plan(view)
        result = self.project_codec_view(view, value)
        self.assertEqual(before, lifecycle.records(flow.root))
        return result

    async def test_unpublished_movie_group_facts_are_strict_source_only_immutable_births(self):
        from src.rename import inherited_subtitle_edits as inherited
        from src.rename.rename_plan import build_default_version_descriptor
        from src.rename.movie_version_selection import POLICY
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            _, base_choice = await self.frozen_codec_input(flow, self.current(flow)[0], episode='S01E01', side='child')
            base = base_choice.to_dict()
            sources = [row['source_path'] for row in base['materials'] if row['media_kind'] == 'video']
            descriptor = replace(build_default_version_descriptor('source-group', None, None),
                version_ref='mv-' + 'a' * 32, display_label='来源分组', identity_policy=POLICY)
            group = dict(sources=sources, version_ref=descriptor.version_ref,
                         label=descriptor.display_label, descriptor=descriptor.to_dict())
            # Historical format probe only; no movie authority or receipt is manufactured.
            value = {**base, 'inherited_movie_groups': dict(
                origin_content_fingerprint=base['partition_origin']['parent_content_fingerprint'], groups=[group])}
            choice = content.ContentSelection.from_mapping(value)
            self.assertEqual(set(), content.omitted_sources(choice, None))
            content.validate_omission_manifest(choice)
            self.assertIsNotNone(inherited.require_unchanged(choice, choice))
            from src.rename import movie_version_selection as movie_groups
            validate_groups = movie_groups.validate_group_facts
            alternate = copy.deepcopy(group)
            alternate['label'] = alternate['descriptor']['display_label'] = '另一个合法对象'
            foreign = validate_groups([alternate], video_sources=sources)
            for invalid in (None, object(), foreign):
                with self.subTest(disconnected=type(invalid).__name__):
                    def disconnect(groups, **kwargs):
                        return invalid if groups else validate_groups(groups, **kwargs)
                    with patch.object(movie_groups, 'validate_group_facts', side_effect=disconnect):
                        with self.assertRaises(ValueError):
                            content.ContentSelection.from_mapping(value)
            for mode in ('add', 'remove', 'replace'):
                with self.subTest(mode=mode):
                    other = copy.deepcopy(value)
                    other['inherited_movie_groups']['groups'][0]['label'] = '更换分组'
                    other['inherited_movie_groups']['groups'][0]['descriptor']['display_label'] = '更换分组'
                    old, new = (base_choice, choice) if mode == 'add' else (
                        (choice, base_choice) if mode == 'remove' else
                        (choice, content.ContentSelection.from_mapping(other)))
                    with self.assertRaises(ValueError):
                        inherited.require_unchanged(old, new)
            for mode in ('origin', 'outside', 'duplicate', 'empty', 'extra', 'label', 'v2'):
                with self.subTest(mode=mode):
                    bad = copy.deepcopy(value)
                    facts = bad['inherited_movie_groups']
                    if mode == 'origin':
                        facts['origin_content_fingerprint'] = 'f' * 64
                    elif mode == 'outside':
                        facts['groups'][0]['sources'] = [str(self.video(self.current(flow)[0], 'S01E03').source_path)]
                    elif mode == 'duplicate':
                        facts['groups'].append(copy.deepcopy(facts['groups'][0]))
                    elif mode == 'empty':
                        facts['groups'] = []
                    elif mode == 'extra':
                        facts['work_key'] = 'forged-authority'
                    elif mode == 'label':
                        facts['groups'][0]['label'] = 'unsafe/name'
                    else:
                        bad['policy'] = content.DEFAULT_POLICY
                    with self.assertRaises(ValueError):
                        content.ContentSelection.from_mapping(bad)

    async def test_complete_scope_digest_and_identity_digest_are_not_interchangeable(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            with patch.object(flow.manager, 'partition_task_sources', side_effect=AssertionError('retired queue')), \
                    patch.object(flow.manager.source_partition_store, 'prepare', side_effect=AssertionError('retired store')):
                probe, choice = await self.frozen_codec_input(flow, self.revisions(flow)[3])
            value = choice.to_dict()
            basis = value['inherited_subtitle_edits']['contexts'][value['initial_source_basis_fingerprint']]
            self.assertEqual(content.digest(basis['task_source_scope']), value['scope_fingerprint'])
            identity_digest = basis['task_source_scope']['source_identity_set_fingerprint']
            self.assertEqual(identity_digest, value['partition_origin']['destination_scope_fingerprint'])
            self.assertNotEqual(identity_digest, value['scope_fingerprint'])
            value['scope_fingerprint'] = identity_digest
            with self.assertRaises(ValueError):
                self.project_codec_view(probe, value)


    def project_codec_view(self, view, value):
        value = copy.deepcopy(value)
        expected = inherited.project(value['inherited_subtitle_edits'])
        for row in value['materials']:
            if row['source_path'] in expected:
                row.update({k: expected[row['source_path']][k] for k in ('subtitle', 'requires_review', 'evidence')})
        value['associations']['relations'] = [expected[r['source_path']]['relation']
            if r['source_path'] in expected else r for r in value['associations']['relations']]
        choice = content.ContentSelection.from_mapping(value)
        materials = {r['source_path']: r for r in value['materials']}
        projected = replace(view, entries=tuple(PlanEntry.from_dict({**materials[str(e.source_path)],
            'target_path': str(e.target_path), 'collision_resolution': None}) for e in view.entries),
            metadata={**view.metadata, 'content_selection': choice.to_dict(), 'content_selection_fingerprint': choice.fingerprint})
        return projected, choice

    async def test_real_observations_project_two_file_steps_and_zero_one_two_undo_without_io(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            final = self.revisions(flow)[3]
            probe, choice = await self.frozen_codec_input(flow, final)
            before = lifecycle.records(flow.root)
            self.assertIsNone(probe.projection_receipt)
            for count in (2, 1, 0):
                value = choice.to_dict()
                value['inherited_subtitle_edits']['active_count'] = count
                with self.subTest(count=count), transitions.no_media_io(flow.source), \
                        patch.object(flow.store, 'load_version', side_effect=AssertionError('static must not read history')):
                    view, selected = self.project_codec_view(probe, value)
                    content.validate_plan_content(view)
                    self.assertEqual((), PlanVerifier().verify_subtitle_rule_materials(view))
                    source = value['inherited_subtitle_edits']['steps'][0]['effects'][0]['source_identity']['source_path']
                    row = next(r for r in selected.to_dict()['materials'] if r['source_path'] == source)
                    birth = value['inherited_subtitle_edits']['terminal']['materials'][0]
                    self.assertEqual((birth['requires_review'], birth['evidence']), (row['requires_review'], row['evidence']))
                    if count:
                        self.assertEqual('manual_transfer', row['subtitle']['decision_source'])
                        self.assertEqual(value['inherited_subtitle_edits']['steps'][count - 1]['fingerprint'],
                                         row['subtitle']['inherited_edit_fingerprint'])
                    else:
                        self.assertNotIn('inherited_edit_fingerprint', row['subtitle'])
                        self.assertIsNone(row['subtitle']['associated_video_source_path'])
                        self.assertEqual('unresolved', row['subtitle']['association_state'])
                        paths = {r['source_path'] for r in selected.to_dict()['identities']}
                        self.assertTrue(set(row['subtitle']['association_candidates']) <= paths)
            self.assertEqual(before, lifecycle.records(flow.root))

    async def test_cross_card_historical_owner_only_blocks_undo_not_birth(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            final = self.revisions(flow)[3]
            probe, choice = await self.frozen_codec_input(flow, final, episode='S01E01')
            self.assertEqual((), PlanVerifier().verify_subtitle_rule_materials(probe))
            value = choice.to_dict()
            value['inherited_subtitle_edits']['active_count'] = 1
            before = lifecycle.records(flow.root)
            with transitions.no_media_io(flow.source), self.assertRaises(ValueError):
                self.project_codec_view(probe, value)
            self.assertEqual(before, lifecycle.records(flow.root))

    async def test_empty_steps_and_terminal_are_legitimate_unpublished_codec(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            probe, choice = await self.frozen_codec_input(flow, self.current(flow)[0])
            finite = choice.to_dict()['inherited_subtitle_edits']
            self.assertEqual([], finite['steps'])
            self.assertEqual([], finite['terminal']['materials'])
            self.assertEqual((), PlanVerifier().verify_subtitle_rule_materials(probe))
            for bad in (None, object()):
                with self.subTest(disconnected=type(bad).__name__), patch.object(inherited, 'validate_plan', return_value=bad):
                    issues = PlanVerifier().verify_subtitle_rule_materials(probe)
                    self.assertIn('subtitle_inherited_content_invalid', {i.code for i in issues})

    async def test_birth_and_cursor_cannot_be_published_through_actual_canonical_or_direct_save(self):
        from src.rename.decision_state import DecisionCommandService
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            current = self.revisions(flow)[3]
            probe, choice = await self.frozen_codec_input(flow, current)
            state = flow.service.decision_state_store.load(flow.task_id)
            before = lifecycle.records(flow.root)
            candidate = replace(state, content_selection=choice)
            with self.assertRaises((ValueError, RuntimeError)):
                flow.service.decision_state_store.save(candidate, expected_revision=state.revision)
            with self.assertRaises((ValueError, RuntimeError)):
                DecisionCommandService(flow.service.decision_state_store, flow.store)._commit_projection(
                    state, candidate, probe)
            from src.rename.projection_authority import FinalPlanProjector
            with self.assertRaises((ValueError, RuntimeError)):
                FinalPlanProjector.revise(current, probe)
            self.assertEqual(before, lifecycle.records(flow.root))

    async def test_unmodified_finite_fields_guard_disconnect_stops_actual_ordinary_change(self):
        from tests.test_content_choice_contract import change
        for bad in (None, object()):
            with self.subTest(result=type(bad).__name__):
                async with self.flow(automatic=False, configure_source=previous.package) as flow:
                    plan = self.current(flow)[0]
                    before = lifecycle.records(flow.root)
                    with patch.object(inherited, 'require_unchanged', return_value=bad):
                        with self.assertRaises((ValueError, RuntimeError)):
                            change(flow.service, plan, [self.video(plan, 'S01E03').source_path])
                    self.assertEqual(before, lifecycle.records(flow.root))

    async def test_static_rejections_bind_structure_destination_terminal_and_forward_references(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            probe, choice = await self.frozen_codec_input(flow, self.revisions(flow)[3])
            def recalculate(value):
                prior = None
                for step in value['inherited_subtitle_edits']['steps']:
                    step['previous_step_fingerprint'] = prior
                    step['fingerprint'] = content.digest({k: v for k, v in step.items() if k != 'fingerprint'})
                    prior = step['fingerprint']
            for mode in ('bool_cursor', 'bool_birth', 'cursor_overflow', 'birth_count', 'terminal_missing',
                         'terminal_duplicate', 'terminal_binding', 'terminal_wrong_identity', 'context_key',
                         'same_scope', 'destination_task', 'destination_basis', 'origin_plan', 'clip_restriction',
                         'first_prior', 'forward_before', 'wrong_after', 'extra_field', 'wrong_step_identity'):
                value = choice.to_dict()
                finite = value['inherited_subtitle_edits']
                if mode == 'bool_cursor': finite['active_count'] = True
                elif mode == 'bool_birth': finite['terminal_active_count'] = True
                elif mode == 'cursor_overflow': finite['active_count'] = len(finite['steps']) + 1
                elif mode == 'birth_count': finite['terminal_active_count'] -= 1
                elif mode == 'terminal_missing': finite['terminal']['materials'].pop()
                elif mode == 'terminal_duplicate': finite['terminal']['materials'].append(copy.deepcopy(finite['terminal']['materials'][0]))
                elif mode == 'terminal_binding': finite['terminal']['parent_content_fingerprint'] = REFERENCE
                elif mode == 'terminal_wrong_identity': finite['terminal']['materials'][0]['source_identity']['size'] += 1
                elif mode == 'context_key':
                    key = next(iter(finite['contexts']))
                    finite['contexts'][REFERENCE] = finite['contexts'].pop(key)
                elif mode == 'same_scope':
                    finite['partitions'][-1]['destination_context'] = finite['partitions'][-1]['source_context']
                    value['partition_origin'] = copy.deepcopy(finite['partitions'][-1])
                elif mode == 'destination_task': value['task_id'] = 'wrong-destination'
                elif mode == 'destination_basis': value['initial_source_basis_fingerprint'] = finite['partitions'][0]['source_context']
                elif mode == 'origin_plan': value['origin_plan_fingerprint'] = REFERENCE
                elif mode == 'clip_restriction': finite['steps'][0]['restriction_chain'] = []
                elif mode == 'first_prior': finite['steps'][0]['restriction_chain'][0]['prior_edit_fingerprint'] = REFERENCE
                elif mode == 'forward_before': finite['steps'][0]['effects'][0]['before'] = copy.deepcopy(finite['steps'][0]['effects'][0]['after'])
                elif mode == 'wrong_after': finite['steps'][0]['effects'][0]['after']['subtitle']['applied_rule_id'] = 'missing-rule'
                elif mode == 'extra_field': finite['steps'][0]['permission'] = True
                elif mode == 'wrong_step_identity': finite['steps'][0]['effects'][0]['source_identity']['size'] += 1
                recalculate(value)
                with self.subTest(mode=mode), transitions.no_media_io(flow.source), self.assertRaises(ValueError):
                    content.ContentSelection.from_mapping(value)
            wrong_basis = choice.to_dict()['inherited_subtitle_edits']['contexts'][choice.to_dict()['partition_origin']['source_context']]
            with transitions.no_media_io(flow.source), self.assertRaises(ValueError):
                content.require_plan_binding(replace(probe, metadata={**probe.metadata, 'initial_source_basis': wrong_basis}), choice)

    async def test_common_verifier_rejects_visible_and_hidden_tampering_and_disconnection(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            probe, choice = await self.frozen_codec_input(flow, self.revisions(flow)[3])
            selected = next(e for e in probe.entries if e.subtitle and e.subtitle.inherited_edit_fingerprint)
            changed = replace(probe, entries=tuple(replace(e, subtitle=replace(e.subtitle, flags=('forced',)))
                if e.source_path == selected.source_path else e for e in probe.entries))
            with transitions.no_media_io(flow.source):
                issues = PlanVerifier().verify(changed, validate_projection_receipt=False).issues
                self.assertIn('subtitle_inherited_reference_invalid', {i.code for i in issues})
            value = choice.to_dict()
            row = next(r for r in value['materials'] if r['source_path'] == str(selected.source_path))
            row['subtitle']['flags'] = ['forced']
            hidden = replace(probe, entries=tuple(e for e in probe.entries if e != selected), metadata={**probe.metadata,
                'content_selection': value, 'content_selection_fingerprint': content.digest(value)})
            with transitions.no_media_io(flow.source), self.assertRaises(ValueError):
                PlanVerifier().verify_subtitle_rule_materials(hidden)
            for bad in (None, object()):
                with self.subTest(disconnected=type(bad).__name__), transitions.no_media_io(flow.source), \
                        patch.object(inherited, 'validate_plan', return_value=bad):
                    issues = PlanVerifier().verify_subtitle_rule_materials(probe)
                    self.assertIn('subtitle_inherited_content_invalid', {i.code for i in issues})
            for bad in (None, object(), {}):
                with self.subTest(selection_disconnect=type(bad).__name__), transitions.no_media_io(flow.source), \
                        patch.object(inherited, 'validate_selection', return_value=bad), self.assertRaises(ValueError):
                    content.ContentSelection.from_mapping(choice.to_dict())

    async def test_affected_subtitle_cannot_be_erased_from_visible_or_hidden_materials(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            probe, choice = await self.frozen_codec_input(flow, self.revisions(flow)[3])
            value = choice.to_dict()
            affected = value['inherited_subtitle_edits']['terminal']['materials'][0]['source_identity']['source_path']
            # Unaffected ordinary empty contracts remain legal, not all None values are forbidden.
            self.assertTrue(any(r['subtitle'] is None for r in value['materials']))
            with transitions.no_media_io(flow.source):
                self.assertEqual(choice, content.ContentSelection.from_mapping(value))
                self.assertEqual((), PlanVerifier().verify_subtitle_rule_materials(probe))
            next(r for r in value['materials'] if r['source_path'] == affected)['subtitle'] = None
            before = lifecycle.records(flow.root)
            with self.subTest(codec=True), transitions.no_media_io(flow.source), self.assertRaises(ValueError):
                content.ContentSelection.from_mapping(value)
            for visible in (True, False):
                view = replace(probe, entries=tuple(
                    replace(e, subtitle=None) if str(e.source_path) == affected else e
                    for e in probe.entries if visible or str(e.source_path) != affected),
                    metadata={**probe.metadata, 'content_selection': value,
                              'content_selection_fingerprint': content.digest(value)})
                with self.subTest(visible=visible), transitions.no_media_io(flow.source), self.assertRaises(ValueError):
                    PlanVerifier().verify_subtitle_rule_materials(view)
            self.assertEqual(before, lifecycle.records(flow.root))

    async def test_restriction_codec_remaps_prior_reference_without_republishing_or_rematching(self):
        """Two scope probes test grammar only; no actual second v3 publication."""
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            final = self.revisions(flow)[3]
            first, first_choice = await self.frozen_codec_input(flow, final)
            second, second_choice = await self.frozen_codec_input(flow, final, episode=('S01E01', 'S01E03'))
            value, old = second_choice.to_dict(), first_choice.to_dict()['inherited_subtitle_edits']
            finite = value['inherited_subtitle_edits']
            new_origin = finite['partitions'][0]
            new_origin.update(source_context=old['partitions'][-1]['destination_context'],
                parent_plan_fingerprint=PlanStore.fingerprint(first), parent_content_fingerprint=first_choice.fingerprint)
            finite['contexts'].update(copy.deepcopy(old['contexts']))
            finite['partitions'] = [copy.deepcopy(old['partitions'][0]), new_origin]
            value['partition_origin'] = copy.deepcopy(new_origin)
            value['origin_plan_fingerprint'] = new_origin['parent_plan_fingerprint']
            paths = {r['source_path'] for r in value['identities']}
            steps = []
            for prior in old['steps'][:old['active_count']]:
                step = copy.deepcopy(prior)
                step['effects'] = [e for e in step['effects'] if e['source_identity']['source_path'] in paths]
                if not step['effects']: continue
                step['restriction_chain'].append(dict(partition_fingerprint=new_origin['partition_fingerprint'],
                    prior_edit_fingerprint=prior['fingerprint']))
                step['previous_step_fingerprint'] = steps[-1]['fingerprint'] if steps else None
                step['fingerprint'] = content.digest({k: v for k, v in step.items() if k != 'fingerprint'})
                steps.append(step)
            finite['steps'] = steps
            finite['active_count'] = finite['terminal_active_count'] = len(steps)
            finite['terminal'] = dict(parent_content_fingerprint=first_choice.fingerprint,
                materials=[r for p, r in inherited.project(old).items() if p in paths])
            before = lifecycle.records(flow.root)
            with transitions.no_media_io(flow.source):
                projected, selected = self.project_codec_view(second, value)
                self.assertIsNone(projected.projection_receipt)
                self.assertEqual((), PlanVerifier().verify_subtitle_rule_materials(projected))
                current_ref = next(r['subtitle']['inherited_edit_fingerprint'] for r in selected.to_dict()['materials']
                    if r['subtitle'] and r['subtitle'].get('inherited_edit_fingerprint'))
                self.assertEqual(steps[-1]['fingerprint'], current_ref)
                self.assertNotEqual(old['steps'][-1]['fingerprint'], current_ref)
                self.assertEqual(old['steps'][-1]['effects'], steps[-1]['effects'], 'unrelated video removal must not shrink this effect')
            self.assertEqual(before, lifecycle.records(flow.root))

    async def test_terminal_after_undo_then_append_uses_actual_tail_not_last_post(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            _, first, _, second, _ = self.revisions(flow)
            undone = flow.service.revert_subtitle_rule(flow.task_id, PlanStore.fingerprint(second),
                second.subtitle_override_rules[-1].rule_id, '撤销第二条后继续修改')
            current = self.edit(flow, undone, language='en')
            from tests.test_content_choice_contract import change
            current = change(flow.service, current, [self.video(current, 'S01E01').source_path.with_suffix('.mka')])
            probe, choice = await self.frozen_codec_input(flow, current)
            finite = choice.to_dict()['inherited_subtitle_edits']
            self.assertEqual([first.subtitle_override_rules[0].rule_id, current.subtitle_override_rules[-1].rule_id],
                             [s['origin']['rule_id'] for s in finite['steps']])
            self.assertNotEqual(PlanStore.fingerprint(current), finite['steps'][-1]['origin']['post_plan_fingerprint'])
            actual = content.from_plan(current).to_dict()
            for row in finite['terminal']['materials']:
                original = next(r for r in actual['materials'] if r['source_path'] == row['source_identity']['source_path'])
                self.assertEqual({k: original[k] for k in ('subtitle', 'requires_review', 'evidence')},
                                 {k: row[k] for k in ('subtitle', 'requires_review', 'evidence')})
            self.assertEqual((), PlanVerifier().verify_subtitle_rule_materials(probe))

    async def test_local_append_pure_kernel_clears_finite_reference_and_keeps_birth_immutable(self):
        """Unpublished v3 kernel proof, not a claimed actual v3 runtime undo."""
        from src.rename import content_choice
        from src.rename.plan_review import _rule_fingerprint, apply_subtitle_rule_entries
        from src.rename.content_associations import project_subtitles
        from src.rename.projection_authority import ProjectionAuthorityError
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            final = self.revisions(flow)[3]
            probe, choice = await self.frozen_codec_input(flow, final)
            entry = next(e for e in probe.entries if e.subtitle and e.subtitle.inherited_edit_fingerprint)
            base_fp = PlanStore.fingerprint(probe)
            rule = replace(final.subtitle_override_rules[-1], rule_id='new-local-rule-after-transfer',
                base_plan_fingerprint=base_fp, selected_entry_id=entry_id(entry),
                scope_id=f'{probe.task_id}:{entry_id(entry)}:{base_fp}', reason='有限继承上的本地修改',
                outcome=replace(final.subtitle_override_rules[-1].outcome, primary_language='ja'))
            rule = replace(rule, rule_fingerprint=_rule_fingerprint(rule))
            original_value = choice.to_dict()
            before = lifecycle.records(flow.root)
            with transitions.no_media_io(flow.source):
                # The actual history adapter must reject this unpublished probe.
                # Test only its shared pure application kernel; never forge a receipt.
                with self.assertRaisesRegex(ProjectionAuthorityError, 'ProjectionReceipt'):
                    content_choice.subtitle_revision_materials_from_store(
                        flow.store, probe, (rule,), choice.associations.to_dict())
                source_view = replace(probe, entries=tuple(PlanEntry.from_dict({
                    **r, 'target_path': r['source_path']}) for r in original_value['materials']))
                applied = apply_subtitle_rule_entries(source_view, rule,
                    associated_video_source_path=entry.subtitle.associated_video_source_path)
                normalized = project_subtitles(replace(source_view, entries=applied), choice.associations)
                materials = [content.source_material(e) for e in normalized.entries]
                relations = choice.associations.to_dict()
                row = next(r for r in materials if r['source_path'] == str(entry.source_path))
                self.assertEqual('manual_file', row['subtitle']['decision_source'])
                self.assertEqual(rule.rule_id, row['subtitle']['applied_rule_id'])
                self.assertNotIn('inherited_edit_fingerprint', row['subtitle'])
                value = {**original_value, 'materials': materials, 'associations': relations,
                         'parent_plan_fingerprint': base_fp, 'reason': rule.reason}
                selected = content.ContentSelection.from_mapping(value)
                body = {r['source_path']: r for r in materials}
                post = replace(probe, subtitle_override_rules=(rule,), entries=tuple(PlanEntry.from_dict({
                    **body[str(e.source_path)], 'target_path': str(e.target_path)}) for e in probe.entries),
                    metadata={**probe.metadata, 'content_selection': value, 'content_selection_fingerprint': selected.fingerprint})
                self.assertEqual((), PlanVerifier().verify_subtitle_rule_materials(post))
                for key in inherited.FIELDS:
                    self.assertEqual(original_value[key], selected.to_dict()[key])
                forged = replace(rule, outcome=replace(rule.outcome, primary_language='en'))
                forged = replace(forged, rule_fingerprint=_rule_fingerprint(forged))
                self.assertTrue(PlanVerifier().verify_subtitle_rule_materials(replace(post, subtitle_override_rules=(forged,))))
            self.assertEqual(before, lifecycle.records(flow.root))

    async def test_static_v3_write_guard_freezes_birth_and_requires_actual_prepared_evidence(self):
        from src.rename import content_policy, source_basis
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            probe, choice = await self.frozen_codec_input(flow, self.revisions(flow)[3])
            old_state = flow.service.decision_state_store.load(flow.task_id)
            # This state is deliberately not installed into the actual store.
            current = replace(old_state, content_selection=choice)
            before = lifecycle.records(flow.root)
            with self.assertRaises(ValueError):
                content_policy.validate_state_write(flow.service.decision_state_store, current, current)
            for bad in (None, object()):
                with self.subTest(prepared_disconnect=type(bad).__name__), \
                        patch.object(source_basis, 'require_prepared_state_publication', return_value=bad), self.assertRaises(ValueError):
                    content_policy.validate_state_write(flow.service.decision_state_store, current, current)
            for count in (1, 0):
                value = choice.to_dict()
                value['inherited_subtitle_edits']['active_count'] = count
                _, altered = self.project_codec_view(probe, value)
                with self.subTest(cursor=count), self.assertRaises(ValueError):
                    inherited.require_unchanged(choice, altered)
            with self.assertRaises(ValueError):
                inherited.require_unchanged(choice, old_state.content_selection)
            checked = inherited.require_unchanged(choice, choice)
            self.assertIs(checked.candidate, choice)
            self.assertEqual(before, lifecycle.records(flow.root))

    async def test_finite_capacity_limits_reject_instead_of_truncating(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            _, choice = await self.frozen_codec_input(flow, self.revisions(flow)[3])
            for field, amount in (('steps', 257), ('partitions', 65)):
                finite = choice.to_dict()['inherited_subtitle_edits']
                finite[field] = [copy.deepcopy(finite[field][0]) for _ in range(amount)]
                if field == 'steps': finite['terminal_active_count'] = finite['active_count'] = amount
                with self.subTest(field=field), transitions.no_media_io(flow.source), self.assertRaisesRegex(ValueError, '超限'):
                    inherited.InheritedSubtitleEdits.from_mapping(finite)

    def test_direct_save_disconnected_state_guard_stops_before_write(self):
        from src.rename import content_policy
        for bad in (None, object()):
            with self.subTest(result=type(bad).__name__), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                service, plan = lifecycle.series(root)
                store = service.decision_state_store
                state = store.load(plan.task_id)
                before = lifecycle.records(root)
                prepared = store.prepare_save(state, expected_revision=state.revision)
                with patch.object(content_policy, 'validate_state_write', return_value=bad):
                    with self.assertRaises((ValueError, RuntimeError)):
                        store.save_prepared(prepared, expected_revision=state.revision)
                self.assertEqual(before, lifecycle.records(root))

    def test_direct_create_disconnected_state_guard_stops_before_write(self):
        from src.rename import content_policy
        from src.rename.decision_state import DecisionStateStore
        for bad in (None, object()):
            with self.subTest(result=type(bad).__name__), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                store = DecisionStateStore(root / 'decisions')
                before = lifecycle.records(root)
                with patch.object(content_policy, 'validate_state_write', return_value=bad):
                    with self.assertRaises((ValueError, RuntimeError)):
                        store.create('own-empty-draft')
                self.assertEqual(before, lifecycle.records(root))

    def test_existing_contract_round_trip_does_not_add_empty_inherited_fields(self):
        with tempfile.TemporaryDirectory() as raw:
            _, plan = lifecycle.series(Path(raw))
            value = first_subtitle(plan).subtitle.to_dict()
            self.assertNotIn('inherited_edit_fingerprint', value)
            self.assertEqual(value, SubtitlePlanContract.from_dict(value).to_dict())
            choice = content.from_plan(plan)
            self.assertEqual(content.POLICY, choice.to_dict()['policy'])
            self.assertEqual(choice, content.ContentSelection.from_mapping(choice.to_dict()))

    def test_nonempty_inherited_reference_must_not_be_silently_discarded_by_codec(self):
        with tempfile.TemporaryDirectory() as raw:
            _, plan = lifecycle.series(Path(raw))
            value = {**first_subtitle(plan).subtitle.to_dict(), 'inherited_edit_fingerprint': REFERENCE}
            self.assertEqual(value, SubtitlePlanContract.from_dict(value).to_dict())

    def test_malformed_inherited_reference_is_rejected_not_downgraded(self):
        with tempfile.TemporaryDirectory() as raw:
            _, plan = lifecycle.series(Path(raw))
            value = first_subtitle(plan).subtitle.to_dict()
            for bad in (None, 7, [], 'short', 'A' * 64):
                with self.subTest(value=bad), self.assertRaises(ValueError):
                    SubtitlePlanContract.from_dict({**value, 'inherited_edit_fingerprint': bad})

    async def test_v1_and_v2_content_cannot_smuggle_new_reference(self):
        with tempfile.TemporaryDirectory() as raw:
            _, plan = lifecycle.series(Path(raw))
            choices = [content.from_plan(plan)]
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            choices.append(self.current(flow)[1].content_selection)
            self.assertEqual([content.POLICY, content.DEFAULT_POLICY], [c.to_dict()['policy'] for c in choices])
            for choice in choices:
                value = choice.to_dict()
                row = next(r for r in value['materials'] if r['subtitle'] is not None)
                row['subtitle']['inherited_edit_fingerprint'] = REFERENCE
                with self.subTest(policy=value['policy']), self.assertRaises(ValueError):
                    content.ContentSelection.from_mapping(value)

    def test_visible_reference_without_finite_content_authority_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            _, plan = lifecycle.series(Path(raw))
            entry = first_subtitle(plan)
            verifier, baseline = PlanVerifier(), []
            verifier._verify_subtitle_metadata(entry, {}, set(), baseline)
            self.assertFalse(baseline, 'ordinary automatic subtitle control must be valid')
            changed = replace(entry, subtitle=SubtitlePlanContract.from_dict({
                **entry.subtitle.to_dict(), 'inherited_edit_fingerprint': REFERENCE}))
            issues = []
            verifier._verify_subtitle_metadata(changed, {}, set(), issues)
            self.assertTrue(issues, 'a new reference cannot silently become an automatic subtitle')

    def test_hidden_material_reference_cannot_escape_shared_material_verifier(self):
        with tempfile.TemporaryDirectory() as raw:
            _, plan = lifecycle.series(Path(raw))
            verifier = PlanVerifier()
            self.assertEqual((), verifier.verify_subtitle_rule_materials(plan))
            value = content.from_plan(plan).to_dict()
            row = next(r for r in value['materials'] if r['subtitle'] is not None)
            row['subtitle']['inherited_edit_fingerprint'] = REFERENCE
            probe = replace(plan, entries=tuple(e for e in plan.entries if str(e.source_path) != row['source_path']),
                projection_receipt=None, metadata={**plan.metadata, 'content_selection': value,
                    'content_selection_fingerprint': content.digest(value)})
            try:
                issues = verifier.verify_subtitle_rule_materials(probe)
            except ValueError:
                return  # Strict content parsing may reject before reference dispatch.
            self.assertTrue(issues, 'hidden finite references must not bypass the common verifier')
