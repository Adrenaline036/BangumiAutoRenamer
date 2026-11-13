"""Incident contracts for reference precedence, provider gates and SP identity."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.rename import season_reference_record as record
from src.rename.season_reference import SeasonReferences
from tests.test_season_reference_record import ReferenceRecord


class ReferenceRepair(unittest.IsolatedAsyncioTestCase):
    relation = ReferenceRecord.relation

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.provider = dict(namespace='tmdb', entity_type='tv', provider_id='50')
        self.seed = dict(schema_version=2, provider_identity=self.provider, relation=None, relation_reason='not_found')

    async def test_explicit_retry_ignores_failed_seed_and_saves_loaded_result(self):
        path = record.record_path(self.root/'tasks', 't', self.provider)
        record.save(path, self.provider, SeasonReferences(status='unavailable'))
        loader = Mock(return_value=self.relation())
        result = await record.start(self.root/'tasks', 't', self.provider, loader, observation=self.seed, retry=True)
        self.assertEqual('found', result.status)
        loader.assert_called_once_with(self.provider)
        self.assertEqual(result, record.read(path, self.provider))

    async def test_old_status_seed_never_overwrites_saved_subjects_or_relation(self):
        loader = Mock(side_effect=AssertionError('no repeated lookup'))
        for saved in (self.relation(), SeasonReferences((dict(provider_id='100', title='独立季度'),), status='not_found')):
            with self.subTest(status=saved.status):
                path = record.record_path(self.root/'tasks', saved.status, self.provider)
                record.save(path, self.provider, saved)
                result = await record.start(self.root/'tasks', saved.status, self.provider, loader, observation=self.seed)
                self.assertEqual(saved, result)
                self.assertEqual(saved, record.read(path, self.provider))
        loader.assert_not_called()

    async def test_status_only_seed_fetches_once_and_then_failure_is_cached(self):
        loader = Mock(return_value=SeasonReferences((dict(provider_id='100', title='已读取季度'),), status='incomplete'))
        first = await record.start(self.root/'tasks', 't', self.provider, loader, observation=self.seed)
        await asyncio.sleep(0)
        second = await record.start(self.root/'tasks', 't', self.provider, loader, observation=self.seed)
        self.assertEqual(1, loader.call_count)
        self.assertEqual(first, second)
        self.assertEqual(1, len(second.subjects))

    async def test_optional_save_failure_retains_successful_result(self):
        expected = self.relation()
        with patch.object(record, 'save', side_effect=OSError('read-only optional cache')):
            result = await record.start(self.root/'tasks', 't', self.provider, Mock(return_value=expected))
        self.assertEqual(expected, result)

    async def test_complete_seed_is_offline_and_does_not_change_authority(self):
        expected = self.relation()
        authority = self.root/'plan.json'; authority.write_bytes(b'unchanged authority')
        loader = Mock(side_effect=AssertionError('already complete'))
        result = await record.start(self.root/'tasks', 't', self.provider, loader,
                                    observation=dict(relation=expected.frozen_relation()))
        self.assertEqual(expected.subjects, result.subjects)
        self.assertEqual(expected.assignments, result.assignments)
        self.assertEqual(expected.frozen_relation(), result.frozen_relation())
        loader.assert_not_called()
        self.assertEqual(b'unchanged authority', authority.read_bytes())


from tests.test_unified_provider_link_contract import UnifiedProviderLinkContracts as LinkFixture
from src.pages import plan_review_page as page


class CandidateGateRepair(unittest.IsolatedAsyncioTestCase):
    flow=LinkFixture.flow
    current=LinkFixture.current
    dialog=LinkFixture.dialog
    controls=LinkFixture.controls
    callback=LinkFixture.callback

    async def test_wrong_domain_cannot_enable_dispatch_or_enter_correction(self):
        candidate=dict(namespace='bangumi', entity_type='subject', provider_id='5252',
            title='动画条目', platform='TV', library_domain='animation', source_url='https://bgm.tv/subject/5252')
        async with self.dialog() as (_, dialog):
            with dialog.client:
                before=set(dialog.client.elements)
                dialog._render_provider_candidate(candidate)
            buttons=[e for key,e in dialog.client.elements.items() if key not in before
                     and e._props.get('label')=='选择这个作品']
            self.assertEqual(1, len(buttons))
            with self.subTest(surface='button'):
                self.assertFalse(buttons[0].enabled)
            fields, buttons=self.controls(dialog)
            fields[0].value=candidate['source_url']
            with dialog.client, patch.object(page,'_read_manual_provider_url',return_value=candidate), \
                    patch.object(page,'notify'), patch.object(dialog,'_select_provider_candidate',new_callable=AsyncMock) as selected:
                await self.callback(buttons[0])()
            with self.subTest(surface='link_callback'):
                selected.assert_not_awaited()
            with dialog.client, patch('src.pages.work_correction_view.show',new_callable=AsyncMock) as show, patch.object(page,'notify'):
                await page.PlanReviewDialog._select_provider_candidate(dialog,candidate)
            with self.subTest(surface='direct_dispatch'):
                show.assert_not_awaited()

    async def test_valid_same_domain_candidate_keeps_existing_manual_correction(self):
        from tests.test_general_work_correction_contract import TARGET
        async with self.dialog() as (_, dialog):
            with dialog.client, patch('src.pages.work_correction_view.show',new_callable=AsyncMock) as show:
                await dialog._select_provider_candidate(TARGET)
            show.assert_awaited_once()


from tests import test_complex_library_special_red_contract as special


class SpecialIdentityRepair(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        old=special._SteinsSearch.get_tv_info
        def dated(search,*args,**kwargs):
            title,info=old(search,*args,**kwargs)
            if info:
                info={**info,'seasons':[{**s,**({'air_date':info['first_air_date']} if s['season_number']>0 else {})} for s in info['seasons']]}
            return title,info
        self.enterContext(patch.object(special._SteinsSearch,'get_tv_info',dated))

    async def test_bangumi_root_sg0_keeps_tmdb_special_and_subtitle(self):
        from tests.test_r22_fullset_p1_red_contract import RegularEpisodeOverflowProductionRedContracts
        await RegularEpisodeOverflowProductionRedContracts.test_real_sg0_24_and_subtitle_become_one_explicit_s00_candidate(self)

    async def test_completed_regular_does_not_reserve_special(self):
        from tests.test_err059_delivery_incidents import DeliveryIncidents
        await DeliveryIncidents.test_completed_main_does_not_reserve_disjoint_official_special(self)

# Imported fixture classes must not be collected as duplicate tests.
del ReferenceRecord, LinkFixture
