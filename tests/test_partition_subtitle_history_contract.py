from src.rename.subtitle_transition_history import observe as observe_subtitle_transitions
"""Read-only subtitle history guards; mutations use disposable fixture data."""
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_partition_content_contract as partition
from src.rename import content_choice, partition_subtitle_history as history
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import SubtitleOverrideScope


def package(root, source):
    partition.package(root, source)
    second = next(source.glob('*S01E02*.mkv'))
    second.with_suffix('.en.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nTest\n', encoding='utf-8')
    return source


class PartitionSubtitleHistoryContracts(unittest.IsolatedAsyncioTestCase):
    flow = partition.PartitionContentContracts.flow
    current = partition.PartitionContentContracts.current
    selected = partition.PartitionContentContracts.selected
    video = partition.PartitionContentContracts.video
    split = partition.PartitionContentContracts.split

    def edit(self, flow, plan, scope=SubtitleOverrideScope.FILE, language='ja'):
        video = self.video(plan).source_path
        selected = next(e for e in content_choice.subtitle_edit_view(plan).entries
                        if e.source_path == video.with_suffix('.en.srt'))
        return flow.service.apply_subtitle_override_rule(flow.task_id, PlanStore.fingerprint(plan),
            entry_id(selected), scope=scope, associated_video_source_path=video,
            primary_language=language, script=None, reason='实际字幕历史前检')

    async def test_complete_history_and_unedited_control_read_without_write(self):
        for edited in (False, True):
            with self.subTest(edited=edited):
                async with self.flow(automatic=False, configure_source=package) as flow:
                    plan, _ = self.current(flow)
                    if edited:
                        plan = self.edit(flow, plan)
                    before = partition.records(flow.root)
                    with patch.object(flow.manager, 'partition_task_sources', side_effect=AssertionError('retired queue')), \
                            patch.object(flow.manager.source_partition_store, 'prepare', side_effect=AssertionError('retired prepare')):
                        proof = history.read_subtitle_history(flow.store, plan)
                    self.assertEqual(flow.task_id, proof.task_id)
                    self.assertEqual(PlanStore.fingerprint(plan), proof.plan_fingerprint)
                    expected = tuple(str(flow.store._history_path(flow.task_id, rule.base_plan_fingerprint))
                                     for rule in plan.subtitle_override_rules)
                    self.assertEqual(expected, tuple(row[0] for row in proof.records))
                    self.assertEqual(int(edited), len(proof.records))
                    self.assertEqual(before, partition.records(flow.root))

    async def test_reader_rejects_missing_wrong_task_fingerprint_and_receipt(self):
        for corruption in ('missing', 'wrong_task', 'fingerprint', 'receipt'):
            with self.subTest(corruption=corruption):
                async with self.flow(automatic=False, configure_source=package) as flow:
                    plan = self.edit(flow, self.current(flow)[0])
                    rule = plan.subtitle_override_rules[-1]
                    path = flow.store._history_path(flow.task_id, rule.base_plan_fingerprint)
                    if corruption == 'missing':
                        path.unlink()
                    else:
                        payload = json.loads(path.read_text('utf-8'))
                        if corruption == 'wrong_task':
                            payload['task_id'] = 'different-owned-task'
                        elif corruption == 'fingerprint':
                            payload['revision_reason'] = 'changed historical bytes'
                        else:
                            payload['projection_receipt'] = None
                        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
                    before = partition.records(flow.root)
                    with self.assertRaises((OSError, RuntimeError, ValueError)):
                        history.read_subtitle_history(flow.store, plan)
                    self.assertEqual(before, partition.records(flow.root))

    async def test_file_override_chain_checks_older_base(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan = self.edit(flow, self.current(flow)[0])
            plan = self.edit(flow, plan, language='en')
            self.assertEqual(2, len(plan.subtitle_override_rules))
            self.assertEqual(2, len(history.read_subtitle_history(flow.store, plan).records))
            flow.store._history_path(flow.task_id, plan.subtitle_override_rules[0].base_plan_fingerprint).unlink()
            before = partition.records(flow.root)
            with self.assertRaises((OSError, RuntimeError, ValueError)):
                history.read_subtitle_history(flow.store, plan)
            self.assertEqual(before, partition.records(flow.root))

    async def test_history_reader_rejects_disconnected_version_loader(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan = self.edit(flow, self.current(flow)[0])
            before = partition.records(flow.root)
            with patch.object(PlanStore, 'load_version', return_value=None) as loader:
                with self.assertRaises((RuntimeError, ValueError)):
                    history.read_subtitle_history(flow.store, plan)
            self.assertTrue(loader.called)
            self.assertEqual(before, partition.records(flow.root))

    async def test_transition_reader_rejects_disconnected_or_clipped_base_history(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan = self.edit(flow, self.current(flow)[0])
            original = history.read_subtitle_history(flow.store, plan)
            self.assertTrue(original.records)
            before = partition.records(flow.root)
            for bad in (None, object(), replace(original, records=())):
                with self.subTest(result=type(bad).__name__), \
                        patch.object(history, 'read_subtitle_history', return_value=bad) as reader:
                    with self.assertRaises((RuntimeError, ValueError)):
                        observe_subtitle_transitions(flow.store, plan, original)
                    self.assertTrue(reader.called)
            self.assertEqual(before, partition.records(flow.root))

    async def test_series_component_history_preserves_overwritten_predecessor_without_media_scan(self):
        from tests import test_content_subtitle_lifecycle as component
        from src.rename import partition_subtitle_history as history, content_selection
        # Real canonical component, not a new-default GUI/admission SERIES claim.
        with tempfile.TemporaryDirectory() as directory:
            service, plan = component.series(Path(directory))
            first = next(e.source_path for e in plan.entries if e.subtitle is not None)
            plan = component.apply(service, plan, first, scope=SubtitleOverrideScope.SERIES)
            selected = next(e for e in content_choice.subtitle_edit_view(plan).entries if e.source_path == first)
            plan = service.apply_subtitle_override_rule(plan.task_id, PlanStore.fingerprint(plan),
                entry_id(selected), scope=SubtitleOverrideScope.FILE,
                associated_video_source_path=selected.subtitle.associated_video_source_path,
                primary_language='ja', script=None, reason='覆盖系列规则的单文件修订')
            from tests.test_inventory_reuse import source_access
            with source_access(plan.source_root):
                proof = history.read_subtitle_history(service.plan_store, plan)
                self.assertEqual(2, len(proof.records))
                service.plan_store._history_path(plan.task_id, plan.subtitle_override_rules[0].base_plan_fingerprint).unlink()
                with self.assertRaises((OSError, ValueError, RuntimeError)):
                    history.read_subtitle_history(service.plan_store, plan)
