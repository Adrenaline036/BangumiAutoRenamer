from src.rename.subtitle_transition_history import observe as observe_subtitle_transitions
"""Intermediate subtitle postimages, through actual disposable revisions."""
from pathlib import Path
from contextlib import contextmanager, ExitStack
from dataclasses import replace
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from src.rename import content_choice, content_selection, partition_subtitle_history as history
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import SubtitleOverrideScope
from tests import test_partition_subtitle_history_contract as previous
from tests import test_partition_content_contract as partition
from tests.test_content_choice_contract import change


@contextmanager
def no_media_io(source):
    import builtins
    import io
    with ExitStack() as stack:
        for owner, name in ((builtins, 'open'), (io, 'open'), (os, 'open'), (os, 'stat'), (os, 'lstat')):
            actual = getattr(owner, name)
            def guarded(*args, _actual=actual, **kwargs):
                path = args[0] if args else kwargs.get('file', kwargs.get('path'))
                if isinstance(path, (str, bytes, os.PathLike)) and Path(os.fsdecode(path)).is_relative_to(source):
                    raise AssertionError('subtitle history attempted source media I/O')
                return _actual(*args, **kwargs)
            stack.enter_context(patch.object(owner, name, side_effect=guarded))
        stack.enter_context(patch('os.scandir', side_effect=AssertionError('history must follow exact versions, not scan')))
        # validate_omission_manifest is a pure structural predicate. Guard the
        # actual I/O above, not its historical function name.
        yield


