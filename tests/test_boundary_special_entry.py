"""Current native boundary preserves official SP observations without inventing a crosswalk."""
import tempfile, unittest
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from unittest.mock import patch
from src.rename.provider_search import BangumiProviderClient
from src.rename.plan_store import PlanStore
from src.rename.special_catalog import read_source, build_observation, from_mapping, suggestions
from tests import test_default_cohort_completion_contract as base

class BoundarySpecialEntry(unittest.IsolatedAsyncioTestCase):
    def assert_native_observation(self, value):
        self.assertIsInstance(value, dict, 'accepted native boundary Plan discarded official BGM specials')
        self.assertEqual(2, value['schema_version'])
        self.assertEqual(1, len(value['catalog']))
        source = value['catalog'][0]
        self.assertEqual(dict(namespace='bangumi', entity_type='subject', provider_id=base.MEMBERS[0]), source['provider'])
        self.assertTrue(source['complete'])
        self.assertEqual([('442284', '6.5', 'Official half'), ('510001', '25', 'Official extra')],
            [(row['provider_episode_id'], row['source_number'], row['title']) for row in source['rows']])
        self.assertIsNone(value['relation'])
        self.assertEqual({}, suggestions(from_mapping(value)))

    async def complete(self,kind="confirm_cohort", *, no_automatic_client=False):
        fixture=base.DefaultCohortCompletionContracts('runTest')
        self.addCleanup(fixture.doCleanups)
        original=BangumiProviderClient._get_json
        def get(client,endpoint):
            if endpoint.startswith('/v0/episodes'):
                sid=parse_qs(urlsplit(endpoint).query).get('subject_id',[base.MEMBERS[0]])[0]
                return dict(total=2,offset=0,data=[dict(id=442284,subject_id=int(sid),type=1,sort=6.5,name='Official half'),dict(id=510001,subject_id=int(sid),type=1,sort=25,name='Official extra')])
            return original(client,endpoint)
        with tempfile.TemporaryDirectory() as directory:
            async with base.IncompleteMetadataFlow(Path(directory)) as runtime:
                saved_client = runtime.renamer.bangumi_search
                if no_automatic_client:
                    runtime.renamer.bangumi_search = None
                with patch.object(BangumiProviderClient,'_get_json',get), (
                    patch('src.rename.process.BangumiProviderClient', return_value=saved_client)
                    if no_automatic_client else nullcontext()
                ):
                    # Same transport, existing reader: distinguishes missing
                    # boundary wiring from malformed/unreadable API data.
                    provider = dict(namespace='bangumi', entity_type='subject', provider_id=base.MEMBERS[0])
                    observed = build_observation(provider, [read_source(provider,
                        client=BangumiProviderClient(token='', user_agent='boundary-fixture'))])
                    self.assert_native_observation(observed.to_dict())
                    result,_=await fixture.pending(runtime,base.MEMBERS[0])
                service=runtime.review
                service.root_snapshot_source=runtime.manager.root_snapshot_source
                with patch.object(BangumiProviderClient,'_get_json',side_effect=AssertionError('boundary commit must stay offline')):
                    intent=service.prepare_tv_boundary_completion(result.task_id,PlanStore.fingerprint(result.plan),review_command=fixture.command(result,kind))
                    updated=service.commit_tv_boundary_completion(intent)
                final=runtime.result(result.task_id)
                self.assertTrue(final.state.confirmed_work)
                self.assertIsNone(final.state.pending_tv_selection)
                self.assertFalse(service.load(updated.task_id).approved_entry_ids)
                runtime.assert_sources_unchanged(self)
                return updated.to_dict()['metadata']['tv_completion_special']
    async def test_boundary_existing_confirmation_control(self):
        await self.complete()
    async def test_native_boundary_keeps_bangumi_raw_without_inventing_tmdb(self):
        value=await self.complete()
        self.assert_native_observation(value)

    async def test_explicit_subject_still_reads_specials_without_automatic_search_client(self):
        self.assert_native_observation(await self.complete(no_automatic_client=True))
