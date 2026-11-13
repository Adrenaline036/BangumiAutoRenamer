"""S6 user incident: batch extras use one current transaction; useful rows first."""
import asyncio
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from src.pages import plan_review_page as page
from src.pages.content_review_view import selection_rows
from src.rename import initial_tv_completion as completion
from src.rename.plan_review import entry_id, PlanReviewError
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from tests import test_initial_tv_completion_contract as initial
from tests.test_content_choice_contract import records, change
from tests import test_content_workbench_gui as gui_fixture


class ExtraBatchContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Run last, after borrowed GUI resources, including after an assertion fails.
        original_task, original_queue = page.get_task, page.task_queue
        def restored():
            self.assertIs(page.get_task, original_task)
            self.assertIs(page.task_queue, original_queue)
        self.addCleanup(restored)

    async def fixture(self, *, scans=0, duplicate=False):
        helper = initial.InitialTVCompletionContracts('runTest')
        self.addCleanup(helper.doCleanups)
        original = initial.flow_fixture._make_season

        def source(*args, **kwargs):
            root = original(*args, **kwargs)
            for tag in ('IV', 'NCOP', 'NCED', 'NCED_EP03'):
                (root / f'Yama no Susume [{tag}].mkv').write_bytes(b'fixture video')
            if scans:
                folder = root / 'CDs' / 'Scans'
                folder.mkdir(parents=True)
                for n in range(scans):
                    (folder / f'{n:02d}.jpg').write_bytes(b'fixture scan')
            return root

        wait_for = asyncio.wait_for
        async def larger_inventory_wait(awaitable, timeout):
            # Only enlarge the original fixture's eight-second queue deadline;
            # the >50-row incident still must finish and reach the same assertions.
            return await wait_for(awaitable, timeout=60 if timeout == 8 else timeout)

        with patch.object(initial.flow_fixture, '_make_season', source), \
                patch.object(asyncio, 'wait_for', larger_inventory_wait):
            result = await helper.fixture(anime=True, exceptional=True, duplicate=duplicate, companions=True)
        helper.assert_success(result[3])
        return result

    async def test_content_rows_put_main_first_and_editable_omitted_misc_later(self):
        _, _, _, result, *_ = await self.fixture(scans=55, duplicate=False)
        choice = result.state.content_selection
        before = choice.to_dict()
        rows = selection_rows(choice, None, work_title=result.plan.work_identity.title)
        self.assertFalse(any(r['locked'] for r in rows))
        main = {Path(m['source_path']) for m in before['materials'] if m['media_kind'] == 'video'
                and m['episode_id'] is not None
                and (m.get('presentation_asset') or {}).get('asset_role', {}).get('kind') == 'content_version'}
        self.assertTrue(main)
        self.assertIn(rows[0]['source'], main)
        scans = [r for r in rows if r['source'].suffix == '.jpg']
        self.assertEqual(55, len(scans))
        self.assertTrue(all(r['source'] in result.plan.ignored_sources for r in scans))
        self.assertTrue(all(rows.index(r) >= len(main) for r in scans))
        self.assertEqual(before, choice.to_dict())
        self.assertEqual(len(rows), len({r['identifier'] for r in rows}))
        self.assertEqual({Path(m['source_path']) for m in before['materials']}, {r['source'] for r in rows})

    async def test_real_multi_select_event_single_revision_bundle_and_guards(self):
        root, service, _, result, extras, *_ = await self.fixture()
        plan = result.plan
        videos = [e for e in plan.entries if e.media_kind == 'video' and
                  (e.source_path == extras['exceptional'] or
                   any(f'[{tag}]' in e.source_path.name for tag in ('IV', 'NCOP', 'NCED', 'NCED_EP03')))]
        ids = [entry_id(e) for e in videos]
        self.assertEqual(5, len(ids))
        state = service.decision_state_store.load(plan.task_id)
        source_bytes = {p: p.read_bytes() for p in plan.source_root.rglob('*') if p.is_file()}
        fingerprint = PlanStore.fingerprint(plan)
        edit = dict(entry_ids=ids, action='unmatched_extra', provider_episode_id=None)
        before = records(root)
        subtitle = next(e for e in plan.entries if e.source_path == extras['subtitle'])
        bad = [dict(edit, entry_ids=[]), dict(edit, entry_ids=[ids[0], ids[0]]),
               dict(edit, entry_ids=[ids[0], 'unknown']), dict(edit, entry_ids=[ids[0], entry_id(subtitle)]),
               dict(edit, provider_episode_id='9001'), dict(edit, action='confirmed_official_special')]
        for invalid in bad:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    service.revise_tv_content(plan.task_id, fingerprint, expected_revision=state.revision,
                                              mode='special', edit=invalid)
                self.assertEqual(before, records(root))

        gui = gui_fixture.ContentWorkbenchGui('runTest')
        gui.create_page(prepared=(root, service, plan, tuple(e.source_path for e in videos)),
                        cleanup_owner=self)
        await gui.driver.click('特别篇 / OVA')
        selects = [gui.view._official_special_controls['files']]
        self.assertEqual(1, len(selects))
        with gui.client:
            selects[0].value = ids
        self.assertFalse(any(getattr(e, 'text', '') == '附加内容，本次不整理' for e in gui.client.elements.values()))
        official = [e for e in gui.client.elements.values() if getattr(e, 'text', None) == '归入特别篇']
        self.assertEqual(1, len(official))
        self.assertTrue(official[0].visible)
        self.assertFalse(official[0].enabled, 'never batch assign one official episode')
        close = next(e for e in gui.view._official_special_picker.descendants()
                     if e._props.get('label') == '关闭')
        await gui.driver.click_element(close)
        from src.rename import special_decision_projection as projection
        saved = records(root)
        with patch.object(projection, 'project_special_decisions', side_effect=ValueError('disconnected')) as disconnected:
            with self.assertRaises(ValueError):
                service.revise_tv_content(plan.task_id, fingerprint, expected_revision=state.revision,
                                          mode='special', edit=edit)
            disconnected.assert_called()
        self.assertEqual(saved, records(root))
        start = time.monotonic()
        with patch.object(service, 'revise_tv_content', wraps=service.revise_tv_content) as command, \
                patch.object(completion, 'prepare', wraps=completion.prepare) as prepare, \
                patch.object(completion, 'commit', wraps=completion.commit) as commit, \
                patch.object(service, 'apply_provider_special_decision', side_effect=AssertionError('legacy writer')):
            await gui.driver.click('内容保留与排除')
            workbench = gui.view._content_workbench
            pages = {row['source']: index // workbench.page_sizes['exclude']
                     for index, row in enumerate(workbench.column_rows('exclude'))}
            for video in videos:
                # Select through the real pager; off-page files retain their selection.
                while workbench.pages['exclude'] != pages[video.source_path]:
                    direction = 'following' if workbench.pages['exclude'] < pages[video.source_path] else 'previous'
                    await gui.driver.click_element(workbench.panels['exclude'][direction])
                gui.source_box(video.source_path).value = True
            await gui.driver.click('排除整理')
            await asyncio.sleep(0)
        command_seconds = time.monotonic() - start
        self.assertEqual(['content', 'fonts'], [call.kwargs['mode'] for call in command.call_args_list])
        self.assertEqual(['content', 'fonts'], [call.kwargs['mode'] for call in prepare.call_args_list])
        self.assertEqual(command.call_count, commit.call_count)
        revised = service.plan_store.load(plan.task_id)
        after = service.decision_state_store.load(plan.task_id)
        self.assertEqual(state.revision + command.call_count, after.revision)
        self.assertEqual(state.provider_special_decisions, after.provider_special_decisions)
        self.assertEqual(state.content_selection.associations, after.content_selection.associations)
        for video in videos:
            self.assertIn(video.source_path, revised.ignored_sources)
            self.assertNotIn(video.source_path, {e.source_path for e in revised.entries})
        for name in ('subtitle', 'audio'):
            self.assertIn(extras[name], revised.ignored_sources)
            self.assertNotIn(extras[name], {e.source_path for e in revised.entries})
        verification = PlanVerifier().verify(revised)
        self.assertFalse(verification.issues)
        self.assertFalse(service.load_or_create(revised).approved_entry_ids)
        saved = records(root)
        with self.assertRaisesRegex(PlanReviewError, '版本已变化'):
            service.revise_tv_content(plan.task_id, fingerprint, expected_revision=state.revision,
                                      mode='special', edit=edit)
        self.assertEqual(saved, records(root))
        excluded = revised
        excluded_state = service.decision_state_store.load(plan.task_id)
        saved = records(root)
        with self.assertRaisesRegex(ValueError, '唯一当前视频'):
            service.revise_tv_content(plan.task_id, PlanStore.fingerprint(excluded),
                expected_revision=excluded_state.revision, mode='special', edit=edit)
        self.assertEqual(saved, records(root))
        self.assertEqual(source_bytes, {p: p.read_bytes() for p in source_bytes})
        print(f'batch=5 commands={command.call_count} prepare={prepare.call_count} commit={commit.call_count} gui_seconds={command_seconds:.3f}')
