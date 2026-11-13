import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from src.rename.season_reference import SeasonReferences, _reference, match_component, tmdb_snapshot
from src.rename.season_reference_record import start, record_path, from_observation
from tests.test_season_reference import evidence


class ReferenceRecord(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.provider=dict(namespace='tmdb',entity_type='tv',provider_id='50')

    def relation(self):
        subjects,relations,info=evidence()
        snapshot=tmdb_snapshot(info)
        return _reference(subjects,relations,snapshot,match_component(subjects,relations,snapshot))

    async def test_special_lookup_failure_is_not_retried_by_reference_widgets(self):
        from src.rename.special_catalog import read_source, build_observation
        from src.rename.season_reference_record import save
        from types import SimpleNamespace
        source=read_source(self.provider,client=SimpleNamespace(get_season_info=lambda *_:dict(episodes=[])))
        loader=Mock(side_effect=AssertionError('already attempted'))
        for status in ('unavailable','incomplete','ambiguous','not_found'):
            observed=build_observation(self.provider,[source],relation_reason=status).to_dict()
            save(record_path(self.root/'tasks',status,self.provider),self.provider,SeasonReferences(status=status))
            result=await start(self.root/'tasks',status,self.provider,loader,observation=observed)
            self.assertEqual(status,result.status)
        loader.assert_not_called()

    async def test_concurrent_controls_and_restart_read_disk_without_provider(self):
        release=threading.Event()
        loader=Mock(side_effect=lambda _p:(release.wait(2),self.relation())[1])
        first=start(self.root/'tasks','t',self.provider,loader)
        second=start(self.root/'tasks','t',self.provider,loader)
        self.assertIs(first,second)
        release.set();result=await first
        await asyncio.sleep(0)
        forbidden=Mock(side_effect=AssertionError('provider must not be revisited'))
        again=await start(self.root/'tasks','t',self.provider,forbidden)
        self.assertEqual(result.names_by_season(),again.names_by_season())
        self.assertEqual(1,loader.call_count);forbidden.assert_not_called()
        # The saved record alone is sufficient for a fresh process/owner.
        from src.rename.season_reference_record import read
        self.assertEqual(result.names_by_season(),read(record_path(self.root/'tasks','t',self.provider),self.provider).names_by_season())

    async def test_existing_relation_skips_lookup_and_does_not_touch_authority(self):
        reference=self.relation()
        authority=self.root/'plan.json';authority.write_text('{"revision":1}',encoding='utf-8')
        loader=Mock(side_effect=AssertionError('already observed'))
        result=await start(self.root/'tasks','t',self.provider,loader,observation=dict(relation=reference.frozen_relation()))
        self.assertEqual(reference.names_by_season(),result.names_by_season())
        loader.assert_not_called();self.assertEqual('{"revision":1}',authority.read_text('utf-8'))

    async def test_failure_saved_until_explicit_retry_and_identity_isolation(self):
        loader=Mock(side_effect=TimeoutError('fixture timeout'))
        result=await start(self.root/'tasks','t',self.provider,loader)
        self.assertEqual('unavailable',result.status)
        await asyncio.sleep(0)
        self.assertEqual('unavailable',(await start(self.root/'tasks','t',self.provider,loader)).status)
        self.assertEqual(1,loader.call_count)
        await asyncio.sleep(0)
        retry=Mock(return_value=self.relation())
        self.assertEqual('found',(await start(self.root/'tasks','t',self.provider,retry,retry=True)).status)
        other=dict(namespace='tmdb',entity_type='tv',provider_id='51')
        self.assertIsNone(from_observation(other,dict(relation=self.relation().frozen_relation())))
        self.assertNotEqual(record_path(self.root/'tasks','t',self.provider),record_path(self.root/'tasks','t',other))

    async def test_corrupt_record_is_not_absence_and_does_not_loop(self):
        path=record_path(self.root/'tasks','t',self.provider)
        path.parent.mkdir();path.write_text('{bad',encoding='utf-8')
        loader=Mock(side_effect=AssertionError('no auto retry'))
        self.assertEqual('unavailable',(await start(self.root/'tasks','t',self.provider,loader)).status)
        loader.assert_not_called()

    async def test_parseable_but_malformed_rows_offer_failure_without_fetching(self):
        from src.rename.season_reference_record import POLICY
        path=record_path(self.root/'tasks','t',self.provider);path.parent.mkdir()
        loader=Mock(side_effect=AssertionError('no auto retry'))
        for subjects,assignments in (([{}],[]),([dict(provider_id='100',title=None)],[]),
                ([dict(provider_id='100',title='Example')],[['100',{}]])):
            with self.subTest(subjects=subjects,assignments=assignments):
                path.write_text(json.dumps(dict(policy=POLICY,provider=self.provider,
                    result=dict(subjects=subjects,assignments=assignments,status='not_found',relation_json=None))),encoding='utf-8')
                result=await start(self.root/'tasks','t',self.provider,loader)
                self.assertEqual('unavailable',result.status)
                self.assertEqual({},result.names_by_season())
                await asyncio.sleep(0)
        loader.assert_not_called()
