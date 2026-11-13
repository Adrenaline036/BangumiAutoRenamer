"""First workspace: real admission and commits, no fabricated Plan history."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from src.rename import content_selection, default_activation, source_basis
from src.rename.content_policy import require_media_ready
from src.rename.plan_store import PlanStore
from src.rename.projection_authority import ProjectionCommitService, validate_plan_projection_receipt
from tests import test_default_activation_contract as activation
from tests.test_content_choice_contract import change


def sources(_root, source):
    video = next(p for p in source.glob('*.mkv') if '[Trailer]' not in p.name)
    video.with_suffix('.chs.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\ntext\n', encoding='utf-8')
    (source / 'Fonts').mkdir()
    (source / 'Fonts' / 'Sample.ttf').write_bytes(b'font input; not inspected by inventory')
    (source / 'Unknown.bin').write_bytes(b'unclassified input')
    return source


def persistent(flow):
    return {p.relative_to(flow.root).as_posix(): p.read_bytes()
            for name in ('plans', 'decisions', 'tasks', 'projection_journal', 'source_ownership')
            for p in (flow.root / name).rglob('*.json')}


class InitialDefaultPublicationTests(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow

    def workspace(self, flow):
        return default_activation.ensure_workspace(flow.service, flow.task_id,
            roots=flow.service.root_snapshot_source.observe())

    def assert_source_semantics(self, flow, state, plan):
        content_selection.validate_plan_content(plan, state)
        validate_plan_projection_receipt(plan)
        paths = {p for p in flow.source.rglob('*') if p.is_file()}
        self.assertGreaterEqual(len(paths), 6)
        self.assertEqual(paths, set(plan.expected_sources))
        ignored = {flow.source / name for name in ('README.txt', 'Fonts/Sample.ttf', 'Unknown.bin')} | {next(flow.source.glob('*[[]Trailer[]].mkv'))}
        self.assertEqual(ignored, set(plan.ignored_sources))
        self.assertEqual(paths - ignored, {e.source_path for e in plan.entries})
        self.assertFalse(state.confirmed_work)
        self.assertEqual(state.initial_source_basis, plan.initial_source_basis)
        with self.assertRaises(ValueError):
            require_media_ready(plan)

    async def test_first_workspace_is_one_real_publication(self):
        for movie in (False, True):
            with self.subTest(movie=movie):
                async with self.flow(movie=movie, admission_only=True, configure_source=sources) as flow:
                    self.assertFalse(flow.store.path_for(flow.task_id).exists())
                    with patch.object(ProjectionCommitService, '_freeze_source_identities',
                            wraps=ProjectionCommitService._freeze_source_identities) as freeze:
                        state, plan = self.workspace(flow)
                    self.assert_source_semantics(flow, state, plan)
                    self.assertEqual(1, len(flow.commits), 'inventory and defaults must publish together')
                    self.assertEqual(1, freeze.call_count, 'one real final source freeze is still required')
                    self.assertEqual(tuple(plan.expected_sources), tuple(freeze.call_args.args[0]))

    async def test_first_choice_refers_to_admission_not_an_intermediate_plan(self):
        async with self.flow(admission_only=True, configure_source=sources) as flow:
            state, plan = self.workspace(flow)
            self.assert_source_semantics(flow, state, plan)
            choice = state.content_selection.to_dict()
            self.assertEqual('admitted-source-basis-v1', choice['origin_kind'])
            self.assertIsNone(choice['origin_plan_fingerprint'])
            self.assertIsNone(choice['parent_plan_fingerprint'])
            self.assertEqual('', plan.revision_parent_fingerprint)
            self.assertEqual(plan, flow.store.load(flow.task_id))
            self.assertEqual(0, state.revision)
            self.assertTrue(all(row['kind'] == 'unresolved_source_observation'
                and row['plan_fingerprint'] is None for row in choice['material_origins']))

    async def test_open_existing_workspace_never_reinitializes_choices(self):
        async with self.flow(admission_only=True, configure_source=sources) as flow:
            _, plan = self.workspace(flow)
            chosen = next(e.source_path for e in plan.entries if e.media_kind == 'video')
            updated = change(flow.service, plan, [chosen])
            count, before = len(flow.commits), persistent(flow)
            state, reopened = self.workspace(flow)
            self.assertEqual(updated, reopened)
            self.assertEqual(PlanStore.fingerprint(plan), state.content_selection.to_dict()['parent_plan_fingerprint'])
            self.assertEqual(count, len(flow.commits))
            self.assertEqual(before, persistent(flow))

    async def test_disconnected_fresh_basis_cannot_publish_or_fallback(self):
        async with self.flow(admission_only=True, configure_source=sources) as flow:
            before = persistent(flow)
            with patch.object(source_basis, 'project_source_basis', return_value=None):
                with self.assertRaisesRegex(ValueError, 'producer.*断开'):
                    self.workspace(flow)
            self.assertEqual([], flow.commits)
            self.assertEqual(before, persistent(flow))

    async def test_source_changed_after_draft_keeps_frozen_inventory_until_explicit_refresh(self):
        changed = []
        def hook(transaction, envelope, terminal, forward):
            if not changed:
                path = next(p for p in envelope.draft_plan.expected_sources if p.name == 'Unknown.bin')
                path.write_bytes(b'changed after observation, before commit')
                changed.append(path)
            return forward()
        async with self.flow(admission_only=True, configure_source=sources, hook=hook) as flow:
            state,plan=self.workspace(flow)
            self.assertEqual(1,len(changed))
            scope=flow.manager._read(flow.task_id)['task_source_scope']
            self.assertEqual(scope,state.initial_source_basis.to_dict()['task_source_scope'])
            self.assertEqual(state.initial_source_basis,plan.initial_source_basis)

    async def test_historical_origin_still_rejects_null_history(self):
        async with self.flow(admission_only=True, configure_source=sources) as flow:
            state, _ = self.workspace(flow)
            value = state.content_selection.to_dict()
            value.update(origin_kind=content_selection.DEFAULT_ORIGIN,
                         origin_plan_fingerprint=None, parent_plan_fingerprint=None)
            with self.assertRaises(ValueError):
                content_selection.ContentSelection.from_mapping(value)

    def test_parentless_plan_wire_format_uses_empty_string(self):
        from pathlib import Path
        from src.rename.rename_plan import RenamePlan
        plan = RenamePlan('unpublished', Path.cwd(), ())
        self.assertEqual('', plan.revision_parent_fingerprint)
        self.assertEqual(plan, RenamePlan.from_dict(plan.to_dict()))
        with self.assertRaisesRegex(ValueError, 'revision_parent_fingerprint must be a string'):
            RenamePlan.from_dict({**plan.to_dict(), 'revision_parent_fingerprint': None})

    async def test_initial_publication_rejects_alternate_or_mixed_inputs(self):
        for variant in ('missing-intent', 'scope', 'other-decision', 'other-command', 'changed-draft'):
            with self.subTest(variant=variant):
                reached = []
                def hook(transaction, envelope, terminal, forward):
                    self.assertIsNotNone(envelope.initial_default)
                    reached.append(variant)
                    if variant == 'missing-intent':
                        altered = replace(envelope, initial_default=None)
                    elif variant == 'scope':
                        altered = replace(envelope, initial_default=replace(envelope.initial_default,
                                                                          scope_fingerprint='0'*64))
                    elif variant == 'other-decision':
                        altered = replace(envelope, decision=replace(envelope.decision, library_domain='general'))
                    elif variant == 'other-command':
                        altered = replace(envelope, movie_change=object())
                    else:
                        altered = replace(envelope, draft_plan=replace(envelope.draft_plan,
                                                                       revision_reason='unrelated revision'))
                    return forward(altered)
                async with self.flow(admission_only=True, configure_source=sources, hook=hook) as flow:
                    before = persistent(flow)
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.workspace(flow)
                    self.assertEqual([variant], reached)
                    self.assertEqual([], flow.commits)
                    self.assertEqual(before, persistent(flow))

    async def test_disconnected_initial_builder_never_uses_historical_initializer(self):
        from src.rename import default_content
        async with self.flow(admission_only=True, configure_source=sources) as flow:
            before = persistent(flow)
            with patch.object(default_content, 'build_initial_default', return_value=None), \
                    patch.object(flow.service, 'prepare_default_content_initialization') as legacy:
                with self.assertRaisesRegex(ValueError, '首建已断开'):
                    self.workspace(flow)
            legacy.assert_not_called()
            self.assertEqual(before, persistent(flow))

    async def test_first_publication_cannot_replace_existing_plan_or_decision(self):
        captured = []
        def hook(transaction, envelope, terminal, forward):
            captured.append((transaction, envelope, terminal))
            return forward()
        async with self.flow(admission_only=True, configure_source=sources, hook=hook) as flow:
            self.workspace(flow)
            before = persistent(flow)
            transaction, envelope, terminal = captured[0]
            with self.assertRaises((ValueError, RuntimeError)):
                transaction.commit(envelope, task_terminal=terminal)
            self.assertEqual(before, persistent(flow))

    async def test_admitted_origin_cannot_claim_saved_history(self):
        async with self.flow(admission_only=True, configure_source=sources) as flow:
            state, _ = self.workspace(flow)
            for variant in ('plan', 'material'):
                with self.subTest(variant=variant):
                    value = state.content_selection.to_dict()
                    if variant == 'plan':
                        value['origin_plan_fingerprint'] = '0'*64
                    else:
                        value['material_origins'][0].update(kind='actual_plan_history', plan_fingerprint='0'*64)
                    with self.assertRaises(ValueError):
                        content_selection.ContentSelection.from_mapping(value)


if __name__ == '__main__':
    unittest.main()
