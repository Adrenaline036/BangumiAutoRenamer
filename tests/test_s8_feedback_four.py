"""Reported movie selection/misc incidents; component evidence, not acceptance."""
from pathlib import Path
import asyncio
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.rename.media_classifier import observe_watchable_sources, observe_watchable_content
from src.rename.plan_store import PlanStore
from tests import test_initial_movie_completion_contract as completion_tests
from tests.source_collection_fixture import admit_task


class MiscCoverage(unittest.TestCase):
    def test_disc_scan_and_audio_companions_are_excluded_together(self):
        root = Path('fixture-source').resolve()
        paths = [root/'BD Scans'/'Cover.jp2'] + [root/'Bonus CDs'/('Disc'+suffix)
                 for suffix in ('.tak', '.cue', '.log', '.png')]
        rows = observe_watchable_sources(paths, source_root=root)
        self.assertEqual(['known_non_watchable'] * len(paths), [r.disposition for r in rows])

    def test_explicit_movie_bonus_segments_survive_release_name_wrapping(self):
        root = Path('fixture-source').resolve()
        for label in ('Menu', '各種PV・CM', '特報・予告編', '4月20日初日舞台挨拶映像', '4月6日プレミアムイベント映像'):
            for title in ('', 'Example Movie 2013', '不同语言的作品标题'):
                with self.subTest(label=label, title=title):
                    path = root / f'Example Movie 2013 - {label} [BD 1920x1080 23.976fps AVC FLAC] - Group.mkv'
                    self.assertEqual('known_non_watchable', observe_watchable_content(path, source_root=root, work_title=title).disposition)

    def test_content_fonts_linked_audio_and_ambiguous_specials_are_not_omitted(self):
        root = Path('fixture-source').resolve()
        for name in ('Movie SP.mkv', 'Movie OVA.mkv', 'Movie S00E01.mkv', 'Menu Story.mkv'):
            self.assertNotEqual('known_non_watchable', observe_watchable_content(root/name, source_root=root).disposition)
        for role in ('font_resource', 'content_version', 'provider_special'):
            self.assertEqual('keep_candidate', observe_watchable_content(root/'[PV].mkv', source_root=root, protected_role=role).disposition)
        self.assertEqual('keep_candidate', observe_watchable_content(root/'Bonus CDs'/'Main.flac',
            source_root=root, linked_video=root/'Main.mkv').disposition)
        paths = [root/'Bonus CDs'/'Disc.tak', root/'Bonus CDs'/'Video.mkv',
                 root/'BD Scans'/'Subtitle.ass', root/'BD Scans'/'Font.ttf']
        self.assertTrue(all(row.disposition != 'known_non_watchable'
            for row in observe_watchable_sources(paths, source_root=root)))


