"""Hidden subtitle materials through public, disposable review transactions."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename.plan_review import PlanReviewService
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import SubtitleOverrideScope
from tests.test_content_choice_contract import change, identifier, migrated, records


def movie(root):
    service, plan, videos = migrated(root)
    return service, change(service, plan, [videos[2]]), videos


def series(root, *, duplicate=False, initialize_content=True):
    from tests.test_subtitle_plan_contract import make_series_plan, persist_subtitle_fixture
    from src.rename.subtitle_labels import DimensionResolution
    base = make_series_plan()
    entries = tuple(replace(entry, subtitle=replace(entry.subtitle, raw_label='SC',
        unknown_tokens=(), parse_state=DimensionResolution.RESOLVED), requires_review=False)
        if entry.subtitle is not None else entry for entry in base.entries)
    second = entries[-1]
    different = replace(second, source_path=second.source_path.with_name('Ep02.tc.ass'),
        target_path=second.target_path.with_name('Show S01E02.zh-Hant.ass'),
        subtitle=replace(second.subtitle, raw_label='TC', script='Hant'))
    base = replace(base, entries=(*entries, different), expected_sources=(*base.expected_sources, different.source_path))
    if duplicate:
        extra = replace(entries[1], source_path=entries[1].source_path.with_name('Ep01.alternate.SC.ass'))
        base = replace(base, entries=(*base.entries, extra), expected_sources=(*base.expected_sources, extra.source_path))
    store = PlanStore(root / 'plans')
    # Existing typed TV component fixture supplies real registration, source
    # and font observations, then uses the canonical initial commit.
    plan = persist_subtitle_fixture(store, base, root)
    service = PlanReviewService(store, root / 'reviews', root / 'work_catalog')
    if not initialize_content:
        return service, plan
    preview = service.prepare_content_migration(plan.task_id, PlanStore.fingerprint(plan), reason='明确迁移字幕材料')
    return service, service.commit_content_change(preview)


def apply(service, plan, source, *, owner=None, scope=SubtitleOverrideScope.FILE):
    return service.apply_subtitle_override_rule(plan.task_id, PlanStore.fingerprint(plan),
        identifier(plan, source), scope=scope, associated_video_source_path=owner,
        primary_language='en', script=None, reason='本地验收字幕规则')


def subtitle(plan, source):
    return next(entry.subtitle for entry in plan.entries if entry.source_path == source)


class ContentSubtitleLifecycle(unittest.TestCase):
    def test_visible_subtitle_contract_cannot_diverge_from_frozen_rule_materials(self):
        from src.rename.decision_state import DecisionCommandService
        original_commit = DecisionCommandService._commit_projection
        for operation in ('append', 'unchanged_rules'):
            for field in ('title', 'flags', 'reason', 'owner'):
                with self.subTest(operation=operation, field=field), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    service, plan, videos = movie(root)
                    source = videos[0].with_suffix('.sc.ass')
                    if operation == 'unchanged_rules':
                        plan = apply(service, plan, source)
                    before, reached = records(root), []
                    def submit(command, previous, candidate, draft, **kwargs):
                        reached.append(True)
                        fields = {'track_title_tokens': ('injected',)} if field == 'title' else (
                            {'flags': ('forced',)} if field == 'flags' else (
                            {'manual_reason': 'changed only in visible draft'} if field == 'reason' else
                            {'associated_video_source_path': videos[1], 'association_candidates': (videos[1],)}))
                        draft = replace(draft, entries=tuple(replace(entry, subtitle=replace(entry.subtitle, **fields))
                            if entry.source_path == source else entry for entry in draft.entries))
                        return original_commit(command, previous, candidate, draft, **kwargs)
                    with patch('src.rename.decision_state.DecisionCommandService._commit_projection', submit):
                        with self.assertRaises((ValueError, RuntimeError)):
                            if operation == 'append':
                                apply(service, plan, source)
                            else:
                                change(service, plan, [videos[0].with_suffix('.mka')])
                    self.assertTrue(reached)
                    self.assertEqual(before, records(root))

    def test_hidden_duplicate_and_default_conflicts_are_not_output_checks(self):
        with tempfile.TemporaryDirectory() as raw:
            service, plan = series(Path(raw), duplicate=True)
            first = plan.source_root / 'Ep01.Mystery.SC.ass'
            hidden = plan.source_root / 'Ep01.alternate.SC.ass'
            plan = change(service, plan, [hidden])
            plan = service.apply_subtitle_override_rule(plan.task_id, PlanStore.fingerprint(plan),
                identifier(plan, first), scope=SubtitleOverrideScope.SERIES,
                primary_language='en', script=None, flags=('default',), reason='隐藏副轨不参与输出冲突')
            # Explicit legacy content migration is not completion of the new
            # safe-content policy. Keep that independent media gate visible.
            self.assertEqual({'content_policy_pending'},
                {issue.code for issue in PlanVerifier().verify(plan).issues})
            self.assertNotIn(hidden, {entry.source_path for entry in plan.entries})
            plan = change(service, plan, [hidden], action='restore')
            codes = {issue.code for issue in PlanVerifier().verify(plan).issues}
            self.assertIn('duplicate_subtitle_semantics', codes)
            self.assertIn('multiple_default_subtitles', codes)
            self.assertFalse(service.load_or_create(plan).approved_entry_ids)

    def test_appended_rule_cannot_change_unmatched_material_series_owner_or_prefix(self):
        from src.rename.content_selection import ContentSelection
        from src.rename.decision_state import DecisionCommandService
        from src.rename.plan_review import _rule_fingerprint
        original_commit = DecisionCommandService._commit_projection
        for fault in ('unmatched', 'series_owner', 'prefix'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                if fault == 'series_owner':
                    service, plan = series(root)
                    selected = plan.source_root / 'Ep01.Mystery.SC.ass'
                    changed = plan.source_root / 'Ep02.MYSTERY.sc.ass'
                    plan = change(service, plan, [changed])
                    scope = SubtitleOverrideScope.SERIES
                    other_owner = subtitle(plan, selected).associated_video_source_path
                else:
                    service, plan, videos = movie(root)
                    selected = videos[0].with_suffix('.sc.ass')
                    changed = videos[1].with_suffix('.sc.ass')
                    scope = SubtitleOverrideScope.FILE
                    if fault == 'prefix':
                        plan = apply(service, plan, selected)
                        selected, changed = changed, selected
                    else:
                        plan = change(service, plan, [changed])
                before, reached = records(root), []
                def submit(command, previous, candidate, draft, **kwargs):
                    reached.append(True)
                    value = candidate.content_selection.to_dict()
                    row = next(row for row in value['materials'] if row['source_path'] == str(changed))
                    if fault == 'unmatched':
                        row['subtitle']['primary_language'] = 'ja'
                    elif fault == 'series_owner':
                        row['subtitle']['associated_video_source_path'] = str(other_owner)
                        relation = next(row for row in value['associations']['relations']
                            if row['source_path'] == str(changed))
                        relation.update(owners=[str(other_owner)], state='linked', source='manual')
                    else:
                        altered = replace(draft.subtitle_override_rules[0], reason='rewrite prior rule')
                        altered = replace(altered, rule_fingerprint=_rule_fingerprint(altered))
                        row['subtitle']['applied_rule_fingerprint'] = altered.rule_fingerprint
                        draft = replace(draft, subtitle_override_rules=(altered, *draft.subtitle_override_rules[1:]),
                            entries=tuple(replace(entry, subtitle=replace(entry.subtitle,
                                applied_rule_fingerprint=altered.rule_fingerprint))
                                if entry.source_path == changed else entry for entry in draft.entries))
                    choice = ContentSelection.from_mapping(value)
                    draft = replace(draft, metadata={**draft.metadata, 'content_selection': choice.to_dict(),
                        'content_selection_fingerprint': choice.fingerprint})
                    return original_commit(command, previous, replace(candidate, content_selection=choice), draft, **kwargs)
                with patch('src.rename.decision_state.DecisionCommandService._commit_projection', submit):
                    with self.assertRaises((ValueError, RuntimeError)):
                        apply(service, plan, selected, scope=scope)
                self.assertTrue(reached, 'must reach canonical transport after valid public preparation')
                self.assertEqual(before, records(root))

    def test_disconnected_shared_rule_change_validator_cannot_commit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = movie(root)
            before = records(root)
            with patch('src.rename.content_choice.validate_subtitle_contracts', return_value=None, create=True) as guard:
                with self.assertRaises((ValueError, RuntimeError)):
                    apply(service, plan, videos[0].with_suffix('.sc.ass'))
            guard.assert_called()
            self.assertEqual(before, records(root))

    def test_excluded_source_drift_blocks_apply_and_undo_without_writes(self):
        from tests.test_content_lifecycle_contract import alter_same_stat
        for action in ('apply', 'undo'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                service, plan, videos = movie(root)
                source = videos[0].with_suffix('.sc.ass')
                plan = apply(service, plan, source)
                rule = plan.subtitle_override_rules[-1].rule_id
                plan = change(service, plan, [source])
                alter_same_stat(source)
                source_before = {p: p.read_bytes() for p in plan.expected_sources}
                if action == 'apply':
                    revised = apply(service, plan, videos[1].with_suffix('.sc.ass'))
                else:
                    revised = service.revert_subtitle_rule(plan.task_id, PlanStore.fingerprint(plan), rule, reason='只撤销规则')
                self.assertIn(source, revised.ignored_sources)
                self.assertNotIn(source, {e.source_path for e in revised.entries})
                self.assertFalse(service.load_or_create(revised).approved_entry_ids)
                self.assertEqual(source_before, {p: p.read_bytes() for p in source_before})
                self.assertFalse(any(e.target_path.exists() for e in revised.entries))

    def test_hidden_series_revision_journal_windows_preserve_materials_and_retry(self):
        for stage in ('prepared', 'decision_written', 'ownership_claimed', 'plan_written'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                service, plan = series(root)
                first = plan.source_root / 'Ep01.Mystery.SC.ass'
                hidden = plan.source_root / 'Ep02.MYSTERY.sc.ass'
                plan = change(service, plan, [hidden])
                before = {key: value for key, value in records(root).items()
                    if not key.startswith('projection_journal/')}
                reached = []
                def checkpoint(_service, phase):
                    if phase == stage:
                        reached.append(phase)
                        raise RuntimeError('subtitle commit interrupted:' + stage)
                with patch('src.rename.projection_authority.ProjectionCommitService._checkpoint', checkpoint):
                    with self.assertRaisesRegex(RuntimeError, 'subtitle commit interrupted:' + stage):
                        apply(service, plan, first, scope=SubtitleOverrideScope.SERIES)
                self.assertEqual([stage], reached)
                after = {key: value for key, value in records(root).items()
                    if not key.startswith('projection_journal/')}
                self.assertEqual(before, {key: after.get(key) for key in before})
                for key in after.keys() - before.keys():
                    self.assertEqual('plan_written', stage)
                    self.assertTrue(key.startswith(f'plans/history/{plan.task_id}/'))
                    fingerprint = Path(key).stem
                    self.assertEqual(fingerprint, PlanStore.fingerprint(service.plan_store.load_version(plan.task_id, fingerprint)))
                plan = apply(service, plan, first, scope=SubtitleOverrideScope.SERIES)
                plan = change(service, plan, [hidden], action='restore')
                self.assertEqual('en', subtitle(plan, hidden).primary_language)

    def test_last_rule_undo_restores_hidden_subtitle_and_owner_without_old_choices(self):
        for hidden in ('subtitle', 'owner', 'both'):
            with self.subTest(hidden=hidden), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                service, plan, videos = movie(root)
                source = videos[0].with_suffix('.sc.ass')
                original = subtitle(plan, source)
                plan = apply(service, plan, source, owner=videos[1])
                rule = plan.subtitle_override_rules[-1].rule_id
                if hidden in {'subtitle', 'both'}:
                    plan = change(service, plan, [source])
                if hidden in {'owner', 'both'}:
                    plan = change(service, plan, [videos[1]])
                audio = videos[0].with_suffix('.mka')
                plan = change(service, plan, [audio])
                plan = service.revert_subtitle_rule(plan.task_id, PlanStore.fingerprint(plan), rule,
                    reason='只撤销最后字幕规则')
                self.assertIn(audio, plan.ignored_sources)
                self.assertIn(videos[2], plan.ignored_sources)
                if hidden in {'owner', 'both'}:
                    plan = change(service, plan, [videos[1]], action='restore')
                if hidden in {'subtitle', 'both'}:
                    self.assertNotIn(source, {entry.source_path for entry in plan.entries})
                    plan = change(service, plan, [source], action='restore')
                self.assertEqual(original, subtitle(plan, source))
                choice = service.decision_state_store.load(plan.task_id).content_selection
                relation = next(row for row in choice.associations.to_dict()['relations']
                    if row['source_path'] == str(source))
                self.assertEqual([str(videos[0])], relation['owners'])
                self.assertFalse(plan.subtitle_override_rules)
                self.assertIn(audio, plan.ignored_sources)
                self.assertFalse(service.load_or_create(plan).approved_entry_ids)

    def test_legitimate_hidden_rule_is_not_an_unreferenced_output_error(self):
        with tempfile.TemporaryDirectory() as raw:
            service, plan, videos = movie(Path(raw))
            source = videos[0].with_suffix('.sc.ass')
            plan = apply(service, plan, source)
            plan = change(service, plan, [source])
            self.assertNotIn('subtitle_rule_unreferenced', {issue.code for issue in PlanVerifier().verify(plan).issues})

    def test_series_rule_updates_hidden_match_and_undo_uses_hidden_baseline(self):
        for undo in (False, True):
            with self.subTest(undo=undo), tempfile.TemporaryDirectory() as raw:
                service, plan = series(Path(raw))
                first = plan.source_root / 'Ep01.Mystery.SC.ass'
                hidden = plan.source_root / 'Ep02.MYSTERY.sc.ass'
                different = plan.source_root / 'Ep02.tc.ass'
                original, other = subtitle(plan, hidden), subtitle(plan, different)
                plan = change(service, plan, [hidden])
                plan = apply(service, plan, first, scope=SubtitleOverrideScope.SERIES)
                if undo:
                    plan = service.revert_subtitle_rule(plan.task_id, PlanStore.fingerprint(plan),
                        plan.subtitle_override_rules[-1].rule_id, reason='基准字幕隐藏时也只撤销规则')
                plan = change(service, plan, [hidden], action='restore')
                if undo:
                    self.assertEqual(original, subtitle(plan, hidden))
                else:
                    self.assertEqual('en', subtitle(plan, hidden).primary_language)
                    self.assertEqual(plan.subtitle_override_rules[-1].rule_id, subtitle(plan, hidden).applied_rule_id)
                self.assertEqual(other, subtitle(plan, different))

    def test_hidden_rule_semantics_are_checked_at_canonical_commit(self):
        from src.rename.content_choice import _draft
        from src.rename.content_selection import ContentSelection
        from src.rename.decision_state import DecisionCommandService
        for fault in ('missing_rule', 'outcome', 'owner'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                service, plan, videos = movie(root)
                source = videos[0].with_suffix('.sc.ass')
                plan = apply(service, plan, source)
                plan = change(service, plan, [source])
                state = service.decision_state_store.load(plan.task_id)
                value = state.content_selection.to_dict()
                material = next(row for row in value['materials'] if row['source_path'] == str(source))
                if fault == 'missing_rule':
                    material['subtitle']['applied_rule_id'] = 'not-a-current-rule'
                elif fault == 'outcome':
                    material['subtitle']['primary_language'] = 'ja'
                else:
                    material['subtitle']['associated_video_source_path'] = str(videos[1])
                    relation = next(row for row in value['associations']['relations']
                        if row['source_path'] == str(source))
                    relation.update(owners=[str(videos[1])], state='linked', source='manual')
                value['parent_plan_fingerprint'] = PlanStore.fingerprint(plan)
                value['reason'] = 'fixture hidden rule mutation'
                choice = ContentSelection.from_mapping(value)
                draft = _draft(service, plan, state, choice, state.movie_version_selection, 'subtitle')
                before = records(root)
                with self.assertRaises((ValueError, RuntimeError)):
                    DecisionCommandService(service.decision_state_store, service.plan_store).update(
                        plan.task_id, expected_revision=state.revision,
                        changes={'content_selection': choice},
                        planner=lambda _: draft, task_terminal=service._revision_task_terminal(plan.task_id))
                self.assertEqual(before, records(root))
