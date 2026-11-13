"""S2: current source workflow, explicit junk exits, no real media or Provider."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from nicegui import ui
from nicegui.client import Client
from nicegui.page import page as nice_page
from src.rename.content_choice import _draft, _preview
from src.rename.content_selection import ContentSelection
from src.rename.decision_state import DecisionCommandService
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.projection_authority import ProjectionCommitService
from tests.test_content_choice_contract import change, records
from tests import test_default_activation_contract as activation_fixture


class NonWatchableEntryContracts(unittest.IsolatedAsyncioTestCase):
    flow = activation_fixture.DefaultActivationProducerContracts.flow
    current = activation_fixture.DefaultActivationProducerContracts.current

    def with_misc(self, root, source):
        (source/'Scans').mkdir()
        (source/'Scans/cover.jpg').write_bytes(b'isolated scan')
        (source/'CDs').mkdir()
        (source/'CDs/track.flac').write_bytes(b'isolated soundtrack')
        return source

    async def test_current_tv_and_movie_allow_manual_restore_without_source_writes(self):
        for movie in (False, True):
            with self.subTest(movie=movie):
                async with self.flow(movie=movie, configure_source=self.with_misc) as flow:
                    plan, state = self.current(flow)
                    expected = {flow.source/'README.txt', flow.source/'Scans/cover.jpg',
                        flow.source/'CDs/track.flac', next(flow.source.glob('*[[]Trailer[]]*'))}
                    self.assertTrue(expected <= set(plan.ignored_sources))
                    original = {p: p.read_bytes() for p in expected}
                    for source in sorted(expected):
                        with self.subTest(source=source.name):
                            plan = change(flow.service, plan, [source], action='restore')
                            self.assertNotIn(source, plan.ignored_sources)
                            self.assertIn(source, {e.source_path for e in plan.entries})
                    self.assertEqual(original, {p: p.read_bytes() for p in expected})
                    self.assertFalse(flow.service.load_or_create(plan).approved_entry_ids)
                    self.assertTrue(all(not e.target_path.exists() for e in plan.entries))

    async def test_unresolved_typed_restore_is_allowed_but_raw_save_cannot_publish(self):
        for route in ('preview', 'update', 'raw_save'):
            with self.subTest(route=route):
                async with self.flow(automatic=False) as flow:
                    plan, state = self.current(flow)
                    self.assertFalse(state.confirmed_work)
                    source = str(flow.source/'README.txt')
                    value = state.content_selection.to_dict()
                    self.assertIn(source, value['excluded_sources'])
                    value['excluded_sources'].remove(source)
                    value['evidence'] = [r for r in value['evidence'] if r['source_path'] != source]
                    value['parent_plan_fingerprint'] = PlanStore.fingerprint(plan)
                    choice = ContentSelection.from_mapping(value)
                    draft = _draft(flow.service, plan, state, choice, state.movie_version_selection, 'restore')
                    before = records(flow.root)
                    if route == 'preview':
                        preview = _preview(flow.service, plan, state, choice, state.movie_version_selection,
                            'restore', (entry_id(replace(plan.entries[0], source_path=Path(source))),))
                        flow.service.commit_content_change(preview)
                    elif route == 'update':
                        DecisionCommandService(flow.service.decision_state_store, flow.store).update(
                            plan.task_id, expected_revision=state.revision,
                            changes={'content_selection': choice}, planner=lambda _: draft,
                            task_terminal=flow.service._revision_task_terminal(plan.task_id))
                    else:
                        # Test a real exposed raw writer, not hand-edited JSON.
                        saved = flow.service.decision_state_store.save(replace(state, content_selection=choice),
                            expected_revision=state.revision)
                        after_save = records(flow.root)
                        with self.assertRaisesRegex(ValueError, '内容|Decision'):
                            ProjectionCommitService(flow.service.decision_state_store, flow.store,
                                task_root=flow.service.task_root).commit_revision(saved, draft)
                        self.assertEqual(after_save, records(flow.root))
                    current = flow.store.load(plan.task_id)
                    if route == 'raw_save':
                        self.assertEqual(plan, current)
                    else:
                        self.assertNotEqual(before, records(flow.root))
                        self.assertNotIn(Path(source), current.ignored_sources)
                        self.assertFalse(flow.service.load_or_create(current).approved_entry_ids)
                    self.assertTrue(all(not e.target_path.exists() for e in draft.entries))

    async def test_needed_subtitle_audio_and_unknown_video_remain_correctable(self):
        def source_with_needed(root, source):
            (source/'Unmatched.mka').write_bytes(b'isolated external audio')
            (source/'Unmatched.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\ntext\n', encoding='utf-8')
            (source/'Unknown OVA.mkv').write_bytes(next(source.glob('*S01E01*')).read_bytes())
            return source
        async with self.flow(configure_source=source_with_needed) as flow:
            plan, _ = self.current(flow)
            targets = {flow.source/name for name in ('Unmatched.mka','Unmatched.srt','Unknown OVA.mkv')}
            self.assertFalse(targets & set(plan.ignored_sources))
            original = {p:p.read_bytes() for p in targets}
            for source in sorted(targets):
                plan = change(flow.service, plan, [source])
                self.assertIn(source, plan.ignored_sources)
                plan = change(flow.service, plan, [source], action='restore')
                self.assertNotIn(source, plan.ignored_sources)
            self.assertEqual(original, {p:p.read_bytes() for p in targets})
            self.assertFalse(flow.service.load_or_create(plan).approved_entry_ids)

    async def test_gui_manual_restore_enabled_after_busy_and_stale_callback_rejected(self):
        from src.pages.content_review_view import ContentWorkbench
        async with self.flow(configure_source=self.with_misc) as flow:
            plan, _ = self.current(flow)
            owner = SimpleNamespace(review_service=flow.service, plan=plan, selected_ids=())
            client = Client(nice_page('/s2-isolated-workbench'), request=None)
            try:
                with client:
                    view = ContentWorkbench(owner, ui=ui, notify=lambda *_a, **_k: None)
                    try:
                        row = next(r for r in view.rows if r['source'].name=='README.txt')
                        for busy in (True, False):
                            view.set_busy(busy)
                            box = next(box for box, _kind in view.boxes if box.text=='README.txt')
                            self.assertEqual(not busy, box.enabled)
                        self.assertEqual('独立不整理', row['status'])
                        before = records(flow.root)
                        view.selected.add(row['identifier'])
                        await view.prepare('restore')
                        self.assertIsNotNone(view.preview)
                        self.assertEqual(before, records(flow.root))
                        change(flow.service, plan, [row['source']], action='restore')
                        before = records(flow.root)
                        view.invalidate()
                        await view.prepare('restore')
                        self.assertIsNone(view.preview)
                        self.assertRegex(view.message.text, '变化|更新')
                        self.assertEqual(before, records(flow.root))
                    finally:
                        view.close()
            finally:
                client.delete()

    async def test_previously_retained_misc_row_can_still_be_selected_for_exclusion(self):
        from src.pages.content_review_view import selection_rows
        async with self.flow() as flow:
            plan, state = self.current(flow)
            value = state.content_selection.to_dict()
            source = str(flow.source/'README.txt')
            value['excluded_sources'].remove(source)
            value['evidence'] = [r for r in value['evidence'] if r['source_path'] != source]
            # Read-only old-choice display control: not a forged accepted Plan.
            retained = ContentSelection.from_mapping(value)
            row = next(r for r in selection_rows(retained, state.movie_version_selection) if str(r['source'])==source)
            self.assertEqual('保留整理', row['status'])
            self.assertFalse(row.get('locked', False))

    async def test_real_preserving_legacy_upgrade_can_exclude_old_retained_misc(self):
        from tests import test_content_policy_contract as policy_fixture
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        helper = policy_fixture.ContentPolicyContracts('runTest')
        original_sources = helper.sources
        def sources(base):
            source = original_sources(base)
            (source/'README.txt').write_bytes(b'historical release instructions')
            return source
        helper.sources = sources
        manager, service, _state, plan = await helper.pending(root)
        self.addAsyncCleanup(manager.stop)
        source = plan.source_root/'README.txt'
        original = {p:p.read_bytes() for p in plan.source_root.iterdir()}
        # Explicit pre-activation compatibility protocol, no forged v2 authority.
        plan = service.commit_content_change(service.prepare_content_migration(plan.task_id,
            PlanStore.fingerprint(plan), reason='historical explicit selection'))
        plan = change(service, plan, [source], action='restore')
        self.assertNotIn(source, plan.ignored_sources)
        plan = service.commit_content_upgrade(service.prepare_content_upgrade(plan.task_id,
            PlanStore.fingerprint(plan), reason='preserve historical choices'))
        self.assertNotIn(source, plan.ignored_sources)
        plan = change(service, plan, [source])
        self.assertIn(source, plan.ignored_sources)
        self.assertEqual(original,{p:p.read_bytes() for p in plan.source_root.iterdir()})
        self.assertFalse(service.load_or_create(plan).approved_entry_ids)

    async def test_failed_source_validation_rejects_public_and_direct_changes(self):
        async with self.flow(automatic=False) as flow:
            plan, state = self.current(flow)
            source = str(flow.source/'README.txt')
            value = state.content_selection.to_dict()
            value['excluded_sources'].remove(source)
            value['evidence'] = [r for r in value['evidence'] if r['source_path'] != source]
            value['parent_plan_fingerprint'] = PlanStore.fingerprint(plan)
            choice = ContentSelection.from_mapping(value)
            draft = _draft(flow.service, plan, state, choice, state.movie_version_selection, 'restore')
            before = records(flow.root)
            with patch('src.rename.content_selection.validate_source_identities', side_effect=ValueError('source validation failed')):
                with self.assertRaises((TypeError,ValueError)):
                    flow.service.prepare_content_change(plan.task_id,PlanStore.fingerprint(plan),
                        selected_entry_ids=(entry_id(replace(plan.entries[0],source_path=Path(source))),),
                        action='restore',reason='disconnected shared rule')
                with self.assertRaises((TypeError,ValueError)):
                    DecisionCommandService(flow.service.decision_state_store,flow.store).update(
                        plan.task_id,expected_revision=state.revision,changes={'content_selection':choice},
                        planner=lambda _:draft,task_terminal=flow.service._revision_task_terminal(plan.task_id))
            self.assertEqual(before,records(flow.root))


if __name__ == '__main__':
    unittest.main()
