"""基准窗口寿命和领域修正的限定复现；真实媒体不参与。"""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from nicegui import Client, ui
from nicegui.page import page


class RebuildWindow(unittest.IsolatedAsyncioTestCase):
    async def test_result_survives_dashboard_refresh(self):
        from src.pages import completed_rebuild_view as module
        client = Client(page('/baseline-rebuild'), request=None)
        self.addCleanup(client.delete)
        entered, finish = asyncio.Event(), asyncio.Event()
        async def inspect(*args):
            entered.set()
            await finish.wait()
            return SimpleNamespace(state='complete', detail='本次核查完成')
        with client:
            card = ui.column()
        with patch.object(module.asyncio, 'to_thread', side_effect=inspect):
            async def open_here():
                with client, card:
                    return await module.open_rebuild_dialog('isolated',
                        manager=SimpleNamespace(execution_service=SimpleNamespace(rebuild_service=SimpleNamespace(inspect=lambda _: None))))
            worker = asyncio.create_task(open_here())
            await asyncio.wait_for(entered.wait(), 5)
            with client:
                card.clear()
            finish.set()
            dialog = await worker
        self.assertFalse(dialog.is_deleted, '任务列表刷新不能删除仍在核查的窗口')
        self.assertTrue(dialog.value)
        self.assertIn('本次核查完成', [getattr(e, 'text', '') for e in dialog.descendants()])


from tests.test_source_regrouping_view_contract import SourceRegroupingViewContracts


class RegroupingResponse(SourceRegroupingViewContracts):
    async def test_preview_does_not_read_records_on_ui_thread(self):
        async with self.workspace() as (flow, work):
            caller, seen = threading.get_ident(), []
            original = flow.manager.preview_source_regrouping
            def observe(*args, **kwargs):
                seen.append(threading.get_ident())
                return original(*args, **kwargs)
            with patch.object(flow.manager, 'preview_source_regrouping', side_effect=observe):
                with work.client:
                    result = self.click(work, '调整计划范围')
                    if hasattr(result, '__await__'):
                        await result
            self.assertTrue(seen)
            self.assertNotIn(caller, seen, '完整分组预览不得在网页事件线程同步读盘')


from tests.test_initial_movie_completion_contract import InitialMovieFullCompletionContracts


def assert_domain_verifiable(case, plan):
    from dataclasses import replace
    from src.rename.plan_verifier import PlanVerifier
    for marker in ('explicit_user', 'unregistered-source'):
        forged = replace(plan, work_identity=replace(plan.work_identity, domain_source=marker))
        case.assertIn('work_identity_domain_source_invalid',
                      {issue.code for issue in PlanVerifier().verify(forged).issues})
    codes = {issue.code for issue in PlanVerifier().verify(plan).issues}
    case.assertNotIn('work_identity_domain_source_invalid', codes)
    case.assertEqual('manual_library_selection', plan.work_identity.domain_source)


