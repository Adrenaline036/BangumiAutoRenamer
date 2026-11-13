"""Complete movie choices on disposable content tasks; no real application."""
from pathlib import Path
from dataclasses import replace
import json
import tempfile
import unittest
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from tests.test_content_choice_contract import change, identifier, migrated, records


def groups(plan):
    return [dict(sources=row['sources'], label=row['label'], technical_descriptor=row['descriptor'])
            for row in registry(plan)]


def registry(plan):
    from src.rename.content_selection import from_plan
    return from_plan(plan).to_dict()['version_groups']


def revise(service, plan, rows, excluded=(), **kwargs):
    return service.revise_movie_versions(plan.task_id, PlanStore.fingerprint(plan),
        groups=rows, excluded_sources=list(map(str, excluded)), **kwargs)


class ContentMovieLifecycle(unittest.TestCase):
    def prepare(self, service, plan, rows=None, excluded=(), **kwargs):
        command = getattr(service, 'prepare_movie_version_change', None)
        self.assertTrue(callable(command), 'missing frozen movie edit preparation')
        return command(plan.task_id, PlanStore.fingerprint(plan), groups=rows or groups(plan),
            excluded_sources=list(map(str, excluded)), **kwargs)

    def test_preview_is_read_only_and_stale_or_source_drift_rejects_before_writes(self):
        from tests.test_content_lifecycle_contract import alter_same_stat
        for fault in ('current_revision', 'same_stat_exclusion'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                service, plan, videos = migrated(root)
                plan = change(service, plan, [videos[1]])
                before = records(root)
                intent = self.prepare(service, plan, excluded=[videos[1]])
                self.assertEqual(before, records(root))
                if fault == 'current_revision':
                    change(service, plan, [videos[0].with_suffix('.mka')])
                else:
                    alter_same_stat(videos[1])
                before = records(root)
                if fault == 'current_revision':
                    with self.assertRaises((ValueError, RuntimeError)):
                        service.commit_movie_version_change(intent)
                    self.assertEqual(before, records(root))
                else:
                    # An excluded video's bytes are not an edit precondition.
                    source_before = {p: p.read_bytes() for p in plan.expected_sources}
                    revised = service.commit_movie_version_change(intent)
                    self.assertIn(videos[1], revised.ignored_sources)
                    self.assertNotIn(videos[1], {e.source_path for e in revised.entries})
                    self.assertFalse(service.load_or_create(revised).approved_entry_ids)
                    self.assertEqual(source_before, {p: p.read_bytes() for p in source_before})
                    self.assertFalse(any(e.target_path.exists() for e in revised.entries))

    def test_canonical_movie_intent_and_visible_contract_are_not_bypassable(self):
        from src.rename.decision_state import DecisionCommandService
        from src.rename.content_selection import ContentSelection
        original = DecisionCommandService._commit_projection
        for fault in ('no_intent', 'wrong_task', 'no_context', 'wrong_context_root',
                      'no_intent_or_context', 'disconnected_builder', 'visible_video',
                      'visible_subtitle', 'forged_group', 'refreshed_omission'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                service, plan, videos = migrated(root)
                plan = change(service, plan, [videos[1]])
                rows = groups(plan)
                rows[0]['label'] = '本轮修改'
                intent = self.prepare(service, plan, rows, [videos[1]])
                before, reached = records(root), []
                def submit(command, previous, candidate, draft, **kwargs):
                    reached.append(True)
                    if fault == 'no_intent':
                        kwargs['movie_change'] = None
                    elif fault == 'wrong_task':
                        kwargs['movie_change'] = replace(kwargs['movie_change'], task_id='foreign-task')
                    elif fault == 'no_context':
                        command.movie_change_context = None
                    elif fault == 'wrong_context_root':
                        command.movie_change_context = replace(command.movie_change_context,
                            catalog_root=root / 'foreign-context')
                    elif fault == 'no_intent_or_context':
                        kwargs['movie_change'] = None
                        command.movie_change_context = None
                    elif fault == 'disconnected_builder':
                        with patch('src.rename.movie_content.build_movie_change', return_value=None):
                            return original(command, previous, candidate, draft, **kwargs)
                    elif fault == 'visible_video':
                        draft = replace(draft, entries=tuple(replace(e, presentation_asset=replace(
                            e.presentation_asset, version_descriptor=replace(
                                e.presentation_asset.version_descriptor, display_label='篡改显示合同')))
                            if e.source_path == videos[0] else e for e in draft.entries))
                    elif fault == 'visible_subtitle':
                        draft = replace(draft, entries=tuple(replace(e, subtitle=replace(
                            e.subtitle, track_title_tokens=('injected',)))
                            if e.source_path == videos[0].with_suffix('.sc.ass') else e for e in draft.entries))
                    else:
                        value = candidate.content_selection.to_dict()
                        if fault == 'forged_group':
                            value['version_groups'][0]['label'] = '篡改完整证明'
                            value['version_groups'][0]['descriptor']['display_label'] = '篡改完整证明'
                        else:
                            value['evidence'][0]['sha256'] = '0' * 64
                        candidate = replace(candidate, content_selection=ContentSelection.from_mapping(value))
                    return original(command, previous, candidate, draft, **kwargs)
                with patch('src.rename.decision_state.DecisionCommandService._commit_projection', submit):
                    with self.assertRaises((ValueError, RuntimeError)):
                        service.commit_movie_version_change(intent)
                self.assertTrue(reached, 'must reach canonical transport')
                self.assertEqual(before, records(root))

    def test_canonical_catalog_lock_is_held_before_projection_and_missing_snapshot_is_bound(self):
        from src.rename.projection_authority import ProjectionCommitService
        original = ProjectionCommitService._commit_locked
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, _videos = migrated(root)
            key = plan.metadata['movie_version_selection']['work_key']
            self.assertFalse(service.work_catalog._path(key).exists())
            intent = self.prepare(service, plan)
            reached = []
            def commit(transaction, envelope, **kwargs):
                reached.append(True)
                with self.assertRaises((ValueError, RuntimeError)):
                    with service.work_catalog.lock(key):
                        pass
                return original(transaction, envelope, **kwargs)
            with patch('src.rename.projection_authority.ProjectionCommitService._commit_locked', commit):
                revised = service.commit_movie_version_change(intent)
            self.assertEqual([True], reached)
            self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(revised))
            intent = self.prepare(service, revised)
            # Only disposable fixture Catalog: absent -> malformed is still drift,
            # never a new binding accepted from a caller's saved path.
            catalog = service.work_catalog._path(key)
            catalog.parent.mkdir(parents=True, exist_ok=True)
            catalog.write_bytes(b'{}')
            before = records(root)
            with self.assertRaises((ValueError, RuntimeError)):
                service.commit_movie_version_change(intent)
            self.assertEqual(before, records(root))

    def test_movie_revision_journal_windows_preserve_previous_records_and_retry(self):
        for stage in ('prepared', 'decision_written', 'ownership_claimed', 'plan_written'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                service, plan, videos = migrated(root)
                plan = change(service, plan, [videos[1]])
                rows = groups(plan)
                rows[1]['label'] = '仍隐藏的新标签'
                before = {k: v for k, v in records(root).items() if not k.startswith('projection_journal/')}
                reached = []
                def checkpoint(_service, phase):
                    if phase == stage:
                        reached.append(phase)
                        raise RuntimeError('movie commit interrupted:' + stage)
                with patch('src.rename.projection_authority.ProjectionCommitService._checkpoint', checkpoint):
                    with self.assertRaisesRegex(RuntimeError, 'movie commit interrupted:' + stage):
                        revise(service, plan, rows, [videos[1]])
                self.assertEqual([stage], reached)
                after = {k: v for k, v in records(root).items() if not k.startswith('projection_journal/')}
                self.assertEqual(before, {k: after.get(k) for k in before})
                for key in after.keys() - before.keys():
                    self.assertEqual('plan_written', stage)
                    self.assertTrue(key.startswith(f'plans/history/{plan.task_id}/'))
                    fingerprint = Path(key).stem
                    self.assertEqual(fingerprint, PlanStore.fingerprint(
                        service.plan_store.load_version(plan.task_id, fingerprint)))
                revised = revise(service, plan, rows, [videos[1]])
                revised = change(service, revised, [videos[1]], action='restore')
                self.assertEqual('仍隐藏的新标签', registry(revised)[1]['label'])

    def test_merge_maps_preference_and_split_requires_explicit_unambiguous_choice(self):
        with tempfile.TemporaryDirectory() as raw:
            service, plan, videos = migrated(Path(raw))
            plan = revise(service, plan, groups(plan), preferred_source=videos[0])
            plan = revise(service, plan, [dict(sources=list(map(str, videos[:2])), label='合并分段')])
            ref = registry(plan)[0]['version_ref']
            self.assertEqual(ref, plan.presentation_groups[0].preferred_version_ref)
            split = [dict(sources=[str(p)], label=f'拆分{i}') for i, p in enumerate(videos[:2])]
            before = records(Path(raw))
            with self.assertRaisesRegex((ValueError, RuntimeError), '偏好|重新选择|歧义'):
                revise(service, plan, split)
            self.assertEqual(before, records(Path(raw)))
            plan = revise(service, plan, split, preferred_source=videos[1])
            self.assertEqual(registry(plan)[1]['version_ref'], plan.presentation_groups[0].preferred_version_ref)

    def test_extra_owner_remaps_only_when_unambiguous(self):
        from tests.test_err060_role_entry import movie_dialog
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            dialog, videos = movie_dialog(root)
            service, plan = dialog.review_service, dialog.plan
            ref = plan.metadata['movie_version_selection']['groups'][0]['version_ref']
            plan = service.revise_presentation_asset(plan.task_id, PlanStore.fingerprint(plan),
                identifier(plan, videos[2]), presentation_group='movie-continuous', role_kind='extra',
                role_subtype='trailer', owner_version_ref=ref, reason='特典明确属于原第一版本')
            plan = service.commit_content_change(service.prepare_content_migration(
                plan.task_id, PlanStore.fingerprint(plan), reason='保留已明确的特典所属版本'))
            plan = revise(service, plan, [dict(sources=list(map(str, videos[:2])), label='合并版本')])
            merged_ref = registry(plan)[0]['version_ref']
            extra = next(e for e in plan.entries if e.source_path == videos[2])
            self.assertEqual(merged_ref, extra.presentation_asset.owner_version_ref)
            before = records(root)
            with self.assertRaisesRegex((ValueError, RuntimeError), '所属|重新选择|歧义'):
                revise(service, plan, [dict(sources=[str(p)], label=f'拆分{i}')
                    for i, p in enumerate(videos[:2])], preferred_source=videos[0])
            self.assertEqual(before, records(root))


    def test_hidden_registry_allows_one_kept_replacement_and_normal_edits_clear_it(self):
        from src.rename.work_catalog import WORK_CATALOG_SCHEMA_VERSION
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root)
            plan = change(service, plan, [videos[1], videos[2]])
            key = plan.metadata['movie_version_selection']['work_key']
            first = registry(plan)[0]
            entry = next(e for e in plan.entries if e.source_path == videos[0])
            # Typed Catalog input fixture only, not an assertion of execution.
            row = dict(target=str(entry.target_path), source=str(videos[0]), version_ref=first['version_ref'],
                technical_variant_key=first['descriptor']['technical_variant_key'], display_label=first['label'],
                task_id='previous-component-task', ledger_fingerprint='a' * 64)
            payload = dict(schema_version=WORK_CATALOG_SCHEMA_VERSION, work_key=key, revision=1,
                title='Movie', year=2026, domain='animation', group_kind='movie_continuous',
                canonical_root=plan.metadata['canonical_work_root'], aliases=[],
                providers=[plan.work_identity.primary_provider.to_dict()], versions=[row], assets=[])
            catalog = service.work_catalog._path(key)
            catalog.parent.mkdir(parents=True, exist_ok=True)
            catalog.write_text(json.dumps(payload), encoding='utf-8')
            service.work_catalog.load(key)
            intent = self.prepare(service, plan, excluded=[videos[1]], replace_version_ref=first['version_ref'])
            altered = {**payload, 'title': 'same revision, changed Catalog evidence'}
            catalog.write_text(json.dumps(altered), encoding='utf-8')
            before = records(root)
            with self.assertRaises((ValueError, RuntimeError)):
                service.commit_movie_version_change(intent)
            self.assertEqual(before, records(root))
            catalog.write_text(json.dumps(payload), encoding='utf-8')
            before = records(root)
            with self.assertRaises((ValueError, RuntimeError)):
                revise(service, plan, groups(plan), [videos[1]], replace_version_ref='mv-' + 'f' * 32)
            self.assertEqual(before, records(root))
            plan = revise(service, plan, groups(plan), [videos[1]], replace_version_ref=first['version_ref'])
            self.assertEqual(2, len(registry(plan)))
            self.assertEqual(first['version_ref'], plan.metadata['movie_version_selection']['replacement']['version_ref'])
            plan = change(service, plan, [videos[1]], action='restore')
            self.assertIsNone(plan.metadata['movie_version_selection']['replacement'])
            plan = change(service, plan, [videos[1]])
            plan = revise(service, plan, groups(plan), [videos[1]], replace_version_ref=first['version_ref'])
            plan = revise(service, plan, groups(plan), [videos[1]])
            self.assertIsNone(plan.metadata['movie_version_selection']['replacement'])
            self.assertNotEqual('replace', (plan.metadata.get('asset_operation') or {}).get('mode'))

    def test_no_plan_intent_and_disconnected_shared_validator_are_rejected(self):
        from src.rename.decision_state import DecisionCommandService
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, _videos = migrated(root)
            intent = self.prepare(service, plan)
            before = records(root)
            state = service.decision_state_store.load(plan.task_id)
            with self.assertRaises(TypeError):
                DecisionCommandService(service.decision_state_store, service.plan_store).update(
                    plan.task_id, expected_revision=state.revision, changes={}, movie_change=intent)
            self.assertEqual(before, records(root))
            with patch('src.rename.movie_content.validate_movie_commit', return_value=None) as guard:
                with self.assertRaises((ValueError, RuntimeError)):
                    service.commit_movie_version_change(intent)
            guard.assert_called()
            self.assertEqual(before, records(root))

    def test_label_and_hidden_edit_survive_restore_without_reviving_independent_subtitle(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root)
            sub = videos[0].with_suffix('.sc.ass')
            plan = change(service, plan, [sub, videos[2]])
            plan = change(service, plan, [videos[1]])
            old = registry(plan)
            rows = groups(plan)
            rows[0]['label'] = '本轮修订版'
            revised = revise(service, plan, rows, [videos[1]])
            current_groups = registry(revised)
            self.assertEqual(old[0]['version_ref'], current_groups[0]['version_ref'])
            self.assertEqual(old[1], current_groups[1], 'untouched hidden group must remain exact')
            self.assertEqual('本轮修订版', current_groups[0]['label'])
            self.assertEqual([str(videos[1])], list(revised.metadata['movie_version_selection']['excluded_sources']))
            rows = groups(revised)
            rows[1]['label'] = '隐藏版新标签'
            revised = revise(service, revised, rows, [videos[1]])
            revised = change(service, revised, [videos[1]], action='restore')
            revised = change(service, revised, [videos[0]])
            revised = change(service, revised, [videos[0]], action='restore')
            self.assertIn(sub, revised.ignored_sources)
            self.assertIn(videos[2], revised.ignored_sources)
            for source, label in zip(videos[:2], ('本轮修订版', '隐藏版新标签')):
                entry = next(e for e in revised.entries if e.source_path == source)
                self.assertEqual(label, entry.presentation_asset.version_descriptor.display_label)
                self.assertIn(label, entry.target_path.name)
            self.assertEqual(PlanStore.fingerprint(revised),
                PlanStore.fingerprint(service.plan_store.load(revised.task_id)))

    def test_segment_order_is_frozen_independently_of_member_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root, segments=True)
            rows = groups(plan)
            original_ref = plan.metadata['content_selection']['version_groups'][0]['version_ref']
            rows[0]['sources'].reverse()
            revised = revise(service, plan, rows)
            self.assertEqual(original_ref, revised.metadata['content_selection']['version_groups'][0]['version_ref'])
            revised = change(service, revised, [videos[0]])
            revised = change(service, revised, [videos[0]], action='restore')
            self.assertEqual(list(map(str, reversed(videos[:2]))),
                registry(revised)[0]['sources'])
            for index, source in enumerate(reversed(videos[:2]), 1):
                entry = next(e for e in revised.entries if e.source_path == source)
                self.assertEqual(index, entry.presentation_asset.segment.index)
                self.assertIn(f'-cd{index}', entry.target_path.stem)
            for fault in ('missing_member', 'duplicate_member', 'partial_exclusion'):
                with self.subTest(fault=fault):
                    invalid = groups(revised)
                    excluded = []
                    if fault == 'missing_member':
                        invalid[0]['sources'].pop()
                    elif fault == 'duplicate_member':
                        invalid[1]['sources'].append(str(videos[0]))
                    else:
                        excluded = [videos[0]]
                    before = records(root)
                    with self.assertRaises((ValueError, RuntimeError)):
                        revise(service, revised, invalid, excluded)
                    self.assertEqual(before, records(root))

    def test_single_subtitle_ignore_uses_content_choice_not_version_revision(self):
        with tempfile.TemporaryDirectory() as raw:
            service, plan, videos = migrated(Path(raw))
            original_movie = plan.metadata['movie_version_selection']
            sub = videos[0].with_suffix('.sc.ass')
            revised = service.ignore_plan_entry(plan.task_id, PlanStore.fingerprint(plan),
                identifier(plan, sub), reason='只排除此字幕，不改变正片版本')
            self.assertEqual(original_movie, revised.metadata['movie_version_selection'])
            self.assertIn(sub, revised.ignored_sources)
            self.assertTrue(set(videos[:2]) <= {e.source_path for e in revised.entries})
            self.assertIn(str(sub), revised.metadata['content_selection']['excluded_sources'])


