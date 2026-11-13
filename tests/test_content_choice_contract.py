"""Content choices through real disposable review/Decision/Plan transactions.

No application entry, real media, NAS, or retained font fixture is involved.
"""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from tests.test_err060_role_entry import movie_dialog


def records(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and 'source' not in p.relative_to(root).parts
            and not p.name.endswith('.lock')}


def identifier(plan, source):
    # Identifiers are GUI selectors, not durable file identity.
    return entry_id(next(e for e in plan.entries if e.source_path == source))


def migrated(root, *, segments=False, executable=False):
    dialog, videos = movie_dialog(root, segments=segments, dry_run=not executable)
    service = dialog.review_service
    plan = dialog.plan
    if segments:
        value = dialog.task_decision.movie_version_selection.to_dict()
        plan = service.revise_movie_versions(plan.task_id, PlanStore.fingerprint(plan),
            groups=[*[dict(sources=g['sources'], label=g['label']) for g in value['groups']],
                    dict(sources=[str(videos[2])], label='另一完整版本')])
    return service, plan, videos


def change(service, plan, sources, *, action='exclude'):
    if action == 'restore':
        # A removed source is displayed by its saved source material, not a scan.
        keys = tuple(entry_id(replace(plan.entries[0], source_path=p)) for p in sources)
    else:
        keys = tuple(identifier(plan, p) for p in sources)
    preview = service.prepare_content_change(plan.task_id, PlanStore.fingerprint(plan),
        selected_entry_ids=keys, action=action, reason='用户明确选择本次范围')
    return service.commit_content_change(preview)