class MovieDomain(InitialMovieFullCompletionContracts):
    async def test_unfinished_movie_domain_can_be_corrected_then_completed(self):
        from src.rename.decision_state import DecisionCommandService
        from src.rename.plan_store import PlanStore
        from src.pages import plan_review_page as page_module
        from src.rename.movie_work_correction import observe_domain, prepare_observed
        from src.rename.initial_movie_completion import commit
        from tests.test_content_choice_contract import records
        root, service, current, primary, sources = await self.fixture()
        from tests.test_content_subtitle_lifecycle import apply
        current = apply(service, current, sources['subtitle'], owner=primary)
        self.assertTrue(current.subtitle_override_rules)
        state = service.decision_state_store.load(current.task_id)
        self.assertIsNone(state.movie_version_selection)
        new_domain = 'general' if state.library_domain == 'animation' else 'animation'
        before_legacy = records(root)
        with self.assertRaises((ValueError, RuntimeError)):
            DecisionCommandService(service.decision_state_store, service.plan_store).update(
                current.task_id, expected_revision=state.revision,
                changes={'library_domain': new_domain}, planner=lambda _: current,
                task_terminal=service._revision_task_terminal(current.task_id))
        self.assertEqual(before_legacy, records(root))
        adopted, notices = [], []
        async def adopt(plan):
            adopted.append(plan)
        view = SimpleNamespace(task_decision=state, plan=current, review_service=service,
            decision_command_service=DecisionCommandService(service.decision_state_store, service.plan_store),
            _adopt_revision=adopt)
        with patch.object(page_module, 'notify', side_effect=lambda text, **_: notices.append(str(text))), \
                patch.object(page_module, '_task_terminal_for_projected_plan', side_effect=service._revision_task_terminal):
            await page_module.PlanReviewDialog._select_library_domain(view, new_domain)
        saved = service.plan_store.load(current.task_id)
        after = service.decision_state_store.load(current.task_id)
        self.assertEqual(new_domain, saved.work_identity.domain, notices)
        self.assertEqual(new_domain, after.library_domain)
        self.assertEqual(current.work_identity.primary_provider, saved.work_identity.primary_provider)
        self.assertEqual(state.content_selection, after.content_selection)
        self.assertEqual(current.subtitle_override_rules, saved.subtitle_override_rules)
        self.assertIsNone(after.movie_version_selection)
        self.assertEqual(1, len(adopted), notices)
        self.assertFalse(service.load_or_create(saved).approved_entry_ids)
        observation = observe_domain(service, saved.task_id, PlanStore.fingerprint(saved),
            expected_revision=after.revision, domain=current.work_identity.domain)
        from dataclasses import replace
        unchanged = records(root)
        with self.assertRaises((ValueError, RuntimeError)):
            prepare_observed(service, replace(observation))
        self.assertEqual(unchanged, records(root))
        bound = prepare_observed(service, observation)
        for forged in (replace(bound, inputs=replace(bound.inputs, groups_json='[{"sources":[],"label":"forged"}]')),
                       replace(bound, inputs=replace(bound.inputs, replace_version_ref='wrong-version'))):
            with self.assertRaises((ValueError, RuntimeError)):
                commit(service, forged)
            self.assertEqual(unchanged, records(root))
        intent = self.prepare(service, saved, [dict(sources=[str(primary)], label='人工版本')])
        completed = service.commit_initial_movie_completion(intent)
        self.assertEqual(new_domain, completed.work_identity.domain)
        assert_domain_verifiable(self, completed)
        self.assertIsNotNone(service.decision_state_store.load(saved.task_id).movie_version_selection)
        self.assertIn('人工版本', str(completed.metadata['movie_version_selection']))
        before_stale = records(root)
        with self.assertRaises((ValueError, RuntimeError)):
            prepare_observed(service, observation)
        self.assertEqual(before_stale, records(root))

    async def test_correction_reprojects_same_movie_and_preserves_choices(self):
        from src.rename.decision_state import DecisionCommandService
        from src.rename.plan_store import PlanStore
        from src.pages import plan_review_page as page_module
        root, service, plan, primary, _ = await self.fixture()
        intent = self.prepare(service, plan, [dict(sources=[str(primary)], label='人工版本')])
        current = service.commit_initial_movie_completion(intent)
        state = service.decision_state_store.load(current.task_id)
        new_domain = 'general' if state.library_domain == 'animation' else 'animation'
        adopted, notices = [], []
        async def adopt(plan):
            adopted.append(plan)
        view = SimpleNamespace(task_decision=state, plan=current, review_service=service,
            decision_command_service=DecisionCommandService(service.decision_state_store, service.plan_store),
            _adopt_revision=adopt)
        with patch.object(page_module, 'notify', side_effect=lambda text, **_: notices.append(str(text))), \
                patch.object(page_module, '_task_terminal_for_projected_plan', side_effect=service._revision_task_terminal):
            await page_module.PlanReviewDialog._select_library_domain(view, new_domain)
        saved = service.plan_store.load(current.task_id)
        self.assertEqual(new_domain, saved.work_identity.domain, notices)
        self.assertEqual(current.work_identity.primary_provider.provider_id,
                         saved.work_identity.primary_provider.provider_id)
        self.assertEqual(current.subtitle_override_rules, saved.subtitle_override_rules)
        self.assertEqual(1, len(adopted), notices)
        self.assertNotEqual(current.metadata['canonical_work_root'], saved.metadata['canonical_work_root'])
        self.assertFalse(service.load_or_create(saved).approved_entry_ids)
        self.assertEqual(new_domain == 'animation', __import__('src.utils.utils', fromlist=['read_task_at']).read_task_at(
            service.task_root, saved.task_id)['is_anime'])
        from src.rename.movie_work_correction import observe_domain, prepare_observed
        from src.rename.initial_movie_completion import commit
        from dataclasses import replace
        state = service.decision_state_store.load(saved.task_id)
        with patch('src.rename.get_info.Search.get_movie_info_by_id', side_effect=AssertionError('同作品保存位置不能重查资料')):
            observed = observe_domain(service, saved.task_id, PlanStore.fingerprint(saved),
                expected_revision=state.revision, domain=current.work_identity.domain)
            with self.assertRaises(ValueError):
                prepare_observed(service, replace(observed))
            corrected = commit(service, prepare_observed(service, observed))
        self.assertEqual(current.work_identity.primary_provider, corrected.work_identity.primary_provider)
        assert_domain_verifiable(self, corrected)
        self.assertEqual(current.metadata['canonical_work_root'], corrected.metadata['canonical_work_root'])
        self.assertEqual(current.subtitle_override_rules, corrected.subtitle_override_rules)
        self.assertFalse(service.load_or_create(corrected).approved_entry_ids)
        with self.assertRaises((ValueError, RuntimeError)):
            prepare_observed(service, observed)  # 已保存的新修订不能再次使用旧预览。



from tests.test_completed_rebuild_contract import CompletedRebuildContracts


