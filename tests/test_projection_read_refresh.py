"""公共校验不重建计划，读取冲突可恢复，刷新请求不重复读任务。"""

import asyncio

import builtins

import io

import json

import tempfile

import threading

import unittest

from dataclasses import replace

from pathlib import Path

from unittest.mock import patch



from nicegui import Client

from nicegui.page import page

from src.pages import data_table_page as dashboard

from src.rename import content_associations, content_selection, episode_range

from src.rename.plan_review import entry_id

from src.rename.plan_store import PlanStore

from src.utils import utils

from src.utils.task_changes import mark_changed

from tests.test_episode_range_contract import merged_package

from tests import test_partition_content_contract as partition





def ranged_audio(root, source):

    merged_package(root, source)

    next(source.glob('*[[]03-05[]]*.mkv')).with_suffix('.mka').write_bytes(b'isolated audio')

    return source





class ProjectionContracts(unittest.IsolatedAsyncioTestCase):

    flow = partition.PartitionContentContracts.flow

    current = partition.PartitionContentContracts.current

    selected = partition.PartitionContentContracts.selected



    async def test_range_validation_does_not_construct_a_plan(self):

        async with self.flow(automatic=False, configure_source=merged_package) as flow:

            plan, state = await self.selected(flow)

            chosen = next(e for e in plan.entries if e.media_kind == 'video' and '[03-05]' in e.source_path.name)

            plan = flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),

                expected_revision=state.revision, mode='range', edit=dict(entry_id=entry_id(chosen),

                    season=1, start=3, end=5, naming_mode='tmdb'))

            with patch('src.rename.rename_plan.RenamePlan.__post_init__', side_effect=AssertionError('校验重建了整份计划')):

                self.assertIn(entry_id(chosen), episode_range.validate_projection(plan))

            for entry in plan.entries:

                if entry.media_kind not in ('video', 'subtitle'):

                    continue

                if entry.episode_end != 5:

                    continue

                broken = replace(plan, entries=tuple(replace(e, episode_end=7) if e == entry else e for e in plan.entries))

                with self.assertRaises(ValueError):

                    episode_range.validate_projection(broken)



    async def test_association_consumer_uses_validated_content(self):

        async with self.flow(automatic=False) as flow:

            plan, _ = await self.selected(flow)

            choice = content_selection.from_plan(plan)

            with patch.object(content_selection.ContentSelection, 'from_mapping', side_effect=AssertionError('重复还原内容选择')):

                self.assertIs(choice.associations, content_associations.from_plan(plan))



    async def test_binding_and_audio_drift_remain_blocked(self):

        async with self.flow(automatic=False, configure_source=ranged_audio) as flow:

            plan, state = await self.selected(flow)

            chosen = next(e for e in plan.entries if e.media_kind == 'video' and '[03-05]' in e.source_path.name)

            plan = flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),

                expected_revision=state.revision, mode='range', edit=dict(entry_id=entry_id(chosen),

                    season=1, start=3, end=5, naming_mode='tmdb'))

            audio = next(e for e in plan.entries if e.source_path == chosen.source_path.with_suffix('.mka'))

            self.assertEqual((1, 3, 5), (audio.season_id, audio.episode_id, audio.episode_end))

            decision = episode_range.from_plan(plan)[0]

            for changes in ({'source_basis': '0' * 64}, {'work_key': 'bangumi:subject:999999'}):

                broken = replace(plan, metadata={**plan.metadata,

                    'episode_ranges': [replace(decision, **changes).to_dict()]})

                with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, '作品或来源已改变'):

                    episode_range.validate_projection(broken)

            for changed in (replace(audio, season_id=2),

                    replace(audio, target_path=audio.target_path.with_name('wrong.mka')),

                    replace(audio, presentation_asset=replace(audio.presentation_asset, owner_version_ref='wrong'))):

                broken = replace(plan, entries=tuple(changed if e == audio else e for e in plan.entries))

                with self.subTest(audio=changed), self.assertRaises(ValueError):

                    episode_range.validate_projection(broken)

            with patch('src.rename.rename_plan.RenamePlan.__post_init__', side_effect=AssertionError('校验重建了整份计划')):

                self.assertIn(entry_id(chosen), episode_range.validate_projection(plan))