class ContentMovieGui(unittest.IsolatedAsyncioTestCase):
    async def test_registered_form_edits_only_selected_group_and_preserves_excluded_label(self):
        from nicegui import ui
        from nicegui.client import Client
        from nicegui.page import page
        from types import SimpleNamespace
        from src.pages.asset_choice_forms import show_movie_versions
        from tests._gui_helpers import GuiDriver
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root)
            plan = change(service, plan, [videos[1], videos[2]])
            owner = SimpleNamespace(plan=plan, review_service=service)
            owner._video_options = lambda: {str(e.source_path): e.source_path.name
                for e in owner.plan.entries if e.media_kind == 'video'}
            owner._selected_movie_primary_sources = lambda: [p for g in
                owner.plan.metadata['movie_version_selection']['groups'] for p in g['sources']]
            owner._adopt_revision = lambda revised: setattr(owner, 'plan', revised)
            notices = []
            client = Client(page('/isolated-content-movie'), request=None)
            self.addCleanup(client.delete)
            driver = GuiDriver('runTest')
            driver.client = client
            def open_form():
                with client:
                    show_movie_versions(owner, 'movie-continuous', ui=ui,
                        notify=lambda text, **kwargs: notices.append((text, kwargs)),
                        complete_movie=lambda *_a, **_k: self.fail('not an initial inventory draft'))
            before = records(root)
            open_form()
            labels = [e for e in client.elements.values() if e._props.get('label') == '版本标签']
            self.assertEqual(1, len(labels), 'excluded groups must not demand an editable label')
            await driver.click('关闭')
            self.assertEqual(before, records(root))
            client = Client(page('/isolated-content-movie-save'), request=None)
            self.addCleanup(client.delete)
            driver.client = client
            open_form()
            labels = [e for e in client.elements.values() if e._props.get('label') == '版本标签']
            labels[0].value = '所选版本的新标签'
            old_hidden = owner.plan.metadata['content_selection']['version_groups'][1]['label']
            with patch('src.rename.plan_review.PlanReviewService.revise_movie_versions',
                    side_effect=ValueError('本轮保存失败控制')):
                await driver.click('保存正片版本')
            self.assertFalse(notices)
            self.assertEqual(before, records(root))
            self.assertTrue(any(getattr(e, 'text', '') == '本轮保存失败控制' for e in client.elements.values()))
            await driver.click('保存正片版本')
            self.assertTrue(notices, 'registered save must publish a successful revision')
            self.assertIn(videos[1], owner.plan.ignored_sources)
            self.assertEqual(old_hidden,
                owner.plan.metadata['content_selection']['version_groups'][1]['label'])


if __name__ == '__main__':
    unittest.main()
