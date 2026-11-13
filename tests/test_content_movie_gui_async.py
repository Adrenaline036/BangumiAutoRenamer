"""Registered GUI events with a bounded real backend preparation barrier."""
import asyncio
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nicegui import ui
from nicegui.client import Client
from nicegui.page import page
from src.pages.asset_choice_forms import show_movie_versions
from src.rename.plan_store import PlanStore
from src.rename.plan_review import PlanReviewService
from src.rename import movie_content
from tests.test_content_choice_contract import change, migrated, records
from tests import _gui_helpers as gui_fixture


class ContentMovieAsyncGui(unittest.IsolatedAsyncioTestCase):
    async def test_stale_open_form_does_not_overwrite_a_later_version_label(self):
        from tests.test_content_movie_lifecycle import groups
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, plan, videos = migrated(root)
            plan = change(service, plan, [videos[1], videos[2]])
            owner = SimpleNamespace(plan=plan, review_service=service)
            owner._video_options = lambda: {str(e.source_path): e.source_path.name
                for e in owner.plan.entries if e.media_kind == 'video'}
            owner._selected_movie_primary_sources = lambda: [str(videos[0])]
            owner._adopt_revision = lambda revised: setattr(owner, 'plan', revised)
            notices = []
            client = Client(page('/isolated-movie-stale-open'), request=None)
            self.addCleanup(client.delete)
            driver = gui_fixture.GuiDriver('runTest')
            driver.client = client
            with client:
                show_movie_versions(owner, 'movie-continuous', ui=ui,
                    notify=lambda text, **_kwargs: notices.append(text), complete_movie=lambda *_a, **_k: None)
            rows = groups(plan)
            rows[0]['label'] = '后来确认的新标签'
            later = service.revise_movie_versions(plan.task_id, PlanStore.fingerprint(plan),
                groups=rows, excluded_sources=[str(videos[1])])
            owner.plan = later
            before = records(root)
            await driver.click('保存正片版本')
            self.assertEqual(PlanStore.fingerprint(later),
                PlanStore.fingerprint(service.plan_store.load(plan.task_id)))
            self.assertEqual(before, records(root))
            self.assertFalse(notices)

    async def test_registered_save_remains_responsive_and_owns_worker_until_real_terminal(self):
        for mode in ('failure', 'stale', 'waiter_cancel', 'view_closed'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
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
                client = Client(page('/isolated-movie-background-' + mode), request=None)
                self.addCleanup(client.delete)
                driver = gui_fixture.GuiDriver('runTest')
                driver.client = client
                with client:
                    show_movie_versions(owner, 'movie-continuous', ui=ui,
                        notify=lambda text, **kwargs: notices.append(text),
                        complete_movie=lambda *_a, **_k: self.fail('unexpected initial inventory route'))
                dialog = next(e for e in client.elements.values() if type(e).__name__ == 'Dialog')
                labels = [e for e in client.elements.values() if e._props.get('label') == '版本标签']
                labels[0].value = '后台修订标签'
                button = driver.button('保存正片版本')
                main_thread = threading.get_ident()
                entered, release = threading.Event(), threading.Event()
                threads, submissions = [], []
                original_build = PlanReviewService.prepare_movie_version_change
                original_command = PlanReviewService.revise_movie_versions
                def building(*args, **kwargs):
                    result = original_build(*args, **kwargs)
                    if not entered.is_set():
                        threads.append(threading.get_ident())
                        entered.set()
                        if threads[-1] == main_thread:
                            return result  # RED detects the wrong thread, never deadlocks it.
                        if not release.wait(5):
                            raise RuntimeError('test backend barrier exceeded its bound')
                        if mode == 'failure':
                            raise ValueError('本轮后台核查失败')
                    return result
                def submitting(*args, **kwargs):
                    submissions.append(True)
                    return original_command(*args, **kwargs)
                before = records(root)
                sources = {p: p.read_bytes() for p in plan.source_root.iterdir() if p.is_file()}
                with patch('src.rename.plan_review.PlanReviewService.prepare_movie_version_change', building), patch(
                        'src.rename.plan_review.PlanReviewService.revise_movie_versions', submitting):
                    waiter = asyncio.create_task(driver.click('保存正片版本'))
                    try:
                        for _ in range(100):
                            if entered.is_set():
                                break
                            await asyncio.sleep(.01)
                        self.assertTrue(entered.is_set(), 'real backend preparation must be reached')
                        self.assertNotEqual(main_thread, threads[0], 'hash/preparation must not run on GUI thread')
                        self.assertFalse(waiter.done(), 'GUI heartbeat ran while the backend is still waiting')
                        self.assertFalse(button.enabled)
                        await driver.click('保存正片版本')
                        await driver.click('关闭')
                        self.assertTrue(dialog.value, 'busy cancel must not pretend to stop submitted work')
                        self.assertEqual([True], submissions)
                        if mode == 'stale':
                            later = change(service, plan, [videos[0].with_suffix('.mka')])
                            owner.plan = later
                            before = records(root)
                        elif mode == 'waiter_cancel':
                            waiter.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await waiter
                            self.assertFalse(button.enabled)
                            await driver.click('保存正片版本')
                            self.assertEqual([True], submissions)
                        elif mode == 'view_closed':
                            dialog.close()  # Lost view does not revoke an already submitted command.
                    finally:
                        release.set()
                        if not waiter.cancelled():
                            await asyncio.wait_for(asyncio.shield(waiter), timeout=5)
                        for _ in range(100):
                            if getattr(owner, '_movie_version_worker', None) is None:
                                break
                            await asyncio.sleep(.01)
                self.assertEqual(sources, {p: p.read_bytes() for p in sources})
                current = service.plan_store.load(plan.task_id)
                if mode in {'failure', 'stale'}:
                    self.assertFalse(notices)
                    self.assertTrue(dialog.value)
                    self.assertEqual(before, records(root))
                    if mode == 'failure':
                        self.assertTrue(any('本轮后台核查失败' in getattr(e, 'text', '') for e in client.elements.values()))
                        await driver.click('保存正片版本')
                        self.assertTrue(notices, 'failure must permit a fresh explicit retry')
                    else:
                        self.assertEqual(PlanStore.fingerprint(later), PlanStore.fingerprint(current))
                else:
                    self.assertNotEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(current))
                    if mode == 'view_closed':
                        self.assertFalse(notices, 'closed view must not report a fabricated GUI completion')


if __name__ == '__main__':
    unittest.main()
