"""Draft incident tests; copied into tests only after PHASE0 closes."""
import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from tests import _media_flow_helpers as tv
from tests import test_cross_provider_work_correction as cross
from src.rename.plan_review import PlanReviewService
from src.rename.plan_store import PlanStore
from src.rename.asset_actions import AssetActionService
from src.rename.asset_replacement import ApprovedReplacementBatch, observe_replacement, observe_retirement
from src.rename.decision_state import DecisionStateStore
from src.rename.series_catalog import SeriesCatalog
from src.rename.work_catalog import WorkCatalog
from src.rename.trans import Trans


class WholeSeason(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.h=tv.MediaFlow('runTest')

    async def incoming(self, name='incoming'):
        source,font=tv.make_season(self.root/name);font.unlink()
        result=await self.h._run_flow(root=self.root,source=source,task_id=name,subject_id='41658')
        self.review=PlanReviewService(PlanStore(self.root/'plans'),self.root/'reviews',decision_state_root=self.root/'decisions',task_root=self.root/'tasks',catalog_root=self.root/'work_catalog',record_root=self.root/'records',provider_link_catalog_root=self.root/'provider_links')
        return result

    async def test_unregistered_whole_directory_preview_and_cancel_are_read_only(self):
        result=await self.incoming();plan=result.plan
        target=next(e.target_path for e in plan.entries if e.media_kind=='video')
        target.parent.mkdir(parents=True);target.write_bytes(b'old-video')
        extra=target.parent/'Scans'/'old.txt';extra.parent.mkdir();extra.write_bytes(b'old-scan')
        untouched=target.parent.parent/'Season 02'/'keep.mkv';untouched.parent.mkdir();untouched.write_bytes(b'other-season')
        try: op,_=self.review.asset_actions().prepare_replacement(plan)
        except ValueError as exc:self.fail(f'new task must be able to preview adopted season: {exc}')
        value=op.to_dict()
        self.assertEqual('asset-operation-v3',value['policy'])
        self.assertIn(str(extra),{r['target'] for r in value['retirements']})
        self.assertNotIn(str(untouched),{r['target'] for r in value['retirements']})
        self.assertEqual(b'old-video',target.read_bytes());self.assertEqual(b'old-scan',extra.read_bytes())
        self.assertFalse(list((self.root/'records').glob('*.ledger.json')))

    async def test_new_empty_season_needs_no_replacement(self):
        result=await self.incoming()
        # Normal new plans remain ordinary unapproved plans; no target created.
        self.assertFalse(any(e.target_path.exists() for e in result.plan.entries))
        self.assertNotEqual('replace',(result.plan.metadata.get('asset_operation') or {}).get('mode'))

    async def test_frozen_directory_detects_added_file_and_physical_directory_swap(self):
        from src.rename.season_replacement import validate_directory
        result=await self.incoming()
        target=next(e.target_path for e in result.plan.entries if e.media_kind=='video')
        target.parent.mkdir(parents=True);target.write_bytes(b'old')
        op,_=self.review.asset_actions().prepare_replacement(result.plan)
        scope=op.to_dict()['season_directory']
        extra=target.parent/'extra.mkv';extra.write_bytes(b'outside approval')
        with self.assertRaisesRegex(ValueError,'变化'):validate_directory(scope)
        extra.unlink()
        target.parent.rename(target.parent.with_name('old-season'))
        target.parent.mkdir()
        with self.assertRaisesRegex(ValueError,'身份'):validate_directory(scope)

    async def test_preview_does_not_read_media_content_or_change_files(self):
        result=await self.incoming()
        target=next(e.target_path for e in result.plan.entries if e.media_kind=='video')
        target.parent.mkdir(parents=True);target.write_bytes(b'old')
        original=Path.open
        def open_file(path,*args,**kwargs):
            if path.suffix.lower() in {'.mkv','.mka','.flac','.mp4'}:
                raise AssertionError('preview read media bytes')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',open_file):
            op,_=self.review.asset_actions().prepare_replacement(result.plan)
        self.assertEqual('asset-operation-v3',op.to_dict()['policy'])
        self.assertEqual(b'old',target.read_bytes())

    async def test_different_device_is_rejected_even_when_every_incoming_name_is_new(self):
        result=await self.incoming()
        target=next(e.target_path for e in result.plan.entries if e.media_kind=='video')
        target.parent.mkdir(parents=True)
        old=target.parent/'old-name.mkv';old.write_bytes(b'old')
        source=next(e.source_path for e in result.plan.entries if e.target_path==target)
        original=Path.stat
        def stat(path,*args,**kwargs):
            value=original(path,*args,**kwargs)
            if path==source:
                from types import SimpleNamespace
                values={key:getattr(value,key) for key in dir(value) if key.startswith('st_')}
                values['st_dev']+=1
                return SimpleNamespace(**values)
            return value
        with patch.object(Path,'stat',stat):
            with self.assertRaisesRegex(ValueError,'同一文件系统'):
                self.review.asset_actions().prepare_replacement(result.plan)
        self.assertEqual(b'old',old.read_bytes());self.assertFalse(target.exists())

    async def test_frozen_scope_cannot_drop_retirement_or_adopt_source_directory(self):
        import copy
        from src.rename.asset_operation import AssetOperation
        result=await self.incoming()
        target=next(e.target_path for e in result.plan.entries if e.media_kind=='video')
        target.parent.mkdir(parents=True)
        old=target.parent/'S01E12.mkv';old.write_bytes(b'old last episode')
        operation,_=self.review.asset_actions().prepare_replacement(result.plan)
        value=operation.to_dict()
        self.assertIn(str(old),{r['target'] for r in value['retirements']})
        broken=copy.deepcopy(value);broken['retirements']=[]
        with self.assertRaisesRegex(ValueError,'闭包'):AssetOperation.from_mapping(broken)
        broken=copy.deepcopy(value)
        broken['season_directory']['incoming'][0]['source']=str(old)
        with self.assertRaisesRegex(ValueError,'来源'):AssetOperation.from_mapping(broken)
        broken=copy.deepcopy(value)
        broken['season_directory']['root']=str(target.parent.parent)
        with self.assertRaisesRegex(ValueError,'季目录'):AssetOperation.from_mapping(broken)


class PreparationOrder(unittest.TestCase):
    def test_failed_new_link_is_detected_before_retiring_old_content(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);library=root/'library';library.mkdir()
            source=root/'new.mkv';source.write_bytes(b'new')
            old=library/'old-extra.mkv';old.write_bytes(b'old')
            target=library/'new.mkv'
            cap=ApprovedReplacementBatch('prepare-order','a'*64,(),(observe_retirement(old,library),),str(library),(str(library),))
            trans=Trans({source:target},'prepare-order','a'*64,mode='hardlink',dry_run=False,record_root=root/'records',operations=[dict(source=str(source),target=str(target),action='create')],replacement_authority=cap)
            original=os.link;observed=[]
            def link(src,dst,*args,**kwargs):
                if Path(src)==source:
                    observed.append(old.exists())
                    raise OSError('fixture staging disk failure')
                return original(src,dst,*args,**kwargs)
            with patch('src.rename.trans.os.link',side_effect=link):
                error=trans._trans_replacements()
            self.assertTrue(error)
            self.assertEqual([True],observed,'old season already retired before incoming could be prepared')
            self.assertEqual(b'old',old.read_bytes());self.assertEqual(b'new',source.read_bytes())


class ReferenceReuse(unittest.IsolatedAsyncioTestCase):
    fixture=cross.CrossProviderWorkCorrection.fixture
    original=cross.CrossProviderWorkCorrection.original
    create_page=cross.CrossProviderWorkCorrection.create_page

    async def test_opening_season_twice_reuses_frozen_reference_without_requests(self):
        from src.rename.season_reference import SeasonReferences
        root,service,_,result,*_=await self.original(tmdb=True)
        with patch('src.pages.season_reference_view.load',return_value=SeasonReferences()) as loader:
            self.create_page(prepared=(root,service,result.plan,[]))
            for _ in range(2):
                await self.driver.click('更改季度')
                await self.view.general_season_choice.reference_task
            self.assertEqual(0,loader.call_count,'complete frozen reference must not refetch')

    async def test_reference_prepared_before_season_selector(self):
        from src.rename.season_reference import SeasonReferences
        root,service,_,result,*_=await self.original(tmdb=True)
        started=threading.Event()
        def read(_provider):
            started.set()
            return SeasonReferences()
        # A legacy/missing reference projection must be obtained before opening
        # the selector. The ordinary complete-seed path is covered above.
        with patch('src.pages.season_reference_view.load',side_effect=read) as loader, \
                patch('src.rename.season_reference_record.from_observation',return_value=None):
            self.create_page(prepared=(root,service,result.plan,[]))
            self.assertTrue(await asyncio.to_thread(started.wait, 2),
                'provider request must start before any season-selector click')
            self.assertEqual(1,loader.call_count)
            await self.driver.click('更改季度')
            await self.view.general_season_choice.reference_task
            self.assertEqual(1,loader.call_count,'selector must reuse the already prepared reference')


class ConfirmationView(unittest.IsolatedAsyncioTestCase):
    fixture=cross.CrossProviderWorkCorrection.fixture
    original=cross.CrossProviderWorkCorrection.original
    create_page=cross.CrossProviderWorkCorrection.create_page

    async def test_adopted_directory_confirmation_lists_old_paths_and_cancel_is_read_only(self):
        from types import SimpleNamespace
        from tests.test_content_choice_contract import records
        from src.rename.season_reference import SeasonReferences
        root,service,_,result,*_=await self.original(tmdb=True)
        with patch('src.pages.season_reference_view.load',return_value=SeasonReferences()):
            self.create_page(prepared=(root,service,result.plan,[]))
            target=next(e.target_path for e in result.plan.entries if e.media_kind=='video')
            old=target.parent/'Scans'/'old.txt'
            old_cd=target.parent/'CDs'/'old.flac'
            # Isolate the real confirmation renderer from the as-yet absent v3 producer.
            value=dict(policy='asset-operation-v3',mode='replace',previous_assets=[],replaces=[],
                retirements=[dict(target=str(old)),dict(target=str(old_cd))],season_directory=dict(root=str(target.parent),
                    files=[dict(target=str(p),disposition='retire') for p in (old,old_cd)]))
            operation=SimpleNamespace(to_dict=lambda:value,fingerprint='a'*64)
            existing_ids=set(self.client.elements)
            with patch.object(AssetActionService,'prepare_replacement',return_value=(operation,result.plan)):
                with self.client:await self.view._preview_asset_replacement()
            # Only the new confirmation surface counts, never the underlying plan table.
            labels=[str(getattr(e,'text','')) for key,e in self.client.elements.items() if key not in existing_ids]
            text=' '.join(labels)
            for path in (old,old_cd):
                self.assertTrue(any(str(path) in label for label in labels),
                    f'confirmation did not disclose frozen old path: {path}; labels={labels}')
            self.assertIn(str(target.parent),text)
            self.assertNotIn('未知归属文件不会被自动删除',text)
            before=records(root)
            await self.driver.click('取消')
            self.assertEqual(before,records(root))
