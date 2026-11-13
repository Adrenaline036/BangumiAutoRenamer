"""Registered NiceGUI handlers over real current TV plan transactions."""
from unittest.mock import patch
import unittest

from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.special_catalog import build_observation
from tests import test_initial_tv_completion_contract as tv
from tests import test_content_workbench_gui as gui
from tests.test_special_catalog_contract import pair
from tests.test_content_choice_contract import records


class SpecialCrosswalkGui(unittest.IsolatedAsyncioTestCase):
    fixture = tv.InitialTVCompletionContracts.fixture
    create_page = gui.ContentWorkbenchGui.create_page
    text = gui.ContentWorkbenchGui.text

    async def prepared(self):
        root, service, _, result, extras, *_ = await self.fixture(anime=False, exceptional=True,
            exceptional_name='Show SP.mkv', companions=True)
        value = pair().to_dict()
        provider = result.plan.work_identity.primary_provider.to_dict()
        value['relation']['tmdb']['provider_id'] = provider['provider_id']
        value['catalog'][1]['provider']['provider_id'] = provider['provider_id']
        observed = build_observation(provider, value['catalog'], relation=value['relation'], relation_reason='found')
        with patch('src.rename.special_crosswalk.observe', return_value=observed):
            plan = service.revise_tv_content(result.plan.task_id, PlanStore.fingerprint(result.plan),
                expected_revision=result.state.revision, mode='special_refresh', edit={})
        self.create_page(prepared=(root, service, plan, []))
        key = entry_id(next(e for e in plan.entries if e.source_path == extras['exceptional']))
        return root, service, plan, extras, key

    async def test_compact_numbers_collapsed_detail_actual_confirm_and_cancel(self):
        root, service, plan, extras, key = await self.prepared()
        before = records(root)
        await self.driver.click('特别篇 / OVA')
        controls = self.view._official_special_controls
        self.assertEqual('移出特别篇', controls['clear'].text)
        self.assertEqual('playlist_add_check', controls['confirm']._props['icon'])
        self.assertEqual('playlist_remove', controls['clear']._props['icon'])
        row = controls['confirm'].parent_slot.parent
        self.assertIs(row, controls['clear'].parent_slot.parent)
        close = next(e for e in row.descendants() if e._props.get('label') == '关闭')
        self.assertEqual('close', close._props['icon'])
        self.assertIn('ml-auto', close.classes)
        self.assertTrue(controls['confirm'].visible)
        self.assertFalse(controls['confirm'].enabled)
        self.assertFalse(controls['details'].value)
        self.assertEqual({'100:1011': 'Bangumi: 6.5'}, controls['bangumi'].options)
        self.assertEqual({'9001': 'TMDB: S00E07'}, controls['episode'].options)
        with self.client:
            controls['files'].set_value(key)
        self.assertEqual('9001', controls['episode'].value)
        self.assertTrue(controls['confirm'].visible)
        self.assertTrue(controls['confirm'].enabled)
        self.assertEqual(before, records(root), 'selection preview does not persist')
        await self.driver.click_element(controls['confirm'])
        saved = service.plan_store.load(plan.task_id)
        self.assertTrue(self.view._official_special_picker.value)
        decision = service.decision_state_store.load(plan.task_id).provider_special_decisions[key]
        self.assertEqual('confirmed_special_correspondence', decision['action'])
        self.assertEqual('6.5', decision['bangumi_reference']['row']['source_number'])
        for name in ('exceptional', 'subtitle', 'audio'):
            entry = next(e for e in saved.entries if e.source_path == extras[name])
            self.assertEqual((0, 7), (entry.season_id, entry.episode_id))
        await self.driver.click('特别篇 / OVA')
        controls = self.view._official_special_controls
        with self.client:
            controls['files'].set_value(key)
        await self.driver.click_element(controls['clear'])
        self.assertNotIn(key, service.decision_state_store.load(plan.task_id).provider_special_decisions)
        self.assertNotIn(extras['exceptional'], service.plan_store.load(plan.task_id).ignored_sources)

    async def test_descriptive_title_and_path_visible_editable_and_persisted(self):
        root, service, plan, extras, key = await self.prepared()
        await self.driver.click('特别篇 / OVA')
        controls = self.view._official_special_controls
        with self.client:
            controls['files'].set_value(key)
            controls['mode'].set_value('descriptive')
            controls['title'].set_value('我的特别篇名称')
        self.assertIn('Season 00', controls['preview'].text)
        self.assertIn('我的特别篇名称', controls['preview'].text)
        await self.driver.click_element(controls['confirm'])
        saved = service.plan_store.load(plan.task_id)
        video = next(e for e in saved.entries if e.source_path == extras['exceptional'])
        self.assertIsNone(video.episode_id)
        self.assertTrue(video.target_path.stem.endswith('我的特别篇名称'))
        self.assertEqual('user', service.decision_state_store.load(plan.task_id).provider_special_decisions[key]['title_source'])
        await self.driver.click('特别篇 / OVA')
        with self.client:
            self.view._official_special_controls['files'].set_value(key)
        self.assertEqual('我的特别篇名称', self.view._official_special_controls['title'].value)

    async def test_closed_popup_cannot_submit_an_old_choice(self):
        root, service, plan, extras, key = await self.prepared()
        await self.driver.click('特别篇 / OVA')
        old = self.view._official_special_controls
        with self.client:
            old['files'].set_value(key)
        closes = [e for e in self.view._official_special_picker.descendants()
                  if e._props.get('label') == '关闭']
        self.assertEqual(1, len(closes))
        await self.driver.click_element(closes[0])
        before = records(root)
        await self.driver.click_element(old['confirm'])
        self.assertEqual(before, records(root))
        self.assertFalse(service.decision_state_store.load(plan.task_id).provider_special_decisions)

    async def test_unready_picker_keeps_confirm_visible_and_disabled(self):
        from dataclasses import replace
        root, service, _, result, *_ = await self.fixture(anime=False, exceptional=True)
        metadata = dict(result.plan.metadata)
        metadata.pop('provider_special_observation', None)
        shown = replace(result.plan, metadata=metadata)
        self.create_page(prepared=(root, service, shown, []))
        # The normal page can finish its own font transaction. Begin the
        # zero-write check afterwards, with the displayed catalog still absent.
        self.view.plan = replace(self.view.plan, metadata={k: v for k, v in
            self.view.plan.metadata.items() if k != 'provider_special_observation'})
        before = records(root)
        await self.driver.click('特别篇 / OVA')
        controls = self.view._official_special_controls
        self.assertTrue(controls['confirm'].visible)
        self.assertFalse(controls['confirm'].enabled)
        self.assertTrue(controls['clear'].visible)
        self.assertFalse(controls['clear'].enabled)
        self.assertIn('bangumi', controls)
        self.assertEqual('Bangumi 编号', controls['bangumi'].label)
        self.assertEqual('TMDB 编号', controls['episode'].label)
        self.assertEqual({}, controls['bangumi'].options)
        self.assertEqual({}, controls['episode'].options)
        text = ' '.join(str(getattr(e, 'text', '')) for e in self.view._official_special_picker.descendants())
        self.assertNotIn('异常分集归属', text)
        self.assertNotIn('作为独立作品整理', text)
        with self.client:
            controls['files'].set_value(next(iter(controls['files'].options)))
        self.assertTrue(controls['confirm'].visible)
        self.assertFalse(controls['confirm'].enabled)
        with patch.object(service, 'revise_tv_content') as revise:
            await self.driver.click_element(controls['confirm'])
        revise.assert_not_called()
        self.assertEqual(before, records(root))

    async def test_no_kept_video_still_has_disabled_confirm(self):
        from dataclasses import replace
        from unittest.mock import AsyncMock
        from nicegui import ui
        from src.pages.special_crosswalk_view import render
        root, _, plan, *_ = await self.prepared()
        before = records(root)
        submit = AsyncMock()
        with self.client:
            controls = render(replace(plan, entries=()), ui_api=ui, submit=submit,
                              notify=lambda *_a, **_kw: None)
        self.assertEqual({}, controls['files'].options)
        self.assertFalse(controls['files'].enabled)
        self.assertTrue(controls['confirm'].visible)
        self.assertFalse(controls['confirm'].enabled)
        await self.driver.click_element(controls['confirm'])
        submit.assert_not_awaited()
        self.assertEqual(before, records(root))

    async def test_legacy_picker_uses_dual_page_without_error_panel_or_writes(self):
        from dataclasses import replace
        from src.rename.provider_special_evidence import freeze_provider_special_observation
        root, service, _, result, *_ = await self.fixture(anime=False, exceptional=True)
        provider = result.plan.work_identity.primary_provider
        old = freeze_provider_special_observation(provider_namespace=provider.namespace,
            provider_entity_type=provider.entity_type, provider_id=provider.provider_id,
            resolver=lambda *_: dict(episodes=[dict(id=9001, episode_number=1, name='Official')]), enabled=True)
        self.assertEqual(1, old.schema_version)
        shown = replace(result.plan, metadata={**result.plan.metadata, 'provider_special_observation':old.to_dict()})
        self.create_page(prepared=(root, service, shown, []))
        before = records(root)
        from src.pages import exceptional_episode_review
        self.assertFalse(hasattr(exceptional_episode_review, 'render_exceptional_episode_repair'))
        await self.driver.click('特别篇 / OVA')
        controls = self.view._official_special_controls
        self.assertEqual('Bangumi 编号', controls['bangumi'].label)
        self.assertEqual('TMDB 编号', controls['episode'].label)
        self.assertEqual({}, controls['bangumi'].options)
        self.assertEqual({}, controls['episode'].options)
        self.assertFalse(controls['confirm'].enabled)
        self.assertTrue(controls['confirm'].visible)
        text = ' '.join(str(getattr(e,'text','')) for e in self.view._official_special_picker.descendants())
        self.assertNotIn('异常分集归属', text)
        self.assertNotIn('作为独立作品整理', text)
        self.assertIn('获取双侧特别篇资料', text)
        self.assertEqual(before, records(root))
