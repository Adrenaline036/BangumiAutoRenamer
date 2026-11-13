"""Series namespace requirements through actual receipted catalog producers."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename.series_catalog import SeriesCatalog, SeriesCatalogError
from tests.test_series_catalog import completed_tv_plan, canonical_series_key, tmdb_tv


def _process_lease_probe(root, key, output):
    from src.rename.catalog_namespace import require_lease
    catalog = SeriesCatalog(Path(root))
    rejected = []
    for action in ('borrow', 'lock'):
        try:
            if action == 'borrow':
                require_lease(catalog, key)
            else:
                with catalog.lock(key, wait_seconds=0.05):
                    pass
        except (ValueError, RuntimeError):
            rejected.append(action)
    output.put(rejected)


class SeriesNamespaceContracts(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        catalog = SeriesCatalog(root / 'series')
        plan = completed_tv_plan(root, task_id='namespace-series',
            series_key=canonical_series_key(tmdb_tv()), season=1, provider=tmdb_tv())
        match = catalog.register_completed(plan)
        record = json.loads(catalog._path(match.series_key).read_text('utf-8'))
        return root, catalog, plan, record

    def test_series_lease_snapshot_and_one_use_proof(self):
        from src.rename.catalog_namespace import consume
        root, catalog, plan, record = self.fixture()
        key = record['series_key']
        with catalog.lock(key) as lease:
            self.assertIsNotNone(lease, 'must use shared actual namespace lease')
            snapshot = catalog.namespace_snapshot(key)
            proof = catalog.validate_namespace_candidate(key, record)
            for fake in (None, object(), replace(proof)):
                with self.subTest(fake=type(fake).__name__):
                    with self.assertRaises((ValueError, RuntimeError)):
                        consume(catalog, key, record, fake)
            consume(catalog, key, record, proof)
            with self.assertRaises((ValueError, RuntimeError)):
                consume(catalog, key, record, proof)
            path = catalog._path(key)
            path.write_bytes(path.read_bytes() + b'\n')
            with self.assertRaises((ValueError, RuntimeError)):
                catalog.verify_namespace_snapshot(key, snapshot)
        with self.assertRaises((ValueError, RuntimeError)):
            catalog.namespace_snapshot(key)

    def test_series_lease_cannot_cross_owner_backend_or_key(self):
        from src.rename.catalog_namespace import require_lease
        from src.rename.work_catalog import WorkCatalog
        root, catalog, plan, record = self.fixture()
        key = record['series_key']
        other = SeriesCatalog(root / 'other-series')
        with catalog.lock(key):
            require_lease(catalog, key)
            for target, wanted in ((other, key), (catalog, canonical_series_key(tmdb_tv('404'))),
                                   (WorkCatalog(catalog.root), 'work-12345678-1234-1234-1234-123456789abc')):
                with self.subTest(backend=type(target).__name__, key=wanted):
                    with self.assertRaises((ValueError, RuntimeError)):
                        require_lease(target, wanted)
            with ThreadPoolExecutor(max_workers=1) as pool:
                with self.assertRaises((ValueError, RuntimeError)):
                    pool.submit(require_lease, catalog, key).result(timeout=5)

    def test_series_snapshot_requires_full_set_and_valid_provider_authority(self):
        for fault in ('created', 'invalid_json', 'provider'):
            with self.subTest(fault=fault):
                root, catalog, plan, record = self.fixture()
                key = record['series_key']
                with catalog.lock(key):
                    snapshot = catalog.namespace_snapshot(key)
                    if fault == 'created':
                        other_key = canonical_series_key(tmdb_tv('444'))
                        altered = {**record, 'series_key': other_key, 'provider_evidence': [tmdb_tv('444')]}
                        catalog._path(other_key).write_text(json.dumps(altered), encoding='utf-8')
                    elif fault == 'invalid_json':
                        catalog._path(key).write_bytes(b'{broken')
                    else:
                        altered = {**record, 'provider_evidence': [{}]}
                        catalog._path(key).write_text(json.dumps(altered), encoding='utf-8')
                    with self.assertRaises((ValueError, RuntimeError)):
                        catalog.verify_namespace_snapshot(key, snapshot)

    def test_namespace_lease_is_not_borrowed_or_reacquired_by_another_process(self):
        root, catalog, plan, record = self.fixture()
        context = multiprocessing.get_context('spawn')
        output = context.Queue()
        process = context.Process(target=_process_lease_probe,
            args=(str(catalog.root), record['series_key'], output))
        with catalog.lock(record['series_key']):
            process.start()
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join(5)
                self.fail('bounded process lease probe timed out')
            self.assertEqual(0, process.exitcode)
            self.assertEqual(['borrow', 'lock'], output.get(timeout=2))
        output.close()

    def test_idempotent_backfill_still_rejects_disconnected_namespace_guard(self):
        root, catalog, plan, record = self.fixture()
        path = catalog._path(record['series_key'])
        before = path.read_bytes()
        with patch.object(catalog, 'validate_namespace_candidate', return_value=None, create=True):
            with self.assertRaises((ValueError, RuntimeError)):
                catalog.backfill_legacy_completed(record['series_key'], ())
        self.assertEqual(before, path.read_bytes())

    def test_actual_relocation_cannot_bypass_namespace_writer(self):
        from tests.test_series_catalog import SeriesCatalogTests
        control = SeriesCatalogTests('test_relocate_series_verifies_all_ledger_hardlinks')
        original = SeriesCatalog.relocate
        reached = []
        def disconnected(catalog, *args, **kwargs):
            reached.append(True)
            owned = catalog.root.parent
            before = {str(p.relative_to(owned)): p.read_bytes() for p in owned.rglob('*.json')}
            with patch.object(catalog, 'validate_namespace_candidate', return_value=None, create=True):
                try:
                    return original(catalog, *args, **kwargs)
                finally:
                    self.assertEqual(before, {str(p.relative_to(owned)): p.read_bytes() for p in owned.rglob('*.json')})
        with patch.object(SeriesCatalog, 'relocate', new=disconnected):
            with self.assertRaises((ValueError, RuntimeError)):
                control.test_relocate_series_verifies_all_ledger_hardlinks()
        self.assertEqual([True], reached)

    def test_legacy_conflicting_registration_can_be_archived_one_at_a_time(self):
        root, catalog, plan, record = self.fixture()
        peer = SeriesCatalog(root / 'peer-series')
        other_key = canonical_series_key(tmdb_tv('445'))
        other = completed_tv_plan(root, task_id='conflict-peer', series_key=other_key,
            season=1, provider=tmdb_tv('445'))
        peer.register_completed(other)
        # Two individually valid historical records, but a shared root.
        catalog._path(other_key).write_bytes(peer._path(other_key).read_bytes())
        tasks = root / 'empty-tasks'
        tasks.mkdir()
        archived = catalog.archive_missing(other_key, task_root=tasks)
        self.assertTrue(archived.is_file())
        self.assertTrue(catalog._path(record['series_key']).is_file())
        self.assertFalse(catalog._path(other_key).exists())

    def test_shared_writer_disconnect_rejects_actual_series_mutations(self):
        for action in ('register', 'bind', 'archive', 'direct'):
            with self.subTest(action=action):
                root, catalog, plan, record = self.fixture()
                key = record['series_key']
                before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*.json')}
                next_plan = None
                if action == 'register':
                    next_plan = completed_tv_plan(root, task_id='next-season', series_key=key,
                        season=2, provider=tmdb_tv())
                    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*.json')}
                with patch.object(catalog, 'validate_namespace_candidate', return_value=None, create=True):
                    with self.assertRaises((ValueError, RuntimeError)):
                        if action == 'register':
                            catalog.register_completed(next_plan, expected_revision=1)
                        elif action == 'bind':
                            catalog.bind_season(key, expected_revision=1, season_number=2,
                                provider_identity=tmdb_tv(), decision_source='explicit_user')
                        elif action == 'archive':
                            tasks = root / 'empty-tasks'
                            tasks.mkdir()
                            catalog.archive_missing(key, task_root=tasks)
                        else:
                            catalog._atomic_write(key, {**record, 'revision': 2})
                self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*.json')})

    def test_series_authority_and_binding_cannot_diverge_from_provider_evidence(self):
        from copy import deepcopy
        for fault in ('binding_provider', 'authority', 'authority_member', 'domain', 'binding_season'):
            with self.subTest(fault=fault):
                root, catalog, plan, record = self.fixture()
                key = record['series_key']
                candidate = deepcopy(record)
                if fault == 'binding_provider':
                    candidate['season_bindings'][0]['provider_identity'] = tmdb_tv('999')
                elif fault == 'authority':
                    candidate['series_registration_authority']['fingerprint'] = '0' * 64
                elif fault == 'authority_member':
                    candidate['provider_evidence'] = [tmdb_tv('999')]
                    candidate['season_bindings'][0]['provider_identity'] = tmdb_tv('999')
                elif fault == 'domain':
                    candidate['domain'] = 'animation'
                else:
                    candidate['season_bindings'][0]['season_number'] = True
                with catalog.lock(key):
                    with self.assertRaises(SeriesCatalogError):
                        catalog.validate_namespace_candidate(key, candidate)

    def test_bangumi_season_number_must_match_typed_membership(self):
        root, _, _, _ = self.fixture()
        provider = dict(namespace='bangumi', entity_type='subject', provider_id='99888')
        key = canonical_series_key(provider)
        plan = completed_tv_plan(root / 'bangumi', task_id='bangumi-season', series_key=key,
            season=1, provider=provider)
        catalog = SeriesCatalog(root / 'bangumi' / 'series')
        catalog.register_completed(plan)
        record = json.loads(catalog._path(key).read_text('utf-8'))
        record['season_bindings'][0]['season_number'] = 2
        with catalog.lock(key):
            with self.assertRaisesRegex(SeriesCatalogError, '季度与登记权威不一致'):
                catalog.validate_namespace_candidate(key, record)

    def test_windows_canonical_root_aliases_do_not_create_distinct_namespaces(self):
        root, catalog, plan, record = self.fixture()
        peer = SeriesCatalog(root / 'peer-series')
        key = canonical_series_key(tmdb_tv('556'))
        second = completed_tv_plan(root / 'second', task_id='alias-peer', series_key=key,
            season=1, provider=tmdb_tv('556'))
        peer.register_completed(second)
        candidate = json.loads(peer._path(key).read_text('utf-8'))
        for alias in (record['canonical_root'].upper(), record['canonical_root'] + '.',
                      record['canonical_root'] + ' ', str(Path(record['canonical_root']) / '..' / '86 (2021)')):
            with self.subTest(alias_kind=alias[-10:]), catalog.lock(key):
                with self.assertRaises(SeriesCatalogError):
                    catalog.validate_namespace_candidate(key, {**candidate, 'canonical_root': alias})

    def test_different_series_cannot_register_one_canonical_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog = SeriesCatalog(root / 'series')
            plans = [completed_tv_plan(root, task_id=f'series-{i}',
                series_key=canonical_series_key(tmdb_tv(str(i))), season=1, provider=tmdb_tv(str(i)))
                for i in (100, 200)]
            self.assertEqual(plans[0].metadata['canonical_work_root'], plans[1].metadata['canonical_work_root'])
            catalog.register_completed(plans[0])
            before = {p.name: p.read_bytes() for p in catalog.root.glob('*.json')}
            with self.assertRaises(SeriesCatalogError):
                catalog.register_completed(plans[1])
            self.assertEqual(before, {p.name: p.read_bytes() for p in catalog.root.glob('*.json')})

    def test_different_series_with_distinct_roots_remain_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog = SeriesCatalog(root / 'series')
            plans = [completed_tv_plan(root / str(i), task_id=f'series-{i}',
                series_key=canonical_series_key(tmdb_tv(str(i))), season=1, provider=tmdb_tv(str(i)))
                for i in (100, 200)]
            for plan in plans:
                record = catalog.register_completed(plan)
                self.assertEqual(Path(plan.metadata['canonical_work_root']), record.canonical_root)
            self.assertEqual(2, len(list(catalog.root.glob('*.json'))))