class TaskReadContracts(unittest.TestCase):

    def check_read(self, transient=False, invalid=False, missing=False):

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        path = root/'sample.json'

        if not missing:

            path.write_text('{' if invalid else json.dumps(dict(uuid='sample', workflow_status='completed')), encoding='utf8')

        return root, path



    def test_transient_permission_recovers_on_both_readers(self):

        for reader in ('get', 'at'):

            with self.subTest(reader=reader):

                root, path = self.check_read()

                original = builtins.open

                attempts = []

                def observed(file, *args, **kwargs):

                    if Path(file) == path:

                        attempts.append(1)

                        if len(attempts) == 1:

                            raise PermissionError(13, '文件正在替换')

                    return original(file, *args, **kwargs)

                with patch.object(utils, 'TASK_PATH', root), patch('builtins.open', side_effect=observed), patch('io.open', side_effect=observed):

                    value = utils.get_task('sample') if reader == 'get' else utils.read_task_at(root, 'sample')

                self.assertEqual('completed', value['workflow_status'])

                self.assertEqual(2, len(attempts))



    def test_persistent_permission_is_not_a_missing_task(self):

        for reader in ('get', 'at'):

            with self.subTest(reader=reader):

                root, path = self.check_read()

                original = builtins.open

                attempts = []

                def observed(file, *args, **kwargs):

                    if Path(file) == path:

                        attempts.append(1)

                        raise PermissionError(13, '持续拒绝读取')

                    return original(file, *args, **kwargs)

                with patch.object(utils, 'TASK_PATH', root), patch('builtins.open', side_effect=observed), patch('io.open', side_effect=observed):

                    with self.assertRaises(PermissionError):

                        utils.get_task('sample') if reader == 'get' else utils.read_task_at(root, 'sample')

                self.assertLessEqual(len(attempts), 3)



    def test_invalid_json_is_not_a_missing_task(self):

        root, _ = self.check_read(invalid=True)

        with patch.object(utils, 'TASK_PATH', root):

            for reader in ('get', 'at'):

                with self.subTest(reader=reader), self.assertRaises(ValueError):

                    utils.get_task('sample') if reader == 'get' else utils.read_task_at(root, 'sample')



    def test_missing_task_still_returns_empty(self):

        root, _ = self.check_read(missing=True)

        with patch.object(utils, 'TASK_PATH', root):

            self.assertEqual({}, utils.get_task('sample'))

            self.assertEqual({}, utils.read_task_at(root, 'sample'))



    def test_dashboard_does_not_publish_partial_rows_on_read_failure(self):

        root, path = self.check_read()

        (root/'ok.json').write_text(json.dumps(dict(uuid='ok', workflow_status='completed')), encoding='utf8')

        original = builtins.open

        def observed(file, *args, **kwargs):

            if Path(file) == path:

                raise PermissionError(13, '持续拒绝读取')

            return original(file, *args, **kwargs)

        with patch.object(dashboard, 'TASK_PATH', root), patch.object(utils, 'TASK_PATH', root):

            with patch('builtins.open', side_effect=observed), patch('io.open', side_effect=observed):

                self.assertEqual('ok', utils.get_task('ok')['uuid'])

                with self.subTest(mode='permission'), self.assertRaises(PermissionError):

                    dashboard.load_task_rows(root)

            (root/'broken.json').write_text('{', encoding='utf8')

            self.assertEqual('sample', utils.get_task('sample')['uuid'])

            with self.subTest(mode='invalid_json'), self.assertRaises(ValueError):

                dashboard.load_task_rows(root)





