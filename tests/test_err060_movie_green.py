"""Production commands for stable movie identities, labels and exclusions."""
from dataclasses import replace
import tempfile
import shutil
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.rename.decision_state import DecisionStateStore
from src.rename.movie_version_selection import MovieVersionSelection, safe_label, validate_selection
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from tests import test_err060_movie_versions as fixture


class MovieVersionGreen(unittest.IsolatedAsyncioTestCase):
    async def test_group_segments_roundtrip_and_unapproved_label_revision(self):
        with tempfile.TemporaryDirectory() as raw:
            _fixture, completed, videos = await fixture.MovieVersionIncidents('runTest').incoming(Path(raw))
            review = completed.review
            plan = review.revise_movie_versions(completed.plan.task_id, PlanStore.fingerprint(completed.plan),
                groups=[dict(sources=list(map(str, videos)), label='院线')])
            rows = [e for e in plan.entries if e.source_path in videos]
            self.assertEqual(1, len({e.presentation_asset.version_descriptor.version_ref for e in rows}))
            self.assertEqual({videos[0]: 1, videos[1]: 2}, {e.source_path: e.presentation_asset.segment.index for e in rows})
            self.assertTrue(all(e.presentation_asset.segment.total == 2 for e in rows))
            state = review.decision_state_store.load(plan.task_id)
            self.assertEqual(state.movie_version_selection, MovieVersionSelection.from_mapping(plan.metadata['movie_version_selection']))
            self.assertEqual(state.movie_version_selection, DecisionStateStore(review.decision_state_store.root).load(plan.task_id).movie_version_selection)
            validate_selection(plan)
            review.approve_entries(plan.task_id, PlanStore.fingerprint(plan), tuple(entry_id(e) for e in plan.entries))
            changed = review.revise_movie_versions(plan.task_id, PlanStore.fingerprint(plan),
                groups=[dict(sources=list(map(str, videos)), label='加长')])
            self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(changed))
            with self.assertRaises((ValueError, RuntimeError)):
                review.approve_entries(plan.task_id, PlanStore.fingerprint(plan), tuple(entry_id(e) for e in plan.entries))

    async def test_raw_identity_label_and_exclusion_tampering_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            _fixture, completed, videos = await fixture.MovieVersionIncidents('runTest').incoming(Path(raw))
            plan = completed.review.revise_movie_versions(completed.plan.task_id, PlanStore.fingerprint(completed.plan),
                groups=[dict(sources=[str(videos[0])], label='1080p - Airota'),
                        dict(sources=[str(videos[1])], label='备选')], excluded_sources=[str(videos[1])])
            video = next(e for e in plan.entries if e.source_path == videos[0])
            descriptor = video.presentation_asset.version_descriptor
            for field, value in [('version_ref', 'mv-' + 'f' * 32), ('display_label', '伪造标签')]:
                forged = replace(video, presentation_asset=replace(video.presentation_asset,
                    version_descriptor=replace(descriptor, **{field: value})))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    validate_selection(replace(plan, entries=tuple(forged if e is video else e for e in plan.entries)))
            with self.assertRaises(ValueError):
                validate_selection(replace(plan, ignored_sources=()))
            state = completed.review.decision_state_store.load(plan.task_id)
            with self.assertRaises((ValueError, RuntimeError)):
                DecisionStateStore.project_plan(replace(state, movie_version_selection=None), plan)

    async def test_disconnect_projector_prevents_new_accepted_revision(self):
        with tempfile.TemporaryDirectory() as raw:
            _fixture, completed, videos = await fixture.MovieVersionIncidents('runTest').incoming(Path(raw))
            before = PlanStore.fingerprint(completed.plan)
            with patch('src.rename.movie_version_selection.project_selection', side_effect=ValueError('disconnected')):
                with self.assertRaises(ValueError):
                    completed.review.revise_movie_versions(completed.plan.task_id, before,
                        groups=[dict(sources=[str(videos[0])], label='院线'),
                                dict(sources=[str(videos[1])], label='备选')], excluded_sources=[str(videos[1])])
            self.assertEqual(before, PlanStore.fingerprint(completed.store.load(completed.plan.task_id)))

    async def test_normalized_labels_collide_without_source_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            _fixture, completed, videos = await fixture.MovieVersionIncidents('runTest').incoming(Path(raw))
            before = [p.stat().st_mtime_ns for p in videos]
            with self.assertRaises(ValueError):
                completed.review.revise_movie_versions(completed.plan.task_id, PlanStore.fingerprint(completed.plan),
                    groups=[dict(sources=[str(videos[0])], label='Ａirota'), dict(sources=[str(videos[1])], label='airota')])
            self.assertEqual(before, [p.stat().st_mtime_ns for p in videos])

    def test_all_nine_windows_characters_are_visible_fullwidth_labels(self):
        self.assertEqual('a＼／：＊？＂＜＞｜b', safe_label('a\\/:*?"<>|b'))
        for label in ('', '.', 'CON', 'LPT1'):
            with self.subTest(label=label), self.assertRaises(ValueError):
                safe_label(label)

    async def test_third_same_spec_package_coexists_and_replacement_selects_registered_id(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            harness, completed, videos = await fixture.MovieVersionIncidents('runTest').incoming(root)
            plan = completed.review.revise_movie_versions(completed.plan.task_id, PlanStore.fingerprint(completed.plan),
                groups=[dict(sources=[str(v)], label=label) for v, label in zip(videos, ('院线', '加长'))])
            completed.plan = plan
            completed.task = json.loads((root / 'tasks' / f'{plan.task_id}.json').read_text('utf-8'))
            task, _ledger, _executor = await harness._approve_and_execute(root=root, result=completed)
            self.assertEqual('completed', task['workflow_status'])
            source = root / 'sources' / 'New Release [1080p]'
            source.mkdir()
            video = source / 'Example Movie [1080p].mkv'
            shutil.copyfile(videos[0], video)
            initial = await harness._run_provider_flow(root, source, 'third-version')
            third = await harness._complete_movie(root=root, result=initial, primary=video)
            third.plan = third.review.revise_movie_versions(third.plan.task_id, PlanStore.fingerprint(third.plan),
                groups=[dict(sources=[str(video)], label='1080p - Airota')])
            third.task = json.loads((root / 'tasks' / f'{third.plan.task_id}.json').read_text('utf-8'))
            task, _ledger, _executor = await harness._approve_and_execute(root=root, result=third)
            self.assertEqual('completed', task['workflow_status'])
            record = third.review.work_catalog.load(plan.metadata['proposed_work_key'])
            self.assertEqual({'院线', '加长', '1080p - Airota'}, {row['display_label'] for row in record['versions']})
            replacement_source = root / 'sources' / 'Replacement [2160p]'
            replacement_source.mkdir()
            replacement_video = replacement_source / 'Example Movie [2160p].mkv'
            shutil.copyfile(videos[0], replacement_video)
            initial = await harness._run_provider_flow(root, replacement_source, 'selected-replacement')
            incoming = await harness._complete_movie(root=root, result=initial, primary=replacement_video)
            selected = next(row['version_ref'] for row in record['versions'] if row['display_label'] == '院线')
            from src.pages import plan_review_page as page
            from nicegui import Client, ui
            from nicegui.page import page as gui_page
            from tests._gui_helpers import GuiDriver
            client = Client(gui_page('/isolated-replacement-version'), request=None)
            self.addCleanup(client.delete)
            driver = GuiDriver('runTest'); driver.client = client
            dialog = SimpleNamespace(plan=incoming.plan, review_service=incoming.review,
                _video_options=lambda: {str(replacement_video): replacement_video.name},
                _selected_movie_primary_sources=lambda: [str(replacement_video)])
            dialog._adopt_revision = lambda plan: setattr(dialog, 'plan', plan)
            from src.pages.asset_choice_forms import show_movie_versions
            with client:
                show_movie_versions(dialog,incoming.plan.presentation_groups[0].group_id,ui=ui,
                    notify=lambda *a,**k:None,complete_movie=page.PlanReviewDialog._complete_movie_with_primary)
                selector = next(e for e in client.elements.values()
                    if type(e).__name__ == 'Select' and e.props.get('label') == '已有版本换源')
                self.assertEqual({'新增独立版本', '院线', '加长', '1080p - Airota'}, set(selector.options.values()))
                selector.value = selected
                await driver.click('保存正片版本')
            revised = dialog.plan
            self.assertEqual({selected}, {e.presentation_asset.version_descriptor.version_ref for e in revised.entries
                if e.presentation_asset and e.presentation_asset.version_descriptor})
            self.assertEqual(record, incoming.review.work_catalog.load(plan.metadata['proposed_work_key']))
            self.assertEqual(selected, revised.metadata['movie_version_selection']['replacement']['version_ref'])

    async def test_confirmed_cm_remains_extra_while_unselected_main_requires_choice(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            harness, completed, videos = await fixture.MovieVersionIncidents('runTest').incoming(root, complete=False)
            unresolved = next(e for e in completed.plan.entries if e.source_path == videos[1])
            self.assertEqual('unknown', unresolved.presentation_asset.asset_role.kind)
            self.assertNotIn('Extras', unresolved.target_path.parts)
            self.assertNotIn(videos[1], completed.plan.ignored_sources)
            source = root / 'sources' / 'CM Control [1080p]'
            source.mkdir()
            primary = source / 'Example Movie [1080p].mkv'
            cm = source / 'Example Movie [CM01].mkv'
            shutil.copyfile(videos[0], primary)
            shutil.copyfile(videos[0], cm)
            initial = await harness._run_provider_flow(root, source, 'cm-control')
            ordinary = await harness._complete_movie(root=root, result=initial, primary=primary)
            self.assertIn(cm, ordinary.plan.ignored_sources)
            # R02 withdrew CM output; retain the unknown-main positive above.
            self.assertNotIn(cm, {e.source_path for e in ordinary.plan.entries})
            self.assertIn(cm, ordinary.plan.expected_sources)
            self.assertEqual(primary.read_bytes(), cm.read_bytes())
            self.assertFalse(ordinary.review.load_or_create(ordinary.plan).approved_entry_ids)


if __name__ == '__main__':
    unittest.main()
