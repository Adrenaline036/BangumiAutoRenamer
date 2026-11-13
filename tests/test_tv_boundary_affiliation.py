"""A registered TMDB season followed by an explicit Bangumi boundary choice."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.test_err056_cohort import IncompleteMetadataFlow
from tests.test_r24_err048_series_registration_contract import MEMBERS
from tests import _media_flow_helpers as font_fixture
from src.pages.plan_review_page import PlanReviewDialog
from src.pages.review_operation_view import build_review_operation_presentation
from src.rename.plan_store import PlanStore
from src.rename.content_associations import encode
from src.rename.series_catalog import SeriesCatalogError


class RegisteredBoundaryFlow(IncompleteMetadataFlow):
    def __init__(self, root):
        super().__init__(root)
        _, _font = font_fixture.make_season(root / 'container-template')
        self.video = (root / 'container-template/fixture-input/valid.mkv').read_bytes()
        self.source_factory = self.make_source
        search = self.search_type
        title = self.subjects[MEMBERS[0]]['original_title']

        class Search(search):
            def get_tv_info_by_id(self, provider_id):
                if str(provider_id) == '83121':
                    return self.get_tv_info(title)
                return super().get_tv_info_by_id(provider_id)

            def get_season_info(self, provider_id, season_number):
                if str(provider_id) == '83121':
                    return dict(season_number=season_number, episodes=[])
                return super().get_season_info(provider_id, season_number)

        self.search_type = Search

    def make_source(self, root, _title, _numbers):
        root.mkdir(parents=True)
        for n in (1, 2):
            (root / f'Kaguya S01E{n:02d}.mkv').write_bytes(self.video + root.name.encode())
        return root

    async def select(self, task_id, selected, *, confirm=True):
        if selected != 'old-tmdb':
            return await super().select(task_id, selected, confirm=confirm)
        current = self.result(task_id)
        owner = SimpleNamespace(task_decision=current.state, plan=current.plan,
            task_data=current.task, close=MagicMock())
        subject = self.subjects[MEMBERS[0]]
        await PlanReviewDialog._select_provider_candidate(owner,
            dict(namespace='tmdb', entity_type='tv', provider_id='83121',
                 title=subject['title'], original_title=subject['original_title'],
                 year=2019, library_domain='animation'), season_number=1)
        await self.joined()
        return self.result(task_id)

    async def pair(self):
        self.review.record_root = self.root / 'records'
        self.review.root_snapshot_source = self.manager.root_snapshot_source
        old = await self.add('old-tmdb')
        case = font_fixture.MediaFlow('runTest')
        task, ledger, _ = await case._approve_and_execute(root=self.root, result=old)
        assert task['execution_status'] == ledger['status'] == 'completed', task.get('error')
        old = self.result(old.task_id)
        new = await self.add(MEMBERS[0], confirm=False)
        command = next(a.payload for a in build_review_operation_presentation(new.task).actions
                       if a.payload and a.payload.get('kind') == 'confirm_cohort')
        return old, new, command


import hashlib
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import patch

from nicegui.client import Client
from nicegui.page import page as nice_page
from nicegui.elements.select import Select
from src.rename import work_affiliation as affiliation
from src.rename.projection_authority import validate_plan_projection_receipt
from src.pages import plan_review_page as page
from tests import _gui_helpers as gui_driver
from tests.test_r24_phase_c_review_operation_migration_contract import current_provider_selection_context


def records(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*.json') if not p.name.endswith('.lock')}


class TVBoundaryAffiliationContracts(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def incident(self):
        with tempfile.TemporaryDirectory() as directory:
            async with RegisteredBoundaryFlow(Path(directory)) as runtime:
                old, new, command = await runtime.pair()
                yield runtime, old, new, command

    def require_choice(self, runtime, new, command):
        before = records(runtime.root)
        with self.assertRaises(affiliation.AffiliationRequired,
                msg='Readonly boundary preparation must request affiliation before committing any pending revision') as caught:
            runtime.review.prepare_tv_boundary_completion(new.task_id,
                PlanStore.fingerprint(new.plan), review_command=command)
        self.assertEqual(before, records(runtime.root))
        self.assertIsNotNone(caught.exception.inputs)
        self.assertEqual('boundary', caught.exception.inputs.mode)
        return caught.exception.inputs

    async def test_original_boundary_and_explicit_affiliation_commit_together(self):
        async with self.incident() as (runtime, old, new, command):
            inputs = self.require_choice(runtime, new, command)
            key = old.state.series_binding['series_key']
            before_catalog = {k:v for k,v in records(runtime.root).items() if 'catalog' in k}
            with patch.object(runtime.renamer.search, 'get_tv_info', side_effect=AssertionError('frozen confirmation cannot request Provider')):
                rows = affiliation.catalog_choices(runtime.review, new.task_id,
                    PlanStore.fingerprint(new.plan), correction_inputs=inputs)
                self.assertIn(key, [row['key'] for row in rows])
                intent, preview = affiliation.preview_correction_choice(runtime.review, inputs, key=key)
                self.assertEqual('boundary', intent.inputs.mode)
                self.assertEqual('tv_content_boundary', preview.revision_action)
                final = affiliation.commit_choice(runtime.review, intent)
            saved = runtime.result(new.task_id)
            self.assertEqual(new.state.revision + 1, saved.state.revision)
            self.assertEqual(key, saved.state.series_binding['series_key'])
            self.assertEqual(old.state.series_binding['canonical_root'], saved.state.series_binding['canonical_root'])
            self.assertEqual(1, saved.state.season_binding['season_number'])
            self.assertIsNone(saved.state.pending_tv_selection)
            self.assertNotIn('pending_tv_selection', final.metadata)
            self.assertNotIn('pending_work_affiliation', final.metadata)
            self.assertEqual(new.state.content_selection.to_dict()['excluded_sources'],
                             saved.state.content_selection.to_dict()['excluded_sources'])
            self.assertEqual(new.plan.expected_sources, final.expected_sources)
            self.assertEqual(new.plan.ignored_sources, final.ignored_sources)
            self.assertEqual(new.plan.subtitle_override_rules, final.subtitle_override_rules)
            self.assertEqual(new.state.initial_source_basis, saved.state.initial_source_basis)
            self.assertEqual('succeeded', current_provider_selection_context(saved.task)['lifecycle'])
            receipt = final.metadata['tv_boundary_completion_receipt']
            self.assertEqual(current_provider_selection_context(new.task)['request_id'], receipt['request_id'])
            self.assertEqual(encode(command), encode(receipt['command']))
            validate_plan_projection_receipt(final)
            self.assertEqual('draft', runtime.review.load_or_create(final).status)
            self.assertEqual(before_catalog, {k:v for k,v in records(runtime.root).items() if 'catalog' in k})
            self.assertFalse((runtime.root/'records'/f'{new.task_id}.ledger.json').exists())
            settled = records(runtime.root)
            with self.assertRaises((ValueError, RuntimeError)):
                affiliation.commit_choice(runtime.review, intent)
            self.assertEqual(settled, records(runtime.root))
            # The old occupied slot still requires an explicit season replacement.
            checked = final
            from src.rename.plan_review import entry_id, PlanReviewError
            with self.assertRaisesRegex(PlanReviewError, '换源|存在|占用|冲突|collision'):
                runtime.review.approve_entries(new.task_id, PlanStore.fingerprint(checked),
                    [entry_id(e) for e in checked.entries])
            operation, _ = runtime.review.asset_actions().prepare_replacement(checked)
            self.assertEqual('replace', operation.to_dict()['mode'])
            self.assertEqual(key, operation.to_dict()['catalog_before']['key'])
            self.assertTrue(operation.to_dict()['catalog_before']['rows']['bindings'])
            runtime.assert_sources_unchanged(self)

    async def test_declining_old_local_work_preserves_original_subject_cohort(self):
        from src.rename.successor_authority import CohortAuthorityReader
        async with self.incident() as (runtime, old, new, command):
            inputs = self.require_choice(runtime, new, command)
            catalog = {k:v for k,v in records(runtime.root).items() if 'catalog' in k}
            intent, _ = affiliation.preview_correction_choice(runtime.review, inputs, separate=True)
            final = affiliation.commit_choice(runtime.review, intent)
            saved = runtime.result(new.task_id)
            self.assertNotEqual(old.state.series_binding['series_key'], saved.state.series_binding['series_key'])
            self.assertEqual('separate', final.metadata['work_affiliation']['action'])
            self.assertEqual(encode(command), encode(final.metadata['tv_boundary_completion_receipt']['command']))
            self.assertEqual('matched', CohortAuthorityReader.from_decision_root(runtime.decisions).lookup(
                MEMBERS[1], consumer_task_id='later-task').status)
            self.assertEqual(catalog, {k:v for k,v in records(runtime.root).items() if 'catalog' in k})
            self.assertIsNone(saved.state.pending_tv_selection)
            self.assertEqual('succeeded', current_provider_selection_context(saved.task)['lifecycle'])
            self.assertFalse(any(e.target_path.exists() for e in final.entries))

    async def test_forged_boundary_choice_and_generic_affiliation_cannot_bypass_request(self):
        async with self.incident() as (runtime, old, new, command):
            inputs = self.require_choice(runtime, new, command)
            before = records(runtime.root)
            key = old.state.series_binding['series_key']
            for changed in (replace(inputs, edit_json='{}'), replace(inputs, task_fingerprint='0'*64),
                            replace(inputs, decision_fingerprint='0'*64),
                            replace(inputs, roots=replace(inputs.roots, anime_path=runtime.root/'elsewhere'))):
                with self.subTest(kind=changed), self.assertRaises(ValueError):
                    affiliation.preview_correction_choice(runtime.review, changed, key=key)
            with self.assertRaises(ValueError):
                affiliation.prepare_existing(runtime.review, new.task_id, PlanStore.fingerprint(new.plan), key=key,
                    reason='测试普通归属入口不能接管未决季度请求')
            self.assertEqual(before, records(runtime.root))

    async def test_source_drift_guard_disconnect_and_failed_commit_preserve_parent(self):
        from src.rename import tv_boundary_completion as boundary
        from src.rename.projection_authority import ProjectionCommitService
        async with self.incident() as (runtime, old, new, command):
            inputs = self.require_choice(runtime, new, command)
            key = old.state.series_binding['series_key']
            before = records(runtime.root)
            with patch.object(boundary, 'resolve', return_value=None), self.assertRaises(ValueError):
                affiliation.preview_correction_choice(runtime.review, inputs, key=key)
            self.assertEqual(before, records(runtime.root))
            import json
            path = runtime.review.series_catalog._path(key)
            original = path.read_bytes()
            value = json.loads(original)
            value['canonical_display_name'] += ' changed'
            path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
            try:
                with self.assertRaisesRegex(SeriesCatalogError, '快照已变化'):
                    affiliation.preview_correction_choice(runtime.review, inputs, key=key)
            finally:
                path.write_bytes(original)
            self.assertEqual(before, records(runtime.root))
            source = new.plan.entries[0].source_path
            data = source.read_bytes()
            stat = source.stat()
            source.write_bytes(data + b'drift')
            with self.assertRaises(ValueError):
                affiliation.preview_correction_choice(runtime.review, inputs, key=key)
            source.write_bytes(data)
            import os
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            intent, _ = affiliation.preview_correction_choice(runtime.review, inputs, key=key)
            with patch.object(ProjectionCommitService, '_commit_task_terminal', side_effect=ValueError('test terminal failure')):
                with self.assertRaisesRegex(ValueError, 'test terminal failure'):
                    affiliation.commit_choice(runtime.review, intent)
            restored = runtime.result(new.task_id)
            self.assertEqual(new.state, restored.state)
            self.assertEqual(PlanStore.fingerprint(new.plan), PlanStore.fingerprint(restored.plan))
            self.assertEqual(new.task, restored.task)
            self.assertEqual('awaiting_user', current_provider_selection_context(restored.task)['lifecycle'])
            # Journal evidence may grow; authoritative catalogs and old output cannot.
            self.assertEqual({k:v for k,v in before.items() if 'catalog' in k},
                             {k:v for k,v in records(runtime.root).items() if 'catalog' in k})
            again = affiliation.preview_correction_choice(runtime.review, inputs, key=key)[0]
            affiliation.commit_choice(runtime.review, again)
            self.assertIsNone(runtime.result(new.task_id).state.pending_tv_selection)

    @asynccontextmanager
    async def gui(self, runtime, new):
        client = Client(nice_page('/isolated-boundary-affiliation'), request=None)
        driver = gui_driver.GuiDriver('runTest')
        driver.client = client
        real = PlanReviewDialog
        notices = []
        with patch.object(page, 'PlanReviewDialog', real), patch.object(page, 'PlanReviewService', lambda *_a, **_kw: runtime.review), \
                patch.object(page, 'notify', lambda text, **kw: notices.append((str(text), kw.get('type')))):
            with client:
                view = real(new.plan, new.task)
                view.open()
            try:
                yield view, driver, client, notices
            finally:
                view.delete()
                client.delete()

    async def test_actual_registered_button_opens_existing_chooser_cancel_and_resume(self):
        async with self.incident() as (runtime, old, new, command):
            async with self.gui(runtime, new) as (view, driver, client, notices):
                label = next(a.label for a in build_review_operation_presentation(new.task).actions if a.payload == command)
                before = records(runtime.root)
                await driver.click(label)
                chooser = [e for e in client.elements.values() if isinstance(e, Select) and e.props.get('label') == '已登记作品']
                self.assertEqual(1, len(chooser), notices)
                self.assertEqual(before, records(runtime.root))
                await driver.click('取消')
                self.assertEqual(before, records(runtime.root))
                await driver.click(label)
                chooser = [e for e in client.elements.values() if isinstance(e, Select) and e.props.get('label') == '已登记作品' and not e.is_deleted]
                chooser[-1].set_value(old.state.series_binding['series_key'])
                await driver.click('预览归属')
                self.assertEqual(before, records(runtime.root))
                await driver.click('确认作品归属')
                self.assertEqual('succeeded', current_provider_selection_context(runtime.result(new.task_id).task)['lifecycle'], notices)
                self.assertFalse([text for text, kind in notices if kind == 'negative'], notices)

    async def test_confirmed_prepare_continues_after_close_and_duplicate_click(self):
        for mode in ('close', 'disconnect'):
            with self.subTest(mode=mode):
                await self.readonly_exit(mode)

    async def readonly_exit(self, mode):
        import threading
        # 已点击确认的季度命令由后台完成；窗口只停止显示结果。
        with tempfile.TemporaryDirectory() as directory:
            async with IncompleteMetadataFlow(Path(directory)) as runtime:
                new = await runtime.add(MEMBERS[0], confirm=False)
                runtime.review.root_snapshot_source = runtime.manager.root_snapshot_source
                async with self.gui(runtime, new) as (view, driver, _client, _notices):
                    presentation = build_review_operation_presentation(new.task)
                    request = runtime.manager._provider_reads.get(new.task_id)
                    self.assertIsNotNone(presentation, getattr(request, 'error', new.task.get('error')))
                    action = next(a for a in presentation.actions if a.payload and a.payload.get('kind') == 'confirm_cohort')
                    started, release = threading.Event(), threading.Event()
                    original = runtime.review.prepare_tv_boundary_completion
                    def hold(*args, **kwargs):
                        started.set()
                        if not release.wait(10): raise AssertionError('prepare barrier timed out')
                        return original(*args, **kwargs)
                    before = records(runtime.root)
                    with patch.object(runtime.review, 'prepare_tv_boundary_completion', side_effect=hold) as prepare, \
                            patch.object(runtime.review, 'commit_tv_boundary_completion', wraps=runtime.review.commit_tv_boundary_completion) as commit:
                        click = asyncio.create_task(driver.click(action.label))
                        try:
                            self.assertTrue(await asyncio.to_thread(started.wait, 5))
                            await driver.click(action.label)
                            if mode == 'close':
                                view.close()
                                self.assertFalse(view.value)
                                self.assertTrue(view._tv_content_closed, '真实关闭必须触发已注册的生命周期回调')
                            else:
                                for handler in tuple(view.client.disconnect_handlers):
                                    view.client.safe_invoke(handler)
                        finally:
                            release.set()
                            await asyncio.wait_for(click, 15)
                        self.assertEqual(1, prepare.call_count)
                        self.assertEqual(1, commit.call_count, '已提交的季度确认应在关窗后保存一次')
                    saved = runtime.result(new.task_id)
                    self.assertEqual(new.state.revision + 1, saved.state.revision)
                    self.assertIsNone(saved.state.pending_tv_selection)
                    self.assertTrue(saved.state.season_binding)
                    self.assertEqual(saved.plan.metadata['decision_state_fingerprint'], runtime.review.decision_state_store.fingerprint(saved.state))
                    self.assertEqual('succeeded', current_provider_selection_context(saved.task)['lifecycle'])
                    self.assertTrue(all(not e.target_path.exists() for e in saved.plan.entries))