class RefreshContracts(unittest.IsolatedAsyncioTestCase):

    async def exercise(self, changed, cancel_first=False):

        client = Client(page('/coalesced-refresh'), request=None)

        self.addCleanup(client.delete)

        entered, released = threading.Event(), threading.Event()

        calls, rendered = [], []

        def prepare(*args, **kwargs):

            calls.append(len(calls))

            first = len(calls) == 1

            if first:

                entered.set()

                if not released.wait(3):

                    raise AssertionError('读请求没有释放')

            return [], ('old' if first and changed else 'fresh',), {}

        def render(key, value):

            rendered.append(value[1][0])

            dashboard._dashboard_signatures[key] = value[1]

            return True

        with client, patch.object(dashboard, '_prepare_dashboard', side_effect=prepare), patch.object(dashboard, '_render_dashboard', side_effect=render):

            async def refresh():

                with client:

                    return await dashboard.refresh_dashboard_background(force=True)

            first = asyncio.create_task(refresh())

            try:

                for _ in range(300):

                    if entered.is_set():

                        break

                    await asyncio.sleep(.002)

                self.assertTrue(entered.is_set())

                if changed:

                    mark_changed()

                others = [asyncio.create_task(refresh()) for _ in range(2)]

                await asyncio.sleep(.01)

            finally:

                released.set()

            if cancel_first:
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                await asyncio.gather(*others)
            else:
                await asyncio.gather(first, *others)

        self.assertEqual(2 if changed else 1, len(calls))

        self.assertEqual(['old', 'fresh'] if changed else ['fresh'], rendered)

        self.assertNotIn(client.id, dashboard._dashboard_reads)



    async def test_concurrent_forced_refresh_reads_once(self):

        await self.exercise(False)



    async def test_change_during_refresh_gets_one_followup(self):

        await self.exercise(True)



    async def test_failed_read_keeps_last_display(self):

        client = Client(page('/failed-refresh'), request=None)

        self.addCleanup(client.delete)

        dashboard._dashboard_signatures[client.id] = ('last-good',)

        with client, patch.object(dashboard, '_prepare_dashboard', side_effect=PermissionError(13, '不可读')), patch.object(dashboard, '_render_dashboard') as render, patch.object(dashboard, 'notify') as notify:

            self.assertIsNone(await dashboard.refresh_dashboard_background(force=True))

            render.assert_not_called()

            notify.assert_called_once()

        self.assertEqual(('last-good',), dashboard._dashboard_signatures[client.id])



    async def test_cancelled_waiter_does_not_cancel_shared_read(self):
        await self.exercise(False, cancel_first=True)

    async def test_repeated_change_and_failed_read_can_retry(self):
        client = Client(page('/refresh-retry'), request=None)
        self.addCleanup(client.delete)
        dashboard._dashboard_signatures[client.id] = ('old',)
        calls = []
        def prepare(*args, **kwargs):
            calls.append(True)
            if len(calls) <= 2:
                mark_changed()
            return [], ('fresh',), {}
        with client, patch.object(dashboard, '_prepare_dashboard', side_effect=prepare), patch.object(dashboard, '_render_dashboard', return_value=True) as render:
            self.assertTrue(await dashboard.refresh_dashboard_background(force=True))
            self.assertEqual(2, len(calls))
            self.assertGreaterEqual(render.call_count, 1)
            render.reset_mock()
            self.assertTrue(await dashboard.refresh_dashboard_background(force=True))
            render.assert_called_once()
        with client, patch.object(dashboard, '_prepare_dashboard', side_effect=PermissionError(13, '不可读')), patch.object(dashboard, 'notify'):
            self.assertIsNone(await dashboard.refresh_dashboard_background(force=True))
        with client, patch.object(dashboard, '_prepare_dashboard', side_effect=prepare), patch.object(dashboard, '_render_dashboard', return_value=True):
            self.assertTrue(await dashboard.refresh_dashboard_background(force=True))
        self.assertNotIn(client.id, dashboard._dashboard_reads)


    async def test_forced_requests_joining_unchanged_timer_still_refresh(self):
        client = Client(page('/force-join'), request=None)
        self.addCleanup(client.delete)
        entered, released = threading.Event(), threading.Event()
        calls = []
        def prepare(previous, *, force=False):
            calls.append(force)
            if len(calls) == 1:
                entered.set()
                if not released.wait(3):
                    raise AssertionError('读请求未释放')
            return ([], ('fresh',), {}) if force else None
        with client, patch.object(dashboard, '_prepare_dashboard', side_effect=prepare), patch.object(dashboard, '_render_dashboard', side_effect=lambda key, value: value is not None):
            async def refresh(force):
                with client:
                    return await dashboard.refresh_dashboard_background(force=force)
            first = asyncio.create_task(refresh(False))
            try:
                for _ in range(300):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.002)
                self.assertTrue(entered.is_set())
                forced = [asyncio.create_task(refresh(True)) for _ in range(2)]
                await asyncio.sleep(.01)
            finally:
                released.set()
            self.assertFalse(await first)
            self.assertEqual([True, True], await asyncio.gather(*forced))
        self.assertEqual([False, True], calls)