class FirstMovieSelection(unittest.IsolatedAsyncioTestCase):
    async def test_unselected_video_needs_no_version_label_and_persists_as_omitted(self):
        helper = completion_tests.InitialMovieFullCompletionContracts('runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        original = completion_tests.source_fixture._make_version
        companions = []
        def sources(*args, **kwargs):
            source, primary, font = original(*args, **kwargs)
            for name, payload in [('Movie Alternate [1080p].sc.ass', b'[Script Info]\nScriptType: v4.00+\n'),
                                  ('Movie Alternate [1080p].mka', b'owned fixture audio')]:
                path = source/name
                path.write_bytes(payload)
                companions.append(path)
            return source, primary, font
        with patch.object(completion_tests.source_fixture, '_make_version', side_effect=sources):
            _root, service, plan, primary, created = await helper.fixture(multiple=True)
        excluded = created['second']
        intent = helper.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')], [excluded])
        revised = service.commit_initial_movie_completion(intent)
        state = service.decision_state_store.load(plan.task_id)
        value = state.content_selection.to_dict()
        self.assertEqual(1, len(value['version_groups']))
        self.assertIn(str(excluded), value['excluded_sources'])
        self.assertIn(excluded, revised.ignored_sources)
        self.assertNotIn(excluded, {e.source_path for e in revised.entries})
        self.assertTrue(set(companions) <= set(revised.ignored_sources))
        self.assertFalse(set(companions).intersection(e.source_path for e in revised.entries))
        from src.rename.content_selection import validate_omission_manifest
        validate_omission_manifest(state.content_selection, movie_selection=state.movie_version_selection)
        self.assertEqual(PlanStore.fingerprint(revised), PlanStore.fingerprint(service.plan_store.load(plan.task_id)))
        self.assertFalse(service.load_or_create(revised).approved_entry_ids)

    async def test_selected_labels_and_invalid_exclusions_still_reject_without_write(self):
        from tests.test_content_choice_contract import records
        helper = completion_tests.InitialMovieFullCompletionContracts('runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        root, service, plan, primary, created = await helper.fixture(multiple=True)
        other = created['second']
        before = records(root)
        cases = [([dict(sources=[str(primary)], label='same'), dict(sources=[str(other)], label='same')], []),
                 ([dict(sources=[str(primary)], label='CON')], [other]),
                 ([dict(sources=[str(primary)], label='1080p')], [primary]),
                 ([dict(sources=[str(primary)], label='1080p')], [other, other]),
                 ([dict(sources=[str(primary)], label='1080p')], [root/'outside.mkv'])]
        for groups, excluded in cases:
            with self.subTest(groups=groups, excluded=excluded):
                with self.assertRaises((ValueError, RuntimeError)):
                    helper.prepare(service, plan, groups, excluded)
                self.assertEqual(before, records(root))

    async def test_real_form_defaults_empty_and_ignores_unselected_bad_labels(self):
        from nicegui import ui
        from nicegui.client import Client
        from nicegui.page import page
        from src.pages.asset_choice_forms import show_movie_versions
        from tests._gui_helpers import GuiDriver
        helper = completion_tests.InitialMovieFullCompletionContracts('runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        _root, service, plan, primary, created = await helper.fixture(multiple=True)
        client = Client(page('/four-feedback-form'), request=None)
        self.addCleanup(client.delete)
        driver = GuiDriver('runTest')
        driver.client = client
        owner = SimpleNamespace(plan=plan, review_service=service,
            _video_options=lambda: {str(p): p.name for p in (primary, created['second'])},
            _adopt_revision=lambda result: setattr(owner, 'plan', result))
        with client:
            show_movie_versions(owner, plan.presentation_groups[0].group_id, ui=ui, notify=Mock(), complete_movie=Mock())
            def picker():
                choices = [e for e in client.elements.values()
                           if isinstance(e, ui.select) and e._props.get('label') == '电影正片文件（可多选）']
                self.assertEqual(1, len(choices))
                self.assertTrue(choices[0]._props.get('multiple'))
                return choices[0]

            def labels():
                return [e for e in client.elements.values()
                        if isinstance(e, ui.input) and e.label == '版本标签']

            choice = picker()
            self.assertEqual([], choice.value)
            self.assertEqual([], labels())
            await driver.click('保存正片版本')
            self.assertEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(service.plan_store.load(plan.task_id)))
            choice.set_value([str(primary)])
            self.assertEqual(1, len(labels()))
            labels()[0].set_value('1080p')
            choice.set_value([str(created['second'])])
            self.assertEqual(1, len(labels()))
            labels()[0].set_value('CON')
            choice.set_value([str(primary), str(created['second'])])
            self.assertCountEqual(['1080p', 'CON'], [e.value for e in labels()])
            choice.set_value([str(primary)])
            self.assertEqual(['1080p'], [e.value for e in labels()])
            await driver.click('保存正片版本')
            self.assertEqual('initial_movie_completion', owner.plan.revision_action)
            self.assertIn(created['second'], owner.plan.ignored_sources)
            for element in list(client.elements.values()):
                if isinstance(element, ui.dialog):
                    element.delete()
            show_movie_versions(owner, plan.presentation_groups[0].group_id, ui=ui, notify=Mock(), complete_movie=Mock())
            self.assertEqual([str(primary)], picker().value)
            self.assertEqual(['1080p'], [e.value for e in labels()])


class SingleSourceNavigation(unittest.IsolatedAsyncioTestCase):
    async def test_real_queue_slow_worker_single_admission_and_closed_window(self):
        from nicegui.client import Client
        from nicegui.page import page
        from src.pages import add_task_page as add, plan_review_page as review
        from src.task_queue import TaskQueueManager
        for close in (False, True):
            with self.subTest(close=close), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root/'input'
                source.mkdir()
                (source/'Movie.mkv').write_bytes(b'owned fixture')
                entered, release = asyncio.Event(), asyncio.Event()
                seen = []
                async def executor(job):
                    seen.append(job.task_id)
                    entered.set()
                    await release.wait()
                    manager._update(job.task_id, workflow_status='need_confirm')
                manager = TaskQueueManager(task_root=root/'tasks', plan_root=root/'plans',
                    decision_state_root=root/'decisions', recovery_abandon_audit_root=root/'abandon',
                     executor=executor)
                client = Client(page('/four-feedback-queue'), request=None)
                work = None
                try:
                    with client, patch.object(add, 'task_queue', manager), patch.object(review, 'plan_review_page', new=AsyncMock()) as navigate:
                        dialog = add.AddTaskDialog()
                        dialog.path = str(source)
                        async def submit():
                            with client:
                                await dialog._next()
                        work = asyncio.create_task(submit())
                        await asyncio.wait_for(entered.wait(), 5)
                        navigate.assert_not_awaited()
                        await dialog._next()
                        await dialog._submit()
                        self.assertEqual(1, len(list(manager.task_root.glob('*.json'))))
                        if close:
                            dialog.close()
                        # A published status alone must not race the active worker.
                        manager._update(seen[0], workflow_status='need_confirm')
                        await asyncio.sleep(0.3)
                        navigate.assert_not_awaited()
                        release.set()
                        await asyncio.wait_for(work, 5)
                        await asyncio.wait_for(manager._queue.join(), 5)
                        self.assertEqual(1, len(seen))
                        self.assertTrue(dialog._closed)
                        navigate.assert_not_awaited()
                        self.assertEqual('need_confirm', manager._read(seen[0])['workflow_status'])
                finally:
                    release.set()
                    if work and not work.done():
                        work.cancel()
                        await asyncio.gather(work, return_exceptions=True)
                    await manager.stop()
                    client.delete()

    async def test_terminal_navigation_renders_actual_plan_or_link_workspace(self):
        from nicegui.client import Client
        from nicegui.page import page
        from src.pages import add_task_page as add, plan_review_page as review
        from tests import test_default_activation_contract as activation
        helper = activation.DefaultActivationProducerContracts('runTest')
        async def admit(manager, source, task_id):
            await admit_task(manager, source, task_id=task_id, is_anime=None, is_movie=None)
        for has_plan in (False, True):
            async with helper.flow(movie=True, admission_only=not has_plan, admit=admit) as flow:
                self.assertEqual(has_plan, flow.store.path_for(flow.task_id).exists())
                shown = []
                def close_after_render(window):
                    async def close():
                        shown.append(window)
                        window.exit_requested = True
                        return None
                    return close().__await__()
                client = Client(page('/four-feedback-real-review'), request=None)
                try:
                    with client, patch.object(add, 'task_queue', flow.manager), \
                            patch.object(review.PlanReviewDialog, '__await__', close_after_render), \
                            patch.object(review.RecognitionRecoveryDialog, '__await__', close_after_render):
                        await review.plan_review_page(flow.task_id)
                    self.assertEqual(1, len(shown))
                    if has_plan:
                        self.assertIsInstance(shown[0], review.PlanReviewDialog)
                        self.assertEqual(flow.task_id, shown[0].plan.task_id)
                    else:
                        self.assertIsInstance(shown[0], review.PlanReviewDialog)
                        self.assertTrue(hasattr(shown[0], 'provider_url_input'))
                        self.assertEqual(flow.task_id, shown[0].task_id)
                finally:
                    client.delete()

    async def test_single_source_skips_confirmation_but_complex_source_does_not(self):
        from nicegui.client import Client
        from nicegui.page import page
        from src.pages import add_task_page as add
        for complex_source in (False, True):
            with self.subTest(complex_source=complex_source), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root/'Movie.mkv').write_bytes(b'test')
                if complex_source:
                    (root/'Season 2').mkdir()
                    (root/'Season 2'/'Show S02E01.mkv').write_bytes(b'test')
                client = Client(page('/four-feedback-add'), request=None)
                try:
                    with client, patch.object(add.AddTaskDialog, '_submit', new=AsyncMock()) as submit:
                        dialog = add.AddTaskDialog()
                        dialog.path = str(root)
                        await dialog._next()
                        self.assertEqual(0 if complex_source else 1, submit.await_count)
                        if complex_source:
                            self.assertEqual(2, dialog.step)
                finally:
                    client.delete()


if __name__ == '__main__':
    unittest.main()
