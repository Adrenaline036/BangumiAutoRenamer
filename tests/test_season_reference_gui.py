"""Regression for the operator's corrected-work dialog, via actual GUI events."""
import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_cross_provider_work_correction as cross
from tests.test_content_choice_contract import records as all_records


def records(root):
    # Optional reference records are allowed; every authoritative/media byte is still compared.
    return {key:value for key,value in all_records(root).items() if 'season_references/' not in key}


class SelectionFeedback(unittest.IsolatedAsyncioTestCase):
    fixture = cross.CrossProviderWorkCorrection.fixture
    original = cross.CrossProviderWorkCorrection.original
    observe = cross.CrossProviderWorkCorrection.observe
    create_page = cross.CrossProviderWorkCorrection.create_page
    gui_correction = cross.CrossProviderWorkCorrection.gui_correction

    def setUp(self):
        from src.rename.season_reference import SeasonReferences
        self.enterContext(patch('src.pages.season_reference_view.load', return_value=SeasonReferences()))

    async def test_success_collapses_old_candidates_in_actual_dialog(self):
        await self.gui_correction(tmdb=False)
        self.assertFalse(self.view.provider_selection_expanded,
                         'committed work is visible below, but obsolete selector remains open')
        self.assertFalse(any(getattr(e, 'text', '') == '选择正确的作品'
                             for e in self.client.elements.values()))

    async def test_failed_commit_keeps_selector_and_original_records(self):
        root, service, _, result, *_ = await self.original()
        observed = self.observe(service, result.plan, cross.TMDB)
        self.create_page(prepared=(root, service, result.plan, []))
        with self.client:
            self.view._show_provider_candidates()
            with patch.object(service, 'observe_work_correction', return_value=observed):
                await self.view._select_provider_candidate(cross.TMDB)
            cross.fill_manual_season(self.client, 3)
        await self.driver.click('预览更正后的计划')
        before = records(root)
        with patch.object(service, 'commit_work_correction', side_effect=ValueError('isolated rejected commit')):
            await self.driver.click('确认作品更正并生成新计划')
        self.assertEqual(before, records(root))
        self.assertTrue(self.view.provider_selection_expanded)
        self.assertTrue(any('isolated rejected commit' in s for s in self.notices))

    async def test_unrelated_adopt_keeps_explicitly_opened_selector(self):
        root, service, _, result, *_ = await self.original()
        self.create_page(prepared=(root, service, result.plan, []))
        with self.client:
            self.view._show_provider_candidates()
            await self.view._adopt_revision(result.plan)
        self.assertTrue(self.view.provider_selection_expanded)

    async def test_unrelated_season_commit_keeps_explicitly_opened_selector(self):
        root, service, _, result, *_ = await self.original(tmdb=True)
        self.create_page(prepared=(root, service, result.plan, []))
        with self.client:
            self.view._show_provider_candidates()
        await self.driver.click('更改季度')
        with self.client:
            cross.fill_manual_season(self.client, 3)
        await self.driver.click('确认季度并生成新计划')
        self.assertEqual(3, service.plan_store.load(result.plan.task_id).metadata['season_number'])
        self.assertTrue(self.view.provider_selection_expanded)
        self.assertTrue(any(getattr(e, 'text', '') == '选择正确的作品'
                            for e in self.client.elements.values()))

    async def prepare_correction(self):
        root, service, _, result, *_ = await self.original()
        observed = self.observe(service, result.plan, cross.TMDB)
        self.create_page(prepared=(root, service, result.plan, []))
        with self.client:
            self.view._show_provider_candidates()
            with patch.object(service, 'observe_work_correction', return_value=observed):
                await self.view._select_provider_candidate(cross.TMDB)
        return root, service, result.plan

    async def test_manual_then_picker_and_reverse_commit_effective_visible_choice(self):
        _, service, plan = await self.prepare_correction()
        choice = self.view.work_season_choice
        with self.client:
            choice.manual.set_value(True)
            choice.custom.set_value(7)
            choice.manual.set_value(False)
            choice.picker.set_value(2)
        await self.driver.click('预览更正后的计划')
        await self.driver.click('确认作品更正并生成新计划')
        self.assertEqual(2, service.plan_store.load(plan.task_id).metadata['season_number'])
        await self.driver.click('更改季度')
        choice = self.view.general_season_choice
        with self.client:
            choice.picker.set_value(2)
            choice.manual.set_value(True)
            choice.custom.set_value(7)
        await self.driver.click('确认季度并生成新计划')
        self.assertEqual(7, service.plan_store.load(plan.task_id).metadata['season_number'])
        await self.driver.click('更改季度')
        choice = self.view.general_season_choice
        with self.client:
            self.assertTrue(choice.manual.value)
            self.assertEqual(7, choice.custom.value)
            choice.manual.set_value(False)
            choice.picker.set_value(0)
        await self.driver.click('确认季度并生成新计划')
        self.assertEqual(0, service.plan_store.load(plan.task_id).metadata['season_number'])

    async def test_saved_revision_refresh_failure_reports_saved(self):
        _, service, plan = await self.prepare_correction()
        with self.client:
            cross.fill_manual_season(self.client, 3)
        await self.driver.click('预览更正后的计划')
        with patch.object(self.view, '_adopt_revision', side_effect=RuntimeError('isolated refresh failed')):
            await self.driver.click('确认作品更正并生成新计划')
        self.assertEqual(3, service.plan_store.load(plan.task_id).metadata['season_number'])
        self.assertTrue(any('计划已保存，页面未能同步' in s for s in self.notices))
        self.assertFalse(any('处理失败' in s or '请重试' in s for s in self.notices))

    async def test_mode_change_same_number_invalidates_old_preview(self):
        root, _, _ = await self.prepare_correction()
        choice = self.view.work_season_choice
        with self.client:
            choice.manual.set_value(False)
            choice.picker.set_value(2)
        await self.driver.click('预览更正后的计划')
        before = records(root)
        with self.client:
            choice.custom.set_value(2)
            choice.manual.set_value(True)
        await self.driver.click('确认作品更正并生成新计划')
        self.assertEqual(before, records(root))
        self.assertTrue(any('请重新预览' in s for s in self.notices))

    async def test_saved_revision_reload_failure_reports_saved(self):
        _, service, plan = await self.prepare_correction()
        with self.client:
            cross.fill_manual_season(self.client, 3)
        await self.driver.click('预览更正后的计划')
        original_commit, original_load = service.commit_work_correction, service.plan_store.load
        saved = False
        def commit(intent):
            nonlocal saved
            result = original_commit(intent)
            saved = True
            return result
        def load(*args):
            if saved:
                raise OSError('isolated post-save reload failed')
            return original_load(*args)
        with patch.object(service, 'commit_work_correction', side_effect=commit), \
                patch.object(service.plan_store, 'load', side_effect=load):
            await self.driver.click('确认作品更正并生成新计划')
        self.assertEqual(3, original_load(plan.task_id).metadata['season_number'])
        self.assertTrue(any('计划已保存，页面未能同步' in s for s in self.notices))

    async def test_reference_completion_preserves_choice_records_and_discards_closed_page(self):
        from src.rename.season_reference import SeasonReferences
        reference = SeasonReferences((dict(provider_id='11', title='第一季资料'),), (('11', 1),), 'found')
        root, service, _, result, *_ = await self.original(tmdb=True)
        self.create_page(prepared=(root, service, result.plan, []))
        await self.view._season_reference_task
        for close in (False, True):
            entered, release = threading.Event(), threading.Event()
            def blocked(_provider):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('fixture timed out')
                return reference
            with patch('src.pages.season_reference_view.load', side_effect=blocked):
                from src.pages.season_reference_view import prepare
                prepare(self.view,retry=True)
                await self.driver.click('更改季度')
                choice = self.view.general_season_choice
                expected_options = list(choice.picker.options)
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                before = records(root)
                with self.client:
                    choice.manual.set_value(True)
                    choice.custom.set_value(9)
                    if close:
                        self.view._show_provider_candidates()
                release.set()
                await choice.reference_task
                self.assertEqual(before, records(root))
                self.assertEqual(9, choice())
                if not close:
                    self.assertEqual(expected_options, list(choice.picker.options))
                    self.assertIn('第一季资料', choice.picker.options[1])
                else:
                    self.assertTrue(choice.reference_container.is_deleted)

    async def test_named_bangumi_sibling_uses_existing_work_correction_transaction(self):
        from nicegui import core
        from src.rename.season_reference import SeasonReferences
        root, service, _, result, *_ = await self.original(tmdb=True)
        plan = cross.CrossProviderWorkCorrection.confirm(self, service, result.plan, cross.BANGUMI, 1)
        sibling = {**cross.BANGUMI, 'provider_id': '53919', 'title': '新的动画 第二季'}
        observed = self.observe(service, plan, sibling)
        refs = SeasonReferences((cross.BANGUMI, sibling), (('53918', 1), ('53919', 2)), 'found')
        with patch('src.pages.season_reference_view.load', return_value=refs):
            self.create_page(prepared=(root, service, plan, []))
            await self.driver.click('更改季度')
            await self.view.general_season_choice.reference_task
            picker = next(e for e in self.client.elements.values()
                          if type(e).__name__ == 'Select' and e._props.get('label') == 'Bangumi 季度条目')
            self.assertIn('第 2 季', picker.options['53919'])
            before = records(root)
            with patch.object(service, 'observe_work_correction', return_value=observed):
                with self.client, patch.multiple(core, loop=asyncio.get_running_loop()):
                    previous = asyncio.all_tasks()
                    picker.set_value('53919')
                    await asyncio.gather(*(asyncio.all_tasks() - previous))
            self.assertEqual(before, records(root), 'choosing a named sibling opens preview only')
            await self.view.work_season_choice.reference_task
            self.assertEqual(2, self.view.work_season_choice())
            await self.driver.click('预览更正后的计划')
            await self.driver.click('确认作品更正并生成新计划')
        saved = service.plan_store.load(plan.task_id)
        self.assertEqual('53919', saved.work_identity.primary_provider.provider_id)
        self.assertEqual(2, saved.metadata['season_number'])
        self.assertFalse(self.view.provider_selection_expanded)