class PartitionSubtitleTransitionContracts(unittest.IsolatedAsyncioTestCase):
    flow = previous.PartitionSubtitleHistoryContracts.flow
    current = previous.PartitionSubtitleHistoryContracts.current
    video = previous.PartitionSubtitleHistoryContracts.video
    split = previous.PartitionSubtitleHistoryContracts.split
    edit = previous.PartitionSubtitleHistoryContracts.edit

    def revisions(self, flow):
        initial = self.current(flow)[0]
        first = self.edit(flow, initial)
        middle = change(flow.service, first, [self.video(first, 'S01E03').source_path])
        subtitle_path = self.video(middle).source_path.with_suffix('.en.srt')
        selected = next(e for e in content_choice.subtitle_edit_view(middle).entries if e.source_path == subtitle_path)
        final = flow.service.apply_subtitle_override_rule(flow.task_id, PlanStore.fingerprint(middle),
            entry_id(selected), scope=SubtitleOverrideScope.FILE,
            associated_video_source_path=self.video(middle, 'S01E02').source_path,
            primary_language='en', script=None, reason='第二次修订明确换到另一集')
        self.assertEqual(2, len(final.subtitle_override_rules))
        self.assertNotEqual(PlanStore.fingerprint(first), final.subtitle_override_rules[1].base_plan_fingerprint)
        return initial, first, middle, final, subtitle_path

    async def test_reader_rejects_missing_intermediate_rule_postimage(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            _, first, _, final, _ = self.revisions(flow)
            bases = history.read_subtitle_history(flow.store, final)
            flow.store._history_path(flow.task_id, PlanStore.fingerprint(first)).unlink()
            before = partition.records(flow.root)
            with no_media_io(flow.source), self.assertRaises((OSError, ValueError, RuntimeError)):
                observe_subtitle_transitions(flow.store, final, bases)
            self.assertEqual(before, partition.records(flow.root))

    async def test_ordered_transitions_bind_exact_rule_postimages_and_owners_without_media_observation(self):
        self.assertTrue(callable(observe_subtitle_transitions), 'missing verified transition reader')
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            initial, first, middle, final, subtitle_path = self.revisions(flow)
            before = partition.records(flow.root)
            bases = history.read_subtitle_history(flow.store, final)
            with no_media_io(flow.source):
                proof = observe_subtitle_transitions(flow.store, final, bases)
            rows = [step.to_dict() for step in proof.transitions]
            self.assertEqual(2, len(rows))
            for row, base, post, index in zip(rows, (initial, middle), (first, final), (0, 1)):
                self.assertEqual({'task_id': flow.task_id, 'base_plan_fingerprint': PlanStore.fingerprint(base),
                    'post_plan_fingerprint': PlanStore.fingerprint(post), 'rule_id': final.subtitle_override_rules[index].rule_id,
                    'rule_fingerprint': final.subtitle_override_rules[index].rule_fingerprint, 'rule_index': index}, row['origin'])
                self.assertEqual([str(subtitle_path)], [e['source_identity']['source_path'] for e in row['effects']])
                effect = row['effects'][0]
                for phase, plan in (('before', base), ('after', post)):
                    value = content_selection.from_plan(plan).to_dict()
                    material = next(m for m in value['materials'] if m['source_path'] == str(subtitle_path))
                    self.assertEqual({k: material[k] for k in ('subtitle', 'requires_review', 'evidence')}, effect[phase])
                    relation = next(r for r in value['associations']['relations'] if r['source_path'] == str(subtitle_path))
                    self.assertEqual(relation, effect[phase + '_relation'])
            self.assertEqual([str(self.video(first).source_path)], rows[0]['effects'][0]['after_relation']['owners'])
            self.assertEqual([str(self.video(final, 'S01E02').source_path)], rows[1]['effects'][0]['after_relation']['owners'])
            required = {str(flow.store._history_path(flow.task_id, PlanStore.fingerprint(p)))
                        for p in (initial, first, middle, final)}
            self.assertTrue(required <= {record[0] for record in proof.records})
            self.assertEqual(before, partition.records(flow.root))

    async def test_undone_intermediate_rule_does_not_poison_active_rule_history(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            _, first, _, second, _ = self.revisions(flow)
            original = content_choice.subtitle_revision_materials_from_store
            with patch.object(content_choice, 'subtitle_revision_materials_from_store', wraps=original) as shared:
                undone = flow.service.revert_subtitle_rule(flow.task_id, PlanStore.fingerprint(second),
                    second.subtitle_override_rules[-1].rule_id, reason='先撤销第二条再重新修改')
                third = self.edit(flow, undone, language='en')
            self.assertGreaterEqual(shared.call_count, 2, 'public append and undo must still use the common kernel')
            bases = history.read_subtitle_history(flow.store, third)
            with no_media_io(flow.source):
                proof = observe_subtitle_transitions(flow.store, third, bases)
            self.assertEqual([first.subtitle_override_rules[0].rule_id, third.subtitle_override_rules[-1].rule_id],
                             [step.to_dict()['origin']['rule_id'] for step in proof.transitions])
            self.assertNotIn(second.subtitle_override_rules[-1].rule_id,
                             [step.to_dict()['origin']['rule_id'] for step in proof.transitions])

    async def test_reader_rejects_bad_post_and_missing_intermediate_history(self):
        for mode in ('middle_missing', 'post_wrong_task', 'post_receipt'):
            with self.subTest(mode=mode):
                async with self.flow(automatic=False, configure_source=previous.package) as flow:
                    _, first, middle, final, _ = self.revisions(flow)
                    bases = history.read_subtitle_history(flow.store, final)
                    target = flow.store._history_path(flow.task_id, PlanStore.fingerprint(
                        middle if mode == 'middle_missing' else first))
                    if mode == 'middle_missing':
                        target.unlink()
                    else:
                        value = json.loads(target.read_text('utf-8'))
                        value['task_id' if mode == 'post_wrong_task' else 'projection_receipt'] = 'wrong-task' if mode == 'post_wrong_task' else None
                        target.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
                    before = partition.records(flow.root)
                    with no_media_io(flow.source), self.assertRaises((OSError, ValueError, RuntimeError)):
                        observe_subtitle_transitions(flow.store, final, bases)
                    self.assertEqual(before, partition.records(flow.root))

    async def test_transition_validator_rejects_missing_clipped_or_forged_proof(self):
        from src.rename import subtitle_transition_history as transitions
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            final = self.revisions(flow)[3]
            bases = history.read_subtitle_history(flow.store, final)
            proof = observe_subtitle_transitions(flow.store, final, bases)
            self.assertEqual(2, len(proof.transitions))
            before = partition.records(flow.root)
            with no_media_io(flow.source):
                self.assertIs(proof, transitions.validate(flow.store, final, bases, proof))
                for mode in ('none', 'object', 'empty_steps', 'omit_step', 'clip_records', 'fake_owner'):
                    if mode == 'none':
                        bad = None
                    elif mode == 'object':
                        bad = object()
                    elif mode == 'empty_steps':
                        bad = replace(proof, transitions=())
                    elif mode == 'omit_step':
                        bad = replace(proof, transitions=proof.transitions[:1])
                    elif mode == 'clip_records':
                        bad = replace(proof, records=proof.records[1:])
                    else:
                        value = proof.transitions[0].to_dict()
                        value['effects'][0]['after_relation']['owners'] = [str(self.video(final, 'S01E02').source_path)]
                        step = replace(proof.transitions[0], payload_json=content_selection.encode(value))
                        bad = replace(proof, transitions=(step, *proof.transitions[1:]))
                    with self.subTest(mode=mode), self.assertRaises((ValueError, RuntimeError)):
                        transitions.validate(flow.store, final, bases, bad)
            self.assertEqual(before, partition.records(flow.root))

    async def test_transition_reader_rejects_disconnected_shared_replay_kernel(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            final = self.revisions(flow)[3]
            bases = history.read_subtitle_history(flow.store, final)
            before = partition.records(flow.root)
            for bad in (None, object()):
                with self.subTest(result=type(bad).__name__), no_media_io(flow.source), \
                        patch.object(content_choice, 'subtitle_revision_materials_from_store', return_value=bad) as kernel:
                    with self.assertRaises((ValueError, RuntimeError)):
                        observe_subtitle_transitions(flow.store, final, bases)
                    self.assertTrue(kernel.called)
            self.assertEqual(before, partition.records(flow.root))

    async def test_modern_without_rules_has_explicit_empty_transition_proof(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            plan, state = self.current(flow)
            self.assertIsNotNone(state.content_policy)
            bases = history.read_subtitle_history(flow.store, plan)
            with no_media_io(flow.source):
                proof = observe_subtitle_transitions(flow.store, plan, bases)
            self.assertEqual((), proof.transitions)
            self.assertEqual((), proof.records)
            self.assertEqual(flow.task_id, proof.task_id)

    async def test_postimage_changed_while_reading_is_rejected_without_media_access(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            _, first, _, final, _ = self.revisions(flow)
            target = flow.store._history_path(flow.task_id, PlanStore.fingerprint(first))
            bases = history.read_subtitle_history(flow.store, final)
            actual, later = history._read, []
            before = partition.records(flow.root)
            def read(path):
                result = actual(path)
                if Path(path) == target and not later:
                    value = json.loads(target.read_text('utf-8'))
                    value['later_actor'] = 'changed-during-history-read'
                    target.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
                    later.append(target.read_bytes())
                return result
            with no_media_io(flow.source), patch.object(history, '_read', side_effect=read):
                with self.assertRaises((ValueError, RuntimeError)):
                    observe_subtitle_transitions(flow.store, final, bases)
            self.assertEqual(1, len(later))
            expected = dict(before)
            expected[str(target.relative_to(flow.root))] = hashlib.sha256(later[0]).hexdigest()
            self.assertEqual(expected, partition.records(flow.root))

    async def test_disconnected_shared_kernel_cannot_publish_ordinary_subtitle_change(self):
        async with self.flow(automatic=False, configure_source=previous.package) as flow:
            plan = self.current(flow)[0]
            before = partition.records(flow.root)
            with patch.object(content_choice, 'subtitle_revision_materials_from_store', return_value=None):
                with self.assertRaises((ValueError, RuntimeError, TypeError)):
                    self.edit(flow, plan)
            self.assertEqual(before, partition.records(flow.root))


class SubtitleTransitionComponentContracts(unittest.TestCase):
    def test_series_then_file_preserves_complete_effect_sets(self):
        from tests import test_content_subtitle_lifecycle as component
        with tempfile.TemporaryDirectory() as raw:
            service, plan = component.series(Path(raw))
            source = next(e.source_path for e in plan.entries if e.subtitle is not None)
            first = component.apply(service, plan, source, scope=SubtitleOverrideScope.SERIES)
            final = component.apply(service, first, source, scope=SubtitleOverrideScope.FILE)
            bases = history.read_subtitle_history(service.plan_store, final)
            with no_media_io(final.source_root):
                proof = observe_subtitle_transitions(service.plan_store, final, bases)
            self.assertEqual([2, 1], [len(step.to_dict()['effects']) for step in proof.transitions])

    def test_actual_legacy_no_content_retains_base_reader_but_cannot_claim_transition(self):
        from tests import test_content_subtitle_lifecycle as component
        with tempfile.TemporaryDirectory() as raw:
            service, plan = component.series(Path(raw), initialize_content=False)
            self.assertIsNone(content_selection.from_plan(plan))
            self.assertIsNone(service.decision_state_store.load(plan.task_id).content_policy)
            source = next(e.source_path for e in plan.entries if e.subtitle is not None)
            plan = component.apply(service, plan, source, scope=SubtitleOverrideScope.SERIES)
            bases = history.read_subtitle_history(service.plan_store, plan)
            self.assertEqual(1, len(bases.records))
            with no_media_io(plan.source_root), self.assertRaisesRegex(ValueError, '完整冻结内容来源'):
                observe_subtitle_transitions(service.plan_store, plan, bases)
