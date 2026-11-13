"""Local routing never becomes a cross-task fact or rewrites historical proof."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from src.rename import series_registration as registration
from src.rename import successor_authority as cohort
from src.rename.provider_link_resolver import ProviderLinkResolution
from src.rename.rename_plan import ProviderIdentity, WorkIdentity
from src.rename.work_affiliation import WorkAffiliation


def assert_bound_videos(case, plan, state, root, season):
    """Metadata-only affiliation is not successful file routing."""
    videos = [entry for entry in plan.entries if entry.media_kind == 'video']
    case.assertTrue(videos)
    for entry in videos:
        case.assertTrue(entry.target_path.is_relative_to(Path(root) / f'Season {season:02d}'), entry.target_path)
    case.assertNotIn('pending_work_affiliation', plan.metadata)
    proof = registration.validate_plan_series_registration(plan, state)
    case.assertTrue(proof.affiliation)
    case.assertEqual(str(root), json.loads(proof.affiliation)['canonical_root'])


class RegisteredLinkContracts(unittest.TestCase):
    def fixture(self, root):
        from tests.test_r24_err048_historical_series_contract import _historical, _mapping, MEMBERS
        from src.rename.provider_link_catalog import match_mapping_provider
        from src.rename.provider_link_resolver import _l0_resolution
        from src.rename.catalog_namespace import NamespaceSnapshot
        from src.rename.tv_completion_observations import ProviderCatalogSnapshot
        from src.rename.target_routing import TargetRootSnapshot
        history = _historical(root)
        path = history['catalog']._path(history['key'])
        namespace = NamespaceSnapshot(str(path.parent), ((path.name, path.read_bytes()),), 'series')
        mapping = _mapping(history['key'])
        provider = ProviderIdentity('confirmed', 'bangumi', 'subject', MEMBERS[1], 'New title', 2020, 'explicit_user')
        work = WorkIdentity('confirmed', 'animation', 'New title', 2020, provider, 'provider_selection')
        resolution = _l0_resolution(match_mapping_provider(mapping, provider.to_dict()))
        providers = ProviderCatalogSnapshot(root/'links', ((mapping.mapping_id+'.json', json.dumps(mapping.to_dict()).encode()),))
        roots = TargetRootSnapshot(root/'TV', root/'Movies', root/'Anime', root/'AnimeMovies')
        return work, namespace, roots, resolution, providers, history

    def test_confirmed_link_resolves_registered_root_before_pending(self):
        from types import SimpleNamespace
        from src.rename.tv_completion_observations import FrozenTVResolution
        from src.rename.work_affiliation import resolve_record, pending_for
        with tempfile.TemporaryDirectory() as directory:
            work, ns, roots, resolution, providers, history = self.fixture(Path(directory))
            record, _, _ = resolve_record(work, ns, roots, resolution=resolution, provider_namespace=providers)
            self.assertEqual(history['key'], record['series_key'])
            inputs = SimpleNamespace(namespace=ns, roots=roots, resolution=FrozenTVResolution.capture(resolution),
                provider_namespace=providers, affiliation_choice=None, previous_affiliation='')
            self.assertIsNone(pending_for(work, inputs, {}))
            for path, before in history['protected'].items():
                self.assertEqual(before, path.read_bytes())

    def test_changed_or_missing_confirmed_link_never_becomes_new_work(self):
        from src.rename.tv_completion_observations import ProviderCatalogSnapshot
        from src.rename.work_affiliation import resolve_record, AffiliationChoice, namespace_fingerprint
        with tempfile.TemporaryDirectory() as directory:
            work, ns, roots, resolution, providers, _ = self.fixture(Path(directory))
            for label, changed, observed, choice in (
                ('revision', replace(resolution, anchor_revision=resolution.anchor_revision+1), providers, None),
                ('fingerprint', replace(resolution, anchor_fingerprint='f'*64), providers, None),
                ('key', replace(resolution, series_key='series-00000000-0000-0000-0000-000000000001'), providers, None),
                ('missing', resolution, ProviderCatalogSnapshot(providers.root, ()), None),
                ('separate', resolution, providers, AffiliationChoice('separate', '', namespace_fingerprint(ns), 'attempt duplicate')),
            ):
                with self.subTest(label=label), self.assertRaises(ValueError):
                    resolve_record(work, ns, roots, resolution=changed, provider_namespace=observed, choice=choice)

    def test_link_requires_member_registered_record_and_consistent_exact_identity(self):
        from src.rename.catalog_namespace import NamespaceSnapshot
        from src.rename.work_affiliation import resolve_record
        with tempfile.TemporaryDirectory() as directory:
            work, ns, roots, resolution, providers, _ = self.fixture(Path(directory))
            outsider = replace(work, primary_provider=replace(work.primary_provider, provider_id='999999'))
            with self.assertRaises(ValueError):
                resolve_record(outsider, ns, roots, resolution=resolution, provider_namespace=providers)
            with self.assertRaises(ValueError):
                resolve_record(work, replace(ns, records=()), roots, resolution=resolution, provider_namespace=providers)
            # The selected Bangumi id independently belongs to another old record.
            other = deepcopy(json.loads(ns.records[0][1]))
            other.update(series_key='series-00000000-0000-0000-0000-000000000002',
                canonical_root=str(roots.anime_path/'Other work'), series_registration_authority=None,
                provider_evidence=[work.primary_provider.to_dict()], season_bindings=[])
            conflict = NamespaceSnapshot(ns.root, tuple(sorted((*ns.records,
                (other['series_key']+'.json', json.dumps(other).encode())))), 'series')
            with self.assertRaises(ValueError):
                resolve_record(work, conflict, roots, resolution=resolution, provider_namespace=providers)


class MalformedLinkContracts(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_snapshot_is_a_typed_rejection_without_writes(self):
        from tests.test_r24_err040_execution_root_contract import ExecutionRootContract
        from src.rename.plan_store import PlanStore
        with tempfile.TemporaryDirectory() as directory:
            helper = ExecutionRootContract('runTest')
            result, plan, _service, _manager, _source, _before = await helper.prepare(Path(directory))
            state = result.state
            # prepare also completes fonts and approves; use its current state.
            from src.rename.decision_state import DecisionStateStore
            state = DecisionStateStore(result.decisions).load(plan.task_id)
            registration.validate_plan_series_registration(plan, state)
            plan_path = PlanStore(result.plans).path_for(plan.task_id)
            before = plan_path.read_bytes()
            key = plan.metadata['series_key']
            for value in ('invalid', {}, {'series_key': key, 'season_assignments': None},
                          {'series_key': key, 'season_assignments': [None]},
                    {'series_key': key, 'season_assignments': [{}]},
                    {'series_key': key, 'season_assignments': [
                        {'bangumi_subject_id': '999999', 'season_number': 1}]}):
                with self.subTest(snapshot=value), self.assertRaises(ValueError):
                    registration.validate_plan_series_registration(replace(plan,
                        metadata={**plan.metadata, 'provider_link_snapshot': value}))
            self.assertEqual(before, plan_path.read_bytes())
            self.assertFalse(any(entry.target_path.exists() for entry in plan.entries))


def affiliated(root, *, existing=False):
    resolution = ProviderLinkResolution(automation_level='L1', status='inferred',
        tmdb_tv_id='53918', season_assignments=(('41658', 1), ('86670', 2)),
        requires_plan_review=True, evidence_path=('official subject relationships',),
        work_segment={'subject_ids': ['41658', '86670']})
    original = registration.build_series_registration_authority(resolution)
    provider = ProviderIdentity('confirmed', 'bangumi', 'subject', '41658',
        '向山进发', 2013, 'explicit_user')
    work = WorkIdentity('confirmed', 'animation', '向山进发', 2013, provider, 'provider_selection')
    record = dict(series_key=original.series_key, revision=3,
        canonical_root=str(root), season_bindings=[]) if existing else None
    authority = WorkAffiliation.from_mapping(dict(schema_version=1, policy_version='work-affiliation-v1',
        action='existing' if existing else 'separate', source='catalog_exact' if existing else 'new_work',
        backend='series', key=original.series_key, domain='animation', group_kind='tv_episodes',
        provider=dict(namespace='bangumi', entity_type='subject', provider_id='41658'),
        work_members=[['bangumi', 'subject', '41658'], ['bangumi', 'subject', '86670']],
        canonical_root=str(root), title='向山进发', year=2013,
        catalog_revision=3 if existing else 0,
        record_fingerprint=registration._digest(record) if existing else None,
        origin=dict(task_id='isolated-proof', plan='1'*64, decision='2'*64, task='3'*64),
        reason='isolated typed proof control'))
    proof = registration.affiliated_registration(work, authority, resolution=resolution, record=record)
    return replace(resolution, registration_authority=proof.to_dict(), proposed_series_key=proof.series_key), proof


class RoutingProjectionContracts(unittest.TestCase):
    def test_two_local_roots_publish_the_same_official_relation(self):
        with tempfile.TemporaryDirectory() as directory:
            values = []
            for name in ('first-library', 'second-library'):
                resolution, local = affiliated(Path(directory) / name, existing=True)
                frozen = local.to_dict()
                projected = cohort._resolution_payload(resolution)
                cohort._reject_target_routing(projected)
                proof = registration.SeriesRegistrationAuthority.from_mapping(projected['registration_authority'])
                self.assertFalse(proof.affiliation)
                self.assertFalse(proof.materialized_mapping)
                self.assertEqual(proof.series_key, projected['proposed_series_key'])
                self.assertEqual(frozen, local.to_dict(), 'local execution authority is not rewritten')
                values.append(projected)
            self.assertEqual(values[0], values[1])

    def test_unrelated_routing_payload_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            resolution, _ = affiliated(Path(directory) / 'series')
            resolution = replace(resolution, work_segment={**resolution.work_segment,
                'canonical_root': str(Path(directory) / 'injected')})
            with self.assertRaisesRegex(cohort.SuccessorAuthorityError, 'authority_contains_target_routing'):
                cohort._reject_target_routing(cohort._resolution_payload(resolution))

    def test_relocated_wrapper_preserves_existing_materialized_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, original = affiliated(root / 'old', existing=True)
            origin = original.to_dict()
            body = dict(schema_version=3, policy_version='series-registration-relocated-v1',
                original_registration=origin,
                relocation=dict(from_root=str(root/'old'), canonical_root=str(root/'new'),
                    prior_catalog_revision=4, prior_catalog_fingerprint='4'*64))
            raw = dict(body, fingerprint=registration._digest(body))
            current = registration.SeriesRegistrationAuthority.from_mapping(raw)
            self.assertEqual(raw, current.to_dict())
            self.assertEqual(origin, current.to_dict()['original_registration'])
            self.assertEqual(str(root/'old'), json.loads(current.materialized_mapping)['canonical_root'])
            self.assertEqual(str(root/'new'), registration.registration_current_root(current))
            self.assertEqual(str(root/'old'), registration.registration_current_root(original))
            prior = dict(series_key=original.series_key, revision=5, canonical_root=str(root/'new'),
                series_registration_authority=current.to_dict())
            again = registration.relocated_registration(current, prior, root/'third')
            self.assertEqual(origin, again.to_dict()['original_registration'])
            self.assertEqual(str(root/'new'), again.to_dict()['relocation']['from_root'])
            self.assertEqual(registration._digest(prior), again.to_dict()['relocation']['prior_catalog_fingerprint'])
            self.assertEqual(str(root/'third'), registration.registration_current_root(again))
            with self.assertRaises(ValueError):
                registration.relocated_registration(current, {**prior, 'canonical_root': str(root/'wrong')}, root/'third')
            for mutation in ('same_root', 'digest', 'nested', 'unknown'):
                with self.subTest(mutation=mutation):
                    bad = deepcopy(body)
                    if mutation == 'same_root': bad['relocation']['canonical_root'] = bad['relocation']['from_root']
                    elif mutation == 'digest': bad['relocation']['prior_catalog_fingerprint'] = 'invalid'
                    elif mutation == 'nested': bad['original_registration'] = raw
                    else: bad['relocation']['unchecked_root'] = str(root/'other')
                    bad['fingerprint'] = registration._digest(bad)
                    with self.assertRaises(ValueError):
                        registration.SeriesRegistrationAuthority.from_mapping(bad)
