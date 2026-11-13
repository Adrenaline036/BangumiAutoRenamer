"""First choose one version, restore a never-grouped video, then confirm it."""
from pathlib import Path
import unittest
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from tests import test_initial_movie_completion_contract as initial
from tests import _media_flow_helpers as sources
from tests import test_movie_file_picker as picker_fixture
from tests.test_content_choice_contract import change, records
from tests.test_content_movie_lifecycle import groups


class RestoredMovieCandidate(unittest.IsolatedAsyncioTestCase):
    async def fixture(self, *, restore=True):
        helper = initial.InitialMovieFullCompletionContracts('runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        make = sources._make_version
        def seeded(*args, **kwargs):
            source, main, font = make(*args, **kwargs)
            for suffix in ('sc', 'tc'):
                (source / f'Movie Alternate [1080p].{suffix}.ass').write_text(
                    '[Script Info]\nScriptType: v4.00+\n', encoding='utf-8')
            return source, main, font
        with patch.object(sources, '_make_version', side_effect=seeded):
            root, service, draft, primary, extra = await helper.fixture(multiple=True)
        second = extra['second']
        plan = service.commit_initial_movie_completion(helper.prepare(service, draft,
            [dict(sources=[str(primary)], label='原版')], excluded=[second]))
        self.assertIn(second, plan.ignored_sources)
        self.assertEqual(1, len(plan.metadata['content_selection']['version_groups']))
        if restore:
            plan = change(service, plan, [second], action='restore')
            self.assertNotIn(second, plan.ignored_sources)
            self.assertEqual('movie_version_unselected', next(e.role for e in plan.entries if e.source_path == second))
        return root, service, plan, primary, second

    def open(self, service, plan):
        helper = picker_fixture.MovieFilePicker('runTest')
        self.addCleanup(helper.doCleanups)
        return helper, helper.open_form(service, plan)

    async def test_actual_gui_restored_video_can_be_confirmed_and_reopened(self):
        root, service, plan, primary, second = await self.fixture()
        old_group = plan.metadata['content_selection']['version_groups'][0]
        old_subtitle = next(e for e in plan.entries if e.source_path == primary.with_suffix('.sc.ass'))
        old_ignored = set(plan.ignored_sources)
        _, (owner, client, driver, picker) = self.open(service, plan)
        self.assertIn(str(second), picker.options, 'restored never-grouped video must be selectable')
        self.assertNotIn(str(second), picker.value, 'do not silently confirm its role')
        picker.value = [*picker.value, str(second)]
        fields = [e for e in client.elements.values() if e._props.get('label') == '版本标签']
        fields[-1].value = 'SDR'
        await driver.click('保存正片版本')
        current = service.plan_store.load(plan.task_id)
        self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(current))
        self.assertEqual(old_group, current.metadata['content_selection']['version_groups'][0])
        self.assertEqual(next(e.target_path for e in plan.entries if e.source_path == primary),
                         next(e.target_path for e in current.entries if e.source_path == primary))
        self.assertEqual(old_ignored, set(current.ignored_sources))
        retained = next(e for e in current.entries if e.source_path == old_subtitle.source_path)
        self.assertEqual(old_subtitle.subtitle, retained.subtitle)
        self.assertEqual(primary, retained.subtitle.associated_video_source_path)
        self.assertEqual(old_subtitle.target_path, retained.target_path)
        video = next(e for e in current.entries if e.source_path == second)
        self.assertEqual('movie_content', video.role)
        self.assertEqual('content_version', video.presentation_asset.asset_role.kind)
        self.assertEqual('movie_content', next(r['role'] for r in current.metadata['content_selection']['materials'] if r['source_path'] == str(second)))
        for suffix in ('sc.ass', 'tc.ass'):
            source = second.with_suffix('.' + suffix)
            sub = next(e for e in current.entries if e.source_path == source)
            self.assertEqual(second, sub.subtitle.associated_video_source_path)
            self.assertEqual(video.target_path.parent, sub.target_path.parent)
            self.assertTrue(sub.target_path.name.startswith(video.target_path.stem + '.'))
        codes = {i.code for i in PlanVerifier().verify(current).issues if i.source_path in {old_subtitle.source_path, second, second.with_suffix('.sc.ass'), second.with_suffix('.tc.ass')}}
        self.assertFalse(codes & {'presentation_asset_role_unknown', 'subtitle_video_not_content_version'})
        self.assertFalse(service.load_or_create(current).approved_entry_ids)
        self.assertFalse(any(e.target_path.exists() for e in current.entries))
        _, (_, _, _, reopened) = self.open(service, current)
        self.assertIn(str(second), reopened.value)

    async def test_service_promotes_only_explicit_candidate_and_keeps_old_group(self):
        root, service, plan, primary, second = await self.fixture()
        old = plan.metadata['content_selection']['version_groups'][0]
        requested = [*groups(plan), dict(sources=[str(second)], label='SDR')]
        intent = service.prepare_movie_version_change(plan.task_id, PlanStore.fingerprint(plan), groups=requested)
        current = service.commit_movie_version_change(intent)
        self.assertEqual(old, current.metadata['content_selection']['version_groups'][0])
        self.assertEqual(2, len(current.metadata['content_selection']['version_groups']))
        # The persisted registry now supports the existing exclude/restore operation.
        hidden = change(service, current, [second])
        restored = change(service, hidden, [second], action='restore')
        self.assertEqual(current.metadata['movie_version_selection']['groups'], restored.metadata['movie_version_selection']['groups'])

    async def test_unchosen_candidate_cancel_and_ordinary_save_leave_it_pending(self):
        root, service, plan, primary, second = await self.fixture()
        before = records(root)
        _, (owner, client, driver, picker) = self.open(service, plan)
        self.assertIn(str(second), picker.options)
        await driver.click('关闭')
        self.assertEqual(before, records(root))
        _, (owner, client, driver, picker) = self.open(service, plan)
        label = next(e for e in client.elements.values() if e._props.get('label') == '版本标签')
        label.value = '原版修改标签'
        await driver.click('保存正片版本')
        current = service.plan_store.load(plan.task_id)
        self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(current), 'ordinary save must really commit')
        self.assertEqual('原版修改标签', current.metadata['content_selection']['version_groups'][0]['label'])
        self.assertNotIn(second, current.ignored_sources)
        self.assertEqual(1, len(current.metadata['content_selection']['version_groups']))
        self.assertEqual('unknown', next(e.presentation_asset.asset_role.kind for e in current.entries if e.source_path == second))

    async def test_hidden_foreign_extra_duplicate_and_omitted_old_members_reject_without_writes(self):
        root, service, hidden, primary, second = await self.fixture(restore=False)
        before = records(root)
        _, (_, _, _, picker) = self.open(service, hidden)
        self.assertNotIn(str(second), picker.options)
        with self.assertRaises((ValueError, RuntimeError)):
            service.prepare_movie_version_change(hidden.task_id, PlanStore.fingerprint(hidden), groups=[*groups(hidden), dict(sources=[str(second)], label='SDR')])
        self.assertEqual(before, records(root))
        current = change(service, hidden, [second], action='restore')
        extra = next(Path(r['source_path']) for r in current.metadata['content_selection']['materials']
            if r['media_kind'] == 'video' and (r.get('presentation_asset') or {}).get('asset_role', {}).get('kind') == 'extra')
        if extra in current.ignored_sources:
            current = change(service, current, [extra], action='restore')
        self.assertNotIn(extra, current.ignored_sources, 'negative case must be an unexcluded non-candidate')
        for name, requested in (
            ('foreign', [*groups(current), dict(sources=[str(root/'foreign.mkv')], label='外部')]),
            ('extra', [*groups(current), dict(sources=[str(extra)], label='特典')]),
            ('duplicate', [*groups(current), dict(sources=[str(primary)], label='重复')]),
            ('missing_old', [dict(sources=[str(second)], label='SDR')]),
        ):
            with self.subTest(name=name):
                before = records(root)
                with self.assertRaises((ValueError, RuntimeError)):
                    service.prepare_movie_version_change(current.task_id, PlanStore.fingerprint(current), groups=requested)
                self.assertEqual(before, records(root))

    async def test_candidate_excluded_after_preview_and_stale_gui_cannot_publish(self):
        root, service, plan, primary, second = await self.fixture()
        _, (owner, client, driver, picker) = self.open(service, plan)
        self.assertIn(str(second), picker.options)
        picker.value = [*picker.value, str(second)]
        intent = service.prepare_movie_version_change(plan.task_id, PlanStore.fingerprint(plan),
            groups=[*groups(plan), dict(sources=[str(second)], label='SDR')])
        later = change(service, plan, [second])
        before = records(root)
        with self.assertRaises((ValueError, RuntimeError)):
            service.commit_movie_version_change(intent)
        owner.plan = later
        await driver.click('保存正片版本')
        self.assertEqual(before, records(root))

    async def test_shared_candidate_gate_is_consumed_by_gui_prepare_and_commit(self):
        from src.rename import movie_content
        root, service, plan, primary, second = await self.fixture()
        requested = [*groups(plan), dict(sources=[str(second)], label='SDR')]
        intent = service.prepare_movie_version_change(plan.task_id, PlanStore.fingerprint(plan), groups=requested)
        before = records(root)
        with patch.object(movie_content, 'pending_movie_sources', return_value=(), create=True) as gate:
            _, (_, _, _, picker) = self.open(service, plan)
            self.assertNotIn(str(second), picker.options)
            with self.assertRaises((ValueError, RuntimeError)):
                service.prepare_movie_version_change(plan.task_id, PlanStore.fingerprint(plan), groups=requested)
            with self.assertRaises((ValueError, RuntimeError)):
                service.commit_movie_version_change(intent)
            self.assertGreaterEqual(gate.call_count, 3)
        self.assertEqual(before, records(root))

    async def test_new_candidate_cannot_absorb_old_group_but_later_explicit_merge_remains(self):
        root, service, plan, primary, second = await self.fixture()
        before = records(root)
        _, (_, client, driver, picker) = self.open(service, plan)
        self.assertIn(str(second), picker.options)
        picker.value = [str(primary), str(second)]
        continuous = next(e for e in client.elements.values()
            if getattr(e, 'text', '') == '将保留的视频按上述顺序视为同一版本的连续分段')
        continuous.value = True
        await driver.click('保存正片版本')
        self.assertEqual(before, records(root), 'mixed new/old GUI grouping must not write')
        with self.assertRaises((ValueError, RuntimeError)):
            service.prepare_movie_version_change(plan.task_id, PlanStore.fingerprint(plan),
                groups=[dict(sources=[str(primary), str(second)], label='连续分段')])
        self.assertEqual(before, records(root))
        current = service.revise_movie_versions(plan.task_id, PlanStore.fingerprint(plan),
            groups=[*groups(plan), dict(sources=[str(second)], label='SDR')])
        merged = service.revise_movie_versions(current.task_id, PlanStore.fingerprint(current),
            groups=[dict(sources=[str(primary), str(second)], label='明确合并分段')])
        self.assertEqual(1, len(merged.metadata['content_selection']['version_groups']))

    async def test_affiliation_consumer_rejects_extra_and_uses_shared_new_member_gate(self):
        from src.rename import initial_movie_completion, movie_content
        root, service, plan, primary, second = await self.fixture()
        extra = next(Path(r['source_path']) for r in plan.metadata['content_selection']['materials']
            if r['media_kind'] == 'video' and (r.get('presentation_asset') or {}).get('asset_role', {}).get('kind') == 'extra')
        if extra in plan.ignored_sources:
            plan = change(service, plan, [extra], action='restore')
        def prepare(rows):
            return initial_movie_completion.prepare(service, plan.task_id, PlanStore.fingerprint(plan),
                groups=rows, reason='核对已有电影归属并确认新版本', mode='affiliation')
        before = records(root)
        with self.assertRaises((ValueError, RuntimeError)):
            prepare([*groups(plan), dict(sources=[str(extra)], label='不应提升的特典')])
        self.assertEqual(before, records(root))
        rows = [*groups(plan), dict(sources=[str(second)], label='SDR')]
        intent = prepare(rows)
        with patch.object(movie_content, 'pending_movie_sources', return_value=(), create=True) as gate:
            with self.assertRaises((ValueError, RuntimeError)):
                prepare(rows)
            with self.assertRaises((ValueError, RuntimeError)):
                initial_movie_completion.commit(service, intent)
            self.assertGreaterEqual(gate.call_count, 2)
        self.assertEqual(before, records(root))
        current = initial_movie_completion.commit(service, intent)
        self.assertEqual(2, len(current.metadata['content_selection']['version_groups']))


if __name__ == '__main__':
    unittest.main()