class CompletedDisplay(CompletedRebuildContracts):
    async def test_real_completed_task_never_says_unexecuted(self):
        import tempfile
        from pathlib import Path
        from src.rename.plan_review import PlanReviewService
        from src.rename.plan_store import PlanStore
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            from tests import _media_flow_helpers as fixture
            harness = fixture.MediaFlow('runTest')
            source, _ = fixture.make_season(root)
            result = await harness._run_flow(root=root, source=source,
                task_id='display-completed-media', subject_id='41658')
            pending_service = PlanReviewService(PlanStore(root / 'plans'), root / 'reviews', root / 'work_catalog',
                decision_state_root=root / 'decisions', task_root=root / 'tasks', record_root=root / 'records',
                provider_link_catalog_root=root / 'provider_links')
            pending_gui = ContentWorkbenchGui('runTest')
            pending_gui.create_page(prepared=(root, pending_service, result.plan, []), cleanup_owner=self)
            pending_labels = [str(getattr(e, 'text', e._props.get('label', ''))) for e in pending_gui.view.descendants() if e.visible]
            self.assertIn('需要人工确认', pending_labels)
            self.assertIn('需要确认', pending_labels)
            self.assertIn(result.plan.entries[0].source_path.name, pending_labels)
            task, ledger, execution = await harness._approve_and_execute(root=root, result=result)
            self.assertEqual('completed', task.get('execution_status'))
            self.assertEqual('completed', task.get('workflow_status'))
            self.assertEqual('completed', ledger.get('status'))
            plan = PlanStore(root / 'plans').load(result.plan.task_id)
            service = PlanReviewService(PlanStore(root / 'plans'), root / 'reviews', root / 'work_catalog',
                decision_state_root=root / 'decisions', task_root=root / 'tasks', record_root=root / 'records',
                provider_link_catalog_root=root / 'provider_links')
            gui = ContentWorkbenchGui('runTest')
            gui.create_page(prepared=(root, service, plan, []), cleanup_owner=self)
            labels = [str(getattr(e, 'text', e._props.get('label', ''))) for e in gui.view.descendants() if e.visible]
            self.assertIn('整理完成', labels, '详情必须正向显示同计划的完成状态')
            self.assertIn('已整理', labels, '已执行的媒体条目必须显示完成')
            self.assertIn(plan.entries[0].source_path.name, labels)
            self.assertFalse({'需要人工确认', '需要确认'} & set(labels),
                             '同一已完成计划不得继续显示冻结计划的待确认标记')
            self.assertFalse(any('尚未创建硬链接' in text or '未执行' in text for text in labels),
                             '已有完成执行记录，详情不得显示未执行：' + repr([t for t in labels if '未执行' in t or '尚未创建硬链接' in t]))


from tests.test_default_activation_contract import DefaultActivationProducerContracts


class AutomaticMovieDomain(DefaultActivationProducerContracts):
    async def test_automatic_identity_retains_manual_domain_until_completion(self):
        from src.rename.movie_work_correction import observe_domain, prepare_observed
        from src.rename.initial_movie_completion import commit
        from src.rename.plan_store import PlanStore
        reached = []
        def interrupt(transaction, envelope, terminal, forward):
            if envelope.draft_plan.revision_action == 'initial_movie_completion' and not reached:
                reached.append(True)
                raise ValueError('disposable pause before selecting main video')
            return forward()
        async with self.flow(movie=True, hook=interrupt) as flow:
            self.assertEqual([True], reached)
            plan, state = self.assert_default(flow)
            self.assertIsNone(state.movie_version_selection)
            observation = observe_domain(flow.service, plan.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, domain='animation')
            revised = commit(flow.service, prepare_observed(flow.service, observation))
            self.assertEqual('animation', revised.work_identity.domain)
            self.assertEqual(state.content_selection,
                             flow.service.decision_state_store.load(plan.task_id).content_selection)
            intent = flow.service.prepare_initial_movie_completion(plan.task_id, PlanStore.fingerprint(revised),
                groups=[dict(sources=[str(flow.source/'Ordinary Movie [1080p].mkv')], label='人工版本')],
                reason='人工选择正片')
            completed = flow.service.commit_initial_movie_completion(intent)
            self.assertEqual('animation', completed.work_identity.domain)
            assert_domain_verifiable(self, completed)
            self.assertEqual(plan.work_identity.primary_provider, completed.work_identity.primary_provider)


class SourceOnlyDisplay(unittest.TestCase):
    def test_source_preview_has_files_without_plan_or_execution_state(self):
        from src.pages.task_source_files_view import SourceFileList
        client = Client(page('/baseline-source-only'), request=None)
        self.addCleanup(client.delete)
        with client:
            view = SourceFileList([dict(source='D:/sample/IV.mkv', name='IV.mkv', file='IV.mkv')])
        self.assertIsNone(view.plan)
        labels = [str(getattr(e, 'text', e._props.get('label', ''))) for e in view.container.descendants() if e.visible]
        self.assertIn('IV.mkv', labels)
        self.assertIn('D:/sample/IV.mkv', labels)
        self.assertFalse({'已整理', '需要确认', '已批准 · 未执行'} & set(labels))