class ContentChoiceContract(unittest.TestCase):
    def test_canonical_first_install_rejects_forged_episode_material(self):
        from tests.test_err060_tv_inventory_retry import completed_tv
        from src.rename.content_choice import _draft
        from src.rename.content_selection import ContentSelection
        from src.rename.decision_state import DecisionCommandService
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, _ = completed_tv(root)
            state = service.decision_state_store.load(plan.task_id)
            preview = service.prepare_content_migration(plan.task_id, PlanStore.fingerprint(plan), reason='明确迁移')
            value = preview.to_dict()['selection']
            row = next(row for row in value['materials'] if row['source_path'].endswith('S01E01.mkv'))
            self.assertEqual(1, row['episode_id'])
            row['episode_id'] = 99
            forged = ContentSelection.from_mapping(value)
            draft = _draft(service, plan, state, forged, None, 'migration')
            before = records(root)
            with self.assertRaisesRegex((ValueError, RuntimeError), '首次|迁移|来源'):
                DecisionCommandService(service.decision_state_store, service.plan_store).update(
                    plan.task_id, expected_revision=state.revision,
                    changes={'content_selection': forged}, planner=lambda _: draft)
            self.assertEqual(before, records(root))
            # The same canonical route must still permit the authentic preview.
            result = service.commit_content_change(preview)
            original = next(e for e in result.entries if e.source_path.name.endswith('S01E01.mkv'))
            result = change(service, result, [original.source_path])
            result = change(service, result, [original.source_path], action='restore')
            self.assertEqual(original.target_path, next(e.target_path for e in result.entries
                if e.source_path == original.source_path))

    def test_canonical_first_install_binds_groups_relations_origin_and_old_ignored(self):
        from src.rename.content_choice import _draft
        from src.rename.content_selection import ContentSelection
        from src.rename.decision_state import DecisionCommandService
        from src.rename.movie_version_selection import MovieVersionSelection
        for attack in ('group_order', 'relation', 'origin', 'old_ignored'):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                dialog, videos = movie_dialog(root, segments=True)
                service, plan = dialog.review_service, dialog.plan
                state = service.decision_state_store.load(plan.task_id)
                preview = service.prepare_content_migration(plan.task_id, PlanStore.fingerprint(plan), reason='明确迁移')
                value = preview.to_dict()['selection']
                movie_value = preview.to_dict()['movie_selection']
                if attack == 'group_order':
                    value['version_groups'][0]['sources'].reverse()
                    movie_value['groups'][0]['sources'].reverse()
                elif attack == 'relation':
                    row = next(r for r in value['associations']['relations']
                        if r['source_path'] == str(videos[0].with_suffix('.sc.ass')))
                    row.update(owners=[str(videos[2])], state='linked', source='manual')
                elif attack == 'origin':
                    value['origin_plan_fingerprint'] = 'f' * 64
                else:
                    self.assertTrue(value['excluded_sources'])
                    value.update(excluded_sources=[], evidence=[])
                choice = ContentSelection.from_mapping(value)
                movie = MovieVersionSelection.from_mapping(movie_value)
                draft = _draft(service, plan, state, choice, movie, 'migration')
                before = records(root)
                with self.assertRaisesRegex((ValueError, RuntimeError), '首次|迁移|来源'):
                    DecisionCommandService(service.decision_state_store, service.plan_store).update(
                        plan.task_id, expected_revision=state.revision,
                        changes={'content_selection': choice, 'movie_version_selection': movie}, planner=lambda _: draft)
                self.assertEqual(before, records(root))
                service.commit_content_change(preview)

    def test_regular_tv_episode_restores_from_frozen_episode_fact_not_old_path(self):
        from tests.test_err060_tv_inventory_retry import completed_tv
        with tempfile.TemporaryDirectory() as raw:
            service, plan, _ = completed_tv(Path(raw))
            preview = service.prepare_content_migration(plan.task_id, PlanStore.fingerprint(plan), reason='明确迁移')
            plan = service.commit_content_change(preview)
            original = next(e for e in plan.entries if e.source_path.name.endswith('S01E01.mkv'))
            plan = change(service, plan, [original.source_path])
            plan = change(service, plan, [original.source_path], action='restore')
            restored = next(e for e in plan.entries if e.source_path == original.source_path)
            self.assertEqual(original.target_path, restored.target_path)
            self.assertEqual(original.episode_id, restored.episode_id)
            self.assertEqual(original.presentation_asset, restored.presentation_asset)
            material = next(row for row in service.decision_state_store.load(plan.task_id).content_selection.to_dict()['materials']
                if row['source_path'] == str(original.source_path))
            self.assertNotIn('target_path', material)
            self.assertNotIn('collision_resolution', material)

    def test_new_contract_does_not_fall_back_to_old_whole_plan_writers(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root)
            before = records(root)
            state = service.decision_state_store.load(plan.task_id)
            with patch('src.rename.content_selection.require_legacy_writer',
                    side_effect=AssertionError('new movie content entered legacy writer')) as legacy:
                plan = service.revise_movie_versions(plan.task_id, PlanStore.fingerprint(plan),
                    groups=[dict(sources=row['sources'], label=row['label'])
                        for row in state.content_selection.to_dict()['version_groups']])
            legacy.assert_not_called()
            before = records(root)
            with self.assertRaisesRegex((ValueError, RuntimeError), '统一|迁移'):
                service.recomplete_inventory_with_role_decisions(plan.task_id, PlanStore.fingerprint(plan),
                    decisions={identifier(plan, videos[2]): 'extra'}, reason='旧用途按钮')
            self.assertEqual(before, records(root))

    def test_forged_preview_cannot_change_selection_without_matching_user_intent(self):
        from src.rename.content_choice import ContentChangePreview
        from src.rename.content_associations import encode
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root)
            preview = service.prepare_content_change(plan.task_id, PlanStore.fingerprint(plan),
                selected_entry_ids=[identifier(plan, videos[2])], action='exclude', reason='只排预告')
            value = preview.to_dict()
            value['selected_entry_ids'] = [identifier(plan, videos[0].with_suffix('.sc.ass'))]
            forged = ContentChangePreview(encode(value))
            before = records(root)
            with self.assertRaisesRegex((ValueError, RuntimeError), '预览|选择'):
                service.commit_content_change(forged)
            self.assertEqual(before, records(root))


    def test_prior_exclusion_is_not_reobserved_when_making_another_choice(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root)
            plan = change(service, plan, [videos[2]])
            before = records(root)
            stat = videos[2].stat()
            videos[2].write_bytes(b'y' * stat.st_size)
            os.utime(videos[2], ns=(stat.st_atime_ns, stat.st_mtime_ns))
            revised = change(service, plan, [videos[0].with_suffix('.sc.ass')])
            self.assertIn(videos[2], revised.ignored_sources)
            self.assertIn(videos[0].with_suffix('.sc.ass'), revised.ignored_sources)

    def test_approval_and_media_admission_use_shared_gate_not_font_only_authority(self):
        import asyncio
        from tests import _media_flow_helpers as movie_fixture
        from tests._media_flow_helpers import _make_version
        from src.rename.plan_execution import PlanExecutionService
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            harness = movie_fixture.MediaFlow('runTest')
            self.addCleanup(harness.doCleanups)
            async def current_movie():
                source, primary, _font = _make_version(root / 'source', '1080p')
                first = await harness._run_provider_flow(root, source, 'content-admission')
                return await harness._complete_movie(root=root, result=first, primary=primary)
            completed = asyncio.run(current_movie())
            service, plan = completed.review, completed.plan
            self.assertEqual('initial_movie_completion', plan.revision_action, completed.form_messages)
            audio = next(e.source_path for e in plan.entries if e.source_path.suffix == '.mka')
            plan = change(service, plan, [audio])
            fp = PlanStore.fingerprint(plan)
            before = records(root)
            with patch('src.rename.content_selection.validate_omission_manifest',
                       side_effect=ValueError('内容证据门断开')) as guard:
                with self.assertRaisesRegex((ValueError, RuntimeError), '内容证据门断开'):
                    service.approve_entries(plan.task_id, fp, [entry_id(e) for e in plan.entries])
                guard.assert_called()
            self.assertEqual(before, records(root))
            service.approve_entries(plan.task_id, fp, [entry_id(e) for e in plan.entries])
            execution = PlanExecutionService(service.plan_store, service,
                task_root=service.task_root, record_root=service.record_root,
                catalog_root=service.work_catalog.root, series_catalog_root=service.series_catalog.root,
                decision_state_root=service.decision_state_store.root,
                recovery_transaction_root=root / 'recovery_transactions',
                provider_link_catalog_root=service.provider_link_catalog.root)
            before = records(root)
            with patch('src.rename.content_selection.validate_omission_manifest',
                       side_effect=ValueError('内容证据门断开')) as guard:
                with self.assertRaisesRegex((ValueError, RuntimeError), '内容证据门断开'):
                    execution.validate_approved(plan.task_id, fp)
                guard.assert_called()
                guard.reset_mock()
                execution.validate_approved_authority(plan.task_id, fp)
                guard.assert_not_called()
            self.assertEqual(before, records(root))

    def test_projection_crashes_roll_back_complete_content_decision_and_plan(self):
        from src.rename.projection_authority import ProjectionCommitService

        class SimulatedCrash(BaseException):
            pass

        for phase in ('prepared', 'decision_written', 'ownership_claimed', 'plan_written'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                service, plan, videos = migrated(root)
                previous = service.decision_state_store.load(plan.task_id)
                preview = service.prepare_content_change(plan.task_id, PlanStore.fingerprint(plan),
                    selected_entry_ids=[identifier(plan, videos[2])], action='exclude', reason='中断控制')
                reached = []

                def crash(_service, current_phase):
                    reached.append(current_phase)
                    if current_phase == phase:
                        raise SimulatedCrash(phase)

                with patch('src.rename.projection_authority.ProjectionCommitService._checkpoint', new=crash):
                    with self.assertRaises(SimulatedCrash):
                        service.commit_content_change(preview)
                self.assertIn(phase, reached)
                projection = ProjectionCommitService(service.decision_state_store, service.plan_store,
                    task_root=service.task_root)
                self.assertEqual('failed_closed', projection.recover(plan.task_id))
                self.assertEqual(previous, service.decision_state_store.load(plan.task_id))
                self.assertEqual(PlanStore.fingerprint(plan),
                    PlanStore.fingerprint(service.plan_store.load(plan.task_id)))
                fresh = service.prepare_content_change(plan.task_id, PlanStore.fingerprint(plan),
                    selected_entry_ids=[identifier(plan, videos[2])], action='exclude', reason='重新明确选择')
                revised = service.commit_content_change(fresh)
                self.assertIn(videos[2], revised.ignored_sources)

    def test_subtitle_rule_association_and_undo_preserve_later_content_choice(self):
        from src.rename.rename_plan import SubtitleOverrideScope
        from src.rename import special_decision_projection, movie_version_selection
        with tempfile.TemporaryDirectory() as raw:
            service, plan, videos = migrated(Path(raw))
            plan = change(service, plan, [videos[2]])
            subtitle = videos[0].with_suffix('.sc.ass')
            plan = service.apply_subtitle_override_rule(plan.task_id, PlanStore.fingerprint(plan),
                identifier(plan, subtitle), scope=SubtitleOverrideScope.FILE,
                associated_video_source_path=videos[1], primary_language='en', script=None,
                reason='手动更正这条字幕所属的视频')
            rule = plan.subtitle_override_rules[-1].rule_id
            state = service.decision_state_store.load(plan.task_id)
            associations = state.content_selection.associations
            self.assertIn(subtitle, movie_version_selection.exclusive_companions(
                plan.entries, [videos[1]], associations=associations))
            self.assertNotIn(subtitle, movie_version_selection.exclusive_companions(
                plan.entries, [videos[0]], associations=associations))
            bundle = special_decision_projection.video_bundle_closure(plan, identifier(plan, videos[1]))
            self.assertIn(subtitle, {e.source_path for _, e in bundle})
            audio = videos[0].with_suffix('.mka')
            plan = change(service, plan, [audio])
            plan = service.revert_subtitle_rule(plan.task_id, PlanStore.fingerprint(plan), rule,
                reason='只撤销最后字幕规则，不撤销其它选择')
            self.assertIn(audio, plan.ignored_sources)
            self.assertIn(videos[2], plan.ignored_sources)
            contract = next(e.subtitle for e in plan.entries if e.source_path == subtitle)
            self.assertEqual(videos[0], contract.associated_video_source_path)
            associations = service.decision_state_store.load(plan.task_id).content_selection.associations
            self.assertIn(subtitle, movie_version_selection.exclusive_companions(
                plan.entries, [videos[0]], associations=associations))

    def test_explicit_special_can_be_excluded_and_restored_without_losing_other_decisions(self):
        from tests.test_err060_tv_inventory_retry import completed_tv
        with tempfile.TemporaryDirectory() as raw:
            service, plan, source = completed_tv(Path(raw))
            preview = service.prepare_content_migration(plan.task_id, PlanStore.fingerprint(plan),
                reason='明确迁移原清单')
            plan = service.commit_content_change(preview)
            decisions = service.decision_state_store.load(plan.task_id).provider_special_decisions
            original = next(e.target_path for e in plan.entries if e.source_path == source)
            plan = change(service, plan, [source])
            self.assertNotIn(source, {e.source_path for e in plan.entries})
            other = next(e.source_path for e in plan.entries if e.source_path.name == 'Collaboration Movie.mkv')
            plan = change(service, plan, [other])
            plan = change(service, plan, [source], action='restore')
            self.assertEqual(decisions, service.decision_state_store.load(plan.task_id).provider_special_decisions)
            self.assertIn(other, plan.ignored_sources)
            self.assertEqual(original, next(e.target_path for e in plan.entries if e.source_path == source))

    def test_migration_preview_is_read_only_and_preserves_explicit_old_choices(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            dialog, _ = movie_dialog(root)
            service, plan = dialog.review_service, dialog.plan
            before = records(root)
            preview = service.prepare_content_migration(plan.task_id, PlanStore.fingerprint(plan),
                reason='逐项确认原先不整理的文件')
            self.assertEqual(before, records(root))
            revised = service.commit_content_change(preview)
            self.assertEqual(set(plan.ignored_sources), set(revised.ignored_sources))
            state = service.decision_state_store.load(plan.task_id)
            self.assertEqual(set(map(str, plan.ignored_sources)),
                set(state.content_selection.to_dict()['excluded_sources']))
            self.assertEqual([], state.movie_version_selection.to_dict()['excluded_sources'])
            self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(revised))

    def test_batch_exclusion_and_restore_preserve_independent_subtitle_exclusion(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root)
            subtitle = videos[0].with_suffix('.sc.ass')
            audio = videos[0].with_suffix('.mka')
            plan = change(service, plan, [subtitle])
            self.assertIn(videos[0], {e.source_path for e in plan.entries})
            self.assertIn(audio, {e.source_path for e in plan.entries})
            old_exclusions = set(plan.ignored_sources)
            plan = change(service, plan, [videos[0], videos[2]])
            self.assertTrue({videos[0], audio, videos[2], videos[2].with_suffix('.mka'),
                videos[2].with_suffix('.sc.ass')} <= set(plan.ignored_sources))
            plan = change(service, plan, [videos[0]], action='restore')
            self.assertIn(audio, {e.source_path for e in plan.entries})
            self.assertNotIn(subtitle, {e.source_path for e in plan.entries})
            self.assertTrue(old_exclusions <= set(plan.ignored_sources))
            self.assertIn(videos[2], set(plan.ignored_sources))

    def test_single_movie_segment_expands_complete_group_not_other_versions(self):
        with tempfile.TemporaryDirectory() as raw:
            service, plan, videos = migrated(Path(raw), segments=True)
            state = service.decision_state_store.load(plan.task_id)
            original = state.movie_version_selection.to_dict()['groups'][0]
            plan = change(service, plan, [videos[0]])
            self.assertTrue(set(videos[:2]) <= set(plan.ignored_sources))
            self.assertIn(videos[2], {e.source_path for e in plan.entries})
            state = service.decision_state_store.load(plan.task_id)
            self.assertEqual(set(map(str, videos[:2])),
                set(state.movie_version_selection.to_dict()['excluded_sources']))
            self.assertFalse(set(map(str, videos[:2])) &
                set(state.content_selection.to_dict()['excluded_sources']))
            plan = change(service, plan, [videos[1]], action='restore')
            groups = service.decision_state_store.load(plan.task_id).movie_version_selection.to_dict()['groups']
            self.assertIn(original, groups)
            self.assertTrue(set(videos[:3]) <= {e.source_path for e in plan.entries})

    def test_excluding_a_file_does_not_audit_its_contents(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root)
            preview = service.prepare_content_change(plan.task_id, PlanStore.fingerprint(plan),
                selected_entry_ids=[identifier(plan, videos[2])], action='exclude', reason='排除预告')
            before = records(root)
            source = videos[2]
            stat = source.stat()
            source.write_bytes(b'x' * stat.st_size)
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            revised = service.commit_content_change(preview)
            self.assertIn(source, revised.ignored_sources)

    def test_restore_uses_current_file_without_historical_inode_audit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root)
            plan = change(service, plan, [videos[2]])
            before = records(root)
            source = videos[2]
            data, old = source.read_bytes(), source.stat()
            source.rename(source.with_suffix('.retained'))
            source.write_bytes(data)
            os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
            self.assertNotEqual(old.st_ino, source.stat().st_ino)
            revised = change(service, plan, [source], action='restore')
            self.assertNotIn(source, revised.ignored_sources)
            self.assertIn(source, {entry.source_path for entry in revised.entries})

    def test_old_preview_does_not_overwrite_later_choice(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root)
            preview = service.prepare_content_change(plan.task_id, PlanStore.fingerprint(plan),
                selected_entry_ids=[identifier(plan, videos[2])], action='exclude', reason='第一个窗口')
            change(service, plan, [videos[0].with_suffix('.sc.ass')])
            before = records(root)
            with self.assertRaises((ValueError, RuntimeError)):
                service.commit_content_change(preview)
            self.assertEqual(before, records(root))

    def test_plan_only_exclusion_or_cleared_decision_cannot_bypass(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root)
            before = records(root)
            forged = replace(plan, entries=tuple(e for e in plan.entries if e.source_path != videos[2]),
                ignored_sources=(*plan.ignored_sources, videos[2]))
            with self.assertRaises((ValueError, RuntimeError)):
                service.persist_unapproved_revision(forged)
            self.assertEqual(before, records(root))
            from src.rename.decision_state import DecisionCommandService
            state = service.decision_state_store.load(plan.task_id)
            with self.assertRaises((ValueError, RuntimeError)):
                DecisionCommandService(service.decision_state_store, service.plan_store).update(
                    plan.task_id, expected_revision=state.revision,
                    changes={'content_selection': None}, planner=lambda _: plan)
            self.assertEqual(before, records(root))

    def test_disconnected_shared_evidence_gate_stops_commit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root)
            preview = service.prepare_content_change(plan.task_id, PlanStore.fingerprint(plan),
                selected_entry_ids=[identifier(plan, videos[2])], action='exclude', reason='明确排除')
            before = records(root)
            with patch('src.rename.content_selection.validate_omission_manifest',
                       side_effect=ValueError('内容证据门断开')) as guard:
                with self.assertRaisesRegex((ValueError, RuntimeError), '内容证据门断开'):
                    service.commit_content_change(preview)
                guard.assert_called()
            self.assertEqual(before, records(root))
