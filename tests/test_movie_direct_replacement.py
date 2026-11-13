"""First movie replacement through canonical services and real GUI callbacks.

Only disposable, generated media and Provider replay; no acceptance records.
"""
from types import SimpleNamespace
import unittest

from src.rename.plan_store import PlanStore
from src.rename.initial_movie_completion import RootSnapshotSource
from src.rename.target_routing import TargetRootSnapshot
from tests import test_completed_rebuild_work_contract as fixture


class MovieDirectReplacement(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = fixture.CompletedMovieRebuildContracts('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    async def incoming(self, *, resolution='1080p', old=None):
        old = old or await self.h.single(with_font=False)
        source, primary, font = fixture.pair_fixture._make_version(self.h.root/'incoming', resolution)
        font.unlink()
        next(source.glob('*.ass')).unlink()  # Omitted before admission.
        primary.write_bytes(primary.read_bytes() + b' new release')
        result = await self.h.harness._run_provider_flow(self.h.root, source, 'direct-movie')
        review = self.h.current(result.plan.task_id).review
        snapshot = TargetRootSnapshot.capture(dict(bangumi_path=self.h.root/'TV',
            movie_path=self.h.root/'Movies', anime_path=self.h.root/'Anime', anime_movie_path=self.h.root/'AnimeMovies'))
        review.root_snapshot_source = RootSnapshotSource(lambda: snapshot)
        record = review.work_catalog.load(fixture.pair_fixture._work_key(old.plan))
        self.assertEqual('inventory_review_draft', result.plan.metadata['mapping_source'])
        return old, result.plan, review, primary, record

    async def test_first_same_label_is_pending_until_difference_confirmed_then_retires_old_subtitle(self):
        from tests.test_content_choice_contract import records
        old, initial, service, primary, record = await self.incoming()
        version = record['versions'][0]
        catalog_before = self.h.catalog_path(old.plan).read_bytes()
        old_subtitle = next(e.target_path for e in old.plan.entries if e.media_kind == 'subtitle')
        intent = service.prepare_initial_movie_completion(initial.task_id, PlanStore.fingerprint(initial),
            groups=[dict(sources=[str(primary)], label=version['display_label'])],
            replace_version_ref=version['version_ref'], reason='直接替换这个已登记版本')
        pending = service.commit_initial_movie_completion(intent)
        selection = pending.metadata['movie_version_selection']
        self.assertEqual(version['version_ref'], selection['replacement']['version_ref'])
        self.assertEqual(version['version_ref'], selection['groups'][0]['version_ref'])
        self.assertEqual(catalog_before, self.h.catalog_path(old.plan).read_bytes())
        before = records(self.h.root)
        with self.assertRaises((ValueError, RuntimeError)):
            from src.rename.plan_review import entry_id
            service.approve_entries(initial.task_id, PlanStore.fingerprint(pending),
                [entry_id(entry) for entry in pending.entries])
        self.assertEqual(before, records(self.h.root))
        self.assertTrue(old_subtitle.exists())
        operation, _ = service.asset_actions().prepare_replacement(service.plan_store.load(initial.task_id))
        self.assertIn(str(old_subtitle), {r['target_path'] for r in operation.to_dict()['previous_assets']
                                      if r['disposition'] == 'retire'})
        confirmed = service.confirm_asset_replacement(initial.task_id, PlanStore.fingerprint(pending),
            expected_operation_fingerprint=operation.fingerprint)
        self.assertFalse(service.load_or_create(confirmed).approved_entry_ids)
        task, ledger, executor = await self.h.harness._approve_and_execute(
            root=self.h.root, result=self.h.current(initial.task_id))
        self.assertEqual('completed', task.get('execution_status'), task.get('error'))
        self.assertEqual('completed', ledger['status'])
        self.assertFalse(old_subtitle.exists())
        after = executor.work_catalog.load(record['work_key'])
        self.assertEqual([initial.task_id], [v['task_id'] for v in after['versions']])
        self.assertTrue(after['asset_history'])

    async def test_new_specifications_keep_old_slot_and_other_version_label_is_rejected(self):
        first, second = await self.h.pair()
        old, initial, service, primary, record = await self.incoming(resolution='2160p', old=first)
        version = next(v for v in record['versions'] if v['task_id'] == first.plan.task_id)
        other = next(v for v in record['versions'] if v['task_id'] == second.plan.task_id)
        from tests.test_content_choice_contract import records
        before = records(self.h.root)
        for ref, label in [('mv-' + '0' * 32, '新版'), (version['version_ref'], other['display_label'])]:
            with self.subTest(ref=ref, label=label), self.assertRaises(ValueError):
                service.prepare_initial_movie_completion(initial.task_id, PlanStore.fingerprint(initial),
                    groups=[dict(sources=[str(primary)], label=label)], replace_version_ref=ref, reason='明确换源')
            self.assertEqual(before, records(self.h.root))
        intent = service.prepare_initial_movie_completion(initial.task_id, PlanStore.fingerprint(initial),
            groups=[dict(sources=[str(primary)], label='新版 2160p')],
            replace_version_ref=version['version_ref'], reason='更换为新规格')
        plan = service.commit_initial_movie_completion(intent)
        group = plan.metadata['movie_version_selection']['groups'][0]
        self.assertEqual(version['version_ref'], group['version_ref'])
        self.assertEqual('2160p', group['descriptor']['resolution']['value'])
        operation, _ = service.asset_actions().prepare_replacement(plan)
        other_targets = {str(e.target_path) for e in second.plan.entries}
        self.assertFalse(other_targets.intersection(r['target_path'] for r in operation.to_dict()['previous_assets']))

    async def test_real_difference_dialog_opens_and_cancel_keeps_plan_unapproved(self):
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        from tests.test_content_choice_contract import records
        old, initial, service, primary, record = await self.incoming()
        gui = ContentWorkbenchGui('runTest')
        gui.create_page(prepared=(self.h.root, service, initial, []), cleanup_owner=self)
        await gui.driver.click('换源')
        videos = next(e for e in gui.client.elements.values() if type(e).__name__ == 'Select' and e.label == '电影正片文件（可多选）')
        videos.set_value([str(primary)])
        await gui.driver.click('核对换源范围')
        await gui.view._movie_replacement_preview_task
        gui.driver.button('生成计划')  # The real range preview finished successfully.
        texts = [str(getattr(e, 'text', '')) for e in gui.client.elements.values()]
        self.assertIn('新来源纳入的文件', texts)
        self.assertTrue(any(str(primary) + ' → ' in text for text in texts))
        self.assertTrue(any('已选择替换此电影版本' in text for text in texts))
        before = records(self.h.root)
        await gui.driver.click('取消')
        self.assertEqual(before, records(self.h.root))
        self.assertTrue(gui.view.plan.metadata['asset_operation']['requires_choice'])
        self.assertFalse(service.load_or_create(gui.view.plan).approved_entry_ids)
        gui.driver.button('换源')  # Reopening can continue the pending choice.

    async def test_multiple_versions_require_choice_and_closed_owner_cannot_submit(self):
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        from tests.test_content_choice_contract import records
        first, _second = await self.h.pair()
        _old, initial, service, primary, record = await self.incoming(old=first)
        gui = ContentWorkbenchGui('runTest')
        gui.create_page(prepared=(self.h.root, service, initial, []), cleanup_owner=self)
        await gui.driver.click('换源')
        selected = next(e for e in gui.client.elements.values() if type(e).__name__ == 'Select' and e.label == '已有版本换源')
        self.assertIsNone(selected.value)
        self.assertEqual({v['version_ref'] for v in record['versions']}, set(selected.options))
        videos = next(e for e in gui.client.elements.values() if type(e).__name__ == 'Select' and e.label == '电影正片文件（可多选）')
        videos.set_value([str(primary)])
        before = records(self.h.root)
        await gui.driver.click('核对换源范围')
        self.assertEqual(before, records(self.h.root), '未选旧版本时不可保存')
        selected.set_value(next(iter(selected.options)))
        gui.view.close()
        await gui.driver.click('核对换源范围')  # A stale child callback after its owner closes.
        self.assertEqual(before, records(self.h.root))
        self.assertIsNone(getattr(gui.view, '_movie_version_worker', None))

    async def test_registered_continuous_segments_are_one_complete_replacement_scope(self):
        source, first, font = fixture.pair_fixture._make_version(self.h.root/'segmented', '1080p')
        font.unlink()
        second = source/'Movie Part 2.mkv'
        second.write_bytes(first.read_bytes())
        result = await self.h.harness._run_provider_flow(self.h.root, source, 'old-segments')
        service = self.h.current(result.plan.task_id).review
        snapshot = TargetRootSnapshot.capture(dict(bangumi_path=self.h.root/'TV', movie_path=self.h.root/'Movies',
            anime_path=self.h.root/'Anime', anime_movie_path=self.h.root/'AnimeMovies'))
        service.root_snapshot_source = RootSnapshotSource(lambda: snapshot)
        intent = service.prepare_initial_movie_completion(result.plan.task_id, PlanStore.fingerprint(result.plan),
            groups=[dict(sources=[str(first), str(second)], label='完整分段版')], reason='登记一个完整连续分段版本')
        service.commit_initial_movie_completion(intent)
        old = self.h.current(result.plan.task_id)
        task, _ledger, _executor = await self.h.harness._approve_and_execute(root=self.h.root, result=old)
        self.assertEqual('completed', task.get('execution_status'), task.get('error'))
        old, initial, service, primary, record = await self.incoming(old=old)
        refs = {v['version_ref'] for v in record['versions']}
        self.assertEqual(1, len(refs))
        self.assertEqual(2, len(record['versions']))
        intent = service.prepare_initial_movie_completion(initial.task_id, PlanStore.fingerprint(initial),
            groups=[dict(sources=[str(primary)], label='新完整版本')], replace_version_ref=next(iter(refs)), reason='替换完整旧分段版本')
        plan = service.commit_initial_movie_completion(intent)
        operation, _ = service.asset_actions().prepare_replacement(plan)
        previous = {row['target_path'] for row in operation.to_dict()['previous_assets']}
        old_videos = {str(e.target_path) for e in old.plan.entries if e.role == 'movie_content'}
        self.assertEqual(2, len(old_videos))
        self.assertTrue(old_videos <= previous)

    async def test_first_movie_card_owns_both_buttons_and_save_opens_differences(self):
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        from src.pages import asset_actions_view
        from unittest.mock import AsyncMock, patch
        old, initial, service, primary, record = await self.incoming()
        gui = ContentWorkbenchGui('runTest')
        gui.create_page(prepared=(self.h.root, service, initial, []), cleanup_owner=self)
        elements = list(gui.client.elements.values())
        affiliate = next(e for e in elements if type(e).__name__ == 'Button' and e.text == '归入已有作品')
        cards = [e for e in affiliate.ancestors() if type(e).__name__ == 'Card']
        card = next((c for c in cards if any(getattr(e, 'text', '') == '已登记的同一作品'
            and c in e.ancestors() for e in elements)), None)
        self.assertIsNotNone(card, '归入按钮必须在已登记作品卡片内')
        replace = [e for e in elements if type(e).__name__ == 'Button' and e.text == '换源' and card in e.ancestors()]
        self.assertEqual(1, len(replace), '首次库存也必须可以直接换源')
        await gui.driver.click('换源')
        selected = next(e for e in gui.client.elements.values() if type(e).__name__ == 'Select' and e.label == '已有版本换源')
        self.assertEqual(record['versions'][0]['version_ref'], selected.value)
        self.assertNotIn('', selected.options, '换源不能偷偷回到新增')
        videos = next(e for e in gui.client.elements.values() if type(e).__name__ == 'Select' and e.label == '电影正片文件（可多选）')
        videos.set_value([str(primary)])
        labels = [e for e in gui.client.elements.values() if type(e).__name__ == 'Input' and e.label == '版本标签']
        self.assertEqual(1, len(labels))
        labels[0].set_value(record['versions'][0]['display_label'])
        with patch.object(asset_actions_view, 'preview_asset_replacement', new_callable=AsyncMock) as preview:
            await gui.driver.click('核对换源范围')
            worker = getattr(gui.view, '_movie_replacement_preview_task', None)
            self.assertIsNotNone(worker, '\n'.join(str(getattr(e, 'text', '')) for e in gui.client.elements.values()))
            await worker
            self.assertEqual(1, preview.await_count, '保存成功后必须自动接续差异确认')
            self.assertIs(gui.view, preview.await_args.args[0])
        self.assertEqual(record['versions'][0]['version_ref'],
            gui.view.plan.metadata['movie_version_selection']['replacement']['version_ref'])


class MovieDirectAffiliation(unittest.IsolatedAsyncioTestCase):
    async def test_first_cross_provider_same_label_keeps_key_without_registering(self):
        from tests.test_rc54_work_affiliation import WorkAffiliationIncidents, candidate
        from src.rename.work_affiliation import prepare_existing, commit_choice
        for first_provider, second_provider in [('tmdb', 'bangumi'), ('bangumi', 'tmdb')]:
            with self.subTest(order=(first_provider, second_provider)):
                h = WorkAffiliationIncidents('runTest'); h.setUp(); self.addCleanup(h.doCleanups)
                old = await h.add(first_provider, 'movie'); await h.finish(old, 'movie')
                source = h.root/'sources'/'incoming'; source.mkdir()
                primary = source/'Violet 2160p.mkv'; primary.write_bytes(h.video_bytes + b' incoming')
                with h.transport():
                    result = await h.flow._run_flow(root=h.root, source=source, task_id='incoming',
                        candidate=candidate(second_provider, 'movie'), is_anime=True, is_movie=True)
                service = h.service(); key = old.plan.metadata['proposed_work_key']
                registered = service.work_catalog.load(key)
                row = registered['versions'][0]
                expected_root = registered['canonical_root']
                self.assertTrue(expected_root)
                self.assertTrue(row['version_ref'].startswith('mv-'))
                before = h.stable_files()
                intent = prepare_existing(service, result.plan.task_id, PlanStore.fingerprint(result.plan),
                    key=key, reason='人工选定作品及换源', groups=[dict(sources=[str(primary)], label=row['display_label'])],
                    excluded_sources=(), replace_version_ref=row['version_ref'])
                pending = commit_choice(service, intent)
                self.assertEqual(key, pending.metadata['catalog_work_key'])
                self.assertEqual(expected_root, pending.metadata['canonical_work_root'])
                self.assertEqual(row['version_ref'], pending.metadata['movie_version_selection']['replacement']['version_ref'])
                self.assertEqual(before, h.stable_files(), '归属与换源意图不得提前登记或操作媒体')
                operation, _ = service.asset_actions().prepare_replacement(pending)
                confirmed = service.confirm_asset_replacement(pending.task_id, PlanStore.fingerprint(pending),
                    expected_operation_fingerprint=operation.fingerprint)
                import json
                await h.finish(SimpleNamespace(**{**vars(result), 'plan': confirmed,
                    'store': service.plan_store, 'review': service,
                    'task': json.loads((h.root/'tasks'/f'{confirmed.task_id}.json').read_text('utf-8'))}), 'movie')
                after = service.work_catalog.load(key)
                self.assertEqual({'tmdb', 'bangumi'}, {p['namespace'] for p in after['providers']})
                self.assertEqual(expected_root, after['canonical_root'])
                self.assertEqual([confirmed.task_id], [v['task_id'] for v in after['versions']])
